//! 调度与编排。
//!
//! 两类常驻循环并存，共享同一浏览器、信号量、状态锁、失败告警状态机与通知器：
//!
//! - 元素文本监控：每个页面一个 tokio 任务，受信号量限流的一次导航 → 提取页面下
//!   全部元素 → 逐元素检测变更/告警。
//! - 列表新条目监控：每个 watch 目标一个任务，抓一次列表页 → 对照已见集合判新 →
//!   标题命中关键字的新帖逐条通知 → 轮末一次性并入已见集合并落盘。

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use tokio::sync::{Mutex, OwnedSemaphorePermit, Semaphore};

use crate::alert::{FailureState, FailureTracker};
use crate::config::{Config, MonitoredElement, Page, WatchTarget};
use crate::detect::{DetectNewResult, DetectResult, detect, detect_new, merge_seen};
use crate::extract::ListItem;
use crate::fetch::{FetchResult, ListResult, PageResult};
use crate::notify::{
    NotifierApi, format_change_message, format_failure_message, format_new_post_message,
};
use crate::state::{Entry, State, load_state, now_iso, save_state};

/// 抓取后端（测试注入假实现，生产用 BrowserManager）。
#[async_trait]
pub trait FetchBackend: Send + Sync {
    async fn fetch_page(&self, page: &Page) -> PageResult;
    async fn fetch_list(&self, watch: &WatchTarget) -> ListResult;
}

/// 调度器面向控制面的接口（控制面测试注入假调度器，免真起后台轮询任务）。
#[async_trait]
pub trait SchedulerApi: Send + Sync {
    async fn config(&self) -> Config;
    async fn snapshot(&self) -> Vec<MonitorRow>;
    async fn reconcile(&self, new_config: Config) -> (usize, usize);
    async fn trial_fetch_page(&self, page: Page) -> PageResult;
    async fn trial_fetch_list(&self, watch: WatchTarget) -> ListResult;
}

/// 快照行的两种类型标签，控制面按它决定走哪套增删变换。
pub const KIND_ELEMENT: &str = "元素";
pub const KIND_WATCH: &str = "论坛";

/// 标题关键字匹配：子串 + 不区分大小写 + 任一命中（OR）。
pub fn matches(keywords: &[String], title: &str) -> bool {
    let lowered = title.to_lowercase();
    keywords
        .iter()
        .any(|kw| lowered.contains(&kw.to_lowercase()))
}

/// `/list` 的一行：类型、稳定标识、来源 URL 与当前状态指示。
#[derive(Debug, Clone, PartialEq)]
pub struct MonitorRow {
    pub kind: &'static str,
    pub identity: String,
    pub url: String,
    pub status: String,
}

/// 把配置、浏览器、通知器、状态编排成常驻循环。
pub struct Scheduler {
    /// 当前生效配置；热重载时替换。读侧用锁内直接读。
    config: Mutex<Config>,
    browser: Arc<dyn FetchBackend>,
    notifier: Arc<dyn NotifierApi>,
    state: Mutex<State>,
    page_failures: Mutex<HashMap<String, FailureState>>,
    elem_failures: Mutex<FailureTracker>,
    watch_failures: Mutex<FailureTracker>,
    semaphore: Arc<Semaphore>,
    /// 常驻任务句柄登记表，键 page:<identity> / watch:<identity>，供热重载增删定位。
    tasks: Mutex<HashMap<String, tokio::task::JoinHandle<()>>>,
    /// 停止信号：命令接收循环与轮询循环共用一个。
    stop_tx: tokio::sync::watch::Sender<bool>,
    state_path: PathBuf,
}

impl Scheduler {
    pub fn new(
        config: Config,
        browser: Arc<dyn FetchBackend>,
        notifier: Arc<dyn NotifierApi>,
    ) -> (Arc<Self>, tokio::sync::watch::Receiver<bool>) {
        let state_path = PathBuf::from(&config.state_path);
        let state = load_state(&state_path);
        let max_concurrent = config.max_concurrent_fetches.max(1) as usize;
        let (stop_tx, stop_rx) = tokio::sync::watch::channel(false);
        let scheduler = Arc::new(Self {
            config: Mutex::new(config),
            browser,
            notifier,
            state: Mutex::new(state),
            page_failures: Mutex::new(HashMap::new()),
            elem_failures: Mutex::new(FailureTracker::default()),
            watch_failures: Mutex::new(FailureTracker::default()),
            semaphore: Arc::new(Semaphore::new(max_concurrent)),
            tasks: Mutex::new(HashMap::new()),
            stop_tx,
            state_path,
        });
        (scheduler, stop_rx)
    }

    pub fn subscribe_stop(&self) -> tokio::sync::watch::Receiver<bool> {
        self.stop_tx.subscribe()
    }

    /// 由信号处理触发，请求优雅停止。
    pub fn request_stop(&self) {
        tracing::info!("收到停止请求，正在退出……");
        let _ = self.stop_tx.send(true);
    }

    /// 当前生效的配置快照；控制面用它与磁盘文件比对以决定是否热重载。
    pub async fn config(&self) -> Config {
        self.config.lock().await.clone()
    }

    async fn spawn_page(self: &Arc<Self>, page: Page) {
        let key = format!("page:{}", page.identity());
        let scheduler = Arc::clone(self);
        let handle = tokio::spawn(async move { scheduler.run_page(page).await });
        self.tasks.lock().await.insert(key, handle);
    }

    async fn spawn_watch(self: &Arc<Self>, watch: WatchTarget) {
        let key = format!("watch:{}", watch.identity());
        let scheduler = Arc::clone(self);
        let handle = tokio::spawn(async move { scheduler.run_watch(watch).await });
        self.tasks.lock().await.insert(key, handle);
    }

    /// 主运行入口：按配置起全部任务，等待停止信号后回收。
    pub async fn run(self: Arc<Self>) {
        let config = self.config().await;
        for page in config.pages() {
            let page = page.clone();
            self.spawn_page(page).await;
        }
        for watch in &config.watches {
            let watch = watch.clone();
            self.spawn_watch(watch.clone()).await;
        }
        // 等待停止信号。
        let mut stop = self.subscribe_stop();
        let _ = stop.changed().await;
        // 回收全部任务。
        let tasks: Vec<_> = self.tasks.lock().await.drain().map(|(_, t)| t).collect();
        for t in &tasks {
            t.abort();
        }
        for t in tasks {
            let _ = t.await;
        }
        self.flush_state().await;
    }

    // ---- 热重载：配置变更后的运行时协调 ----

    /// 把运行中的任务对齐到 new_config，返回（新增数, 移除数）。
    ///
    /// 页面 / watch 对象逐字段相同则任务原样保留，计时器不重置；被改动的目标按
    /// 「先撤后建」重启。必须先取消并等待任务真正退出，才能清理失败状态与 state
    /// 条目。消失目标的 state 条目一并删除，避免下次同名新建时把陈旧基线当作
    /// 「已见」而漏掉首次变更。
    pub async fn reconcile(self: &Arc<Self>, new_config: Config) -> (usize, usize) {
        let old_config = self.config().await;

        let old_pages: HashMap<String, Page> = old_config
            .pages()
            .into_iter()
            .map(|p| (p.identity(), p.clone()))
            .collect();
        let new_pages: HashMap<String, Page> = new_config
            .pages()
            .into_iter()
            .map(|p| (p.identity(), p.clone()))
            .collect();
        let old_watches: HashMap<String, WatchTarget> = old_config
            .watches
            .iter()
            .map(|w| (w.identity(), w.clone()))
            .collect();
        let new_watches: HashMap<String, WatchTarget> = new_config
            .watches
            .iter()
            .map(|w| (w.identity(), w.clone()))
            .collect();

        let gone_pages: Vec<String> = old_pages
            .keys()
            .filter(|k| !new_pages.contains_key(*k))
            .cloned()
            .collect();
        let gone_watches: Vec<String> = old_watches
            .keys()
            .filter(|k| !new_watches.contains_key(*k))
            .cloned()
            .collect();
        let added_pages: Vec<String> = new_pages
            .keys()
            .filter(|k| !old_pages.contains_key(*k))
            .cloned()
            .collect();
        let added_watches: Vec<String> = new_watches
            .keys()
            .filter(|k| !old_watches.contains_key(*k))
            .cloned()
            .collect();
        let changed_pages: Vec<String> = new_pages
            .iter()
            .filter(|(k, p)| old_pages.get(*k).map(|o| o != *p).unwrap_or(false))
            .map(|(k, _)| k.clone())
            .collect();
        let changed_watches: Vec<String> = new_watches
            .iter()
            .filter(|(k, w)| old_watches.get(*k).map(|o| o != *w).unwrap_or(false))
            .map(|(k, _)| k.clone())
            .collect();

        // 消失与被改动的元素标识（旧配置里有、新配置里没有的才算真正消失）。
        let surviving_elements: HashSet<String> = new_config
            .pages()
            .iter()
            .flat_map(|p| p.elements.iter().map(|e| e.identity()))
            .collect();
        let gone_elements: Vec<String> = old_config
            .pages()
            .iter()
            .flat_map(|p| p.elements.iter().map(|e| e.identity()))
            .filter(|id| !surviving_elements.contains(id))
            .collect();

        // ① 先撤：取消所有要停的任务并等它们真正退出。
        let stopping_pages: Vec<String> = gone_pages
            .iter()
            .chain(changed_pages.iter())
            .cloned()
            .collect();
        let stopping_watches: Vec<String> = gone_watches
            .iter()
            .chain(changed_watches.iter())
            .cloned()
            .collect();
        let mut cancelled = Vec::new();
        {
            let mut tasks = self.tasks.lock().await;
            for identity in &stopping_pages {
                if let Some(t) = tasks.remove(&format!("page:{identity}")) {
                    t.abort();
                    cancelled.push(t);
                }
            }
            for identity in &stopping_watches {
                if let Some(t) = tasks.remove(&format!("watch:{identity}")) {
                    t.abort();
                    cancelled.push(t);
                }
            }
        }
        for t in cancelled {
            let _ = t.await;
        }

        // ② 再清：只清真正消失的目标，存活目标的失败计数与基线一律保留。
        for identity in &gone_pages {
            self.page_failures.lock().await.remove(identity);
        }
        {
            let mut elem_failures = self.elem_failures.lock().await;
            let mut state = self.state.lock().await;
            for identity in &gone_elements {
                elem_failures.remove(identity);
                state.remove(identity);
            }
        }
        {
            let mut watch_failures = self.watch_failures.lock().await;
            let mut state = self.state.lock().await;
            for identity in &gone_watches {
                watch_failures.remove(identity);
                state.remove(identity);
            }
        }
        if !gone_elements.is_empty() || !gone_watches.is_empty() {
            self.flush_state().await;
        }

        // ③ 换配置并重建：新增的与被改动的都按新配置起任务。
        *self.config.lock().await = new_config.clone();
        for identity in added_pages.iter().chain(changed_pages.iter()) {
            let page = new_pages[identity].clone();
            self.spawn_page(page).await;
        }
        for identity in added_watches.iter().chain(changed_watches.iter()) {
            let watch = new_watches[identity].clone();
            self.spawn_watch(watch).await;
        }

        let added = added_pages.len() + added_watches.len();
        let removed = gone_pages.len() + gone_watches.len();
        tracing::info!(
            "配置热重载完成：新增 {added}、移除 {removed}、重启 {}",
            changed_pages.len() + changed_watches.len()
        );
        (added, removed)
    }

    // ---- 试抓：/add 向导在保存前的一次性验证抓取 ----

    pub async fn trial_fetch_page(self: &Arc<Self>, page: Page) -> PageResult {
        let _permit = self.acquire_permit().await;
        self.browser.fetch_page(&page).await
    }

    pub async fn trial_fetch_list(self: &Arc<Self>, watch: WatchTarget) -> ListResult {
        let _permit = self.acquire_permit().await;
        self.browser.fetch_list(&watch).await
    }

    async fn acquire_permit(self: &Arc<Self>) -> OwnedSemaphorePermit {
        let sem = Arc::clone(&self.semaphore);
        sem.acquire_owned().await.expect("信号量未关闭")
    }

    // ---- 快照：/list 的数据来源 ----

    /// 列出当前生效的全部监控及其状态（读内存，不触碰磁盘）。
    pub async fn snapshot(&self) -> Vec<MonitorRow> {
        let config = self.config().await;
        let state = self.state.lock().await;
        let mut rows = Vec::new();
        for page in config.pages() {
            for element in &page.elements {
                rows.push(MonitorRow {
                    kind: KIND_ELEMENT,
                    identity: element.identity(),
                    url: page.url.clone(),
                    status: status_of(&state, &element.identity()),
                });
            }
        }
        for watch in &config.watches {
            rows.push(MonitorRow {
                kind: KIND_WATCH,
                identity: watch.identity(),
                url: watch.url.clone(),
                status: status_of(&state, &watch.identity()),
            });
        }
        rows
    }

    // ---- 元素文本监控循环 ----

    async fn run_page(self: Arc<Self>, page: Page) {
        self.page_failures
            .lock()
            .await
            .entry(page.identity())
            .or_default();
        {
            let mut elem_failures = self.elem_failures.lock().await;
            for element in &page.elements {
                elem_failures.get_mut(&element.identity());
            }
        }
        let mut stop = self.subscribe_stop();
        loop {
            if *stop.borrow() {
                break;
            }
            if let Err(e) = self.poll_page(&page).await {
                tracing::error!("页面 {} 轮询出现未预期异常：{e}", page.identity());
            }
            // 可被停止事件立即打断的间隔等待。
            tokio::select! {
                _ = tokio::time::sleep(Duration::from_secs(page.poll_interval_secs.max(1) as u64)) => {}
                _ = stop.changed() => {}
            }
        }
    }

    pub(crate) async fn poll_page(self: &Arc<Self>, page: &Page) -> anyhow::Result<()> {
        let permit = self.acquire_permit().await;
        let result = self.browser.fetch_page(page).await;
        drop(permit);

        let identity = page.identity();
        match result {
            PageResult::LoadError { reason } => {
                tracing::warn!("页面 {identity} 加载失败：{reason}");
                let should_alert = {
                    let mut failures = self.page_failures.lock().await;
                    let f = failures.entry(identity.clone()).or_default();
                    f.record_failure(page.failure_threshold)
                };
                if should_alert {
                    let sent = self
                        .send_failure_alert(
                            &identity,
                            page.failure_threshold,
                            &reason,
                            Some(&page.url),
                        )
                        .await;
                    if sent {
                        self.page_failures
                            .lock()
                            .await
                            .entry(identity)
                            .or_default()
                            .mark_alerted();
                    } else {
                        tracing::error!("页面 {identity} 失败告警发送失败，下轮重试");
                    }
                }
                return Ok(());
            }
            PageResult::Fetched { results } => {
                self.page_failures
                    .lock()
                    .await
                    .entry(identity)
                    .or_default()
                    .record_success();
                for (element, el_result) in results {
                    self.handle_element(page, &element, el_result).await;
                }
            }
        }
        Ok(())
    }

    async fn handle_element(
        self: &Arc<Self>,
        page: &Page,
        element: &MonitoredElement,
        el_result: FetchResult,
    ) {
        let identity = element.identity();
        match el_result {
            FetchResult::Ok { value } => {
                self.elem_failures
                    .lock()
                    .await
                    .get_mut(&identity)
                    .record_success();
                self.handle_value(page, element, value).await;
            }
            FetchResult::NoMatch { reason } | FetchResult::Error { reason } => {
                // 原因一律取抓取层给的原话，调度层不再硬编码一句「选择器未匹配」。
                tracing::warn!("元素 {identity} 抓取失败：{reason}");
                let should_alert = {
                    let mut failures = self.elem_failures.lock().await;
                    let f = failures.get_mut(&identity);
                    f.record_failure(page.failure_threshold)
                };
                if should_alert {
                    let jump_url = element.url.clone().unwrap_or_else(|| page.url.clone());
                    let sent = self
                        .send_failure_alert(
                            &identity,
                            page.failure_threshold,
                            &reason,
                            Some(&jump_url),
                        )
                        .await;
                    if sent {
                        self.elem_failures
                            .lock()
                            .await
                            .get_mut(&identity)
                            .mark_alerted();
                    } else {
                        tracing::error!("元素 {identity} 失败告警发送失败，下轮重试");
                    }
                }
            }
        }
    }

    async fn handle_value(
        self: &Arc<Self>,
        page: &Page,
        element: &MonitoredElement,
        value: String,
    ) {
        let identity = element.identity();
        let previous = {
            let state = self.state.lock().await;
            match state.get(&identity) {
                Some(Entry::State { value, .. }) => Some(value.clone()),
                _ => None,
            }
        };
        match detect(previous.as_deref(), &value) {
            DetectResult::Unchanged { .. } => {
                tracing::debug!("元素 {identity} 未变更：{value}");
            }
            DetectResult::Baseline { .. } => {
                tracing::info!("元素 {identity} 建立基线：{value}");
                self.store(
                    &identity,
                    Entry::State {
                        value,
                        updated_at: now_iso(),
                    },
                )
                .await;
            }
            DetectResult::Changed { old, new } => {
                tracing::info!("元素 {identity} 变更：{old} → {new}");
                let jump_url = element.url.clone().unwrap_or_else(|| page.url.clone());
                let text =
                    format_change_message(&identity, &old, &new, &now_iso(), Some(&jump_url));
                // 至少一次交付：发送成功后才更新已记录值；失败则保留旧值，下轮重试。
                if self.notifier.send(&text).await {
                    self.store(
                        &identity,
                        Entry::State {
                            value,
                            updated_at: now_iso(),
                        },
                    )
                    .await;
                } else {
                    tracing::error!("元素 {identity} 通知发送失败，保留旧值等待下次重试");
                }
            }
        }
    }

    async fn send_failure_alert(
        self: &Arc<Self>,
        label: &str,
        threshold: i64,
        reason: &str,
        url: Option<&str>,
    ) -> bool {
        let text = format_failure_message(label, threshold, reason, &now_iso(), url);
        self.notifier.send(&text).await
    }

    async fn store(self: &Arc<Self>, identity: &str, entry: Entry) {
        let mut state = self.state.lock().await;
        state.insert(identity.to_string(), entry);
        save_state(&self.state_path, &state);
    }

    async fn flush_state(&self) {
        let state = self.state.lock().await;
        save_state(&self.state_path, &state);
    }

    // ---- 列表新条目监控 ----

    async fn run_watch(self: Arc<Self>, watch: WatchTarget) {
        self.watch_failures.lock().await.get_mut(&watch.identity());
        let mut stop = self.subscribe_stop();
        loop {
            if *stop.borrow() {
                break;
            }
            if let Err(e) = self.poll_watch(&watch).await {
                tracing::error!("列表 {} 轮询出现未预期异常：{e}", watch.identity());
            }
            tokio::select! {
                _ = tokio::time::sleep(Duration::from_secs(watch.poll_interval_secs.max(1) as u64)) => {}
                _ = stop.changed() => {}
            }
        }
    }

    pub(crate) async fn poll_watch(self: &Arc<Self>, watch: &WatchTarget) -> anyhow::Result<()> {
        let permit = self.acquire_permit().await;
        let result = self.browser.fetch_list(watch).await;
        drop(permit);

        let identity = watch.identity();
        match result {
            ListResult::LoadError { reason } => {
                tracing::warn!("列表 {identity} 加载失败：{reason}");
                self.alert_watch_failure(watch, &reason).await;
                return Ok(());
            }
            ListResult::Fetched { items } => {
                // 空提取视为失败：此时不应把已见集合当作「全新一轮」误判，
                // 否则下轮页面恢复时会把历史帖当新帖刷屏。
                if items.is_empty() {
                    let reason = "列表项提取为空（选择器未匹配或页面结构变化）";
                    tracing::warn!("列表 {identity} {reason}");
                    self.alert_watch_failure(watch, reason).await;
                    return Ok(());
                }
                self.watch_failures
                    .lock()
                    .await
                    .get_mut(&identity)
                    .record_success();
                self.handle_watch(watch, items).await;
            }
        }
        Ok(())
    }

    async fn alert_watch_failure(self: &Arc<Self>, watch: &WatchTarget, reason: &str) {
        let identity = watch.identity();
        let should_alert = {
            let mut failures = self.watch_failures.lock().await;
            failures
                .get_mut(&identity)
                .record_failure(watch.failure_threshold)
        };
        if should_alert {
            let sent = self
                .send_failure_alert(&identity, watch.failure_threshold, reason, Some(&watch.url))
                .await;
            if sent {
                self.watch_failures
                    .lock()
                    .await
                    .get_mut(&identity)
                    .mark_alerted();
            } else {
                tracing::error!("列表 {identity} 失败告警发送失败，下轮重试");
            }
        }
    }

    async fn handle_watch(self: &Arc<Self>, watch: &WatchTarget, items: Vec<ListItem>) {
        let identity = watch.identity();
        let (previous_seen, prior) = {
            let state = self.state.lock().await;
            match state.get(&identity) {
                Some(Entry::SeenSet { seen_ids, .. }) => {
                    let set: HashSet<String> = seen_ids.iter().cloned().collect();
                    (Some(set), seen_ids.clone())
                }
                _ => (None, Vec::new()),
            }
        };
        let current_ids: Vec<String> = items.iter().map(|i| i.post_id.clone()).collect();
        match detect_new(previous_seen.as_ref(), &current_ids) {
            DetectNewResult::SeenBaseline { ids } => {
                tracing::info!(
                    "列表 {identity} 首次运行，静默建立基线：{} 个帖子",
                    ids.len()
                );
                self.store(
                    &identity,
                    Entry::SeenSet {
                        seen_ids: ids,
                        updated_at: now_iso(),
                    },
                )
                .await;
            }
            DetectNewResult::NewItems { new_ids } => {
                if new_ids.is_empty() {
                    tracing::debug!("列表 {identity} 无新帖");
                    return;
                }
                // 帖子 ID → 列表项；同 ID 保留首个。
                let mut by_id: HashMap<String, &ListItem> = HashMap::new();
                for item in &items {
                    by_id.entry(item.post_id.clone()).or_insert(item);
                }
                // 命中关键字的新帖逐条通知；未命中的新帖也需记入已见。
                // 发送成功的帖子才算「已交付」并入已见（至少一次交付）。
                let mut confirmed: Vec<String> = Vec::new();
                for pid in &new_ids {
                    let Some(post) = by_id.get(pid) else { continue };
                    if matches(&watch.keywords, &post.title) {
                        let text = format_new_post_message(&post.title, &post.url, &now_iso());
                        if self.notifier.send(&text).await {
                            tracing::info!("列表 {identity} 命中新帖并已通知：{}", post.title);
                            confirmed.push(pid.clone());
                        } else {
                            tracing::error!(
                                "列表 {identity} 新帖通知发送失败，下轮重试：{}",
                                post.title
                            );
                        }
                    } else {
                        confirmed.push(pid.clone());
                    }
                }
                if !confirmed.is_empty() {
                    let merged = merge_seen(
                        &prior,
                        &current_ids,
                        &confirmed,
                        crate::detect::SEEN_IDS_MAX,
                    );
                    self.store(
                        &identity,
                        Entry::SeenSet {
                            seen_ids: merged,
                            updated_at: now_iso(),
                        },
                    )
                    .await;
                }
            }
        }
    }
}

fn status_of(state: &State, identity: &str) -> String {
    match state.get(identity) {
        Some(Entry::State { value, .. }) => value.clone(),
        Some(Entry::SeenSet { seen_ids, .. }) => format!("已见 {} 帖", seen_ids.len()),
        None => "尚未建立基线".to_string(),
    }
}

#[async_trait]
impl SchedulerApi for Arc<Scheduler> {
    async fn config(&self) -> Config {
        Scheduler::config(self).await
    }

    async fn snapshot(&self) -> Vec<MonitorRow> {
        Scheduler::snapshot(self).await
    }

    async fn reconcile(&self, new_config: Config) -> (usize, usize) {
        Scheduler::reconcile(self, new_config).await
    }

    async fn trial_fetch_page(&self, page: Page) -> PageResult {
        Scheduler::trial_fetch_page(self, page).await
    }

    async fn trial_fetch_list(&self, watch: WatchTarget) -> ListResult {
        Scheduler::trial_fetch_list(self, watch).await
    }
}

#[async_trait]
impl FetchBackend for crate::fetch::BrowserManager {
    async fn fetch_page(&self, page: &Page) -> PageResult {
        crate::fetch::BrowserManager::fetch_page(self, page).await
    }

    async fn fetch_list(&self, watch: &WatchTarget) -> ListResult {
        crate::fetch::BrowserManager::fetch_list(self, watch).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{Merchant, TelegramConfig};
    use crate::detect::SEEN_IDS_MAX;
    use crate::notify::{BotCommand, TelegramFatalError};
    use std::future::pending;

    // ---- 脚本化假件 ----

    struct MockBackend {
        page_results: Mutex<Vec<PageResult>>,
        list_results: Mutex<Vec<ListResult>>,
        page_calls: AtomicU64,
        list_calls: AtomicU64,
    }

    impl MockBackend {
        fn pages(results: Vec<PageResult>) -> Arc<Self> {
            Arc::new(Self {
                page_results: Mutex::new(results),
                list_results: Mutex::new(vec![ListResult::Fetched { items: vec![] }]),
                page_calls: AtomicU64::new(0),
                list_calls: AtomicU64::new(0),
            })
        }

        fn lists(results: Vec<ListResult>) -> Arc<Self> {
            Arc::new(Self {
                page_results: Mutex::new(vec![PageResult::Fetched { results: vec![] }]),
                list_results: Mutex::new(results),
                page_calls: AtomicU64::new(0),
                list_calls: AtomicU64::new(0),
            })
        }

        fn dual(page: PageResult, list: ListResult) -> Arc<Self> {
            Arc::new(Self {
                page_results: Mutex::new(vec![page]),
                list_results: Mutex::new(vec![list]),
                page_calls: AtomicU64::new(0),
                list_calls: AtomicU64::new(0),
            })
        }
    }

    use std::sync::atomic::{AtomicU64, Ordering as AtomicOrdering};

    #[async_trait]
    impl FetchBackend for MockBackend {
        async fn fetch_page(&self, _page: &Page) -> PageResult {
            self.page_calls.fetch_add(1, AtomicOrdering::SeqCst);
            let mut results = self.page_results.lock().await;
            match results.len() {
                0 => PageResult::Fetched { results: vec![] },
                1 => results[0].clone(),
                _ => results.remove(0),
            }
        }

        async fn fetch_list(&self, _watch: &WatchTarget) -> ListResult {
            self.list_calls.fetch_add(1, AtomicOrdering::SeqCst);
            let mut results = self.list_results.lock().await;
            match results.len() {
                0 => ListResult::Fetched { items: vec![] },
                1 => results[0].clone(),
                _ => results.remove(0),
            }
        }
    }

    /// 永远挂起的后端：让 run() 起的任务停在抓取上，不产生轮询副作用。
    struct HangingBackend;

    #[async_trait]
    impl FetchBackend for HangingBackend {
        async fn fetch_page(&self, _page: &Page) -> PageResult {
            pending().await
        }

        async fn fetch_list(&self, _watch: &WatchTarget) -> ListResult {
            pending().await
        }
    }

    struct MockNotifier {
        /// 按序返回的 send 结果；耗尽后沿用最后一个。
        results: std::sync::Mutex<Vec<bool>>,
        sent: std::sync::Mutex<Vec<String>>,
    }

    impl MockNotifier {
        fn new(results: Vec<bool>) -> Arc<Self> {
            Arc::new(Self {
                results: std::sync::Mutex::new(results),
                sent: std::sync::Mutex::new(Vec::new()),
            })
        }

        fn sent(&self) -> Vec<String> {
            self.sent.lock().unwrap().clone()
        }
    }

    #[async_trait]
    impl NotifierApi for MockNotifier {
        async fn send(&self, text: &str) -> bool {
            let idx = self.sent.lock().unwrap().len();
            self.sent.lock().unwrap().push(text.to_string());
            let results = self.results.lock().unwrap();
            results.get(idx).copied().unwrap_or(true)
        }

        async fn sync_commands(&self, _commands: &[BotCommand]) -> String {
            "快捷菜单已同步。".to_string()
        }

        async fn verify(&self) -> Result<(), TelegramFatalError> {
            Ok(())
        }
    }

    // ---- 装配辅助 ----

    fn element(name: &str) -> MonitoredElement {
        MonitoredElement {
            merchant_name: "m".into(),
            page_name: "p".into(),
            name: name.into(),
            selector: ".x".into(),
            selector_type: "css".into(),
            nth: None,
            js: None,
            url: None,
        }
    }

    fn page_with(threshold: i64, elements: Vec<MonitoredElement>) -> Page {
        Page {
            merchant_name: "m".into(),
            name: "p".into(),
            url: "https://e.com".into(),
            poll_interval_secs: 60,
            wait_until: "load".into(),
            nav_timeout_secs: 30,
            failure_threshold: threshold,
            elements,
            fingerprint: Default::default(),
            proxy: None,
        }
    }

    fn page_named(name: &str, poll: i64, elements: Vec<MonitoredElement>) -> Page {
        let mut p = page_with(1, elements);
        p.name = name.into();
        p.poll_interval_secs = poll;
        p
    }

    fn watch_named(name: &str, keywords: &[&str], threshold: i64) -> WatchTarget {
        WatchTarget {
            name: name.into(),
            url: "https://www.nodeseek.com/".into(),
            link_selector: "a".into(),
            selector_type: "css".into(),
            keywords: keywords.iter().map(|s| s.to_string()).collect(),
            id_pattern: None,
            poll_interval_secs: 60,
            wait_until: "load".into(),
            nav_timeout_secs: 30,
            failure_threshold: threshold,
            fingerprint: Default::default(),
            proxy: None,
        }
    }

    fn state_path(tag: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("hawkeye_sched_test_{}_{}", std::process::id(), tag));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir.join("state.json")
    }

    fn config_with(
        tag: &str,
        pages: Vec<Page>,
        watches: Vec<WatchTarget>,
        max_concurrent: i64,
    ) -> Config {
        Config {
            telegram: TelegramConfig {
                bot_token: "x".into(),
                chat_id: "1".into(),
            },
            merchants: vec![Merchant {
                name: "m".into(),
                pages,
                fingerprint: Default::default(),
                proxy: None,
            }],
            poll_interval_secs: 60,
            failure_threshold: 3,
            state_path: state_path(tag).to_string_lossy().into(),
            max_concurrent_fetches: max_concurrent,
            nav_timeout_secs: 30,
            wait_until: "load".into(),
            watches,
            fingerprint: Default::default(),
            proxy: None,
        }
    }

    fn item(post_id: &str, title: &str) -> ListItem {
        ListItem {
            post_id: post_id.into(),
            title: title.into(),
            url: format!("https://www.nodeseek.com/post-{post_id}-1"),
        }
    }

    fn fetched(values: Vec<(MonitoredElement, FetchResult)>) -> PageResult {
        PageResult::Fetched { results: values }
    }

    fn setup(
        _tag: &str,
        config: Config,
        backend: Arc<dyn FetchBackend>,
        notifier: Arc<MockNotifier>,
    ) -> Arc<Scheduler> {
        let (scheduler, _stop) = Scheduler::new(config, backend, notifier);
        scheduler
    }

    // ---- 元素监控 ----

    #[tokio::test]
    async fn test_page_load_failure_retries_until_sent() {
        let page = page_with(1, vec![element("e")]);
        let config = config_with("pl1", vec![page.clone()], vec![], 4);
        let backend = MockBackend::pages(vec![PageResult::LoadError {
            reason: "boom".into(),
        }]);
        let notifier = MockNotifier::new(vec![false, true]);
        let sched = setup("pl1", config, backend, notifier.clone());

        sched.poll_page(&page).await.unwrap(); // 失败#1: 达阈值, 发送失败 -> 不抑制
        sched.poll_page(&page).await.unwrap(); // 失败#2: 仍应重试, 发送成功 -> 抑制
        sched.poll_page(&page).await.unwrap(); // 失败#3: 已抑制 -> 不再发送

        assert_eq!(notifier.sent().len(), 2);
        let failures = sched.page_failures.lock().await;
        assert!(
            failures
                .get(&page.identity())
                .map(|f| f.alerted)
                .unwrap_or(false)
        );
    }

    #[tokio::test]
    async fn test_element_failure_retries_until_sent() {
        let el = element("e");
        let page = page_with(1, vec![el.clone()]);
        let config = config_with("el1", vec![page.clone()], vec![], 4);
        let backend = MockBackend::pages(vec![fetched(vec![(
            el.clone(),
            FetchResult::Error {
                reason: "boom".into(),
            },
        )])]);
        let notifier = MockNotifier::new(vec![false, true]);
        let sched = setup("el1", config, backend, notifier.clone());

        sched.poll_page(&page).await.unwrap();
        sched.poll_page(&page).await.unwrap();
        sched.poll_page(&page).await.unwrap();

        assert_eq!(notifier.sent().len(), 2);
        let failures = sched.elem_failures.lock().await;
        let f = failures.get(&el.identity()).unwrap();
        assert!(f.alerted);
    }

    #[tokio::test]
    async fn test_no_match_reason_reaches_the_alert() {
        // 告警只许转达抓取层给的原话，不许硬编码一句「选择器未匹配」。
        let el = element("e");
        let page = page_with(1, vec![el.clone()]);
        let config = config_with("nm1", vec![page.clone()], vec![], 4);
        let reason = "选择器匹配到元素，但其文本为空".to_string();
        let backend = MockBackend::pages(vec![fetched(vec![(
            el.clone(),
            FetchResult::NoMatch {
                reason: reason.clone(),
            },
        )])]);
        let notifier = MockNotifier::new(vec![true]);
        let sched = setup("nm1", config, backend, notifier.clone());

        sched.poll_page(&page).await.unwrap();

        assert_eq!(notifier.sent().len(), 1);
        assert!(
            notifier.sent()[0].contains(&reason),
            "原话必须到达告警：{}",
            notifier.sent()[0]
        );
    }

    #[tokio::test]
    async fn test_change_stored_only_after_send_success() {
        let el = element("e");
        let page = page_with(3, vec![el.clone()]);
        let config = config_with("cs1", vec![page.clone()], vec![], 4);
        let backend = MockBackend::pages(vec![
            fetched(vec![(el.clone(), FetchResult::Ok { value: "A".into() })]),
            fetched(vec![(el.clone(), FetchResult::Ok { value: "B".into() })]),
            fetched(vec![(el.clone(), FetchResult::Ok { value: "B".into() })]),
        ]);
        let notifier = MockNotifier::new(vec![false, true]);
        let sched = setup("cs1", config.clone(), backend, notifier.clone());

        async fn state_value(sched: &Scheduler, identity: &str) -> Option<String> {
            sched.state.lock().await.get(identity).map(|e| match e {
                Entry::State { value, .. } => value.clone(),
                _ => String::new(),
            })
        }

        sched.poll_page(&page).await.unwrap(); // 基线 A: 静默落盘, 不发送
        assert!(notifier.sent().is_empty());
        assert_eq!(state_value(&sched, &el.identity()).await, Some("A".into()));

        sched.poll_page(&page).await.unwrap(); // A->B: 发送失败 -> 保留旧值 A
        assert_eq!(notifier.sent().len(), 1);
        assert_eq!(state_value(&sched, &el.identity()).await, Some("A".into()));

        sched.poll_page(&page).await.unwrap(); // 仍 B: 再次 A->B, 发送成功 -> 落盘 B
        assert_eq!(notifier.sent().len(), 2);
        assert_eq!(state_value(&sched, &el.identity()).await, Some("B".into()));
        let on_disk = load_state(PathBuf::from(&config.state_path).as_path());
        assert!(matches!(
            on_disk.get(&el.identity()),
            Some(Entry::State { value, .. }) if value == "B"
        ));
    }

    #[tokio::test]
    async fn test_unchanged_does_not_notify() {
        let el = element("e");
        let page = page_with(3, vec![el.clone()]);
        let config = config_with("uc1", vec![page.clone()], vec![], 4);
        let backend = MockBackend::pages(vec![fetched(vec![(
            el.clone(),
            FetchResult::Ok {
                value: "充足".into(),
            },
        )])]);
        let notifier = MockNotifier::new(vec![true]);
        let sched = setup("uc1", config, backend, notifier.clone());
        sched.state.lock().await.insert(
            el.identity(),
            Entry::State {
                value: "充足".into(),
                updated_at: "t0".into(),
            },
        );

        sched.poll_page(&page).await.unwrap(); // 值未变化: 不通知

        assert!(notifier.sent().is_empty());
    }

    #[tokio::test]
    async fn test_multiple_elements_share_one_page_load() {
        let e1 = element("是否可售");
        let e2 = element("价格");
        let page = page_with(3, vec![e1.clone(), e2.clone()]);
        let config = config_with("ms1", vec![page.clone()], vec![], 4);
        let backend = MockBackend::pages(vec![fetched(vec![
            (
                e1.clone(),
                FetchResult::Ok {
                    value: "充足".into(),
                },
            ),
            (
                e2.clone(),
                FetchResult::Ok {
                    value: "¥10".into(),
                },
            ),
        ])]);
        let notifier = MockNotifier::new(vec![true]);
        let sched = setup("ms1", config, backend.clone(), notifier.clone());

        sched.poll_page(&page).await.unwrap();

        assert_eq!(
            backend.page_calls.load(AtomicOrdering::SeqCst),
            1,
            "两个元素只触发一次页面加载"
        );
        let state = sched.state.lock().await;
        assert!(matches!(
            state.get(&e1.identity()),
            Some(Entry::State { value, .. }) if value == "充足"
        ));
        assert!(matches!(
            state.get(&e2.identity()),
            Some(Entry::State { value, .. }) if value == "¥10"
        ));
        drop(state);
        assert!(notifier.sent().is_empty(), "均为基线, 静默不打扰");
    }

    #[tokio::test]
    async fn test_mixed_element_results_alert_only_failing() {
        let ok = element("可售");
        let bad = element("价格");
        let page = page_with(1, vec![ok.clone(), bad.clone()]);
        let config = config_with("mx1", vec![page.clone()], vec![], 4);
        let backend = MockBackend::pages(vec![fetched(vec![
            (
                ok.clone(),
                FetchResult::Ok {
                    value: "充足".into(),
                },
            ),
            (
                bad.clone(),
                FetchResult::Error {
                    reason: "boom".into(),
                },
            ),
        ])]);
        let notifier = MockNotifier::new(vec![true]);
        let sched = setup("mx1", config, backend, notifier.clone());

        sched.poll_page(&page).await.unwrap();

        // 成功元素: 落基线, 不告警
        let state = sched.state.lock().await;
        assert!(
            matches!(state.get(&ok.identity()), Some(Entry::State { value, .. }) if value == "充足")
        );
        drop(state);
        // 失败元素: 达阈值发一条告警; 告警只针对失败元素
        assert_eq!(notifier.sent().len(), 1);
        assert!(notifier.sent()[0].contains(&bad.identity()));
        assert!(!notifier.sent()[0].contains(&ok.identity()));
        let failures = sched.elem_failures.lock().await;
        assert!(
            !failures
                .get(&ok.identity())
                .map(|f| f.alerted)
                .unwrap_or(false)
        );
        assert!(
            failures
                .get(&bad.identity())
                .map(|f| f.alerted)
                .unwrap_or(false)
        );
    }

    // ---- 列表新条目监控 ----

    fn watch_setup(
        tag: &str,
        watch: WatchTarget,
        backend: Arc<dyn FetchBackend>,
        notifier: Arc<MockNotifier>,
    ) -> (Arc<Scheduler>, WatchTarget, Config) {
        let threshold = watch.failure_threshold;
        let config = Config {
            telegram: TelegramConfig {
                bot_token: "x".into(),
                chat_id: "1".into(),
            },
            merchants: vec![],
            poll_interval_secs: 60,
            failure_threshold: threshold,
            state_path: state_path(tag).to_string_lossy().into(),
            max_concurrent_fetches: 4,
            nav_timeout_secs: 30,
            wait_until: "load".into(),
            watches: vec![watch.clone()],
            fingerprint: Default::default(),
            proxy: None,
        };
        let (sched, _stop) = Scheduler::new(config.clone(), backend, notifier);
        (sched, watch, config)
    }

    async fn seen_of(sched: &Scheduler, identity: &str) -> Option<Vec<String>> {
        sched.state.lock().await.get(identity).map(|e| match e {
            Entry::SeenSet { seen_ids, .. } => seen_ids.clone(),
            _ => vec![],
        })
    }

    #[tokio::test]
    async fn test_watch_first_run_silent_baseline() {
        let watch = watch_named("NS", &["hk"], 1);
        let backend = MockBackend::lists(vec![ListResult::Fetched {
            items: vec![item("100", "HK 节点"), item("101", "其它")],
        }]);
        let notifier = MockNotifier::new(vec![true]);
        let (sched, watch, _config) = watch_setup("wb1", watch, backend, notifier.clone());

        sched.poll_watch(&watch).await.unwrap();

        assert!(notifier.sent().is_empty());
        let seen = seen_of(&sched, &watch.identity()).await.unwrap();
        assert_eq!(
            seen.iter()
                .cloned()
                .collect::<std::collections::HashSet<_>>(),
            ["100", "101"].into_iter().map(String::from).collect()
        );
    }

    #[tokio::test]
    async fn test_watch_new_matching_post_notifies_and_records() {
        let watch = watch_named("NS", &["hk"], 1);
        let backend = MockBackend::lists(vec![
            ListResult::Fetched {
                items: vec![item("100", "老帖")],
            },
            ListResult::Fetched {
                items: vec![item("200", "HK 原生 IP"), item("100", "老帖")],
            },
        ]);
        let notifier = MockNotifier::new(vec![true]);
        let (sched, watch, _config) = watch_setup("wb2", watch, backend, notifier.clone());

        sched.poll_watch(&watch).await.unwrap(); // 基线 {100}
        assert!(notifier.sent().is_empty());

        sched.poll_watch(&watch).await.unwrap(); // 新帖 200 命中 hk
        assert_eq!(notifier.sent().len(), 1);
        assert!(notifier.sent()[0].contains("HK 原生 IP"));
        assert!(notifier.sent()[0].contains("https://www.nodeseek.com/post-200-1"));
        let seen = seen_of(&sched, &watch.identity()).await.unwrap();
        assert_eq!(
            seen.iter()
                .cloned()
                .collect::<std::collections::HashSet<_>>(),
            ["100", "200"].into_iter().map(String::from).collect()
        );
    }

    #[tokio::test]
    async fn test_watch_seen_id_bumped_not_renotified() {
        // 已见 ID 的帖被顶到列表首位（顺序变化）→ 不判为新帖、不推送。
        let watch = watch_named("NS", &["hk"], 1);
        let backend = MockBackend::lists(vec![
            ListResult::Fetched {
                items: vec![item("100", "HK 老帖"), item("101", "其它")],
            },
            ListResult::Fetched {
                items: vec![item("101", "其它"), item("100", "HK 老帖")],
            },
        ]);
        let notifier = MockNotifier::new(vec![true]);
        let (sched, watch, _config) = watch_setup("wb3", watch, backend, notifier.clone());

        sched.poll_watch(&watch).await.unwrap();
        sched.poll_watch(&watch).await.unwrap();
        assert!(notifier.sent().is_empty());
    }

    #[tokio::test]
    async fn test_watch_new_non_matching_recorded_not_notified() {
        // 新帖标题未命中 → 不通知但记入已见, 下轮不再判新。
        let watch = watch_named("NS", &["hk"], 1);
        let backend = MockBackend::lists(vec![
            ListResult::Fetched {
                items: vec![item("100", "老帖")],
            },
            ListResult::Fetched {
                items: vec![item("300", "美国 VPS"), item("100", "老帖")],
            },
            ListResult::Fetched {
                items: vec![item("300", "美国 VPS"), item("100", "老帖")],
            },
        ]);
        let notifier = MockNotifier::new(vec![true]);
        let (sched, watch, _config) = watch_setup("wb4", watch, backend, notifier.clone());

        sched.poll_watch(&watch).await.unwrap();
        sched.poll_watch(&watch).await.unwrap();
        assert!(notifier.sent().is_empty());
        assert!(
            seen_of(&sched, &watch.identity())
                .await
                .unwrap()
                .contains(&"300".to_string())
        );

        sched.poll_watch(&watch).await.unwrap();
        assert!(notifier.sent().is_empty());
    }

    #[tokio::test]
    async fn test_watch_send_failure_retries_next_round() {
        // 命中新帖发送失败 → 不记入 ID; 下轮仍判新并重试直至成功。
        let watch = watch_named("NS", &["hk"], 1);
        let backend = MockBackend::lists(vec![
            ListResult::Fetched {
                items: vec![item("100", "老帖")],
            },
            ListResult::Fetched {
                items: vec![item("200", "HK 新帖"), item("100", "老帖")],
            },
            ListResult::Fetched {
                items: vec![item("200", "HK 新帖"), item("100", "老帖")],
            },
        ]);
        let notifier = MockNotifier::new(vec![false, true]);
        let (sched, watch, _config) = watch_setup("wb5", watch, backend, notifier.clone());

        sched.poll_watch(&watch).await.unwrap(); // 基线 {100}
        sched.poll_watch(&watch).await.unwrap(); // 200 发送失败 → 不记入
        assert_eq!(notifier.sent().len(), 1);
        assert!(
            !seen_of(&sched, &watch.identity())
                .await
                .unwrap()
                .contains(&"200".to_string())
        );

        sched.poll_watch(&watch).await.unwrap(); // 200 仍判新, 重试成功 → 记入
        assert_eq!(notifier.sent().len(), 2);
        assert!(
            seen_of(&sched, &watch.identity())
                .await
                .unwrap()
                .contains(&"200".to_string())
        );
    }

    #[tokio::test]
    async fn test_watch_load_failure_alert_threshold_and_reset() {
        // 加载失败连续达阈值发一条、边沿触发不刷屏、成功后复位。
        let watch = watch_named("NS", &["hk"], 2);
        let backend = MockBackend::lists(vec![
            ListResult::LoadError {
                reason: "boom".into(),
            },
            ListResult::LoadError {
                reason: "boom".into(),
            },
            ListResult::LoadError {
                reason: "boom".into(),
            },
            ListResult::Fetched {
                items: vec![item("100", "HK")],
            },
        ]);
        let notifier = MockNotifier::new(vec![true]);
        let (sched, watch, _config) = watch_setup("wb6", watch, backend, notifier.clone());

        sched.poll_watch(&watch).await.unwrap(); // 失败#1: 未达阈值(2)
        assert!(notifier.sent().is_empty());
        sched.poll_watch(&watch).await.unwrap(); // 失败#2: 达阈值 → 发一条
        assert_eq!(notifier.sent().len(), 1);
        assert!(notifier.sent()[0].contains(&watch.identity()));
        sched.poll_watch(&watch).await.unwrap(); // 失败#3: 已抑制 → 不再发
        assert_eq!(notifier.sent().len(), 1);
        {
            let failures = sched.watch_failures.lock().await;
            let f = failures.get(&watch.identity()).unwrap();
            assert!(f.alerted);
        }

        sched.poll_watch(&watch).await.unwrap(); // 恢复 → record_success 复位
        let failures = sched.watch_failures.lock().await;
        let f = failures.get(&watch.identity()).unwrap();
        assert!(!f.alerted);
        assert_eq!(f.consecutive_failures, 0);
    }

    #[tokio::test]
    async fn test_watch_empty_extraction_treated_as_failure() {
        // 空提取视为失败：走失败告警而非建基线。
        let watch = watch_named("NS", &["hk"], 1);
        let backend = MockBackend::lists(vec![ListResult::Fetched { items: vec![] }]);
        let notifier = MockNotifier::new(vec![true]);
        let (sched, watch, _config) = watch_setup("wb7", watch, backend, notifier.clone());

        sched.poll_watch(&watch).await.unwrap();

        assert_eq!(notifier.sent().len(), 1);
        assert!(notifier.sent()[0].contains(&watch.identity()));
        assert!(sched.state.lock().await.get(&watch.identity()).is_none());
    }

    #[tokio::test]
    async fn test_watch_multiple_new_matches_each_notified() {
        // 一轮多个命中新帖各发一条；HK / hk 均命中（不区分大小写）。
        let watch = watch_named("NS", &["hk"], 1);
        let backend = MockBackend::lists(vec![
            ListResult::Fetched {
                items: vec![item("100", "老帖")],
            },
            ListResult::Fetched {
                items: vec![
                    item("201", "HK 甲"),
                    item("202", "US 乙"),
                    item("203", "hk 丙"),
                    item("100", "老帖"),
                ],
            },
        ]);
        let notifier = MockNotifier::new(vec![true]);
        let (sched, watch, _config) = watch_setup("wb8", watch, backend, notifier.clone());

        sched.poll_watch(&watch).await.unwrap(); // 基线 {100}
        sched.poll_watch(&watch).await.unwrap();

        assert_eq!(notifier.sent().len(), 2, "201 与 203 各一条; 202 未命中");
        assert!(notifier.sent().iter().any(|m| m.contains("HK 甲")));
        assert!(notifier.sent().iter().any(|m| m.contains("hk 丙")));
        let seen = seen_of(&sched, &watch.identity()).await.unwrap();
        assert_eq!(
            seen.iter()
                .cloned()
                .collect::<std::collections::HashSet<_>>(),
            ["100", "201", "202", "203"]
                .into_iter()
                .map(String::from)
                .collect()
        );
    }

    #[tokio::test]
    async fn test_watch_seen_set_capped_at_limit() {
        // 已见集合有条数上限：达上限后新帖挤掉最久未见的 ID。
        let watch = watch_named("NS", &["hk"], 1);
        let backend = MockBackend::lists(vec![ListResult::Fetched {
            items: vec![item("900001", "HK 新帖")],
        }]);
        let notifier = MockNotifier::new(vec![true]);
        let (sched, watch, config) = watch_setup("wb9", watch, backend, notifier.clone());
        sched.state.lock().await.insert(
            watch.identity(),
            Entry::SeenSet {
                seen_ids: (0..SEEN_IDS_MAX).map(|i| i.to_string()).collect(),
                updated_at: "t0".into(),
            },
        );

        sched.poll_watch(&watch).await.unwrap();

        assert_eq!(notifier.sent().len(), 1);
        let seen = seen_of(&sched, &watch.identity()).await.unwrap();
        assert_eq!(seen.len(), SEEN_IDS_MAX);
        assert_eq!(seen.last().unwrap(), "900001");
        assert!(!seen.contains(&"0".to_string()), "最久未见的被淘汰");
        let on_disk = load_state(PathBuf::from(&config.state_path).as_path());
        match on_disk.get(&watch.identity()) {
            Some(Entry::SeenSet { seen_ids, .. }) => assert_eq!(seen_ids.len(), SEEN_IDS_MAX),
            other => panic!("state 落盘异常：{other:?}"),
        }
    }

    #[tokio::test]
    async fn test_element_and_watch_modes_coexist() {
        // 元素监控与列表监控共用一个 Scheduler / 状态文件, 互不干扰。
        let el = element("e");
        let page = page_with(3, vec![el.clone()]);
        let watch = watch_named("NS", &["hk"], 3);
        let config = Config {
            telegram: TelegramConfig {
                bot_token: "x".into(),
                chat_id: "1".into(),
            },
            merchants: vec![Merchant {
                name: "m".into(),
                pages: vec![page.clone()],
                fingerprint: Default::default(),
                proxy: None,
            }],
            poll_interval_secs: 60,
            failure_threshold: 3,
            state_path: state_path("co1").to_string_lossy().into(),
            max_concurrent_fetches: 4,
            nav_timeout_secs: 30,
            wait_until: "load".into(),
            watches: vec![watch.clone()],
            fingerprint: Default::default(),
            proxy: None,
        };
        let backend = MockBackend::dual(
            fetched(vec![(
                el.clone(),
                FetchResult::Ok {
                    value: "充足".into(),
                },
            )]),
            ListResult::Fetched {
                items: vec![item("100", "HK 帖")],
            },
        );
        let notifier = MockNotifier::new(vec![true]);
        let (sched, _stop) = Scheduler::new(config, backend, notifier.clone());

        sched.poll_page(&page).await.unwrap();
        sched.poll_watch(&watch).await.unwrap();

        assert!(notifier.sent().is_empty());
        let state = sched.state.lock().await;
        assert!(
            matches!(state.get(&el.identity()), Some(Entry::State { value, .. }) if value == "充足")
        );
        assert!(matches!(
            state.get(&watch.identity()),
            Some(Entry::SeenSet { .. })
        ));
    }

    // ---- run / reconcile ----

    async fn start_scheduler(
        _tag: &str,
        config: Config,
    ) -> (Arc<Scheduler>, tokio::task::JoinHandle<()>) {
        let (sched, _stop) = Scheduler::new(
            config,
            Arc::new(HangingBackend),
            MockNotifier::new(vec![true]),
        );
        let runner = tokio::spawn({
            let s = Arc::clone(&sched);
            async move { s.run().await }
        });
        // 等任务登记表填好。
        let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(5);
        while sched.tasks.lock().await.is_empty() {
            assert!(tokio::time::Instant::now() < deadline, "任务未启动");
            tokio::time::sleep(std::time::Duration::from_millis(5)).await;
        }
        let _ = _tag;
        (sched, runner)
    }

    async fn shutdown(sched: &Scheduler, runner: tokio::task::JoinHandle<()>) {
        sched.request_stop();
        let _ = runner.await;
    }

    async fn task_keys(sched: &Scheduler) -> Vec<String> {
        sched.tasks.lock().await.keys().cloned().collect()
    }

    #[tokio::test]
    async fn test_run_registers_tasks_by_identity_key() {
        let config = config_with(
            "rk1",
            vec![page_named("p1", 60, vec![element("e")])],
            vec![watch_named("W1", &["hk"], 1)],
            4,
        );
        let (sched, runner) = start_scheduler("rk1", config).await;

        let mut keys = task_keys(&sched).await;
        keys.sort();
        assert_eq!(
            keys,
            vec!["page:m / p1".to_string(), "watch:watch / W1".to_string()]
        );

        shutdown(&sched, runner).await;
        assert!(sched.tasks.lock().await.is_empty(), "停止后任务表清空");
    }

    #[tokio::test]
    async fn test_reconcile_adds_and_removes() {
        let config = config_with(
            "ra1",
            vec![page_named("p1", 60, vec![element("e")])],
            vec![watch_named("W1", &["hk"], 1)],
            4,
        );
        let (sched, runner) = start_scheduler("ra1", config).await;

        let new_config = config_with(
            "ra1b",
            vec![page_named("p2", 60, vec![element("e")])],
            vec![watch_named("W2", &["hk"], 1)],
            4,
        );
        let (added, removed) = sched.reconcile(new_config).await;

        assert_eq!((added, removed), (2, 2));
        let mut keys = task_keys(&sched).await;
        keys.sort();
        assert_eq!(
            keys,
            vec!["page:m / p2".to_string(), "watch:watch / W2".to_string()]
        );
        shutdown(&sched, runner).await;
    }

    #[tokio::test]
    async fn test_reconcile_removal_leaves_surviving_page_running() {
        // 删掉一个页面时，其余页面原样继续——同一个 Task，未被连带取消。
        let config = config_with(
            "rr1",
            vec![
                page_named("keep", 60, vec![element("e")]),
                page_named("drop", 60, vec![element("e")]),
            ],
            vec![],
            4,
        );
        let (sched, runner) = start_scheduler("rr1", config).await;
        let survivor_id = sched.tasks.lock().await.get("page:m / keep").unwrap().id();

        let new_config = config_with(
            "rr1b",
            vec![page_named("keep", 60, vec![element("e")])],
            vec![],
            4,
        );
        let (added, removed) = sched.reconcile(new_config).await;

        assert_eq!((added, removed), (0, 1));
        let tasks = sched.tasks.lock().await;
        assert_eq!(tasks.len(), 1);
        assert_eq!(
            tasks.get("page:m / keep").unwrap().id(),
            survivor_id,
            "存活任务未被重启"
        );
        drop(tasks);
        shutdown(&sched, runner).await;
    }

    #[tokio::test]
    async fn test_reconcile_keeps_identical_targets_running() {
        // 结构相等的目标任务原样保留，失败计数与告警抑制不丢。
        let page = page_named("p1", 60, vec![element("e")]);
        let watch = watch_named("W1", &["hk"], 1);
        let config = config_with("rk2", vec![page.clone()], vec![watch.clone()], 4);
        let (sched, runner) = start_scheduler("rk2", config).await;
        {
            let mut failures = sched.page_failures.lock().await;
            let f = failures.entry(page.identity()).or_default();
            f.record_failure(1);
            f.mark_alerted();
        }

        let new_config = config_with(
            "rk2b",
            vec![page_named("p1", 60, vec![element("e")])],
            vec![watch_named("W1", &["hk"], 1)],
            4,
        );
        let (added, removed) = sched.reconcile(new_config).await;

        assert_eq!((added, removed), (0, 0));
        let failures = sched.page_failures.lock().await;
        assert!(
            failures
                .get(&page.identity())
                .map(|f| f.alerted)
                .unwrap_or(false)
        );
        drop(failures);
        let watch_failures = sched.watch_failures.lock().await;
        assert!(watch_failures.contains_key(&watch.identity()));
        drop(watch_failures);
        shutdown(&sched, runner).await;
    }

    #[tokio::test]
    async fn test_reconcile_restarts_changed_target() {
        // 标识不变但内容变了（改了轮询间隔）→ 先撤后建，不计入增减。
        let config = config_with(
            "rc1",
            vec![page_named("p1", 60, vec![element("e")])],
            vec![],
            4,
        );
        let (sched, runner) = start_scheduler("rc1", config).await;
        let old_id = sched.tasks.lock().await.get("page:m / p1").unwrap().id();

        let new_config = config_with(
            "rc1b",
            vec![page_named("p1", 30, vec![element("e")])],
            vec![],
            4,
        );
        let (added, removed) = sched.reconcile(new_config).await;

        assert_eq!((added, removed), (0, 0));
        let tasks = sched.tasks.lock().await;
        let new_task = tasks.get("page:m / p1").unwrap();
        assert_ne!(new_task.id(), old_id, "被改动的目标按「先撤后建」重启");
        drop(tasks);
        assert_eq!(sched.config().await.pages()[0].poll_interval_secs, 30);
        shutdown(&sched, runner).await;
    }

    #[tokio::test]
    async fn test_reconcile_prunes_state_of_removed_element() {
        // 消失元素的失败计数与 state 基线一并清掉并落盘，存活元素的基线保留。
        let keep = element("留下");
        let drop_el = element("删掉");
        let page = page_named("p1", 60, vec![keep.clone(), drop_el.clone()]);
        let config = config_with("rp1", vec![page.clone()], vec![], 4);
        let (sched, runner) = start_scheduler("rp1", config.clone()).await;
        sched
            .store(
                &keep.identity(),
                Entry::State {
                    value: "留".into(),
                    updated_at: "t".into(),
                },
            )
            .await;
        sched
            .store(
                &drop_el.identity(),
                Entry::State {
                    value: "删".into(),
                    updated_at: "t".into(),
                },
            )
            .await;

        let new_config = config_with(
            "rp1b",
            vec![page_named("p1", 60, vec![keep.clone()])],
            vec![],
            4,
        );
        let (added, removed) = sched.reconcile(new_config).await;

        assert_eq!((added, removed), (0, 0), "页面标识未变，属于重启而非增减");
        let elem_failures = sched.elem_failures.lock().await;
        assert!(!elem_failures.contains_key(&drop_el.identity()));
        assert!(elem_failures.contains_key(&keep.identity()));
        drop(elem_failures);
        let state = sched.state.lock().await;
        assert!(!state.contains_key(&drop_el.identity()));
        assert!(
            matches!(state.get(&keep.identity()), Some(Entry::State { value, .. }) if value == "留")
        );
        drop(state);
        // 清理必须落盘，否则重启后陈旧基线复活。
        let on_disk = load_state(PathBuf::from(&config.state_path).as_path());
        assert!(!on_disk.contains_key(&drop_el.identity()));
        assert!(on_disk.contains_key(&keep.identity()));
        shutdown(&sched, runner).await;
    }

    #[tokio::test]
    async fn test_reconcile_prunes_removed_watch_state() {
        // 删掉最后一个监控（零监控）也要成立：任务全撤、已见集合清空落盘。
        let watch = watch_named("W1", &["hk"], 1);
        let config = Config {
            telegram: TelegramConfig {
                bot_token: "x".into(),
                chat_id: "1".into(),
            },
            merchants: vec![],
            poll_interval_secs: 60,
            failure_threshold: 1,
            state_path: state_path("rp2").to_string_lossy().into(),
            max_concurrent_fetches: 4,
            nav_timeout_secs: 30,
            wait_until: "load".into(),
            watches: vec![watch.clone()],
            fingerprint: Default::default(),
            proxy: None,
        };
        let (sched, runner) = start_scheduler("rp2", config.clone()).await;
        sched
            .store(
                &watch.identity(),
                Entry::SeenSet {
                    seen_ids: vec!["1".into(), "2".into()],
                    updated_at: "t".into(),
                },
            )
            .await;

        let empty_config = Config {
            telegram: TelegramConfig {
                bot_token: "x".into(),
                chat_id: "1".into(),
            },
            merchants: vec![],
            poll_interval_secs: 60,
            failure_threshold: 1,
            state_path: state_path("rp2").to_string_lossy().into(),
            max_concurrent_fetches: 4,
            nav_timeout_secs: 30,
            wait_until: "load".into(),
            watches: vec![],
            fingerprint: Default::default(),
            proxy: None,
        };
        let (added, removed) = sched.reconcile(empty_config).await;

        assert_eq!((added, removed), (0, 1));
        assert!(sched.tasks.lock().await.is_empty());
        let watch_failures = sched.watch_failures.lock().await;
        assert!(!watch_failures.contains_key(&watch.identity()));
        drop(watch_failures);
        assert!(sched.state.lock().await.get(&watch.identity()).is_none());
        assert!(load_state(PathBuf::from(&config.state_path).as_path()).is_empty());
        shutdown(&sched, runner).await;
    }

    #[tokio::test]
    async fn test_trial_fetch_waits_for_semaphore() {
        // 试抓与常规轮询共用同一信号量，额度占满时排队而非另开并发。
        let page = page_with(1, vec![element("e")]);
        let mut config = config_with("tf1", vec![page.clone()], vec![], 1);
        config.max_concurrent_fetches = 1;
        let backend = MockBackend::pages(vec![fetched(vec![])]);
        let notifier = MockNotifier::new(vec![true]);
        let (sched, _stop) = Scheduler::new(config, backend, notifier);

        // 占满唯一额度，试抓应排队超时。
        let _permit = sched.semaphore.clone().acquire_owned().await.unwrap();
        let result = tokio::time::timeout(
            std::time::Duration::from_millis(100),
            sched.trial_fetch_page(page),
        )
        .await;
        assert!(result.is_err(), "信号量占满时试抓应排队");
    }

    #[tokio::test]
    async fn test_snapshot_lists_elements_and_watches() {
        let page = page_named("p1", 60, vec![element("e")]);
        let watch = watch_named("W1", &["hk"], 1);
        let config = config_with("sn1", vec![page], vec![watch], 4);
        let (sched, _stop) = Scheduler::new(
            config,
            Arc::new(HangingBackend),
            MockNotifier::new(vec![true]),
        );

        let rows = sched.snapshot().await;
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].kind, KIND_ELEMENT);
        assert_eq!(rows[0].status, "尚未建立基线");
        assert_eq!(rows[1].kind, KIND_WATCH);
        assert_eq!(rows[1].status, "尚未建立基线");
    }

    #[test]
    fn test_matches_case_insensitive_substring() {
        let keywords = vec!["HK".to_string()];
        assert!(matches(&keywords, "hk 原生"));
        assert!(matches(&keywords, "原生 HK"));
        assert!(matches(&keywords, "Hk"));
        assert!(!matches(&keywords, "美国 VPS"));
    }
}
