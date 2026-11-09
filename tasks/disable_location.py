#!/usr/bin/env python3
"""Disable (deactivate) a location after a show, keeping it on file.

    ./shopify.sh disable-location "Market A" --move-stock-to Home --commit

Disabled locations don't count toward the plan's limit, and keep their address
and history for next time.

**Any inventory record blocks this, not only units.** A venue emptied to zero
still has a record for every item that was ever there, and Shopify refuses to
disable it until told where that inventory goes -- even though it reports the
location as ``deactivatable``. So ``--move-stock-to`` is required whenever the
location has inventory records, units or not. Shopify then moves the units and
removes the records in the same call; nothing is left half-done.
"""
from __future__ import annotations

from shopifylib import inventory, locations
from shopifylib.errors import TaskError
from shopifylib.task import task

EXPLANATIONS = {
    "HAS_ACTIVE_INVENTORY_ERROR": "It still has inventory records; re-run with "
                                  "--move-stock-to Home.",
    "HAS_FULFILLMENT_ORDERS_ERROR": "It has unfulfilled orders (often a POS sale not "
                                    "yet marked fulfilled). Fulfil them first.",
    "HAS_ACTIVE_TRANSFERS_ERROR": "It has inventory transfers in progress. Finish or "
                                  "cancel them first.",
    "HAS_INCOMING_MOVEMENTS_ERROR": "Stock is on its way to it. Receive or cancel "
                                    "that first.",
    "HAS_OPEN_PURCHASE_ORDERS_ERROR": "It has open purchase orders.",
    "CANNOT_DISABLE_ONLINE_ORDER_FULFILLMENT": "It is the only location fulfilling "
                                               "online orders.",
    "DESTINATION_LOCATION_NOT_FOUND_OR_INACTIVE": "The --move-stock-to location is "
                                                  "missing or disabled.",
    "TEMPORARILY_BLOCKED_FROM_DEACTIVATION_ERROR": "Shopify is temporarily blocking "
                                                   "this; try again shortly.",
}


def add_arguments(parser):
    parser.add_argument("location", metavar="LOCATION", help="location name or id")
    parser.add_argument("--move-stock-to", metavar="LOC",
                        help="where its inventory goes (required if it has any "
                             "inventory records, even at 0 units); usually Home")


@task(
    name="disable-location",
    help="Disable a location (e.g. after a show), moving its stock out",
    writes=True,
    scopes=["read_locations", "write_locations", "read_inventory"],
)
def run(ctx, args):
    """Disable a location (e.g. after a show), moving its stock out.

    Requires --move-stock-to whenever the location has inventory records, even
    at zero units. Refuses the last location fulfilling online orders.
    """
    known = locations.load(ctx)
    target = locations.resolve(known, args.location, "LOCATION")
    destination = check(known, target, args.move_stock_to, ctx=ctx)

    describe = f"disable {target['name']!r}"
    if destination:
        units = _units_at(ctx, target)
        describe += (f", moving {units['items']} item record(s) / {units['units']} "
                     f"unit(s) to {destination['name']!r}")

    result = ctx.mutate(locations.DEACTIVATE_MUTATION,
                        {"id": target["id"],
                         "destination": destination["id"] if destination else None,
                         "key": locations.new_key()},
                        field="locationDeactivate", check_errors=False,
                        describe=describe)
    if not result:
        return
    locations.raise_for(locations.payload_errors(result), EXPLANATIONS,
                        heading=f"Could not disable {target['name']!r}:")
    ctx.log.info("Done: %s.", describe)


def check(known: list[dict], target: dict, move_to: str | None, *, ctx=None) -> dict | None:
    """Validate the request; return the destination location, or None."""
    name = target["name"]
    if not target["isActive"]:
        raise TaskError(f"{name!r} is already disabled.")
    if locations.is_last_online(known, target):
        raise TaskError(f"{name!r} is the only location fulfilling online orders; "
                        f"disabling it would leave the online store nowhere to ship "
                        f"from.")
    if target.get("hasUnfulfilledOrders"):
        raise TaskError(f"{name!r} has unfulfilled orders -- often a POS sale not yet "
                        f"marked fulfilled. Fulfil them, then re-run.")

    if not target.get("hasActiveInventory"):
        if move_to and ctx is not None:
            ctx.log.info("%r has no inventory records; --move-stock-to not needed.", name)
        return None

    if not move_to:
        units = _units_at(ctx, target) if ctx is not None else None
        held = (f"holds {units['units']} unit(s) across {units['items']} item(s)"
                if units and units["units"] else
                "holds 0 units, but still has inventory records for items that were "
                "there, which Shopify counts as inventory")
        raise TaskError(
            f"{name!r} {held}. Say where they go:\n\n"
            f"  ./shopify.sh disable-location \"{name}\" --move-stock-to Home")

    destination = locations.resolve(known, move_to, "--move-stock-to")
    if destination["id"] == target["id"]:
        raise TaskError("--move-stock-to is the location being disabled.")
    if not destination["isActive"]:
        raise TaskError(f"--move-stock-to {destination['name']!r} is disabled; "
                        f"stock can only go to an active location.")
    return destination


def _units_at(ctx, target: dict) -> dict:
    stock = inventory.read_levels(ctx.client, [target])
    return {"items": len(stock),
            "units": sum(q.get(target["name"], 0) for q in stock.values())}
