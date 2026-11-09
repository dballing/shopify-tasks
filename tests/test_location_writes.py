#!/usr/bin/env python3
"""add-, enable-, disable- and delete-location.

Several rules here encode live findings from the dev store rather than the
docs, and say so: activate/deactivate need @idempotent; zero-unit inventory
records block deactivation; deletion requires a disabled location.
"""
from __future__ import annotations

import json
import logging

import pytest

from shopifylib.errors import TaskError
from shopifylib.task import TaskContext
from tasks import add_location, delete_location, disable_location, enable_location
from tests.conftest import gql
from tests.test_list_locations import loc

HOME = loc(1, "Home", online=True, hasActiveInventory=True)
FAIR = loc(3, "Craft Fair", hasActiveInventory=True)
EMPTY = loc(4, "Market B")                                  # active, no records
MARKET_A = loc(2, "Market A", active=False)                 # disabled, deletable


def make_ctx(client, *, dry_run=False, json_output=False) -> TaskContext:
    return TaskContext(client=client, shop=client.shop, dry_run=dry_run,
                       log=logging.getLogger("test"), json_output=json_output)


def known(*locs):
    return gql({"locations": {"nodes": list(locs)}})


def limit(n=10):
    return gql({"shop": {"resourceLimits": {"locationLimit": n}}})


def ok(field, **payload):
    errors_key = {"locationActivate": "locationActivateUserErrors",
                  "locationDeactivate": "locationDeactivateUserErrors",
                  "locationDelete": "locationDeleteUserErrors"}.get(field, "userErrors")
    return gql({field: {errors_key: [], **payload}})


def failed(field, code, message="nope"):
    errors_key = {"locationActivate": "locationActivateUserErrors",
                  "locationDeactivate": "locationDeactivateUserErrors",
                  "locationDelete": "locationDeleteUserErrors"}[field]
    return gql({field: {errors_key: [{"field": ["locationId"], "message": message,
                                      "code": code}]}})


def sent(client, field) -> list[dict]:
    return [r["payload"] for r in client.session.requests
            if field + "(" in r["payload"]["query"]]


def ns(**kw):
    return type("A", (), kw)()


# ---------------------------------------------------------------------------
# add-location
# ---------------------------------------------------------------------------

def add_args(**kw):
    base = dict(name="Market C", address1=" 123 Main St ", address2=None,
                city="Anytown", state="va", zip="22201", country="us", phone=None,
                fulfill_online=False, disabled=False)
    base.update(kw)
    return ns(**base)


ADDRESS = {"address1": "123 Main St", "address2": None, "city": "Anytown",
           "provinceCode": "VA", "zip": "22201", "countryCode": "US"}


def added(suggestions=(), address=ADDRESS):
    return ok("locationAdd", location={"id": "gid://shopify/Location/9",
                                       "name": "Market C", "address": address,
                                       "suggestedAddresses": list(suggestions)})


def test_add_sends_a_normalised_address_and_is_pos_only_by_default(make_client):
    client = make_client([known(HOME), limit(), added()], writes_enabled=True)
    add_location.run(make_ctx(client), add_args())
    [payload] = sent(client, "locationAdd")
    assert payload["variables"]["input"] == {
        "name": "Market C", "fulfillsOnlineOrders": False,
        "address": {"address1": "123 Main St", "city": "Anytown",
                    "provinceCode": "VA", "zip": "22201", "countryCode": "US"}}


def test_fulfill_online_is_opt_in(make_client):
    client = make_client([known(HOME), limit(), added()], writes_enabled=True)
    add_location.run(make_ctx(client), add_args(fulfill_online=True))
    assert sent(client, "locationAdd")[0]["variables"]["input"]["fulfillsOnlineOrders"]


def test_duplicate_name_is_refused_even_against_a_disabled_location(make_client):
    client = make_client([known(HOME, MARKET_A)])
    with pytest.raises(TaskError, match="already exists \\(disabled\\)"):
        add_location.run(make_ctx(client), add_args(name="market a"))


def test_full_house_is_refused_before_any_write(make_client):
    """locationAdd creates an active location, so even --disabled needs a slot."""
    client = make_client([known(HOME, FAIR), limit(2)])
    with pytest.raises(TaskError, match="2 of 2 locations are already active"):
        add_location.run(make_ctx(client), add_args(disabled=True))
    assert sent(client, "locationAdd") == []


def test_disabled_flag_adds_then_deactivates_with_an_idempotency_key(make_client):
    client = make_client([known(HOME), limit(), added(), ok("locationDeactivate")],
                         writes_enabled=True)
    add_location.run(make_ctx(client), add_args(disabled=True))
    [deactivate] = sent(client, "locationDeactivate")
    assert "@idempotent" in deactivate["query"]
    assert deactivate["variables"]["key"] and deactivate["variables"]["destination"] is None


def test_dry_run_sends_nothing(make_client):
    client = make_client([known(HOME), limit()])
    add_location.run(make_ctx(client, dry_run=True), add_args(disabled=True))
    assert sent(client, "locationAdd") == [] and sent(client, "locationDeactivate") == []


SUGGESTED = {**ADDRESS, "address1": "123 Main St", "zip": "22201-1234"}


def test_one_suggestion_is_accepted_and_reported(make_client, caplog):
    client = make_client([
        known(HOME), limit(), added([SUGGESTED]),
        ok("locationEdit", location={"id": "gid://shopify/Location/9",
                                     "address": SUGGESTED}),
    ], writes_enabled=True)
    assert add_location.run(make_ctx(client), add_args()) == 0
    [edit] = sent(client, "locationEdit")
    assert edit["variables"]["input"]["address"]["zip"] == "22201-1234"
    assert "address2" not in edit["variables"]["input"]["address"], "nulls not sent"
    assert ("Your address 123 Main St, Anytown VA 22201 was suggested by Shopify's "
            "address verification to be changed to: 123 Main St, Anytown VA "
            "22201-1234, and we did so. Please verify this for accuracy") in caplog.text


def test_several_suggestions_apply_none_and_exit_1(make_client, caplog):
    """Their order isn't guaranteed stable, so picking 'the first' is a guess."""
    other = {**ADDRESS, "zip": "22202"}
    client = make_client([known(HOME), limit(), added([SUGGESTED, other]),
                          ok("locationDeactivate")], writes_enabled=True)
    assert add_location.run(make_ctx(client), add_args(disabled=True)) == 1
    assert sent(client, "locationEdit") == []
    assert "22201-1234" in caplog.text and "22202" in caplog.text
    assert sent(client, "locationDeactivate"), "still disabled, not left using a slot"


def test_json_reports_the_address_on_file(make_client, capsys):
    client = make_client([known(HOME), limit(), added()], writes_enabled=True)
    add_location.run(make_ctx(client, json_output=True), add_args())
    out = json.loads(capsys.readouterr().out)
    assert out["address"]["zip"] == "22201" and out["ok"] is True


# ---------------------------------------------------------------------------
# enable-location
# ---------------------------------------------------------------------------

def test_enable_sends_an_idempotency_key(make_client):
    """Required at runtime, unmarked in the schema -- verified on dev."""
    client = make_client([known(HOME, MARKET_A), limit(),
                          ok("locationActivate", location={"id": "x", "isActive": True})],
                         writes_enabled=True)
    enable_location.run(make_ctx(client), ns(location="Market A"))
    [payload] = sent(client, "locationActivate")
    assert "@idempotent(key: $key)" in payload["query"] and payload["variables"]["key"]


def test_enable_refuses_an_active_location(make_client):
    with pytest.raises(TaskError, match="already active"):
        enable_location.run(make_ctx(make_client([known(HOME)])), ns(location="Home"))


def test_enable_at_the_limit_lists_whats_active(make_client):
    client = make_client([known(HOME, FAIR, MARKET_A), limit(2)])
    with pytest.raises(TaskError, match="(?s)Disable one first.*Craft Fair.*Home"):
        enable_location.run(make_ctx(client), ns(location="Market A"))


def test_enable_explains_shopify_errors_by_code(make_client):
    client = make_client([known(HOME, MARKET_A), limit(),
                          failed("locationActivate", "LOCATION_LIMIT")],
                         writes_enabled=True)
    with pytest.raises(TaskError, match="active-location limit is reached"):
        enable_location.run(make_ctx(client), ns(location="Market A"))


# ---------------------------------------------------------------------------
# disable-location
# ---------------------------------------------------------------------------

def on_hand(*rows):
    """A location's records as ON_HAND_QUERY sees them: (available, on_hand)."""
    return gql({"location": {"inventoryLevels": {
        "nodes": [{"item": {"id": f"i{n}"},
                   "quantities": [{"name": "available", "quantity": a},
                                  {"name": "on_hand", "quantity": h}]}
                  for n, (a, h) in enumerate(rows)],
        "pageInfo": {"hasNextPage": False, "endCursor": None}}}})


def test_units_on_hand_are_refused_even_with_the_flag(make_client):
    """Shopify's own relocation logs 'Items relocated' with no creator; units
    go through move-inventory instead, which attributes them."""
    client = make_client([known(HOME, FAIR), on_hand((4, 4), (1, 1), (0, 0))])
    with pytest.raises(TaskError) as exc:
        disable_location.run(make_ctx(client), ns(location="Craft Fair",
                                                  move_stock_to="Home"))
    assert "5 unit(s) on hand across 2 item(s)" in str(exc.value)
    assert ('move-inventory --from "Craft Fair" --to Home --all --commit'
            in str(exc.value))
    assert "not available" not in str(exc.value)
    assert sent(client, "locationDeactivate") == []


def test_unavailable_units_are_called_out(make_client):
    """on_hand, not available: damaged units would otherwise be relocated by
    Shopify, unattributed, and move-inventory wouldn't have moved them."""
    client = make_client([known(HOME, FAIR), on_hand((0, 2))])
    with pytest.raises(TaskError, match="2 of those unit\\(s\\) are on hand but not "
                                        "available"):
        disable_location.run(make_ctx(client), ns(location="Craft Fair",
                                                  move_stock_to="Home"))


def test_zero_units_still_need_the_flag(make_client):
    """Verified on dev: zero-unit records block deactivation outright."""
    client = make_client([known(HOME, FAIR), on_hand((0, 0))])
    with pytest.raises(TaskError, match="holds no units, but still has inventory "
                                        "records") as exc:
        disable_location.run(make_ctx(client), ns(location="Craft Fair",
                                                  move_stock_to=None))
    assert 'disable-location "Craft Fair" --move-stock-to Home' in str(exc.value)


def test_empty_records_are_cleared_via_the_destination(make_client):
    client = make_client([known(HOME, FAIR), on_hand((0, 0), (0, 0)),
                          ok("locationDeactivate", location={"id": "x", "isActive": False})],
                         writes_enabled=True)
    disable_location.run(make_ctx(client), ns(location="Craft Fair", move_stock_to="Home"))
    [payload] = sent(client, "locationDeactivate")
    assert payload["variables"]["destination"] == HOME["id"]
    assert payload["variables"]["id"] == FAIR["id"]
    assert "@idempotent(key: $key)" in payload["query"]


def test_location_without_records_needs_no_destination(make_client):
    client = make_client([known(HOME, EMPTY),
                          ok("locationDeactivate", location={"id": "x", "isActive": False})],
                         writes_enabled=True)
    disable_location.run(make_ctx(client), ns(location="Market B", move_stock_to=None))
    assert sent(client, "locationDeactivate")[0]["variables"]["destination"] is None


def test_home_is_never_disabled(make_client):
    with pytest.raises(TaskError, match="only location fulfilling online orders"):
        disable_location.run(make_ctx(make_client([known(HOME, FAIR)])),
                             ns(location="Home", move_stock_to="Craft Fair"))


def test_unfulfilled_orders_are_refused_up_front(make_client):
    busy = loc(5, "Market D", hasUnfulfilledOrders=True)
    with pytest.raises(TaskError, match="unfulfilled orders"):
        disable_location.run(make_ctx(make_client([known(HOME, busy)])),
                             ns(location="Market D", move_stock_to="Home"))


def test_destination_must_be_active(make_client):
    with pytest.raises(TaskError, match="is disabled"):
        disable_location.run(make_ctx(make_client([known(HOME, FAIR, MARKET_A),
                                                   on_hand((0, 0))])),
                             ns(location="Craft Fair", move_stock_to="Market A"))


def test_disable_explains_shopify_errors_by_code(make_client):
    client = make_client([known(HOME, EMPTY),
                          failed("locationDeactivate", "HAS_FULFILLMENT_ORDERS_ERROR")],
                         writes_enabled=True)
    with pytest.raises(TaskError, match="POS sale not yet marked fulfilled"):
        disable_location.run(make_ctx(client), ns(location="Market B", move_stock_to=None))


def test_disable_dry_run_sends_nothing(make_client):
    client = make_client([known(HOME, FAIR), on_hand((0, 0))])
    disable_location.run(make_ctx(client, dry_run=True),
                         ns(location="Craft Fair", move_stock_to="Home"))
    assert sent(client, "locationDeactivate") == []


# ---------------------------------------------------------------------------
# delete-location
# ---------------------------------------------------------------------------

def test_delete_a_disabled_location(make_client):
    client = make_client([known(HOME, MARKET_A),
                          ok("locationDelete", deletedLocationId=MARKET_A["id"])],
                         writes_enabled=True)
    delete_location.run(make_ctx(client), ns(location="Market A"))
    [payload] = sent(client, "locationDelete")
    assert payload["variables"]["id"] == MARKET_A["id"]
    assert "@idempotent" not in payload["query"], "not needed for delete (verified)"


def test_delete_refuses_an_active_location_and_says_disable_first(make_client):
    with pytest.raises(TaskError, match="Disable it first"):
        delete_location.run(make_ctx(make_client([known(HOME, FAIR)])),
                            ns(location="Craft Fair"))


def test_delete_needs_the_exact_name(make_client):
    with pytest.raises(TaskError, match="full name or id"):
        delete_location.run(make_ctx(make_client([known(HOME, MARKET_A)])),
                            ns(location="Market"))


def test_delete_refuses_home(make_client):
    with pytest.raises(TaskError, match="only location fulfilling online orders"):
        delete_location.run(make_ctx(make_client([known(HOME)])), ns(location="Home"))


def test_delete_dry_run_sends_nothing(make_client):
    client = make_client([known(HOME, MARKET_A)])
    delete_location.run(make_ctx(client, dry_run=True), ns(location="Market A"))
    assert sent(client, "locationDelete") == []
