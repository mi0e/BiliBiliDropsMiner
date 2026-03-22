from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Iterable


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not found, installing...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])


def format_cmd(cmd: list[str]) -> str:
    # Windows 下可读性更好
    return subprocess.list2cmdline(cmd)


def build(
    entry: str,
    name: str,
    *,
    windowed: bool = False,
    onefile: bool = False,
    clean: bool = False,
    noupx: bool = True,
    debug: bool = False,
    extra_args: Iterable[str] | None = None,
) -> None:
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--name",
        name,
    ]

    cmd.append("--onefile" if onefile else "--onedir")

    if clean:
        cmd.append("--clean")

    if noupx:
        cmd.append("--noupx")

    if windowed:
        cmd.append("--windowed")

    if debug:
        cmd.extend(["--log-level", "DEBUG"])

    if extra_args:
        cmd.extend(extra_args)

    cmd.append(entry)

    print(f"\nBuilding {name} ...")
    print(format_cmd(cmd))
    subprocess.check_call(cmd)

    if onefile:
        print(f"Done: dist/{name}.exe")
    else:
        print(f"Done: dist/{name}/")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build Bilibili Drops Miner with PyInstaller."
    )
    parser.add_argument(
        "--release",
        action="store_true",
        help="build release version (onefile). Default is development build (onedir).",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="clean PyInstaller cache before build.",
    )
    parser.add_argument(
        "--target",
        choices=["gui", "cli", "all"],
        default="all",
        help="select which target to build.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="enable PyInstaller debug log output.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ensure_pyinstaller()

    is_release = args.release

    # 开发阶段尽量少收集，通常比 --collect-all 更快
    # 如果之后 GUI 启动发现缺资源，再退回 --collect-all customtkinter
    gui_extra_args = [
        "--collect-data",
        "customtkinter",
        "--hidden-import",
        "darkdetect",
        "--collect-all",
        "selenium",
    ]

    if args.target in ("gui", "all"):
        build(
            "bilibili_gui.py",
            "bilibili-drops-miner-gui",
            windowed=True,
            onefile=is_release,
            clean=args.clean,
            noupx=True,
            debug=args.debug,
            extra_args=gui_extra_args,
        )

    if args.target in ("cli", "all"):
        build(
            "bilibili.py",
            "bilibili-drops-miner-cli",
            onefile=is_release,
            clean=False,  # 避免第二个目标再次清缓存
            noupx=True,
            debug=args.debug,
        )

    mode = "release" if is_release else "development"
    print(f"\nAll builds complete. Mode: {mode}. Output in dist/")


if __name__ == "__main__":
    main()