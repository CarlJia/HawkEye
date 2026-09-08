"""ssh 模块测试：TOFU 主机密钥、提权探测、上传原子化、远端打码、依赖方向。

按计划 U3 的覆盖范围：redact 纯函数、TOFU 三分支、提权四档、上传顺序、
白名单校验先抛、known_hosts 四条卫生规则、依赖方向（notify.py 不 import
ssh.py）。真实 SSH 握手与 SFTP 不在自动化范围。
"""

from __future__ import annotations

import inspect
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from hawkeye import notify as notify_mod
from hawkeye import ssh as ssh_mod
from hawkeye.notify import redact
from hawkeye.ssh import (
    HostKeyMismatchError,
    HostKeyRejectedError,
    SSHError,
    SudoUnavailableError,
    append_known_hosts,
    compute_fingerprint,
    fingerprint_from_known_hosts_line,
    lookup_known_hosts,
    make_known_hosts_entry,
    probe_privilege,
    run_script,
    tofu_then_connect,
    upload_part_then_mv,
    validate_safe,
)

# ---- 替身 ----


class _FakeKey:
    """模拟 asyncssh 返回的 SSHKey 对象。"""

    def __init__(self, algorithm: str, public_bytes: bytes, b64: str) -> None:
        self.algorithm = algorithm
        self._public_bytes = public_bytes
        self._b64 = b64

    def export_public(self) -> bytes:
        return self._public_bytes

    def get_base64(self) -> str:
        return self._b64


class _FakeResult:
    """模拟 asyncssh SSHClientProcess 的最小子集。"""

    def __init__(self, stdout: str = "", stderr: str = "", exit_status: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_status = exit_status


class _FakeStdin:
    """模拟 SSHWriterProcess.stdin：记录写入与 eof。"""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.eof_called = False

    def write(self, data: str | bytes) -> None:
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.writes.append(data)

    def write_eof(self) -> None:
        self.eof_called = True


class _FakeProcess:
    """模拟 SSHServerProcess：可控 stdin / wait / exit_status。"""

    def __init__(self, exit_status: int = 0) -> None:
        self.stdin = _FakeStdin()
        self.exit_status = exit_status
        self.waited = False

    async def wait(self) -> None:
        self.waited = True


class _FakeConn:
    """模拟 SSHClientConnection：记录所有 run / create_process 调用。"""

    def __init__(
        self,
        *,
        id_u_stdout: str = "0",
        id_u_exit: int = 0,
        sudo_n_exit: int = 1,
        sudo_s_exit: int = 0,
    ) -> None:
        self.run_calls: list[dict[str, Any]] = []
        self.create_process_calls: list[dict[str, Any]] = []
        self._id_u_stdout = id_u_stdout
        self._id_u_exit = id_u_exit
        self._sudo_n_exit = sudo_n_exit
        self._sudo_s_exit = sudo_s_exit
        # 顺序：id -u、sudo -n true、sudo -S -p '' true
        self._sudo_s_process = _FakeProcess(exit_status=sudo_s_exit)

    async def run(self, command: str, *args: Any, **kwargs: Any) -> _FakeResult:
        self.run_calls.append({"command": command, "args": args, "kwargs": kwargs})
        if command == "id -u":
            return _FakeResult(stdout=self._id_u_stdout, exit_status=self._id_u_exit)
        if command == "sudo -n true":
            return _FakeResult(exit_status=self._sudo_n_exit)
        # 未知命令
        return _FakeResult(exit_status=1, stderr=f"unknown command: {command}")

    async def create_process(self, command: str, *args: Any, **kwargs: Any) -> _FakeProcess:
        self.create_process_calls.append({"command": command, "args": args, "kwargs": kwargs})
        return self._sudo_s_process


class _FakeSFTP:
    """模拟 SFTPClient：记录 open/chmod/posix_rename 顺序与参数。"""

    def __init__(self) -> None:
        self.ops: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.files: dict[str, bytes] = {}  # path -> content
        self.modes: dict[str, int] = {}  # path -> mode after chmod
        # 记录 'x' 模式创建后的 chmod 是否在写入之前
        self.x_then_chmod_before_write = False
        self._track_write_order = False

    # async context manager 支持：``async with sftp.open(path, 'w') as f:``
    class _FakeFile:
        def __init__(self, sftp: _FakeSFTP, path: str, mode: str) -> None:
            self._sftp = sftp
            self._path = path
            self._mode = mode
            self.written: list[bytes] = []
            self.closed = False

        async def write(self, data: str | bytes) -> None:
            if isinstance(data, str):
                data = data.encode("utf-8")
            self.written.append(data)

        async def __aenter__(self) -> _FakeSFTP._FakeFile:
            self._sftp.ops.append(("open", (self._path, self._mode), {}))
            # 若之前已经 'x' 创建过此 path，且 mode 为 'wb'，记一笔顺序证据。
            if self._mode == "wb" and self._sftp.modes.get(self._path) == 0o600:
                self._sftp.x_then_chmod_before_write = True
            return self

        async def __aexit__(self, *exc: Any) -> None:
            self.closed = True
            self._sftp.files[self._path] = b"".join(self.written)

    def open(self, path: str, mode: str = "r") -> _FakeSFTP._FakeFile:
        return _FakeSFTP._FakeFile(self, path, mode)

    async def chmod(self, path: str, mode: int) -> None:
        self.ops.append(("chmod", (path, mode), {}))
        self.modes[path] = mode

    async def posix_rename(self, src: str, dst: str) -> None:
        self.ops.append(("posix_rename", (src, dst), {}))
        self.files[dst] = self.files.pop(src, b"")


class _FakeAsyncssh:
    """最小化的 asyncssh 替身：记录 connect / get_server_host_key 调用。"""

    def __init__(self, key: _FakeKey, conn: Any) -> None:
        self._key = key
        self._conn = conn
        self.get_server_host_key_calls: list[tuple[str, int]] = []
        self.connect_calls: list[dict[str, Any]] = []

    def get_server_host_key(self, host: str, port: int = 22) -> _FakeKey:
        self.get_server_host_key_calls.append((host, port))
        return self._key

    async def connect(self, host: str, port: int = 22, **kwargs: Any) -> Any:
        self.connect_calls.append({"host": host, "port": port, "kwargs": kwargs})
        return self._conn


# ---- 路径工具 ----


def _make_known_hosts(tmp_path: Path, content: str = "") -> Path:
    p = tmp_path / ".ssh" / "known_hosts"
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    p.write_text(content)
    p.chmod(0o600)
    return p


def _confirm_factory(answer: bool) -> Callable[[str], bool]:
    asked: list[str] = []

    def _confirm(prompt: str) -> bool:
        asked.append(prompt)
        return answer

    _confirm.asked = asked  # type: ignore[attr-defined]
    return _confirm


# ---- redact 纯函数（AE12、R15、KTD19）----


def test_redact_replaces_token_and_password() -> None:
    text = "remote log: bot_token=8428922140:AA-secret; password=hunter2; ok"
    out = redact(text, ["8428922140:AA-secret", "hunter2"])
    assert "8428922140:AA-secret" not in out
    assert "hunter2" not in out
    assert "<REDACTED>" in out
    assert out.count("<REDACTED>") == 2


def test_redact_unchanged_for_empty_secrets() -> None:
    """空密钥集合 → 原文不变；不产生逐字符替换。"""
    text = "no secrets here\nsecond line"
    assert redact(text, []) == text
    assert redact(text, [""]) == text
    assert redact(text, [None, "", ""]) == text  # type: ignore[list-item]


def test_redact_pure_function() -> None:
    """纯函数：调用前后入参不被修改；多次调用结果一致。"""
    text = "tok=ABC123 pw=secret"
    secrets = ["ABC123", "secret"]
    out1 = redact(text, secrets)
    out2 = redact(text, secrets)
    assert out1 == out2 == "tok=<REDACTED> pw=<REDACTED>"
    assert text == "tok=ABC123 pw=secret"  # 入参未被修改


def test_redact_handles_overlap_consistently() -> None:
    """secret 是另一个 secret 的子串时仍能各自正确替换（不会因为顺序问题漏替换）。"""
    out = redact("token=abcdef-extra password=abc", ["abcdef-extra", "abc"])
    assert "abcdef-extra" not in out
    # abc 在原文中出现两次（一次作为 secret 自身、一次作为 "abcdef-extra" 的子串）
    # 由于 "abcdef-extra" 先被替成 <REDACTED>，原 "abc" 子串也不再出现。
    assert "abcdef" not in out
    assert "<REDACTED>" in out


def test_redact_is_same_object_in_notify_and_ssh() -> None:
    """ssh.py 与 notify.py 共用同一个 redact 函数（KTD19）。"""
    from hawkeye.ssh import redact as ssh_redact

    assert redact is ssh_redact
    assert redact is notify_mod.redact


def test_notify_source_does_not_import_ssh() -> None:
    """方向闸门：notify.py 绝不能 import ssh.py（KTD19）。

    ``from .ssh`` / ``import ssh`` / ``from hawkeye.ssh`` 三种写法任一出现
    都会让 VPS 上的守护进程在 import 阶段崩（那边没装 asyncssh）。
    """
    notify_src = inspect.getsource(notify_mod)
    assert "from .ssh" not in notify_src
    assert "from hawkeye.ssh" not in notify_src
    assert "import ssh" not in notify_src


# ---- 白名单校验（R26）----


def test_validate_safe_rejects_shell_metachars() -> None:
    with pytest.raises(ValueError, match="非法字符"):
        validate_safe("h$(id)")
    with pytest.raises(ValueError):
        validate_safe("1.0; rm -rf /x")
    with pytest.raises(ValueError):
        validate_safe("user with space")
    with pytest.raises(ValueError):
        validate_safe("../etc/passwd")
    with pytest.raises(ValueError):
        validate_safe("")


def test_validate_safe_accepts_normal_values() -> None:
    # 不抛
    validate_safe("myhost")
    validate_safe("user.name")
    validate_safe("user_name")
    validate_safe("user-name")
    validate_safe("1.2.3")


def test_validate_safe_allows_colon_in_host() -> None:
    # host 允许 :（IPv6，如 ``::1``）。``[host]:port`` 是 known_hosts 行的格式，
    # 不是 host 的值；用户传进来的 host 不该带方括号。
    validate_safe("::1", allow_colon=True)
    validate_safe("fe80::1", allow_colon=True)
    # 但不允许除 : 外的 shell 元字符
    with pytest.raises(ValueError):
        validate_safe("h$(id)", allow_colon=True)
    with pytest.raises(ValueError):
        validate_safe("h;rm", allow_colon=True)
    with pytest.raises(ValueError):
        validate_safe("[v6]:2222", allow_colon=True)  # 方括号不在白名单


# ---- asyncssh 惰性导入（R22）----


def test_import_asyncssh_chinese_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有 asyncssh 时给出中文安装指引，而不是 traceback（R22）。"""
    import builtins

    real_import = builtins.__import__

    def _import_blocker(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "asyncssh" or name.startswith("asyncssh."):
            raise ImportError("No module named 'asyncssh'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _import_blocker)
    with pytest.raises(SSHError) as excinfo:
        ssh_mod._import_asyncssh()
    msg = str(excinfo.value)
    assert "pip install" in msg
    assert "deploy" in msg
    assert "asyncssh" in msg


def test_import_asyncssh_surfaces_internal_crypto_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """asyncssh 内部 ``import cryptography`` 失败时也给同一句中文提示。

    cryptography 的原生扩展加载失败会冒泡为 ImportError；用户感知到的应该
    是「请装可选依赖」，而不是 cryptography 自身的 traceback。
    """
    import builtins

    real_import = builtins.__import__

    def _import_blocker(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "asyncssh" or name.startswith("asyncssh."):
            raise ImportError("No module named 'cryptography'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _import_blocker)
    with pytest.raises(SSHError) as excinfo:
        ssh_mod._import_asyncssh()
    assert "pip install" in str(excinfo.value)


# ---- 主机密钥指纹（KTD22）----


def test_compute_fingerprint_known_value() -> None:
    """与 ``ssh-keygen -lf`` 一致：``SHA256:<base64(rstrip '=')>``。"""
    # 用 SHA256("hello") 的字节验证公式
    import base64
    import hashlib

    raw = b"hello"
    expected_b64 = base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")
    assert compute_fingerprint("ssh-ed25519", raw) == f"SHA256:{expected_b64}"


# ---- known_hosts 卫生（KTD22）----


def test_make_known_hosts_entry_default_port() -> None:
    line = make_known_hosts_entry("myhost", 22, "ssh-ed25519", "AAAAC3...")
    assert line == "myhost ssh-ed25519 AAAAC3...\n"


def test_make_known_hosts_entry_non_default_port_brackets() -> None:
    line = make_known_hosts_entry("myhost", 2222, "ssh-ed25519", "AAAAC3...")
    assert line == "[myhost]:2222 ssh-ed25519 AAAAC3...\n"


def test_lookup_known_hosts_returns_existing_line(tmp_path: Path) -> None:
    p = _make_known_hosts(tmp_path, "myhost ssh-ed25519 AAAAC3...\n[other]:2222 ssh-rsa BBB\n")
    assert lookup_known_hosts(p, "myhost", 22) == "myhost ssh-ed25519 AAAAC3..."
    assert lookup_known_hosts(p, "other", 2222) == "[other]:2222 ssh-rsa BBB"
    assert lookup_known_hosts(p, "absent", 22) is None


def test_lookup_known_hosts_returns_none_when_file_absent(tmp_path: Path) -> None:
    p = tmp_path / "nope" / "known_hosts"
    assert lookup_known_hosts(p, "myhost", 22) is None


def test_append_known_hosts_preserves_existing(tmp_path: Path) -> None:
    p = _make_known_hosts(tmp_path, "first ssh-ed25519 AAA\n")
    append_known_hosts(p, "second ssh-rsa BBB\n")
    assert p.read_text() == "first ssh-ed25519 AAA\nsecond ssh-rsa BBB\n"


def test_append_known_hosts_adds_newline_when_missing(tmp_path: Path) -> None:
    """末尾无换行 → 追加前先补一个，避免把上一行尾弄坏（KTD22）。"""
    p = _make_known_hosts(tmp_path, "first ssh-ed25519 AAA")  # 末尾无换行
    append_known_hosts(p, "second ssh-rsa BBB\n")
    content = p.read_text()
    assert content == "first ssh-ed25519 AAA\nsecond ssh-rsa BBB\n"
    # 两行各自完整、不粘连
    lines = content.splitlines()
    assert lines == ["first ssh-ed25519 AAA", "second ssh-rsa BBB"]


def test_append_known_hosts_creates_dir_and_file(tmp_path: Path) -> None:
    p = tmp_path / "fresh" / ".ssh" / "known_hosts"
    append_known_hosts(p, "myhost ssh-ed25519 AAA\n")
    assert p.exists()
    # 目录 0700、文件 0600
    if sys.platform != "win32":
        assert (p.parent.stat().st_mode & 0o777) == 0o700
        assert (p.stat().st_mode & 0o777) == 0o600


def test_fingerprint_from_known_hosts_line() -> None:
    import base64
    import hashlib

    raw = b"keybytes"
    b64 = base64.b64encode(raw).decode("ascii")
    line = f"myhost ssh-ed25519 {b64}"
    expected_b64 = base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")
    assert fingerprint_from_known_hosts_line(line) == f"SHA256:{expected_b64}"


def test_fingerprint_from_known_hosts_line_bad_format() -> None:
    # 不足 3 个 token（host + keytype + b64 都凑不齐）
    assert fingerprint_from_known_hosts_line("only two") is None
    # 看起来是 3 个 token 但 b64 不是合法 base64（base64 字符集为 A-Za-z0-9+/=）
    assert fingerprint_from_known_hosts_line("h ssh-ed25519 !@#bad") is None


# ---- TOFU 探测（R9 / KTD22）----


def test_tofu_skip_when_known_with_same_key(tmp_path: Path) -> None:
    """known_hosts 已有该主机且密钥相同 → 不打扰用户，直接连接。"""
    key_bytes = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"\x01" * 32
    import base64

    key_b64 = base64.b64encode(key_bytes).decode("ascii")
    existing_line = f"myhost ssh-ed25519 {key_b64}"
    p = _make_known_hosts(tmp_path, existing_line + "\n")

    key = _FakeKey("ssh-ed25519", key_bytes, key_b64)
    conn = _FakeConn()
    fake = _FakeAsyncssh(key, conn)
    confirm = _confirm_factory(answer=True)

    result = asyncio_run(
        tofu_then_connect(
            "myhost", 22, "user", "secret", p, confirm_fn=confirm, asyncssh_factory=lambda: fake
        )
    )
    assert result is conn
    # 没问用户（密钥一致）
    assert confirm.asked == []  # type: ignore[attr-defined]
    # 探测走 get_server_host_key
    assert fake.get_server_host_key_calls == [("myhost", 22)]
    # 带 known_hosts 重连
    assert len(fake.connect_calls) == 1
    call = fake.connect_calls[0]
    assert call["kwargs"]["username"] == "user"
    assert call["kwargs"]["password"] == "secret"
    assert call["kwargs"]["known_hosts"] == str(p)
    # known_hosts 内容未被改写
    assert p.read_text() == existing_line + "\n"


def test_tofu_probe_then_confirm_then_connect(tmp_path: Path) -> None:
    """known_hosts 没有该主机 → 探测、用户接受、写入、带校验重连。"""
    p = _make_known_hosts(tmp_path, "")  # 空文件
    key_bytes = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"\x02" * 32
    import base64

    key_b64 = base64.b64encode(key_bytes).decode("ascii")
    key = _FakeKey("ssh-ed25519", key_bytes, key_b64)
    conn = _FakeConn()
    fake = _FakeAsyncssh(key, conn)
    confirm = _confirm_factory(answer=True)

    asyncio_run(
        tofu_then_connect(
            "new", 22, "user", "secret", p, confirm_fn=confirm, asyncssh_factory=lambda: fake
        )
    )

    # 用户被问了一次
    assert len(confirm.asked) == 1  # type: ignore[attr-defined]
    # 探测走 get_server_host_key
    assert fake.get_server_host_key_calls == [("new", 22)]
    # 带校验重连
    assert fake.connect_calls[0]["kwargs"]["known_hosts"] == str(p)
    # known_hosts 文件多了一行
    content = p.read_text()
    assert "[new]" not in content  # 22 端口裸 host
    assert "new ssh-ed25519" in content


def test_tofu_user_rejection_aborts(tmp_path: Path) -> None:
    """用户拒绝接受指纹 → 抛 HostKeyRejectedError，不建立带凭据的连接。"""
    p = _make_known_hosts(tmp_path, "")
    key_bytes = b"k" * 32
    import base64

    key_b64 = base64.b64encode(key_bytes).decode("ascii")
    key = _FakeKey("ssh-ed25519", key_bytes, key_b64)
    conn = _FakeConn()
    fake = _FakeAsyncssh(key, conn)
    confirm = _confirm_factory(answer=False)

    with pytest.raises(HostKeyRejectedError):
        asyncio_run(
            tofu_then_connect(
                "new", 22, "user", "secret", p, confirm_fn=confirm, asyncssh_factory=lambda: fake
            )
        )
    # 探测做了，但 connect 没做
    assert fake.get_server_host_key_calls == [("new", 22)]
    assert fake.connect_calls == []
    # known_hosts 文件未被修改
    assert p.read_text() == ""


def test_tofu_mismatch_aborts_and_prints_two_fingerprints(tmp_path: Path) -> None:
    """已有条目但密钥不同 → 抛 HostKeyMismatchError，两个指纹都在消息里。"""
    old_key_bytes = b"\x01" * 32
    new_key_bytes = b"\x02" * 32
    import base64

    old_b64 = base64.b64encode(old_key_bytes).decode("ascii")
    new_b64 = base64.b64encode(new_key_bytes).decode("ascii")
    p = _make_known_hosts(tmp_path, f"myhost ssh-ed25519 {old_b64}\n")

    key = _FakeKey("ssh-ed25519", new_key_bytes, new_b64)
    conn = _FakeConn()
    fake = _FakeAsyncssh(key, conn)
    confirm = _confirm_factory(answer=True)

    with pytest.raises(HostKeyMismatchError) as excinfo:
        asyncio_run(
            tofu_then_connect(
                "myhost", 22, "user", "secret", p, confirm_fn=confirm, asyncssh_factory=lambda: fake
            )
        )

    msg = str(excinfo.value)
    new_fp = compute_fingerprint("ssh-ed25519", new_key_bytes)
    old_fp = compute_fingerprint("ssh-ed25519", old_key_bytes)
    assert new_fp in msg
    assert old_fp in msg

    # known_hosts 文件未被改写
    assert p.read_text() == f"myhost ssh-ed25519 {old_b64}\n"
    # 没建连接
    assert fake.connect_calls == []


def test_tofu_probe_does_not_send_credentials(tmp_path: Path) -> None:
    """探测那一步只调 get_server_host_key，不发送凭据。"""
    p = _make_known_hosts(tmp_path, "")
    key_bytes = b"k" * 32
    import base64

    key_b64 = base64.b64encode(key_bytes).decode("ascii")
    key = _FakeKey("ssh-ed25519", key_bytes, key_b64)
    conn = _FakeConn()
    fake = _FakeAsyncssh(key, conn)
    confirm = _confirm_factory(answer=True)

    asyncio_run(
        tofu_then_connect(
            "new", 22, "user", "secret", p, confirm_fn=confirm, asyncssh_factory=lambda: fake
        )
    )
    # get_server_host_key 的签名里就不存在 password / client_keys 字段
    # （_FakeAsyncssh 替身也只暴露 host / port）。这里再断言一次源码静态事实。
    src = inspect.getsource(ssh_mod)
    assert "get_server_host_key" in src
    # 同时：known_hosts=None 与 password 绝不在同一行出现
    for i, line in enumerate(src.splitlines(), 1):
        if "known_hosts=None" in line and "password" in line:
            pytest.fail(f"ssh.py:{i} 同时出现 known_hosts=None 与 password")


def test_tofu_invalid_host_rejected_before_remote_call(tmp_path: Path) -> None:
    """白名单校验先抛，替身零调用（R26）。"""
    p = _make_known_hosts(tmp_path, "")
    key = _FakeKey("ssh-ed25519", b"k" * 32, "AAAA")
    conn = _FakeConn()
    fake = _FakeAsyncssh(key, conn)
    confirm = _confirm_factory(answer=True)

    with pytest.raises(ValueError, match="非法字符"):
        asyncio_run(
            tofu_then_connect(
                "h$(id)", 22, "user", "secret", p, confirm_fn=confirm, asyncssh_factory=lambda: fake
            )
        )
    assert fake.get_server_host_key_calls == []
    assert fake.connect_calls == []


def test_tofu_invalid_user_rejected_before_remote_call(tmp_path: Path) -> None:
    p = _make_known_hosts(tmp_path, "")
    key = _FakeKey("ssh-ed25519", b"k" * 32, "AAAA")
    conn = _FakeConn()
    fake = _FakeAsyncssh(key, conn)
    confirm = _confirm_factory(answer=True)

    with pytest.raises(ValueError, match="非法字符"):
        asyncio_run(
            tofu_then_connect(
                "myhost",
                22,
                "user;rm",
                "secret",
                p,
                confirm_fn=confirm,
                asyncssh_factory=lambda: fake,
            )
        )
    assert fake.connect_calls == []


def test_tofu_non_22_port_writes_brackets(tmp_path: Path) -> None:
    """非 22 端口 → known_hosts 条目写成 [host]:port（KTD22）。"""
    p = _make_known_hosts(tmp_path, "")
    key_bytes = b"k" * 32
    import base64

    key_b64 = base64.b64encode(key_bytes).decode("ascii")
    key = _FakeKey("ssh-ed25519", key_bytes, key_b64)
    conn = _FakeConn()
    fake = _FakeAsyncssh(key, conn)
    confirm = _confirm_factory(answer=True)

    asyncio_run(
        tofu_then_connect(
            "myhost", 2222, "user", "secret", p, confirm_fn=confirm, asyncssh_factory=lambda: fake
        )
    )
    assert "[myhost]:2222" in p.read_text()
    assert fake.connect_calls[0]["port"] == 2222


# ---- 提权探测（R10）----


def test_probe_privilege_root_user() -> None:
    """id -u 为 0 → 不再 sudo，直接返回。"""
    conn = _FakeConn(id_u_stdout="0", id_u_exit=0, sudo_n_exit=99)
    asyncio_run(probe_privilege(conn, "pw"))
    # 只调了 id -u，没动 sudo
    assert [c["command"] for c in conn.run_calls] == ["id -u"]
    assert conn.create_process_calls == []


def test_probe_privilege_passwordless_sudo() -> None:
    """id -u 非 0 + sudo -n true 成功 → 不喂密码。"""
    conn = _FakeConn(id_u_stdout="1000", id_u_exit=0, sudo_n_exit=0, sudo_s_exit=99)
    asyncio_run(probe_privilege(conn, "pw"))
    assert [c["command"] for c in conn.run_calls] == ["id -u", "sudo -n true"]
    assert conn.create_process_calls == []


def test_probe_privilege_password_sudo() -> None:
    """id -u 非 0 + 免密失败 + sudo -S 成功 → 喂密码 + write_eof。"""
    conn = _FakeConn(id_u_stdout="1000", id_u_exit=0, sudo_n_exit=1, sudo_s_exit=0)
    asyncio_run(probe_privilege(conn, "hunter2"))
    assert [c["command"] for c in conn.run_calls] == ["id -u", "sudo -n true"]
    assert len(conn.create_process_calls) == 1
    proc = conn._sudo_s_process
    assert proc.stdin.writes == [b"hunter2\n"]
    assert proc.stdin.eof_called is True
    assert proc.waited is True


def test_probe_privilege_no_sudo_available() -> None:
    """三档都不通 → SudoUnavailableError，且消息要点出「SSH 密码不能用于 sudo」。"""
    conn = _FakeConn(id_u_stdout="1000", id_u_exit=0, sudo_n_exit=1, sudo_s_exit=1)
    with pytest.raises(SudoUnavailableError) as excinfo:
        asyncio_run(probe_privilege(conn, "pw"))
    msg = str(excinfo.value)
    assert "sudo" in msg
    assert "SSH 密码" in msg or "免密" in msg  # 提示用户检查 sudoers / 密码


def test_probe_privilege_id_u_fails() -> None:
    """id -u 退出非零 → 直接 SudoUnavailableError，不试 sudo。"""
    conn = _FakeConn(id_u_stdout="", id_u_exit=1, sudo_n_exit=99)
    with pytest.raises(SudoUnavailableError):
        asyncio_run(probe_privilege(conn, "pw"))
    assert [c["command"] for c in conn.run_calls] == ["id -u"]
    assert conn.create_process_calls == []


def test_probe_privilege_does_not_request_pty() -> None:
    """任何一档都不传 ``term_type``，不申请 pty（避免密码回显）。"""
    conn = _FakeConn(id_u_stdout="1000", id_u_exit=0, sudo_n_exit=1, sudo_s_exit=0)
    asyncio_run(probe_privilege(conn, "pw"))
    for call in conn.run_calls + conn.create_process_calls:
        assert "term_type" not in call["kwargs"], call


# ---- 上传原子化（R13）----


def test_upload_part_then_mv_order() -> None:
    """上传顺序：写 .part → posix_rename → chmod。"""
    sftp = _FakeSFTP()
    asyncio_run(upload_part_then_mv(sftp, "/tmp/dst", "hello", final_mode=0o644))
    op_names = [op[0] for op in sftp.ops]
    assert op_names == ["open", "posix_rename", "chmod"]
    # open 是 .part 路径
    assert sftp.ops[0][1][0] == "/tmp/dst.part"
    # rename 从 .part 到正式名
    assert sftp.ops[1][1] == ("/tmp/dst.part", "/tmp/dst")
    # chmod 应用到正式名
    assert sftp.ops[2][1] == ("/tmp/dst", 0o644)
    # 内容写到了正式名
    assert sftp.files["/tmp/dst"] == b"hello"


def test_upload_part_then_mv_skips_chmod_when_zero() -> None:
    sftp = _FakeSFTP()
    asyncio_run(upload_part_then_mv(sftp, "/tmp/dst", "x", final_mode=0))
    op_names = [op[0] for op in sftp.ops]
    assert op_names == ["open", "posix_rename"]


def test_upload_config_file_600_before_content() -> None:
    """配置文件：'x' 创建空文件 → chmod 600 → 写内容；顺序由替身记录。"""
    sftp = _FakeSFTP()
    asyncio_run(
        upload_part_then_mv(
            sftp,
            "/etc/hawk/config.toml",
            "secret=1",
            final_mode=0o600,
            ensure_600_before_write=True,
        )
    )
    op_names = [op[0] for op in sftp.ops]
    # 期望：open('x') → chmod → open('wb') → posix_rename → chmod
    assert op_names == ["open", "chmod", "open", "posix_rename", "chmod"]
    # 第一对 open/chmod 是 .part
    assert sftp.ops[0][1] == ("/etc/hawk/config.toml.part", "x")
    assert sftp.ops[1][1] == ("/etc/hawk/config.toml.part", 0o600)
    # 第二次 open 是 'wb'，顺序证据：在 chmod 600 之后
    assert sftp.x_then_chmod_before_write is True
    # 最终权限
    assert sftp.modes["/etc/hawk/config.toml"] == 0o600
    # 内容确实写到了正式名
    assert sftp.files["/etc/hawk/config.toml"] == b"secret=1"


# ---- 远端执行与打码（R15、R26）----


class _FakeRunConn:
    """run_script 用的 conn：记录 run 调用与 stdout。"""

    def __init__(self, stdout: str = "", stderr: str = "", exit_status: int = 0) -> None:
        self.run_calls: list[dict[str, Any]] = []
        self._stdout = stdout
        self._stderr = stderr
        self._exit_status = exit_status

    async def run(self, command: str, *args: Any, **kwargs: Any) -> _FakeResult:
        self.run_calls.append({"command": command, "kwargs": kwargs})
        return _FakeResult(stdout=self._stdout, stderr=self._stderr, exit_status=self._exit_status)


def test_run_script_redacts_output() -> None:
    sftp = _FakeSFTP()
    conn = _FakeRunConn(stdout="bot_token=8428922140:AA-secret ok\n")
    out = asyncio_run(
        run_script(
            sftp,
            conn,
            "#!/bin/sh\necho hi\n",
            remote_dir="/tmp",
            script_basename="step.sh",
            secrets=("8428922140:AA-secret",),
        )
    )
    assert "8428922140:AA-secret" not in out
    assert "<REDACTED>" in out


def test_run_script_uses_bash_quoted_path() -> None:
    """不拼 shell 字符串：远端只用 ``bash <shlex.quote>`` 一个被 quote 的路径（R26）。"""
    sftp = _FakeSFTP()
    conn = _FakeRunConn(stdout="ok")
    asyncio_run(run_script(sftp, conn, "echo hi\n", remote_dir="/tmp", script_basename="s1.sh"))
    assert len(conn.run_calls) == 1
    cmd = conn.run_calls[0]["command"]
    assert cmd.startswith("bash ")
    # 路径被 shlex.quote 包过，含 /tmp/s1.sh
    assert "/tmp/s1.sh" in cmd


def test_run_script_rejects_bad_basename() -> None:
    """脚本文件名也过白名单；含 shell 元字符时抛 ValueError，零远端调用。"""
    sftp = _FakeSFTP()
    conn = _FakeRunConn()
    with pytest.raises(ValueError):
        asyncio_run(
            run_script(sftp, conn, "echo\n", remote_dir="/tmp", script_basename="bad;rm.sh")
        )
    assert conn.run_calls == []
    assert sftp.ops == []


# ---- 工具：``asyncio.run`` 别名（无 pytest-asyncio 时也能跑）----


def asyncio_run(coro: Any) -> Any:
    """``pytest-asyncio`` 模式下也支持 ``async def``，但这里同步入口更直观。

    把这条实现成模块级函数，方便所有测试用同一形态。Python 3.11+ 自带
    ``asyncio.run``，本模块只对它做薄包装。
    """
    import asyncio

    return asyncio.run(coro)
