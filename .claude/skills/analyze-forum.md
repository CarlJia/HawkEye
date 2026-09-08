---
name: analyze-forum
description: 分析任意论坛/产品页面 URL，自动生成 [[watches]] 或 [[merchants]] 配置片段并追加到 config.toml
---

# Analyze Forum / 产品页面配置生成

## 快速使用

用户提供目标 URL，然后执行：

```bash
python3 .claude/skills/analyze_url.py <url>
```

脚本会依次尝试三种抓取方式，按优先级：
1. `browser-act stealth-extract`（需配置 API key，效果最佳）
2. `requests + BeautifulSoup`（需已安装，标准 Python 环境）
3. `curl` + 纯 Python HTML 解析（零依赖，作为最后备选）

## 工作流程

### 页面类型自动识别

| 检测信号 | 识别类型 | 生成块 |
|----------|----------|--------|
| 论坛关键词（回复、主题、topic） | 论坛/列表页 | `[[watches]]` |
| 商品关键词（价格、库存、buy now） | 产品页 | `[[merchants]]` |

### 内置域名知识库

以下常见论坛可自动填入最优 `link_selector` + `id_pattern`（无需分析 HTML）：

| 域名 | link_selector | id_pattern |
|------|---------------|------------|
| v2ex.com | `//span[@class='item_title']/a` | `/t/(\d+)` |
| nodeseek.com | `//*[@id="nsk-body-left"]/ul/li/div/div[1]/a` | `post-(\d+)-` |
| hostloc.com / hostloc.cn | `//td[@class='fl_g']/a` | (无) |
| lowendtalk.com | `//a[contains(@href,'/discussion/')]` | `/discussion/(\d+)` |
| webhorde.net | `//a[contains(@href,'/thread/')]` | `/thread/(\d+)` |

未知域名：对页面链接做泛化分析，自动推断选择器。

### merchants（商品/元素监控）

当检测到商品关键词时，会生成 `[[merchants]]` 片段，并从 HTML 中提取可能的价格/库存 class/id 作为注释候选，供手工选择。

### 交互式补充

脚本在生成 TOML 前会询问：
- 监控名称（可回车用默认值）
- `link_selector`（可回车用自动推断值）
- `id_pattern`（可回车跳过）
- `keywords`（watches 专用）

非交互模式（`--dry-run`、管道、cron）自动跳过交互，使用默认值。

## 输出示例（V2EX）

```
[[watches]]
name = "自动生成-www.v2ex.com"
url = "https://www.v2ex.com/"
link_selector = "//span[@class='item_title']/a"
keywords = [
    "CHANGE_ME"  # ← 替换为你的关键字
]
id_pattern = "/t/(\d+)"
```

## 写入与生效

- 写入前自动备份 `config.toml` → `config.toml.bak.analyze`
- 追加后验证 TOML 格式：`python3 -c "import tomllib; tomllib.load(open('config.toml','rb')); print('OK')"`
- 重启生效：`/hawkeye start-detached`

## dry-run 模式

```bash
python3 .claude/skills/analyze_url.py <url> --dry-run
```

只打印 TOML 片段，不写入文件，适合预览。
