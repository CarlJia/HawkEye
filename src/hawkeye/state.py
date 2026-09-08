"""状态持久化。

扁平 JSON，单文件内支持两种条目形状：

- 元素文本条目 ``{标识: {"value", "updated_at"}}`` → :class:`StateEntry`；
- 列表已见集合条目 ``{标识: {"seen_ids": [...], "updated_at"}}`` → :class:`SeenSetEntry`。

写入采用先写 ``.tmp`` 再 ``os.replace`` 的原子替换；读取到损坏文件时备份并从
空状态重建。两类标识命名空间不重叠（元素为「商家 / 页面 / 元素」，列表监控为
「watch / 名称」），互不干扰。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class StateEntry:
    """单个元素目标的最近已知值。"""

    value: str
    updated_at: str


@dataclass
class SeenSetEntry:
    """单个列表监控目标的已见帖子 ID 集合（保序去重存储）。"""

    seen_ids: tuple[str, ...]
    updated_at: str


Entry = StateEntry | SeenSetEntry


def now_iso() -> str:
    """本地时区、精确到秒的 ISO8601 时间戳。"""
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def load_state(path: str | Path) -> dict[str, Entry]:
    """读取状态文件；不存在返回空；损坏则备份后返回空。"""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("状态文件根节点应为对象")
        result: dict[str, Entry] = {}
        for k, v in raw.items():
            if not isinstance(k, str) or not isinstance(v, dict):
                raise ValueError("状态文件条目格式错误")
            result[k] = _parse_entry(v)
        return result
    except (json.JSONDecodeError, ValueError, OSError) as e:
        _backup_corrupt(p, e)
        return {}


def _parse_entry(v: dict[str, object]) -> Entry:
    """按 JSON 键形状分派：含 seen_ids 为集合条目，否则为元素文本条目。"""
    updated_at = v.get("updated_at")
    if not isinstance(updated_at, str):
        raise ValueError("状态文件条目缺少合法 updated_at")
    if "seen_ids" in v:
        seen_raw = v.get("seen_ids")
        if not isinstance(seen_raw, list):
            raise ValueError("状态文件 seen_ids 必须为列表")
        seen_ids: list[str] = []
        for item in seen_raw:
            if not isinstance(item, str):
                raise ValueError("状态文件 seen_ids 元素必须为字符串")
            seen_ids.append(item)
        return SeenSetEntry(seen_ids=tuple(seen_ids), updated_at=updated_at)
    value = v.get("value")
    if not isinstance(value, str):
        raise ValueError("状态文件条目字段类型错误")
    return StateEntry(value=value, updated_at=updated_at)


def _backup_corrupt(p: Path, e: Exception) -> None:
    ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    backup = p.with_name(f"{p.name}.corrupt.{ts}")
    try:
        p.replace(backup)
        logger.error("状态文件损坏（%s），已备份到 %s 并重置为空", e, backup)
    except OSError as be:
        logger.error("状态文件损坏（%s），且备份失败：%s", e, be)


def _serialize_entry(v: Entry) -> dict[str, object]:
    if isinstance(v, SeenSetEntry):
        return {"seen_ids": list(v.seen_ids), "updated_at": v.updated_at}
    return {"value": v.value, "updated_at": v.updated_at}


def save_state(path: str | Path, state: dict[str, Entry]) -> None:
    """原子写入：先写临时文件再 os.replace 覆盖。"""
    p = Path(path)
    payload = {k: _serialize_entry(v) for k, v in state.items()}
    tmp = p.with_name(f"{p.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
