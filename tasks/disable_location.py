#!/usr/bin/env python3
"""Disable (deactivate) a location after a show, keeping it on file.

    ./shopify.sh move-inventory --from "Market A" --to Home --all --commit
    ./shopify.sh disable-location "Market A" --move-stock-to Home --commit

Disabled locations don't count toward the plan's limit, and keep their address
and history for next time.

**Units must be moved out first, by us.** Shopify can relocate a location's
stock itself as part of deactivating it, but its adjustment history then shows
"Items relocated" with no creator and no reason. move-inventory records
"Movement created" by this app on both sides. So any unit *on hand* -- not just
available; damaged or otherwise unavailable units would be relocated just the
same -- makes this refuse, with the move-inventory command to run.

**Empty records still block it.** A venue emptied to zero keeps a record for
every item that was ever there, and Shopify refuses to disable it until told
where those go -- while still reporting it ``deactivatable``. So
``--move-stock-to`` is required whenever records remain. With no units left it
moves nothing; Shopify just clears the records.

The unit check is a read moments before the write. A sale or move landing in
between would be relocated by Shopify, unattributed -- a narrow window, and
the history would still show it.
"""
from __future__ import annotations

from shopifylib import locations
from shopifylib.errors import TaskError
from shopifylib.task import task

# Flat: one location's records, with both quantities the decision needs.
ON_HAND_QUERY = """
query OnHandAt($id: ID!, $first: Int!, $after: String) {
  location(id: $id) {
    inventoryLevels(first: $first, after: $after) {
      nodes {
        item { id }
        quantities(names: ["available", "on_hand"]) { name quantity }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

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
                        help="where its empty inventory records go -- required "
                             "whenever it has any, even with 0 units; usually Home. "
                             "Units must already be moved out with move-inventory")


@task(
    name="disable-location",
    help="Disable a location (e.g. after a show) once its stock is moved out",
    writes=True,
    scopes=["read_locations", "write_locations", "read_inventory"],
)
def run(ctx, args):
    """Disable a location (e.g. after a show) once its stock is moved out.

    Refuses while any unit is on hand there (move it with move-inventory, so
    the history says who moved it). Requires --move-stock-to for the empty
    records left behind. Refuses the last location fulfilling online orders.
    """
    known = locations.load(ctx)
    target = locations.resolve(known, args.location, "LOCATION")
    destination, records = check(known, target, args.move_stock_to, ctx=ctx)

    describe = f"disable {target['name']!r}"
    if destination:
        describe += (f", clearing {records} empty item record(s) "
                     f"(via {destination['name']!r})")

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


def check(known: list[dict], target: dict, move_to: str | None, *,
          ctx=None) -> tuple[dict | None, int]:
    """Validate the request; return (destination or None, empty records to clear)."""
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
        return None, 0

    stock = _stock_at(ctx, target) if ctx is not None else []
    _refuse_units(name, stock)

    if not move_to:
        raise TaskError(
            f"{name!r} holds no units, but still has inventory records for items "
            f"that were there, and Shopify won't disable it until told where those "
            f"go. Nothing will move:\n\n"
            f"  ./shopify.sh disable-location \"{name}\" --move-stock-to Home")

    destination = locations.resolve(known, move_to, "--move-stock-to")
    if destination["id"] == target["id"]:
        raise TaskError("--move-stock-to is the location being disabled.")
    if not destination["isActive"]:
        raise TaskError(f"--move-stock-to {destination['name']!r} is disabled; "
                        f"stock can only go to an active location.")
    return destination, len(stock)


def _stock_at(ctx, target: dict) -> list[dict]:
    """``[{'available': n, 'on_hand': n}, ...]``, one per item record."""
    rows = []
    for level in ctx.paginate(ON_HAND_QUERY, "location.inventoryLevels",
                              variables={"id": target["id"]}, page_size=250):
        quantities = {q["name"]: int(q["quantity"] or 0)
                      for q in level.get("quantities") or []}
        rows.append({"available": quantities.get("available", 0),
                     "on_hand": quantities.get("on_hand", 0)})
    return rows


def _refuse_units(name: str, stock: list[dict]) -> None:
    """Refuse while anything is on hand, naming the command that moves it."""
    on_hand = sum(r["on_hand"] for r in stock)
    if not on_hand:
        return
    items = sum(1 for r in stock if r["on_hand"])
    stuck = on_hand - sum(min(r["available"], r["on_hand"]) for r in stock)
    message = (
        f"{name!r} still has {on_hand} unit(s) on hand across {items} item(s). Move "
        f"them out first, so the history shows who moved them and why (Shopify's "
        f"own relocation logs them as 'Items relocated' with no attribution):\n\n"
        f"  ./shopify.sh move-inventory --from \"{name}\" --to Home --all --commit\n\n"
        f"then re-run this.")
    if stuck:
        message += (f"\n\n{stuck} of those unit(s) are on hand but not available "
                    f"(e.g. damaged or reserved), which move-inventory does not move. "
                    f"Adjust them in the admin first.")
    raise TaskError(message)
