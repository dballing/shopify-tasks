#!/usr/bin/env python3
"""List every location, active or disabled, with its address and stock.

    ./shopify.sh list-locations
    ./shopify.sh list-locations --json | jq ...

Read-only. Market venues are kept disabled between shows -- only active
locations count toward the plan's limit -- so the admin's location list is
mostly the place you *can't* see them at a glance. This is that glance:

    status    active, or disabled (Shopify's "deactivated")
    online    whether stock here is sellable on the online store
    units     available units held; -- for a disabled location, which
              cannot hold any
    address   what POS charges tax against

The footer counts active locations against the live limit, and names any
location Shopify would currently refuse to disable or delete, with the reason
when Shopify gives one.
"""
from __future__ import annotations

from shopifylib import inventory, locations, output
from shopifylib.inventory import NOT_ACTIVATED
from shopifylib.task import task


@task(
    name="list-locations",
    help="List all locations, active and disabled, with address and stock",
    scopes=["read_locations", "read_inventory"],
)
def run(ctx, args):
    """List all locations, active and disabled, with address and stock.

    Read-only. Shows how many locations are active against the plan's limit.
    """
    known = sorted(locations.load(ctx), key=locations.sort_key)
    limit = locations.location_limit(ctx)
    # Disabled locations hold nothing and their levels are not worth reading.
    stock = inventory.read_levels(ctx.client, locations.active(known))
    units = {l["name"]: 0 for l in known}
    for per_location in stock.values():
        for name, qty in per_location.items():
            units[name] += qty

    if ctx.json_output:
        output.emit_json({
            "active": len(locations.active(known)), "limit": limit,
            "locations": [{**l, "units": units[l["name"]] if l["isActive"] else None}
                          for l in known],
        })
        return

    print(output.format_table([_row(l, units) for l in known]))
    print(f"\n{len(locations.active(known))} of {limit} locations active "
          f"(disabled ones don't count)")
    for line in _blocked(known):
        print(line)


def _row(location: dict, units: dict[str, int]) -> dict:
    address = locations.one_line(location.get("address"))
    return {
        "name": location["name"],
        "status": "active" if location["isActive"] else "disabled",
        "online": "yes" if location.get("fulfillsOnlineOrders") else "",
        "units": units[location["name"]] if location["isActive"] else NOT_ACTIVATED,
        "address": address,
    }


def _blocked(known: list[dict]) -> list[str]:
    """What stands between each location and its next step.

    Computed here rather than read from Shopify's ``deactivatable``, which said
    true for a location Shopify then refused to deactivate (it had zero-unit
    inventory records). Only the transition that applies is reported: active
    locations get disabled, disabled ones get deleted.
    """
    lines = []
    for location in known:
        name = location["name"]
        if location["isActive"]:
            reasons = []
            if locations.is_last_online(known, location):
                reasons.append("the only location fulfilling online orders")
            if location.get("hasUnfulfilledOrders"):
                reasons.append("has unfulfilled orders")
            if reasons:
                lines.append(f"  {name} cannot be disabled ({', '.join(reasons)})")
            elif location.get("hasActiveInventory"):
                lines.append(f"  {name} has inventory records, so disabling it needs "
                             f"--move-stock-to")
        elif not location.get("deletable", True):
            reasons = [r for r, flag in (("holds inventory", "hasActiveInventory"),
                                         ("has unfulfilled orders", "hasUnfulfilledOrders"))
                       if location.get(flag)]
            why = f" ({', '.join(reasons)})" if reasons else ""
            lines.append(f"  {name} cannot currently be deleted{why}")
    return lines
