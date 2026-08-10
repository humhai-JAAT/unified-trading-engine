"""Visits a Streamlit Cloud app with a real headless browser to keep it awake, or wake
it back up if it's already asleep.

A plain HTTP GET (curl) does NOT work for this — verified directly (in the sibling
bots this script was ported from): curl against a live app returns an infinite 303
redirect loop and never resolves. Streamlit only renders real app content via a
JS-driven WebSocket connection, and a slept app's "wake up" screen is a button that
JS has to click — neither is reachable without an actual browser executing JavaScript.

Cold starts observed to take anywhere from ~25s to 90s+ depending on the app, so this
polls patiently rather than giving up early.
"""

import os
import sys
import time

from playwright.sync_api import sync_playwright

APP_URL = os.environ["APP_URL"]
PAGE_LOAD_TIMEOUT_MS = 30_000
TOTAL_TIMEOUT_S = 150
POLL_INTERVAL_S = 3

SLEEP_MARKERS = [
    "zzzz", "this app has gone to sleep", "wake this app back up",
    "yes, get this app back up",
]
WAKE_BUTTON_SELECTORS = [
    "button:has-text('Yes, get this app back up!')",
    "[data-testid='wakeup-button-owner']",
    "[data-testid='wakeup-button-viewer']",
    "[data-testid='wakeup-button']",
]


def body_text_lower(page) -> str:
    try:
        return (page.inner_text("body") or "").lower()
    except Exception:
        return ""


def looks_asleep(text: str) -> bool:
    return any(marker in text for marker in SLEEP_MARKERS)


def looks_loaded(text: str) -> bool:
    return len(text.strip()) > 40 and not looks_asleep(text)


def click_wake_button(page) -> bool:
    for selector in WAKE_BUTTON_SELECTORS:
        try:
            button = page.locator(selector).first
            if button.is_visible(timeout=1000):
                button.click()
                return True
        except Exception:
            continue
    return False


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(APP_URL, timeout=PAGE_LOAD_TIMEOUT_MS, wait_until="domcontentloaded")
        except Exception as e:
            print(f"Initial navigation raised ({e}) — continuing to poll anyway")

        deadline = time.time() + TOTAL_TIMEOUT_S
        clicked = False
        while time.time() < deadline:
            text = body_text_lower(page)

            if looks_loaded(text):
                print(f"App is awake and loaded: {APP_URL}")
                browser.close()
                return 0

            if not clicked and click_wake_button(page):
                clicked = True
                print("Clicked wake-up button, waiting for app to load...")

            page.wait_for_timeout(int(POLL_INTERVAL_S * 1000))

        print(f"Timed out after {TOTAL_TIMEOUT_S}s waiting for app content: {APP_URL}")
        browser.close()
        return 1


if __name__ == "__main__":
    sys.exit(main())
