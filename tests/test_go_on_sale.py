#!/usr/bin/env python3
"""go-on-sale: the markdown convention, its refusals, and --deep-discount."""
from __future__ import annotations

import pytest

from shopifylib.errors import TaskError
from tasks import go_on_sale as gos
from tests.test_pricing import (make_ctx, product_node, products_response, updated,
                                variant, variant_node, variants_response)


def args(price="15.00", *, deep=False, pid="1", vids=None):
    return type("A", (), dict(pid=None if vids else pid, vids=vids, price=price,
                              deep_discount=deep))()


def mutations(client) -> list[dict]:
    return [r["payload"]["variables"] for r in client.session.requests
            if "productVariantsBulkUpdate" in r["payload"]["query"]]


def run(make_client, node, a, stored=(), *, dry_run=False):
    responses = [products_response(node)]
    if stored:
        responses.append(updated(*stored))
    client = make_client(responses, writes_enabled=not dry_run)
    gos.run(make_ctx(client, dry_run=dry_run), a)
    return client


def test_markdown_moves_price_into_compare_at(make_client):
    client = run(make_client, product_node(variant(price="20.00")), args("15.00"),
                 [{"id": variant()["id"], "price": "15.00", "compareAtPrice": "20.00"}])
    [sent] = mutations(client)
    assert sent["productId"] == "gid://shopify/Product/1"
    assert sent["variants"] == [{"id": variant()["id"], "price": "15.00",
                                 "compareAtPrice": "20.00"}]


def test_every_variant_gets_the_one_sale_price(make_client):
    node = product_node(variant(1, price="20.00"), variant(2, price="25.00"))
    client = run(make_client, node, args("15.00"), [
        {"id": variant(1)["id"], "price": "15.00", "compareAtPrice": "20.00"},
        {"id": variant(2)["id"], "price": "15.00", "compareAtPrice": "25.00"}])
    [sent] = mutations(client)
    assert [(v["price"], v["compareAtPrice"]) for v in sent["variants"]] == [
        ("15.00", "20.00"), ("15.00", "25.00")]


def test_already_on_sale_is_refused_and_names_the_override(make_client):
    with pytest.raises(TaskError, match="already on sale.*--deep-discount"):
        run(make_client, product_node(variant(price="15.00", compare_at="20.00")),
            args("12.00"))


def test_price_that_is_not_a_markdown_is_refused(make_client):
    with pytest.raises(TaskError, match="not a markdown"):
        run(make_client, product_node(variant(price="20.00")), args("20.00"))


def test_stale_compare_at_is_overwritten(make_client):
    """A compare-at at or below price is not a sale; going on sale fixes it."""
    client = run(make_client, product_node(variant(price="20.00", compare_at="18.00")),
                 args("15.00"),
                 [{"id": variant()["id"], "price": "15.00", "compareAtPrice": "20.00"}])
    assert mutations(client)[0]["variants"][0]["compareAtPrice"] == "20.00"


def test_one_bad_variant_writes_nothing(make_client):
    node = product_node(variant(1, price="20.00"),
                        variant(2, price="15.00", compare_at="20.00"))
    client = make_client([products_response(node)], writes_enabled=True)
    with pytest.raises(TaskError, match="Bead - V2: already on sale"):
        gos.run(make_ctx(client), args("12.00"))
    assert mutations(client) == []


def test_deep_discount_leaves_compare_at_alone(make_client):
    client = run(make_client, product_node(variant(price="15.00", compare_at="20.00")),
                 args("12.00", deep=True),
                 [{"id": variant()["id"], "price": "12.00", "compareAtPrice": "20.00"}])
    [sent] = mutations(client)
    assert sent["variants"] == [{"id": variant()["id"], "price": "12.00"}]


def test_deep_discount_on_a_regular_product_is_refused(make_client):
    with pytest.raises(TaskError, match="not on sale.*drop --deep-discount"):
        run(make_client, product_node(variant(price="20.00")), args("12.00", deep=True))


def test_deep_discount_on_a_stale_product_is_refused(make_client):
    with pytest.raises(TaskError, match="not on sale"):
        run(make_client, product_node(variant(price="20.00", compare_at="20.00")),
            args("12.00", deep=True))


def test_deep_discount_must_stay_below_the_regular_price(make_client):
    with pytest.raises(TaskError, match="not below the regular price 20.00"):
        run(make_client, product_node(variant(price="15.00", compare_at="20.00")),
            args("20.00", deep=True))


def test_dry_run_sends_no_mutation(make_client, caplog):
    caplog.set_level("INFO")
    client = run(make_client, product_node(variant(price="20.00")), args("15.00"),
                 dry_run=True)
    assert mutations(client) == []
    assert "would put 'Bead' on sale: 20.00 -> 15.00" in caplog.text


def test_bad_price_is_refused_before_any_request(make_client):
    client = make_client([])
    with pytest.raises(TaskError):
        gos.run(make_ctx(client), args("12.345"))
    assert client.session.requests == []


# ---------------------------------------------------------------------------
# --vids
# ---------------------------------------------------------------------------

def test_vids_marks_down_only_the_named_variant(make_client):
    client = make_client([
        variants_response(variant_node(2, price="62.00", compare_at=None)),
        updated({"id": variant(2)["id"], "price": "50.00", "compareAtPrice": "62.00"}),
    ], writes_enabled=True)
    gos.run(make_ctx(client), args("50.00", vids="2"))
    [sent] = mutations(client)
    assert sent["productId"] == "gid://shopify/Product/1"
    assert sent["variants"] == [{"id": variant(2)["id"], "price": "50.00",
                                 "compareAtPrice": "62.00"}]


def test_vids_across_products_is_one_write_per_product(make_client):
    client = make_client([
        variants_response(variant_node(1, pid=1, price="20.00", compare_at=None),
                          variant_node(2, pid=2, title="Other", price="30.00",
                                       compare_at=None)),
        updated({"id": variant(1)["id"], "price": "10.00", "compareAtPrice": "20.00"}),
        updated({"id": variant(2)["id"], "price": "10.00", "compareAtPrice": "30.00"}),
    ], writes_enabled=True)
    gos.run(make_ctx(client), args("10.00", vids="1,2"))
    assert [m["productId"][-1] for m in mutations(client)] == ["1", "2"]


def test_vids_one_bad_variant_in_another_product_writes_nothing(make_client):
    client = make_client([variants_response(
        variant_node(1, pid=1, price="20.00", compare_at=None),
        variant_node(2, pid=2, title="Other"))], writes_enabled=True)
    with pytest.raises(TaskError, match="Other - V2: already on sale"):
        gos.run(make_ctx(client), args("10.00", vids="1,2"))
    assert mutations(client) == []
