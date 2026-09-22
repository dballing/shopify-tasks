#!/usr/bin/env python3
"""list-inventory: row building, filters, and the shared cell vocabulary.

The cell tests live in test_inventory.py; these cover turning live API shapes
into rows and the flags that narrow them.
"""
from __future__ import annotations

import json
import logging

import pytest

from shopifylib import inventory
from shopifylib.errors import TaskError
from shopifylib.task import TaskContext
from tasks import list_inventory as li
from tests.conftest import gql

HOME = {"id": "gid://shopify/Location/1", "name": "Home",
        "isActive": True, "fulfillsOnlineOrders": True}
FAIR = {"id": "gid://shopify/Location/2", "name": "Craft Fair Inventory",
        "isActive": True, "fulfillsOnlineOrders": False}
LOCATIONS = [HOME, FAIR]


def make_ctx(client, *, json_output=False):
    return TaskContext(client=client, shop=client.shop, dry_run=False,
                       log=logging.getLogger("test"), json_output=json_output)


def level(name, qty):
    """One row of a location's inventoryLevels connection."""
    return {"item": {"id": "x"},
            "quantities": [{"name": "available", "quantity": qty}]}


def product(title="Bead", *, variants=None, tracked=True, status="ACTIVE"):
    """A product node plus the stock map that would accompany it.

    `variants` is a list of per-variant {location: qty} maps; a location absent
    from one means that variant is not activated there.
    """
    variants = variants if variants is not None else [{"Home": 3}]
    node = {
        "id": "gid://shopify/Product/1", "title": title, "status": status,
        "variants": {
            "nodes": [{
                "displayName": f"{title} - Default Title",
                "sku": f"SKU-{i}",
                "inventoryItem": {"id": f"item-{title}-{i}", "tracked": tracked},
            } for i, _ in enumerate(variants)],
            "pageInfo": {"hasNextPage": False},
        },
    }
    stock = {f"item-{title}-{i}": dict(q) for i, q in enumerate(variants)}
    return node, stock


def row_from(pair, ctx=None):
    node, stock = pair
    return li._to_row(ctx or make_ctx(_null_client()), node, stock)


class _null_client:
    shop = None


# ---------------------------------------------------------------------------
# Row building
# ---------------------------------------------------------------------------

def test_row_records_quantities_per_location():
    row = row_from(product(variants=[{"Home": 4, "Craft Fair Inventory": 1}]))
    assert row.at("Home") == 4
    assert row.at("Craft Fair Inventory") == 1
    assert row.total == 5


def test_absent_level_is_not_recorded_as_zero():
    """Presence in the map is the activation flag -- a location the item was
    never activated at must be missing, not zero."""
    row = row_from(product(variants=[{"Home": 4}]))
    assert "Craft Fair Inventory" not in row.variant_quantities[0]
    assert row.at("Craft Fair Inventory") == 0      # still sums to zero


def test_zero_level_is_recorded():
    row = row_from(product(variants=[{"Home": 4, "Craft Fair Inventory": 0}]))
    assert row.variant_quantities[0]["Craft Fair Inventory"] == 0


def test_placeholder_suffix_is_stripped_from_the_title():
    assert row_from(product("Coral Melange")).title == "Coral Melange"


def test_multi_variant_rows_keep_each_variant_separate():
    row = row_from(product(variants=[
        {"Home": 2, "Craft Fair Inventory": 1},
        {"Home": 3},
    ]))
    assert row.variants == 2
    assert row.at("Home") == 5
    assert row.at("Craft Fair Inventory") == 1


def test_untracked_products_are_flagged():
    assert row_from(product(tracked=False)).tracked is False


def test_partially_tracked_product_counts_as_tracked():
    node, stock = product(variants=[{"Home": 1}, {}])
    node["variants"]["nodes"][1]["inventoryItem"]["tracked"] = False
    assert row_from((node, stock)).tracked is True


# ---------------------------------------------------------------------------
# Cells, via the shared vocabulary
# ---------------------------------------------------------------------------

def test_cells_match_the_seed_dev_vocabulary():
    row = row_from(product(variants=[
        {"Home": 2, "Craft Fair Inventory": 0},
        {"Home": 3},
    ]))
    assert inventory.location_cell(row.variant_quantities, "Home") == "5"
    assert inventory.location_cell(
        row.variant_quantities, "Craft Fair Inventory") == "0*"


def test_never_activated_renders_as_the_marker():
    row = row_from(product(variants=[{"Home": 2}]))
    assert inventory.location_cell(
        row.variant_quantities, "Craft Fair Inventory") == inventory.NOT_ACTIVATED


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def args(**kw):
    defaults = dict(pids=None, at=None, with_stock=False, needs_activation=False,
                    untracked=False, sku=False, limit=None)
    defaults.update(kw)
    return type("A", (), defaults)()


def test_with_stock_drops_empty_products():
    empty = row_from(product("Empty", variants=[{"Home": 0}]))
    stocked = row_from(product("Stocked", variants=[{"Home": 2}]))
    kept = li._filter([empty, stocked], LOCATIONS, args(with_stock=True))
    assert [r.title for r in kept] == ["Stocked"]


def test_needs_activation_finds_products_missing_somewhere():
    both = row_from(product("Both", variants=[
        {"Home": 1, "Craft Fair Inventory": 1}]))
    partial = row_from(product("Partial", variants=[{"Home": 1}]))
    kept = li._filter([both, partial], LOCATIONS, args(needs_activation=True))
    assert [r.title for r in kept] == ["Partial"]


def test_untracked_filter():
    tracked = row_from(product("T", tracked=True))
    untracked = row_from(product("U", tracked=False))
    kept = li._filter([tracked, untracked], LOCATIONS, args(untracked=True))
    assert [r.title for r in kept] == ["U"]


def test_filters_respect_the_shown_locations_only():
    """--needs-activation should ignore locations that aren't columns."""
    row = row_from(product("X", variants=[{"Home": 1}]))
    kept = li._filter([row], [HOME], args(needs_activation=True))
    assert kept == [], "activated at every shown location"


# ---------------------------------------------------------------------------
# Location columns
# ---------------------------------------------------------------------------

def test_columns_default_to_every_location_active_first(make_client):
    inactive = {"id": "gid://shopify/Location/3", "name": "Aardvark Depot",
                "isActive": False, "fulfillsOnlineOrders": False}
    ctx = make_ctx(make_client([gql({"locations": {"nodes": [inactive, FAIR, HOME]}})]))
    assert [l["name"] for l in li._columns(ctx, None)] == [
        "Craft Fair Inventory", "Home", "Aardvark Depot"]


def test_at_selects_and_orders_columns(make_client):
    ctx = make_ctx(make_client([gql({"locations": {"nodes": LOCATIONS}})]))
    assert [l["name"] for l in li._columns(ctx, ["Craft Fair"])] == \
        ["Craft Fair Inventory"]


def test_at_accepts_repeats_without_duplicating(make_client):
    ctx = make_ctx(make_client([gql({"locations": {"nodes": LOCATIONS}})]))
    assert len(li._columns(ctx, ["Home", "home"])) == 1


def test_unknown_at_lists_the_real_locations(make_client):
    ctx = make_ctx(make_client([gql({"locations": {"nodes": LOCATIONS}})]))
    with pytest.raises(TaskError, match="Craft Fair Inventory"):
        li._columns(ctx, ["Warehouse"])


def test_store_with_no_locations_is_an_error(make_client):
    ctx = make_ctx(make_client([gql({"locations": {"nodes": []}})]))
    with pytest.raises(TaskError, match="no locations"):
        li._columns(ctx, None)


def test_headers_fall_back_to_full_names_when_truncation_collides():
    names = ["Craft Fair Inventory North", "Craft Fair Inventory South"]
    assert li._short_names(names, width=16) == names


def test_headers_are_shortened_when_distinct():
    assert li._short_names(["Home", "Craft Fair Inventory"], width=10) == \
        ["Home", "Craft Fair"]


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def levels_page(rows):
    """A location.inventoryLevels response."""
    return gql({"location": {"inventoryLevels": {
        "nodes": rows, "pageInfo": {"hasNextPage": False, "endCursor": None}}}})


def item_level(item_id, qty):
    return {"item": {"id": item_id},
            "quantities": [{"name": "available", "quantity": qty}]}


def test_json_makes_activation_explicit(make_client, capsys):
    """0 and 'absent' are different states; a JSON consumer shouldn't have to
    infer which it got."""
    node, _ = product("X", variants=[{"Home": 2}])
    item_id = node["variants"]["nodes"][0]["inventoryItem"]["id"]
    client = make_client([
        gql({"locations": {"nodes": LOCATIONS}}),
        # Columns sort active-first then alphabetically, so Craft Fair is read
        # before Home.
        levels_page([]),                         # Craft Fair: not stocked
        levels_page([item_level(item_id, 2)]),   # Home
        gql({"products": {"nodes": [node],
                          "pageInfo": {"hasNextPage": False, "endCursor": None}}}),
    ])
    li.run(make_ctx(client, json_output=True), args())

    payload = json.loads(capsys.readouterr().out)
    stock = payload["products"][0]["stock"]
    assert stock["Home"] == {"available": 2, "activated_variants": 1}
    assert stock["Craft Fair Inventory"] == {"available": 0, "activated_variants": 0}


def test_levels_are_read_per_location_not_nested_in_products(make_client):
    """Nesting products -> variants -> inventoryLevels blows the 1000-point
    per-query cap (measured at 1007 on a real store). Two flat queries joined
    locally is the shape that scales."""
    node, _ = product("X", variants=[{"Home": 1}])
    item_id = node["variants"]["nodes"][0]["inventoryItem"]["id"]
    client = make_client([
        gql({"locations": {"nodes": LOCATIONS}}),
        levels_page([]),                         # Craft Fair
        levels_page([item_level(item_id, 1)]),   # Home
        gql({"products": {"nodes": [node],
                          "pageInfo": {"hasNextPage": False, "endCursor": None}}}),
    ])
    li.run(make_ctx(client), args())

    documents = [r["payload"]["query"] for r in client.session.requests]
    product_query = next(d for d in documents if "products(" in d)
    assert "inventoryLevels" not in product_query, \
        "the product query must not nest inventoryLevels"


def test_stock_map_is_copied_per_variant(make_client):
    """Rows must not alias the shared map, or mutating one row's quantities
    would silently change another's."""
    node, _ = product("X", variants=[{"Home": 1}])
    item_id = node["variants"]["nodes"][0]["inventoryItem"]["id"]
    shared = {item_id: {"Home": 1}}
    row = li._to_row(make_ctx(_null_client()), node, shared)
    row.variant_quantities[0]["Home"] = 99
    assert shared[item_id]["Home"] == 1


def test_task_is_read_only(make_client):
    """No --commit, no writes; listing stock must never mutate."""
    from shopifylib.task import REGISTRY
    assert REGISTRY["list-inventory"].writes is False
