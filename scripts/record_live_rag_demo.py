#!/usr/bin/env python3
"""Record a concise browser walkthrough of the local SignalRAG demo."""

from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
SYSTEM_CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8010/live-rag")
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "signalrag_demo.webm")
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="signalrag-video-") as temp_dir:
        with sync_playwright() as playwright:
            launch_options = {"headless": True}
            if SYSTEM_CHROME.exists():
                launch_options["executable_path"] = str(SYSTEM_CHROME)
            browser = playwright.chromium.launch(**launch_options)
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                record_video_dir=temp_dir,
                record_video_size={"width": 1440, "height": 900},
                reduced_motion="reduce",
            )
            page = context.new_page()
            page.goto(args.url, wait_until="networkidle")
            page.wait_for_selector("text=EPHEMERAL SESSION ONLINE", timeout=20_000)
            page.wait_for_timeout(1800)

            page.get_by_role("button", name="01 Compound intent").click()
            page.wait_for_function("document.querySelector('#versionMetric').textContent === 'v1'", timeout=30_000)
            page.wait_for_timeout(2200)

            page.get_by_role("button", name="02 Late constraint").click()
            page.wait_for_function("document.querySelector('#versionMetric').textContent === 'v2'", timeout=35_000)
            page.wait_for_timeout(2200)

            page.get_by_role("button", name="03 No-search turn").click()
            page.wait_for_selector("text=REUSED EVIDENCE", timeout=10_000)
            page.wait_for_timeout(1800)

            page.get_by_role("button", name="Tool registry 7").click()
            page.wait_for_timeout(2600)
            page.get_by_role("button", name="Close tool registry").click()
            page.wait_for_timeout(900)

            video = page.video
            context.close()
            browser.close()
            if video is None:
                raise RuntimeError("Playwright did not create a video artifact")
            shutil.copy2(video.path(), args.output)

    print(f"Recorded {args.output} ({args.output.stat().st_size / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main()
