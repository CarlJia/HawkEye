"""notify 模块测试：文本格式、发送/重试、启动自检与 token 脱敏（httpx MockTransport）。"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from hawkeye.config import TelegramConfig
from hawkeye.notify import (
    Notifier,
    TelegramFatalError,
    TokenRedactingFactory,
    format_change_message,
    format_failure_message,
    format_new_post_message,
)


def test_format_change_message() -> None:
    msg = format_change_message("商品A", "售罄", "充足", "2026-09-02T10:00:00+08:00")
    assert "商品A" in msg
    assert "售罄 → 充足" in msg
    assert "2026-09-02T10:00:00+08:00" in msg


def test_format_change_message_with_url() -> None:
    # url 非空时附在最后一行,原样保留不转义（Telegram 客户端自动成链）
    url = "https://e.com/order/123"
    msg = format_change_message("商品A", "售罄", "充足", "2026-09-02T10:00:00+08:00", url=url)
    assert "商品A" in msg
    assert "售罄 → 充足" in msg
    assert "2026-09-02T10:00:00+08:00" in msg
    assert url in msg
    assert msg.endswith(url)  # URL 在末尾,客户端成链位置稳定


def test_format_change_message_url_none_omitted() -> None:
    # url 缺省（None）时与历史格式逐字符相同,兼容老消费者
    msg = format_change_message("商品A", "售罄", "充足", "2026-09-02T10:00:00+08:00", url=None)
    assert msg == "【变更】商品A\n售罄 → 充足\n时间：2026-09-02T10:00:00+08:00"


def test_format_new_post_message() -> None:
    # Covers R4：输出含标题、URL、时间三部分，且 URL 原样保留不转义
    url = "https://www.nodeseek.com/post-911200-1"
    msg = format_new_post_message("HK 机房测评", url, "2026-09-04T10:00:00+08:00")
    assert "HK 机房测评" in msg
    assert url in msg  # 原样保留，保证客户端可成链
    assert "2026-09-04T10:00:00+08:00" in msg


def test_format_failure_message() -> None:
    msg = format_failure_message("商品A", 3, "导航失败：Timeout", "2026-09-02T10:00:00+08:00")
    assert "商品A" in msg
    assert "3" in msg
    assert "导航失败" in msg


def test_format_failure_message_with_url() -> None:
    # url 非空时附在最后一行,原样保留不转义
    url = "https://e.com/page"
    msg = format_failure_message(
        "商品A", 3, "导航失败：Timeout", "2026-09-02T10:00:00+08:00", url=url
    )
    assert "商品A" in msg
    assert "3" in msg
    assert "导航失败" in msg
    assert msg.endswith(url)


async def test_send_success_and_payload() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        notifier = Notifier(TelegramConfig("tok", "chat"), client)
        assert await notifier.send("hi") is True

    assert seen["body"] == {"chat_id": "chat", "text": "hi"}
    assert "parse_mode" not in seen["body"]


async def test_send_retries_then_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("hawkeye.notify.asyncio.sleep", _no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        notifier = Notifier(TelegramConfig("tok", "chat"), client)
        assert await notifier.send("hi") is False

    assert calls["n"] == 3  # 重试到上限


async def test_send_gives_up_on_permanent_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """400 chat not found 是配置错误：必须只请求一次、不退避，而非重试到上限。"""
    calls = {"n": 0}
    slept: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("hawkeye.notify.asyncio.sleep", _record_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            400,
            json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        notifier = Notifier(TelegramConfig("tok", "chat"), client)
        assert await notifier.send("hi") is False

    assert calls["n"] == 1
    assert slept == []


async def test_verify_rejects_unreachable_chat() -> None:
    """启动自检遇到永久性拒绝应抛错，让守护进程 fail-closed 而不是静默空转。"""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            400,
            json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        notifier = Notifier(TelegramConfig("tok", "chat"), client)
        with pytest.raises(TelegramFatalError, match="chat not found"):
            await notifier.verify()

    assert seen["path"].endswith("/getChat")
    assert seen["body"] == {"chat_id": "chat"}


async def test_verify_tolerates_transient_failure() -> None:
    """Telegram 抖动（5xx / 网络异常）不应阻止守护进程启动。"""
    transport = httpx.MockTransport(lambda _r: httpx.Response(502, text="bad gateway"))
    async with httpx.AsyncClient(transport=transport) as client:
        notifier = Notifier(TelegramConfig("tok", "chat"), client)
        await notifier.verify()


_MENU = (("add", "新建监控"), ("help", "显示命令说明"))
_MENU_PAYLOAD = [
    {"command": "add", "description": "新建监控"},
    {"command": "help", "description": "显示命令说明"},
]


async def test_sync_commands_creates_menu_when_absent() -> None:
    """菜单为空时写入一次 setMyCommands，命令表原样上报（不带斜杠）。"""
    calls: list[tuple[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/getMyCommands"):
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(200, json={"ok": True, "result": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        notifier = Notifier(TelegramConfig("tok", "chat"), client)
        reply = await notifier.sync_commands(_MENU)

    assert [path for path, _ in calls] == ["/bottok/getMyCommands", "/bottok/setMyCommands"]
    assert calls[1][1] == {"commands": _MENU_PAYLOAD}
    assert "2" in reply


async def test_sync_commands_skips_write_when_unchanged() -> None:
    """菜单已一致就不再写：同步是比对后才动手，不是每次启动都盖一遍。"""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "result": _MENU_PAYLOAD})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        notifier = Notifier(TelegramConfig("tok", "chat"), client)
        reply = await notifier.sync_commands(_MENU)

    assert paths == ["/bottok/getMyCommands"]
    assert "已是最新" in reply


async def test_sync_commands_rewrites_when_menu_drifted() -> None:
    """描述被改过或少一条都算漂移，必须重新写回，否则菜单永远停在旧内容。"""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/getMyCommands"):
            stale = [{"command": "add", "description": "旧描述"}]
            return httpx.Response(200, json={"ok": True, "result": stale})
        return httpx.Response(200, json={"ok": True, "result": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        notifier = Notifier(TelegramConfig("tok", "chat"), client)
        await notifier.sync_commands(_MENU)

    assert paths == ["/bottok/getMyCommands", "/bottok/setMyCommands"]


async def test_sync_commands_survives_telegram_rejection() -> None:
    """快捷菜单只是输入便利：Telegram 拒绝也只回执失败，绝不抛错拖垮启动。"""
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(400, json={"ok": False, "description": "Bad Request"})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        notifier = Notifier(TelegramConfig("tok", "chat"), client)
        reply = await notifier.sync_commands(_MENU)

    assert "失败" in reply


async def test_sync_commands_survives_network_error() -> None:
    """网络异常同样只回执，不外抛——监控本体不该因为菜单没同步上而不启动。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        notifier = Notifier(TelegramConfig("tok", "chat"), client)
        reply = await notifier.sync_commands(_MENU)

    assert "失败" in reply


def test_token_redaction_filter_scrubs_url_in_args() -> None:
    """httpx 以 INFO 记录含 token 的完整 URL，工厂层脱敏后 token 不出现。"""
    token = "8428922140:AA-fake-token-for-test"
    factory = TokenRedactingFactory(token, logging.getLogRecordFactory())
    record = factory(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='HTTP Request: POST https://api.telegram.org/bot%s/sendMessage "HTTP/1.1 400"',
        args=(token,),
        exc_info=None,
    )

    assert token not in record.getMessage()
    assert "<REDACTED>" in record.getMessage()
