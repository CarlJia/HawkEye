#!/usr/bin/env bash
# install.sh 的冒烟测试。
#
#   bash tests/install_smoke.sh
#
# 全程把 ROOT、systemd 单元、以及所有会被探测的绝对浏览器路径重定向到临时目录，
# 并用桩替换 id / systemctl / dpkg / apt-get / curl，所以：不需要 root、不联网、
# 不碰宿主机的 /usr/bin、/usr/local/bin、/opt、/etc。
#
# 覆盖：安装 / 升级 / 回滚、配置三态、卸载，以及浏览器的四级自动安装。
set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_SH="$REPO_ROOT/install.sh"
[ -f "$INSTALL_SH" ] || { echo "找不到 $INSTALL_SH"; exit 1; }

BASE="$(mktemp -d "${TMPDIR:-/tmp}/hawkeye-install-smoke.XXXXXX")"
FAILED=0
# 失败时保留现场供排查，成功才清掉。
trap 'if [ "$FAILED" -eq 0 ]; then rm -rf "$BASE"; else echo "工件保留在 $BASE"; fi' EXIT

# ---- 断言 ----
check() { # check <说明> <实际> <期望>
	if [ "$2" = "$3" ]; then printf '  ✓ %s\n' "$1"; else printf '  ✗ %s（实际=%s 期望=%s）\n' "$1" "$2" "$3"; FAILED=1; fi
}
has() { if grep -q "$2" "$3"; then printf '  ✓ %s\n' "$1"; else printf '  ✗ %s（%s 里没有 %s）\n' "$1" "$3" "$2"; FAILED=1; fi; }
hasnt() { if grep -q "$2" "$3"; then printf '  ✗ %s（%s 里不该有 %s）\n' "$1" "$3" "$2"; FAILED=1; else printf '  ✓ %s\n' "$1"; fi; }
exists() { if [ -e "$1" ]; then printf '  ✓ %s\n' "$2"; else printf '  ✗ %s（缺 %s）\n' "$2" "$1"; FAILED=1; fi; }
absent() { if [ ! -e "$1" ]; then printf '  ✓ %s\n' "$2"; else printf '  ✗ %s（不该有 %s）\n' "$2" "$1"; FAILED=1; fi; }

# ---- 夹具 ----
# 一个「能用」的浏览器：报版本、退出 0。
good_browser() { printf '#!/bin/sh\necho "Chromium 140.0.0.0"\n' > "$1"; chmod +x "$1"; }
# 一个「在但跑不起来」的：Ubuntu 的 chromium-browser snap 壳子。
broken_browser() { printf '#!/bin/sh\necho "requires the chromium snap" >&2\nexit 1\n' > "$1"; chmod +x "$1"; }
real_config() { printf '[telegram]\nbot_token = "%s"\nchat_id = "1"\n' "$1" > "$2"; }
# 上一版二进制：回滚场景里「换回来的那一份」，内容只是标记，供断言。
old_binary() { printf '#!/bin/sh\necho OLD-GOOD\n' > "$1"; }
# 结构损坏的配置：不含占位符（is_configured 判真）却没有 [telegram] 段——现场形态，
# 守护进程读它会以退出码 2 停在 failed。名字点在「怎么坏」上，不是 real_config 的反面。
broken_config() { printf 'poll_interval_secs = 60\n' > "$1"; }

# 造一份 Chrome for Testing 的假资产：清单里一条本机架构的地址 + 对应的 zip。
# 用 python3 打包而不是 zip(1)，免得依赖 runner 上恰好装了 zip。
CFT_PLATFORM=""; CFT_DIR=""
make_cft_assets() {
	case "$(uname -m)" in
	x86_64 | amd64) CFT_PLATFORM=linux64 CFT_DIR=chrome-linux64 ;;
	aarch64 | arm64) CFT_PLATFORM=linux-arm64 CFT_DIR=chrome-linux-arm64 ;;
	*) echo "不支持的架构：$(uname -m)"; exit 1 ;;
	esac
	mkdir -p "$1/pack/$CFT_DIR"
	good_browser "$1/pack/$CFT_DIR/chrome"
	(cd "$1/pack" && python3 -m zipfile -c "$1/cft.zip" "$CFT_DIR")
	cat > "$1/index.json" <<JSON
{"channels":{"Stable":{"version":"140.0.0","downloads":{"chrome":[
{"platform":"$CFT_PLATFORM","url":"https://storage.googleapis.com/chrome-for-testing-public/140.0.0/$CFT_PLATFORM/$CFT_DIR.zip"}
]}}}}
JSON
}

# setup <名字>：造一份改写过的 install.sh，所有绝对路径都指向本用例的临时目录。
setup() {
	CASE="$BASE/$1"
	SYSBIN="$CASE/sysbin" LOCALBIN="$CASE/localbin" SNAPBIN="$CASE/snapbin" PATHBIN="$CASE/pathbin"
	mkdir -p "$CASE" "$SYSBIN" "$LOCALBIN" "$SNAPBIN" "$PATHBIN"
	# 「没有可用浏览器」必须不依赖 runner 上装了什么：GitHub 的 ubuntu 镜像自带
	# Google Chrome，install.sh 的 `command -v` 兜底会找到它。所以在 PATH 最前面
	# 放一组跑不起来的同名壳子，把这条路也钉死（需要真浏览器时再覆盖掉）。
	for brb in google-chrome google-chrome-stable chromium chromium-browser; do
		broken_browser "$PATHBIN/$brb"
	done
	SCRIPT="$CASE/install.sh"
	# 顺序要紧：先替长串 google-chrome-stable，再替 google-chrome。
	sed \
		-e "s#^ROOT=\"/opt/hawkeye\"#ROOT=\"$CASE/root\"#" \
		-e "s#^UNIT=\"/etc/systemd/system/hawkeye.service\"#UNIT=\"$CASE/hawkeye.service\"#" \
		-e "s#/usr/bin/google-chrome-stable#$SYSBIN/google-chrome-stable#g" \
		-e "s#/usr/bin/google-chrome#$SYSBIN/google-chrome#g" \
		-e "s#/usr/local/bin/google-chrome#$LOCALBIN/google-chrome#g" \
		-e "s#/snap/bin/chromium#$SNAPBIN/chromium#g" \
		-e "s#/usr/bin/chromium-browser#$SYSBIN/chromium-browser#g" \
		-e "s#/usr/bin/chromium#$SYSBIN/chromium#g" \
		"$INSTALL_SH" > "$SCRIPT"
	mkdir -p "$CASE/bin"
	printf '#!/bin/sh\necho NEW-BINARY\n' > "$CASE/bin/hawkeye"
	cp "$REPO_ROOT/config.example.toml" "$CASE/config.example.toml"

	FFAKE="$CASE/fakebin"; mkdir -p "$FFAKE"
	printf '#!/bin/sh\necho 0\n' > "$FFAKE/id"
	printf '#!/bin/sh\nexit 0\n' > "$FFAKE/dpkg"
	cat > "$FFAKE/systemctl" <<'EOF'
#!/bin/sh
echo "systemctl $*" >> "$SMOKE_LOG"
case "$1" in
is-active)
	# 第一次问是判新版本起没起；回滚之后会再问一次，判回滚有没有真的把服务带回来。
	calls="$(cat "$SMOKE_ISACTIVE_CALLS" 2>/dev/null || echo 0)"
	calls=$((calls + 1))
	echo "$calls" > "$SMOKE_ISACTIVE_CALLS"
	if [ "$calls" -le 1 ]; then want="${SMOKE_ACTIVE:-1}"; else want="${SMOKE_ACTIVE_AFTER:-1}"; fi
	[ "$want" = 1 ] && exit 0 || exit 3
	;;
show)
	# 只认安装器真正要问的那个属性，并按 systemctl 的真实输出格式作答（`属性=值`）：
	# 桩若对参数不敏感或格式不对，安装器把属性名写错、解析写坏也照样通过。
	case "$*" in
	*ExecMainStatus*) printf 'ExecMainStatus=%s\n' "${SMOKE_EXEC_STATUS:-0}" ;;
	*) exit 1 ;;
	esac
	;;
*) exit 0 ;;
esac
EOF
	cat > "$FFAKE/journalctl" <<'EOF'
#!/bin/sh
echo "journalctl $*" >> "$SMOKE_LOG"
[ -n "${SMOKE_JOURNAL:-}" ] && [ -f "$SMOKE_JOURNAL" ] && cat "$SMOKE_JOURNAL"
exit 0
EOF
	# 安装器每个失败用例真的会 sleep 3 等 is-active 落定；桩 systemctl 是确定性的，
	# 这段等待在测试里不提供任何信息，空转掉能省下整套几十秒。
	printf '#!/bin/sh\nexit 0\n' > "$FFAKE/sleep"
	# apt-get 默认失败（等价于「这个发行版没有这个包」）；APT_OK=1 时装成功，
	# APT_MAKES_CHROME=1 时顺手把「chromium」放到被测的探测器路径上。
	cat > "$FFAKE/apt-get" <<'EOF'
#!/bin/sh
echo "apt-get $*" >> "$SMOKE_LOG"
[ "${APT_OK:-0}" = 1 ] || exit 1
if [ "${APT_MAKES_CHROME:-0}" = 1 ]; then
	good="$SMOKE_SYSBIN/chromium"
	printf '#!/bin/sh\necho "Chromium 140.0.0.0"\n' > "$good"
	chmod +x "$good"
fi
exit 0
EOF
	# curl 默认离线（退出 22）；CURL_MODE=serve 时按 URL 提供清单与 zip。
	cat > "$FFAKE/curl" <<'EOF'
#!/bin/sh
out=""; url=""; prev=""
for a in "$@"; do
	[ "$prev" = "-o" ] && out="$a"
	case "$a" in http*) url="$a" ;; esac
	prev="$a"
done
case "${CURL_MODE:-off}" in
serve)
	case "$url" in
	*last-known-good-versions*) cat "$CURL_INDEX"; exit 0 ;;
	*.zip) cp "$CURL_ZIP" "$out"; exit 0 ;;
	esac
	;;
esac
exit 22
EOF
	chmod +x "$FFAKE"/*
	SMOKE_LOG="$CASE/systemctl.log" SMOKE_SYSBIN="$SYSBIN"
	SMOKE_ISACTIVE_CALLS="$CASE/isactive.calls"
	export SMOKE_LOG SMOKE_SYSBIN SMOKE_ISACTIVE_CALLS
	: > "$SMOKE_LOG"
}

# run <用例名> [install.sh 的参数...]
run() {
	shift
	# 写 0 而不是截成空文件：空串在 dash 的 $(( )) 里是语法错误，桩一跑就失手。
	echo 0 > "$SMOKE_ISACTIVE_CALLS"
	PATH="$FFAKE:$PATHBIN:$PATH" \
		SMOKE_ACTIVE="${SMOKE_ACTIVE:-1}" \
		SMOKE_ACTIVE_AFTER="${SMOKE_ACTIVE_AFTER:-1}" \
		SMOKE_EXEC_STATUS="${SMOKE_EXEC_STATUS:-0}" \
		SMOKE_JOURNAL="${SMOKE_JOURNAL:-}" \
		APT_OK="${APT_OK:-0}" APT_MAKES_CHROME="${APT_MAKES_CHROME:-0}" \
		CURL_MODE="${CURL_MODE:-off}" CURL_INDEX="$CASE/cft/index.json" CURL_ZIP="$CASE/cft/cft.zip" \
		sh "$SCRIPT" "$@"
}

section() { printf '\n== %s ==\n' "$1"; }

# ---- 载荷与配置三态 ----

section "A. 首次安装：模板配置不启动服务"
setup a; mkdir -p "$CASE/cft"
run a install >"$CASE/out" 2>&1; check "退出码" "$?" "0"
exists "$CASE/root/hawkeye" "二进制已安装"
exists "$CASE/root/config.toml" "配置已播种"
has "提示未启动服务" "未启动服务" "$CASE/out"
hasnt "没有 restart" "systemctl restart" "$CASE/systemctl.log"

section "B. 真实配置：启动并清理 .old"
setup b; mkdir -p "$CASE/root"
real_config "999:real" "$CASE/root/config.toml"
printf '#!/bin/sh\necho OLD\n' > "$CASE/root/hawkeye"
run b install >"$CASE/out" 2>&1; check "退出码" "$?" "0"
has "报告已启动" "已启动并开机自启" "$CASE/out"
has "二进制已替换" "NEW-BINARY" "$CASE/root/hawkeye"
absent "$CASE/root/hawkeye.old" "成功后删掉 .old"
# 单元里的 2 与安装器拿来判「配置/凭据致命」的那个退出码是同一份契约；顺带钉住
# heredoc 里的常量真的展开了（写错名字会留下空值，单元非法而没人发现）。
has "单元的退出码契约没走样" "RestartPreventExitStatus=2" "$CASE/hawkeye.service"
# shellcheck disable=SC2016  # 要的就是字面量 ${：单元里不该留下没展开的变量
hasnt "单元里没有未展开的变量" '${' "$CASE/hawkeye.service"

section "C. --config 且服务器已有真实配置、未加 --overwrite"
setup c; mkdir -p "$CASE/root" "$CASE/cft"
real_config "server:real" "$CASE/root/config.toml"
real_config "local:real" "$CASE/incoming.toml"
run c install --config "$CASE/incoming.toml" >"$CASE/out" 2>&1; check "退出码" "$?" "0"
has "保留服务器配置" "server:real" "$CASE/root/config.toml"
exists "$CASE/root/config.toml.incoming" "新配置存为 .incoming"

section "D. --config --overwrite-config：先备份再覆盖"
setup d; mkdir -p "$CASE/root" "$CASE/cft"
real_config "server:real" "$CASE/root/config.toml"
real_config "local:real" "$CASE/incoming.toml"
run d install --config "$CASE/incoming.toml" --overwrite-config >"$CASE/out" 2>&1; check "退出码" "$?" "0"
has "采用本机配置" "local:real" "$CASE/root/config.toml"
if ls "$CASE/root"/config.toml.bak.* >/dev/null 2>&1; then
	printf '  ✓ 旧配置已备份\n'
else
	printf '  ✗ 无备份\n'; FAILED=1
fi

section "E. 健康检查失败：回滚到上一版"
setup e; mkdir -p "$CASE/root" "$CASE/cft"
real_config "999:real" "$CASE/root/config.toml"
old_binary "$CASE/root/hawkeye"
SMOKE_ACTIVE=0 run e install >"$CASE/out" 2>&1; check "退出码" "$?" "1"
has "报告回滚" "已回滚" "$CASE/out"
has "二进制回到旧版" "OLD-GOOD" "$CASE/root/hawkeye"
hasnt "回滚成功就不说「仍未起来」" "仍未起来" "$CASE/out"
# is-active 桩按调用次数作答，答不出「回滚到底重启没重启」——直接数 systemctl 日志：
# 升级后一次 restart + 回滚后一次 restart。删掉或挪走回滚那次重启，这条就挂。
check "回滚确实重启了服务" "$(grep -c 'systemctl restart hawkeye.service' "$CASE/systemctl.log")" "2"

section "E2. 回滚也起不来：不能说成回滚成功"
setup e2; mkdir -p "$CASE/root" "$CASE/cft"
# 配置坏着（退出码 2）：上一版换回来读同一份配置，同样起不来——回滚的复验必须说出来。
broken_config "$CASE/root/config.toml"
old_binary "$CASE/root/hawkeye"
SMOKE_ACTIVE=0 SMOKE_ACTIVE_AFTER=0 SMOKE_EXEC_STATUS=2 run e2 install >"$CASE/out" 2>&1; check "退出码" "$?" "1"
has "回滚后复验失败被说出" "仍未起来" "$CASE/out"
has "二进制仍回到旧版" "OLD-GOOD" "$CASE/root/hawkeye"

section "E3. 退出码 2（配置/凭据）：报错指向配置，不甩锅新版本"
setup f2; mkdir -p "$CASE/root" "$CASE/cft"
broken_config "$CASE/root/config.toml"
old_binary "$CASE/root/hawkeye"
printf 'ERROR hawkeye: 缺少 [telegram] 配置段\n' > "$CASE/journal"
SMOKE_ACTIVE=0 SMOKE_ACTIVE_AFTER=0 SMOKE_EXEC_STATUS=2 SMOKE_JOURNAL="$CASE/journal" \
	run f2 install >"$CASE/out" 2>&1; check "退出码" "$?" "1"
has "报错指向配置/凭据" "配置或 Telegram 凭据" "$CASE/out"
hasnt "不再说成新版本的问题" "新版本没能启动" "$CASE/out"
has "回显守护进程自己的报错" "缺少 \\[telegram\\] 配置段" "$CASE/out"

section "E4. 回显日志时抹掉 config 里那把 token"
setup f3; mkdir -p "$CASE/root" "$CASE/cft"
real_config "999:SECRET" "$CASE/root/config.toml"
old_binary "$CASE/root/hawkeye"
# reqwest 的错误串会把带 token 的完整 URL 带进日志。
printf 'WARN hawkeye::notify: 自检失败：error sending request for url (https://api.telegram.org/bot999:SECRET/getChat)\n' >"$CASE/journal"
SMOKE_ACTIVE=0 SMOKE_EXEC_STATUS=1 SMOKE_JOURNAL="$CASE/journal" run f3 install >"$CASE/out" 2>&1
check "退出码" "$?" "1"
hasnt "token 不出现" "SECRET" "$CASE/out"
has "URL 骨架保留、token 被抹" "bot<REDACTED>" "$CASE/out"

section "E5. config 已坏、抠不出 token 时靠形状兜底"
setup f4; mkdir -p "$CASE/root" "$CASE/cft"
broken_config "$CASE/root/config.toml"
old_binary "$CASE/root/hawkeye"
printf 'WARN hawkeye::notify: 自检失败：error sending request for url (https://api.telegram.org/bot8123456:AAFakeSecretPart/getChat)\n' >"$CASE/journal"
SMOKE_ACTIVE=0 SMOKE_EXEC_STATUS=1 SMOKE_JOURNAL="$CASE/journal" run f4 install >"$CASE/out" 2>&1
check "退出码" "$?" "1"
hasnt "token 不出现" "AAFakeSecretPart" "$CASE/out"
has "URL 骨架保留、token 被抹" "bot<REDACTED>" "$CASE/out"

section "E6. 首次安装起不来：没有上一版，不说「新版本」"
setup f5; mkdir -p "$CASE/root" "$CASE/cft"
real_config "999:real" "$CASE/root/config.toml"
SMOKE_ACTIVE=0 run f5 install >"$CASE/out" 2>&1; check "退出码" "$?" "1"
has "说的是服务启动失败" "服务启动失败" "$CASE/out"
hasnt "不提新版本" "新版本没能启动" "$CASE/out"
absent "$CASE/root/hawkeye.old" "没有上一版可备份"

section "E7. 回显日志失败不能吞掉回滚"
setup f6; mkdir -p "$CASE/root" "$CASE/cft"
real_config "999:real" "$CASE/root/config.toml"
old_binary "$CASE/root/hawkeye"
# 日志里混进非法 UTF-8 字节，sed 会报错。诊断失败绝不能连累后面的回滚——
# 这里只查回滚的物证（二进制、重启次数），因为 out 里带着坏字节，grep 未必肯读。
printf 'WARN 坏字节 \377\376 结束\n' >"$CASE/journal"
SMOKE_ACTIVE=0 SMOKE_EXEC_STATUS=1 SMOKE_JOURNAL="$CASE/journal" run f6 install >"$CASE/out" 2>&1
check "退出码" "$?" "1"
has "二进制回到旧版" "OLD-GOOD" "$CASE/root/hawkeye"
check "回滚重启没被跳过" "$(grep -c 'systemctl restart hawkeye.service' "$CASE/systemctl.log")" "2"
absent "$CASE/root/hawkeye.old" "回滚后不留下备份"

section "E8. config 里的 token 形状可疑：宁可不回显"
setup f7; mkdir -p "$CASE/root" "$CASE/cft"
real_config "999:SEC|RET" "$CASE/root/config.toml"
old_binary "$CASE/root/hawkeye"
printf 'WARN 自检失败：error sending request for url (https://api.telegram.org/bot999:SEC|RET/getChat)\n' >"$CASE/journal"
SMOKE_ACTIVE=0 SMOKE_EXEC_STATUS=1 SMOKE_JOURNAL="$CASE/journal" run f7 install >"$CASE/out" 2>&1
check "退出码" "$?" "1"
hasnt "可疑凭据一个字都没漏" 'SEC[|]RET' "$CASE/out"
hasnt "也没有把 sed 的报错漏出来" "sed:" "$CASE/out"
has "仍然报了回滚" "已回滚" "$CASE/out"

section "F. 卸载：默认保留配置，--purge 删除"
setup f; mkdir -p "$CASE/cft"
run f install >/dev/null 2>&1
run f --uninstall --yes >"$CASE/out" 2>&1; check "卸载退出码" "$?" "0"
absent "$CASE/root/hawkeye" "二进制已移除"
absent "$CASE/hawkkeye.service" "单元已移除"
exists "$CASE/root/config.toml" "配置保留"
run f --purge --yes >"$CASE/out2" 2>&1; check "purge 退出码" "$?" "0"
absent "$CASE/root/config.toml" "purge 后配置删除"

# ---- 浏览器四级自动安装 ----

section "G. 没有浏览器且离线：告警但不阻断"
setup g; mkdir -p "$CASE/cft"
run g install >"$CASE/out" 2>&1; check "退出码" "$?" "0"
has "四级都失败" "自动安装浏览器失败" "$CASE/out"
has "摘要显示未找到" "未找到" "$CASE/out"
has "试过 apt" "apt-get install" "$CASE/systemctl.log"

section "H. 已装可用：直接用，不联网"
setup h; mkdir -p "$CASE/cft"; good_browser "$PATHBIN/google-chrome"
run h install >"$CASE/out" 2>&1; check "退出码" "$?" "0"
has "采用已装浏览器" "$PATHBIN/google-chrome" "$CASE/out"
hasnt "没有走 apt" "apt-get install" "$CASE/systemctl.log"

section "I. 已装的跑不起来（snap 壳子）：识破并继续找"
setup i; mkdir -p "$CASE/cft"; broken_browser "$SYSBIN/chromium-browser"
run i install >"$CASE/out" 2>&1; check "退出码" "$?" "0"
has "识破 snap 壳子" "snap 壳子" "$CASE/out"
has "最终告警" "自动安装浏览器失败" "$CASE/out"

section "J. apt 装成 chromium：采用"
setup j; mkdir -p "$CASE/cft"
APT_OK=1 APT_MAKES_CHROME=1 run j install >"$CASE/out" 2>&1; check "退出码" "$?" "0"
exists "$SYSBIN/chromium" "chromium 已就位"
hasnt "没走到兜底" "自动安装浏览器失败" "$CASE/out"

section "K. 兜底 Chrome for Testing：解包 + 软链 + 幂等"
setup k; make_cft_assets "$CASE/cft"
CURL_MODE=serve run k install >"$CASE/out" 2>&1; check "退出码" "$?" "0"
has "走了 CfT 分支" "Chrome for Testing" "$CASE/out"
exists "$CASE/root/browser/$CFT_DIR/chrome" "CfT 已解包"
exists "$LOCALBIN/google-chrome" "软链已建立"
check "软链指向 CfT" "$(readlink "$LOCALBIN/google-chrome")" "$CASE/root/browser/$CFT_DIR/chrome"
hasnt "没有告警" "自动安装浏览器失败" "$CASE/out"
run k install >"$CASE/out2" 2>&1
hasnt "第二次不再下载" "Chrome for Testing" "$CASE/out2"
run k --uninstall --yes >"$CASE/out3" 2>&1; check "卸载退出码" "$?" "0"
absent "$LOCALBIN/google-chrome" "卸载摘掉软链"

printf '\n'
if [ "$FAILED" -eq 0 ]; then echo "全部通过"; else echo "有失败项"; fi
exit "$FAILED"
