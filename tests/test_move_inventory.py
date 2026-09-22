#!/usr/bin/env python3
"""move-inventory: location resolution, additive math, and partial failure.

The arithmetic tests encode the contract directly -- Home=2/Fair=1 becomes
Home=0/Fair=3 -- because the failure mode of getting this wrong is silently
wrong stock counts rather than an error.
"""
from __future__ import annotations

import logging

import pytest

from shopifylib.errors import TaskError
from shopifylib.task import TaskContext
from tasks import move_inventory as mi
from tests.conftest import gql

HOME = {"id": "gid://shopify/Location/1", "name": "Home",
        "isActive": True, "fulfillsOnlineOrders": True}
FAIR = {"id": "gid://shopify/Location/2", "name": "Craft Fair Inventory",
        "isActive": True, "fulfillsOnlineOrders": False}
LOCATIONS = [FAIR, HOME]


def make_ctx(client, *, dry_run=False, json_output=False) -> TaskContext:
    return TaskContext(client=client, shop=client.shop, dry_run=dry_run,
                       log=logging.getLogger("test"), json_output=json_output)


def qty(n):
    return {"quantities": [{"name": "available", "quantity": n}]}


def level(n, *, level_id="gid://shopify/InventoryLevel/9"):
    return {"id": level_id, **qty(n)}


def move(qty_=2, from_=2, to_=1, **kw):
    defaults = dict(item_id="gid://shopify/InventoryItem/1", label="Bead - Blue",
                    sku="B-1", qty=qty_, from_available=from_, to_available=to_,
                    needs_activation=False)
    defaults.update(kw)
    return mi.Move(**defaults)


# ---------------------------------------------------------------------------
# The arithmetic contract
# ---------------------------------------------------------------------------

def test_move_is_additive_not_a_copy():
    """Home=2, Fair=1 -> Home=0, Fair=3. The spec, encoded."""
    m = move(qty_=2, from_=2, to_=1)
    assert (m.new_from, m.new_to) == (0, 3)


def test_totals_are_conserved():
    m = move(qty_=7, from_=7, to_=13)
    assert m.from_available + m.to_available == m.new_from + m.new_to


def test_destination_with_nothing_still_gains():
    m = move(qty_=5, from_=5, to_=0)
    assert (m.new_from, m.new_to) == (0, 5)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def test_zero_stock_at_source_is_skipped():
    """'A product with 0 inventory on the --from side has no impact.'"""
    built = mi._build_move(item={"id": "i", "tracked": True}, from_available=0,
                           to_level=level(4), label="x")
    assert built.skipped == "nothing at source"


def test_untracked_inventory_is_skipped():
    """No quantity exists to move; adjusting would error."""
    built = mi._build_move(item={"id": "i", "tracked": False}, from_available=3,
                           to_level=level(0), label="x")
    assert built.skipped == "inventory not tracked"


def test_missing_destination_level_means_activation_needed():
    """A null level means the item was never stocked there."""
    built = mi._build_move(item={"id": "i", "tracked": True}, from_available=3,
                           to_level=None, label="x")
    assert built.needs_activation and built.to_available == 0


def test_existing_destination_level_needs_no_activation():
    built = mi._build_move(item={"id": "i", "tracked": True}, from_available=3,
                           to_level=level(2), label="x")
    assert not built.needs_activation and built.to_available == 2


def test_default_title_suffix_is_stripped():
    """Most products here have one variant, so Shopify's placeholder suffix
    would otherwise appear on nearly every row and crowd out the real name."""
    assert mi._clean_label("Coral Melange - Default Title") == "Coral Melange"


def test_real_variant_names_are_kept():
    assert mi._clean_label("Bead - Blue") == "Bead - Blue"
    assert mi._clean_label('Coral Candy - 16" with extender') == \
        'Coral Candy - 16" with extender'


def test_available_is_read_from_the_quantities_list():
    assert mi._available(level(5)) == 5
    assert mi._available(None) == 0
    assert mi._available({"quantities": []}) == 0
    assert mi._available({"quantities": [{"name": "on_hand", "quantity": 9}]}) == 0


# ---------------------------------------------------------------------------
# Location resolution
# ---------------------------------------------------------------------------

def test_resolves_by_exact_name():
    assert mi._resolve_location(LOCATIONS, "Home", "--from")["id"] == HOME["id"]


def test_name_match_is_case_insensitive():
    assert mi._resolve_location(LOCATIONS, "home", "--from")["id"] == HOME["id"]


def test_resolves_by_unique_partial_name():
    """'Craft Fair' should find 'Craft Fair Inventory'."""
    assert mi._resolve_location(LOCATIONS, "Craft Fair", "--to")["id"] == FAIR["id"]


def test_resolves_by_numeric_id_and_gid():
    assert mi._resolve_location(LOCATIONS, "1", "--from")["id"] == HOME["id"]
    assert mi._resolve_location(
        LOCATIONS, "gid://shopify/Location/2", "--to")["id"] == FAIR["id"]


def test_exact_name_beats_a_partial_match():
    locations = [{"id": "gid://shopify/Location/3", "name": "Home Annex",
                  "isActive": True, "fulfillsOnlineOrders": True}, HOME]
    assert mi._resolve_location(locations, "Home", "--from")["id"] == HOME["id"]


def test_ambiguous_partial_is_an_error_listing_candidates():
    locations = [FAIR, {"id": "gid://shopify/Location/3", "name": "Craft Fair Backup",
                        "isActive": True, "fulfillsOnlineOrders": False}]
    with pytest.raises(TaskError, match="more than one location"):
        mi._resolve_location(locations, "Craft", "--to")


def test_unknown_location_lists_what_exists():
    with pytest.raises(TaskError, match="Craft Fair Inventory"):
        mi._resolve_location(LOCATIONS, "Warehouse", "--from")


# ---------------------------------------------------------------------------
# Compare-and-swap / partial failure
# ---------------------------------------------------------------------------

def test_changes_are_paired_with_compare_and_swap(make_client):
    """Each item emits -qty at source and +qty at destination, each guarded by
    the quantity the scan observed."""
    client = make_client(
        [gql({"inventoryAdjustQuantities": {"userErrors": [],
                                            "inventoryAdjustmentGroup": {"id": "g"}}})],
        writes_enabled=True)
    ctx = make_ctx(client)
    mi._adjust_batch(ctx, [move(qty_=2, from_=2, to_=1)],
                     HOME["id"], FAIR["id"], reason="movement_created")

    changes = client.session.requests[0]["payload"]["variables"]["input"]["changes"]
    assert changes[0] == {"inventoryItemId": "gid://shopify/InventoryItem/1",
                          "locationId": HOME["id"], "delta": -2, "changeFromQuantity": 2}
    assert changes[1] == {"inventoryItemId": "gid://shopify/InventoryItem/1",
                          "locationId": FAIR["id"], "delta": 2, "changeFromQuantity": 1}


def test_adjustment_carries_an_idempotency_key(make_client):
    """Mandatory for inventory mutations since API 2026-04, and it stops a
    retried request from double-applying."""
    client = make_client(
        [gql({"inventoryAdjustQuantities": {"userErrors": []}})], writes_enabled=True)
    mi._adjust_batch(make_ctx(client), [move()], HOME["id"], FAIR["id"], reason="r")

    variables = client.session.requests[0]["payload"]["variables"]
    assert len(variables["key"]) == 36            # a UUID
    assert "@idempotent" in client.session.requests[0]["payload"]["query"]


def test_state_moved_is_available_not_on_hand(make_client):
    """Committed stock must stay where it will ship from."""
    client = make_client(
        [gql({"inventoryAdjustQuantities": {"userErrors": []}})], writes_enabled=True)
    mi._adjust_batch(make_ctx(client), [move()], HOME["id"], FAIR["id"], reason="r")
    assert client.session.requests[0]["payload"]["variables"]["input"]["name"] == "available"


# The exact userError Shopify returns for a lost compare-and-swap, captured
# from a live run against a development store. Using the real shape matters:
# the whole partial-retry path keys off that field-path index, and an invented
# fixture would prove nothing about the real one.
STALE_MESSAGE = ("The changeFromQuantity argument no longer matches the "
                 "persisted quantity.")


def stale_error(change_index: int) -> dict:
    return {"field": ["input", "changes", str(change_index), "changeFromQuantity"],
            "message": STALE_MESSAGE,
            "code": mi.STALE_CODE}


def test_conflicting_item_is_dropped_and_the_rest_retried(make_client):
    """A sale landing mid-run must cost one item, not the whole run."""
    a, b, c = move(), move(item_id="B", label="B"), move(item_id="C", label="C")
    client = make_client([
        # changes[2]/[3] are item index 1 -> B loses the compare-and-swap.
        gql({"inventoryAdjustQuantities": {"userErrors": [stale_error(2)]}}),
        gql({"inventoryAdjustQuantities": {"userErrors": []}}),
    ], writes_enabled=True)

    mi._adjust_batch(make_ctx(client), [a, b, c], HOME["id"], FAIR["id"], reason="r")

    assert b.failed == STALE_MESSAGE and b.stale is True
    assert a.failed is None and c.failed is None
    retried = client.session.requests[1]["payload"]["variables"]["input"]["changes"]
    assert [ch["inventoryItemId"] for ch in retried] == [
        a.item_id, a.item_id, "C", "C"]


def test_staleness_is_read_from_the_code_not_the_message(make_client):
    """The real message contains none of the words you'd grep for -- no
    'changed', no 'conflict' -- so only the code identifies it."""
    assert "chang" not in STALE_MESSAGE.lower().replace("changefromquantity", "")

    message, stale = mi._error_for([stale_error(0)], 0)
    assert stale is True and message == STALE_MESSAGE

    _, stale = mi._error_for(
        [{"field": ["input", "changes", "0"], "message": "nope", "code": "OTHER"}], 0)
    assert stale is False


def test_non_stale_failures_are_not_flagged_as_concurrency(make_client):
    """An activation failure told to 're-run to pick them up' sends you hunting
    a sale that never happened."""
    a = move()
    client = make_client([
        gql({"inventoryAdjustQuantities": {"userErrors": [
            {"field": ["input", "changes", "0"], "message": "invalid reason",
             "code": "INVALID"}]}}),
        gql({"inventoryAdjustQuantities": {"userErrors": []}}),
    ], writes_enabled=True)
    mi._adjust_batch(make_ctx(client), [a, move(item_id="B")],
                     HOME["id"], FAIR["id"], reason="bogus")
    assert a.failed == "invalid reason" and a.stale is False


def test_retry_uses_a_fresh_idempotency_key(make_client):
    """Reusing a key with a different payload would replay the first result."""
    a, b = move(), move(item_id="B")
    client = make_client([
        gql({"inventoryAdjustQuantities": {"userErrors": [
            stale_error(2)]}}),
        gql({"inventoryAdjustQuantities": {"userErrors": []}}),
    ], writes_enabled=True)
    mi._adjust_batch(make_ctx(client), [a, b], HOME["id"], FAIR["id"], reason="r")

    keys = [r["payload"]["variables"]["key"] for r in client.session.requests]
    assert keys[0] != keys[1]


def test_unattributable_errors_fail_the_whole_batch(make_client):
    """Better to report everything than to guess which entry was rejected."""
    a, b = move(), move(item_id="B")
    client = make_client([
        gql({"inventoryAdjustQuantities": {"userErrors": [
            {"field": None, "message": "invalid reason"}]}}),
    ], writes_enabled=True)
    mi._adjust_batch(make_ctx(client), [a, b], HOME["id"], FAIR["id"], reason="bogus")
    assert a.failed == "invalid reason" and b.failed == "invalid reason"


def test_failed_indices_maps_change_position_to_item():
    """Two changes per item, so change 3 belongs to item 1."""
    errors = [{"field": ["input", "changes", "3", "changeFromQuantity"]}]
    assert mi._failed_indices(errors, 6) == {1}


def test_failed_indices_ignores_out_of_range_positions():
    assert mi._failed_indices([{"field": ["input", "changes", "99"]}], 4) == set()


def test_retries_are_bounded(make_client):
    """A batch that keeps failing must not loop."""
    moves = [move(item_id=str(i)) for i in range(5)]
    client = make_client([
        gql({"inventoryAdjustQuantities": {"userErrors": [
            stale_error(0)]}})
    ] * 8, writes_enabled=True)
    mi._adjust_batch(make_ctx(client), moves, HOME["id"], FAIR["id"], reason="r")
    assert len(client.session.requests) <= 4
    assert all(m.failed for m in moves)


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------

def test_only_unstocked_items_are_activated(make_client):
    stocked, unstocked = move(), move(item_id="B", needs_activation=True)
    client = make_client(
        [gql({"inventoryActivate": {"inventoryLevel": {"id": "l"}, "userErrors": []}})],
        writes_enabled=True)
    mi._activate(make_ctx(client), [stocked, unstocked], FAIR)

    assert len(client.session.requests) == 1
    variables = client.session.requests[0]["payload"]["variables"]
    assert variables["inventoryItemId"] == "B"
    assert variables["locationId"] == FAIR["id"]


def test_activation_carries_an_idempotency_key(make_client):
    """inventoryActivate needs @idempotent too, though Shopify's changelog for
    the 2026-04 change does not list it. Discovered the hard way: without a key
    it fails at runtime, and every activation in a real run was rejected."""
    m = move(item_id="B", needs_activation=True)
    client = make_client(
        [gql({"inventoryActivate": {"inventoryLevel": {"id": "l"}, "userErrors": []}})],
        writes_enabled=True)
    mi._activate(make_ctx(client), [m], FAIR)

    request = client.session.requests[0]["payload"]
    assert "@idempotent" in request["query"]
    assert len(request["variables"]["key"]) == 36
    assert m.failed is None


def test_activation_failure_is_confined_to_that_item(make_client):
    a = move(item_id="A", needs_activation=True)
    b = move(item_id="B", needs_activation=True)
    client = make_client([
        gql({"inventoryActivate": {"userErrors": [
            {"field": ["inventoryItemId"], "message": "cannot activate"}]}}),
        gql({"inventoryActivate": {"inventoryLevel": {"id": "l"}, "userErrors": []}}),
    ], writes_enabled=True)
    mi._activate(make_ctx(client), [a, b], FAIR)

    assert a.failed and "cannot activate" in a.failed
    assert b.failed is None


def test_nothing_to_activate_makes_no_calls(make_client):
    client = make_client([], writes_enabled=True)
    mi._activate(make_ctx(client), [move()], FAIR)
    assert client.session.requests == []


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def test_dry_run_writes_nothing(make_client, capsys):
    """The whole point: preview a bulk stock change before it happens."""
    scan = gql({"location": {"inventoryLevels": {
        "nodes": [{
            **qty(2),
            "item": {"id": "gid://shopify/InventoryItem/1", "sku": "B-1",
                     "tracked": True,
                     "variant": {"id": "v", "displayName": "Bead - Blue",
                                 "product": {"id": "p", "title": "Bead",
                                             "status": "ACTIVE"}},
                     "toLevel": level(1)},
        }],
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }}})
    client = make_client([gql({"locations": {"nodes": LOCATIONS}}), scan],
                         writes_enabled=False)
    ctx = make_ctx(client, dry_run=True)

    args = type("A", (), {"from_location": "Home", "to_location": "Craft Fair",
                          "all": True, "pids": None, "reason": "movement_created",
                          "limit": None, "no_activate": False,
                          "include_archived": False})()
    mi.run(ctx, args)

    assert len(client.session.requests) == 2, "reads only; nothing written"
    assert ctx.writes_performed == 0 and ctx.writes_skipped == 1
    out = capsys.readouterr().out
    assert "2 -> 0" in out and "1 -> 3" in out    # the additive preview


# ---------------------------------------------------------------------------
# Archived products
# ---------------------------------------------------------------------------

def archived_item(available=3):
    """An archived product's inventory item, as the location-first scan sees it."""
    return {
        "id": "gid://shopify/InventoryItem/9", "sku": "ARCH-1", "tracked": True,
        "variant": {"id": "v", "displayName": "Retired Bead",
                    "product": {"id": "p", "title": "Retired Bead",
                                "status": "ARCHIVED"}},
    }


def test_archived_products_are_skipped_by_default():
    """An archived product keeps its inventory levels, so the location-first
    scan still sees it -- filtering is a deliberate act."""
    built = mi._build_move(item=archived_item(), from_available=3, to_level=level(0),
                           label="Retired Bead", status="ARCHIVED",
                           include_archived=False)
    assert built.skipped == "product archived"


def test_archived_products_move_when_asked():
    built = mi._build_move(item=archived_item(), from_available=3, to_level=level(0),
                           label="Retired Bead", status="ARCHIVED",
                           include_archived=True)
    assert built.skipped is None and built.qty == 3


def test_archived_takes_priority_over_other_skip_reasons():
    """Reporting an archived product as 'nothing at source' would hide why it
    was left behind, and the two have different fixes."""
    built = mi._build_move(item=archived_item(), from_available=0, to_level=level(0),
                           label="x", status="ARCHIVED", include_archived=False)
    assert built.skipped == "product archived"


def test_active_products_are_unaffected_by_the_filter():
    built = mi._build_move(item={"id": "i", "tracked": True}, from_available=2,
                           to_level=level(0), label="x", status="ACTIVE",
                           include_archived=False)
    assert built.skipped is None


def test_draft_products_still_move():
    """Only archiving means 'out of sight'; a draft is merely unfinished."""
    built = mi._build_move(item={"id": "i", "tracked": True}, from_available=2,
                           to_level=level(0), label="x", status="DRAFT",
                           include_archived=False)
    assert built.skipped is None
