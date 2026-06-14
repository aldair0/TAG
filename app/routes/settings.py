"""Admin settings page — runtime-editable POS rates stored in app_setting."""

from __future__ import annotations

import socket
import subprocess
import sys
from typing import Any

import httpx

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings as cfg
from app.db.session import get_session
from app.paths import templates_dir
from app.settings_store import get_secret_setting, get_setting, set_secret_setting, set_setting

_SHOPIFY_API_VERSION = "2026-04"

_FIREWALL_RULE = "TAG Inventory POS (port 8000)"
_PORT = 8000

router = APIRouter()
templates = Jinja2Templates(directory=str(templates_dir()))

_RATE_KEYS = ("pos_tax_rate", "pos_card_surcharge", "pos_cash_discount")


def get_pos_rates(session: Session) -> dict[str, float]:
    """Current effective rates — DB overrides win over config defaults."""
    defaults = {
        "pos_tax_rate": cfg.pos_tax_rate,
        "pos_card_surcharge": cfg.pos_card_surcharge,
        "pos_cash_discount": cfg.pos_cash_discount,
    }
    out: dict[str, float] = {}
    for key, default in defaults.items():
        raw = get_setting(session, key)
        try:
            out[key] = float(raw) if raw is not None else default
        except (ValueError, TypeError):
            out[key] = default
    return out


def _local_ips() -> list[str]:
    """All non-loopback IPv4 addresses on this machine."""
    try:
        hostname = socket.gethostname()
        infos = socket.getaddrinfo(hostname, None, socket.AF_INET)
        ips = list(dict.fromkeys(i[4][0] for i in infos if not i[4][0].startswith("127.")))
        return ips or ["(unavailable)"]
    except Exception:
        return ["(unavailable)"]


def _firewall_rule_exists() -> bool:
    if sys.platform != "win32":
        return False
    try:
        result = subprocess.run(
            ["powershell", "-NonInteractive", "-Command",
             f'Get-NetFirewallRule -DisplayName "{_FIREWALL_RULE}" -ErrorAction SilentlyContinue'],
            capture_output=True, text=True, timeout=5,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def _open_firewall() -> str:
    """Spawn an elevated PowerShell to add the inbound rule. Returns a
    status string: 'ok', 'already_exists', or 'elevation_required'."""
    if sys.platform != "win32":
        return "not_windows"
    if _firewall_rule_exists():
        return "already_exists"
    cmd = (
        f'New-NetFirewallRule -DisplayName "{_FIREWALL_RULE}" '
        f'-Direction Inbound -Protocol TCP -LocalPort {_PORT} '
        f'-Action Allow -Profile Private'
    )
    try:
        # Start-Process with -Verb RunAs triggers the UAC elevation prompt.
        subprocess.run(
            ["powershell", "-NonInteractive", "-Command",
             f"Start-Process powershell -Verb RunAs -ArgumentList '-NonInteractive -Command {cmd}' -Wait"],
            timeout=30,
        )
        return "ok" if _firewall_rule_exists() else "elevation_required"
    except subprocess.TimeoutExpired:
        return "elevation_required"
    except Exception:
        return "elevation_required"


def get_shopify_creds(session: Session) -> dict[str, str]:
    domain = get_setting(session, "shopify_shop_domain") or cfg.shopify_shop_domain or ""
    token = get_secret_setting(session, "shopify_admin_api_token") or cfg.shopify_admin_api_token or ""
    api_key = get_setting(session, "shopify_api_key") or cfg.shopify_api_key or ""
    api_secret = get_secret_setting(session, "shopify_api_secret") or cfg.shopify_api_secret or ""
    location_id = get_setting(session, "shopify_location_id") or ""
    # Strip any stale masked values that may have been saved before guardrails were in place
    if all(c == '*' for c in token) if token else False:
        token = ""
    if all(c == '*' for c in api_secret) if api_secret else False:
        api_secret = ""
    return {
        "domain": domain.strip(),
        "token": token.strip(),
        "api_key": api_key.strip(),
        "api_secret_set": bool(api_secret.strip()),
        "location_id": location_id.strip(),
    }


def _shopify_test(domain: str, token: str) -> dict[str, Any]:
    """Call the Shopify Admin API and return shop info + locations, or an error dict."""
    if not domain or not token:
        return {"ok": False, "error": "No credentials configured."}
    base = f"https://{domain}/admin/api/{_SHOPIFY_API_VERSION}"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=10.0) as client:
            r_shop = client.get(f"{base}/shop.json", headers=headers)
            if r_shop.status_code == 401:
                return {"ok": False, "error": "Invalid API token — check the value and try again."}
            if r_shop.status_code == 404:
                return {"ok": False, "error": f"Store not found: {domain}"}
            r_shop.raise_for_status()
            shop = r_shop.json().get("shop", {})

            r_loc = client.get(f"{base}/locations.json", headers=headers)
            r_loc.raise_for_status()
            locations = r_loc.json().get("locations", [])

        return {
            "ok": True,
            "shop_name": shop.get("name", domain),
            "plan": shop.get("plan_display_name", ""),
            "locations": [{"id": str(l["id"]), "name": l["name"]} for l in locations],
        }
    except httpx.HTTPStatusError as e:
        return {"ok": False, "error": f"HTTP {e.response.status_code} from Shopify."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/", response_class=HTMLResponse)
def settings_index(
    request: Request,
    session: Session = Depends(get_session),
    saved: str = "",
    error: str = "",
    fw: str = "",
    shopify_saved: str = "",
    shopify_test: str = "",
    shopify_error: str = "",
) -> HTMLResponse:
    rates = get_pos_rates(session)
    creds = get_shopify_creds(session)

    # Run a live connection test if the user just saved or explicitly requested
    shopify_status: dict[str, Any] | None = None
    if shopify_test == "1" or (shopify_saved == "1" and creds["domain"] and creds["token"]):
        shopify_status = _shopify_test(creds["domain"], creds["token"])

    return templates.TemplateResponse(
        request,
        "admin/settings.html",
        {
            "title": "Settings",
            "phase": "4 — POS UI",
            "rates": rates,
            "saved": saved,
            "error": error,
            "fw_status": fw,
            "firewall_open": _firewall_rule_exists(),
            "local_ips": _local_ips(),
            "port": _PORT,
            "shopify": creds,
            "shopify_saved": shopify_saved,
            "shopify_status": shopify_status,
            "shopify_error": shopify_error,
        },
    )


@router.post("/network/firewall", response_class=HTMLResponse)
def setup_firewall() -> RedirectResponse:
    status = _open_firewall()
    return RedirectResponse(url=f"/admin/settings/?fw={status}", status_code=303)


@router.post("/", response_class=HTMLResponse)
def settings_save(
    pos_tax_rate: str = Form(...),
    pos_card_surcharge: str = Form(...),
    pos_cash_discount: str = Form(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    for key, raw in [
        ("pos_tax_rate", pos_tax_rate),
        ("pos_card_surcharge", pos_card_surcharge),
        ("pos_cash_discount", pos_cash_discount),
    ]:
        try:
            val = float(raw.strip().rstrip("%"))
            # Accept either 7 or 0.07 — normalise to decimal fraction
            if val > 1:
                val = val / 100
            if val < 0 or val > 1:
                raise ValueError
            set_setting(session, key, str(val))
        except (ValueError, AttributeError):
            session.rollback()
            return RedirectResponse(
                url=f"/admin/settings/?error=invalid_{key}", status_code=303
            )
    session.commit()
    return RedirectResponse(url="/admin/settings/?saved=1", status_code=303)


@router.post("/shopify", response_class=HTMLResponse)
def shopify_save(
    shopify_shop_domain: str = Form(""),
    shopify_admin_api_token: str = Form(""),
    shopify_api_key: str = Form(""),
    shopify_api_secret: str = Form(""),
    shopify_location_id: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    domain = shopify_shop_domain.strip().lower().removeprefix("https://").removesuffix("/")
    set_setting(session, "shopify_shop_domain", domain)
    token_val = shopify_admin_api_token.strip()
    if token_val and not all(c == '*' for c in token_val):
        set_secret_setting(session, "shopify_admin_api_token", token_val)
    api_key_val = shopify_api_key.strip()
    if api_key_val and not all(c == '*' for c in api_key_val):
        set_setting(session, "shopify_api_key", api_key_val)
    secret_val = shopify_api_secret.strip()
    if secret_val and not all(c == '*' for c in secret_val):
        set_secret_setting(session, "shopify_api_secret", secret_val)
    if shopify_location_id.strip():
        set_setting(session, "shopify_location_id", shopify_location_id.strip())
    session.commit()
    return RedirectResponse(url="/admin/settings/?shopify_saved=1&shopify_test=1", status_code=303)


@router.post("/shopify/test", response_class=HTMLResponse)
def shopify_test_connection(session: Session = Depends(get_session)) -> RedirectResponse:
    return RedirectResponse(url="/admin/settings/?shopify_test=1", status_code=303)
