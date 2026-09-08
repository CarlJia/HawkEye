"""调度与编排。

两类常驻循环并存，共享同一浏览器、信号量、状态锁、失败告警状态机与通知器：

- 元素文本监控：每个页面一个 asyncio 任务，受信号量限流的一次导航 → 提取页面下
  全部元素 → 逐元素检测变更/告警。失败分两级（页面加载失败按页面告警，单个元素
  未匹配/提取异常按元素告警）。
- 列表新条目监控：每个 watch 目标一个任务，抓一次列表页 → 对照已见集合判新 →
  标题命中关键字的新帖逐条通知 → 轮末一次性并入已见集合并落盘（首次静默建基线）。

状态写入统一用锁串行化。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from .alert import FailureState
from .config import Config, MonitoredElement, Page, WatchTarget
from .detect import (
    Baseline,
    Changed,
    NewItems,
    SeenBaseline,
    Unchanged,
    detect,
    detect_new,
    merge_seen,
)
from .extract import ListItem
from .fetch import (
    BrowserManager,
    FetchOk,
    FetchResult,
    ListFetched,
    ListResult,
    PageFetched,
    PageLoadError,
    PageResult,
)
from .notify import (
    Notifier,
    format_change_message,
    format_failure_message,
    format_new_post_message,
)
from .state import SeenSetEntry, StateEntry, load_state, now_iso, save_state

logger = logging.getLogger(__name__)

# 快照行的两种类型标签，控制面按它决定走哪套增删变换，故做成共享常量。
KIND_ELEMENT = "元素"
KIND_WATCH = "论坛"


def _page_key(identity: str) -> str:
    return f"page:{identity}"


def _watch_key(identity: str) -> str:
    return f"watch:{identity}"


def matches(keywords: Sequence[str], title: str) -> bool:
    """标题关键字匹配：子串 + 不区分大小写 + 任一命中（OR）。

    公开给控制面复用：`/add` 的试抓回显「M 条命中」必须与真实轮询的判定口径一致。
    """
    lowered = title.lower()
    return any(kw.lower() in lowered for kw in keywords)


@dataclass(frozen=True)
class MonitorRow:
    """`/list` 的一行：类型、稳定标识、来源 URL 与当前状态指示。"""

    kind: str
    identity: str
    url: str
    status: str


class Scheduler:
    """把配置、浏览器、通知器、状态编排成常驻循环。"""

    def __init__(
        self,
        config: Config,
        browser: BrowserManager,
        notifier: Notifier,
        stop: asyncio.Event | None = None,
    ) -> None:
        self._config = config
        self._browser = browser
        self._notifier = notifier
        self._state = load_state(config.state_path)
        self._page_failures: dict[str, FailureState] = {}
        self._elem_failures: dict[str, FailureState] = {}
        self._watch_failures: dict[str, FailureState] = {}
        self._state_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(config.max_concurrent_fetches)
        # 停止事件可由外部注入：命令接收循环与轮询循环共用一个，信号一到一起退出。
        self._stop = stop if stop is not None else asyncio.Event()
        # 常驻任务登记表，键 page:<identity> / watch:<identity>，供热重载增删定位。
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def request_stop(self) -> None:
        """由信号处理触发，请求优雅停止。"""
        logger.info("收到停止请求，正在退出……")
        self._stop.set()

    @property
    def config(self) -> Config:
        """当前生效的配置；控制面用它与磁盘文件比对以决定是否热重载。"""
        return self._config

    def _spawn_page(self, page: Page) -> None:
        key = _page_key(page.identity)
        self._tasks[key] = asyncio.create_task(self._run_page(page), name=key)

    def _spawn_watch(self, watch: WatchTarget) -> None:
        key = _watch_key(watch.identity)
        self._tasks[key] = asyncio.create_task(self._run_watch(watch), name=key)

    async def run(self) -> None:
        for page in self._config.pages:
            self._spawn_page(page)
        for watch in self._config.watches:
            self._spawn_watch(watch)
        try:
            await self._stop.wait()
        finally:
            tasks = list(self._tasks.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._tasks.clear()
            await self._flush_state()

    # ---- 热重载：配置变更后的运行时协调 ----

    async def reconcile(self, new_config: Config) -> tuple[int, int]:
        """把运行中的任务对齐到 new_config，返回（新增数, 移除数）。

        按结构相等逐个比对（KTD11）：页面 / watch 对象逐字段相同则任务原样保留，
        计时器不重置；被改动的目标按「先撤后建」重启。顺序至关重要——必须先取消并
        等待任务真正退出，才能清理失败状态与 state 条目，否则 `_poll_page` /
        `_handle_element` 会撞上 KeyError。消失目标的 state 条目一并删除（KTD14），
        避免下次同名新建时把陈旧基线当作「已见」而漏掉首次变更。
        """
        old_pages = {p.identity: p for p in self._config.pages}
        new_pages = {p.identity: p for p in new_config.pages}
        old_watches = {w.identity: w for w in self._config.watches}
        new_watches = {w.identity: w for w in new_config.watches}

        gone_pages = [i for i in old_pages if i not in new_pages]
        gone_watches = [i for i in old_watches if i not in new_watches]
        added_pages = [i for i in new_pages if i not in old_pages]
        added_watches = [i for i in new_watches if i not in old_watches]
        # 标识不变但内容改了（改了选择器 / 轮询间隔 / 增删元素）→ 重启，不计入增减。
        changed_pages = [i for i, p in new_pages.items() if i in old_pages and old_pages[i] != p]
        changed_watches = [
            i for i, w in new_watches.items() if i in old_watches and old_watches[i] != w
        ]

        # 消失与被改动的元素标识（旧配置里有、新配置里没有的才算真正消失）
        surviving_elements = {e.identity for p in new_config.pages for e in p.elements}
        gone_elements = [
            e.identity
            for p in self._config.pages
            for e in p.elements
            if e.identity not in surviving_elements
        ]

        # ① 先撤：取消所有要停的任务并等它们真正退出。
        stopping_pages = gone_pages + changed_pages
        stopping_watches = gone_watches + changed_watches
        cancelled: list[asyncio.Task[None]] = []
        for identity in stopping_pages:
            task = self._tasks.pop(_page_key(identity), None)
            if task is not None:
                task.cancel()
                cancelled.append(task)
        for identity in stopping_watches:
            task = self._tasks.pop(_watch_key(identity), None)
            if task is not None:
                task.cancel()
                cancelled.append(task)
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)

        # ② 再清：只清真正消失的目标，存活目标的失败计数与基线一律保留。
        for identity in gone_pages:
            self._page_failures.pop(identity, None)
        for identity in gone_elements:
            self._elem_failures.pop(identity, None)
            self._state.pop(identity, None)
        for identity in gone_watches:
            self._watch_failures.pop(identity, None)
            self._state.pop(identity, None)
        if gone_elements or gone_watches:
            await self._flush_state()

        # ③ 换配置并重建：新增的与被改动的都按新配置起任务。
        self._config = new_config
        for identity in added_pages + changed_pages:
            self._spawn_page(new_pages[identity])
        for identity in added_watches + changed_watches:
            self._spawn_watch(new_watches[identity])

        added = len(added_pages) + len(added_watches)
        removed = len(gone_pages) + len(gone_watches)
        logger.info(
            "配置热重载完成：新增 %d、移除 %d、重启 %d",
            added,
            removed,
            len(changed_pages) + len(changed_watches),
        )
        return added, removed

    # ---- 试抓：/add 向导在保存前的一次性验证抓取 ----

    async def trial_fetch_page(self, page: Page) -> PageResult:
        """按临时 Page 抓一次，用于 /add 保存前回显命中值；共用信号量不挤占轮询（A4）。"""
        async with self._semaphore:
            return await self._browser.fetch_page(page)

    async def trial_fetch_list(self, watch: WatchTarget) -> ListResult:
        """按临时 WatchTarget 抓一次列表，用于 /add 回显条目数与关键字命中数（A4）。"""
        async with self._semaphore:
            return await self._browser.fetch_list(watch)

    # ---- 快照：/list 的数据来源 ----

    def snapshot(self) -> tuple[MonitorRow, ...]:
        """列出当前生效的全部监控及其状态（读内存，不触碰磁盘）。"""
        rows: list[MonitorRow] = []
        for page in self._config.pages:
            for element in page.elements:
                rows.append(
                    MonitorRow(
                        kind=KIND_ELEMENT,
                        identity=element.identity,
                        url=page.url,
                        status=self._status_of(element.identity),
                    )
                )
        for watch in self._config.watches:
            rows.append(
                MonitorRow(
                    kind=KIND_WATCH,
                    identity=watch.identity,
                    url=watch.url,
                    status=self._status_of(watch.identity),
                )
            )
        return tuple(rows)

    def _status_of(self, identity: str) -> str:
        entry = self._state.get(identity)
        if isinstance(entry, StateEntry):
            return entry.value
        if isinstance(entry, SeenSetEntry):
            return f"已见 {len(entry.seen_ids)} 帖"
        return "尚未建立基线"

    async def _run_page(self, page: Page) -> None:
        self._page_failures.setdefault(page.identity, FailureState())
        for element in page.elements:
            self._elem_failures.setdefault(element.identity, FailureState())
        while not self._stop.is_set():
            try:
                await self._poll_page(page)
            except Exception:  # noqa: BLE001 —— 单次轮询异常不应终止整个循环
                logger.exception("页面 %s 轮询出现未预期异常", page.identity)
            # 可被停止事件立即打断的间隔等待
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=page.poll_interval_secs)
            except TimeoutError:
                pass

    async def _poll_page(self, page: Page) -> None:
        async with self._semaphore:
            if self._stop.is_set():
                return
            result = await self._browser.fetch_page(page)

        page_failure = self._page_failures[page.identity]
        if isinstance(result, PageLoadError):
            logger.warning("页面 %s 加载失败：%s", page.identity, result.reason)
            if page_failure.record_failure(page.failure_threshold):
                if await self._send_failure_alert(
                    page.identity, page.failure_threshold, result.reason, url=page.url
                ):
                    page_failure.mark_alerted()
                else:
                    logger.error("页面 %s 失败告警发送失败，下轮重试", page.identity)
            return

        assert isinstance(result, PageFetched)
        page_failure.record_success()
        for element, el_result in result.results:
            await self._handle_element(page, element, el_result)

    async def _handle_element(
        self, page: Page, element: MonitoredElement, el_result: FetchResult
    ) -> None:
        failure = self._elem_failures[element.identity]
        if isinstance(el_result, FetchOk):
            failure.record_success()
            await self._handle_value(page, element, el_result.value)
            return

        # 原因一律取抓取层给的原话：这里曾硬编码「选择器未匹配到元素」，结果「匹配到了
        # 但文本为空」也顶着这句报出来，诊断反过来把人引向改选择器（正确改法是提一级）。
        reason = el_result.reason
        logger.warning("元素 %s 抓取失败：%s", element.identity, reason)
        if failure.record_failure(page.failure_threshold):
            jump_url = element.url or page.url
            if await self._send_failure_alert(
                element.identity, page.failure_threshold, reason, url=jump_url
            ):
                failure.mark_alerted()
            else:
                logger.error("元素 %s 失败告警发送失败，下轮重试", element.identity)

    async def _handle_value(self, page: Page, element: MonitoredElement, value: str) -> None:
        entry = self._state.get(element.identity)
        previous = entry.value if isinstance(entry, StateEntry) else None
        outcome = detect(previous, value)

        if isinstance(outcome, Unchanged):
            logger.debug("元素 %s 未变更：%s", element.identity, value)
            return
        if isinstance(outcome, Baseline):
            logger.info("元素 %s 建立基线：%s", element.identity, value)
            await self._store(element.identity, value)
            return

        assert isinstance(outcome, Changed)
        logger.info("元素 %s 变更：%s → %s", element.identity, outcome.old, outcome.new)
        jump_url = element.url or page.url
        text = format_change_message(
            element.identity, outcome.old, outcome.new, now_iso(), url=jump_url
        )
        # 至少一次交付：发送成功后才更新已记录值；失败则保留旧值，下轮重试。
        if await self._notifier.send(text):
            await self._store(element.identity, value)
        else:
            logger.error("元素 %s 通知发送失败，保留旧值等待下次重试", element.identity)

    async def _send_failure_alert(
        self, label: str, threshold: int, reason: str, url: str | None = None
    ) -> bool:
        text = format_failure_message(label, threshold, reason, now_iso(), url=url)
        return await self._notifier.send(text)

    async def _store(self, identity: str, value: str) -> None:
        async with self._state_lock:
            self._state[identity] = StateEntry(value=value, updated_at=now_iso())
            save_state(self._config.state_path, self._state)

    async def _flush_state(self) -> None:
        async with self._state_lock:
            save_state(self._config.state_path, self._state)

    # ---- 列表新条目监控 ----

    async def _run_watch(self, watch: WatchTarget) -> None:
        self._watch_failures.setdefault(watch.identity, FailureState())
        while not self._stop.is_set():
            try:
                await self._poll_watch(watch)
            except Exception:  # noqa: BLE001 —— 单次轮询异常不应终止整个循环
                logger.exception("列表 %s 轮询出现未预期异常", watch.identity)
            # 可被停止事件立即打断的间隔等待
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=watch.poll_interval_secs)
            except TimeoutError:
                pass

    async def _poll_watch(self, watch: WatchTarget) -> None:
        async with self._semaphore:
            if self._stop.is_set():
                return
            result = await self._browser.fetch_list(watch)

        watch_failure = self._watch_failures[watch.identity]
        if isinstance(result, PageLoadError):
            logger.warning("列表 %s 加载失败：%s", watch.identity, result.reason)
            await self._alert_watch_failure(watch, result.reason)
            return

        assert isinstance(result, ListFetched)
        # 空提取视为失败：选择器未匹配或页面结构变化，此时不应把已见集合当作“全新
        # 一轮”误判，否则下轮页面恢复时会把历史帖当新帖刷屏。按 R12 走失败告警路径。
        if not result.items:
            reason = "列表项提取为空（选择器未匹配或页面结构变化）"
            logger.warning("列表 %s %s", watch.identity, reason)
            await self._alert_watch_failure(watch, reason)
            return

        watch_failure.record_success()
        await self._handle_watch(watch, result.items)

    async def _alert_watch_failure(self, watch: WatchTarget, reason: str) -> None:
        failure = self._watch_failures[watch.identity]
        if failure.record_failure(watch.failure_threshold):
            if await self._send_failure_alert(
                watch.identity, watch.failure_threshold, reason, url=watch.url
            ):
                failure.mark_alerted()
            else:
                logger.error("列表 %s 失败告警发送失败，下轮重试", watch.identity)

    async def _handle_watch(self, watch: WatchTarget, items: tuple[ListItem, ...]) -> None:
        entry = self._state.get(watch.identity)
        previous_seen = frozenset(entry.seen_ids) if isinstance(entry, SeenSetEntry) else None
        current_ids = [item.post_id for item in items]
        outcome = detect_new(previous_seen, current_ids)

        if isinstance(outcome, SeenBaseline):
            logger.info(
                "列表 %s 首次运行，静默建立基线：%d 个帖子", watch.identity, len(outcome.ids)
            )
            await self._store_seen(watch.identity, outcome.ids)
            return

        assert isinstance(outcome, NewItems)
        if not outcome.new_ids:
            logger.debug("列表 %s 无新帖", watch.identity)
            return

        # 帖子 ID → 列表项；同 ID 保留首个（列表内理应唯一，去重仅为稳妥）
        by_id: dict[str, ListItem] = {}
        for item in items:
            by_id.setdefault(item.post_id, item)

        # 命中关键字的新帖逐条通知；未命中的新帖也需记入已见，避免下轮反复判新。
        # 发送成功的帖子才算“已交付”并入已见（至少一次交付，R5）；发送失败则不并入，
        # 保留待下轮重试。轮末一次性并入并落盘，减少写状态次数。
        confirmed: list[str] = []
        for pid in outcome.new_ids:
            post = by_id.get(pid)
            if post is None:
                continue
            if matches(watch.keywords, post.title):
                text = format_new_post_message(post.title, post.url, now_iso())
                if await self._notifier.send(text):
                    logger.info("列表 %s 命中新帖并已通知：%s", watch.identity, post.title)
                    confirmed.append(pid)
                else:
                    logger.error(
                        "列表 %s 新帖通知发送失败，下轮重试：%s", watch.identity, post.title
                    )
            else:
                confirmed.append(pid)

        if confirmed:
            prior = entry.seen_ids if isinstance(entry, SeenSetEntry) else ()
            merged = merge_seen(prior, current_ids, confirmed)
            await self._store_seen(watch.identity, merged)

    async def _store_seen(self, identity: str, seen_ids: tuple[str, ...]) -> None:
        async with self._state_lock:
            self._state[identity] = SeenSetEntry(seen_ids=seen_ids, updated_at=now_iso())
            save_state(self._config.state_path, self._state)
