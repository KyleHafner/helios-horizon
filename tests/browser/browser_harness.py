from __future__ import annotations

import shutil
from contextlib import contextmanager

from playwright.sync_api import sync_playwright


@contextmanager
def browser_page(*, viewport, has_touch=False, init_script=None):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=shutil.which("google-chrome-stable") or shutil.which("google-chrome")
        )
        context = browser.new_context(
            has_touch=has_touch,
            viewport=viewport,
            extra_http_headers={"X-Forwarded-User": "operator@example.test"},
        )
        if init_script:
            context.add_init_script(init_script)
        page = context.new_page()
        try:
            yield page
        finally:
            context.close()
            browser.close()
