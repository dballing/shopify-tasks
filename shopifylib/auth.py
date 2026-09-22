#!/usr/bin/env python3
"""Access-token acquisition, caching and refresh.

Shopify retired admin-created custom apps on 2026-01-01. Those were the ones
that handed you a permanent ``shpat_`` token from the admin UI; apps created in
the Dev Dashboard do not. So a token now comes from one of three places, and
this module resolves whichever applies:

  1. **A static token in config.** Either a legacy ``shpat_`` token from a
     pre-2026 custom app, or a non-expiring offline token obtained once through
     the authorization code grant (``./shopify.sh auth``). Nothing to refresh.

  2. **Client credentials grant.** POST the app's client id and secret, get a
     token good for 24 hours. No browser, no callback -- but it only works when
     the app and the store belong to the same Shopify organization.

  3. **A cached offline token with a refresh token.** If the authorization code
     grant was run with ``expiring=1``, the access token lasts about an hour and
     comes with a 90-day refresh token; each refresh issues a fresh pair.

Resolution is lazy and cached, so a run that makes four hundred API calls
fetches a token at most once.

**Cache location.** Tokens are cached under ``~/.cache/shopify-tasks/`` rather
than in the repo, deliberately: this repo lives inside a Dropbox tree, and a
token file written beside the code would be synced to Dropbox and to every
machine linked to that account. The file is written 0600.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from .config import Shop
from .errors import AuthError, ConfigError

log = logging.getLogger(__name__)

CACHE_ENV = "SHOPIFY_TASKS_CACHE"

# Refresh this far ahead of expiry. A 24h token with a 5-minute cushion still
# only refreshes once a day, and the cushion covers clock skew plus the time a
# long-running task spends between acquiring the token and using it.
EXPIRY_SKEW_SECONDS = 300

OAUTH_TOKEN_PATH = "/admin/oauth/access_token"
OAUTH_AUTHORIZE_PATH = "/admin/oauth/authorize"


def cache_path() -> Path:
    """Where cached tokens live. Never inside the repo -- see module docstring."""
    override = os.environ.get(CACHE_ENV)
    if override:
        return Path(override)
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "shopify-tasks" / "tokens.json"


@dataclass
class TokenRecord:
    """One cached token and everything needed to decide whether it is still good."""

    access_token: str
    # None means "does not expire" -- a legacy or non-expiring offline token.
    expires_at: float | None = None
    refresh_token: str | None = None
    refresh_token_expires_at: float | None = None
    scope: str = ""

    def is_fresh(self, *, now: float | None = None, skew: float = EXPIRY_SKEW_SECONDS) -> bool:
        if self.expires_at is None:
            return True
        return (now if now is not None else time.time()) < self.expires_at - skew

    def can_refresh(self, *, now: float | None = None) -> bool:
        if not self.refresh_token:
            return False
        if self.refresh_token_expires_at is None:
            return True
        return (now if now is not None else time.time()) < self.refresh_token_expires_at

    def to_json(self) -> dict:
        return {
            "access_token": self.access_token,
            "expires_at": self.expires_at,
            "refresh_token": self.refresh_token,
            "refresh_token_expires_at": self.refresh_token_expires_at,
            "scope": self.scope,
        }

    @classmethod
    def from_json(cls, raw: dict) -> "TokenRecord":
        return cls(
            access_token=raw["access_token"],
            expires_at=raw.get("expires_at"),
            refresh_token=raw.get("refresh_token"),
            refresh_token_expires_at=raw.get("refresh_token_expires_at"),
            scope=raw.get("scope", ""),
        )

    def __repr__(self) -> str:
        # Defensive: keep the token itself out of tracebacks and debug logs.
        when = "never" if self.expires_at is None else f"{self.expires_at:.0f}"
        return f"TokenRecord(expires_at={when}, refreshable={bool(self.refresh_token)})"


class TokenStore:
    """A small JSON file of cached tokens, keyed by shop.

    Not locked across processes: two concurrent runs might both fetch a token,
    and the loser's write simply wins. That is harmless -- Shopify issues
    independent tokens and both remain valid -- and it avoids a lockfile for a
    file that is written roughly once a day.
    """

    def __init__(self, path: Path | None = None):
        self.path = path or cache_path()
        self._lock = threading.Lock()

    @staticmethod
    def key(shop: Shop) -> str:
        return f"{shop.id}:{shop.domain}"

    def _read_all(self) -> dict:
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt cache must not break a run: the worst case is one extra
            # token fetch, which is cheap and self-healing.
            log.warning("Ignoring unreadable token cache %s: %s", self.path, exc)
            return {}

    def get(self, shop: Shop) -> TokenRecord | None:
        with self._lock:
            raw = self._read_all().get(self.key(shop))
        if not raw:
            return None
        try:
            return TokenRecord.from_json(raw)
        except KeyError:
            return None

    def put(self, shop: Shop, record: TokenRecord) -> None:
        with self._lock:
            data = self._read_all()
            data[self.key(shop)] = record.to_json()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Write private from the outset: creating the file world-readable and
            # then chmod'ing leaves a window where the token is exposed.
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
            tmp.replace(self.path)

    def forget(self, shop: Shop) -> None:
        with self._lock:
            data = self._read_all()
            if data.pop(self.key(shop), None) is None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            tmp.replace(self.path)


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------

def _post_token(shop: Shop, payload: dict, session: requests.Session | None = None) -> dict:
    """POST to the OAuth token endpoint and return the decoded response.

    All three grants (client credentials, authorization code, refresh) use this
    one endpoint and differ only in the form fields.
    """
    url = f"https://{shop.domain}{OAUTH_TOKEN_PATH}"
    http = session or requests
    try:
        response = http.post(url, data=payload, timeout=(10.0, 30.0))
    except requests.RequestException as exc:
        raise AuthError(
            f"Could not reach the token endpoint for {shop.id} ({shop.domain}): {exc}",
            shop=shop.id,
        ) from exc

    if response.status_code >= 400:
        raise AuthError(_token_error_message(shop, response, payload.get("grant_type")),
                        status=response.status_code, shop=shop.id)
    try:
        return response.json()
    except ValueError as exc:
        raise AuthError(
            f"Token endpoint for {shop.id} returned a non-JSON response: "
            f"{response.text[:200]}",
            shop=shop.id,
        ) from exc


def _token_error_message(shop: Shop, response, grant_type: str | None) -> str:
    detail = response.text[:300]
    base = (f"Token request for {shop.id} ({shop.domain}) failed with "
            f"HTTP {response.status_code}: {detail}")
    if grant_type == "client_credentials":
        # By far the most common cause, and the error text does not say it.
        base += (
            "\n\nThe client credentials grant only works when the app and the store "
            "belong to the SAME Shopify organization, and the store must appear under "
            "'Dev stores' in your Dev Dashboard. For a store outside the app's "
            "organization, use the one-time browser flow instead:\n"
            "    ./shopify.sh auth --shop " + shop.id
        )
    elif grant_type == "refresh_token":
        base += ("\n\nThe refresh token may have expired (they last 90 days). "
                 "Re-run:  ./shopify.sh auth --shop " + shop.id)
    return base


def _record_from_response(body: dict, *, now: float | None = None) -> TokenRecord:
    """Normalize a token response into a TokenRecord.

    ``expires_in`` absent means a non-expiring offline token, which is what the
    authorization code grant returns when ``expiring=1`` is not requested.
    """
    now = now if now is not None else time.time()
    token = body.get("access_token")
    if not token:
        raise AuthError(f"Token response contained no access_token: {body}")

    expires_in = body.get("expires_in")
    refresh_expires_in = body.get("refresh_token_expires_in")
    return TokenRecord(
        access_token=token,
        expires_at=(now + float(expires_in)) if expires_in else None,
        refresh_token=body.get("refresh_token"),
        refresh_token_expires_at=(now + float(refresh_expires_in)) if refresh_expires_in else None,
        scope=body.get("scope", ""),
    )


def client_credentials_grant(shop: Shop, session=None) -> TokenRecord:
    """Exchange the app's client id/secret for a ~24h token. No browser needed."""
    body = _post_token(shop, {
        "grant_type": "client_credentials",
        "client_id": shop.client_id,
        "client_secret": shop.client_secret,
    }, session)
    return _record_from_response(body)


def refresh_grant(shop: Shop, refresh_token: str, session=None) -> TokenRecord:
    """Trade a refresh token for a fresh access/refresh pair."""
    body = _post_token(shop, {
        "grant_type": "refresh_token",
        "client_id": shop.client_id,
        "client_secret": shop.client_secret,
        "refresh_token": refresh_token,
    }, session)
    return _record_from_response(body)


def authorization_code_grant(
    shop: Shop, code: str, *, expiring: bool = False, session=None
) -> TokenRecord:
    """Exchange an OAuth callback code for an offline token.

    Without ``expiring``, the returned offline token does not expire -- which is
    what makes this the closest replacement for the retired static ``shpat_``
    token. With it, you get a ~1h token plus a 90-day refresh token.
    """
    payload = {
        "client_id": shop.client_id,
        "client_secret": shop.client_secret,
        "code": code,
    }
    if expiring:
        payload["expiring"] = "1"
    return _record_from_response(_post_token(shop, payload, session))


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

class TokenProvider:
    """Resolves (and re-resolves) the access token for one shop.

    Handed to ShopifyClient, which calls ``token()`` per request and
    ``invalidate()`` on a 401 so an expired-early token costs one retry rather
    than a failed run.
    """

    def __init__(self, shop: Shop, *, store: TokenStore | None = None, session=None):
        self.shop = shop
        self.store = store if store is not None else TokenStore()
        self.session = session
        self._record: TokenRecord | None = None
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            if self._record is not None and self._record.is_fresh():
                return self._record.access_token
            self._record = self._resolve()
            return self._record.access_token

    def invalidate(self) -> bool:
        """Drop the current token. Returns True if another can be obtained.

        A static token cannot be re-fetched, so a 401 against one is final and
        there is no point retrying.
        """
        with self._lock:
            self._record = None
            if self.shop.access_token:
                return False
            self.store.forget(self.shop)
            return bool(self.shop.client_id and self.shop.client_secret)

    def _resolve(self) -> TokenRecord:
        shop = self.shop

        # 1. A static token in config: legacy shpat_, or a non-expiring offline
        #    token. Nothing to fetch or refresh.
        if shop.access_token:
            return TokenRecord(access_token=shop.access_token)

        # 2. A cached token that is still good.
        cached = self.store.get(shop)
        if cached and cached.is_fresh():
            log.debug("Using cached token for %s", shop.id)
            return cached

        # 3. A cached refresh token.
        if cached and cached.can_refresh():
            log.info("Refreshing expired access token for %s", shop.id)
            record = refresh_grant(shop, cached.refresh_token, self.session)
            self.store.put(shop, record)
            return record

        # 4. Client credentials.
        if shop.client_id and shop.client_secret:
            log.info("Requesting a new access token for %s (client credentials)", shop.id)
            record = client_credentials_grant(shop, self.session)
            self.store.put(shop, record)
            return record

        raise ConfigError(
            f"No way to authenticate as {shop.id} ({shop.domain}).\n"
            f"Give the shop either an access_token, or a client_id and client_secret "
            f"in config.toml.\n"
            f"If you have the app's credentials but no token yet, run:\n"
            f"    ./shopify.sh auth --shop {shop.id}"
        )
