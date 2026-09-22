#!/usr/bin/env python3
"""Token acquisition, caching, refresh, and OAuth callback validation.

The callback-validation tests are security tests: state, HMAC and shop-domain
checks are what stop anything else on the machine from driving a code exchange
through the localhost listener. Treat them like the write-guard poison tests --
if one starts failing, the check it covers has been weakened.
"""
from __future__ import annotations

import json
import stat
import time

import pytest
import requests

from shopifylib import auth
from shopifylib.config import Shop
from shopifylib.errors import AuthError, ConfigError
from tests.conftest import FakeResponse


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv(auth.CACHE_ENV, str(tmp_path / "tokens.json"))
    return auth.TokenStore()


def make_shop(**kw) -> Shop:
    base = dict(
        id="prod", domain="acme.myshopify.com", api_version="2026-07", plan="standard",
        access_token=None, client_id=None, client_secret=None,
    )
    base.update(kw)
    return Shop(**base)


class FakeTokenSession:
    """Records form posts to the OAuth token endpoint."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []

    def post(self, url, data=None, timeout=None, **kw):
        self.posts.append({"url": url, "data": data})
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt if isinstance(nxt, FakeResponse) else FakeResponse(nxt)


# ---------------------------------------------------------------------------
# TokenRecord
# ---------------------------------------------------------------------------

def test_token_without_expiry_is_always_fresh():
    """A legacy or non-expiring offline token never needs refreshing."""
    assert auth.TokenRecord("shpat_x").is_fresh()


def test_expired_token_is_not_fresh():
    assert not auth.TokenRecord("t", expires_at=time.time() - 1).is_fresh()


def test_token_within_the_skew_window_is_stale():
    """Refresh slightly early so a long task can't have one lapse mid-flight."""
    soon = time.time() + auth.EXPIRY_SKEW_SECONDS / 2
    assert not auth.TokenRecord("t", expires_at=soon).is_fresh()


def test_token_well_before_expiry_is_fresh():
    assert auth.TokenRecord("t", expires_at=time.time() + 86000).is_fresh()


def test_expired_refresh_token_cannot_refresh():
    record = auth.TokenRecord("t", refresh_token="r",
                              refresh_token_expires_at=time.time() - 1)
    assert not record.can_refresh()


def test_record_repr_hides_the_token():
    assert "shpat_secret" not in repr(auth.TokenRecord("shpat_secret"))


# ---------------------------------------------------------------------------
# TokenStore
# ---------------------------------------------------------------------------

def test_store_round_trip(store):
    shop = make_shop()
    store.put(shop, auth.TokenRecord("tok", expires_at=123.0, scope="read_products"))
    got = store.get(shop)
    assert (got.access_token, got.expires_at, got.scope) == ("tok", 123.0, "read_products")


def test_store_is_written_private(store):
    """The cache holds live credentials; it must not be group/world readable."""
    store.put(make_shop(), auth.TokenRecord("tok"))
    mode = stat.S_IMODE(store.path.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {mode:o}"


def test_store_is_not_inside_the_repo(monkeypatch):
    """This repo lives in a Dropbox tree; a token beside the code would sync."""
    monkeypatch.delenv(auth.CACHE_ENV, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/xdg")
    assert "shopify-tasks/tokens.json" in str(auth.cache_path())
    assert str(auth.cache_path()).startswith("/tmp/xdg")


def test_store_keys_by_shop(store):
    a, b = make_shop(id="prod"), make_shop(id="dev", domain="dev.myshopify.com")
    store.put(a, auth.TokenRecord("token-a"))
    store.put(b, auth.TokenRecord("token-b"))
    assert store.get(a).access_token == "token-a"
    assert store.get(b).access_token == "token-b"


def test_forget_removes_only_that_shop(store):
    a, b = make_shop(id="prod"), make_shop(id="dev", domain="dev.myshopify.com")
    store.put(a, auth.TokenRecord("a"))
    store.put(b, auth.TokenRecord("b"))
    store.forget(a)
    assert store.get(a) is None
    assert store.get(b) is not None


def test_corrupt_cache_is_ignored_not_fatal(store):
    """One extra token fetch beats a broken run."""
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ not json")
    assert store.get(make_shop()) is None


def test_missing_cache_is_not_an_error(store):
    assert store.get(make_shop()) is None


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------

def test_client_credentials_posts_the_right_form():
    shop = make_shop(client_id="cid", client_secret="csec")
    session = FakeTokenSession([{"access_token": "tok", "expires_in": 86399,
                                 "scope": "read_products"}])
    record = auth.client_credentials_grant(shop, session)

    assert session.posts[0]["url"] == "https://acme.myshopify.com/admin/oauth/access_token"
    assert session.posts[0]["data"] == {
        "grant_type": "client_credentials", "client_id": "cid", "client_secret": "csec",
    }
    assert record.access_token == "tok"
    assert record.expires_at is not None


def test_authorization_code_grant_without_expiring_yields_a_permanent_token():
    """The whole point: this replaces the retired static shpat_ token."""
    shop = make_shop(client_id="cid", client_secret="csec")
    session = FakeTokenSession([{"access_token": "shpat_new", "scope": "read_products"}])
    record = auth.authorization_code_grant(shop, "thecode", session=session)

    assert "expiring" not in session.posts[0]["data"]
    assert session.posts[0]["data"]["code"] == "thecode"
    assert record.expires_at is None
    assert record.is_fresh()


def test_authorization_code_grant_with_expiring_yields_a_refresh_pair():
    shop = make_shop(client_id="cid", client_secret="csec")
    session = FakeTokenSession([{
        "access_token": "tok", "expires_in": 3600,
        "refresh_token": "shprt_x", "refresh_token_expires_in": 7776000,
    }])
    record = auth.authorization_code_grant(shop, "c", expiring=True, session=session)

    assert session.posts[0]["data"]["expiring"] == "1"
    assert record.refresh_token == "shprt_x"
    assert record.can_refresh()


def test_refresh_grant_posts_the_refresh_token():
    shop = make_shop(client_id="cid", client_secret="csec")
    session = FakeTokenSession([{"access_token": "tok2", "expires_in": 3600}])
    auth.refresh_grant(shop, "shprt_old", session)
    assert session.posts[0]["data"]["grant_type"] == "refresh_token"
    assert session.posts[0]["data"]["refresh_token"] == "shprt_old"


def test_client_credentials_failure_explains_the_org_constraint():
    """The real error text never mentions it, and it's the usual cause."""
    shop = make_shop(client_id="cid", client_secret="csec")
    session = FakeTokenSession([FakeResponse("unauthorized", status=401)])
    with pytest.raises(AuthError, match="SAME Shopify organization"):
        auth.client_credentials_grant(shop, session)


APP_NOT_INSTALLED_PAGE = (
    "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"utf-8\">\n"
    "  <title>400 - Oauth error app_not_installed</title>\n"
    "  <style type=\"text/css\">* { border:0; margin:0; padding:0; }</style>\n"
    "</head><body>...</body></html>"
)


def test_app_not_installed_is_named_and_given_the_right_fix():
    """Same org is necessary but not sufficient -- the app must be installed
    too, and that is a different fix from an org mismatch."""
    shop = make_shop(client_id="cid", client_secret="csec")
    session = FakeTokenSession([FakeResponse(APP_NOT_INSTALLED_PAGE, status=400)])
    with pytest.raises(AuthError) as exc:
        auth.client_credentials_grant(shop, session)
    text = str(exc.value)
    assert "never been installed on this store" in text
    assert "shopify.sh auth --shop prod" in text
    assert "same organization is not enough" in text.lower()


def test_html_error_pages_are_reduced_to_their_title():
    """Shopify puts the actual error code in <title> and surrounds it with a
    screenful of CSS; dumping the raw page buries the one useful word."""
    shop = make_shop(client_id="cid", client_secret="csec")
    session = FakeTokenSession([FakeResponse(APP_NOT_INSTALLED_PAGE, status=400)])
    with pytest.raises(AuthError) as exc:
        auth.client_credentials_grant(shop, session)
    assert "<style" not in str(exc.value)
    assert "400 - Oauth error app_not_installed" in str(exc.value)


def test_plain_text_errors_pass_through_unchanged():
    shop = make_shop(client_id="cid", client_secret="csec")
    session = FakeTokenSession([FakeResponse("invalid_client", status=401)])
    with pytest.raises(AuthError, match="invalid_client"):
        auth.client_credentials_grant(shop, session)


def test_refresh_failure_points_at_re_authing():
    shop = make_shop(client_id="cid", client_secret="csec")
    session = FakeTokenSession([FakeResponse("bad", status=400)])
    with pytest.raises(AuthError, match="shopify.sh auth"):
        auth.refresh_grant(shop, "old", session)


def test_network_failure_is_an_auth_error():
    shop = make_shop(client_id="cid", client_secret="csec")
    session = FakeTokenSession([requests.ConnectionError("no route")])
    with pytest.raises(AuthError, match="Could not reach"):
        auth.client_credentials_grant(shop, session)


def test_response_without_a_token_is_rejected():
    shop = make_shop(client_id="cid", client_secret="csec")
    session = FakeTokenSession([{"scope": "read_products"}])
    with pytest.raises(AuthError, match="no access_token"):
        auth.client_credentials_grant(shop, session)


# ---------------------------------------------------------------------------
# TokenProvider resolution order
# ---------------------------------------------------------------------------

def test_static_token_needs_no_network(store):
    provider = auth.TokenProvider(make_shop(access_token="shpat_static"), store=store)
    assert provider.token() == "shpat_static"


def test_static_token_beats_client_credentials(store):
    """No round trip when a usable token is already configured."""
    shop = make_shop(access_token="shpat_static", client_id="cid", client_secret="csec")
    session = FakeTokenSession([])  # would raise IndexError if used
    assert auth.TokenProvider(shop, store=store, session=session).token() == "shpat_static"


def test_fresh_cached_token_is_reused(store):
    shop = make_shop(client_id="cid", client_secret="csec")
    store.put(shop, auth.TokenRecord("cached", expires_at=time.time() + 80000))
    session = FakeTokenSession([])
    assert auth.TokenProvider(shop, store=store, session=session).token() == "cached"


def test_expired_cached_token_is_refreshed_when_possible(store):
    shop = make_shop(client_id="cid", client_secret="csec")
    store.put(shop, auth.TokenRecord(
        "old", expires_at=time.time() - 1,
        refresh_token="shprt_x", refresh_token_expires_at=time.time() + 7776000,
    ))
    session = FakeTokenSession([{"access_token": "renewed", "expires_in": 3600}])
    assert auth.TokenProvider(shop, store=store, session=session).token() == "renewed"
    assert session.posts[0]["data"]["grant_type"] == "refresh_token"


def test_expired_token_without_refresh_falls_back_to_client_credentials(store):
    shop = make_shop(client_id="cid", client_secret="csec")
    store.put(shop, auth.TokenRecord("old", expires_at=time.time() - 1))
    session = FakeTokenSession([{"access_token": "fresh", "expires_in": 86399}])
    assert auth.TokenProvider(shop, store=store, session=session).token() == "fresh"
    assert session.posts[0]["data"]["grant_type"] == "client_credentials"


def test_fetched_token_is_cached(store):
    shop = make_shop(client_id="cid", client_secret="csec")
    session = FakeTokenSession([{"access_token": "fresh", "expires_in": 86399}])
    auth.TokenProvider(shop, store=store, session=session).token()
    assert store.get(shop).access_token == "fresh"


def test_token_is_resolved_once_per_provider(store):
    """A 400-call run must not fetch a token 400 times."""
    shop = make_shop(client_id="cid", client_secret="csec")
    session = FakeTokenSession([{"access_token": "fresh", "expires_in": 86399}])
    provider = auth.TokenProvider(shop, store=store, session=session)
    for _ in range(5):
        provider.token()
    assert len(session.posts) == 1


def test_no_credentials_at_all_is_a_config_error(store):
    provider = auth.TokenProvider(make_shop(), store=store)
    with pytest.raises(ConfigError, match="shopify.sh auth"):
        provider.token()


def test_invalidate_allows_a_retry_for_fetchable_tokens(store):
    shop = make_shop(client_id="cid", client_secret="csec")
    store.put(shop, auth.TokenRecord("cached", expires_at=time.time() + 80000))
    provider = auth.TokenProvider(shop, store=store)
    assert provider.invalidate() is True
    assert store.get(shop) is None, "the rejected token must leave the cache"


def test_invalidate_reports_no_retry_for_a_static_token(store):
    """A static token can't be re-fetched, so a 401 against one is final."""
    provider = auth.TokenProvider(make_shop(access_token="shpat_x"), store=store)
    assert provider.invalidate() is False


# ---------------------------------------------------------------------------
# Client integration
# ---------------------------------------------------------------------------

def test_401_triggers_one_token_refetch_then_succeeds(make_client, store, shop):
    """A client-credentials token that lapses mid-run costs a retry, not a run."""
    from tests.conftest import gql

    credentialed = Shop(**{**shop.__dict__, "access_token": None,
                           "client_id": "cid", "client_secret": "csec"})
    session = FakeTokenSession([
        {"access_token": "first", "expires_in": 86399},
        {"access_token": "second", "expires_in": 86399},
    ])
    provider = auth.TokenProvider(credentialed, store=store, session=session)
    client = make_client([FakeResponse({}, status=401), gql({"shop": {"name": "Acme"}})],
                         shop_override=credentialed)
    client.tokens = provider

    assert client.execute("query { shop { name } }")["shop"]["name"] == "Acme"
    used = [r["headers"]["X-Shopify-Access-Token"] for r in client.session.requests]
    assert used == ["first", "second"]


def test_401_on_a_static_token_fails_immediately(make_client):
    """No point retrying a token that cannot be re-fetched."""
    client = make_client([FakeResponse({}, status=401)])
    with pytest.raises(AuthError):
        client.execute("query { shop { name } }")
    assert len(client.session.requests) == 1
