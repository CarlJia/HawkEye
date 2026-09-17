//! 配置加载与校验。
//!
//! 从 TOML 读取全局默认值、Telegram 凭据与「商家 → 页面 → 监控元素」三层
//! 结构，做严格校验后返回不可变的 [`Config`]。上层默认值（全局 → 商家 →
//! 页面）向下级联；同一页面的多个元素共享一次页面加载。任何非法配置都
//! 返回 [`ConfigError`]，由启动流程捕获并给出可读错误（fail-closed）。

use std::collections::HashSet;
use std::fmt;
use std::path::Path;

use regex::Regex;
use serde::{Deserialize, Serialize};

// ---- 允许的取值集合 ----
pub const VALID_SELECTOR_TYPES: &[&str] = &["auto", "css", "xpath"];
pub const VALID_WAIT_UNTIL: &[&str] = &["load", "domcontentloaded", "networkidle", "commit"];
pub const VALID_COLOR_SCHEME: &[&str] = &["light", "dark", "no-preference", "null"];

// ---- 全局默认值 ----
const DEFAULT_POLL_INTERVAL: i64 = 60;
const DEFAULT_FAILURE_THRESHOLD: i64 = 3;
const DEFAULT_STATE_PATH: &str = "state.json";
const DEFAULT_MAX_CONCURRENT: i64 = 4;
const DEFAULT_NAV_TIMEOUT: i64 = 30;
const DEFAULT_WAIT_UNTIL: &str = "domcontentloaded";
const DEFAULT_SELECTOR_TYPE: &str = "auto";

#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub struct ConfigError(pub String);

impl ConfigError {
    fn new(msg: impl Into<String>) -> Self {
        Self(msg.into())
    }
}

fn err(ctx: &str, msg: impl fmt::Display) -> ConfigError {
    ConfigError::new(format!("{ctx}{msg}"))
}

/// 根据选择器写法推断类型：以 // ( / . 或 xpath= 开头视为 XPath，否则 CSS。
pub fn detect_selector_type(selector: &str) -> &'static str {
    let s = selector.trim();
    if s.starts_with("css=") {
        return "css";
    }
    if s.starts_with("//")
        || s.starts_with('(')
        || s.starts_with('/')
        || s.starts_with("./")
        || s.starts_with("..")
        || s.starts_with("xpath=")
    {
        return "xpath";
    }
    "css"
}

/// 谓词版 URL 校验：http(s) 且有 host。
pub fn is_http_url(url: &str) -> bool {
    match url::Url::parse(url) {
        Ok(u) => u.scheme() == "http" || u.scheme() == "https",
        Err(_) => false,
    }
}

fn validate_url(url: &str, ctx: &str) -> Result<(), ConfigError> {
    if is_http_url(url) {
        Ok(())
    } else {
        Err(err(ctx, format!("的 url 不是合法的 http(s) 地址：{url}")))
    }
}

// ---- 数据结构 ----

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
pub struct TelegramConfig {
    pub bot_token: String,
    pub chat_id: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
pub struct ProxyConfig {
    pub server: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub username: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub password: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub bypass: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct Viewport {
    pub width: i64,
    pub height: i64,
}

/// 浏览器环境维度：UA / locale / timezone / color_scheme / viewport。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
pub struct Fingerprint {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub user_agent: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub locale: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub timezone_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub color_scheme: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub viewport: Option<Viewport>,
}

/// 页面内的单个监控元素（已完成默认值回填）。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MonitoredElement {
    pub merchant_name: String,
    pub page_name: String,
    pub name: String,
    pub selector: String,
    pub selector_type: String,
    #[serde(default)]
    pub nth: Option<i64>,
    /// 非空时取代 inner_text：对元素执行 JS 表达式，字符串返回值作为状态值。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub js: Option<String>,
    /// 可选跳转 URL：缺省时调度层沿用所属 page.url。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub url: Option<String>,
}

impl MonitoredElement {
    /// 状态文件中的稳定标识：商家 / 页面 / 元素。
    pub fn identity(&self) -> String {
        format!(
            "{} / {} / {}",
            self.merchant_name, self.page_name, self.name
        )
    }

    /// 把 auto 归一化为具体的 css / xpath。
    pub fn effective_selector_type(&self) -> &'static str {
        if self.selector_type == "auto" {
            detect_selector_type(&self.selector)
        } else {
            match self.selector_type.as_str() {
                "xpath" => "xpath",
                _ => "css",
            }
        }
    }
}

/// 单个商品页面：一次导航即提取其下全部元素。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Page {
    pub merchant_name: String,
    pub name: String,
    pub url: String,
    pub poll_interval_secs: i64,
    pub wait_until: String,
    pub nav_timeout_secs: i64,
    pub failure_threshold: i64,
    pub elements: Vec<MonitoredElement>,
    #[serde(default)]
    pub fingerprint: Fingerprint,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub proxy: Option<ProxyConfig>,
}

impl Page {
    /// 页面级稳定标识：商家 / 页面。
    pub fn identity(&self) -> String {
        format!("{} / {}", self.merchant_name, self.name)
    }
}

/// 商家：一组商品页面的集合。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Merchant {
    pub name: String,
    pub pages: Vec<Page>,
    #[serde(default)]
    pub fingerprint: Fingerprint,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub proxy: Option<ProxyConfig>,
}

/// 列表新条目监控目标（已完成默认值回填）。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WatchTarget {
    pub name: String,
    pub url: String,
    pub link_selector: String,
    pub selector_type: String,
    pub keywords: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub id_pattern: Option<String>,
    pub poll_interval_secs: i64,
    pub wait_until: String,
    pub nav_timeout_secs: i64,
    pub failure_threshold: i64,
    #[serde(default)]
    pub fingerprint: Fingerprint,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub proxy: Option<ProxyConfig>,
}

impl WatchTarget {
    /// 状态文件中的稳定标识：watch / 名称。
    pub fn identity(&self) -> String {
        format!("watch / {}", self.name)
    }

    pub fn effective_selector_type(&self) -> &'static str {
        if self.selector_type == "auto" {
            detect_selector_type(&self.link_selector)
        } else {
            match self.selector_type.as_str() {
                "xpath" => "xpath",
                _ => "css",
            }
        }
    }
}

/// 完整配置。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Config {
    pub telegram: TelegramConfig,
    pub merchants: Vec<Merchant>,
    pub poll_interval_secs: i64,
    pub failure_threshold: i64,
    pub state_path: String,
    pub max_concurrent_fetches: i64,
    pub nav_timeout_secs: i64,
    pub wait_until: String,
    #[serde(default)]
    pub watches: Vec<WatchTarget>,
    #[serde(default)]
    pub fingerprint: Fingerprint,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub proxy: Option<ProxyConfig>,
}

impl Config {
    /// 扁平化所有页面，供调度层按页建任务。
    pub fn pages(&self) -> Vec<&Page> {
        self.merchants.iter().flat_map(|m| m.pages.iter()).collect()
    }

    /// 监控元素总数。
    pub fn element_count(&self) -> usize {
        self.pages().iter().map(|p| p.elements.len()).sum()
    }
}

// ---- 内部取值/校验辅助 ----

fn require_str(d: &toml::Table, key: &str, ctx: &str) -> Result<String, ConfigError> {
    match d.get(key) {
        None => Err(err(ctx, format!("缺少必填字段 “{key}”"))),
        Some(toml::Value::String(s)) if !s.trim().is_empty() => Ok(s.clone()),
        Some(_) => Err(err(ctx, format!("字段 “{key}” 必须为非空字符串"))),
    }
}

fn opt_str(d: &toml::Table, key: &str, default: &str, ctx: &str) -> Result<String, ConfigError> {
    match d.get(key) {
        None => Ok(default.to_string()),
        Some(toml::Value::String(s)) => Ok(s.clone()),
        Some(_) => Err(err(ctx, format!("字段 “{key}” 必须为字符串"))),
    }
}

fn opt_pos_int(d: &toml::Table, key: &str, default: i64, ctx: &str) -> Result<i64, ConfigError> {
    match d.get(key) {
        None => Ok(default),
        Some(toml::Value::Integer(v)) if *v > 0 => Ok(*v),
        Some(toml::Value::Integer(v)) => {
            Err(err(ctx, format!("字段 “{key}” 必须为正整数，当前为 {v}")))
        }
        Some(_) => Err(err(ctx, format!("字段 “{key}” 必须为整数"))),
    }
}

fn opt_nth(d: &toml::Table, ctx: &str) -> Result<Option<i64>, ConfigError> {
    match d.get("nth") {
        None => Ok(None),
        Some(toml::Value::Integer(v)) if *v >= 0 => Ok(Some(*v)),
        Some(toml::Value::Integer(v)) => {
            Err(err(ctx, format!("字段 “nth” 必须为非负整数，当前为 {v}")))
        }
        Some(_) => Err(err(ctx, "字段 “nth” 必须为非负整数")),
    }
}

/// 可选 JS 表达式：空字符串也允许，空白校验留给运行时。
fn opt_js(d: &toml::Table, ctx: &str) -> Result<Option<String>, ConfigError> {
    match d.get("js") {
        None => Ok(None),
        Some(toml::Value::String(s)) => Ok(Some(s.clone())),
        Some(_) => Err(err(ctx, "字段 “js” 必须为字符串")),
    }
}

/// 可选跳转 URL：非空时校验 http(s)。
fn opt_url(d: &toml::Table, ctx: &str) -> Result<Option<String>, ConfigError> {
    match d.get("url") {
        None => Ok(None),
        Some(toml::Value::String(s)) if !s.trim().is_empty() => {
            let url = s.trim().to_string();
            if !is_http_url(&url) {
                return Err(err(ctx, format!("的 url 不是合法的 http(s) 地址：{url}")));
            }
            Ok(Some(url))
        }
        Some(_) => Err(err(ctx, "字段 “url” 必须为非空字符串")),
    }
}

// ---- Fingerprint / Proxy 解析 ----

fn parse_viewport(v: &toml::Value, ctx: &str) -> Result<Viewport, ConfigError> {
    let table = v
        .as_table()
        .ok_or_else(|| err(ctx, "字段 “viewport” 必须为表（table）"))?;
    let bad_w = || err(ctx, "字段 “viewport.width” 必须为正整数");
    let bad_h = || err(ctx, "字段 “viewport.height” 必须为正整数");
    let width = match table.get("width") {
        Some(toml::Value::Integer(v)) if *v > 0 => *v,
        _ => return Err(bad_w()),
    };
    let height = match table.get("height") {
        Some(toml::Value::Integer(v)) if *v > 0 => *v,
        _ => return Err(bad_h()),
    };
    Ok(Viewport { width, height })
}

fn as_table(v: Option<&toml::Value>) -> Option<&toml::Table> {
    v.and_then(|v| v.as_table())
}

fn parse_fingerprint(
    d: Option<&toml::Table>,
    base: &Fingerprint,
    ctx: &str,
) -> Result<Fingerprint, ConfigError> {
    let Some(d) = d else {
        return Ok(base.clone());
    };

    let user_agent =
        non_empty(opt_str(d, "user_agent", "", ctx)?).or_else(|| base.user_agent.clone());
    let locale = non_empty(opt_str(d, "locale", "", ctx)?).or_else(|| base.locale.clone());

    let tz_raw = opt_str(d, "timezone_id", "", ctx)?;
    let tz = if tz_raw.is_empty() {
        base.timezone_id.clone()
    } else {
        let re = Regex::new(r"^[A-Za-z][A-Za-z0-9_+\-/]{0,40}$").unwrap();
        if !re.is_match(&tz_raw) {
            return Err(err(
                ctx,
                format!("字段 “fingerprint.timezone_id” 不是合法的 IANA 时区 ID：{tz_raw}"),
            ));
        }
        Some(tz_raw)
    };

    let color_raw = opt_str(d, "color_scheme", "", ctx)?;
    let color = if color_raw.is_empty() {
        base.color_scheme.clone()
    } else {
        if !VALID_COLOR_SCHEME.contains(&color_raw.as_str()) {
            let opts = VALID_COLOR_SCHEME
                .iter()
                .filter(|c| **c != "null")
                .cloned()
                .collect::<Vec<_>>()
                .join("、");
            return Err(err(
                ctx,
                format!(
                    "字段 “fingerprint.color_scheme” 取值非法：{color_raw}（可选：{opts}、null）"
                ),
            ));
        }
        Some(color_raw)
    };

    let viewport = match d.get("viewport") {
        Some(v) => Some(parse_viewport(v, ctx)?),
        None => base.viewport,
    };

    Ok(Fingerprint {
        user_agent,
        locale,
        timezone_id: tz,
        color_scheme: color,
        viewport,
    })
}

fn parse_proxy(d: Option<&toml::Value>, ctx: &str) -> Result<Option<ProxyConfig>, ConfigError> {
    let Some(v) = d else { return Ok(None) };
    match v {
        toml::Value::String(s) => {
            let url = s.trim();
            if url.is_empty() {
                return Ok(None);
            }
            if !is_http_url(url) && !url.starts_with("socks5://") {
                return Err(err(
                    ctx,
                    "字段 “proxy” 字符串必须为 http(s):// 或 socks5://",
                ));
            }
            Ok(Some(parse_proxy_string(url)))
        }
        toml::Value::Table(t) => {
            let server = require_str(t, "server", ctx)?;
            if !is_http_url(&server) && !server.starts_with("socks5://") {
                return Err(err(
                    ctx,
                    "字段 “proxy.server” 必须为 http(s):// 或 socks5://",
                ));
            }
            let username = non_empty(opt_str(t, "username", "", ctx)?);
            let password = non_empty(opt_str(t, "password", "", ctx)?);
            let bypass = non_empty(opt_str(t, "bypass", "", ctx)?);
            Ok(Some(ProxyConfig {
                server,
                username,
                password,
                bypass,
            }))
        }
        _ => Err(err(ctx, "字段 “proxy” 必须为字符串或表")),
    }
}

fn non_empty(s: String) -> Option<String> {
    if s.is_empty() { None } else { Some(s) }
}

/// 拆 `http://user:pass@host:port` 字符串为结构化字段。
fn parse_proxy_string(url: &str) -> ProxyConfig {
    let parsed = url::Url::parse(url).ok();
    let (scheme, host, port, userinfo) = if let Some(p) = &parsed {
        (
            p.scheme().to_string(),
            p.host_str().map(|h| h.to_string()),
            p.port(),
            {
                let u = p.username();
                let pw = p.password();
                match (u.is_empty(), pw) {
                    (true, None) => None,
                    _ => Some((percent_decode(u), pw.map(percent_decode))),
                }
            },
        )
    } else {
        (String::from("http"), None, None, None)
    };
    let mut server = format!("{scheme}://{}", host.unwrap_or_default());
    if let Some(port) = port {
        server.push_str(&format!(":{port}"));
    }
    let (username, password) = match userinfo {
        Some((u, p)) => (Some(u).filter(|s| !s.is_empty()), p),
        None => (None, None),
    };
    ProxyConfig {
        server,
        username,
        password,
        bypass: None,
    }
}

fn percent_decode(s: &str) -> String {
    let mut out = Vec::new();
    let bytes = s.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            let hex = &s[i + 1..i + 3];
            if let Ok(b) = u8::from_str_radix(hex, 16) {
                out.push(b);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn check_enum(value: &str, allowed: &[&str], field: &str, ctx: &str) -> Result<(), ConfigError> {
    if allowed.contains(&value) {
        Ok(())
    } else {
        let opts = allowed.join("、");
        Err(err(
            ctx,
            format!("字段 {field} 取值非法：{value}（可选：{opts}）"),
        ))
    }
}

/// 可向下级联的默认值集合（全局 → 商家 → 页面）。
#[derive(Debug, Clone)]
struct Defaults {
    poll_interval_secs: i64,
    wait_until: String,
    nav_timeout_secs: i64,
    failure_threshold: i64,
    selector_type: String,
}

fn override_defaults(d: &toml::Table, base: &Defaults, ctx: &str) -> Result<Defaults, ConfigError> {
    let poll = opt_pos_int(d, "poll_interval_secs", base.poll_interval_secs, ctx)?;
    let nav = opt_pos_int(d, "nav_timeout_secs", base.nav_timeout_secs, ctx)?;
    let failure = opt_pos_int(d, "failure_threshold", base.failure_threshold, ctx)?;
    let wait_until = opt_str(d, "wait_until", &base.wait_until, ctx)?;
    check_enum(&wait_until, VALID_WAIT_UNTIL, "wait_until", ctx)?;
    let selector_type = opt_str(d, "selector_type", &base.selector_type, ctx)?;
    check_enum(&selector_type, VALID_SELECTOR_TYPES, "selector_type", ctx)?;
    Ok(Defaults {
        poll_interval_secs: poll,
        wait_until,
        nav_timeout_secs: nav,
        failure_threshold: failure,
        selector_type,
    })
}

fn parse_telegram(raw: &toml::Table) -> Result<TelegramConfig, ConfigError> {
    let Some(tg) = raw.get("telegram").and_then(|v| v.as_table()) else {
        return Err(ConfigError::new("缺少 [telegram] 配置段"));
    };
    let bot_token = require_str(tg, "bot_token", "[telegram] ")?;
    let chat_id = parse_chat_id(tg)?;
    Ok(TelegramConfig { bot_token, chat_id })
}

fn parse_chat_id(tg: &toml::Table) -> Result<String, ConfigError> {
    match tg.get("chat_id") {
        None => Err(ConfigError::new("[telegram] 缺少必填字段 “chat_id”")),
        Some(toml::Value::Integer(v)) => Ok(v.to_string()),
        Some(toml::Value::String(s)) if !s.trim().is_empty() => Ok(s.clone()),
        Some(_) => Err(ConfigError::new(
            "[telegram] 字段 “chat_id” 必须为非空字符串或整数",
        )),
    }
}

fn parse_element(
    e: &toml::Table,
    ctx: &str,
    merchant_name: &str,
    page_name: &str,
    defaults: &Defaults,
) -> Result<MonitoredElement, ConfigError> {
    let selector = require_str(e, "selector", ctx)?;
    let nth = opt_nth(e, ctx)?;
    let js = opt_js(e, ctx)?;
    let jump_url = opt_url(e, ctx)?;
    // name 缺省回退：带上 nth 后缀，避免同页面同选择器不同 nth 的元素标识撞车。
    let default_name = match nth {
        None => selector.clone(),
        Some(n) => format!("{selector}#{n}"),
    };
    let name = match non_empty(opt_str(e, "name", "", ctx)?) {
        Some(n) => n,
        None => default_name,
    };
    let selector_type = opt_str(e, "selector_type", &defaults.selector_type, ctx)?;
    check_enum(&selector_type, VALID_SELECTOR_TYPES, "selector_type", ctx)?;

    Ok(MonitoredElement {
        merchant_name: merchant_name.to_string(),
        page_name: page_name.to_string(),
        name,
        selector,
        selector_type,
        nth,
        js,
        url: jump_url,
    })
}

fn parse_page(
    p: &toml::Table,
    ctx: &str,
    merchant_name: &str,
    defaults: &Defaults,
    seen_elements: &mut HashSet<String>,
    fingerprint: &Fingerprint,
    proxy: &Option<ProxyConfig>,
) -> Result<Page, ConfigError> {
    let url = require_str(p, "url", ctx)?;
    validate_url(&url, ctx)?;
    let name = match non_empty(opt_str(p, "name", "", ctx)?) {
        Some(n) => n,
        None => url.clone(),
    };
    let page_defaults = override_defaults(p, defaults, ctx)?;

    let Some(elements_raw) = p.get("elements").and_then(|v| v.as_array()) else {
        return Err(err(ctx, "至少需要配置一个 [[merchants.pages.elements]]"));
    };
    if elements_raw.is_empty() {
        return Err(err(ctx, "至少需要配置一个 [[merchants.pages.elements]]"));
    }

    let mut elements = Vec::new();
    for (i, e) in elements_raw.iter().enumerate() {
        let Some(e_table) = e.as_table() else {
            return Err(err(
                ctx,
                format!("第 {} 个 element 格式错误，应为表（table）", i + 1),
            ));
        };
        let element = parse_element(
            e_table,
            &format!("{ctx}element #{} ", i + 1),
            merchant_name,
            &name,
            &page_defaults,
        )?;
        if !seen_elements.insert(element.identity()) {
            return Err(ConfigError::new(format!(
                "监控元素标识重复：{}（同页面内 name 需唯一）",
                element.identity()
            )));
        }
        elements.push(element);
    }

    Ok(Page {
        merchant_name: merchant_name.to_string(),
        name,
        url,
        poll_interval_secs: page_defaults.poll_interval_secs,
        wait_until: page_defaults.wait_until,
        nav_timeout_secs: page_defaults.nav_timeout_secs,
        failure_threshold: page_defaults.failure_threshold,
        elements,
        fingerprint: fingerprint.clone(),
        proxy: proxy.clone(),
    })
}

fn parse_merchant(
    m: &toml::Table,
    ctx: &str,
    defaults: &Defaults,
    seen_pages: &mut HashSet<String>,
    seen_elements: &mut HashSet<String>,
    base_fingerprint: &Fingerprint,
    _base_proxy: &Option<ProxyConfig>,
) -> Result<Merchant, ConfigError> {
    let name = require_str(m, "name", ctx)?;
    let merchant_defaults = override_defaults(m, defaults, ctx)?;
    let merchant_fingerprint = parse_fingerprint(
        as_table(m.get("fingerprint")),
        base_fingerprint,
        &format!("{ctx}fingerprint "),
    )?;
    let merchant_proxy = parse_proxy(m.get("proxy"), &format!("{ctx}proxy "))?;

    let Some(pages_raw) = m.get("pages").and_then(|v| v.as_array()) else {
        return Err(err(ctx, "至少需要配置一个 [[merchants.pages]]"));
    };
    if pages_raw.is_empty() {
        return Err(err(ctx, "至少需要配置一个 [[merchants.pages]]"));
    }

    let mut pages = Vec::new();
    for (i, p) in pages_raw.iter().enumerate() {
        let Some(p_table) = p.as_table() else {
            return Err(err(
                ctx,
                format!("第 {} 个 page 格式错误，应为表（table）", i + 1),
            ));
        };
        let page = parse_page(
            p_table,
            &format!("{ctx}page #{} ", i + 1),
            &name,
            &merchant_defaults,
            seen_elements,
            &merchant_fingerprint,
            &merchant_proxy,
        )?;
        if !seen_pages.insert(page.identity()) {
            return Err(ConfigError::new(format!(
                "页面标识重复：{}（同商家内 name 需唯一）",
                page.identity()
            )));
        }
        pages.push(page);
    }

    Ok(Merchant {
        name,
        pages,
        fingerprint: merchant_fingerprint,
        proxy: merchant_proxy,
    })
}

fn parse_keywords(w: &toml::Table, ctx: &str) -> Result<Vec<String>, ConfigError> {
    let Some(raw) = w.get("keywords").and_then(|v| v.as_array()) else {
        return Err(err(ctx, "字段 “keywords” 必须为非空字符串列表"));
    };
    if raw.is_empty() {
        return Err(err(ctx, "字段 “keywords” 必须为非空字符串列表"));
    }
    let mut keywords = Vec::new();
    for (i, kw) in raw.iter().enumerate() {
        match kw {
            toml::Value::String(s) if !s.trim().is_empty() => keywords.push(s.clone()),
            _ => {
                return Err(err(
                    ctx,
                    format!("字段 “keywords” 第 {} 项必须为非空字符串", i + 1),
                ));
            }
        }
    }
    Ok(keywords)
}

fn parse_id_pattern(w: &toml::Table, ctx: &str) -> Result<Option<String>, ConfigError> {
    match w.get("id_pattern") {
        None => Ok(None),
        Some(toml::Value::String(s)) if !s.trim().is_empty() => {
            Regex::new(s).map_err(|e| err(ctx, format!("字段 “id_pattern” 不是合法正则：{e}")))?;
            Ok(Some(s.clone()))
        }
        Some(_) => Err(err(ctx, "字段 “id_pattern” 必须为非空字符串")),
    }
}

fn parse_watch(
    w: &toml::Table,
    ctx: &str,
    defaults: &Defaults,
    base_fingerprint: &Fingerprint,
    _base_proxy: &Option<ProxyConfig>,
) -> Result<WatchTarget, ConfigError> {
    let url = require_str(w, "url", ctx)?;
    validate_url(&url, ctx)?;
    let name = match non_empty(opt_str(w, "name", "", ctx)?) {
        Some(n) => n,
        None => url.clone(),
    };
    let link_selector = require_str(w, "link_selector", ctx)?;
    let keywords = parse_keywords(w, ctx)?;
    let id_pattern = parse_id_pattern(w, ctx)?;
    let wd = override_defaults(w, defaults, ctx)?;
    let fingerprint = parse_fingerprint(
        as_table(w.get("fingerprint")),
        base_fingerprint,
        &format!("{ctx}fingerprint "),
    )?;
    let proxy = parse_proxy(w.get("proxy"), &format!("{ctx}proxy "))?;
    Ok(WatchTarget {
        name,
        url,
        link_selector,
        selector_type: wd.selector_type,
        keywords,
        id_pattern,
        poll_interval_secs: wd.poll_interval_secs,
        wait_until: wd.wait_until,
        nav_timeout_secs: wd.nav_timeout_secs,
        failure_threshold: wd.failure_threshold,
        fingerprint,
        proxy,
    })
}

fn parse_merchants(
    raw: &toml::Table,
    defaults: &Defaults,
    base_fingerprint: &Fingerprint,
    base_proxy: &Option<ProxyConfig>,
) -> Result<Vec<Merchant>, ConfigError> {
    let Some(merchants_raw) = raw.get("merchants").and_then(|v| v.as_array()) else {
        return Ok(Vec::new());
    };
    let mut merchants = Vec::new();
    let mut seen_merchants: HashSet<String> = HashSet::new();
    let mut seen_pages: HashSet<String> = HashSet::new();
    let mut seen_elements: HashSet<String> = HashSet::new();
    for (i, m) in merchants_raw.iter().enumerate() {
        let Some(m_table) = m.as_table() else {
            return Err(ConfigError::new(format!(
                "第 {} 个 merchant 格式错误，应为表（table）",
                i + 1
            )));
        };
        let merchant = parse_merchant(
            m_table,
            &format!("merchant #{} ", i + 1),
            defaults,
            &mut seen_pages,
            &mut seen_elements,
            base_fingerprint,
            base_proxy,
        )?;
        if !seen_merchants.insert(merchant.name.clone()) {
            return Err(ConfigError::new(format!(
                "商家名称重复：{}（name 需唯一）",
                merchant.name
            )));
        }
        merchants.push(merchant);
    }
    Ok(merchants)
}

fn parse_watches(
    raw: &toml::Table,
    defaults: &Defaults,
    base_fingerprint: &Fingerprint,
    base_proxy: &Option<ProxyConfig>,
) -> Result<Vec<WatchTarget>, ConfigError> {
    let Some(watches_raw) = raw.get("watches").and_then(|v| v.as_array()) else {
        return Ok(Vec::new());
    };
    let mut watches = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    for (i, w) in watches_raw.iter().enumerate() {
        let Some(w_table) = w.as_table() else {
            return Err(ConfigError::new(format!(
                "第 {} 个 watch 格式错误，应为表（table）",
                i + 1
            )));
        };
        let watch = parse_watch(
            w_table,
            &format!("watch #{} ", i + 1),
            defaults,
            base_fingerprint,
            base_proxy,
        )?;
        if !seen.insert(watch.identity()) {
            return Err(ConfigError::new(format!(
                "监控目标标识重复：{}（name 需唯一）",
                watch.identity()
            )));
        }
        watches.push(watch);
    }
    Ok(watches)
}

/// 读取 TOML 文件为未解析的 raw table。只负责「读文件 + 语法解析」。
pub fn load_raw(path: &Path) -> Result<toml::Table, ConfigError> {
    let text = std::fs::read_to_string(path).map_err(|e| {
        ConfigError::new(format!("配置文件不存在或不可读：{}（{e}）", path.display()))
    })?;
    toml::from_str(&text).map_err(|e| ConfigError::new(format!("配置文件 TOML 解析失败：{e}")))
}

/// 从已解析的 raw table 校验并构造 [`Config`]（不触碰文件系统）。
///
/// 「零监控」是允许的：既无 [[merchants]] 也无 [[watches]] 时返回空集合的合法 Config。
pub fn parse_config(raw: &toml::Table) -> Result<Config, ConfigError> {
    let g_max = opt_pos_int(
        raw,
        "max_concurrent_fetches",
        DEFAULT_MAX_CONCURRENT,
        "全局配置",
    )?;
    let g_state = opt_str(raw, "state_path", DEFAULT_STATE_PATH, "全局配置")?;
    let base = override_defaults(
        raw,
        &Defaults {
            poll_interval_secs: DEFAULT_POLL_INTERVAL,
            wait_until: DEFAULT_WAIT_UNTIL.to_string(),
            nav_timeout_secs: DEFAULT_NAV_TIMEOUT,
            failure_threshold: DEFAULT_FAILURE_THRESHOLD,
            selector_type: DEFAULT_SELECTOR_TYPE.to_string(),
        },
        "全局配置",
    )?;

    let telegram = parse_telegram(raw)?;

    let base_fingerprint = parse_fingerprint(
        as_table(raw.get("fingerprint")),
        &Fingerprint::default(),
        "全局配置 fingerprint ",
    )?;
    let base_proxy = parse_proxy(raw.get("proxy"), "全局配置 proxy ")?;

    let merchants = parse_merchants(raw, &base, &base_fingerprint, &base_proxy)?;
    let watches = parse_watches(raw, &base, &base_fingerprint, &base_proxy)?;

    Ok(Config {
        telegram,
        merchants,
        watches,
        poll_interval_secs: base.poll_interval_secs,
        failure_threshold: base.failure_threshold,
        state_path: g_state,
        max_concurrent_fetches: g_max,
        nav_timeout_secs: base.nav_timeout_secs,
        wait_until: base.wait_until,
        fingerprint: base_fingerprint,
        proxy: base_proxy,
    })
}

/// 从 TOML 文件加载并校验配置。
pub fn load_config(path: &Path) -> Result<Config, ConfigError> {
    let raw = load_raw(path)?;
    parse_config(&raw)
}
