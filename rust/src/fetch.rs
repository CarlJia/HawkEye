//! 无头浏览器抓取（chromiumoxide / CDP）。
//!
//! 进程级共享单个 Chromium 实例；每个页面一次导航即提取其下全部监控元素，
//! 结束后关闭 context 以释放内存。页面加载失败与单个元素的选择器未匹配/提取
//! 异常区分返回，交由调度层分两级处理。
//!
//! 反 bot 检测两层各补一面：
//!
//! - **stealth JS 注入**（等价 playwright-stealth）：evasion 脚本打包进二进制，
//!   通过 `Page.addScriptToEvaluateOnNewDocument` 在每个新页面加载前自动执行。
//! - **真 Chrome 优先**：优先用系统安装的 Google Chrome（等价 channel="chrome"），
//!   消除 sec-ch-ua 里的 HeadlessChrome 字样与 UA 版本错位；未装时降级到
//!   Playwright 下载的 bundled chromium。

use std::path::PathBuf;
use std::time::Duration;

use chromiumoxide::Page;
use chromiumoxide::browser::{Browser, BrowserConfig};
use futures::StreamExt;

use crate::config::{
    Config, Fingerprint, MonitoredElement, Page as ConfigPage, ProxyConfig, WatchTarget,
};
use crate::extract::{ListItem, extract_list_items, extract_text};

/// 导航时的瞬时网络错误：代理抖动会掐断在途连接，下一瞬多半自愈，做有限次重试。
const TRANSIENT_NAV_ERRORS: &[&str] = &[
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_REFUSED",
    "ERR_CONNECTION_ABORTED",
    "ERR_EMPTY_RESPONSE",
    "ERR_SOCKET_NOT_CONNECTED",
    "ERR_NETWORK_CHANGED",
    "ERR_ABORTED",
];
/// 退避序列长度即重试次数；总尝试次数 = len + 1。控制在 poll 间隔内。
const NAV_RETRY_BACKOFF_SECS: [f64; 2] = [1.0, 2.0];

/// 反爬挑战页特征：CF IUAM / Turnstile 与 DDoS-Guard 的标准文案与 DOM 标记。
const CHALLENGE_TITLE_FRAGMENTS: &[&str] = &[
    "just a moment",
    "checking your browser",
    "ddos-guard",
    "verifying you are human",
    "attention required",
];
const CHALLENGE_DOM_SELECTOR: &str = concat!(
    r#"script[src*="challenges.cloudflare.com"], "#,
    "#cf-challenge-running, ",
    "#cf-mitigated, ",
    r#"[class*="cf-turnstile"]"#
);
/// 反爬挑战页放行预算：实测 CF IUAM / Turnstile 在 IP 风控时段经常 8–18s 才放行，
/// 风控段 IP 实测 25–45s。给到 45s 总预算；预算内一旦检测到挑战页消失立刻放行。
const CHALLENGE_BACKOFF_SECS: [f64; 3] = [8.0, 12.0, 25.0];

fn is_transient_nav_error(reason: &str) -> bool {
    TRANSIENT_NAV_ERRORS
        .iter()
        .any(|code| reason.contains(code))
}

fn first_line(s: &str) -> String {
    s.lines().next().unwrap_or("").to_string()
}

/// 与 Chromium 主版本对齐的 UA（版本错位即被 CF 视为伪造）。
fn default_user_agent(chromium_major: u32) -> String {
    format!(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 \
         (KHTML, like Gecko) Chrome/{chromium_major}.0.0.0 Safari/537.36"
    )
}

/// 从 UA 串推导 sec-ch-ua-platform 风格的平台名；识别不了返回 None（不覆盖，保留真值）。
fn platform_from_ua(ua: &str) -> Option<&'static str> {
    if ua.contains("Windows") {
        Some("Windows")
    } else if ua.contains("Macintosh") || ua.contains("Mac OS X") {
        Some("macOS")
    } else if ua.contains("Android") {
        Some("Android")
    } else if ua.contains("Linux") || ua.contains("X11") {
        Some("Linux")
    } else {
        None
    }
}

fn proxy_is_socks5(proxy: &ProxyConfig) -> bool {
    proxy.server.starts_with("socks5://")
}

/// socks5://user:pass@host:port → socks5://host:port。
///
/// 用户名密码无法用 Chromium CLI 形参表达（落到应用层代理前置解决），这里只保留 host:port。
fn socks5_server_without_userinfo(server: &str) -> String {
    match server.rsplit_once('@') {
        Some((_, host)) => format!("socks5://{host}"),
        None => server.to_string(),
    }
}

/// 当前进程是否以 root 运行（getuid 或 geteuid 为 0）。
///
/// 只在 Linux 上有意义：Chromium 的 root 拒绝检查位于 zygote_host_impl_linux.cc，
/// macOS 以 root 运行不需要（也不应）关沙箱。
#[cfg(target_os = "linux")]
fn running_as_root() -> bool {
    // SAFETY: getuid/geteuid 为只读系统调用，无副作用。
    unsafe { libc::getuid() == 0 || libc::geteuid() == 0 }
}

#[cfg(not(target_os = "linux"))]
fn running_as_root() -> bool {
    false
}

/// 构建 Chromium 启动配置。
///
/// 形参一律走 chromiumoxide 的 builder 方法，不手拼字符串：它内部的 ArgsBuilder 会把每个形参
/// 渲染成 `--<key>`，带前导 `--` 的字符串会被拼成 `----xxx` 而被 Chromium 忽略——沙箱、
/// 无头模式、AutomationControlled 与代理全都踩过这个坑。
///
/// `is_root` 为真时关闭沙箱：Linux 上 zygote 检测到以 root 运行且未关沙箱会直接拒绝启动
/// （stderr 打印 "Running as root without --no-sandbox"），而服务默认以 root 运行。
fn build_browser_config(
    exec: PathBuf,
    proxy: Option<&ProxyConfig>,
    is_root: bool,
) -> Result<BrowserConfig, String> {
    let mut builder = BrowserConfig::builder()
        .chrome_executable(exec)
        .new_headless_mode() // --headless=new（旧默认是 --headless）
        .hide() // --disable-blink-features=AutomationControlled
        .request_timeout(Duration::from_secs(60));
    if let Some(proxy) = proxy {
        // 代理走 CLI 形参（chromiumoxide 的 connect 代理对 SOCKS 支持有限）。
        let server = if proxy_is_socks5(proxy) {
            socks5_server_without_userinfo(&proxy.server)
        } else {
            proxy.server.clone()
        };
        builder = builder.arg(format!("proxy-server={server}"));
    }
    if is_root {
        builder = builder.no_sandbox();
    }
    builder
        .build()
        .map_err(|e| format!("构建浏览器配置失败：{e}"))
}

/// 日志里显示代理 server 时剥离 userinfo 段。
fn redact_proxy_server(server: &str) -> String {
    match server.rsplit_once('@') {
        None => server.to_string(),
        Some((_, host)) => {
            let scheme = server.split("://").next().unwrap_or("http");
            format!("{scheme}://{host}")
        }
    }
}

// ---- stealth payload 组装（对应 playwright-stealth 的 script_payload）----

const STEALTH_EVASIONS: &[&str] = &[
    include_str!("stealth_evasion_chrome_app.js"),
    include_str!("stealth_evasion_chrome_csi.js"),
    include_str!("stealth_evasion_chrome_hairline.js"),
    include_str!("stealth_evasion_chrome_load_times.js"),
    // chrome.runtime 默认关闭（与 playwright-stealth 默认一致，false）。
    include_str!("stealth_evasion_iframe_contentWindow.js"),
    include_str!("stealth_evasion_media_codecs.js"),
    include_str!("stealth_evasion_navigator_languages.js"),
    include_str!("stealth_evasion_navigator_permissions.js"),
    include_str!("stealth_evasion_navigator_platform.js"),
    include_str!("stealth_evasion_navigator_plugins.js"),
    include_str!("stealth_evasion_navigator_userAgent.js"),
    include_str!("stealth_evasion_navigator_userAgentData.js"),
    include_str!("stealth_evasion_navigator_vendor.js"),
    include_str!("stealth_evasion_navigator_webdriver.js"),
    include_str!("stealth_evasion_error_prototype.js"),
    include_str!("stealth_evasion_webgl_vendor.js"),
];

/// stealth IIFE：opts + utils + magic arrays + 各 evasion，包成一次执行。
fn stealth_payload() -> String {
    let opts = r#"const opts = {"navigator_languages_override":["en-US","en"],"navigator_platform":"Win32","script_logging":false};"#;
    let utils = include_str!("stealth_utils.js");
    let magic = include_str!("stealth_magic_arrays.js");
    let evasions = STEALTH_EVASIONS.join("\n");
    format!("(() => {{\n{opts}\n{utils}\n{magic}\n{evasions}\n}})();")
}

// ---- 抓取结果类型 ----

/// 单个元素的提取结果。
#[derive(Debug, Clone, PartialEq)]
#[allow(dead_code)]
pub enum FetchResult {
    /// 成功取到文本。
    Ok { value: String },
    /// 导航成功但没取到可用文本；reason 区分「压根没匹配到」与「匹配到了却是空文本」。
    NoMatch { reason: String },
    /// 单个元素提取异常。
    Error { reason: String },
}

/// 页面级结果。
#[derive(Debug, Clone)]
pub enum PageResult {
    /// 页面级失败：创建上下文/页面或导航失败，本轮无法提取任何元素。
    LoadError { reason: String },
    /// 页面导航成功，携带每个元素各自的提取结果。
    Fetched {
        results: Vec<(MonitoredElement, FetchResult)>,
    },
}

/// 列表页结果。
#[derive(Debug, Clone)]
pub enum ListResult {
    LoadError { reason: String },
    Fetched { items: Vec<ListItem> },
}

// ---- 导航与等待 ----

/// Playwright wait_until 语义在 CDP lifecycleEvent 上的映射。
async fn goto_and_wait(
    page: &Page,
    url: &str,
    wait_until: &str,
    timeout: Duration,
) -> Result<(), String> {
    use chromiumoxide::cdp::browser_protocol::page::NavigateParams;

    // CDP lifecycle 事件名：init（导航提交）/ DOMContentLoaded / load / networkIdle。
    let target_event: &'static str = match wait_until {
        "load" => "load",
        "networkidle" => "networkIdle",
        "commit" => "init",
        _ => "DOMContentLoaded",
    };

    // 先订阅 lifecycle 事件再导航，避免竞态漏事件。
    let mut events = page
        .event_listener::<chromiumoxide::cdp::browser_protocol::page::EventLifecycleEvent>()
        .await
        .map_err(|e| format!("订阅生命周期事件失败：{e}"))?;

    let nav = page.goto(NavigateParams::builder().url(url).build().unwrap());
    let wait = async {
        while let Some(ev) = events.next().await {
            if ev.name == target_event {
                return Ok(());
            }
        }
        Err("事件流关闭".to_string())
    };
    let (nav_res, wait_res) = tokio::join!(
        async {
            nav.await
                .map_err(|e| format!("导航失败：{}", first_line(&e.to_string())))
        },
        async {
            tokio::time::timeout(timeout, wait)
                .await
                .map_err(|_| format!("{wait_until} 等待超时").to_string())
                .and_then(|r| r)
        }
    );
    // commit / domcontentloaded / load / networkIdle：等到了才算导航成功。
    wait_res?;
    // 导航本身报错（net::ERR_*）也按失败处理；但若事件已到，优先事件结果。
    nav_res?;
    Ok(())
}

/// 判断当前页是否仍是反爬挑战页。title 与 DOM 标记同时检查。
async fn is_challenge_page(page: &Page) -> (bool, Vec<String>) {
    let title = page
        .get_title()
        .await
        .ok()
        .flatten()
        .unwrap_or_default()
        .to_lowercase();
    let title_hits: Vec<&str> = CHALLENGE_TITLE_FRAGMENTS
        .iter()
        .filter(|frag| title.contains(*frag))
        .copied()
        .collect();
    let dom_expr = format!(
        "(()=>{{const el=document.querySelector({CHALLENGE_DOM_SELECTOR:?});if(!el)return null;return el.tagName.toLowerCase()+(el.id?'#'+el.id:'')+(typeof el.className==='string'&&el.className?'.'+el.className.trim().split(/\\s+/).join('.'):'')}})()"
    );
    let dom_hit = page
        .evaluate_expression(dom_expr)
        .await
        .ok()
        .and_then(|r| r.into_value::<serde_json::Value>().ok())
        .and_then(|v| v.as_str().map(|s| s.to_string()));

    let mut hits = Vec::new();
    if !title_hits.is_empty() {
        hits.push(format!("title[{}]={title:?}", title_hits.join(",")));
    }
    if let Some(dom) = dom_hit {
        hits.push(format!("dom[{dom}]"));
    }
    (!hits.is_empty(), hits)
}

/// 若当前是反爬挑战页，按退避序列等页面自愈。返回 true 表示已自愈。
async fn wait_for_challenge_clear(page: &Page, label: &str) -> bool {
    let (mut challenge, hits) = is_challenge_page(page).await;
    if !challenge {
        return true;
    }
    tracing::info!(
        "页面 {label} 检测到反爬挑战页，命中片段：{}",
        hits.join(";")
    );
    for (attempt, backoff) in CHALLENGE_BACKOFF_SECS.iter().enumerate() {
        tracing::info!(
            "页面 {label} 等待反爬挑战页放行，{backoff:.0}s 后重检（第 {} 次）",
            attempt + 1
        );
        tokio::time::sleep(Duration::from_secs_f64(*backoff)).await;
        let (is_challenge, hits) = is_challenge_page(page).await;
        tracing::debug!(
            "页面 {label} 挑战页重检（第 {} 次）：{}",
            attempt + 1,
            if hits.is_empty() {
                "已放行".to_string()
            } else {
                hits.join(";")
            }
        );
        if !is_challenge {
            tracing::info!(
                "页面 {label} 反爬挑战页已通过（第 {} 次重检后，命中片段：{}）",
                attempt + 1,
                hits.join(";")
            );
            return true;
        }
        challenge = is_challenge;
    }
    !challenge
}

// ---- 浏览器管理 ----

/// 管理进程级共享 Chromium。
pub struct BrowserManager {
    browser: tokio::sync::Mutex<Option<Browser>>,
    #[allow(dead_code)]
    fingerprint: Fingerprint,
    proxy: Option<ProxyConfig>,
    chromium_major: std::sync::atomic::AtomicU32,
}

/// 常见 Chrome 可执行文件路径（macOS / Linux），按序探测。
fn detect_chrome_path() -> Option<PathBuf> {
    const CANDIDATES: &[&str] = &[
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/local/bin/google-chrome",
        "/snap/bin/chromium",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ];
    CANDIDATES.iter().map(PathBuf::from).find(|p| p.exists())
}

/// Playwright 下载的 bundled chromium 路径（按版本号取最新的 chromium-*）。
fn detect_bundled_chromium() -> Option<PathBuf> {
    let base = match std::env::var("PLAYWRIGHT_BROWSERS_PATH") {
        Ok(p) => PathBuf::from(p),
        Err(_) => {
            #[cfg(target_os = "macos")]
            {
                dirs_home().join("Library/Caches/ms-playwright")
            }
            #[cfg(target_os = "linux")]
            {
                dirs_home().join(".cache/ms-playwright")
            }
            #[cfg(not(any(target_os = "macos", target_os = "linux")))]
            {
                return None;
            }
        }
    };
    let mut best: Option<(u64, PathBuf)> = None;
    let entries = std::fs::read_dir(&base).ok()?;
    for e in entries.flatten() {
        let name = e.file_name().to_string_lossy().to_string();
        if let Some(rest) = name.strip_prefix("chromium-")
            && let Ok(ver) = rest.parse::<u64>()
        {
            let exec = e.path().join("chrome");
            if exec.exists() && best.as_ref().map(|(v, _)| ver > *v).unwrap_or(true) {
                best = Some((ver, exec));
            }
        }
    }
    best.map(|(_, p)| p)
}

fn dirs_home() -> PathBuf {
    std::env::var("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("."))
}

impl BrowserManager {
    pub fn new(config: &Config) -> Self {
        Self {
            browser: tokio::sync::Mutex::new(None),
            fingerprint: config.fingerprint.clone(),
            proxy: config.proxy.clone(),
            chromium_major: std::sync::atomic::AtomicU32::new(0),
        }
    }

    pub async fn start(&self) -> Result<(), String> {
        let is_root = running_as_root();
        if is_root {
            tracing::warn!("以 root 运行：Chromium 拒绝在未关沙箱时启动，已自动关闭沙箱");
        }

        // 优先真 Chrome（等价 channel="chrome"），未装时降级 bundled chromium。
        let exec = detect_chrome_path();
        let (exec, using_chrome) = match exec {
            Some(p) => (p, true),
            None => {
                match detect_bundled_chromium() {
                    Some(p) => {
                        tracing::warn!(
                            "未装 Google Chrome，降级 bundled chromium；装 Google Chrome 可更稳过 CF"
                        );
                        (p, false)
                    }
                    None => return Err(
                        "找不到可用的 Chromium 可执行文件（Google Chrome 或 Playwright chromium）"
                            .to_string(),
                    ),
                }
            }
        };

        let config = build_browser_config(exec, self.proxy.as_ref(), is_root)?;
        let (browser, mut handler) = Browser::launch(config)
            .await
            .map_err(|e| format!("启动 Chromium 失败：{}", first_line(&e.to_string())))?;
        // 驱动事件循环：chromiumoxide 需要持续 poll handler。
        tokio::spawn(async move {
            while let Some(event) = handler.next().await {
                if let Err(e) = event {
                    tracing::debug!("浏览器事件处理异常：{e}");
                }
            }
        });

        let version = browser
            .version()
            .await
            .map_err(|e| format!("读取 Chromium 版本失败：{e}"))?;
        self.chromium_major.store(
            version
                .product
                .split('/')
                .nth(1)
                .and_then(|v| v.split('.').next())
                .and_then(|v| v.parse().ok())
                .unwrap_or(0),
            std::sync::atomic::Ordering::Relaxed,
        );
        tracing::info!(
            "无头浏览器已启动，{} + stealth（JS 层指纹补丁）",
            if using_chrome {
                "真 Chrome（HTTP 层对齐）"
            } else {
                "bundled chromium"
            }
        );
        if let Some(proxy) = &self.proxy {
            tracing::info!(
                "代理已配置（{}，server={}）",
                if proxy_is_socks5(proxy) {
                    "SOCKS5（Chromium CLI）"
                } else {
                    "HTTP（Chromium CLI）"
                },
                redact_proxy_server(&proxy.server)
            );
        }
        *self.browser.lock().await = Some(browser);
        Ok(())
    }

    pub async fn close(&self) {
        let mut guard = self.browser.lock().await;
        if let Some(mut browser) = guard.take()
            && let Err(e) = browser.close().await
        {
            tracing::warn!(
                "关闭浏览器时忽略异常（驱动可能已退出）：{}",
                first_line(&e.to_string())
            );
        }
        tracing::info!("无头浏览器已关闭");
    }

    /// 创建新页面：stealth 注入 + fingerprint 覆盖（UA / locale / tz / viewport）。
    async fn new_stealth_page(
        &self,
        fingerprint: &Fingerprint,
        proxy: Option<&ProxyConfig>,
    ) -> Result<Page, String> {
        let browser_guard = self.browser.lock().await;
        let browser = browser_guard.as_ref().ok_or("BrowserManager 尚未启动")?;
        let page = browser
            .new_page("about:blank")
            .await
            .map_err(|e| format!("创建页面失败：{}", first_line(&e.to_string())))?;
        drop(browser_guard);

        // stealth + fingerprint：在导航前一次性注入 init script。
        let ua = fingerprint.user_agent.clone().unwrap_or_else(|| {
            default_user_agent(
                self.chromium_major
                    .load(std::sync::atomic::Ordering::Relaxed),
            )
        });
        // HTTP 层 UA 覆盖（等价 Playwright 的 context user_agent）：init script 只能改
        // JS 层的 navigator.*，User-Agent / Accept-Language 头仍是真机值。两层不一致
        // 正是 CF 判定伪造 UA 的信号，挑战页因此永不放行——必须用 CDP 一并盖掉。
        use chromiumoxide::cdp::browser_protocol::emulation::SetUserAgentOverrideParams;
        let accept_language = fingerprint
            .locale
            .clone()
            .unwrap_or_else(|| "en-US,en".to_string());
        let mut ua_override = SetUserAgentOverrideParams::builder()
            .user_agent(ua.clone())
            .accept_language(accept_language);
        if let Some(platform) = platform_from_ua(&ua) {
            ua_override = ua_override.platform(platform);
        }
        page.execute(ua_override.build().expect("构建 SetUserAgentOverride 失败"))
            .await
            .map_err(|e| format!("覆盖 UA 失败：{}", first_line(&e.to_string())))?;
        let mut inject = stealth_payload();
        inject.push_str(&format!(
            "\nObject.defineProperty(navigator,'userAgent',{{get:()=>{ua:?}}});"
        ));
        if let Some(locale) = &fingerprint.locale {
            inject.push_str(&format!(
                "\nObject.defineProperty(navigator,'language',{{get:()=>{locale:?}}});\nObject.defineProperty(navigator,'languages',{{get:()=>[{locale:?}]}});"
            ));
        }
        if let Some(tz) = &fingerprint.timezone_id {
            // Intl 时区覆盖：Emulation.setTimezoneOverride。
            use chromiumoxide::cdp::browser_protocol::emulation::SetTimezoneOverrideParams;
            let _ = page.execute(SetTimezoneOverrideParams::new(tz)).await;
        }
        if let Some(color) = &fingerprint.color_scheme
            && color != "null"
        {
            use chromiumoxide::cdp::browser_protocol::emulation::{
                MediaFeature, SetEmulatedMediaParams,
            };
            let feature = MediaFeature::builder()
                .name("prefers-color-scheme".to_string())
                .value(color.clone())
                .build()
                .expect("构建 MediaFeature 失败");
            let _ = page
                .execute(
                    SetEmulatedMediaParams::builder()
                        .features(vec![feature])
                        .build(),
                )
                .await;
        }
        use chromiumoxide::cdp::browser_protocol::page::AddScriptToEvaluateOnNewDocumentParams;
        let add_script = AddScriptToEvaluateOnNewDocumentParams::builder()
            .source(inject)
            .build()
            .expect("构建 AddScriptToEvaluateOnNewDocument 失败");
        page.execute(add_script)
            .await
            .map_err(|e| format!("注入 stealth 失败：{e}"))?;

        // viewport 覆盖。
        if let Some(viewport) = &fingerprint.viewport {
            use chromiumoxide::cdp::browser_protocol::emulation::SetDeviceMetricsOverrideParams;
            let metrics = SetDeviceMetricsOverrideParams::builder()
                .width(viewport.width)
                .height(viewport.height)
                .device_scale_factor(1.0)
                .mobile(false)
                .build()
                .expect("构建 SetDeviceMetricsOverride 失败");
            let _ = page.execute(metrics).await;
        }

        // 页面级 HTTP 代理在 launch 时全局注入；此处仅记录不支持页面级差异。
        if let Some(p) = proxy
            && !proxy_is_socks5(p)
        {
            tracing::debug!("页面级 HTTP 代理差异暂由全局 --proxy-server 覆盖");
        }
        Ok(page)
    }

    /// 带瞬时错误重试的导航 + 挑战页等待。
    async fn navigate(
        &self,
        page: &Page,
        url: &str,
        wait_until: &str,
        timeout_secs: i64,
        label: &str,
    ) -> Result<(), String> {
        let timeout = Duration::from_secs(timeout_secs.max(1) as u64);
        // 首次 + 每个退避档各一次，最后那次没有退避（None）也不再重试。
        let attempts = NAV_RETRY_BACKOFF_SECS
            .iter()
            .map(Some)
            .chain(std::iter::once(None));
        for (attempt, backoff) in attempts.enumerate() {
            match goto_and_wait(page, url, wait_until, timeout).await {
                Ok(()) => break,
                Err(reason) => {
                    if let Some(backoff) = backoff.filter(|_| is_transient_nav_error(&reason)) {
                        tracing::info!(
                            "页面 {label} 导航瞬时失败（第 {} 次），{backoff:.0}s 后重试：{reason}",
                            attempt + 1
                        );
                        tokio::time::sleep(Duration::from_secs_f64(*backoff)).await;
                        continue;
                    }
                    return Err(reason);
                }
            }
        }
        if !wait_for_challenge_clear(page, label).await {
            tracing::warn!("页面 {label} 仍卡在反爬挑战页,按失败处理");
            return Err("反爬挑战页未在预算时间内放行".to_string());
        }
        Ok(())
    }

    /// 一次导航后提取页面下全部元素；页面级失败返回 LoadError。
    pub async fn fetch_page(&self, page_cfg: &ConfigPage) -> PageResult {
        let page = match self
            .new_stealth_page(&page_cfg.fingerprint, page_cfg.proxy.as_ref())
            .await
        {
            Ok(p) => p,
            Err(e) => return PageResult::LoadError { reason: e },
        };
        if let Err(reason) = self
            .navigate(
                &page,
                &page_cfg.url,
                &page_cfg.wait_until,
                page_cfg.nav_timeout_secs,
                &page_cfg.identity(),
            )
            .await
        {
            let _ = page.close().await;
            return PageResult::LoadError { reason };
        }

        let timeout = Duration::from_secs(page_cfg.nav_timeout_secs.max(1) as u64);
        let mut results = Vec::new();
        for element in &page_cfg.elements {
            let outcome = extract_text(&page, element, timeout).await;
            let result = match outcome.value {
                Some(v) if !v.is_empty() => FetchResult::Ok { value: v },
                Some(_) => FetchResult::NoMatch {
                    reason: outcome.reason.unwrap_or_else(|| "值为空".to_string()),
                },
                None => FetchResult::NoMatch {
                    reason: outcome.reason.unwrap_or_else(|| "提取失败".to_string()),
                },
            };
            results.push((element.clone(), result));
        }
        let _ = page.close().await;
        PageResult::Fetched { results }
    }

    /// 一次导航加载列表页并返回提取到的列表项；页面级失败返回 LoadError。
    pub async fn fetch_list(&self, watch: &WatchTarget) -> ListResult {
        let page = match self
            .new_stealth_page(&watch.fingerprint, watch.proxy.as_ref())
            .await
        {
            Ok(p) => p,
            Err(e) => return ListResult::LoadError { reason: e },
        };
        if let Err(reason) = self
            .navigate(
                &page,
                &watch.url,
                &watch.wait_until,
                watch.nav_timeout_secs,
                &watch.identity(),
            )
            .await
        {
            let _ = page.close().await;
            return ListResult::LoadError { reason };
        }
        let timeout = Duration::from_secs(watch.nav_timeout_secs.max(1) as u64);
        let items = extract_list_items(&page, watch, timeout).await;
        let _ = page.close().await;
        ListResult::Fetched { items }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_platform_from_ua() {
        assert_eq!(platform_from_ua(&default_user_agent(152)), Some("Windows"));
        assert_eq!(
            platform_from_ua(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/152.0.0.0 Safari/537.36"
            ),
            Some("macOS")
        );
        assert_eq!(
            platform_from_ua(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/152.0.0.0 Safari/537.36"
            ),
            Some("Linux")
        );
        assert_eq!(
            platform_from_ua("Mozilla/5.0 (Linux; Android 14) Chrome/152.0.0.0"),
            Some("Android")
        );
        assert_eq!(platform_from_ua("curl/8.0"), None);
    }

    /// 用一个假 chrome 把 chromiumoxide 实际生成的命令行落盘，返回其 argv。
    ///
    /// 真实的 Chromium 会因为 root/macOS 环境差异不可用，但「最终命令行长什么样」是纯
    /// 确定性的——这正是本 bug 的现场：形参被 chromiumoxide 二次拼接后失真。
    /// 用一个假 chrome 把 chromiumoxide 实际生成的命令行落盘，返回其 argv。
    ///
    /// 真实 Chromium 会因 root/平台差异不可用，但「最终命令行长什么样」是纯确定性的——
    /// 本类 bug 的现场就在这一步：形参被 chromiumoxide 二次拼接后失真。
    #[cfg(unix)]
    fn fake_chrome_argv(is_root: bool, proxy: Option<&ProxyConfig>) -> Vec<String> {
        use std::io::Write as _;
        use std::os::unix::fs::PermissionsExt;
        use std::sync::atomic::{AtomicUsize, Ordering};
        static SEQ: AtomicUsize = AtomicUsize::new(0);
        let n = SEQ.fetch_add(1, Ordering::Relaxed);
        let dir =
            std::env::temp_dir().join(format!("hawkeye-fake-chrome-{}-{n}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let script = dir.join("fake-chrome.sh");
        let out = dir.join("argv.txt");
        let mut f = std::fs::File::create(&script).unwrap();
        writeln!(f, "#!/bin/sh").unwrap();
        writeln!(
            f,
            "for a in \"$@\"; do printf '%s\\n' \"$a\"; done > '{}'",
            out.display()
        )
        .unwrap();
        writeln!(f, "sleep 3").unwrap();
        drop(f);
        std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();

        let cfg = build_browser_config(script, proxy, is_root).unwrap();
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        rt.block_on(async {
            let _child = cfg.launch().unwrap();
            for _ in 0..100 {
                if let Ok(c) = std::fs::read_to_string(&out)
                    && !c.trim().is_empty()
                {
                    return c.lines().map(str::to_string).collect();
                }
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
            panic!("假 chrome 没写出 argv");
        })
    }

    fn proxy(server: &str) -> ProxyConfig {
        ProxyConfig {
            server: server.to_string(),
            username: None,
            password: None,
            bypass: None,
        }
    }

    #[cfg(unix)]
    #[test]
    fn test_root_config_passes_no_sandbox_flag() {
        let root = fake_chrome_argv(true, None);
        assert!(
            root.iter().any(|a| a == "--no-sandbox"),
            "root 必须真的把 --no-sandbox 传到命令行，否则 zygote 拒绝启动；实际 argv：{root:?}"
        );
        assert!(root.iter().any(|a| a == "--disable-setuid-sandbox"));

        let user = fake_chrome_argv(false, None);
        assert!(
            !user.iter().any(|a| a.contains("sandbox")),
            "非 root 应保留沙箱，不能无条件关沙箱；实际 argv：{user:?}"
        );
    }

    #[cfg(unix)]
    #[test]
    fn test_headless_and_stealth_args_render_correctly() {
        let argv = fake_chrome_argv(false, None);
        // chromiumoxide 的 ArgsBuilder 会给每个形参补 `--`；形参自带前导 `--` 就会变成
        // `----xxx` 而被 Chromium 忽略。这条断言是当初沙箱/无头/隐身三处的回归护栏。
        assert!(
            !argv.iter().any(|a| a.starts_with("----")),
            "有形参被二次拼接（前导 -- 未去掉）：{argv:?}"
        );
        assert!(
            argv.iter().any(|a| a == "--headless=new"),
            "应显式启用新版无头模式：{argv:?}"
        );
        assert!(
            argv.iter()
                .any(|a| a == "--disable-blink-features=AutomationControlled"),
            "应带上 AutomationControlled 关闭标记：{argv:?}"
        );
    }

    #[cfg(unix)]
    #[test]
    fn test_proxy_args_render_correctly() {
        let socks = fake_chrome_argv(false, Some(&proxy("socks5://user:pass@1.2.3.4:1080")));
        assert!(
            socks
                .iter()
                .any(|a| a == "--proxy-server=socks5://1.2.3.4:1080"),
            "SOCKS 代理应剥掉 userinfo 后落到命令行：{socks:?}"
        );
        assert!(!socks.iter().any(|a| a.starts_with("----")), "{socks:?}");

        let http = fake_chrome_argv(false, Some(&proxy("http://1.2.3.4:8080")));
        assert!(
            http.iter()
                .any(|a| a == "--proxy-server=http://1.2.3.4:8080"),
            "HTTP 代理应落到命令行：{http:?}"
        );
    }
}
