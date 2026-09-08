#!/usr/bin/env bash
#
# HawkEye VPS 一键部署脚本（支持 Debian / Ubuntu）
#
#   ./deploy.sh package           # 本地打 zip 部署包，用于上传/更新 VPS
#   sudo ./deploy.sh install      # 安装 / 升级并注册 systemd 服务
#   sudo ./deploy.sh install --config <路径> [--overwrite-config]
#                                 # 装时采用上传来的 config.toml（按三态判定处理）
#   sudo ./deploy.sh uninstall    # 停用并卸载服务（可选清理数据）
#   ./deploy.sh help              # 查看帮助
#
# package 在开发机运行（无需 root）：按白名单把运行所需文件打成
# dist/hawkeye-<版本>-<时间戳>.zip，scp 到服务器解压后跑 install 即可更新。
# install 会：检测系统与 Python(>=3.11)、用 apt 安装依赖、创建 hawkeye 系统用户、
# 将项目部署到 /opt/hawkeye、建立 venv 并安装、用 Playwright 下载 Chromium 及系统依赖、
# 处理 config.toml（默认沿用服务器上已有的；--config 时按三态判定）、写入并启用 systemd 服务。
# 不带 --config 时 install 行为与之前完全一致（这是 R16 幂等的基线）；带 --config 时
# 才会触及 config.toml，按 KTD5 三态判定：缺失/占位符 → 采用；真实配置 → 默认保留，
# 显式给 --overwrite-config 才覆盖（旧配置先备份为 config.toml.bak.<时间戳>）。
# 首启前需在 /opt/hawkeye/config.toml 填入 Telegram 凭据；已填好则脚本会直接启动。

set -euo pipefail

# ---- 常量 ----
readonly APP_NAME="hawkeye"
readonly APP_USER="hawkeye"
readonly INSTALL_DIR="/opt/hawkeye"
readonly VENV_DIR="${INSTALL_DIR}/.venv"
readonly CONFIG_FILE="${INSTALL_DIR}/config.toml"
readonly BROWSERS_DIR="${INSTALL_DIR}/ms-playwright"   # Playwright 浏览器固定落盘位置（服务用户可读）
readonly SERVICE_NAME="hawkeye.service"
readonly SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"
readonly PLACEHOLDER_TOKEN="123456:ABC-your-bot-token"  # config.example.toml 中的占位符，用于判断是否已配置

# install 选项（被 cmd_install 解析；顶层空默认，让 `source deploy.sh` 后调单个函数也安全）。
STAGED_CONFIG=""         # --config 指定的暂存配置路径（R14）
OVERWRITE_CONFIG="no"    # --overwrite-config 是否给出（R14）
DEPLOY_GENERATIONAL_SWAP="no"  # deploy_files 是否完成了 src.new → src 的代际交换（KTD21）

# 安装互斥锁文件路径（KTD20）。默认 /run/hawkeye-install.lock，重启自动清空不留陈旧锁；
# 测试可经环境变量 HAWKEYE_INSTALL_LOCK 改到 /tmp 等位置。
HAWKEYE_INSTALL_LOCK="${HAWKEYE_INSTALL_LOCK:-/run/hawkeye-install.lock}"

# 脚本所在目录即项目根目录（部署源）。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR

# 全程无人值守：安装依赖时避免 apt 交互式弹窗。
export DEBIAN_FRONTEND=noninteractive

# ---- 日志 ----
if [ -t 1 ]; then
    readonly C_RESET=$'\033[0m' C_INFO=$'\033[32m' C_WARN=$'\033[33m' C_ERR=$'\033[31m'
else
    readonly C_RESET="" C_INFO="" C_WARN="" C_ERR=""
fi
log()  { printf '%s[HawkEye]%s %s\n' "$C_INFO" "$C_RESET" "$*"; }
warn() { printf '%s[HawkEye]%s %s\n' "$C_WARN" "$C_RESET" "$*" >&2; }
err()  { printf '%s[HawkEye]%s %s\n' "$C_ERR"  "$C_RESET" "$*" >&2; }
die()  { err "$*"; exit 1; }

# ---- 前置检查 ----

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        die "本命令需要 root 权限，请用 sudo 运行：sudo $0 $*"
    fi
}

# 仅支持 Debian / Ubuntu（含以 debian 为基础的发行版）。
check_os() {
    [ -r /etc/os-release ] || die "无法读取 /etc/os-release，无法识别系统；本脚本仅支持 Debian / Ubuntu。"
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-}" in
        debian | ubuntu) log "检测到系统：${PRETTY_NAME:-${ID}}" ;;
        *)
            case " ${ID_LIKE:-} " in
                *" debian "*) log "检测到类 Debian 系统：${PRETTY_NAME:-${ID:-unknown}}" ;;
                *) die "不支持的系统：${PRETTY_NAME:-${ID:-unknown}}；本脚本仅支持 Debian / Ubuntu。" ;;
            esac
            ;;
    esac
    command -v systemctl >/dev/null 2>&1 || die "未找到 systemd（systemctl），本脚本依赖 systemd 管理服务。"
    command -v apt-get   >/dev/null 2>&1 || die "未找到 apt-get，本脚本仅支持基于 apt 的 Debian / Ubuntu。"
}

# 在 PATH 中挑选 >=3.11 的 python 解释器；找不到则给出可执行的补救指引后退出。
PYTHON_BIN=""
resolve_python() {
    local candidate ver
    for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
        command -v "$candidate" >/dev/null 2>&1 || continue
        # 形如 "3.12"，比较主次版本号是否 >= 3.11
        ver="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" || continue
        if [ "$(printf '%s\n3.11\n' "$ver" | sort -V | head -n1)" = "3.11" ]; then
            PYTHON_BIN="$candidate"
            log "使用 Python 解释器：$candidate（${ver}）"
            return 0
        fi
    done
    err "未找到 Python 3.11+（HawkEye 要求 >=3.11）。"
    err "Debian 12+ / Ubuntu 24.04+ 自带满足要求；较旧的系统请先安装新版 Python，例如 Ubuntu 22.04："
    err "    sudo add-apt-repository -y ppa:deadsnakes/ppa"
    err "    sudo apt-get update && sudo apt-get install -y python3.11 python3.11-venv"
    err "安装后重新运行本脚本即可。"
    exit 1
}

# ---- 安装步骤 ----

install_system_deps() {
    # 上一次被打断的 apt 可能把 dpkg 锁住，这里兜底恢复一下，避免重跑时卡死。
    log "修复上次被中断的 apt 状态（如有）……"
    dpkg --configure -a >/dev/null 2>&1 || true
    log "更新 apt 索引并安装系统依赖……"
    # DPkg::Lock::Timeout=600：开机时 unattended-upgrades 经常占着锁，等它结束而不是立刻失败。
    apt-get update -y -o DPkg::Lock::Timeout=600
    apt-get install -y -o DPkg::Lock::Timeout=600 python3 python3-venv python3-pip ca-certificates
}

create_app_user() {
    if id "$APP_USER" >/dev/null 2>&1; then
        log "系统用户 ${APP_USER} 已存在，跳过创建。"
    else
        log "创建系统用户 ${APP_USER}……"
        useradd --system --no-create-home --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$APP_USER"
    fi
}

check_payload_sources() {
    [ -f "${SCRIPT_DIR}/pyproject.toml" ]      || die "在 ${SCRIPT_DIR} 未找到 pyproject.toml，请在 HawkEye 项目根目录运行本脚本。"
    [ -d "${SCRIPT_DIR}/src" ]                 || die "在 ${SCRIPT_DIR} 未找到 src/，请在 HawkEye 项目根目录运行本脚本。"
    [ -f "${SCRIPT_DIR}/config.example.toml" ] || die "在 ${SCRIPT_DIR} 未找到 config.example.toml，请在 HawkEye 项目根目录运行本脚本。"
}

# 白名单拷贝：只放运行所需文件，绝不复制本地 config.toml / state.json / IDE 与缓存文件，
# 避免把本地密钥或状态带上服务器。install 与 package 共用这一份清单。
copy_payload() {
    local dest="$1"
    cp -a "${SCRIPT_DIR}/src" "${dest}/src"
    cp -a "${SCRIPT_DIR}/pyproject.toml" "${dest}/pyproject.toml"
    cp -a "${SCRIPT_DIR}/config.example.toml" "${dest}/config.example.toml"
    [ -f "${SCRIPT_DIR}/README.md" ] && cp -a "${SCRIPT_DIR}/README.md" "${dest}/README.md"
    cp -a "${SCRIPT_DIR}/deploy.sh" "${dest}/deploy.sh"
}

strip_build_artifacts() {
    find "$1" -type d \( -name __pycache__ -o -name '*.egg-info' \) -prune -exec rm -rf {} + 2>/dev/null || true
}

# 重复运行时保留服务器上已有的 config.toml 与 state.json。
deploy_files() {
    log "部署项目文件到 ${INSTALL_DIR}……"
    check_payload_sources
    install -d -m 755 "$INSTALL_DIR"

    # 若直接从安装目录内的副本重跑（源即目标），文件已就位，跳过拷贝以免自我删除/覆盖。
    if [ "$SCRIPT_DIR" = "$INSTALL_DIR" ]; then
        log "源目录即安装目录，跳过文件拷贝。"
        return 0
    fi

    # 代际交换（KTD21）：唯一不可逆的动作加一层可回退窗口。
    # 新代码先拷成 src.new，旧代码退到 src.old，最后 atomic swap；
    # 三个后续步骤（pip install / playwright install / enable_and_start）全部通过
    # 才删 src.old，任一步失败由 cmd_install 的 EXIT 陷阱把 src.old 换回去。
    if [ -d "${INSTALL_DIR}/src" ]; then
        rm -rf "${INSTALL_DIR}/src.old" || true
        if ! mv "${INSTALL_DIR}/src" "${INSTALL_DIR}/src.old"; then
            die "无法把 ${INSTALL_DIR}/src 移到 src.old，安装中止。"
        fi
    fi
    if ! cp -a "${SCRIPT_DIR}/src" "${INSTALL_DIR}/src.new"; then
        if [ -d "${INSTALL_DIR}/src.old" ]; then
            mv "${INSTALL_DIR}/src.old" "${INSTALL_DIR}/src" || true
        fi
        die "复制新代码到 ${INSTALL_DIR}/src.new 失败，已回滚。"
    fi
    if ! mv "${INSTALL_DIR}/src.new" "${INSTALL_DIR}/src"; then
        if [ -d "${INSTALL_DIR}/src.old" ]; then
            mv "${INSTALL_DIR}/src.old" "${INSTALL_DIR}/src" || true
        fi
        die "把 ${INSTALL_DIR}/src.new 移到 src 失败，已回滚。"
    fi
    DEPLOY_GENERATIONAL_SWAP="yes"

    # 顶层文件（pyproject.toml / config.example.toml / README.md / deploy.sh）非不可逆，
    # 原位覆盖即可；不要把这一组合并进 src.new，否则代际交换的语义就糊了。
    cp -a "${SCRIPT_DIR}/pyproject.toml" "${INSTALL_DIR}/pyproject.toml"
    cp -a "${SCRIPT_DIR}/config.example.toml" "${INSTALL_DIR}/config.example.toml"
    [ -f "${SCRIPT_DIR}/README.md" ] && cp -a "${SCRIPT_DIR}/README.md" "${INSTALL_DIR}/README.md"
    cp -a "${SCRIPT_DIR}/deploy.sh" "${INSTALL_DIR}/deploy.sh"

    # 清掉可能被一起拷进来的本地构建产物（字节码缓存、egg-info）。
    strip_build_artifacts "${INSTALL_DIR}/src"
}

setup_venv() {
    log "创建虚拟环境并安装 HawkEye……"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    "${VENV_DIR}/bin/pip" install --upgrade pip setuptools
    "${VENV_DIR}/bin/pip" install "$INSTALL_DIR"
}

install_browser() {
    log "下载 Chromium 及其系统依赖（首次较慢，请耐心等待）……"
    # 固定浏览器落盘目录，确保以 hawkeye 用户运行的服务能找到；--with-deps 会用 apt 安装所需系统库。
    PLAYWRIGHT_BROWSERS_PATH="$BROWSERS_DIR" "${VENV_DIR}/bin/playwright" install --with-deps chromium
}

# 三态判定（R14 / KTD5）：
#   1. 给了 --config 且远端 config.toml 不存在 / 仍是占位符模板 → 采用上传来的；占位符态也先备份。
#   2. 给了 --config 且远端已是真实配置 + --overwrite-config → 旧配置先备份，再用上传的覆盖。
#   3. 给了 --config 且远端已是真实配置 + 未给 --overwrite-config → 保留远端、打印提示、退出码 0。
#   4. 没给 --config 且远端没有 → 生成模板。
#   5. 没给 --config 且远端已有 → 原样保留。
# 占位符态也必须备份的理由：is_configured()（下方）对整个文件做子串 grep，占位 token
# 只要在文件里出现过（一行注释掉的旧配置、粘进去的示例片段）就翻成「未配置」；不先备份
# 就可能毁掉服务器上唯一那份真实配置。
seed_config() {
    if [ -n "$STAGED_CONFIG" ] && [ -f "$STAGED_CONFIG" ]; then
        if is_configured; then
            if [ "$OVERWRITE_CONFIG" = "yes" ]; then
                log "按 --overwrite-config 覆盖；旧配置先备份。"
                backup_existing_config
                install -m 600 "$STAGED_CONFIG" "$CONFIG_FILE"
            else
                warn "服务器上已有真实 config.toml，本次保留不覆盖；你本机的 config.toml 未生效。"
                warn "确实要用本机那份顶掉服务器上的，请加 --overwrite-config（会先备份旧配置）。"
            fi
        else
            log "采用上传来的 config.toml。"
            backup_existing_config
            install -m 600 "$STAGED_CONFIG" "$CONFIG_FILE"
        fi
    elif [ ! -f "$CONFIG_FILE" ]; then
        log "生成 config.toml 模板（请稍后填入 Telegram 凭据）……"
        cp -a "${INSTALL_DIR}/config.example.toml" "$CONFIG_FILE"
    else
        log "检测到已存在的 config.toml，保留不覆盖。"
    fi
    chmod 600 "$CONFIG_FILE"
}

# 把服务器上现有 config.toml 备份为 config.toml.bak.<时间戳>（600）。
# 文件不存在时 no-op。被 seed_config 的两条采用分支共享。
backup_existing_config() {
    [ -f "$CONFIG_FILE" ] || return 0
    local bak
    bak="${CONFIG_FILE}.bak.$(date +%Y%m%d-%H%M%S)"
    log "旧配置备份为 ${bak}。"
    install -m 600 "$CONFIG_FILE" "$bak"
}

set_ownership() {
    log "调整 ${INSTALL_DIR} 属主为 ${APP_USER}……"
    chown -R "${APP_USER}:${APP_USER}" "$INSTALL_DIR"
    chmod 600 "$CONFIG_FILE"
}

write_service() {
    log "写入 systemd 服务单元 ${SERVICE_PATH}……"
    cat > "$SERVICE_PATH" <<EOF
[Unit]
Description=HawkEye 网页元素变更监控
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${INSTALL_DIR}
Environment=PLAYWRIGHT_BROWSERS_PATH=${BROWSERS_DIR}
ExecStart=${VENV_DIR}/bin/python -m hawkeye -c ${CONFIG_FILE}
Restart=on-failure
RestartSec=10
# 退出码 2 表示配置错误或 Telegram 凭据/chat_id 不可用：重启无用，直接停在
# failed 状态等人工修配置，避免每 10 秒空转重启一次。
RestartPreventExitStatus=2

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
}

# 判断 config.toml 是否已实际配置（不再含示例占位符 token）。
is_configured() {
    [ -f "$CONFIG_FILE" ] || return 1
    ! grep -q "$PLACEHOLDER_TOKEN" "$CONFIG_FILE"
}

enable_and_start() {
    systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
    if is_configured; then
        log "配置已就绪，启动 ${SERVICE_NAME}……"
        systemctl restart "$SERVICE_NAME"
        sleep 2
        systemctl --no-pager --full status "$SERVICE_NAME" || true
        # 分层健康判据（R25）：deploy.sh 退出码 0 不等于服务在跑。
        layered_health_check
    else
        warn "config.toml 仍为模板（未填入真实 Telegram 凭据），暂不启动服务。"
    fi
}

# 分层健康判据（R25）。逐条把 `deploy.sh` 退出 0 ≠ 服务可用这件事拆出来：
#   1. is-enabled 必须 enabled（enable 步骤本身失败 = 开机不自启 = 等于没装）。
#   2. 配置已就绪时 is-active 必须 active（active 是被 systemd 实际拉起的证据）。
#   3. 观察窗内 NRestarts 不增长（持续重启 = 配置/依赖在抖，但还没被 systemd 标 failed）。
#   4. ExecMainStatus=2 单独给消息，那是 systemd 单元里 RestartPreventExitStatus=2
#      故意让它停在 failed 的退出码，意味着配置或 Telegram 凭据致命错误，重启无用。
# 任一条不满足：以非零退出（cmd_install 用 trap 转成 die）并打印该查什么。
layered_health_check() {
    local enabled status restarts restarts_after exec_status
    enabled="$(systemctl is-enabled "$SERVICE_NAME" 2>/dev/null || echo unknown)"
    if [ "$enabled" != "enabled" ]; then
        warn "分层健康检查失败：服务未启用（is-enabled=${enabled}）。"
        warn "查什么：sudo systemctl status ${SERVICE_NAME}；sudo journalctl -u ${SERVICE_NAME} -n 50。"
        return 1
    fi
    # 配置未就绪时不要求 active；只确认 enable 已成功即可。
    is_configured || return 0
    status="$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || echo unknown)"
    if [ "$status" != "active" ]; then
        warn "分层健康检查失败：服务未运行（is-active=${status}）。"
        warn "查什么：sudo journalctl -u ${SERVICE_NAME} -n 50 --no-pager。"
        return 1
    fi
    restarts="$(systemctl show -p NRestarts --value "$SERVICE_NAME" 2>/dev/null || echo 0)"
    sleep 5
    restarts_after="$(systemctl show -p NRestarts --value "$SERVICE_NAME" 2>/dev/null || echo 0)"
    if [ "$restarts" != "$restarts_after" ]; then
        warn "分层健康检查失败：NRestarts 在观察窗内增长（${restarts} → ${restarts_after}），服务在持续重启。"
        warn "查什么：sudo journalctl -u ${SERVICE_NAME} -n 100 --no-pager。"
        return 1
    fi
    exec_status="$(systemctl show -p ExecMainStatus --value "$SERVICE_NAME" 2>/dev/null || echo 0)"
    if [ "$exec_status" = "2" ]; then
        warn "分层健康检查失败：服务主进程以 2 退出（配置/Telegram 凭据致命错误）。"
        warn "systemd 单元里 RestartPreventExitStatus=2 已让它停在 failed 状态，重启无用。"
        warn "请检查 ${CONFIG_FILE} 中的 bot_token / chat_id 是否正确。"
        return 1
    fi
    log "分层健康检查通过。"
    return 0
}

print_install_summary() {
    echo
    log "安装完成。"
    echo "  安装目录 : ${INSTALL_DIR}"
    echo "  配置文件 : ${CONFIG_FILE}"
    echo "  服务名称 : ${SERVICE_NAME}"
    echo
    if is_configured; then
        echo "服务已启动。常用命令："
    else
        echo "下一步：编辑配置并启动服务："
        echo "  sudo -e ${CONFIG_FILE}                      # 填入 bot_token / chat_id 及监控项"
        echo "  sudo systemctl start ${SERVICE_NAME}         # 启动"
    fi
    echo "  sudo systemctl status ${SERVICE_NAME}        # 查看状态"
    echo "  sudo journalctl -u ${SERVICE_NAME} -f        # 跟随日志"
    echo "  sudo systemctl restart ${SERVICE_NAME}       # 改配置后重启"
    echo "  sudo ${INSTALL_DIR}/deploy.sh uninstall      # 卸载"
    echo
}

# cmd_install 的 EXIT 陷阱（R18 / KTD21）：成功失败都清理暂存配置、失败时回滚代际交换。
# 无论 cmd_install 是因 set -e 触发 ERR、被 die 中止还是正常结束都走这里，所以用一个
# 入口统一处理：先看退出码，0 走「删 src.old + 清暂存」成功路径，非 0 走「回滚 src +
# 打印恢复命令 + 清暂存」失败路径。
cmd_install_on_exit() {
    local exit_code=$?
    # 1) 暂存配置成功失败都删（R18）。不存在的文件静默忽略。
    if [ -n "$STAGED_CONFIG" ] && [ -f "$STAGED_CONFIG" ]; then
        rm -f "$STAGED_CONFIG" || warn "删除暂存配置 ${STAGED_CONFIG} 失败，请手动清理。"
    fi
    # 2) 代际交换的收尾或回滚。
    if [ "$DEPLOY_GENERATIONAL_SWAP" = "yes" ] && [ -d "${INSTALL_DIR}/src.old" ]; then
        if [ "$exit_code" -eq 0 ]; then
            rm -rf "${INSTALL_DIR}/src.old" || warn "删除 ${INSTALL_DIR}/src.old 失败。"
        else
            warn "升级失败，把 ${INSTALL_DIR}/src.old 换回 ${INSTALL_DIR}/src。"
            rm -rf "${INSTALL_DIR}/src" 2>/dev/null || true
            mv "${INSTALL_DIR}/src.old" "${INSTALL_DIR}/src" || warn "自动回滚失败，请手动执行下面的恢复命令。"
            warn "手动恢复命令（如自动回滚未生效）："
            warn "  sudo mv ${INSTALL_DIR}/src.old ${INSTALL_DIR}/src    # 如果 src.old 还在"
            warn "  sudo systemctl restart ${SERVICE_NAME}"
            warn "  sudo journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
        fi
    fi
    exit "$exit_code"
}

cmd_install() {
    # 选项解析（形状照 cmd_uninstall 的 while 循环；未知选项一律 die，避免拼错的选项被静默忽略）。
    while [ $# -gt 0 ]; do
        case "$1" in
            --config)
                [ "$#" -ge 2 ] || die "--config 需要一个路径参数（见 $0 help）"
                STAGED_CONFIG="$2"
                shift 2
                ;;
            --overwrite-config)
                OVERWRITE_CONFIG="yes"
                shift
                ;;
            *)
                die "install 未知选项：$1（见 $0 help）"
                ;;
        esac
    done

    require_root install
    check_os
    resolve_python

    # 文件锁互斥（KTD20）：R17 的抗断连重跑会让两个 install 并发，第二个的 rm -rf src
    # 会在第一个的 pip install 正读这棵树时把它抽走。/run 是 tmpfs，重启自动清空。
    # HAWKEYE_INSTALL_LOCK 默认是 /run/hawkeye-install.lock，测试可改到 /tmp 等位置。
    install -d -m 755 "$(dirname "$HAWKEYE_INSTALL_LOCK")" 2>/dev/null || true
    exec 9>"$HAWKEYE_INSTALL_LOCK"
    flock -n 9 || die "另一个安装正在进行中（${HAWKEYE_INSTALL_LOCK}），请等它结束。"

    # EXIT 陷阱从这里开始接管：从此以后任何步骤失败都会走 cmd_install_on_exit。
    trap cmd_install_on_exit EXIT

    install_system_deps
    create_app_user
    deploy_files
    setup_venv
    install_browser
    seed_config
    set_ownership
    write_service
    enable_and_start
    print_install_summary
}

# ---- 打包步骤（在本地开发机运行，无需 root）----

# 暂存目录由 EXIT trap 清理，打包失败也不留垃圾。
STAGE_DIR=""
cleanup_stage() {
    [ -n "$STAGE_DIR" ] || return 0
    rm -rf "$STAGE_DIR"
}

# 从 pyproject.toml 取版本号用于命名部署包。
read_version() {
    local ver
    ver="$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "${SCRIPT_DIR}/pyproject.toml" | head -n1)"
    printf '%s' "${ver:-unknown}"
}

print_package_summary() {
    local archive="$1" pkg_name="$2" size
    size="$(du -h "$archive" | awk '{print $1}')"
    echo
    log "打包完成：${archive}（${size}）"
    echo
    echo "上传并更新 VPS（把 user@vps 换成你的服务器）："
    echo "  scp ${archive} user@vps:/tmp/"
    echo "  ssh user@vps"
    echo "  unzip -q /tmp/${pkg_name}.zip -d /tmp && cd /tmp/${pkg_name}"
    echo "  sudo ./deploy.sh install"
    echo
    echo "install 幂等：保留服务器上已有的 config.toml 与 state.json，配置就绪时自动重启服务。"
    echo
}

cmd_package() {
    [ $# -eq 0 ] || die "package 不接受选项：$1（见 $0 help）"

    # 包语义已搬到 Python（U2）：这里是薄委托。
    # PYTHONPATH 让 -m hawkeye 找到 src/ 下的本地源码；--root 把项目根显式
    # 传进去，避免 packaging 从 __file__ 反推。
    local py
    py="$(command -v python3 || true)"
    [ -n "$py" ] || die "未找到 python3，无法打包。请改用 hawkeye package（需要先 pip install -e .）。"
    log "委托给 hawkeye package 打 zip……"
    PYTHONPATH="${SCRIPT_DIR}/src" "$py" -m hawkeye package --root "${SCRIPT_DIR}" \
        || die "hawkeye package 失败，请改用 hawkeye package 直接调用排查。"
}

# ---- 卸载步骤 ----

stop_service() {
    if systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE_NAME}"; then
        log "停止并停用 ${SERVICE_NAME}……"
        systemctl stop "$SERVICE_NAME" 2>/dev/null || true
        systemctl disable "$SERVICE_NAME" 2>/dev/null || true
    fi
    if [ -f "$SERVICE_PATH" ]; then
        log "删除服务单元 ${SERVICE_PATH}……"
        rm -f "$SERVICE_PATH"
        systemctl daemon-reload
        systemctl reset-failed "$SERVICE_NAME" 2>/dev/null || true
    fi
}

remove_data() {
    # $1: 是否自动确认（yes/no）
    local assume_yes="$1" reply
    if [ ! -d "$INSTALL_DIR" ]; then
        log "安装目录 ${INSTALL_DIR} 不存在，无需清理。"
        return
    fi
    if [ "$assume_yes" != "yes" ]; then
        printf '%s[HawkEye]%s 是否删除安装目录 %s（含 config.toml 密钥与 state.json 状态）？[y/N] ' \
            "$C_WARN" "$C_RESET" "$INSTALL_DIR" >&2
        read -r reply || reply=""
        case "$reply" in
            [yY] | [yY][eE][sS]) ;;
            *) log "已保留 ${INSTALL_DIR}（含配置与状态）。用户 ${APP_USER} 亦保留。"; return ;;
        esac
    fi
    log "删除安装目录 ${INSTALL_DIR}……"
    rm -rf "$INSTALL_DIR"
    if id "$APP_USER" >/dev/null 2>&1; then
        log "删除系统用户 ${APP_USER}……"
        userdel "$APP_USER" 2>/dev/null || warn "删除用户 ${APP_USER} 失败（可能仍有进程占用），可稍后手动 userdel。"
    fi
}

cmd_uninstall() {
    local assume_yes="no" keep_data="no"
    while [ $# -gt 0 ]; do
        case "$1" in
            -y | --yes) assume_yes="yes" ;;
            --keep-data) keep_data="yes" ;;
            *) die "uninstall 未知选项：$1（见 $0 help）" ;;
        esac
        shift
    done

    require_root uninstall
    stop_service
    if [ "$keep_data" = "yes" ]; then
        log "按 --keep-data 保留 ${INSTALL_DIR} 与用户 ${APP_USER}。"
    else
        remove_data "$assume_yes"
    fi
    log "卸载完成。"
}

# ---- 用法 ----

usage() {
    cat <<EOF
HawkEye VPS 一键部署脚本（Debian / Ubuntu）

用法：
  $0 package                          在本地打出 dist/${APP_NAME}-<版本>-<时间戳>.zip 部署包（无需 sudo）
  sudo $0 install                     安装或升级 HawkEye，并注册、启用 systemd 服务
  sudo $0 install --config <路径>      安装时采用上传来的 config.toml（按三态判定处理）
  sudo $0 install --overwrite-config  即使服务器上已有真实配置，也用上传来的顶掉（旧配置先备份）
  sudo $0 uninstall [选项]             停用并删除服务
  $0 help                             显示本帮助

install 选项：
      --config <路径>        采用上传来的 config.toml（路径由调用方提供，本脚本不验证合法性）
      --overwrite-config     即便服务器上已有真实配置也用上传来的顶掉（旧配置先备份为
                             ${INSTALL_DIR}/config.toml.bak.<时间戳>，权限 600）

uninstall 选项：
  -y, --yes        非交互模式，直接删除安装目录与系统用户（含配置/状态）
      --keep-data  只移除 systemd 服务，保留 ${INSTALL_DIR} 与用户 ${APP_USER}

说明：
  - package 只收运行所需文件（src/、pyproject.toml、config.example.toml、README.md、
    deploy.sh），不含本地 config.toml / state.json / 缓存，可放心上传。
  - install 幂等，可重复运行以升级；升级时保留服务器上已有的 config.toml 与 state.json。
  - 不带 --config 时 install 行为与之前完全一致（这是升级的基线）。
  - 带 --config 时按三态判定：远端缺失 → 采用上传的；远端是占位符模板 → 采用上传的且旧配置先备份；
    远端已是真实配置且未给 --overwrite-config → 保留并打印提示（退出码仍 0）。
  - install 同时持 /run/hawkeye-install.lock 互斥，并发或断连重跑的第二次会立刻被挡掉。
  - 首次安装后若 config.toml 仍为模板，服务不会自动启动；填好凭据后
    执行 sudo systemctl start ${SERVICE_NAME}。
EOF
}

main() {
    local cmd="${1:-help}"
    [ $# -gt 0 ] && shift || true
    case "$cmd" in
        package)         cmd_package "$@" ;;
        install)         cmd_install "$@" ;;
        uninstall)       cmd_uninstall "$@" ;;
        help | -h | --help) usage ;;
        *) err "未知命令：${cmd}"; echo; usage; exit 1 ;;
    esac
}

# 仅当作为脚本直接执行时才跑 main；source 进来（测试）时只暴露函数与常量。
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
