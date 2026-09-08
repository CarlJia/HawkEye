"""scheduler 模块测试：页面/元素两级失败告警发送成功后才抑制；
变更通知发送成功后才落盘；同一页面多个元素共享一次页面加载。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path

import pytest

from hawkeye.alert import FailureState
from hawkeye.config import Config, Merchant, MonitoredElement, Page, TelegramConfig, WatchTarget
from hawkeye.detect import SEEN_IDS_MAX
from hawkeye.extract import ListItem
from hawkeye.fetch import (
    FetchError,
    FetchNoMatch,
    FetchOk,
    ListFetched,
    ListResult,
    PageFetched,
    PageLoadError,
    PageResult,
)
from hawkeye.scheduler import Scheduler, matches
from hawkeye.state import SeenSetEntry, StateEntry, load_state


def _element(name: str = "e") -> MonitoredElement:
    return MonitoredElement(
        merchant_name="m",
        page_name="p",
        name=name,
        selector=".x",
        selector_type="css",
        nth=None,
    )


def _page(*, threshold: int = 1, elements: tuple[MonitoredElement, ...] = ()) -> Page:
    els = elements or (_element(),)
    return Page(
        merchant_name="m",
        name="p",
        url="https://e.com",
        poll_interval_secs=60,
        wait_until="load",
        nav_timeout_secs=30,
        failure_threshold=threshold,
        elements=els,
    )


def _config(tmp_path: Path, page: Page) -> Config:
    return Config(
        telegram=TelegramConfig(bot_token="x", chat_id="1"),
        merchants=(Merchant(name="m", pages=(page,)),),
        poll_interval_secs=60,
        failure_threshold=page.failure_threshold,
        state_path=str(tmp_path / "state.json"),
        max_concurrent_fetches=4,
        nav_timeout_secs=30,
        wait_until="load",
    )


def _seed(sched: Scheduler, page: Page) -> None:
    """直接调用 _poll_page 会绕过 _run_page 的初始化，这里手动补齐失败状态。"""
    sched._page_failures[page.identity] = FailureState()
    for el in page.elements:
        sched._elem_failures[el.identity] = FailureState()


class _FakeBrowser:
    def __init__(self, results: list[PageResult]) -> None:
        self._results = results
        self.calls = 0

    async def fetch_page(self, page: Page) -> PageResult:
        r = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        return r


class _FakeNotifier:
    def __init__(self, results: list[bool]) -> None:
        self._results = results
        self.sent: list[str] = []

    async def send(self, text: str) -> bool:
        idx = len(self.sent)
        self.sent.append(text)
        return self._results[min(idx, len(self._results) - 1)]


async def test_page_load_failure_retries_until_sent(tmp_path: Path) -> None:
    page = _page(threshold=1)
    config = _config(tmp_path, page)
    browser = _FakeBrowser([PageLoadError(reason="boom")])
    notifier = _FakeNotifier([False, True])  # 首次发送失败, 第二次成功
    sched = Scheduler(config, browser, notifier)  # type: ignore[arg-type]
    _seed(sched, page)

    await sched._poll_page(page)  # 失败#1: 达阈值, 发送失败 -> 不抑制
    await sched._poll_page(page)  # 失败#2: 仍应重试, 发送成功 -> 抑制
    await sched._poll_page(page)  # 失败#3: 已抑制 -> 不再发送

    assert len(notifier.sent) == 2
    assert sched._page_failures[page.identity].alerted is True


async def test_element_failure_retries_until_sent(tmp_path: Path) -> None:
    el = _element()
    page = _page(threshold=1, elements=(el,))
    config = _config(tmp_path, page)
    browser = _FakeBrowser([PageFetched(results=((el, FetchError(reason="boom")),))])
    notifier = _FakeNotifier([False, True])
    sched = Scheduler(config, browser, notifier)  # type: ignore[arg-type]
    _seed(sched, page)

    await sched._poll_page(page)  # 元素失败#1: 达阈值, 发送失败 -> 不抑制
    await sched._poll_page(page)  # 元素失败#2: 重试, 发送成功 -> 抑制
    await sched._poll_page(page)  # 元素失败#3: 已抑制 -> 不再发送

    assert len(notifier.sent) == 2
    assert sched._elem_failures[el.identity].alerted is True


async def test_no_match_reason_reaches_the_alert(tmp_path: Path) -> None:
    # 未匹配一路的告警文案曾在这里硬编码成「选择器未匹配到元素」，于是「匹配到了但文本
    # 为空」也顶着这句话报出来，诊断反过来误导人。告警只许转达抓取层给的原话。
    el = _element()
    page = _page(threshold=1, elements=(el,))
    config = _config(tmp_path, page)
    reason = "选择器匹配到元素，但其文本为空"
    browser = _FakeBrowser([PageFetched(results=((el, FetchNoMatch(reason=reason)),))])
    notifier = _FakeNotifier([True])
    sched = Scheduler(config, browser, notifier)  # type: ignore[arg-type]
    _seed(sched, page)

    await sched._poll_page(page)

    assert len(notifier.sent) == 1
    assert reason in notifier.sent[0]


async def test_change_stored_only_after_send_success(tmp_path: Path) -> None:
    el = _element()
    page = _page(threshold=3, elements=(el,))
    config = _config(tmp_path, page)
    browser = _FakeBrowser(
        [
            PageFetched(results=((el, FetchOk(value="A")),)),
            PageFetched(results=((el, FetchOk(value="B")),)),
            PageFetched(results=((el, FetchOk(value="B")),)),
        ]
    )
    notifier = _FakeNotifier([False, True])
    sched = Scheduler(config, browser, notifier)  # type: ignore[arg-type]
    _seed(sched, page)

    await sched._poll_page(page)  # 基线 A: 静默落盘, 不发送
    assert notifier.sent == []
    assert sched._state[el.identity].value == "A"

    await sched._poll_page(page)  # A->B: 发送失败 -> 保留旧值 A
    assert len(notifier.sent) == 1
    assert sched._state[el.identity].value == "A"

    await sched._poll_page(page)  # 仍 B: 再次 A->B, 发送成功 -> 落盘 B
    assert len(notifier.sent) == 2
    assert sched._state[el.identity].value == "B"
    assert load_state(config.state_path)[el.identity].value == "B"


async def test_unchanged_emits_debug_result_log(tmp_path: Path, caplog) -> None:
    el = _element()
    page = _page(threshold=3, elements=(el,))
    config = _config(tmp_path, page)
    browser = _FakeBrowser([PageFetched(results=((el, FetchOk(value="充足")),))])
    notifier = _FakeNotifier([True])
    sched = Scheduler(config, browser, notifier)  # type: ignore[arg-type]
    _seed(sched, page)
    sched._state[el.identity] = StateEntry(value="充足", updated_at="t0")  # 预置同值基线

    with caplog.at_level(logging.DEBUG, logger="hawkeye.scheduler"):
        await sched._poll_page(page)  # 值未变化: 不通知, 但产出一条 DEBUG 结果日志

    assert notifier.sent == []
    assert any(
        r.levelno == logging.DEBUG and el.identity in r.getMessage() and "未变更" in r.getMessage()
        for r in caplog.records
    )


async def test_multiple_elements_share_one_page_load(tmp_path: Path) -> None:
    e1 = _element("是否可售")
    e2 = _element("价格")
    page = _page(threshold=3, elements=(e1, e2))
    config = _config(tmp_path, page)
    browser = _FakeBrowser(
        [PageFetched(results=((e1, FetchOk(value="充足")), (e2, FetchOk(value="¥10"))))]
    )
    notifier = _FakeNotifier([True])
    sched = Scheduler(config, browser, notifier)  # type: ignore[arg-type]
    _seed(sched, page)

    await sched._poll_page(page)

    assert browser.calls == 1  # 两个元素只触发一次页面加载
    assert sched._state[e1.identity].value == "充足"
    assert sched._state[e2.identity].value == "¥10"
    assert notifier.sent == []  # 均为基线, 静默不打扰


async def test_mixed_element_results_alert_only_failing(tmp_path: Path) -> None:
    ok = _element("可售")
    bad = _element("价格")
    page = _page(threshold=1, elements=(ok, bad))
    config = _config(tmp_path, page)
    browser = _FakeBrowser(
        [PageFetched(results=((ok, FetchOk(value="充足")), (bad, FetchError(reason="boom"))))]
    )
    notifier = _FakeNotifier([True])
    sched = Scheduler(config, browser, notifier)  # type: ignore[arg-type]
    _seed(sched, page)

    await sched._poll_page(page)

    # 成功元素: 落基线, 不告警
    assert sched._state[ok.identity].value == "充足"
    assert sched._elem_failures[ok.identity].alerted is False
    # 失败元素: 达阈值发一条告警并被抑制; 告警只针对失败元素
    assert len(notifier.sent) == 1
    assert bad.identity in notifier.sent[0]
    assert ok.identity not in notifier.sent[0]
    assert sched._elem_failures[bad.identity].alerted is True


# ---- 列表新条目监控（watch）----


def _watch(
    *, name: str = "NS", keywords: tuple[str, ...] = ("hk",), threshold: int = 1
) -> WatchTarget:
    return WatchTarget(
        name=name,
        url="https://www.nodeseek.com/",
        link_selector="a",
        selector_type="css",
        keywords=keywords,
        id_pattern=None,
        poll_interval_secs=60,
        wait_until="load",
        nav_timeout_secs=30,
        failure_threshold=threshold,
    )


def _watch_config(tmp_path: Path, watch: WatchTarget) -> Config:
    return Config(
        telegram=TelegramConfig(bot_token="x", chat_id="1"),
        merchants=(),
        poll_interval_secs=60,
        failure_threshold=watch.failure_threshold,
        state_path=str(tmp_path / "state.json"),
        max_concurrent_fetches=4,
        nav_timeout_secs=30,
        wait_until="load",
        watches=(watch,),
    )


def _seed_watch(sched: Scheduler, watch: WatchTarget) -> None:
    """直接调用 _poll_watch 绕过了 _run_watch 的初始化，这里手动补齐失败状态。"""
    sched._watch_failures[watch.identity] = FailureState()


def _item(post_id: str, title: str) -> ListItem:
    return ListItem(post_id=post_id, title=title, url=f"https://www.nodeseek.com/post-{post_id}-1")


class _FakeListBrowser:
    def __init__(self, results: list[ListResult]) -> None:
        self._results = results
        self.calls = 0

    async def fetch_list(self, watch: WatchTarget) -> ListResult:
        r = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        return r


class _FakeDualBrowser:
    """同时支持元素页面与列表页抓取，用于验证两种模式共存（R11）。"""

    def __init__(self, page_result: PageResult, list_result: ListResult) -> None:
        self._page_result = page_result
        self._list_result = list_result

    async def fetch_page(self, page: Page) -> PageResult:
        return self._page_result

    async def fetch_list(self, watch: WatchTarget) -> ListResult:
        return self._list_result


async def test_watch_first_run_silent_baseline(tmp_path: Path) -> None:
    # Covers AE4 / R7: 首次运行把当前全部可见 ID 记入已见集合、一律不推送
    watch = _watch(keywords=("hk",))
    config = _watch_config(tmp_path, watch)
    browser = _FakeListBrowser([ListFetched(items=(_item("100", "HK 节点"), _item("101", "其它")))])
    notifier = _FakeNotifier([True])
    sched = Scheduler(config, browser, notifier)  # type: ignore[arg-type]
    _seed_watch(sched, watch)

    await sched._poll_watch(watch)

    assert notifier.sent == []
    assert set(sched._state[watch.identity].seen_ids) == {"100", "101"}
    assert set(load_state(config.state_path)[watch.identity].seen_ids) == {"100", "101"}


async def test_watch_new_matching_post_notifies_and_records(tmp_path: Path) -> None:
    # Covers AE1: 已建基线后出现命中新帖 → 一条含标题+链接的通知, 成功后记入 ID
    watch = _watch(keywords=("hk",))
    config = _watch_config(tmp_path, watch)
    browser = _FakeListBrowser(
        [
            ListFetched(items=(_item("100", "老帖"),)),
            ListFetched(items=(_item("200", "HK 原生 IP"), _item("100", "老帖"))),
        ]
    )
    notifier = _FakeNotifier([True])
    sched = Scheduler(config, browser, notifier)  # type: ignore[arg-type]
    _seed_watch(sched, watch)

    await sched._poll_watch(watch)  # 基线 {100}
    assert notifier.sent == []

    await sched._poll_watch(watch)  # 新帖 200 命中 hk
    assert len(notifier.sent) == 1
    assert "HK 原生 IP" in notifier.sent[0]
    assert "https://www.nodeseek.com/post-200-1" in notifier.sent[0]
    assert set(sched._state[watch.identity].seen_ids) == {"100", "200"}


async def test_watch_seen_id_bumped_not_renotified(tmp_path: Path) -> None:
    # Covers AE2 / R2: 已见 ID 的帖被顶到列表首位（顺序变化）→ 不判为新帖、不推送
    watch = _watch(keywords=("hk",))
    config = _watch_config(tmp_path, watch)
    browser = _FakeListBrowser(
        [
            ListFetched(items=(_item("100", "HK 老帖"), _item("101", "其它"))),
            ListFetched(items=(_item("101", "其它"), _item("100", "HK 老帖"))),  # 顺序颠倒
        ]
    )
    notifier = _FakeNotifier([True])
    sched = Scheduler(config, browser, notifier)  # type: ignore[arg-type]
    _seed_watch(sched, watch)

    await sched._poll_watch(watch)  # 基线 {100,101}
    await sched._poll_watch(watch)  # 位置变化但无新 ID
    assert notifier.sent == []


async def test_watch_new_non_matching_recorded_not_notified(tmp_path: Path) -> None:
    # Covers AE3 / R6: 新帖标题未命中 → 不通知但记入已见, 下轮不再判新
    watch = _watch(keywords=("hk",))
    config = _watch_config(tmp_path, watch)
    browser = _FakeListBrowser(
        [
            ListFetched(items=(_item("100", "老帖"),)),
            ListFetched(items=(_item("300", "美国 VPS"), _item("100", "老帖"))),
            ListFetched(items=(_item("300", "美国 VPS"), _item("100", "老帖"))),
        ]
    )
    notifier = _FakeNotifier([True])
    sched = Scheduler(config, browser, notifier)  # type: ignore[arg-type]
    _seed_watch(sched, watch)

    await sched._poll_watch(watch)  # 基线 {100}
    await sched._poll_watch(watch)  # 新帖 300 未命中
    assert notifier.sent == []
    assert "300" in sched._state[watch.identity].seen_ids

    await sched._poll_watch(watch)  # 300 已见, 不再判新
    assert notifier.sent == []


async def test_watch_send_failure_retries_next_round(tmp_path: Path) -> None:
    # Covers AE6 / R5: 命中新帖发送失败 → 不记入 ID; 下轮仍判新并重试直至成功
    watch = _watch(keywords=("hk",))
    config = _watch_config(tmp_path, watch)
    browser = _FakeListBrowser(
        [
            ListFetched(items=(_item("100", "老帖"),)),
            ListFetched(items=(_item("200", "HK 新帖"), _item("100", "老帖"))),
            ListFetched(items=(_item("200", "HK 新帖"), _item("100", "老帖"))),
        ]
    )
    notifier = _FakeNotifier([False, True])  # 首次发送失败, 二次成功
    sched = Scheduler(config, browser, notifier)  # type: ignore[arg-type]
    _seed_watch(sched, watch)

    await sched._poll_watch(watch)  # 基线 {100}
    await sched._poll_watch(watch)  # 200 发送失败 → 不记入
    assert len(notifier.sent) == 1
    assert "200" not in sched._state[watch.identity].seen_ids

    await sched._poll_watch(watch)  # 200 仍判新, 重试成功 → 记入
    assert len(notifier.sent) == 2
    assert "200" in sched._state[watch.identity].seen_ids


async def test_watch_new_keyword_no_backfill(tmp_path: Path) -> None:
    # Covers AE5 / R9: 已见集合已含历史帖 100；即便新增关键字能匹配 100 标题, 也不回溯推送
    watch = _watch(keywords=("hk", "vps"))  # 新增了 vps
    config = _watch_config(tmp_path, watch)
    browser = _FakeListBrowser(
        [ListFetched(items=(_item("100", "美国 VPS 促销"), _item("200", "无关新帖")))]
    )
    notifier = _FakeNotifier([True])
    sched = Scheduler(config, browser, notifier)  # type: ignore[arg-type]
    _seed_watch(sched, watch)
    sched._state[watch.identity] = SeenSetEntry(seen_ids=("100",), updated_at="t0")

    await sched._poll_watch(watch)

    # 100 命中 vps 但 ID 已见 → 不推送; 200 未命中 → 记入不推送
    assert notifier.sent == []
    assert set(sched._state[watch.identity].seen_ids) == {"100", "200"}


async def test_watch_load_failure_alert_threshold_and_reset(tmp_path: Path) -> None:
    # Covers R12: 加载失败连续达阈值发一条、边沿触发不刷屏、成功后复位
    watch = _watch(threshold=2)
    config = _watch_config(tmp_path, watch)
    browser = _FakeListBrowser(
        [
            PageLoadError(reason="boom"),
            PageLoadError(reason="boom"),
            PageLoadError(reason="boom"),
            ListFetched(items=(_item("100", "HK"),)),  # 恢复
        ]
    )
    notifier = _FakeNotifier([True])
    sched = Scheduler(config, browser, notifier)  # type: ignore[arg-type]
    _seed_watch(sched, watch)

    await sched._poll_watch(watch)  # 失败#1: 未达阈值(2)
    assert notifier.sent == []
    await sched._poll_watch(watch)  # 失败#2: 达阈值 → 发一条
    assert len(notifier.sent) == 1
    assert watch.identity in notifier.sent[0]
    await sched._poll_watch(watch)  # 失败#3: 已抑制 → 不再发
    assert len(notifier.sent) == 1
    assert sched._watch_failures[watch.identity].alerted is True

    await sched._poll_watch(watch)  # 恢复 → record_success 复位
    assert sched._watch_failures[watch.identity].alerted is False
    assert sched._watch_failures[watch.identity].consecutive_failures == 0


async def test_watch_empty_extraction_treated_as_failure(tmp_path: Path) -> None:
    # 空提取视为失败（PF1）：走失败告警而非建基线, 状态里不应写入该 watch 的已见集合
    watch = _watch(threshold=1)
    config = _watch_config(tmp_path, watch)
    browser = _FakeListBrowser([ListFetched(items=())])
    notifier = _FakeNotifier([True])
    sched = Scheduler(config, browser, notifier)  # type: ignore[arg-type]
    _seed_watch(sched, watch)

    await sched._poll_watch(watch)

    assert len(notifier.sent) == 1
    assert watch.identity in notifier.sent[0]
    assert watch.identity not in sched._state


async def test_watch_multiple_new_matches_each_notified(tmp_path: Path) -> None:
    # Covers R4: 一轮出现多个命中新帖 → 各发一条独立通知, 已见集合一次性落盘;
    # 同时覆盖不区分大小写（HK / hk 均命中）
    watch = _watch(keywords=("hk",))
    config = _watch_config(tmp_path, watch)
    browser = _FakeListBrowser(
        [
            ListFetched(items=(_item("100", "老帖"),)),
            ListFetched(
                items=(
                    _item("201", "HK 甲"),
                    _item("202", "US 乙"),  # 不命中
                    _item("203", "hk 丙"),  # 小写命中
                    _item("100", "老帖"),
                )
            ),
        ]
    )
    notifier = _FakeNotifier([True])
    sched = Scheduler(config, browser, notifier)  # type: ignore[arg-type]
    _seed_watch(sched, watch)

    await sched._poll_watch(watch)  # 基线 {100}
    await sched._poll_watch(watch)

    assert len(notifier.sent) == 2  # 201 与 203 各一条; 202 未命中
    assert any("HK 甲" in m for m in notifier.sent)
    assert any("hk 丙" in m for m in notifier.sent)
    assert set(sched._state[watch.identity].seen_ids) == {"100", "201", "202", "203"}


async def test_watch_seen_set_capped_at_limit(tmp_path: Path) -> None:
    # 已见集合有条数上限, state.json 不会随运行时长无限增长: 达上限后新帖挤掉最久未见的 ID
    watch = _watch(keywords=("hk",))
    config = _watch_config(tmp_path, watch)
    browser = _FakeListBrowser([ListFetched(items=(_item("900001", "HK 新帖"),))])
    notifier = _FakeNotifier([True])
    sched = Scheduler(config, browser, notifier)  # type: ignore[arg-type]
    _seed_watch(sched, watch)
    sched._state[watch.identity] = SeenSetEntry(
        seen_ids=tuple(str(i) for i in range(SEEN_IDS_MAX)), updated_at="t0"
    )

    await sched._poll_watch(watch)

    assert len(notifier.sent) == 1
    seen = sched._state[watch.identity].seen_ids
    assert len(seen) == SEEN_IDS_MAX
    assert seen[-1] == "900001"
    assert "0" not in seen  # 最久未见的被淘汰
    assert len(load_state(config.state_path)[watch.identity].seen_ids) == SEEN_IDS_MAX


async def test_element_and_watch_modes_coexist(tmp_path: Path) -> None:
    # Covers R11: 元素监控与列表监控共用一个 Scheduler / 状态文件, 互不干扰
    el = _element()
    page = _page(threshold=3, elements=(el,))
    watch = _watch(keywords=("hk",))
    config = Config(
        telegram=TelegramConfig(bot_token="x", chat_id="1"),
        merchants=(Merchant(name="m", pages=(page,)),),
        poll_interval_secs=60,
        failure_threshold=3,
        state_path=str(tmp_path / "state.json"),
        max_concurrent_fetches=4,
        nav_timeout_secs=30,
        wait_until="load",
        watches=(watch,),
    )
    browser = _FakeDualBrowser(
        PageFetched(results=((el, FetchOk(value="充足")),)),
        ListFetched(items=(_item("100", "HK 帖"),)),
    )
    notifier = _FakeNotifier([True])
    sched = Scheduler(config, browser, notifier)  # type: ignore[arg-type]
    _seed(sched, page)
    _seed_watch(sched, watch)

    await sched._poll_page(page)  # 元素基线 → StateEntry
    await sched._poll_watch(watch)  # watch 基线 → SeenSetEntry

    assert notifier.sent == []
    assert isinstance(sched._state[el.identity], StateEntry)
    assert sched._state[el.identity].value == "充足"
    assert isinstance(sched._state[watch.identity], SeenSetEntry)
    assert set(sched._state[watch.identity].seen_ids) == {"100"}

    reloaded = load_state(config.state_path)
    assert isinstance(reloaded[el.identity], StateEntry)
    assert isinstance(reloaded[watch.identity], SeenSetEntry)


# ---- U3：热重载协调、试抓与快照 ----


def _named_element(page_name: str, name: str, selector: str = ".x") -> MonitoredElement:
    return MonitoredElement(
        merchant_name="m",
        page_name=page_name,
        name=name,
        selector=selector,
        selector_type="css",
        nth=None,
    )


def _named_page(name: str, *, elements: tuple[MonitoredElement, ...] = (), poll: int = 60) -> Page:
    return Page(
        merchant_name="m",
        name=name,
        url=f"https://e.com/{name}",
        poll_interval_secs=poll,
        wait_until="load",
        nav_timeout_secs=30,
        failure_threshold=1,
        elements=elements or (_named_element(name, "e"),),
    )


def _multi_config(
    tmp_path: Path,
    pages: tuple[Page, ...] = (),
    watches: tuple[WatchTarget, ...] = (),
) -> Config:
    return Config(
        telegram=TelegramConfig(bot_token="x", chat_id="1"),
        merchants=(Merchant(name="m", pages=pages),) if pages else (),
        poll_interval_secs=60,
        failure_threshold=1,
        state_path=str(tmp_path / "state.json"),
        max_concurrent_fetches=4,
        nav_timeout_secs=30,
        wait_until="load",
        watches=watches,
    )


class _HangingBrowser:
    """抓取永不返回：让常驻任务停在第一次抓取上，热重载测试因此没有任何后台状态写入。"""

    async def fetch_page(self, page: Page) -> PageResult:
        await asyncio.Event().wait()
        raise AssertionError("不可达")

    async def fetch_list(self, watch: WatchTarget) -> ListResult:
        await asyncio.Event().wait()
        raise AssertionError("不可达")


async def _start(sched: Scheduler) -> asyncio.Task[None]:
    """起真实的 run()，让它把常驻任务登记进 _tasks 后停在等待停止事件上。"""
    runner = asyncio.create_task(sched.run())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return runner


async def _shutdown(sched: Scheduler, runner: asyncio.Task[None]) -> None:
    sched.request_stop()
    await runner


async def test_run_registers_tasks_by_identity_key(tmp_path: Path) -> None:
    config = _multi_config(tmp_path, (_named_page("p1"),), (_watch(name="W1"),))
    sched = Scheduler(config, _HangingBrowser(), _FakeNotifier([True]))  # type: ignore[arg-type]
    runner = await _start(sched)

    assert set(sched._tasks) == {"page:m / p1", "watch:watch / W1"}

    await _shutdown(sched, runner)
    assert sched._tasks == {}  # 停止后任务表清空


async def test_reconcile_adds_and_removes(tmp_path: Path) -> None:
    # 新旧配置完全不重叠：旧目标全撤、新目标全建，计数如实反映。
    config = _multi_config(tmp_path, (_named_page("p1"),), (_watch(name="W1"),))
    sched = Scheduler(config, _HangingBrowser(), _FakeNotifier([True]))  # type: ignore[arg-type]
    runner = await _start(sched)

    added, removed = await sched.reconcile(
        _multi_config(tmp_path, (_named_page("p2"),), (_watch(name="W2"),))
    )

    assert (added, removed) == (2, 2)
    assert set(sched._tasks) == {"page:m / p2", "watch:watch / W2"}
    await _shutdown(sched, runner)


async def test_reconcile_removal_leaves_surviving_page_running(tmp_path: Path) -> None:
    # AE6 / R13：删掉一个页面时，其余页面的抓取必须原样继续——同一个 Task，未被连带取消。
    keep = _named_page("keep")
    config = _multi_config(tmp_path, (keep, _named_page("drop")))
    sched = Scheduler(config, _HangingBrowser(), _FakeNotifier([True]))  # type: ignore[arg-type]
    runner = await _start(sched)
    survivor = sched._tasks["page:m / keep"]

    added, removed = await sched.reconcile(_multi_config(tmp_path, (keep,)))

    assert (added, removed) == (0, 1)
    assert set(sched._tasks) == {"page:m / keep"}
    assert sched._tasks["page:m / keep"] is survivor
    assert not survivor.done()  # 仍停在自己的抓取上
    await _shutdown(sched, runner)


async def test_reconcile_keeps_identical_targets_running(tmp_path: Path) -> None:
    # KTD11：结构相等的目标任务原样保留（计时器不重置），失败计数与告警抑制不丢。
    page, watch = _named_page("p1"), _watch(name="W1")
    config = _multi_config(tmp_path, (page,), (watch,))
    sched = Scheduler(config, _HangingBrowser(), _FakeNotifier([True]))  # type: ignore[arg-type]
    runner = await _start(sched)
    _seed(sched, page)
    _seed_watch(sched, watch)
    sched._page_failures[page.identity].record_failure(1)
    sched._page_failures[page.identity].mark_alerted()
    before = dict(sched._tasks)

    added, removed = await sched.reconcile(
        _multi_config(tmp_path, (_named_page("p1"),), (_watch(name="W1"),))
    )

    assert (added, removed) == (0, 0)
    assert sched._tasks == before  # 同一批 Task 对象，未被重启
    assert sched._page_failures[page.identity].alerted is True
    assert watch.identity in sched._watch_failures
    await _shutdown(sched, runner)


async def test_reconcile_restarts_changed_target(tmp_path: Path) -> None:
    # 标识不变但内容变了（改了轮询间隔）→ 先撤后建，不计入增减。
    config = _multi_config(tmp_path, (_named_page("p1", poll=60),))
    sched = Scheduler(config, _HangingBrowser(), _FakeNotifier([True]))  # type: ignore[arg-type]
    runner = await _start(sched)
    old_task = sched._tasks["page:m / p1"]

    added, removed = await sched.reconcile(_multi_config(tmp_path, (_named_page("p1", poll=30),)))

    assert (added, removed) == (0, 0)
    assert sched._tasks["page:m / p1"] is not old_task
    assert old_task.cancelled()  # 旧任务确实已退出，不会与新任务并存
    assert sched._config.pages[0].poll_interval_secs == 30
    await _shutdown(sched, runner)


async def test_reconcile_prunes_state_of_removed_element(tmp_path: Path) -> None:
    # KTD14：消失元素的失败计数与 state 基线一并清掉并落盘，存活元素的基线保留。
    keep, drop = _named_element("p1", "留下"), _named_element("p1", "删掉")
    page = _named_page("p1", elements=(keep, drop))
    config = _multi_config(tmp_path, (page,))
    sched = Scheduler(config, _HangingBrowser(), _FakeNotifier([True]))  # type: ignore[arg-type]
    runner = await _start(sched)
    _seed(sched, page)
    await sched._store(keep.identity, "留")
    await sched._store(drop.identity, "删")

    added, removed = await sched.reconcile(
        _multi_config(tmp_path, (_named_page("p1", elements=(keep,)),))
    )

    assert (added, removed) == (0, 0)  # 页面标识未变，属于重启而非增减
    assert drop.identity not in sched._elem_failures
    assert keep.identity in sched._elem_failures
    assert drop.identity not in sched._state
    assert sched._state[keep.identity].value == "留"
    on_disk = load_state(config.state_path)  # 清理必须落盘，否则重启后陈旧基线复活
    assert drop.identity not in on_disk
    assert keep.identity in on_disk
    await _shutdown(sched, runner)


async def test_reconcile_prunes_removed_watch_state(tmp_path: Path) -> None:
    # 删掉最后一个监控（零监控）也要成立：任务全撤、已见集合清空落盘。
    watch = _watch(name="W1")
    config = _multi_config(tmp_path, (), (watch,))
    sched = Scheduler(config, _HangingBrowser(), _FakeNotifier([True]))  # type: ignore[arg-type]
    runner = await _start(sched)
    _seed_watch(sched, watch)
    await sched._store_seen(watch.identity, ("1", "2"))

    added, removed = await sched.reconcile(_multi_config(tmp_path))

    assert (added, removed) == (0, 1)
    assert sched._tasks == {}
    assert watch.identity not in sched._watch_failures
    assert watch.identity not in sched._state
    assert load_state(config.state_path) == {}
    await _shutdown(sched, runner)


async def test_trial_fetch_page_waits_for_semaphore(tmp_path: Path) -> None:
    # A4：试抓与常规轮询共用同一信号量，额度占满时排队而非另开并发。
    page = _page()
    config = replace(_config(tmp_path, page), max_concurrent_fetches=1)
    browser = _FakeBrowser([PageFetched(results=())])
    sched = Scheduler(config, browser, _FakeNotifier([True]))  # type: ignore[arg-type]
    await sched._semaphore.acquire()

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(sched.trial_fetch_page(page), timeout=0.05)
    assert browser.calls == 0  # 拿不到额度就没抓

    sched._semaphore.release()
    assert isinstance(await sched.trial_fetch_page(page), PageFetched)
    assert browser.calls == 1


async def test_trial_fetch_list_returns_browser_result(tmp_path: Path) -> None:
    watch = _watch()
    fetched = ListFetched(items=(_item("100", "HK 节点"),))
    browser = _FakeListBrowser([fetched])
    sched = Scheduler(_watch_config(tmp_path, watch), browser, _FakeNotifier([True]))  # type: ignore[arg-type]

    assert await sched.trial_fetch_list(watch) == fetched
    assert browser.calls == 1
    assert sched._state == {}  # 试抓不碰状态，不会把当前帖子当基线记下


async def test_snapshot_reports_kinds_and_statuses(tmp_path: Path) -> None:
    based, fresh = _named_element("p1", "有基线"), _named_element("p1", "无基线")
    page = _named_page("p1", elements=(based, fresh))
    watch = _watch(name="W1")
    config = _multi_config(tmp_path, (page,), (watch,))
    sched = Scheduler(config, _HangingBrowser(), _FakeNotifier([True]))  # type: ignore[arg-type]
    await sched._store(based.identity, "¥99")
    await sched._store_seen(watch.identity, ("1", "2", "3"))

    rows = sched.snapshot()

    assert [(r.kind, r.identity, r.url, r.status) for r in rows] == [
        ("元素", "m / p1 / 有基线", "https://e.com/p1", "¥99"),
        ("元素", "m / p1 / 无基线", "https://e.com/p1", "尚未建立基线"),
        ("论坛", "watch / W1", "https://www.nodeseek.com/", "已见 3 帖"),
    ]


def test_snapshot_empty_when_no_monitors(tmp_path: Path) -> None:
    sched = Scheduler(_multi_config(tmp_path), _HangingBrowser(), _FakeNotifier([True]))  # type: ignore[arg-type]
    assert sched.snapshot() == ()


def test_matches_is_case_insensitive_or() -> None:
    # _matches 改公开为 matches，供 /add 试抓回显复用同一判定口径。
    assert matches(("hk", "jp"), "香港 HK 大促") is True
    assert matches(("HK",), "香港 hk 大促") is True
    assert matches(("hk",), "日本节点上新") is False
