//! 配置写事务与 raw TOML 变换（对 config.toml 的唯一写入口）。
//!
//! 四个纯变换（新增元素 / 新增列表监控 / 删除元素 / 删除列表监控）都在原始 TOML
//! table 的深拷贝上做最小改动后返回新 table，**绝不序列化已级联展开的 Config**：
//! Config 里 poll_interval 等默认值已逐层落到每个页面 / 元素上，回写会盖满全文、
//! 破坏级联语义。
//!
//! [`write_config`] 是唯一写入口，采用「先验证、后替换」事务：先把新 table
//! 序列化并重新 parse_config 校验，通过后才复制出带时间戳的备份（600、留最近
//! 10 份），再写 .tmp 并原子替换。任一步失败都在触碰原文件之前，原文件字节不变。

use std::path::Path;

use chrono::Local;

use crate::config::{Config, ConfigError, parse_config};

const BACKUP_KEEP: usize = 10;

#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub struct EditError(pub String);

impl From<ConfigError> for EditError {
    fn from(e: ConfigError) -> Self {
        EditError(e.0)
    }
}

// ---- 标识回推（必须与 config.rs 的缺省回退规则逐字一致，否则删除定位不到） ----

fn merchant_name(merchant: &toml::Value) -> String {
    merchant
        .get("name")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string()
}

fn page_name(page: &toml::Value) -> String {
    if let Some(name) = page.get("name").and_then(|v| v.as_str())
        && !name.is_empty()
    {
        return name.to_string();
    }
    page.get("url")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string()
}

fn element_name(element: &toml::Value) -> String {
    if let Some(name) = element.get("name").and_then(|v| v.as_str())
        && !name.is_empty()
    {
        return name.to_string();
    }
    let sel = element
        .get("selector")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    match element.get("nth").and_then(|v| v.as_integer()) {
        None => sel,
        Some(n) => format!("{sel}#{n}"),
    }
}

fn element_identity(merchant: &toml::Value, page: &toml::Value, element: &toml::Value) -> String {
    format!(
        "{} / {} / {}",
        merchant_name(merchant),
        page_name(page),
        element_name(element)
    )
}

fn watch_identity(watch: &toml::Value) -> String {
    let name = match watch.get("name").and_then(|v| v.as_str()) {
        Some(n) if !n.is_empty() => n.to_string(),
        _ => watch
            .get("url")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
    };
    format!("watch / {name}")
}

// ---- 四个纯变换（各自在深拷贝上改，互不耦合） ----

/// 新增一个元素监控，按页面 URL 精确判重合并。
///
/// URL 命中已有页面 → 并入该页面的 elements；未命中 → 在以 URL host 命名的商家下
/// 新建页面承载它（同 host 已存在则复用）。仅在显式给了 name / nth / js /
/// element_url 时才写这些键，否则留空让 config.rs 的缺省回退接管。
pub fn add_element(
    raw: &toml::Table,
    url: &str,
    selector: &str,
    name: Option<&str>,
    nth: Option<i64>,
    js: Option<&str>,
    element_url: Option<&str>,
) -> toml::Table {
    let mut new = raw.clone();
    let mut element = toml::Table::new();
    element.insert("selector".into(), toml::Value::String(selector.to_string()));
    if let Some(name) = name {
        element.insert("name".into(), toml::Value::String(name.to_string()));
    }
    if let Some(nth) = nth {
        element.insert("nth".into(), toml::Value::Integer(nth));
    }
    if let Some(js) = js {
        element.insert("js".into(), toml::Value::String(js.to_string()));
    }
    if let Some(element_url) = element_url {
        element.insert("url".into(), toml::Value::String(element_url.to_string()));
    }
    let element = toml::Value::Table(element);

    // URL 命中已有页面 → 并入。
    if let Some(merchants) = new.get_mut("merchants").and_then(|v| v.as_array_mut()) {
        for merchant in merchants.iter_mut() {
            if let Some(pages) = merchant.get_mut("pages").and_then(|v| v.as_array_mut()) {
                for page in pages.iter_mut() {
                    if page.get("url").and_then(|v| v.as_str()) == Some(url) {
                        let elements = page
                            .as_table_mut()
                            .expect("page is table")
                            .entry("elements")
                            .or_insert_with(|| toml::Value::Array(Vec::new()));
                        elements
                            .as_array_mut()
                            .expect("elements is array")
                            .push(element);
                        return new;
                    }
                }
            }
        }
    }

    // 未命中 → 以 URL host 命名的商家下新建页面。
    let host_name = url::Url::parse(url)
        .ok()
        .and_then(|u| u.host_str().map(|h| h.to_string()))
        .or_else(|| url::Url::parse(url).ok().map(|u| u.authority().to_string()))
        .unwrap_or_else(|| url.to_string());

    let merchants = new
        .entry("merchants")
        .or_insert_with(|| toml::Value::Array(Vec::new()));
    let merchants_arr = merchants.as_array_mut().expect("merchants is array");

    let mut target_idx: Option<usize> = None;
    for (i, m) in merchants_arr.iter().enumerate() {
        if m.get("name").and_then(|v| v.as_str()) == Some(host_name.as_str()) {
            target_idx = Some(i);
            break;
        }
    }
    let target = match target_idx {
        Some(i) => merchants_arr[i].as_table_mut().expect("merchant is table"),
        None => {
            let mut m = toml::Table::new();
            m.insert("name".into(), toml::Value::String(host_name));
            m.insert("pages".into(), toml::Value::Array(Vec::new()));
            merchants_arr.push(toml::Value::Table(m));
            let last = merchants_arr.len() - 1;
            merchants_arr[last]
                .as_table_mut()
                .expect("merchant is table")
        }
    };
    let pages = target
        .entry("pages")
        .or_insert_with(|| toml::Value::Array(Vec::new()))
        .as_array_mut()
        .expect("pages is array");
    let mut page = toml::Table::new();
    page.insert("url".into(), toml::Value::String(url.to_string()));
    page.insert("elements".into(), toml::Value::Array(vec![element]));
    pages.push(toml::Value::Table(page));
    new
}

/// 新增一个论坛关键词监控（watch），追加到顶层 watches。
///
/// URL 已存在则挡下（EditError），避免上层撞见解析层的「标识重复」错误。
pub fn add_watch(
    raw: &toml::Table,
    url: &str,
    link_selector: &str,
    keywords: &[String],
    name: Option<&str>,
    id_pattern: Option<&str>,
) -> Result<toml::Table, EditError> {
    let mut new = raw.clone();
    if let Some(watches) = new.get("watches").and_then(|v| v.as_array()) {
        for watch in watches {
            if watch.get("url").and_then(|v| v.as_str()) == Some(url) {
                return Err(EditError("该列表页已在监控中，请先删除再新建".to_string()));
            }
        }
    }
    let mut entry = toml::Table::new();
    entry.insert("url".into(), toml::Value::String(url.to_string()));
    entry.insert(
        "link_selector".into(),
        toml::Value::String(link_selector.to_string()),
    );
    entry.insert(
        "keywords".into(),
        toml::Value::Array(
            keywords
                .iter()
                .map(|k| toml::Value::String(k.clone()))
                .collect(),
        ),
    );
    if let Some(name) = name {
        entry.insert("name".into(), toml::Value::String(name.to_string()));
    }
    if let Some(id_pattern) = id_pattern {
        entry.insert(
            "id_pattern".into(),
            toml::Value::String(id_pattern.to_string()),
        );
    }
    let watches = new
        .entry("watches")
        .or_insert_with(|| toml::Value::Array(Vec::new()));
    watches
        .as_array_mut()
        .expect("watches is array")
        .push(toml::Value::Table(entry));
    Ok(new)
}

/// 按「商家 / 页面 / 元素」标识删除元素；空页面与随之变空的商家一并移除。
pub fn remove_element(raw: &toml::Table, identity: &str) -> Result<toml::Table, EditError> {
    let mut new = raw.clone();
    // 先在不可变视图上找到匹配下标，再回到可变引用做删除，避免借用冲突。
    let mut found: Option<(usize, usize, usize)> = None;
    {
        let merchants_view = new.get("merchants").and_then(|v| v.as_array());
        'outer: for (mi, merchant) in merchants_view.into_iter().flatten().enumerate() {
            let Some(pages) = merchant.get("pages").and_then(|v| v.as_array()) else {
                continue;
            };
            for (pi, page) in pages.iter().enumerate() {
                let Some(elements) = page.get("elements").and_then(|v| v.as_array()) else {
                    continue;
                };
                for (ei, element) in elements.iter().enumerate() {
                    if element_identity(merchant, page, element) == identity {
                        found = Some((mi, pi, ei));
                        break 'outer;
                    }
                }
            }
        }
    }
    let Some((mi, pi, ei)) = found else {
        return Err(EditError(format!("未找到要删除的监控元素：{identity}")));
    };
    let merchants = new
        .get_mut("merchants")
        .and_then(|v| v.as_array_mut())
        .expect("merchants is array");
    let pages = merchants[mi]
        .get_mut("pages")
        .and_then(|v| v.as_array_mut())
        .expect("pages is array");
    let elements = pages[pi]
        .get_mut("elements")
        .and_then(|v| v.as_array_mut())
        .expect("elements is array");
    elements.remove(ei);
    if elements.is_empty() {
        pages.remove(pi);
        if pages.is_empty() {
            merchants.remove(mi);
        }
    }
    Ok(new)
}

/// 从顶层 watches 删除对应目标，其余顺序不动。
pub fn remove_watch(raw: &toml::Table, identity: &str) -> Result<toml::Table, EditError> {
    let mut new = raw.clone();
    if let Some(watches) = new.get_mut("watches").and_then(|v| v.as_array_mut()) {
        for wi in 0..watches.len() {
            if watch_identity(&watches[wi]) == identity {
                watches.remove(wi);
                return Ok(new);
            }
        }
    }
    Err(EditError(format!("未找到要删除的监控目标：{identity}")))
}

// ---- 写事务：先验证、后替换 ----

pub fn tighten_permissions(path: &Path) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if let Err(e) = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600)) {
            tracing::warn!("chmod 600 {} 失败：{e}", path.display());
        }
    }
    #[cfg(not(unix))]
    {
        // 非 POSIX 平台收紧权限属于尽力而为，此处仅提示。
        tracing::warn!(
            "非 POSIX 平台，请自行确认 {} 不在共享目录里",
            path.display()
        );
    }
}

fn unique_backup_path(config_path: &Path, ts: &str) -> std::path::PathBuf {
    let name = config_path
        .file_name()
        .unwrap_or_default()
        .to_string_lossy();
    let base = config_path.with_file_name(format!("{name}.bak.{ts}"));
    if !base.exists() {
        return base;
    }
    let mut i = 1;
    loop {
        let candidate = config_path.with_file_name(format!("{name}.bak.{ts}-{i}"));
        if !candidate.exists() {
            return candidate;
        }
        i += 1;
    }
}

fn prune_backups(config_path: &Path) {
    let Some(parent) = config_path.parent() else {
        return;
    };
    let prefix = format!(
        "{}.bak.",
        config_path
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
    );
    let mut backups: Vec<_> = std::fs::read_dir(parent)
        .into_iter()
        .flatten()
        .flatten()
        .filter(|e| {
            e.file_name()
                .to_str()
                .map(|n| n.starts_with(&prefix))
                .unwrap_or(false)
        })
        .filter_map(|e| {
            let meta = e.metadata().ok()?;
            let mtime = meta
                .modified()
                .ok()?
                .duration_since(std::time::UNIX_EPOCH)
                .ok()?
                .as_secs();
            Some((mtime, e.path()))
        })
        .collect();
    backups.sort_by_key(|b| std::cmp::Reverse(b.0));
    for (_, old) in backups.into_iter().skip(BACKUP_KEEP) {
        if let Err(e) = std::fs::remove_file(&old) {
            tracing::warn!("清理旧备份 {} 失败：{e}", old.display());
        }
    }
}

fn make_backup(config_path: &Path) -> std::io::Result<()> {
    let ts = Local::now().format("%Y%m%d-%H%M%S");
    let backup = unique_backup_path(config_path, &ts.to_string());
    // 先以 600 权限创建备份再写入，避免权限窗口。
    #[cfg(unix)]
    {
        use std::io::Write;
        use std::os::unix::fs::OpenOptionsExt;
        let data = std::fs::read(config_path)?;
        let mut f = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&backup)?;
        f.write_all(&data)?;
    }
    #[cfg(not(unix))]
    {
        std::fs::copy(config_path, &backup)?;
    }
    tighten_permissions(&backup);
    prune_backups(config_path);
    Ok(())
}

/// 把变换后的 raw table 以「先验证、后替换」事务写回，返回新 Config（唯一写入口）。
pub fn write_config(path: &Path, new_raw: &toml::Table) -> Result<Config, EditError> {
    let text =
        toml::to_string_pretty(new_raw).map_err(|e| EditError(format!("配置序列化失败：{e}")))?;

    // ① 序列化结果必须既可解析又语义合法；失败即抛，此时原文件尚未被触碰。
    let reparsed: toml::Table = toml::from_str(&text).map_err(|e| {
        EditError(format!(
            "改写后的配置未通过校验，已放弃写入（原文件未改动）：{e}"
        ))
    })?;
    let config = parse_config(&reparsed).map_err(|e| {
        EditError(format!(
            "改写后的配置未通过校验，已放弃写入（原文件未改动）：{e}"
        ))
    })?;

    // ② 备份原文件（600、留最近 10 份）。
    if path.exists()
        && let Err(e) = make_backup(path)
    {
        return Err(EditError(format!("备份原配置失败：{e}")));
    }

    // ③ 写 .tmp（600）→ 原子替换。必须先删除残留 tmp，否则 create_new 会永远失败。
    let tmp = path.with_file_name(format!(
        "{}.tmp",
        path.file_name().unwrap_or_default().to_string_lossy()
    ));
    let _ = std::fs::remove_file(&tmp);
    #[cfg(unix)]
    {
        use std::io::Write;
        use std::os::unix::fs::OpenOptionsExt;
        let mut f = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&tmp)
            .map_err(|e| EditError(format!("写临时文件失败：{e}")))?;
        f.write_all(text.as_bytes())
            .map_err(|e| EditError(format!("写临时文件失败：{e}")))?;
    }
    #[cfg(not(unix))]
    {
        std::fs::write(&tmp, &text).map_err(|e| EditError(format!("写临时文件失败：{e}")))?;
    }
    if let Err(e) = std::fs::rename(&tmp, path) {
        return Err(EditError(format!("原子替换配置失败：{e}")));
    }
    // ④ 最终路径权限收紧放在替换之后。
    tighten_permissions(path);
    Ok(config)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn empty_raw() -> toml::Table {
        let mut t = toml::Table::new();
        t.insert(
            "telegram".into(),
            toml::Value::Table({
                let mut tg = toml::Table::new();
                tg.insert("bot_token".into(), toml::Value::String("T".into()));
                tg.insert("chat_id".into(), toml::Value::String("1".into()));
                tg
            }),
        );
        t
    }

    #[test]
    fn test_add_element_new_merchant() {
        let raw = empty_raw();
        let new = add_element(&raw, "https://a.com/p", "#x", None, None, None, None);
        let merchants = new.get("merchants").unwrap().as_array().unwrap();
        assert_eq!(merchants[0].get("name").unwrap().as_str(), Some("a.com"));
        let pages = merchants[0].get("pages").unwrap().as_array().unwrap();
        assert_eq!(
            pages[0].get("url").unwrap().as_str(),
            Some("https://a.com/p")
        );
    }

    #[test]
    fn test_add_element_merge_existing_page() {
        let raw = empty_raw();
        let raw = add_element(&raw, "https://a.com/p", "#x", None, None, None, None);
        let new = add_element(
            &raw,
            "https://a.com/p",
            "#y",
            Some("第二个"),
            None,
            None,
            None,
        );
        let merchants = new.get("merchants").unwrap().as_array().unwrap();
        assert_eq!(merchants.len(), 1);
        let elements = merchants[0].get("pages").unwrap().as_array().unwrap()[0]
            .get("elements")
            .unwrap()
            .as_array()
            .unwrap();
        assert_eq!(elements.len(), 2);
        assert_eq!(elements[1].get("name").unwrap().as_str(), Some("第二个"));
    }

    #[test]
    fn test_add_watch_rejects_duplicate_url() {
        let raw = empty_raw();
        let raw = add_watch(
            &raw,
            "https://f.com",
            "a.title",
            &["kw".to_string()],
            None,
            None,
        )
        .unwrap();
        let err = add_watch(
            &raw,
            "https://f.com",
            "a.title",
            &["kw".to_string()],
            None,
            None,
        );
        assert!(err.is_err());
    }

    #[test]
    fn test_remove_element_cascade() {
        let raw = empty_raw();
        let raw = add_element(&raw, "https://a.com/p", "#x", None, None, None, None);
        let new = remove_element(&raw, "a.com / https://a.com/p / #x").unwrap();
        // 唯一元素删除后页面与商家一并消失。
        assert!(new.get("merchants").unwrap().as_array().unwrap().is_empty());
    }

    #[test]
    fn test_remove_element_not_found() {
        let raw = empty_raw();
        assert!(remove_element(&raw, "不存在").is_err());
    }

    #[test]
    fn test_write_config_transaction() {
        let dir = std::env::temp_dir().join("hawkeye_cfgedit_test");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("config.toml");
        let raw = empty_raw();
        let raw = add_element(&raw, "https://a.com/p", "#x", None, None, None, None);
        let config = write_config(&path, &raw).unwrap();
        assert_eq!(config.merchants.len(), 1);
        // 再写一次验证备份机制。
        let raw2 = add_element(&raw, "https://b.com/q", "#y", None, None, None, None);
        write_config(&path, &raw2).unwrap();
        let text = std::fs::read_to_string(&path).unwrap();
        assert!(text.contains("b.com"));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
