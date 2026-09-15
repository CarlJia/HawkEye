//! 部署包构造：把 HawkEye（Rust 版）打成 `dist/hawkeye-<版本>-<时间戳>.zip`。
//!
//! 包内容为**Rust 二进制部署**形态：`bin/hawkeye`（release 二进制）、
//! `config.example.toml`、`install.sh`（远端 systemd 安装脚本）、可选
//! `README.md`。VPS 上不再需要 Python / Playwright 环境，仅需一个
//! Chromium（install.sh 会探测并尝试安装）。
//!
//! 泄漏兜底与 Python 版同源：白名单收集出的文件列表与最终 zip 成员名两侧都扫，
//! 命中七族敏感文件名（大小写不敏感）就中止并不生成 zip（写到临时路径，
//! 校验通过后再 rename 到 `dist/`，失败时清掉 stage 与临时 zip 不留残骸）。

use std::path::{Path, PathBuf};

use chrono::Local;

#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub struct PackageError(pub String);

/// config.example.toml 中 telegram.bot_token 的占位值；与 deploy 的硬闸门共用。
pub const PLACEHOLDER_TOKEN: &str = "123456:ABC-your-bot-token";

/// 远端安装脚本（root 运行；幂等；与 Python 版 deploy.sh 的 install 语义对应）。
const INSTALL_SH: &str = r##"#!/bin/bash
# HawkEye Rust 版安装脚本（root 运行；幂等；保留已有 config.toml 与 state.json）
set -e
DEST=/opt/hawkeye
SERVICE=hawkeye.service

install_cmd() {
    local config_arg="" overwrite=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --config) config_arg="$2"; shift 2 ;;
            --overwrite-config) overwrite=1; shift ;;
            *) shift ;;
        esac
    done

    SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

    # 1) 目标目录
    mkdir -p "$DEST"

    # 2) 二进制：旧版备份后替换（回滚用）
    if [ -f "$DEST/hawkeye" ]; then
        cp -f "$DEST/hawkeye" "$DEST/hawkeye.old"
    fi
    install -m 755 "$SRC_DIR/bin/hawkeye" "$DEST/hawkeye"

    # 3) 配置三态处理
    if [ -n "$config_arg" ]; then
        if [ -f "$DEST/config.toml" ] && [ "$overwrite" -eq 0 ]; then
            cp -f "$config_arg" "$DEST/config.toml.incoming"
            echo "已保留现有 $DEST/config.toml（新配置存为 config.toml.incoming）"
        else
            if [ -f "$DEST/config.toml" ]; then
                cp -f "$DEST/config.toml" "$DEST/config.toml.bak.$(date +%Y%m%d-%H%M%S)"
            fi
            install -m 600 "$config_arg" "$DEST/config.toml"
        fi
    fi
    [ -f "$DEST/config.toml" ] || install -m 600 "$SRC_DIR/config.example.toml" "$DEST/config.toml"

    # 4) Chromium（headless 抓取需要；已装任何一种都跳过；装不上只告警不阻断）
    if ! command -v chromium >/dev/null 2>&1 \
       && ! command -v chromium-browser >/dev/null 2>&1 \
       && ! command -v google-chrome >/dev/null 2>&1 \
       && ! command -v google-chrome-stable >/dev/null 2>&1; then
        if command -v apt-get >/dev/null 2>&1; then
            DEBIAN_FRONTEND=noninteractive apt-get install -y -q chromium \
              || DEBIAN_FRONTEND=noninteractive apt-get install -y -q chromium-browser \
              || echo "警告：Chromium 安装失败；守护进程将找不到浏览器而退出"
        else
            echo "警告：未检测到 Chromium 且无 apt-get；请手动安装"
        fi
    fi

    # 5) systemd 单元（RestartPreventExitStatus=2 与守护进程退出码契约一致）
    cat > /etc/systemd/system/$SERVICE <<'UNIT'
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
RestartPreventExitStatus=2

[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
    systemctl enable $SERVICE

    # 6) 配置就绪（非占位符）才启动/重启
    if grep -qF '123456:ABC-your-bot-token' "$DEST/config.toml"; then
        echo "config.toml 仍是模板，未启动服务；配置后：systemctl start $SERVICE"
    else
        systemctl restart $SERVICE
    fi
    echo "install 完成：$DEST/hawkeye"
}

case "${1:-}" in
    install) shift; install_cmd "$@" ;;
    *) echo "用法： $0 install [--config <path>] [--overwrite-config]"; exit 2 ;;
esac
"##;

// 七族敏感文件名（大小写不敏感）：白名单与 zip 成员名两侧都扫。
fn is_leaked(leaf: &str) -> bool {
    let lower = leaf.to_lowercase();
    let prefix_hits = [
        "config.toml.bak.",
        "state.json.corrupt.",
    ];
    lower == "config.toml"
        || lower == "config.toml.tmp"
        || lower == "state.json"
        || lower == "state.json.tmp"
        || lower == ".hawkeye-deploy.toml"
        || prefix_hits.iter().any(|p| lower.starts_with(p))
}

fn scan_for_leaks(rels: &[String], where_: &str) -> Result<(), PackageError> {
    let leaked: Vec<&str> = rels
        .iter()
        .filter(|rel| {
            let leaf = rel.rsplit('/').next().unwrap_or(rel);
            is_leaked(leaf)
        })
        .map(|s| s.as_str())
        .collect();
    if leaked.is_empty() {
        return Ok(());
    }
    Err(PackageError(format!(
        "{where_}出现敏感文件（{}），已中止打包。这些文件可能含明文 Telegram 凭据或运行状态，绝不能进入部署包。",
        leaked.join("、")
    )))
}

/// 从 rust/Cargo.toml 的 [package].version 读版本号。
fn read_version(cargo_toml: &Path) -> Result<String, PackageError> {
    let text = std::fs::read_to_string(cargo_toml)
        .map_err(|e| PackageError(format!("无法读取 {}：{e}", cargo_toml.display())))?;
    let table: toml::Table = toml::from_str(&text)
        .map_err(|e| PackageError(format!("{} 不是合法 TOML：{e}", cargo_toml.display())))?;
    let version = table
        .get("package")
        .and_then(|p| p.get("version"))
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if version.is_empty() {
        return Err(PackageError(format!(
            "{} 的 [package].version 不是非空字符串", cargo_toml.display()
        )));
    }
    Ok(version.to_string())
}

fn timestamp() -> String {
    Local::now().format("%Y%m%d-%H%M%S").to_string()
}

fn check_root(root: &Path) -> Result<(), PackageError> {
    let mut missing = Vec::new();
    if !root.join("rust").join("Cargo.toml").is_file() {
        missing.push("rust/Cargo.toml".to_string());
    }
    if !root.join("config.example.toml").is_file() {
        missing.push("config.example.toml".to_string());
    }
    if !missing.is_empty() {
        return Err(PackageError(format!(
            "{} 不是合法的 HawkEye 项目根，缺少：{}。请在项目根目录下运行，或用 --root 指定项目根路径。",
            root.display(),
            missing.join("、")
        )));
    }
    Ok(())
}

/// 拒绝路径下的任何符号链接——跟随符号链接会把仓库外文件复制进包，
/// 绕过白名单与七族敏感兜底。
fn reject_symlinks(dir: &Path) -> Result<(), PackageError> {
    fn walk(dir: &Path) -> Result<(), PackageError> {
        for entry in std::fs::read_dir(dir).map_err(|e| PackageError(format!("读目录失败：{e}")))? {
            let entry = entry.map_err(|e| PackageError(format!("读目录失败：{e}")))?;
            let path = entry.path();
            if entry.file_type().map(|t| t.is_symlink()).unwrap_or(false) {
                return Err(PackageError(format!(
                    "{} 含符号链接 {}，拒绝打包以避免任意文件外泄",
                    dir.display(),
                    path.display()
                )));
            }
            if path.is_dir() {
                walk(&path)?;
            }
        }
        Ok(())
    }
    walk(dir)
}

/// 构建发布二进制：在 root/rust 下 `cargo build --release`，返回二进制路径。
fn build_release(root: &Path) -> Result<PathBuf, PackageError> {
    let cargo_dir = root.join("rust");
    let output = std::process::Command::new("cargo")
        .arg("build")
        .arg("--release")
        .current_dir(&cargo_dir)
        .output()
        .map_err(|e| PackageError(format!("无法启动 cargo（需要本机装有 Rust 工具链）：{e}")))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(PackageError(format!(
            "cargo build --release 失败：{}",
            stderr.lines().take(5).collect::<Vec<_>>().join("; ")
        )));
    }
    Ok(cargo_dir.join("target").join("release").join("hawkeye"))
}

/// 把项目根打成 zip；返回 zip 路径。
///
/// `binary`：已构建好的二进制路径；传 `None` 时自动 `cargo build --release`
/// （测试传现成文件避免真实编译）。
///
/// 流程：检查根 → 读版本 → 构建二进制 → stage 白名单 → 扫白名单 →
/// 写临时 zip → 扫成员名 → rename 到 `dist/`。任一步失败清掉临时产物。
pub fn build_package(
    root: &Path,
    dist_dir: Option<&Path>,
    binary: Option<&Path>,
) -> Result<PathBuf, PackageError> {
    let root = root
        .canonicalize()
        .map_err(|e| PackageError(format!("项目根不存在：{e}")))?;
    check_root(&root)?;
    let version = read_version(&root.join("rust").join("Cargo.toml"))?;
    if !version
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
    {
        return Err(PackageError(format!(
            "Cargo.toml 的 [package].version={version:?} 含非法字符，仅允许字母、数字、点、下划线、连字符（防止 zip 路径穿越）"
        )));
    }
    let pkg_name = format!("hawkeye-{version}-{}", timestamp());
    let dist_dir = dist_dir
        .map(|d| d.to_path_buf())
        .unwrap_or_else(|| root.join("dist"));
    std::fs::create_dir_all(&dist_dir).map_err(|e| PackageError(format!("建 dist 失败：{e}")))?;
    let archive = dist_dir.join(format!("{pkg_name}.zip"));
    let tmp_archive = dist_dir.join(format!(".{pkg_name}.tmp"));

    let stage_dir = root.join(format!(".hawkeye-stage-{pkg_name}"));
    let _ = std::fs::remove_dir_all(&stage_dir);
    std::fs::create_dir_all(stage_dir.join("bin"))
        .map_err(|e| PackageError(format!("建 stage 失败：{e}")))?;

    let result = (|| -> Result<PathBuf, PackageError> {
        // stage 白名单：bin/hawkeye、config.example.toml、install.sh、可选 README.md
        let binary_path = match binary {
            Some(p) => p.to_path_buf(),
            None => build_release(&root)?,
        };
        if !binary_path.is_file() {
            return Err(PackageError(format!(
                "二进制不存在：{}（测试场景请显式传入）", binary_path.display()
            )));
        }
        reject_symlinks(&stage_dir)?;
        std::fs::copy(&binary_path, stage_dir.join("bin").join("hawkeye"))
            .map_err(|e| PackageError(format!("复制二进制失败：{e}")))?;
        std::fs::copy(root.join("config.example.toml"), stage_dir.join("config.example.toml"))
            .map_err(|e| PackageError(format!("复制 config.example.toml 失败：{e}")))?;
        std::fs::write(stage_dir.join("install.sh"), INSTALL_SH)
            .map_err(|e| PackageError(format!("写 install.sh 失败：{e}")))?;
        let mut rels = vec![
            "bin/hawkeye".to_string(),
            "config.example.toml".to_string(),
            "install.sh".to_string(),
        ];
        let readme = root.join("README.md");
        if readme.is_file() {
            std::fs::copy(&readme, stage_dir.join("README.md"))
                .map_err(|e| PackageError(format!("复制 README.md 失败：{e}")))?;
            rels.push("README.md".to_string());
        }

        // 兜底第一道：白名单 leaf 扫描。
        scan_for_leaks(&rels, "白名单拷贝结果")?;

        // zip 成员名 = <pkg_name>/<rel>；第二道：成员名扫描。
        let members: Vec<String> = rels.iter().map(|rel| format!("{pkg_name}/{rel}")).collect();
        scan_for_leaks(
            &members.iter().map(|m| m.trim_start_matches(&format!("{pkg_name}/")).to_string()).collect::<Vec<_>>(),
            "zip 成员名",
        )?;

        // 写临时 zip，全部通过后再 rename。
        let _ = std::fs::remove_file(&tmp_archive);
        write_zip(&tmp_archive, &members, &stage_dir)?;
        std::fs::rename(&tmp_archive, &archive).map_err(|e| PackageError(format!("移动 zip 失败：{e}")))?;
        Ok(archive)
    })();

    let _ = std::fs::remove_dir_all(&stage_dir);
    let _ = std::fs::remove_file(&tmp_archive);
    result
}

fn write_zip(tmp_zip: &Path, members: &[String], stage_dir: &Path) -> Result<(), PackageError> {
    use std::io::Write;
    let file = std::fs::File::create(tmp_zip)
        .map_err(|e| PackageError(format!("创建临时 zip 失败：{e}")))?;
    let mut zip = zip::ZipWriter::new(file);
    let options: zip::write::SimpleFileOptions = zip::write::SimpleFileOptions::default()
        .compression_method(zip::CompressionMethod::Deflated)
        .unix_permissions(0o755);
    for member in members {
        let rel = member.split_once('/').map(|(_, r)| r).unwrap_or(member);
        let abs = stage_dir.join(rel);
        let data = std::fs::read(&abs).map_err(|e| PackageError(format!("读 {} 失败：{e}", abs.display())))?;
        zip.start_file(member.as_str(), options)
            .map_err(|e| PackageError(format!("写 zip 成员 {member} 失败：{e}")))?;
        zip.write_all(&data)
            .map_err(|e| PackageError(format!("写 zip 成员 {member} 失败：{e}")))?;
    }
    zip.finish().map_err(|e| PackageError(format!("收尾 zip 失败：{e}")))?;
    Ok(())
}

fn format_size(size: u64) -> String {
    if size < 1024 {
        format!("{size}B")
    } else if size < 1024 * 1024 {
        format!("{:.1}K", size as f64 / 1024.0)
    } else {
        format!("{:.1}M", size as f64 / (1024.0 * 1024.0))
    }
}

/// 打包完成摘要（与 Python 版 cmd_package 的人工上传指引对应）。
pub fn print_summary(archive: &Path, pkg_name: &str) {
    let size = std::fs::metadata(archive)
        .map(|m| format_size(m.len()))
        .unwrap_or_else(|_| "?".into());
    println!();
    println!("[HawkEye] 打包完成：{}（{size}）", archive.display());
    println!();
    println!("上传并更新 VPS（把 user@vps 换成你的服务器）：");
    println!("  scp {} user@vps:/tmp/", archive.display());
    println!("  ssh user@vps");
    println!("  unzip -q /tmp/{pkg_name}.zip -d /tmp && cd /tmp/{pkg_name}");
    println!("  sudo ./install.sh install");
    println!();
    println!("install 幂等：保留服务器上已有的 config.toml 与 state.json，配置就绪时自动重启服务。");
    println!("下一步推荐：hawkeye deploy（同一台机器免密 SSH 时直接复用，无需手动 scp）。");
    println!();
}
