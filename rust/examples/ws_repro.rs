//! 临时诊断入口：复现 "WS Invalid message" 并抓取被丢弃的原始 CDP 消息。
//! 诊断完成后删除本文件。

use std::io::{Read, Write};
use std::net::TcpListener;
use std::time::Duration;

use hawkeye::config::{
    Config, Fingerprint, MonitoredElement, Page as ConfigPage, ProxyConfig, TelegramConfig,
};
use hawkeye::fetch::{BrowserManager, PageResult};

fn serve(html: &'static str) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    std::thread::spawn(move || {
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

const PAGE_HTML: &str = r#"<html><body><div class="status">充足</div></body></html>"#;

#[tokio::main]
async fn main() {
    use tracing_subscriber::EnvFilter;
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::new(
            "info,chromiumoxide=warn,chromiumoxide::conn::raw_ws::parse_errors=debug",
        ))
        .init();

    let config = Config {
        telegram: TelegramConfig { bot_token: "t".into(), chat_id: "c".into() },
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
    };
    let manager = BrowserManager::new(&config);
    manager.start().await.expect("浏览器启动失败");
    eprintln!("[repro] 浏览器已启动");

    let page = ConfigPage {
        merchant_name: "repro".into(),
        name: "诊断".into(),
        url: serve(PAGE_HTML),
        poll_interval_secs: 60,
        wait_until: "domcontentloaded".into(),
        nav_timeout_secs: 10,
        failure_threshold: 3,
        elements: vec![MonitoredElement {
            merchant_name: "repro".into(),
            page_name: "诊断".into(),
            name: "库存".into(),
            selector: ".status".into(),
            selector_type: "auto".into(),
            nth: None,
            js: None,
            url: None,
        }],
        fingerprint: Fingerprint::default(),
        proxy: None,
    };
    match tokio::time::timeout(Duration::from_secs(60), manager.fetch_page(&page)).await {
        Ok(PageResult::Fetched { .. }) => eprintln!("[repro] fetch_page 完成"),
        Ok(other) => eprintln!("[repro] fetch_page 异常：{other:?}"),
        Err(_) => eprintln!("[repro] fetch_page 超时"),
    }
    manager.close().await;
}
