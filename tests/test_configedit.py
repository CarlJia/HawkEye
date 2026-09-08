"""configedit 模块的单元测试（raw dict 变换 + 先验证后替换的写事务）。"""

from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path
from typing import Any

import pytest

from hawkeye.config import load_config, load_raw
from hawkeye.configedit import (
    EditError,
    add_element,
    add_watch,
    remove_element,
    remove_watch,
    write_config,
)

# 两个商家、各一页各一元素：够验证「只动目标、旁边完全不受影响」。
_BASE = """
poll_interval_secs = 90
failure_threshold = 5
state_path = "s.json"

[telegram]
bot_token = "123:ABC"
chat_id = "987654321"

[[merchants]]
name = "yunyoo"
poll_interval_secs = 30

[[merchants.pages]]
name = "购物车"
url = "https://yunyoo.cc/cart"

[[merchants.pages.elements]]
name = "商品A"
selector = "#a"

[[merchants]]
name = "other"

[[merchants.pages]]
url = "https://other.com/p"

[[merchants.pages.elements]]
selector = ".price"
"""

_ONLY_TELEGRAM = """
[telegram]
bot_token = "t"
chat_id = "c"
"""


def _write(tmp_path: Path, content: str = _BASE) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(content, encoding="utf-8")
    os.chmod(p, 0o600)
    return p


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


# ---- add_element ----


def test_add_element_merges_existing_url() -> None:
    # AE1：URL 命中已有页面 → 并入该页面，商家 / 页面条数都不变。
    raw = tomllib.loads(_BASE)
    new = add_element(raw, "https://yunyoo.cc/cart", "#b")

    assert len(new["merchants"]) == 2
    assert len(new["merchants"][0]["pages"]) == 1
    elements = new["merchants"][0]["pages"][0]["elements"]
    assert [e["selector"] for e in elements] == ["#a", "#b"]
    # 原 dict 未被就地修改（纯变换）
    assert len(raw["merchants"][0]["pages"][0]["elements"]) == 1


def test_add_element_creates_host_merchant_for_new_url() -> None:
    # AE2：URL 未命中 → 新建以 URL host 命名的商家承载该页面。
    new = add_element(tomllib.loads(_BASE), "https://shop.example.com/x?a=1", "#c")

    assert [m["name"] for m in new["merchants"]] == ["yunyoo", "other", "shop.example.com"]
    page = new["merchants"][2]["pages"][0]
    assert page["url"] == "https://shop.example.com/x?a=1"
    assert "name" not in page  # 页面不写 name，交给缺省回退（KTD15）
    assert page["elements"] == [{"selector": "#c"}]


def test_add_element_reuses_host_merchant() -> None:
    # 同 host 第二次新建 → 复用该商家，不重复建商家。
    new = add_element(tomllib.loads(_BASE), "https://shop.example.com/x", "#c")
    new = add_element(new, "https://shop.example.com/y", "#d")

    assert [m["name"] for m in new["merchants"]] == ["yunyoo", "other", "shop.example.com"]
    assert len(new["merchants"][2]["pages"]) == 2


def test_add_element_omits_name_and_nth(tmp_path: Path) -> None:
    # 未显式给 name / nth 时不写这两个键，标识回退为 selector。
    new = add_element(tomllib.loads(_BASE), "https://yunyoo.cc/cart", "#b")
    assert new["merchants"][0]["pages"][0]["elements"][1] == {"selector": "#b"}

    cfg = write_config(_write(tmp_path), new)
    identities = [e.identity for e in cfg.pages[0].elements]
    assert identities == ["yunyoo / 购物车 / 商品A", "yunyoo / 购物车 / #b"]


def test_add_element_writes_name_and_nth_when_given() -> None:
    new = add_element(tomllib.loads(_BASE), "https://yunyoo.cc/cart", ".s", name="库存", nth=1)
    assert new["merchants"][0]["pages"][0]["elements"][1] == {
        "selector": ".s",
        "name": "库存",
        "nth": 1,
    }


def test_add_element_writes_element_url_when_given() -> None:
    # 显式给 element_url 时落到 element.url 键,缺省时省略该键（让 _opt_url 接管）
    new = add_element(
        tomllib.loads(_BASE),
        "https://yunyoo.cc/cart",
        ".s",
        element_url="https://yunyoo.cc/order/123",
    )
    assert new["merchants"][0]["pages"][0]["elements"][1] == {
        "selector": ".s",
        "url": "https://yunyoo.cc/order/123",
    }


def test_add_element_on_config_without_merchants(tmp_path: Path) -> None:
    # 零监控配置里没有 merchants 段，变换必须自己建出来而不是 KeyError。
    new = add_element(tomllib.loads(_ONLY_TELEGRAM), "https://e.com/p", "#a")
    cfg = write_config(_write(tmp_path, _ONLY_TELEGRAM), new)
    assert cfg.element_count == 1
    assert cfg.merchants[0].name == "e.com"


# ---- add_watch ----


def test_add_watch_appends_without_name(tmp_path: Path) -> None:
    new = add_watch(tomllib.loads(_BASE), "https://ns.com/", "ul li a", ["hk", "HK"])
    assert new["watches"] == [
        {"url": "https://ns.com/", "link_selector": "ul li a", "keywords": ["hk", "HK"]}
    ]

    cfg = write_config(_write(tmp_path), new)
    assert cfg.watches[0].identity == "watch / https://ns.com/"  # name 回退为 url
    assert cfg.watches[0].id_pattern is None


def test_add_watch_writes_name_when_given(tmp_path: Path) -> None:
    new = add_watch(tomllib.loads(_BASE), "https://ns.com/", "a", ["hk"], name="NS 交易区")
    assert new["watches"][0]["name"] == "NS 交易区"

    cfg = write_config(_write(tmp_path), new)
    assert cfg.watches[0].identity == "watch / NS 交易区"


def test_add_watch_writes_id_pattern_when_given() -> None:
    new = add_watch(tomllib.loads(_BASE), "https://ns.com/", "a", ["hk"], id_pattern=r"p-(\d+)")
    assert new["watches"][0]["id_pattern"] == r"p-(\d+)"


def test_add_watch_duplicate_url_raises() -> None:
    new = add_watch(tomllib.loads(_BASE), "https://ns.com/", "a", ["hk"])
    with pytest.raises(EditError, match="该列表页已在监控中"):
        add_watch(new, "https://ns.com/", "a", ["other"])


def test_add_watch_on_config_without_watches(tmp_path: Path) -> None:
    # 配置里没有 watches 段时同样要自己建出来。
    new = add_watch(tomllib.loads(_ONLY_TELEGRAM), "https://ns.com/", "a", ["hk"])
    cfg = write_config(_write(tmp_path, _ONLY_TELEGRAM), new)
    assert len(cfg.watches) == 1


# ---- remove_element / remove_watch ----


def test_remove_element_prunes_empty_page_and_merchant() -> None:
    # AE4：删掉页面下最后一个元素 → 空页面与随之变空的商家一并消失，另一商家不受影响。
    new = remove_element(tomllib.loads(_BASE), "yunyoo / 购物车 / 商品A")

    assert [m["name"] for m in new["merchants"]] == ["other"]
    assert new["merchants"][0]["pages"][0]["elements"] == [{"selector": ".price"}]


def test_remove_element_keeps_siblings() -> None:
    raw = add_element(tomllib.loads(_BASE), "https://yunyoo.cc/cart", "#b")
    new = remove_element(raw, "yunyoo / 购物车 / #b")

    assert len(new["merchants"]) == 2
    assert new["merchants"][0]["pages"][0]["elements"] == [{"name": "商品A", "selector": "#a"}]


def test_remove_element_identity_with_nth_fallback() -> None:
    # 未命名但带 nth 的元素，标识回退为 selector#nth，删除要能按同一口径定位到。
    raw = add_element(tomllib.loads(_BASE), "https://yunyoo.cc/cart", ".s", nth=1)
    new = remove_element(raw, "yunyoo / 购物车 / .s#1")
    assert len(new["merchants"][0]["pages"][0]["elements"]) == 1


def test_remove_element_missing_raises() -> None:
    with pytest.raises(EditError, match="未找到要删除的监控元素"):
        remove_element(tomllib.loads(_BASE), "yunyoo / 购物车 / 不存在")


def test_remove_watch_only_removes_target() -> None:
    raw = add_watch(tomllib.loads(_BASE), "https://a.com/", "a", ["x"])
    raw = add_watch(raw, "https://b.com/", "a", ["y"])
    new = remove_watch(raw, "watch / https://a.com/")

    assert [w["url"] for w in new["watches"]] == ["https://b.com/"]


def test_remove_watch_missing_raises() -> None:
    with pytest.raises(EditError, match="未找到要删除的监控目标"):
        remove_watch(tomllib.loads(_BASE), "watch / https://nope.com/")


# ---- write_config ----


def test_write_config_roundtrip_keeps_config_equal(tmp_path: Path) -> None:
    # R12：原样写回后 Config 逐字段相等，[telegram] 与全局默认完好。
    p = _write(tmp_path)
    before = load_config(p)
    returned = write_config(p, load_raw(p))

    assert returned == before
    assert load_config(p) == before


def test_write_config_adds_no_cascaded_keys(tmp_path: Path) -> None:
    # KTD8 的机器可验证形式：写回的文件里，页面 / 元素上没有多出原本没有的键
    # （若误把已级联展开的 Config 序列化回去，这里会冒出 poll_interval_secs 等）。
    p = _write(tmp_path)
    original_raw = load_raw(p)
    write_config(p, original_raw)

    assert load_raw(p) == original_raw


def test_write_config_preserves_permissions(tmp_path: Path) -> None:
    p = _write(tmp_path)
    write_config(p, load_raw(p))
    assert _mode(p) == 0o600


def test_write_config_backup_content_and_perms(tmp_path: Path) -> None:
    p = _write(tmp_path)
    original_bytes = p.read_bytes()
    write_config(p, add_element(load_raw(p), "https://yunyoo.cc/cart", "#b"))

    backups = list(tmp_path.glob("config.toml.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original_bytes  # 备份是写前原文
    assert _mode(backups[0]) == 0o600  # 备份含明文 token，同样 600
    assert p.read_bytes() != original_bytes  # 原文件确实被改写了


def test_write_config_backup_keeps_ten(tmp_path: Path) -> None:
    p = _write(tmp_path)
    for i in range(12):
        write_config(p, add_element(load_raw(p), "https://yunyoo.cc/cart", f"#s{i}"))

    assert len(list(tmp_path.glob("config.toml.bak.*"))) == 10
    assert load_config(p).element_count == 14  # 原有 2 个 + 12 次新增都落了盘


def test_write_config_validation_failure_leaves_file_untouched(tmp_path: Path) -> None:
    p = _write(tmp_path)
    original_bytes = p.read_bytes()
    broken = load_raw(p)
    del broken["merchants"][0]["pages"][0]["url"]  # 必填字段缺失，必然解析失败

    with pytest.raises(EditError, match="未通过校验"):
        write_config(p, broken)

    assert p.read_bytes() == original_bytes
    assert list(tmp_path.glob("config.toml.tmp")) == []
    assert list(tmp_path.glob("config.toml.bak.*")) == []


def test_write_config_creates_file_when_absent(tmp_path: Path) -> None:
    # 文件不存在时不做备份，直接落盘并保持 600。
    p = tmp_path / "config.toml"
    write_config(p, tomllib.loads(_BASE))

    assert _mode(p) == 0o600
    assert list(tmp_path.glob("config.toml.bak.*")) == []
    assert load_config(p).element_count == 2


def test_write_config_recovers_from_existing_tmp(tmp_path: Path) -> None:
    """KTD14：写盘前 ``config.toml.tmp`` 残留不能卡死 ``/add`` / ``/del``。

    tmp 文件名固定，一次崩溃会留下残留；O_EXCL 直接打开会失败，所以
    write_config 必须先 unlink(missing_ok=True) 再 O_EXCL 创建——这条也是
    把 0644 窗口关掉的关键改造。
    """
    p = _write(tmp_path)
    tmp = tmp_path / "config.toml.tmp"
    # 模拟上一次崩溃留下的残留：内容是错的、权限也不是 600。
    tmp.write_text("STALE FROM PREVIOUS CRASH", encoding="utf-8")
    os.chmod(tmp, 0o644)

    write_config(p, add_element(load_raw(p), "https://yunyoo.cc/cart", "#c"))

    assert p.read_text(encoding="utf-8") != "STALE FROM PREVIOUS CRASH"
    assert not tmp.exists()  # 写完后临时文件已被替换走
    assert _mode(p) == 0o600


def test_write_config_writes_tmp_atomically_with_600(tmp_path: Path) -> None:
    """KTD14：tmp 文件直接以 0o600 创建，没有 0644 中间窗口。"""
    p = _write(tmp_path)
    real_open = os.open

    seen_modes: list[int] = []

    def watching_open(
        path: str | bytes,
        flags: int,
        mode: int = 0o777,
        *args: Any,
        **kw: Any,
    ) -> int:
        is_tmp = isinstance(path, str) and path.endswith("config.toml.tmp")
        if is_tmp and (flags & os.O_CREAT):
            seen_modes.append(mode)
        return real_open(path, flags, mode, *args, **kw)

    monkeypatch_session = pytest.MonkeyPatch()
    monkeypatch_session.setattr(os, "open", watching_open)
    try:
        write_config(p, add_element(load_raw(p), "https://yunyoo.cc/cart", "#c"))
    finally:
        monkeypatch_session.undo()

    assert seen_modes, "应该看到对 config.toml.tmp 的 O_CREAT open"
    assert all(mode == 0o600 for mode in seen_modes)
