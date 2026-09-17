//! Telegram 通知。
//!
//! 使用 [`TelegramApi`]（默认 reqwest 实现）POST 到 sendMessage；不使用
//! parse_mode（纯文本，避免 Markdown/HTML 转义问题）。可重试的错误（网络异常、
//! 5xx、429）按指数退避重试，永久性拒绝（4xx）立即放弃；是否成功由布尔返回值
//! 告知，上层据此决定是否更新已记录值（至少一次交付）。
//!
//! [`TelegramApi`] 以 trait 注入是硬性设计：测试直接传脚本化的假 API
//! （等价 Python 侧的 httpx.MockTransport），不依赖网络。

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;

use crate::config::TelegramConfig;

const API: &str = "https://api.telegram.org/bot{token}/{method}";
const MAX_ATTEMPTS: u32 = 3;
const DEFAULT_BASE_BACKOFF: f64 = 2.0;
/// Telegram 用 4xx 表达「请求本身不合法」：401/404 token 有误，400 多为 chat_id
/// 不可达，403 被封禁。这些都不会因为再试一次而变好。
const FATAL_STATUS: [u16; 4] = [400, 401, 403, 404];

/// Telegram 明确拒绝了凭据或目标会话，重试无意义。
#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub struct TelegramFatalError(pub String);

/// 把 text 中每个非空 secret 全部替换为 <REDACTED>。纯函数。
#[allow(dead_code)]
pub fn redact(text: &str, secrets: &[&str]) -> String {
    let mut out = text.to_string();
    for secret in secrets {
        if secret.is_empty() {
            continue;
        }
        out = out.replace(secret, "<REDACTED>");
    }
    out
}

fn api_url(token: &str, method: &str) -> String {
    API.replace("{token}", token).replace("{method}", method)
}

fn first_line(s: &str) -> String {
    s.lines().next().unwrap_or("").to_string()
}

/// 变更通知文本，包含 旧值 → 新值。url 非空时附在最后一行。
pub fn format_change_message(
    name: &str,
    old: &str,
    new: &str,
    when: &str,
    url: Option<&str>,
) -> String {
    let mut lines = vec![
        format!("【变更】{name}"),
        format!("{old} → {new}"),
        format!("时间：{when}"),
    ];
    if let Some(url) = url {
        lines.push(url.to_string());
    }
    lines.join("\n")
}

/// 连续失败告警文本。url 非空时附在最后一行。
pub fn format_failure_message(
    name: &str,
    threshold: i64,
    reason: &str,
    when: &str,
    url: Option<&str>,
) -> String {
    let mut lines = vec![
        format!("【异常】{name}"),
        format!("连续 {threshold} 次抓取失败"),
        format!("最近原因：{reason}"),
        format!("时间：{when}"),
    ];
    if let Some(url) = url {
        lines.push(url.to_string());
    }
    lines.join("\n")
}

/// 新帖通知文本：纯文本、裸 URL 由 Telegram 客户端自动成链。
pub fn format_new_post_message(title: &str, url: &str, when: &str) -> String {
    format!("【新帖】{title}\n{url}\n时间：{when}")
}

#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct BotCommand {
    pub command: String,
    pub description: String,
}

/// Telegram HTTP 传输层：POST JSON，返回 (status, body)。网络异常返回 Err。
#[async_trait]
pub trait TelegramApi: Send + Sync {
    async fn post_json(
        &self,
        url: &str,
        payload: &serde_json::Value,
        timeout: Duration,
    ) -> Result<(u16, String), String>;
}

/// 默认传输层：reqwest。
pub struct ReqwestApi {
    client: reqwest::Client,
}

impl ReqwestApi {
    pub fn new() -> Self {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(40))
            .build()
            .expect("构建 reqwest client 失败");
        Self { client }
    }
}

impl Default for ReqwestApi {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl TelegramApi for ReqwestApi {
    async fn post_json(
        &self,
        url: &str,
        payload: &serde_json::Value,
        timeout: Duration,
    ) -> Result<(u16, String), String> {
        let resp = self
            .client
            .post(url)
            .json(payload)
            .timeout(timeout)
            .send()
            .await
            .map_err(|e| e.to_string())?;
        let status = resp.status().as_u16();
        let body = resp.text().await.unwrap_or_default();
        Ok((status, body))
    }
}

/// 通知器面向调度层 / 控制面的接口（测试注入假实现）。
#[async_trait]
pub trait NotifierApi: Send + Sync {
    async fn send(&self, text: &str) -> bool;
    async fn sync_commands(&self, commands: &[BotCommand]) -> String;
    async fn verify(&self) -> Result<(), TelegramFatalError>;
}

/// 封装 Telegram 发送与重试。
pub struct Notifier {
    telegram: TelegramConfig,
    api: Arc<dyn TelegramApi>,
    /// 重试退避基数（秒）。测试可置 0 免真实等待。
    backoff_secs: f64,
}

impl Notifier {
    pub fn new(telegram: TelegramConfig) -> Self {
        Self::with_api(telegram, Arc::new(ReqwestApi::new()))
    }

    pub fn with_api(telegram: TelegramConfig, api: Arc<dyn TelegramApi>) -> Self {
        Self {
            telegram,
            api,
            backoff_secs: DEFAULT_BASE_BACKOFF,
        }
    }

    fn url(&self, method: &str) -> String {
        api_url(&self.telegram.bot_token, method)
    }

    /// 启动前确认 bot_token 与 chat_id 可用。
    ///
    /// 永久性拒绝直接抛 TelegramFatalError（fail-closed）——一个发不出消息的监控
    /// 进程等于没在监控。网络异常或 5xx 只告警。
    pub async fn verify(&self) -> Result<(), TelegramFatalError> {
        let (status, body) = match self
            .api
            .post_json(
                &self.url("getChat"),
                &serde_json::json!({"chat_id": self.telegram.chat_id}),
                Duration::from_secs(20),
            )
            .await
        {
            Ok(r) => r,
            Err(e) => {
                tracing::warn!("Telegram 连通性自检失败（网络异常），继续启动：{e}");
                return Ok(());
            }
        };
        if status == 200 {
            tracing::info!("Telegram 自检通过：chat_id {} 可达", self.telegram.chat_id);
            return Ok(());
        }
        if FATAL_STATUS.contains(&status) {
            return Err(TelegramFatalError(format!(
                "Telegram 拒绝 chat_id {}：{status} {}",
                self.telegram.chat_id,
                first_line(&body)
            )));
        }
        tracing::warn!(
            "Telegram 自检返回 {status}，继续启动：{}",
            first_line(&body)
        );
        Ok(())
    }

    /// 把命令表同步到 Telegram 快捷菜单。失败一律收敛成一句回执，绝不外抛。
    pub async fn sync_commands(&self, commands: &[BotCommand]) -> String {
        let desired: Vec<serde_json::Value> = commands
            .iter()
            .map(|c| serde_json::json!({"command": c.command, "description": c.description}))
            .collect();

        // 先读回现有菜单，没变就不写。
        let current: Option<Vec<serde_json::Value>> = match self
            .api
            .post_json(
                &self.url("getMyCommands"),
                &serde_json::json!({}),
                Duration::from_secs(20),
            )
            .await
        {
            Ok((200, body)) => serde_json::from_str::<serde_json::Value>(&body)
                .ok()
                .and_then(|v| v.get("result").and_then(|r| r.as_array()).cloned()),
            Ok(_) => None,
            Err(e) => {
                tracing::warn!("Telegram 快捷菜单同步失败（网络异常）：{e}");
                return "快捷菜单同步失败：网络异常。稍后发送 /menu 可重试。".to_string();
            }
        };
        if current.as_deref() == Some(desired.as_slice()) {
            tracing::info!("Telegram 快捷菜单已是最新（{} 个命令）", desired.len());
            return format!("快捷菜单已是最新，共 {} 个命令。", desired.len());
        }

        let (status, body) = match self
            .api
            .post_json(
                &self.url("setMyCommands"),
                &serde_json::json!({"commands": desired}),
                Duration::from_secs(20),
            )
            .await
        {
            Ok(r) => r,
            Err(e) => {
                tracing::warn!("Telegram 快捷菜单同步失败（网络异常）：{e}");
                return "快捷菜单同步失败：网络异常。稍后发送 /menu 可重试。".to_string();
            }
        };
        if status != 200 {
            tracing::warn!(
                "Telegram 快捷菜单同步失败（{status}）：{}",
                first_line(&body)
            );
            return format!("快捷菜单同步失败：Telegram 返回 {status}。稍后发送 /menu 可重试。");
        }
        tracing::info!("Telegram 快捷菜单已同步：{} 个命令", desired.len());
        format!("快捷菜单已同步，共 {} 个命令。", desired.len())
    }

    /// 发送一条消息；永久拒绝或重试耗尽返回 false。
    pub async fn send(&self, text: &str) -> bool {
        let payload = serde_json::json!({"chat_id": self.telegram.chat_id, "text": text});
        for attempt in 1..=MAX_ATTEMPTS {
            match self
                .api
                .post_json(&self.url("sendMessage"), &payload, Duration::from_secs(20))
                .await
            {
                Ok((200, _)) => return true,
                Ok((status, body)) => {
                    if FATAL_STATUS.contains(&status) {
                        tracing::error!(
                            "Telegram 拒绝请求（{status}），重试无意义，请检查 bot_token 与 chat_id：{}",
                            first_line(&body)
                        );
                        return false;
                    }
                    tracing::warn!(
                        "Telegram 返回非 200（第 {attempt} 次）：{status} {}",
                        first_line(&body)
                    );
                }
                Err(e) => {
                    tracing::warn!("Telegram 请求异常（第 {attempt} 次）：{e}");
                }
            }
            if attempt < MAX_ATTEMPTS {
                tokio::time::sleep(Duration::from_secs_f64(self.backoff_secs * attempt as f64))
                    .await;
            }
        }
        false
    }
}

#[async_trait]
impl NotifierApi for Notifier {
    async fn send(&self, text: &str) -> bool {
        Notifier::send(self, text).await
    }

    async fn sync_commands(&self, commands: &[BotCommand]) -> String {
        Notifier::sync_commands(self, commands).await
    }

    async fn verify(&self) -> Result<(), TelegramFatalError> {
        Notifier::verify(self).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    /// 脚本化假 API：按序返回预设响应；记录全部 (url, payload)。
    struct MockApi {
        responses: Mutex<Vec<Result<(u16, String), String>>>,
        calls: Mutex<Vec<(String, serde_json::Value)>>,
    }

    impl MockApi {
        fn new(responses: Vec<Result<(u16, String), String>>) -> Arc<Self> {
            Arc::new(Self {
                responses: Mutex::new(responses),
                calls: Mutex::new(Vec::new()),
            })
        }

        fn calls(&self) -> Vec<(String, serde_json::Value)> {
            self.calls.lock().unwrap().clone()
        }
    }

    #[async_trait]
    impl TelegramApi for MockApi {
        async fn post_json(
            &self,
            url: &str,
            payload: &serde_json::Value,
            _timeout: Duration,
        ) -> Result<(u16, String), String> {
            self.calls
                .lock()
                .unwrap()
                .push((url.to_string(), payload.clone()));
            let mut responses = self.responses.lock().unwrap();
            match responses.len() {
                0 => Ok((200, r#"{"ok":true}"#.to_string())),
                _ => responses.remove(0),
            }
        }
    }

    fn notifier(api: Arc<MockApi>) -> Notifier {
        Notifier {
            telegram: TelegramConfig {
                bot_token: "tok".into(),
                chat_id: "chat".into(),
            },
            api,
            backoff_secs: 0.0,
        }
    }

    fn ok_body(result: &str) -> Result<(u16, String), String> {
        Ok((200, format!(r#"{{"ok":true,"result":{result}}}"#)))
    }

    #[test]
    fn test_format_messages() {
        let msg = format_change_message("商品A", "售罄", "充足", "2026-09-02T10:00:00+08:00", None);
        assert_eq!(
            msg,
            "【变更】商品A\n售罄 → 充足\n时间：2026-09-02T10:00:00+08:00"
        );

        let url = "https://e.com/order/123";
        let msg = format_change_message("商品A", "售罄", "充足", "t", Some(url));
        assert!(msg.ends_with(url));

        let msg = format_failure_message(
            "商品A",
            3,
            "导航失败：Timeout",
            "t",
            Some("https://e.com/page"),
        );
        assert!(msg.contains("连续 3 次抓取失败"));
        assert!(msg.ends_with("https://e.com/page"));

        let msg =
            format_new_post_message("HK 机房测评", "https://www.nodeseek.com/post-911200-1", "t");
        assert!(msg.contains("HK 机房测评"));
        assert!(msg.contains("https://www.nodeseek.com/post-911200-1"));
    }

    #[tokio::test]
    async fn test_send_success_and_payload() {
        let api = MockApi::new(vec![ok_body("true")]);
        let n = notifier(api.clone());
        assert!(n.send("hi").await);
        let calls = api.calls();
        assert_eq!(calls.len(), 1);
        assert!(calls[0].0.ends_with("/bottok/sendMessage"));
        assert_eq!(
            calls[0].1,
            serde_json::json!({"chat_id": "chat", "text": "hi"})
        );
    }

    #[tokio::test]
    async fn test_send_retries_then_fails() {
        let api = MockApi::new(vec![
            Ok((500, "boom".into())),
            Ok((500, "boom".into())),
            Ok((500, "boom".into())),
        ]);
        let n = notifier(api.clone());
        assert!(!n.send("hi").await);
        assert_eq!(api.calls().len(), 3);
    }

    #[tokio::test]
    async fn test_send_gives_up_on_permanent_error() {
        // 400 chat not found 是配置错误：只请求一次、不退避。
        let api = MockApi::new(vec![Ok((
            400,
            r#"{"ok":false,"error_code":400,"description":"Bad Request: chat not found"}"#.into(),
        ))]);
        let n = notifier(api.clone());
        assert!(!n.send("hi").await);
        assert_eq!(api.calls().len(), 1);
    }

    #[tokio::test]
    async fn test_verify_rejects_unreachable_chat() {
        let api = MockApi::new(vec![Ok((
            400,
            r#"{"ok":false,"error_code":400,"description":"Bad Request: chat not found"}"#.into(),
        ))]);
        let n = notifier(api.clone());
        let err = n.verify().await.unwrap_err();
        assert!(err.0.contains("chat not found"), "{err}");
        let calls = api.calls();
        assert!(calls[0].0.ends_with("/getChat"));
        assert_eq!(calls[0].1, serde_json::json!({"chat_id": "chat"}));
    }

    #[tokio::test]
    async fn test_verify_tolerates_transient_failure() {
        // 5xx / 网络异常不应阻止守护进程启动。
        let api = MockApi::new(vec![Ok((502, "bad gateway".into()))]);
        let n = notifier(api);
        n.verify().await.unwrap();

        let api = MockApi::new(vec![Err("connect error".into())]);
        let n = notifier(api);
        n.verify().await.unwrap();
    }

    fn menu() -> Vec<BotCommand> {
        vec![
            BotCommand {
                command: "add".into(),
                description: "新建监控".into(),
            },
            BotCommand {
                command: "help".into(),
                description: "显示命令说明".into(),
            },
        ]
    }

    fn menu_payload() -> serde_json::Value {
        serde_json::json!([
            {"command": "add", "description": "新建监控"},
            {"command": "help", "description": "显示命令说明"}
        ])
    }

    #[tokio::test]
    async fn test_sync_commands_creates_menu_when_absent() {
        let api = MockApi::new(vec![ok_body("[]"), ok_body("true")]);
        let n = notifier(api.clone());
        let reply = n.sync_commands(&menu()).await;
        let calls = api.calls();
        let paths: Vec<&str> = calls.iter().map(|(u, _)| u.as_str()).collect();
        assert!(paths[0].ends_with("/getMyCommands"));
        assert!(paths[1].ends_with("/setMyCommands"));
        assert_eq!(calls[1].1, serde_json::json!({"commands": menu_payload()}));
        assert!(reply.contains('2'));
    }

    #[tokio::test]
    async fn test_sync_commands_skips_write_when_unchanged() {
        let api = MockApi::new(vec![ok_body(&menu_payload().to_string())]);
        let n = notifier(api.clone());
        let reply = n.sync_commands(&menu()).await;
        let calls = api.calls();
        assert_eq!(calls.len(), 1);
        assert!(calls[0].0.ends_with("/getMyCommands"));
        assert!(reply.contains("已是最新"));
    }

    #[tokio::test]
    async fn test_sync_commands_rewrites_when_menu_drifted() {
        let stale = r#"[{"command":"add","description":"旧描述"}]"#;
        let api = MockApi::new(vec![ok_body(stale), ok_body("true")]);
        let n = notifier(api.clone());
        n.sync_commands(&menu()).await;
        let paths: Vec<String> = api.calls().into_iter().map(|(u, _)| u).collect();
        assert!(paths[0].ends_with("/getMyCommands"));
        assert!(paths[1].ends_with("/setMyCommands"));
    }

    #[tokio::test]
    async fn test_sync_commands_survives_telegram_rejection() {
        // Telegram 拒绝也只回执失败，绝不抛错拖垮启动（get/set 都返回 400）。
        let api = MockApi::new(vec![
            Ok((400, r#"{"ok":false}"#.into())),
            Ok((400, r#"{"ok":false}"#.into())),
        ]);
        let n = notifier(api);
        let reply = n.sync_commands(&menu()).await;
        assert!(reply.contains("失败"), "{reply}");
    }

    #[tokio::test]
    async fn test_sync_commands_survives_network_error() {
        let api = MockApi::new(vec![Err("boom".into())]);
        let n = notifier(api);
        let reply = n.sync_commands(&menu()).await;
        assert!(reply.contains("失败"), "{reply}");
    }

    #[test]
    fn test_redact() {
        assert_eq!(redact("a TOKEN b", &["TOKEN"]), "a <REDACTED> b");
        assert_eq!(redact("a b", &[""]), "a b");
    }
}
