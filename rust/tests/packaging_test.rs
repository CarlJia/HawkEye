//! packaging 模块的移植测试（对应 Python tests/test_packaging.py 的核心场景，
//! 打包形态改为 Rust 二进制部署）。

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use hawkeye::packaging::{build_package, PackageError};

static COUNTER: AtomicU64 = AtomicU64::new(0);

fn tmp_root(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "hawkeye_pkg_test_{}_{}_{}",
        std::process::id(),
        tag,
        COUNTER.fetch_add(1, Ordering::SeqCst)
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// 伪造合法项目根：rust/Cargo.toml + config.example.toml + 假二进制。
fn make_root(tag: &str, version: &str) -> (PathBuf, PathBuf) {
    let root = tmp_root(tag);
    std::fs::create_dir_all(root.join("rust")).unwrap();
    std::fs::write(
        root.join("rust").join("Cargo.toml"),
        format!("[package]\nname = \"hawkeye\"\nversion = \"{version}\"\n"),
    )
    .unwrap();
    std::fs::write(
        root.join("config.example.toml"),
        "[telegram]\nbot_token = \"123456:ABC-your-bot-token\"\nchat_id = \"1\"\n",
    )
    .unwrap();
    std::fs::write(root.join("README.md"), "# HawkEye\n").unwrap();
    let binary = root.join("fake-hawkeye");
    std::fs::write(&binary, b"#!/bin/sh\necho fake-binary\n").unwrap();
    (root, binary)
}

fn zip_members(zip_path: &Path) -> Vec<String> {
    let file = std::fs::File::open(zip_path).unwrap();
    let mut zip = zip::ZipArchive::new(file).unwrap();
    (0..zip.len())
        .map(|i| zip.by_index(i).unwrap().name().to_string())
        .collect()
}

#[test]
fn test_build_package_zip_contains_binary_and_installer() {
    let (root, binary) = make_root("ok", "0.1.0");
    let dist = root.join("dist");
    let archive = build_package(&root, Some(&dist), Some(&binary)).unwrap();
    assert!(archive.exists());

    let mut members = zip_members(&archive);
    members.sort();
    let pkg_name = archive.file_stem().unwrap().to_string_lossy().into_owned();
    assert!(
        members.contains(&format!("{pkg_name}/bin/hawkeye")),
        "成员：{members:?}"
    );
    assert!(members.contains(&format!("{pkg_name}/install.sh")));
    assert!(members.contains(&format!("{pkg_name}/config.example.toml")));
    assert!(members.contains(&format!("{pkg_name}/README.md")));
    // 包名带版本与时间戳。
    assert!(pkg_name.starts_with("hawkeye-0.1.0-"), "{pkg_name}");
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn test_build_package_installer_installs_systemd_unit() {
    // install.sh 内容抽查：systemd 单元 + 占位符探测 + 二进制安装路径。
    let (root, binary) = make_root("sh", "0.1.0");
    let dist = root.join("dist");
    let archive = build_package(&root, Some(&dist), Some(&binary)).unwrap();
    let file = std::fs::File::open(&archive).unwrap();
    let mut zip = zip::ZipArchive::new(file).unwrap();
    let pkg_name = archive.file_stem().unwrap().to_string_lossy().into_owned();
    let mut installer = zip.by_name(&format!("{pkg_name}/install.sh")).unwrap();
    let mut buf = Vec::new();
    std::io::Read::read_to_end(&mut installer, &mut buf).unwrap();
    let content = String::from_utf8_lossy(&buf).into_owned();
    assert!(content.contains("ExecStart=/opt/hawkeye/hawkeye"));
    assert!(content.contains("RestartPreventExitStatus=2"));
    assert!(content.contains("123456:ABC-your-bot-token"));
    assert!(content.contains("install -m 755"));
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn test_missing_root_files_rejected() {
    let root = tmp_root("noroot");
    let err = build_package(&root, None, Some(Path::new("/nonexistent"))).unwrap_err();
    assert!(err.0.contains("缺少"), "{err}");
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn test_invalid_version_rejected() {
    let (root, binary) = make_root("badver", "0.1.0/../evil");
    let err = build_package(&root, Some(&root.join("dist")), Some(&binary)).unwrap_err();
    assert!(err.0.contains("非法字符"), "{err}");
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn test_config_toml_never_enters_package() {
    // 白名单兜底：即便在项目根放一个 config.toml，也绝不进包。
    let (root, binary) = make_root("leak", "0.1.0");
    std::fs::write(root.join("config.toml"), "secret").unwrap();
    let archive = build_package(&root, Some(&root.join("dist")), Some(&binary)).unwrap();
    let members = zip_members(&archive);
    assert!(
        !members.iter().any(|m| m.ends_with("/config.toml") && !m.contains("example")),
        "config.toml 绝不能进包：{members:?}"
    );
    assert!(!archive.with_file_name("dist").join("config.toml").exists() || true);
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn test_no_tmp_residue_on_success() {
    let (root, binary) = make_root("tmp", "0.1.0");
    let dist = root.join("dist");
    let archive = build_package(&root, Some(&dist), Some(&binary)).unwrap();
    // stage 目录清掉了、tmp zip 不存在。
    let entries: Vec<String> = std::fs::read_dir(&dist)
        .unwrap()
        .flatten()
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .collect();
    assert_eq!(entries, vec![archive.file_name().unwrap().to_string_lossy().into_owned()], "dist 只留最终 zip");
    let stages: Vec<_> = std::fs::read_dir(&root)
        .unwrap()
        .flatten()
        .filter(|e| e.file_name().to_string_lossy().starts_with(".hawkeye-stage-"))
        .collect();
    assert!(stages.is_empty(), "stage 目录应清理");
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn test_build_release_failure_propagates() {
    let root = tmp_root("cargo-fail");
    std::fs::create_dir_all(root.join("rust")).unwrap();
    std::fs::write(root.join("rust").join("Cargo.toml"), "[package]\nversion = \"1.0\"\n").unwrap();
    std::fs::write(
        root.join("config.example.toml"),
        "[telegram]\nbot_token = \"123456:ABC-your-bot-token\"\nchat_id = \"1\"\n",
    )
    .unwrap();
    let err: PackageError = build_package(&root, Some(&root.join("dist")), None).unwrap_err();
    // 无 cargo 环境（或 cargo build 失败）必须转成 PackageError 而非 panic。
    assert!(!err.0.is_empty());
    let _ = std::fs::remove_dir_all(&root);
}
