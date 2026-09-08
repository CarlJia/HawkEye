"""Telegram 通知。

使用 httpx 异步 POST 到 sendMessage；不使用 parse_mode（纯文本，避免
Markdown/HTML 转义问题）。可重试的错误（网络异常、5xx、429）按指数退避重试，
永久性拒绝（4xx）立即放弃；是否成功由布尔返回值告知，上层据此决定是否更新已
记录值（至少一次交付）。启动时另有 :meth:`Notifier.verify` 自检凭据与 chat_id。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Sequence

import httpx

from .config import TelegramConfig

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"
_MAX_ATTEMPTS = 3
_BASE_BACKOFF = 2.0
# Telegram 用 4xx 表达「请求本身不合法」：401/404 token 有误，400 多为 chat_id
# 不可达（对方从未 /start 过本 bot，或群已升级为超级群导致 id 变化），403 被封禁。
# 这些都不会因为再试一次而变好，重试只会拖慢轮询并刷掉真正的错误信息。
_FATAL_STATUS = frozenset({400, 401, 403, 404})


class TelegramFatalError(Exception):
    """Telegram 明确拒绝了凭据或目标会话，重试无意义。"""


def redact(text: str, secrets: Iterable[str]) -> str:
    """把 ``text`` 中每个非空 secret 全部替换为 ``<REDACTED>``。

    立场照 :class:`TokenRedactingFactory`：宁可多替换、不可漏；空串与 ``None``
    不参与替换（否则会把每个字符都插上打码串）。**纯函数**：不读不写任何外部
    状态、不修改入参，因此 :mod:`hawkeye.ssh` 远端输出打码与本模块日志脱敏
    共用同一个实现（KTD19）。``secrets`` 接受任意可迭代对象，调用方不需要
    临时构造列表。
    """
    out = text
    for secret in secrets:
        if not secret:
            continue
        out = out.replace(secret, "<REDACTED>")
    return out


class TokenRedactingFactory:
    """日志记录工厂：把每条 LogRecord 的 message 中出现的 bot token 替换为 <REDACTED>。

    用 setLogRecordFactory 注入（而非 handler.addFilter），因此：
    - 对所有 handler 路径均生效，包括运行后才 addHandler 的代码路径。
    - exc_info 中的异常消息同样经此工厂处理后才会被记录。

    替换逻辑下沉到 :func:`redact` 纯函数，与 :mod:`hawkeye.ssh` 远端输出
    打码共用一份实现（KTD19）。
    """

    def __init__(self, token: str, next_factory: Callable[..., logging.LogRecord]) -> None:
        self._token = token
        self._next = next_factory

    def __call__(self, *args: object, **kwargs: object) -> logging.LogRecord:
        record: logging.LogRecord = self._next(*args, **kwargs)
        if self._token:
            redacted = redact(record.getMessage(), (self._token,))
            if redacted != record.getMessage():
                record.msg = redacted
                record.args = ()
        # exc_info 里的 traceback.formattedException 也会输出敏感内容，
        # 但 exc_info 是不可变 tuple(None)，此处只能通过工厂层面脱敏 msg 来缓解。
        return record


def install_token_redaction(token: str) -> None:
    """给根 logger 装上 token 脱敏工厂（配置加载完成后调用一次）。

    工厂覆盖所有 handler 路径，包括运行后才 addHandler 的代码路径。
    """
    old_factory = logging.getLogRecordFactory()
    logging.setLogRecordFactory(TokenRedactingFactory(token, old_factory))


def _first_line(s: str) -> str:
    return s.splitlines()[0] if s else s


def format_change_message(name: str, old: str, new: str, when: str, url: str | None = None) -> str:
    """变更通知文本，包含 旧值 → 新值。``url`` 非空时附在最后一行（Telegram 客户端自动成链）。"""
    lines = [f"【变更】{name}", f"{old} → {new}", f"时间：{when}"]
    if url:
        lines.append(url)
    return "\n".join(lines)


def format_failure_message(
    name: str, threshold: int, reason: str, when: str, url: str | None = None
) -> str:
    """连续失败告警文本。``url`` 非空时附在最后一行（Telegram 客户端自动成链）。"""
    lines = [
        f"【异常】{name}",
        f"连续 {threshold} 次抓取失败",
        f"最近原因：{reason}",
        f"时间：{when}",
    ]
    if url:
        lines.append(url)
    return "\n".join(lines)


def format_new_post_message(title: str, url: str, when: str) -> str:
    """新帖通知文本：纯文本、裸 URL 由 Telegram 客户端自动成链，不做转义。"""
    return f"【新帖】{title}\n{url}\n时间：{when}"


class Notifier:
    """封装 Telegram 发送与重试。"""

    def __init__(self, telegram: TelegramConfig, client: httpx.AsyncClient) -> None:
        self._telegram = telegram
        self._client = client

    def _url(self, method: str) -> str:
        return _API.format(token=self._telegram.bot_token, method=method)

    async def verify(self) -> None:
        """启动前确认 bot_token 与 chat_id 可用。

        永久性拒绝直接抛 :class:`TelegramFatalError`（fail-closed）——一个发不出
        消息的监控进程等于没在监控。网络异常或 5xx 只告警，避免 Telegram 抖动时
        守护进程被 systemd 反复重启。
        """
        try:
            resp = await self._client.post(
                self._url("getChat"), json={"chat_id": self._telegram.chat_id}, timeout=20.0
            )
        except httpx.HTTPError as e:
            logger.warning("Telegram 连通性自检失败（网络异常），继续启动：%s", e)
            return
        if resp.status_code == 200:
            logger.info("Telegram 自检通过：chat_id %s 可达", self._telegram.chat_id)
            return
        if resp.status_code in _FATAL_STATUS:
            raise TelegramFatalError(
                f"Telegram 拒绝 chat_id {self._telegram.chat_id}："
                f"{resp.status_code} {_first_line(resp.text)}"
            )
        logger.warning(
            "Telegram 自检返回 %s，继续启动：%s", resp.status_code, _first_line(resp.text)
        )

    async def sync_commands(self, commands: Sequence[tuple[str, str]]) -> str:
        """把命令表同步到 Telegram 快捷菜单（客户端输入框旁那份命令列表）。

        先 getMyCommands 比对再决定要不要写：菜单没变时一次 setMyCommands 都不发。
        失败一律收敛成一句回执加一条告警，绝不外抛——快捷菜单只是输入便利，同步不上
        不该让监控进程起不来，用户随时可以再发 /menu 重试。
        """
        desired = [{"command": name, "description": text} for name, text in commands]
        try:
            if await self._current_commands() == desired:
                logger.info("Telegram 快捷菜单已是最新（%d 个命令）", len(desired))
                return f"快捷菜单已是最新，共 {len(desired)} 个命令。"
            resp = await self._client.post(
                self._url("setMyCommands"), json={"commands": desired}, timeout=20.0
            )
        except httpx.HTTPError as e:
            logger.warning("Telegram 快捷菜单同步失败（网络异常）：%s", e)
            return "快捷菜单同步失败：网络异常。稍后发送 /menu 可重试。"
        if resp.status_code != 200:
            logger.warning(
                "Telegram 快捷菜单同步失败（%s）：%s", resp.status_code, _first_line(resp.text)
            )
            return f"快捷菜单同步失败：Telegram 返回 {resp.status_code}。稍后发送 /menu 可重试。"
        logger.info("Telegram 快捷菜单已同步：%d 个命令", len(desired))
        return f"快捷菜单已同步，共 {len(desired)} 个命令。"

    async def _current_commands(self) -> list[dict[str, str]] | None:
        """读回 Telegram 端现有菜单用于比对；读不出来就返回 None，按「需要写入」处理。"""
        resp = await self._client.post(self._url("getMyCommands"), json={}, timeout=20.0)
        if resp.status_code != 200:
            return None
        try:
            payload = resp.json()
        except ValueError:
            return None
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, list):
            return None
        return [
            {"command": item["command"], "description": item["description"]}
            for item in result
            if isinstance(item, dict) and "command" in item and "description" in item
        ]

    async def send(self, text: str) -> bool:
        url = self._url("sendMessage")
        payload = {"chat_id": self._telegram.chat_id, "text": text}
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = await self._client.post(url, json=payload, timeout=20.0)
                if resp.status_code == 200:
                    return True
                if resp.status_code in _FATAL_STATUS:
                    logger.error(
                        "Telegram 拒绝请求（%s），重试无意义，请检查 bot_token 与 chat_id：%s",
                        resp.status_code,
                        _first_line(resp.text),
                    )
                    return False
                logger.warning(
                    "Telegram 返回非 200（第 %d 次）：%s %s",
                    attempt,
                    resp.status_code,
                    _first_line(resp.text),
                )
            except httpx.HTTPError as e:
                logger.warning("Telegram 请求异常（第 %d 次）：%s", attempt, e)
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_BASE_BACKOFF * attempt)
        return False
