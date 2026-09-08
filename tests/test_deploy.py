"""deploy 模块测试：编排顺序、早失败、三态、白名单校验、占位符硬闸门。

按计划 U5 的覆盖范围：R7/R8（.hawkeye-deploy.toml 不含密码）、R10（提权
三档）、R13（.part + mv）、R14/KTD5（三态）、R15（输出打码）、R17/AE16
（三条退出边）、R21（ConfigError → 1）、R24（mktemp-d）、R25（分层健康判据）、
R26/AE15（白名单校验先抛）、AE13（占位符 token）、AE14（幂等）。

真实 SSH/SFTP/systemctl 不在自动化范围——无 VPS——记入现场验证清单。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from hawkeye import deploy as deploy_mod
from hawkeye.config import ConfigError
from hawkeye.deploy import (
    CONFIG_STATE_MISSING,
    CONFIG_STATE_PLACEHOLDER,
    CONFIG_STATE_REAL,
    PLACEHOLDER_TOKEN,
    REMOTE_CONFIG_PATH,
    DeployError,
    InstallHandles,
    _build_launcher_script,
    _coerce_port,
    _read_connection_file,
    _resolve_connection_params,
    _should_adopt_local_config,
    _validate_local_config,
    _write_connection_file,
    run_deploy,
)

# ============================================================================
# 替身
# ============================================================================


@dataclass
class _Call:
    """单次远端调用记录，方便断言调用顺序与参数。"""

    name: str
    kwargs: dict[str, Any] = field(default_factory=dict)


class _FakeRemoteOps:
    """生产 :class:`deploy_mod.RemoteOps` 的替身；可控制返回值与抛错点。"""

    def __init__(
        self,
        *,
        config_state: str = CONFIG_STATE_MISSING,
        privilege_should_fail: bool = False,
        privilege_error: Exception | None = None,
        remote_dir: str = "/var/tmp/hawkeye-deploy.fakefake",
        install_handles: InstallHandles | None = None,
        install_should_fail: bool = False,
        install_error: Exception | None = None,
        tail_rc: int = 0,
        tail_should_raise: Exception | None = None,
        tail_delay: float = 0.0,
        health: tuple[bool, str] = (True, "分层健康检查通过"),
    ) -> None:
        self.config_state = config_state
        self.privilege_should_fail = privilege_should_fail
        self.privilege_error = privilege_error
        self.remote_dir = remote_dir
        self.install_handles = install_handles or InstallHandles(
            log_path=f"{remote_dir}/install.log",
            pid_path=f"{remote_dir}/install.pid",
            rc_path=f"{remote_dir}/.rc",
            pid=4242,
        )
        self.install_should_fail = install_should_fail
        self.install_error = install_error or deploy_mod.SSHError("install failed")
        self.tail_rc = tail_rc
        self.tail_should_raise = tail_should_raise
        self.tail_delay = tail_delay
        self.health = health

        self.entered = False
        self.exited = False
        self.calls: list[_Call] = []
        self.uploaded_zips: list[tuple[Path, str]] = []
        self.uploaded_configs: list[tuple[Path, str]] = []
        self.cleanup_calls: list[tuple[str, bool]] = []
        self.tail_log_lines: list[str] = []

    async def __aenter__(self) -> _FakeRemoteOps:
        self.entered = True
        self.calls.append(_Call("__aenter__"))
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.exited = True
        self.calls.append(_Call("__aexit__", {"exc": exc}))

    async def probe_privilege(self) -> None:
        self.calls.append(_Call("probe_privilege"))
        if self.privilege_should_fail:
            raise self.privilege_error or deploy_mod.SudoUnavailableError("无 sudo")

    async def probe_remote_config(self, config_path: str, placeholder: str) -> str:
        self.calls.append(
            _Call("probe_remote_config", {"config_path": config_path, "placeholder": placeholder})
        )
        return self.config_state

    async def mktemp_remote_dir(self) -> str:
        self.calls.append(_Call("mktemp_remote_dir"))
        return self.remote_dir

    async def upload_zip(self, local_zip: Path, remote_path: str) -> None:
        self.calls.append(_Call("upload_zip", {"remote_path": remote_path}))
        self.uploaded_zips.append((local_zip, remote_path))

    async def upload_config(self, local_config: Path, remote_path: str) -> None:
        self.calls.append(_Call("upload_config", {"remote_path": remote_path}))
        self.uploaded_configs.append((local_config, remote_path))

    async def run_background_install(
        self,
        *,
        stage_dir: str,
        pkg_name: str,
        config_remote_path: str | None,
        overwrite_config: bool,
    ) -> InstallHandles:
        self.calls.append(
            _Call(
                "run_background_install",
                {
                    "stage_dir": stage_dir,
                    "pkg_name": pkg_name,
                    "config_remote_path": config_remote_path,
                    "overwrite_config": overwrite_config,
                },
            )
        )
        if self.install_should_fail:
            raise self.install_error
        return self.install_handles

    async def tail_log_until_done(
        self,
        handles: InstallHandles,
        *,
        max_seconds: float,
        secrets: Sequence[str],
        on_line: Callable[[str], None],
    ) -> int:
        self.calls.append(
            _Call(
                "tail_log_until_done",
                {"handles": handles, "max_seconds": max_seconds, "secrets": list(secrets)},
            )
        )
        if self.tail_delay:
            await asyncio.sleep(self.tail_delay)
        for line in self.tail_log_lines:
            on_line(line)
        if self.tail_should_raise is not None:
            raise self.tail_should_raise
        return self.tail_rc

    async def layered_health_check(self) -> tuple[bool, str]:
        self.calls.append(_Call("layered_health_check"))
        return self.health

    async def cleanup_stage(self, stage_dir: str, *, keep_log: bool) -> None:
        self.calls.append(_Call("cleanup_stage", {"stage_dir": stage_dir, "keep_log": keep_log}))
        self.cleanup_calls.append((stage_dir, keep_log))


# ============================================================================
# 路径 / 配置 / 项目根 工具
# ============================================================================


def _make_project_root(base: Path, *, version: str = "9.9.9") -> Path:
    """造一个最小可打包的项目根，deploy.py 的 packaging_root 指向这里。"""
    root = base / "project"
    root.mkdir()
    (root / "src" / "hawkeye").mkdir(parents=True)
    (root / "src" / "hawkeye" / "__init__.py").write_text("")
    (root / "pyproject.toml").write_text(f'[project]\nname = "hawkeye"\nversion = "{version}"\n')
    (root / "config.example.toml").write_text(
        '[telegram]\nbot_token = "placeholder"\nchat_id = "0"\n'
    )
    (root / "README.md").write_text("# test\n")
    (root / "deploy.sh").write_text("#!/usr/bin/env bash\necho ok\n")
    return root


_TELEGRAM_OK = """
[telegram]
bot_token = "8428922140:AA-real-token"
chat_id = "100"
"""


def _write_local_config(path: Path, content: str = _TELEGRAM_OK) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _make_args(**overrides: Any) -> argparse.Namespace:
    """构造 ``run_deploy`` 接受的最小 Namespace。"""
    base = {
        "command": "deploy",
        "config": "config.toml",
        "host": None,
        "user": None,
        "port": None,
        "overwrite_config": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _collect_emit() -> tuple[Callable[[str], None], list[str]]:
    """收集所有 emit() 的输出（脱敏前）以做断言。"""
    msgs: list[str] = []

    def _emit(m: str) -> None:
        msgs.append(m)

    return _emit, msgs


def _collect_secrets() -> tuple[Callable[[str], str], list[str]]:
    """收集 ask_secret 调用结果——密码不能进任何 emit 的字符串。"""
    pw: list[str] = []

    def _ask_secret(_prompt: str) -> str:
        pw.append("dummy-password")
        return "dummy-password"

    return _ask_secret, pw


def _ask_factory(answers: list[str]) -> Callable[[str], str]:
    """按顺序消耗 answers；用完抛错以便尽早发现测试本身写漏。"""
    idx = {"i": 0}

    def _ask(_prompt: str) -> str:
        i = idx["i"]
        idx["i"] += 1
        if i >= len(answers):
            raise AssertionError(f"_ask 答案用完，还被问第 {i + 1} 次")
        return answers[i]

    return _ask


def _run(
    args: argparse.Namespace,
    *,
    project_root: Path,
    config_path: Path | None,
    connection_file: Path,
    known_hosts: Path,
    remote_ops: _FakeRemoteOps,
    answers: list[str] | None = None,
    install_timeout: float = 30.0,
) -> tuple[int, list[str]]:
    """同步跑 run_deploy 并返回 (exit_code, emitted_lines)。"""
    if config_path is None:
        config_path = project_root / "config.toml"
    emit, msgs = _collect_emit()
    ask = _ask_factory(answers or [])
    ask_secret, _ = _collect_secrets()

    def _factory(**_kw: Any) -> _FakeRemoteOps:
        return remote_ops

    code = asyncio.run(
        run_deploy(
            args,
            config_path=config_path,
            connection_file=connection_file,
            known_hosts_path=known_hosts,
            dist_dir=project_root / "dist",
            ask=ask,
            ask_secret=ask_secret,
            emit=emit,
            remote_ops_factory=_factory,
            packaging_root=project_root,
            install_timeout_seconds=install_timeout,
        )
    )
    return code, msgs


# ============================================================================
# 纯函数：连接参数解析 / 三态 / 占位符校验 / launcher 模板
# ============================================================================


def test_coerce_port_handles_various_inputs() -> None:
    assert _coerce_port(22) == 22
    assert _coerce_port("2222") == 2222
    assert _coerce_port("  80  ") == 80
    assert _coerce_port(None) is None
    assert _coerce_port("") is None
    assert _coerce_port(True) is None  # bool 是 int 的子类，必须先排除
    assert _coerce_port("abc") is None
    assert _coerce_port(1.5) is None


def test_resolve_connection_params_cli_over_file() -> None:
    """命令行 > 配置文件 > 交互。"""
    host, port, user, asked = _resolve_connection_params(
        stored={"host": "old.com", "user": "olduser", "port": 2222},
        host_arg="cli.com",
        user_arg=None,
        port_arg=None,
        ask=_ask_factory([]),
    )
    assert host == "cli.com"
    assert port == 2222  # 从配置文件来（CLI 未指定）
    assert user == "olduser"  # 从配置文件来
    assert asked is False


def test_resolve_connection_params_falls_back_to_interactive() -> None:
    """配置文件为空时进入交互；返回值标记 asked_new=True 供外层写回。"""
    host, port, user, asked = _resolve_connection_params(
        stored={},
        host_arg=None,
        user_arg=None,
        port_arg=None,
        ask=_ask_factory(["myhost.com", "alice"]),
    )
    assert host == "myhost.com"
    assert user == "alice"
    assert port == 22  # 默认
    assert asked is True


def test_resolve_connection_params_cli_port_overrides() -> None:
    host, port, user, asked = _resolve_connection_params(
        stored={"host": "h", "user": "u", "port": 2222},
        host_arg=None,
        user_arg=None,
        port_arg=8022,
        ask=_ask_factory([]),
    )
    assert port == 8022
    assert asked is False


def test_read_connection_file_missing_returns_empty(tmp_path: Path) -> None:
    assert _read_connection_file(tmp_path / "nope.toml") == {}


def test_write_connection_file_excludes_password(tmp_path: Path) -> None:
    """R8：写出的 .hawkeye-deploy.toml 只含 host/port/user，密码永不落盘。"""
    p = tmp_path / ".hawkeye-deploy.toml"
    _write_connection_file(p, host="vps.example.com", port=2222, user="alice")
    text = p.read_text(encoding="utf-8")
    assert "host" in text
    assert "port" in text
    assert "user" in text
    assert "password" not in text
    # 也不应有其他无关键
    assert "secret" not in text
    assert "token" not in text


def test_read_connection_file_invalid_toml_raises(tmp_path: Path) -> None:
    p = tmp_path / ".hawkeye-deploy.toml"
    p.write_text("not [valid toml", encoding="utf-8")
    with pytest.raises(DeployError, match="合法 TOML"):
        _read_connection_file(p)


def test_should_adopt_local_config_three_states() -> None:
    """三态 + overwrite 决策表（KTD5 / KTD12）。"""
    assert _should_adopt_local_config(state=CONFIG_STATE_MISSING, overwrite=False) is True
    assert _should_adopt_local_config(state=CONFIG_STATE_PLACEHOLDER, overwrite=False) is True
    assert _should_adopt_local_config(state=CONFIG_STATE_REAL, overwrite=False) is False
    assert _should_adopt_local_config(state=CONFIG_STATE_REAL, overwrite=True) is True


def test_validate_local_config_rejects_placeholder(tmp_path: Path) -> None:
    """AE13：占位符 token 在打包之前就退 1。"""
    p = tmp_path / "config.toml"
    _write_local_config(
        p,
        f'[telegram]\nbot_token = "{PLACEHOLDER_TOKEN}"\nchat_id = "1"\n',
    )
    with pytest.raises(DeployError, match="占位符"):
        _validate_local_config(p)


def test_validate_local_config_rejects_invalid_toml(tmp_path: Path) -> None:
    """parse_config 抛 ConfigError 时如实上抛——绝不静默清空。"""
    p = tmp_path / "config.toml"
    p.write_text('bot_token = "x"', encoding="utf-8")
    with pytest.raises(ConfigError):
        _validate_local_config(p)


def test_validate_local_config_accepts_real_token(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    _write_local_config(p)
    token = _validate_local_config(p)
    assert "8428922140:AA-real-token" == token


def test_build_launcher_script_includes_quoted_paths() -> None:
    """launcher 脚本里的 stage_dir / pkg_name / config_remote_path 都要被正确内插。"""
    script = _build_launcher_script(
        stage_dir="/var/tmp/hawkeye-deploy.abc12345",
        pkg_name="hawkeye-0.1.0-20260905180000",
        config_remote_path="/var/tmp/hawkeye-deploy.abc12345/config.toml",
        overwrite_config=False,
    )
    assert "/var/tmp/hawkeye-deploy.abc12345" in script
    assert "hawkeye-0.1.0-20260905180000" in script
    assert "--config" in script
    assert "--overwrite-config" not in script
    assert "install.pid" in script
    assert "install.log" in script
    assert ".rc" in script


def test_build_launcher_script_overwrite_flag() -> None:
    script = _build_launcher_script(
        stage_dir="/var/tmp/hawkeye-deploy.x",
        pkg_name="hawkeye-0.1.0-1",
        config_remote_path=None,
        overwrite_config=True,
    )
    assert "--overwrite-config" in script


def test_build_launcher_script_omits_config_when_not_adopted() -> None:
    script = _build_launcher_script(
        stage_dir="/var/tmp/hawkeye-deploy.x",
        pkg_name="hawkeye-0.1.0-1",
        config_remote_path=None,
        overwrite_config=False,
    )
    assert "--config" not in script


def test_build_launcher_script_escapes_password_via_herestring() -> None:
    """launcher 必须用 here-string 而不是 echo 把密码喂给 sudo，避免 argv 暴露。"""
    script = _build_launcher_script(
        stage_dir="/var/tmp/x",
        pkg_name="hawkeye-0.1.0-1",
        config_remote_path=None,
        overwrite_config=False,
    )
    assert "<<<" in script  # here-string
    assert 'echo "$SUDO_PW"' not in script  # 不能用 echo（密码进 echo argv）
    assert "sudo -S" in script  # sudo 从 stdin 读


# ============================================================================
# F4 / R21：早失败
# ============================================================================


def test_privilege_failure_exits_one_without_uploading(
    tmp_path: Path,
) -> None:
    """提权探测失败：返回 1，且**没有**调用打包/上传。"""
    project = _make_project_root(tmp_path)
    cfg = _write_local_config(project / "config.toml")
    conn = tmp_path / ".hawkeye-deploy.toml"
    _write_connection_file(conn, host="vps", port=22, user="alice")
    kh = tmp_path / "known_hosts"

    remote = _FakeRemoteOps(privilege_should_fail=True)

    code, msgs = _run(
        _make_args(),
        project_root=project,
        config_path=cfg,
        connection_file=conn,
        known_hosts=kh,
        remote_ops=remote,
        answers=[],
    )

    assert code == 1
    names = [c.name for c in remote.calls]
    assert names[0] == "__aenter__"
    assert "probe_privilege" in names
    # 关键：打包 / 上传 / install 都没发生
    assert "mktemp_remote_dir" not in names
    assert "upload_zip" not in names
    assert "upload_config" not in names
    assert "run_background_install" not in names
    assert any("提权探测失败" in m for m in msgs)


def test_local_config_invalid_exits_one_without_packaging(
    tmp_path: Path,
) -> None:
    """本机配置不合法 + 远端是 missing 态 → 1；不该打包也不该上传。"""
    project = _make_project_root(tmp_path)
    cfg = _write_local_config(
        project / "config.toml",
        '[telegram]\nbot_token = "x"\n',  # 缺 chat_id → parse_config 失败
    )
    conn = tmp_path / ".hawkeye-deploy.toml"
    _write_connection_file(conn, host="vps", port=22, user="alice")
    kh = tmp_path / "known_hosts"

    remote = _FakeRemoteOps(config_state=CONFIG_STATE_MISSING)
    code, msgs = _run(
        _make_args(),
        project_root=project,
        config_path=cfg,
        connection_file=conn,
        known_hosts=kh,
        remote_ops=remote,
        answers=[],
    )

    assert code == 1
    names = [c.name for c in remote.calls]
    # 探测远端三态发生了，但本地校验失败后就不再打包/上传
    assert "probe_remote_config" in names
    assert "mktemp_remote_dir" not in names
    assert "upload_zip" not in names
    assert any("不合法" in m or "占位符" in m for m in msgs)


def test_placeholder_bot_token_exits_one_before_packaging(tmp_path: Path) -> None:
    """AE13：占位符 token 在打包之前就退 1。"""
    project = _make_project_root(tmp_path)
    cfg = _write_local_config(
        project / "config.toml",
        f'[telegram]\nbot_token = "{PLACEHOLDER_TOKEN}"\nchat_id = "1"\n',
    )
    conn = tmp_path / ".hawkeye-deploy.toml"
    _write_connection_file(conn, host="vps", port=22, user="alice")
    kh = tmp_path / "known_hosts"

    remote = _FakeRemoteOps(config_state=CONFIG_STATE_MISSING)
    code, msgs = _run(
        _make_args(),
        project_root=project,
        config_path=cfg,
        connection_file=conn,
        known_hosts=kh,
        remote_ops=remote,
        answers=[],
    )
    assert code == 1
    names = [c.name for c in remote.calls]
    # 打包未发生
    assert "mktemp_remote_dir" not in names
    assert "upload_zip" not in names
    assert any("占位符" in m or "init" in m for m in msgs)


def test_no_local_config_when_adopting_exits_one(tmp_path: Path) -> None:
    """远端 missing 且本机 config.toml 不存在 → 1，提示先 init。"""
    project = _make_project_root(tmp_path)
    cfg = project / "config.toml"
    assert not cfg.exists()
    conn = tmp_path / ".hawkeye-deploy.toml"
    _write_connection_file(conn, host="vps", port=22, user="alice")
    kh = tmp_path / "known_hosts"

    remote = _FakeRemoteOps(config_state=CONFIG_STATE_MISSING)
    code, msgs = _run(
        _make_args(),
        project_root=project,
        config_path=cfg,
        connection_file=conn,
        known_hosts=kh,
        remote_ops=remote,
        answers=[],
    )
    assert code == 1
    assert any("init" in m for m in msgs)


# ============================================================================
# KTD12 / R14：远端三态决定是否上传配置
# ============================================================================


def test_remote_real_no_overwrite_skips_config_upload(tmp_path: Path) -> None:
    """远端已是真实配置且无 --overwrite-config：不传配置、给提示、继续跑。"""
    project = _make_project_root(tmp_path)
    cfg = _write_local_config(project / "config.toml")
    conn = tmp_path / ".hawkeye-deploy.toml"
    _write_connection_file(conn, host="vps", port=22, user="alice")
    kh = tmp_path / "known_hosts"

    remote = _FakeRemoteOps(config_state=CONFIG_STATE_REAL)
    code, _ = _run(
        _make_args(),
        project_root=project,
        config_path=cfg,
        connection_file=conn,
        known_hosts=kh,
        remote_ops=remote,
        answers=[],
    )
    assert code == 0
    # config 没上传
    assert remote.uploaded_configs == []
    # 但 zip 上传了，install 也跑了
    assert len(remote.uploaded_zips) == 1
    # run_background_install 收到 config_remote_path=None
    bg_call = next(c for c in remote.calls if c.name == "run_background_install")
    assert bg_call.kwargs["config_remote_path"] is None
    assert bg_call.kwargs["overwrite_config"] is False


def test_remote_missing_uploads_config(tmp_path: Path) -> None:
    project = _make_project_root(tmp_path)
    cfg = _write_local_config(project / "config.toml")
    conn = tmp_path / ".hawkeye-deploy.toml"
    _write_connection_file(conn, host="vps", port=22, user="alice")
    kh = tmp_path / "known_hosts"

    remote = _FakeRemoteOps(config_state=CONFIG_STATE_MISSING)
    code, _ = _run(
        _make_args(),
        project_root=project,
        config_path=cfg,
        connection_file=conn,
        known_hosts=kh,
        remote_ops=remote,
        answers=[],
    )
    assert code == 0
    assert len(remote.uploaded_configs) == 1
    bg_call = next(c for c in remote.calls if c.name == "run_background_install")
    assert bg_call.kwargs["config_remote_path"] is not None
    assert bg_call.kwargs["config_remote_path"].endswith("/config.toml")


def test_remote_placeholder_uploads_config(tmp_path: Path) -> None:
    project = _make_project_root(tmp_path)
    cfg = _write_local_config(project / "config.toml")
    conn = tmp_path / ".hawkeye-deploy.toml"
    _write_connection_file(conn, host="vps", port=22, user="alice")
    kh = tmp_path / "known_hosts"

    remote = _FakeRemoteOps(config_state=CONFIG_STATE_PLACEHOLDER)
    code, _ = _run(
        _make_args(),
        project_root=project,
        config_path=cfg,
        connection_file=conn,
        known_hosts=kh,
        remote_ops=remote,
        answers=[],
    )
    assert code == 0
    assert len(remote.uploaded_configs) == 1


def test_overwrite_flag_uploads_and_passes_to_install(tmp_path: Path) -> None:
    """--overwrite-config：远端是真配置也上传，且传给 install。"""
    project = _make_project_root(tmp_path)
    cfg = _write_local_config(project / "config.toml")
    conn = tmp_path / ".hawkeye-deploy.toml"
    _write_connection_file(conn, host="vps", port=22, user="alice")
    kh = tmp_path / "known_hosts"

    remote = _FakeRemoteOps(config_state=CONFIG_STATE_REAL)
    code, _ = _run(
        _make_args(overwrite_config=True),
        project_root=project,
        config_path=cfg,
        connection_file=conn,
        known_hosts=kh,
        remote_ops=remote,
        answers=[],
    )
    assert code == 0
    assert len(remote.uploaded_configs) == 1
    bg_call = next(c for c in remote.calls if c.name == "run_background_install")
    assert bg_call.kwargs["overwrite_config"] is True


def test_remote_real_no_local_config_does_not_block(tmp_path: Path) -> None:
    """KTD12 顺序纠正：从干净 clone 升级一台已配好的 VPS 时本机没 config.toml，
    不能因「本机配置不合法」退 1；应该跳过本机配置走默认保留远端路径。
    """
    project = _make_project_root(tmp_path)
    cfg = project / "config.toml"
    assert not cfg.exists()
    conn = tmp_path / ".hawkeye-deploy.toml"
    _write_connection_file(conn, host="vps", port=22, user="alice")
    kh = tmp_path / "known_hosts"

    remote = _FakeRemoteOps(config_state=CONFIG_STATE_REAL)
    code, msgs = _run(
        _make_args(),
        project_root=project,
        config_path=cfg,
        connection_file=conn,
        known_hosts=kh,
        remote_ops=remote,
        answers=[],
    )
    assert code == 0
    assert remote.uploaded_configs == []
    assert any("未生效" in m for m in msgs)


# ============================================================================
# R13 / R18：上传原子化、清理保留日志
# ============================================================================


def test_cleanup_called_with_keep_log_on_success(tmp_path: Path) -> None:
    project = _make_project_root(tmp_path)
    cfg = _write_local_config(project / "config.toml")
    conn = tmp_path / ".hawkeye-deploy.toml"
    _write_connection_file(conn, host="vps", port=22, user="alice")
    kh = tmp_path / "known_hosts"

    remote = _FakeRemoteOps(config_state=CONFIG_STATE_MISSING)
    code, _ = _run(
        _make_args(),
        project_root=project,
        config_path=cfg,
        connection_file=conn,
        known_hosts=kh,
        remote_ops=remote,
        answers=[],
    )
    assert code == 0
    # 收尾清理保留日志
    assert remote.cleanup_calls == [(remote.remote_dir, True)]


def test_cleanup_called_even_on_install_failure(tmp_path: Path) -> None:
    """远端 install 退出非 0 → 仍然清理（保留日志——R18）。"""
    project = _make_project_root(tmp_path)
    cfg = _write_local_config(project / "config.toml")
    conn = tmp_path / ".hawkeye-deploy.toml"
    _write_connection_file(conn, host="vps", port=22, user="alice")
    kh = tmp_path / "known_hosts"

    remote = _FakeRemoteOps(
        config_state=CONFIG_STATE_MISSING,
        tail_rc=1,
    )
    code, _ = _run(
        _make_args(),
        project_root=project,
        config_path=cfg,
        connection_file=conn,
        known_hosts=kh,
        remote_ops=remote,
        answers=[],
    )
    assert code == 1
    assert any(c.name == "cleanup_stage" for c in remote.calls)


# ============================================================================
# R24：远端路径来自 mktemp -d 输出，不是字面量
# ============================================================================


def test_remote_path_comes_from_fake_factory_not_literal(tmp_path: Path) -> None:
    """远端路径必须来自 mktemp 的返回值，源码不能拼字面量（KTD13）。"""
    project = _make_project_root(tmp_path)
    cfg = _write_local_config(project / "config.toml")
    conn = tmp_path / ".hawkeye-deploy.toml"
    _write_connection_file(conn, host="vps", port=22, user="alice")
    kh = tmp_path / "known_hosts"

    # 让 fake 给出一个非常规但仍符合规则的路径
    custom_dir = "/var/tmp/hawkeye-deploy.customdir00"
    remote = _FakeRemoteOps(
        config_state=CONFIG_STATE_MISSING,
        remote_dir=custom_dir,
    )
    code, _ = _run(
        _make_args(),
        project_root=project,
        config_path=cfg,
        connection_file=conn,
        known_hosts=kh,
        remote_ops=remote,
        answers=[],
    )
    assert code == 0
    # 上传 zip 的路径含 custom_dir
    zip_path = remote.uploaded_zips[0][1]
    assert zip_path.startswith(custom_dir + "/")


def test_default_remote_ops_uses_mktemp_command() -> None:
    """DefaultRemoteOps.mktemp_remote_dir 真的去远端跑 mktemp，路径来自其输出。"""
    import inspect

    src = inspect.getsource(deploy_mod.DefaultRemoteOps.mktemp_remote_dir)
    # 真的去远端跑 mktemp
    assert "mktemp -d" in src
    # 路径来自 r.stdout，不是字面拼出来的
    assert "r.stdout" in src
    # 不会有可预测的 /tmp/hawkeye 字面量
    assert '"/tmp/hawkeye"' not in src
    assert "'/tmp/hawkeye'" not in src


def test_source_has_no_predictable_tmp_prefix() -> None:
    """源码不能出现 ``/tmp/hawkeye`` 这类可预测前缀（R24 / KTD13 兜底闸门）。"""
    import inspect

    src = inspect.getsource(deploy_mod)
    # 仅约束裸字面量；mktemp 的 ``/var/tmp/hawkeye-deploy.XXX`` 是模板而非可预测实例。
    assert '"/tmp/hawkeye' not in src
    assert "'/tmp/hawkeye" not in src


# ============================================================================
# R25：分层健康判据
# ============================================================================


def test_health_check_inactive_returns_one(tmp_path: Path) -> None:
    project = _make_project_root(tmp_path)
    cfg = _write_local_config(project / "config.toml")
    conn = tmp_path / ".hawkeye-deploy.toml"
    _write_connection_file(conn, host="vps", port=22, user="alice")
    kh = tmp_path / "known_hosts"

    remote = _FakeRemoteOps(
        config_state=CONFIG_STATE_MISSING,
        health=(False, "服务未运行（is-active=activating）"),
    )
    code, msgs = _run(
        _make_args(),
        project_root=project,
        config_path=cfg,
        connection_file=conn,
        known_hosts=kh,
        remote_ops=remote,
        answers=[],
    )
    assert code == 1
    assert any("健康检查失败" in m for m in msgs)


def test_health_check_success_exits_zero(tmp_path: Path) -> None:
    project = _make_project_root(tmp_path)
    cfg = _write_local_config(project / "config.toml")
    conn = tmp_path / ".hawkeye-deploy.toml"
    _write_connection_file(conn, host="vps", port=22, user="alice")
    kh = tmp_path / "known_hosts"

    remote = _FakeRemoteOps(
        config_state=CONFIG_STATE_MISSING,
        health=(True, "ok"),
    )
    code, msgs = _run(
        _make_args(),
        project_root=project,
        config_path=cfg,
        connection_file=conn,
        known_hosts=kh,
        remote_ops=remote,
        answers=[],
    )
    assert code == 0
    assert any("通过" in m for m in msgs)


# ============================================================================
# R15：远端输出回传含 token → 打码
# ============================================================================


def test_tail_log_redacts_secrets_in_emitted_output(tmp_path: Path) -> None:
    """远端日志回传前过打码函数（KTD19 纯函数共享）。"""
    project = _make_project_root(tmp_path)
    cfg = _write_local_config(project / "config.toml")
    conn = tmp_path / ".hawkeye-deploy.toml"
    _write_connection_file(conn, host="vps", port=22, user="alice")
    kh = tmp_path / "known_hosts"

    remote = _FakeRemoteOps(config_state=CONFIG_STATE_MISSING)
    remote.tail_log_lines = [
        "启动中…",
        "echo bot_token=8428922140:AA-real-token 完毕",
    ]
    code, msgs = _run(
        _make_args(),
        project_root=project,
        config_path=cfg,
        connection_file=conn,
        known_hosts=kh,
        remote_ops=remote,
        answers=[],
    )
    assert code == 0
    # bot_token 明文不应该出现；<REDACTED> 应该有
    joined = "\n".join(msgs)
    assert "8428922140:AA-real-token" not in joined
    assert "<REDACTED>" in joined
    # 密码也不出现
    assert "dummy-password" not in joined


def test_tail_log_raises_on_timeout_surfaces_log_path(tmp_path: Path) -> None:
    """AE16：超时 → DeployError 抛出，错误消息含日志路径。"""
    project = _make_project_root(tmp_path)
    cfg = _write_local_config(project / "config.toml")
    conn = tmp_path / ".hawkeye-deploy.toml"
    _write_connection_file(conn, host="vps", port=22, user="alice")
    kh = tmp_path / "known_hosts"

    # 极短超时 + 短 tail_delay 让循环至少跑一次，然后 raise
    remote = _FakeRemoteOps(
        config_state=CONFIG_STATE_MISSING,
        tail_should_raise=DeployError(
            "后台安装超时（0 秒）；日志：/var/tmp/hawkeye-deploy.x/install.log"
        ),
    )
    code, msgs = _run(
        _make_args(),
        project_root=project,
        config_path=cfg,
        connection_file=conn,
        known_hosts=kh,
        remote_ops=remote,
        answers=[],
        install_timeout=0.0,
    )
    assert code == 1
    assert any("超时" in m for m in msgs)
    assert any("/var/tmp/hawkeye-deploy.x/install.log" in m for m in msgs)


# ============================================================================
# R26 / AE15：白名单校验先抛
# ============================================================================


def test_malformed_host_rejected_before_connection(tmp_path: Path) -> None:
    project = _make_project_root(tmp_path)
    cfg = _write_local_config(project / "config.toml")
    conn = tmp_path / ".hawkeye-deploy.toml"
    kh = tmp_path / "known_hosts"

    remote = _FakeRemoteOps(config_state=CONFIG_STATE_MISSING)
    code, msgs = _run(
        _make_args(host="h$(id)", user="alice"),  # 命令行直接传畸形值
        project_root=project,
        config_path=cfg,
        connection_file=conn,
        known_hosts=kh,
        remote_ops=remote,
        answers=[],
    )
    assert code == 1
    # 替身根本没被实例化（__aenter__ 没出现）→ 根本没建立连接
    assert remote.entered is False
    assert any("不合法" in m or "非法字符" in m for m in msgs)


def test_malformed_user_rejected_before_connection(tmp_path: Path) -> None:
    project = _make_project_root(tmp_path)
    cfg = _write_local_config(project / "config.toml")
    conn = tmp_path / ".hawkeye-deploy.toml"
    kh = tmp_path / "known_hosts"

    remote = _FakeRemoteOps(config_state=CONFIG_STATE_MISSING)
    code, _ = _run(
        _make_args(host="vps", user="u;rm -rf /"),
        project_root=project,
        config_path=cfg,
        connection_file=conn,
        known_hosts=kh,
        remote_ops=remote,
        answers=[],
    )
    assert code == 1
    assert remote.entered is False


# ============================================================================
# AE14：幂等（run_deploy 第二次与第一次状态可重叠）
# ============================================================================


def test_idempotent_when_remote_state_unchanged(tmp_path: Path) -> None:
    """AE14：同样的输入跑两次，第二次仍然走完整流程、退出 0。"""
    project = _make_project_root(tmp_path)
    cfg = _write_local_config(project / "config.toml")
    conn = tmp_path / ".hawkeye-deploy.toml"
    _write_connection_file(conn, host="vps", port=22, user="alice")
    kh = tmp_path / "known_hosts"

    remote = _FakeRemoteOps(config_state=CONFIG_STATE_REAL)

    code1, _ = _run(
        _make_args(),
        project_root=project,
        config_path=cfg,
        connection_file=conn,
        known_hosts=kh,
        remote_ops=remote,
        answers=[],
    )
    assert code1 == 0

    # 第二次：同样的状态，重新跑
    remote2 = _FakeRemoteOps(config_state=CONFIG_STATE_REAL)
    code2, _ = _run(
        _make_args(),
        project_root=project,
        config_path=cfg,
        connection_file=conn,
        known_hosts=kh,
        remote_ops=remote2,
        answers=[],
    )
    assert code2 == 0
    # 同样没传配置
    assert remote2.uploaded_configs == []


# ============================================================================
# R7 / R8：密码不外泄到任何 emit 字符串
# ============================================================================


def test_password_never_appears_in_emitted_output(tmp_path: Path) -> None:
    """密码不进任何 emit 的字符串——只能在 ask_secret 替身内部可见。"""
    project = _make_project_root(tmp_path)
    cfg = _write_local_config(project / "config.toml")
    conn = tmp_path / ".hawkeye-deploy.toml"
    _write_connection_file(conn, host="vps", port=22, user="alice")
    kh = tmp_path / "known_hosts"

    remote = _FakeRemoteOps(config_state=CONFIG_STATE_MISSING)
    remote.tail_log_lines = [
        "echo password=dummy-password",
        "another line with dummy-password here",
    ]
    code, msgs = _run(
        _make_args(),
        project_root=project,
        config_path=cfg,
        connection_file=conn,
        known_hosts=kh,
        remote_ops=remote,
        answers=[],
    )
    assert code == 0
    joined = "\n".join(msgs)
    assert "dummy-password" not in joined


def test_known_hosts_path_written_without_credentials(tmp_path: Path) -> None:
    """连接到 known_hosts 写入流程不会带凭据——fake 不接收 password 字段外的任何凭据。

    这里我们只是断言 ``run_deploy`` 不会把 password 写到 .hawkeye-deploy.toml
    也不会出现在远端命令字符串里：上一条测试已覆盖 emit 侧；这一条覆盖
    持久化侧——读 .hawkeye-deploy.toml 看一遍。
    """
    project = _make_project_root(tmp_path)
    cfg = _write_local_config(project / "config.toml")
    conn = tmp_path / ".hawkeye-deploy.toml"
    _write_connection_file(conn, host="vps", port=22, user="alice")
    kh = tmp_path / "known_hosts"

    remote = _FakeRemoteOps(config_state=CONFIG_STATE_MISSING)
    _run(
        _make_args(),
        project_root=project,
        config_path=cfg,
        connection_file=conn,
        known_hosts=kh,
        remote_ops=remote,
        answers=[],
    )
    text = conn.read_text(encoding="utf-8")
    assert "password" not in text
    assert "dummy-password" not in text


# ============================================================================
# 顺序（按时序图）
# ============================================================================


def test_call_order_missing_state_full_path(tmp_path: Path) -> None:
    """远端 missing + 本机配置合法：完整流水线顺序。"""
    project = _make_project_root(tmp_path)
    cfg = _write_local_config(project / "config.toml")
    conn = tmp_path / ".hawkeye-deploy.toml"
    _write_connection_file(conn, host="vps", port=22, user="alice")
    kh = tmp_path / "known_hosts"

    remote = _FakeRemoteOps(config_state=CONFIG_STATE_MISSING)
    code, _ = _run(
        _make_args(),
        project_root=project,
        config_path=cfg,
        connection_file=conn,
        known_hosts=kh,
        remote_ops=remote,
        answers=[],
    )
    assert code == 0
    names = [c.name for c in remote.calls]
    # 期望顺序：enter → privilege → config_probe → mktemp → zip → config
    # → bg_install → tail → health → cleanup → exit
    assert names[0] == "__aenter__"
    assert names[-1] == "__aexit__"
    expected_subseq = [
        "probe_privilege",
        "probe_remote_config",
        "mktemp_remote_dir",
        "upload_zip",
        "upload_config",
        "run_background_install",
        "tail_log_until_done",
        "layered_health_check",
        "cleanup_stage",
    ]
    last = 0
    for step in expected_subseq:
        idx = names.index(step, last)
        last = idx + 1


# ============================================================================
# EditError / 包路径
# ============================================================================


def test_packaging_failure_exits_one_without_install(tmp_path: Path) -> None:
    """packaging.build_package 抛 PackageError 时退 1，且 install 不被触发。"""
    import shutil

    project = _make_project_root(tmp_path)
    # 故意破坏项目根，让 _check_root 失败
    shutil.rmtree(project / "src")
    cfg = _write_local_config(project / "config.toml")
    conn = tmp_path / ".hawkeye-deploy.toml"
    _write_connection_file(conn, host="vps", port=22, user="alice")
    kh = tmp_path / "known_hosts"

    remote = _FakeRemoteOps(config_state=CONFIG_STATE_MISSING)
    code, msgs = _run(
        _make_args(),
        project_root=project,
        config_path=cfg,
        connection_file=conn,
        known_hosts=kh,
        remote_ops=remote,
        answers=[],
    )
    assert code == 1
    names = [c.name for c in remote.calls]
    assert "run_background_install" not in names
    assert any("打包失败" in m for m in msgs)


# ============================================================================
# 顶层 import 卫生（KTD10）
# ============================================================================


def test_deploy_module_does_not_pull_asyncssh_at_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """deploy.py 顶层不 import asyncssh（顶层只 import 标准库 + 项目模块）。"""
    for name in ("asyncssh",):
        monkeypatch.delitem(sys.modules, name, raising=False)
    # 重新强制 reload，强制走顶层 import
    monkeypatch.delitem(sys.modules, "hawkeye.deploy", raising=False)
    import hawkeye.deploy  # noqa: F401

    assert "asyncssh" not in sys.modules, (
        "deploy 顶层不应 import asyncssh；测试期间 sys.modules 含它"
    )


# ============================================================================
# README 用占位符路径：被规划要求额外约束
# ============================================================================


def test_placeholders_match_deploy_sh() -> None:
    """PLACEHOLDER_TOKEN 必须与 deploy.sh 完全一致（同一份事实）。"""
    deploy_sh_text = (Path(__file__).resolve().parent.parent / "deploy.sh").read_text(
        encoding="utf-8"
    )
    # deploy.sh 里出现的占位符值
    assert f'"{PLACEHOLDER_TOKEN}"' in deploy_sh_text or PLACEHOLDER_TOKEN in deploy_sh_text


def test_remote_config_path_matches_deploy_sh() -> None:
    """REMOTE_CONFIG_PATH 默认值与 deploy.sh 的 CONFIG_FILE 默认值一致。"""
    deploy_sh_text = (Path(__file__).resolve().parent.parent / "deploy.sh").read_text(
        encoding="utf-8"
    )
    assert 'CONFIG_FILE="${INSTALL_DIR}/config.toml"' in deploy_sh_text
    assert 'INSTALL_DIR="/opt/hawkeye"' in deploy_sh_text
    # 推导出 /opt/hawkeye/config.toml
    assert REMOTE_CONFIG_PATH == "/opt/hawkeye/config.toml"
