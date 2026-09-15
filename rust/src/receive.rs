//! Telegram 命令接收（getUpdates 长轮询）。
//!
//! 只做三件事：长轮询取回 update、把**授权会话**的文本消息转成 [`Command`]、
//! 在异常时退避重试而不拖垮进程。命令语义一概不懂，交给上层分发。
//!
//! 两条容易踩的坑，都在本层挡掉：
//!
//! - **游标必须无条件推进。** 被忽略的消息（非授权来源、非文本）如果不推进 offset，
//!   Telegram 每轮都会把同一条 update 再发一遍，长轮询从此永久卡死。
//! - **启动先丢积压。** 守护进程重启后不该去执行几小时前的 /add。`offset=-1`
//!   只取最后一条 update，把游标推到它之后即等于跳过全部积压。

use std::sync::Arc;
use std::time::Duration;

use serde_json::Value;

use crate::notify::{TelegramApi, TelegramFatalError};

const LONG_POLL_SECS: u64 = 30;
const DEFAULT_BASE_BACKOFF: f64 = 2.0;
const MAX_BACKOFF_SECS: f64 = 60.0;
/// 401/404 token 有误、403 被封禁：重试不会变好，交给 main 走退出码 2。
const FATAL_STATUS: [u16; 3] = [401, 403, 404];

/// 来自授权会话的一条文本命令（来源校验已在本层完成，上层只管正文）。
#[derive(Debug, Clone, PartialEq)]
pub struct Command {
    pub text: String,
}

pub struct Receiver {
    api: Arc<dyn TelegramApi>,
    token: String,
    chat_id: String,
    offset: Option<i64>,
    /// 退避基数（秒）。测试可调小免真实等待。
    backoff_base: f64,
}

/// 接收循环错误：Http 走退避重试，Fatal 上抛终止进程。
#[derive(Debug, thiserror::Error)]
pub enum ReceiveError {
    #[error("{0}")]
    Http(#[source] anyhow::Error),
    #[error("{0}")]
    Fatal(#[from] TelegramFatalError),
}

impl Receiver {
    pub fn new(token: &str, chat_id: &str) -> Self {
        Self::with_api(token, chat_id, Arc::new(crate::notify::ReqwestApi::new()))
    }

    pub fn with_api(token: &str, chat_id: &str, api: Arc<dyn TelegramApi>) -> Self {
        Self {
            api,
            token: token.to_string(),
            chat_id: chat_id.to_string(),
            offset: None,
            backoff_base: DEFAULT_BASE_BACKOFF,
        }
    }

    fn url(&self, method: &str) -> String {
        format!("https://api.telegram.org/bot{}/{method}", self.token)
    }

    async fn get_updates(
        &self,
        params: &Value,
        request_timeout: Duration,
    ) -> Result<Vec<Value>, ReceiveError> {
        let (status, body) = self
            .api
            .post_json(&self.url("getUpdates"), params, request_timeout)
            .await
            .map_err(|e| ReceiveError::Http(anyhow::anyhow!("{e}")))?;
        if FATAL_STATUS.contains(&status) {
            return Err(ReceiveError::Fatal(TelegramFatalError(format!(
                "Telegram 拒绝 getUpdates（{status}），请检查 bot_token：{}",
                &body[..body.len().min(200)]
            ))));
        }
        if !(200..300).contains(&status) {
            return Err(ReceiveError::Http(anyhow::anyhow!(
                "getUpdates 返回 {status}"
            )));
        }
        let payload: Value = serde_json::from_str(&body)
            .map_err(|e| ReceiveError::Fatal(TelegramFatalError(format!("Telegram 返回了无效的 JSON：{e}"))))?;
        let result = payload
            .get("result")
            .and_then(|r| r.as_array())
            .cloned()
            .unwrap_or_default();
        Ok(result.into_iter().filter(|u| u.is_object()).collect())
    }

    /// 丢弃启动前积压的消息，只把游标推到最后一条之后。
    pub async fn drain_backlog(&mut self) -> Result<(), ReceiveError> {
        let updates = self
            .get_updates(
                &serde_json::json!({"offset": -1, "timeout": 0}),
                Duration::from_secs(20),
            )
            .await?;
        let Some(last) = updates.last() else {
            return Ok(());
        };
        if let Some(update_id) = last.get("update_id").and_then(|v| v.as_i64()) {
            self.offset = Some(update_id + 1);
            tracing::info!("已丢弃启动前的积压消息，接收游标推进至 {}", update_id + 1);
        }
        Ok(())
    }

    /// 长轮询一次，返回本轮采纳的命令（未采纳的消息同样推进游标）。
    pub async fn poll(&mut self) -> Result<Vec<Command>, ReceiveError> {
        let mut params = serde_json::json!({"timeout": LONG_POLL_SECS, "allowed_updates": ["message"]});
        if let Some(offset) = self.offset {
            params["offset"] = serde_json::json!(offset);
        }
        // 请求超时必须大于长轮询时长，否则每轮都被判超时。
        let updates = self
            .get_updates(&params, Duration::from_secs(LONG_POLL_SECS + 10))
            .await?;

        let mut commands = Vec::new();
        for update in &updates {
            if let Some(update_id) = update.get("update_id").and_then(|v| v.as_i64()) {
                self.offset = Some(update_id + 1);
            }
            if let Some(command) = self.to_command(update) {
                commands.push(command);
            }
        }
        Ok(commands)
    }

    fn to_command(&self, update: &Value) -> Option<Command> {
        let message = update.get("message")?;
        let chat_id = message
            .get("chat")
            .and_then(|c| c.get("id"))
            .and_then(|v| v.as_i64())
            .map(|v| v.to_string());
        let Some(chat_id) = chat_id else {
            tracing::warn!("忽略非授权会话的消息：chat_id 不可读");
            return None;
        };
        if chat_id != self.chat_id {
            // 非授权来源静默丢弃、绝不回复，免得成了陌生人的回声探测器。
            tracing::warn!("忽略非授权会话的消息：chat_id={chat_id}");
            return None;
        }
        let text = message.get("text").and_then(|v| v.as_str()).map(|s| s.trim());
        match text {
            Some(t) if !t.is_empty() => Some(Command { text: t.to_string() }),
            _ => {
                tracing::debug!("忽略非文本消息");
                None
            }
        }
    }

    /// 常驻接收循环：先丢积压，再逐轮长轮询并把命令交给 on_command。
    pub async fn run<F, Fut>(
        &mut self,
        mut on_command: F,
        mut stop: tokio::sync::watch::Receiver<bool>,
    ) -> Result<(), ReceiveError>
    where
        F: FnMut(Command) -> Fut,
        Fut: std::future::Future<Output = ()>,
    {
        let mut backoff = self.backoff_base;
        let mut drained = false;
        loop {
            if *stop.borrow() {
                break;
            }
            // 长轮询与停止事件竞速：收到停止就丢下在途请求，不必空等满 30 秒。
            let poll = async {
                if !drained {
                    self.drain_backlog().await?;
                    drained = true;
                }
                self.poll().await
            };
            let result = tokio::select! {
                r = poll => r,
                _ = stop.changed() => break,
            };
            let commands = match result {
                Ok(c) => c,
                Err(ReceiveError::Http(e)) => {
                    tracing::warn!("命令接收请求异常，{backoff:.0} 秒后重试：{e}");
                    sleep_or_stop(Duration::from_secs_f64(backoff), &mut stop).await;
                    backoff = (backoff * 2.0).min(MAX_BACKOFF_SECS);
                    continue;
                }
                Err(ReceiveError::Fatal(e)) => return Err(ReceiveError::Fatal(e)),
            };
            backoff = self.backoff_base;
            for command in commands {
                if *stop.borrow() {
                    break;
                }
                on_command(command).await;
            }
        }
        tracing::info!("命令接收循环已退出");
        Ok(())
    }
}

async fn sleep_or_stop(delay: Duration, stop: &mut tokio::sync::watch::Receiver<bool>) {
    tokio::select! {
        _ = tokio::time::sleep(delay) => {}
        _ = stop.changed() => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use async_trait::async_trait;
    use crate::notify::ReqwestApi;
    use std::sync::Mutex;

    /// 脚本化假 API 的响应：立即返回，或模拟长轮询挂起。
    enum MockResponse {
        Immediate(Result<(u16, String), String>),
        Hang,
    }

    /// 脚本化假 API：闭包按调用序给出响应；记录全部 payload。
    struct MockApi {
        handler: Box<dyn Fn(usize, &serde_json::Value) -> MockResponse + Send + Sync>,
        calls: Mutex<Vec<serde_json::Value>>,
    }

    impl MockApi {
        fn new(
            handler: impl Fn(usize, &serde_json::Value) -> Result<(u16, String), String>
                + Send
                + Sync
                + 'static,
        ) -> Arc<Self> {
            Arc::new(Self {
                handler: Box::new(move |n, p| MockResponse::Immediate(handler(n, p))),
                calls: Mutex::new(Vec::new()),
            })
        }

        fn hanging(
            handler: impl Fn(usize, &serde_json::Value) -> MockResponse + Send + Sync + 'static,
        ) -> Arc<Self> {
            Arc::new(Self {
                handler: Box::new(handler),
                calls: Mutex::new(Vec::new()),
            })
        }

        fn bodies(&self) -> Vec<serde_json::Value> {
            self.calls.lock().unwrap().clone()
        }
    }

    #[async_trait]
    impl TelegramApi for MockApi {
        async fn post_json(
            &self,
            _url: &str,
            payload: &serde_json::Value,
            _timeout: Duration,
        ) -> Result<(u16, String), String> {
            let n = self.calls.lock().unwrap().len();
            self.calls.lock().unwrap().push(payload.clone());
            match (self.handler)(n, payload) {
                MockResponse::Immediate(r) => r,
                MockResponse::Hang => {
                    // 模拟挂起的长轮询：远超测试窗口，异步等待不阻塞调度。
                    tokio::time::sleep(Duration::from_secs(30)).await;
                    Ok((200, ok(vec![])))
                }
            }
        }
    }

    const CHAT: &str = "42";

    fn update(update_id: i64, chat_id: Value, text: Option<&str>) -> Value {
        let mut message = serde_json::json!({"message_id": update_id, "chat": {"id": chat_id}});
        if let Some(text) = text {
            message["text"] = serde_json::json!(text);
        }
        serde_json::json!({"update_id": update_id, "message": message})
    }

    fn ok(updates: Vec<Value>) -> String {
        serde_json::json!({"ok": true, "result": updates}).to_string()
    }

    fn receiver(api: Arc<MockApi>) -> Receiver {
        Receiver {
            api,
            token: "tok".into(),
            chat_id: CHAT.into(),
            offset: None,
            backoff_base: 0.001,
        }
    }

    #[tokio::test]
    async fn test_authorized_text_becomes_command() {
        let api = MockApi::new(|_, _| Ok((200, ok(vec![update(1, json_num(42), Some(" /list "))]))));
        let mut r = receiver(api);
        assert_eq!(r.poll().await.unwrap(), vec![Command { text: "/list".into() }]);
    }

    fn json_num(v: i64) -> Value {
        serde_json::json!(v)
    }

    #[tokio::test]
    async fn test_unauthorized_chat_is_dropped_without_reply() {
        let api = MockApi::new(|_, _| Ok((200, ok(vec![update(7, json_num(999), Some("/del 1"))]))));
        let mut r = receiver(api.clone());
        assert!(r.poll().await.unwrap().is_empty());
        // 全程只有一次 getUpdates，没有任何 sendMessage。
        assert_eq!(api.bodies().len(), 1);
    }

    #[tokio::test]
    async fn test_non_text_message_is_ignored() {
        let update = serde_json::json!({
            "update_id": 3,
            "message": {"chat": {"id": 42}, "photo": [{"file_id": "x"}]}
        });
        let api = MockApi::new(move |_, _| Ok((200, ok(vec![update.clone()]))));
        let mut r = receiver(api);
        assert!(r.poll().await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn test_offset_advances_even_for_ignored_updates() {
        let api = MockApi::new(|n, _| {
            if n == 0 {
                Ok((200, ok(vec![
                    update(10, json_num(999), Some("/del 1")),
                    update(11, json_num(42), Some("/help")),
                ])))
            } else {
                Ok((200, ok(vec![])))
            }
        });
        let mut r = receiver(api.clone());
        assert_eq!(
            r.poll().await.unwrap(),
            vec![Command { text: "/help".into() }]
        );
        assert!(r.poll().await.unwrap().is_empty());
        let bodies = api.bodies();
        assert!(bodies[0].get("offset").is_none(), "首轮没有游标可带");
        assert_eq!(bodies[0]["timeout"], 30);
        assert_eq!(bodies[0]["allowed_updates"], serde_json::json!(["message"]));
        assert_eq!(bodies[1]["offset"], 12, "被忽略的 10 同样推进了");
    }

    #[tokio::test]
    async fn test_drain_backlog_skips_pending_updates() {
        let api = MockApi::new(|_, payload| {
            if payload.get("offset").and_then(|v| v.as_i64()) == Some(-1) {
                Ok((200, ok(vec![
                    update(4, json_num(42), Some("/del 1")),
                    update(5, json_num(42), Some("/add")),
                ])))
            } else {
                Ok((200, ok(vec![])))
            }
        });
        let mut r = receiver(api.clone());
        r.drain_backlog().await.unwrap();
        assert!(r.poll().await.unwrap().is_empty());
        let bodies = api.bodies();
        assert_eq!(
            bodies[0],
            serde_json::json!({"offset": -1, "timeout": 0})
        );
        assert_eq!(bodies[1]["offset"], 6, "直接跳到积压最后一条之后");
    }

    #[tokio::test]
    async fn test_drain_backlog_without_backlog_leaves_offset_unset() {
        let api = MockApi::new(|_, _| Ok((200, ok(vec![]))));
        let mut r = receiver(api.clone());
        r.drain_backlog().await.unwrap();
        r.poll().await.unwrap();
        let bodies = api.bodies();
        assert!(bodies[1].get("offset").is_none());
    }

    #[tokio::test]
    async fn test_fatal_status_raises_telegram_fatal_error() {
        let api = MockApi::new(|_, _| Ok((401, "Unauthorized".into())));
        let mut r = receiver(api);
        let err = r.poll().await.unwrap_err();
        match err {
            ReceiveError::Fatal(e) => assert!(e.0.contains("401"), "{}", e.0),
            other => panic!("应为 Fatal：{other:?}"),
        }
    }

    #[tokio::test]
    async fn test_run_propagates_fatal_status() {
        let api = MockApi::new(|_, _| Ok((403, "Forbidden".into())));
        let mut r = receiver(api);
        let (_tx, stop_rx) = tokio::sync::watch::channel(false);
        let err = r
            .run(|_| async { panic!("不应有命令产出") }, stop_rx)
            .await
            .unwrap_err();
        match err {
            ReceiveError::Fatal(e) => assert!(e.0.contains("403"), "{}", e.0),
            other => panic!("应为 Fatal：{other:?}"),
        }
    }

    #[tokio::test]
    async fn test_run_backs_off_and_survives_network_errors() {
        // 网络抖动不该拖垮进程：退避、恢复后照常收命令。
        let api = MockApi::new(|n, payload| {
            if n <= 7 {
                Err("boom".into())
            } else if payload.get("offset").and_then(|v| v.as_i64()) == Some(-1) {
                Ok((200, ok(vec![])))
            } else {
                Ok((200, ok(vec![update(9, json_num(42), Some("/list"))])))
            }
        });
        let mut r = receiver(api);
        let (stop_tx, stop_rx) = tokio::sync::watch::channel(false);
        let got = Arc::new(Mutex::new(Vec::new()));
        let got_clone = Arc::clone(&got);
        let stop_tx = Arc::new(stop_tx);
        let stop_clone = Arc::clone(&stop_tx);
        r.run(
            move |cmd| {
                let got = Arc::clone(&got_clone);
                let stop = Arc::clone(&stop_clone);
                async move {
                    got.lock().unwrap().push(cmd);
                    let _ = stop.send(true);
                }
            },
            stop_rx,
        )
        .await
        .unwrap();
        assert_eq!((*got.lock().unwrap()).clone(), vec![Command { text: "/list".into() }]);
        drop(stop_tx);
    }

    #[tokio::test]
    async fn test_run_returns_promptly_while_long_poll_in_flight() {
        // 停止时长轮询正挂在途，必须立刻放弃它，而不是空等满一个 30 秒窗口。
        let in_flight = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let flag = Arc::clone(&in_flight);
        let api = MockApi::hanging(move |_, payload| {
            if payload.get("offset").and_then(|v| v.as_i64()) == Some(-1) {
                return MockResponse::Immediate(Ok((200, ok(vec![]))));
            }
            flag.store(true, std::sync::atomic::Ordering::SeqCst);
            MockResponse::Hang
        });
        let mut r = receiver(api);
        let (stop_tx, stop_rx) = tokio::sync::watch::channel(false);
        let runner = tokio::spawn(async move {
            r.run(|_| async { panic!("不应有命令产出") }, stop_rx)
                .await
                .unwrap()
        });
        // 等第一轮长轮询挂起后立即停止。
        while !in_flight.load(std::sync::atomic::Ordering::SeqCst) {
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        stop_tx.send(true).unwrap();
        tokio::time::timeout(Duration::from_secs(1), runner)
            .await
            .expect("停止后应立刻退出，而不是等满长轮询窗口")
            .unwrap();
    }

    #[test]
    fn test_default_api_is_reqwest() {
        // 默认构造走 reqwest 传输层（冒烟：类型可构造）。
        let _ = Receiver::new("tok", "1");
        let _ = ReqwestApi::new();
    }
}
