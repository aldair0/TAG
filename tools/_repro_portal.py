"""Throwaway repro for the InvalidSessionIdException seen on Chrome 149.
Mimics download_pricing_csv's driver lifecycle WITHOUT needing a login:
build driver (non-headless), navigate, then poll find_element for ~12s.
If Chrome drops the DevTools connection we'll see the same exception.
"""
from __future__ import annotations

import time
import traceback
from pathlib import Path

from selenium.webdriver.common.by import By

from app.sync.tcgplayer.portal_downloader import _build_driver, find_browser_executable

browser = find_browser_executable()
print("browser:", browser)
profile = Path("data/_repro_profile")
dl = Path("data/csv/_incoming")

driver = _build_driver(browser, profile_dir=profile, download_dir=dl, headless=False)
print("driver built")
try:
    driver.get("https://store.tcgplayer.com/")
    print("nav1 ok, title:", driver.title[:60])
    deadline = time.monotonic() + 12
    n = 0
    while time.monotonic() < deadline:
        try:
            driver.find_element(By.CSS_SELECTOR, "input[value='Export From Live']")
        except Exception as e:
            # NoSuchElement is fine/expected; InvalidSessionId is the bug.
            if "invalid session id" in str(e).lower():
                print("REPRODUCED InvalidSessionIdException after", n, "polls")
                raise
        n += 1
        time.sleep(0.5)
    print("survived 12s, polls:", n, "-- no disconnect")
except Exception:
    traceback.print_exc()
finally:
    try:
        driver.quit()
    except Exception:
        pass
