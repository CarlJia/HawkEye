"""Telegram 命令接收（getUpdates 长轮询）。

只做三件事：长轮询取回 update、把**授权会话**的文本消息转成 :class:`Command`、
在异常时退避重试而不拖垮进程。命令语义一概不懂，交给上层分发。

两条容易踩的坑，都在本层挡掉：

- **游标必须无条件推进。** 被忽略的消息（非授权来源、非文本）如果不推进 offset，
  Telegram 每轮都会把同一条 update 再发一遍，长轮询从此永久卡死。
- **启动先丢积压（KTD16）。** 守护进程重启后不该去执行几小时前的 `/add`，更不该
  重放一条 `/del` 的确认。`offset=-1` 只取最后一条 update，把游标推到它之后即等于
  跳过全部积压。

token 脱敏由 `notify.install_token_redaction` 装的全局过滤器覆盖（httpx 会把含
token 的 URL 记进 INFO 日志），本模块无需另做处理。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .notify import TelegramFatalError

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"
_LONG_POLL_SECS = 30
_BASE_BACKOFF = 2.0
_MAX_BACKOFF = 60.0
# 401/404 token 有误、403 被封禁：重试不会变好，交给 __main__ 走退出码 2。
# 与 notify 不同，这里不含 400——getUpdates 的 400 多是参数/游标问题，退避重试有意义。
_FATAL_STATUS = frozenset({401, 403, 404})


@dataclass(frozen=True)
class Command:
    """来自授权会话的一条文本命令（来源校验已在本层完成，上层只管正文）。"""

    text: str


class Receiver:
    """把授权会话的文本消息交给上层的长轮询接收器。"""

    def __init__(self, client: httpx.AsyncClient, token: str, chat_id: str) -> None:
        self._client = client
        self._token = token
        self._chat_id = chat_id
        self._offset: int | None = None

    def _url(self, method: str) -> str:
        return _API.format(token=self._token, method=method)

    async def _get_updates(
        self, params: dict[str, Any], request_timeout: float
    ) -> list[dict[str, Any]]:
        resp = await self._client.post(
            self._url("getUpdates"), json=params, timeout=request_timeout
        )
        if resp.status_code in _FATAL_STATUS:
            raise TelegramFatalError(
                f"Telegram 拒绝 getUpdates（{resp.status_code}），"
                f"请检查 bot_token：{resp.text[:200]}"
            )
        resp.raise_for_status()
        try:
            payload = resp.json()
        except (ValueError, TypeError) as e:
            # JSONDecodeError（ValueError 子类）不被 httpx.HTTPError 覆盖，需单独捕获后走退避路径。
            raise TelegramFatalError(f"Telegram 返回了无效的 JSON：{e}") from e
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, list):
            return []
        return [u for u in result if isinstance(u, dict)]

    async def drain_backlog(self) -> None:
        """丢弃启动前积压的消息，只把游标推到最后一条之后（KTD16）。"""
        updates = await self._get_updates({"offset": -1, "timeout": 0}, 20.0)
        if not updates:
            return
        update_id = updates[-1].get("update_id")
        if isinstance(update_id, int):
            self._offset = update_id + 1
            logger.info("已丢弃启动前的积压消息，接收游标推进至 %d", self._offset)

    async def poll(self) -> list[Command]:
        """长轮询一次，返回本轮采纳的命令（未采纳的消息同样推进游标）。"""
        params: dict[str, Any] = {"timeout": _LONG_POLL_SECS, "allowed_updates": ["message"]}
        if self._offset is not None:
            params["offset"] = self._offset
        # 请求超时必须大于长轮询时长，否则每轮都被 httpx 判超时。
        updates = await self._get_updates(params, _LONG_POLL_SECS + 10)

        commands: list[Command] = []
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self._offset = update_id + 1
            command = self._to_command(update)
            if command is not None:
                commands.append(command)
        return commands

    def _to_command(self, update: dict[str, Any]) -> Command | None:
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        chat = message.get("chat")
        chat_id = str(chat.get("id")) if isinstance(chat, dict) else None
        if chat_id != self._chat_id:
            # R2：非授权来源静默丢弃、绝不回复，免得成了陌生人的回声探测器。
            logger.warning("忽略非授权会话的消息：chat_id=%s", chat_id)
            return None
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            logger.debug("忽略非文本消息")
            return None
        return Command(text=text.strip())

    async def run(
        self, on_command: Callable[[Command], Awaitable[None]], stop: asyncio.Event
    ) -> None:
        """常驻接收循环：先丢积压，再逐轮长轮询并把命令交给 on_command。"""
        backoff = _BASE_BACKOFF
        drained = False
        while not stop.is_set():
            try:
                if not drained:
                    await self.drain_backlog()
                    drained = True
                commands = await self._poll_or_stop(stop)
            except httpx.HTTPError as e:
                logger.warning("命令接收请求异常，%.0f 秒后重试：%s", backoff, e)
                await self._sleep_or_stop(backoff, stop)
                backoff = min(backoff * 2, _MAX_BACKOFF)
                continue
            backoff = _BASE_BACKOFF
            for command in commands:
                await on_command(command)
        logger.info("命令接收循环已退出")

    async def _poll_or_stop(self, stop: asyncio.Event) -> list[Command]:
        """长轮询与停止事件竞速：收到停止就丢下在途请求，不必空等满 30 秒。"""
        poll = asyncio.create_task(self.poll(), name="receive:poll")
        waiter = asyncio.create_task(stop.wait(), name="receive:stop")
        done, _ = await asyncio.wait({poll, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if poll in done:
            waiter.cancel()
            return poll.result()
        poll.cancel()
        await asyncio.gather(poll, return_exceptions=True)
        return []

    async def _sleep_or_stop(self, delay: float, stop: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass
