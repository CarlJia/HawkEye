"""交互式配置向导（本机侧 `hawkeye init`）。

形状照 :mod:`hawkeye.control`：全中文单行提示、输入不合法就地重问、结尾一次
确认。只问最小可启动集——Telegram ``bot_token`` 与 ``chat_id``（KTD2）；其余
``[[merchants]]`` / ``[[watches]]`` 在用户在 Telegram 里 ``/add`` 现场加，不在
这里一并问。

实现纪律：

- **不直接调** ``input`` / ``print``，而是把 ``ask`` / ``ask_secret`` / ``emit`` 作为
  可调用对象注入——``Notifier`` 把 ``httpx.AsyncClient`` 作为构造参数注入是同一
  套路；测试直接传替身，不依赖 ``capsys`` 或 monkeypatch 替身 ``builtins.input``。
- **不直接落盘**。写盘一律经 :func:`configedit.write_config`，保留其「先验证、
  后替换」事务、600 权限与备份修剪（KTD14 / KTD15）。
- **凭据自检先于写盘**（R5）。``Notifier.verify()`` 永久拒绝（400/401/403/404）
  就地重问 ``bot_token`` / ``chat_id``；网络异常由 ``verify`` 自己降级为告警；
  跳过自检的用户完全不加载 ``httpx`` / ``notify``（KTD10）。
- **目标 TOML 已坏时不静默清空**。``load_raw`` 抛 ``ConfigError`` 时如实上抛——
  把 ``[[merchants]]`` / ``[[watches]]`` 静默丢光是不可逆的数据损失。
"""

from __future__ import annotations

import logging
import warnings
from collections.abc import Callable
from getpass import GetPassWarning, getpass
from pathlib import Path
from typing import Any

from .config import TelegramConfig, load_raw
from .configedit import write_config

logger = logging.getLogger(__name__)

_YES = frozenset({"1", "y", "yes", "是", "确认", "确定", "保存"})
_TOKEN_TAIL_LEN = 4  # 摘要里 token 只露尾部若干位（R6）


def _default_ask(prompt: str) -> str:
    return input(prompt)


def _default_ask_secret(prompt: str) -> str:
    """默认隐藏输入：``getpass.getpass`` 在无 tty 时会发 ``GetPassWarning``，就地提示。"""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        value = getpass(prompt)
    if any(issubclass(w.category, GetPassWarning) for w in caught):
        print("提示：当前终端可能无法隐藏输入，请确认周围没有旁人。")
    return value


def _default_emit(message: str) -> None:
    print(message)


# ---- 纯变换：加载现有 raw、构造新 raw ----


def _load_existing(path: Path) -> dict[str, Any]:
    """读目标文件为 raw dict；不存在返回 ``{}``；TOML 语法损坏如实抛。"""
    if not path.exists():
        return {}
    # 故意不兜成 {}：那会把坏 TOML 里的 [[merchants]] / [[watches]] 静默清空。
    return load_raw(path)


def _apply_telegram(raw: dict[str, Any], bot_token: str, chat_id: str) -> dict[str, Any]:
    """只替换 ``raw["telegram"]`` 两个键，其余原样带过（R3）。"""
    new_raw = dict(raw)
    tg = dict(new_raw.get("telegram", {}))
    tg["bot_token"] = bot_token
    tg["chat_id"] = chat_id
    new_raw["telegram"] = tg
    return new_raw


# ---- 单步问答 ----


def _ask_bot_token(ask_secret: Callable[[str], str]) -> str:
    while True:
        value = ask_secret("请输入 Telegram bot_token（输入隐藏）：").strip()
        if value:
            return value
        print("bot_token 不能为空，请重新输入。")


def _ask_chat_id(ask: Callable[[str], str]) -> str:
    """``chat_id`` 允许负数（群聊 supergroup id 为负）。空串重问。"""
    while True:
        value = ask("请输入 Telegram chat_id（个人或群，群 id 通常为负数）：").strip()
        if not value:
            print("chat_id 不能为空，请重新输入。")
            continue
        return value


def _ask_verify(ask: Callable[[str], str]) -> bool:
    """问是否做 Telegram 自检；离线用户可直接跳过。"""
    while True:
        value = ask("是否现在做一次 Telegram 凭据自检？(y/N，回车跳过)：").strip().lower()
        if value == "" or value in {"n", "no", "否"}:
            return False
        if value in _YES or value == "y":
            return True
        print("请回复 y 或 n（回车等同 n）。")


def _ask_confirm_save(ask: Callable[[str], str], bot_token: str, chat_id: str) -> bool:
    """写盘前最后一次确认；显示 token 尾部和 chat_id。"""
    print()
    print("即将写入以下内容：")
    print(f"  bot_token = ***{bot_token[-_TOKEN_TAIL_LEN:]}")
    print(f"  chat_id   = {chat_id}")
    while True:
        value = ask("确认保存？(y/N，回车取消)：").strip().lower()
        if value == "" or value in {"n", "no", "否"}:
            return False
        if value in _YES or value == "y":
            return True
        print("请回复 y 或 n（回车等同 n）。")


# ---- Telegram 自检（R5）----


async def _verify(bot_token: str, chat_id: str) -> tuple[bool, str | None]:
    """凭据自检；返回 ``(ok, message)``。

    - ``TelegramFatalError`` → ``(False, None)``，外层重问。
    - ``httpx`` 网络异常 → ``(True, warning)``：告警但不阻断。
    - 200 → ``(True, None)``。

    ``httpx`` 与 ``notify`` 在这里导入，跳过自检的用户完全不加载（KTD10）。
    """
    import httpx  # noqa: PLC0415 —— 惰性，避免 init 的纯配置路径也拉起网络栈

    from .notify import Notifier, TelegramFatalError  # noqa: PLC0415

    async with httpx.AsyncClient() as client:
        notifier = Notifier(TelegramConfig(bot_token=bot_token, chat_id=chat_id), client)
        try:
            await notifier.verify()
        except TelegramFatalError:
            return False, None
    return True, None


async def _collect_credentials(
    ask: Callable[[str], str],
    ask_secret: Callable[[str], str],
) -> tuple[str, str]:
    """问出 bot_token / chat_id，自检失败就地重问。"""
    while True:
        bot_token = _ask_bot_token(ask_secret)
        chat_id = _ask_chat_id(ask)
        if _ask_verify(ask):
            ok, _ = await _verify(bot_token, chat_id)
            if not ok:
                print("Telegram 拒绝了 bot_token 或 chat_id，请重新填写。")
                continue
        return bot_token, chat_id


# ---- 结束摘要（R6）----


def _format_token_tail(token: str) -> str:
    if len(token) <= _TOKEN_TAIL_LEN:
        return "***（已隐藏）"
    return f"***{token[-_TOKEN_TAIL_LEN:]}"


def _print_summary(
    emit: Callable[[str], None],
    path: Path,
    bot_token: str,
    chat_id: str,
) -> None:
    """打印目标路径、权限状态、token 尾部、chat_id，下一步指向 hawkeye deploy。"""
    try:
        mode = path.stat().st_mode & 0o777
        if mode == 0o600:
            perm_text = "600（仅当前用户可读写）"
        else:
            perm_text = f"{mode:o}"
    except OSError as e:
        perm_text = f"无法读取（{e}）"
    emit("")
    emit("已写入配置：")
    emit(f"  路径       {path}")
    emit(f"  权限       {perm_text}")
    emit(f"  bot_token  {_format_token_tail(bot_token)}")
    emit(f"  chat_id    {chat_id}")
    emit("")
    emit("下一步：运行 `hawkeye deploy` 把这份配置与程序部署到 VPS。")


# ---- 公开入口 ----


async def run_wizard(
    path: str | Path,
    *,
    ask: Callable[[str], str] = _default_ask,
    ask_secret: Callable[[str], str] = _default_ask_secret,
    emit: Callable[[str], None] = _default_emit,
    run_verify: bool = True,
) -> None:
    """交互式向导主入口。

    :param path: 目标配置文件路径（来自顶层 ``-c/--config``）。
    :param ask: 普通输入的可调用对象，测试用替身替换。
    :param ask_secret: 隐藏输入的可调用对象，默认 ``getpass.getpass``。
    :param emit: 输出可调用对象，默认 ``print``。
    :param run_verify: 是否走 Telegram 自检；离线场景可关掉。
    :raises ConfigError: 目标 TOML 已坏——交给上层映射为退出码 1。
    :raises EditError: 写盘事务失败——同上。
    """
    target = Path(path)
    emit(f"开始配置 {target}（Ctrl-C 随时中止，未确认前不会落盘）")

    raw = _load_existing(target)

    if run_verify:
        bot_token, chat_id = await _collect_credentials(ask, ask_secret)
    else:
        bot_token = _ask_bot_token(ask_secret)
        chat_id = _ask_chat_id(ask)

    if not _ask_confirm_save(ask, bot_token, chat_id):
        emit("已取消，未写入任何内容。")
        return

    new_raw = _apply_telegram(raw, bot_token, chat_id)
    write_config(target, new_raw)
    _print_summary(emit, target, bot_token, chat_id)
