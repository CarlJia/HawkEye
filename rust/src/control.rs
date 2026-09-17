//! Telegram 命令分发与引导式向导（控制面）。
//!
//! 把一条条纯文本命令翻译成对 config.toml 的原子改写与调度器的即时协调。三条纪律：
//!
//! - **单锁串行。** 「读文件 → 变换 → 写回 → reconcile」全程在一把锁内完成；
//!   命令发得再快也不会有两条同时改配置。
//! - **配置文件是唯一真相。** 每条命令先 [`Controller::sync`] 把外部手改同步进
//!   运行时，再在最新内容上做变换；写入一律走 `configedit::write_config` 的
//!   「先验证、后替换」事务，失败时原文件字节不变。
//! - **会话只在内存。** 多轮向导的中间态随进程消失，重启后半截的 /add 自然作废。
//!
//! 回执一律纯文本、不带 parse_mode：URL 与选择器里的下划线、星号、方括号在
//! Markdown 解析下会被吞掉。

use std::path::{Path, PathBuf};
use std::sync::Arc;

use tokio::sync::Mutex;

use crate::config::{MonitoredElement, Page, WatchTarget, is_http_url, load_config, load_raw};
use crate::configedit::{
    EditError, add_element, add_watch, remove_element, remove_watch, write_config,
};
use crate::extract::normalize_text;
use crate::fetch::{FetchResult, ListResult, PageResult};
use crate::notify::{BotCommand, NotifierApi};
use crate::receive::Command;
use crate::scheduler::SchedulerApi;
use crate::scheduler::{KIND_ELEMENT, KIND_WATCH, MonitorRow, matches};

const MAX_MESSAGE: usize = 4096;
const TRIAL: &str = "试抓";
const YES: &[&str] = &["1", "y", "yes", "是", "确认", "确定", "保存"];
const TOP_COMMANDS: &[&str] = &["/help", "/add", "/list", "/del", "/menu"];

// 向导的各个等待步骤。
const PICK_KIND: &str = "选类型";
const ELEMENT_URL: &str = "元素URL";
const ELEMENT_SELECTOR: &str = "元素选择器";
const ELEMENT_JS: &str = "元素JS";
const ELEMENT_NAME: &str = "元素名称";
const ELEMENT_JUMP_URL: &str = "元素跳转URL";
const WATCH_URL: &str = "列表URL";
const WATCH_SELECTOR: &str = "链接选择器";
const WATCH_KEYWORDS: &str = "关键词";
const WATCH_NAME: &str = "列表名称";
const CONFIRM_SAVE: &str = "确认保存";
const DEL_INDEX: &str = "删除编号";
const DEL_CONFIRM: &str = "删除确认";

/// 快捷菜单与 /help 的唯一真相。
pub fn menu_commands() -> Vec<BotCommand> {
    vec![
        BotCommand {
            command: "add".into(),
            description: "新建监控，按提示逐步填写（网页元素变更 / 论坛关键词）".into(),
        },
        BotCommand {
            command: "list".into(),
            description: "列出全部监控及当前状态".into(),
        },
        BotCommand {
            command: "del".into(),
            description: "按编号删除一个监控，需二次确认".into(),
        },
        BotCommand {
            command: "menu".into(),
            description: "重新同步本快捷菜单".into(),
        },
        BotCommand {
            command: "help".into(),
            description: "显示命令说明".into(),
        },
        BotCommand {
            command: "cancel".into(),
            description: "取消进行中的操作".into(),
        },
    ]
}

fn help_text() -> String {
    let mut lines = vec!["HawkEye 控制命令：".to_string()];
    for c in menu_commands() {
        lines.push(format!("/{} —— {}", c.command, c.description));
    }
    lines.push(String::new());
    lines.push("改动会直接写回 config.toml 并即时生效，无需重启。".to_string());
    lines.join("\n")
}

const UNPARSABLE_REFUSAL: &str = "配置文件当前不可解析，已拒绝写入。请先修好 config.toml 再重试。";

const PICK_KIND_PROMPT: &str =
    "要新建哪种监控？\n1 = 网页元素变更监控\n2 = 论坛关键词监控\n回复 1 或 2，或 /cancel 取消。";

/// 一次多轮向导的中间态：正在等哪一步、已经收到什么（仅存内存）。
#[derive(Debug)]
struct Session {
    step: &'static str,
    kind: &'static str,
    url: String,
    selector: String,
    js: Option<String>,
    name: Option<String>,
    element_url: Option<String>,
    link_selector: String,
    keywords: Vec<String>,
    rows: Vec<MonitorRow>,
    target: Option<MonitorRow>,
}

impl Session {
    fn new(step: &'static str) -> Self {
        Self {
            step,
            kind: KIND_ELEMENT,
            url: String::new(),
            selector: String::new(),
            js: None,
            name: None,
            element_url: None,
            link_selector: String::new(),
            keywords: Vec::new(),
            rows: Vec::new(),
            target: None,
        }
    }
}

// ---- 纯文本辅助 ----

/// 压掉换页与连续空白再截断：元素当前值可能是一整段描述，会把回执撑爆。
pub fn clip(text: &str, limit: usize) -> String {
    let flat = normalize_text(text);
    if flat.chars().count() <= limit {
        flat
    } else {
        let truncated: String = flat.chars().take(limit.saturating_sub(1)).collect();
        format!("{truncated}…")
    }
}

/// 空格、逗号、中文逗号、顿号皆可作分隔符。
fn split_keywords(text: &str) -> Vec<String> {
    let normalized = text.replace(['，', ',', '、'], " ");
    normalized
        .split_whitespace()
        .map(|s| s.to_string())
        .collect()
}

fn parse_index(text: &str, total: usize) -> Option<usize> {
    let raw = text.trim_start_matches('#').trim();
    if raw.is_empty() || !raw.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    let index: usize = raw.parse().ok()?;
    (1..=total).contains(&index).then_some(index)
}

fn format_rows(rows: &[MonitorRow]) -> Vec<String> {
    rows.iter()
        .enumerate()
        .map(|(i, row)| {
            format!(
                "#{} [{}] {} — {} — {}",
                i + 1,
                row.kind,
                row.identity,
                row.url,
                clip(&row.status, 60)
            )
        })
        .collect()
}

/// 按 Telegram 单条 4096 字符上限把多行拆成若干条消息。
fn segment(lines: &[String]) -> Vec<String> {
    let mut messages = Vec::new();
    let mut current: Vec<String> = Vec::new();
    let mut size = 0;
    for line in lines {
        let extra = line.len() + usize::from(!current.is_empty());
        if !current.is_empty() && size + extra > MAX_MESSAGE {
            messages.push(current.join("\n"));
            current = vec![line.clone()];
            size = line.len();
            continue;
        }
        current.push(line.clone());
        size += extra;
    }
    if !current.is_empty() {
        messages.push(current.join("\n"));
    }
    messages
}

fn is_yes(text: &str) -> bool {
    YES.contains(&text.to_lowercase().as_str())
}

/// 一步的执行结果：回执 + 会话是否继续。
struct StepOutcome {
    replies: Vec<String>,
    keep: bool,
}

fn replies_out(replies: Vec<String>) -> StepOutcome {
    StepOutcome {
        replies,
        keep: true,
    }
}

fn done_out(replies: Vec<String>) -> StepOutcome {
    StepOutcome {
        replies,
        keep: false,
    }
}

/// 把授权会话的命令翻译成配置改写与运行时协调。
pub struct Controller {
    path: PathBuf,
    scheduler: Arc<dyn SchedulerApi>,
    notifier: Arc<dyn NotifierApi>,
    lock: Mutex<()>,
    session: Mutex<Option<Session>>,
    /// sync() 发现文件不可解析时置 false：此时绝不写入，否则等于拿破损内容当基线覆盖。
    writable: Mutex<bool>,
}

impl Controller {
    pub fn new(
        config_path: impl AsRef<Path>,
        scheduler: Arc<dyn SchedulerApi>,
        notifier: Arc<dyn NotifierApi>,
    ) -> Self {
        Self {
            path: config_path.as_ref().to_path_buf(),
            scheduler,
            notifier,
            lock: Mutex::new(()),
            session: Mutex::new(None),
            writable: Mutex::new(true),
        }
    }

    /// 处理一条命令并把回执发回 Telegram；全过程串行。
    pub async fn handle(&self, command: Command) {
        let _guard = self.lock.lock().await;
        let outcome = self.dispatch(command.text.trim()).await;
        let replies = match outcome {
            Ok(replies) => replies,
            Err(e) => {
                tracing::error!("处理命令失败：{}（{e}）", command.text);
                *self.session.lock().await = None;
                vec!["处理命令时出现内部错误，操作未完成。请稍后重试或查看日志。".to_string()]
            }
        };
        for reply in replies {
            self.notifier.send(&reply).await;
        }
    }

    /// 把外部手改同步进运行时。返回 Some(警告) 表示文件当前不可解析，写入被拒。
    pub async fn sync(&self) -> Option<String> {
        match load_config(&self.path) {
            Err(e) => {
                *self.writable.lock().await = false;
                tracing::warn!("配置文件当前不可解析：{e}");
                Some(format!(
                    "注意：配置文件当前无法解析（{e}）。以下为运行中的配置，且已暂停写入。"
                ))
            }
            Ok(new_config) => {
                *self.writable.lock().await = true;
                if new_config != self.scheduler.config().await {
                    self.scheduler.reconcile(new_config).await;
                    tracing::info!("配置文件已被外部修改，已同步");
                }
                None
            }
        }
    }

    async fn dispatch(&self, text: &str) -> Result<Vec<String>, EditError> {
        let keyword = text
            .split_whitespace()
            .next()
            .map(|s| s.to_lowercase())
            .unwrap_or_default();

        if keyword == "/cancel" {
            let cancelled = self.session.lock().await.take().is_some();
            return Ok(vec![if cancelled {
                "已取消当前操作。".to_string()
            } else {
                "当前没有进行中的操作。".to_string()
            }]);
        }

        if TOP_COMMANDS.contains(&keyword.as_str()) {
            // 顶层命令即视为放弃上一个向导，不再追问，但必须明确告知。
            let cancelled_prev = self.session.lock().await.take().is_some();
            let mut prefix = vec![];
            if cancelled_prev {
                prefix.push("已取消上一个未完成的操作。".to_string());
            }
            if keyword == "/menu" {
                // 快捷菜单与 config.toml 无关，不必为它做一次配置同步。
                let msg = self.notifier.sync_commands(&menu_commands()).await;
                prefix.push(msg);
                return Ok(prefix);
            }
            if let Some(warning) = self.sync().await {
                prefix.push(warning);
            }
            match keyword.as_str() {
                "/help" => prefix.push(help_text()),
                "/list" => prefix.extend(self.list().await),
                "/add" => {
                    *self.session.lock().await = Some(Session::new(PICK_KIND));
                    prefix.push(PICK_KIND_PROMPT.to_string());
                }
                "/del" => prefix.extend(self.del_prompt().await),
                _ => {}
            }
            return Ok(prefix);
        }

        // 会话进行中一律当步骤输入：XPath 选择器就以 / 开头，不能按命令拦下来。
        let Some(mut session) = self.session.lock().await.take() else {
            return Ok(vec!["没有进行中的操作。发送 /help 查看用法。".to_string()]);
        };
        let outcome = self.step(&mut session, text).await?;
        if outcome.keep {
            *self.session.lock().await = Some(session);
        }
        Ok(outcome.replies)
    }

    async fn step(&self, session: &mut Session, text: &str) -> Result<StepOutcome, EditError> {
        match session.step {
            PICK_KIND => {
                if text == "1" {
                    session.kind = KIND_ELEMENT;
                    session.step = ELEMENT_URL;
                    return Ok(replies_out(vec![
                        "请发送要监控的页面 URL（http 或 https 开头）。".to_string(),
                    ]));
                }
                if text == "2" {
                    session.kind = KIND_WATCH;
                    session.step = WATCH_URL;
                    return Ok(replies_out(vec![
                        "请发送要监控的列表页 URL（http 或 https 开头）。".to_string(),
                    ]));
                }
                Ok(replies_out(vec![
                    "请回复 1（网页元素变更）或 2（论坛关键词），或 /cancel 取消。".to_string(),
                ]))
            }
            ELEMENT_URL | WATCH_URL => {
                if !is_http_url(text) {
                    return Ok(replies_out(vec![
                        "这不像一个 http(s) 地址，请重新发送完整 URL。".to_string(),
                    ]));
                }
                session.url = text.to_string();
                if session.step == ELEMENT_URL {
                    session.step = ELEMENT_SELECTOR;
                    return Ok(replies_out(vec![
                        "请发送元素选择器，CSS 与 XPath 自动识别，例如 #price 或 //span[@id='p']。"
                            .to_string(),
                    ]));
                }
                session.step = WATCH_SELECTOR;
                Ok(replies_out(vec![
                    "请发送帖子链接的选择器，例如 .topic-list a.title。".to_string(),
                ]))
            }
            ELEMENT_SELECTOR => {
                session.selector = text.to_string();
                session.step = ELEMENT_JS;
                Ok(replies_out(vec![
                    "输入 JS 表达式（可选，直接回车或输入 - 跳过）\n例：el => el.classList.contains('disabled') ? '售罄' : '可订'"
                        .to_string(),
                ]))
            }
            ELEMENT_JS => {
                session.js = if text.is_empty() || text == "-" {
                    None
                } else {
                    Some(text.to_string())
                };
                session.step = ELEMENT_NAME;
                Ok(replies_out(vec![
                    "给它起个名字，方便以后在 /list 里认出来；回复 - 表示直接用选择器当名字。"
                        .to_string(),
                ]))
            }
            ELEMENT_JUMP_URL => {
                if text.is_empty() || text == "-" {
                    session.element_url = None;
                } else if !is_http_url(text) {
                    return Ok(replies_out(vec![
                        "这不像一个 http(s) 地址，请重新发送完整 URL，或直接回车/- 跳过。"
                            .to_string(),
                    ]));
                } else {
                    session.element_url = Some(text.to_string());
                }
                if session.kind == KIND_ELEMENT {
                    return self.trial_element(session).await;
                }
                self.trial_watch(session).await
            }
            WATCH_SELECTOR => {
                session.link_selector = text.to_string();
                session.step = WATCH_KEYWORDS;
                Ok(replies_out(vec![
                    "请发送关键词，多个用空格或逗号分隔（不区分大小写，命中任一即通知）。"
                        .to_string(),
                ]))
            }
            WATCH_KEYWORDS => {
                let keywords = split_keywords(text);
                if keywords.is_empty() {
                    return Ok(replies_out(vec![
                        "至少需要一个关键词，请重新发送。".to_string(),
                    ]));
                }
                session.keywords = keywords;
                session.step = WATCH_NAME;
                Ok(replies_out(vec![
                    "给它起个名字，方便以后在 /list 里认出来；回复 - 表示直接用列表页 URL 当名字。"
                        .to_string(),
                ]))
            }
            ELEMENT_NAME | WATCH_NAME => {
                if session.step == WATCH_NAME && text != "-" {
                    let occupied = self
                        .scheduler
                        .snapshot()
                        .await
                        .iter()
                        .any(|row| row.identity == format!("watch / {text}"));
                    if occupied {
                        return Ok(replies_out(vec![
                            "这个名字已被现有监控占用，请换一个，或回复 - 用列表页 URL 当名字。"
                                .to_string(),
                        ]));
                    }
                }
                session.name = if text == "-" {
                    None
                } else {
                    Some(text.to_string())
                };
                if session.step == ELEMENT_NAME {
                    session.step = ELEMENT_JUMP_URL;
                    return Ok(replies_out(vec![
                        "请发送跳转 URL（可选，直接回车或 - 跳过；留空时用页面 URL 推送通知，点击消息直跳目标下单/详情页）。"
                            .to_string(),
                    ]));
                }
                self.trial_watch(session).await
            }
            CONFIRM_SAVE => {
                if is_yes(text) {
                    return self.save(session, String::new()).await;
                }
                Ok(done_out(vec!["已取消，未保存任何内容。".to_string()]))
            }
            DEL_INDEX => Ok(self.pick_target(session, text)),
            DEL_CONFIRM => {
                if is_yes(text) {
                    return self.delete(session).await;
                }
                Ok(done_out(vec!["已取消，未删除任何监控。".to_string()]))
            }
            _ => Ok(done_out(vec![
                "没有进行中的操作。发送 /help 查看用法。".to_string(),
            ])),
        }
    }

    async fn list(&self) -> Vec<String> {
        let rows = self.scheduler.snapshot().await;
        if rows.is_empty() {
            return vec!["当前没有任何监控。发送 /add 添加第一个。".to_string()];
        }
        let mut lines = vec![format!("共 {} 个监控：", rows.len())];
        lines.extend(format_rows(&rows));
        segment(&lines)
    }

    async fn del_prompt(&self) -> Vec<String> {
        let rows = self.scheduler.snapshot().await;
        if rows.is_empty() {
            return vec!["当前没有任何监控可删除。".to_string()];
        }
        *self.session.lock().await = Some(Session {
            step: DEL_INDEX,
            rows: rows.clone(),
            ..Session::new(DEL_INDEX)
        });
        let mut lines = format_rows(&rows);
        lines.push("请回复要删除的编号，或 /cancel 取消。".to_string());
        segment(&lines)
    }

    fn pick_target(&self, session: &mut Session, text: &str) -> StepOutcome {
        let Some(index) = parse_index(text, session.rows.len()) else {
            return done_out(vec![format!(
                "编号无效（当前共 {} 项）。请重新发送 /del 查看最新列表。",
                session.rows.len()
            )]);
        };
        let target = session.rows[index - 1].clone();
        session.target = Some(target.clone());
        session.step = DEL_CONFIRM;
        replies_out(vec![format!(
            "将要删除 #{index} [{}] {}\n{}\n回复 1 确认删除，回复其他内容取消。",
            target.kind, target.identity, target.url
        )])
    }

    // ---- 试抓：保存前先看一眼到底抓到了什么 ----

    fn temp_page(&self, session: &Session) -> Page {
        let element = MonitoredElement {
            merchant_name: TRIAL.to_string(),
            page_name: TRIAL.to_string(),
            name: session
                .name
                .clone()
                .unwrap_or_else(|| session.selector.clone()),
            selector: session.selector.clone(),
            selector_type: "auto".to_string(),
            nth: None,
            js: session.js.clone(),
            url: session.element_url.clone(),
        };
        Page {
            merchant_name: TRIAL.to_string(),
            name: TRIAL.to_string(),
            url: session.url.clone(),
            poll_interval_secs: 60,
            wait_until: "domcontentloaded".to_string(),
            nav_timeout_secs: 30,
            failure_threshold: 3,
            elements: vec![element],
            fingerprint: Default::default(),
            proxy: None,
        }
    }

    fn temp_watch(&self, session: &Session) -> WatchTarget {
        WatchTarget {
            name: session.name.clone().unwrap_or_else(|| TRIAL.to_string()),
            url: session.url.clone(),
            link_selector: session.link_selector.clone(),
            selector_type: "auto".to_string(),
            keywords: session.keywords.clone(),
            id_pattern: None,
            poll_interval_secs: 60,
            wait_until: "domcontentloaded".to_string(),
            nav_timeout_secs: 30,
            failure_threshold: 3,
            fingerprint: Default::default(),
            proxy: None,
        }
    }

    async fn trial_element(&self, session: &mut Session) -> Result<StepOutcome, EditError> {
        let result = self
            .scheduler
            .trial_fetch_page(self.temp_page(session))
            .await;
        match result {
            PageResult::LoadError { reason } => Ok(self.ask_anyway(
                session,
                format!("试抓失败：页面打不开（{}）。", clip(&reason, 120)),
            )),
            PageResult::Fetched { results } => {
                let (_element, outcome) = &results[0];
                match outcome {
                    FetchResult::Ok { value } => {
                        let note = format!("试抓成功，当前取到：{}", clip(value, 200));
                        self.save(session, note).await
                    }
                    FetchResult::NoMatch { reason } | FetchResult::Error { reason } => {
                        Ok(self
                            .ask_anyway(session, format!("试抓没取到值：{}。", clip(reason, 120))))
                    }
                }
            }
        }
    }

    async fn trial_watch(&self, session: &mut Session) -> Result<StepOutcome, EditError> {
        let result = self
            .scheduler
            .trial_fetch_list(self.temp_watch(session))
            .await;
        match result {
            ListResult::LoadError { reason } => Ok(self.ask_anyway(
                session,
                format!("试抓失败：列表页打不开（{}）。", clip(&reason, 120)),
            )),
            ListResult::Fetched { items } => {
                if items.is_empty() {
                    return Ok(self.ask_anyway(
                        session,
                        "试抓没提取到任何链接，链接选择器可能不对。".to_string(),
                    ));
                }
                let hits = items
                    .iter()
                    .filter(|item| matches(&session.keywords, &item.title))
                    .count();
                let note = format!(
                    "试抓成功：找到 {} 条链接，其中 {hits} 条命中关键词。",
                    items.len()
                );
                if hits == 0 {
                    return Ok(self.ask_anyway(
                        session,
                        format!("{note}\n当前没有命中项，也可能只是暂时没有符合的帖子。"),
                    ));
                }
                self.save(session, note).await
            }
        }
    }

    /// 试抓不理想时不擅自决定：让用户在「仍然保存」和「取消」之间选。
    fn ask_anyway(&self, session: &mut Session, reason: String) -> StepOutcome {
        session.step = CONFIRM_SAVE;
        replies_out(vec![format!(
            "{reason}\n回复 1 仍然保存，回复其他内容取消。"
        )])
    }

    // ---- 落盘：写事务 + 立即协调运行时 ----

    async fn save(&self, session: &Session, note: String) -> Result<StepOutcome, EditError> {
        let prefix = if note.is_empty() {
            String::new()
        } else {
            format!("{note}\n")
        };
        if !*self.writable.lock().await {
            return Ok(done_out(vec![format!("{prefix}{UNPARSABLE_REFUSAL}")]));
        }
        let raw = load_raw(&self.path)?;
        let new_raw = if session.kind == KIND_ELEMENT {
            add_element(
                &raw,
                &session.url,
                &session.selector,
                session.name.as_deref(),
                None,
                session.js.as_deref(),
                session.element_url.as_deref(),
            )
        } else {
            add_watch(
                &raw,
                &session.url,
                &session.link_selector,
                &session.keywords,
                session.name.as_deref(),
                None,
            )?
        };
        let config = match write_config(&self.path, &new_raw) {
            Ok(c) => c,
            Err(e) => {
                tracing::warn!("保存失败：{e}");
                return Ok(done_out(vec![format!(
                    "{prefix}保存失败：{e}\nconfig.toml 未被改动。"
                )]));
            }
        };
        self.scheduler.reconcile(config).await;
        Ok(done_out(vec![format!("{prefix}已保存并即时生效。")]))
    }

    async fn delete(&self, session: &Session) -> Result<StepOutcome, EditError> {
        let target = session.target.clone().expect("del_confirm 前必有 target");
        if !*self.writable.lock().await {
            return Ok(done_out(vec![UNPARSABLE_REFUSAL.to_string()]));
        }
        let raw = load_raw(&self.path)?;
        let new_raw = if target.kind == KIND_ELEMENT {
            remove_element(&raw, &target.identity)?
        } else {
            remove_watch(&raw, &target.identity)?
        };
        let config = match write_config(&self.path, &new_raw) {
            Ok(c) => c,
            Err(e) => {
                tracing::warn!("删除失败：{e}");
                return Ok(done_out(vec![format!(
                    "删除失败：{e}\nconfig.toml 未被改动。请重新发送 /del 查看最新列表。"
                )]));
            }
        };
        self.scheduler.reconcile(config).await;
        Ok(done_out(vec![format!(
            "已删除 {}，即时生效。",
            target.identity
        )]))
    }
}

#[cfg(test)]
mod text_tests {
    use super::*;

    #[test]
    fn test_clip() {
        assert_eq!(clip("  a   b\n c ", 60), "a b c");
        let long = "x".repeat(100);
        let clipped = clip(&long, 10);
        assert!(clipped.chars().count() == 10);
        assert!(clipped.ends_with('…'));
    }

    #[test]
    fn test_split_keywords() {
        assert_eq!(split_keywords("a b，c、d"), vec!["a", "b", "c", "d"]);
        assert!(split_keywords("   ").is_empty());
    }

    #[test]
    fn test_parse_index() {
        assert_eq!(parse_index("1", 3), Some(1));
        assert_eq!(parse_index("#2", 3), Some(2));
        assert_eq!(parse_index("0", 3), None);
        assert_eq!(parse_index("4", 3), None);
        assert_eq!(parse_index("x", 3), None);
    }

    #[test]
    fn test_segment() {
        let long = "y".repeat(3000);
        let msgs = segment(&[long.clone(), long.clone(), long.clone()]);
        assert_eq!(msgs.len(), 3);
        let short = vec!["a".to_string(), "b".to_string()];
        assert_eq!(segment(&short).len(), 1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;
    use crate::notify::TelegramFatalError;
    use async_trait::async_trait;

    const BASE: &str = r##"
state_path = "state.json"

[telegram]
bot_token = "t"
chat_id = "c"

[[merchants]]
name = "yunyoo"

[[merchants.pages]]
name = "购物车"
url = "https://yunyoo.cc/cart"

[[merchants.pages.elements]]
name = "商品A"
selector = "#a"
"##;

    const WATCH_BLOCK: &str = r##"

[[watches]]
url = "https://ns.com/"
link_selector = "a"
keywords = ["hk"]
"##;

    const ONLY_TELEGRAM: &str = r##"
[telegram]
bot_token = "t"
chat_id = "c"
"##;

    // ---- 假件 ----

    struct FakeNotifier {
        sent: std::sync::Mutex<Vec<String>>,
        synced: std::sync::Mutex<Vec<Vec<BotCommand>>>,
    }

    impl FakeNotifier {
        fn new() -> Arc<Self> {
            Arc::new(Self {
                sent: std::sync::Mutex::new(Vec::new()),
                synced: std::sync::Mutex::new(Vec::new()),
            })
        }

        fn sent(&self) -> Vec<String> {
            self.sent.lock().unwrap().clone()
        }
    }

    #[async_trait]
    impl NotifierApi for FakeNotifier {
        async fn send(&self, text: &str) -> bool {
            self.sent.lock().unwrap().push(text.to_string());
            true
        }

        async fn sync_commands(&self, commands: &[BotCommand]) -> String {
            self.synced.lock().unwrap().push(commands.to_vec());
            "快捷菜单已同步。".to_string()
        }

        async fn verify(&self) -> Result<(), TelegramFatalError> {
            Ok(())
        }
    }

    /// 替身调度器：只提供控制面用到的接口，免得真起后台轮询任务干扰断言。
    struct FakeScheduler {
        config: std::sync::Mutex<Config>,
        reconciled: std::sync::Mutex<Vec<Config>>,
        page_result: std::sync::Mutex<Option<PageResult>>,
        list_result: std::sync::Mutex<Option<ListResult>>,
        trial_pages: std::sync::Mutex<Vec<Page>>,
        trial_watches: std::sync::Mutex<Vec<WatchTarget>>,
    }

    impl FakeScheduler {
        fn new(config: Config) -> Arc<Self> {
            Arc::new(Self {
                config: std::sync::Mutex::new(config),
                reconciled: std::sync::Mutex::new(Vec::new()),
                page_result: std::sync::Mutex::new(None),
                list_result: std::sync::Mutex::new(None),
                trial_pages: std::sync::Mutex::new(Vec::new()),
                trial_watches: std::sync::Mutex::new(Vec::new()),
            })
        }

        fn set_page_result(&self, r: PageResult) {
            *self.page_result.lock().unwrap() = Some(r);
        }

        fn set_list_result(&self, r: ListResult) {
            *self.list_result.lock().unwrap() = Some(r);
        }
    }

    #[async_trait]
    impl SchedulerApi for FakeScheduler {
        async fn config(&self) -> Config {
            self.config.lock().unwrap().clone()
        }

        async fn snapshot(&self) -> Vec<MonitorRow> {
            let config = self.config.lock().unwrap().clone();
            let mut rows: Vec<MonitorRow> = config
                .pages()
                .iter()
                .flat_map(|p| {
                    p.elements.iter().map(|e| MonitorRow {
                        kind: KIND_ELEMENT,
                        identity: e.identity(),
                        url: p.url.clone(),
                        status: "尚未建立基线".to_string(),
                    })
                })
                .collect();
            rows.extend(config.watches.iter().map(|w| MonitorRow {
                kind: KIND_WATCH,
                identity: w.identity(),
                url: w.url.clone(),
                status: "已见 3 帖".to_string(),
            }));
            rows
        }

        async fn reconcile(&self, new_config: Config) -> (usize, usize) {
            self.reconciled.lock().unwrap().push(new_config.clone());
            *self.config.lock().unwrap() = new_config;
            (0, 0)
        }

        async fn trial_fetch_page(&self, page: Page) -> PageResult {
            self.trial_pages.lock().unwrap().push(page);
            self.page_result
                .lock()
                .unwrap()
                .clone()
                .expect("测试未设置 page_result")
        }

        async fn trial_fetch_list(&self, watch: WatchTarget) -> ListResult {
            self.trial_watches.lock().unwrap().push(watch);
            self.list_result
                .lock()
                .unwrap()
                .clone()
                .expect("测试未设置 list_result")
        }
    }

    // ---- 装配 ----

    fn tmp_config(tag: &str, content: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("hawkeye_ctrl_test_{}_{}", std::process::id(), tag));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("config.toml");
        std::fs::write(&path, content).unwrap();
        path
    }

    struct Fixture {
        controller: Controller,
        scheduler: Arc<FakeScheduler>,
        notifier: Arc<FakeNotifier>,
        path: PathBuf,
    }

    fn setup_with(tag: &str, content: &str) -> Fixture {
        let path = tmp_config(tag, content);
        let scheduler = FakeScheduler::new(load_config(&path).unwrap());
        let notifier = FakeNotifier::new();
        let controller = Controller::new(
            &path,
            scheduler.clone() as Arc<dyn SchedulerApi>,
            notifier.clone() as Arc<dyn NotifierApi>,
        );
        Fixture {
            controller,
            scheduler,
            notifier,
            path,
        }
    }

    fn setup(tag: &str) -> Fixture {
        setup_with(tag, BASE)
    }

    fn trial_element() -> MonitoredElement {
        MonitoredElement {
            merchant_name: "试抓".into(),
            page_name: "试抓".into(),
            name: "x".into(),
            selector: "#x".into(),
            selector_type: "auto".into(),
            nth: None,
            js: None,
            url: None,
        }
    }

    fn page_ok(value: &str) -> PageResult {
        PageResult::Fetched {
            results: vec![(
                trial_element(),
                FetchResult::Ok {
                    value: value.into(),
                },
            )],
        }
    }

    async fn send(controller: &Controller, texts: &[&str]) {
        for text in texts {
            controller
                .handle(Command {
                    text: text.to_string(),
                })
                .await;
        }
    }

    // ---- /help 与无会话闲聊 ----

    #[tokio::test]
    async fn test_help_lists_all_commands() {
        // /help 与快捷菜单共用一张表：表里的每条命令连描述一起出现。
        let f = setup("h1");
        send(&f.controller, &["/help"]).await;
        let sent = f.notifier.sent();
        assert_eq!(sent.len(), 1);
        for c in menu_commands() {
            assert!(
                sent[0].contains(&format!("/{} —— {}", c.command, c.description)),
                "缺少 /{}",
                c.command
            );
        }
    }

    #[test]
    fn test_menu_commands_satisfy_telegram_constraints() {
        for c in menu_commands() {
            assert!(!c.command.starts_with('/'), "Telegram 要求命令名不带斜杠");
            assert!(c.command.len() <= 32);
            assert!(!c.description.is_empty());
        }
    }

    #[tokio::test]
    async fn test_plain_text_without_session_points_to_help() {
        let f = setup("h2");
        send(&f.controller, &["你好"]).await;
        assert_eq!(
            f.notifier.sent(),
            vec!["没有进行中的操作。发送 /help 查看用法。"]
        );
    }

    #[tokio::test]
    async fn test_menu_pushes_the_whole_command_table() {
        let f = setup("h3");
        send(&f.controller, &["/menu"]).await;
        let synced = f.notifier.synced.lock().unwrap().clone();
        assert_eq!(synced.len(), 1);
        assert_eq!(synced[0], menu_commands());
        assert_eq!(f.notifier.sent(), vec!["快捷菜单已同步。"]);
    }

    #[tokio::test]
    async fn test_menu_ignores_broken_config() {
        // 快捷菜单与 config.toml 无关，文件坏了也照常同步（不为它做一次配置同步）。
        let f = setup("h4");
        std::fs::write(&f.path, "这不是 TOML {{{").unwrap();
        send(&f.controller, &["/menu"]).await;
        assert_eq!(f.notifier.sent(), vec!["快捷菜单已同步。"]);
    }

    // ---- /add 元素监控 ----

    #[tokio::test]
    async fn test_add_element_end_to_end_writes_file_and_reconciles() {
        // URL 未命中已有页面 → 新建以 host 命名的商家承载它。
        let f = setup("a1");
        f.scheduler.set_page_result(page_ok("¥99"));

        send(
            &f.controller,
            &[
                "/add",
                "1",
                "https://yunyoo.cc/new",
                "#price",
                "-",
                "价格",
                "-",
            ],
        )
        .await;

        let sent = f.notifier.sent();
        assert!(sent.last().unwrap().contains("¥99"), "{sent:?}");
        assert!(
            sent.last().unwrap().contains("已保存并即时生效"),
            "{sent:?}"
        );
        assert_eq!(f.scheduler.reconciled.lock().unwrap().len(), 1);

        let cfg = load_config(&f.path).unwrap();
        let names: Vec<&str> = cfg.merchants.iter().map(|m| m.name.as_str()).collect();
        assert_eq!(names, vec!["yunyoo", "yunyoo.cc"]);
        let identities: Vec<String> = cfg
            .pages()
            .iter()
            .flat_map(|p| p.elements.iter().map(|e| e.identity()))
            .collect();
        assert!(identities.contains(&"yunyoo.cc / https://yunyoo.cc/new / 价格".to_string()));
    }

    #[tokio::test]
    async fn test_add_element_merges_into_existing_page() {
        // URL 命中已有页面 → 并入该页面，不新建商家 / 页面。
        let f = setup("a2");
        f.scheduler.set_page_result(page_ok("¥1"));

        send(
            &f.controller,
            &["/add", "1", "https://yunyoo.cc/cart", "#b", "-", "-", "-"],
        )
        .await;

        let cfg = load_config(&f.path).unwrap();
        assert_eq!(cfg.merchants.len(), 1);
        assert_eq!(cfg.pages().len(), 1);
        let identities: Vec<String> = cfg.pages()[0]
            .elements
            .iter()
            .map(|e| e.identity())
            .collect();
        assert_eq!(
            identities,
            vec![
                "yunyoo / 购物车 / 商品A".to_string(),
                "yunyoo / 购物车 / #b".to_string(),
            ]
        );
    }

    #[tokio::test]
    async fn test_invalid_url_is_rejected_without_advancing() {
        let f = setup("a3");
        send(&f.controller, &["/add", "1", "yunyoo.cc/cart"]).await;
        assert!(f.notifier.sent().last().unwrap().contains("http(s)"));

        send(&f.controller, &["https://yunyoo.cc/cart"]).await;
        // 步骤没被推进，补上合法 URL 后照常继续。
        assert!(
            f.notifier.sent().last().unwrap().contains("选择器"),
            "{:?}",
            f.notifier.sent()
        );
    }

    #[tokio::test]
    async fn test_xpath_selector_is_not_mistaken_for_a_command() {
        // XPath 以 / 开头，若按命令前缀拦截就永远填不进去。
        let f = setup("a4");
        f.scheduler.set_page_result(page_ok("¥5"));

        send(
            &f.controller,
            &[
                "/add",
                "1",
                "https://e.com/p",
                "//span[@id='p']",
                "-",
                "-",
                "-",
            ],
        )
        .await;

        let trial_pages = f.scheduler.trial_pages.lock().unwrap().clone();
        assert_eq!(
            trial_pages.last().unwrap().elements[0].selector,
            "//span[@id='p']"
        );
        assert!(
            f.notifier
                .sent()
                .last()
                .unwrap()
                .contains("已保存并即时生效")
        );
    }

    #[tokio::test]
    async fn test_trial_miss_offers_choice_and_saves_when_confirmed() {
        // 试抓没命中不代表用户填错，必须给「仍然保存」的出口。
        let f = setup("a5");
        f.scheduler.set_page_result(PageResult::Fetched {
            results: vec![(
                trial_element(),
                FetchResult::NoMatch {
                    reason: "选择器匹配到元素，但其文本为空".into(),
                },
            )],
        });

        send(
            &f.controller,
            &["/add", "1", "https://e.com/p", "#nope", "-", "-", "-"],
        )
        .await;
        let sent = f.notifier.sent();
        // 控制面转达抓取层给的原话，不另编一套说法。
        assert!(
            sent.last()
                .unwrap()
                .contains("选择器匹配到元素，但其文本为空"),
            "{sent:?}"
        );
        assert!(sent.last().unwrap().contains("仍然保存"));
        assert!(f.scheduler.reconciled.lock().unwrap().is_empty());

        send(&f.controller, &["1"]).await;
        assert!(
            f.notifier
                .sent()
                .last()
                .unwrap()
                .contains("已保存并即时生效")
        );
        assert_eq!(load_config(&f.path).unwrap().element_count(), 2);
    }

    #[tokio::test]
    async fn test_trial_failure_cancel_leaves_file_untouched() {
        let f = setup("a6");
        f.scheduler.set_page_result(PageResult::LoadError {
            reason: "导航超时".into(),
        });
        let before = std::fs::read(&f.path).unwrap();

        send(
            &f.controller,
            &["/add", "1", "https://e.com/p", "#x", "-", "-", "-", "2"],
        )
        .await;

        let sent = f.notifier.sent();
        assert!(sent[sent.len() - 2].contains("导航超时"), "{sent:?}");
        assert_eq!(sent.last().unwrap(), "已取消，未保存任何内容。");
        assert_eq!(std::fs::read(&f.path).unwrap(), before);
        assert!(f.scheduler.reconciled.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn test_add_element_with_jump_url_persists_url_field() {
        // 跳转 URL 填写后落到 config.toml,留空时不写该键。
        let f = setup("a7");
        f.scheduler.set_page_result(page_ok("¥99"));

        send(
            &f.controller,
            &[
                "/add",
                "1",
                "https://yunyoo.cc/new",
                "#price",
                "-",
                "价格",
                "https://yunyoo.cc/order/123",
            ],
        )
        .await;

        let cfg = load_config(&f.path).unwrap();
        let el = cfg
            .pages()
            .iter()
            .flat_map(|p| p.elements.iter())
            .find(|e| e.name == "价格")
            .unwrap();
        assert_eq!(el.url.as_deref(), Some("https://yunyoo.cc/order/123"));
    }

    #[tokio::test]
    async fn test_add_element_skip_jump_url_omits_field() {
        let f = setup("a8");
        f.scheduler.set_page_result(page_ok("¥1"));

        send(
            &f.controller,
            &[
                "/add",
                "1",
                "https://yunyoo.cc/new",
                "#price",
                "-",
                "价格",
                "-",
            ],
        )
        .await;

        let cfg = load_config(&f.path).unwrap();
        let el = cfg
            .pages()
            .iter()
            .flat_map(|p| p.elements.iter())
            .find(|e| e.name == "价格")
            .unwrap();
        assert!(el.url.is_none());
    }

    // ---- /add 列表监控 ----

    #[tokio::test]
    async fn test_add_watch_end_to_end_reports_hit_count() {
        let f = setup("w1");
        f.scheduler.set_list_result(ListResult::Fetched {
            items: vec![
                crate::extract::ListItem {
                    post_id: "1".into(),
                    title: "出售 HK 节点".into(),
                    url: "https://ns.com/1".into(),
                },
                crate::extract::ListItem {
                    post_id: "2".into(),
                    title: "收 VPS".into(),
                    url: "https://ns.com/2".into(),
                },
                crate::extract::ListItem {
                    post_id: "3".into(),
                    title: "hk 便宜机".into(),
                    url: "https://ns.com/3".into(),
                },
            ],
        });

        send(
            &f.controller,
            &["/add", "2", "https://ns.com/", ".title a", "hk、香港", "-"],
        )
        .await;

        let sent = f.notifier.sent();
        assert!(
            sent.last()
                .unwrap()
                .contains("找到 3 条链接，其中 2 条命中关键词"),
            "{sent:?}"
        );
        assert!(sent.last().unwrap().contains("已保存并即时生效"));
        assert_eq!(f.scheduler.reconciled.lock().unwrap().len(), 1);

        let cfg = load_config(&f.path).unwrap();
        assert_eq!(cfg.watches.len(), 1);
        assert_eq!(cfg.watches[0].keywords, vec!["hk", "香港"]);
        assert_eq!(cfg.watches[0].link_selector, ".title a");
        assert_eq!(cfg.watches[0].identity(), "watch / https://ns.com/");
        assert_eq!(cfg.element_count(), 1, "原有的元素监控没被动过");
    }

    #[tokio::test]
    async fn test_add_watch_asks_for_name_before_trial() {
        // 名称是最后一步：收齐之前不该先去试抓。
        let f = setup("w2");
        send(&f.controller, &["/add", "2", "https://ns.com/", "a", "hk"]).await;

        assert!(f.notifier.sent().last().unwrap().contains("起个名字"));
        assert!(f.scheduler.trial_watches.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn test_add_watch_with_name_uses_it_as_identity() {
        let f = setup("w3");
        f.scheduler.set_list_result(ListResult::Fetched {
            items: vec![crate::extract::ListItem {
                post_id: "1".into(),
                title: "出售 HK 节点".into(),
                url: "https://ns.com/1".into(),
            }],
        });

        send(
            &f.controller,
            &["/add", "2", "https://ns.com/", "a", "hk", "NS 交易区"],
        )
        .await;

        assert!(
            f.notifier
                .sent()
                .last()
                .unwrap()
                .contains("已保存并即时生效")
        );
        let trial_watches = f.scheduler.trial_watches.lock().unwrap().clone();
        assert_eq!(trial_watches.last().unwrap().name, "NS 交易区");
        let cfg = load_config(&f.path).unwrap();
        assert_eq!(cfg.watches[0].identity(), "watch / NS 交易区");
    }

    #[tokio::test]
    async fn test_add_watch_without_hits_asks_before_saving() {
        let f = setup("w4");
        f.scheduler.set_list_result(ListResult::Fetched {
            items: vec![crate::extract::ListItem {
                post_id: "1".into(),
                title: "无关帖子".into(),
                url: "https://ns.com/1".into(),
            }],
        });
        let before = std::fs::read(&f.path).unwrap();

        send(
            &f.controller,
            &["/add", "2", "https://ns.com/", "a", "hk", "-"],
        )
        .await;

        let sent = f.notifier.sent();
        assert!(
            sent.last()
                .unwrap()
                .contains("找到 1 条链接，其中 0 条命中关键词"),
            "{sent:?}"
        );
        assert!(sent.last().unwrap().contains("仍然保存"));
        assert_eq!(std::fs::read(&f.path).unwrap(), before);
    }

    // ---- /list ----

    #[tokio::test]
    async fn test_list_empty_hints_add() {
        let f = setup_with("l1", ONLY_TELEGRAM);
        send(&f.controller, &["/list"]).await;
        assert_eq!(
            f.notifier.sent(),
            vec!["当前没有任何监控。发送 /add 添加第一个。"]
        );
    }

    #[tokio::test]
    async fn test_list_row_carries_kind_name_url_and_status() {
        let f = setup_with("l2", &format!("{BASE}{WATCH_BLOCK}"));
        send(&f.controller, &["/list"]).await;
        let sent = f.notifier.sent();
        assert!(
            sent.last()
                .unwrap()
                .contains("#1 [元素] yunyoo / 购物车 / 商品A"),
            "{sent:?}"
        );
        assert!(sent.last().unwrap().contains("https://yunyoo.cc/cart"));
        assert!(
            sent.last()
                .unwrap()
                .contains("#2 [论坛] watch / https://ns.com/")
        );
        assert!(sent.last().unwrap().contains("已见 3 帖"));
    }

    #[tokio::test]
    async fn test_list_segments_output_under_telegram_limit() {
        // 单条 4096 上限：大量监控时按条拆分。
        let mut content = ONLY_TELEGRAM.to_string();
        for i in 0..40 {
            content.push_str(&format!(
                "\n[[merchants]]\nname = \"m{i}\"\n[[merchants.pages]]\nurl = \"https://e{i}.com/some/longer/path\"\n[[merchants.pages.elements]]\nname = \"元素名{i}\"\nselector = \"#selector-{i}\"\n"
            ));
        }
        let f = setup_with("l3", &content);
        send(&f.controller, &["/list"]).await;
        let sent = f.notifier.sent();
        assert!(sent.len() >= 2, "应拆分为多条：{}", sent.len());
        for msg in &sent {
            assert!(msg.len() <= MAX_MESSAGE, "单条超出 Telegram 上限");
        }
    }

    // ---- /del ----

    #[tokio::test]
    async fn test_del_removes_entry_and_prunes_empty_page() {
        // 删掉页面下最后一个元素 → 空页面与随之变空的商家一并消失。
        let f = setup("d1");
        send(&f.controller, &["/del", "1", "1"]).await;

        let sent = f.notifier.sent();
        assert!(
            sent[0].contains("#1 [元素] yunyoo / 购物车 / 商品A"),
            "{sent:?}"
        );
        assert!(sent[1].contains("将要删除 #1"));
        assert_eq!(
            sent.last().unwrap(),
            "已删除 yunyoo / 购物车 / 商品A，即时生效。"
        );
        assert_eq!(f.scheduler.reconciled.lock().unwrap().len(), 1);

        let cfg = load_config(&f.path).unwrap();
        assert!(cfg.merchants.is_empty());
        assert_eq!(cfg.element_count(), 0);
    }

    #[tokio::test]
    async fn test_del_cancel_leaves_file_untouched() {
        let f = setup("d2");
        let before = std::fs::read(&f.path).unwrap();
        send(&f.controller, &["/del", "1", "/cancel"]).await;

        assert_eq!(f.notifier.sent().last().unwrap(), "已取消当前操作。");
        assert_eq!(std::fs::read(&f.path).unwrap(), before);
        assert!(f.scheduler.reconciled.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn test_del_index_out_of_range_asks_to_retry() {
        let f = setup("d3");
        let before = std::fs::read(&f.path).unwrap();
        send(&f.controller, &["/del", "9"]).await;

        assert!(f.notifier.sent().last().unwrap().contains("编号无效"));
        assert_eq!(std::fs::read(&f.path).unwrap(), before);
        assert!(f.scheduler.reconciled.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn test_del_second_confirmation_is_required() {
        // 删除不可逆：除明确确认外一律按取消处理。
        let f = setup("d4");
        let before = std::fs::read(&f.path).unwrap();
        send(&f.controller, &["/del", "1", "算了"]).await;

        assert_eq!(
            f.notifier.sent().last().unwrap(),
            "已取消，未删除任何监控。"
        );
        assert_eq!(std::fs::read(&f.path).unwrap(), before);
        assert!(f.scheduler.reconciled.lock().unwrap().is_empty());
    }

    // ---- 一致性同步 ----

    #[tokio::test]
    async fn test_external_edit_is_synced_before_command() {
        let f = setup("s1");
        std::fs::write(&f.path, format!("{BASE}{WATCH_BLOCK}")).unwrap();

        send(&f.controller, &["/list"]).await;

        assert_eq!(f.scheduler.reconciled.lock().unwrap().len(), 1);
        assert!(
            f.notifier
                .sent()
                .last()
                .unwrap()
                .contains("watch / https://ns.com/")
        );
    }

    #[tokio::test]
    async fn test_unparsable_config_lists_running_set_with_warning() {
        let f = setup("s2");
        std::fs::write(&f.path, "这不是 TOML {{{").unwrap();

        send(&f.controller, &["/list"]).await;

        let sent = f.notifier.sent();
        assert!(sent[0].contains("无法解析"), "{sent:?}");
        assert!(sent[1].contains("yunyoo / 购物车 / 商品A"), "{sent:?}");
        assert!(f.scheduler.reconciled.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn test_unparsable_config_refuses_to_write() {
        let f = setup("s3");
        f.scheduler.set_page_result(page_ok("¥1"));
        std::fs::write(&f.path, "坏文件 {{{").unwrap();
        let broken = std::fs::read(&f.path).unwrap();

        send(
            &f.controller,
            &["/add", "1", "https://e.com/p", "#x", "-", "-", "-"],
        )
        .await;

        assert!(
            f.notifier.sent().last().unwrap().contains("已拒绝写入"),
            "{:?}",
            f.notifier.sent()
        );
        assert_eq!(std::fs::read(&f.path).unwrap(), broken);
        assert!(f.scheduler.reconciled.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn test_top_level_command_discards_in_progress_wizard() {
        // 顶层命令即视为放弃上一个向导，不再追问，但必须明确告知。
        let f = setup("s4");
        send(&f.controller, &["/add", "1", "/list"]).await;

        let sent = f.notifier.sent();
        assert_eq!(sent[sent.len() - 2], "已取消上一个未完成的操作。");
        assert!(sent.last().unwrap().contains("共 1 个监控"), "{sent:?}");
    }

    #[tokio::test]
    async fn test_cancel_without_session_is_a_noop() {
        let f = setup("s5");
        send(&f.controller, &["/cancel"]).await;
        assert_eq!(f.notifier.sent(), vec!["当前没有进行中的操作。"]);
    }
}
