//! config 模块的移植测试（对应 Python tests/test_config.py）。

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

use hawkeye::config::{
    MonitoredElement, detect_selector_type, load_config, load_raw, parse_config,
};

static COUNTER: AtomicU64 = AtomicU64::new(0);

fn tmp_dir() -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "hawkeye_cfg_test_{}_{}",
        std::process::id(),
        COUNTER.fetch_add(1, Ordering::SeqCst)
    ));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn write(content: &str) -> PathBuf {
    let dir = tmp_dir();
    let p = dir.join("config.toml");
    std::fs::write(&p, content).unwrap();
    p
}

/// 最小可用的商家块，拼在各错误用例后面凑齐必填结构。
const MERCHANT: &str = r##"
[[merchants]]
name = "m"
[[merchants.pages]]
url = "https://e.com"
[[merchants.pages.elements]]
selector = "#a"
"##;

const VALID: &str = r##"
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
selector = "//*[@id=\"x\"]/span"
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
"##;

#[test]
fn test_valid_config() {
    let cfg = load_config(&write(VALID)).unwrap();
    assert_eq!(cfg.telegram.bot_token, "123:ABC");
    assert_eq!(cfg.telegram.chat_id, "987654321");
    assert_eq!(cfg.poll_interval_secs, 90);
    assert_eq!(cfg.failure_threshold, 5);
    assert_eq!(cfg.state_path, "s.json");
    assert_eq!(cfg.max_concurrent_fetches, 2);
    assert_eq!(cfg.wait_until, "load");

    assert_eq!(cfg.merchants.len(), 1);
    assert_eq!(cfg.pages().len(), 2);
    assert_eq!(cfg.element_count(), 3);

    let merchant = &cfg.merchants[0];
    assert_eq!(merchant.name, "yunyoo");
    assert_eq!(merchant.pages.len(), 2);

    let pages = cfg.pages();
    let (cart, detail) = (&pages[0], &pages[1]);
    assert_eq!(cart.name, "购物车");
    assert_eq!(cart.poll_interval_secs, 30);
    assert_eq!(cart.wait_until, "load");
    assert_eq!(cart.failure_threshold, 5);
    assert_eq!(cart.elements.len(), 2);

    let (a, b) = (&cart.elements[0], &cart.elements[1]);
    assert_eq!(a.name, "商品A");
    assert_eq!(a.effective_selector_type(), "xpath");
    assert_eq!(a.identity(), "yunyoo / 购物车 / 商品A");
    assert_eq!(b.name, "商品B");
    assert_eq!(b.selector_type, "css");
    assert_eq!(b.effective_selector_type(), "css");

    assert_eq!(detail.poll_interval_secs, 15);
    assert_eq!(detail.wait_until, "networkidle");
    assert_eq!(detail.elements[0].name, ".price");
}

#[test]
fn test_defaults_applied() {
    let content = r##"
[telegram]
bot_token = "t"
chat_id = "c"

[[merchants]]
name = "m"
[[merchants.pages]]
url = "https://example.com"
[[merchants.pages.elements]]
selector = "#a"
"##;
    let cfg = load_config(&write(content)).unwrap();
    assert_eq!(cfg.poll_interval_secs, 60);
    assert_eq!(cfg.failure_threshold, 3);
    assert_eq!(cfg.state_path, "state.json");
    assert_eq!(cfg.max_concurrent_fetches, 4);
    assert_eq!(cfg.nav_timeout_secs, 30);
    assert_eq!(cfg.wait_until, "domcontentloaded");

    let page = &cfg.pages()[0];
    assert_eq!(page.name, "https://example.com");
    assert_eq!(page.poll_interval_secs, 60);
    assert_eq!(page.failure_threshold, 3);
    let element = &page.elements[0];
    assert_eq!(element.name, "#a");
    assert_eq!(element.selector_type, "auto");
}

#[test]
fn test_cascade_overrides() {
    let content = r##"
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
"##;
    let cfg = load_config(&write(content)).unwrap();
    let pages = cfg.pages();
    let (p1, p2) = (&pages[0], &pages[1]);
    assert_eq!(p1.poll_interval_secs, 25);
    assert_eq!(p1.failure_threshold, 7);
    assert_eq!(p1.nav_timeout_secs, 50);
    assert_eq!(p1.wait_until, "load");
    assert_eq!(p2.poll_interval_secs, 50);
    assert_eq!(p2.failure_threshold, 7);
}

#[test]
fn test_chat_id_accepts_int() {
    let content = format!("[telegram]\nbot_token = \"t\"\nchat_id = 987654321\n{MERCHANT}");
    let cfg = load_config(&write(&content)).unwrap();
    assert_eq!(cfg.telegram.chat_id, "987654321");
}

#[test]
fn test_missing_telegram_fields() {
    for content in [
        format!("[telegram]\nchat_id = \"c\"\n{MERCHANT}"),
        format!("[telegram]\nbot_token = \"t\"\n{MERCHANT}"),
        MERCHANT.to_string(),
    ] {
        assert!(load_config(&write(&content)).is_err(), "应拒绝：{content}");
    }
}

#[test]
fn test_non_positive_interval() {
    for interval in [0, -1] {
        let content = format!(
            "poll_interval_secs = {interval}\n[telegram]\nbot_token = \"t\"\nchat_id = \"c\"\n{MERCHANT}"
        );
        assert!(load_config(&write(&content)).is_err());
    }
}

#[test]
fn test_invalid_selector_type() {
    let content = r##"
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
"##;
    assert!(load_config(&write(content)).is_err());
}

#[test]
fn test_invalid_wait_until_at_page() {
    let content = r##"
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
"##;
    assert!(load_config(&write(content)).is_err());
}

#[test]
fn test_invalid_url() {
    let content = r##"
[telegram]
bot_token = "t"
chat_id = "c"
[[merchants]]
name = "m"
[[merchants.pages]]
url = "ftp://e.com"
[[merchants.pages.elements]]
selector = "#a"
"##;
    assert!(load_config(&write(content)).is_err());
}

#[test]
fn test_empty_selector() {
    let content = r##"
[telegram]
bot_token = "t"
chat_id = "c"
[[merchants]]
name = "m"
[[merchants.pages]]
url = "https://e.com"
[[merchants.pages.elements]]
selector = "   "
"##;
    assert!(load_config(&write(content)).is_err());
}

#[test]
fn test_zero_monitors_now_ok() {
    let content = r##"
[telegram]
bot_token = "t"
chat_id = "c"
"##;
    let cfg = load_config(&write(content)).unwrap();
    assert!(cfg.merchants.is_empty());
    assert!(cfg.pages().is_empty());
    assert!(cfg.watches.is_empty());
    assert_eq!(cfg.element_count(), 0);
}

#[test]
fn test_merchant_without_pages() {
    let content = r##"
[telegram]
bot_token = "t"
chat_id = "c"
[[merchants]]
name = "m"
"##;
    assert!(load_config(&write(content)).is_err());
}

#[test]
fn test_page_without_elements() {
    let content = r##"
[telegram]
bot_token = "t"
chat_id = "c"
[[merchants]]
name = "m"
[[merchants.pages]]
url = "https://e.com"
"##;
    assert!(load_config(&write(content)).is_err());
}

#[test]
fn test_duplicate_merchant_name() {
    let content = r##"
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
"##;
    assert!(load_config(&write(content)).is_err());
}

#[test]
fn test_duplicate_page_identity() {
    let content = r##"
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
"##;
    assert!(load_config(&write(content)).is_err());
}

#[test]
fn test_duplicate_element_identity() {
    let content = r##"
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
"##;
    assert!(load_config(&write(content)).is_err());
}

#[test]
fn test_same_selector_different_nth_not_duplicate() {
    let content = r##"
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
"##;
    let cfg = load_config(&write(content)).unwrap();
    let (e0, e1) = {
        let els = &cfg.pages()[0].elements;
        (&els[0], &els[1])
    };
    assert_eq!(e0.name, ".status");
    assert!(e0.nth.is_none());
    assert_eq!(e1.name, ".status#1");
    assert_eq!(e1.nth, Some(1));
    assert_ne!(e0.identity(), e1.identity());
}

#[test]
fn test_js_field_parsed() {
    let content = r##"
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
"##;
    let cfg = load_config(&write(content)).unwrap();
    let (a, b) = {
        let els = &cfg.pages()[0].elements;
        (&els[0], &els[1])
    };
    assert_eq!(
        a.js.as_deref(),
        Some("el => el.classList.contains('disabled') ? '售罄' : '可订'")
    );
    assert!(b.js.is_none());
    assert_eq!(a.identity(), "m / https://e.com / 带JS");
    assert_eq!(b.identity(), "m / https://e.com / 纯文本");
}

#[test]
fn test_js_non_string_rejected() {
    let content = r##"
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
"##;
    let err = load_config(&write(content)).unwrap_err();
    assert!(err.0.contains("js"), "错误信息应提到 js：{err}");
}

#[test]
fn test_element_url_parsed() {
    let content = r##"
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
"##;
    let cfg = load_config(&write(content)).unwrap();
    let (a, b) = {
        let els = &cfg.pages()[0].elements;
        (&els[0], &els[1])
    };
    assert_eq!(a.url.as_deref(), Some("https://e.com/order"));
    assert!(b.url.is_none());
    assert_eq!(a.identity(), "m / https://e.com/page / 带跳转");
    assert_eq!(b.identity(), "m / https://e.com/page / 用页面URL");
}

#[test]
fn test_element_url_invalid_scheme_rejected() {
    let content = r##"
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
"##;
    let err = load_config(&write(content)).unwrap_err();
    assert!(err.0.contains("http"), "错误信息应提到 http：{err}");
}

#[test]
fn test_element_url_empty_string_rejected() {
    let content = r##"
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
"##;
    let err = load_config(&write(content)).unwrap_err();
    assert!(err.0.contains("url"), "错误信息应提到 url：{err}");
}

#[test]
fn test_missing_file() {
    let dir = tmp_dir();
    assert!(load_config(&dir.join("nope.toml")).is_err());
}

#[test]
fn test_malformed_toml() {
    assert!(load_config(&write("this is = = not toml")).is_err());
}

#[test]
fn test_parse_config_matches_load_config() {
    let from_file = load_config(&write(VALID)).unwrap();
    let raw: toml::Table = toml::from_str(VALID).unwrap();
    let from_raw = parse_config(&raw).unwrap();
    assert_eq!(from_raw, from_file);
}

#[test]
fn test_load_raw_returns_unparsed_dict() {
    let raw = load_raw(&write(VALID)).unwrap();
    assert_eq!(
        raw.get("telegram")
            .and_then(|t| t.get("bot_token"))
            .and_then(|v| v.as_str()),
        Some("123:ABC")
    );
    assert_eq!(
        raw.get("merchants")
            .and_then(|m| m.as_array())
            .and_then(|a| a.first())
            .and_then(|m| m.get("name"))
            .and_then(|v| v.as_str()),
        Some("yunyoo")
    );
    // raw 不含 Config 才有的派生字段，只有原始键。
    assert!(raw.get("pages").is_none());
}

#[test]
fn test_load_raw_missing_file() {
    let dir = tmp_dir();
    let err = load_raw(&dir.join("nope.toml")).unwrap_err();
    assert!(err.0.contains("配置文件不存在"), "{err}");
}

#[test]
fn test_load_raw_malformed() {
    let err = load_raw(&write("this is = = not toml")).unwrap_err();
    assert!(err.0.contains("TOML 解析失败"), "{err}");
}

#[test]
fn test_detect_selector_type() {
    let cases = [
        ("//*[@id=\"x\"]/span", "xpath"),
        ("(//div)[1]", "xpath"),
        ("/html/body/div", "xpath"),
        ("xpath=//span", "xpath"),
        ("#stock-status", "css"),
        (".price > span", "css"),
        ("css=//not-really", "css"),
    ];
    for (selector, expected) in cases {
        assert_eq!(
            detect_selector_type(selector),
            expected,
            "selector: {selector}"
        );
    }
}

#[test]
fn test_element_identity() {
    let el = MonitoredElement {
        merchant_name: "yunyoo".into(),
        page_name: "购物车".into(),
        name: "商品A".into(),
        selector: "#a".into(),
        selector_type: "css".into(),
        nth: None,
        js: None,
        url: None,
    };
    assert_eq!(el.identity(), "yunyoo / 购物车 / 商品A");
}

// ---- 列表监控目标 [[watches]] ----

#[test]
fn test_valid_watch() {
    let content = r##"
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
"##;
    let cfg = load_config(&write(content)).unwrap();
    assert!(cfg.merchants.is_empty());
    assert_eq!(cfg.watches.len(), 1);
    let w = &cfg.watches[0];
    assert_eq!(w.name, "NodeSeek 首页");
    assert_eq!(w.url, "https://www.nodeseek.com/");
    assert_eq!(
        w.link_selector,
        "//*[@id=\"nsk-body-left\"]/ul/li/div/div[1]/a"
    );
    assert_eq!(w.keywords, vec!["hk", "HK"]);
    assert_eq!(w.id_pattern.as_deref(), Some(r"post-(\d+)-"));
    assert_eq!(w.poll_interval_secs, 45);
    assert_eq!(w.nav_timeout_secs, 20);
    assert_eq!(w.wait_until, "load");
    assert_eq!(w.effective_selector_type(), "xpath");
    assert_eq!(w.identity(), "watch / NodeSeek 首页");
}

#[test]
fn test_watch_id_pattern_optional_and_name_falls_back() {
    let content = r##"
[telegram]
bot_token = "t"
chat_id = "c"
[[watches]]
url = "https://e.com/list"
link_selector = "ul li a"
keywords = ["hk"]
"##;
    let cfg = load_config(&write(content)).unwrap();
    let w = &cfg.watches[0];
    assert!(w.id_pattern.is_none());
    assert_eq!(w.name, "https://e.com/list");
    assert_eq!(w.effective_selector_type(), "css");
}

#[test]
fn test_watches_only_no_merchants_ok() {
    let content = r##"
[telegram]
bot_token = "t"
chat_id = "c"
[[watches]]
url = "https://e.com"
link_selector = "a"
keywords = ["hk"]
"##;
    let cfg = load_config(&write(content)).unwrap();
    assert!(cfg.merchants.is_empty());
    assert_eq!(cfg.watches.len(), 1);
}

#[test]
fn test_merchant_only_config_has_empty_watches() {
    let cfg = load_config(&write(VALID)).unwrap();
    assert!(cfg.watches.is_empty());
}

#[test]
fn test_watch_invalid_fields() {
    let cases = [
        // 缺 url
        "[telegram]\nbot_token = \"t\"\nchat_id = \"c\"\n[[watches]]\nlink_selector = \"a\"\nkeywords = [\"hk\"]\n",
        // 缺 link_selector
        "[telegram]\nbot_token = \"t\"\nchat_id = \"c\"\n[[watches]]\nurl = \"https://e.com\"\nkeywords = [\"hk\"]\n",
        // 缺 keywords
        "[telegram]\nbot_token = \"t\"\nchat_id = \"c\"\n[[watches]]\nurl = \"https://e.com\"\nlink_selector = \"a\"\n",
        // keywords 为空列表
        "[telegram]\nbot_token = \"t\"\nchat_id = \"c\"\n[[watches]]\nurl = \"https://e.com\"\nlink_selector = \"a\"\nkeywords = []\n",
        // keywords 含非字符串
        "[telegram]\nbot_token = \"t\"\nchat_id = \"c\"\n[[watches]]\nurl = \"https://e.com\"\nlink_selector = \"a\"\nkeywords = [1]\n",
        // url 非法
        "[telegram]\nbot_token = \"t\"\nchat_id = \"c\"\n[[watches]]\nurl = \"ftp://e.com\"\nlink_selector = \"a\"\nkeywords = [\"hk\"]\n",
    ];
    for content in cases {
        assert!(load_config(&write(content)).is_err(), "应拒绝：{content}");
    }
}

#[test]
fn test_watch_invalid_id_pattern() {
    let content = r##"
[telegram]
bot_token = "t"
chat_id = "c"
[[watches]]
url = "https://e.com"
link_selector = "a"
keywords = ["hk"]
id_pattern = 'post-(\d+'
"##;
    assert!(load_config(&write(content)).is_err());
}

#[test]
fn test_duplicate_watch_name() {
    let content = r##"
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
"##;
    assert!(load_config(&write(content)).is_err());
}

// ---- fingerprint / proxy 解析 ----

#[test]
fn test_global_fingerprint_parses() {
    let content = r##"
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
"##;
    let cfg = load_config(&write(content)).unwrap();
    assert_eq!(cfg.fingerprint.user_agent.as_deref(), Some("ua/1.0"));
    assert_eq!(cfg.fingerprint.locale.as_deref(), Some("zh-CN"));
    assert_eq!(
        cfg.fingerprint.timezone_id.as_deref(),
        Some("Asia/Shanghai")
    );
    assert_eq!(cfg.fingerprint.color_scheme.as_deref(), Some("dark"));
    assert_eq!(
        cfg.fingerprint.viewport,
        Some(hawkeye::config::Viewport {
            width: 1440,
            height: 900
        })
    );
    let page = &cfg.pages()[0];
    assert_eq!(page.fingerprint.user_agent.as_deref(), Some("ua/1.0"));
    assert_eq!(
        page.fingerprint.timezone_id.as_deref(),
        Some("Asia/Shanghai")
    );
    assert!(page.proxy.is_none());
}

#[test]
fn test_merchant_fingerprint_overrides_global() {
    let content = r##"
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
"##;
    let cfg = load_config(&write(content)).unwrap();
    assert_eq!(cfg.fingerprint.locale.as_deref(), Some("en-US"));
    assert_eq!(
        cfg.merchants[0].fingerprint.locale.as_deref(),
        Some("ja-JP")
    );
    assert_eq!(cfg.pages()[0].fingerprint.locale.as_deref(), Some("ja-JP"));
}

#[test]
fn test_proxy_string_shorthand_parsed() {
    let content = r##"
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
"##;
    let cfg = load_config(&write(content)).unwrap();
    let proxy = cfg.proxy.expect("proxy 应被解析");
    assert_eq!(proxy.username.as_deref(), Some("alice"));
    assert_eq!(proxy.password.as_deref(), Some("s3cret"));
    assert_eq!(proxy.server, "http://10.0.0.1:7890");
    assert!(proxy.bypass.is_none());
}

#[test]
fn test_proxy_table_form_parsed() {
    let content = r##"
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
"##;
    let cfg = load_config(&write(content)).unwrap();
    let proxy = cfg.proxy.expect("proxy 应被解析");
    assert_eq!(proxy.server, "socks5://1.2.3.4:1080");
    assert_eq!(proxy.bypass.as_deref(), Some("*.x"));
}

#[test]
fn test_proxy_invalid_scheme_rejected() {
    let content = r##"
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
"##;
    let err = load_config(&write(content)).unwrap_err();
    assert!(err.0.contains("proxy.server"), "{err}");
}

#[test]
fn test_fingerprint_invalid_timezone_rejected() {
    let content = r##"
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
"##;
    let err = load_config(&write(content)).unwrap_err();
    assert!(err.0.contains("timezone_id"), "{err}");
}

#[test]
fn test_fingerprint_invalid_color_scheme_rejected() {
    let content = r##"
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
"##;
    let err = load_config(&write(content)).unwrap_err();
    assert!(err.0.contains("color_scheme"), "{err}");
}

#[test]
fn test_watch_fingerprint_and_proxy() {
    let content = r##"
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
proxy = "socks5://127.0.0.1:1080"
"##;
    let cfg = load_config(&write(content)).unwrap();
    let w = &cfg.watches[0];
    assert_eq!(w.fingerprint.locale.as_deref(), Some("ja-JP"));
    assert_eq!(
        w.proxy.as_ref().map(|p| p.server.as_str()),
        Some("socks5://127.0.0.1:1080")
    );
}
