"""Backward-compatibility shim re-exporting the public surface of ``app.deriv``.

The Deriv gateway implementation now lives in the ``app.deriv`` package. This
module keeps the ``app.deriv_gateway`` import path working for ``app/main.py``
(``from app.deriv_gateway import router``) and for ``tests/test_deriv_gateway.py``,
which reads and monkeypatches attributes such as ``deriv_gateway._store_credential``
and ``deriv_gateway.current_user``.

Because tests patch helpers on *this* module, the new submodules call the five
mutable credential helpers (``_store_credential`` / ``_load_credential`` /
``_delete_credential`` / ``_store_account_links`` / ``_delete_account_links``)
through this module at call time (``import app.deriv_gateway as _gw``), so the
patches take effect on the routes and ``_owned_credential``. No logic lives here.
"""
from __future__ import annotations

from app.deriv import router
from app.deriv.credentials import (
    Credential,
    _credential_cipher,
    _credential_store_config,
    _delete_account_links,
    _delete_credential,
    _linked_account_ids,
    _load_credential,
    _owned_credential,
    _store_account_links,
    _store_credential,
)
from app.deriv.models import (
    ACCOUNT_ID_RE,
    AccountOwnershipRequest,
    AccountRequest,
    CredentialTransfer,
    OAuthExchangeRequest,
)
from app.deriv.security import (
    SYMBOL_PATTERN,
    _origin_allowed,
    _require_bridge_secret,
    _resolve_user_token,
    _ws_tickets,
    bearer,
    current_user,
)
from app.deriv.trading_proxy import _ALLOWED_CLIENT_REQUESTS

__all__ = [
    "ACCOUNT_ID_RE",
    "AccountOwnershipRequest",
    "AccountRequest",
    "Credential",
    "CredentialTransfer",
    "OAuthExchangeRequest",
    "SYMBOL_PATTERN",
    "_ALLOWED_CLIENT_REQUESTS",
    "_credential_cipher",
    "_credential_store_config",
    "_delete_account_links",
    "_delete_credential",
    "_linked_account_ids",
    "_load_credential",
    "_origin_allowed",
    "_owned_credential",
    "_require_bridge_secret",
    "_resolve_user_token",
    "_store_account_links",
    "_store_credential",
    "_ws_tickets",
    "bearer",
    "current_user",
    "router",
]
