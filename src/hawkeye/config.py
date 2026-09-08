"""配置加载与校验。

从 TOML 读取全局默认值、Telegram 凭据与「商家 → 页面 → 监控元素」三层
结构，做严格校验后返回不可变的 :class:`Config`。上层默认值（全局 → 商家 →
页面）向下级联，减少重复配置；同一页面的多个元素共享一次页面加载。任何非法
配置都会抛出 :class:`ConfigError`，由启动流程捕获并给出可读错误（fail-closed）。
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# ---- 允许的取值集合 ----
VALID_SELECTOR_TYPES = frozenset({"auto", "css", "xpath"})
VALID_WAIT_UNTIL = frozenset({"load", "domcontentloaded", "networkidle", "commit"})
VALID_COLOR_SCHEME = frozenset({"light", "dark", "no-preference", "null"})
# 主流 IANA 时区 ID；运行时校验：缺省时用系统时区。
_VALID_TIMEZONE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-/]{0,40}$")

# ---- 全局默认值 ----
_DEFAULT_POLL_INTERVAL = 60
_DEFAULT_FAILURE_THRESHOLD = 3
_DEFAULT_STATE_PATH = "state.json"
_DEFAULT_MAX_CONCURRENT = 4
_DEFAULT_NAV_TIMEOUT = 30
_DEFAULT_WAIT_UNTIL = "domcontentloaded"
_DEFAULT_SELECTOR_TYPE = "auto"
_DEFAULT_VIEWPORT: dict[str, int] = {"width": 1366, "height": 768}


class ConfigError(Exception):
    """配置加载或校验失败。"""


@dataclass(frozen=True)
class TelegramConfig:
    """Telegram 机器人凭据。"""

    bot_token: str
    chat_id: str


@dataclass(frozen=True)
class ProxyConfig:
    """HTTP/SOCKS5 代理配置。

    与 Playwright ``browser.new_context(proxy=...)`` 同形：``server`` 必填，
    其余字段可选。Chromix 风格：把字符串简写（``"http://user:pass@host:port"``）
    解析为结构化字段后再交给 Playwright，避免把密码塞进 Chromium CLI 形参。
    """

    server: str
    username: str | None = None
    password: str | None = None
    bypass: str | None = None


@dataclass(frozen=True)
class Fingerprint:
    """浏览器环境维度：UA / locale / timezone / color_scheme / viewport。

    这些字段对应 HTTP 头 / Client Hints / ``navigator.languages`` / ``Intl.DateTimeFormat``
    等可观测值：站点反爬常按其是否一致判定 headless 流量（Chromix 在 ``--fingerprint-*``
    一族开关里管的事）。HawkEye 不像 Chromix 那样要管到 canvas/WebGL/字体（只用
    Playwright stealth 包的 JS 补丁），但 locale/timezone/color_scheme 三件套仍要走
    Playwright context 形参——否则 ``Accept-Language`` 与 ``navigator.language`` 错位
    一样会被识别为代理/脚本流量。
    """

    user_agent: str | None = None
    locale: str | None = None
    timezone_id: str | None = None
    color_scheme: str | None = None
    # 存为 ``{"width": int, "height": int}`` 而非 tuple——Playwright context 形参本身就
    # 接受这种 dict，省一次翻译；frozen=True 下 dict 是 mutable 但 dataclass 不会写入。
    viewport: dict[str, int] | None = None

    def to_context_kwargs(self, fallback_ua: str) -> dict[str, Any]:
        """翻译成 Playwright ``browser.new_context`` 的 kwargs。

        ``viewport`` 用 dict 形式以兼容 Playwright 类型签名；``color_scheme`` 未配时
        传 ``"light"``——stealth 默认对暗色站点伪装差，留空时维持 None 让站点真实探测。
        ``user_agent`` 缺省时回落到 ``fallback_ua``（运行期拼的「与 Chromium 主版本对齐」
        UA），保证 sec-ch-ua / User-Agent 不脱节。
        """
        kwargs: dict[str, Any] = {
            "user_agent": self.user_agent or fallback_ua,
        }
        if self.locale:
            kwargs["locale"] = self.locale
        if self.timezone_id:
            kwargs["timezone_id"] = self.timezone_id
        if self.color_scheme:
            kwargs["color_scheme"] = self.color_scheme
        if self.viewport:
            kwargs["viewport"] = dict(self.viewport)
        return kwargs


@dataclass(frozen=True)
class MonitoredElement:
    """页面内的单个监控元素（已完成默认值回填）。"""

    merchant_name: str
    page_name: str
    name: str
    selector: str
    selector_type: str
    nth: int | None
    # 非空时取代 inner_text：对元素执行 JS 表达式，字符串返回值作为状态值。
    js: str | None = None
    # 可选跳转 URL：缺省时调度层沿用所属 page.url；用于 Telegram 通知附上可点链接，
    # 让用户从消息直跳到目标下单/详情页。
    url: str | None = None

    @property
    def identity(self) -> str:
        """状态文件中的稳定标识：商家 / 页面 / 元素。"""
        return f"{self.merchant_name} / {self.page_name} / {self.name}"

    @property
    def effective_selector_type(self) -> str:
        """把 auto 归一化为具体的 css / xpath。"""
        if self.selector_type == "auto":
            return detect_selector_type(self.selector)
        return self.selector_type


@dataclass(frozen=True)
class Page:
    """单个商品页面：一次导航即提取其下全部元素。"""

    merchant_name: str
    name: str
    url: str
    poll_interval_secs: int
    wait_until: str
    nav_timeout_secs: int
    failure_threshold: int
    elements: tuple[MonitoredElement, ...]
    # fingerprint / proxy 已在解析时按「全局 → 商家」回填：避免运行时再去 config 里
    # 查上游层级，与其他向下级联字段口径一致。两者都给默认值（空 fingerprint + 无代理）
    # 让测试代码可直接构造 Page() 而无需每个字段都写。
    fingerprint: Fingerprint = Fingerprint()
    proxy: ProxyConfig | None = None

    @property
    def identity(self) -> str:
        """页面级稳定标识：商家 / 页面。"""
        return f"{self.merchant_name} / {self.name}"


@dataclass(frozen=True)
class Merchant:
    """商家：一组商品页面的集合。"""

    name: str
    pages: tuple[Page, ...]
    fingerprint: Fingerprint = Fingerprint()
    proxy: ProxyConfig | None = None


@dataclass(frozen=True)
class WatchTarget:
    """列表新条目监控目标（已完成默认值回填）。

    与「商家 → 页面 → 元素」三层结构平行的顶层监控对象：每轮抓一次列表页，
    用 ``link_selector`` 定位每个帖子的标题链接，对照已见 ID 集合发现新帖。
    """

    name: str
    url: str
    link_selector: str
    selector_type: str
    keywords: tuple[str, ...]
    id_pattern: str | None
    poll_interval_secs: int
    wait_until: str
    nav_timeout_secs: int
    failure_threshold: int
    fingerprint: Fingerprint = Fingerprint()
    proxy: ProxyConfig | None = None

    @property
    def identity(self) -> str:
        """状态文件中的稳定标识：watch / 名称（与元素命名空间不重叠）。"""
        return f"watch / {self.name}"

    @property
    def effective_selector_type(self) -> str:
        """把 auto 归一化为具体的 css / xpath（依据标题链接选择器写法）。"""
        if self.selector_type == "auto":
            return detect_selector_type(self.link_selector)
        return self.selector_type


@dataclass(frozen=True)
class Config:
    """完整配置。"""

    telegram: TelegramConfig
    merchants: tuple[Merchant, ...]
    poll_interval_secs: int
    failure_threshold: int
    state_path: str
    max_concurrent_fetches: int
    nav_timeout_secs: int
    wait_until: str
    watches: tuple[WatchTarget, ...] = ()
    fingerprint: Fingerprint = Fingerprint()
    proxy: ProxyConfig | None = None

    @property
    def pages(self) -> tuple[Page, ...]:
        """扁平化所有页面，供调度层按页建任务。"""
        return tuple(p for m in self.merchants for p in m.pages)

    @property
    def element_count(self) -> int:
        """监控元素总数。"""
        return sum(len(p.elements) for p in self.pages)


@dataclass(frozen=True)
class _Defaults:
    """可向下级联的默认值集合（全局 → 商家 → 页面）。"""

    poll_interval_secs: int
    wait_until: str
    nav_timeout_secs: int
    failure_threshold: int
    selector_type: str


def detect_selector_type(selector: str) -> str:
    """根据选择器写法推断类型：以 // ( / . 或 xpath= 开头视为 XPath，否则 CSS。"""
    s = selector.strip()
    if s.startswith("css="):
        return "css"
    if s.startswith(("//", "(", "/", "./", "..", "xpath=")):
        return "xpath"
    return "css"


# ---- 内部取值/校验辅助 ----


def _require_str(d: Mapping[str, object], key: str, ctx: str) -> str:
    if key not in d:
        raise ConfigError(f"{ctx}缺少必填字段 “{key}”")
    v = d[key]
    if not isinstance(v, str) or not v.strip():
        raise ConfigError(f"{ctx}字段 “{key}” 必须为非空字符串")
    return v


def _opt_str(d: Mapping[str, object], key: str, default: str, ctx: str) -> str:
    if key not in d:
        return default
    v = d[key]
    if not isinstance(v, str):
        raise ConfigError(f"{ctx}字段 “{key}” 必须为字符串")
    return v


def _opt_pos_int(d: Mapping[str, object], key: str, default: int, ctx: str) -> int:
    if key not in d:
        return default
    v = d[key]
    if isinstance(v, bool) or not isinstance(v, int):
        raise ConfigError(f"{ctx}字段 “{key}” 必须为整数")
    if v <= 0:
        raise ConfigError(f"{ctx}字段 “{key}” 必须为正整数，当前为 {v}")
    return v


def _opt_nth(d: Mapping[str, object], ctx: str) -> int | None:
    if "nth" not in d:
        return None
    v = d["nth"]
    if isinstance(v, bool) or not isinstance(v, int):
        raise ConfigError(f"{ctx}字段 “nth” 必须为非负整数")
    if v < 0:
        raise ConfigError(f"{ctx}字段 “nth” 必须为非负整数，当前为 {v}")
    return v


def _opt_js(d: Mapping[str, object], ctx: str) -> str | None:
    """可选 JS 表达式：键缺省返回 None；非字符串报错。空字符串也允许（与 _opt_str 不同，
    JS 表达式可能返回空串语义,空白校验留给运行时）。"""
    if "js" not in d:
        return None
    v = d["js"]
    if not isinstance(v, str):
        raise ConfigError(f"{ctx}字段 “js” 必须为字符串")
    return v


def _opt_url(d: Mapping[str, object], ctx: str) -> str | None:
    """可选跳转 URL：键缺省返回 None；非空时校验 http(s)（与 _validate_url 同一口径）。

    元素级 url 缺省时由调度层沿用所属 page.url 推送通知——此函数不强制非空,只做合法性
    兜底,避免在配置层就拒绝「不填」这种合法写法。
    """
    if "url" not in d:
        return None
    v = d["url"]
    if not isinstance(v, str) or not v.strip():
        raise ConfigError(f"{ctx}字段 “url” 必须为非空字符串")
    url = v.strip()
    if not is_http_url(url):
        raise ConfigError(f"{ctx}的 url 不是合法的 http(s) 地址：{url}")
    return url


def is_http_url(url: str) -> bool:
    """谓词版 URL 校验：与 _validate_url 同一口径，供控制面先验过滤。"""
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _validate_url(url: str, ctx: str) -> None:
    if not is_http_url(url):
        raise ConfigError(f"{ctx}的 url 不是合法的 http(s) 地址：{url}")


# ---- Fingerprint / Proxy 解析（按 Chromix 风格集中翻译，避免在 fetch.py 内散开）----


def _parse_viewport(v: object, ctx: str) -> dict[str, int]:
    """viewport 必须为 ``{ width = 整数, height = 整数 }``，否则抛 ConfigError。"""
    if not isinstance(v, dict):
        raise ConfigError(f"{ctx}字段 “viewport” 必须为表（table）")
    raw_w = v.get("width")
    raw_h = v.get("height")
    if not isinstance(raw_w, int) or isinstance(raw_w, bool) or raw_w <= 0:
        raise ConfigError(f"{ctx}字段 “viewport.width” 必须为正整数")
    if not isinstance(raw_h, int) or isinstance(raw_h, bool) or raw_h <= 0:
        raise ConfigError(f"{ctx}字段 “viewport.height” 必须为正整数")
    return {"width": raw_w, "height": raw_h}


def _parse_fingerprint(d: Mapping[str, object] | None, base: Fingerprint, ctx: str) -> Fingerprint:
    """解析 ``[fingerprint]`` 子表，与 ``base``（上层默认值）做字段级覆盖。"""
    if d is None:
        return base
    if not isinstance(d, dict):
        raise ConfigError(f"{ctx}字段 “fingerprint” 必须为表（table）")

    user_agent = _opt_str(d, "user_agent", base.user_agent or "", ctx) or None
    locale = _opt_str(d, "locale", base.locale or "", ctx) or None
    tz = _opt_str(d, "timezone_id", base.timezone_id or "", ctx) or None
    if tz and not _VALID_TIMEZONE_ID.match(tz):
        raise ConfigError(
            f"{ctx}字段 “fingerprint.timezone_id” 不是合法的 IANA 时区 ID：{tz}"
        )

    color = _opt_str(d, "color_scheme", base.color_scheme or "", ctx) or None
    if color and color not in VALID_COLOR_SCHEME:
        opts = "、".join(sorted(c for c in VALID_COLOR_SCHEME if c != "null"))
        raise ConfigError(
            f"{ctx}字段 “fingerprint.color_scheme” 取值非法：{color}（可选：{opts}、null）"
        )

    if "viewport" in d:
        viewport: dict[str, int] | None = _parse_viewport(d["viewport"], ctx)
    else:
        viewport = base.viewport

    return Fingerprint(
        user_agent=user_agent,
        locale=locale,
        timezone_id=tz,
        color_scheme=color,
        viewport=viewport,
    )


def _parse_proxy(d: object, ctx: str) -> ProxyConfig | None:
    """``[proxy]`` 子表为 ``None`` 时视作「不使用代理」；字符串简写与结构化字段都允许。

    Chromix 风格：字符串 ``http://user:pass@host:port`` 先解析成结构化字段，再交给
    Playwright——避免把密码塞进 Chromium CLI 形参（``ps`` 里可见）。Chromium 本身对
    SOCKS 代理需要 ``--proxy-server`` CLI 形参（Playwright 的 proxy 字段只对 HTTP 代理
    完整生效），这一层差异在 fetch.py 内做翻译。
    """
    if d is None:
        return None
    if isinstance(d, str):
        url = d.strip()
        if not url:
            return None
        if not is_http_url(url) and not url.startswith("socks5://"):
            raise ConfigError(f"{ctx}字段 “proxy” 字符串必须为 http(s):// 或 socks5://")
        return _parse_proxy_string(url, ctx)
    if not isinstance(d, dict):
        raise ConfigError(f"{ctx}字段 “proxy” 必须为字符串或表")
    server = _require_str(d, "server", ctx)
    if not (is_http_url(server) or server.startswith("socks5://")):
        raise ConfigError(f"{ctx}字段 “proxy.server” 必须为 http(s):// 或 socks5://")
    username = _opt_str(d, "username", "", ctx) or None
    password = _opt_str(d, "password", "", ctx) or None
    bypass = _opt_str(d, "bypass", "", ctx) or None
    return ProxyConfig(
        server=server,
        username=username,
        password=password,
        bypass=bypass,
    )


def _parse_proxy_string(url: str, ctx: str) -> ProxyConfig:
    """拆 ``http://user:pass@host:port`` 字符串为结构化字段。

    仅做语法层解析——``urllib.parse`` 在 userinfo 段允许未编码的特殊字符，Chromix
    也是同样的「先解析再用 Playwright proxy 字段转发」做法。我们只在明显错误时抛
    ConfigError，其他情况交给运行时 Playwright 报错。
    """
    from urllib.parse import urlparse as _urlparse

    parsed = _urlparse(url)
    return ProxyConfig(
        server=f"{parsed.scheme}://{parsed.hostname}"
        + (f":{parsed.port}" if parsed.port else ""),
        username=_unquote(parsed.username),
        password=_unquote(parsed.password),
        bypass=None,
    )


def _unquote(v: str | None) -> str | None:
    if v is None:
        return None
    from urllib.parse import unquote

    return unquote(v) or None


def _as_mapping(v: object) -> Mapping[str, object] | None:
    """把 ``raw.get('xxx')`` 的 ``object`` 安全收窄为 ``Mapping | None``。

    TOML 解析后 ``raw`` 是 ``dict[str, Any]``，但 ``dict.get`` 返回 ``Any``——直接传给
    ``Mapping[str, object]`` 形参会过不了 mypy 严格模式。这层收窄把非 dict（list /
    标量）都规范化为 None，让 _parse_fingerprint / _parse_proxy 自己抛 ConfigError。
    """
    if v is None:
        return None
    if isinstance(v, dict):
        return v
    return None


def _check_enum(value: str, allowed: frozenset[str], field: str, ctx: str) -> None:
    if value not in allowed:
        opts = "、".join(sorted(allowed))
        raise ConfigError(f"{ctx}字段 {field} 取值非法：{value}（可选：{opts}）")


def _override_defaults(d: Mapping[str, object], base: _Defaults, ctx: str) -> _Defaults:
    """读取本层可选覆盖项，未写则沿用上层默认值。"""
    poll = _opt_pos_int(d, "poll_interval_secs", base.poll_interval_secs, ctx)
    nav = _opt_pos_int(d, "nav_timeout_secs", base.nav_timeout_secs, ctx)
    failure = _opt_pos_int(d, "failure_threshold", base.failure_threshold, ctx)

    wait_until = _opt_str(d, "wait_until", base.wait_until, ctx)
    _check_enum(wait_until, VALID_WAIT_UNTIL, "wait_until", ctx)

    selector_type = _opt_str(d, "selector_type", base.selector_type, ctx)
    _check_enum(selector_type, VALID_SELECTOR_TYPES, "selector_type", ctx)

    return _Defaults(
        poll_interval_secs=poll,
        wait_until=wait_until,
        nav_timeout_secs=nav,
        failure_threshold=failure,
        selector_type=selector_type,
    )


def _parse_telegram(raw: Mapping[str, object]) -> TelegramConfig:
    tg = raw.get("telegram")
    if not isinstance(tg, dict):
        raise ConfigError("缺少 [telegram] 配置段")
    bot_token = _require_str(tg, "bot_token", "[telegram] ")
    chat_id = _parse_chat_id(tg)
    return TelegramConfig(bot_token=bot_token, chat_id=chat_id)


def _parse_chat_id(tg: Mapping[str, object]) -> str:
    if "chat_id" not in tg:
        raise ConfigError("[telegram] 缺少必填字段 “chat_id”")
    v = tg["chat_id"]
    if isinstance(v, bool):
        raise ConfigError("[telegram] 字段 “chat_id” 必须为非空字符串或整数")
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str) and v.strip():
        return v
    raise ConfigError("[telegram] 字段 “chat_id” 必须为非空字符串或整数")


def _parse_element(
    e: Mapping[str, object],
    ctx: str,
    *,
    merchant_name: str,
    page_name: str,
    defaults: _Defaults,
) -> MonitoredElement:
    selector = _require_str(e, "selector", ctx)
    nth = _opt_nth(e, ctx)
    js = _opt_js(e, ctx)
    jump_url = _opt_url(e, ctx)
    # name 缺省回退：带上 nth 后缀，避免同页面同选择器不同 nth 的元素标识撞车。
    default_name = selector if nth is None else f"{selector}#{nth}"
    name = _opt_str(e, "name", "", ctx) or default_name

    selector_type = _opt_str(e, "selector_type", defaults.selector_type, ctx)
    _check_enum(selector_type, VALID_SELECTOR_TYPES, "selector_type", ctx)

    return MonitoredElement(
        merchant_name=merchant_name,
        page_name=page_name,
        name=name,
        selector=selector,
        selector_type=selector_type,
        nth=nth,
        js=js,
        url=jump_url,
    )


def _parse_page(
    p: Mapping[str, object],
    ctx: str,
    *,
    merchant_name: str,
    defaults: _Defaults,
    seen_elements: set[str],
    fingerprint: Fingerprint,
    proxy: ProxyConfig | None,
) -> Page:
    url = _require_str(p, "url", ctx)
    _validate_url(url, ctx)
    name = _opt_str(p, "name", "", ctx) or url
    page_defaults = _override_defaults(p, defaults, ctx)

    elements_raw = p.get("elements")
    if not isinstance(elements_raw, list) or not elements_raw:
        raise ConfigError(f"{ctx}至少需要配置一个 [[merchants.pages.elements]]")

    elements: list[MonitoredElement] = []
    for i, e in enumerate(elements_raw):
        if not isinstance(e, dict):
            raise ConfigError(f"{ctx}第 {i + 1} 个 element 格式错误，应为表（table）")
        element = _parse_element(
            e,
            f"{ctx}element #{i + 1} ",
            merchant_name=merchant_name,
            page_name=name,
            defaults=page_defaults,
        )
        if element.identity in seen_elements:
            raise ConfigError(f"监控元素标识重复：{element.identity}（同页面内 name 需唯一）")
        seen_elements.add(element.identity)
        elements.append(element)

    return Page(
        merchant_name=merchant_name,
        name=name,
        url=url,
        poll_interval_secs=page_defaults.poll_interval_secs,
        wait_until=page_defaults.wait_until,
        nav_timeout_secs=page_defaults.nav_timeout_secs,
        failure_threshold=page_defaults.failure_threshold,
        elements=tuple(elements),
        fingerprint=fingerprint,
        proxy=proxy,
    )


def _parse_merchant(
    m: Mapping[str, object],
    ctx: str,
    *,
    defaults: _Defaults,
    seen_pages: set[str],
    seen_elements: set[str],
    base_fingerprint: Fingerprint,
    base_proxy: ProxyConfig | None,
) -> Merchant:
    name = _require_str(m, "name", ctx)
    merchant_defaults = _override_defaults(m, defaults, ctx)
    merchant_fingerprint = _parse_fingerprint(
        _as_mapping(m.get("fingerprint")), base_fingerprint, f"{ctx}fingerprint "
    )
    merchant_proxy = _parse_proxy(m.get("proxy"), f"{ctx}proxy ")

    pages_raw = m.get("pages")
    if not isinstance(pages_raw, list) or not pages_raw:
        raise ConfigError(f"{ctx}至少需要配置一个 [[merchants.pages]]")

    pages: list[Page] = []
    for i, p in enumerate(pages_raw):
        if not isinstance(p, dict):
            raise ConfigError(f"{ctx}第 {i + 1} 个 page 格式错误，应为表（table）")
        page = _parse_page(
            p,
            f"{ctx}page #{i + 1} ",
            merchant_name=name,
            defaults=merchant_defaults,
            seen_elements=seen_elements,
            fingerprint=merchant_fingerprint,
            proxy=merchant_proxy,
        )
        if page.identity in seen_pages:
            raise ConfigError(f"页面标识重复：{page.identity}（同商家内 name 需唯一）")
        seen_pages.add(page.identity)
        pages.append(page)

    return Merchant(
        name=name,
        pages=tuple(pages),
        fingerprint=merchant_fingerprint,
        proxy=merchant_proxy,
    )


def _parse_keywords(w: Mapping[str, object], ctx: str) -> tuple[str, ...]:
    raw = w.get("keywords")
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{ctx}字段 “keywords” 必须为非空字符串列表")
    keywords: list[str] = []
    for i, kw in enumerate(raw):
        if not isinstance(kw, str) or not kw.strip():
            raise ConfigError(f"{ctx}字段 “keywords” 第 {i + 1} 项必须为非空字符串")
        keywords.append(kw)
    return tuple(keywords)


def _parse_id_pattern(w: Mapping[str, object], ctx: str) -> str | None:
    if "id_pattern" not in w:
        return None
    v = w["id_pattern"]
    if not isinstance(v, str) or not v.strip():
        raise ConfigError(f"{ctx}字段 “id_pattern” 必须为非空字符串")
    try:
        re.compile(v)
    except re.error as e:
        raise ConfigError(f"{ctx}字段 “id_pattern” 不是合法正则：{e}") from e
    return v


def _parse_watch(
    w: Mapping[str, object],
    ctx: str,
    *,
    defaults: _Defaults,
    base_fingerprint: Fingerprint,
    base_proxy: ProxyConfig | None,
) -> WatchTarget:
    url = _require_str(w, "url", ctx)
    _validate_url(url, ctx)
    name = _opt_str(w, "name", "", ctx) or url
    link_selector = _require_str(w, "link_selector", ctx)
    keywords = _parse_keywords(w, ctx)
    id_pattern = _parse_id_pattern(w, ctx)
    wd = _override_defaults(w, defaults, ctx)
    fingerprint = _parse_fingerprint(
        _as_mapping(w.get("fingerprint")), base_fingerprint, f"{ctx}fingerprint "
    )
    proxy = _parse_proxy(w.get("proxy"), f"{ctx}proxy ")
    return WatchTarget(
        name=name,
        url=url,
        link_selector=link_selector,
        selector_type=wd.selector_type,
        keywords=keywords,
        id_pattern=id_pattern,
        poll_interval_secs=wd.poll_interval_secs,
        wait_until=wd.wait_until,
        nav_timeout_secs=wd.nav_timeout_secs,
        failure_threshold=wd.failure_threshold,
        fingerprint=fingerprint,
        proxy=proxy,
    )


def _parse_merchants(
    raw: Mapping[str, object],
    defaults: _Defaults,
    *,
    base_fingerprint: Fingerprint,
    base_proxy: ProxyConfig | None,
) -> tuple[Merchant, ...]:
    merchants_raw = raw.get("merchants")
    if merchants_raw is None:
        return ()
    if not isinstance(merchants_raw, list):
        raise ConfigError("[[merchants]] 必须为数组")

    merchants: list[Merchant] = []
    seen_merchants: set[str] = set()
    seen_pages: set[str] = set()
    seen_elements: set[str] = set()
    for i, m in enumerate(merchants_raw):
        if not isinstance(m, dict):
            raise ConfigError(f"第 {i + 1} 个 merchant 格式错误，应为表（table）")
        merchant = _parse_merchant(
            m,
            f"merchant #{i + 1} ",
            defaults=defaults,
            seen_pages=seen_pages,
            seen_elements=seen_elements,
            base_fingerprint=base_fingerprint,
            base_proxy=base_proxy,
        )
        if merchant.name in seen_merchants:
            raise ConfigError(f"商家名称重复：{merchant.name}（name 需唯一）")
        seen_merchants.add(merchant.name)
        merchants.append(merchant)
    return tuple(merchants)


def _parse_watches(
    raw: Mapping[str, object],
    defaults: _Defaults,
    *,
    base_fingerprint: Fingerprint,
    base_proxy: ProxyConfig | None,
) -> tuple[WatchTarget, ...]:
    watches_raw = raw.get("watches")
    if watches_raw is None:
        return ()
    if not isinstance(watches_raw, list):
        raise ConfigError("[[watches]] 必须为数组")

    watches: list[WatchTarget] = []
    seen: set[str] = set()
    for i, w in enumerate(watches_raw):
        if not isinstance(w, dict):
            raise ConfigError(f"第 {i + 1} 个 watch 格式错误，应为表（table）")
        watch = _parse_watch(
            w,
            f"watch #{i + 1} ",
            defaults=defaults,
            base_fingerprint=base_fingerprint,
            base_proxy=base_proxy,
        )
        if watch.identity in seen:
            raise ConfigError(f"监控目标标识重复：{watch.identity}（name 需唯一）")
        seen.add(watch.identity)
        watches.append(watch)
    return tuple(watches)


def load_raw(path: str | Path) -> dict[str, Any]:
    """读取 TOML 文件为未解析的 raw dict。

    只负责「读文件 + 语法解析」，把 :class:`FileNotFoundError` 与
    :class:`tomllib.TOMLDecodeError` 统一包装成 :class:`ConfigError`。返回的 raw
    dict 既供 :func:`parse_config` 校验，也供写路径（configedit）在其上做最小结构
    改动后原样写回 —— 绝不回写已级联展开的 :class:`Config`（KTD8）。
    """
    p = Path(path)
    try:
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"配置文件不存在：{p}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"配置文件 TOML 解析失败：{e}") from e


def parse_config(raw: dict[str, Any]) -> Config:
    """从已解析的 raw dict 校验并构造 :class:`Config`（不触碰文件系统）。

    「零监控」是允许的：既无 [[merchants]] 也无 [[watches]] 时返回一个
    ``pages == ()``、``watches == ()`` 的合法 Config（KTD13），让「删掉最后一个
    监控」不至于失败；启动时是否为空由 ``__main__`` 以 warning 提示。``[telegram]``
    的校验一律不放宽。
    """
    g_max = _opt_pos_int(raw, "max_concurrent_fetches", _DEFAULT_MAX_CONCURRENT, "全局配置")
    g_state = _opt_str(raw, "state_path", _DEFAULT_STATE_PATH, "全局配置")
    base = _override_defaults(
        raw,
        _Defaults(
            poll_interval_secs=_DEFAULT_POLL_INTERVAL,
            wait_until=_DEFAULT_WAIT_UNTIL,
            nav_timeout_secs=_DEFAULT_NAV_TIMEOUT,
            failure_threshold=_DEFAULT_FAILURE_THRESHOLD,
            selector_type=_DEFAULT_SELECTOR_TYPE,
        ),
        "全局配置",
    )

    telegram = _parse_telegram(raw)

    # 全局 fingerprint / proxy：默认从空 Fingerprint 出发；用户写 [fingerprint] /
    # [proxy] 子表即覆盖。merchant / page / watch 沿此 base 链回填（见
    # _parse_merchant、_parse_watch）。
    base_fingerprint = _parse_fingerprint(
        _as_mapping(raw.get("fingerprint")), Fingerprint(), "全局配置 fingerprint "
    )
    base_proxy = _parse_proxy(raw.get("proxy"), "全局配置 proxy ")

    merchants = _parse_merchants(
        raw,
        base,
        base_fingerprint=base_fingerprint,
        base_proxy=base_proxy,
    )
    watches = _parse_watches(
        raw,
        base,
        base_fingerprint=base_fingerprint,
        base_proxy=base_proxy,
    )

    return Config(
        telegram=telegram,
        merchants=merchants,
        watches=watches,
        poll_interval_secs=base.poll_interval_secs,
        failure_threshold=base.failure_threshold,
        state_path=g_state,
        max_concurrent_fetches=g_max,
        nav_timeout_secs=base.nav_timeout_secs,
        wait_until=base.wait_until,
        fingerprint=base_fingerprint,
        proxy=base_proxy,
    )


def load_config(path: str | Path) -> Config:
    """从 TOML 文件加载并校验配置。"""
    return parse_config(load_raw(path))
