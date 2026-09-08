# HawkEye

24×7 监控指定网页元素的文本变化，变化时通过 Telegram 推送「旧值 → 新值」。
典型用途：商品「是否可售」状态（充足 / 较少 / 售罄）的到货/变更提醒。

基于 **Python + Playwright 无头浏览器**：能渲染 JS 动态页面，选择器同时支持
CSS 与 XPath（可直接粘贴浏览器复制出来的 XPath）。

除元素文本监控外，还支持并行的「列表新条目监控」：定期抓取论坛/列表页，按帖子 ID
去重发现「标题命中关键字的全新帖子」并逐条推送（首个实例：NodeSeek 首页含 `hk` 的新帖）。

## 特性

- 三层配置：商家 → 页面 → 监控元素，上层默认值向下级联，减少重复配置
- 同一页面的多个元素共享一次页面加载，省资源、省被封风险
- 轮询间隔可配置，默认 1 分钟；可在商家、页面任意层级覆盖
- 变更检测：首次仅建立基线（不打扰），之后仅在文本变化时通知
- 列表新条目监控（可选的第二种模式）：抓论坛/列表页，按帖子 ID 去重发现新帖，
  标题命中关键字（子串 / 不区分大小写 / 任一命中）即逐条推送；首次静默建基线
- 至少一次交付：通知发送成功后才更新记录值，失败下轮重试
- 两级失败告警：页面加载失败按页面告警，元素未匹配/提取异常按元素告警；
  达到阈值发一条，边沿触发不刷屏，成功后自动复位
- Telegram 控制命令：在聊天里 `/add` `/list` `/del` 增删查监控，直接写回
  `config.toml` 并即时生效，无需登服务器、无需重启
- 状态原子落盘，损坏自动备份重建

## 安装

需要 Python 3.11+（开发使用 3.14）。Homebrew 的 Python 受 PEP 668 管控，请用 venv。

```bash
python3 -m venv .venv
source .venv/bin/activate          # fish: source .venv/bin/activate.fish
pip install -e ".[dev]"            # 安装开发依赖（测试等）
pip install -e ".[deploy]"          # 可选：解锁 hawkeye init / hawkeye deploy
playwright install --with-deps chromium   # 下载 Chromium 及系统依赖
```

`.[deploy]` 不安装时，`hawkeye init`（配置向导）与 `hawkeye deploy`（一键部署到 VPS）
不可用，其余功能不受影响。

运行时依赖三个包：`playwright`（无头浏览器）、`httpx`（Telegram HTTP 调用）、
`tomli-w`（把配置写回 TOML，控制命令改配置时用到）。

## 配置

```bash
cp config.example.toml config.toml
chmod 600 config.toml              # 内含 Telegram 密钥，务必收紧权限
```

Windows 没有 `chmod` 时请用 `icacls` 收紧 ACL；若执行失败会打印告警，权限收紧
属于尽力而为，请自行确认配置文件不在共享目录。

### 两条命令上机（Debian / Ubuntu + macOS / Windows）

安装可选依赖后，两条命令即可完成从零到服务运行：

```bash
hawkeye init                              # 交互式问 bot_token / chat_id，生成 config.toml
hawkeye deploy                            # 收集 VPS 信息，打包上传，在 VPS 上完成全部安装
```

`init` 与 `deploy` 在 macOS / Windows / Linux 均可运行。监控守护进程本身仍限 Linux /
macOS（VPS 上跑）。

### `hawkeye init` 详解

`init` 在交互式向导里问两个问题后直接写出文件，不会跑 Playwright 或网络检查（除非你选自检）：

```
$ hawkeye init
开始配置 config.toml（Ctrl-C 随时中止，未确认前不会落盘）
请输入 Telegram bot_token（输入隐藏）：
请输入 Telegram chat_id（个人或群，群 id 通常为负数）：
是否现在做一次 Telegram 凭据自检？(y/N，回车跳过) y
  → 调用 Telegram API 验证 token + chat_id
  → 永久性拒绝（400/401/403/404）会就地重问，不会写盘
  → 网络异常只告警，继续走完流程
  → 跳过自检完全不加载 httpx
请确认保存？(y/N，回车取消) y
已写入配置：
  路径       config.toml
  权限       600
  bot_token  ***<末尾4位>
  chat_id    <你的id>
下一步：运行 `hawkeye deploy` 把这份配置与程序部署到 VPS。
```

已有配置时，`init` 只改 `[telegram]` 两键，`[[merchants]]` / `[[watches]]` 原样保留。
写盘前会留一份 `config.toml.bak.<时间戳>`（600 权限）。`bot_token` 全程不回显。

### `hawkeye deploy` 详解

完整流程如下（每步出错都会在打包之前退 1，不留半截远端状态）：

```
$ hawkeye deploy
请输入 VPS 主机地址：<你的IP或域名>
请输入 SSH 端口（回车默认 22）：
请输入 SSH 用户名：<你的用户名>
请输入 SSH 密码（输入隐藏）：<你的密码>
  → 首次连接：打印服务器 SHA256 指纹，等待确认（输入 y/yes/是/确认/确定/保存 其一）
    确认后指纹写入 ~/.ssh/known_hosts，此后每次自动校验
  → 探测 id -u / sudo 可用性；无权限在打包之前退 1
正在打包……（本地操作，秒级完成）
正在上传……（~zip 大小决定耗时）
正在远端安装……
[HawkEye] 正在远端后台安装 Chromium（首次约 10–15 分钟）……
  → 服务已启用，配置已就绪
安装完成。常用运维命令：
  systemctl status hawkeye    # 查看服务状态
  journalctl -u hawkeye -f   # 跟随日志
  hawkeye deploy             # 再次运行即升级
```

**远端 config.toml 三态判定：**

| 远端状态 | 本机有 config.toml | 行为 |
|---|---|---|
| 不存在 | 有 | 采用本机配置 |
| 仍是占位符（`123456:ABC-...`） | 有 | 采用本机配置，**先备份远端那份** |
| 已是真实配置 | 有 | 保留远端，本机那份本次不生效 |
| — | 无 | 打印提示，服务不会自动启动，手工填好后再 `systemctl start hawkeye` |

确需用本机配置覆盖远端时加 `--overwrite-config`（会先备份远端旧配置）。

**连接参数复用：** 首次输入 host / port / user 后，`deploy` 会存到本机 `.hawkeye-deploy.toml`
（权限 600，已进 `.gitignore`）。下次只需输密码。

### 部署后操作

```bash
# 查看服务状态
systemctl status hawkeye

# 跟随日志（实时看监控心跳）
journalctl -u hawkeye -f

# 查看 Telegram 是否收到监控心跳消息（正常约每分钟一条 DEBUG）
journalctl -u hawkeye --since "1 minute ago" | grep -v "^$"

# 修改配置后直接生效（Telegram 命令会即时写回 config.toml）
# 也可以直接 vim /opt/hawkeye/config.toml 然后：
systemctl reload-or-restart hawkeye

# 升级（再次运行 hawkeye deploy 即可，config.toml 与 state.json 保留）
hawkeye deploy

# 完全卸载
sudo /opt/hawkeye/deploy.sh uninstall
```

### 故障排查

| 症状 | 排查命令 |
|---|---|
| 部署后服务起不来 | `journalctl -u hawkeye -n 50` 看启动日志 |
| Telegram 没收到消息 | 确认 bot 已私聊过 `/start`、chat_id 为负数则在群里加 bot |
| 元素监控返回空值 | 站点改版，选择器失效；用 `DEBUG` 日志级别重新抓取确认 |
| 占用内存高 | 减少 `max_concurrent_fetches`（默认 4），或调大轮询间隔 |
| `systemctl start hawkeye` 卡住 | Chromium 可能卡在下载；`journalctl -u hawkeye` 看是否还在装 |

> 注意：`deploy` 失败时**不会有任何 Telegram 告警**。进程在 `Notifier` 构造出来之前就退出了。
> 没收到通知 ≠ 部署成功。**部署后请先检查服务状态。**

### 取得 bot_token 与 chat_id

1. 私聊 [@BotFather](https://t.me/BotFather) 发送 `/newbot`，拿到 `bot_token`。
2. **先用自己的账号私聊这个新 bot 并发送 `/start`。** Telegram 不允许 bot 主动向
   陌生用户发起会话，跳过这一步的话之后每次 `sendMessage` 都会返回
   `400 Bad Request: chat not found`。
3. 访问 `https://api.telegram.org/bot<你的token>/getUpdates`，从返回的
   `result[].message.chat.id` 读出自己的 `chat_id`（纯数字）。
4. 若要发到群里：先把 bot 拉进群，群的 `chat_id` 是负数；群升级为超级群后 id 会
   变成 `-100` 前缀的新值，需同步更新配置。

启动时会用 `getChat` 做一次自检：凭据或 `chat_id` 不可用会直接以退出码 2 终止并
打印原因，不会带着「发不出通知」的状态空转。

关键字段见 `config.example.toml` 注释。配置分三层：每个 `[[merchants]]` 需要
`name`；其下每个 `[[merchants.pages]]` 需要完整 `url`；每个页面下至少一个
`[[merchants.pages.elements]]`，元素只需 `selector`（`name` 缺省回退为 selector；若写了
`nth` 则回退为 `selector#nth`，确保同选择器不同 `nth` 的元素标识唯一）。
默认值向下级联（全局 → 商家 → 页面），`selector_type` 缺省 `auto`（以 `//`、`(`、
`/`、`xpath=` 开头判为 XPath）。同一页面下的多个元素只加载一次页面。

### 浏览器环境维度（`[fingerprint]`）

locale / timezone / color_scheme / viewport 等可观测值在 HTTP 头 / Client Hints /
`navigator.languages` / `Intl.DateTimeFormat` 之间互相对齐，是反爬常按「这些维度
是否互相矛盾」判定 headless 流量的依据。HawkEye 把这些做成顶层字段
（Chromix 风格的 launch 参数）：在 `[telegram]` 之前写 `[fingerprint]` 即为全局；
某个商家 / 监控目标想换就 inline table `fingerprint = { ... }` 覆盖。留空时
HawkEye 维持与 Chromium 主版本对齐的默认 UA、跟随系统 locale。其余 JS 层指纹
（navigator.webdriver / UA Brands / chrome.runtime / plugins / WebGL）由
`playwright-stealth` 包负责——这是两个互不重叠的补丁层。

```toml
# 全局（写在 [telegram] 之前）
[fingerprint]
locale = "zh-CN"
timezone_id = "Asia/Shanghai"
color_scheme = "light"
viewport = { width = 1366, height = 768 }

# 商家级 inline table 覆盖
[[merchants]]
name = "yunyoo-en"
fingerprint = { locale = "en-US", timezone_id = "America/New_York" }
```

### 代理（`proxy` 字段）

HTTP / SOCKS5 代理按 Chromix 思路集中翻译：HTTP 代理走 Playwright `new_context(
proxy=...)` 字段；SOCKS5 走 Chromium `--proxy-server` CLI 形参（Playwright 的 proxy
字段对 SOCKS 不完整生效）。字符串简写与结构化字段都接受：

```toml
# 全局（写在 [telegram] 之前）
proxy = "http://user:pass@127.0.0.1:7890"
# 或结构化
# proxy = { server = "socks5://1.2.3.4:1080", bypass = "*.example.com" }

# 商家级覆盖（inline table）
[[merchants]]
name = "shop-behind-vpn"
proxy = "socks5://10.0.0.1:1080"
```

代理密码不会留进启动 journal——日志打印时自动剥离 userinfo 段。SOCKS5 鉴权
（用户名密码）无法用 Chromium CLI 形参表达，落到应用层代理前置解决。

### 自定义提取（`js` 字段）

文本路径只取元素自身的 `inner_text`，遇到「class 是否含某关键字」「按钮 `disabled` 状态」
`data-*` 之类「纯文本拿不到/不直观」的状态就不够用。可给元素加 `js` 字段，传一个 JS
**箭头函数表达式**，对元素求值后返回的字符串即状态值：

```toml
[[merchants.pages.elements]]
name = "按钮是否可订"
selector = "#order-button"
js = "el => el.classList.contains('disabled') ? '售罄' : '可订'"
```

`js` 非空时完全取代 `inner_text`（同页面下别的元素仍走文本路径，不受影响）。返回值：

- 字符串 → 直接作为状态值，归一化（折叠空白、trim）后写入 detect；
- `null` / `undefined` → 等同「选择器未匹配到元素」；
- 空字符串 → 「JS 表达式返回空字符串」（诊断文案区分于文本模式的「文本为空」）；
- 抛错（语法错、`el.foo` 不存在等）→「提取失败：{JS 报错原文}」，由失败告警路径处理。

JS 表达式以字符串形式由 Playwright 在浏览器里执行。常见用法见 `config.example.toml`
的「自定义提取」示例。

状态文件 `state.json` 无需手动创建：首次运行会为每个元素建立基线并自动生成。
状态按「商家 / 页面 / 元素」标识记录；若修改了这三者中的名称，旧标识失效，
对应元素会被视为新元素、静默重建基线（不会误报一次变更）。

`config.toml` 是唯一配置来源，手工编辑与 Telegram 命令改的是同一个文件。但要注意：
经 Telegram `/add` `/del` 增删监控后，`config.toml` 会被**整份重写**，注释与排版可能不
保留（写入前会先留一份备份，见「注意事项」）。想长期保住注释就只手工编辑。

### 列表新条目监控（`[[watches]]`）

除元素监控外，可用顶层 `[[watches]]` 块声明「列表新条目监控」目标，与 `[[merchants]]`
平行；`[[merchants]]` 与 `[[watches]]` 至少配置一个，两者可同时存在。每轮抓一次列表页，
用 `link_selector` 定位每个帖子的标题链接 `<a>`：从其 `href` 取帖子 ID、从链接文本取标题；
ID 不在「已见集合」中才算新帖，标题命中关键字即推送一条独立 Telegram（标题 + 可点击链接）。
判新只看帖子 ID，与帖子在列表中的位置/排序无关——论坛按最新回帖时间重排老帖不会误报。

每个 `[[watches]]` 需要：

- `name`：目标名称（状态按「watch / {name}」标识记录，改名等价于新目标、静默重建基线）。
- `url`：列表页地址。
- `link_selector`：匹配列表中每个帖子标题链接 `<a>` 的选择器（CSS 或 XPath）。
- `keywords`：非空关键字列表；匹配规则为子串、不区分大小写、任一命中即命中（OR），只针对标题。
- `id_pattern`（可选）：用捕获组 1 从 `href` 提取帖子 ID 的正则；缺省取 `href` path 末段。
  例：`href="/post-911200-1"` 配 `'post-(\d+)-'` → ID `911200`。

`selector_type` / `poll_interval_secs` / `wait_until` / `nav_timeout_secs` / `failure_threshold`
均沿用全局默认，可在块内按需覆盖。首次运行只把当前可见帖子 ID 记入已见集合、一律不推送
（静默建基线，避免上线刷屏历史帖）；后续新增关键字只对其后 ID 未见过的新帖生效，不回溯历史帖。
命中新帖发送成功后才记入已见集合，失败下轮重试（至少一次交付）；列表加载或提取失败走同一套
两级失败告警。相对链接（如 `/post-911200-1`）会自动补全为可点击的绝对 URL。字段示例见
`config.example.toml`。

已见集合有 1000 条上限，超出后淘汰「最久未见」的 ID，故 `state.json` 不随运行时长无限增长。
本轮仍出现在列表页上的帖子会被刷新为最近见过，长期置顶帖不会被挤出集合而重复推送。

## 运行

`pip install -e ".[dev]"` 之后，pyproject.toml 里的 `[project.scripts]` 会装出
`hawkeye` 控制台命令，与 `python -m hawkeye` 完全等价——前者更短、后者不依赖
console_script 注册（适合临时跑/容器里用）。两者参数一致，按习惯选一个即可。

```bash
hawkeye -c config.toml               # 前台运行（与 python -m hawkeye -c config.toml 等价）
python -m hawkeye -c config.toml     # 等价形式，不依赖 console_script
hawkeye -c config.toml -v            # 附带调试日志
```

### 本地调试

`DEBUG` 级别会把「每轮抓取结果」都打出来，是验证选择器、观察轮询心跳最直接的方式。
改完 `config.toml` 不必重启服务——`Ctrl-C` 停掉再前台起一次即可（几秒级别）。

```bash
# 边写选择器边验证：DEBUG 下每次抓取都会打印一条结果日志
hawkeye -c config.toml -v
# 等价于
hawkeye -c config.toml --log-level DEBUG

# 临时压低噪音（验证没问题后调回 WARNING / ERROR，只看告警）
hawkeye -c config.toml --log-level WARNING

# 跑别的实例（用不同配置目录），与生产隔离
hawkeye -c ./debug/config.toml -v

# 调试时不必登服务器：在本机也能完整跑——只是收到 Telegram 通知的就是本机的 chat_id
```

### 调整日志等级

默认输出 `INFO` 及以上。日志等级有两种调法，`--log-level` 优先级更高：

```bash
hawkeye -c config.toml -v                  # 等价于 --log-level DEBUG
hawkeye -c config.toml --log-level DEBUG   # 显式指定，取值不区分大小写
hawkeye -c config.toml --log-level WARNING # 只保留告警及以上，压低噪音
```

可选等级：`DEBUG` / `INFO` / `WARNING` / `ERROR`。`DEBUG` 下每次抓取都会打印一条
结果日志：元素未变更记为 `DEBUG`（默认不显示，便于确认轮询在正常工作），首次基线
与实际变更仍记为 `INFO`。部署为 systemd 服务时，把对应参数加到 `ExecStart` 即可。

### 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | `init` / `deploy` 失败（配置错误、网络错误、凭据错误等） |
| 2 | 守护进程专属：`config.toml` 含明文 `bot_token` 或 `chat_id` 不可达；systemd 用它停在 failed 状态 |
| 130 | Ctrl-C 中断 |

`init` / `deploy` 的失败永远不用 2。

## Telegram 控制命令

进程在轮询之外还会长轮询接收命令，因此增删监控无需登服务器、无需重启。直接在与 bot
的聊天里发送：

| 命令 | 作用 |
|---|---|
| `/add` | 新建监控，按提示逐步填写（网页元素变更 / 论坛关键词） |
| `/list` | 列出全部监控及当前状态 |
| `/del` | 按编号删除一个监控，需二次确认 |
| `/menu` | 重新同步 Telegram 快捷菜单 |
| `/help` | 显示命令说明 |
| `/cancel` | 取消进行中的操作 |

- **只有配置里那个 `chat_id` 能用。** 其他任何人发来的消息一概静默丢弃、不回复，
  也不会有任何副作用——bot 不会成为陌生人的回声探测器。
- **`/add` 是多轮向导**：先选类型（1 = 网页元素变更，2 = 论坛关键词），再逐步问 URL、
  选择器（元素）或链接选择器、关键词（论坛），最后统一问一次名称——回复 `-` 表示不起名，
  标识回退为选择器（元素）或列表页 URL（论坛）。选择器 CSS 与 XPath 自动识别，
  XPath 以 `/` 开头也不会被误当成命令。
- **保存前会先试抓一次并回显结果**：元素监控回显当前取到的文本，论坛监控回显「找到 N
  条链接，其中 M 条命中关键词」。没抓到值不代表填错（可能只是暂时没有符合的帖子），
  此时会问「仍然保存 / 取消」，由你决定。
- **`/del` 需要二次确认**：先回复编号，再回复 `1` 确认；回复其他内容一律按取消处理。
- **快捷菜单自动创建并同步**：启动时把上表推给 Telegram（`setMyCommands`），之后点输入框
  旁的菜单按钮或只打一个 `/` 就能挑命令，不用记。推送前先读回现有菜单比对，一致就不重复
  写；菜单与 `/help` 共用同一张表，不会各说各话。菜单只是输入便利，同步失败只记一条告警、
  不影响监控，事后随时可发 `/menu` 重试。
- **改动即时生效**：写回 `config.toml` 后调度器立刻按新配置增删轮询循环，未受影响的
  监控计时器不重置、基线与失败计数不丢。
- 期间手工编辑过 `config.toml` 也没关系：每条命令都会先把文件内容同步进运行时，再在最
  新内容上改。文件若被改成不可解析的状态，`/list` 仍会输出运行中的配置，但写入会被拒
  绝并提示先修文件。


## 一键部署（Debian / Ubuntu）

`deploy.sh` 只负责 VPS 服务器侧的安装与卸载，不承担本机打包或上传职责。通常在本机
安装 `.[deploy]` 后使用 `hawkeye deploy` 完成收集 VPS 信息、打包上传和远端安装。
服务器上也可以直接运行：

```bash
sudo ./deploy.sh install
```

脚本会自动完成：检测系统与 Python（要求 3.11+）、用 apt 安装依赖、创建 `hawkeye` 系统用户、
将项目部署到 `/opt/hawkeye`、建立 venv 并安装、用 Playwright 下载 Chromium 及其系统依赖、
生成 `config.toml` 模板（权限 600），并写入、启用下文的 systemd 服务。

配置可通过 `--config <路径>` 指定本机配置文件；`--overwrite-config` 强制用本机配置覆盖远端
已有配置。默认按以下三态处理：

- 远端没有 `config.toml`：采用本机配置（若提供）。
- 远端 `config.toml` 仍含占位符：采用本机配置，并先备份远端文件。
- 远端已有真实配置：保留远端配置，本机那份本次不生效；确需覆盖时加
  `--overwrite-config`。

首次安装后若 `config.toml` 仍是模板，服务不会自动启动；填好 Telegram 凭据后再启动：

```bash
sudo -e /opt/hawkeye/config.toml            # 填入 bot_token / chat_id 及监控项
sudo systemctl start hawkeye
sudo journalctl -u hawkeye -f               # 跟随日志
```

`install` 幂等，可重复运行以升级（保留服务器上已有的 `config.toml` 与 `state.json`）。卸载：

```bash
sudo /opt/hawkeye/deploy.sh uninstall             # 交互确认是否删除数据目录
sudo /opt/hawkeye/deploy.sh uninstall -y          # 非交互，一并删除 /opt/hawkeye 与 hawkeye 用户
sudo /opt/hawkeye/deploy.sh uninstall --keep-data # 只移除服务，保留数据
```

### 手工上传路径（进阶）

手工上传路径仍可用；`./deploy.sh package` 现在委托给 Python 的 `hawkeye package`，三平台
均可运行。不用把整个仓库传上去，在本地开发机一条命令打出部署包：

```bash
./deploy.sh package        # 无需 sudo，产出 dist/hawkeye-<版本>-<时间戳>.zip
```

包内只含运行所需文件：`src/`、`pyproject.toml`、`config.example.toml`、`README.md`、
`deploy.sh`；本地 `config.toml`、`state.json`、缓存与 IDE 文件都不会入包，可放心传输。
命令结束时会打印带实际文件名的上传更新命令：

```bash
scp dist/hawkeye-0.1.0-20260905-165656.zip user@vps:/tmp/
ssh user@vps
unzip -q /tmp/hawkeye-0.1.0-20260905-165656.zip -d /tmp && cd /tmp/hawkeye-0.1.0-20260905-165656
sudo ./deploy.sh install
```

`install` 会用包内代码整体替换 `/opt/hawkeye/src`（不留已删除的模块），保留服务器上的
`config.toml` 与 `state.json`，并在配置就绪时重启服务。每个包解压成带时间戳的独立目录，
不会和上一版混在一起。

## 手动部署为 systemd 服务（进阶）

`/etc/systemd/system/hawkeye.service`：

```ini
[Unit]
Description=HawkEye 网页元素变更监控
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=hawkeye
WorkingDirectory=/opt/hawkeye
ExecStart=/opt/hawkeye/.venv/bin/python -m hawkeye -c /opt/hawkeye/config.toml
Restart=on-failure
RestartSec=10
# 退出码 2 表示配置错误或 Telegram 凭据/chat_id 不可用：重启无用，直接停在
# failed 状态等人工修配置，避免每 10 秒空转重启一次。
RestartPreventExitStatus=2

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hawkeye
journalctl -u hawkeye -f
```

进程收到 `SIGTERM`/`SIGINT`（`systemctl stop` 或 Ctrl-C）会优雅退出：轮询与命令接收
两条循环一起收敛，关闭浏览器并把状态落盘。

## 资源占用

无头 Chromium 比纯 HTTP 抓取更吃内存。建议：

- VPS 内存 ≥ 1GB
- 用 `max_concurrent_fetches`（默认 4）限制同时打开的页面数
- 目标很多时适当调大 `poll_interval_secs`，降低瞬时并发与被封风险

## 注意事项

- **密钥安全**：`bot_token`、`chat_id` 明文存于 `config.toml`，请 `chmod 600`，
  切勿提交到版本库（`.gitignore` 已忽略 `config.toml` 与 `state.json`）。
  日志已装脱敏过滤器，token 在 httpx 打印的请求 URL 中会显示为 `<REDACTED>`；
  若 token 曾出现在旧日志或被贴到别处，请用 @BotFather 的 `/revoke` 重置。
- **备份文件同样敏感**：通过 Telegram 命令改配置时，会在同目录留下
  `config.toml.bak.<时间戳>`，**其中同样含明文 token**。它们的权限也是 600、只保留最近
  10 份，清理、打包、拷走时请按与 `config.toml` 同等的敏感度对待。
- **其它可能残留的敏感文件**：
  - `.hawkeye-deploy.toml`：`hawkeye deploy` 存 VPS 连接参数用，不含密码，已进 `.gitignore`。
  - `config.toml.tmp`：`configedit` 写盘时的临时文件，**含明文 `bot_token`**，崩溃时
    残留，已进 `.gitignore`。
  - `state.json.tmp` / `state.json.corrupt.<时间戳>`：状态文件读写时的临时/损坏备份，含
    帖子 ID 等信息，已进 `.gitignore`。
  - 这三族是既有代码会留下的残留，此前既不在文档里也不在 `.gitignore` 里。

### 从反向选择器迁移到 `js`

之前用「反向选择器」（如 `#product32-order-button:not(.disabled)`）监听按钮状态翻转的
写法有陷阱：状态翻转时选择器匹配不到元素，走的是失败告警路径而不是变更通知。
切到 `js` 字段后状态值直接当字符串做 detect，售罄/恢复都会以变更通知发出。

迁移时注意：`state.json` 里同标识的旧基线（如 `"Order Now"`）与新 JS 返回值（如
`"可订"`）不同，首轮会按 state 协议触发一条变更通知——这是预期行为，不是 bug。
若不想收到这条「假」通知，**改 `config.toml` 前先从 `state.json` 删掉对应条目**（删前
可记录其当前值，下次真有变化时仍能正常通知）。

### Windows 注意事项

- `getpass` 在 mintty / Git Bash 下可能回显输入，请使用 Windows Terminal 或 PowerShell。
- 控制台编码尽量设 UTF-8（`chcp 65001`），中文才不乱码。
- `icacls` 权限收紧是尽力而为；若执行失败会打印告警，请自行确认 `config.toml` 不在
  共享目录。
- 测试套件的权限断言在 Windows 上不通过，测试只在 macOS / Linux 上跑。

### 部署失败了怎么办

1. 查看本机输出的日志路径（在「正在部署……」之后会打印
   `VPS 上日志在 /var/tmp/hawkeye-deploy.XXXXXXXXXX/install.log`）。
2. 连接 VPS：`ssh user@vps`。
3. 查看日志：`journalctl -u hawkeye -f` 或
   `cat /var/tmp/hawkeye-deploy.XXXXXXXXXX/install.log`。
4. 若升级失败，按提示执行恢复命令（三行以内），通常是把 `src.old` 换回 `src` 并重启服务。

**重要：升级失败时不会有任何 Telegram 告警。** 进程在 `Notifier` 构造出来之前就死了，
10 秒一次的静默重启循环不会发出通知。**没收到告警 ≠ 部署成功。**

- **选择器脆弱性**：浏览器复制的定位式 XPath（如 `.../article[4]/div/div/span`）
  依赖页面结构，站点改版易失效。可优先选用带 `id`/`class` 的稳定选择器。
- **反爬/封禁**：默认 1 分钟轮询较激进；若目标站点敏感，适当放宽间隔。

## 开发

```bash
pytest                     # 运行测试（test_extract / test_fetch / test_packaging / test_wizard 需已安装 chromium）
ruff check . && ruff format --check .
mypy src/hawkeye
```

### 本地开发调试

修完代码、跑通单测后，还要看真实页面渲染效果再上线。`test_extract` 等单测用
fake locator 不实际启 Chromium，但选择器微调还是要靠真浏览器确认。

```bash
# 跑全部测试（需要 chromium 才能跑通 test_extract / test_fetch / test_packaging / test_wizard）
pytest

# 跑单个文件 / 某个测试名 / 首个失败即停 —— 反馈循环的核心
pytest tests/test_config.py
pytest tests/test_fetch.py::test_xxx
pytest -x                       # 首个失败即停，不跑剩下的
pytest -k xxx                   # 按名字筛
pytest --lf                     # 只跑上次失败的
pytest -s                       # 不抓 stdout，breakpoint() / print() 输出可见

# 跳过需要 chromium 的测试（不想装 Chromium 也能跑主体）
pytest --ignore=tests/test_extract.py --ignore=tests/test_fetch.py --ignore=tests/test_packaging.py --ignore=tests/test_wizard.py

# 改完实际启动看效果：DEBUG 级别会把每轮抓取结果都打出来（见「本地调试」）
hawkeye -c config.toml -v

# 临时隔离一份配置做实验（不影响主 config.toml / state.json）
cp config.toml config.toml.main.bak
cp state.json state.json.main.bak
cp config.example.toml config.debug.toml
hawkeye -c config.debug.toml -v

# 启动到一半想进 debugger：在源码某行插 breakpoint()，再以 -s 跑 pytest，或：
PYTHONBREAKPOINT=pudb hawkeye -c config.toml -v      # 需要 pip install pudb
```
