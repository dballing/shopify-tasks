#!/usr/bin/env python3
"""Exception hierarchy for shopify-tasks.

The shape of this hierarchy mirrors the three *independent* layers at which a
Shopify Admin API call can fail. Collapsing them into one exception type is
tempting and wrong: they need different handling, and two of the three look like
success if you only check the HTTP status.

  1. HTTP            -> TransportError (and subclasses)
  2. GraphQL errors[] -> GraphQLError (and subclasses) -- arrives as HTTP 200
  3. payload userErrors -> UserError -- arrives as HTTP 200 with no errors[]

Layer 3 is the dangerous one. A mutation that violates a business rule returns
200, an empty ``errors`` array, and a payload whose ``userErrors`` list explains
that nothing happened. Code that checks only the status code records a
successful run that changed nothing -- so ``UserError`` exists to make that
condition loud.

Every exception here carries enough context to be printed verbatim to a user who
is not looking at the source: which shop, which field, which scope.
"""
from __future__ import annotations


class ShopifyTasksError(Exception):
    """Base for everything this project raises deliberately.

    ``cli.py`` catches this to print a clean message instead of a traceback; an
    escaping exception of any other type is a bug and *should* show its stack.
    """


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

class ConfigError(ShopifyTasksError):
    """config.toml is missing, malformed, or internally inconsistent.

    Raised during startup, before any network call. The message is expected to
    name the fix (which key, which shop id), because this is the error a new
    checkout hits first.
    """


# --------------------------------------------------------------------------
# Layer 1: HTTP transport
# --------------------------------------------------------------------------

class TransportError(ShopifyTasksError):
    """An HTTP-level failure: status code, connection, or timeout."""

    def __init__(self, message: str, *, status: int | None = None, shop: str | None = None):
        super().__init__(message)
        self.status = status
        self.shop = shop


class AuthError(TransportError):
    """401/403 from the Admin API.

    Almost always one of: a revoked or mistyped token, a token belonging to a
    different store, or a custom app whose scopes were never granted. The client
    folds the task's declared scopes into the message so the reader is told what
    to grant rather than left to guess.
    """


class NotFoundError(TransportError):
    """404 from the Admin API endpoint itself (not a missing resource).

    The GraphQL endpoint 404s for exactly two reasons worth distinguishing: the
    shop domain doesn't resolve to a store, or the pinned api_version has aged
    out of support. A missing *resource* is not this -- that comes back as a
    null field in a 200 response.
    """


class RetriesExhausted(TransportError):
    """A retryable failure (429, 5xx, connection reset) survived every attempt."""

    def __init__(self, message: str, *, attempts: int, last: Exception | None = None, **kw):
        super().__init__(message, **kw)
        self.attempts = attempts
        self.last = last


# --------------------------------------------------------------------------
# Layer 2: GraphQL top-level errors
# --------------------------------------------------------------------------

class GraphQLError(ShopifyTasksError):
    """One or more entries in the response's top-level ``errors`` array.

    These are document-level problems -- a malformed query, an unknown field, a
    denied scope -- as opposed to per-record business-rule failures, which are
    ``UserError``. Arrives as HTTP 200.
    """

    def __init__(self, errors: list[dict], *, query: str | None = None):
        self.errors = errors
        self.query = query
        super().__init__(self._format(errors))

    @staticmethod
    def _format(errors: list[dict]) -> str:
        parts = []
        for err in errors:
            msg = err.get("message", "(no message)")
            # 'path' locates the offending field in the document, which is the
            # difference between a usable error and "something was wrong".
            path = err.get("path")
            if path:
                msg = f"{msg} (at {'.'.join(str(p) for p in path)})"
            parts.append(msg)
        return "; ".join(parts)


class ThrottledError(GraphQLError):
    """Code ``THROTTLED``: the cost bucket was empty when the query arrived.

    Normally invisible to tasks -- the client sleeps on the reported restore
    rate and retries. It escapes only when retries are exhausted, which means
    the pacing in throttle.py is mis-tuned or something else is draining the
    same app's bucket concurrently.
    """


class AccessDeniedError(GraphQLError):
    """Code ``ACCESS_DENIED``: authenticated fine, but the token lacks a scope.

    Distinct from AuthError because the HTTP request itself succeeded; Shopify
    reports missing scopes for *specific fields* inside an otherwise valid
    query. The client attaches ``required_scopes`` from the running task's
    declared metadata so the message can name what to grant.
    """

    def __init__(self, errors: list[dict], *, required_scopes: list[str] | None = None, **kw):
        super().__init__(errors, **kw)
        self.required_scopes = required_scopes or []

    def __str__(self) -> str:
        base = super().__str__()
        if self.required_scopes:
            return f"{base}\nThis task declares these scopes: {', '.join(self.required_scopes)}"
        return base


# --------------------------------------------------------------------------
# Layer 3: mutation userErrors
# --------------------------------------------------------------------------

class UserError(ShopifyTasksError):
    """A mutation returned a non-empty ``userErrors`` -- it did nothing.

    HTTP 200, no top-level ``errors``, a well-formed payload. The only signal
    that the write failed is this list. Treating it as an exception rather than
    a return value is deliberate: it makes the failure impossible to ignore by
    omission, which is the whole point.
    """

    def __init__(self, user_errors: list[dict], *, mutation: str | None = None):
        self.user_errors = user_errors
        self.mutation = mutation
        super().__init__(self._format(user_errors, mutation))

    @staticmethod
    def _format(user_errors: list[dict], mutation: str | None) -> str:
        head = f"{mutation} reported userErrors" if mutation else "Mutation reported userErrors"
        lines = [head + ":"]
        for err in user_errors:
            # 'field' is a path list naming the offending input argument; 'code'
            # is an enum that is usually more actionable than the prose message.
            field = err.get("field")
            where = ".".join(str(f) for f in field) if field else "(no field)"
            code = err.get("code")
            msg = err.get("message", "(no message)")
            lines.append(f"  {where}: {msg}" + (f" [{code}]" if code else ""))
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Write safety
# --------------------------------------------------------------------------

class WriteBlocked(ShopifyTasksError):
    """A mutation was attempted through a client that has writes disabled.

    This is the backstop behind dry-run mode, not a routine control-flow signal.
    Tasks are expected to branch on ``ctx.dry_run`` (or simply use
    ``ctx.mutate()``, which does it for them) and never reach this. Seeing it
    means a task found a code path that writes without consulting the flag --
    which is exactly the bug the guard exists to catch, so it is an error and
    not a warning.
    """


class TaskError(ShopifyTasksError):
    """A task failed for its own reasons (bad input file, nothing to do, etc.).

    Raised by task code; the dispatcher prints the message and exits non-zero.
    """
