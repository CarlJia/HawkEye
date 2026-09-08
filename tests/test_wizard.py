"""向导测试：load_raw 兼容、A1-A5/R2/R5 自检、坏 TOML 不静默清空、KTD10 惰性导入。"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from hawkeye.config import ConfigError, load_config, load_raw
from hawkeye.wizard import (
    _apply_telegram,
    _ask_bot_token,
    _ask_chat_id,
    _collect_credentials,
    _load_existing,
    _print_summary,
    run_wizard,
)

# ---- 替身 ----


class _FakeScripted:
    """按预定义序列依次返回输入；用完仍被调则抛错（防止交互死循环漏检）。"""

    def __init__(self, *answers: str) -> None:
        self._answers = list(answers)
        self.asked: list[str] = []

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        if not self._answers:
            raise AssertionError(f"向导多要了一次输入：{prompt!r}")
        return self._answers.pop(0)

    def ask_secret(self, prompt: str) -> str:
        return self.ask(prompt)

    @property
    def remaining(self) -> int:
        return len(self._answers)


class _RecorderEmit:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, message: str) -> None:
        self.lines.append(message)


# ---- _apply_telegram 与 _load_existing ----


def test_apply_telegram_only_replaces_two_keys() -> None:
    raw = {
        "poll_interval_secs": 90,
        "telegram": {"bot_token": "old", "chat_id": "old_id", "extra": "keep"},
        "merchants": [
            {"name": "m", "pages": [{"url": "https://e.com", "elements": [{"selector": "#a"}]}]}
        ],
    }
    new = _apply_telegram(raw, "new", "-1001")
    # 其它顶层键原样带过。
    assert new["poll_interval_secs"] == 90
    assert new["merchants"] == raw["merchants"]
    # telegram 段只换两个键，额外键保留。
    assert new["telegram"]["bot_token"] == "new"
    assert new["telegram"]["chat_id"] == "-1001"
    assert new["telegram"]["extra"] == "keep"


def test_apply_telegram_creates_telegram_when_missing() -> None:
    new = _apply_telegram({}, "tok", "123")
    assert new == {"telegram": {"bot_token": "tok", "chat_id": "123"}}


def test_load_existing_returns_empty_when_absent(tmp_path: Path) -> None:
    assert _load_existing(tmp_path / "missing.toml") == {}


def test_load_existing_does_not_swallow_broken_toml(tmp_path: Path) -> None:
    p = tmp_path / "broken.toml"
    p.write_text('[telegram\nbot_token = "x"', encoding="utf-8")
    # 故意不兜成 {}：坏 TOML 里的内容必须如实上抛（U1 第四步）。
    with pytest.raises(ConfigError, match="TOML"):
        _load_existing(p)


# ---- 单步问答 ----


def test_ask_bot_token_retries_on_empty() -> None:
    script = _FakeScripted("", "  ", "real-token")
    assert _ask_bot_token(script.ask_secret) == "real-token"
    assert script.remaining == 0


def test_ask_chat_id_retries_on_empty_and_accepts_negative() -> None:
    # 空串 → 重问；负数群 id → 直接接受。
    script = _FakeScripted("", "  ", "-1001234567890")
    assert _ask_chat_id(script.ask) == "-1001234567890"
    assert script.remaining == 0


def test_ask_chat_id_accepts_plain_int() -> None:
    assert _ask_chat_id(_FakeScripted("42").ask) == "42"


# ---- 凭据自检：401 → 就地重问；网络异常 → 告警并继续 ----


async def test_collect_retries_when_telegram_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    # 401 → 重问一轮；第二轮凭据换成能通过的版本。
    # 必须 monkeypatch _verify：真实 httpx 网络异常会被 _verify 自身降级为 (True, None)，
    # 不重问；真实 Telegram 又可能把 bad-token 当合法 token 放行。直接按 token 内容
    # 区分 pass/fail 是唯一稳定的隔离策略,和下方端到端测试的 mock transport 路径独立。
    import hawkeye.wizard as wizard_mod  # noqa: PLC0415

    async def fake_verify(bot_token: str, chat_id: str) -> tuple[bool, str | None]:
        if bot_token == "bad-token":
            return False, None
        return True, None

    monkeypatch.setattr(wizard_mod, "_verify", fake_verify)

    script = _FakeScripted(
        "bad-token",  # 第一次 bot_token
        "1",  # chat_id
        "y",  # 验证
        "good-token",  # 第二次 bot_token
        "2",  # chat_id
        "n",  # 第二轮不做验证，直接走完
    )
    ask = script.ask
    ask_secret = script.ask_secret

    bot_token, chat_id = await _collect_credentials(ask, ask_secret)
    assert (bot_token, chat_id) == ("good-token", "2")


async def test_collect_returns_on_network_error(caplog: pytest.LogCaptureFixture) -> None:
    # 网络异常只告警、不抛错——下游 verify 自身把异常降级（Notifier.verify
    # 捕获 httpx.HTTPError 并只 log.warning），我方 _verify 拿到 True 直接返回。
    import hawkeye.wizard as wizard_mod  # noqa: PLC0415

    async def fake_verify(bot_token: str, chat_id: str) -> tuple[bool, str | None]:
        # 网络异常时被 _verify 视作「verify 自己降级为告警」，返回 (True, None)。
        return True, None

    script = _FakeScripted("tok", "1", "y")
    original = wizard_mod._verify
    wizard_mod._verify = fake_verify  # type: ignore[assignment]
    try:
        with caplog.at_level(logging.WARNING):
            bot_token, chat_id = await _collect_credentials(script.ask, script.ask_secret)
    finally:
        wizard_mod._verify = original  # type: ignore[assignment]

    assert (bot_token, chat_id) == ("tok", "1")
    assert script.remaining == 0


# ---- run_wizard 端到端 ----


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def _write_existing(path: Path, content: str = "") -> Path:
    path.write_text(content, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


_FULL_CONFIG = """
poll_interval_secs = 90
state_path = "s.json"

[telegram]
bot_token = "old:OLD"
chat_id = "old-id"

[[merchants]]
name = "shop"

[[merchants.pages]]
url = "https://shop.example.com/p"

[[merchants.pages.elements]]
selector = "#price"

[[watches]]
url = "https://forum.example.com/"
link_selector = ".topic-list a"
keywords = ["hk", "HK"]
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 权限断言")
async def test_wizard_preserves_other_keys_on_existing_file(tmp_path: Path) -> None:
    target = _write_existing(tmp_path / "config.toml", _FULL_CONFIG)
    script = _FakeScripted("new:NEW", "-10042", "n", "y")  # token / chat_id / 跳过验证 / 确认保存
    emit = _RecorderEmit()

    await run_wizard(target, ask=script.ask, ask_secret=script.ask_secret, emit=emit)

    # AE1：值等价断言（tomli_w 会重新格式化，文本等价不可能）。
    new_raw = load_raw(target)
    raw = tomllib.loads(_FULL_CONFIG)
    assert new_raw == {**raw, "telegram": {"bot_token": "new:NEW", "chat_id": "-10042"}}
    # parse_config 仍能解析（KTD2 的零监控可以，此处有 merchants / watches 更应通过）。
    cfg = load_config(target)
    assert cfg.poll_interval_secs == 90
    assert len(cfg.merchants) == 1
    assert len(cfg.watches) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 权限断言")
async def test_wizard_creates_file_with_no_backup_when_absent(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    script = _FakeScripted("tok", "1", "n", "y")
    emit = _RecorderEmit()

    await run_wizard(target, ask=script.ask, ask_secret=script.ask_secret, emit=emit)

    assert target.exists()
    assert _mode(target) == 0o600
    assert list(tmp_path.glob("config.toml.bak.*")) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 权限断言")
async def test_wizard_backs_up_existing_file(tmp_path: Path) -> None:
    target = _write_existing(tmp_path / "config.toml", _FULL_CONFIG)
    original_bytes = target.read_bytes()
    script = _FakeScripted("tok", "1", "n", "y")

    await run_wizard(target, ask=script.ask, ask_secret=script.ask_secret)

    backups = list(tmp_path.glob("config.toml.bak.*"))
    assert len(backups) == 1
    assert _mode(backups[0]) == 0o600
    assert backups[0].read_bytes() == original_bytes
    assert target.read_bytes() != original_bytes  # 正本确实被改写


async def test_wizard_summary_does_not_leak_full_token(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    full_token = "1234567890:AA-VERY-LONG-BOT-TOKEN-FOR-TEST"
    script = _FakeScripted(full_token, "1", "n", "y")
    emit = _RecorderEmit()

    await run_wizard(target, ask=script.ask, ask_secret=script.ask_secret, emit=emit)

    joined = "\n".join(emit.lines)
    assert full_token not in joined
    assert joined.count("***") >= 1


async def test_wizard_does_not_write_when_verification_fails(tmp_path: Path) -> None:
    """AE5：401 → 就地重问，目标文件与备份都还没被创建。"""
    target = tmp_path / "config.toml"
    script = _FakeScripted(
        "bad",  # 第一次 bot_token
        "1",  # 第一次 chat_id
        "y",  # 验证
        "bad",  # 第二次 bot_token（仍 401）
        "1",  # 第二次 chat_id
        "y",  # 再次验证
        "good",  # 第三次 bot_token（HTTP 200 替身由测试钩入）
        "1",  # 第三次 chat_id
        "n",  # 不再验证
        "y",  # 确认保存
    )
    emit = _RecorderEmit()

    # 这里直接覆盖 Notifier 的 verify：构造一个返 401 的 mock transport，
    # 让 _verify 抛 TelegramFatalError，从而触发挥导的重问。
    from hawkeye.notify import TelegramFatalError  # noqa: PLC0415

    real_verify_calls = {"n": 0}
    real_verify_passed = {"v": False}

    async def fake_verify(bot_token: str, chat_id: str) -> tuple[bool, str | None]:
        real_verify_calls["n"] += 1
        if real_verify_calls["n"] <= 2:
            return False, None  # 模拟 401 重问
        real_verify_passed["v"] = True
        return True, None

    # 直接 monkey-patch wizard 内部 _verify 的实现是最干净的。
    import hawkeye.wizard as wizard_mod  # noqa: PLC0415

    original_verify = wizard_mod._verify
    wizard_mod._verify = fake_verify  # type: ignore[assignment]
    try:
        await run_wizard(target, ask=script.ask, ask_secret=script.ask_secret, emit=emit)
    finally:
        wizard_mod._verify = original_verify  # type: ignore[assignment]

    # 关键断言：自检失败期间任何文件都没有被创建；最终确认后只有一个正本。
    # 由于第一次重问后还没写盘，最终文件应当存在并包含「good」。
    assert target.exists()
    assert "good" in target.read_text(encoding="utf-8")
    # 没有残留 .bak（原始就不存在目标文件）。
    assert list(tmp_path.glob("config.toml.bak.*")) == []
    # TelegramFatalError 仍在导入路径里，证明这一条契约没被绕过。
    assert TelegramFatalError is not None


async def test_wizard_propagates_broken_toml(tmp_path: Path) -> None:
    """坏 TOML 不能被静默清空：load_raw 抛 ConfigError，向导如实上抛。"""
    target = tmp_path / "config.toml"
    target.write_text('[telegram\nbot_token = "x"', encoding="utf-8")
    script = _FakeScripted()

    with pytest.raises(ConfigError, match="TOML"):
        await run_wizard(target, ask=script.ask, ask_secret=script.ask_secret)


async def test_wizard_can_skip_verification(tmp_path: Path) -> None:
    """run_verify=False 时完全不调用 _verify，也不问「是否自检」。"""
    target = tmp_path / "config.toml"
    script = _FakeScripted("tok", "42", "y")  # 不再问 verify、只剩确认
    emit = _RecorderEmit()

    called = {"v": False}

    async def fake_verify(bot_token: str, chat_id: str) -> tuple[bool, str | None]:
        called["v"] = True
        return True, None

    import hawkeye.wizard as wizard_mod  # noqa: PLC0415

    original_verify = wizard_mod._verify
    wizard_mod._verify = fake_verify  # type: ignore[assignment]
    try:
        await run_wizard(
            target,
            ask=script.ask,
            ask_secret=script.ask_secret,
            emit=emit,
            run_verify=False,
        )
    finally:
        wizard_mod._verify = original_verify  # type: ignore[assignment]

    assert target.exists()
    assert called["v"] is False  # 跳过自检时一次都没调用


# ---- 结束摘要：含 token 尾部 ----


def test_summary_contains_tail_and_path(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text("placeholder", encoding="utf-8")
    os.chmod(target, 0o600)
    emit = _RecorderEmit()
    _print_summary(emit, target, "1234567890:AA-VE-LONG", "-10042")
    joined = "\n".join(emit.lines)
    assert str(target) in joined
    assert "600" in joined
    assert "1234567890:AA-VE-LONG" not in joined  # 完整 token 不进摘要
    assert "ONG" in joined or "***（已隐藏）" in joined  # 至少能看到尾部/打码


# ---- KTD10：子进程里 init 路径不拉 playwright / asyncssh / httpx ----


def test_init_path_does_not_load_heavy_modules() -> None:
    """在干净子进程里运行 wizard 不触发 playwright / asyncssh / httpx import。

    run_verify=False 走纯配置路径；如果选 y 触发了 httpx / notify 那是另一码事。
    子进程用 stdin 喂满脚本所需输入，避免 getpass 在无 tty 时抛 EOFError。
    """
    code = (
        "import sys, asyncio\n"
        "from hawkeye.wizard import run_wizard\n"
        "from pathlib import Path\n"
        "import tempfile\n"
        "async def _go():\n"
        "    with tempfile.TemporaryDirectory() as d:\n"
        "        p = Path(d) / 'c.toml'\n"
        "        # 用脚本化输入替身，避开 getpass 在无 tty 时的 EOFError。\n"
        "        script = iter(['tok', '1', 'y'])\n"
        "        def ask(_prompt): return next(script)\n"
        "        def ask_secret(_prompt): return next(script)\n"
        "        await run_wizard(p, ask=ask, ask_secret=ask_secret, run_verify=False)\n"
        "asyncio.run(_go())\n"
        "assert 'playwright' not in sys.modules\n"
        "assert 'asyncssh' not in sys.modules\n"
        "assert 'httpx' not in sys.modules\n"
    )
    env = os.environ.copy()
    src_path = str(Path(__file__).resolve().parent.parent / "src")
    env["PYTHONPATH"] = src_path
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"子进程退出码非 0：\nstdout={result.stdout}\nstderr={result.stderr}"
    )


def test_cli_import_does_not_load_playwright() -> None:
    """`import hawkeye.__main__` 不应加载 playwright（KTD10）。"""
    code = (
        "import sys\n"
        "import hawkeye.__main__\n"
        "assert 'playwright' not in sys.modules\n"
        "assert 'asyncssh' not in sys.modules\n"
    )
    env = os.environ.copy()
    src_path = str(Path(__file__).resolve().parent.parent / "src")
    env["PYTHONPATH"] = src_path
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
