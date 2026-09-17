#!/bin/sh
# HawkEye 安装器（Debian / Ubuntu VPS）。
#
# 服务器上一条命令：
#   curl -fsSL https://raw.githubusercontent.com/CarlJia/HawkEye/main/install.sh -o install.sh
#   chmod +x install.sh
#   sudo ./install.sh
#
# 部署包内（hawkeye deploy 上传解压后调用，脚本旁边就带着 bin/hawkeye）：
#   sudo ./install.sh install --config <暂存配置> [--overwrite-config]
#
# 同一份脚本两种入场：旁边有 bin/hawkeye 就用它，否则先去 GitHub 找发行版
# （下载 + sha256 校验），查不到再退到在服务器上编译源码。
#
# 有终端时给菜单；`curl ... | sh` 没有终端可读答案，直接按默认安装。
set -eu

REPO="CarlJia/HawkEye"
REF="main"
# 用户是否显式给了 --ref。显式给定时强制源码构建：否则发行版路径会把 --ref
# 静默忽略（装到的不是你要的 ref，且没有任何提示）。
REF_EXPLICIT=0
SERVICE="hawkeye.service"
UNIT="/etc/systemd/system/hawkeye.service"
# 二进制、配置、state.json 全在 /opt/hawkeye 下：一个路径备份、迁移，也把
# state_path 的默认相对值（state.json）落在 WorkingDirectory 里。
ROOT="/opt/hawkeye"
BIN="$ROOT/hawkeye"
CONFIG="$ROOT/config.toml"
# 模板里的占位 token：它同时是「配置还没填」的判据（与打包侧 PLACEHOLDER_TOKEN 同值）。
# 只要它在文件里出现过，就判定为未配置、不启动服务。
PLACEHOLDER="123456:ABC-your-bot-token"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# 自动装浏览器时下载到的位置，在 $ROOT 下（--purge 一并清掉，软链指向这里）。
BROWSER_DIR="$ROOT/browser"
# Chrome for Testing 的官方清单：版本号现查，不写死（写死迟早下到过期构建）。
CFT_INDEX="https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json"
# 官方 Chrome 的 .deb（只有 x86_64）：依赖由 dpkg 一并装好，之后随 Google 源自动更新。
CHROME_DEB="https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"
BROWSER_PATH=""
APT_UPDATED=""
STAGED_CONFIG=""
OVERWRITE=0
YES=""
PURGE=""
ACTION=""
SRC_BIN=""
SRC_EXAMPLE=""
WORK=""

# ---- ui ----
# 只在终端上上色，且 NO_COLOR 时不上：管道运行的输出属于日志，不该混进转义序列。
if [ -t 1 ] && [ -z "${NO_COLOR-}" ]; then
	B="$(printf '\033[1m')" D="$(printf '\033[2m')" N="$(printf '\033[0m')"
	G="$(printf '\033[32m')" R="$(printf '\033[31m')" Y="$(printf '\033[33m')"
else
	B="" D="" N="" G="" R="" Y=""
fi

rule() { printf '  %s────────────────────────────────────────────%s\n' "$D" "$N"; }

banner() {
	if [ -t 1 ]; then printf '\033[H\033[2J'; fi
	printf '\n  %sHawkEye%s  %s·%s  网页变更监控安装器\n' "$B" "$N" "$D" "$N"
	rule
	printf '\n'
}

# 标签统一两个 CJK 字宽：printf 按字节补位，宽度不一致会把列对齐弄乱。
ok() { printf '  %s✓%s  %s    %s%s%s\n' "$G" "$N" "$1" "$D" "${2-}" "$N"; }
field() { printf '  %s%s%s    %s\n' "$D" "$1" "$N" "$2"; }
warn() { printf '  %s!%s  %s\n' "$Y" "$N" "$1"; }
die() { printf '  %s✗%s  %s\n' "$R" "$N" "$1" >&2; exit 1; }

ask() {
	if [ ! -t 0 ]; then printf '%s' "$2"; return; fi
	printf '  %s?%s  %s %s[%s]%s ' "$Y" "$N" "$1" "$D" "$2" "$N" >&2
	read -r reply || reply=""
	printf '%s' "${reply:-$2}"
}

confirm() {
	if [ -n "$YES" ]; then return 0; fi
	if [ ! -t 0 ]; then die "$1（非交互运行时加 --yes 确认）"; fi
	printf '  %s?%s  %s  %s[y/N]%s ' "$Y" "$N" "$1" "$D" "$N"
	read -r reply || reply=""
	case "$reply" in y | Y | yes) return 0 ;; *) printf '  已取消\n'; return 1 ;; esac
}

press() {
	if [ ! -t 0 ]; then return 0; fi
	printf '\n  %s回车返回菜单%s ' "$D" "$N"
	read -r _ || true
}

# ---- 载荷来源 ----
# 后三个变量由 resolve_payload 填出：SRC_BIN 必填，SRC_EXAMPLE 可缺（缺了就不播种模板）。
resolve_payload() {
	# 显式 --ref：跳过部署包与发行版两条快捷路径，直接按该 ref 源码构建。
	# 否则发行版存在时 --ref 被静默忽略，装到的不是用户要的分支。
	if [ "$REF_EXPLICIT" = 1 ]; then
		build_from_source
		return 0
	fi
	# 1) 部署包：脚本旁边带着二进制，直接用它，不联网。
	if [ -f "$SCRIPT_DIR/bin/hawkeye" ]; then
		SRC_BIN="$SCRIPT_DIR/bin/hawkeye"
		if [ -f "$SCRIPT_DIR/config.example.toml" ]; then
			SRC_EXAMPLE="$SCRIPT_DIR/config.example.toml"
		fi
		ok "来源" "部署包"
		return 0
	fi
	# 2) GitHub 发行版（没有发行版时返回 1，落到源码构建）。
	if fetch_release; then return 0; fi
	# 3) 源码构建。
	build_from_source
}

detect_arch() {
	case "$(uname -m)" in
	x86_64 | amd64) ARCH=x86_64 ;;
	aarch64 | arm64) ARCH=aarch64 ;;
	*) die "不支持的架构：$(uname -m)（发行版提供 x86_64 与 aarch64）" ;;
	esac
}

# 发行版路径：能拿到就返回 0，没有发行版就返回 1 让调用方回落到源码构建。
fetch_release() {
	command -v curl >/dev/null 2>&1 || die "需要 curl：apt-get install -y curl"
	detect_arch
	asset="hawkeye-$ARCH-unknown-linux-musl"
	base="https://github.com/$REPO/releases/latest/download"
	# tag 取自 GitHub 对 latest 的跳转，不打 API、不解析 JSON，也就不会被限流。
	# 只有第一跳带 tag（链路止于 release-assets.githubusercontent.com），所以不能跟随跳转。
	tag="$(curl -fsSI -o /dev/null -w '%{redirect_url}' "$base/$asset" 2>/dev/null |
		sed -n 's#.*/download/\([^/]*\)/.*#\1#p')" || true
	[ -n "$tag" ] || return 1
	ok "版本" "$tag"

	curl -fsSL --max-time 300 "$base/$asset" -o "$WORK/$asset" ||
		die "二进制下载失败：$base/$asset"
	# 用发行版自己的校验文件核对：截断的传输或被替换的资源，都要在落进 /opt 之前挡下。
	curl -fsSL --max-time 30 "$base/sha256sums.txt" -o "$WORK/sums" ||
		die "校验文件下载失败"
	want="$(sed -n "s/^\([0-9a-f]\{64\}\)  *$asset\$/\1/p" "$WORK/sums")"
	[ -n "$want" ] || die "sha256sums.txt 里没有 $asset 这一项"
	got="$(sha256sum "$WORK/$asset" | cut -d' ' -f1)"
	[ "$got" = "$want" ] || die "校验不通过，已丢弃下载的文件。期望 ${want}，实得 ${got}"
	ok "校验" "sha256 一致"
	SRC_BIN="$WORK/$asset"

	# 模板随发行版一起取；取不到只告警，用户仍可自己写配置，不该因此中断安装。
	if curl -fsSL --max-time 30 \
		"https://raw.githubusercontent.com/$REPO/$tag/config.example.toml" \
		-o "$WORK/config.example.toml"; then
		SRC_EXAMPLE="$WORK/config.example.toml"
	else
		warn "未取到 config.example.toml；安装后请自行创建 $CONFIG"
	fi
	return 0
}

# 源码构建：没有发行版时的兜底（也能给开发中的分支用，见 --ref）。
build_from_source() {
	ok "来源" "源码构建"

	if ! command -v git >/dev/null 2>&1; then
		if command -v apt-get >/dev/null 2>&1; then
			DEBIAN_FRONTEND=noninteractive apt-get update -q || true
			DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
				-o DPkg::Lock::Timeout=600 git || die "需要 git，安装失败"
		else
			die "需要 git，且这台机器没有 apt-get"
		fi
	fi

	if ! command -v cargo >/dev/null 2>&1; then
		command -v curl >/dev/null 2>&1 || die "需要 curl 才能安装 Rust 工具链"
		warn "未检测到 Rust 工具链，正在安装 rustup（几分钟）……"
		curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs |
			sh -s -- -y --profile minimal || die "rustup 安装失败"
		# rustup 把 cargo 放进 ~/.cargo/bin，当前 shell 需要显式加载。
		# shellcheck disable=SC1091
		. "$HOME/.cargo/env"
	fi

	low_mem_warn

	git clone --depth 1 --branch "$REF" "https://github.com/$REPO.git" "$WORK/src" ||
		die "克隆 ${REPO}（${REF}）失败"
	# 只在 main 上见过 Python 版的仓库：Rust 版没并进来时先失败在这里，
	# 而不是让 cargo 抛一条看不懂的「找不到 Cargo.toml」。
	[ -f "$WORK/src/rust/Cargo.toml" ] ||
		die "分支 $REF 里没有 rust/Cargo.toml（Rust 版可能还没并进这个分支）；用 --ref <分支/标签> 指定"

	ok "构建" "cargo build --release（首次较久）……"
	(cd "$WORK/src/rust" && cargo build --release) ||
		die "cargo build --release 失败（日志见上）"

	SRC_BIN="$WORK/src/rust/target/release/hawkeye"
	if [ -f "$WORK/src/config.example.toml" ]; then
		SRC_EXAMPLE="$WORK/src/config.example.toml"
	fi
}

# cargo 编译单个 crate 峰值内存不低，小内存 VPS 上先提醒一句，免得只看到 OOM。
low_mem_warn() {
	mem_kb="$(sed -n 's/^MemTotal:[[:space:]]*\([0-9]*\).*/\1/p' /proc/meminfo 2>/dev/null || true)"
	case "$mem_kb" in '' | *[!0-9]*) return 0 ;; esac
	if [ "$mem_kb" -lt 1500000 ]; then
		warn "内存约 $((mem_kb / 1024))MB，cargo 可能因 OOM 失败；可先加 swap 或改用发行版二进制"
	fi
}

# ---- 安装 ----
is_configured() {
	[ -f "$CONFIG" ] || return 1
	! grep -qF "$PLACEHOLDER" "$CONFIG" 2>/dev/null
}

backup_config() {
	bak="$CONFIG.bak.$(date +%Y%m%d-%H%M%S)"
	install -m 600 "$CONFIG" "$bak"
	ok "备份" "$bak"
}

# 配置的三种来源：上传来的（--config）、已有的、模板。上传来的那份若服务器上
# 已有真实配置且没给 --overwrite-config，就并排放成 .incoming 而不动现有那份。
seed_config() {
	if [ -n "$STAGED_CONFIG" ] && [ -f "$STAGED_CONFIG" ]; then
		if [ -f "$CONFIG" ] && [ "$OVERWRITE" -eq 0 ]; then
			install -m 600 "$STAGED_CONFIG" "$CONFIG.incoming"
			warn "已保留现有 ${CONFIG}（新配置存为 $CONFIG.incoming）"
			return 0
		fi
		if [ -f "$CONFIG" ]; then backup_config; fi
		install -m 600 "$STAGED_CONFIG" "$CONFIG"
		ok "配置" "已采用上传的 config.toml"
		return 0
	fi

	if [ ! -f "$CONFIG" ]; then
		if [ -n "$SRC_EXAMPLE" ] && [ -f "$SRC_EXAMPLE" ]; then
			install -m 600 "$SRC_EXAMPLE" "$CONFIG"
			ok "配置" "已生成模板"
		else
			warn "$CONFIG 不存在且没有模板可用；请自行创建后再启动服务"
		fi
	else
		ok "配置" "保留现有 config.toml"
	fi
	# 已有文件可能是 644（手工建的），收紧一次；失败不阻断。
	chmod 600 "$CONFIG" 2>/dev/null || true
}

# ---- 浏览器 ----
# 应用只按固定绝对路径探测浏览器（/usr/bin/google-chrome(-stable)、
# /usr/local/bin/google-chrome、/snap/bin/chromium、/usr/bin/chromium(-browser)），
# 顺序也照抄在下面——先找到的那个就是应用真正会用的那个。

apt_install() {
	DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
		-o DPkg::Lock::Timeout=600 "$@" >/dev/null 2>&1
}

apt_update_once() {
	if [ -z "$APT_UPDATED" ]; then
		APT_UPDATED=1
		# 上一次被打断的 apt 会卡住重跑；开机时的 unattended-upgrades 会占锁，
		# -o DPkg::Lock::Timeout 让它等锁而不是直接失败。
		dpkg --configure -a >/dev/null 2>&1 || true
		DEBIAN_FRONTEND=noninteractive apt-get update -q \
			-o DPkg::Lock::Timeout=600 >/dev/null 2>&1 || true
	fi
}

existing_browser() {
	for brb in /usr/bin/google-chrome /usr/bin/google-chrome-stable \
		/usr/local/bin/google-chrome /snap/bin/chromium \
		/usr/bin/chromium /usr/bin/chromium-browser; do
		if [ -x "$brb" ]; then
			printf '%s\n' "$brb"
			return 0
		fi
	done
	for brb in google-chrome google-chrome-stable chromium chromium-browser; do
		if command -v "$brb" >/dev/null 2>&1; then
			command -v "$brb"
			return 0
		fi
	done
	return 1
}

# 文件在不等于能用：Ubuntu 上的 chromium-browser 是 snap 壳子，命令在、一跑就
# 找不到 snapd。所以按「能不能真的报出版本」判定。
browser_works() {
	[ -n "${1-}" ] || return 1
	if command -v timeout >/dev/null 2>&1; then
		timeout 30 "$1" --version >/dev/null 2>&1
	else
		"$1" --version >/dev/null 2>&1
	fi
}

unzip_to() {
	if command -v unzip >/dev/null 2>&1; then
		unzip -q -o "$1" -d "$2"
	elif command -v python3 >/dev/null 2>&1; then
		python3 -m zipfile -e "$1" "$2"
	else
		return 1
	fi
}

# 最小化 VPS 上缺的就是这一组运行库。逐个装：某个包名在某个发行版上不存在
# （如 libasound2 的 t64 过渡）时只丢一个，不会整批失败。
install_browser_libs() {
	for brb_pkg in libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
		libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
		libgbm1 libpango-1.0-0 libcairo2 libatspi2.0-0 fonts-liberation; do
		apt_install "$brb_pkg" || true
	done
	apt_install libasound2 || apt_install libasound2t64 || true
}

# Chrome for Testing：Google 官方发布的 Chrome 构建，linux64 / linux-arm64 都有，
# 不依赖发行版包管理（Ubuntu 早把 chromium 换成 snap，apt 拿不到真浏览器）。
# 解到 $BROWSER_DIR 再软链到 /usr/local/bin/google-chrome 供应用发现。
install_cft_chrome() {
	case "$(uname -m)" in
	x86_64 | amd64) brb_platform=linux64 brb_dir=chrome-linux64 ;;
	aarch64 | arm64) brb_platform=linux-arm64 brb_dir=chrome-linux-arm64 ;;
	*) return 1 ;;
	esac
	command -v curl >/dev/null 2>&1 || return 1

	brb_url="$(curl -fsSL --max-time 60 "$CFT_INDEX" 2>/dev/null |
		grep -o 'https://storage.googleapis.com/chrome-for-testing-public/[^"]*/'"$brb_platform"'/'"$brb_dir"'.zip' |
		head -1)"
	[ -n "$brb_url" ] || return 1

	curl -fsSL --max-time 600 "$brb_url" -o "$WORK/cft.zip" || return 1
	# ${VAR:?}：万一路径为空，宁可报错也不要 rm -rf /
	rm -rf "${BROWSER_DIR:?}/$brb_dir"
	mkdir -p "$BROWSER_DIR"
	unzip_to "$WORK/cft.zip" "$BROWSER_DIR" || return 1

	[ -f "$BROWSER_DIR/$brb_dir/chrome" ] || return 1
	chmod +x "$BROWSER_DIR/$brb_dir/chrome" 2>/dev/null || true
	install_browser_libs
	ln -sf "$BROWSER_DIR/$brb_dir/chrome" /usr/local/bin/google-chrome
	return 0
}

# 无头抓取必须有本机 Chrome/Chromium。四级依次尝试：已装 → apt 的 chromium
# （Debian 才有这个包）→ 官方 Chrome 的 .deb（仅 x86_64，依赖自带）→ Chrome for
# Testing（x64/arm64 都有）。全都拿不到只告警不阻断：用户可能想自己指定浏览器。
install_browser() {
	BROWSER_PATH="$(existing_browser)" || BROWSER_PATH=""
	if browser_works "$BROWSER_PATH"; then
		ok "浏览器" "$BROWSER_PATH"
		return 0
	fi
	if [ -n "$BROWSER_PATH" ]; then
		warn "已装的 $BROWSER_PATH 跑不起来（Ubuntu 的 chromium-browser 是 snap 壳子），继续找替代"
	fi

	if ! command -v apt-get >/dev/null 2>&1; then
		warn "未检测到可用的浏览器且无 apt-get；请手动安装 Chrome / Chromium"
		return 0
	fi
	apt_update_once

	ok "浏览器" "尝试 apt 安装 chromium……"
	if apt_install chromium; then
		BROWSER_PATH="$(existing_browser)" || BROWSER_PATH=""
		if browser_works "$BROWSER_PATH"; then
			ok "浏览器" "$BROWSER_PATH"
			return 0
		fi
	fi

	case "$(uname -m)" in
	x86_64 | amd64)
		ok "浏览器" "尝试 Google Chrome 官方 .deb（约 110MB）……"
		if curl -fsSL --max-time 600 "$CHROME_DEB" -o "$WORK/chrome.deb" &&
			apt_install "$WORK/chrome.deb"; then
			BROWSER_PATH="$(existing_browser)" || BROWSER_PATH=""
			if browser_works "$BROWSER_PATH"; then
				ok "浏览器" "$BROWSER_PATH"
				return 0
			fi
		fi
		;;
	esac

	ok "浏览器" "尝试 Chrome for Testing……"
	if install_cft_chrome; then
		BROWSER_PATH="$(existing_browser)" || BROWSER_PATH=""
		if browser_works "$BROWSER_PATH"; then
			ok "浏览器" "$BROWSER_PATH"
			return 0
		fi
	fi

	# 走到这里说明现有这些都没通过验证：摘要不该把它们当成可用浏览器报出去。
	BROWSER_PATH=""
	warn "自动安装浏览器失败；守护进程会因找不到浏览器而退出"
	warn "手动装：apt-get install -y chromium，或按 README 指定浏览器路径"
}

# Chrome for Testing 解在 $ROOT/browser 下，/usr/local/bin/google-chrome 是指向它的
# 软链。卸载时若还指着我们的目录就摘掉，别在系统里留一个断链。
remove_browser_link() {
	if [ -L /usr/local/bin/google-chrome ]; then
		case "$(readlink /usr/local/bin/google-chrome)" in
		"$ROOT"/*) rm -f /usr/local/bin/google-chrome ;;
		esac
	fi
}

# 单元里的路径与上面的常量同源，改一处要一起改。
write_unit() {
	cat >"$UNIT" <<UNIT
[Unit]
Description=HawkEye 网页元素变更监控
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/hawkeye
ExecStart=/opt/hawkeye/hawkeye -c /opt/hawkeye/config.toml
Restart=always
RestartSec=5
# 退出码 2 是配置错误或 Telegram 凭据/chat_id 不可用：重启无用，停在 failed
# 等人工修配置，避免空转重启循环。
RestartPreventExitStatus=2

[Install]
WantedBy=multi-user.target
UNIT
}

print_summary() {
	printf '\n  %s%s%s\n' "$B" "安装完成" "$N"
	rule
	printf '\n'
	field "二进制" "$BIN"
	field "配置" "$CONFIG"
	if [ -n "$BROWSER_PATH" ]; then
		field "浏览器" "$BROWSER_PATH"
	else
		field "浏览器" "未找到（服务会启动失败）"
	fi
	if is_configured; then
		field "服务" "运行中"
	else
		field "服务" "未启动（config.toml 还是模板）"
		field "    " "${D}填好 Telegram 凭据后：systemctl start $SERVICE${N}"
	fi
	field "状态" "systemctl status $SERVICE"
	field "日志" "journalctl -u $SERVICE -f"
	printf '\n'
}

install_hawkeye() {
	WORK="$(mktemp -d)"
	trap 'rm -rf "$WORK"' EXIT

	resolve_payload
	[ -n "$SRC_BIN" ] || die "没有可安装的二进制"

	install -d -m 0755 "$ROOT"

	# 留一份旧二进制，新版本起不来时回滚用。绝不覆盖已有的备份：上一次若死在
	# 安装与健康检查之间，$BIN 里那份是没被证明能启动的，盖掉好备份会让回滚
	# 复原回同一个坏二进制却报成功。
	backup=""
	if [ -f "$BIN" ]; then
		backup="$BIN.old"
		if [ ! -f "$backup" ]; then cp -f "$BIN" "$backup"; fi
	fi
	# 先停，避免在活进程底下替换可执行文件。
	systemctl stop "$SERVICE" 2>/dev/null || true
	install -m 755 "$SRC_BIN" "$BIN"
	ok "安装" "$BIN"

	seed_config
	install_browser
	# 二进制与浏览器都落盘了，临时目录可以收了。菜单会多次调用本函数，每次都会
	# 替换 trap，留着就会让上一份目录和里面的下载物无人回收。
	rm -rf "$WORK"
	trap - EXIT

	write_unit

	systemctl daemon-reload
	systemctl enable "$SERVICE" >/dev/null 2>&1 || true

	if is_configured; then
		# 不交给 set -e：起不来的二进制会让 restart 失败，而这正是下面回滚
		# 存在的理由。放任 set -e，脚本会在这里带着 systemd 的原始报错退出。
		systemctl restart "$SERVICE" || true
		# is-active 答得比「立刻退出的进程」还早，等一拍再问。
		sleep 3
		if ! systemctl is-active --quiet "$SERVICE"; then
			if [ -n "$backup" ]; then
				install -m 755 "$backup" "$BIN"
				rm -f "$backup"
				systemctl restart "$SERVICE" 2>/dev/null || true
				die "新版本没能启动，已回滚到上一版。日志：journalctl -u $SERVICE -n 50"
			fi
			die "服务启动失败。日志：journalctl -u $SERVICE -n 50"
		fi
		rm -f "$BIN.old"
		ok "服务" "已启动并开机自启"
	else
		warn "config.toml 仍是模板，未启动服务"
	fi

	print_summary
}

# ---- 卸载 ----
uninstall_hawkeye() {
	if [ ! -f "$BIN" ] && [ ! -f "$UNIT" ]; then
		# 配置与状态比服务活得久，所以 --purge 在这条分支上仍有事可做。
		if [ -n "$PURGE" ] && { [ -e "$CONFIG" ] || [ -e "$ROOT/state.json" ]; }; then
			confirm "服务已经卸载了。删除 $ROOT 下的配置与状态？不可撤销" || return 0
			rm -rf "$ROOT"
			remove_browser_link
			ok "数据" "已删除"
			return 0
		fi
		if [ -e "$CONFIG" ] || [ -e "$ROOT/state.json" ]; then
			die "服务已经卸载了，配置与状态还留在 ${ROOT}；要一并删掉就加 --purge"
		fi
		die "这台机器上没有装 HawkEye"
	fi

	if [ -n "$PURGE" ]; then
		confirm "卸载 HawkEye，并删除 $ROOT 下的配置与状态？不可撤销" || return 0
	else
		confirm "卸载 HawkEye？配置与状态保留在 $ROOT" || return 0
	fi

	systemctl disable --now "$SERVICE" 2>/dev/null || true
	rm -f "$UNIT" "$BIN" "$BIN.old"
	remove_browser_link
	systemctl daemon-reload
	ok "服务" "已移除"
	if [ -n "$PURGE" ]; then
		rm -rf "$ROOT"
		ok "数据" "已删除"
	else
		field "配置" "保留在 ${ROOT}，重新安装会直接接着用"
	fi
}

menu() {
	while :; do
		banner
		printf '    1  安装 / 升级\n'
		printf '    2  卸载\n'
		printf '    3  状态\n'
		printf '    4  日志\n'
		printf '    q  退出\n\n'
		printf '  %s›%s ' "$B" "$N"
		read -r choice || exit 0
		printf '\n'
		case "$choice" in
		1) install_hawkeye; press ;;
		2) uninstall_hawkeye; press ;;
		3) systemctl status "$SERVICE" --no-pager || true; press ;;
		4) journalctl -u "$SERVICE" -f --no-pager ;;
		q | Q | exit | "") exit 0 ;;
		*) ;;
		esac
	done
}

usage() {
	cat <<TXT
HawkEye 安装器（Debian / Ubuntu）

  sudo ./install.sh                 有终端时给菜单，否则按默认安装
  sudo ./install.sh install         直接安装 / 升级（部署包内由 hawkeye deploy 调用）
  sudo ./install.sh --uninstall     卸载，保留配置与状态
  sudo ./install.sh --purge         卸载并删除配置与状态

  --config <路径>      采用这份 config.toml；服务器上已有配置且未给
                       --overwrite-config 时，现有那份保留、新配置存为 .incoming
  --overwrite-config   用 --config 顶掉服务器上已有的 config.toml（先备份）
  --repo <owner/repo>  GitHub 仓库，默认 $REPO
  --ref <分支/标签>    源码构建用的 git ref，默认 $REF。显式指定时强制源码构建，
                       跳过部署包与发行版两条快捷路径
  --yes, -y            跳过确认
  --help, -h           显示这段

脚本旁边若有 bin/hawkeye 就直接用它（部署包形态）；否则先找 GitHub 发行版
（下载 + sha256 校验），查不到再在服务器上编译源码（缺 Rust 时自动装 rustup）。
显式 --ref <分支/标签> 会跳过前两条，直接按该 ref 源码构建。

二进制、配置与 state.json 都在 ${ROOT}。卸载默认保留配置与状态。
TXT
}

# ---- 入口 ----
while [ $# -gt 0 ]; do
	case "$1" in
	install) ACTION=install; shift ;;
	uninstall | --uninstall) ACTION=uninstall; shift ;;
	# 显式护栏而不是 ${2-}：dash 下 shift 2 越界是致命的，输出会是 shell 的
	# 诊断而不是这句提示。
	--config) [ $# -ge 2 ] || die "--config 后面要跟路径"; STAGED_CONFIG="$2"; shift 2 ;;
	--overwrite-config) OVERWRITE=1; shift ;;
	--repo) [ $# -ge 2 ] || die "--repo 后面要跟 owner/repo"; REPO="$2"; shift 2 ;;
	--ref) [ $# -ge 2 ] || die "--ref 后面要跟分支或标签"; REF="$2"; REF_EXPLICIT=1; shift 2 ;;
	--purge) ACTION=uninstall; PURGE=1; shift ;;
	--yes | -y) YES=1; shift ;;
	-h | --help) usage; exit 0 ;;
	*) die "未知参数：$1（--help 看用法）" ;;
	esac
done

[ "$(id -u)" = 0 ] || die "需要 root：sudo sh $0"
command -v systemctl >/dev/null 2>&1 ||
	die "这个安装器只装 systemd 服务。手动运行：$BIN -c $CONFIG"

case "$ACTION" in
uninstall) banner; uninstall_hawkeye ;;
install) banner; install_hawkeye ;;
*)
	if [ -t 0 ]; then
		menu
	else
		banner
		install_hawkeye
	fi
	;;
esac
