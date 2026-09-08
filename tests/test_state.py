"""state 模块测试：往返、缺失、损坏备份、原子写。"""

from __future__ import annotations

from pathlib import Path

from hawkeye.state import SeenSetEntry, StateEntry, load_state, save_state


def test_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    state = {"商品A": StateEntry("充足", "2026-09-02T10:00:00+08:00")}
    save_state(p, state)
    assert load_state(p) == state


def test_missing_returns_empty(tmp_path: Path) -> None:
    assert load_state(tmp_path / "nope.json") == {}


def test_atomic_no_tmp_left(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    save_state(p, {"a": StateEntry("v", "t")})
    assert p.exists()
    assert not (tmp_path / "state.json.tmp").exists()


def test_corrupt_backed_up_and_reset(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    p.write_text("not json {{{", encoding="utf-8")
    assert load_state(p) == {}
    backups = list(tmp_path.glob("state.json.corrupt.*"))
    assert len(backups) == 1
    assert not p.exists()


def test_wrong_shape_treated_as_corrupt(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    p.write_text('{"a": {"value": 1}}', encoding="utf-8")
    assert load_state(p) == {}
    assert list(tmp_path.glob("state.json.corrupt.*"))


# ---- 已见集合条目（SeenSetEntry） ----


def test_seen_set_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    state = {
        "watch / NodeSeek 首页": SeenSetEntry(
            seen_ids=("911200", "911201"), updated_at="2026-09-04T10:00:00+08:00"
        )
    }
    save_state(p, state)
    assert load_state(p) == state


def test_seen_set_empty_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    state = {"watch / w": SeenSetEntry(seen_ids=(), updated_at="t")}
    save_state(p, state)
    loaded = load_state(p)
    assert loaded == state
    assert isinstance(loaded["watch / w"], SeenSetEntry)


def test_mixed_shapes_preserved(tmp_path: Path) -> None:
    # 同一文件混合元素条目与集合条目，load/save 均正确保留两类形状
    p = tmp_path / "state.json"
    state = {
        "m / pg / 商品A": StateEntry("充足", "2026-09-04T10:00:00+08:00"),
        "watch / w": SeenSetEntry(seen_ids=("1", "2", "3"), updated_at="2026-09-04T10:00:00+08:00"),
    }
    save_state(p, state)
    loaded = load_state(p)
    assert loaded == state
    assert isinstance(loaded["m / pg / 商品A"], StateEntry)
    assert isinstance(loaded["watch / w"], SeenSetEntry)


def test_seen_ids_non_str_treated_as_corrupt(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    p.write_text('{"watch / w": {"seen_ids": ["ok", 42], "updated_at": "t"}}', encoding="utf-8")
    assert load_state(p) == {}
    assert list(tmp_path.glob("state.json.corrupt.*"))


def test_seen_ids_not_list_treated_as_corrupt(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    p.write_text('{"watch / w": {"seen_ids": "nope", "updated_at": "t"}}', encoding="utf-8")
    assert load_state(p) == {}
    assert list(tmp_path.glob("state.json.corrupt.*"))
