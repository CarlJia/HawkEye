#!/usr/bin/env bash
#
# HawkEye 开发环境一键调试启动脚本
#
#   ./dev.sh           # 检查依赖并以 DEBUG 日志启动守护进程
#   ./dev.sh --dry-run # 仅检查依赖，不启动
#   ./dev.sh test      # 运行 pytest
#   ./dev.sh lint       # 运行 ruff check
#   ./dev.sh typecheck # 运行 mypy
#   ./dev.sh browser   # 仅安装 / 校验 Playwright 浏览器
#   ./dev.sh help      # 查看帮助

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---- 彩色输出 ----
RED=$'\033[0;31m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[0;33m'
CYAN=$'\033[0;36m'
BOLD=$'\033[1m'
RESET=$'\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${RESET} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${RESET} $*"; }
log_error() { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
log_step()  { echo -e "${CYAN}[STEP]${RESET} ${BOLD}$*${RESET}"; }

# ---- 依赖检查 ----
check_deps() {
    log_step "检查 Python 环境"
    if ! command -v uv &>/dev/null; then
        log_error "uv 未安装，请先运行: curl -LsSf https://astral.sh/uv/install.sh | sh"
        exit 1
    fi
    log_info "uv $(uv --version | awk '{print $2}')"

    log_step "同步项目依赖"
    uv sync --all-extras
    log_info "依赖就绪"
}

check_browser() {
    log_step "检查 Playwright 浏览器"
    # dry-run 输出含 "Install location" 表示已安装；有 "Download url" 无 "Install location" 表示需下载。
    local output
    output=$(uv run python -m playwright install --dry-run 2>&1)
    if echo "$output" | grep -q "^  Install location:"; then
        log_info "Chromium 已就绪"
    else
        log_warn "浏览器未安装，正在下载 Chromium……"
        uv run python -m playwright install chromium
        log_info "Chromium 安装完成"
    fi
}

# ---- 命令分发 ----
CMD="${1:-run}"
shift || true  # 跳过已捕获的命令名，剩余参数透传给子命令

case "$CMD" in
    --dry-run)
        log_info "dry-run 模式，仅检查依赖"
        check_deps
        check_browser
        log_info "检查完毕，可正常运行"
        ;;

    test)
        check_deps
        log_step "运行测试"
        uv run pytest -v tests/ "$@"
        ;;

    lint)
        check_deps
        log_step "运行 ruff"
        uv run ruff check src/ tests/
        ;;

    typecheck)
        check_deps
        log_step "运行 mypy"
        uv run mypy src/
        ;;

    browser)
        check_deps
        check_browser
        ;;

    run|daemon|"")
        check_deps
        check_browser

        log_step "启动 HawkEye 守护进程（DEBUG 模式）"
        log_info "日志等级: DEBUG"
        log_info "按 Ctrl+C 停止"
        echo
        uv run hawkeye -v
        ;;

    help|-h|--help)
        echo -e "${BOLD}HawkEye 开发环境脚本${RESET}"
        echo
        echo -e "  ${BOLD}./dev.sh${RESET}           一键检查依赖并启动调试模式"
        echo -e "  ${BOLD}./dev.sh --dry-run${RESET}  仅检查依赖，不启动"
        echo -e "  ${BOLD}./dev.sh test${RESET}       运行 pytest"
        echo -e "  ${BOLD}./dev.sh lint${RESET}       运行 ruff check"
        echo -e "  ${BOLD}./dev.sh typecheck${RESET}  运行 mypy"
        echo -e "  ${BOLD}./dev.sh browser${RESET}    仅安装 / 校验 Playwright 浏览器"
        echo -e "  ${BOLD}./dev.sh help${RESET}       显示本帮助"
        echo
        ;;

    *)
        log_error "未知命令: $CMD"
        echo "运行 ./dev.sh help 查看用法"
        exit 1
        ;;
esac
