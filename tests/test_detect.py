"""detect 模块测试。"""

from __future__ import annotations

from hawkeye.detect import (
    SEEN_IDS_MAX,
    Baseline,
    Changed,
    NewItems,
    SeenBaseline,
    Unchanged,
    detect,
    detect_new,
    merge_seen,
)


def test_baseline() -> None:
    assert detect(None, "充足") == Baseline("充足")


def test_unchanged() -> None:
    assert detect("充足", "充足") == Unchanged("充足")


def test_changed() -> None:
    assert detect("售罄", "充足") == Changed("售罄", "充足")


# ---- detect_new：列表新条目判定 ----


def test_detect_new_baseline() -> None:
    # Covers R7：previous_seen 为 None → 返回携带当前全部 ID 的基线（去重、保序）
    result = detect_new(None, ["3", "1", "2", "1"])
    assert result == SeenBaseline(ids=("3", "1", "2"))


def test_detect_new_all_seen() -> None:
    # Covers R2：全部 ID 已见 → 新 ID 为空
    result = detect_new(frozenset({"1", "2", "3"}), ["1", "2", "3"])
    assert result == NewItems(new_ids=())


def test_detect_new_partial_preserves_order() -> None:
    # Covers R2：部分未见 → 只返回未见 ID，且保持输入顺序
    result = detect_new(frozenset({"2"}), ["3", "2", "1"])
    assert result == NewItems(new_ids=("3", "1"))


def test_detect_new_dedups_current() -> None:
    result = detect_new(frozenset(), ["5", "5", "6"])
    assert result == NewItems(new_ids=("5", "6"))


def test_detect_new_empty_current_not_baseline() -> None:
    result = detect_new(frozenset({"1"}), [])
    assert result == NewItems(new_ids=())


# ---- merge_seen：已见集合上限裁剪 ----


def test_merge_seen_under_limit_appends_confirmed() -> None:
    # 未超上限时行为与「旧集合 + 本轮确认」拼接一致
    assert merge_seen(("1", "2"), ["3"], ["3"]) == ("1", "2", "3")


def test_merge_seen_trims_oldest_beyond_limit() -> None:
    assert merge_seen(("1", "2", "3"), ["4"], ["4"], limit=3) == ("2", "3", "4")


def test_merge_seen_refreshes_still_visible_ids() -> None:
    # 置顶帖长期挂在列表页上：本轮仍可见的旧 ID 刷新到队尾，裁剪时淘汰的是真正
    # 最久未见的 "1"，否则置顶帖被淘汰后会被当成新帖再推一次。
    assert merge_seen(("pin", "1", "2", "3"), ["4", "pin"], ["4"], limit=4) == (
        "2",
        "3",
        "4",
        "pin",
    )


def test_merge_seen_excludes_unconfirmed_new_ids() -> None:
    # 通知发送失败的新帖不进入已见集合，留待下轮重试（R5）
    assert merge_seen(("1",), ["2", "3"], ["2"]) == ("1", "2")


def test_merge_seen_dedups() -> None:
    assert merge_seen(("1", "2"), ["1", "2"], ["1"]) == ("1", "2")


def test_merge_seen_shrinks_oversized_prior() -> None:
    # 上限引入前遗留的超长集合，下一次落盘即收敛到上限
    prior = tuple(str(i) for i in range(SEEN_IDS_MAX + 50))
    result = merge_seen(prior, ["new"], ["new"])
    assert len(result) == SEEN_IDS_MAX
    assert result[-1] == "new"
    assert "0" not in result
