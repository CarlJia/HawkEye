//! 真实浏览器冒烟测试：起 Chromium → 打开本地 HTML → CSS/XPath/JS 提取 → 列表提取。
//!
//! 覆盖原 Python tests/test_extract.py 中依赖真实页面渲染的核心场景（夹具已随
//! Rust 迁移搬到 rust/tests/fixtures/）。
//! 需要本机有 Chrome 或 Playwright chromium，标记 ignore 避免常规 `cargo test`
//! 拉起浏览器；显式 `cargo test --test browser_smoke -- --ignored` 触发。
//! 多个用例共用 chromiumoxide 的默认 profile 目录，必须串行：
//! `cargo test --test browser_smoke -- --ignored --test-threads=1`。

use std::io::{Read, Write};
use std::net::TcpListener;

use hawkeye::config::{
    Config, Fingerprint, MonitoredElement, Page as ConfigPage, ProxyConfig, TelegramConfig,
    WatchTarget,
};
use hawkeye::fetch::{BrowserManager, FetchResult, ListResult, PageResult};

const YUNYOO_SAMPLE: &str = include_str!("fixtures/yunyoo_sample.html");
const NODESEEK_SAMPLE: &str = include_str!("fixtures/nodeseek_sample.html");

/// 阻塞式 HTTP 服务必须跑在独立线程：冒烟测试用单线程 tokio runtime，
/// 若占用唯一执行线程会让浏览器启动的 future 永远得不到调度。
fn serve(html: &'static str) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    std::thread::spawn(move || {
        listener
            .set_nonblocking(false)
            .expect("set_nonblocking 失败");
        for stream in listener.incoming() {
            let mut stream = match stream {
                Ok(s) => s,
                Err(_) => break,
            };
            let mut buf = [0u8; 1024];
            let _ = stream.read(&mut buf);
            let resp = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                html.len(),
                html
            );
            let _ = stream.write_all(resp.as_bytes());
        }
    });
    format!("http://{addr}/")
}

/// 回显请求的 User-Agent / sec-ch-ua-platform / Accept-Language 头到页面正文。
fn serve_header_echo() -> String {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    std::thread::spawn(move || {
        listener
            .set_nonblocking(false)
            .expect("set_nonblocking 失败");
        for stream in listener.incoming() {
            let mut stream = match stream {
                Ok(s) => s,
                Err(_) => break,
            };
            let mut buf = [0u8; 8192];
            let _ = stream.read(&mut buf);
            let req = String::from_utf8_lossy(&buf);
            let header = |name: &str| {
                req.lines()
                    .find_map(|l| {
                        let (k, v) = l.split_once(':')?;
                        k.trim()
                            .eq_ignore_ascii_case(name)
                            .then(|| v.trim().to_string())
                    })
                    .unwrap_or_else(|| format!("({name} 缺失)"))
            };
            let body = format!(
                "<html><body><span id=\"ua\">{}</span><span id=\"al\">{}</span></body></html>",
                header("user-agent"),
                header("accept-language")
            );
            let resp = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            let _ = stream.write_all(resp.as_bytes());
        }
    });
    format!("http://{addr}/")
}

fn dummy_config() -> Config {
    Config {
        telegram: TelegramConfig {
            bot_token: "t".into(),
            chat_id: "c".into(),
        },
        merchants: vec![],
        poll_interval_secs: 60,
        failure_threshold: 3,
        state_path: "state.json".into(),
        max_concurrent_fetches: 4,
        nav_timeout_secs: 10,
        wait_until: "domcontentloaded".into(),
        watches: vec![],
        fingerprint: Fingerprint::default(),
        proxy: None::<ProxyConfig>,
    }
}

fn element(
    merchant: &str,
    page: &str,
    name: &str,
    selector: &str,
    nth: Option<i64>,
    js: Option<&str>,
) -> MonitoredElement {
    MonitoredElement {
        merchant_name: merchant.into(),
        page_name: page.into(),
        name: name.into(),
        selector: selector.into(),
        selector_type: "auto".into(),
        nth,
        js: js.map(|s| s.into()),
        url: None,
    }
}

#[tokio::test]
#[ignore = "需要真实 Chromium；显式 --ignored 运行"]
async fn browser_smoke_extract_and_list() {
    eprintln!("[smoke] 启动浏览器……");
    let url = serve(YUNYOO_SAMPLE);
    let manager = BrowserManager::new(&dummy_config());
    tokio::time::timeout(std::time::Duration::from_secs(60), manager.start())
        .await
        .expect("浏览器启动超时")
        .expect("浏览器启动失败");
    eprintln!("[smoke] 浏览器已启动");

    let page = ConfigPage {
        merchant_name: "yunyoo".into(),
        name: "购物车".into(),
        url: url.clone(),
        poll_interval_secs: 60,
        wait_until: "domcontentloaded".into(),
        nav_timeout_secs: 10,
        failure_threshold: 3,
        elements: vec![
            // CSS 首个匹配 + 空白修剪
            element("yunyoo", "购物车", "库存", ".status", None, None),
            // nth=1 折叠空白
            element("yunyoo", "购物车", "第二个", ".status", Some(1), None),
            // XPath
            element(
                "yunyoo",
                "购物车",
                "XPath",
                "//*[@id=\"yy-cart-page\"]/main/article[1]/div",
                None,
                None,
            ),
            // JS 模式
            element(
                "yunyoo",
                "购物车",
                "JS",
                ".status",
                None,
                Some("el => el.textContent.trim() + '!'"),
            ),
            // stealth 生效性：init script 在页面加载前注入，navigator.webdriver 不为 true
            element(
                "yunyoo",
                "购物车",
                "stealth",
                "body",
                None,
                Some("el => String(navigator.webdriver) === 'true' ? 'FAIL' : 'OK'"),
            ),
            // 未匹配 → NoMatch
            element("yunyoo", "购物车", "不存在", "#nope", None, None),
            // 匹配但文本为空
            element("yunyoo", "购物车", "空节点", ".empty", None, None),
        ],
        fingerprint: Fingerprint::default(),
        proxy: None,
    };

    eprintln!("[smoke] 开始 fetch_page……");
    let fetch_result = tokio::time::timeout(
        std::time::Duration::from_secs(90),
        manager.fetch_page(&page),
    )
    .await
    .expect("fetch_page 超时");
    eprintln!("[smoke] fetch_page 完成");
    match fetch_result {
        PageResult::Fetched { results } => {
            let get = |name: &str| {
                results
                    .iter()
                    .find(|(e, _)| e.name == name)
                    .map(|(_, r)| r.clone())
                    .unwrap()
            };
            assert_eq!(
                get("库存"),
                FetchResult::Ok {
                    value: "充足".into()
                },
                "CSS 首个匹配"
            );
            assert_eq!(
                get("第二个"),
                FetchResult::Ok {
                    value: "较少".into()
                },
                "nth=1"
            );
            assert_eq!(
                get("XPath"),
                FetchResult::Ok {
                    value: "充足".into()
                },
                "XPath"
            );
            assert_eq!(
                get("JS"),
                FetchResult::Ok {
                    value: "充足!".into()
                },
                "JS 模式"
            );
            assert_eq!(
                get("stealth"),
                FetchResult::Ok { value: "OK".into() },
                "stealth 注入"
            );
            assert!(
                matches!(get("不存在"), FetchResult::NoMatch { .. }),
                "未匹配"
            );
            let empty = get("空节点");
            match &empty {
                FetchResult::NoMatch { reason } => {
                    assert!(reason.contains("文本为空"), "空节点 reason：{reason}");
                }
                other => panic!("空节点应为 NoMatch：{other:?}"),
            }
        }
        PageResult::LoadError { reason } => panic!("页面加载失败：{reason}"),
    }

    // 列表提取：NodeSeek 夹具（XPath link_selector + id_pattern 捕获组）。
    let list_url = serve(NODESEEK_SAMPLE);
    let watch = WatchTarget {
        name: "NodeSeek".into(),
        url: list_url.clone(),
        link_selector: "//*[@id=\"nsk-body-left\"]//div[@class=\"post-title\"]/a".into(),
        selector_type: "auto".into(),
        keywords: vec!["hk".into()],
        id_pattern: Some(r"post-(\d+)-".into()),
        poll_interval_secs: 60,
        wait_until: "domcontentloaded".into(),
        nav_timeout_secs: 10,
        failure_threshold: 3,
        fingerprint: Fingerprint::default(),
        proxy: None,
    };
    match manager.fetch_list(&watch).await {
        ListResult::Fetched { items } => {
            assert_eq!(items.len(), 4);
            assert_eq!(items[0].post_id, "911200");
            assert_eq!(items[0].title, "HK 原生 IP 测评");
            // 相对 href 补全为绝对 URL。
            assert!(items[0].url.starts_with("http://127.0.0.1:"));
            assert!(items[0].url.ends_with("/post-911200-1"));
            // 换行标题折叠空白。
            assert_eq!(items[2].title, "香港 HK 高防");
            // 绝对 href 原样保留。
            assert_eq!(items[3].url, "https://www.nodeseek.com/post-911203-1");
        }
        ListResult::LoadError { reason } => panic!("列表加载失败：{reason}"),
    }

    manager.close().await;
}

/// 回归：UA / Accept-Language 覆盖必须同时作用于 HTTP 层（CDP setUserAgentOverride）
/// 与 JS 层（stealth init script）。修复前只在 JS 层注入 navigator.userAgent，HTTP 头
/// 泄露真机指纹（macOS UA + 系统 Accept-Language），CF 挑战页交叉核对两层不一致
/// 即永不放行（vmiss / US.LA.TRI 每轮 45s 预算烧满）。
#[tokio::test]
#[ignore = "需要真实 Chromium；显式 --ignored 运行"]
async fn browser_smoke_ua_consistent_across_http_and_js_layers() {
    let url = serve_header_echo();
    let manager = BrowserManager::new(&dummy_config());
    tokio::time::timeout(std::time::Duration::from_secs(60), manager.start())
        .await
        .expect("浏览器启动超时")
        .expect("浏览器启动失败");

    let page = ConfigPage {
        merchant_name: "echo".into(),
        name: "请求头回显".into(),
        url: url.clone(),
        poll_interval_secs: 60,
        wait_until: "domcontentloaded".into(),
        nav_timeout_secs: 10,
        failure_threshold: 3,
        elements: vec![
            element("echo", "请求头回显", "HTTP UA 头", "#ua", None, None),
            element(
                "echo",
                "请求头回显",
                "JS navigator.userAgent",
                "#ua",
                None,
                Some("el => navigator.userAgent"),
            ),
            element(
                "echo",
                "请求头回显",
                "HTTP Accept-Language 头",
                "#al",
                None,
                None,
            ),
            element(
                "echo",
                "请求头回显",
                "JS navigator.languages",
                "#al",
                None,
                Some("el => navigator.languages.join(',')"),
            ),
        ],
        fingerprint: Fingerprint::default(),
        proxy: None,
    };
    match manager.fetch_page(&page).await {
        PageResult::Fetched { results } => {
            let get = |name: &str| {
                results
                    .iter()
                    .find(|(e, _)| e.name == name)
                    .map(|(_, r)| r.clone())
                    .unwrap()
            };
            let http_ua = match get("HTTP UA 头") {
                FetchResult::Ok { value } => value,
                other => panic!("HTTP UA 头提取失败：{other:?}"),
            };
            let js_ua = match get("JS navigator.userAgent") {
                FetchResult::Ok { value } => value,
                other => panic!("JS navigator.userAgent 提取失败：{other:?}"),
            };
            assert_eq!(
                http_ua, js_ua,
                "HTTP 层 User-Agent 头必须与 JS 层 navigator.userAgent 一致（不一致即 CF 伪造信号）"
            );
            let http_al = match get("HTTP Accept-Language 头") {
                FetchResult::Ok { value } => value,
                other => panic!("HTTP Accept-Language 头提取失败：{other:?}"),
            };
            let js_langs = match get("JS navigator.languages") {
                FetchResult::Ok { value } => value,
                other => panic!("JS navigator.languages 提取失败：{other:?}"),
            };
            // Chrome 会在 Accept-Language 头上追加质量参数（如 "en;q=0.9"），
            // 比较时剥掉 q 值，只看语言列表本身。
            let normalize_langs = |s: &str| {
                s.split(',')
                    .map(|p| p.split(';').next().unwrap_or(p).trim().to_string())
                    .collect::<Vec<_>>()
                    .join(",")
            };
            assert_eq!(
                normalize_langs(&http_al),
                normalize_langs(&js_langs),
                "Accept-Language 头必须与 navigator.languages 一致"
            );
        }
        PageResult::LoadError { reason } => panic!("页面加载失败：{reason}"),
    }
    manager.close().await;
}
