#!/usr/bin/env python3
"""One-time OAuth flow to obtain an access token for a store.

Run this once per store. It opens your browser, you approve the app, and it
prints a token to paste into config.toml.

Needed because Shopify retired admin-created custom apps on 2026-01-01 -- the
ones that showed you a permanent ``shpat_`` token in the admin UI. A Dev
Dashboard app gets its token one of two ways, and this task covers the one that
works on a store outside the app's own organization.

The token this produces is an **offline** token: tied to the app and the store
rather than to your staff login, and (unless you pass ``--expiring``) it does
not expire. That makes it a drop-in replacement for the old static token.

Prerequisites, set in the Dev Dashboard (dev.shopify.com/dashboard) under your
app's configuration:

  * **App URL**: ``http://localhost:5004``
  * **Allowed redirection URL(s)**: ``http://localhost:5004/callback``

Both must be present, and they must share a host -- Shopify rejects the callback
with "redirect_uri and application url must have matching hosts" otherwise.
"""
from __future__ import annotations

import hashlib
import hmac
import http.server
import re
import secrets
import socket
import threading
import urllib.parse
import webbrowser

from shopifylib import auth, output
from shopifylib.errors import TaskError
from shopifylib.task import task

# Matches the port registered in the Dev Dashboard. Changing it means changing
# the App URL and redirect URL there too, which is why it is not arbitrary:
# 5000 is macOS AirPlay, 5001-5003 are other projects in this tree.
DEFAULT_PORT = 5004
CALLBACK_PATH = "/callback"

# Anchored at both ends: an unanchored match would accept
# "evil.myshopify.com.attacker.example" as a Shopify domain.
_SHOP_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]*\.myshopify\.com$")


def add_arguments(parser):
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"local callback port (default {DEFAULT_PORT}); must match "
                             f"the redirect URL registered in the Dev Dashboard")
    parser.add_argument("--expiring", action="store_true",
                        help="request a ~1h token plus a 90-day refresh token instead "
                             "of a non-expiring offline token")
    parser.add_argument("--no-browser", action="store_true",
                        help="print the authorization URL instead of opening a browser")
    parser.add_argument("--scopes", metavar="LIST",
                        help="comma-separated scopes to request (default: everything "
                             "the tasks in this repo declare)")
    parser.add_argument("--print-only", action="store_true",
                        help="show the token but do not write it to the token cache")


@task(
    name="auth",
    help="One-time browser login to get an access token for a store",
)
def run(ctx, args):
    """One-time browser login to get an access token for a store.

    Opens Shopify's authorization page, captures the callback on localhost, and
    exchanges the code for an offline access token.

    Requires client_id and client_secret in config.toml for this shop, and the
    matching App URL / redirect URL registered in the Dev Dashboard.
    """
    shop = ctx.shop

    if not (shop.client_id and shop.client_secret):
        raise TaskError(
            f"Shop {shop.id!r} has no client_id/client_secret in config.toml.\n"
            f"Find them in the Shopify Dev Dashboard (dev.shopify.com/dashboard) under "
            f"your app > Client credentials, then add:\n\n"
            f"  [[shops]]\n"
            f"  id = \"{shop.id}\"\n"
            f"  domain = \"{shop.domain}\"\n"
            f"  client_id = \"...\"\n"
            f"  client_secret = \"...\"\n"
        )

    scopes = args.scopes or _default_scopes()
    redirect_uri = f"http://localhost:{args.port}{CALLBACK_PATH}"
    state = secrets.token_urlsafe(32)

    authorize_url = (
        f"https://{shop.domain}{auth.OAUTH_AUTHORIZE_PATH}?"
        + urllib.parse.urlencode({
            "client_id": shop.client_id,
            "scope": scopes,
            "redirect_uri": redirect_uri,
            "state": state,
        })
    )

    _preflight_port(args.port)

    ctx.log.info("Requesting scopes: %s", scopes)
    ctx.log.info("Callback listening on %s", redirect_uri)

    catcher = _CallbackCatcher(state=state, client_secret=shop.client_secret,
                               expected_shop=shop.domain, port=args.port)

    with catcher:
        if args.no_browser:
            print("\nOpen this URL in your browser to authorize the app:\n")
            print(f"  {authorize_url}\n")
        else:
            print(f"\nOpening your browser to authorize {shop.domain} ...")
            print("If it doesn't open, visit:\n")
            print(f"  {authorize_url}\n")
            webbrowser.open(authorize_url)

        code = catcher.wait()

    ctx.log.info("Authorization received; exchanging for an access token.")
    record = auth.authorization_code_grant(shop, code, expiring=args.expiring)

    _warn_on_missing_scopes(ctx, requested=scopes, granted=record.scope)

    if not args.print_only:
        auth.TokenStore().put(shop, record)
        ctx.log.info("Token cached at %s", auth.cache_path())

    if ctx.json_output:
        output.emit_json({
            "shop": shop.id,
            "access_token": record.access_token,
            "scope": record.scope,
            "expires_at": record.expires_at,
            "refresh_token": record.refresh_token,
        })
        return

    print("\n" + "=" * 68)
    print(f"Access token for {shop.id} ({shop.domain}):\n")
    print(f"  {record.access_token}\n")
    if record.expires_at is None:
        print("This token does not expire. Add it to config.toml to avoid")
        print("depending on the cache:\n")
        print(f"  [[shops]]")
        print(f"  id = \"{shop.id}\"")
        print(f"  access_token = \"{record.access_token}\"\n")
        print("Or keep it out of the file entirely:\n")
        print(f"  export SHOPIFY_TOKEN_{shop.id.upper().replace('-', '_')}"
              f"='{record.access_token}'")
    else:
        print("This token expires, but a refresh token was issued alongside it,")
        print("so it will be renewed automatically. Nothing to copy.")
    print("=" * 68)
    print("\nVerify with:  ./shopify.sh shop-info --shop " + shop.id)


def _default_scopes() -> str:
    """Union of every scope declared by a registered task.

    Requesting the union once means you approve the app a single time rather
    than re-running this each time a new task needs something. Shopify grants
    only what the app is configured for, so this cannot exceed the Dev Dashboard
    settings -- it just avoids asking for less than you'll need.
    """
    from shopifylib.task import REGISTRY

    scopes = {scope for spec in REGISTRY.values() for scope in spec.scopes}
    return ",".join(sorted(scopes)) if scopes else "read_products"


def _warn_on_missing_scopes(ctx, *, requested: str, granted: str) -> None:
    """Flag scopes we asked for but did not get.

    A write scope implies its read counterpart, so granted lists often omit the
    read form -- comparing naively would cry wolf on every run.
    """
    if not granted:
        return
    granted_set = set(filter(None, (s.strip() for s in granted.split(","))))
    implied = {s.replace("write_", "read_", 1) for s in granted_set if s.startswith("write_")}
    have = granted_set | implied

    missing = sorted(
        s for s in (t.strip() for t in requested.split(",")) if s and s not in have
    )
    if missing:
        ctx.log.warning(
            "These scopes were requested but not granted: %s\n"
            "Add them to the app's 'Scopes' field in the Dev Dashboard, then re-run "
            "this command.",
            ", ".join(missing),
        )


def _preflight_port(port: int) -> None:
    """Fail early and clearly if the callback port is already taken.

    Otherwise the browser opens, you approve the app, and the callback lands on
    whatever else owns the port -- burning the one-time code with a confusing
    error.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError as exc:
        raise TaskError(
            f"Port {port} is already in use ({exc}).\n"
            f"Either free it, or pass --port N -- but if you change it you must also "
            f"update the App URL and Allowed redirection URL in the Dev Dashboard, "
            f"since Shopify requires them to match."
        ) from exc
    finally:
        probe.close()


class _CallbackCatcher:
    """A one-shot localhost HTTP server that captures the OAuth callback.

    Validates the callback before accepting it -- state nonce, HMAC signature,
    and shop domain -- because this endpoint is reachable by anything running on
    the machine, and an unvalidated code exchange is a CSRF foothold.
    """

    TIMEOUT_SECONDS = 300

    def __init__(self, *, state: str, client_secret: str, expected_shop: str, port: int):
        self.state = state
        self.client_secret = client_secret
        self.expected_shop = expected_shop
        self.port = port
        self.code: str | None = None
        self.error: str | None = None
        self._done = threading.Event()
        self._server: http.server.HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_CallbackCatcher":
        catcher = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 -- name fixed by BaseHTTPRequestHandler
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != CALLBACK_PATH:
                    self.send_error(404)
                    return
                params = {
                    k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()
                }
                ok, message = catcher._validate(params)
                body = _RESULT_PAGE.format(
                    heading="Authorized" if ok else "Authorization failed",
                    message=message,
                    color="#1a7f37" if ok else "#cf222e",
                ).encode()
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                catcher._done.set()

            def log_message(self, *a):
                pass  # the task's own logging is the user-facing narration

        self._server = http.server.HTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def wait(self) -> str:
        if not self._done.wait(self.TIMEOUT_SECONDS):
            raise TaskError(
                f"Timed out after {self.TIMEOUT_SECONDS}s waiting for the browser "
                f"callback. Re-run when you're ready to complete the login."
            )
        if self.error:
            raise TaskError(f"Authorization failed: {self.error}{self._hint()}")
        if not self.code:
            raise TaskError("Callback arrived without an authorization code.")
        return self.code

    def _hint(self) -> str:
        """Terminal-side guidance for a shop mismatch.

        Kept out of ``_validate`` so the browser page stays short while the
        terminal, where the retry actually happens, gets the full recipe.

        The likeliest cause is not what it looks like. A store can answer on
        more than one myshopify domain -- a readable alias alongside the
        random handle Shopify generates -- and OAuth always reports the
        canonical one. So a mismatch usually means the configured domain is an
        alias for this very store, not that the wrong store was authorized.
        That case is listed first because it is both more common and the one
        that looks most alarming.
        """
        if not self.error or "but this run targets" not in self.error:
            return ""
        reported = self.error.split("callback was for ", 1)[-1].split(",", 1)[0]
        return (
            f"\n\n"
            f"Most likely these are the SAME store under two domains. Shopify gives "
            f"a store a permanent random handle and may also answer on a readable "
            f"alias, and OAuth always reports the canonical one.\n\n"
            f"Check whether both serve the same storefront:\n"
            f"    curl -sIL -o /dev/null -w '%{{url_effective}}\\n' "
            f"https://{self.expected_shop}\n"
            f"    curl -sIL -o /dev/null -w '%{{url_effective}}\\n' https://{reported}\n\n"
            f"Same URL means one store: set domain = \"{reported}\" in config.toml "
            f"and re-run.\n\n"
            f"Different URLs mean you really did authorize another store -- Shopify "
            f"follows the store your browser is signed into. Sign in to "
            f"https://{self.expected_shop}/admin and re-run, or add {reported} to "
            f"config.toml as its own [[shops]] entry and use --shop."
        )

    def _validate(self, params: dict) -> tuple[bool, str]:
        """Check the callback. Returns (ok, message shown in the browser)."""
        if "error" in params:
            self.error = params.get("error_description") or params["error"]
            return False, self.error

        # 1. State nonce -- proves this callback answers our request.
        if not hmac.compare_digest(params.get("state", ""), self.state):
            self.error = "state mismatch (possible CSRF); nothing was exchanged"
            return False, self.error

        # 2. Shop domain -- anchored, so a lookalike host is rejected.
        shop = params.get("shop", "")
        if not _SHOP_RE.match(shop):
            self.error = f"callback named an invalid shop domain: {shop!r}"
            return False, self.error
        if shop != self.expected_shop:
            # Reaching here means state and the domain shape were fine, so
            # Shopify really did process our request -- it just resolved it
            # against a different store. The authorize endpoint follows the
            # browser's signed-in store, not only the domain in the URL, so the
            # usual cause is being logged into the wrong one.
            self.error = (
                f"callback was for {shop}, but this run targets "
                f"{self.expected_shop}"
            )
            return False, (
                f"This authorized <b>{shop}</b>, but the run targets "
                f"<b>{self.expected_shop}</b>.<br><br>"
                f"Shopify authorizes whichever store your browser is signed into. "
                f"Sign in to <code>{self.expected_shop}</code> first, then re-run."
            )

        # 3. HMAC -- proves Shopify sent it. Constant-time compare.
        if not self._hmac_ok(params):
            self.error = "HMAC verification failed; the callback was not signed by Shopify"
            return False, self.error

        code = params.get("code")
        if not code:
            self.error = "callback contained no authorization code"
            return False, self.error

        self.code = code
        return True, "You can close this tab and return to the terminal."

    def _hmac_ok(self, params: dict) -> bool:
        provided = params.get("hmac", "")
        if not provided:
            return False
        # Everything except hmac itself, sorted, urlencoded -- Shopify's scheme.
        message = "&".join(
            f"{k}={v}" for k, v in sorted(params.items()) if k != "hmac"
        )
        expected = hmac.new(
            self.client_secret.encode(), message.encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, provided)


_RESULT_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>shopify-tasks</title></head>
<body style="font-family:system-ui,-apple-system,sans-serif;max-width:32rem;
             margin:15vh auto;padding:0 1.5rem;line-height:1.5">
  <h1 style="color:{color};font-size:1.5rem;margin:0 0 .5rem">{heading}</h1>
  <p style="color:#57606a;margin:0">{message}</p>
</body></html>
"""
