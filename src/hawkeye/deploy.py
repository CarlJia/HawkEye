"""一键部署编排（hawkeye deploy）。

把 U2 / U3 / U4 串成一条命令，顺序按「早失败」排——探权限只要几秒，打包
上传是分钟级，Chromium 下载是十几分钟级。顺序错了用户会在十分钟后才
知道自己没有 sudo。

纪律：

- **顶层不 import asyncssh**——由 :mod:`hawkeye.ssh` 内部惰性加载，本模块只
  通过 ``ssh`` 的公开接口与远端对话（即便如此也仅在 :class:`DefaultRemoteOps`
  实际被实例化时才触达；测试注入替身时整条 SSH 栈都不会被加载）。
- **远端执行一律走脚本文件**——本机生成 launcher 脚本、SFTP 上传后
  ``bash <脚本>`` 执行，不在 Python 里拼 shell 字符串（R26）。
- **远端暂存目录名来自** ``mktemp -d`` **的输出**，不是代码里字面拼出来的
  字面量；用 ``/var/tmp/hawkeye-deploy.XXXXXXXXXX`` 模板（R24）。
- **完成信号三选一**：``.rc`` 出现 / ``kill -0 <pid>`` 失败 / 总时长超上限
  （默认 30 分钟，可调）——只等 ``.rc`` 不够，OOM killer 会让本机永久挂住
  （R17 / AE16）。
- **密码永不落盘**——只走隐藏输入与 launcher 内一次性 ``read``；``ps`` 里
  也只以 here-string / 子 shell 形式短暂出现，绝不进 ``deploy.sh`` 的 argv
  （R8 / R15）。
- **本机配置解析失败、占位符 token 漏出都在打包之前退 1**（AE13 / R5），
  ``parse_config`` 挡不住占位符（它只要求非空字符串）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shlex
import time
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import tomli_w

from .config import ConfigError, load_raw, parse_config
from .configedit import _tighten_permissions
from .notify import redact
from .ssh import SSHError, SudoUnavailableError, validate_safe

logger = logging.getLogger(__name__)


# ---- 常量（与 deploy.sh 保持一致；改了一处要同步另一处）----


# config.example.toml 中 telegram.bot_token 的占位值；deploy.sh 同样用这个串。
PLACEHOLDER_TOKEN = "123456:ABC-your-bot-token"

# 远端配置路径——与 deploy.sh 的 CONFIG_FILE 默认值保持一致。
REMOTE_CONFIG_PATH = "/opt/hawkeye/config.toml"
REMOTE_SERVICE_NAME = "hawkeye.service"

DEFAULT_SSH_PORT = 22
DEFAULT_INSTALL_TIMEOUT_SECONDS = 30 * 60
DEFAULT_LOG_POLL_INTERVAL = 2.0
HEALTH_RESTART_OBSERVE_SECONDS = 5.0

# 远端三态
CONFIG_STATE_MISSING = "missing"
CONFIG_STATE_PLACEHOLDER = "placeholder"
CONFIG_STATE_REAL = "real"


# ---- 错误类型 ----


class DeployError(Exception):
    """部署编排中预期内的失败；CLI 映射为退出码 1。"""


# ---- 数据结构 ----


@dataclass
class InstallHandles:
    """后台安装的远端路径与 PID，deploy.py 拿这些去找尾日志/读 .rc/查存活。"""

    log_path: str
    pid_path: str
    rc_path: str
    pid: int


# ---- 远端操作高层协议 ----


class RemoteOps(Protocol):
    """远端操作高层接口。

    :class:`DefaultRemoteOps` 用 :mod:`hawkeye.ssh` 实现真实逻辑；测试用
    ``_FakeRemoteOps`` 替身记录调用并返回罐头数据。这样编排（deploy.py）
    与传输（ssh.py）解耦，编排可测、传输可分别覆盖。
    """

    async def __aenter__(self) -> RemoteOps: ...
    async def __aexit__(self, *exc: object) -> None: ...
    async def probe_privilege(self) -> None: ...
    async def probe_remote_config(self, config_path: str, placeholder: str) -> str: ...
    async def mktemp_remote_dir(self) -> str: ...
    async def upload_zip(self, local_zip: Path, remote_path: str) -> None: ...
    async def upload_config(self, local_config: Path, remote_path: str) -> None: ...
    async def run_background_install(
        self,
        *,
        stage_dir: str,
        pkg_name: str,
        config_remote_path: str | None,
        overwrite_config: bool,
    ) -> InstallHandles: ...
    async def tail_log_until_done(
        self,
        handles: InstallHandles,
        *,
        max_seconds: float,
        secrets: Sequence[str],
        on_line: Callable[[str], None],
    ) -> int: ...
    async def layered_health_check(self) -> tuple[bool, str]: ...
    async def cleanup_stage(self, stage_dir: str, *, keep_log: bool) -> None: ...


# ---- 连接参数读写（R8）----


def _read_connection_file(path: Path) -> dict[str, Any]:
    """读 ``.hawkeye-deploy.toml``；不存在返回 ``{}``，解析失败抛 :class:`DeployError`。"""
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise DeployError(f"{path} 不是合法 TOML：{e}") from e


def _write_connection_file(path: Path, *, host: str, port: int, user: str) -> None:
    """把 host / port / user 写到 ``.hawkeye-deploy.toml``；权限收紧。

    **不含密码**——密码只经 ``getpass`` 隐藏输入、当次使用，绝不落盘（R8）。
    """
    payload: dict[str, Any] = {"host": host, "port": int(port), "user": user}
    path.write_text(tomli_w.dumps(payload), encoding="utf-8")
    _tighten_permissions(path)


def _coerce_port(value: object) -> int | None:
    """把配置文件里的 port 字段转成 ``int``；无效返回 ``None``（外层走默认 22）。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _resolve_connection_params(
    *,
    stored: dict[str, Any],
    host_arg: str | None,
    user_arg: str | None,
    port_arg: int | None,
    ask: Callable[[str], str],
) -> tuple[str, int, str, bool]:
    """按优先级解析连接参数：命令行 > 配置文件 > 交互。

    返回 ``(host, port, user, asked_new)``；``asked_new=True`` 表示至少一个
    值是交互问出来的、且不在已有配置里——外层据此决定是否要写回配置文件。
    """
    host = (host_arg or stored.get("host") or "").strip()
    user = (user_arg or stored.get("user") or "").strip()
    port = port_arg if port_arg is not None else _coerce_port(stored.get("port"))
    if port is None:
        port = DEFAULT_SSH_PORT

    asked_new = False
    if not host:
        host = ask("请输入 VPS 主机地址：").strip()
        asked_new = True
    if not user:
        user = ask("请输入 SSH 用户名：").strip()
        asked_new = True

    return host, int(port), user, asked_new


# ---- 本机配置占位符硬闸门（AE13 / R5）----


def _validate_local_config(path: Path) -> str:
    """校验本机 ``config.toml`` 合法且 ``bot_token`` 不是占位符。

    返回 ``bot_token`` 字符串供打码用。``parse_config`` 只要求非空字符串，
    占位符 token 能合法通过——必须单独再过一遍，避免漏到服务器上让
    ``is_configured()`` 判成「未配置」、服务静默不启动。
    """
    raw = load_raw(path)
    parse_config(raw)  # 抛 ConfigError；不再兜成 {}
    tg = raw.get("telegram") or {}
    token = tg.get("bot_token") if isinstance(tg, dict) else None
    if not isinstance(token, str) or PLACEHOLDER_TOKEN in token:
        raise DeployError(
            f"{path} 里的 bot_token 仍是占位符 {PLACEHOLDER_TOKEN!r}。"
            "请先运行 `hawkeye init` 填入真实凭据。"
        )
    return token


# ---- 远端三态判定（KTD5 / KTD12）----


def _should_adopt_local_config(*, state: str, overwrite: bool) -> bool:
    """根据远端三态 + ``--overwrite-config`` 决定是否采用本机配置。"""
    if state == CONFIG_STATE_MISSING:
        return True
    if state == CONFIG_STATE_PLACEHOLDER:
        return True
    if state == CONFIG_STATE_REAL:
        return overwrite
    raise DeployError(f"未知的远端 config 状态：{state!r}")


# ---- 摘要 ----


def _print_summary(
    emit: Callable[[str], None],
    *,
    success: bool,
    log_path: str | None,
    config_path: Path | None,
) -> None:
    emit("")
    if success:
        emit("[HawkEye] 部署完成。")
    else:
        emit("[HawkEye] 部署失败；日志路径已打印，可继续排查。")
    if log_path:
        emit(f"  安装日志 : {log_path}")
        emit(f"  查看日志 : ssh <user>@<host> 'tail -F {log_path}'")
    if config_path is not None:
        emit(f"  本机配置 : {config_path}")
    emit("")
    emit("常用运维命令：")
    emit("  sudo systemctl status hawkeye.service")
    emit("  sudo journalctl -u hawkeye.service -f")
    if not success and log_path:
        emit("失败回退命令（如自动回滚未生效）：")
        emit("  sudo mv /opt/hawkeye/src.old /opt/hawkeye/src    # 如果 src.old 还在")
        emit("  sudo systemctl restart hawkeye.service")


# ---- launcher 脚本模板 ----


# 直接以 f-string 内插生成的 launcher 脚本。
# 安全保证：stage_dir 来自 mktemp（十六进制），pkg_name 来自 pyproject 版本号 +
# 时间戳（外层 validate_safe 覆盖 host/user/version），config_part 同理。
# inner bash 用单引号包住，$D / $PKG / $SUDO_PW / $rc / $$ 由内层 bash 解析。
# 这里 ``\\\\`` 在 f-string 后变成 ``\\``，进入远端 shell 后变成 ``\`` 字面。
_LAUNCHER_TEMPLATE = """#!/bin/bash
set -e
read -r SUDO_PW
D={stage_dir}
PKG={pkg_name}
export D PKG SUDO_PW
install -m 600 /dev/null "$D/install.log"
setsid nohup bash -c '
echo $$ > "$D/install.pid"
if command -v unzip >/dev/null 2>&1; then
    unzip -q -o "$D/${{PKG}}.zip" -d "$D"
else
    python3 -m zipfile -e "$D/${{PKG}}.zip" "$D"
fi
sudo -S -p "" bash "$D/$PKG/deploy.sh" install {config_part}{overwrite_flag} \
    >> "$D/install.log" 2>&1 <<< "$SUDO_PW"
rc=$?
echo $rc > "$D/.rc.part"
mv "$D/.rc.part" "$D/.rc"
' </dev/null >/dev/null 2>&1 &
disown
for i in $(seq 1 100); do
    [ -f "$D/install.pid" ] && break
    sleep 0.3
done
cat "$D/install.pid"
"""


def _build_launcher_script(
    *,
    stage_dir: str,
    pkg_name: str,
    config_remote_path: str | None,
    overwrite_config: bool,
) -> str:
    """生成 launcher.sh 文本。

    值已在外层校验/标准化（``validate_safe`` / mktemp / ``stage_dir``），所以
    这里直接内插；``shlex.quote`` 只对 ``config_remote_path`` 做防御。
    """
    if config_remote_path:
        config_part = f"--config {shlex.quote(config_remote_path)} "
    else:
        config_part = ""
    overwrite_flag = "--overwrite-config " if overwrite_config else ""
    return _LAUNCHER_TEMPLATE.format(
        stage_dir=stage_dir,
        pkg_name=pkg_name,
        config_part=config_part,
        overwrite_flag=overwrite_flag,
    )


# ---- 编排主入口 ----


async def run_deploy(
    args: argparse.Namespace,
    *,
    config_path: Path,
    connection_file: Path,
    known_hosts_path: Path,
    dist_dir: Path,
    ask: Callable[[str], str],
    ask_secret: Callable[[str], str],
    emit: Callable[[str], None],
    remote_ops_factory: Callable[..., RemoteOps],
    packaging_root: Path,
    install_timeout_seconds: float = DEFAULT_INSTALL_TIMEOUT_SECONDS,
) -> int:
    """编排一次部署；返回退出码（0 成功，1 失败，130 Ctrl-C）。"""
    emit("[HawkEye] 一键部署开始……")

    # 1. 读 .hawkeye-deploy.toml
    try:
        stored = _read_connection_file(connection_file)
    except DeployError as e:
        emit(f"读取 {connection_file} 失败：{e}")
        return 1

    # 2. 收集 host / port / user
    try:
        host, port, user, asked_new = _resolve_connection_params(
            stored=stored,
            host_arg=getattr(args, "host", None),
            user_arg=getattr(args, "user", None),
            port_arg=getattr(args, "port", None),
            ask=ask,
        )
    except Exception as e:  # pragma: no cover —— input() 不会到这里
        emit(f"收集连接参数失败：{e}")
        return 1

    # 3. 白名单校验（AE15）——畸形 host/user 在连接之前就抛
    try:
        validate_safe(host, allow_colon=True)
        validate_safe(user)
    except ValueError as e:
        emit(f"连接参数不合法：{e}")
        return 1

    # 4. 写回 .hawkeye-deploy.toml（仅当本次新问到值）
    if asked_new and not connection_file.exists():
        try:
            _write_connection_file(connection_file, host=host, port=port, user=user)
            emit(f"已写入连接参数：{connection_file}")
        except OSError as e:
            emit(f"写入 {connection_file} 失败（仍继续部署）：{e}")

    # 5. 隐藏输入密码（仅本轮用，绝不落盘——R8）
    try:
        password = ask_secret("请输入 SSH 密码（输入隐藏）：")
    except (EOFError, KeyboardInterrupt):
        return 130
    if not password:
        emit("密码不能为空。")
        return 1

    secrets: list[str] = [password]
    confirm_fn = _default_confirm(ask, emit)

    # 6. 建立连接（TOFU + 带校验重连 + SFTP）
    remote_ops = remote_ops_factory(
        host=host,
        port=port,
        user=user,
        password=password,
        known_hosts_path=known_hosts_path,
        confirm_fn=confirm_fn,
    )

    try:
        async with remote_ops as ops:
            # 7. 探 id -u / sudo（F4：这一步就能挡住 NOPASSWD 都没配的用户）
            try:
                await ops.probe_privilege()
            except (SSHError, SudoUnavailableError) as e:
                emit(f"远端提权探测失败：{e}")
                return 1

            # 8. 前置探测远端 config.toml 三态
            try:
                state = await ops.probe_remote_config(REMOTE_CONFIG_PATH, PLACEHOLDER_TOKEN)
            except SSHError as e:
                emit(f"远端探测失败：{e}")
                return 1

            adopt_local_config = _should_adopt_local_config(
                state=state,
                overwrite=bool(getattr(args, "overwrite_config", False)),
            )

            # 9. 仅当本机配置本次会被采用：parse_config + 占位符硬闸门（AE13）
            local_config_for_upload: Path | None = None
            if adopt_local_config:
                if not config_path.exists():
                    emit(
                        f"本机配置 {config_path} 不存在。"
                        "请先运行 `hawkeye init`，或确认服务器是否已配好。"
                    )
                    return 1
                try:
                    bot_token = _validate_local_config(config_path)
                    secrets.append(bot_token)
                except ConfigError as e:
                    emit(f"本机配置 {config_path} 不合法：{e}")
                    return 1
                except DeployError as e:
                    emit(str(e))
                    return 1
                local_config_for_upload = config_path
            else:
                emit(
                    "服务器上已有真实 config.toml，本次保留不覆盖。"
                    "你本机的 config.toml 未生效；如需覆盖请加 --overwrite-config。"
                )

            # 10. 打包（白名单 + 泄漏兜底；中止即退 1——R12）
            emit("[HawkEye] 打包中……")
            from .packaging import PackageError, build_package

            try:
                archive = build_package(root=packaging_root, dist_dir=dist_dir)
            except PackageError as e:
                emit(f"打包失败：{e}")
                return 1
            pkg_name = archive.stem
            try:
                validate_safe(pkg_name)
            except ValueError as e:  # 兜底：版本号即便异常也不能落进远端命令
                emit(f"包名不合法：{e}")
                return 1

            # 11. 远端暂存目录（mktemp -d，路径来自命令输出——R24）
            try:
                stage_dir = await ops.mktemp_remote_dir()
            except SSHError as e:
                emit(f"创建远端暂存目录失败：{e}")
                return 1
            emit(f"远端暂存目录：{stage_dir}")

            # 12. 上传 zip（先 .part 再 mv——R13）
            remote_zip = f"{stage_dir}/{pkg_name}.zip"
            try:
                await ops.upload_zip(archive, remote_zip)
            except SSHError as e:
                emit(f"上传部署包失败：{e}")
                return 1

            # 13. 仅当本机配置本次会被采用：上传 config.toml
            remote_config: str | None = None
            if local_config_for_upload is not None:
                remote_config = f"{stage_dir}/config.toml"
                try:
                    await ops.upload_config(local_config_for_upload, remote_config)
                except SSHError as e:
                    emit(f"上传配置失败：{e}")
                    return 1

            # 14. 远端后台 install（launcher 上传 + 跑 + 读 pid）
            try:
                handles = await ops.run_background_install(
                    stage_dir=stage_dir,
                    pkg_name=pkg_name,
                    config_remote_path=remote_config,
                    overwrite_config=bool(getattr(args, "overwrite_config", False)),
                )
            except SSHError as e:
                emit(f"启动远端安装失败：{e}")
                return 1
            emit(f"远端安装日志：{handles.log_path}")
            emit(f"远端安装 PID：{handles.pid}")

            # 15. 跟随日志 + 等待完成（三条退出边——R17）
            emit("[HawkEye] 等待远端安装完成（可能十几分钟）……")

            def _on_line(line: str) -> None:
                emit(redact(line, secrets))

            try:
                rc = await ops.tail_log_until_done(
                    handles,
                    max_seconds=install_timeout_seconds,
                    secrets=tuple(secrets),
                    on_line=_on_line,
                )
            except DeployError as e:
                emit(str(e))
                _print_summary(
                    emit, success=False, log_path=handles.log_path, config_path=config_path
                )
                await _safe_cleanup(ops, stage_dir)
                return 1

            if rc != 0:
                emit(f"远端 install 退出码 {rc}；请查看日志：{handles.log_path}")
                _print_summary(
                    emit, success=False, log_path=handles.log_path, config_path=config_path
                )
                await _safe_cleanup(ops, stage_dir)
                return 1

            # 16. 分层健康判据（R25）——.rc=0 也不等于服务在跑
            emit("[HawkEye] 检查服务健康……")
            try:
                ok, msg = await ops.layered_health_check()
            except SSHError as e:
                ok, msg = False, f"无法执行健康检查：{e}"
            if not ok:
                emit(f"分层健康检查失败：{msg}")
                _print_summary(
                    emit, success=False, log_path=handles.log_path, config_path=config_path
                )
                await _safe_cleanup(ops, stage_dir)
                return 1
            emit(f"健康检查通过：{msg}")

            # 17. 清理（保留 install.log——R18）
            await _safe_cleanup(ops, stage_dir)

            _print_summary(emit, success=True, log_path=handles.log_path, config_path=config_path)
            return 0

    except SSHError as e:
        emit(f"SSH 错误：{e}")
        return 1
    except Exception as e:  # noqa: BLE001 —— 兜底，防止 asyncssh/底层异常把 traceback 漏给用户
        emit(f"部署失败：{e}")
        return 1
    except KeyboardInterrupt:
        return 130


async def _safe_cleanup(ops: RemoteOps, stage_dir: str) -> None:
    """清理远端暂存目录，失败仅告警、不抛出——失败路径上的二次清理不应再抛。"""
    try:
        await ops.cleanup_stage(stage_dir, keep_log=True)
    except Exception as e:  # noqa: BLE001 —— 清不掉只能告警，不能再抛中断流程
        logger.warning("清理远端暂存目录 %s 失败：%s", stage_dir, e)


def _default_confirm(
    ask: Callable[[str], str],
    emit: Callable[[str], None],
) -> Callable[[str], bool]:
    """默认 TOFU 确认函数：先打印提示再问 y/N。"""

    def _confirm(prompt: str) -> bool:
        emit(prompt)
        answer = ask("  接受？(y/N，回车拒绝)：").strip().lower()
        return answer in {"y", "yes", "是", "确认", "确定", "保存"}

    return _confirm


# ---- 真实远端操作（用 ssh.py + asyncssh）----


class DefaultRemoteOps:
    """生产用 RemoteOps：用 :mod:`hawkeye.ssh` + asyncssh 实现。

    本类只在被实例化时触达 asyncssh；未装可选依赖时连接会抛
    :class:`SSHError`（R22）。
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        known_hosts_path: Path,
        confirm_fn: Callable[[str], bool],
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._known_hosts_path = known_hosts_path
        self._confirm_fn = confirm_fn
        self._conn: Any = None
        self._sftp_cm: Any = None
        self._sftp: Any = None

    async def __aenter__(self) -> DefaultRemoteOps:
        from . import ssh as ssh_mod  # noqa: PLC0415 —— 触达 asyncssh 的真实点

        self._conn = await ssh_mod.tofu_then_connect(
            self._host,
            self._port,
            self._user,
            self._password,
            self._known_hosts_path,
            confirm_fn=self._confirm_fn,
        )
        # SFTP 用上下文管理器包住；tail_log_until_done 需要长期持有。
        self._sftp_cm = self._conn.start_sftp()
        self._sftp = await self._sftp_cm.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._sftp_cm is not None:
            try:
                await self._sftp_cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                await self._conn.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    @property
    def _connection(self) -> Any:
        if self._conn is None:
            raise SSHError("连接尚未建立")
        return self._conn

    @property
    def _sftp_client(self) -> Any:
        if self._sftp is None:
            raise SSHError("SFTP 尚未建立")
        return self._sftp

    async def probe_privilege(self) -> None:
        from . import ssh as ssh_mod  # noqa: PLC0415

        await ssh_mod.probe_privilege(self._connection, self._password)

    async def probe_remote_config(self, config_path: str, placeholder: str) -> str:
        """探测远端 config.toml 三态。"""
        r = await self._connection.run(f"test -f {shlex.quote(config_path)}", check=False)
        if r.exit_status != 0:
            return CONFIG_STATE_MISSING
        r = await self._connection.run(
            f"grep -qF {shlex.quote(placeholder)} {shlex.quote(config_path)}",
            check=False,
        )
        if r.exit_status == 0:
            return CONFIG_STATE_PLACEHOLDER
        return CONFIG_STATE_REAL

    async def mktemp_remote_dir(self) -> str:
        """``mktemp -d /var/tmp/hawkeye-deploy.XXXXXXXXXX``；路径来自命令输出（R24）。

        用 ``/var/tmp`` 而非 ``/tmp`` 是因为它不受 tmpfiles 的短周期清理影响，
        十几分钟的安装期内不会被扫掉。
        """
        r = await self._connection.run("mktemp -d /var/tmp/hawkeye-deploy.XXXXXXXXXX", check=False)
        if r.exit_status != 0:
            raise SSHError(f"mktemp -d 失败：{(r.stderr or '').strip()}")
        path = (r.stdout or "").strip()
        if not path.startswith("/var/tmp/hawkeye-deploy."):
            raise SSHError(f"mktemp 返回的路径不符合预期：{path!r}")
        return path

    async def upload_zip(self, local_zip: Path, remote_path: str) -> None:
        from . import ssh as ssh_mod  # noqa: PLC0415

        await ssh_mod.upload_part_then_mv(
            self._sftp_client,
            remote_path,
            local_zip.read_bytes(),
            final_mode=0o600,
            ensure_600_before_write=False,
        )

    async def upload_config(self, local_config: Path, remote_path: str) -> None:
        from . import ssh as ssh_mod  # noqa: PLC0415

        await ssh_mod.upload_part_then_mv(
            self._sftp_client,
            remote_path,
            local_config.read_text(encoding="utf-8"),
            final_mode=0o600,
            ensure_600_before_write=True,
        )

    async def run_background_install(
        self,
        *,
        stage_dir: str,
        pkg_name: str,
        config_remote_path: str | None,
        overwrite_config: bool,
    ) -> InstallHandles:
        """上传 launcher.sh、跑（喂密码到 stdin）、读 install.pid 拿 PID。"""
        from . import ssh as ssh_mod  # noqa: PLC0415

        launcher = _build_launcher_script(
            stage_dir=stage_dir,
            pkg_name=pkg_name,
            config_remote_path=config_remote_path,
            overwrite_config=overwrite_config,
        )
        launcher_remote = f"{stage_dir}/deploy-launcher.sh"
        await ssh_mod.upload_part_then_mv(
            self._sftp_client,
            launcher_remote,
            launcher,
            final_mode=0o700,
            ensure_600_before_write=False,
        )

        # 跑 launcher；密码经 stdin 一次性喂入（不分配 pty——R10）。
        process = await self._connection.create_process(
            f"bash {shlex.quote(launcher_remote)}", stderr=None
        )
        try:
            process.stdin.write(self._password + "\n")
            process.stdin.write_eof()
            await process.wait()
        finally:
            # 不显式 close，避免不同 asyncssh 版本对 wait 后 close 的兼容性差异。
            pass

        if process.exit_status != 0:
            stderr = ""
            try:
                stderr = (await process.stderr.read()) if process.stderr is not None else ""
            except Exception:  # noqa: BLE001
                pass
            safe_stderr = redact(stderr.strip() or "(无)", [self._password])
            raise SSHError(f"launcher 退出码 {process.exit_status}；stderr: {safe_stderr}")

        # 读 install.pid
        try:
            async with self._sftp_client.open(f"{stage_dir}/install.pid", "r") as f:
                pid_text = (await f.read()).decode("utf-8").strip()
        except Exception as e:  # noqa: BLE001
            raise SSHError(f"读取 install.pid 失败：{e}") from e

        try:
            pid = int(pid_text)
        except ValueError as e:
            raise SSHError(f"install.pid 不是数字：{pid_text!r}") from e

        return InstallHandles(
            log_path=f"{stage_dir}/install.log",
            pid_path=f"{stage_dir}/install.pid",
            rc_path=f"{stage_dir}/.rc",
            pid=pid,
        )

    async def tail_log_until_done(
        self,
        handles: InstallHandles,
        *,
        max_seconds: float,
        secrets: Sequence[str],
        on_line: Callable[[str], None],
    ) -> int:
        """三条退出边：.rc 出现 / kill -0 失败 / 超时（R17 / AE16）。"""
        start = time.monotonic()
        last_pos = 0
        first_iter = True
        while True:
            # 1) 读日志新增内容
            try:
                async with self._sftp_client.open(handles.log_path, "r") as f:
                    await f.seek(last_pos)
                    chunk = await f.read()
                if chunk:
                    last_pos += len(chunk)
                    text = chunk.decode("utf-8", errors="replace")
                    for line in text.splitlines():
                        on_line(line)
            except FileNotFoundError:
                # 日志还没建出来，第一轮允许
                if not first_iter:
                    pass
            except Exception as e:  # noqa: BLE001
                logger.debug("tail 读取失败：%s", e)
            first_iter = False

            # 2) .rc 出现 → 读退出码
            try:
                async with self._sftp_client.open(handles.rc_path, "r") as f:
                    rc_text = (await f.read()).decode("utf-8", errors="replace").strip()
                if rc_text:
                    try:
                        return int(rc_text)
                    except ValueError:
                        # 半截写入解释为异常退出
                        return -1
            except FileNotFoundError:
                pass
            except Exception as e:  # noqa: BLE001
                logger.debug("读 .rc 失败：%s", e)

            # 3) pid 已死 → 立刻以非零退出（防止后台进程被 OOM 时永久挂住）
            r = await self._connection.run(f"kill -0 {handles.pid}", check=False)
            if r.exit_status != 0:
                return -1

            # 4) 超时
            elapsed = time.monotonic() - start
            if elapsed >= max_seconds:
                raise DeployError(f"后台安装超时（{max_seconds:.0f} 秒）；日志：{handles.log_path}")

            await asyncio.sleep(DEFAULT_LOG_POLL_INTERVAL)

    async def layered_health_check(self) -> tuple[bool, str]:
        """``deploy.sh`` 退出码 0 之后还要查 systemd 真实状态（R25）。"""
        # 0) 远端是否有 systemctl；没有（容器/非 systemd）直接放过（R25 不强行套 systemd）
        r = await self._connection.run("command -v systemctl", check=False)
        if r.exit_status != 0:
            return True, "远端无 systemctl，跳过分层健康检查"

        # 1) is-enabled 必须 enabled
        r = await self._connection.run(f"systemctl is-enabled {REMOTE_SERVICE_NAME}", check=False)
        enabled = (r.stdout or "").strip()
        if enabled != "enabled":
            return False, f"服务未启用（is-enabled={enabled}）"

        # 2) 远端配置就绪性
        r = await self._connection.run(
            f"grep -qF {shlex.quote(PLACEHOLDER_TOKEN)} {shlex.quote(REMOTE_CONFIG_PATH)}",
            check=False,
        )
        # grep 退出码 0 = 找到占位符（未配置）；非 0 = 没找到（已配置或文件不在）
        if r.exit_status == 0:
            return True, "服务已启用，配置文件仍是模板（is-active 不要求）"

        # 3) 已配置：is-active 必须 active
        r = await self._connection.run(f"systemctl is-active {REMOTE_SERVICE_NAME}", check=False)
        status = (r.stdout or "").strip()
        if status != "active":
            return False, f"服务未运行（is-active={status}）"

        # 4) NRestarts 观察窗内不增长
        r1 = await self._connection.run(
            f"systemctl show -p NRestarts --value {REMOTE_SERVICE_NAME}", check=False
        )
        n1 = (r1.stdout or "").strip()
        await asyncio.sleep(HEALTH_RESTART_OBSERVE_SECONDS)
        r2 = await self._connection.run(
            f"systemctl show -p NRestarts --value {REMOTE_SERVICE_NAME}", check=False
        )
        n2 = (r2.stdout or "").strip()
        if n1 != n2:
            return False, f"NRestarts 在观察窗内增长（{n1} → {n2}）"

        # 5) ExecMainStatus=2 单独给诊断（RestartPreventExitStatus=2 让它停在 failed）
        r3 = await self._connection.run(
            f"systemctl show -p ExecMainStatus --value {REMOTE_SERVICE_NAME}", check=False
        )
        exec_status = (r3.stdout or "").strip()
        if exec_status == "2":
            return False, (
                "服务主进程以 2 退出（配置/Telegram 凭据致命错误）；"
                "systemd 单元里 RestartPreventExitStatus=2 已让它停在 failed。"
            )

        return True, "分层健康检查通过"

    async def cleanup_stage(self, stage_dir: str, *, keep_log: bool) -> None:
        """删远端暂存目录里的配置、解压内容；保留 install.log（R18）。"""
        r = await self._connection.run(f"ls -1 {shlex.quote(stage_dir)}", check=False)
        if r.exit_status != 0:
            return
        for line in (r.stdout or "").splitlines():
            name = line.strip()
            if not name:
                continue
            if keep_log and name == "install.log":
                continue
            await self._connection.run(f"rm -rf {shlex.quote(f'{stage_dir}/{name}')}", check=False)


__all__ = [
    "CONFIG_STATE_MISSING",
    "CONFIG_STATE_PLACEHOLDER",
    "CONFIG_STATE_REAL",
    "DEFAULT_INSTALL_TIMEOUT_SECONDS",
    "DEFAULT_SSH_PORT",
    "DeployError",
    "DefaultRemoteOps",
    "InstallHandles",
    "PLACEHOLDER_TOKEN",
    "REMOTE_CONFIG_PATH",
    "REMOTE_SERVICE_NAME",
    "RemoteOps",
    "run_deploy",
]
