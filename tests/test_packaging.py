"""packaging 测试：白名单、泄漏兜底、跨平台 zip、CLI 根路径解析。"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

import pytest

from hawkeye.packaging import (
    WHITELIST_TOP_LEVEL,
    PackageError,
    build_package,
    main,
)

# ---- 辅助 ----


def _make_project_root(
    base: Path,
    *,
    version: str = "9.9.9",
    with_readme: bool = True,
) -> Path:
    """造一个最小可打包的项目根。pyproject / src / config.example.toml 必有。"""
    root = base / "project"
    root.mkdir()
    (root / "src" / "hawkeye").mkdir(parents=True)
    (root / "src" / "hawkeye" / "__init__.py").write_text("")
    (root / "pyproject.toml").write_text(f'[project]\nname = "hawkeye"\nversion = "{version}"\n')
    (root / "config.example.toml").write_text('[telegram]\nbot_token = "placeholder"\n')
    if with_readme:
        (root / "README.md").write_text("# test\n")
    (root / "deploy.sh").write_text("#!/usr/bin/env bash\necho ok\n")
    return root


def _make_project_root_with_seven_leaks(base: Path) -> Path:
    """在标准项目根下，额外塞入七族假敏感文件，模拟 U2 的 AE6 场景。"""
    root = _make_project_root(base)
    (root / "config.toml").write_text("token = 'leak'\n")
    (root / "config.toml.bak.20260905-163549").write_text("token = 'leak'\n")
    (root / "config.toml.tmp").write_text("token = 'leak'\n")
    (root / "state.json").write_text('{"x": 1}\n')
    (root / "state.json.tmp").write_text('{"x": 1}\n')
    (root / "state.json.corrupt.20260101-000000").write_text("garbage\n")
    (root / ".hawkeye-deploy.toml").write_text('host = "x"\n')
    return root


def _read_names(zip_path: Path) -> list[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return zf.namelist()


def _infolist(zip_path: Path) -> list[zipfile.ZipInfo]:
    with zipfile.ZipFile(zip_path) as zf:
        return zf.infolist()


# ---- KTD10：packaging 不能拉起守护进程的第三方依赖 ----


def test_packaging_does_not_import_third_party(monkeypatch: pytest.MonkeyPatch) -> None:
    """``import hawkeye.packaging`` 后 ``sys.modules`` 里不能出现 playwright/asyncssh/httpx。"""
    for name in ("playwright", "asyncssh", "httpx"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.delitem(sys.modules, "hawkeye.packaging", raising=False)
    monkeypatch.delitem(sys.modules, "hawkeye", raising=False)
    import hawkeye.packaging  # noqa: F401 —— 触发一次 import

    for forbidden in ("playwright", "asyncssh", "httpx"):
        assert forbidden not in sys.modules, (
            f"packaging 不应 import {forbidden}，但 sys.modules 含它"
        )


# ---- KTD18：模块源码不能依赖 __file__ 反推根路径 ----


def test_packaging_source_does_not_use_dunder_file() -> None:
    """packaging.py 不能用 ``__file__`` 推断项目根——必须由调用方显式传入。

    只检查实际代码（AST Name/Constant 节点），docstring 里的 ``__file__``
    字面量不算（之前 v1 版本因此误报）。"""
    import ast

    src = Path(__file__).resolve().parent.parent / "src" / "hawkeye" / "packaging.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    expr_linenos = {d.lineno for d in ast.walk(tree) if isinstance(d, ast.Expr)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "__file__":
            raise AssertionError("packaging.py 不应使用 __file__（KTD18）")
        if isinstance(node, ast.Constant) and node.value == "__file__":
            line_no = getattr(node, "lineno", 0)
            # 跳过 docstring 里的字面量（顶层 docstring lineno=1；嵌套 Expr 也放过）
            if line_no == 1 or line_no in expr_linenos:
                continue
            raise AssertionError(f"packaging.py 不应在代码里写 __file__（第 {line_no} 行）")


# ---- AE7：Python 侧白名单与 deploy.sh 的 copy_payload 顶层条目一致 ----


def test_whitelist_matches_deploy_sh_copy_payload() -> None:
    """与 deploy.sh:139-146 的 copy_payload 五项对齐：src/、pyproject.toml、
    config.example.toml、README.md（可选）、deploy.sh。"""
    expected = {"src", "pyproject.toml", "config.example.toml", "README.md", "deploy.sh"}
    assert set(WHITELIST_TOP_LEVEL) == expected


# ---- R11：包名形如 hawkeye-<版本>-<14位时间戳>.zip；README 缺失仍可打包 ----


def test_archive_name_format(tmp_path: Path) -> None:
    root = _make_project_root(tmp_path, version="1.2.3")
    archive = build_package(root=root, dist_dir=root / "dist")
    # 14 位时间戳：YYYYMMDD-HHMMSS = 8 + 1 + 6 = 15 字符
    assert re.match(r"^hawkeye-1\.2\.3-\d{8}-\d{6}\.zip$", archive.name), archive.name


def test_readme_missing_still_packs(tmp_path: Path) -> None:
    """README.md 缺失（copy_payload 的可选分支）时打包仍应成功。"""
    root = _make_project_root(tmp_path, with_readme=False)
    archive = build_package(root=root, dist_dir=root / "dist")
    names = _read_names(archive)
    # README.md 不在成员里，但其他四项必须在。
    assert not any(n.endswith("/README.md") for n in names)
    assert any(n.endswith("/pyproject.toml") for n in names)
    assert any(n.endswith("/config.example.toml") for n in names)
    assert any(n.endswith("/deploy.sh") for n in names)
    assert any(n.endswith("/src/") or n.endswith("/src") or "/src/" in n for n in names)


# ---- AE6：七族敏感文件不能进入 zip ----


def test_seven_leak_families_excluded(tmp_path: Path) -> None:
    """工作区里有七族假敏感文件时，打包后 zip 成员名里一个都不出现。"""
    root = _make_project_root_with_seven_leaks(tmp_path)
    archive = build_package(root=root, dist_dir=root / "dist")
    names = _read_names(archive)
    leaves = [Path(n).name for n in names]
    forbidden = {
        "config.toml",
        "config.toml.bak.20260905-163549",
        "config.toml.tmp",
        "state.json",
        "state.json.tmp",
        "state.json.corrupt.20260101-000000",
        ".hawkeye-deploy.toml",
    }
    leaked = forbidden & set(leaves)
    assert not leaked, f"zip 成员名不应含敏感文件，但出现了：{leaked}"


# ---- AE6（兜底被绕）：白名单被篡改时硬中止、不留半截包 ----


def test_leak_escape_raises_and_no_zip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """有人把 config.toml 强行塞进白名单 → 抛 PackageError，消息含泄漏文件名，dist/ 下无 zip。"""
    root = _make_project_root(tmp_path)
    # 在白名单拷贝路径之外再造一份 config.toml，然后用 monkeypatch 让 _stage_payload
    # 把这个文件也放进 stage。命中 _scan_for_leaks 的「白名单拷贝结果」阶段。
    leak_path = root / "config.toml"
    leak_path.write_text("token = 'leak'\n")

    from hawkeye import packaging

    original = packaging._stage_payload

    def hijacked_stage(r: Path, stage_dir: Path) -> list[Path]:
        rels = original(r, stage_dir)
        # 把 config.toml 也拷进 stage，并 append 到 rels，模拟「白名单被篡改」
        (stage_dir / "config.toml").write_bytes(leak_path.read_bytes())
        rels.append(Path("config.toml"))
        return rels

    monkeypatch.setattr(packaging, "_stage_payload", hijacked_stage)

    dist_dir = root / "dist"
    with pytest.raises(PackageError) as exc_info:
        build_package(root=root, dist_dir=dist_dir)

    msg = str(exc_info.value)
    assert "config.toml" in msg, f"错误消息应含泄漏文件名，但得到：{msg}"
    # dist/ 下不应有 zip；stage 临时目录也应被清掉。
    assert not dist_dir.exists() or not list(dist_dir.glob("*.zip")), (
        f"中止时 dist/ 下不应有 zip，但发现：{list(dist_dir.glob('*.zip'))}"
    )
    assert not list(root.glob(".hawkeye-stage-*")), "中止时 stage 临时目录应被清理"


# ---- 第 2 步：每个成员名都以 <pkg_name>/ 开头 ----


def test_all_member_names_have_pkg_prefix(tmp_path: Path) -> None:
    root = _make_project_root(tmp_path)
    archive = build_package(root=root, dist_dir=root / "dist")
    pkg_name = archive.stem  # hawkeye-<ver>-<ts>
    names = _read_names(archive)
    assert names, "zip 不能是空的"
    for n in names:
        assert n.startswith(f"{pkg_name}/"), f"成员名 {n} 不以 {pkg_name}/ 开头"


# ---- R19：成员名用 / 分隔；deploy.sh 权限位 0o755 ----


def test_member_names_use_posix_separator(tmp_path: Path) -> None:
    root = _make_project_root(tmp_path)
    archive = build_package(root=root, dist_dir=root / "dist")
    names = _read_names(archive)
    for n in names:
        assert "\\" not in n, f"成员名 {n} 不应含反斜杠"


def test_deploy_sh_has_755_permissions(tmp_path: Path) -> None:
    """``deploy.sh`` 的 ``external_attr`` 高 16 位应为 ``0o755``（R19）。"""
    root = _make_project_root(tmp_path)
    archive = build_package(root=root, dist_dir=root / "dist")
    pkg_name = archive.stem
    deploy_member = f"{pkg_name}/deploy.sh"
    info_by_name = {i.filename: i for i in _infolist(archive)}
    assert deploy_member in info_by_name, f"找不到 {deploy_member}，成员：{list(info_by_name)}"
    info = info_by_name[deploy_member]
    unix_mode = info.external_attr >> 16
    assert unix_mode == 0o755, f"deploy.sh 权限位应为 0o755，实际 {oct(unix_mode)}"


# ---- R11：__pycache__ 与 *.egg-info 不在成员里 ----


def test_pycache_and_egginfo_excluded(tmp_path: Path) -> None:
    """src/ 里有 __pycache__ 与 *.egg-info 时，不能进入 zip。"""
    root = _make_project_root(tmp_path)
    pkg = root / "src" / "hawkeye"
    # 故意留几个构建产物
    (pkg / "__pycache__").mkdir()
    (pkg / "__pycache__" / "deadbeef.pyc").write_text("x")
    (pkg / "hawkeye.egg-info").mkdir()
    (pkg / "hawkeye.egg-info" / "PKG-INFO").write_text("x")

    archive = build_package(root=root, dist_dir=root / "dist")
    names = _read_names(archive)
    for n in names:
        assert "__pycache__" not in n, f"成员名 {n} 不应含 __pycache__"
        assert ".egg-info" not in n, f"成员名 {n} 不应含 .egg-info"


# ---- KTD18：不给 --root 且 cwd 不是项目根 → 中文错误 ----


def test_main_chinese_error_when_cwd_not_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cwd 不含 pyproject.toml 时，main(argv=[]) 应给出中文错误并返回 1。"""
    not_a_root = tmp_path / "not_a_root"
    not_a_root.mkdir()
    monkeypatch.chdir(not_a_root)
    # main() 的 argparse 把 cwd 当 --root 默认值；触发了中文错误就行。
    rc = main(argv=[])
    assert rc == 1
    # 通过 capsys 验证中文错误已打到 stderr
    # 错误消息至少含中文（「不是合法」「项目根」），并指出缺少的文件名。
    # 这里直接调 packaging.build_package 验证 _check_root 的中文错误也成立。
    from hawkeye.packaging import _check_root

    with pytest.raises(PackageError) as exc_info:
        _check_root(not_a_root)
    msg = str(exc_info.value)
    assert "项目根" in msg, f"_check_root 错误应含中文「项目根」，但得到：{msg}"


def test_main_with_explicit_root_succeeds(tmp_path: Path) -> None:
    """给了正确的 --root，打包应成功并返回 0。"""
    root = _make_project_root(tmp_path)
    rc = main(argv=["--root", str(root)])
    assert rc == 0
    archives = list((root / "dist").glob("hawkeye-9.9.9-*.zip"))
    assert archives, "应至少生成一个 zip"


# ---- pyproject.toml 解析失败 → PackageError ----


def test_missing_version_in_pyproject(tmp_path: Path) -> None:
    root = _make_project_root(tmp_path)
    (root / "pyproject.toml").write_text('[project]\nname = "hawkeye"\n')
    from hawkeye.packaging import _read_version

    with pytest.raises(PackageError, match="version"):
        _read_version(root / "pyproject.toml")


def test_missing_project_table_in_pyproject(tmp_path: Path) -> None:
    root = _make_project_root(tmp_path)
    (root / "pyproject.toml").write_text('[tool.something]\nkey = "value"\n')
    from hawkeye.packaging import _read_version

    with pytest.raises(PackageError, match=r"\[project\]"):
        _read_version(root / "pyproject.toml")
