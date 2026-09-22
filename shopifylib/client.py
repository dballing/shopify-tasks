#!/usr/bin/env python3
"""The GraphQL Admin API client every task goes through.

Tasks never touch ``requests`` directly. Everything that is tedious-but-required
about talking to Shopify lives here, so a task module can be a query, a loop,
and some domain logic:

  * pinned API version and auth header
  * proactive rate-limit pacing (throttle.Budget) and reactive THROTTLED retries
  * retry with backoff on 429/5xx/connection failures
  * the three error layers untangled into distinct exception types
  * the write guard that makes dry-run mode structural

Only the GraphQL Admin API is supported. The REST Admin API was marked legacy on
2024-10-01 and new apps have been GraphQL-only since 2025-04-01, so building a
REST path now would be building something already scheduled for removal.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Any

import requests

from .config import Shop
from .errors import (
    AccessDeniedError,
    AuthError,
    GraphQLError,
    NotFoundError,
    RetriesExhausted,
    ThrottledError,
    TransportError,
    UserError,
    WriteBlocked,
)
from .throttle import Budget, Cost

log = logging.getLogger(__name__)

# Statuses worth retrying. 429 shouldn't normally reach us (GraphQL reports
# throttling in the body, not the status) but Shopify's edge can still emit one
# under load, and 5xx is transient by definition.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# Strips comments and leading whitespace so the write guard sees the real first
# keyword of the document rather than a comment or blank line.
_LEADING_JUNK_RE = re.compile(r"(?:\s|#[^\n]*\n?)+")

_DEFAULT_TIMEOUT = (10.0, 60.0)  # (connect, read); exports can be slow to first byte


def is_mutation(document: str) -> bool:
    """True if a GraphQL document's first operation is a mutation.

    Used by the write guard. GraphQL permits a document to open with comments,
    whitespace, or the shorthand ``{...}`` query form, so this strips leading
    junk and looks at the first real token. Shorthand and ``query`` are reads;
    only an explicit ``mutation`` keyword writes.

    Conservative by construction: anything it cannot parse as a read is treated
    as a read, because the guard's job is to catch accidental writes, and the
    tasks in this repo always name their operations. The belt-and-braces check
    is that every write also goes through ``TaskContext.mutate()``.
    """
    stripped = _LEADING_JUNK_RE.sub("", document or "", count=1).lstrip()
    return stripped.startswith("mutation")


class ShopifyClient:
    """A GraphQL Admin API client bound to one shop.

    Stateful on purpose: the rate-limit budget and the HTTP connection pool both
    need to live across calls, which is what turns a few hundred small queries
    from a stuttering mess into a steady stream.
    """

    MAX_ATTEMPTS = 5
    MAX_THROTTLE_RETRIES = 8

    def __init__(
        self,
        shop: Shop,
        *,
        writes_enabled: bool = False,
        session: requests.Session | None = None,
        required_scopes: list[str] | None = None,
        timeout: tuple[float, float] = _DEFAULT_TIMEOUT,
        sleeper=time.sleep,
        token_provider=None,
    ):
        self.shop = shop
        # The backstop behind dry-run. Set only by the dispatcher, only when
        # --commit was passed. See WriteBlocked in errors.py for the reasoning.
        self.writes_enabled = writes_enabled
        self.required_scopes = required_scopes or []
        self.timeout = timeout
        self._sleep = sleeper
        capacity, restore_rate = shop.bucket
        self.budget = Budget(capacity, restore_rate, sleeper=sleeper)
        self.session = session or requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "shopify-tasks/0.1 (+https://github.com/dballing/shopify-tasks)",
        })
        # Resolved lazily and per request rather than pinned in a header at
        # construction: a client-credentials token lasts 24h, so a long-lived
        # process (or a run that starts near expiry) needs to pick up a fresh
        # one without being rebuilt.
        if token_provider is None:
            from .auth import TokenProvider
            token_provider = TokenProvider(shop)
        self.tokens = token_provider
        self.request_count = 0

    # -- public API --------------------------------------------------------

    def execute(
        self,
        document: str,
        variables: dict[str, Any] | None = None,
        *,
        operation_name: str | None = None,
        allow_write: bool = False,
    ) -> dict:
        """Run a GraphQL document and return its ``data`` payload.

        Raises rather than returning on every failure mode -- see errors.py.
        ``allow_write`` is the explicit acknowledgement that a mutation is
        intended; it still requires ``writes_enabled`` on the client.
        """
        if is_mutation(document):
            self._guard_write(document, allow_write)

        payload: dict[str, Any] = {"query": document}
        if variables:
            payload["variables"] = variables
        if operation_name:
            payload["operationName"] = operation_name

        for throttle_attempt in range(self.MAX_THROTTLE_RETRIES):
            self.budget.acquire()
            body = self._post(payload)

            cost = Cost.parse(body.get("extensions"))
            errors = body.get("errors")

            if errors:
                if self._is_throttled(errors):
                    delay = self.budget.penalize(cost)
                    log.debug(
                        "THROTTLED on %s; sleeping %.2fs (attempt %d/%d)",
                        self.shop.id, delay, throttle_attempt + 1, self.MAX_THROTTLE_RETRIES,
                    )
                    self._sleep(delay)
                    continue
                self.budget.update(cost)
                self._raise_graphql(errors, document)

            self.budget.update(cost)

            data = body.get("data")
            if data is None:
                # No errors and no data is not a documented response; surfacing
                # it as a GraphQL error beats returning None into task code that
                # will dereference it two lines later.
                raise GraphQLError(
                    [{"message": "Response contained neither 'data' nor 'errors'."}],
                    query=document,
                )
            return data

        raise RetriesExhausted(
            f"Still rate-limited after {self.MAX_THROTTLE_RETRIES} attempts against "
            f"{self.shop.id}. Another process may be sharing this app's cost bucket.",
            attempts=self.MAX_THROTTLE_RETRIES,
            shop=self.shop.id,
        )

    def mutate(
        self,
        document: str,
        variables: dict[str, Any] | None = None,
        *,
        field: str | None = None,
        operation_name: str | None = None,
        check_errors: bool = True,
    ) -> dict:
        """Run a mutation and check its ``userErrors`` before returning.

        ``field`` names the mutation field in the payload (e.g.
        ``productVariantsBulkUpdate``). Given it, the returned value is that
        field's payload with its userErrors already verified empty. Without it,
        every top-level field in the response is scanned for userErrors, which
        is the safe default but a less precise error message.

        Use this rather than ``execute`` for writes. A mutation whose userErrors
        go unchecked reports success while changing nothing.

        ``check_errors=False`` returns the payload with its userErrors intact
        instead of raising. That exists for batch mutations that need to tell
        *which* entries failed and retry the rest -- a partial failure there is
        an expected outcome, not an exception. **A caller that passes False owns
        the userErrors** and must inspect them; ignoring them reintroduces
        exactly the silent-success bug this method exists to prevent. Nothing
        else should use it.
        """
        data = self.execute(
            document, variables, operation_name=operation_name, allow_write=True
        )
        if field is not None:
            payload = data.get(field)
            if payload is None:
                raise GraphQLError(
                    [{"message": f"Mutation response has no field {field!r}.",
                      "path": [field]}],
                    query=document,
                )
            if check_errors:
                check_user_errors(payload, mutation=field)
            return payload

        if check_errors:
            for name, payload in data.items():
                if isinstance(payload, dict):
                    check_user_errors(payload, mutation=name)
        return data

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "ShopifyClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- internals ---------------------------------------------------------

    def _guard_write(self, document: str, allow_write: bool) -> None:
        if not self.writes_enabled:
            raise WriteBlocked(
                "This run is a dry run, but a mutation reached the API client.\n"
                "Nothing was sent. Re-run with --commit to write for real.\n"
                "(If you are seeing this from inside task code, that task has a "
                "write path that does not check ctx.dry_run -- use ctx.mutate().)"
            )
        if not allow_write:
            # Writes are enabled, but this call came through execute() without
            # declaring itself a write -- which means its userErrors are about
            # to go unchecked. Refusing here is what keeps mutate() the only
            # practical way to write.
            raise WriteBlocked(
                "A mutation was passed to execute() without allow_write=True.\n"
                "Use client.mutate() (or ctx.mutate()) so userErrors get checked -- "
                "a mutation that fails its business rules returns HTTP 200."
            )

    def _post(self, payload: dict) -> dict:
        """POST with retry/backoff, returning the decoded JSON body."""
        last_exc: Exception | None = None
        token_retried = False

        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                self.request_count += 1
                response = self.session.post(
                    self.shop.endpoint,
                    json=payload,
                    timeout=self.timeout,
                    headers={"X-Shopify-Access-Token": self.tokens.token()},
                )
            except requests.RequestException as exc:
                # Connection reset, DNS blip, read timeout: transient by nature.
                last_exc = exc
                if attempt == self.MAX_ATTEMPTS:
                    break
                self._backoff(attempt, reason=type(exc).__name__)
                continue

            status = response.status_code

            if status == 401 and not token_retried:
                # A fetched token may lapse mid-run (client-credentials tokens
                # last 24h) or be revoked. Drop it and try once more before
                # concluding the credentials are wrong. A static token cannot be
                # re-fetched, so invalidate() says so and we fall through.
                token_retried = True
                if self.tokens.invalidate():
                    log.info("Access token for %s rejected; fetching a new one.",
                             self.shop.id)
                    continue

            if status in (401, 403):
                raise AuthError(self._auth_message(status), status=status, shop=self.shop.id)
            if status == 404:
                raise NotFoundError(
                    f"404 from {self.shop.endpoint}\n"
                    f"Either the shop domain {self.shop.domain!r} is wrong, or API version "
                    f"{self.shop.api_version!r} is no longer supported (Shopify retires "
                    f"versions about a year after release). Try a current version.",
                    status=status,
                    shop=self.shop.id,
                )
            if status in _RETRY_STATUSES:
                last_exc = TransportError(
                    f"HTTP {status} from {self.shop.id}", status=status, shop=self.shop.id
                )
                if attempt == self.MAX_ATTEMPTS:
                    break
                self._backoff(attempt, reason=f"HTTP {status}",
                              retry_after=response.headers.get("Retry-After"))
                continue
            if status >= 400:
                raise TransportError(
                    f"HTTP {status} from {self.shop.id}: {response.text[:500]}",
                    status=status,
                    shop=self.shop.id,
                )

            try:
                return response.json()
            except json.JSONDecodeError as exc:
                # A 200 that isn't JSON generally means an edge/proxy interposed
                # an HTML error page -- transient, so retry rather than fail.
                last_exc = TransportError(
                    f"Non-JSON 200 response from {self.shop.id}: {response.text[:200]}",
                    status=status,
                    shop=self.shop.id,
                )
                if attempt == self.MAX_ATTEMPTS:
                    break
                self._backoff(attempt, reason="non-JSON response")
                continue

        raise RetriesExhausted(
            f"Gave up after {self.MAX_ATTEMPTS} attempts against {self.shop.id}: {last_exc}",
            attempts=self.MAX_ATTEMPTS,
            last=last_exc,
            shop=self.shop.id,
        )

    def _backoff(self, attempt: int, *, reason: str, retry_after: str | None = None) -> None:
        """Exponential backoff with jitter, honoring Retry-After when present.

        Jitter matters even single-threaded: without it, a task retrying against
        a struggling endpoint lands its retries on the same cadence every time.
        """
        delay = min(2.0 ** (attempt - 1), 30.0)
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass  # Retry-After may be an HTTP-date; the default is fine.
        delay += random.uniform(0, delay * 0.25)
        log.warning(
            "%s from %s; retrying in %.1fs (attempt %d/%d)",
            reason, self.shop.id, delay, attempt, self.MAX_ATTEMPTS,
        )
        self._sleep(delay)

    def _auth_message(self, status: int) -> str:
        base = (
            f"HTTP {status} from {self.shop.id} ({self.shop.domain}).\n"
            f"The Admin API rejected the access token."
        )
        hints = [
            "Check that the token belongs to this store and has not been revoked",
            "(Settings > Apps and sales channels > Develop apps > your app > API credentials).",
        ]
        if self.required_scopes:
            hints.append(
                f"This task needs these scopes: {', '.join(self.required_scopes)}. "
                f"Granting a new scope requires reinstalling the custom app."
            )
        return base + "\n" + " ".join(hints)

    @staticmethod
    def _is_throttled(errors: list[dict]) -> bool:
        return any(
            (err.get("extensions") or {}).get("code") == "THROTTLED" for err in errors
        )

    def _raise_graphql(self, errors: list[dict], document: str) -> None:
        codes = {(err.get("extensions") or {}).get("code") for err in errors}
        if "ACCESS_DENIED" in codes:
            raise AccessDeniedError(
                errors, required_scopes=self.required_scopes, query=document
            )
        if "THROTTLED" in codes:
            raise ThrottledError(errors, query=document)
        raise GraphQLError(errors, query=document)


def collect_user_errors(payload: dict) -> list[dict]:
    """Every userError in a mutation payload, without raising.

    Shopify names this field inconsistently across mutations -- ``userErrors``
    is the standard, but several product and media mutations return a typed
    variant (older ones used ``mediaUserErrors``, and a few carry both).
    Gathering every key ending in ``UserErrors`` catches all of them without a
    per-mutation lookup table.
    """
    if not isinstance(payload, dict):
        return []
    collected: list[dict] = []
    for key, value in payload.items():
        if (key == "userErrors" or key.endswith("UserErrors")) and value:
            collected.extend(value)
    return collected


def check_user_errors(payload: dict, *, mutation: str | None = None) -> None:
    """Raise ``UserError`` if a mutation payload reports any."""
    collected = collect_user_errors(payload)
    if collected:
        raise UserError(collected, mutation=mutation)
