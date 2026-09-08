"""元素文本提取（纯粹围绕 Playwright Page 的读取与归一化）。

放在独立模块，便于用 ``page.set_content`` 加载本地 HTML 夹具直接测试，
无需真实网络导航。除单元素文本外，另提供列表页的「标题链接」批量提取。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .config import MonitoredElement, WatchTarget

logger = logging.getLogger(__name__)

_WS = re.compile(r"\s+")


def normalize_text(s: str) -> str:
    """去首尾空白，并把内部连续空白（含换行/缩进）折叠为单个空格。"""
    return _WS.sub(" ", s).strip()


@dataclass(frozen=True)
class ExtractResult:
    """元素提取结果。

    ``value`` = 归一化后的字符串（或空串、None）。
    ``reason`` = 失败原因（仅在 ``value`` 不可用时非 None）；成功时为 None。
    文本模式与 JS 模式共用同一套语义，区别仅在 reason 文案——fetch.py 不再需要分支 if/elif，
    直接把 reason 转给 FetchNoMatch，由 scheduler / Telegram 失败告警原样转达。
    """

    value: str | None
    reason: str | None = None


def _prefixed_selector(selector: str, effective_type: str) -> str:
    """把配置选择器转成 Playwright 明确前缀形式，避免 auto 猜测歧义。"""
    sel = selector.strip()
    if effective_type == "xpath":
        return sel if sel.startswith("xpath=") else f"xpath={sel}"
    return sel


def _playwright_selector(element: MonitoredElement) -> str:
    """单元素选择器的明确前缀形式。"""
    return _prefixed_selector(element.selector, element.effective_selector_type)


async def extract_text(page: Page, element: MonitoredElement, timeout_ms: int) -> ExtractResult:
    """取匹配元素的状态值，连同失败原因一起返回。

    文本模式（``element.js`` 缺省）：等元素 attached，取 inner_text（空时 text_content 兜底），
    经 normalize_text 归一化。失败时返回 ``(None, reason)`` 或 ``("", reason)``，fetch.py
    据此区分「未匹配」与「匹配但值为空」。

    JS 模式（``element.js`` 非空）：先等元素 attached，再 ``loc.element_handle()`` 拿真实
    DOMElement 句柄交给 ``page.evaluate(js, handle)``。element handle stale 时
    page.evaluate 会抛 PlaywrightError（走 fetch.py 的现有 except 归到 FetchError），
    而非 ``loc.evaluate`` 在某些场景下静默返回 undefined 后归到「JS 表达式未返回值」
    （掩盖真实原因）。

    所有失败路径都返回 reason：文本模式是「选择器未匹配到元素」/「选择器匹配到元素，但其
    文本为空」；JS 模式根据失败点给出更具体的文案（含 handle / evaluate / 选择器未匹配）。
    """
    idx = element.nth if element.nth is not None else 0
    loc = page.locator(_playwright_selector(element)).nth(idx)

    try:
        # 导航只等到 domcontentloaded，JS 渲染的元素此刻可能还没挂载，必须在这里等。
        # 等 attached 而非 visible：隐藏元素也要能取到，靠下面 text_content 兜底。
        await loc.wait_for(state="attached", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        # 挑战页是页面级现象,与 JS / 文本模式无关——一律把页面 title 塞到 reason,
        # 让用户从 Telegram 失败告警直接看出「被反爬拦了」（典型 title「Just a moment...」）,
        # 而不是干巴巴的「选择器未匹配」。取 title 失败时退回到纯静态文案,不阻断失败路径。
        try:
            title = await page.title()
        except PlaywrightError:
            title = ""
        reason = "选择器未匹配到元素"
        if title:
            reason = f"选择器未匹配到元素（页面标题：{title!r}，可能是反爬挑战页）"
        return ExtractResult(value=None, reason=reason)

    if element.js is not None:
        handle = await loc.element_handle()
        if handle is None:
            return ExtractResult(
                value=None,
                reason="JS 模式 element_handle 不可用（strict mode 违规或元素被移除）",
            )
        try:
            raw = await page.evaluate(element.js, handle)
        except PlaywrightError as e:
            return ExtractResult(value=None, reason=f"JS 执行失败：{_first_line(str(e))}")
        if raw is None:
            return ExtractResult(value=None, reason="JS 表达式未返回值")
        return ExtractResult(value=normalize_text(str(raw)), reason=None)

    raw = await loc.inner_text()
    if not raw.strip():
        raw = await loc.text_content() or ""
    if not raw:
        return ExtractResult(
            value="",
            reason="选择器匹配到元素，但其文本为空（多半指向了纯装饰节点，试试上一级）",
        )
    return ExtractResult(value=normalize_text(raw), reason=None)


def _first_line(s: str) -> str:
    """取第一行：Playwright 异常消息常含多行堆栈，嵌入 reason 前先压扁。"""
    return s.splitlines()[0] if s else s


# ---- 列表页标题链接提取 ----


@dataclass(frozen=True)
class ListItem:
    """列表页中的单个帖子项：帖子 ID、标题、可点击的绝对 URL。"""

    post_id: str
    title: str
    url: str


def _extract_id(href: str, pattern: re.Pattern[str] | None) -> str | None:
    """从 href 提取帖子 ID：有 id_pattern 用其捕获组 1，否则取 URL path 末段。"""
    if pattern is not None:
        m = pattern.search(href)
        # 正则可能匹配但无捕获组（如 id_pattern 写成无分组）——此时无从取 ID，跳过。
        if m is None or not m.groups():
            return None
        return m.group(1)
    path = urlsplit(href).path.rstrip("/")
    if not path:
        return None
    return path.rsplit("/", 1)[-1] or None


async def extract_list_items(page: Page, watch: WatchTarget, timeout_ms: int) -> list[ListItem]:
    """提取列表页每个帖子的 (ID, 标题, 绝对 URL)。

    用 ``watch.link_selector`` 定位全部标题链接 ``<a>``：逐个读 href 与文本，href 经
    ``id_pattern``（缺省取 path 末段）提取帖子 ID、经 ``urljoin`` 补全为绝对 URL，
    文本经 :func:`normalize_text` 折叠空白作标题。缺 href 或提取不到 ID 的链接跳过
    （debug 日志、绝不抛异常）；选择器无匹配时返回空列表，交由上层归入失败路径。
    """
    loc = page.locator(_prefixed_selector(watch.link_selector, watch.effective_selector_type))

    try:
        # 与 extract_text 同理：domcontentloaded 落地时列表可能尚未渲染，先等首项挂载。
        await loc.first.wait_for(state="attached", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        return []

    pattern = re.compile(watch.id_pattern) if watch.id_pattern is not None else None
    items: list[ListItem] = []
    for i in range(await loc.count()):
        a = loc.nth(i)
        href = await a.get_attribute("href")
        if not href:
            logger.debug("列表项缺少 href，跳过：watch=%s 第 %d 项", watch.name, i)
            continue
        post_id = _extract_id(href, pattern)
        if post_id is None:
            logger.debug("列表项无法提取帖子 ID，跳过：watch=%s href=%s", watch.name, href)
            continue
        raw_title = await a.inner_text()
        if not raw_title.strip():
            raw_title = await a.text_content() or ""
        items.append(
            ListItem(post_id=post_id, title=normalize_text(raw_title), url=urljoin(watch.url, href))
        )
    return items
