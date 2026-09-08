"""无头浏览器抓取。

进程级共享单个 Chromium 实例；每个页面一次导航即提取其下全部监控元素，
结束后在 ``finally`` 关闭 context 以释放内存。页面加载失败与单个元素的
选择器未匹配/提取异常区分返回，交由调度层分两级处理。

设计参考 Chromix SDK 的 launch() 风格：所有 fingerprint 维度
（user_agent / locale / timezone_id / color_scheme / viewport）、proxy、
extension_paths 在 :class:`BrowserManager` 构造时显式传入，编译到 Playwright
context kwargs 与 Chromium CLI 形参。这一层是「在
launch() 风格 vs. 启动后再调 setLocale/setTimezone 风格」之间选前者——后者要在
每次 new_page 后才能改，已经跨页漏指纹；前者在新 context 创建瞬间就把所有可观测值
一致覆盖。Chromix 把这件事叫 "persona unification"，HawkEye 也走同一路径。
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass
from typing import Any, Literal, cast

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page as PlaywrightPage

# 反 bot 检测,两层各补一面:
#   playwright-stealth.Stealth — JS 层指纹补丁(navigator.webdriver / UA Brands / chrome.runtime /
#     plugins / WebGL 等)。bundled headless Chromium 装了它就能过绝大多数 CF IUAM。
#   channel="chrome" — HTTP/Client Hints 层修复:消除 sec-ch-ua 里的 HeadlessChrome 字样,
#     并让 sec-ch-ua 的 v= 与请求头 User-Agent 的 Chrome/x 保持一致,避免被 CF 抓版本错位。
# 都不可用时降级到原生 Playwright。
try:
    from playwright_stealth import Stealth as _Stealth  # type: ignore[import-untyped]

    _STEALTH_AVAILABLE = True
except ImportError:  # pragma: no cover —— 包未装时走降级路径
    _Stealth = None
    _STEALTH_AVAILABLE = False

from .config import Config, Fingerprint, MonitoredElement, Page, ProxyConfig, WatchTarget
from .extract import ListItem, extract_list_items, extract_text

logger = logging.getLogger(__name__)


def _default_user_agent(chromium_major: int) -> str:
    # 用运行时真实主版本号拼 UA,避免与 sec-ch-ua / User-Agent Client Hints 的
    # v= 错位(CF 拿来对照,版本错位即视为伪造)。launch 后由 BrowserManager.start()
    # 拿到 Browser.version 的主版本号填进来;测试里会 mock 成固定值。
    return (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{chromium_major}.0.0.0 Safari/537.36"
    )


_WaitUntil = Literal["load", "domcontentloaded", "networkidle", "commit"]

# 导航时的瞬时网络错误：本机走代理（TUN/系统代理）时，代理内核重载配置或节点抖动
# 会掐断在途连接，Chromium 报这些 net::ERR_*。它们下一瞬多半自愈，重试一次即恢复；
# 因此对这类错误做有限次重试，避免单次抖动被记成页面加载失败、误触发失败告警。
# 超时、DNS、证书等持续性错误不在此列——重试只是白等，仍直接返回 PageLoadError。
_TRANSIENT_NAV_ERRORS = (
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_REFUSED",
    "ERR_CONNECTION_ABORTED",
    "ERR_EMPTY_RESPONSE",
    "ERR_SOCKET_NOT_CONNECTED",
    "ERR_NETWORK_CHANGED",
    "ERR_ABORTED",
)
# 退避序列长度即重试次数；总尝试次数 = len + 1。控制在 poll 间隔内，勿吃满一轮。
_NAV_RETRY_BACKOFF_SECS: tuple[float, ...] = (1.0, 2.0)

# 反爬挑战页特征：CF IUAM / Turnstile 与 DDoS-Guard 的标准文案与 DOM 标记。
# 真实站点常被改写 / 部分 CDN 拦截后回退文案,因此同时看 title 与 DOM 标记,提高命中率。
# 任一命中即视为「页面还在挑战页」,需等待 stealth + channel 自动放行。
_CHALLENGE_TITLE_FRAGMENTS = (
    "just a moment",  # Cloudflare IUAM 经典文案
    "checking your browser",  # CF + 部分自建风控
    "ddos-guard",  # DDoS-Guard
    "verifying you are human",  # Turnstile 独立页面
    "attention required",  # CF 阻断页变体
)
# CF 注入的脚本 / 容器标记;任一存在即视为挑战页尚未放行。
_CHALLENGE_DOM_SELECTOR = (
    'script[src*="challenges.cloudflare.com"]',
    "#cf-challenge-running",
    "#cf-mitigated",
    '[class*="cf-turnstile"]',
)
# 反爬挑战页放行预算：实测 CF IUAM / Turnstile 在 IP 风控时段经常 8–18s 才放行，
# 但 VPS / 数据中心 IP 段被风控时实测 25–45s 才放，偶发更久。原 (8, 12) 20s 预算对
# 风控段 IP 不够，会每轮都失败告警。给到 8 + 12 + 25 = 45s 总预算（仍
# < nav_timeout_secs=30s 的 1.5 倍）——既覆盖常见放行区间,又不至于把单次轮询拖到
# 接近 poll_interval_secs=60s 引发排队。预算内一旦检测到挑战页消失立刻放行——
# 绝大多数首次访问远小于此预算,正常页面零开销。
_CHALLENGE_BACKOFF_SECS: tuple[float, ...] = (8.0, 12.0, 25.0)


def _is_transient_nav_error(reason: str) -> bool:
    return any(code in reason for code in _TRANSIENT_NAV_ERRORS)


async def _is_challenge_page(page: PlaywrightPage) -> tuple[bool, tuple[str, ...]]:
    """判断当前页是否仍是反爬挑战页。

    CF 失败时按 False 处理（保守）：避免因 title()/evaluate 异常把真页面误判为挑战页
    而无谓等待。title 与 DOM 标记同时检查——文案常被本地化改写,DOM 标记更稳。

    返回 (命中, 命中片段) 二元组——命中片段同时返回 title 命中词与 DOM 命中
    selector，便于排错时区分 IUAM ("just a moment") / Turnstile (cf-turnstile class)
    / DDoS-Guard / DDoS-Guard 变体。光返回 bool 让 25s+ 等待期间用户无法判断
    「为什么 CF 没放行」，日志里加一行诊断相当于「自带诊断包的失败告警」。
    """
    try:
        title = (await page.title()).lower()
    except PlaywrightError:
        title = ""
    title_hits = [frag for frag in _CHALLENGE_TITLE_FRAGMENTS if frag in title]
    selector = ", ".join(_CHALLENGE_DOM_SELECTOR)
    try:
        dom_has_marker = await page.evaluate(
            "(sel) => {"
            " const el = document.querySelector(sel);"
            " if (!el) return null;"
            " return el.tagName.toLowerCase() + (el.id ? '#' + el.id : '')"
            " + (typeof el.className === 'string' && el.className"
            " ? '.' + el.className.trim().split(/\\s+/).join('.') : '');"
            "}",
            selector,
        )
    except PlaywrightError:
        dom_has_marker = None
    hits: list[str] = []
    if title_hits:
        hits.append(f"title[{','.join(title_hits)}]={title!r}")
    if dom_has_marker:
        hits.append(f"dom[{dom_has_marker}]")
    return (bool(hits), tuple(hits))


async def _wait_for_challenge_clear(page: PlaywrightPage, label: str) -> bool:
    """若当前是反爬挑战页,按退避序列等页面自愈。

    返回 True 表示已自愈（页面不再是挑战页）；False 表示仍卡在挑战页（调用方应按失败处理）。
    总预算约 45s（_CHALLENGE_BACKOFF_SECS 之和）,不影响正常页面吞吐——非挑战页命中后
    立即返回；预算内一旦通过也立即放行,不会等满。
    """
    is_challenge, hits = await _is_challenge_page(page)
    if not is_challenge:
        return True
    logger.info(
        "页面 %s 检测到反爬挑战页，命中片段：%s",
        label,
        "; ".join(hits) or "(未知)",
    )
    for attempt, backoff in enumerate(_CHALLENGE_BACKOFF_SECS):
        logger.info(
            "页面 %s 等待反爬挑战页放行，%.0fs 后重检（第 %d 次）",
            label,
            backoff,
            attempt + 1,
        )
        await asyncio.sleep(backoff)
        is_challenge, hits = await _is_challenge_page(page)
        if not is_challenge:
            logger.info(
                "页面 %s 反爬挑战页已通过（第 %d 次重检后，命中片段：%s）",
                label,
                attempt + 1,
                "; ".join(hits) or "(无)",
            )
            return True
    return not is_challenge


@dataclass(frozen=True)
class FetchOk:
    """成功取到文本。"""

    value: str


@dataclass(frozen=True)
class FetchNoMatch:
    """导航成功但没取到可用文本；reason 区分「压根没匹配到」与「匹配到了却是空文本」。"""

    reason: str


@dataclass(frozen=True)
class FetchError:
    """单个元素提取异常。"""

    reason: str


FetchResult = FetchOk | FetchNoMatch | FetchError


@dataclass(frozen=True)
class PageLoadError:
    """页面级失败：创建上下文/页面或导航失败，本轮无法提取任何元素。"""

    reason: str


@dataclass(frozen=True)
class PageFetched:
    """页面导航成功，携带每个元素各自的提取结果。"""

    results: tuple[tuple[MonitoredElement, FetchResult], ...]


PageResult = PageLoadError | PageFetched


@dataclass(frozen=True)
class ListFetched:
    """列表页导航成功，携带提取到的全部列表项（可能为空，交由调度层判定失败）。"""

    items: tuple[ListItem, ...]


ListResult = PageLoadError | ListFetched


def _first_line(s: str) -> str:
    return s.splitlines()[0] if s else s


def _proxy_is_socks5(proxy: ProxyConfig) -> bool:
    """SOCKS5 代理需要走 Chromium CLI 形参：Playwright proxy 字段对 SOCKS 不生效。

    返回 True 表示此代理是 SOCKS5 协议。Chromix 也走同样策略：HTTP 代理交给
    Playwright，SOCKS5 走 ``--proxy-server``。HawkEye 的 proxy.server 形如
    ``socks5://host:port``，据此判定。
    """
    return proxy.server.startswith("socks5://")


def _socks5_proxy_server_arg(proxy: ProxyConfig) -> str | None:
    """翻译 SOCKS5 代理为 Chromium ``--proxy-server`` 形参。

    Playwright 的 proxy 字段对 SOCKS 不完整生效——实测仅 HTTP/HTTPS scheme 可走。
    把 SOCKS5 落进 CLI 形参是 Chromix / Chrome 文档均确认的兜底方案。注意：
    Chromium CLI 形参无法表达用户名密码，SOCKS5 鉴权场景只能交给应用层代理前置。
    返回 None 表示不是 SOCKS5（HTTP 代理走 Playwright 字段）。
    """
    if not _proxy_is_socks5(proxy):
        return None
    # socks5://user:pass@host:port → Chromium 接受 scheme://host:port，无认证信息。
    server = proxy.server
    # 去掉 userinfo 段
    if "@" in server:
        server = "socks5://" + server.split("@", 1)[1]
    return f"--proxy-server={shlex.quote(server)}"


def _playwright_proxy_dict(proxy: ProxyConfig) -> dict[str, object]:
    """翻译 HTTP 代理为 Playwright ``new_context(proxy=...)`` 字段。"""
    pw: dict[str, object] = {"server": proxy.server}
    if proxy.username:
        pw["username"] = proxy.username
    if proxy.password:
        pw["password"] = proxy.password
    if proxy.bypass:
        pw["bypass"] = proxy.bypass
    return pw


def _playwright_proxy_kwargs(proxy: ProxyConfig) -> dict[str, object]:
    """用于 ``chromium.launch(proxy=...)``：与 context 层 proxy 字段同形。"""
    return {"proxy": _playwright_proxy_dict(proxy)}


def _redact_proxy_server(server: str) -> str:
    """日志里显示代理 server 时剥离 userinfo 段：与 Telegram token 脱敏同一口径。

    启动日志会打印「代理已配置」一行便于排错，但密码不应留在 journal；与
    :func:`hawkeye.notify.redact` 思路一致。
    """
    if "@" not in server:
        return server
    scheme, rest = server.split("://", 1) if "://" in server else ("http", server)
    _, host = rest.rsplit("@", 1)
    return f"{scheme}://{host}"


class BrowserManager:
    """管理进程级共享 Chromium。

    launch 风格 API（参考 Chromix ``launch()``）：构造时把 fingerprint / proxy /
    extension_paths 等显式传入，:meth:`start` 时编译到 Chromium CLI 形参与默认
    context kwargs；每次 :meth:`fetch_page` / :meth:`fetch_list` 又可按
    ``Page`` / ``WatchTarget`` 的 ``fingerprint`` / ``proxy`` 字段 override——
    让某些商家单独走代理或换 locale 时不用动全局配置。Config 里的 fingerprint /
    proxy 作为「未 override 时」的默认值。
    """

    def __init__(
        self,
        config: Config,
        *,
        fingerprint: Fingerprint | None = None,
        proxy: ProxyConfig | None = None,
        extension_paths: tuple[str, ...] | None = None,
    ) -> None:
        self._config = config
        # 默认 fingerprint / proxy 从 config 顶层取；调用方可显式覆盖。
        self._fingerprint: Fingerprint = fingerprint or config.fingerprint
        self._proxy: ProxyConfig | None = proxy if proxy is not None else config.proxy
        self._extension_paths: tuple[str, ...] = tuple(extension_paths or ())
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        # 反检测栈：playwright-stealth (JS 层) + channel="chrome" (HTTP 层)。前者装好
        # 大部分 CF 都会放行,后者进一步消除 sec-ch-ua 里的 HeadlessChrome 字样与
        # UA 版本错位;真 Chrome 未装时降级到 bundled chromium,记录 warning 提醒。
        self._stealth: _Stealth | None = _Stealth() if _STEALTH_AVAILABLE else None
        self._using_channel: bool = False
        # _chromium_major 推迟到 start() 之后再填,测试里可通过 start 路径或直接
        # 注入 Browser.version 来覆盖;避免在构造时拍死版本。
        self._chromium_major: int = 0

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        # proxy 翻译：HTTP 代理由 Playwright proxy 字段处理；SOCKS5 在 Playwright 的
        # proxy 字段下不生效（实测只对 HTTP scheme 完整转发），需要落到 Chromium CLI
        # 形参 --proxy-server 上。两者可叠加——CLI 形参给 SOCKS5，HTTP 代理不走 CLI。
        launch_args: list[str] = ["--headless=new"]
        proxy_kwargs: dict[str, Any] = {}
        if self._proxy is not None:
            socks5_arg = _socks5_proxy_server_arg(self._proxy)
            if socks5_arg:
                launch_args.append(socks5_arg)
            else:
                proxy_kwargs = _playwright_proxy_kwargs(self._proxy)
        if self._extension_paths:
            # Chromix 风格：每个扩展路径同时挂 --load-extension 与
            # --disable-extensions-except，仅放行列表内的扩展，避免其他无关扩展污染指纹。
            ext_csv = ",".join(self._extension_paths)
            launch_args.append(f"--load-extension={ext_csv}")
            launch_args.append(f"--disable-extensions-except={ext_csv}")

        # 新无头模式（Chrome 109+）：headless=True 仍传,但加 --headless=new 让 Chromium
        # 走"新无头"路径,headless 流量更难被 TLS/HTTP2 指纹识别为 bot。
        # 优先 channel="chrome" 拿真 Chrome,消除 sec-ch-ua 里的 HeadlessChrome 字样与
        # UA 版本错位;目标机未装 Chrome 时降级到 bundled chromium。
        try:
            self._browser = await self._pw.chromium.launch(
                headless=True,
                channel="chrome",
                args=launch_args,
                **proxy_kwargs,
            )
            self._using_channel = True
        except PlaywrightError as e:
            logger.warning(
                "channel=chrome 启动失败,降级 bundled chromium: %s;装 Google Chrome 可更稳过 CF",
                _first_line(str(e)),
            )
            try:
                self._browser = await self._pw.chromium.launch(
                    headless=True,
                    args=launch_args,
                    **proxy_kwargs,
                )
            except PlaywrightError as e2:
                raise RuntimeError(
                    f"启动 Chromium 失败：{_first_line(str(e2))}"
                    "（已尝试 channel=chrome 与 bundled）"
                ) from e2
            self._using_channel = False
        self._chromium_major = int(self._browser.version.split(".")[0])
        if self._stealth is not None:
            tag = "已启用 stealth（JS 层指纹补丁）"
            if self._using_channel:
                tag += " + channel=chrome（HTTP 层：消除 HeadlessChrome 与 UA 版本错位）"
            else:
                tag += " + bundled chromium（未装 Google Chrome，HTTP 层仍可能暴露）"
            logger.info("无头浏览器已启动,%s", tag)
        else:
            logger.warning("未装 playwright-stealth,JS 层指纹检测会暴露 headless 流量")
        if self._proxy is not None:
            logger.info(
                "代理已配置（%s，server=%s）",
                "SOCKS5（Chromium CLI）" if _proxy_is_socks5(self._proxy) else "HTTP（Playwright）",
                _redact_proxy_server(self._proxy.server),
            )

    async def close(self) -> None:
        # 关停时驱动子进程可能已随进程组信号先行退出（如 Ctrl+C 的 SIGINT 传遍
        # 整个进程组）。此时 close()/stop() 会因连接断开抛异常，且未必是
        # PlaywrightError——传输层直接抛基类 Exception（"Connection closed while
        # reading from the driver"）。清理是幂等的尽力操作，统一吞掉并置 None
        # 以保证优雅退出；KeyboardInterrupt/CancelledError 继承自 BaseException，
        # 不会被这里误吞。
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as e:
                logger.warning("关闭浏览器时忽略异常（驱动可能已退出）：%s", _first_line(str(e)))
            self._browser = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception as e:
                logger.warning("停止 Playwright 时忽略异常：%s", _first_line(str(e)))
            self._pw = None
        logger.info("无头浏览器已关闭")

    async def _new_context(
        self,
        *,
        fingerprint: Fingerprint | None = None,
        proxy: ProxyConfig | None = None,
    ) -> BrowserContext:
        """创建 context 并注入 stealth。

        stealth 注入到 context 层而非 page 层,因为 ``add_init_script`` 会在该 context 下
        每个新页面加载前自动跑——与现有"fetch_page / fetch_list 各自 new_page"的
        调用约定一致。stealth 装入 context 后会修复 navigator.webdriver / UA Brands /
        chrome.runtime / plugins / WebGL 等 JS 层指纹位;HTTP 层(sec-ch-ua 与 UA 版本
        对齐)由 start() 选择的 channel + 这里的 user_agent 共同负责。

        ``fingerprint`` / ``proxy`` 缺省时用 manager 默认（来自 config 顶层）；
        调用方可按 Page / WatchTarget 字段覆盖。
        """
        eff_fingerprint = fingerprint or self._fingerprint
        # context 层 proxy：仅对 HTTP 代理生效（SOCKS5 已在 start() 通过 CLI 注入）；
        # 重复设置会让 Playwright 报「proxy already configured」。
        kwargs: dict[str, Any] = eff_fingerprint.to_context_kwargs(
            fallback_ua=_default_user_agent(self._chromium_major)
        )
        if proxy is not None and not _proxy_is_socks5(proxy):
            kwargs["proxy"] = _playwright_proxy_dict(proxy)
        ctx = await self._browser.new_context(  # type: ignore[union-attr]
            **kwargs
        )
        if self._stealth is not None:
            await self._stealth.apply_stealth_async(ctx)
        return ctx

    async def fetch_page(self, page: Page) -> PageResult:
        """一次导航后提取页面下全部元素；页面级失败返回 PageLoadError。"""
        if self._browser is None:
            raise RuntimeError("BrowserManager 尚未启动，请先调用 start()")

        try:
            context = await self._new_context(
                fingerprint=page.fingerprint, proxy=page.proxy
            )
        except PlaywrightError as e:
            return PageLoadError(reason=f"创建浏览器上下文失败：{_first_line(str(e))}")

        try:
            try:
                pw_page = await context.new_page()
            except PlaywrightError as e:
                return PageLoadError(reason=f"创建页面失败：{_first_line(str(e))}")

            for attempt in range(len(_NAV_RETRY_BACKOFF_SECS) + 1):
                try:
                    await pw_page.goto(
                        page.url,
                        wait_until=cast(_WaitUntil, page.wait_until),
                        timeout=page.nav_timeout_secs * 1000,
                    )
                    break
                except PlaywrightError as e:
                    reason = _first_line(str(e))
                    if attempt < len(_NAV_RETRY_BACKOFF_SECS) and _is_transient_nav_error(reason):
                        backoff = _NAV_RETRY_BACKOFF_SECS[attempt]
                        logger.info(
                            "页面 %s 导航瞬时失败（第 %d 次），%.0fs 后重试：%s",
                            page.identity,
                            attempt + 1,
                            backoff,
                            reason,
                        )
                        await asyncio.sleep(backoff)
                        continue
                    return PageLoadError(reason=f"导航失败：{reason}")

            # 反爬挑战页（Cloudflare IUAM / Turnstile 等）放行通常 5–18s；stealth + channel
            # 已注入但首次 goto 时页面已落地,CF 还在跑 JS 挑战,这里等它通过再提取,
            # 避免每次 poll 都立刻判「选择器未匹配」并累积失败告警。卡住时直接归为页面级
            # 失败,不再走 element 提取(页面还停在挑战页,等 nav_timeout_secs=30s 也只是
            # 白等),reason 标明「反爬挑战页未放行」便于和导航/选择器失败区分诊断。
            if not await _wait_for_challenge_clear(pw_page, page.identity):
                logger.warning(
                    "页面 %s 仍卡在反爬挑战页,按失败处理",
                    page.identity,
                )
                return PageLoadError(reason="反爬挑战页未在预算时间内放行")

            results: list[tuple[MonitoredElement, FetchResult]] = []
            for element in page.elements:
                try:
                    outcome = await extract_text(pw_page, element, page.nav_timeout_secs * 1000)
                except PlaywrightError as e:
                    results.append((element, FetchError(reason=f"提取失败：{_first_line(str(e))}")))
                    continue
                # extract_text 直接给出诊断 reason（JS 模式含挑战页 title 等上下文信息）；
                # 调度层与控制面一律原样转达,不再各自硬编码一句「未匹配到元素」。
                if outcome.value is None:
                    results.append((element, FetchNoMatch(reason=outcome.reason or "提取失败")))
                elif not outcome.value:
                    results.append((element, FetchNoMatch(reason=outcome.reason or "值为空")))
                else:
                    results.append((element, FetchOk(value=outcome.value)))
            return PageFetched(results=tuple(results))
        finally:
            # 关停竞态：驱动子进程可能已随进程组信号先行退出（Ctrl+C 的 SIGINT 传遍
            # 整个进程组），此时 context.close() 会因 target 已消失抛 TargetClosedError；
            # 传输层断连时也可能抛基类 Exception。清理是尽力操作，吞掉以免正常返回被
            # finally 的清理异常覆盖、被调度层误记为“轮询未预期异常”。同 close()：捕获
            # Exception；KeyboardInterrupt/CancelledError 属 BaseException，不会被误吞。
            try:
                await context.close()
            except Exception as e:
                logger.warning(
                    "关闭浏览器上下文时忽略异常（驱动可能已退出）：%s", _first_line(str(e))
                )

    async def fetch_list(self, watch: WatchTarget) -> ListResult:
        """一次导航加载列表页并返回提取到的列表项；页面级失败返回 PageLoadError。

        镜像 :meth:`fetch_page` 的 context 创建、UA/viewport、导航瞬时错误重试与
        ``finally`` 关闭 context；导航成功后调 :func:`extract_list_items`。提取过程中
        的 Playwright 异常归为页面级失败（R12），空列表则原样返回交由调度层判定。
        """
        if self._browser is None:
            raise RuntimeError("BrowserManager 尚未启动，请先调用 start()")

        try:
            context = await self._new_context(
                fingerprint=watch.fingerprint, proxy=watch.proxy
            )
        except PlaywrightError as e:
            return PageLoadError(reason=f"创建浏览器上下文失败：{_first_line(str(e))}")

        try:
            try:
                pw_page = await context.new_page()
            except PlaywrightError as e:
                return PageLoadError(reason=f"创建页面失败：{_first_line(str(e))}")

            for attempt in range(len(_NAV_RETRY_BACKOFF_SECS) + 1):
                try:
                    await pw_page.goto(
                        watch.url,
                        wait_until=cast(_WaitUntil, watch.wait_until),
                        timeout=watch.nav_timeout_secs * 1000,
                    )
                    break
                except PlaywrightError as e:
                    reason = _first_line(str(e))
                    if attempt < len(_NAV_RETRY_BACKOFF_SECS) and _is_transient_nav_error(reason):
                        backoff = _NAV_RETRY_BACKOFF_SECS[attempt]
                        logger.info(
                            "列表 %s 导航瞬时失败（第 %d 次），%.0fs 后重试：%s",
                            watch.identity,
                            attempt + 1,
                            backoff,
                            reason,
                        )
                        await asyncio.sleep(backoff)
                        continue
                    return PageLoadError(reason=f"导航失败：{reason}")

            # 反爬挑战页等待：与 fetch_page 同源——列表 / 论坛页同样可能撞 CF IUAM,
            # 在提取链接元素前等挑战页自愈,避免空列表 / 列表项未挂载导致连续失败告警。
            # 卡住时直接归为页面级失败,reason 标明「反爬挑战页未放行」便于区分诊断。
            if not await _wait_for_challenge_clear(pw_page, watch.identity):
                logger.warning(
                    "列表 %s 仍卡在反爬挑战页,按失败处理",
                    watch.identity,
                )
                return PageLoadError(reason="反爬挑战页未在预算时间内放行")

            try:
                items = await extract_list_items(pw_page, watch, watch.nav_timeout_secs * 1000)
            except PlaywrightError as e:
                return PageLoadError(reason=f"列表项提取失败：{_first_line(str(e))}")
            return ListFetched(items=tuple(items))
        finally:
            try:
                await context.close()
            except Exception as e:
                logger.warning(
                    "关闭浏览器上下文时忽略异常（驱动可能已退出）：%s", _first_line(str(e))
                )
