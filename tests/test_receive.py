"""receive 模块测试：授权过滤、游标推进、启动丢积压、退避重试与及时停止（MockTransport）。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from hawkeye.notify import TelegramFatalError
from hawkeye.receive import Command, Receiver

_CHAT = "42"


def _update(
    update_id: int, *, chat_id: int | str = 42, text: str | None = "/help"
) -> dict[str, Any]:
    message: dict[str, Any] = {"message_id": update_id, "chat": {"id": chat_id}}
    if text is not None:
        message["text"] = text
    return {"update_id": update_id, "message": message}


def _ok(updates: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": updates})


async def test_authorized_text_becomes_command() -> None:
    transport = httpx.MockTransport(lambda _r: _ok([_update(1, text=" /list ")]))
    async with httpx.AsyncClient(transport=transport) as client:
        assert await Receiver(client, "tok", _CHAT).poll() == [Command(text="/list")]


async def test_unauthorized_chat_is_dropped_without_reply() -> None:
    # AE5 / R2：陌生人发来的命令既不执行也不回复，否则等于给对方一个回声探测器。
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return _ok([_update(7, chat_id=999, text="/del 1")])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await Receiver(client, "tok", _CHAT).poll() == []

    assert paths == ["/bottok/getUpdates"]  # 全程没有一次 sendMessage


async def test_non_text_message_is_ignored() -> None:
    update = {"update_id": 3, "message": {"chat": {"id": 42}, "photo": [{"file_id": "x"}]}}
    transport = httpx.MockTransport(lambda _r: _ok([update]))
    async with httpx.AsyncClient(transport=transport) as client:
        assert await Receiver(client, "tok", _CHAT).poll() == []


async def test_offset_advances_even_for_ignored_updates() -> None:
    # 被忽略的消息若不推进游标，Telegram 每轮都会重发它，长轮询从此永久卡死。
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return _ok([_update(10, chat_id=999), _update(11, text="/help")])
        return _ok([])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        receiver = Receiver(client, "tok", _CHAT)
        assert await receiver.poll() == [Command(text="/help")]
        assert await receiver.poll() == []

    assert "offset" not in bodies[0]  # 首轮没有游标可带
    assert bodies[0]["timeout"] == 30
    assert bodies[0]["allowed_updates"] == ["message"]
    assert bodies[1]["offset"] == 12  # 被忽略的 10 同样推进了


async def test_drain_backlog_skips_pending_updates() -> None:
    # KTD16：重启后不该重放几小时前的 /add，更不该重放一条 /del 确认。
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("offset") == -1:
            return _ok([_update(4, text="/del 1"), _update(5, text="/add")])
        return _ok([])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        receiver = Receiver(client, "tok", _CHAT)
        await receiver.drain_backlog()
        assert await receiver.poll() == []

    assert bodies[0] == {"offset": -1, "timeout": 0}
    assert bodies[1]["offset"] == 6  # 直接跳到积压最后一条之后


async def test_drain_backlog_without_backlog_leaves_offset_unset() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _ok([])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        receiver = Receiver(client, "tok", _CHAT)
        await receiver.drain_backlog()
        await receiver.poll()

    assert "offset" not in bodies[1]


async def test_fatal_status_raises_telegram_fatal_error() -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(401, text="Unauthorized"))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(TelegramFatalError, match="401"):
            await Receiver(client, "tok", _CHAT).poll()


async def test_run_propagates_fatal_status() -> None:
    # 凭据被拒时接收循环必须让进程退出（__main__ 映射为退出码 2），不能默默重试。
    async def _never(_cmd: Command) -> None:
        raise AssertionError("不应有命令产出")

    transport = httpx.MockTransport(lambda _r: httpx.Response(403, text="Forbidden"))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(TelegramFatalError, match="403"):
            await Receiver(client, "tok", _CHAT).run(_never, asyncio.Event())


async def test_run_backs_off_and_survives_network_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 网络抖动不该拖垮进程：指数退避、上限 60 秒，恢复后照常收命令。
    slept: list[float] = []

    async def _record_sleep(_self: Receiver, delay: float, _stop: asyncio.Event) -> None:
        slept.append(delay)

    monkeypatch.setattr(Receiver, "_sleep_or_stop", _record_sleep)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 7:
            raise httpx.ConnectError("boom")
        if json.loads(request.content).get("offset") == -1:
            return _ok([])
        return _ok([_update(9, text="/list")])

    got: list[Command] = []
    stop = asyncio.Event()

    async def _on_command(cmd: Command) -> None:
        got.append(cmd)
        stop.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await Receiver(client, "tok", _CHAT).run(_on_command, stop)

    assert got == [Command(text="/list")]
    assert slept == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


async def test_run_returns_promptly_while_long_poll_in_flight() -> None:
    # 停止时长轮询往往正挂在途，必须立刻放弃它，而不是空等满一个 30 秒窗口。
    in_flight = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content).get("offset") == -1:
            return _ok([])
        in_flight.set()
        await asyncio.Event().wait()
        raise AssertionError("不可达")

    async def _never(_cmd: Command) -> None:
        raise AssertionError("不应有命令产出")

    stop = asyncio.Event()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runner = asyncio.create_task(Receiver(client, "tok", _CHAT).run(_never, stop))
        await asyncio.wait_for(in_flight.wait(), timeout=1.0)
        stop.set()
        await asyncio.wait_for(runner, timeout=1.0)
