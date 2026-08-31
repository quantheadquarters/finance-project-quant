#!/usr/bin/env python3
"""Record a walkthrough of the Alpha Engine web app as a video.

VHS (demo/demo.tape) covers the terminal. This covers the browser: it drives the
real dashboard with Playwright, captures frames, and encodes them with ffmpeg.

Everything shown is the live app served by `alpha-engine dashboard` against the
real signal log — no mockups and no seeded data. If the log is empty, the video
will honestly show an empty dashboard.

Usage:
    alpha-engine dashboard --port 8787 &          # in another shell
    python demo/record_app.py                     # -> demo/alpha-engine-app.mp4

The scene list is data, so adjusting the tour means editing SCENES, not code.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

FPS = 30
W, H = 1440, 900

# (action, argument, seconds_to_hold). "hold" repeats the previous frame, which
# is what gives a viewer time to actually read a panel.
SCENES: list[tuple[str, object, float]] = [
    ("goto", "/", 3.5),
    ("scroll", 420, 2.5),
    ("goto", "/dashboard", 4.0),
    ("hover", '.explain[data-tip^="Of the past calls"]', 4.0),  # Hit Rate
    ("hover", '.explain[data-tip^="Average price move"]', 4.0),  # Avg Return
    ("hover", '.explain[data-tip^="Regime is a statistical"]', 4.5),  # Regime
    ("scroll", 430, 1.0),
    ("hover", '.explain[data-tip^="Does a stated confidence"]', 4.5),  # Calibration
    ("hover", '.explain[data-tip^="What became of every"]', 4.0),  # Outcome Mix
    ("scroll", 380, 1.0),
    ("hover", '.explain[data-tip^="If you held all of these"]', 4.5),  # Sizing
    ("hover", '.explain[data-tip^="How bad a bad day looks"]', 4.5),  # Tail risk
    ("scroll", 420, 3.0),
    ("hover", '.explain[data-tip^="All current signals"]', 4.0),  # Portfolio
    ("scroll", 460, 3.5),
    ("scroll", 420, 3.5),  # signal feed
    ("click", "tbody tr", 4.5),  # per-asset history drawer
    ("scroll", 300, 3.0),
    ("goto", "/terminal", 4.0),
]


def record(base: str, out: Path) -> int:
    frames = Path(tempfile.mkdtemp(prefix="ae-frames-"))
    n = 0
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)

            for action, arg, hold in SCENES:
                try:
                    if action == "goto":
                        page.goto(base + str(arg), wait_until="networkidle")
                        page.wait_for_timeout(900)  # let reveal animations settle
                    elif action == "scroll":
                        # Smooth, so the video does not jump a screen at a time.
                        steps = 18
                        for _ in range(steps):
                            page.mouse.wheel(0, int(arg) / steps)
                            page.wait_for_timeout(16)
                            page.screenshot(path=str(frames / f"{n:05d}.png"))
                            n += 1
                        page.wait_for_timeout(250)
                    elif action == "hover":
                        el = page.locator(str(arg)).first
                        el.scroll_into_view_if_needed()
                        page.wait_for_timeout(350)
                        el.hover()
                        page.wait_for_timeout(320)  # tooltip fade-in
                    elif action == "click":
                        el = page.locator(str(arg)).first
                        el.scroll_into_view_if_needed()
                        page.wait_for_timeout(250)
                        el.click()
                        page.wait_for_timeout(900)
                except Exception as exc:  # a missing panel must not kill the take
                    print(f"[record] skipped {action} {arg!r}: {exc}", file=sys.stderr)
                    continue

                shot = frames / f"{n:05d}.png"
                page.screenshot(path=str(shot))
                n += 1
                # Hold: duplicate the frame rather than re-screenshot, so the
                # image is identical and the encoder compresses it to nothing.
                for _ in range(int(hold * FPS) - 1):
                    shutil.copyfile(shot, frames / f"{n:05d}.png")
                    n += 1

            browser.close()

        if n == 0:
            print("[record] no frames captured", file=sys.stderr)
            return 1

        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-framerate",
                str(FPS),
                "-i",
                str(frames / "%05d.png"),
                # yuv420p + even dimensions: the combination every player accepts.
                "-vf",
                "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-crf",
                "20",
                "-movflags",
                "+faststart",
                str(out),
            ],
            check=True,
        )
        print(f"[record] {n} frames -> {out} ({n / FPS:.1f}s)")
        return 0
    finally:
        shutil.rmtree(frames, ignore_errors=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default="http://127.0.0.1:8787")
    ap.add_argument("--out", type=Path, default=Path("demo/alpha-engine-app.mp4"))
    raise SystemExit(record(ap.parse_args().base, ap.parse_args().out))
