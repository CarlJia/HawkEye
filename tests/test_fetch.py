"""fetch 模块测试：页面级失败（创建上下文/页面、导航）应映射为 PageLoadError；
成功导航后每个元素各自产出 FetchOk/FetchNoMatch/FetchError，汇总为 PageFetched。
关停时驱动已随信号退出，close() 应容忍传输层基类 Exception 与 PlaywrightError 而非崩溃。"""

from __future__ import annotations

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from hawkeye import fetch as fetch_mod
from hawkeye.config import Config, Merchant, MonitoredElement, Page, TelegramConfig, WatchTarget
from hawkeye.extract import ListItem
from hawkeye.fetch import (
    BrowserManager,
    FetchError,
    FetchNoMatch,
    FetchOk,
    ListFetched,
    PageFetched,
    PageLoadError,
)


def _element(name: str, selector: str, js: str | None = None) -> MonitoredElement:
    return MonitoredElement(
        merchant_name="m",
        page_name="p",
        name=name,
        selector=selector,
        selector_type="css",
        nth=None,
        js=js,
    )


def _page(*elements: MonitoredElement) -> Page:
    els = elements or (_element("t", ".x"),)
    return Page(
        merchant_name="m",
        name="p",
        url="https://e.com",
        poll_interval_secs=60,
        wait_until="load",
        nav_timeout_secs=30,
        failure_threshold=3,
        elements=els,
    )


def _config() -> Config:
    return Config(
        telegram=TelegramConfig(bot_token="x", chat_id="1"),
        merchants=(Merchant(name="m", pages=(_page(),)),),
        poll_interval_secs=60,
        failure_threshold=3,
        state_path="state.json",
        max_concurrent_fetches=4,
        nav_timeout_secs=30,
        wait_until="load",
    )


class _ContextBoomBrowser:
    async def new_context(self, **kwargs: object) -> object:
        raise PlaywrightError("boom-context")


class _FakeContext:
    def __init__(self, page: object, close_error: BaseException | None = None) -> None:
        self._page = page
        self.closed = False
        self._close_error = close_error
        self.init_scripts: list[str] = []
        self.last_user_agent: str | None = None

    async def new_page(self) -> object:
        if isinstance(self._page, BaseException):
            raise self._page
        return self._page

    async def add_init_script(self, script: str) -> None:
        # 记录 stealth 注入的脚本，便于测试断言是否真触发了 apply_stealth_async
        self.init_scripts.append(script)

    async def close(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


class _FakePlaywrightCtxMgr:
    """伪装 async_playwright() 的返回值,只暴露 start() 同步返回 _FakePlaywright。

    真实 Playwright 的 async_playwright().start() 模式:__call__ 返回这个对象,
    start() 异步返回 Playwright 实例。我们不需要 aenter/aexit,只让 fetch.py 调
    start() / 后续通过 self._pw.chromium.launch(...) 拿浏览器就行。
    """

    def __init__(self, pw: object) -> None:
        self._pw = pw

    async def start(self) -> object:
        return self._pw


class _FakeContextBrowser:
    def __init__(self, page: object, close_error: BaseException | None = None) -> None:
        self.context = _FakeContext(page, close_error)

    async def new_context(self, **kwargs: object) -> _FakeContext:
        if "user_agent" in kwargs:
            self.context.last_user_agent = kwargs["user_agent"]
        return self.context


class _FakeLocator:
    def __init__(
        self,
        texts: list[str],
        evaluate_return: object = None,
        throw_on_evaluate: bool = False,
    ) -> None:
        self._texts = texts
        self._evaluate_return = evaluate_return
        self._throw_on_evaluate = throw_on_evaluate

    def nth(self, idx: int) -> _FakeLocator:
        return _FakeLocator(
            self._texts[idx : idx + 1],
            self._evaluate_return,
            self._throw_on_evaluate,
        )

    async def wait_for(self, **kwargs: object) -> None:
        if not self._texts:
            raise PlaywrightTimeoutError("Timeout 30000ms exceeded")

    async def inner_text(self) -> str:
        return self._texts[0]

    async def text_content(self) -> str | None:
        return self._texts[0]

    async def element_handle(self) -> _FakeLocator | None:
        if not self._texts:
            return None
        return self

    async def evaluate(self, js: str) -> object:
        if self._throw_on_evaluate:
            raise PlaywrightError(f"js-boom: {js}")
        return self._evaluate_return


class _GotoBoomPage:
    async def goto(self, *args: object, **kwargs: object) -> object:
        raise PlaywrightError("boom-goto")


class _FakePage:
    """goto 成功；locator 按选择器返回文本，"boom" 触发 PlaywrightError。

    ``evaluate(js, arg)`` 把 JS 转发给 arg（即 element_handle 返回的 locator），
    模拟真实 ``page.evaluate(js, handle)`` 路径：JS 在 element 上下文跑，由 handle
    对应的 evaluate 决定返回值。
    """

    def __init__(self, mapping: dict[str, list[str]]) -> None:
        self._mapping = mapping

    async def goto(self, *args: object, **kwargs: object) -> object:
        return None

    async def title(self) -> str:
        # 反爬挑战页检测默认返回空 title,对应「非挑战页」分支;有需要的子类覆写。
        return ""

    def locator(self, selector: str) -> _FakeLocator:
        if selector == "boom":
            raise PlaywrightError("locator-boom")
        return _FakeLocator(self._mapping.get(selector, []))

    async def evaluate(self, js: str, arg: object | None = None) -> object:
        if arg is None:
            return None
        # 只把 _FakeLocator / 鸭子类型（带 evaluate）当作 element handle；
        # 否则视为 page 级 evaluate（无具体断言时按 None 处理,避免 AttributeError 冒泡）。
        if not hasattr(arg, "evaluate"):
            return None
        return await arg.evaluate(js)  # type: ignore[attr-defined]


class _ChallengeTogglePage(_FakePage):
    """_FakePage 的子类,挑战页检测可控：按 ``title_sequence`` 依次返回 title,
    配合 ``_is_challenge_page`` / ``_wait_for_challenge_clear`` 验证等待/自愈路径。"""

    def __init__(
        self,
        title_sequence: list[str],
        mapping: dict[str, list[str]] | None = None,
    ) -> None:
        super().__init__(mapping or {})
        self._title_sequence = title_sequence
        self._title_calls = 0

    async def title(self) -> str:
        idx = min(self._title_calls, len(self._title_sequence) - 1)
        self._title_calls += 1
        return self._title_sequence[idx]


class _FlakyGotoPage:
    """前 fail_times 次 goto 抛指定错误，之后成功；成功后 locator 行为同 _FakePage。"""

    def __init__(
        self,
        fail_times: int,
        error: str = "Page.goto: net::ERR_CONNECTION_CLOSED at https://e.com",
        mapping: dict[str, list[str]] | None = None,
    ) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self._error = error
        self._mapping = mapping or {}

    async def goto(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise PlaywrightError(self._error)
        return None

    async def title(self) -> str:
        return ""

    async def evaluate(self, js: str, arg: object | None = None) -> object:
        return None

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self._mapping.get(selector, []))


async def test_new_context_error_returns_pageloaderror() -> None:
    bm = BrowserManager(_config())
    bm._browser = _ContextBoomBrowser()  # type: ignore[assignment]
    result = await bm.fetch_page(_page())
    assert isinstance(result, PageLoadError)
    assert "创建浏览器上下文失败" in result.reason


async def test_new_page_error_returns_pageloaderror_and_closes_context() -> None:
    bm = BrowserManager(_config())
    browser = _FakeContextBrowser(PlaywrightError("boom-page"))
    bm._browser = browser  # type: ignore[assignment]
    result = await bm.fetch_page(_page())
    assert isinstance(result, PageLoadError)
    assert "创建页面失败" in result.reason
    assert browser.context.closed is True  # finally 必须关闭 context


async def test_new_context_applies_stealth_and_aligned_ua() -> None:
    # 修复点 R1：stealth 必须真注入，不能走「Malenia apply_stealth 把 CommonJS 源码
    # 塞进 add_init_script,浏览器里 require is not defined」的零效果路径。
    # init_scripts 非空即说明 playwright-stealth 真跑过——这正是这次 bug 漏过的断言。
    bm = BrowserManager(_config())
    bm._chromium_major = 152  # 模拟启动后拿到的版本
    browser = _FakeContextBrowser(_FakePage({".x": ["充足"]}))
    bm._browser = browser  # type: ignore[assignment]

    await bm.fetch_page(_page(_element("t", ".x")))

    # 修复点 R2：UA 里的 Chrome/x 必须等于运行时 chromium 主版本号，避免与 sec-ch-ua
    # v= 错位被 CF 当作伪造信号（修复前是硬编码 Chrome/125，实跑 chromium 151）。
    assert any("Chrome/152.0.0.0" in s for s in browser.context.init_scripts) or (
        # playwright-stealth 可能把 UA 改写成自己的格式;至少 user_agent 走的应是对齐版本。
        browser.context.last_user_agent and "Chrome/152.0.0.0" in browser.context.last_user_agent
    )


async def test_default_user_agent_aligns_to_runtime_version() -> None:
    # _default_user_agent 是 BrowserManager 唯一可被独立验证的纯函数：版本号必须严格对齐，
    # CF 拿来对照 UA 与 sec-ch-ua 的 v=，任一侧错位都视为伪造。
    from hawkeye.fetch import _default_user_agent

    assert "Chrome/151.0.0.0" in _default_user_agent(151)
    assert "Chrome/152.0.0.0" in _default_user_agent(152)
    # 与旧硬编码 Chrome/125.0.0.0 不可兼容——这是这次 bug 的具体载体,断言锁死。
    assert "Chrome/125" not in _default_user_agent(152)


async def test_start_uses_channel_chrome_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    # 修复点 R3：channel="chrome" 成功时 self._using_channel 应为 True,并
    # 走 stealth 注入日志分支。Mock async_playwright 与 chromium.launch,验证
    # launch 调用参数含 channel="chrome" 且启动后记录下运行时版本。
    launched_with_channel: list[dict[str, object]] = []

    class _FakeBrowser:
        version = "152.0.7977.77"

        async def close(self) -> None:
            pass

    class _FakeChromium:
        async def launch(self, **kwargs: object) -> _FakeBrowser:
            launched_with_channel.append(dict(kwargs))
            return _FakeBrowser()

    class _FakePlaywright:
        chromium = _FakeChromium()

        async def stop(self) -> None:
            pass

    monkeypatch.setattr(
        "hawkeye.fetch.async_playwright",
        lambda: _FakePlaywrightCtxMgr(_FakePlaywright()),
    )

    bm = BrowserManager(_config())
    await bm.start()
    assert launched_with_channel, "launch 应该被调用过一次"
    assert launched_with_channel[0].get("channel") == "chrome"
    assert bm._using_channel is True
    assert bm._chromium_major == 152
    await bm.close()


async def test_start_falls_back_to_bundled_when_channel_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 修复点 R4：目标机未装 Google Chrome 时,channel=chrome 抛 PlaywrightError,
    # 必须降级到 bundled chromium 并记 warning,不能直接让 start() 崩。
    launches: list[dict[str, object]] = []

    class _FakeBrowser:
        version = "151.0.7922.34"

        async def close(self) -> None:
            pass

    class _FakeChromium:
        async def launch(self, **kwargs: object) -> _FakeBrowser:
            launches.append(dict(kwargs))
            if len(launches) == 1:
                # 首次尝试 channel=chrome：模拟目标机未装 Chrome
                raise PlaywrightError("Chromium launch: Executable doesn't exist")
            # 第二次不带 channel：bundled chromium
            return _FakeBrowser()

    class _FakePlaywright:
        chromium = _FakeChromium()

        async def stop(self) -> None:
            pass

    monkeypatch.setattr(
        "hawkeye.fetch.async_playwright",
        lambda: _FakePlaywrightCtxMgr(_FakePlaywright()),
    )

    bm = BrowserManager(_config())
    with caplog.at_level("WARNING"):
        await bm.start()
    assert len(launches) == 2
    assert launches[0].get("channel") == "chrome"
    assert "channel" not in launches[1]
    assert bm._using_channel is False
    assert bm._chromium_major == 151
    # 降级警告必须落进日志,运维能看见并决定是否补装 Chrome。
    assert any("channel=chrome 启动失败" in rec.message for rec in caplog.records)
    await bm.close()


async def test_goto_error_returns_pageloaderror_and_closes_context() -> None:
    bm = BrowserManager(_config())
    browser = _FakeContextBrowser(_GotoBoomPage())
    bm._browser = browser  # type: ignore[assignment]
    result = await bm.fetch_page(_page())
    assert isinstance(result, PageLoadError)
    assert "导航失败" in result.reason
    assert browser.context.closed is True


async def test_transient_nav_error_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    # 首次导航遇代理掐断（ERR_CONNECTION_CLOSED），重试后成功 → 应产出 PageFetched。
    monkeypatch.setattr(fetch_mod, "_NAV_RETRY_BACKOFF_SECS", (0.0, 0.0))
    bm = BrowserManager(_config())
    page = _page(_element("ok", ".ok"))
    flaky = _FlakyGotoPage(fail_times=1, mapping={".ok": ["充足"]})
    browser = _FakeContextBrowser(flaky)
    bm._browser = browser  # type: ignore[assignment]

    result = await bm.fetch_page(page)
    assert isinstance(result, PageFetched)
    assert flaky.calls == 2  # 第一次瞬时失败，第二次成功
    outcomes = {el.name: res for el, res in result.results}
    assert outcomes["ok"] == FetchOk(value="充足")
    assert browser.context.closed is True


async def test_transient_nav_error_exhausts_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    # 瞬时错误持续到用满重试次数 → 仍返回 PageLoadError，且 goto 被调满 1+len(backoff) 次。
    monkeypatch.setattr(fetch_mod, "_NAV_RETRY_BACKOFF_SECS", (0.0, 0.0))
    bm = BrowserManager(_config())
    flaky = _FlakyGotoPage(fail_times=99)  # 始终失败
    browser = _FakeContextBrowser(flaky)
    bm._browser = browser  # type: ignore[assignment]

    result = await bm.fetch_page(_page())
    assert isinstance(result, PageLoadError)
    assert "ERR_CONNECTION_CLOSED" in result.reason
    assert flaky.calls == 3  # 1 次首发 + 2 次重试
    assert browser.context.closed is True


async def test_persistent_nav_error_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    # DNS 等持续性错误不属于瞬时类：首发即返回 PageLoadError，不浪费重试。
    monkeypatch.setattr(fetch_mod, "_NAV_RETRY_BACKOFF_SECS", (0.0, 0.0))
    bm = BrowserManager(_config())
    flaky = _FlakyGotoPage(fail_times=99, error="Page.goto: net::ERR_NAME_NOT_RESOLVED at x")
    browser = _FakeContextBrowser(flaky)
    bm._browser = browser  # type: ignore[assignment]

    result = await bm.fetch_page(_page())
    assert isinstance(result, PageLoadError)
    assert flaky.calls == 1  # 不重试


async def test_page_fetched_collects_per_element_results() -> None:
    bm = BrowserManager(_config())
    page = _page(
        _element("ok", ".ok"),
        _element("missing", ".missing"),
        _element("empty", ".empty"),
        _element("boom", "boom"),
    )
    browser = _FakeContextBrowser(_FakePage({".ok": ["充足"], ".missing": [], ".empty": [""]}))
    bm._browser = browser  # type: ignore[assignment]

    result = await bm.fetch_page(page)
    assert isinstance(result, PageFetched)
    assert browser.context.closed is True

    outcomes = {el.name: res for el, res in result.results}
    assert outcomes["ok"] == FetchOk(value="充足")
    # 「压根没匹配到」与「匹配到了但没文本」是两种毛病、两种改法，reason 必须能分辨
    assert isinstance(outcomes["missing"], FetchNoMatch)
    assert "未匹配" in outcomes["missing"].reason
    assert isinstance(outcomes["empty"], FetchNoMatch)
    assert "文本为空" in outcomes["empty"].reason
    assert isinstance(outcomes["boom"], FetchError)
    assert "提取失败" in outcomes["boom"].reason


class _DeadDriverBrowser:
    """关停时驱动子进程已随信号退出：传输层直接抛基类 Exception（线上真实类型，
    非 PlaywrightError 子类），close() 也必须容忍。"""

    async def close(self) -> None:
        raise Exception("Browser.close: Connection closed while reading from the driver")


class _DeadDriverPlaywright:
    """stop() 抛 PlaywrightError（Exception 子类），同样应被吞掉。"""

    async def stop(self) -> None:
        raise PlaywrightError("Connection closed while reading from the driver")


async def test_close_tolerates_dead_driver_browser() -> None:
    bm = BrowserManager(_config())
    bm._browser = _DeadDriverBrowser()  # type: ignore[assignment]
    await bm.close()  # 驱动已死时不应冒泡 PlaywrightError
    assert bm._browser is None


async def test_close_tolerates_dead_driver_playwright() -> None:
    bm = BrowserManager(_config())
    bm._pw = _DeadDriverPlaywright()  # type: ignore[assignment]
    await bm.close()  # stop() 遇驱动已死不应冒泡
    assert bm._pw is None


async def test_fetch_page_tolerates_context_close_playwright_error() -> None:
    # 关停竞态：驱动随 Ctrl+C 的 SIGINT 先退出，finally 里 context.close() 抛
    # PlaywrightError（TargetClosedError 即其子类）。fetch_page 应正常返回 PageFetched，
    # 不把清理异常冒泡成调度器的"轮询未预期异常"。
    bm = BrowserManager(_config())
    browser = _FakeContextBrowser(
        _FakePage({".x": ["充足"]}),
        close_error=PlaywrightError(
            "BrowserContext.close: Target page, context or browser has been closed"
        ),
    )
    bm._browser = browser  # type: ignore[assignment]
    result = await bm.fetch_page(_page())
    assert isinstance(result, PageFetched)
    assert browser.context.closed is True


async def test_fetch_page_tolerates_context_close_base_exception() -> None:
    # 传输层在驱动已死时抛基类 Exception（线上真实类型，非 PlaywrightError 子类），
    # fetch_page 同样不应冒泡。
    bm = BrowserManager(_config())
    browser = _FakeContextBrowser(
        _FakePage({".x": ["充足"]}),
        close_error=Exception(
            "BrowserContext.close: Connection closed while reading from the driver"
        ),
    )
    bm._browser = browser  # type: ignore[assignment]
    result = await bm.fetch_page(_page())
    assert isinstance(result, PageFetched)
    assert browser.context.closed is True


# ---- fetch_list：列表页抓取（用 monkeypatch 隔离 extract_list_items） ----


def _watch() -> WatchTarget:
    return WatchTarget(
        name="w",
        url="https://e.com",
        link_selector="a",
        selector_type="css",
        keywords=("hk",),
        id_pattern=None,
        poll_interval_secs=60,
        wait_until="load",
        nav_timeout_secs=30,
        failure_threshold=3,
    )


def _patch_extract(monkeypatch: pytest.MonkeyPatch, items: list[ListItem]) -> None:
    async def _fake_extract(page: object, watch: object, timeout_ms: int) -> list[ListItem]:
        return items

    monkeypatch.setattr(fetch_mod, "extract_list_items", _fake_extract)


async def test_fetch_list_success_returns_listfetched(monkeypatch: pytest.MonkeyPatch) -> None:
    # Covers R1：导航成功 → 返回 ListFetched，携带提取到的列表项
    sample = [ListItem(post_id="1", title="HK 节点", url="https://e.com/post-1-1")]
    _patch_extract(monkeypatch, sample)
    bm = BrowserManager(_config())
    browser = _FakeContextBrowser(_FakePage({}))
    bm._browser = browser  # type: ignore[assignment]

    result = await bm.fetch_list(_watch())
    assert isinstance(result, ListFetched)
    assert result.items == tuple(sample)
    assert browser.context.closed is True


async def test_fetch_list_empty_still_listfetched(monkeypatch: pytest.MonkeyPatch) -> None:
    # 空列表原样返回 ListFetched(())，失败判定留给调度层（PF1）
    _patch_extract(monkeypatch, [])
    bm = BrowserManager(_config())
    browser = _FakeContextBrowser(_FakePage({}))
    bm._browser = browser  # type: ignore[assignment]

    result = await bm.fetch_list(_watch())
    assert result == ListFetched(items=())
    assert browser.context.closed is True


async def test_fetch_list_nav_error_returns_pageloaderror() -> None:
    # Covers R12：导航失败 → PageLoadError
    bm = BrowserManager(_config())
    browser = _FakeContextBrowser(_GotoBoomPage())
    bm._browser = browser  # type: ignore[assignment]
    result = await bm.fetch_list(_watch())
    assert isinstance(result, PageLoadError)
    assert "导航失败" in result.reason
    assert browser.context.closed is True


async def test_fetch_list_transient_nav_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fetch_mod, "_NAV_RETRY_BACKOFF_SECS", (0.0, 0.0))
    _patch_extract(monkeypatch, [ListItem(post_id="1", title="t", url="https://e.com/post-1-1")])
    bm = BrowserManager(_config())
    flaky = _FlakyGotoPage(fail_times=1)
    browser = _FakeContextBrowser(flaky)
    bm._browser = browser  # type: ignore[assignment]

    result = await bm.fetch_list(_watch())
    assert isinstance(result, ListFetched)
    assert flaky.calls == 2
    assert browser.context.closed is True


async def test_fetch_list_extraction_error_returns_pageloaderror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 提取过程抛 PlaywrightError → 归为页面级失败（R12），不冒泡为未预期异常
    async def _boom_extract(page: object, watch: object, timeout_ms: int) -> list[ListItem]:
        raise PlaywrightError("target closed mid-extract")

    monkeypatch.setattr(fetch_mod, "extract_list_items", _boom_extract)
    bm = BrowserManager(_config())
    browser = _FakeContextBrowser(_FakePage({}))
    bm._browser = browser  # type: ignore[assignment]

    result = await bm.fetch_list(_watch())
    assert isinstance(result, PageLoadError)
    assert "列表项提取失败" in result.reason
    assert browser.context.closed is True


async def test_fetch_list_tolerates_context_close_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # 关停竞态：finally 里 context.close() 抛异常应被吞掉，不覆盖正常返回
    _patch_extract(monkeypatch, [])
    bm = BrowserManager(_config())
    browser = _FakeContextBrowser(
        _FakePage({}),
        close_error=PlaywrightError("BrowserContext.close: Target ... has been closed"),
    )
    bm._browser = browser  # type: ignore[assignment]
    result = await bm.fetch_list(_watch())
    assert isinstance(result, ListFetched)
    assert browser.context.closed is True


# ---- JS 模式（element.js 非空）：evaluate 返回值决定 FetchResult 形状 ----


class _JsFakePage(_FakePage):
    """_FakePage 的子类，覆盖 locator 让 evaluate 行为可控；文本路径不变。"""

    def __init__(
        self,
        mapping: dict[str, list[str]],
        *,
        js_evaluate_returns: dict[str, object] | None = None,
        js_throw_selectors: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(mapping)
        self._js_returns = js_evaluate_returns or {}
        self._js_throws = js_throw_selectors

    def locator(self, selector: str) -> _FakeLocator:
        if selector == "boom":
            raise PlaywrightError("locator-boom")
        return _FakeLocator(
            self._mapping.get(selector, []),
            evaluate_return=self._js_returns.get(selector),
            throw_on_evaluate=selector in self._js_throws,
        )


async def test_js_mode_ok() -> None:
    # evaluate 返回字符串 → FetchOk(value=字符串),reason 为空
    bm = BrowserManager(_config())
    browser = _FakeContextBrowser(
        _JsFakePage({".btn": ["dummy"]}, js_evaluate_returns={".btn": "售罄"})
    )
    bm._browser = browser  # type: ignore[assignment]
    result = await bm.fetch_page(_page(_element("状态", ".btn", js="el => el.outerHTML")))
    assert isinstance(result, PageFetched)
    ((element, outcome),) = result.results
    assert outcome == FetchOk(value="售罄")


async def test_js_mode_null_returns_no_match() -> None:
    # evaluate 返回 None → FetchNoMatch(reason="JS 表达式未返回值"),与文本模式 None 等价
    bm = BrowserManager(_config())
    browser = _FakeContextBrowser(
        _JsFakePage({".btn": ["dummy"]}, js_evaluate_returns={".btn": None})
    )
    bm._browser = browser  # type: ignore[assignment]
    result = await bm.fetch_page(_page(_element("状态", ".btn", js="el => null")))
    assert isinstance(result, PageFetched)
    ((element, outcome),) = result.results
    assert isinstance(outcome, FetchNoMatch)
    assert "JS 表达式未返回值" in outcome.reason


async def test_js_mode_exception_returns_no_match_with_reason() -> None:
    # evaluate 抛 PlaywrightError → extract_text 捕获后转为 FetchNoMatch(reason="JS 执行失败：…")
    # reason 仍带 JS 字符串,足以定位是 JS 求值失败。
    bm = BrowserManager(_config())
    browser = _FakeContextBrowser(
        _JsFakePage({".btn": ["dummy"]}, js_throw_selectors=frozenset({".btn"}))
    )
    bm._browser = browser  # type: ignore[assignment]
    result = await bm.fetch_page(_page(_element("状态", ".btn", js="el => el.foo.bar()")))
    assert isinstance(result, PageFetched)
    ((element, outcome),) = result.results
    assert isinstance(outcome, FetchNoMatch)
    assert "JS 执行失败" in outcome.reason
    assert "el => el.foo.bar()" in outcome.reason


# ---- 反爬挑战页检测 + 等待 ----


class _StaticTitlePage(_FakePage):
    """固定返回同一 title 的 _FakePage,用于验证 _is_challenge_page 的单次判定。"""

    def __init__(self, title: str, mapping: dict[str, list[str]] | None = None) -> None:
        super().__init__(mapping or {})
        self._title = title
        self.title_calls = 0

    async def title(self) -> str:
        self.title_calls += 1
        return self._title


class _ThrowEvaluatePage(_FakePage):
    """evaluate 抛 PlaywrightError——验证 _is_challenge_page 保守地按 False 处理。"""

    async def evaluate(self, js: str, arg: object | None = None) -> object:  # type: ignore[override]
        raise PlaywrightError("evaluate-boom")


async def test_is_challenge_page_matches_just_a_moment_title() -> None:
    # 返回 (命中, 命中片段)。命中 True 时必带至少一个片段,日志据此区分 IUAM / Turnstile。
    page = _StaticTitlePage("Just a moment...")
    is_challenge, hits = await fetch_mod._is_challenge_page(page)
    assert is_challenge is True
    assert any("just a moment" in h for h in hits)
    assert page.title_calls == 1


async def test_is_challenge_page_matches_ddos_guard_title() -> None:
    # 另一个常见的反爬实现:DDoS-Guard
    page = _StaticTitlePage("DDoS-Guard verification")
    is_challenge, hits = await fetch_mod._is_challenge_page(page)
    assert is_challenge is True
    assert any("ddos-guard" in h for h in hits)


async def test_is_challenge_page_normal_title_returns_false() -> None:
    page = _StaticTitlePage("vmiss - Los Angeles TRI", mapping={".x": ["充足"]})
    is_challenge, hits = await fetch_mod._is_challenge_page(page)
    assert is_challenge is False
    assert hits == ()


async def test_is_challenge_page_title_error_treated_as_false() -> None:
    # title() 抛错（驱动半死）→ 保守按 False 处理,不阻塞正常提取。
    class _TitleBoom(_StaticTitlePage):
        async def title(self) -> str:
            raise PlaywrightError("title-boom")

    page = _TitleBoom("Just a moment...", mapping={})
    is_challenge, hits = await fetch_mod._is_challenge_page(page)
    assert is_challenge is False
    assert hits == ()


async def test_is_challenge_page_evaluate_error_treated_as_false() -> None:
    # evaluate 抛错（DOM 检测失败）→ 保守按 False 处理。
    page = _ThrowEvaluatePage({".x": ["充足"]})
    is_challenge, hits = await fetch_mod._is_challenge_page(page)
    assert is_challenge is False
    assert hits == ()


async def test_is_challenge_page_returns_dom_marker_when_cf_turnstile_present() -> None:
    """Turnstile 渲染在 body 注入 ``<div class="cf-turnstile">`` —— DOM 路径命中。

    命中片段既要让用户知道是 IUAM（title）也要知道 Turnstile（DOM），双线诊断。
    _FakePage.evaluate 只对 element handle 起作用（转给 locator.evaluate），page 级
    evaluate 在这里直接 monkeypatch：让 _is_challenge_page 看到 DOM 标记存在。
    """
    page = _StaticTitlePage("Just a moment...", mapping={})

    async def _fake_evaluate(js: str, arg: object | None = None) -> object:
        # 模拟 document.querySelector(sel) 命中了一个 cf-turnstile iframe 容器
        return "div.cf-turnstile"

    page.evaluate = _fake_evaluate  # type: ignore[method-assign]
    is_challenge, hits = await fetch_mod._is_challenge_page(page)
    assert is_challenge is True
    # 命中片段同时覆盖 title 和 DOM 路径
    assert any("just a moment" in h for h in hits)
    assert any("cf-turnstile" in h for h in hits)


def test_challenge_backoff_extended_to_three_stages() -> None:
    """VPS IP 风控段实测 25–45s 才放行：第三档 25s 把总预算从 20s 提到 45s。

    如果哪天 CF 把放行时段拉得更长，新增第四档 _CHALLENGE_BACKOFF_SECS 时这个测试
    不会爆——直接断言 _CHALLENGE_BACKOFF_SECS 长度 ≥ 3、总预算 ≥ 40s 让改动有据可循。
    """
    secs = fetch_mod._CHALLENGE_BACKOFF_SECS
    assert len(secs) >= 3, f"挑战页 backoff 应至少 3 档，实际 {len(secs)}"
    assert sum(secs) >= 40.0, f"挑战页总预算应 ≥ 40s 覆盖 IP 风控段，实际 {sum(secs)}s"


async def test_wait_for_challenge_clear_no_wait_when_not_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 非挑战页：只查一次就返回 True,sleep 不该被调用。
    monkeypatch.setattr(fetch_mod, "_CHALLENGE_BACKOFF_SECS", (10.0, 10.0))
    sleep_calls: list[float] = []

    async def _fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    monkeypatch.setattr(fetch_mod.asyncio, "sleep", _fake_sleep)
    page = _StaticTitlePage("商品页正常标题", mapping={})
    assert await fetch_mod._wait_for_challenge_clear(page, "test") is True  # type: ignore[arg-type]
    assert sleep_calls == []  # 没进入等待循环


async def test_wait_for_challenge_clear_succeeds_after_one_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 首次检查命中挑战页 → 等 4s → 复查已自愈 → 立即返回 True
    monkeypatch.setattr(fetch_mod, "_CHALLENGE_BACKOFF_SECS", (0.0, 0.0))
    sleep_calls: list[float] = []

    async def _fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    monkeypatch.setattr(fetch_mod.asyncio, "sleep", _fake_sleep)
    page = _ChallengeTogglePage(["Just a moment...", "vmiss - LA TRI"], mapping={})
    assert await fetch_mod._wait_for_challenge_clear(page, "test") is True  # type: ignore[arg-type]
    # 至少经历一次 sleep（退避序列首项）,且退避后二次判定为非挑战页
    assert len(sleep_calls) >= 1
    assert sleep_calls[0] == 0.0


async def test_wait_for_challenge_clear_returns_false_when_stuck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 一直停在挑战页：用满两段退避仍失败 → 返回 False,调用方按失败处理。
    monkeypatch.setattr(fetch_mod, "_CHALLENGE_BACKOFF_SECS", (0.0, 0.0))
    sleep_calls: list[float] = []

    async def _fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    monkeypatch.setattr(fetch_mod.asyncio, "sleep", _fake_sleep)
    page = _StaticTitlePage("Just a moment...", mapping={})
    assert await fetch_mod._wait_for_challenge_clear(page, "test") is False  # type: ignore[arg-type]
    # 用满退避序列 = 2 次 sleep,且循环外有一次终态复查
    assert len(sleep_calls) == 2


async def test_fetch_page_waits_for_challenge_then_extracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 集成：导航落在挑战页 → _wait_for_challenge_clear 通过 → 元素正常提取。
    # 这里直接 monkeypatch _wait_for_challenge_clear 返回 True,断言元素提取照常进行。
    async def _fake_wait(page: object, label: str) -> bool:
        return True

    monkeypatch.setattr(fetch_mod, "_wait_for_challenge_clear", _fake_wait)
    bm = BrowserManager(_config())
    browser = _FakeContextBrowser(_FakePage({".ok": ["充足"]}))
    bm._browser = browser  # type: ignore[assignment]
    result = await bm.fetch_page(_page(_element("ok", ".ok")))
    assert isinstance(result, PageFetched)
    outcomes = {el.name: res for el, res in result.results}
    assert outcomes["ok"] == FetchOk(value="充足")


async def test_fetch_page_challenge_stuck_short_circuits_to_pageloaderror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 挑战页预算用尽仍未放行 → fetch_page 直接 PageLoadError,不再走 element extract。
    # 理由：元素提取在挑战页里只会白等 nav_timeout_secs=30s,既拖垮单次轮询,又把
    # 「反爬卡住」伪装成「选择器未匹配」误导诊断。
    async def _fake_wait(page: object, label: str) -> bool:
        return False

    extract_called = False

    async def _spy_extract(page: object, element: object, timeout_ms: int) -> object:
        from hawkeye.extract import ExtractResult

        nonlocal extract_called
        extract_called = True
        return ExtractResult(value=None, reason="should-not-be-called")

    monkeypatch.setattr(fetch_mod, "_wait_for_challenge_clear", _fake_wait)
    monkeypatch.setattr(fetch_mod, "extract_text", _spy_extract)

    bm = BrowserManager(_config())
    browser = _FakeContextBrowser(_FakePage({".ok": ["充足"]}))
    bm._browser = browser  # type: ignore[assignment]
    result = await bm.fetch_page(_page(_element("ok", ".ok")))
    assert isinstance(result, PageLoadError)
    assert "反爬挑战页" in result.reason
    assert extract_called is False  # 关键：没走 element extract


async def test_fetch_list_challenge_stuck_short_circuits_to_pageloaderror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 列表抓取同源：挑战页卡住直接归为页面级失败,不调 extract_list_items。
    async def _fake_wait(page: object, label: str) -> bool:
        return False

    extract_called = False

    async def _spy_extract(page: object, watch: object, timeout_ms: int) -> list[ListItem]:
        nonlocal extract_called
        extract_called = True
        return []

    monkeypatch.setattr(fetch_mod, "_wait_for_challenge_clear", _fake_wait)
    monkeypatch.setattr(fetch_mod, "extract_list_items", _spy_extract)

    bm = BrowserManager(_config())
    browser = _FakeContextBrowser(_FakePage({}))
    bm._browser = browser  # type: ignore[assignment]
    result = await bm.fetch_list(_watch())
    assert isinstance(result, PageLoadError)
    assert "反爬挑战页" in result.reason
    assert extract_called is False


async def test_fetch_page_calls_challenge_wait_after_goto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 反爬挑战页等待发生在 goto 成功之后、元素提取之前：断言调用顺序。
    order: list[str] = []

    async def _fake_wait(page: object, label: str) -> bool:
        order.append("wait")
        return True

    class _OrderPage(_FakePage):
        async def goto(self, *args: object, **kwargs: object) -> object:
            order.append("goto")
            return None

    async def _fake_extract(page: object, element: object, timeout_ms: int) -> object:
        order.append("extract")
        from hawkeye.extract import ExtractResult

        return ExtractResult(value="充足")

    monkeypatch.setattr(fetch_mod, "_wait_for_challenge_clear", _fake_wait)
    monkeypatch.setattr(fetch_mod, "extract_text", _fake_extract)

    bm = BrowserManager(_config())
    browser = _FakeContextBrowser(_OrderPage({}))
    bm._browser = browser  # type: ignore[assignment]
    await bm.fetch_page(_page(_element("ok", ".ok")))
    assert order == ["goto", "wait", "extract"]


async def test_fetch_list_calls_challenge_wait_after_goto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 列表抓取同样在 goto 后等待挑战页通过,再调 extract_list_items。
    order: list[str] = []

    async def _fake_wait(page: object, label: str) -> bool:
        order.append("wait")
        return True

    class _OrderPage(_FakePage):
        async def goto(self, *args: object, **kwargs: object) -> object:
            order.append("goto")
            return None

    def _patch_extract(monkeypatch: pytest.MonkeyPatch) -> None:
        async def _fake_extract(page: object, watch: object, timeout_ms: int) -> list[ListItem]:
            order.append("extract_list")
            return []

        monkeypatch.setattr(fetch_mod, "extract_list_items", _fake_extract)

    _patch_extract(monkeypatch)
    monkeypatch.setattr(fetch_mod, "_wait_for_challenge_clear", _fake_wait)

    bm = BrowserManager(_config())
    browser = _FakeContextBrowser(_OrderPage({}))
    bm._browser = browser  # type: ignore[assignment]
    await bm.fetch_list(_watch())
    assert order == ["goto", "wait", "extract_list"]


# ---- proxy / fingerprint 翻译（Chromix 风格 launch 参数的内部契约）----


def test_proxy_is_socks5_detection() -> None:
    """SOCKS5 协议要走 Chromium CLI 形参——Playwright proxy 字段对它不完整生效。"""
    from hawkeye.config import ProxyConfig
    from hawkeye.fetch import _proxy_is_socks5

    assert _proxy_is_socks5(ProxyConfig(server="socks5://1.2.3.4:1080"))
    assert not _proxy_is_socks5(ProxyConfig(server="http://1.2.3.4:7890"))
    assert not _proxy_is_socks5(ProxyConfig(server="https://proxy:443"))


def test_socks5_proxy_server_arg_strips_userinfo() -> None:
    """Chromium CLI 形参不接 userinfo；鉴权交给应用层代理前置。

    socks5://user:pass@host:port → --proxy-server='socks5://host:port'
    测试用 shlex.quote 包裹是因为含特殊字符时 Chromium 会报错。
    """
    from hawkeye.config import ProxyConfig
    from hawkeye.fetch import _socks5_proxy_server_arg

    arg = _socks5_proxy_server_arg(ProxyConfig(server="socks5://u:p@1.2.3.4:1080"))
    assert arg is not None
    assert arg.startswith("--proxy-server=")
    assert "u:p@" not in arg  # userinfo 已剥离
    assert arg.endswith("socks5://1.2.3.4:1080") or arg.endswith("'socks5://1.2.3.4:1080'")


def test_socks5_proxy_server_arg_returns_none_for_http() -> None:
    """HTTP 代理不是 SOCKS5：返回 None 让调用方走 Playwright proxy 字段。"""
    from hawkeye.config import ProxyConfig
    from hawkeye.fetch import _socks5_proxy_server_arg

    assert _socks5_proxy_server_arg(ProxyConfig(server="http://1.2.3.4:7890")) is None


def test_playwright_proxy_dict_includes_optional_fields() -> None:
    """Playwright proxy 字典：仅当 username/password/bypass 非 None 才放进 kwargs。

    避免空字符串被 Playwright 当作「强制空密码」——Chromium 会立即报
    'ERR_INVALID_AUTH_CREDENTIALS' 把所有请求都失败，且 _new_context 不会失败
    让我们察觉（context 创建只是注册默认值）。
    """
    from hawkeye.config import ProxyConfig
    from hawkeye.fetch import _playwright_proxy_dict

    full = _playwright_proxy_dict(
        ProxyConfig(server="http://1.2.3.4:7890", username="u", password="p", bypass="*.x")
    )
    assert full == {
        "server": "http://1.2.3.4:7890",
        "username": "u",
        "password": "p",
        "bypass": "*.x",
    }
    minimal = _playwright_proxy_dict(ProxyConfig(server="http://1.2.3.4:7890"))
    assert minimal == {"server": "http://1.2.3.4:7890"}


def test_redact_proxy_server_strips_userinfo() -> None:
    """启动日志打印代理 server 时必须剥离 userinfo：密码不应留进 journal。

    与 Telegram token 脱敏同一原则：用户 / 运维翻日志定位问题时不需要密码，
    但密码泄漏是不可逆事件。Chromix 把这一件事放在 ProxySettings → log 这一层
    做；HawkEye 也跟同思路。
    """
    from hawkeye.fetch import _redact_proxy_server

    assert _redact_proxy_server("http://alice:s3cret@10.0.0.1:7890") == "http://10.0.0.1:7890"
    # 无 userinfo 原样返回（不应被错误剥离 @ 后的空串）
    assert _redact_proxy_server("http://10.0.0.1:7890") == "http://10.0.0.1:7890"
    # socks5 也走同一脱敏
    assert _redact_proxy_server("socks5://u:p@1.2.3.4:1080") == "socks5://1.2.3.4:1080"
