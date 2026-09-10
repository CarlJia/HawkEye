"""extract 模块测试：用 set_content 加载本地夹具，走真实无头浏览器提取。"""

from __future__ import annotations

import time
from pathlib import Path

from playwright.async_api import Page, async_playwright

from hawkeye.config import MonitoredElement, WatchTarget
from hawkeye.extract import extract_list_items, extract_text, normalize_text

_FIXTURE = (Path(__file__).parent / "fixtures" / "yunyoo_sample.html").read_text(encoding="utf-8")
_NODESEEK = (Path(__file__).parent / "fixtures" / "nodeseek_sample.html").read_text(
    encoding="utf-8"
)

# 导航返回后才由 JS 追加节点：模拟 domcontentloaded 落地时选择器尚匹配不到的动态页。
_LATE_RENDER = """
<div id="host"></div>
<script>
  setTimeout(function () {
    var d = document.createElement("div");
    d.className = "late";
    d.textContent = "  充足\\n ";
    document.getElementById("host").appendChild(d);
  }, 800);
</script>
"""

_HIDDEN = '<div class="hidden-status" style="display:none">充足</div>'

# 徽章里的纯装饰小圆点：DevTools 的「Copy full XPath」常落在这种最内层节点上——
# 选择器确实匹配得到，但它自己没文本，真正的文本在父节点。
_EMPTY_DECORATION = '<span class="badge"><span class="decoration"></span>已售罄</span>'


def _element(
    selector: str, selector_type: str = "auto", nth: int | None = None, js: str | None = None
) -> MonitoredElement:
    return MonitoredElement(
        merchant_name="m",
        page_name="p",
        name="t",
        selector=selector,
        selector_type=selector_type,
        nth=nth,
        js=js,
    )


async def _extract(
    selector: str,
    selector_type: str = "auto",
    nth: int | None = None,
    *,
    js: str | None = None,
    html: str = _FIXTURE,
    timeout_ms: int = 1_000,
) -> str | None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(html)
            result = await extract_text(
                page, _element(selector, selector_type, nth, js), timeout_ms
            )
            return result.value
        finally:
            await browser.close()


async def test_css_first_match_and_trim() -> None:
    assert await _extract(".status") == "充足"


async def test_nth_collapses_whitespace() -> None:
    assert await _extract(".status", nth=2) == "售罄"


async def test_xpath() -> None:
    assert await _extract('//div[@class="status"]') == "充足"


async def test_no_match_gives_up_on_the_given_budget() -> None:
    # 等待预算到点后回落到失败路径（返回 None 而非抛异常）；
    # 上限断言用于钉住 timeout_ms 真的传给了 wait_for，而不是退回 Playwright 默认的 30s
    started = time.monotonic()
    assert await _extract(".does-not-exist", timeout_ms=300) is None
    assert time.monotonic() - started < 10.0


async def test_text_mode_no_match_includes_page_title() -> None:
    # 挑战页是页面级现象,与 JS / 文本模式无关——文本模式未匹配也必须带 title,
    # 否则用户从 Telegram 失败告警看不到「被反爬拦了」,会去改选择器(改错方向)。
    challenge_html = "<html><head><title>Just a moment...</title></head><body></body></html>"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(challenge_html)
            result = await extract_text(page, _element(".does-not-exist"), 300)
            assert result.value is None
            assert result.reason is not None
            assert "Just a moment..." in result.reason
            assert "可能是反爬挑战页" in result.reason
        finally:
            await browser.close()


async def test_nth_out_of_range_returns_none() -> None:
    assert await _extract(".status", nth=99, timeout_ms=300) is None


async def test_matched_but_empty_text_returns_empty_string() -> None:
    # 与上面两条区分开：等不到匹配是 None，匹配到了却没文本是 ""。
    # 合并成 None 的话，上层只能给出一句「未匹配到元素」的假诊断（把人往改选择器上引，
    # 而正确的动作是把选择器往上提一级）。
    assert await _extract(".decoration", html=_EMPTY_DECORATION) == ""


async def test_waits_for_element_rendered_after_navigation() -> None:
    # 导航返回时 .late 还不存在；必须等它挂载，否则会误判为「选择器未匹配」
    assert await _extract(".late", html=_LATE_RENDER, timeout_ms=5_000) == "充足"


async def test_hidden_element_still_extracted() -> None:
    # 等 attached 而非 visible：隐藏元素 inner_text 为空，须保留 text_content 兜底
    assert await _extract(".hidden-status", html=_HIDDEN) == "充足"


def test_normalize_text() -> None:
    assert normalize_text("  a\n   b  ") == "a b"
    assert normalize_text("") == ""


# ---- JS 求值模式（element.js 非空时走 loc.evaluate） ----

# 模拟一个带 class 的按钮：vmiss 场景的镜像。让两个 article 的 class 不同，
# JS 通过 classList.contains 区分「售罄/可订」。
_JS_HTML = """<article id="card-1" class="product">
<a href="#" class="btn btn-order">Commander</a>
</article>
<article id="card-2" class="product">
<a href="#" class="btn disabled">Commander</a>
</article>
"""


async def test_js_classlist_ternary() -> None:
    # JS 返回字符串作为状态值；classList.contains('disabled') 走三元表达式
    js = "el => el.classList.contains('disabled') ? '售罄' : '可订'"
    assert await _extract("#card-1 a", js=js, html=_JS_HTML) == "可订"
    assert await _extract("#card-2 a", js=js, html=_JS_HTML) == "售罄"


async def test_js_returns_null_means_no_match() -> None:
    # JS 返回 null/undefined → 视为「未匹配」，与文本模式 None 同语义
    # 直接拿 ExtractResult 也断言 reason,确保 fetch.py 透传后能区分根因
    js = "el => el.getAttribute('data-stock')"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(_JS_HTML)
            result = await extract_text(page, _element("#card-1 a", js=js), 1_000)
            assert result.value is None
            assert "未返回值" in (result.reason or "")
        finally:
            await browser.close()


async def test_js_non_string_value_normalized() -> None:
    # bool / number 经 str() 归一化为可读字符串；非空就走 FetchOk
    js = "el => el.classList.contains('disabled')"
    # True/False 是合法状态值,不是"未匹配"
    assert await _extract("#card-1 a", js=js, html=_JS_HTML) == "False"
    assert await _extract("#card-2 a", js=js, html=_JS_HTML) == "True"


async def test_js_throws_becomes_reason() -> None:
    # JS 抛错（属性未定义等）不再向上抛,而是被 extract_text 捕获转为 reason 含 "JS 执行失败"
    js = "el => el.nonexistent.foo()"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(_JS_HTML)
            result = await extract_text(page, _element("#card-1 a", js=js), 1_000)
            assert result.value is None
            assert "JS 执行失败" in (result.reason or "")
        finally:
            await browser.close()


# ---- 列表项提取（extract_list_items） ----

_LINK_XPATH = '//*[@id="nsk-body-left"]/ul/li/div/div[1]/a'


def _watch(
    link_selector: str = _LINK_XPATH,
    *,
    url: str = "https://www.nodeseek.com/",
    selector_type: str = "auto",
    id_pattern: str | None = r"post-(\d+)-",
) -> WatchTarget:
    return WatchTarget(
        name="NodeSeek 首页",
        url=url,
        link_selector=link_selector,
        selector_type=selector_type,
        keywords=("hk",),
        id_pattern=id_pattern,
        poll_interval_secs=60,
        wait_until="domcontentloaded",
        nav_timeout_secs=30,
        failure_threshold=3,
    )


async def _extract_items(
    watch: WatchTarget, *, html: str = _NODESEEK, timeout_ms: int = 1_000
) -> list[tuple[str, str, str]]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page: Page = await browser.new_page()
            await page.set_content(html)
            items = await extract_list_items(page, watch, timeout_ms)
            return [(it.post_id, it.title, it.url) for it in items]
        finally:
            await browser.close()


async def test_extract_list_items_basic() -> None:
    # Covers R1：提取出全部帖子的 (ID, 标题, URL)，数量与内容正确
    items = await _extract_items(_watch())
    assert len(items) == 4
    ids = [i[0] for i in items]
    assert ids == ["911200", "911201", "911202", "911203"]
    assert items[0] == (
        "911200",
        "HK 原生 IP 测评",
        "https://www.nodeseek.com/post-911200-1",
    )


async def test_relative_href_resolved_to_absolute() -> None:
    # Covers R4：相对 href 补全为可点击绝对 URL
    items = await _extract_items(_watch())
    assert items[1][2] == "https://www.nodeseek.com/post-911201-1"


async def test_absolute_href_kept() -> None:
    # 已是绝对 URL 的 href 原样保留（urljoin 不改写）
    items = await _extract_items(_watch())
    assert items[3][2] == "https://www.nodeseek.com/post-911203-1"


async def test_id_pattern_extracts_numeric_id() -> None:
    items = await _extract_items(_watch(id_pattern=r"post-(\d+)-"))
    assert items[0][0] == "911200"


async def test_default_id_pattern_uses_path_tail() -> None:
    # 缺省 id_pattern → 按 href path 末段取 ID
    items = await _extract_items(_watch(id_pattern=None))
    assert items[0][0] == "post-911200-1"


async def test_title_whitespace_collapsed() -> None:
    items = await _extract_items(_watch())
    assert items[2][1] == "香港 HK 高防"


async def test_no_match_returns_empty() -> None:
    items = await _extract_items(_watch(link_selector="#does-not-exist a"), timeout_ms=300)
    assert items == []
