#!/usr/bin/env python3
"""shopifylib.pricing: the on-sale vocabulary, money handling, read and write helpers."""
from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from shopifylib import pricing
from shopifylib.errors import TaskError
from shopifylib.task import TaskContext
from tests.conftest import gql

D = Decimal


def make_ctx(client, *, dry_run=False, json_output=False) -> TaskContext:
    return TaskContext(client=client, shop=client.shop, dry_run=dry_run,
                       log=logging.getLogger("test"), json_output=json_output)


def variant(n=1, *, price="20.00", compare_at=None, name="Bead"):
    return {"id": f"gid://shopify/ProductVariant/{n}",
            "displayName": f"{name} - Default Title" if n == 1 else f"{name} - V{n}",
            "price": price, "compareAtPrice": compare_at}


def product_node(*variants, title="Bead", pid=1, status="ACTIVE", more=False):
    return {"id": f"gid://shopify/Product/{pid}", "title": title, "status": status,
            "variants": {"nodes": list(variants) or [variant()],
                         "pageInfo": {"hasNextPage": more}}}


def products_response(*nodes):
    return gql({"nodes": list(nodes)})


def updated(*variants):
    """A productVariantsBulkUpdate response echoing the stored variants."""
    return gql({"productVariantsBulkUpdate": {
        "productVariants": list(variants), "userErrors": []}})


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("price,compare_at,expected", [
    (D("15"), D("20"), pricing.ON_SALE),
    (D("20"), None, pricing.REGULAR),
    (D("20"), D("0"), pricing.REGULAR),        # zero means unset
    (D("20"), D("20"), pricing.STALE),         # equal: Shopify shows no sale
    (D("20"), D("15"), pricing.STALE),
])
def test_state(price, compare_at, expected):
    assert pricing.state(price, compare_at) == expected


def test_variant_state_reads_api_strings():
    assert pricing.variant_state(variant(price="9.99", compare_at="12.00")) == pricing.ON_SALE


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

def test_money_round_trips_without_float():
    assert pricing.parse_money("0.10") + pricing.parse_money("0.20") == D("0.30")
    assert pricing.format_money(D("12.5")) == "12.50"
    assert pricing.parse_money(None) is None
    assert pricing.format_money(None) == ""


@pytest.mark.parametrize("text", ["0", "-1", "12.345", "abc", "", "nan", "inf"])
def test_bad_price_args_are_refused(text):
    with pytest.raises(TaskError):
        pricing.parse_price_arg(text)


def test_good_price_arg_is_normalised():
    assert pricing.parse_price_arg(" 15 ") == D("15.00")
    assert str(pricing.parse_price_arg("9.9")) == "9.90"


def test_parse_ids_dedupe_and_coerce():
    assert pricing.parse_ids("1, gid://shopify/Product/1,2", "Product", "--pids") == [
        "gid://shopify/Product/1", "gid://shopify/Product/2"]
    with pytest.raises(TaskError, match="--pids was empty"):
        pricing.parse_ids(" , ", "Product", "--pids")


def test_parse_ids_refuses_a_product_gid_as_a_variant():
    with pytest.raises(Exception, match="Expected a ProductVariant id"):
        pricing.parse_ids("gid://shopify/Product/1", "ProductVariant", "--vids")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def test_load_products_refuses_an_unknown_id(make_client):
    ctx = make_ctx(make_client([products_response(None)]))
    with pytest.raises(TaskError, match="No such product: 7"):
        pricing.load_products(ctx, ["gid://shopify/Product/7"])


def test_load_products_refuses_a_partial_read(make_client):
    """Repricing the first 100 variants and leaving the rest at full price is
    worse than not running."""
    ctx = make_ctx(make_client([products_response(product_node(more=True))]))
    with pytest.raises(TaskError, match="more than 100 variants"):
        pricing.load_products(ctx, ["gid://shopify/Product/1"])


def variant_node(n, *, pid=1, title="Bead", price="15.00", compare_at="20.00",
                 status="ACTIVE"):
    """A ProductVariant from VARIANTS_BY_ID_QUERY: a variant carrying its product."""
    return {**variant(n, price=price, compare_at=compare_at, name=title),
            "product": {"id": f"gid://shopify/Product/{pid}", "title": title,
                        "status": status}}


def variants_response(*nodes):
    return gql({"nodes": list(nodes)})


def test_load_variants_groups_by_product_keeping_only_those_named(make_client):
    """One product per write, and only the named variants in it -- naming one
    size must not drag its siblings along."""
    ctx = make_ctx(make_client([variants_response(
        variant_node(1, pid=1), variant_node(3, pid=2, title="Other"),
        variant_node(2, pid=1))]))
    products = pricing.load_variants(ctx, [f"gid://shopify/ProductVariant/{n}"
                                           for n in (1, 3, 2)])
    assert [p["id"] for p in products] == ["gid://shopify/Product/1",
                                           "gid://shopify/Product/2"]
    assert [v["id"][-1] for v in products[0]["variants"]["nodes"]] == ["1", "2"]
    assert "product" in products[0]["variants"]["nodes"][0]  # harmless extra


def test_load_variants_refuses_an_unknown_id(make_client):
    """Also what a product number passed to --vids looks like: it builds a
    variant GID that resolves to nothing, not to the wrong record."""
    ctx = make_ctx(make_client([variants_response(None)]))
    with pytest.raises(TaskError, match="No such variant: 9"):
        pricing.load_variants(ctx, ["gid://shopify/ProductVariant/9"])


def test_load_variants_query_has_no_connection():
    assert "first:" not in pricing.VARIANTS_BY_ID_QUERY


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def test_apply_checks_what_was_stored(make_client):
    """A clean response that stored a different price is an error, not a success."""
    client = make_client([updated({"id": variant()["id"], "price": "19.00",
                                   "compareAtPrice": "20.00"})], writes_enabled=True)
    with pytest.raises(TaskError, match="price for variant 1 is 19.00"):
        pricing.apply(make_ctx(client), product_node(),
                      [{"id": variant()["id"], "price": "15.00",
                        "compareAtPrice": "20.00"}], describe="x")


def test_apply_accepts_a_cleared_compare_at(make_client):
    client = make_client([updated({"id": variant()["id"], "price": "20.00",
                                   "compareAtPrice": None})], writes_enabled=True)
    pricing.apply(make_ctx(client), product_node(),
                  [{"id": variant()["id"], "price": "20.00", "compareAtPrice": None}],
                  describe="x")


def test_apply_ignores_fields_it_did_not_send(make_client):
    """Deep discount omits compareAtPrice, so its value must not be compared."""
    client = make_client([updated({"id": variant()["id"], "price": "12.00",
                                   "compareAtPrice": "20.00"})], writes_enabled=True)
    pricing.apply(make_ctx(client), product_node(),
                  [{"id": variant()["id"], "price": "12.00"}], describe="x")


def test_apply_sends_nothing_on_a_dry_run(make_client):
    client = make_client([])
    pricing.apply(make_ctx(client, dry_run=True), product_node(),
                  [{"id": variant()["id"], "price": "12.00"}], describe="x")
    assert client.session.requests == []
