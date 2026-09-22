#!/usr/bin/env python3
"""Client behavior: the three error layers, retries, and the write guard.

The write-guard tests are the ones to keep working. Everything about dry-run
safety rests on a mutation being unable to leave the process when writes are
disabled, so those are poison tests: if someone loosens the guard, they fail.
"""
from __future__ import annotations

import pytest
import requests

from shopifylib.client import ShopifyClient, check_user_errors, is_mutation
from shopifylib.errors import (
    AccessDeniedError,
    AuthError,
    GraphQLError,
    NotFoundError,
    RetriesExhausted,
    TransportError,
    UserError,
    WriteBlocked,
)
from tests.conftest import FakeResponse, gql, throttled

READ = "query Shop { shop { name } }"
WRITE = "mutation Update($id: ID!) { productUpdate(input: {id: $id}) { userErrors { field message } } }"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_execute_returns_data(make_client):
    client = make_client([gql({"shop": {"name": "Acme"}})])
    assert client.execute(READ) == {"shop": {"name": "Acme"}}


def test_request_carries_auth_header_and_versioned_url(make_client):
    client = make_client([gql({"shop": {}})])
    client.execute(READ)
    request = client.session.requests[0]
    assert request["headers"]["X-Shopify-Access-Token"] == "shpat_fake"
    assert request["url"].endswith("/admin/api/2026-07/graphql.json")


def test_token_is_resolved_per_request(make_client):
    """The header is set per call, not pinned at construction, so a token that
    is refreshed mid-run reaches the very next request."""
    client = make_client([gql({"shop": {}}), gql({"shop": {}})])
    client.execute(READ)
    client.tokens._record = None
    client.tokens.shop = client.shop.__class__(
        **{**client.shop.__dict__, "access_token": "shpat_rotated"}
    )
    client.execute(READ)
    tokens = [r["headers"]["X-Shopify-Access-Token"] for r in client.session.requests]
    assert tokens == ["shpat_fake", "shpat_rotated"]


def test_variables_are_forwarded(make_client):
    client = make_client([gql({"shop": {}})])
    client.execute(READ, {"id": "gid://shopify/Product/1"})
    assert client.session.requests[0]["payload"]["variables"] == {"id": "gid://shopify/Product/1"}


# ---------------------------------------------------------------------------
# Layer 1: HTTP
# ---------------------------------------------------------------------------

def test_401_raises_auth_error(make_client):
    client = make_client([FakeResponse({}, status=401)])
    with pytest.raises(AuthError, match="rejected the access token"):
        client.execute(READ)


def test_403_names_the_declared_scopes(make_client):
    """A permission failure should say what to grant, not just that it failed."""
    client = make_client([FakeResponse({}, status=403)], scopes=["write_inventory"])
    with pytest.raises(AuthError, match="write_inventory"):
        client.execute(READ)


def test_404_blames_domain_or_version(make_client):
    client = make_client([FakeResponse("", status=404)])
    with pytest.raises(NotFoundError, match="2026-07"):
        client.execute(READ)


def test_500_is_retried_then_succeeds(make_client, sleeps):
    client = make_client([
        FakeResponse({}, status=500),
        FakeResponse({}, status=502),
        gql({"shop": {"name": "Acme"}}),
    ])
    assert client.execute(READ)["shop"]["name"] == "Acme"
    assert len(client.session.requests) == 3
    assert len(sleeps) == 2  # backed off between attempts


def test_connection_error_is_retried(make_client):
    client = make_client([
        requests.ConnectionError("reset by peer"),
        gql({"shop": {"name": "Acme"}}),
    ])
    assert client.execute(READ)["shop"]["name"] == "Acme"


def test_retry_after_header_is_honored(make_client, sleeps):
    client = make_client([
        FakeResponse({}, status=429, headers={"Retry-After": "7"}),
        gql({"shop": {}}),
    ])
    client.execute(READ)
    assert sleeps[0] >= 7.0


def test_persistent_5xx_exhausts_retries(make_client):
    client = make_client([FakeResponse({}, status=503)] * ShopifyClient.MAX_ATTEMPTS)
    with pytest.raises(RetriesExhausted) as exc:
        client.execute(READ)
    assert exc.value.attempts == ShopifyClient.MAX_ATTEMPTS


def test_non_json_200_is_retried(make_client):
    """An HTML error page from an edge proxy is transient, not fatal."""
    client = make_client([
        FakeResponse("<html>502 Bad Gateway</html>"),
        gql({"shop": {"name": "Acme"}}),
    ])
    assert client.execute(READ)["shop"]["name"] == "Acme"


def test_400_is_not_retried(make_client):
    """A client error will fail identically on every attempt; fail fast."""
    client = make_client([FakeResponse("bad request", status=400)])
    with pytest.raises(TransportError):
        client.execute(READ)
    assert len(client.session.requests) == 1


# ---------------------------------------------------------------------------
# Layer 2: GraphQL errors[]
# ---------------------------------------------------------------------------

def test_graphql_error_includes_the_path(make_client):
    client = make_client([{"errors": [
        {"message": "Field 'nope' doesn't exist", "path": ["shop", "nope"]},
    ]}])
    with pytest.raises(GraphQLError, match=r"shop\.nope"):
        client.execute(READ)


def test_access_denied_suggests_the_scope(make_client):
    client = make_client([{"errors": [
        {"message": "Access denied for inventoryLevels",
         "extensions": {"code": "ACCESS_DENIED"}},
    ]}], scopes=["read_inventory"])
    with pytest.raises(AccessDeniedError, match="read_inventory"):
        client.execute(READ)


def test_throttled_sleeps_then_retries(make_client, sleeps):
    """The common case: pause for the reported refill, then carry on."""
    client = make_client([throttled(), gql({"shop": {"name": "Acme"}})])
    assert client.execute(READ)["shop"]["name"] == "Acme"
    assert sleeps and sleeps[0] > 0


def test_persistent_throttling_eventually_gives_up(make_client):
    client = make_client([throttled()] * ShopifyClient.MAX_THROTTLE_RETRIES)
    with pytest.raises(RetriesExhausted, match="rate-limited"):
        client.execute(READ)


def test_neither_data_nor_errors_is_an_error(make_client):
    """Returning None into task code would fail two lines later, opaquely."""
    client = make_client([{"extensions": {}}])
    with pytest.raises(GraphQLError, match="neither"):
        client.execute(READ)


# ---------------------------------------------------------------------------
# Layer 3: userErrors
# ---------------------------------------------------------------------------

def test_user_errors_raise_rather_than_return(make_client):
    """HTTP 200, no errors[], nothing changed -- the silent-failure case."""
    client = make_client([gql({"productUpdate": {
        "userErrors": [{"field": ["input", "id"], "message": "Product does not exist",
                        "code": "NOT_FOUND"}],
    }})], writes_enabled=True)
    with pytest.raises(UserError, match="Product does not exist"):
        client.mutate(WRITE, {"id": "gid://shopify/Product/1"}, field="productUpdate")


def test_user_error_message_names_field_and_code(make_client):
    client = make_client([gql({"productUpdate": {
        "userErrors": [{"field": ["input", "title"], "message": "can't be blank",
                        "code": "BLANK"}],
    }})], writes_enabled=True)
    with pytest.raises(UserError) as exc:
        client.mutate(WRITE, field="productUpdate")
    text = str(exc.value)
    assert "input.title" in text and "BLANK" in text and "productUpdate" in text


def test_clean_mutation_returns_its_payload(make_client):
    client = make_client([gql({"productUpdate": {
        "product": {"id": "gid://shopify/Product/1"}, "userErrors": [],
    }})], writes_enabled=True)
    payload = client.mutate(WRITE, field="productUpdate")
    assert payload["product"]["id"] == "gid://shopify/Product/1"


def test_typed_user_error_fields_are_also_checked():
    """Some mutations name the field mediaUserErrors rather than userErrors."""
    with pytest.raises(UserError, match="bad file"):
        check_user_errors(
            {"mediaUserErrors": [{"field": ["files"], "message": "bad file"}]},
            mutation="productCreateMedia",
        )


def test_mutation_without_field_scans_every_payload(make_client):
    client = make_client([gql({"productUpdate": {
        "userErrors": [{"field": None, "message": "nope"}],
    }})], writes_enabled=True)
    with pytest.raises(UserError, match="nope"):
        client.mutate(WRITE)


def test_missing_named_field_is_diagnosed(make_client):
    client = make_client([gql({"somethingElse": {}})], writes_enabled=True)
    with pytest.raises(GraphQLError, match="productUpdate"):
        client.mutate(WRITE, field="productUpdate")


# ---------------------------------------------------------------------------
# The write guard -- poison tests
# ---------------------------------------------------------------------------

def test_mutation_is_blocked_when_writes_disabled(make_client):
    """The property every dry run depends on. If this fails, dry-run is a lie."""
    client = make_client([gql({})], writes_enabled=False)
    with pytest.raises(WriteBlocked, match="dry run"):
        client.mutate(WRITE, field="productUpdate")
    assert client.session.requests == [], "nothing may reach the network"


def test_mutation_via_execute_is_blocked_when_writes_disabled(make_client):
    """Bypassing mutate() must not bypass the guard."""
    client = make_client([gql({})], writes_enabled=False)
    with pytest.raises(WriteBlocked):
        client.execute(WRITE, allow_write=True)
    assert client.session.requests == []


def test_execute_refuses_mutations_even_when_writes_enabled(make_client):
    """Writes must go through mutate() so userErrors get checked."""
    client = make_client([gql({})], writes_enabled=True)
    with pytest.raises(WriteBlocked, match="userErrors"):
        client.execute(WRITE)
    assert client.session.requests == []


def test_reads_are_unaffected_by_the_guard(make_client):
    client = make_client([gql({"shop": {}})], writes_enabled=False)
    client.execute(READ)  # must not raise


@pytest.mark.parametrize("document,expected", [
    ("mutation Foo { x }", True),
    ("  \n  mutation Foo { x }", True),
    ("# a comment\nmutation Foo { x }", True),
    ("query Foo { x }", False),
    ("{ shop { name } }", False),          # shorthand query form
    ("  query Foo { mutation }", False),   # 'mutation' as a field name, not an op
])
def test_is_mutation_detection(document, expected):
    assert is_mutation(document) is expected
