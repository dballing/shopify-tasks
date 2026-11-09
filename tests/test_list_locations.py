#!/usr/bin/env python3
"""list-locations: active and disabled together, the limit, and what's blocked."""
from __future__ import annotations

import json
import logging

from shopifylib.task import TaskContext
from tasks import list_locations as ll
from tests.conftest import gql


def make_ctx(client, *, json_output=False) -> TaskContext:
    return TaskContext(client=client, shop=client.shop, dry_run=False,
                       log=logging.getLogger("test"), json_output=json_output)


def loc(n, name, *, active=True, online=False, **extra):
    return {"id": f"gid://shopify/Location/{n}", "name": name, "isActive": active,
            "fulfillsOnlineOrders": online, "hasActiveInventory": False,
            "hasUnfulfilledOrders": False, "deactivatable": True, "deletable": not active,
            "address": {"address1": f"{n} Main St", "address2": None, "city": "Anytown",
                        "provinceCode": "VA", "zip": "22201", "countryCode": "US"},
            **extra}


HOME = loc(1, "Home", online=True, deactivatable=False, hasActiveInventory=True)
MARKET_A = loc(2, "Market A", active=False)
FAIR = loc(3, "Craft Fair", hasActiveInventory=True)


def levels(*rows):
    return gql({"location": {"inventoryLevels": {
        "nodes": [{"item": {"id": item}, "quantities": [{"name": "available",
                                                         "quantity": qty}]}
                  for item, qty in rows],
        "pageInfo": {"hasNextPage": False, "endCursor": None}}}})


def responses(known=(HOME, MARKET_A, FAIR), limit=10):
    # Order of reads: locations, limit, then levels for each *active* location
    # in sorted order (Craft Fair, Home). Disabled ones are never read.
    return [gql({"locations": {"nodes": list(known)}}),
            gql({"shop": {"resourceLimits": {"locationLimit": limit}}}),
            levels(("i1", 4)),
            levels(("i1", 2), ("i2", 5))]


def rows(out: str) -> list[str]:
    return out.split("\n\n")[0].splitlines()[2:]


def test_disabled_locations_are_listed_after_active_ones(make_client, capsys):
    ll.run(make_ctx(make_client(responses())), None)
    names = [r.split("  ")[0] for r in rows(capsys.readouterr().out)]
    assert names == ["Craft Fair", "Home", "Market A"]


def test_units_for_active_and_marker_for_disabled(make_client, capsys):
    ll.run(make_ctx(make_client(responses())), None)
    table = {r.split()[0]: r.split() for r in rows(capsys.readouterr().out)}
    assert table["Craft"][3] == "4"
    assert table["Home"][3] == "7"
    assert table["Market"][3] == "--"
    assert table["Market"][2] == "disabled"


def test_disabled_locations_are_not_read_for_stock(make_client):
    client = make_client(responses())
    ll.run(make_ctx(client), None)
    read = [v.get("id") for v in client.session.variables if "id" in v]
    assert MARKET_A["id"] not in read


def test_limit_footer_is_read_live(make_client, capsys):
    ll.run(make_ctx(make_client(responses(limit=200))), None)
    assert "2 of 200 locations active" in capsys.readouterr().out


def test_blocked_transitions_are_explained(make_client, capsys):
    ll.run(make_ctx(make_client(responses())), None)
    out = capsys.readouterr().out
    assert "Home cannot be disabled (the only location fulfilling online orders)" in out
    assert "Market A" not in out.split("active (disabled")[1]


def test_inventory_records_are_flagged_even_when_shopify_says_deactivatable(
        make_client, capsys):
    """Shopify's deactivatable was true for a location it then refused to
    deactivate over zero-unit records, so the footer doesn't trust it."""
    ll.run(make_ctx(make_client(responses())), None)
    assert ("Craft Fair has inventory records, so disabling it needs --move-stock-to"
            in capsys.readouterr().out)


def test_undeletable_disabled_location_is_explained(make_client, capsys):
    stuck = loc(4, "Market B", active=False, deletable=False, hasUnfulfilledOrders=True)
    ll.run(make_ctx(make_client(responses((HOME, FAIR, stuck)))), None)
    assert "Market B cannot currently be deleted (has unfulfilled orders)" in \
        capsys.readouterr().out


def test_json_includes_flags_and_null_units_for_disabled(make_client, capsys):
    ll.run(make_ctx(make_client(responses()), json_output=True), None)
    payload = json.loads(capsys.readouterr().out)
    assert (payload["active"], payload["limit"]) == (2, 10)
    market = next(l for l in payload["locations"] if l["name"] == "Market A")
    assert market["units"] is None
    assert market["deletable"] is True and "deactivatable" in market


def test_task_is_read_only():
    from shopifylib.task import REGISTRY
    assert REGISTRY["list-locations"].writes is False
