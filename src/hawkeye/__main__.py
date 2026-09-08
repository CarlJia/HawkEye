"""命令行入口与守护装配。

本模块有两个责任：

- **守护进程路径**：没传子命令时按既有行为拉起 ``scheduler`` + ``receiver``，
  跑在 macOS / Linux 上（KTD6：Windows 上的事件循环不支持
  ``add_signal_handler``）。
- **子命令分派**：``hawkeye init`` 与 ``hawkeye deploy`` 通过
  :func:`argparse.ArgumentParser.add_subparsers` 注册，全局选项 ``-c`` /
  ``-v`` / ``--log-level`` 通过共享父解析器让子命令也能吃到（KTD16）。

惰性导入是硬约束，不是优化（KTD10）：``fetch`` 顶层 ``import playwright``，
``notify`` / ``receive`` / ``scheduler`` / ``control`` 又都拉 ``fetch``——
所以顶层只留 stdlib 与 ``.config`` / ``.configedit``；守护进程分支内部再
``import httpx`` / ``.control`` / ``.fetch`` / ``.notify`` / ``.receive`` /
``.scheduler``，这样 ``hawkeye init`` 在没装 Playwright 的机器上也能跑。
子命令的失败映射为退出码 1，守护进程的 ``ConfigError`` / ``TelegramFatalError``
继续映射为 2（保留 systemd 的 ``RestartPreventExitStatus=2`` 契约，R21）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import warnings
from pathlib import Path
from typing import Any

from .config import ConfigError, load_config
from .configedit import EditError

logger = logging.getLogger("hawkeye")

_LOG_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}


def _build_common() -> argparse.ArgumentParser:
    """子命令共享的父解析器：选项默认都是 ``SUPPRESS``，避免覆盖顶层已解析的值。"""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-c",
        "--config",
        default=argparse.SUPPRESS,
        help="配置文件路径（顶层默认 config.toml）",
    )
    common.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="输出调试日志（等价于 --log-level DEBUG）",
    )
    common.add_argument(
        "--log-level",
        type=str.upper,
        choices=tuple(_LOG_LEVELS),
        default=argparse.SUPPRESS,
        help="日志等级（默认 INFO；与 -v 同时给出时以本项为准）",
    )
    return common


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数；裸调用 ``args.command is None``。"""
    parser = argparse.ArgumentParser(
        prog="hawkeye",
        description="网页元素变更监控与 Telegram 通知守护进程",
    )
    # 顶层用真实默认值（KTD16）。子命令的 SUPPRESS 父解析器不再覆盖这些。
    parser.add_argument(
        "-c", "--config", default="config.toml", help="配置文件路径（默认 config.toml）"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="输出调试日志（等价于 --log-level DEBUG）"
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=tuple(_LOG_LEVELS),
        default=None,
        help="日志等级（默认 INFO；与 -v 同时给出时以本项为准）",
    )

    subparsers = parser.add_subparsers(dest="command")
    common = _build_common()
    init_parser = subparsers.add_parser(
        "init",
        parents=[common],
        help="交互式生成本机 config.toml（最小可启动配置）",
    )
    init_parser.add_argument(
        "--no-verify", action="store_true", help="跳过 Telegram 凭据自检（默认询问）"
    )
    # deploy 子命令：一键部署（U5）。选项全可选——host/user/port 缺省从
    # .hawkeye-deploy.toml 读或交互问；密码只能交互输入，不暴露给 CLI（R8）。
    deploy_parser = subparsers.add_parser(
        "deploy",
        parents=[common],
        help="一键把程序与配置部署到 VPS（包打包、上传、远端 install、健康判据）",
    )
    deploy_parser.add_argument(
        "--host", help="VPS 主机地址（默认从 .hawkeye-deploy.toml 读或交互问）"
    )
    deploy_parser.add_argument("--port", type=int, help="SSH 端口（默认 22）")
    deploy_parser.add_argument(
        "--user", help="SSH 用户名（默认从 .hawkeye-deploy.toml 读或交互问）"
    )
    deploy_parser.add_argument(
        "--overwrite-config",
        action="store_true",
        help="即便服务器上已有真实 config.toml，也用本机的顶掉（旧配置会先备份）",
    )
    # package 子命令：跨平台打 zip 部署包（U2 / KTD11）。
    # 守护进程分支不依赖 packaging；packaging 也只 import 标准库（KTD10）。
    package_parser = subparsers.add_parser(
        "package",
        parents=[common],
        help="把项目打成 dist/hawkeye-<版本>-<时间戳>.zip 部署包（无需 sudo）",
    )
    package_parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="项目根路径（默认当前目录；必须含 pyproject.toml / src/ / config.example.toml）",
    )

    return parser.parse_args(argv)


def _resolve_level(args: argparse.Namespace) -> int:
    """显式 --log-level 优先，其次 -v 视为 DEBUG，默认 INFO。"""
    level = getattr(args, "log_level", None)
    if level is not None:
        return _LOG_LEVELS[level]
    verbose = getattr(args, "verbose", False)
    return logging.DEBUG if verbose else logging.INFO


# ---- 守护进程路径：第三方依赖在这里导入，避免 init / deploy 被强制加载 ----


async def _run_daemon(config_path: str) -> None:
    # 全部第三方依赖都在函数内导入（KTD10）。
    import httpx  # noqa: PLC0415

    from .control import MENU_COMMANDS, Controller  # noqa: PLC0415
    from .fetch import BrowserManager  # noqa: PLC0415
    from .notify import Notifier, install_token_redaction  # noqa: PLC0415
    from .receive import Receiver  # noqa: PLC0415
    from .scheduler import Scheduler  # noqa: PLC0415

    config = load_config(config_path)
    install_token_redaction(config.telegram.bot_token)
    logger.info(
        "已加载配置：%d 个商家 / %d 个页面 / %d 个监控元素 / %d 个列表监控",
        len(config.merchants),
        len(config.pages),
        config.element_count,
        len(config.watches),
    )
    if not config.pages and not config.watches:
        # KTD13：零监控不是错误——用户可以先起进程，再在 Telegram 里现场加。
        logger.warning("当前未配置任何监控，可在 Telegram 中用 /add 添加")

    # 自检放在启动浏览器之前：发不出通知的进程没有必要先拉起 Chromium。
    async with httpx.AsyncClient() as client:
        notifier = Notifier(config.telegram, client)
        await notifier.verify()
        # 顺手把快捷菜单建好/对齐；同步失败只告警，不影响监控本体。
        await notifier.sync_commands(MENU_COMMANDS)

        browser = BrowserManager(config)
        await browser.start()
        try:
            # 两条循环共享同一个停止事件：信号处理只需置一次，二者一起收敛。
            stop = asyncio.Event()
            scheduler = Scheduler(config, browser, notifier, stop=stop)
            controller = Controller(config_path, scheduler, notifier)
            receiver = Receiver(client, config.telegram.bot_token, config.telegram.chat_id)

            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, scheduler.request_stop)

            loops = (
                asyncio.create_task(scheduler.run(), name="scheduler"),
                asyncio.create_task(receiver.run(controller.handle, stop), name="receiver"),
            )
            try:
                await asyncio.gather(*loops)
            finally:
                # 任一循环抛异常都要置停止事件并回收另一条，否则进程会挂在这里不退。
                stop.set()
                await asyncio.gather(*loops, return_exceptions=True)
        finally:
            await browser.close()


def _run_daemon_entry(args: argparse.Namespace) -> int:
    """守护进程入口；失败映射为退出码 2（R21 / systemd RestartPreventExitStatus=2）。

    ``TelegramFatalError`` 在函数内 import（守护进程分支的依赖只在用户跑
    守护进程时才加载，KTD10）。
    """
    from .notify import TelegramFatalError  # noqa: PLC0415 —— 与 _run_daemon 一致延迟加载

    try:
        asyncio.run(_run_daemon(args.config))
    except ConfigError as e:
        logger.error("配置错误：%s", e)
        return 2
    except TelegramFatalError as e:
        logger.error(
            "%s\n请确认：1) chat_id 正确；2) 该用户已私聊 bot 并发送过 /start"
            "（bot 无法主动向陌生用户发起会话）；3) 群聊需先把 bot 拉进群。",
            e,
        )
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


# ---- 子命令分派：失败一律映射为 1（KTD17）----


def _run_subcommand(args: argparse.Namespace) -> int:
    if args.command == "init":
        return _dispatch_init(args)
    if args.command == "deploy":
        return _dispatch_deploy(args)
    if args.command == "package":
        return _dispatch_package(args)
    # 不会被走到（_parse_args 已经限定了已知子命令），但保留兜底。
    logger.error("未知子命令：%s", args.command)
    return 1


def _dispatch_init(args: argparse.Namespace) -> int:
    # 惰性导入：init 路径不该触发守护进程的第三方依赖（KTD10）。
    try:
        from .wizard import run_wizard  # noqa: PLC0415
    except ImportError as e:
        logger.error("无法加载向导模块，请确认 hawkeye 已正确安装：%s", e)
        return 1
    try:
        run_verify = not getattr(args, "no_verify", False)
        asyncio.run(run_wizard(args.config, run_verify=run_verify))
    except (ConfigError, EditError) as e:
        # 子命令的失败一律 1，不复用守护进程的 2（R21）。
        logger.error("init 失败：%s", e)
        return 1
    except KeyboardInterrupt:
        print("\n已中止。")
        return 130
    return 0


def _dispatch_deploy(args: argparse.Namespace) -> int:
    """deploy 子命令入口（U5）；失败一律映射为 1（R21）。"""
    # deploy 涉及 ssh + packaging，第三方依赖都在函数内导入；缺可选依赖时给安装指引。
    from .config import ConfigError  # noqa: PLC0415
    from .configedit import EditError  # noqa: PLC0415
    from .deploy import DeployError, run_deploy  # noqa: PLC0415
    from .packaging import PackageError  # noqa: PLC0415

    config_path = Path(getattr(args, "config", "config.toml"))
    connection_file = Path.cwd() / ".hawkeye-deploy.toml"
    known_hosts_path = Path.home() / ".ssh" / "known_hosts"
    dist_dir = Path.cwd() / "dist"
    packaging_root = Path.cwd()

    # 真实生产用 DefaultRemoteOps；测试可注入替身。
    from .deploy import DefaultRemoteOps  # noqa: PLC0415

    def _remote_ops_factory(**kwargs: Any) -> Any:
        return DefaultRemoteOps(**kwargs)

    try:
        return asyncio.run(
            run_deploy(
                args,
                config_path=config_path,
                connection_file=connection_file,
                known_hosts_path=known_hosts_path,
                dist_dir=dist_dir,
                ask=input,
                ask_secret=_safe_getpass,
                emit=lambda m: print(m),
                remote_ops_factory=_remote_ops_factory,
                packaging_root=packaging_root,
            )
        )
    except (ConfigError, EditError, DeployError, PackageError) as e:
        logger.error("deploy 失败：%s", e)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\n已中止。")
        return 130


def _safe_getpass(prompt: str) -> str:
    """隐藏输入密码；GetPassWarning 时提示用户。

    Ctrl-C（KeyboardInterrupt）与 Ctrl-D（EOFError）均向上冒泡，由调用方
    _dispatch_deploy 统一映射为 R21 退出码 130，不在工具内部吞掉。
    """
    import getpass  # noqa: PLC0415 —— stdlib，仅在 deploy 路径使用
    from getpass import GetPassWarning  # noqa: PLC0415

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        value = getpass.getpass(prompt)
    if caught and any(issubclass(w.category, GetPassWarning) for w in caught):
        print("提示：当前终端可能无法隐藏输入，请确认周围没有旁人。")
    return value


def _dispatch_package(args: argparse.Namespace) -> int:
    # packaging 只 import 标准库（KTD10）：拉起它不会触发守护进程的第三方依赖。
    # --root 已在 _parse_args 的 package_parser 上注册（默认 Path.cwd()），
    # 这里直接用，不再调 packaging.main 重复解析 argv。
    from .packaging import PackageError, build_package, print_summary  # noqa: PLC0415

    try:
        archive = build_package(root=args.root)
    except PackageError as e:
        logger.error("打包失败：%s", e)
        return 1
    print_summary(archive, archive.stem)
    return 0


# ---- 入口 ----


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=_resolve_level(args),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.command is None:
        return _run_daemon_entry(args)
    return _run_subcommand(args)


if __name__ == "__main__":
    sys.exit(main())
