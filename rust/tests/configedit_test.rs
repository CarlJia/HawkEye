//! configedit 模块的移植测试（对应 Python tests/test_configedit.py）。

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

use hawkeye::configedit::{add_element, add_watch, remove_element, remove_watch, write_config, EditError};

static COUNTER: AtomicU64 = AtomicU64::new(0);

fn tmp_dir() -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "hawkeye_ce_test_{}_{}",
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
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o600));
    }
    p
}

/// 两个商家、各一页各一元素：够验证「只动目标、旁边完全不受影响」。
const BASE: &str = r##"
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
"##;

const ONLY_TELEGRAM: &str = r##"
[telegram]
bot_token = "t"
chat_id = "c"
"##;

fn base_raw() -> toml::Table {
    toml::from_str(BASE).unwrap()
}

fn merchants(new: &toml::Table) -> &Vec<toml::Value> {
    new.get("merchants").and_then(|v| v.as_array()).unwrap()
}

fn elements_of(new: &toml::Table, mi: usize) -> &Vec<toml::Value> {
    merchants(new)[mi]
        .get("pages")
        .and_then(|v| v.as_array())
        .unwrap()[0]
        .get("elements")
        .and_then(|v| v.as_array())
        .unwrap()
}

// ---- add_element ----

#[test]
fn test_add_element_merges_existing_url() {
    let raw = base_raw();
    let new = add_element(&raw, "https://yunyoo.cc/cart", "#b", None, None, None, None);

    assert_eq!(merchants(&new).len(), 2);
    assert_eq!(merchants(&new)[0].get("pages").and_then(|v| v.as_array()).unwrap().len(), 1);
    let selectors: Vec<&str> = elements_of(&new, 0)
        .iter()
        .map(|e| e.get("selector").and_then(|v| v.as_str()).unwrap())
        .collect();
    assert_eq!(selectors, vec!["#a", "#b"]);
    // 原 table 未被就地修改（纯变换）
    assert_eq!(elements_of(&raw, 0).len(), 1);
}

#[test]
fn test_add_element_creates_host_merchant_for_new_url() {
    let new = add_element(&base_raw(), "https://shop.example.com/x?a=1", "#c", None, None, None, None);

    let names: Vec<&str> = merchants(&new)
        .iter()
        .map(|m| m.get("name").and_then(|v| v.as_str()).unwrap())
        .collect();
    assert_eq!(names, vec!["yunyoo", "other", "shop.example.com"]);
    let page = &merchants(&new)[2].get("pages").and_then(|v| v.as_array()).unwrap()[0];
    assert_eq!(page.get("url").and_then(|v| v.as_str()), Some("https://shop.example.com/x?a=1"));
    // 页面不写 name，交给缺省回退
    assert!(page.get("name").is_none());
    assert_eq!(
        page.get("elements").and_then(|v| v.as_array()).unwrap(),
        &vec![toml::Value::Table({
            let mut t = toml::Table::new();
            t.insert("selector".into(), toml::Value::String("#c".into()));
            t
        })]
    );
}

#[test]
fn test_add_element_reuses_host_merchant() {
    let new = add_element(&base_raw(), "https://shop.example.com/x", "#c", None, None, None, None);
    let new = add_element(&new, "https://shop.example.com/y", "#d", None, None, None, None);

    let names: Vec<&str> = merchants(&new)
        .iter()
        .map(|m| m.get("name").and_then(|v| v.as_str()).unwrap())
        .collect();
    assert_eq!(names, vec!["yunyoo", "other", "shop.example.com"]);
    assert_eq!(merchants(&new)[2].get("pages").and_then(|v| v.as_array()).unwrap().len(), 2);
}

#[test]
fn test_add_element_omits_name_and_nth() {
    let dir = tmp_dir();
    let p = write(BASE);
    let new = add_element(&base_raw(), "https://yunyoo.cc/cart", "#b", None, None, None, None);
    let element = &elements_of(&new, 0)[1];
    assert!(element.get("name").is_none());
    assert!(element.get("nth").is_none());

    let cfg = write_config(&p, &new).unwrap();
    let identities: Vec<String> = cfg.pages()[0].elements.iter().map(|e| e.identity()).collect();
    assert_eq!(identities, vec!["yunyoo / 购物车 / 商品A", "yunyoo / 购物车 / #b"]);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn test_add_element_writes_name_and_nth_when_given() {
    let new = add_element(
        &base_raw(),
        "https://yunyoo.cc/cart",
        ".s",
        Some("库存"),
        Some(1),
        None,
        None,
    );
    let element = &elements_of(&new, 0)[1];
    assert_eq!(element.get("selector").and_then(|v| v.as_str()), Some(".s"));
    assert_eq!(element.get("name").and_then(|v| v.as_str()), Some("库存"));
    assert_eq!(element.get("nth").and_then(|v| v.as_integer()), Some(1));
}

#[test]
fn test_add_element_writes_element_url_when_given() {
    let new = add_element(
        &base_raw(),
        "https://yunyoo.cc/cart",
        ".s",
        None,
        None,
        None,
        Some("https://yunyoo.cc/order/123"),
    );
    let element = &elements_of(&new, 0)[1];
    assert_eq!(element.get("selector").and_then(|v| v.as_str()), Some(".s"));
    assert_eq!(
        element.get("url").and_then(|v| v.as_str()),
        Some("https://yunyoo.cc/order/123")
    );
}

#[test]
fn test_add_element_on_config_without_merchants() {
    let p = write(ONLY_TELEGRAM);
    let new = add_element(&toml::from_str(ONLY_TELEGRAM).unwrap(), "https://e.com/p", "#a", None, None, None, None);
    let cfg = write_config(&p, &new).unwrap();
    assert_eq!(cfg.element_count(), 1);
    assert_eq!(cfg.merchants[0].name, "e.com");
    let _ = std::fs::remove_dir_all(p.parent().unwrap());
}

// ---- add_watch ----

#[test]
fn test_add_watch_appends_without_name() {
    let keywords = vec!["hk".to_string(), "HK".to_string()];
    let new = add_watch(&base_raw(), "https://ns.com/", "ul li a", &keywords, None, None).unwrap();
    let watches = new.get("watches").and_then(|v| v.as_array()).unwrap();
    assert_eq!(watches.len(), 1);
    assert_eq!(watches[0].get("url").and_then(|v| v.as_str()), Some("https://ns.com/"));
    assert_eq!(watches[0].get("link_selector").and_then(|v| v.as_str()), Some("ul li a"));
    assert!(watches[0].get("name").is_none());

    let p = write(BASE);
    let cfg = write_config(&p, &new).unwrap();
    assert_eq!(cfg.watches[0].identity(), "watch / https://ns.com/");
    assert!(cfg.watches[0].id_pattern.is_none());
    let _ = std::fs::remove_dir_all(p.parent().unwrap());
}

#[test]
fn test_add_watch_writes_name_when_given() {
    let keywords = vec!["hk".to_string()];
    let new = add_watch(&base_raw(), "https://ns.com/", "a", &keywords, Some("NS 交易区"), None).unwrap();
    assert_eq!(
        new.get("watches").and_then(|v| v.as_array()).unwrap()[0]
            .get("name")
            .and_then(|v| v.as_str()),
        Some("NS 交易区")
    );
}

#[test]
fn test_add_watch_writes_id_pattern_when_given() {
    let keywords = vec!["hk".to_string()];
    let new = add_watch(&base_raw(), "https://ns.com/", "a", &keywords, None, Some(r"p-(\d+)")).unwrap();
    assert_eq!(
        new.get("watches").and_then(|v| v.as_array()).unwrap()[0]
            .get("id_pattern")
            .and_then(|v| v.as_str()),
        Some(r"p-(\d+)")
    );
}

#[test]
fn test_add_watch_duplicate_url_raises() {
    let keywords = vec!["hk".to_string()];
    let new = add_watch(&base_raw(), "https://ns.com/", "a", &keywords, None, None).unwrap();
    let other = vec!["other".to_string()];
    let err = add_watch(&new, "https://ns.com/", "a", &other, None, None).unwrap_err();
    assert!(err.0.contains("该列表页已在监控中"), "{err}");
}

#[test]
fn test_add_watch_on_config_without_watches() {
    let p = write(ONLY_TELEGRAM);
    let keywords = vec!["hk".to_string()];
    let new = add_watch(&toml::from_str(ONLY_TELEGRAM).unwrap(), "https://ns.com/", "a", &keywords, None, None).unwrap();
    let cfg = write_config(&p, &new).unwrap();
    assert_eq!(cfg.watches.len(), 1);
    let _ = std::fs::remove_dir_all(p.parent().unwrap());
}

// ---- remove_element / remove_watch ----

#[test]
fn test_remove_element_prunes_empty_page_and_merchant() {
    let new = remove_element(&base_raw(), "yunyoo / 购物车 / 商品A").unwrap();

    let names: Vec<&str> = merchants(&new)
        .iter()
        .map(|m| m.get("name").and_then(|v| v.as_str()).unwrap())
        .collect();
    assert_eq!(names, vec!["other"]);
    assert_eq!(elements_of(&new, 0).len(), 1);
}

#[test]
fn test_remove_element_keeps_siblings() {
    let raw = add_element(&base_raw(), "https://yunyoo.cc/cart", "#b", None, None, None, None);
    let new = remove_element(&raw, "yunyoo / 购物车 / #b").unwrap();

    assert_eq!(merchants(&new).len(), 2);
    let elements = elements_of(&new, 0);
    assert_eq!(elements.len(), 1);
    assert_eq!(elements[0].get("name").and_then(|v| v.as_str()), Some("商品A"));
}

#[test]
fn test_remove_element_identity_with_nth_fallback() {
    let raw = add_element(&base_raw(), "https://yunyoo.cc/cart", ".s", None, Some(1), None, None);
    let new = remove_element(&raw, "yunyoo / 购物车 / .s#1").unwrap();
    assert_eq!(elements_of(&new, 0).len(), 1);
}

#[test]
fn test_remove_element_missing_raises() {
    let err = remove_element(&base_raw(), "yunyoo / 购物车 / 不存在").unwrap_err();
    assert!(err.0.contains("未找到要删除的监控元素"), "{err}");
}

#[test]
fn test_remove_watch_only_removes_target() {
    let kx = vec!["x".to_string()];
    let ky = vec!["y".to_string()];
    let raw = add_watch(&base_raw(), "https://a.com/", "a", &kx, None, None).unwrap();
    let raw = add_watch(&raw, "https://b.com/", "a", &ky, None, None).unwrap();
    let new = remove_watch(&raw, "watch / https://a.com/").unwrap();

    let urls: Vec<&str> = new
        .get("watches")
        .and_then(|v| v.as_array())
        .unwrap()
        .iter()
        .map(|w| w.get("url").and_then(|v| v.as_str()).unwrap())
        .collect();
    assert_eq!(urls, vec!["https://b.com/"]);
}

#[test]
fn test_remove_watch_missing_raises() {
    let err = remove_watch(&base_raw(), "watch / https://nope.com/").unwrap_err();
    assert!(err.0.contains("未找到要删除的监控目标"), "{err}");
}

// ---- write_config ----

#[test]
fn test_write_config_roundtrip_keeps_config_equal() {
    use hawkeye::config::load_config;
    let p = write(BASE);
    let before = load_config(&p).unwrap();
    let raw = hawkeye::config::load_raw(&p).unwrap();
    let after = write_config(&p, &raw).unwrap();
    assert_eq!(before, after);
    let _ = std::fs::remove_dir_all(p.parent().unwrap());
}

#[test]
fn test_write_config_adds_no_cascaded_keys() {
    // 写回的是 raw table：级联展开的默认值绝不落盘。
    let p = write(BASE);
    let raw = hawkeye::config::load_raw(&p).unwrap();
    let new = add_element(&raw, "https://yunyoo.cc/cart", "#b", None, None, None, None);
    write_config(&p, &new).unwrap();
    let text = std::fs::read_to_string(&p).unwrap();
    assert!(!text.contains("poll_interval_secs = 60"));
    let _ = std::fs::remove_dir_all(p.parent().unwrap());
}

#[test]
fn test_write_config_backup_created() {
    let p = write(BASE);
    let raw = hawkeye::config::load_raw(&p).unwrap();
    let new = add_element(&raw, "https://e.com/x", "#z", None, None, None, None);
    write_config(&p, &new).unwrap();
    // 备份文件已生成且含原内容。
    let prefix = format!("{}.", p.file_name().unwrap().to_string_lossy());
    let mut found = false;
    if let Some(dir) = p.parent() {
        for e in std::fs::read_dir(dir).unwrap().flatten() {
            let name = e.file_name().to_string_lossy().to_string();
            if name.starts_with(&format!("{prefix}bak.")) {
                let content = std::fs::read_to_string(e.path()).unwrap();
                assert!(content.contains("商品A"));
                found = true;
            }
        }
    }
    assert!(found);
    let _ = std::fs::remove_dir_all(p.parent().unwrap());
}

#[test]
fn test_write_config_validation_failure_leaves_file_untouched() {
    // 删掉 [telegram] 段 → 校验失败 → 原文件字节不变。
    let p = write(BASE);
    let before = std::fs::read(&p).unwrap();
    let mut broken = hawkeye::config::load_raw(&p).unwrap();
    broken.remove("telegram");
    assert!(write_config(&p, &broken).is_err());
    let after = std::fs::read(&p).unwrap();
    assert_eq!(before, after);
    let _ = std::fs::remove_dir_all(p.parent().unwrap());
}

#[test]
fn test_write_config_creates_file_when_absent() {
    let dir = tmp_dir();
    let p = dir.join("config.toml");
    let raw: toml::Table = toml::from_str(ONLY_TELEGRAM).unwrap();
    let new = add_element(&raw, "https://e.com/p", "#a", None, None, None, None);
    let cfg = write_config(&p, &new).unwrap();
    assert_eq!(cfg.element_count(), 1);
    assert!(p.exists());
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn test_write_config_keeps_permissions_unix() {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let p = write(BASE);
        let raw = hawkeye::config::load_raw(&p).unwrap();
        let new = add_element(&raw, "https://e.com/x", "#z", None, None, None, None);
        write_config(&p, &new).unwrap();
        let mode = std::fs::metadata(&p).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600);
        let _ = std::fs::remove_dir_all(p.parent().unwrap());
    }
}

#[allow(unused)]
fn _silence(_: EditError) {}
