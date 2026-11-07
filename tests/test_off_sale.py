#!/usr/bin/env python3
"""off-sale: restoring the regular price, and refusing what isn't on sale."""
from __future__ import annotations

import pytest

from shopifylib.errors import TaskError
from tasks import off_sale as ofs
from tests.test_pricing import (make_ctx, product_node, products_response, updated,
                                variant, variant_node, variants_response)


def args(pids="1", *, vids=None):
    return type("A", (), dict(pids=None if vids else pids, vids=vids))()


def mutations(client) -> list[dict]:
    return [r["payload"]["variables"] for r in client.session.requests
            if "productVariantsBulkUpdate" in r["payload"]["query"]]


ON_SALE = product_node(variant(price="15.00", compare_at="20.00"))
RESTORED = {"id": variant()["id"], "price": "20.00", "compareAtPrice": None}


def test_price_is_restored_and_compare_at_cleared(make_client):
    client = make_client([products_response(ON_SALE), updated(RESTORED)],
                         writes_enabled=True)
    ofs.run(make_ctx(client), args())
    [sent] = mutations(client)
    # None, not omitted: an omitted compareAtPrice would be left in place.
    assert sent["variants"] == [{"id": variant()["id"], "price": "20.00",
                                 "compareAtPrice": None}]


@pytest.mark.parametrize("compare_at,state", [(None, "regular"),
                                               ("18.00", "stale compare-at")])
def test_not_on_sale_is_refused(make_client, compare_at, state):
    node = product_node(variant(price="20.00", compare_at=compare_at))
    with pytest.raises(TaskError, match=state):
        ofs.run(make_ctx(make_client([products_response(node)])), args())


def test_one_invalid_product_blocks_the_whole_run(make_client):
    """All-or-nothing: a cron run either fully applies or fully refuses."""
    regular = product_node(variant(2, price="20.00", name="Plain"), title="Plain", pid=2)
    client = make_client([products_response(ON_SALE, regular)], writes_enabled=True)
    with pytest.raises(TaskError, match="Plain - V2: regular"):
        ofs.run(make_ctx(client), args("1,2"))
    assert mutations(client) == []


def test_one_mutation_per_product(make_client):
    other = product_node(variant(2, price="8.00", compare_at="10.00"), title="Other", pid=2)
    client = make_client([
        products_response(ON_SALE, other),
        updated(RESTORED),
        updated({"id": variant(2)["id"], "price": "10.00", "compareAtPrice": None}),
    ], writes_enabled=True)
    ofs.run(make_ctx(client), args("1,2"))
    assert [m["productId"] for m in mutations(client)] == [
        "gid://shopify/Product/1", "gid://shopify/Product/2"]


def test_dry_run_sends_no_mutation(make_client):
    client = make_client([products_response(ON_SALE)])
    ofs.run(make_ctx(client, dry_run=True), args())
    assert mutations(client) == []


def test_vids_ends_the_sale_on_only_the_named_variant(make_client):
    """The case that prompted --vids: two sizes on sale, end one."""
    client = make_client([
        variants_response(variant_node(2, price="50.00", compare_at="62.00")),
        updated({"id": variant(2)["id"], "price": "62.00", "compareAtPrice": None}),
    ], writes_enabled=True)
    ofs.run(make_ctx(client), args(vids="2"))
    [sent] = mutations(client)
    assert sent["variants"] == [{"id": variant(2)["id"], "price": "62.00",
                                 "compareAtPrice": None}]


def test_vids_not_on_sale_is_refused(make_client):
    client = make_client([variants_response(
        variant_node(2, price="62.00", compare_at=None))], writes_enabled=True)
    with pytest.raises(TaskError, match="regular"):
        ofs.run(make_ctx(client), args(vids="2"))
    assert mutations(client) == []
