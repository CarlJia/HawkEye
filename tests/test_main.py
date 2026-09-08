"""__main__ 测试：日志等级解析优先级，以及两条循环并存的装配与退出语义。"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import pytest

from hawkeye.__main__ import _parse_args, _resolve_level, main
from hawkeye.control import MENU_COMMANDS
from hawkeye.notify import TelegramFatalError

_TELEGRAM = """
[telegram]
bot_token = "123456:AAEFghIJ"
chat_id = "42"
"""

_CONFIG = f"""
state_path = "state.json"
{_TELEGRAM}
[[merchants]]
name = "m"

[[merchants.pages]]
url = "https://e.com/p"

[[merchants.pages.elements]]
selector = "#a"
"""


def test_default_level_is_info() -> None:
    assert _resolve_level(_parse_args([])) == logging.INFO


def test_verbose_maps_to_debug() -> None:
    assert _resolve_level(_parse_args(["-v"])) == logging.DEBUG


def test_log_level_overrides_verbose() -> None:
    assert _resolve_level(_parse_args(["-v", "--log-level", "warning"])) == logging.WARNING


def test_log_level_is_case_insensitive() -> None:
    assert _resolve_level(_parse_args(["--log-level", "error"])) == logging.ERROR


# ---- 进程装配：两条循环并存、共享停止事件、异常时互不挂住 ----


class _Recorder:
    """收集被替换掉的组件实例，让测试在 main() 返回后还能拿到句柄做断言。"""

    def __init__(self) -> None:
        self.scheduler: Any = None
        self.receiver: Any = None
        self.controller: Any = None
        self.verified = False
        self.menu_synced: Any = None
        self.menu_synced_before_browser = False
        self.browser_started = False
        self.browser_closed = False


def _install(monkeypatch: pytest.MonkeyPatch, rec: _Recorder, receiver_run: Any) -> None:
    """把 __main__ 依赖的五个组件全换成替身，只留装配逻辑本身受测。"""

    class FakeNotifier:
        def __init__(self, telegram: Any, client: Any) -> None:
            pass

        async def verify(self) -> None:
            rec.verified = True

        async def sync_commands(self, commands: Any) -> str:
            rec.menu_synced = tuple(commands)
            rec.menu_synced_before_browser = not rec.browser_started
            return "快捷菜单已同步。"

        async def send(self, text: str) -> bool:
            return True

    class FakeBrowser:
        def __init__(self, config: Any) -> None:
            pass

        async def start(self) -> None:
            rec.browser_started = True

        async def close(self) -> None:
            rec.browser_closed = True

    class FakeScheduler:
        def __init__(
            self, config: Any, browser: Any, notifier: Any, stop: asyncio.Event | None = None
        ) -> None:
            assert stop is not None, "装配必须注入共享停止事件，否则信号只能停住一条循环"
            self.config = config
            self._stop = stop
            self.runs = 0
            self.returned = False
            rec.scheduler = self

        def request_stop(self) -> None:
            self._stop.set()

        async def run(self) -> None:
            self.runs += 1
            await self._stop.wait()
            self.returned = True

    class FakeController:
        def __init__(self, config_path: Any, scheduler: Any, notifier: Any) -> None:
            self.config_path = config_path
            rec.controller = self

        async def handle(self, command: Any) -> None:
            pass

    class FakeReceiver:
        def __init__(self, client: Any, token: str, chat_id: str) -> None:
            self.token = token
            self.chat_id = chat_id
            self.runs = 0
            self.returned = False
            rec.receiver = self

        async def run(self, on_command: Any, stop: asyncio.Event) -> None:
            self.runs += 1
            self.on_command = on_command
            await receiver_run(rec, stop)
            self.returned = True

    monkeypatch.setattr("hawkeye.notify.Notifier", FakeNotifier)
    monkeypatch.setattr("hawkeye.fetch.BrowserManager", FakeBrowser)
    monkeypatch.setattr("hawkeye.scheduler.Scheduler", FakeScheduler)
    monkeypatch.setattr("hawkeye.control.Controller", FakeController)
    monkeypatch.setattr("hawkeye.receive.Receiver", FakeReceiver)


def _write(tmp_path: Path, content: str = _CONFIG) -> str:
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    os.chmod(path, 0o600)
    return str(path)


async def _stop_directly(rec: _Recorder, stop: asyncio.Event) -> None:
    stop.set()


async def _stop_via_signal_path(rec: _Recorder, stop: asyncio.Event) -> None:
    """走信号处理真正调用的那条路径：只 request_stop 调度器，看接收循环是否跟着退。"""
    rec.scheduler.request_stop()


async def _raise_fatal(rec: _Recorder, stop: asyncio.Event) -> None:
    raise TelegramFatalError("Telegram 拒绝 getUpdates（401）")


def test_both_loops_are_started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    _install(monkeypatch, rec, _stop_directly)

    assert main(["-c", _write(tmp_path)]) == 0

    assert rec.verified is True  # 自检先于浏览器启动
    assert rec.scheduler.runs == 1
    assert rec.receiver.runs == 1
    assert rec.receiver.on_command == rec.controller.handle  # 命令确实接到控制面
    assert rec.browser_closed is True


def test_menu_is_created_at_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """快捷菜单在启动时自动建好，且与自检一样排在拉起 Chromium 之前。"""
    rec = _Recorder()
    _install(monkeypatch, rec, _stop_directly)

    assert main(["-c", _write(tmp_path)]) == 0

    assert rec.menu_synced == MENU_COMMANDS
    assert rec.menu_synced_before_browser is True


def test_request_stop_returns_both_loops_with_exit_code_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _Recorder()
    _install(monkeypatch, rec, _stop_via_signal_path)

    assert main(["-c", _write(tmp_path)]) == 0

    assert rec.scheduler.returned is True
    assert rec.receiver.returned is True


def test_receiver_fatal_error_exits_two_and_stops_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 接收循环致命失败时，调度循环必须被置停并真正返回，否则进程挂在 gather 上不退。
    rec = _Recorder()
    _install(monkeypatch, rec, _raise_fatal)

    assert main(["-c", _write(tmp_path)]) == 2

    assert rec.scheduler.returned is True
    assert rec.browser_closed is True


def test_zero_monitor_config_starts_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # KTD13：零监控不是错误，进程照常起，用户可在 Telegram 里现场 /add。
    rec = _Recorder()
    _install(monkeypatch, rec, _stop_directly)

    with caplog.at_level(logging.WARNING, logger="hawkeye"):
        assert main(["-c", _write(tmp_path, _TELEGRAM)]) == 0

    assert rec.receiver.runs == 1
    assert any("未配置任何监控" in record.message for record in caplog.records)


# ---- 子命令骨架与退出码映射（U1 / KTD16 / KTD17 / R21）----


def test_parse_args_default_command_is_none() -> None:
    """裸调用时 args.command 为 None，args.config 仍为默认 config.toml。"""
    args = _parse_args([])
    assert args.command is None
    assert args.config == "config.toml"


def test_parse_args_init_sets_command_and_keeps_config() -> None:
    args = _parse_args(["init"])
    assert args.command == "init"
    assert args.config == "config.toml"


def test_parse_args_init_accepts_config_after_subcommand() -> None:
    """`hawkeye init -c x.toml` 不应触发 argparse 的 usage error（KTD16）。"""
    args = _parse_args(["init", "-c", "x.toml"])
    assert args.command == "init"
    assert args.config == "x.toml"


def test_parse_args_init_accepts_config_before_subcommand() -> None:
    """`hawkeye -c x.toml init` 同样成立（R21 / systemd RestartPreventExitStatus=2 不能被吞）。"""
    args = _parse_args(["-c", "x.toml", "init"])
    assert args.command == "init"
    assert args.config == "x.toml"


def test_main_init_returns_one_on_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """子命令内 ConfigError 映射为 1，不是守护进程的 2（R21）。"""
    from hawkeye.config import ConfigError

    async def boom(_path: str, **_kw: Any) -> None:
        raise ConfigError("test: 配置坏了")

    monkeypatch.setattr("hawkeye.wizard.run_wizard", boom)
    assert main(["init", "-c", str(tmp_path / "c.toml")]) == 1


def test_main_init_returns_one_on_edit_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hawkeye.configedit import EditError

    async def boom(_path: str, **_kw: Any) -> None:
        raise EditError("test: 写盘失败")

    monkeypatch.setattr("hawkeye.wizard.run_wizard", boom)
    assert main(["init", "-c", str(tmp_path / "c.toml")]) == 1


def test_main_daemon_still_returns_two_on_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """守护进程路径的 ConfigError 必须仍映射为 2（systemd RestartPreventExitStatus=2）。"""
    from hawkeye.config import ConfigError

    rec = _Recorder()
    _install(monkeypatch, rec, _stop_directly)

    # 把 load_config 替换成抛错版本，看 main 是否仍返回 2 而不是 1。
    def _boom_load_config(_path: str) -> Any:
        raise ConfigError("test: 配置坏了")

    monkeypatch.setattr("hawkeye.__main__.load_config", _boom_load_config)
    assert main(["-c", _write(tmp_path)]) == 2


def test_main_deploy_placeholder_returns_one() -> None:
    """deploy 现阶段只是占位，未实现时返回 1 并给清晰提示。"""
    assert main(["deploy"]) == 1
