"""SSH 传输层（asyncssh 封装）。

薄层，只做传输：连接与主机密钥确认（TOFU）、提权探测、SFTP 上传
（``.part`` + ``mv``）、远端执行与流式回传、远端输出打码。**不含**任何
部署编排逻辑——编排由 :mod:`hawkeye.deploy` 负责。

设计纪律：

- **顶层不 import asyncssh**——它在 :func:`_import_asyncssh` 内惰性加载；
  ``ImportError``（含 asyncssh 内部 ``import cryptography`` 原生扩展加载
  失败的情况）转成一条中文提示（含 ``pip install '.[deploy]'``），由
  ``__main__._dispatch_deploy`` 映射为退出码 1（R22）。
- **TOFU 走** :func:`asyncssh.get_server_host_key`——**不是**先
  ``known_hosts=None`` 连一次（那等于在未校验通道上做密码鉴权，中间人
  拿到明文密码；探测只做密钥交换、不接受任何凭据参数，正是 R9 与
  KTD22 的语义）。
- **known_hosts 四条卫生规则**：`~/.ssh` 按 0700 建、文件按 0600 建、
  追加前确认末尾有换行（否则新条目会粘到上一行尾把它一起弄坏）、非 22
  端口写成 ``[host]:port``；同一主机已有**不同**密钥视为潜在中间人，
  打印两个指纹并中止，不静默追加也不覆盖（KTD22）。
- **远端执行一律走脚本文件**——脚本由本机写好经 SFTP 上传，``bash <脚本>``
  执行；确实需要内联的少数场合（例如 ``mktemp -d`` 那一句）每个变量在本
  机用 ``shlex.quote`` 包好（R26）。``host`` / ``user`` / ``version`` 先过
  白名单校验，不合法直接抛，不落到远端命令里。
- **不打码第二份实现**——远端输出走 :func:`hawkeye.notify.redact`，
  ``TokenRedactingFactory`` 也调它（KTD19）。
- **不申请 pty**——``conn.run`` / ``conn.create_process`` 不传
  ``term_type``，避免密码被回显进伪终端；喂密码后立即
  ``stdin.write_eof()``，否则 ``sudo`` 会等更多输入而挂住（R10）。
- **方向闸门**：本模块 ``from .notify import redact`` 是允许的（运行时层
  之间的依赖）；``notify.py`` **绝不能** import 本模块——VPS 上的守护
  进程那边没装 asyncssh，import 阶段就会崩。测试以静态文本扫描兜底
  （``tests/test_ssh.py::test_notify_does_not_import_ssh``）。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import shlex
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .notify import redact

logger = logging.getLogger(__name__)

_INSTALL_HINT = "请先安装可选依赖：`pip install '.[deploy]'`（含 asyncssh）。"

# 白名单字符集：host/user/version 都只允许这些字符。
# host 额外允许 : 用于 IPv6（如 `[::1]:2222`）以及 [host]:port 形式。
_SAFE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE_WITH_COLON_RE = re.compile(r"^[A-Za-z0-9._:-]+$")

# 默认 SSH 端口；非 22 端口的 known_hosts 条目要写成 ``[host]:port``。
_DEFAULT_SSH_PORT = 22


# ---- 错误类型 ----


class SSHError(Exception):
    """SSH 层所有可恢复错误的基类；具体子类带更明确的语义。"""


class HostKeyMismatchError(SSHError):
    """known_hosts 中同一主机已存在不同密钥——疑似中间人。"""


class HostKeyRejectedError(SSHError):
    """用户拒绝接受新的主机指纹。"""


class SudoUnavailableError(SSHError):
    """远端无法用 SSH 密码提权；消息要点出这一点（A3）。"""


# ---- 白名单校验（R26）----


def validate_safe(value: str, *, allow_colon: bool = False) -> None:
    """校验 ``value`` 仅含白名单字符；不合法直接抛 :class:`ValueError`。

    ``host`` 额外允许 ``:``（IPv6 / ``[host]:port`` 形态）。校验在所有远端
    调用之前完成——畸形值绝不落到任何 ``conn.run`` 的字符串里。
    """
    if not value:
        raise ValueError("值不能为空")
    pattern = _SAFE_WITH_COLON_RE if allow_colon else _SAFE_RE
    if not pattern.match(value):
        raise ValueError(f"值含非法字符：{value!r}")


# ---- asyncssh 惰性导入 ----


def _import_asyncssh() -> Any:
    """惰性导入 asyncssh；``ImportError`` 转成中文提示（R22）。

    这条 ``ImportError`` 也可能来自 asyncssh 内部 ``import cryptography``
    时原生扩展加载失败——那同样需要给安装指引，而不是 traceback。
    """
    try:
        import asyncssh  # noqa: PLC0415 —— 故意惰性（R22）
    except ImportError as e:
        raise SSHError(_INSTALL_HINT) from e
    return asyncssh


# ---- 主机密钥指纹（KTD22）----


def compute_fingerprint(key_type: str, public_key_bytes: bytes) -> str:
    """计算 SSH 公钥指纹：``SHA256:<base64>``，与 ``ssh-keygen -lf`` 一致。

    输入是公钥的原始字节（不是 base64 编码），与 OpenSSH 的 ``base64(SHA256(key))``
    定义一致。
    """
    digest = hashlib.sha256(public_key_bytes).digest()
    b64 = base64.b64encode(digest).decode("ascii").rstrip("=")
    return f"SHA256:{b64}"


# ---- known_hosts 卫生（KTD22）----


def _ensure_known_hosts_dir(path: Path) -> None:
    """``~/.ssh`` 0700；``known_hosts`` 不存在则建、存在则收紧到 0600。"""
    ssh_dir = path.parent
    if not ssh_dir.exists():
        ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        path.touch(mode=0o600)
    else:
        path.chmod(0o600)


def make_known_hosts_entry(host: str, port: int, key_type: str, key_base64: str) -> str:
    """合成一条 known_hosts 条目；非 22 端口写成 ``[host]:port``（KTD22）。"""
    validate_safe(host, allow_colon=True)
    if port == _DEFAULT_SSH_PORT:
        return f"{host} {key_type} {key_base64}\n"
    return f"[{host}]:{port} {key_type} {key_base64}\n"


def _known_hosts_prefix(host: str, port: int) -> str:
    """known_hosts 一行里 host 部分的前缀（含端口），不含 ``key_type``。"""
    if port == _DEFAULT_SSH_PORT:
        return f"{host} "
    return f"[{host}]:{port} "


def lookup_known_hosts(path: Path, host: str, port: int) -> str | None:
    """返回该主机的现有条目（不含尾换行）；无则 ``None``。

    按行扫描，命中 ``host`` 前缀即返回该行原文。不解析 ``known_hosts`` 的
    完整语法（无选项、无标记键），因为我们只关心「有没有、有的话是什么」。
    """
    if not path.exists():
        return None
    prefix = _known_hosts_prefix(host, port)
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("#"):
            continue
        if raw.startswith(prefix):
            return raw
    return None


def fingerprint_from_known_hosts_line(line: str) -> str | None:
    """从 known_hosts 一行反算指纹；行格式不对返回 ``None``。

    用于「已有条目但密钥不同」分支：把已知条目的指纹也打出来给用户看。
    """
    parts = line.split()
    if len(parts) < 3:
        return None
    key_type, key_b64 = parts[-2], parts[-1]
    try:
        key_bytes = base64.b64decode(key_b64, validate=True)
    except Exception:
        return None
    return compute_fingerprint(key_type, key_bytes)


def append_known_hosts(path: Path, entry: str) -> None:
    """追加一条；末尾无换行先补一个，避免把上一行弄坏（KTD22）。"""
    _ensure_known_hosts_dir(path)
    if path.exists() and path.stat().st_size > 0:
        with path.open("rb") as f:
            f.seek(-1, 2)
            last_byte = f.read(1)
        if last_byte != b"\n":
            with path.open("ab") as f:
                f.write(b"\n")
    with path.open("a", encoding="utf-8") as f:
        f.write(entry)


# ---- 主机密钥探测（TOFU，KTD22）----


async def fetch_server_host_key(
    host: str,
    port: int,
    *,
    asyncssh_factory: Callable[[], Any] | None = None,
) -> tuple[str, bytes, str]:
    """探测主机公钥：返回 ``(key_type, public_key_bytes, base64)``。

    这一步**只做密钥交换**——``asyncssh.get_server_host_key`` 不接受
    ``password`` / ``client_keys`` 等凭据参数，正是 R9 与 KTD22 要求
    「探测连接不发送任何凭据」的语义。``asyncssh_factory`` 仅用于测试注入。
    """
    validate_safe(host, allow_colon=True)
    asyncssh = asyncssh_factory() if asyncssh_factory is not None else _import_asyncssh()
    key = asyncssh.get_server_host_key(host, port)
    key_type = key.algorithm
    public_key_bytes = key.export_public()
    key_b64 = key.get_base64()
    return key_type, public_key_bytes, key_b64


async def _confirm_or_skip(
    host: str,
    port: int,
    known_hosts_path: Path,
    *,
    confirm_fn: Callable[[str], bool],
    asyncssh_factory: Callable[[], Any] | None = None,
) -> bool:
    """首见主机的指纹探测与确认；返回是否成功落条目。

    行为表：

    - ``known_hosts`` 无该主机 → 探测公钥、打印指纹、用户确认、写入。
    - ``known_hosts`` 有该主机且密钥相同 → 静默返回 True（不打扰用户）。
    - ``known_hosts`` 有该主机但密钥不同 → 抛 :class:`HostKeyMismatchError`，
      错误消息含两个指纹；``known_hosts`` **不会被修改**（KTD22）。
    """
    key_type, public_key_bytes, key_b64 = await fetch_server_host_key(
        host, port, asyncssh_factory=asyncssh_factory
    )
    new_fingerprint = compute_fingerprint(key_type, public_key_bytes)
    new_entry = make_known_hosts_entry(host, port, key_type, key_b64)

    existing_line = lookup_known_hosts(known_hosts_path, host, port)
    if existing_line is not None:
        existing_b64 = existing_line.split()[-1]
        new_b64_part = new_entry.rstrip("\n").split()[-1]
        if existing_b64 == new_b64_part:
            # 密钥一致：什么都不做
            return True
        # 密钥不一致：疑似中间人——打印两个指纹并中止
        existing_fp = fingerprint_from_known_hosts_line(existing_line)
        raise HostKeyMismatchError(
            f"主机 {host}:{port} 的密钥与 known_hosts 中已记录的密钥不一致，"
            f"疑似中间人攻击。新指纹：{new_fingerprint}；"
            f"已存指纹：{existing_fp}。"
            f"为安全起见不会自动覆盖，请人工核对后处理。"
        )

    # 没有条目：用户确认后写入
    prompt = f"接受主机 {host}:{port} 的指纹 {new_fingerprint} 并写入 {known_hosts_path}？"
    if not confirm_fn(prompt):
        raise HostKeyRejectedError(f"用户拒绝接受主机 {host}:{port} 的指纹")
    append_known_hosts(known_hosts_path, new_entry)
    return True


# ---- 公开入口：TOFU + 带校验重连 ----


async def tofu_then_connect(
    host: str,
    port: int,
    user: str,
    password: str,
    known_hosts_path: Path,
    *,
    confirm_fn: Callable[[str], bool],
    asyncssh_factory: Callable[[], Any] | None = None,
) -> Any:
    """TOFU 后做带 ``known_hosts`` 校验的密码鉴权连接。

    返回的 ``conn`` 是 ``asyncssh.SSHClientConnection`` 实例；调用方负责
    ``await conn.close()`` 收尾。``asyncssh_factory`` 仅用于测试注入，
    默认调 :func:`_import_asyncssh`。
    """
    validate_safe(host, allow_colon=True)
    validate_safe(user)
    if not password:
        raise SSHError("密码不能为空")

    asyncssh = asyncssh_factory() if asyncssh_factory is not None else _import_asyncssh()

    # 1) 探测主机公钥（只做密钥交换，不发送凭据——KTD22）
    await _confirm_or_skip(
        host, port, known_hosts_path, confirm_fn=confirm_fn, asyncssh_factory=asyncssh_factory
    )

    # 2) 带校验重连 + 密码鉴权；不传 term_type、不申请 pty
    conn = await asyncssh.connect(
        host,
        port=port,
        username=user,
        password=password,
        known_hosts=str(known_hosts_path),
    )
    return conn


# ---- 提权探测（R10）----


async def probe_privilege(conn: Any, password: str) -> None:
    """三档提权探测：root / 免密 sudo / 喂密码 sudo；都不通则抛 :class:`SudoUnavailableError`。

    任一档成功即返回；任一档有 ``term_type`` 都会被测试断言出来（不申请 pty，
    避免密码被回显进伪终端）。喂密码走 ``create_process`` + ``stdin``，
    **写完立刻** ``write_eof()``，否则 ``sudo`` 会等更多输入而挂住。
    """
    # 1) id -u；为 0 即 root，无需 sudo
    r = await conn.run("id -u", check=False, timeout=10)
    if r.exit_status != 0:
        raise SudoUnavailableError(f"远端执行 id -u 失败（退出码 {r.exit_status}）")
    if r.stdout.strip() == "0":
        return

    # 2) sudo -n true（免密 sudo）
    r = await conn.run("sudo -n true", check=False, timeout=10)
    if r.exit_status == 0:
        return

    # 3) sudo -S -p '' true（喂密码）；不申请 pty
    process = await conn.create_process("sudo -S -p '' true", timeout=10)
    try:
        # ``create_process`` 返回的 process.stdin 接受 ``write`` 与 ``write_eof``。
        # 必须先把密码写进去再立刻 eof，否则 sudo 会阻塞在 stdin 上。
        process.stdin.write(password + "\n")
        process.stdin.write_eof()
        await process.wait()
    finally:
        # process 会随连接关闭被回收；这里显式不做 close 以避免在不同 asyncssh
        # 版本下的兼容性问题（部分版本要求在 wait 后才允许 close）。
        pass

    if process.exit_status != 0:
        raise SudoUnavailableError(
            "远端提权失败：免密 sudo 不可用，且用 SSH 密码喂 sudo 也未成功。"
            "请检查 sudoers 中的 NOPASSWD 配置；"
            "SSH 密码可能不能用于 sudo（A3）。"
        )


# ---- SFTP 上传（R13）----


async def upload_part_then_mv(
    sftp: Any,
    remote_path: str,
    content: str | bytes,
    *,
    final_mode: int = 0o600,
    ensure_600_before_write: bool = False,
) -> None:
    """先写 ``<remote_path>.part``，再远端 ``mv`` 到正式名（R13）。

    - ``ensure_600_before_write=True``：用于配置文件类敏感数据。先以 ``'x'``
      独占创建空文件、chmod 600、再写内容——避免半截文件以默认权限被读到。
      默认 ``False``：普通上传（程序 zip 之类）。
    - ``final_mode=0``：不调 chmod；保持 SFTP 默认权限。
    """
    if isinstance(content, str):
        content = content.encode("utf-8")

    part_path = f"{remote_path}.part"

    if ensure_600_before_write:
        # 1) 独占创建空 .part 文件
        async with sftp.open(part_path, "x") as f:
            pass
        # 2) 立刻 chmod 600
        await sftp.chmod(part_path, 0o600)
        # 3) 写内容（'wb' 截断再写，文件已存在）
        async with sftp.open(part_path, "wb") as f:
            await f.write(content)
    else:
        async with sftp.open(part_path, "w") as f:
            await f.write(content)

    # 4) 远端改名
    await sftp.posix_rename(part_path, remote_path)

    # 5) 设权限（如需）
    if final_mode:
        await sftp.chmod(remote_path, final_mode)


# ---- 远端执行（R26）----


async def run_script(
    sftp: Any,
    conn: Any,
    script_content: str,
    *,
    remote_dir: str,
    script_basename: str = "deploy-step.sh",
    secrets: Sequence[str] = (),
) -> str:
    """上传脚本到 ``remote_dir``、``bash <脚本>`` 执行、返回打码后的合并输出。

    不直接拼 shell 字符串：脚本由本机写好经 SFTP 上传（R26），远端只用
    ``bash <path>`` 这一个被 ``shlex.quote`` 包过的路径。``secrets`` 是
    已知密钥（bot token、SSH 密码），输出在打印前经
    :func:`hawkeye.notify.redact` 替换。
    """
    # 脚本路径：用户提供的 basename 过白名单，避免注入到 remote_dir 里。
    validate_safe(script_basename)
    remote_script = f"{remote_dir.rstrip('/')}/{script_basename}"

    await upload_part_then_mv(sftp, remote_script, script_content, final_mode=0o700)

    # bash <quoted path>：变量已经在本机被 quote，路径里不会再有 shell 元字符
    cmd = f"bash {shlex.quote(remote_script)}"
    result = await conn.run(cmd, check=False)
    output = (result.stdout or "") + (result.stderr or "")
    return redact(output, secrets)


# 重导出 redact 供 :mod:`hawkeye.deploy` 一行调用，同时让测试能直接断言
# `hawkeye.ssh.redact is hawkeye.notify.redact`（KTD19）。
__all__ = [
    "SSHError",
    "HostKeyMismatchError",
    "HostKeyRejectedError",
    "SudoUnavailableError",
    "append_known_hosts",
    "compute_fingerprint",
    "fetch_server_host_key",
    "fingerprint_from_known_hosts_line",
    "lookup_known_hosts",
    "make_known_hosts_entry",
    "probe_privilege",
    "redact",
    "run_script",
    "tofu_then_connect",
    "upload_part_then_mv",
    "validate_safe",
]
