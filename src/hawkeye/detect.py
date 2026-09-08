"""变更检测（纯函数）。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Baseline:
    """首次观测，仅建立基线，不通知。"""

    value: str


@dataclass(frozen=True)
class Unchanged:
    """与上次相同。"""

    value: str


@dataclass(frozen=True)
class Changed:
    """发生变更，需通知。"""

    old: str
    new: str


DetectResult = Baseline | Unchanged | Changed


def detect(previous: str | None, current: str) -> DetectResult:
    """比较上次值与当前值。previous 为 None 表示尚无记录。"""
    if previous is None:
        return Baseline(value=current)
    if previous == current:
        return Unchanged(value=current)
    return Changed(old=previous, new=current)


# ---- 列表新条目判定 ----


@dataclass(frozen=True)
class SeenBaseline:
    """首次观测某列表监控目标：携带当前全部去重 ID，供静默建基线、不通知。"""

    ids: tuple[str, ...]


@dataclass(frozen=True)
class NewItems:
    """稳态：携带本轮相对已见集合的全部新 ID（保持列表顺序、去重）。"""

    new_ids: tuple[str, ...]


DetectNewResult = SeenBaseline | NewItems


def detect_new(previous_seen: frozenset[str] | None, current_ids: Sequence[str]) -> DetectNewResult:
    """对照已见集合算出新帖 ID。

    ``previous_seen is None`` → 首次运行，返回 :class:`SeenBaseline`（当前全部 ID
    去重、保序），供上层静默记入、不推送；否则返回 :class:`NewItems`，只含不在
    已见集合中的 ID，保持输入顺序并去重。无副作用、不触碰 I/O。
    """
    # 去重且保序：dict 保留首次插入顺序，故不能替换成无序的 set。
    deduped = list(dict.fromkeys(current_ids))
    if previous_seen is None:
        return SeenBaseline(ids=tuple(deduped))
    return NewItems(new_ids=tuple(pid for pid in deduped if pid not in previous_seen))


# 已见集合的条数上限，超出后淘汰最久未见的 ID。取值需远大于单页条目数（论坛首页
# 量级为数十条），否则本轮仍可见的帖子会被淘汰、下轮又当成新帖推一次。
SEEN_IDS_MAX = 1000


def merge_seen(
    prior: Sequence[str],
    current_ids: Sequence[str],
    confirmed: Sequence[str],
    *,
    limit: int = SEEN_IDS_MAX,
) -> tuple[str, ...]:
    """并入本轮确认的新 ID，并把已见集合压到 ``limit`` 条以内。

    集合按「最久未见 → 最近见过」排序，超限时从队首淘汰。本轮仍出现在列表页上的
    ID 会被刷新到队尾：置顶帖长期挂在页面上，若纯按插入顺序淘汰，它迟早被挤出集合
    并在下轮被当成新帖再推一次。无副作用、不触碰 I/O。
    """
    seen = dict.fromkeys(prior)
    seen.update(dict.fromkeys(confirmed))
    for pid in current_ids:
        if pid in seen:
            del seen[pid]  # dict 保留插入顺序，删后重插即「移到队尾」
            seen[pid] = None
    return tuple(seen)[-limit:]
