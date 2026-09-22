#!/usr/bin/env python3
"""seed-dev: the development-store guard, generation, and cloning.

The guard tests are the load-bearing ones. This task fabricates and deletes
products wholesale, and dry-run does not help when the task is correct and the
*store* is wrong -- the preview looks perfect either way.
"""
from __future__ import annotations

import logging

import pytest

from shopifylib.errors import TaskError
from shopifylib.task import TaskContext
from tasks import seed_dev as sd
from tests.conftest import gql

LOCATIONS = gql({"locations": {"nodes": [
    {"id": "gid://shopify/Location/1", "name": "Home"},
    {"id": "gid://shopify/Location/2", "name": "Craft Fair Inventory"},
]}})

LOC_IDS = {"Home": "gid://shopify/Location/1",
           "Craft Fair Inventory": "gid://shopify/Location/2"}


def dev_shop(partner=True):
    return gql({"shop": {"name": "testdev", "plan": {
        "displayName": "Basic App Development", "partnerDevelopment": partner}}})


def make_ctx(client, *, dry_run=False):
    return TaskContext(client=client, shop=client.shop, dry_run=dry_run,
                       log=logging.getLogger("test"))


# ---------------------------------------------------------------------------
# The development-store guard
# ---------------------------------------------------------------------------

def test_refuses_a_production_store(make_client):
    """The single most important behaviour in this file."""
    ctx = make_ctx(make_client([dev_shop(partner=False)]))
    with pytest.raises(TaskError, match="not a development store"):
        ctx.require_development_store()


def test_allows_a_development_store(make_client):
    ctx = make_ctx(make_client([dev_shop(partner=True)]))
    ctx.require_development_store()      # must not raise


def test_refusal_names_the_store_and_the_fix(make_client):
    ctx = make_ctx(make_client([dev_shop(partner=False)]))
    with pytest.raises(TaskError) as exc:
        ctx.require_development_store()
    text = str(exc.value)
    assert "test-store.myshopify.com" in text
    assert "dev.shopify.com/dashboard" in text


def test_guard_runs_before_anything_is_written(make_client):
    """Ordering matters: a preview that renders first would imply it's safe."""
    client = make_client([dev_shop(partner=False)], writes_enabled=True)
    args = type("A", (), {"count": 5, "clone_from": None, "limit": None,
                          "reset": True})()
    with pytest.raises(TaskError, match="not a development store"):
        sd.run(make_ctx(client), args)
    assert len(client.session.requests) == 1, "only the guard query may have run"


# ---------------------------------------------------------------------------
# Location preconditions
# ---------------------------------------------------------------------------

def test_missing_locations_explain_what_to_create(make_client):
    ctx = make_ctx(make_client([gql({"locations": {"nodes": [
        {"id": "gid://shopify/Location/1", "name": "Shop location"}]}})]))
    with pytest.raises(TaskError) as exc:
        sd._require_locations(ctx)
    text = str(exc.value)
    assert "Home" in text and "Craft Fair Inventory" in text
    assert "Shop location" in text          # says what IS there


def test_present_locations_resolve_to_ids(make_client):
    ctx = make_ctx(make_client([LOCATIONS]))
    assert sd._require_locations(ctx) == LOC_IDS


# ---------------------------------------------------------------------------
# Synthetic generation
# ---------------------------------------------------------------------------

def test_generation_is_deterministic():
    """--reset must return the store to an identical state, or a rehearsal
    can't be repeated."""
    a, b = sd._generate(30), sd._generate(30)
    assert [p.title for p in a] == [p.title for p in b]
    assert [v.quantities for p in a for v in p.variants] == \
           [v.quantities for p in b for v in p.variants]


def test_generation_covers_the_awkward_cases():
    products = sd._generate(30)
    variants = [v for p in products for v in p.variants]

    assert any(not v.tracked for v in variants), "needs an untracked item"
    assert any(len(p.variants) > 1 for p in products), "needs a multi-variant product"
    assert any(v.quantities.get(sd.HOME, 0) > 0 and v.quantities.get(sd.FAIR, 0) == 0
               for v in variants), "needs source-only stock"
    assert any(v.quantities.get(sd.HOME, 0) == 0 and v.quantities.get(sd.FAIR, 0) > 0
               for v in variants), "needs destination-only stock"
    assert any(v.quantities.get(sd.HOME, 0) > 0 and v.quantities.get(sd.FAIR, 0) > 0
               for v in variants), "needs stock at both"
    assert any(not any(v.quantities.values()) for v in variants if v.tracked), \
        "needs a tracked item with nothing anywhere"


def test_generation_leaves_some_items_unactivated_at_each_location():
    """A location absent from quantities is never activated there, so a move
    has to call inventoryActivate. Without these, every item already exists at
    both locations and that path is dead code in the rehearsal."""
    variants = [v for p in sd._generate(30) for v in p.variants if v.tracked]

    assert any(sd.HOME in v.quantities and sd.FAIR not in v.quantities
               for v in variants), "need items not activated at the fair"
    assert any(sd.FAIR in v.quantities and sd.HOME not in v.quantities
               for v in variants), "need items not activated at home"


def test_absent_location_is_distinct_from_zero():
    """Both look like 'no stock', but only one needs activating."""
    variants = [v for p in sd._generate(30) for v in p.variants if v.tracked]
    assert any(v.quantities.get(sd.FAIR) == 0 for v in variants), \
        "need an item stocked-but-empty at the fair"
    assert any(sd.FAIR not in v.quantities for v in variants), \
        "need an item not stocked at the fair at all"


def test_a_multi_variant_product_mixes_activation_states():
    """One product should exercise both paths in a single move."""
    products = sd._generate(30)
    mixed = [
        p for p in products
        if len(p.variants) > 1
        and any(sd.FAIR in v.quantities for v in p.variants)
        and any(sd.FAIR not in v.quantities for v in p.variants)
    ]
    assert mixed, "no multi-variant product mixes activated and unactivated variants"


def test_unactivated_locations_are_omitted_from_the_input():
    """The payload must not mention a location the item isn't stocked at."""
    product = sd.SeedProduct(
        title="X", options=[],
        variants=[sd.SeedVariant([], "1.00", "S", tracked=True,
                                 quantities={sd.HOME: 3})])
    quantities = sd._to_input(product, LOC_IDS)["variants"][0]["inventoryQuantities"]
    assert [q["locationId"] for q in quantities] == [LOC_IDS[sd.HOME]]


def test_generation_includes_a_large_holding():
    """So a move crosses the batch boundary in units, not just item count."""
    products = sd._generate(5)
    assert any(v.quantities.get(sd.HOME, 0) >= 250
               for p in products for v in p.variants)


def test_untracked_variants_carry_no_quantities():
    """Quantities on an untracked item are a userError, not a no-op."""
    products = sd._generate(30)
    for product in products:
        for variant in product.variants:
            if not variant.tracked:
                assert variant.quantities == {}


def test_count_is_respected():
    # +1 for the appended bulk product.
    assert len(sd._generate(10)) == 11


# ---------------------------------------------------------------------------
# productSet payload
# ---------------------------------------------------------------------------

def test_input_maps_quantities_to_location_ids():
    product = sd.SeedProduct(
        title="X", options=[],
        variants=[sd.SeedVariant([], "1.00", "S-1", tracked=True,
                                 quantities={sd.HOME: 5, sd.FAIR: 2})])
    payload = sd._to_input(product, LOC_IDS)
    quantities = payload["variants"][0]["inventoryQuantities"]
    assert {q["locationId"]: q["quantity"] for q in quantities} == {
        LOC_IDS[sd.HOME]: 5, LOC_IDS[sd.FAIR]: 2}
    assert all(q["name"] == "available" for q in quantities)


def test_untracked_input_omits_quantities_entirely():
    product = sd.SeedProduct(
        title="X", options=[],
        variants=[sd.SeedVariant([], "1.00", "S-1", tracked=False)])
    payload = sd._to_input(product, LOC_IDS)
    assert "inventoryQuantities" not in payload["variants"][0]
    assert payload["variants"][0]["inventoryItem"]["tracked"] is False


def test_options_become_product_options():
    product = sd.SeedProduct(
        title="X", options=[("Size", ["6mm", "8mm"])],
        variants=[sd.SeedVariant([("Size", "6mm")], "1.00", "A", tracked=True)])
    payload = sd._to_input(product, LOC_IDS)
    assert payload["productOptions"] == [
        {"name": "Size", "values": [{"name": "6mm"}, {"name": "8mm"}]}]
    assert payload["variants"][0]["optionValues"] == [
        {"optionName": "Size", "name": "6mm"}]


def test_single_variant_products_get_the_default_title_option():
    """productSet rejects variants without productOptions -- "Product options
    input is required when updating variants" -- even when there are no real
    options. Shopify's own shape for that is Title/Default Title."""
    payload = sd._to_input(
        sd.SeedProduct(title="X", options=[],
                       variants=[sd.SeedVariant([], "1.00", "A", tracked=True)]),
        LOC_IDS)
    assert payload["productOptions"] == [
        {"name": "Title", "values": [{"name": "Default Title"}]}]
    assert payload["variants"][0]["optionValues"] == [
        {"optionName": "Title", "name": "Default Title"}]


def test_every_product_input_carries_options():
    """The failure this guards cost 25 of 31 products on the first real run."""
    for product in sd._generate(30):
        payload = sd._to_input(product, LOC_IDS)
        assert payload.get("productOptions"), f"{product.title} has no productOptions"
        for variant in payload["variants"]:
            assert variant["optionValues"], f"{product.title} variant has no optionValues"


# ---------------------------------------------------------------------------
# Cloning
# ---------------------------------------------------------------------------

SOURCE_NODE = {
    "title": "Coral Melange", "descriptionHtml": "", "productType": "Beads",
    "vendor": "Gumball", "tags": ["necklace"], "status": "ACTIVE",
    "options": [{"name": "Title", "optionValues": [{"name": "Default Title"}]}],
    "variants": {"nodes": [{
        "title": "Default Title", "price": "12.00", "sku": "CM-1",
        "selectedOptions": [{"name": "Title", "value": "Default Title"}],
        "inventoryItem": {"tracked": True, "inventoryLevels": {"nodes": [
            {"location": {"name": "Home"},
             "quantities": [{"name": "available", "quantity": 4}]},
            {"location": {"name": "Craft Fair Inventory"},
             "quantities": [{"name": "available", "quantity": 1}]},
        ]}},
    }]},
}


def test_clone_carries_stock_per_location():
    product = sd._from_source(SOURCE_NODE)
    assert product.variants[0].quantities == {"Home": 4, "Craft Fair Inventory": 1}
    assert product.variants[0].sku == "CM-1"


def test_clone_keeps_the_default_title_option():
    """It looks like a placeholder worth dropping, but it is the real structure
    of an optionless product and productSet requires it. Stripping it made
    every single-variant clone fail."""
    product = sd._from_source(SOURCE_NODE)
    assert product.options == [("Title", ["Default Title"])]
    assert product.variants[0].option_values == [("Title", "Default Title")]


def test_cloned_single_variant_products_round_trip():
    """Source shape -> input shape, without losing the required option."""
    payload = sd._to_input(sd._from_source(SOURCE_NODE), LOC_IDS)
    assert payload["productOptions"] == [
        {"name": "Title", "values": [{"name": "Default Title"}]}]
    assert payload["variants"][0]["optionValues"] == [
        {"optionName": "Title", "name": "Default Title"}]


def test_clone_tags_its_output():
    assert "cloned" in sd._from_source(SOURCE_NODE).tags


def test_clone_ignores_locations_we_do_not_mirror():
    node = {**SOURCE_NODE, "variants": {"nodes": [{
        **SOURCE_NODE["variants"]["nodes"][0],
        "inventoryItem": {"tracked": True, "inventoryLevels": {"nodes": [
            {"location": {"name": "Warehouse B"},
             "quantities": [{"name": "available", "quantity": 99}]}]}},
    }]}}
    assert sd._from_source(node).variants[0].quantities == {}


def test_clone_skips_products_with_no_variants():
    assert sd._from_source({**SOURCE_NODE, "variants": {"nodes": []}}) is None


def test_reader_for_is_always_read_only(make_client, write_config):
    """A task writes to one store; a source it reads must not become a second
    write target because --commit was passed."""
    from tests.conftest import VALID_CONFIG
    write_config(VALID_CONFIG)
    ctx = make_ctx(make_client([], writes_enabled=True))
    reader = ctx.reader_for("dev")
    try:
        assert reader.writes_enabled is False
    finally:
        reader.close()


def test_reader_for_refuses_the_same_store(make_client, write_config, shop):
    from dataclasses import replace

    from tests.conftest import VALID_CONFIG
    write_config(VALID_CONFIG)
    client = make_client([], shop_override=replace(shop, domain="acme.myshopify.com"))
    with pytest.raises(TaskError, match="cannot be copied onto itself"):
        make_ctx(client).reader_for("prod")
