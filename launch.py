#!/usr/bin/env python3
"""Alpha Engine — one command, any operating system.

    python launch.py            set up, generate signals, open the dashboard
    python launch.py --no-open  same, but do not launch a browser
    python launch.py scan BTC   run any CLI command inside the managed venv

Double-clickable wrappers sit beside this file: `Alpha Engine.command` on macOS
and `Alpha Engine.bat` on Windows. Both just call this script, so there is one
implementation of the setup logic rather than one per platform.

Why this exists alongside start.sh: start.sh is 400+ lines of bash and cannot
run on Windows. Rewriting it as a .bat would mean maintaining the same logic
twice in two languages that are each bad at it. This runs anywhere Python does,
which is the only prerequisite the project already had.

Deliberately dependency-free and stdlib-only: it has to run BEFORE anything is
installed, on whatever Python the user happens to have.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import venv
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
PORT = 8787
MIN_PY = (3, 10)

IS_WINDOWS = os.name == "nt"
# Windows puts console scripts in Scripts\ and everyone else in bin/.
BIN = VENV / ("Scripts" if IS_WINDOWS else "bin")
PY = BIN / ("python.exe" if IS_WINDOWS else "python")


def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(msg: str) -> None:
    print(f"==> {msg}", flush=True)


def die(msg: str, hint: str = "") -> "NoReturn":  # noqa: F821
    print(f"\nerror: {msg}", file=sys.stderr)
    if hint:
        print(f"       {hint}", file=sys.stderr)
    # On Windows this is usually a double-clicked window that would vanish
    # instantly, taking the error message with it.
    if IS_WINDOWS and sys.stdin.isatty():
        input("\nPress Enter to close...")
    sys.exit(1)


def check_python() -> None:
    if sys.version_info < MIN_PY:
        die(
            f"Python {MIN_PY[0]}.{MIN_PY[1]}+ is required, "
            f"but this is {sys.version_info.major}.{sys.version_info.minor}.",
            "Install a newer Python from https://python.org and run this again.",
        )


def ensure_venv() -> None:
    """Create the project-local virtual environment if it is missing.

    A venv keeps every install inside this folder. Nothing touches the system
    Python, so removing the folder removes the project completely.
    """
    if PY.exists():
        return
    step("Creating an isolated Python environment (.venv)")
    try:
        venv.EnvBuilder(with_pip=True, clear=False).create(VENV)
    except Exception as exc:
        die(
            f"could not create a virtual environment: {exc}",
            "On Debian/Ubuntu this usually means: sudo apt install python3-venv",
        )
    if not PY.exists():
        die("the virtual environment was created but has no Python executable.")


def pip(*args: str) -> int:
    return subprocess.call([str(PY), "-m", "pip", *args])


def ensure_installed() -> None:
    """Install the package in editable mode, but only when it is not already
    importable — `pip install` on every launch adds seconds to a warm start."""
    probe = subprocess.run(
        [str(PY), "-c", "import alpha_engine, sys; sys.exit(0)"],
        capture_output=True,
    )
    if probe.returncode == 0:
        return
    step("Installing Alpha Engine and its one dependency (first run only)")
    pip("install", "--quiet", "--upgrade", "pip")
    if pip("install", "--quiet", "-e", ".") != 0:
        die(
            "installation failed.",
            "Scroll up for pip's output — it names the actual problem.",
        )


def cli(*args: str, quiet: bool = False) -> int:
    """Run an alpha-engine CLI command inside the managed venv."""
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL} if quiet else {}
    return subprocess.call([str(PY), "-m", "alpha_engine.cli.main", *args], **kwargs)


def has_signals() -> bool:
    log = ROOT / "data" / "signals" / "signals.jsonl"
    return log.exists() and log.stat().st_size > 0


def seed_if_empty() -> None:
    """Generate a few signals so the dashboard has something to show.

    An empty dashboard is technically correct and completely useless as a first
    impression, and a new user cannot tell it apart from a broken one.
    """
    if has_signals():
        return
    step("Generating a few starter signals (needs internet, ~30s)")
    made = 0
    for asset in ("BTC", "ETH", "AAPL", "MSFT"):
        say(f"    {asset}...")
        if cli("scan", asset, quiet=True) == 0:
            made += 1
    if made == 0:
        say("    No signals could be generated — the dashboard will open empty.")
        say("    That usually means no internet connection right now.")


def main() -> int:
    check_python()

    args = [a for a in sys.argv[1:] if a != "--no-open"]
    open_browser = "--no-open" not in sys.argv

    os.chdir(ROOT)  # every relative path in the engine resolves from the root
    ensure_venv()
    ensure_installed()

    # Any other arguments mean "run this CLI command", not "start the app".
    if args:
        return cli(*args)

    seed_if_empty()

    url = f"http://127.0.0.1:{PORT}/dashboard"
    say()
    say("  Alpha Engine is starting.")
    say(f"  Dashboard   {url}")
    say(f"  Terminal    http://127.0.0.1:{PORT}/terminal")
    say()
    say("  Leave this window open while you use it. Press Ctrl+C to stop.")
    say()

    if open_browser:
        # Fires slightly before the server is listening; browsers retry, and
        # waiting on the port would mean threading a health check through here.
        webbrowser.open(url)

    try:
        return cli("dashboard", "--port", str(PORT))
    except KeyboardInterrupt:
        say("\n  Stopped.")
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # a double-clicked window must not vanish silently
        die(f"unexpected failure on {platform.system()}: {exc}")
