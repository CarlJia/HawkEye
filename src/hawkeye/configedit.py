"""配置写事务与 raw TOML dict 变换（对 config.toml 的唯一写入口）。

四个纯变换（新增元素 / 新增列表监控 / 删除元素 / 删除列表监控）都在原始 TOML
dict 的深拷贝上做最小改动后返回新 dict，**绝不序列化已级联展开的 Config**（KTD8）：
Config 里 poll_interval 等默认值已逐层落到每个页面 / 元素上，回写会盖满全文、破坏
级联语义并违反 R12。

:func:`write_config` 是唯一写入口，采用「先验证、后替换」事务（KTD9）：先把新 dict
序列化并重新 parse_config 校验（同时验证「可解析」与「语义合法」），通过后才复制出
带时间戳的备份（600、留最近 10 份，KTD10），再写 .tmp（600）并 os.replace 覆盖
（手法同 state.py）。任一步失败都在触碰原文件之前，原 config.toml 字节不变。

权限收紧：POSIX 走 chmod(0o600)，Windows 走 icacls 拿当前用户 SID 授权
（失败只告警、不阻断，KTD15），收紧落在 os.replace 之后对最终路径执行，
``_make_backup`` 产出的每份备份同步走一遍——备份含明文 token，权限必须与正本一致。
"""

from __future__ import annotations

import copy
import logging
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import tomli_w

from .config import Config, ConfigError, parse_config

logger = logging.getLogger(__name__)

_BACKUP_KEEP = 10


class EditError(Exception):
    """配置写事务失败：校验不通过、语义上不允许的变换，或备份 / 替换失败。"""


# ---- 标识回推（必须与 config.py 的缺省回退规则逐字一致，否则删除定位不到） ----


def _merchant_name(merchant: dict[str, Any]) -> str:
    raw = merchant.get("name")
    return raw if isinstance(raw, str) else ""


def _page_name(page: dict[str, Any]) -> str:
    raw = page.get("name")
    if isinstance(raw, str) and raw:
        return raw
    url = page.get("url")
    return url if isinstance(url, str) else ""


def _element_name(element: dict[str, Any]) -> str:
    raw = element.get("name")
    if isinstance(raw, str) and raw:
        return raw
    selector = element.get("selector")
    sel = selector if isinstance(selector, str) else ""
    nth = element.get("nth")
    if nth is None:
        return sel
    return f"{sel}#{nth}"


def _element_identity(
    merchant: dict[str, Any], page: dict[str, Any], element: dict[str, Any]
) -> str:
    return f"{_merchant_name(merchant)} / {_page_name(page)} / {_element_name(element)}"


def _watch_identity(watch: dict[str, Any]) -> str:
    raw = watch.get("name")
    if isinstance(raw, str) and raw:
        name = raw
    else:
        url = watch.get("url")
        name = url if isinstance(url, str) else ""
    return f"watch / {name}"


# ---- 四个纯变换（各自在深拷贝上改，互不耦合） ----


def add_element(
    raw: dict[str, Any],
    url: str,
    selector: str,
    name: str | None = None,
    nth: int | None = None,
    js: str | None = None,
    element_url: str | None = None,
) -> dict[str, Any]:
    """新增一个元素监控，按页面 URL 精确判重合并（R6 / KTD5）。

    URL 命中已有页面 → 并入该页面的 elements；未命中 → 在以 URL host 命名的商家下
    新建页面承载它（同 host 已存在则复用，KTD15）。仅在显式给了 name / nth / js /
    element_url 时才写这些键，否则留空让 config.py 的缺省回退接管。

    ``element_url`` 是元素级跳转 URL：缺省时调度层沿用 page.url 推送通知；填写后
    在 Telegram 消息里挂上这条链接,让用户直跳目标下单/详情页。命名用 element_url
    而不是 url,避免与入参 ``url``（页面 URL）混淆。
    """
    new = copy.deepcopy(raw)
    element: dict[str, Any] = {"selector": selector}
    if name is not None:
        element["name"] = name
    if nth is not None:
        element["nth"] = nth
    if js is not None:
        element["js"] = js
    if element_url is not None:
        element["url"] = element_url

    merchants = new.setdefault("merchants", [])
    for merchant in merchants:
        for page in merchant.get("pages", []):
            if page.get("url") == url:
                page.setdefault("elements", []).append(element)
                return new

    parts = urlsplit(url)
    host_name = parts.hostname or parts.netloc or url
    target: dict[str, Any] | None = None
    for merchant in merchants:
        if merchant.get("name") == host_name:
            target = merchant
            break
    if target is None:
        target = {"name": host_name, "pages": []}
        merchants.append(target)
    target.setdefault("pages", []).append({"url": url, "elements": [element]})
    return new


def add_watch(
    raw: dict[str, Any],
    url: str,
    link_selector: str,
    keywords: Sequence[str],
    name: str | None = None,
    id_pattern: str | None = None,
) -> dict[str, Any]:
    """新增一个论坛关键词监控（watch），追加到顶层 watches。

    watch 是顶层平行目标，不做 URL 合并；URL 已存在则在此挡下（EditError），避免
    上层撞见 _parse_watch 的原始「标识重复」ConfigError。仅在显式给了 name 时才写
    该键，否则留空让 config.py 把标识回退成 URL。
    """
    new = copy.deepcopy(raw)
    watches = new.setdefault("watches", [])
    for watch in watches:
        if watch.get("url") == url:
            raise EditError("该列表页已在监控中，请先删除再新建")
    entry: dict[str, Any] = {
        "url": url,
        "link_selector": link_selector,
        "keywords": list(keywords),
    }
    if name is not None:
        entry["name"] = name
    if id_pattern is not None:
        entry["id_pattern"] = id_pattern
    watches.append(entry)
    return new


def remove_element(raw: dict[str, Any], identity: str) -> dict[str, Any]:
    """按「商家 / 页面 / 元素」标识删除元素；空页面与随之变空的商家一并移除（R10）。"""
    new = copy.deepcopy(raw)
    merchants = new.get("merchants")
    if isinstance(merchants, list):
        for mi, merchant in enumerate(merchants):
            if not isinstance(merchant, dict):
                continue
            pages = merchant.get("pages")
            if not isinstance(pages, list):
                continue
            for pi, page in enumerate(pages):
                if not isinstance(page, dict):
                    continue
                elements = page.get("elements")
                if not isinstance(elements, list):
                    continue
                for ei, element in enumerate(elements):
                    if not isinstance(element, dict):
                        continue
                    if _element_identity(merchant, page, element) != identity:
                        continue
                    del elements[ei]
                    if not elements:
                        del pages[pi]
                        if not pages:
                            del merchants[mi]
                    return new
    raise EditError(f"未找到要删除的监控元素：{identity}")


def remove_watch(raw: dict[str, Any], identity: str) -> dict[str, Any]:
    """从顶层 watches 删除对应目标，其余顺序不动。"""
    new = copy.deepcopy(raw)
    watches = new.get("watches")
    if isinstance(watches, list):
        for wi, watch in enumerate(watches):
            if isinstance(watch, dict) and _watch_identity(watch) == identity:
                del watches[wi]
                return new
    raise EditError(f"未找到要删除的监控目标：{identity}")


# ---- 写事务：先验证、后替换（KTD9） ----


def _unique_backup_path(config_path: Path, ts: str) -> Path:
    """备份路径 config.toml.bak.<时间戳>；同秒冲突时追加 -N 后缀保证唯一。"""
    base = config_path.with_name(f"{config_path.name}.bak.{ts}")
    if not base.exists():
        return base
    i = 1
    while True:
        candidate = config_path.with_name(f"{config_path.name}.bak.{ts}-{i}")
        if not candidate.exists():
            return candidate
        i += 1


def _prune_backups(config_path: Path) -> None:
    """按 mtime 只保留最近 _BACKUP_KEEP 份备份，多出的删掉（KTD10）。"""
    backups = sorted(
        config_path.parent.glob(f"{config_path.name}.bak.*"),
        key=lambda b: b.stat().st_mtime,
        reverse=True,
    )
    for old in backups[_BACKUP_KEEP:]:
        try:
            old.unlink()
        except OSError as e:
            logger.warning("清理旧备份 %s 失败：%s", old, e)


def _make_backup(config_path: Path) -> None:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = _unique_backup_path(config_path, ts)
    # 用 os.open(O_CREAT|O_EXCL, 0o600) 直接创建正确权限的文件，避免 copy2+chmod
    # 之间的 0o644 窗口（P0-2 fix）。
    with os.fdopen(
        os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
    ) as tmp_fd:
        with open(config_path, "rb") as src:
            shutil.copyfileobj(src, tmp_fd)
    # 备份同样含明文 token，权限必须与正本一致（KTD15）。
    _tighten_permissions(backup)
    _prune_backups(config_path)


def _current_user_sid() -> str | None:
    """从 whoami /user 取当前用户的 SID；非 Windows 直接返回 None。

    不用 ``os.getlogin()`` 那样的用户名——域账号、非 ASCII 用户名、本地化的
    ``Users`` 组名都会让按名授权失败。
    """
    if sys.platform != "win32":
        return None
    try:
        result = subprocess.run(  # noqa: S603 —— 命令固定，无 shell 注入
            ["whoami", "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("读取当前用户 SID 失败（whoami /user）：%s", e)
        return None
    # CSV 形如 "S-1-5-21-...\n,name\n"，取第一段。
    line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    sid = line.strip().strip('"').split(",")[0].strip() if line else ""
    if not sid.startswith("S-1-"):
        logger.warning("whoami /user 输出无法解析：%r", result.stdout)
        return None
    return sid


def _tighten_permissions(path: Path) -> None:
    """把含凭据的文件权限收紧到「只有当前用户可读写」。

    POSIX: ``os.chmod(0o600)``。
    Windows: ``icacls`` 去掉继承、只授当前用户 SID 完全控制，失败降级为告警
    （KTD7 / KTD15）——Windows 没有与 600 完全等价的语义，能收紧就收紧，
    收不紧也不阻断主流程，但要让用户明确知道。
    """
    if not path.exists():
        return
    if sys.platform == "win32":
        sid = _current_user_sid()
        if sid is None:
            logger.warning("无法收紧 %s 的权限：当前用户 SID 未取到。", path)
            return
        try:
            subprocess.run(  # noqa: S603 —— 参数列表形式，无 shell 注入
                [
                    "icacls",
                    str(path),
                    "/inheritance:r",
                    "/grant:r",
                    f"{sid}:F",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("icacls 收紧 %s 权限失败，请自行确认它不在共享目录里：%s", path, e)
        return
    try:
        os.chmod(path, 0o600)
    except OSError as e:
        logger.warning("chmod 600 %s 失败：%s", path, e)


def write_config(path: str | Path, new_raw: dict[str, Any]) -> Config:
    """把变换后的 raw dict 以「先验证、后替换」事务写回，返回新 Config（唯一写入口）。"""
    p = Path(path)
    text = tomli_w.dumps(new_raw)

    # ① 序列化结果必须既可解析又语义合法；失败即抛，此时原文件尚未被触碰。
    try:
        config = parse_config(tomllib.loads(text))
    except ConfigError as e:
        raise EditError(f"改写后的配置未通过校验，已放弃写入（原文件未改动）：{e}") from e

    # ② 备份原文件（600、留最近 10 份）。
    if p.exists():
        _make_backup(p)

    # ③ 写 .tmp（600）→ os.replace 原子覆盖。
    # 必须先 unlink：tmp 文件名固定 ``config.toml.tmp``，崩溃残留若不删，
    # O_EXCL 会让任何后续写都失败，把 /add / /del 永久卡死（KTD14）。
    tmp = p.with_name(f"{p.name}.tmp")
    tmp.unlink(missing_ok=True)
    with os.fdopen(os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as tmp_fd:
        tmp_fd.write(text.encode("utf-8"))
    os.replace(tmp, p)
    # ④ 最终路径与备份的权限收紧放在 replace 之后（icacls 只对最终路径生效）。
    _tighten_permissions(p)
    return config
