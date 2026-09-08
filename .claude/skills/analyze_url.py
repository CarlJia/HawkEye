#!/usr/bin/env python3
"""分析任意 URL 的页面结构，为 HawkEye 生成 [[watches]] 或 [[merchants]] 配置片段。

用法:
    python3 .claude/skills/analyze_url.py <url>
    python3 .claude/skills/analyze_url.py <url> --dry-run    # 只打印，不写入

依赖:
    browser-act (已在 PATH)
    python 标准库
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path
from urllib.parse import urlparse

TOML_TEMPLATE = """\
[[{block_type}]]
name = "{name}"
url = "{url}"
link_selector = "{link_selector}"
keywords = [
    {keywords},
]
{id_pattern_line}"""


def run(cmd: list[str], timeout: int = 30) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        print(f"[browser-act 错误] returncode={result.returncode}", file=sys.stderr)
        print(result.stderr[:500], file=sys.stderr)
        raise RuntimeError(f"browser-act failed: {result.stderr[:200]}")
    return result.stdout


def _links_from_raw_html(raw: str, base_url: str) -> tuple[str, str]:
    """从原始 HTML 提取纯文本和 [title](href) 格式的链接列表（不依赖 bs4）。"""
    # 去掉 script / style 块
    text = re.sub(r'<script[^>]*>.*?</script>', '', raw, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    # 纯文本
    plain = re.sub(r'<[^>]+>', ' ', text)
    plain = re.sub(r'\s+', ' ', plain).strip()

    # 提取链接
    link_lines: list[str] = []
    parsed_base = urlparse(base_url)
    for m in re.finditer(r'<a\s[^>]*href="([^"]+)"[^>]*>([^<]*)</a>', raw, re.IGNORECASE):
        href, title = m.group(1).strip(), m.group(2).strip()
        if not title:
            continue
        if len(title) > 60:
            title = title[:60]
        if href.startswith("/"):
            href = f"{parsed_base.scheme}://{parsed_base.netloc}{href}"
        link_lines.append(f"[{title}]({href})")

    links_text = "\n--- LINKS ---\n" + "\n".join(link_lines[:200])
    return plain, links_text


def extract_page(url: str) -> str:
    """按优先级尝试三种抓取方式：browser-act → requests+bs4 → curl+纯Python解析。"""
    # 方式 1: browser-act
    try:
        return run(["browser-act", "stealth-extract", url, "--content-type", "markdown"], timeout=40)
    except Exception as e1:
        print(f"  browser-act 不可用: {e1}", file=sys.stderr)

    # 方式 2: requests + BeautifulSoup（需已安装）
    try:
        import requests
        from bs4 import BeautifulSoup
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}
        resp = requests.get(url, headers=headers, timeout=20)
        resp.encoding = resp.apparent_encoding or "utf-8"
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        link_lines: list[str] = []
        base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            title = a.get_text(strip=True)[:60]
            if not title:
                continue
            if href.startswith("/"):
                href = base + href
            link_lines.append(f"[{title}]({href})")
        links_text = "\n--- LINKS ---\n" + "\n".join(link_lines[:200])
        return text + links_text
    except ImportError:
        print("  requests/BeautifulSoup 未安装，尝试 curl 方式...", file=sys.stderr)
    except Exception as e2:
        print(f"  requests 方式失败: {e2}，尝试 curl...", file=sys.stderr)

    # 方式 3: curl + 纯 Python HTML 解析
    try:
        raw = run(["curl", "-s", "-L", "--max-time", "20", "-A",
                   "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                   url], timeout=25)
        text, links_text = _links_from_raw_html(raw, url)
        return text + links_text
    except Exception as e3:
        print(f"  curl 方式失败: {e3}", file=sys.stderr)

    raise RuntimeError("所有抓取方式均失败，请确认网络正常")


def analyze_forum(content: str, url: str) -> dict | None:
    """检测是否是论坛/列表页，返回配置字典或 None。"""
    # 常见论坛关键词在 markdown 中出现的频率
    indicators = [
        r"回复", r"\d+\s*回复", r"最后回复",
        r"主题", r"帖子", r"topic",
        r"reply", r"热门", r"最新",
    ]
    score = sum(1 for ind in indicators if re.search(ind, content, re.IGNORECASE))

    # 尝试从 markdown/文本中提取链接
    # 同时支持相对路径 /path 和完整 URL https://host/path
    links = re.findall(r'\[([^\]]{2,50})\]\((https?://[^\s"\'<>)]+|[/][^\s"\'<>)]+)\)', content)
    if not links:
        # 备选：提取所有 URL
        links = re.findall(r'\[([^\]]{2,50})\]\(([/][^\s"\'<>)]+)\)', content)
    if not links:
        return None

    # 找最频繁出现的 path 前缀（排除静态资源）
    path_counter: dict[str, list] = {}
    for title, path in links:
        if any(ext in path for ext in [".png", ".jpg", ".css", ".js", ".ico", ".svg"]):
            continue
        # 提取 path 部分（去掉协议和域名）
        if path.startswith("http"):
            parsed_l = urlparse(path)
            path = parsed_l.path
        # 泛化：数字序列和长 ID 替换为占位符
        generalized = re.sub(r'\d+', '#NUM#', path)
        generalized = re.sub(r'[-a-zA-Z0-9]{8,}', '#ID#', generalized)
        path_counter.setdefault(generalized, []).append((title, path))

    # 取最多链接的前缀
    if not path_counter:
        return None
    best_prefix = max(path_counter, key=lambda k: len(path_counter[k]))
    samples = path_counter[best_prefix][:3]

    # 从样本中推断 id_pattern
    sample_paths = [p for _, p in samples]
    id_candidates: list[str] = []

    for p in sample_paths:
        nums = re.findall(r'/(\d+)(?:[/\?#&]|$)', p)
        if nums:
            id_candidates.append(nums[-1])

    # 构造 id_pattern：尝试从 path 中找规律
    id_pattern = ""
    if len(set(id_candidates)) == len(id_candidates) and id_candidates:
        # 简单模式：path 以 /数字 结尾
        id_pattern = "/(\\d+)"
    else:
        # 尝试从 prefix 推断
        if "#NUM#" in best_prefix:
            segs = best_prefix.split("#NUM#")
            if len(segs) == 2:
                prefix_part = segs[0].rstrip("/")
                id_pattern = f"{prefix_part}/(\\d+)"

    # 构造 link_selector（从 prefix 转换）
    # 把 #NUM# 替换为实际匹配模式
    selector_path = best_prefix
    if "#NUM#" in selector_path:
        selector_path = selector_path.replace("#NUM#", "*")

    # 用 XPath 表达
    segments = [s for s in selector_path.split("/") if s and s != "*"]
    xpath_parts = []
    for seg in segments:
        if seg == "*":
            xpath_parts.append("*")
        else:
            xpath_parts.append(f"*[contains(@class,'{seg}')]")

    # 简单策略：匹配所有帖子链接 a 标签
    link_selector = "//a[contains(@href,'/t/')]"

    # 从 URL 推断 link_selector
    parsed = urlparse(url)
    domain = parsed.netloc
    path = parsed.path.rstrip("/")

    # 根据不同论坛定制选择器
    known_sel, known_pat = _generic_forum_selector(url)
    if known_sel:
        link_selector = known_sel
        id_pattern = known_pat
    else:
        # 通用策略：从 best_prefix 构造
        if segments:
            if "#ID#" not in best_prefix and "#NUM#" not in best_prefix:
                # 固定 path，找其下所有链接
                link_selector = f"//a[starts-with(@href,'{best_prefix}')]"
            else:
                link_selector = f"//a[contains(@href,'{path}')]"

    return {
        "block_type": "watches",
        "name": f"自动生成-{domain}",
        "url": url,
        "link_selector": link_selector,
        "keywords": ["CHANGE_ME"],
        "id_pattern": id_pattern if id_pattern else None,
    }


def _generic_forum_selector(url: str) -> tuple[str, str]:
    """根据域名返回已知的 link_selector 和 id_pattern。"""
    domain = urlparse(url).netloc
    known: dict[str, tuple[str, str]] = {
        "v2ex.com":         ("//span[@class='item_title']/a", "/t/(\\d+)"),
        "nodeseek.com":      ('//*[@id="nsk-body-left"]/ul/li/div/div[1]/a', "post-(\\d+)-"),
        "hostloc.com":       ("//td[@class='fl_g']/a", ""),
        "hostloc.cn":        ("//td[@class='fl_g']/a", ""),
        "lowendtalk.com":    ("//a[contains(@href,'/discussion/')]", "/discussion/(\\d+)"),
        "webhorde.net":      ("//a[contains(@href,'/thread/')]", "/thread/(\\d+)"),
        "it保镖.cn":         ("//a[contains(@class,'thread-link')]", ""),
    }
    for d, (sel, pat) in known.items():
        if d in domain:
            return sel, pat
    return "", ""


def _suggest_merchant_elements(content: str) -> list[dict]:
    """从页面内容中提取可能的价格/库存元素，返回选择器候选列表。"""
    suggestions: list[dict] = []
    # 常见 class/id 模式
    patterns = [
        (r'class="([^"]*(?:price|Price| PRICE|cost|cost)[^"]*)"', 'class', '价格/价值'),
        (r'class="([^"]*(?:stock|Stock|库存|available|inventory)[^"]*)"', 'class', '库存状态'),
        (r'class="([^"]*(?:btn|button|add-cart|add-to-cart|buy)[^"]*)"', 'class', '购买按钮'),
        (r'id="([^"]*(?:price|Stock|buy|add-cart|cart)[^"]*)"', 'id', '价格/库存ID'),
        (r'data-(?:sku|product)="([^"]+)"', 'data-attr', '商品标识'),
    ]
    seen = set()
    for pat, attr, desc in patterns:
        for m in re.finditer(pat, content):
            val = m.group(1)
            if val in seen or len(val) < 2:
                continue
            seen.add(val)
            suggestions.append({
                "name": desc,
                "selector": f'@{attr}="{val}"' if attr != "data-attr" else f'data-sku="{val}"',
                "hint": f"class={val}" if attr == "class" else val,
            })
    return suggestions[:5]


def analyze_merchant(content: str, url: str) -> dict | None:
    """检测是否是商品/产品页，返回配置字典（带 merchants 模板）或 None。"""
    indicators = [
        r"价格", r"库存", r"立即购买", r"加入购物车",
        r"price", r"stock", r"in stock", r"out of stock",
        r"add to cart", r"buy now", r"¥[\d,]", r"\$[\d,]+",
        r"已售", r"剩余", r"限购", r"月付", r"年付",
    ]
    score = sum(1 for ind in indicators if re.search(ind, content, re.IGNORECASE))
    if score < 2:
        return None

    parsed = urlparse(url)
    suggestions = _suggest_merchant_elements(content)

    return {
        "block_type": "merchants",
        "name": f"自动生成-{parsed.netloc}",
        "url": url,
        "page_name": f"{parsed.netloc} 商品页",
        "elements": suggestions,
    }


MERCHANTS_TEMPLATE = """\
[[merchants]]
name = "{merchant_name}"

[[merchants.pages]]
name = "{page_name}"
url = "{url}"

# 从以下候选中选择或补充需要的元素选择器
# 选择器格式：nth 为可选（从 0 开始，取第 N 个匹配），留空则取第 0 个
{elements}"""


def build_toml(block: dict) -> str:
    """把分析结果字典渲染为 TOML 片段。"""
    if block["block_type"] == "watches":
        id_val = block.get("id_pattern", "")
        id_line = f'id_pattern = "{id_val}"' if id_val else "# id_pattern = \"/t/(\\d+)\"  # 可选"
        keywords_line = '"CHANGE_ME"  # ← 替换为你的关键字'
        return TOML_TEMPLATE.format(
            block_type="watches",
            name=block["name"],
            url=block["url"],
            link_selector=block["link_selector"],
            keywords=keywords_line,
            id_pattern_line=id_line,
        )
    elif block["block_type"] == "merchants":
        el_lines: list[str] = []
        for el in block.get("elements", []):
            el_lines.append(
                f'# {el["name"]}（建议选择器：@{el["selector"]}）\n'
                f'# {{ name = "CHANGE_ME", selector = "//*[@{el['selector']}]" }}'
            )
        if not el_lines:
            el_lines = [
                "# 请在浏览器开发者工具中定位要监控的元素，\n"
                "# 复制其 XPath 或 CSS 选择器，填入下方：\n"
                "# { name = \"商品名称\", selector = \"//*[@id=\\\"product\\\"]/div[2]\" }"
            ]
        elements_str = "\n".join(el_lines)
        return MERCHANTS_TEMPLATE.format(
            merchant_name=block["name"],
            page_name=block["page_name"],
            url=block["url"],
            elements=elements_str,
        )
    return ""


def _is_interactive() -> bool:
    return sys.stdin.isatty()


def interactive_fill(block: dict) -> dict:
    """交互式补充配置项（同时支持 watches 和 merchants）。"""
    interactive = _is_interactive()
    hint = " (非交互模式，使用默认值)" if not interactive else ""
    print(f"\n=== 交互式配置补充{hint} ===")

    if not interactive:
        print(f"  名称: {block['name']} (保留)")
        print(f"  类型: {block['block_type']}")
        if block["block_type"] == "watches":
            print(f"  link_selector: {block['link_selector']}")
            print(f"  id_pattern: {block.get('id_pattern', '无')}")
            print(f"  keywords: {block['keywords']}")
        return block

    name = input(f"  监控名称 [{block['name']}]: ").strip()
    if name:
        block["name"] = name

    if block["block_type"] == "watches":
        print(f"  当前 link_selector: {block['link_selector']}")
        ls = input("  link_selector (XPath/CSS，直接回车保留): ").strip()
        if ls:
            block["link_selector"] = ls

        print(f"  当前 id_pattern: {block.get('id_pattern', '无')}")
        ip = input("  id_pattern (直接回车保留/跳过): ").strip()
        if ip:
            block["id_pattern"] = ip
        elif not block.get("id_pattern"):
            block["id_pattern"] = None

        print(f"  当前关键字: {block['keywords']}")
        kw = input("  关键字 (逗号分隔，回车跳过): ").strip()
        if kw:
            block["keywords"] = [k.strip() for k in kw.split(",") if k.strip()]

    elif block["block_type"] == "merchants":
        page_name = input(f"  页面名称 [{block.get('page_name', '')}]: ").strip()
        if page_name:
            block["page_name"] = page_name
        print("  元素选择器将在 TOML 片段中以注释形式提供，请在追加后手工编辑 config.toml 补充。")

    return block


def append_to_config(toml_fragment: str) -> None:
    """把 TOML 片段追加到 config.toml。"""
    config_path = Path("config.toml")
    backup = config_path.with_suffix(".toml.bak.analyze")
    import shutil
    shutil.copy(config_path, backup)
    print(f"  [备份] {backup} 已创建")

    with open(config_path, "a", encoding="utf-8") as f:
        f.write("\n" + toml_fragment + "\n")
    print(f"  [已追加] config.toml")


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    argv = [a for a in sys.argv if a != "--dry-run"]
    if len(argv) < 2:
        print("用法: python3 analyze_url.py <url> [--dry-run]", file=sys.stderr)
        sys.exit(1)

    url = argv[1].strip()

    print(f"\n[1/5] 正在抓取页面: {url}")
    content = extract_page(url)
    print(f"  抓取成功，内容长度: {len(content)} 字符")

    print("\n[2/5] 正在分析页面结构...")
    watch_block = analyze_forum(content, url)
    merch_block = analyze_merchant(content, url)

    if merch_block and not watch_block:
        block = merch_block
        print("  识别为：商品/产品页 → [[merchants]]")
    elif watch_block and not merch_block:
        block = watch_block
        print("  识别为：论坛/列表页 → [[watches]]")
    elif merch_block and watch_block:
        print("  两种类型均检测到，请选择:")
        print("    [w] watches（论坛列表监控）")
        print("    [m] merchants（商品元素监控）")
        choice = input("  选择 [w/m，回车默认 w]: ").strip().lower()
        block = merch_block if choice == "m" else watch_block
        print(f"  使用: {block['block_type']}")
    else:
        print("  未能自动识别页面类型，使用通用 watches 配置...")
        parsed = urlparse(url)
        block = {
            "block_type": "watches",
            "name": f"自动生成-{parsed.netloc}",
            "url": url,
            "link_selector": "//a",
            "keywords": ["CHANGE_ME"],
            "id_pattern": None,
        }

    block = interactive_fill(block)

    print("\n[3/5] 生成配置片段:")
    print()
    toml_fragment = build_toml(block)
    print(toml_fragment)

    print("[4/5] 确认写入 config.toml? (y/n)", end=" ", flush=True)
    if dry_run or not _is_interactive():
        print("(dry-run / 非交互模式，跳过写入)")
    else:
        confirm = input().strip().lower()
        if confirm == "y":
            append_to_config(toml_fragment)
        else:
            print("  已取消写入。")

    print("\n[5/5] 完成。重启守护进程使配置生效:")
    print("  /hawkeye start-detached  (或前台: python -m hawkeye)")


if __name__ == "__main__":
    main()
