"""TCGPlayer quantity update via Selenium portal automation.

After a POS sale, ``record_sale`` enqueues ``OutboundChange`` rows with
``channel='tcgplayer'`` / ``action='update_qty'``.  This module drives
the TCGPlayer seller portal to apply those changes and marks each row
complete (or records the error for retry).

Entry point: :func:`apply_pending_tcgplayer_updates`.  Called from the
POS checkout routes on a background daemon thread so the cashier never
waits on a browser session.

Portal flow per item
--------------------
1. Navigate to https://store.tcgplayer.com/admin/product/catalog
2. Check "My Inventory Only", type card name, click Search.
3. Find the result row whose ProductName matches; click its Manage link.
4. On the manage page, find the condition row whose conditionName
   contains our stored condition string (partial, case-insensitive).
5. Read the quantity input, overwrite with the new quantity, click Save.

Auth
----
Re-uses the same persistent Chrome profile and stored-cookie injection
as the CSV downloader.  If the session has expired the update will fail
gracefully (the OutboundChange row records the error so the admin can
see it in the outbound queue and retry after re-authenticating).

Concurrency
-----------
Shares ``_BROWSER_LOCK`` from ``portal_downloader`` so a scheduled CSV
download and a post-sale update never race for the same Chrome profile.
"""

from __future__ import annotations

import logging
import time
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

CATALOG_URL = "https://store.tcgplayer.com/admin/product/catalog"
_PAGE_WAIT_SEC = 25  # max seconds to wait for portal elements
_SAVE_SETTLE_SEC = 2  # brief pause after clicking Save

# Condition abbreviation → TCGPlayer full-name fragment mapping.
# TCGPlayer condition names look like "Near Mint Unlimited" — partial match
# on the fragment handles all print runs.
_CONDITION_MAP: dict[str, str] = {
    "NM": "Near Mint",
    "LP": "Lightly Played",
    "MP": "Moderately Played",
    "HP": "Heavily Played",
    "D": "Damaged",
    "DMG": "Damaged",
}


def _normalize_condition(condition: str) -> str:
    """Expand short abbreviations to the full TCGPlayer condition name fragment.

    If the stored condition is already a full phrase (e.g. "Near Mint"),
    it is returned unchanged.  Unknown values are also returned unchanged —
    they may still partially match.
    """
    c = condition.strip()
    return _CONDITION_MAP.get(c.upper(), c)


# ---------------------------------------------------------------------------
# Portal interaction helpers
# ---------------------------------------------------------------------------


def _search_and_open_manage(driver, card_name: str) -> bool:
    """Search the catalog for *card_name* and click the Manage button.

    Returns True when the Manage page starts loading, False when the
    card cannot be found.
    """
    from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    driver.get(CATALOG_URL)
    wait = WebDriverWait(driver, _PAGE_WAIT_SEC)

    # Wait for the search box — do NOT enable "My Inventory Only".
    # That filter hides cards whose TCGPlayer qty is already 0, which is
    # exactly the state we're setting.  Instead we search the full catalog
    # and identify our row by its "Manage" action button (other sellers'
    # rows show "Add", only ours shows "Manage").
    wait.until(EC.presence_of_element_located((By.ID, "SearchValue")))

    # Type card name and search
    search_input = driver.find_element(By.ID, "SearchValue")
    search_input.clear()
    search_input.send_keys(card_name)

    # Snapshot existing rows so we can detect when the search results replace them.
    rows_before = driver.find_elements(By.CSS_SELECTOR, "table tr.gradeA")
    first_row_before = rows_before[0] if rows_before else None

    driver.find_element(By.ID, "Search").click()

    # Wait for the search to complete.
    # Phase 1: wait for the OLD rows to go stale (KO removes them when the
    #          AJAX response comes back and replaces the observable array).
    if first_row_before is not None:
        try:
            WebDriverWait(driver, _PAGE_WAIT_SEC).until(
                EC.staleness_of(first_row_before)
            )
        except TimeoutException:
            pass  # table may have been rebuilt in-place rather than replaced

    # Phase 2: wait for NEW tr.gradeA rows to appear.
    try:
        WebDriverWait(driver, _PAGE_WAIT_SEC).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table tr.gradeA"))
        )
    except TimeoutException:
        logger.warning("qty_updater: search returned no results for %r", card_name)
        return False

    rows = driver.find_elements(By.CSS_SELECTOR, "table tr.gradeA")
    if not rows:
        logger.warning("qty_updater: no result rows for %r", card_name)
        return False

    # Find our row: match card name AND the action button must say "Manage"
    # (not "Add") — that's the seller's own listing row.
    for row in rows:
        try:
            btn = row.find_element(By.CSS_SELECTOR, "a.blue-button-sm-darker")
            if btn.text.strip().lower() != "manage":
                continue  # another seller's row
            spans = row.find_elements(
                By.CSS_SELECTOR,
                "td a span[data-bind*='ProductName'], td span[data-bind*='ProductName']",
            )
            for span in spans:
                if span.text.strip().lower() == card_name.strip().lower():
                    btn.click()
                    return True
        except StaleElementReferenceException:
            break
        except Exception:
            continue

    # Fallback: name match failed but click the first row whose button is "Manage"
    logger.warning(
        "qty_updater: exact name match failed for %r — clicking first Manage row",
        card_name,
    )
    try:
        for row in driver.find_elements(By.CSS_SELECTOR, "table tr.gradeA"):
            btn = row.find_element(By.CSS_SELECTOR, "a.blue-button-sm-darker")
            if btn.text.strip().lower() == "manage":
                btn.click()
                return True
    except Exception:
        pass

    logger.error("qty_updater: no Manage row found for %r", card_name)
    return False


def _update_qty_on_manage_page(driver, condition: str, new_qty: int) -> bool:
    """On the product manage page, find the matching condition row,
    overwrite the quantity, and click Save.

    Returns True on success.
    """
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    wait = WebDriverWait(driver, _PAGE_WAIT_SEC)

    # Wait for condition rows to render (Knockout binding)
    try:
        wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "tr.product-listing"))
        )
    except TimeoutException:
        logger.warning("qty_updater: manage page did not render product-listing rows")
        return False

    # Resolve condition to a TCGPlayer fragment for partial matching
    cond_fragment = _normalize_condition(condition).lower()

    rows = driver.find_elements(By.CSS_SELECTOR, "tr.product-listing")
    matched_row = None
    matched_cond_name = ""

    for row in rows:
        try:
            cond_el = row.find_element(
                By.CSS_SELECTOR, "span[data-bind*='conditionName']"
            )
            cond_text = cond_el.text.strip().lower()
            if cond_fragment and cond_fragment in cond_text:
                matched_row = row
                matched_cond_name = cond_el.text.strip()
                break
        except Exception:
            continue

    if matched_row is None:
        logger.warning(
            "qty_updater: no condition row matched %r (fragment=%r) — "
            "available: %s",
            condition,
            cond_fragment,
            [
                r.find_element(By.CSS_SELECTOR, "span[data-bind*='conditionName']").text
                for r in rows
                if r.find_elements(By.CSS_SELECTOR, "span[data-bind*='conditionName']")
            ],
        )
        return False

    logger.info(
        "qty_updater: matched condition row %r → setting qty=%d",
        matched_cond_name,
        new_qty,
    )

    # Locate the quantity input in this row.
    # aria-label contains "Quantity for <full product description>"
    try:
        qty_input = matched_row.find_element(
            By.CSS_SELECTOR, "input[type='text'][aria-label*='Quantity for']"
        )
    except Exception:
        logger.warning(
            "qty_updater: qty input not found in condition row %r", matched_cond_name
        )
        return False

    # Update the Knockout-bound input.
    # Selenium's clear() does not fire the `input` event that KO's textInput
    # binding listens to.  Select-all + send_keys fires it correctly.
    try:
        qty_input.click()
        qty_input.send_keys(Keys.CONTROL + "a")
        qty_input.send_keys(str(new_qty))
        # Dispatch an extra `input` event via JS in case KO missed the keys
        driver.execute_script(
            "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));",
            qty_input,
        )
    except Exception as e:
        logger.warning("qty_updater: could not update qty input: %s", e)
        return False

    # Click Save — it is disabled while KO is processing (isBusy observable)
    try:
        save_btn = wait.until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "input[type='button'][value='Save']")
            )
        )
        save_btn.click()
    except TimeoutException:
        logger.warning("qty_updater: Save button never became clickable")
        return False
    except Exception as e:
        logger.warning("qty_updater: could not click Save: %s", e)
        return False

    # Brief settle so the XHR completes before we navigate away
    time.sleep(_SAVE_SETTLE_SEC)
    logger.info(
        "qty_updater: saved qty=%d for condition=%r", new_qty, matched_cond_name
    )
    return True


# ---------------------------------------------------------------------------
# DB helpers — mark outbound rows done / error
# ---------------------------------------------------------------------------


def _mark_done(change_id: int) -> None:
    from app.db.models import OutboundChange
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        row = s.get(OutboundChange, change_id)
        if row is not None:
            row.completed_at = datetime.now(timezone.utc)
            row.attempted_at = row.completed_at
            row.attempts = (row.attempts or 0) + 1
            row.last_error = None
            s.commit()


def _mark_error(change_id: int, error: str) -> None:
    from app.db.models import OutboundChange
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        row = s.get(OutboundChange, change_id)
        if row is not None:
            now = datetime.now(timezone.utc)
            row.attempted_at = now
            row.attempts = (row.attempts or 0) + 1
            row.last_error = error[:1000]  # guard against huge tracebacks
            s.commit()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def apply_pending_tcgplayer_updates(unit_ids: list[int] | None = None) -> int:
    """Process pending OutboundChange rows for TCGPlayer qty updates.

    Drives the seller portal in headless Chrome/Edge to apply each pending
    ``channel='tcgplayer'`` / ``action='update_qty'`` change.  Marks rows
    complete on success or records the error for manual retry.

    Parameters
    ----------
    unit_ids:
        When provided, limits processing to rows whose ``inventory_unit_id``
        is in this list.  Pass the IDs from the current sale so we only touch
        what changed right now; ``None`` processes all pending rows (useful
        for a manual "catch-up" trigger from the admin panel).

    Returns
    -------
    int
        Number of rows successfully updated.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload

    from app.db.models import Channel, InventoryUnit, OutboundAction, OutboundChange
    from app.db.session import SessionLocal
    from app.sync.tcgplayer.portal_downloader import (
        DOWNLOAD_DIR,
        PROFILE_DIR,
        _BROWSER_LOCK,
        _build_driver,
        _inject_stored_cookies,
        find_browser_executable,
    )

    # ---- collect pending rows -------------------------------------------
    with SessionLocal() as session:
        stmt = (
            select(OutboundChange)
            .options(
                joinedload(OutboundChange.inventory_unit).joinedload(
                    InventoryUnit.product
                )
            )
            .where(
                OutboundChange.channel == Channel.TCGPLAYER.value,
                OutboundChange.action == OutboundAction.UPDATE_QTY.value,
                OutboundChange.completed_at.is_(None),
            )
            .order_by(OutboundChange.enqueued_at)
        )
        if unit_ids:
            stmt = stmt.where(OutboundChange.inventory_unit_id.in_(unit_ids))

        rows = session.execute(stmt).unique().scalars().all()

        # Build a plain list so we can close the session before opening the browser
        items: list[dict] = []
        for row in rows:
            unit = row.inventory_unit
            if unit is None or unit.product is None:
                continue
            # Supplies are not listed on TCGPlayer — nothing to update
            if unit.product.kind == "supply":
                _mark_done(row.id)
                continue
            items.append(
                {
                    "change_id": row.id,
                    "unit_id": unit.id,
                    "card_name": unit.product.name,
                    "condition": unit.condition or "",
                    "new_qty": (row.payload or {}).get(
                        "quantity", unit.quantity_on_hand
                    ),
                }
            )

    if not items:
        logger.debug("qty_updater: no pending TCGPlayer outbound rows")
        return 0

    # ---- launch browser -------------------------------------------------
    browser = find_browser_executable()
    if browser is None:
        logger.warning(
            "qty_updater: no Chrome/Edge found — cannot update TCGPlayer portal"
        )
        for item in items:
            _mark_error(item["change_id"], "No Chrome/Edge browser found on this machine")
        return 0

    logger.info(
        "qty_updater: processing %d item(s) via TCGPlayer portal", len(items)
    )

    acquired = _BROWSER_LOCK.acquire(blocking=True, timeout=60)
    if not acquired:
        logger.warning(
            "qty_updater: could not acquire browser lock within 60s — "
            "a download is in progress; will retry next time"
        )
        return 0

    driver = None
    success_count = 0
    try:
        driver = _build_driver(
            browser,
            profile_dir=PROFILE_DIR,
            download_dir=DOWNLOAD_DIR,
            headless=False,  # headed: avoids bot-detection on portal pages
        )
        _inject_stored_cookies(driver)

        for item in items:
            change_id = item["change_id"]
            card_name = item["card_name"]
            condition = item["condition"]
            new_qty = item["new_qty"]

            logger.info(
                "qty_updater: updating %r condition=%r → qty=%d (change_id=%d)",
                card_name,
                condition,
                new_qty,
                change_id,
            )
            try:
                found = _search_and_open_manage(driver, card_name)
                if not found:
                    _mark_error(change_id, f"Card {card_name!r} not found in TCGPlayer catalog")
                    continue

                # Brief pause for the manage page to start loading
                time.sleep(1)

                saved = _update_qty_on_manage_page(driver, condition, new_qty)
                if saved:
                    _mark_done(change_id)
                    success_count += 1
                    logger.info(
                        "qty_updater: ✓ updated %r → qty=%d", card_name, new_qty
                    )
                else:
                    _mark_error(
                        change_id,
                        f"Could not find/update condition {condition!r} on manage page",
                    )
            except Exception as exc:
                logger.exception(
                    "qty_updater: unexpected error for unit_id=%d (%r)",
                    item["unit_id"],
                    card_name,
                )
                _mark_error(change_id, str(exc)[:500])

    except Exception:
        logger.exception("qty_updater: browser session failed")
    finally:
        try:
            if driver is not None:
                driver.quit()
        except Exception:
            logger.warning("qty_updater: driver.quit() raised", exc_info=True)
        _BROWSER_LOCK.release()

    logger.info(
        "qty_updater: finished — %d/%d updated", success_count, len(items)
    )
    return success_count


# ---------------------------------------------------------------------------
# Background dispatcher — fire-and-forget after a POS sale
# ---------------------------------------------------------------------------


def dispatch_after_sale(unit_ids: list[int]) -> None:
    """Spawn a daemon thread to update TCGPlayer after a POS sale.

    ``unit_ids`` should be the ``inventory_unit_id`` values from the sale
    lines.  The thread runs :func:`apply_pending_tcgplayer_updates` which
    reads the already-enqueued OutboundChange rows for those units.

    This is non-blocking — the POS checkout response is not delayed.
    """
    if not unit_ids:
        return

    def _run() -> None:
        try:
            apply_pending_tcgplayer_updates(unit_ids=unit_ids)
        except Exception:
            logger.exception("qty_updater background thread crashed")

    thread = threading.Thread(
        target=_run, name="TCGPlayerQtyUpdate", daemon=True
    )
    thread.start()
    logger.debug(
        "qty_updater: dispatched background thread for units %s", unit_ids
    )
