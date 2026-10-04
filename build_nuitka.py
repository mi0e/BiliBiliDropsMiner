from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable


OUTPUT_DIR = Path("dist-nuitka")
ICON_PATH = Path("img/app.icns" if sys.platform == "darwin" else "img/app.ico")

UNUSED_OPTIONAL_EXCLUDES = [
    "IPython",
    "jedi",
    "jinja2",
    "matplotlib",
    "numpy",
    "PIL",
    "prompt_toolkit",
    "pygments",
    # Selenium's Trio dependency can expose optional testing helpers when
    # pytest is installed in the build environment. It is not a runtime dep.
    "pytest",
    "_pytest",
    "tkinter",
    "traitlets",
]


def format_cmd(cmd: list[str]) -> str:
    return subprocess.list2cmdline(cmd)


def extend_nofollow_args(cmd: list[str], modules: Iterable[str]) -> None:
    for module in modules:
        cmd.append(f"--nofollow-import-to={module}")


def selenium_build_args(platform: str) -> list[str]:
    # webdriver.Chrome/Edge are lazy imports in recent Selenium versions.
    # Include their entry points explicitly, then follow their dependencies
    # (including BiDi/WebExtension) instead of compiling all of Selenium.
    args = [
        "--include-package=selenium.webdriver.chrome",
        "--include-package=selenium.webdriver.edge",
        "--nofollow-import-to=selenium.webdriver.common.devtools",
    ]
    # The sniffer uses BiDi, not start_devtools()/bidi_connection(), which need
    # the versioned CDP bindings above. Keep Selenium Manager for this OS;
    # Nuitka's Selenium data hook otherwise copies managers for every OS.
    foreign_platforms = {
        "win32": ("linux*", "macos"),
        "darwin": ("linux*", "windows"),
        "linux": ("macos", "windows"),
    }.get(platform, ())
    args.extend(
        f"--noinclude-data-files=selenium/webdriver/common/{folder}/*"
        for folder in foreign_platforms
    )
    return args


def build(
    entry: str,
    output_name: str,
    *,
    windowed: bool = False,
) -> None:
    cmd = [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone",
        "--assume-yes-for-downloads",
        f"--output-dir={OUTPUT_DIR}",
        f"--output-filename={output_name}{'.exe' if sys.platform == 'win32' else ''}",
        f"--report={OUTPUT_DIR / (Path(entry).stem + '-report.xml')}",
    ]

    if ICON_PATH.exists() and sys.platform == "win32":
        cmd.append(f"--windows-icon-from-ico={ICON_PATH}")

    if windowed:
        cmd.append("--enable-plugin=pyside6")
        cmd.extend(selenium_build_args(sys.platform))
        if sys.platform == "darwin":
            cmd.extend(
                [
                    "--macos-create-app-bundle",
                    "--macos-app-name=Bilibili Drops Miner",
                    "--macos-signed-app-name=com.mi0e.BiliBiliDropsMiner",
                ]
            )
            if ICON_PATH.exists():
                cmd.append(f"--macos-app-icon={ICON_PATH}")
        elif sys.platform == "win32":
            cmd.append("--windows-console-mode=disable")

    extend_nofollow_args(cmd, UNUSED_OPTIONAL_EXCLUDES)
    cmd.append("--nofollow-import-to=apprise")

    cmd.append(entry)

    print(f"\nBuilding {output_name} with Nuitka ...")
    print(format_cmd(cmd))
    subprocess.check_call(cmd)

    if windowed and sys.platform == "darwin":
        # --macos-create-app-bundle 输出 <entry>.app 而非 <entry>.dist
        source_dir = OUTPUT_DIR / f"{Path(entry).stem}.app"
        target_dir = OUTPUT_DIR / f"{output_name}.app"
    else:
        source_dir = OUTPUT_DIR / f"{Path(entry).stem}.dist"
        target_dir = OUTPUT_DIR / output_name
    if source_dir == target_dir:
        return
    if not source_dir.exists():
        raise FileNotFoundError(f"Nuitka output not found: {source_dir}")
    if target_dir.exists():
        shutil.rmtree(target_dir)
    source_dir.rename(target_dir)
    print(f"Done: {target_dir}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build Bilibili Drops Miner with Nuitka."
    )
    parser.add_argument(
        "--target",
        choices=["gui", "cli", "all"],
        default="all",
        help="select which target to build.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.target in ("gui", "all"):
        build(
            "bilibili_gui.py",
            "Bilibili Drops Miner",
            windowed=True,
        )

    if args.target in ("cli", "all"):
        build(
            "bilibili.py",
            "bilibili-drops-miner-cli",
        )

    print("\nAll Nuitka builds complete. Output in dist-nuitka/")


if __name__ == "__main__":
    main()
