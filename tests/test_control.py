"""control 模块测试：四条命令闭环、试抓回显、二次确认与一致性同步（假调度器 + 真配置文件）。"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path

from hawkeye.config import Config, MonitoredElement, Page, WatchTarget, load_config
from hawkeye.control import MENU_COMMANDS, Controller
from hawkeye.extract import ListItem
from hawkeye.fetch import (
    FetchNoMatch,
    FetchOk,
    ListFetched,
    ListResult,
    PageFetched,
    PageLoadError,
    PageResult,
)
from hawkeye.receive import Command
from hawkeye.scheduler import KIND_ELEMENT, KIND_WATCH, MonitorRow

_BASE = """
state_path = "state.json"

[telegram]
bot_token = "t"
chat_id = "c"

[[merchants]]
name = "yunyoo"

[[merchants.pages]]
name = "购物车"
url = "https://yunyoo.cc/cart"

[[merchants.pages.elements]]
name = "商品A"
selector = "#a"
"""

_WATCH_BLOCK = """
[[watches]]
url = "https://ns.com/"
link_selector = "a"
keywords = ["hk"]
"""

_ONLY_TELEGRAM = """
[telegram]
bot_token = "t"
chat_id = "c"
"""

_ELEMENT = MonitoredElement(
    merchant_name="试抓",
    page_name="试抓",
    name="x",
    selector="#x",
    selector_type="auto",
    nth=None,
)


class _FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.synced: list[tuple[tuple[str, str], ...]] = []

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return True

    async def sync_commands(self, commands: Sequence[tuple[str, str]]) -> str:
        self.synced.append(tuple(commands))
        return "快捷菜单已同步。"


class _FakeScheduler:
    """替身调度器：只提供控制面用到的四个接口，免得真起后台轮询任务干扰断言。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.reconciled: list[Config] = []
        self.page_result: PageResult | None = None
        self.list_result: ListResult | None = None
        self.trial_pages: list[Page] = []
        self.trial_watches: list[WatchTarget] = []

    def snapshot(self) -> tuple[MonitorRow, ...]:
        rows = [
            MonitorRow(kind=KIND_ELEMENT, identity=e.identity, url=p.url, status="尚未建立基线")
            for p in self.config.pages
            for e in p.elements
        ]
        rows += [
            MonitorRow(kind=KIND_WATCH, identity=w.identity, url=w.url, status="已见 3 帖")
            for w in self.config.watches
        ]
        return tuple(rows)

    async def reconcile(self, new_config: Config) -> tuple[int, int]:
        self.reconciled.append(new_config)
        self.config = new_config
        return (0, 0)

    async def trial_fetch_page(self, page: Page) -> PageResult:
        self.trial_pages.append(page)
        assert self.page_result is not None
        return self.page_result

    async def trial_fetch_list(self, watch: WatchTarget) -> ListResult:
        self.trial_watches.append(watch)
        assert self.list_result is not None
        return self.list_result


def _setup(
    tmp_path: Path, content: str = _BASE
) -> tuple[Controller, _FakeScheduler, _FakeNotifier]:
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    os.chmod(path, 0o600)
    scheduler = _FakeScheduler(load_config(path))
    notifier = _FakeNotifier()
    return Controller(path, scheduler, notifier), scheduler, notifier  # type: ignore[arg-type]


async def _send(controller: Controller, *texts: str) -> None:
    for text in texts:
        await controller.handle(Command(text=text))


def _page_ok(value: str) -> PageFetched:
    return PageFetched(results=((_ELEMENT, FetchOk(value=value)),))


# ---- /help 与无会话闲聊 ----


async def test_help_lists_all_commands(tmp_path: Path) -> None:
    """/help 与快捷菜单共用一张表：表里的每条命令连描述一起出现，二者不可能漂移。"""
    controller, _, notifier = _setup(tmp_path)
    await _send(controller, "/help")

    assert len(notifier.sent) == 1
    for name, description in MENU_COMMANDS:
        assert f"/{name}" in notifier.sent[0]
        assert description in notifier.sent[0]


def test_menu_commands_satisfy_telegram_constraints() -> None:
    """setMyCommands 只收小写字母/数字/下划线的命令名与 1-256 字符描述，越界整批被拒。"""
    for name, description in MENU_COMMANDS:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", name), f"命令名不合法：{name}"
        assert 1 <= len(description) <= 256, f"描述长度越界：{name}"


async def test_plain_text_without_session_points_to_help(tmp_path: Path) -> None:
    controller, _, notifier = _setup(tmp_path)
    await _send(controller, "随便说点什么")

    assert notifier.sent == ["没有进行中的操作。发送 /help 查看用法。"]


# ---- /menu 快捷菜单同步 ----


async def test_menu_pushes_the_whole_command_table(tmp_path: Path) -> None:
    """/menu 把整张命令表交给 Telegram，并把同步回执原样转达。"""
    controller, _, notifier = _setup(tmp_path)
    await _send(controller, "/menu")

    assert notifier.synced == [MENU_COMMANDS]
    assert notifier.sent == ["快捷菜单已同步。"]


async def test_menu_is_a_top_level_command(tmp_path: Path) -> None:
    """向导进行中发 /menu 按顶层命令处理，不能被当成 URL 之类的步骤输入吞掉。"""
    controller, _, notifier = _setup(tmp_path)
    await _send(controller, "/add", "/menu")

    assert notifier.synced == [MENU_COMMANDS]
    assert notifier.sent[-2] == "已取消上一个未完成的操作。"


async def test_menu_ignores_broken_config(tmp_path: Path) -> None:
    """快捷菜单与 config.toml 无关：文件坏了照样能同步，回执里不夹带配置告警。"""
    controller, _, notifier = _setup(tmp_path)
    (tmp_path / "config.toml").write_text("这不是 toml [[[", encoding="utf-8")
    await _send(controller, "/menu")

    assert notifier.synced == [MENU_COMMANDS]
    assert notifier.sent == ["快捷菜单已同步。"]


# ---- /add：元素监控 ----


async def test_add_element_end_to_end_writes_file_and_reconciles(tmp_path: Path) -> None:
    # AE2：URL 未命中已有页面 → 新建以 host 命名的商家承载它。
    controller, scheduler, notifier = _setup(tmp_path)
    scheduler.page_result = _page_ok("¥99")

    await _send(controller, "/add", "1", "https://yunyoo.cc/new", "#price", "-", "价格", "-")

    assert "¥99" in notifier.sent[-1]
    assert "已保存并即时生效" in notifier.sent[-1]
    assert len(scheduler.reconciled) == 1

    cfg = load_config(tmp_path / "config.toml")
    assert [m.name for m in cfg.merchants] == ["yunyoo", "yunyoo.cc"]
    identities = [e.identity for p in cfg.pages for e in p.elements]
    assert "yunyoo.cc / https://yunyoo.cc/new / 价格" in identities


async def test_add_element_merges_into_existing_page(tmp_path: Path) -> None:
    # AE1：URL 命中已有页面 → 并入该页面，不新建商家 / 页面。
    controller, scheduler, _ = _setup(tmp_path)
    scheduler.page_result = _page_ok("¥1")

    await _send(controller, "/add", "1", "https://yunyoo.cc/cart", "#b", "-", "-", "-")

    cfg = load_config(tmp_path / "config.toml")
    assert len(cfg.merchants) == 1
    assert len(cfg.pages) == 1
    assert [e.identity for e in cfg.pages[0].elements] == [
        "yunyoo / 购物车 / 商品A",
        "yunyoo / 购物车 / #b",  # 回复 - 表示不起名，标识回退为选择器
    ]


async def test_invalid_url_is_rejected_without_advancing(tmp_path: Path) -> None:
    controller, _, notifier = _setup(tmp_path)

    await _send(controller, "/add", "1", "yunyoo.cc/cart")
    assert "http(s)" in notifier.sent[-1]

    await _send(controller, "https://yunyoo.cc/cart")
    assert "选择器" in notifier.sent[-1]  # 步骤没被推进，补上合法 URL 后照常继续


async def test_xpath_selector_is_not_mistaken_for_a_command(tmp_path: Path) -> None:
    # XPath 以 / 开头，若按命令前缀拦截就永远填不进去。
    controller, scheduler, notifier = _setup(tmp_path)
    scheduler.page_result = _page_ok("¥5")

    await _send(controller, "/add", "1", "https://e.com/p", "//span[@id='p']", "-", "-", "-")

    assert scheduler.trial_pages[-1].elements[0].selector == "//span[@id='p']"
    assert "已保存并即时生效" in notifier.sent[-1]


async def test_trial_miss_offers_choice_and_saves_when_confirmed(tmp_path: Path) -> None:
    # AE3：试抓没命中不代表用户填错，必须给「仍然保存」的出口。
    controller, scheduler, notifier = _setup(tmp_path)
    scheduler.page_result = PageFetched(
        results=((_ELEMENT, FetchNoMatch(reason="选择器匹配到元素，但其文本为空")),)
    )

    await _send(controller, "/add", "1", "https://e.com/p", "#nope", "-", "-", "-")
    # 控制面转达抓取层给的原话，不在这里另编一套说法——否则用户拿着假诊断去改错地方
    assert "选择器匹配到元素，但其文本为空" in notifier.sent[-1]
    assert "仍然保存" in notifier.sent[-1]
    assert scheduler.reconciled == []

    await _send(controller, "1")
    assert "已保存并即时生效" in notifier.sent[-1]
    assert load_config(tmp_path / "config.toml").element_count == 2


async def test_trial_failure_cancel_leaves_file_untouched(tmp_path: Path) -> None:
    controller, scheduler, notifier = _setup(tmp_path)
    scheduler.page_result = PageLoadError(reason="导航超时")
    before = (tmp_path / "config.toml").read_bytes()

    await _send(controller, "/add", "1", "https://e.com/p", "#x", "-", "-", "-", "2")

    assert "导航超时" in notifier.sent[-2]
    assert notifier.sent[-1] == "已取消，未保存任何内容。"
    assert (tmp_path / "config.toml").read_bytes() == before
    assert scheduler.reconciled == []


async def test_add_element_with_jump_url_persists_url_field(tmp_path: Path) -> None:
    # 跳转 URL 是新增的「元素 url」字段：填写后落到 config.toml,留空时不写该键
    controller, scheduler, notifier = _setup(tmp_path)
    scheduler.page_result = _page_ok("¥99")

    await _send(
        controller,
        "/add",
        "1",
        "https://e.com/p",
        "#price",
        "-",
        "价格",
        "https://e.com/order/123",
    )

    assert "已保存并即时生效" in notifier.sent[-1]
    cfg = load_config(tmp_path / "config.toml")
    # 新页面是 e.com / https://e.com/p,在 yunyoo 之后追加;按 identity 锁定新建的元素
    new_identity = "e.com / https://e.com/p / 价格"
    el = next(e for p in cfg.pages for e in p.elements if e.identity == new_identity)
    assert el.url == "https://e.com/order/123"
    # 试抓用的临时 Page 也带上 url,保证 trial 路径与最终落地一致
    assert scheduler.trial_pages[-1].elements[0].url == "https://e.com/order/123"


async def test_add_element_skip_jump_url_omits_field(tmp_path: Path) -> None:
    # 跳转 URL 留空 → 不写 url 键,调度层会回退到 page.url
    controller, scheduler, _ = _setup(tmp_path)
    scheduler.page_result = _page_ok("¥1")

    await _send(controller, "/add", "1", "https://e.com/p", "#price", "-", "价格", "-")

    cfg = load_config(tmp_path / "config.toml")
    new_identity = "e.com / https://e.com/p / 价格"
    el = next(e for p in cfg.pages for e in p.elements if e.identity == new_identity)
    assert el.url is None


async def test_add_element_invalid_jump_url_keeps_wizard(tmp_path: Path) -> None:
    # 跳转 URL 非 http(s) → 拒绝但不推进,用户可重输或回退
    controller, scheduler, notifier = _setup(tmp_path)
    scheduler.page_result = _page_ok("¥1")

    await _send(controller, "/add", "1", "https://e.com/p", "#price", "-", "价格")
    # 此时进入 JUMP_URL 步骤,首条回复是跳转 URL 提示
    assert "跳转 URL" in notifier.sent[-1]

    await _send(controller, "not-a-url")
    assert "http(s)" in notifier.sent[-1]
    # 还在 JUMP_URL 步骤,scheduler 还没被试抓
    assert scheduler.trial_pages == []


# ---- /add：论坛关键词监控 ----


async def test_add_watch_end_to_end_reports_hit_count(tmp_path: Path) -> None:
    controller, scheduler, notifier = _setup(tmp_path)
    scheduler.list_result = ListFetched(
        items=(
            ListItem(post_id="1", title="出售 HK 节点", url="https://ns.com/1"),
            ListItem(post_id="2", title="收 VPS", url="https://ns.com/2"),
            ListItem(post_id="3", title="hk 便宜机", url="https://ns.com/3"),
        )
    )

    await _send(controller, "/add", "2", "https://ns.com/", ".title a", "hk、香港", "-")

    assert "找到 3 条链接，其中 2 条命中关键词" in notifier.sent[-1]
    assert "已保存并即时生效" in notifier.sent[-1]
    assert len(scheduler.reconciled) == 1

    cfg = load_config(tmp_path / "config.toml")
    assert len(cfg.watches) == 1
    assert cfg.watches[0].keywords == ("hk", "香港")
    assert cfg.watches[0].link_selector == ".title a"
    assert cfg.watches[0].identity == "watch / https://ns.com/"  # 回复 - 表示不起名
    assert cfg.element_count == 1  # 原有的元素监控没被动过


async def test_add_watch_asks_for_name_before_trial(tmp_path: Path) -> None:
    # 名称是最后一步：收齐之前不该先去试抓，否则用户改名还得重抓一次。
    controller, scheduler, notifier = _setup(tmp_path)

    await _send(controller, "/add", "2", "https://ns.com/", "a", "hk")

    assert "起个名字" in notifier.sent[-1]
    assert scheduler.trial_watches == []


async def test_add_watch_with_name_uses_it_as_identity(tmp_path: Path) -> None:
    controller, scheduler, notifier = _setup(tmp_path)
    scheduler.list_result = ListFetched(
        items=(ListItem(post_id="1", title="出售 HK 节点", url="https://ns.com/1"),)
    )

    await _send(controller, "/add", "2", "https://ns.com/", "a", "hk", "NS 交易区")

    assert "已保存并即时生效" in notifier.sent[-1]
    # 试抓也用上这个名字，日志里能对上号
    assert scheduler.trial_watches[-1].name == "NS 交易区"
    cfg = load_config(tmp_path / "config.toml")
    assert cfg.watches[0].identity == "watch / NS 交易区"


async def test_add_watch_without_hits_asks_before_saving(tmp_path: Path) -> None:
    controller, scheduler, notifier = _setup(tmp_path)
    scheduler.list_result = ListFetched(
        items=(ListItem(post_id="1", title="无关帖子", url="https://ns.com/1"),)
    )
    before = (tmp_path / "config.toml").read_bytes()

    await _send(controller, "/add", "2", "https://ns.com/", "a", "hk", "-")

    assert "找到 1 条链接，其中 0 条命中关键词" in notifier.sent[-1]
    assert "仍然保存" in notifier.sent[-1]
    assert (tmp_path / "config.toml").read_bytes() == before


# ---- /list ----


async def test_list_empty_hints_add(tmp_path: Path) -> None:
    controller, _, notifier = _setup(tmp_path, _ONLY_TELEGRAM)
    await _send(controller, "/list")

    assert notifier.sent == ["当前没有任何监控。发送 /add 添加第一个。"]


async def test_list_row_carries_kind_name_url_and_status(tmp_path: Path) -> None:
    controller, _, notifier = _setup(tmp_path, _BASE + _WATCH_BLOCK)
    await _send(controller, "/list")

    assert notifier.sent == [
        "共 2 个监控：\n"
        "#1 [元素] yunyoo / 购物车 / 商品A — https://yunyoo.cc/cart — 尚未建立基线\n"
        "#2 [论坛] watch / https://ns.com/ — https://ns.com/ — 已见 3 帖"
    ]


async def test_list_segments_output_under_telegram_limit(tmp_path: Path) -> None:
    # A6：条目多到超过单条 4096 字符时必须分段，否则 sendMessage 直接 400。
    extra = "\n".join(
        f'[[merchants.pages.elements]]\nname = "商品{i:03d}"\nselector = "#s{i}"'
        for i in range(120)
    )
    controller, _, notifier = _setup(tmp_path, f"{_BASE}\n{extra}")

    await _send(controller, "/list")

    assert len(notifier.sent) > 1
    assert all(len(message) <= 4096 for message in notifier.sent)
    assert sum(message.count("[元素]") for message in notifier.sent) == 121
    assert "#121 [元素]" in notifier.sent[-1]


# ---- /del ----


async def test_del_removes_entry_and_prunes_empty_page(tmp_path: Path) -> None:
    # AE4：删掉页面下最后一个元素 → 空页面与随之变空的商家一并消失。
    controller, scheduler, notifier = _setup(tmp_path)

    await _send(controller, "/del", "1", "1")

    assert "#1 [元素] yunyoo / 购物车 / 商品A" in notifier.sent[0]
    assert "将要删除 #1" in notifier.sent[1]
    assert notifier.sent[-1] == "已删除 yunyoo / 购物车 / 商品A，即时生效。"
    assert len(scheduler.reconciled) == 1

    cfg = load_config(tmp_path / "config.toml")
    assert cfg.merchants == ()
    assert cfg.element_count == 0


async def test_del_cancel_leaves_file_untouched(tmp_path: Path) -> None:
    controller, scheduler, notifier = _setup(tmp_path)
    before = (tmp_path / "config.toml").read_bytes()

    await _send(controller, "/del", "1", "/cancel")

    assert notifier.sent[-1] == "已取消当前操作。"
    assert (tmp_path / "config.toml").read_bytes() == before
    assert scheduler.reconciled == []


async def test_del_index_out_of_range_asks_to_retry(tmp_path: Path) -> None:
    controller, scheduler, notifier = _setup(tmp_path)
    before = (tmp_path / "config.toml").read_bytes()

    await _send(controller, "/del", "9")

    assert "编号无效" in notifier.sent[-1]
    assert (tmp_path / "config.toml").read_bytes() == before
    assert scheduler.reconciled == []


async def test_del_second_confirmation_is_required(tmp_path: Path) -> None:
    # 删除不可逆：除明确确认外一律按取消处理。
    controller, scheduler, notifier = _setup(tmp_path)
    before = (tmp_path / "config.toml").read_bytes()

    await _send(controller, "/del", "1", "算了")

    assert notifier.sent[-1] == "已取消，未删除任何监控。"
    assert (tmp_path / "config.toml").read_bytes() == before
    assert scheduler.reconciled == []


# ---- 一致性同步（R14 / KTD12） ----


async def test_external_edit_is_synced_before_command(tmp_path: Path) -> None:
    controller, scheduler, notifier = _setup(tmp_path)
    (tmp_path / "config.toml").write_text(_BASE + _WATCH_BLOCK, encoding="utf-8")

    await _send(controller, "/list")

    assert len(scheduler.reconciled) == 1
    assert "watch / https://ns.com/" in notifier.sent[-1]


async def test_unparsable_config_lists_running_set_with_warning(tmp_path: Path) -> None:
    controller, scheduler, notifier = _setup(tmp_path)
    (tmp_path / "config.toml").write_text("这不是 TOML {{{", encoding="utf-8")

    await _send(controller, "/list")

    assert "无法解析" in notifier.sent[0]
    assert "yunyoo / 购物车 / 商品A" in notifier.sent[1]
    assert scheduler.reconciled == []


async def test_unparsable_config_refuses_to_write(tmp_path: Path) -> None:
    controller, scheduler, notifier = _setup(tmp_path)
    scheduler.page_result = _page_ok("¥1")
    (tmp_path / "config.toml").write_text("坏文件 {{{", encoding="utf-8")
    broken = (tmp_path / "config.toml").read_bytes()

    await _send(controller, "/add", "1", "https://e.com/p", "#x", "-", "-", "-")

    assert "已拒绝写入" in notifier.sent[-1]
    assert (tmp_path / "config.toml").read_bytes() == broken
    assert scheduler.reconciled == []


async def test_top_level_command_discards_in_progress_wizard(tmp_path: Path) -> None:
    # A3：不追问「确定要放弃吗」，但必须明确告知旧向导已作废。
    controller, _, notifier = _setup(tmp_path)

    await _send(controller, "/add", "1", "/list")

    assert notifier.sent[-2] == "已取消上一个未完成的操作。"
    assert "共 1 个监控" in notifier.sent[-1]
