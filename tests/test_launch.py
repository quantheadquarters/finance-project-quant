"""`launch.py` is the first thing a new user runs, on an operating system we
cannot test here. These pin the properties that make it work on both.

It is deliberately not imported at module scope: importing it is harmless, but
these tests are about the file's *shape* (stdlib-only, path handling), which is
what breaks when someone edits it on one platform and never opens the other.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

LAUNCHER = Path(__file__).parent.parent / "launch.py"


def _tree() -> ast.Module:
    return ast.parse(LAUNCHER.read_text())


def test_launcher_imports_only_the_standard_library() -> None:
    """It runs BEFORE anything is installed, on whatever Python the user has.
    A third-party import here is a chicken-and-egg failure on first run."""
    stdlib = {
        "os",
        "sys",
        "venv",
        "subprocess",
        "webbrowser",
        "pathlib",
        "platform",
        "shutil",
        "argparse",
        "json",
        "time",
        "__future__",
    }
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root in stdlib, f"non-stdlib import: {alias.name}"
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            root = (node.module or "").split(".")[0]
            assert root in stdlib, f"non-stdlib import: {node.module}"


def test_launcher_handles_the_windows_venv_layout() -> None:
    """Windows puts console scripts in Scripts\\ and the binary is python.exe;
    every other platform uses bin/ and python. Hardcoding either one makes the
    launcher silently fail to find its own interpreter on the other."""
    src = LAUNCHER.read_text()
    assert "Scripts" in src and "bin" in src
    assert "python.exe" in src
    assert "os.name" in src or "platform.system" in src


def test_launcher_never_hardcodes_a_posix_path_separator() -> None:
    """Paths must be built with pathlib so they work on both separators."""
    src = LAUNCHER.read_text()
    # A string literal that looks like an absolute POSIX path is the tell.
    assert not re.search(r'"\s*/(usr|tmp|home|bin)\b', src)
    assert "Path(" in src


def test_double_click_wrappers_exist_for_both_platforms() -> None:
    root = LAUNCHER.parent
    mac = root / "Alpha Engine.command"
    win = root / "Alpha Engine.bat"
    assert mac.exists(), "macOS double-click wrapper is missing"
    assert win.exists(), "Windows double-click wrapper is missing"
    # The .command must be executable or Finder will refuse to run it.
    assert mac.stat().st_mode & 0o111, "Alpha Engine.command is not executable"
    # Both must defer to launch.py rather than reimplementing the setup.
    assert "launch.py" in mac.read_text()
    assert "launch.py" in win.read_text()
