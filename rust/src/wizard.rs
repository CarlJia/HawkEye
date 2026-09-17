//! 交互式配置向导（本机侧 `hawkeye init`）。
//!
//! 形状照 control.rs：全中文单行提示、输入不合法就地重问、结尾一次确认。只问
//! 最小可启动集——Telegram `bot_token` 与 `chat_id`；其余 `[[merchants]]` /
//! `[[watches]]` 在用户在 Telegram 里 `/add` 现场加，不在这里一并问。
//!
//! 实现纪律：
//!
//! - **不直接调** stdin/stdout，而是把 `ask` / `ask_secret` / `emit` 作为可调用
//!   对象注入——测试直接传替身，不依赖终端。
//! - **不直接落盘**。写盘一律经 [`crate::configedit::write_config`]，保留其
//!   「先验证、后替换」事务、600 权限与备份修剪。
//! - **凭据自检先于写盘**。永久拒绝（400/401/403/404）就地重问；网络异常由
//!   verify 自己降级为告警；跳过自检的用户完全不走网络。
//! - **目标 TOML 已坏时不静默清空**——如实上抛。

use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::config::{TelegramConfig, load_raw};
use crate::configedit::write_config;
use crate::notify::{Notifier, TelegramApi};

const YES: &[&str] = &["1", "y", "yes", "是", "确认", "确定", "保存"];
const TOKEN_TAIL_LEN: usize = 4; // 摘要里 token 只露尾部若干位

pub trait Ask: Send + Sync {
    fn ask(&mut self, prompt: &str) -> String;
    fn ask_secret(&mut self, prompt: &str) -> String;
}

/// 默认终端实现：普通输入走 stdin；密码走 rpassword 隐藏输入。
pub struct TerminalAsk;

impl Ask for TerminalAsk {
    fn ask(&mut self, prompt: &str) -> String {
        use std::io::Write;
        print!("{prompt}");
        let _ = std::io::stdout().flush();
        let mut line = String::new();
        let _ = std::io::stdin().read_line(&mut line);
        line.trim_end_matches(['\n', '\r']).to_string()
    }

    fn ask_secret(&mut self, prompt: &str) -> String {
        rpassword::prompt_password(prompt).unwrap_or_default()
    }
}

fn is_yes(value: &str) -> bool {
    YES.contains(&value.trim().to_lowercase().as_str())
}

/// 读目标文件为 raw table；不存在返回空表；TOML 语法损坏如实抛。
fn load_existing(path: &Path) -> Result<toml::Table, crate::config::ConfigError> {
    if !path.exists() {
        return Ok(toml::Table::new());
    }
    // 故意不兜成空表：那会把坏 TOML 里的监控静默清空。
    load_raw(path)
}

/// 只替换 telegram 两个键，其余原样带过。
fn apply_telegram(raw: &toml::Table, bot_token: &str, chat_id: &str) -> toml::Table {
    let mut new_raw = raw.clone();
    let mut tg = new_raw
        .get("telegram")
        .and_then(|v| v.as_table())
        .cloned()
        .unwrap_or_default();
    tg.insert(
        "bot_token".into(),
        toml::Value::String(bot_token.to_string()),
    );
    tg.insert("chat_id".into(), toml::Value::String(chat_id.to_string()));
    new_raw.insert("telegram".into(), toml::Value::Table(tg));
    new_raw
}

// ---- 单步问答 ----

fn ask_bot_token(ask: &mut dyn Ask) -> String {
    loop {
        let value = ask
            .ask_secret("请输入 Telegram bot_token（输入隐藏）：")
            .trim()
            .to_string();
        if !value.is_empty() {
            return value;
        }
        println!("bot_token 不能为空，请重新输入。");
    }
}

fn ask_chat_id(ask: &mut dyn Ask) -> String {
    // chat_id 允许负数（群聊 supergroup id 为负）。空串重问。
    loop {
        let value = ask
            .ask("请输入 Telegram chat_id（个人或群，群 id 通常为负数）：")
            .trim()
            .to_string();
        if !value.is_empty() {
            return value;
        }
        println!("chat_id 不能为空，请重新输入。");
    }
}

fn ask_verify(ask: &mut dyn Ask) -> bool {
    loop {
        let value = ask
            .ask("是否现在做一次 Telegram 凭据自检？(y/N，回车跳过)：")
            .trim()
            .to_lowercase();
        if value.is_empty() || value == "n" || value == "no" || value == "否" {
            return false;
        }
        if value == "y" || is_yes(&value) {
            return true;
        }
        println!("请回复 y 或 n（回车等同 n）。");
    }
}

fn ask_confirm_save(ask: &mut dyn Ask, bot_token: &str, chat_id: &str) -> bool {
    println!();
    println!("即将写入以下内容：");
    println!("  bot_token = {}", format_token_tail(bot_token));
    println!("  chat_id   = {chat_id}");
    loop {
        let value = ask.ask("确认保存？(y/N，回车取消)：").trim().to_lowercase();
        if value.is_empty() || value == "n" || value == "no" || value == "否" {
            return false;
        }
        if value == "y" || is_yes(&value) {
            return true;
        }
        println!("请回复 y 或 n（回车等同 n）。");
    }
}

// ---- Telegram 自检 ----

/// api 为 None 时走真实 reqwest；测试注入脚本化假 API。
async fn verify(bot_token: &str, chat_id: &str, api: Option<Arc<dyn TelegramApi>>) -> bool {
    let telegram = TelegramConfig {
        bot_token: bot_token.to_string(),
        chat_id: chat_id.to_string(),
    };
    let notifier = match api {
        Some(api) => Notifier::with_api(telegram, api),
        None => Notifier::new(telegram),
    };
    notifier.verify().await.is_ok()
}

async fn collect_credentials(
    ask: &mut dyn Ask,
    api: Option<Arc<dyn TelegramApi>>,
) -> (String, String) {
    // 自检失败就地重问。
    loop {
        let bot_token = ask_bot_token(ask);
        let chat_id = ask_chat_id(ask);
        if ask_verify(ask) {
            if verify(&bot_token, &chat_id, api.clone()).await {
                return (bot_token, chat_id);
            }
            println!("Telegram 拒绝了 bot_token 或 chat_id，请重新填写。");
            continue;
        }
        return (bot_token, chat_id);
    }
}

// ---- 摘要 ----

fn format_token_tail(token: &str) -> String {
    if token.chars().count() <= TOKEN_TAIL_LEN {
        return "***（已隐藏）".to_string();
    }
    let tail: String = token
        .chars()
        .skip(token.chars().count() - TOKEN_TAIL_LEN)
        .collect();
    format!("***{tail}")
}

fn print_summary(emit: &mut dyn FnMut(&str), path: &Path, bot_token: &str, chat_id: &str) {
    let perm_text = std::fs::metadata(path)
        .ok()
        .map(|m| {
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let mode = m.permissions().mode() & 0o777;
                if mode == 0o600 {
                    "600（仅当前用户可读写）".to_string()
                } else {
                    format!("{mode:o}")
                }
            }
            #[cfg(not(unix))]
            {
                "（非 POSIX 平台）".to_string()
            }
        })
        .unwrap_or_else(|| "无法读取".to_string());
    emit("");
    emit("已写入配置：");
    emit(&format!("  路径       {}", path.display()));
    emit(&format!("  权限       {perm_text}"));
    emit(&format!("  bot_token  {}", format_token_tail(bot_token)));
    emit(&format!("  chat_id    {chat_id}"));
    emit("");
    emit("下一步：运行 `hawkeye deploy` 把这份配置与程序部署到 VPS。");
}

// ---- 公开入口 ----

/// 交互式向导主入口。
///
/// `run_verify`：是否走 Telegram 自检；离线场景可关掉。
/// 返回 Err 时上层映射为退出码 1；用户取消返回 Ok(false)。
pub async fn run_wizard(
    path: &Path,
    ask: &mut dyn Ask,
    emit: &mut dyn FnMut(&str),
    run_verify: bool,
    api: Option<Arc<dyn TelegramApi>>,
) -> Result<bool, crate::configedit::EditError> {
    let target: PathBuf = path.to_path_buf();
    emit(&format!(
        "开始配置 {}（Ctrl-C 随时中止，未确认前不会落盘）",
        target.display()
    ));

    let raw = load_existing(&target)?;

    let (bot_token, chat_id) = if run_verify {
        collect_credentials(ask, api).await
    } else {
        (ask_bot_token(ask), ask_chat_id(ask))
    };

    if !ask_confirm_save(ask, &bot_token, &chat_id) {
        emit("已取消，未写入任何内容。");
        return Ok(false);
    }

    let new_raw = apply_telegram(&raw, &bot_token, &chat_id);
    write_config(&target, &new_raw)?;
    print_summary(emit, &target, &bot_token, &chat_id);
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use async_trait::async_trait;
    use std::sync::Mutex;

    /// 按预定义序列依次返回输入；用完仍被调则抛错（防交互死循环漏检）。
    struct ScriptedAsk {
        answers: Mutex<Vec<String>>,
        asked: Mutex<Vec<String>>,
    }

    impl ScriptedAsk {
        fn new(answers: &[&str]) -> Self {
            Self {
                answers: Mutex::new(answers.iter().map(|s| s.to_string()).collect()),
                asked: Mutex::new(Vec::new()),
            }
        }
    }

    impl Ask for ScriptedAsk {
        fn ask(&mut self, prompt: &str) -> String {
            self.asked.lock().unwrap().push(prompt.to_string());
            let mut answers = self.answers.lock().unwrap();
            answers
                .pop()
                .unwrap_or_else(|| panic!("向导多要了一次输入：{prompt:?}"))
        }

        fn ask_secret(&mut self, prompt: &str) -> String {
            self.ask(prompt)
        }
    }

    fn tmp_path(tag: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("hawkeye_wiz_test_{}_{}", std::process::id(), tag));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir.join("config.toml")
    }

    #[test]
    fn test_apply_telegram_only_replaces_two_keys() {
        let mut raw = toml::Table::new();
        raw.insert("poll_interval_secs".into(), toml::Value::Integer(90));
        let mut tg = toml::Table::new();
        tg.insert("bot_token".into(), toml::Value::String("OLD".into()));
        tg.insert("chat_id".into(), toml::Value::String("OLD_CHAT".into()));
        raw.insert("telegram".into(), toml::Value::Table(tg));

        let new = apply_telegram(&raw, "NEW", "1");
        assert_eq!(
            new.get("poll_interval_secs").and_then(|v| v.as_integer()),
            Some(90),
            "其他键原样带过"
        );
        let tg = new.get("telegram").and_then(|v| v.as_table()).unwrap();
        assert_eq!(tg.get("bot_token").and_then(|v| v.as_str()), Some("NEW"));
        assert_eq!(tg.get("chat_id").and_then(|v| v.as_str()), Some("1"));
    }

    #[test]
    fn test_apply_telegram_creates_telegram_when_missing() {
        let raw = toml::Table::new();
        let new = apply_telegram(&raw, "T", "C");
        let tg = new.get("telegram").and_then(|v| v.as_table()).unwrap();
        assert_eq!(tg.len(), 2);
    }

    #[test]
    fn test_load_existing_returns_empty_when_absent() {
        let raw = load_existing(Path::new("/nonexistent/nowhere.toml")).unwrap();
        assert!(raw.is_empty());
    }

    #[test]
    fn test_load_existing_does_not_swallow_broken_toml() {
        let p = tmp_path("broken");
        std::fs::write(&p, "这 = 不是 TOML {{{").unwrap();
        assert!(load_existing(&p).is_err(), "坏 TOML 必须如实上抛");
        let _ = std::fs::remove_dir_all(p.parent().unwrap());
    }

    #[test]
    fn test_ask_bot_token_retries_on_empty() {
        let mut ask = ScriptedAsk::new(&["token-123", "", ""]);
        // 答案从队尾弹出：先空两次、再给真值。
        let value = ask_bot_token(&mut ask);
        assert_eq!(value, "token-123");
        assert_eq!(ask.asked.lock().unwrap().len(), 3);
    }

    #[test]
    fn test_ask_chat_id_retries_on_empty_and_accepts_negative() {
        let mut ask = ScriptedAsk::new(&["-100200", "", ""]);
        let value = ask_chat_id(&mut ask);
        assert_eq!(value, "-100200");
    }

    /// 脚本化 Telegram API：按序返回响应。
    struct MockApi {
        responses: Mutex<Vec<Result<(u16, String), String>>>,
    }

    #[async_trait]
    impl TelegramApi for MockApi {
        async fn post_json(
            &self,
            _url: &str,
            _payload: &serde_json::Value,
            _timeout: std::time::Duration,
        ) -> Result<(u16, String), String> {
            let mut responses = self.responses.lock().unwrap();
            match responses.len() {
                0 => Ok((200, r#"{"ok":true}"#.into())),
                _ => responses.remove(0),
            }
        }
    }

    #[tokio::test]
    async fn test_collect_retries_when_telegram_rejects() {
        // 首轮 400（永久拒绝）→ 就地重问；第二轮 200 → 通过。
        let api = Arc::new(MockApi {
            responses: Mutex::new(vec![
                Ok((
                    400,
                    r#"{"description":"Bad Request: chat not found"}"#.into(),
                )),
                Ok((200, r#"{"ok":true}"#.into())),
            ]),
        });
        // 输入序列（从队尾弹出）：确认保存 n 不需要——collect 只问到 chat_id 为止。
        // 顺序：token1, chat1, y(自检), token2, chat2, y(自检)
        let mut ask = ScriptedAsk::new(&["y", "chat2", "token2", "y", "chat1", "token1"]);
        let (token, chat) = collect_credentials(&mut ask, Some(api)).await;
        assert_eq!(token, "token2", "被拒后应重问");
        assert_eq!(chat, "chat2");
    }

    #[tokio::test]
    async fn test_collect_returns_on_network_error() {
        // 网络异常由 verify 降级为通过，不阻断。
        let api = Arc::new(MockApi {
            responses: Mutex::new(vec![Err("connect error".into())]),
        });
        let mut ask = ScriptedAsk::new(&["y", "chat1", "token1"]);
        let (token, chat) = collect_credentials(&mut ask, Some(api)).await;
        assert_eq!(token, "token1");
        assert_eq!(chat, "chat1");
    }

    async fn run(
        path: &Path,
        answers: &[&str],
        run_verify: bool,
    ) -> (Result<bool, crate::configedit::EditError>, Vec<String>) {
        let mut ask = ScriptedAsk::new(answers);
        let mut lines = Vec::new();
        let result = run_wizard(
            path,
            &mut ask,
            &mut |m: &str| lines.push(m.to_string()),
            run_verify,
            None,
        )
        .await;
        (result, lines)
    }

    #[tokio::test]
    async fn test_wizard_preserves_other_keys_on_existing_file() {
        let p = tmp_path("preserve");
        std::fs::write(
            &p,
            "poll_interval_secs = 90\n\n[telegram]\nbot_token = \"OLD\"\nchat_id = \"1\"\n\n[[watches]]\nurl = \"https://e.com\"\nlink_selector = \"a\"\nkeywords = [\"k\"]\n",
        )
        .unwrap();
        // 输入（队尾弹出序）：确认保存 y ← chat ← token。
        let (result, _) = run(&p, &["y", "-100", "tok-123456"], false).await;
        result.unwrap();
        let cfg = crate::config::load_config(&p).unwrap();
        assert_eq!(cfg.poll_interval_secs, 90, "其他键原样带过");
        assert_eq!(cfg.telegram.bot_token, "tok-123456");
        assert_eq!(cfg.telegram.chat_id, "-100");
        assert_eq!(cfg.watches.len(), 1);
        let _ = std::fs::remove_dir_all(p.parent().unwrap());
    }

    #[tokio::test]
    async fn test_wizard_creates_file_when_absent() {
        let p = tmp_path("absent");
        let (result, _) = run(&p, &["y", "1", "tok"], false).await;
        assert!(result.unwrap());
        assert!(p.exists());
        let cfg = crate::config::load_config(&p).unwrap();
        assert_eq!(cfg.telegram.bot_token, "tok");
        let _ = std::fs::remove_dir_all(p.parent().unwrap());
    }

    #[tokio::test]
    async fn test_wizard_backs_up_existing_file() {
        let p = tmp_path("backup");
        std::fs::write(&p, "[telegram]\nbot_token = \"OLD\"\nchat_id = \"1\"\n").unwrap();
        let (result, _) = run(&p, &["y", "1", "NEW"], false).await;
        result.unwrap();
        // 备份已生成且含旧 token。
        let dir = p.parent().unwrap();
        let prefix = format!("{}.", p.file_name().unwrap().to_string_lossy());
        let mut found = false;
        for e in std::fs::read_dir(dir).unwrap().flatten() {
            let name = e.file_name().to_string_lossy().to_string();
            if name.starts_with(&format!("{prefix}bak.")) {
                assert!(std::fs::read_to_string(e.path()).unwrap().contains("OLD"));
                found = true;
            }
        }
        assert!(found, "写盘前应留备份");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[tokio::test]
    async fn test_wizard_summary_does_not_leak_full_token() {
        let p = tmp_path("leak");
        let (result, lines) = run(&p, &["y", "1", "8428922140:AA-fake-token"], false).await;
        result.unwrap();
        let summary = lines.join("\n");
        assert!(summary.contains("***"), "摘要应只露尾部：{summary}");
        assert!(
            !summary.contains("8428922140:AA-fake-token"),
            "完整 token 绝不能出现在摘要"
        );
        let _ = std::fs::remove_dir_all(p.parent().unwrap());
    }

    #[tokio::test]
    async fn test_wizard_cancel_does_not_write() {
        let p = tmp_path("cancel");
        let (result, _) = run(&p, &["n", "1", "tok"], false).await;
        assert!(!result.unwrap(), "用户取消返回 false");
        assert!(!p.exists(), "取消时不落盘");
        let _ = std::fs::remove_dir_all(p.parent().unwrap());
    }

    #[tokio::test]
    async fn test_wizard_propagates_broken_toml() {
        let p = tmp_path("prop");
        std::fs::write(&p, "坏文件 {{{").unwrap();
        // 还没问任何输入就该失败——ScriptedAsk 用完即抛,不会死循环。
        let (result, _) = run(&p, &["y", "1", "tok"], false).await;
        assert!(result.is_err(), "坏 TOML 必须上抛而不是静默清空");
        let _ = std::fs::remove_dir_all(p.parent().unwrap());
    }

    #[tokio::test]
    async fn test_wizard_can_skip_verification() {
        let p = tmp_path("skip");
        let (result, _) = run(&p, &["y", "1", "tok"], false).await;
        assert!(result.unwrap());
        assert!(p.exists());
        let _ = std::fs::remove_dir_all(p.parent().unwrap());
    }

    #[test]
    fn test_summary_contains_tail_and_path() {
        let p = tmp_path("summary");
        std::fs::write(&p, "[telegram]\nbot_token = \"t\"\nchat_id = \"c\"\n").unwrap();
        let mut lines = Vec::new();
        print_summary(
            &mut |m: &str| lines.push(m.to_string()),
            &p,
            "1234567890:ABC",
            "42",
        );
        let text = lines.join("\n");
        assert!(text.contains(":ABC"), "{text}");
        assert!(text.contains("42"));
        assert!(text.contains(&p.display().to_string()));
        let _ = std::fs::remove_dir_all(p.parent().unwrap());
    }
}
