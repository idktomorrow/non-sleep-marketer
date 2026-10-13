"""대시보드 화면 7개를 데스크톱·휴대폰 폭으로 찍는다(배치 I 전후 비교용).

uv run python scripts/dashboard_shots.py --base http://127.0.0.1:8799 --out docs/samples/ui/before
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

SCREENS = [
    ("1-overview", "/"),
    ("2-review-list", "/review"),
    ("3-review-detail", "/review/demo-review-1"),
    ("4-comments", "/comments"),
    ("5-performance", "/performance"),
    ("6-reports", "/reports"),
    ("7-logs", "/logs"),
]
SIZES = {"desktop": (1280, 860), "mobile": (390, 844)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8799")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--full", action="store_true", help="페이지 전체 길이")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for size, (w, h) in SIZES.items():
            page = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=1)
            for name, path in SCREENS:
                page.goto(args.base + path, wait_until="networkidle")
                overflow = page.evaluate(
                    "document.documentElement.scrollWidth > window.innerWidth + 1"
                )
                page.screenshot(path=str(args.out / f"{name}-{size}.png"), full_page=args.full)
                print(f"{name}-{size}: 가로 넘침={'예' if overflow else '아니오'}")
            page.close()
        browser.close()


if __name__ == "__main__":
    main()
