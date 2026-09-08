"""config 模块的单元测试（商家 → 页面 → 元素三层结构）。"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from hawkeye.config import (
    ConfigError,
    MonitoredElement,
    detect_selector_type,
    load_config,
    load_raw,
    parse_config,
)

# 最小可用的商家块，拼在各错误用例后面凑齐必填结构。
_MERCHANT = """
[[merchants]]
name = "m"
[[merchants.pages]]
url = "https://e.com"
[[merchants.pages.elements]]
selector = "#a"
"""

_VALID = """
poll_interval_secs = 90
failure_threshold = 5
state_path = "s.json"
max_concurrent_fetches = 2
nav_timeout_secs = 20
wait_until = "load"
selector_type = "css"

[telegram]
bot_token = "123:ABC"
chat_id = "987654321"

[[merchants]]
name = "yunyoo"
poll_interval_secs = 30

[[merchants.pages]]
name = "购物车"
url = "https://yunyoo.cc/cart?fid=1&gid=27"

[[merchants.pages.elements]]
name = "商品A"
selector = "//*[@id=\\"x\\"]/span"
selector_type = "auto"

[[merchants.pages.elements]]
name = "商品B"
selector = "#stock"

[[merchants.pages]]
name = "详情页"
url = "https://yunyoo.cc/p/2"
poll_interval_secs = 15
wait_until = "networkidle"

[[merchants.pages.elements]]
selector = ".price"
"""


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(content, encoding="utf-8")
    return p


def test_valid_config(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _VALID))

    assert cfg.telegram.bot_token == "123:ABC"
    assert cfg.telegram.chat_id == "987654321"
    assert cfg.poll_interval_secs == 90
    assert cfg.failure_threshold == 5
    assert cfg.state_path == "s.json"
    assert cfg.max_concurrent_fetches == 2
    assert cfg.wait_until == "load"

    assert len(cfg.merchants) == 1
    assert len(cfg.pages) == 2
    assert cfg.element_count == 3

    merchant = cfg.merchants[0]
    assert merchant.name == "yunyoo"
    assert len(merchant.pages) == 2

    cart, detail = cfg.pages
    # 购物车页：未写 poll，继承商家 30；wait_until 继承全局 load；failure 继承全局 5
    assert cart.name == "购物车"
    assert cart.poll_interval_secs == 30
    assert cart.wait_until == "load"
    assert cart.failure_threshold == 5
    assert len(cart.elements) == 2

    a, b = cart.elements
    # 元素 A：显式 auto，选择器以 // 开头 -> xpath；identity 为 商家/页面/元素
    assert a.name == "商品A"
    assert a.effective_selector_type == "xpath"
    assert a.identity == "yunyoo / 购物车 / 商品A"
    # 元素 B：selector_type 缺省级联到全局 css
    assert b.name == "商品B"
    assert b.selector_type == "css"
    assert b.effective_selector_type == "css"

    # 详情页：page 级覆盖 poll=15、wait_until=networkidle；元素 name 回退为 selector
    assert detail.poll_interval_secs == 15
    assert detail.wait_until == "networkidle"
    assert detail.elements[0].name == ".price"


def test_defaults_applied(tmp_path: Path) -> None:
    content = """
[telegram]
bot_token = "t"
chat_id = "c"

[[merchants]]
name = "m"
[[merchants.pages]]
url = "https://example.com"
[[merchants.pages.elements]]
selector = "#a"
"""
    cfg = load_config(_write(tmp_path, content))
    assert cfg.poll_interval_secs == 60
    assert cfg.failure_threshold == 3
    assert cfg.state_path == "state.json"
    assert cfg.max_concurrent_fetches == 4
    assert cfg.nav_timeout_secs == 30
    assert cfg.wait_until == "domcontentloaded"

    page = cfg.pages[0]
    assert page.name == "https://example.com"  # name 回退为 url
    assert page.poll_interval_secs == 60
    assert page.failure_threshold == 3
    element = page.elements[0]
    assert element.name == "#a"  # name 回退为 selector
    assert element.selector_type == "auto"


def test_cascade_overrides(tmp_path: Path) -> None:
    content = """
poll_interval_secs = 100
failure_threshold = 9
nav_timeout_secs = 50
wait_until = "load"

[telegram]
bot_token = "t"
chat_id = "c"

[[merchants]]
name = "m"
poll_interval_secs = 50
failure_threshold = 7

[[merchants.pages]]
url = "https://e.com/1"
poll_interval_secs = 25
[[merchants.pages.elements]]
selector = "#a"

[[merchants.pages]]
url = "https://e.com/2"
[[merchants.pages.elements]]
selector = "#b"
"""
    cfg = load_config(_write(tmp_path, content))
    p1, p2 = cfg.pages
    # 页面 1：poll 页面级覆盖 25；failure 继承商家 7；nav/wait 继承全局
    assert p1.poll_interval_secs == 25
    assert p1.failure_threshold == 7
    assert p1.nav_timeout_secs == 50
    assert p1.wait_until == "load"
    # 页面 2：poll 继承商家 50；failure 继承商家 7
    assert p2.poll_interval_secs == 50
    assert p2.failure_threshold == 7


def test_chat_id_accepts_int(tmp_path: Path) -> None:
    content = (
        """
[telegram]
bot_token = "t"
chat_id = 987654321
"""
        + _MERCHANT
    )
    cfg = load_config(_write(tmp_path, content))
    assert cfg.telegram.chat_id == "987654321"


@pytest.mark.parametrize(
    "content",
    [
        '[telegram]\nchat_id = "c"\n' + _MERCHANT,  # 缺 bot_token
        '[telegram]\nbot_token = "t"\n' + _MERCHANT,  # 缺 chat_id
        _MERCHANT,  # 缺 [telegram] 段
    ],
)
def test_missing_telegram_fields(tmp_path: Path, content: str) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, content))


@pytest.mark.parametrize("interval", [0, -1])
def test_non_positive_interval(tmp_path: Path, interval: int) -> None:
    content = f'poll_interval_secs = {interval}\n[telegram]\nbot_token = "t"\nchat_id = "c"\n' + (
        _MERCHANT
    )
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, content))


def test_invalid_selector_type(tmp_path: Path) -> None:
    content = """
[telegram]
bot_token = "t"
chat_id = "c"
[[merchants]]
name = "m"
[[merchants.pages]]
url = "https://e.com"
[[merchants.pages.elements]]
selector = "#a"
selector_type = "regex"
"""
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, content))


def test_invalid_wait_until_at_page(tmp_path: Path) -> None:
    content = """
[telegram]
bot_token = "t"
chat_id = "c"
[[merchants]]
name = "m"
[[merchants.pages]]
url = "https://e.com"
wait_until = "whenever"
[[merchants.pages.elements]]
selector = "#a"
"""
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, content))


def test_invalid_url(tmp_path: Path) -> None:
    content = """
[telegram]
bot_token = "t"
chat_id = "c"
[[merchants]]
name = "m"
[[merchants.pages]]
url = "ftp://e.com"
[[merchants.pages.elements]]
selector = "#a"
"""
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, content))


def test_empty_selector(tmp_path: Path) -> None:
    content = """
[telegram]
bot_token = "t"
chat_id = "c"
[[merchants]]
name = "m"
[[merchants.pages]]
url = "https://e.com"
[[merchants.pages.elements]]
selector = "   "
"""
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, content))


def test_zero_monitors_now_ok(tmp_path: Path) -> None:
    # 既无 merchants 也无 watches 现在应加载成功（KTD13：允许「零监控」，
    # 让「删掉最后一个监控」不至于失败；是否为空由 __main__ 以 warning 提示）。
    content = """
[telegram]
bot_token = "t"
chat_id = "c"
"""
    cfg = load_config(_write(tmp_path, content))
    assert cfg.merchants == ()
    assert cfg.pages == ()
    assert cfg.watches == ()
    assert cfg.element_count == 0


def test_merchant_without_pages(tmp_path: Path) -> None:
    content = """
[telegram]
bot_token = "t"
chat_id = "c"
[[merchants]]
name = "m"
"""
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, content))


def test_page_without_elements(tmp_path: Path) -> None:
    content = """
[telegram]
bot_token = "t"
chat_id = "c"
[[merchants]]
name = "m"
[[merchants.pages]]
url = "https://e.com"
"""
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, content))


def test_duplicate_merchant_name(tmp_path: Path) -> None:
    content = """
[telegram]
bot_token = "t"
chat_id = "c"
[[merchants]]
name = "dup"
[[merchants.pages]]
url = "https://e.com/1"
[[merchants.pages.elements]]
selector = "#a"
[[merchants]]
name = "dup"
[[merchants.pages]]
url = "https://e.com/2"
[[merchants.pages.elements]]
selector = "#b"
"""
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, content))


def test_duplicate_page_identity(tmp_path: Path) -> None:
    content = """
[telegram]
bot_token = "t"
chat_id = "c"
[[merchants]]
name = "m"
[[merchants.pages]]
name = "dup"
url = "https://e.com/1"
[[merchants.pages.elements]]
selector = "#a"
[[merchants.pages]]
name = "dup"
url = "https://e.com/2"
[[merchants.pages.elements]]
selector = "#b"
"""
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, content))


def test_duplicate_element_identity(tmp_path: Path) -> None:
    content = """
[telegram]
bot_token = "t"
chat_id = "c"
[[merchants]]
name = "m"
[[merchants.pages]]
name = "pg"
url = "https://e.com"
[[merchants.pages.elements]]
name = "x"
selector = "#a"
[[merchants.pages.elements]]
name = "x"
selector = "#b"
"""
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, content))


def test_same_selector_different_nth_not_duplicate(tmp_path: Path) -> None:
    content = """
[telegram]
bot_token = "t"
chat_id = "c"
[[merchants]]
name = "m"
[[merchants.pages]]
name = "pg"
url = "https://e.com"
[[merchants.pages.elements]]
selector = ".status"
[[merchants.pages.elements]]
selector = ".status"
nth = 1
"""
    cfg = load_config(_write(tmp_path, content))
    e0, e1 = cfg.pages[0].elements
    # 同选择器、仅 nth 不同、均未显式命名：name 回退带 nth 后缀，标识不撞车
    assert e0.name == ".status"
    assert e0.nth is None
    assert e1.name == ".status#1"
    assert e1.nth == 1
    assert e0.identity != e1.identity


def test_js_field_parsed(tmp_path: Path) -> None:
    # js 非空时原样落库;缺省为 None（不参与回填链）
    content = """
[telegram]
bot_token = "t"
chat_id = "c"
[[merchants]]
name = "m"
[[merchants.pages]]
url = "https://e.com"
[[merchants.pages.elements]]
name = "带JS"
selector = ".btn"
js = "el => el.classList.contains('disabled') ? '售罄' : '可订'"
[[merchants.pages.elements]]
name = "纯文本"
selector = ".price"
"""
    cfg = load_config(_write(tmp_path, content))
    a, b = cfg.pages[0].elements
    assert a.js == "el => el.classList.contains('disabled') ? '售罄' : '可订'"
    assert b.js is None
    # js 不影响 identity（仍按 name 走,与 nth 一致）
    assert a.identity == "m / https://e.com / 带JS"
    assert b.identity == "m / https://e.com / 纯文本"


def test_js_non_string_rejected(tmp_path: Path) -> None:
    # js 非字符串要报错（与 _opt_nth / _opt_str 一致的错误格式）
    content = """
[telegram]
bot_token = "t"
chat_id = "c"
[[merchants]]
name = "m"
[[merchants.pages]]
url = "https://e.com"
[[merchants.pages.elements]]
selector = ".btn"
js = 42
"""
    with pytest.raises(ConfigError, match="js"):
        load_config(_write(tmp_path, content))


def test_element_url_parsed(tmp_path: Path) -> None:
    # url 非空时原样落库；缺省为 None（不参与 identity / 回填链）
    content = """
[telegram]
bot_token = "t"
chat_id = "c"
[[merchants]]
name = "m"
[[merchants.pages]]
url = "https://e.com/page"
[[merchants.pages.elements]]
name = "带跳转"
selector = ".btn"
url = "https://e.com/order"
[[merchants.pages.elements]]
name = "用页面URL"
selector = ".price"
"""
    cfg = load_config(_write(tmp_path, content))
    a, b = cfg.pages[0].elements
    assert a.url == "https://e.com/order"
    assert b.url is None
    # url 不影响 identity（仍按 name 走,与 nth / js 一致）
    assert a.identity == "m / https://e.com/page / 带跳转"
    assert b.identity == "m / https://e.com/page / 用页面URL"


def test_element_url_invalid_scheme_rejected(tmp_path: Path) -> None:
    # url 必须是合法 http(s),与 _validate_url 同一口径
    content = """
[telegram]
bot_token = "t"
chat_id = "c"
[[merchants]]
name = "m"
[[merchants.pages]]
url = "https://e.com"
[[merchants.pages.elements]]
selector = ".btn"
url = "javascript:alert(1)"
"""
    with pytest.raises(ConfigError, match="http"):
        load_config(_write(tmp_path, content))


def test_element_url_empty_string_rejected(tmp_path: Path) -> None:
    # url 非空校验,显式 "" 视作错（与 page.url 行为一致：缺省留空,显式填空则拒绝）
    content = """
[telegram]
bot_token = "t"
chat_id = "c"
[[merchants]]
name = "m"
[[merchants.pages]]
url = "https://e.com"
[[merchants.pages.elements]]
selector = ".btn"
url = ""
"""
    with pytest.raises(ConfigError, match="url"):
        load_config(_write(tmp_path, content))


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.toml")


def test_malformed_toml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "this is = = not toml"))


def test_parse_config_matches_load_config(tmp_path: Path) -> None:
    # parse_config 直接吃 raw dict，结果应与 load_config 走文件路径完全一致。
    from_file = load_config(_write(tmp_path, _VALID))
    from_raw = parse_config(tomllib.loads(_VALID))
    assert from_raw == from_file


def test_load_raw_returns_unparsed_dict(tmp_path: Path) -> None:
    # load_raw 只做「读文件 + 语法解析」，原样返回 TOML 结构（不做级联展开）。
    raw = load_raw(_write(tmp_path, _VALID))
    assert isinstance(raw, dict)
    assert raw["telegram"]["bot_token"] == "123:ABC"
    assert raw["merchants"][0]["name"] == "yunyoo"
    # raw 不含 Config 才有的派生字段，只有原始键。
    assert "pages" not in raw


def test_load_raw_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="配置文件不存在"):
        load_raw(tmp_path / "nope.toml")


def test_load_raw_malformed(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="TOML 解析失败"):
        load_raw(_write(tmp_path, "this is = = not toml"))


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ('//*[@id="x"]/span', "xpath"),
        ("(//div)[1]", "xpath"),
        ("/html/body/div", "xpath"),
        ("xpath=//span", "xpath"),
        ("#stock-status", "css"),
        (".price > span", "css"),
        ("css=//not-really", "css"),
    ],
)
def test_detect_selector_type(selector: str, expected: str) -> None:
    assert detect_selector_type(selector) == expected


def test_element_identity() -> None:
    el = MonitoredElement(
        merchant_name="yunyoo",
        page_name="购物车",
        name="商品A",
        selector="#a",
        selector_type="css",
        nth=None,
    )
    assert el.identity == "yunyoo / 购物车 / 商品A"


# ---- 列表监控目标 [[watches]] ----


def test_valid_watch(tmp_path: Path) -> None:
    content = r"""
poll_interval_secs = 90
nav_timeout_secs = 20
wait_until = "load"

[telegram]
bot_token = "t"
chat_id = "c"

[[watches]]
name = "NodeSeek 首页"
url = "https://www.nodeseek.com/"
link_selector = '//*[@id="nsk-body-left"]/ul/li/div/div[1]/a'
keywords = ["hk", "HK"]
id_pattern = 'post-(\d+)-'
poll_interval_secs = 45
"""
    cfg = load_config(_write(tmp_path, content))
    assert len(cfg.merchants) == 0
    assert len(cfg.watches) == 1
    w = cfg.watches[0]
    assert w.name == "NodeSeek 首页"
    assert w.url == "https://www.nodeseek.com/"
    assert w.link_selector == '//*[@id="nsk-body-left"]/ul/li/div/div[1]/a'
    assert w.keywords == ("hk", "HK")
    assert w.id_pattern == r"post-(\d+)-"
    assert w.poll_interval_secs == 45  # watch 级覆盖
    assert w.nav_timeout_secs == 20  # 继承全局
    assert w.wait_until == "load"  # 继承全局
    assert w.effective_selector_type == "xpath"  # // 开头，auto → xpath
    assert w.identity == "watch / NodeSeek 首页"


def test_watch_id_pattern_optional_and_name_falls_back(tmp_path: Path) -> None:
    content = """
[telegram]
bot_token = "t"
chat_id = "c"
[[watches]]
url = "https://e.com/list"
link_selector = "ul li a"
keywords = ["hk"]
"""
    cfg = load_config(_write(tmp_path, content))
    w = cfg.watches[0]
    assert w.id_pattern is None
    assert w.name == "https://e.com/list"  # 未写 name，回退为 url
    assert w.effective_selector_type == "css"


def test_watches_only_no_merchants_ok(tmp_path: Path) -> None:
    # 只配置 watches、无 merchants 应加载成功（KTD1 放宽「至少一个 merchant」校验）
    content = """
[telegram]
bot_token = "t"
chat_id = "c"
[[watches]]
url = "https://e.com"
link_selector = "a"
keywords = ["hk"]
"""
    cfg = load_config(_write(tmp_path, content))
    assert cfg.merchants == ()
    assert len(cfg.watches) == 1


def test_merchant_only_config_has_empty_watches(tmp_path: Path) -> None:
    # 既有 merchant-only 配置行为不变，watches 默认空（回归）
    cfg = load_config(_write(tmp_path, _VALID))
    assert cfg.watches == ()


@pytest.mark.parametrize(
    "content",
    [
        # 缺 url
        '[telegram]\nbot_token = "t"\nchat_id = "c"\n'
        '[[watches]]\nlink_selector = "a"\nkeywords = ["hk"]\n',
        # 缺 link_selector
        '[telegram]\nbot_token = "t"\nchat_id = "c"\n'
        '[[watches]]\nurl = "https://e.com"\nkeywords = ["hk"]\n',
        # 缺 keywords
        '[telegram]\nbot_token = "t"\nchat_id = "c"\n'
        '[[watches]]\nurl = "https://e.com"\nlink_selector = "a"\n',
        # keywords 为空列表
        '[telegram]\nbot_token = "t"\nchat_id = "c"\n'
        '[[watches]]\nurl = "https://e.com"\nlink_selector = "a"\nkeywords = []\n',
        # keywords 含非字符串
        '[telegram]\nbot_token = "t"\nchat_id = "c"\n'
        '[[watches]]\nurl = "https://e.com"\nlink_selector = "a"\nkeywords = [1]\n',
        # url 非法
        '[telegram]\nbot_token = "t"\nchat_id = "c"\n'
        '[[watches]]\nurl = "ftp://e.com"\nlink_selector = "a"\nkeywords = ["hk"]\n',
    ],
)
def test_watch_invalid_fields(tmp_path: Path, content: str) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, content))


def test_watch_invalid_id_pattern(tmp_path: Path) -> None:
    content = r"""
[telegram]
bot_token = "t"
chat_id = "c"
[[watches]]
url = "https://e.com"
link_selector = "a"
keywords = ["hk"]
id_pattern = 'post-(\d+'
"""
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, content))


def test_duplicate_watch_name(tmp_path: Path) -> None:
    content = """
[telegram]
bot_token = "t"
chat_id = "c"
[[watches]]
name = "dup"
url = "https://e.com/1"
link_selector = "a"
keywords = ["x"]
[[watches]]
name = "dup"
url = "https://e.com/2"
link_selector = "a"
keywords = ["y"]
"""
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, content))


# ---- fingerprint / proxy 解析（Chromix 风格 launch 参数的 TOML 入口）----


def test_global_fingerprint_parses(tmp_path: Path) -> None:
    """全局 [fingerprint] 子表被解析并沿 merchant / page 链回填。"""
    content = """
[telegram]
bot_token = "t"
chat_id = "c"

[fingerprint]
user_agent = "ua/1.0"
locale = "zh-CN"
timezone_id = "Asia/Shanghai"
color_scheme = "dark"
viewport = { width = 1440, height = 900 }

[[merchants]]
name = "m"
[[merchants.pages]]
url = "https://e.com"
[[merchants.pages.elements]]
selector = "#a"
"""
    cfg = load_config(_write(tmp_path, content))
    assert cfg.fingerprint.user_agent == "ua/1.0"
    assert cfg.fingerprint.locale == "zh-CN"
    assert cfg.fingerprint.timezone_id == "Asia/Shanghai"
    assert cfg.fingerprint.color_scheme == "dark"
    assert cfg.fingerprint.viewport == {"width": 1440, "height": 900}
    # merchant / page 沿 base 链回填
    page = cfg.pages[0]
    assert page.fingerprint.user_agent == "ua/1.0"
    assert page.fingerprint.timezone_id == "Asia/Shanghai"
    assert page.proxy is None


def test_merchant_fingerprint_overrides_global(tmp_path: Path) -> None:
    """商家级 fingerprint 子表覆盖全局；其下所有页面继承此覆盖。

    TOML 不允许同一张表内有两个同名 section header，所以商家级 fingerprint
    走 inline table（``fingerprint = { ... }``）；这与 Chromix 的字典形参风格一致。
    """
    content = """
[telegram]
bot_token = "t"
chat_id = "c"

[fingerprint]
locale = "en-US"

[[merchants]]
name = "m"
fingerprint = { locale = "ja-JP" }

[[merchants.pages]]
url = "https://e.com"
[[merchants.pages.elements]]
selector = "#a"
"""
    cfg = load_config(_write(tmp_path, content))
    assert cfg.fingerprint.locale == "en-US"  # 全局未变
    assert cfg.merchants[0].fingerprint.locale == "ja-JP"  # 商家覆盖
    assert cfg.pages[0].fingerprint.locale == "ja-JP"  # 页面继承商家


def test_proxy_string_shorthand_parsed(tmp_path: Path) -> None:
    """Chromix 风格：``proxy = "http://user:pass@host:port"`` 字符串简写被拆为结构化字段。

    TOML 顶层 ``proxy = "..."`` 与现有顶层字段（``wait_until`` / ``nav_timeout_secs``）
    同一层级——结构化形式则用 ``proxy = { ... }`` 同一字段覆盖（inline table）。
    注意 TOML 把同 section header 内的 key 视为该 table 字段，所以 ``proxy`` 必须
    写在 ``[telegram]`` **之前**（与现有 ``wait_until`` 等全局字段同一段）。
    """
    content = """
proxy = "http://alice:s3cret@10.0.0.1:7890"

[telegram]
bot_token = "t"
chat_id = "c"

[[merchants]]
name = "m"
[[merchants.pages]]
url = "https://e.com"
[[merchants.pages.elements]]
selector = "#a"
"""
    cfg = load_config(_write(tmp_path, content))
    assert cfg.proxy is not None
    assert cfg.proxy.username == "alice"
    assert cfg.proxy.password == "s3cret"
    assert cfg.proxy.server == "http://10.0.0.1:7890"
    assert cfg.proxy.bypass is None


def test_proxy_table_form_parsed(tmp_path: Path) -> None:
    """结构化 ``proxy = { ... }``：HTTP 与 SOCKS5 server 都接受；其余字段可选。"""
    content = """
proxy = { server = "socks5://1.2.3.4:1080", username = "u", password = "p", bypass = "*.x" }

[telegram]
bot_token = "t"
chat_id = "c"

[[merchants]]
name = "m"
[[merchants.pages]]
url = "https://e.com"
[[merchants.pages.elements]]
selector = "#a"
"""
    cfg = load_config(_write(tmp_path, content))
    assert cfg.proxy is not None
    assert cfg.proxy.server == "socks5://1.2.3.4:1080"
    assert cfg.proxy.bypass == "*.x"


def test_proxy_invalid_scheme_rejected(tmp_path: Path) -> None:
    """非 http(s)/socks5 scheme 的 server 直接拒绝。"""
    content = """
proxy = { server = "ftp://x" }

[telegram]
bot_token = "t"
chat_id = "c"

[[merchants]]
name = "m"
[[merchants.pages]]
url = "https://e.com"
[[merchants.pages.elements]]
selector = "#a"
"""
    with pytest.raises(ConfigError, match="proxy.server"):
        load_config(_write(tmp_path, content))


def test_fingerprint_invalid_timezone_rejected(tmp_path: Path) -> None:
    """非 IANA 形状的 timezone_id 拒绝；防止下游 Playwright 静默接受但行为异常。"""
    content = """
[telegram]
bot_token = "t"
chat_id = "c"

[fingerprint]
timezone_id = "Not/A Real TZ!!!"

[[merchants]]
name = "m"
[[merchants.pages]]
url = "https://e.com"
[[merchants.pages.elements]]
selector = "#a"
"""
    with pytest.raises(ConfigError, match="timezone_id"):
        load_config(_write(tmp_path, content))


def test_fingerprint_invalid_color_scheme_rejected(tmp_path: Path) -> None:
    """color_scheme 必须是枚举值，避免 Playwright 抛 ValueError 时连失败原因都难定位。"""
    content = """
[telegram]
bot_token = "t"
chat_id = "c"

[fingerprint]
color_scheme = "neon"

[[merchants]]
name = "m"
[[merchants.pages]]
url = "https://e.com"
[[merchants.pages.elements]]
selector = "#a"
"""
    with pytest.raises(ConfigError, match="color_scheme"):
        load_config(_write(tmp_path, content))


def test_watch_fingerprint_and_proxy(tmp_path: Path) -> None:
    """watch 顶层也可独立配 fingerprint / proxy，与全局并行存在。"""
    content = """
[telegram]
bot_token = "t"
chat_id = "c"

[fingerprint]
locale = "en-US"

[[watches]]
name = "w"
url = "https://e.com/list"
link_selector = "a"
keywords = ["k"]
fingerprint = { locale = "ja-JP" }
proxy = "socks5://1.2.3.4:1080"
"""
    cfg = load_config(_write(tmp_path, content))
    assert cfg.watches[0].fingerprint.locale == "ja-JP"
    assert cfg.watches[0].proxy is not None
    assert cfg.watches[0].proxy.server == "socks5://1.2.3.4:1080"
    # 全局未动
    assert cfg.fingerprint.locale == "en-US"


def test_viewport_must_be_positive_ints(tmp_path: Path) -> None:
    """viewport.width / height 必须为正整数；零 / 负 / 浮点都拒绝。"""
    content = """
[telegram]
bot_token = "t"
chat_id = "c"

[fingerprint]
viewport = { width = 0, height = 100 }

[[merchants]]
name = "m"
[[merchants.pages]]
url = "https://e.com"
[[merchants.pages.elements]]
selector = "#a"
"""
    with pytest.raises(ConfigError, match="viewport.width"):
        load_config(_write(tmp_path, content))


def test_fingerprint_to_context_kwargs() -> None:
    """Fingerprint.to_context_kwargs 把 fingerprint 翻译为 Playwright kwargs 的契约。

    直接构造 Fingerprint + 断言输出，避免依赖 Config 解析——这是 fetch.py 内部
    _new_context 的入口契约，错了会直接破坏反爬指纹一致性。
    """
    from hawkeye.config import Fingerprint

    fp = Fingerprint(
        user_agent="ua/1.0",
        locale="ja-JP",
        timezone_id="Asia/Tokyo",
        color_scheme="dark",
        viewport={"width": 1920, "height": 1080},
    )
    out = fp.to_context_kwargs(fallback_ua="fallback")
    assert out == {
        "user_agent": "ua/1.0",
        "locale": "ja-JP",
        "timezone_id": "Asia/Tokyo",
        "color_scheme": "dark",
        "viewport": {"width": 1920, "height": 1080},
    }

    # 缺省时回落 fallback_ua，不在 kwargs 里塞 None。
    empty = Fingerprint().to_context_kwargs(fallback_ua="fb/1.0")
    assert empty == {"user_agent": "fb/1.0"}
    assert "locale" not in empty
    assert "timezone_id" not in empty
