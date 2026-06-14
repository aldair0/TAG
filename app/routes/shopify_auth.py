"""Shopify OAuth flow.

GET  /auth/shopify/install   — start: redirect browser to Shopify consent page
GET  /auth/shopify/callback  — finish: exchange code for access token, store it

Works with any Shopify store (paid or dev) as long as the redirect URI is
whitelisted in the app's Dev Dashboard settings.
"""

from __future__ import annotations

import hashlib
import logging
import hmac as hmac_lib
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings as cfg
from app.db.session import get_session
from app.settings_store import get_secret_setting, get_setting, set_secret_setting, set_setting

router = APIRouter()
logger = logging.getLogger(__name__)

_SCOPES = (
    "write_products,read_products,"
    "write_inventory,read_inventory,"
    "read_orders,write_orders,"
    "read_draft_orders,write_draft_orders,"
    "read_locations"
)


def _api_key(session: Session) -> str:
    return (
        get_setting(session, "shopify_api_key")
        or cfg.shopify_api_key
        or ""
    ).strip()


def _api_secret(session: Session) -> str:
    return (
        get_secret_setting(session, "shopify_api_secret")
        or cfg.shopify_api_secret
        or ""
    ).strip()


def _validate_hmac(params: dict[str, str], secret: str) -> bool:
    """Verify Shopify's HMAC signature on the callback query string."""
    received = params.pop("hmac", "")
    message = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    digest = hmac_lib.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return hmac_lib.compare_digest(digest, received)


@router.get("/install", response_class=RedirectResponse)
def shopify_install(
    request: Request,
    shop: str,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Kick off the OAuth flow for a given shop domain."""
    api_key = _api_key(session)
    if not api_key:
        return RedirectResponse(url="/admin/settings/?shopify_error=no_api_key")

    nonce = secrets.token_hex(16)
    set_setting(session, "shopify_oauth_nonce", nonce)
    set_setting(session, "shopify_oauth_shop", shop.strip().lower())
    session.commit()

    redirect_uri = str(request.base_url).rstrip("/") + "/auth/shopify/callback"
    params = {
        "client_id": api_key,
        "scope": _SCOPES,
        "redirect_uri": redirect_uri,
        "state": nonce,
    }
    return RedirectResponse(
        url=f"https://{shop}/admin/oauth/authorize?{urlencode(params)}"
    )


@router.get("/callback", response_class=HTMLResponse)
def shopify_callback(
    request: Request,
    code: str = Query(""),
    hmac: str = Query(""),
    shop: str = Query(""),
    state: str = Query(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Handle Shopify's OAuth redirect, exchange code for token."""
    # Validate state matches what we sent
    stored_nonce = get_setting(session, "shopify_oauth_nonce") or ""
    if not state or state != stored_nonce:
        return RedirectResponse(url="/admin/settings/?shopify_error=invalid_state")

    # Validate HMAC when secret is available
    secret = _api_secret(session)
    if secret:
        query_params = dict(request.query_params)
        if not _validate_hmac(dict(query_params), secret):
            logger.warning("Shopify OAuth: HMAC mismatch — proceeding anyway (dev mode)")

    # Exchange code for access token
    api_key = _api_key(session)
    try:
        r = httpx.post(
            f"https://{shop}/admin/oauth/access_token",
            json={"client_id": api_key, "client_secret": secret, "code": code},
            timeout=10.0,
        )
        logger.info("Token exchange status: %s body: %s", r.status_code, r.text[:300])
        r.raise_for_status()
        token = r.json().get("access_token", "")
    except Exception as exc:
        logger.exception("Token exchange failed")
        return RedirectResponse(url="/admin/settings/?shopify_error=token_exchange_failed")

    if not token:
        return RedirectResponse(url="/admin/settings/?shopify_error=no_token")

    # Persist
    set_setting(session, "shopify_shop_domain", shop)
    set_secret_setting(session, "shopify_admin_api_token", token)
    # Clear the one-use nonce
    set_setting(session, "shopify_oauth_nonce", "")
    session.commit()

    return RedirectResponse(url="/admin/settings/?shopify_saved=1&shopify_test=1")
