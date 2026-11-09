#!/usr/bin/env python3
"""OAuth callback validation in the `auth` task.

These are security tests. The localhost listener is reachable by anything
running on the machine, so every one of these checks is what stops an attacker
from driving a code exchange through it. If one of these starts failing, the
check it guards has been weakened -- that is the bug, not the test.
"""
from __future__ import annotations

import hashlib
import hmac
import urllib.parse

import pytest

from tasks import auth as auth_task

SECRET = "csec"
STATE = "the-state-nonce"
SHOP = "acme.myshopify.com"


def catcher(**kw):
    defaults = dict(state=STATE, client_secret=SECRET, expected_shop=SHOP, port=5004)
    defaults.update(kw)
    return auth_task._CallbackCatcher(**defaults)


def sign(params: dict, secret: str = SECRET) -> dict:
    """Add a valid Shopify HMAC to a set of callback params."""
    message = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k != "hmac")
    params = dict(params)
    params["hmac"] = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return params


def valid_params(**overrides) -> dict:
    params = {"code": "thecode", "shop": SHOP, "state": STATE, "timestamp": "1700000000"}
    params.update(overrides)
    return sign(params)


# ---------------------------------------------------------------------------
# Accepting a good callback
# ---------------------------------------------------------------------------

def test_valid_callback_is_accepted():
    c = catcher()
    ok, _ = c._validate(valid_params())
    assert ok and c.code == "thecode" and c.error is None


def test_extra_params_do_not_break_the_hmac():
    """Shopify adds fields over time; the signature covers whatever is present."""
    c = catcher()
    ok, _ = c._validate(valid_params(host="YWNtZQ"))
    assert ok


# ---------------------------------------------------------------------------
# State nonce (CSRF)
# ---------------------------------------------------------------------------

def test_wrong_state_is_rejected():
    c = catcher()
    ok, _ = c._validate(valid_params(state="attacker-supplied"))
    assert not ok and "state mismatch" in c.error
    assert c.code is None, "no code may be captured from an unverified callback"


def test_missing_state_is_rejected():
    c = catcher()
    params = sign({"code": "x", "shop": SHOP, "timestamp": "1"})
    ok, _ = c._validate(params)
    assert not ok and c.code is None


# ---------------------------------------------------------------------------
# HMAC
# ---------------------------------------------------------------------------

def test_forged_hmac_is_rejected():
    c = catcher()
    params = valid_params()
    params["hmac"] = "0" * 64
    ok, _ = c._validate(params)
    assert not ok and "HMAC" in c.error
    assert c.code is None


def test_missing_hmac_is_rejected():
    c = catcher()
    params = valid_params()
    del params["hmac"]
    ok, _ = c._validate(params)
    assert not ok and c.code is None


def test_hmac_signed_with_the_wrong_secret_is_rejected():
    c = catcher()
    ok, _ = c._validate(sign({"code": "x", "shop": SHOP, "state": STATE}, secret="wrong"))
    assert not ok and c.code is None


def test_tampered_code_invalidates_the_hmac():
    """Swapping in another code must not survive signature checking."""
    c = catcher()
    params = valid_params()
    params["code"] = "substituted-code"
    ok, _ = c._validate(params)
    assert not ok and c.code is None


# ---------------------------------------------------------------------------
# Shop domain
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("domain", [
    "acme.myshopify.com.attacker.example",   # suffix attack
    "attacker.example",
    "-acme.myshopify.com",                   # must start alphanumeric
    "acme.myshopify.com/evil",
    "",
])
def test_invalid_shop_domains_are_rejected(domain):
    c = catcher()
    ok, _ = c._validate(valid_params(shop=domain))
    assert not ok and c.code is None


def test_callback_for_a_different_store_is_rejected():
    """Valid Shopify domain, wrong store -- would write a token to the wrong shop."""
    c = catcher()
    ok, _ = c._validate(valid_params(shop="someone-else.myshopify.com"))
    assert not ok and "targets" in c.error
    assert c.code is None


def test_wrong_store_hint_leads_with_the_alias_explanation():
    """A store can answer on two myshopify domains, and OAuth reports the
    canonical one -- so the alarming-looking mismatch is usually one store."""
    c = catcher()
    # A Shopify-generated permanent handle, which is what OAuth reports when the
    # configured domain is the store's readable alias.
    canonical = "ab3xyz-qq.myshopify.com"
    c._validate(valid_params(shop=canonical))
    hint = c._hint()
    assert "SAME store" in hint
    assert f'domain = "{canonical}"' in hint   # the concrete fix
    assert hint.index("SAME store") < hint.index("signed into"), \
        "the alias case is commoner; it must come before the wrong-store case"


def test_wrong_store_hint_still_covers_a_genuinely_different_store():
    c = catcher()
    c._validate(valid_params(shop="someone-else.myshopify.com"))
    hint = c._hint()
    assert f"https://{SHOP}/admin" in hint     # sign in to the right one
    assert "--shop" in hint                    # or authorize that one on purpose


def test_no_hint_for_unrelated_failures():
    """A forged HMAC is not a wrong-store problem; don't misdirect."""
    c = catcher()
    params = valid_params()
    params["hmac"] = "0" * 64
    c._validate(params)
    assert c._hint() == ""


# ---------------------------------------------------------------------------
# Error callbacks
# ---------------------------------------------------------------------------

def test_merchant_denial_is_surfaced():
    c = catcher()
    ok, _ = c._validate({"error": "access_denied",
                         "error_description": "The merchant declined"})
    assert not ok and c.error == "The merchant declined"


def test_callback_without_a_code_is_rejected():
    c = catcher()
    params = valid_params()
    del params["code"]
    params = sign({k: v for k, v in params.items() if k != "hmac"})
    ok, _ = c._validate(params)
    assert not ok and "no authorization code" in c.error


# ---------------------------------------------------------------------------
# Scope handling
# ---------------------------------------------------------------------------

class FakeCtx:
    def __init__(self):
        self.warnings = []

    class _Log:
        def __init__(self, out):
            self.out = out

        def warning(self, msg, *args):
            self.out.append(msg % args if args else msg)

    @property
    def log(self):
        return self._Log(self.warnings)


def test_write_scope_implies_read_so_no_false_warning():
    """Shopify omits the read form when the write form is granted."""
    ctx = FakeCtx()
    auth_task._warn_on_missing_scopes(
        ctx, requested="read_products,write_products", granted="write_products"
    )
    assert ctx.warnings == []


def test_genuinely_missing_scope_warns():
    ctx = FakeCtx()
    auth_task._warn_on_missing_scopes(
        ctx, requested="read_products,write_inventory", granted="read_products"
    )
    assert ctx.warnings and "write_inventory" in ctx.warnings[0]


def test_default_scopes_are_the_union_of_registered_tasks():
    scopes = auth_task._default_scopes()
    assert "read_products" in scopes


# ---------------------------------------------------------------------------
# Port preflight
# ---------------------------------------------------------------------------

def test_busy_port_fails_before_opening_a_browser():
    """Otherwise you approve the app and burn the one-time code on a 404."""
    import socket

    from shopifylib.errors import TaskError

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        with pytest.raises(TaskError, match="already in use"):
            auth_task._preflight_port(port)
    finally:
        holder.close()


def test_free_port_passes_preflight():
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    auth_task._preflight_port(port)  # must not raise


# ---------------------------------------------------------------------------
# End-to-end through the HTTP listener
# ---------------------------------------------------------------------------

def test_listener_captures_a_real_callback():
    """Drive the actual HTTP server, not just the validator."""
    import socket
    import threading
    import urllib.request

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    c = catcher(port=port)
    with c:
        query = urllib.parse.urlencode(valid_params())
        url = f"http://127.0.0.1:{port}{auth_task.CALLBACK_PATH}?{query}"

        result = {}

        def fetch():
            with urllib.request.urlopen(url, timeout=5) as resp:
                result["status"] = resp.status
                result["body"] = resp.read().decode()

        thread = threading.Thread(target=fetch)
        thread.start()
        code = c.wait()
        thread.join(timeout=5)

    assert code == "thecode"
    assert result["status"] == 200
    assert "Authorized" in result["body"]


def test_listener_rejects_a_forged_callback():
    import socket
    import threading
    import urllib.error
    import urllib.request

    from shopifylib.errors import TaskError

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    c = catcher(port=port)
    with c:
        bad = valid_params()
        bad["hmac"] = "0" * 64
        url = (f"http://127.0.0.1:{port}{auth_task.CALLBACK_PATH}?"
               + urllib.parse.urlencode(bad))

        status = {}

        def fetch():
            try:
                urllib.request.urlopen(url, timeout=5)
            except urllib.error.HTTPError as exc:
                status["code"] = exc.code

        thread = threading.Thread(target=fetch)
        thread.start()
        with pytest.raises(TaskError, match="Authorization failed"):
            c.wait()
        thread.join(timeout=5)

    assert status.get("code") == 400


# ---------------------------------------------------------------------------
# What to do next
# ---------------------------------------------------------------------------

def _shop(token=None):
    from shopifylib.config import Shop
    return Shop(id="prod", domain="acme.myshopify.com", access_token=token,
                client_id="cid", client_secret="secret", api_version="2026-07",
                plan="basic")


def _record(expires_at=None):
    from shopifylib.auth import TokenRecord
    return TokenRecord(access_token="shpat_new", scope="read_products",
                       expires_at=expires_at)


def test_cached_token_is_neither_printed_nor_sent_to_config():
    """The repo is in Dropbox; config.toml syncs to every linked machine, which
    is exactly why the cache lives outside it. Advice to paste the token there
    undid that, and printing it at all left it in terminal scrollback."""
    text = "\n".join(auth_task._next_steps(_shop(), _record(), cached=True))
    assert "shpat_new" not in text
    assert "config.toml" not in text
    assert "Nothing to copy" in text
    assert "shop-info --shop prod" in text


def test_static_token_in_config_is_flagged_because_it_beats_the_cache():
    """Otherwise re-authorizing for a new scope looks like it worked while every
    task keeps using the old token."""
    text = "\n".join(auth_task._next_steps(_shop("shpat_old"), _record(), cached=True))
    assert "WARNING" in text and "SHOPIFY_TOKEN_PROD" in text
    assert "will NOT be used" in text


def test_print_only_shows_the_token_as_an_env_var_not_a_config_line():
    text = "\n".join(auth_task._next_steps(_shop(), _record(), cached=False))
    assert "export SHOPIFY_TOKEN_PROD='shpat_new'" in text
    assert "access_token =" not in text
    assert "NOT saved" in text


def test_print_only_with_an_expiring_token_says_renewal_is_lost():
    text = "\n".join(auth_task._next_steps(_shop(), _record(expires_at=1.0),
                                           cached=False))
    assert "refresh token is lost" in text
