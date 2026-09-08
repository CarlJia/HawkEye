"""Telegram 命令分发与引导式向导（控制面）。

把一条条纯文本命令翻译成对 config.toml 的原子改写与调度器的即时协调。三条纪律：

- **单锁串行。** 「读文件 → 变换 → 写回 → reconcile」全程在一把 :class:`asyncio.Lock`
  内完成（A1）；命令发得再快也不会有两条同时改配置，后写覆盖先写无从发生。
- **配置文件是唯一真相。** 每条命令先 :meth:`Controller.sync` 把外部手改同步进运行时
  （R14 / KTD12），再在最新内容上做变换；写入一律走 `configedit.write_config` 的
  「先验证、后替换」事务，失败时原文件字节不变。
- **会话只在内存。** 多轮向导的中间态随进程消失（KTD16），重启后半截的 `/add` 自然
  作废——配合接收层启动即丢积压，不会有隔夜命令诈尸。

回执一律纯文本、不带 parse_mode：URL 与选择器里的下划线、星号、方括号在 Markdown
解析下会被吞掉，甚至直接让 sendMessage 返回 400。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import (
    ConfigError,
    MonitoredElement,
    Page,
    WatchTarget,
    is_http_url,
    load_config,
    load_raw,
)
from .configedit import (
    EditError,
    add_element,
    add_watch,
    remove_element,
    remove_watch,
    write_config,
)
from .extract import normalize_text
from .fetch import FetchOk, ListFetched, PageFetched, PageLoadError
from .notify import Notifier
from .receive import Command
from .scheduler import KIND_ELEMENT, KIND_WATCH, MonitorRow, Scheduler, matches

logger = logging.getLogger(__name__)

_MAX_MESSAGE = 4096
_TRIAL = "试抓"
_YES = frozenset({"1", "y", "yes", "是", "确认", "确定", "保存"})
_TOP_COMMANDS = frozenset({"/help", "/add", "/list", "/del", "/menu"})

# 向导的各个等待步骤。做成常量而非散落的字面量，改名时不会漏掉分支而静默失效。
_PICK_KIND = "选类型"
_ELEMENT_URL = "元素URL"
_ELEMENT_SELECTOR = "元素选择器"
_ELEMENT_JS = "元素JS"
_ELEMENT_NAME = "元素名称"
_ELEMENT_JUMP_URL = "元素跳转URL"
_WATCH_URL = "列表URL"
_WATCH_SELECTOR = "链接选择器"
_WATCH_KEYWORDS = "关键词"
_WATCH_NAME = "列表名称"
_CONFIRM_SAVE = "确认保存"
_DEL_INDEX = "删除编号"
_DEL_CONFIRM = "删除确认"

# 快捷菜单与 /help 的唯一真相：命令名（不带斜杠，Telegram 的要求）加一句话描述。
# 新增命令只改这里，帮助文本与 Telegram 快捷菜单一起跟着变，不会各说各话。
MENU_COMMANDS: tuple[tuple[str, str], ...] = (
    ("add", "新建监控，按提示逐步填写（网页元素变更 / 论坛关键词）"),
    ("list", "列出全部监控及当前状态"),
    ("del", "按编号删除一个监控，需二次确认"),
    ("menu", "重新同步本快捷菜单"),
    ("help", "显示命令说明"),
    ("cancel", "取消进行中的操作"),
)

_HELP = "\n".join(
    [
        "HawkEye 控制命令：",
        *(f"/{name} —— {description}" for name, description in MENU_COMMANDS),
        "",
        "改动会直接写回 config.toml 并即时生效，无需重启。",
    ]
)

_PICK_KIND_PROMPT = (
    "要新建哪种监控？\n1 = 网页元素变更监控\n2 = 论坛关键词监控\n回复 1 或 2，或 /cancel 取消。"
)

_UNPARSABLE_REFUSAL = "配置文件当前不可解析，已拒绝写入。请先修好 config.toml 再重试。"


@dataclass
class Session:
    """一次多轮向导的中间态：正在等哪一步、已经收到什么（仅存内存，KTD16）。"""

    step: str
    kind: str = ""
    url: str = ""
    selector: str = ""
    js: str | None = None
    name: str | None = None
    element_url: str | None = None
    link_selector: str = ""
    keywords: tuple[str, ...] = ()
    rows: tuple[MonitorRow, ...] = ()
    target: MonitorRow | None = None


# ---- 纯文本辅助 ----


def _clip(text: str, limit: int = 60) -> str:
    """压掉换页与连续空白再截断：元素当前值可能是一整段描述，会把回执撑爆。"""
    flat = normalize_text(text)
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _split_keywords(text: str) -> tuple[str, ...]:
    """空格、逗号、中文逗号、顿号皆可作分隔符。"""
    normalized = text.replace("，", " ").replace(",", " ").replace("、", " ")
    return tuple(part for part in normalized.split() if part)


def _parse_index(text: str, total: int) -> int | None:
    raw = text.lstrip("#").strip()
    if not raw.isdigit():
        return None
    index = int(raw)
    return index if 1 <= index <= total else None


def _format_rows(rows: Sequence[MonitorRow]) -> list[str]:
    return [
        f"#{i} [{row.kind}] {row.identity} — {row.url} — {_clip(row.status)}"
        for i, row in enumerate(rows, start=1)
    ]


def _segment(lines: Sequence[str]) -> list[str]:
    """按 Telegram 单条 4096 字符上限把多行拆成若干条消息（A6）。"""
    messages: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        extra = len(line) + (1 if current else 0)
        if current and size + extra > _MAX_MESSAGE:
            messages.append("\n".join(current))
            current, size = [line], len(line)
            continue
        current.append(line)
        size += extra
    if current:
        messages.append("\n".join(current))
    return messages


class Controller:
    """把授权会话的命令翻译成配置改写与运行时协调。"""

    def __init__(self, config_path: str | Path, scheduler: Scheduler, notifier: Notifier) -> None:
        self._path = Path(config_path)
        self._scheduler = scheduler
        self._notifier = notifier
        self._lock = asyncio.Lock()
        self._session: Session | None = None
        # sync() 发现文件不可解析时置 False：此时绝不写入，否则等于拿破损内容当基线覆盖。
        self._writable = True

    async def handle(self, command: Command) -> None:
        """处理一条命令并把回执发回 Telegram；全过程串行（A1）。"""
        async with self._lock:
            try:
                replies = await self._dispatch(command.text.strip())
            except Exception:  # noqa: BLE001 —— 单条命令出错不该拖垮接收循环
                logger.exception("处理命令失败：%s", command.text)
                self._session = None
                replies = ["处理命令时出现内部错误，操作未完成。请稍后重试或查看日志。"]
            for reply in replies:
                await self._notifier.send(reply)

    async def sync(self) -> str | None:
        """把外部手改同步进运行时（R14 / KTD12）。

        返回 None 表示磁盘与运行时一致；返回一段文本表示文件当前不可解析，此时命令
        照常执行并输出运行中的集合，但写入被拒（`_writable`）。
        """
        try:
            new_config = load_config(self._path)
        except ConfigError as e:
            self._writable = False
            logger.warning("配置文件当前不可解析：%s", e)
            return f"注意：配置文件当前无法解析（{e}）。以下为运行中的配置，且已暂停写入。"
        self._writable = True
        if new_config != self._scheduler.config:
            added, removed = await self._scheduler.reconcile(new_config)
            logger.info("配置文件已被外部修改，已同步：新增 %d、移除 %d", added, removed)
        return None

    async def _dispatch(self, text: str) -> list[str]:
        keyword = text.split(maxsplit=1)[0].lower() if text else ""

        if keyword == "/cancel":
            cancelled = self._session is not None
            self._session = None
            return ["已取消当前操作。" if cancelled else "当前没有进行中的操作。"]

        if keyword in _TOP_COMMANDS:
            prefix: list[str] = []
            if self._session is not None:
                # A3：顶层命令即视为放弃上一个向导，不再追问「确定吗」，但必须明确告知。
                self._session = None
                prefix.append("已取消上一个未完成的操作。")
            if keyword == "/menu":
                # 快捷菜单与 config.toml 无关，不必为它做一次配置同步。
                return prefix + [await self._notifier.sync_commands(MENU_COMMANDS)]
            warning = await self.sync()
            if warning is not None:
                prefix.append(warning)
            if keyword == "/help":
                return prefix + [_HELP]
            if keyword == "/list":
                return prefix + self._list()
            if keyword == "/add":
                self._session = Session(step=_PICK_KIND)
                return prefix + [_PICK_KIND_PROMPT]
            if keyword == "/del":
                return prefix + self._del_prompt()
            # A1：超出此处的 keyword 不可能再进入这里（已在上面的 _TOP_COMMANDS 过滤）
            return prefix

        session = self._session
        if session is None:
            return ["没有进行中的操作。发送 /help 查看用法。"]
        # 会话进行中一律当步骤输入：XPath 选择器就以 / 开头，不能按命令拦下来。
        return await self._step(session, text)

    # ---- /list 与 /del 的列表输出 ----

    def _list(self) -> list[str]:
        rows = self._scheduler.snapshot()
        if not rows:
            return ["当前没有任何监控。发送 /add 添加第一个。"]
        return _segment([f"共 {len(rows)} 个监控：", *_format_rows(rows)])

    def _del_prompt(self) -> list[str]:
        rows = self._scheduler.snapshot()
        if not rows:
            return ["当前没有任何监控可删除。"]
        self._session = Session(step=_DEL_INDEX, rows=rows)
        return _segment([*_format_rows(rows), "请回复要删除的编号，或 /cancel 取消。"])

    # ---- 向导状态机 ----

    async def _step(self, session: Session, text: str) -> list[str]:
        if session.step == _PICK_KIND:
            if text == "1":
                session.kind = KIND_ELEMENT
                session.step = _ELEMENT_URL
                return ["请发送要监控的页面 URL（http 或 https 开头）。"]
            if text == "2":
                session.kind = KIND_WATCH
                session.step = _WATCH_URL
                return ["请发送要监控的列表页 URL（http 或 https 开头）。"]
            return ["请回复 1（网页元素变更）或 2（论坛关键词），或 /cancel 取消。"]

        if session.step in (_ELEMENT_URL, _WATCH_URL):
            if not is_http_url(text):
                return ["这不像一个 http(s) 地址，请重新发送完整 URL。"]
            session.url = text
            if session.step == _ELEMENT_URL:
                session.step = _ELEMENT_SELECTOR
                return ["请发送元素选择器，CSS 与 XPath 自动识别，例如 #price 或 //span[@id='p']。"]
            session.step = _WATCH_SELECTOR
            return ["请发送帖子链接的选择器，例如 .topic-list a.title。"]

        if session.step == _ELEMENT_SELECTOR:
            session.selector = text
            session.step = _ELEMENT_JS
            return [
                "输入 JS 表达式（可选，直接回车或输入 - 跳过）\n"
                "例：el => el.classList.contains('disabled') ? '售罄' : '可订'"
            ]

        if session.step == _ELEMENT_JS:
            session.js = None if text in ("", "-") else text
            session.step = _ELEMENT_NAME
            return ["给它起个名字，方便以后在 /list 里认出来；回复 - 表示直接用选择器当名字。"]

        if session.step == _ELEMENT_JUMP_URL:
            if text in ("", "-"):
                session.element_url = None
            else:
                if not is_http_url(text):
                    return ["这不像一个 http(s) 地址，请重新发送完整 URL，或直接回车/- 跳过。"]
                session.element_url = text
            if session.kind == KIND_ELEMENT:
                return await self._trial_element(session)
            return await self._trial_watch(session)

        if session.step == _WATCH_SELECTOR:
            session.link_selector = text
            session.step = _WATCH_KEYWORDS
            return ["请发送关键词，多个用空格或逗号分隔（不区分大小写，命中任一即通知）。"]

        if session.step == _WATCH_KEYWORDS:
            keywords = _split_keywords(text)
            if not keywords:
                return ["至少需要一个关键词，请重新发送。"]
            session.keywords = keywords
            session.step = _WATCH_NAME
            return ["给它起个名字，方便以后在 /list 里认出来；回复 - 表示直接用列表页 URL 当名字。"]

        if session.step in (_ELEMENT_NAME, _WATCH_NAME):
            if session.step == _WATCH_NAME and text != "-":
                if f"watch / {text}" in {row.identity for row in self._scheduler.snapshot()}:
                    return ["这个名字已被现有监控占用，请换一个，或回复 - 用列表页 URL 当名字。"]
            session.name = None if text == "-" else text
            if session.step == _ELEMENT_NAME:
                session.step = _ELEMENT_JUMP_URL
                return [
                    "请发送跳转 URL（可选，直接回车或 - 跳过；"
                    "留空时用页面 URL 推送通知，点击消息直跳目标下单/详情页）。"
                ]
            return await self._trial_watch(session)

        if session.step == _CONFIRM_SAVE:
            if text.lower() in _YES:
                return await self._save(session)
            self._session = None
            return ["已取消，未保存任何内容。"]

        if session.step == _DEL_INDEX:
            return self._pick_target(session, text)

        # 只剩 _DEL_CONFIRM：删除是不可逆操作，除明确确认外一概按取消处理。
        if text.lower() in _YES:
            return await self._delete(session)
        self._session = None
        return ["已取消，未删除任何监控。"]

    def _pick_target(self, session: Session, text: str) -> list[str]:
        index = _parse_index(text, len(session.rows))
        if index is None:
            self._session = None
            return [f"编号无效（当前共 {len(session.rows)} 项）。请重新发送 /del 查看最新列表。"]
        target = session.rows[index - 1]
        session.target = target
        session.step = _DEL_CONFIRM
        return [
            f"将要删除 #{index} [{target.kind}] {target.identity}\n{target.url}\n"
            "回复 1 确认删除，回复其他内容取消。"
        ]

    # ---- 试抓：保存前先看一眼到底抓到了什么（A4 / R7） ----

    def _temp_page(self, session: Session) -> Page:
        """拼一个一次性 Page 用于试抓；轮询类参数取全局默认，只求跑通一次抓取。"""
        cfg = self._scheduler.config
        element = MonitoredElement(
            merchant_name=_TRIAL,
            page_name=_TRIAL,
            name=session.name or session.selector,
            selector=session.selector,
            selector_type="auto",
            nth=None,
            js=session.js,
            url=session.element_url,
        )
        return Page(
            merchant_name=_TRIAL,
            name=_TRIAL,
            url=session.url,
            poll_interval_secs=cfg.poll_interval_secs,
            wait_until=cfg.wait_until,
            nav_timeout_secs=cfg.nav_timeout_secs,
            failure_threshold=cfg.failure_threshold,
            elements=(element,),
        )

    def _temp_watch(self, session: Session) -> WatchTarget:
        cfg = self._scheduler.config
        return WatchTarget(
            name=session.name or _TRIAL,
            url=session.url,
            link_selector=session.link_selector,
            selector_type="auto",
            keywords=session.keywords,
            id_pattern=None,
            poll_interval_secs=cfg.poll_interval_secs,
            wait_until=cfg.wait_until,
            nav_timeout_secs=cfg.nav_timeout_secs,
            failure_threshold=cfg.failure_threshold,
        )

    async def _trial_element(self, session: Session) -> list[str]:
        result = await self._scheduler.trial_fetch_page(self._temp_page(session))
        if isinstance(result, PageLoadError):
            return self._ask_anyway(
                session, f"试抓失败：页面打不开（{_clip(result.reason, 120)}）。"
            )
        assert isinstance(result, PageFetched)
        _element, outcome = result.results[0]
        if isinstance(outcome, FetchOk):
            return await self._save(session, f"试抓成功，当前取到：{_clip(outcome.value, 200)}")
        return self._ask_anyway(session, f"试抓没取到值：{_clip(outcome.reason, 120)}。")

    async def _trial_watch(self, session: Session) -> list[str]:
        result = await self._scheduler.trial_fetch_list(self._temp_watch(session))
        if isinstance(result, PageLoadError):
            return self._ask_anyway(
                session, f"试抓失败：列表页打不开（{_clip(result.reason, 120)}）。"
            )
        assert isinstance(result, ListFetched)
        if not result.items:
            return self._ask_anyway(session, "试抓没提取到任何链接，链接选择器可能不对。")
        hits = sum(1 for item in result.items if matches(session.keywords, item.title))
        note = f"试抓成功：找到 {len(result.items)} 条链接，其中 {hits} 条命中关键词。"
        if hits == 0:
            return self._ask_anyway(
                session, f"{note}\n当前没有命中项，也可能只是暂时没有符合的帖子。"
            )
        return await self._save(session, note)

    def _ask_anyway(self, session: Session, reason: str) -> list[str]:
        """试抓不理想时不擅自决定：AE3 要求让用户在「仍然保存」和「取消」之间选。"""
        session.step = _CONFIRM_SAVE
        return [f"{reason}\n回复 1 仍然保存，回复其他内容取消。"]

    # ---- 落盘：写事务 + 立即协调运行时 ----

    async def _save(self, session: Session, note: str = "") -> list[str]:
        self._session = None
        prefix = f"{note}\n" if note else ""
        if not self._writable:
            return [f"{prefix}{_UNPARSABLE_REFUSAL}"]
        try:
            raw = load_raw(self._path)
            if session.kind == KIND_ELEMENT:
                new_raw = add_element(
                    raw,
                    session.url,
                    session.selector,
                    name=session.name,
                    js=session.js,
                    element_url=session.element_url,
                )
            else:
                new_raw = add_watch(
                    raw,
                    session.url,
                    session.link_selector,
                    session.keywords,
                    name=session.name,
                )
            config = write_config(self._path, new_raw)
        except (ConfigError, EditError) as e:
            logger.warning("保存失败：%s", e)
            return [f"{prefix}保存失败：{e}\nconfig.toml 未被改动。"]
        try:
            await self._scheduler.reconcile(config)
        except Exception:
            logger.error("运行时热重载失败，config.toml 已变更但调度器仍用旧配置，请重启进程。")
            raise
        return [f"{prefix}已保存并即时生效。"]

    async def _delete(self, session: Session) -> list[str]:
        self._session = None
        target = session.target
        assert target is not None
        if not self._writable:
            return [_UNPARSABLE_REFUSAL]
        try:
            raw = load_raw(self._path)
            if target.kind == KIND_ELEMENT:
                new_raw = remove_element(raw, target.identity)
            else:
                new_raw = remove_watch(raw, target.identity)
            config = write_config(self._path, new_raw)
        except (ConfigError, EditError) as e:
            logger.warning("删除失败：%s", e)
            return [f"删除失败：{e}\nconfig.toml 未被改动。请重新发送 /del 查看最新列表。"]
        try:
            await self._scheduler.reconcile(config)
        except Exception:
            logger.error("运行时热重载失败，config.toml 已变更但调度器仍用旧配置，请重启进程。")
            raise
        return [f"已删除 {target.identity}，即时生效。"]
