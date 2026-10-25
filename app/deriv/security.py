"""Origin, bridge-secret and Supabase-token validation plus WebSocket tickets."""
from __future__ import annotations

import hmac
import os
import re
import secrets
import time
from typing import Annotated, Any

import httpx
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.deriv import router

bearer = HTTPBearer(auto_error=False)
SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9_]{2,32}$")
# Short-lived, one-use WebSocket handshake tickets (ticket -> (user_id, expires_at)).
_ws_tickets: dict[str, tuple[str, float]] = {}


def _origin_allowed(origin: str | None) -> bool:
    configured = {
        value.strip().rstrip("/")
        for value in os.getenv("TRADEBRIDGE_ALLOWED_ORIGINS", "").split(",")
        if value.strip()
    }
    # If configured, browser WebSocket handshakes must originate from one of
    # these exact origins. Native clients without Origin remain supported.
    return not origin or not configured or origin.rstrip("/") in configured


def _require_bridge_secret(supplied: str | None) -> None:
    expected = os.getenv("TRADEBRIDGE_SESSION_BRIDGE_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Credential bridge is not configured.")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Unauthorized credential transfer.")


async def _resolve_user_token(token: str) -> str:
    """Validate a Supabase access token without exposing it to logs or responses."""
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    anon_key = os.getenv("SUPABASE_ANON_KEY", "")
    if not token:
        raise HTTPException(status_code=401, detail="Sign in is required.")
    if not supabase_url or not anon_key:
        raise HTTPException(status_code=503, detail="User authentication is not configured.")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{supabase_url}/auth/v1/user",
                headers={"apikey": anon_key, "Authorization": f"Bearer {token}"},
            )
        if response.status_code != 200:
            raise HTTPException(status_code=401, detail="Invalid or expired app session.")
        payload = response.json()
        user_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(user_id, str) or not user_id:
            raise HTTPException(status_code=401, detail="Invalid app identity.")
        if not payload.get("email_confirmed_at"):
            raise HTTPException(status_code=403, detail="Verify your app email first.")
        return user_id
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=502, detail="Could not verify app session.")


async def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    return await _resolve_user_token(credentials.credentials if credentials else "")


@router.post("/ws-ticket")
async def create_websocket_ticket(
    user_id: Annotated[str, Depends(current_user)],
) -> dict[str, Any]:
    """Issue a short-lived, one-use WebSocket ticket instead of putting a JWT in handshake headers."""
    now = time.time()
    for ticket, (_, expires_at) in list(_ws_tickets.items()):
        if expires_at <= now:
            _ws_tickets.pop(ticket, None)
    ticket = secrets.token_urlsafe(32)
    _ws_tickets[ticket] = (user_id, now + 30)
    return {"ticket": ticket, "expires_in": 30}
