"""部署包构造：把 HawkEye 项目根打成 ``dist/hawkeye-<版本>-<时间戳>.zip``。

与 :file:`deploy.sh` 的 ``cmd_package`` 语义一致：白名单五项
（``src/``、``pyproject.toml``、``config.example.toml``、可选 ``README.md``、
``deploy.sh``），剥 ``__pycache__`` / ``*.egg-info``。泄漏兜底比 shell 版
更严（KTD11 / R12）：白名单收集出的文件列表与最终 zip 成员名两侧都扫，
命中七族敏感文件名（``config.toml`` / ``config.toml.bak.*`` /
``config.toml.tmp`` / ``state.json`` / ``state.json.tmp`` /
``state.json.corrupt.*`` / ``.hawkeye-deploy.toml``）就抛并不生成 zip
（写到临时路径，校验通过后再 rename 到 ``dist/``，失败时清掉 stage 与
临时 zip 不留残骸）。

只依赖标准库（``pathlib`` / ``shutil`` / ``zipfile`` / ``re`` / ``tomllib``），
不 import ``playwright`` / ``asyncssh`` / ``httpx``，避免 ``hawkeye package``
路径拉起守护进程的第三方依赖（KTD10）。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tomllib
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

# 七族敏感文件名（KTD11 / R12）：白名单与 zip 成员名两侧都扫。
# 后三族是既有代码会留下的残留（configedit.py:262、state.py:92,110）；
# config.toml.tmp 含明文 token；.hawkeye-deploy.toml 是 U5 引入的新族。
_LEAKED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^config\.toml$"),
    re.compile(r"^config\.toml\.bak\..+$"),
    re.compile(r"^config\.toml\.tmp$"),
    re.compile(r"^state\.json$"),
    re.compile(r"^state\.json\.tmp$"),
    re.compile(r"^state\.json\.corrupt\..+$"),
    re.compile(r"^\.hawkeye-deploy\.toml$"),
)

# 与 deploy.sh 的 copy_payload 完全对齐的顶层白名单（README.md 可选）。
# 公开为常量供测试断言「Python 侧白名单与 shell 侧 copy_payload 一致」（AE7）。
WHITELIST_TOP_LEVEL: tuple[str, ...] = (
    "src",
    "pyproject.toml",
    "config.example.toml",
    "README.md",  # 可选：根下不存在时跳过（与 deploy.sh copy_payload 一致）
    "deploy.sh",
)


class PackageError(Exception):
    """打包过程中可恢复的错误：根目录不合法、版本号取不到、泄漏兜底命中。"""


def _read_version(pyproject: Path) -> str:
    """从 ``pyproject.toml`` 的 ``[project].version`` 读版本号。"""
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError as e:
        raise PackageError(f"无法读取 {pyproject}：{e}") from e
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise PackageError(f"{pyproject} 不是合法 TOML：{e}") from e
    project = data.get("project")
    if not isinstance(project, dict):
        raise PackageError(f"{pyproject} 缺少 [project] 表")
    version = project.get("version")
    if not isinstance(version, str) or not version:
        raise PackageError(f"{pyproject} 的 [project].version 不是非空字符串")
    return version


def _timestamp() -> str:
    """本地时区、14 位时间戳，形如 ``20260905-174022``。"""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _check_root(root: Path) -> None:
    """断言 ``root`` 是合法的项目根：pyproject.toml / src/ / config.example.toml 都在。

    项目根**必须显式传入**：本模块不依赖 ``__file__`` 反推位置（KTD18），
    调用方负责给出正确的根路径（CLI 默认是 ``Path.cwd()``）。
    """
    missing: list[str] = []
    for name in ("pyproject.toml", "config.example.toml"):
        if not (root / name).is_file():
            missing.append(name)
    if not (root / "src").is_dir():
        missing.append("src/")
    if missing:
        joined = "、".join(missing)
        raise PackageError(
            f"{root} 不是合法的 HawkEye 项目根，缺少：{joined}。"
            "请在项目根目录下运行，或用 --root 指定项目根路径。"
        )


def _strip_build_artifacts(src: Path) -> None:
    """在打包前清掉 ``__pycache__`` 与 ``*.egg-info``（与 deploy.sh 一致）。"""
    for pattern in ("__pycache__", "*.egg-info"):
        for entry in src.rglob(pattern):
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            elif entry.is_file():
                entry.unlink(missing_ok=True)


def _reject_symlinks(src: Path) -> None:
    """拒绝 src/ 下的任何符号链接。

    ``shutil.copytree`` 默认 ``symlinks=False``，会跟随符号链接，把目标文件
    的内容直接写到 stage。攻击者可借此把仓库外任意文件（如 ``/etc/passwd``）
    复制进包，绕过白名单与七族敏感兜底（finding #2）。
    """
    for entry in src.rglob("*"):
        if entry.is_symlink():
            raise PackageError(f"src/ 含符号链接 {entry}，拒绝打包以避免任意文件外泄")


def _stage_payload(root: Path, stage_dir: Path) -> list[Path]:
    """在 ``stage_dir`` 下复刻 ``copy_payload`` 的白名单，返回相对路径列表。"""
    src_root = root / "src"
    _reject_symlinks(src_root)
    rels: list[Path] = []
    shutil.copytree(src_root, stage_dir / "src")
    rels.append(Path("src"))
    for name in ("pyproject.toml", "config.example.toml"):
        shutil.copy2(root / name, stage_dir / name)
        rels.append(Path(name))
    readme = root / "README.md"
    if readme.is_file():
        shutil.copy2(readme, stage_dir / "README.md")
        rels.append(Path("README.md"))
    shutil.copy2(root / "deploy.sh", stage_dir / "deploy.sh")
    rels.append(Path("deploy.sh"))
    return rels


def _is_leaked(leaf: str) -> bool:
    """若 ``leaf`` 命中七族敏感文件名模式之一，返回 True。

    大小写不敏感匹配（finding #7）：macOS 默认 APFS 与 Windows NTFS
    都是 case-insensitive 文件系统，若只比 ``config.toml`` 而放过
    ``Config.toml``，攻击者即可在大写重名文件下绕过兜底。
    """
    lower = leaf.lower()
    return any(pattern.match(lower) for pattern in _LEAKED_PATTERNS)


def _scan_for_leaks(rels: list[Path], where: str) -> None:
    """扫一遍相对路径列表；命中即抛 ``PackageError``（含文件名与所处阶段）。"""
    leaked: list[str] = []
    for rel in rels:
        leaf = rel.parts[-1] if rel.parts else rel.name
        if _is_leaked(leaf):
            leaked.append(str(rel))
    if leaked:
        joined = "、".join(leaked)
        raise PackageError(
            f"{where}出现敏感文件（{joined}），已中止打包。"
            "这些文件可能含明文 Telegram 凭据或运行状态，绝不能进入部署包。"
        )


def _collect_files(stage_dir: Path, rels: list[Path]) -> list[Path]:
    """把白名单的相对路径展开成「所有被加入 zip 的文件」的相对路径列表。

    目录条目（如 ``src/``）递归下钻到里面的每个文件；文件条目原样保留。
    这样 zip 成员里 ``src/hawkeye/__init__.py`` 等内容都能进包，同时
    ``_scan_for_leaks`` 可以对每个文件的 leaf 做命中判断。
    """
    flat: list[Path] = []
    for rel in rels:
        abs_path = stage_dir / rel
        if abs_path.is_dir():
            for p in sorted(abs_path.rglob("*")):
                if p.is_file():
                    flat.append(rel / p.relative_to(abs_path))
        elif abs_path.is_file():
            flat.append(rel)
    return flat


def _build_zip(tmp_zip: Path, members: list[tuple[str, Path]]) -> None:
    """用 ``zipfile`` 直接写包；成员名带 ``<pkg_name>/`` 顶层前缀，权限 0o755 << 16。"""
    external_attr = 0o755 << 16
    with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, abs_path in members:
            info = zipfile.ZipInfo(name)
            info.external_attr = external_attr
            info.compress_type = zipfile.ZIP_DEFLATED
            with abs_path.open("rb") as f:
                zf.writestr(info, f.read())


def build_package(*, root: Path, dist_dir: Path | None = None) -> Path:
    """把项目根打成 zip；返回 zip 路径。

    流程：白名单拷贝到 stage → 剥构建产物 → 扫白名单 → 写临时 zip → 扫成员名 →
    rename 到 ``dist/``。任一步失败都会清掉临时 zip 和 stage 目录，不留半截包。
    """
    root = root.resolve()
    _check_root(root)
    version = _read_version(root / "pyproject.toml")
    if not re.match(r"^[A-Za-z0-9._-]+$", version):
        raise PackageError(
            f"pyproject.toml 的 [project].version={version!r} 含非法字符，"
            "仅允许字母、数字、点、下划线、连字符（防止 zip 路径穿越）"
        )
    pkg_name = f"hawkeye-{version}-{_timestamp()}"
    dist_dir = (dist_dir or root / "dist").resolve()
    dist_dir.mkdir(parents=True, exist_ok=True)
    archive = dist_dir / f"{pkg_name}.zip"
    tmp_archive = dist_dir / f".{pkg_name}.tmp"

    stage_dir = root / f".hawkeye-stage-{pkg_name}"
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True)

    try:
        rels = _stage_payload(root, stage_dir)
        _strip_build_artifacts(stage_dir / "src")

        # 展开白名单（src/ 目录要递归下钻），得到所有将被写入 zip 的文件。
        flat_rels = _collect_files(stage_dir, rels)

        # 兜底第一道：所有文件 leaf 里不能有七族敏感文件名。
        _scan_for_leaks(flat_rels, where="白名单拷贝结果")

        # 准备 zip 成员：(成员名, 绝对路径) 对；成员名统一用 ``/`` 分隔。
        members: list[tuple[str, Path]] = []
        for rel in flat_rels:
            member_name = f"{pkg_name}/{PurePosixPath(rel.as_posix())}"
            members.append((member_name, stage_dir / rel))

        # 兜底第二道：成员名再扫一遍（同一份七族清单，但作用于最终 zip 成员）。
        member_rels = [Path(name) for name, _ in members]
        _scan_for_leaks(member_rels, where="zip 成员名")

        # 写到临时 zip，全部通过后再 rename 到 dist/，失败时只留残骸在 .tmp。
        if tmp_archive.exists():
            tmp_archive.unlink()
        _build_zip(tmp_archive, members)
        shutil.move(str(tmp_archive), str(archive))
    except BaseException:
        if tmp_archive.exists():
            tmp_archive.unlink()
        raise
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)
    return archive


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}K"
    return f"{size / (1024 * 1024):.1f}M"


def print_summary(archive: Path, pkg_name: str) -> None:
    """打印 ``cmd_package`` 等价的人工上传指引，并提示下一步推荐 ``hawkeye deploy``。"""
    size = _format_size(archive.stat().st_size)
    print()
    print(f"[HawkEye] 打包完成：{archive}（{size}）")
    print()
    print("上传并更新 VPS（把 user@vps 换成你的服务器）：")
    print(f"  scp {archive} user@vps:/tmp/")
    print("  ssh user@vps")
    print(f"  unzip -q /tmp/{pkg_name}.zip -d /tmp && cd /tmp/{pkg_name}")
    print("  sudo ./deploy.sh install")
    print()
    print("install 幂等：保留服务器上已有的 config.toml 与 state.json，配置就绪时自动重启服务。")
    print("下一步推荐：hawkeye deploy（同一台机器免密 SSH 时直接复用，无需手动 scp）。")
    print()


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：解析 ``--root``，调用 :func:`build_package` 并打印摘要。"""
    parser = argparse.ArgumentParser(
        prog="hawkeye package",
        description="把项目打成部署 zip（与 deploy.sh package 语义一致，跨平台）",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="项目根路径（默认当前目录；必须含 pyproject.toml / src/ / config.example.toml）",
    )
    args = parser.parse_args(argv)
    try:
        archive = build_package(root=args.root)
    except PackageError as e:
        print(f"[HawkEye] {e}", file=sys.stderr)
        return 1
    print_summary(archive, archive.stem)
    return 0


if __name__ == "__main__":
    sys.exit(main())
