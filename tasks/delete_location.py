#!/usr/bin/env python3
"""Delete a location for good.

    ./shopify.sh delete-location "Market Q" --commit

Cannot be undone. For a venue you might return to, disable it instead: a
disabled location costs nothing against the plan's limit and keeps its address.

Two deliberate frictions:

  * The location must already be disabled (Shopify requires this too), so a
    delete is always the second of two separate commands.
  * It must be named exactly -- full name or id. Elsewhere "Market" may match
    "Market Q" as a convenience; here that would be a hazard.
"""
from __future__ import annotations

from shopifylib import gid, locations
from shopifylib.errors import TaskError
from shopifylib.task import task

EXPLANATIONS = {
    "LOCATION_IS_ACTIVE": "It is still active; disable it first with "
                          "disable-location.",
    "LOCATION_HAS_INVENTORY": "It still has inventory.",
    "LOCATION_HAS_PENDING_ORDERS": "It has pending orders.",
}


def add_arguments(parser):
    parser.add_argument("location", metavar="LOCATION",
                        help="exact location name, or its id")


@task(
    name="delete-location",
    help="Permanently delete a disabled location",
    writes=True,
    scopes=["read_locations", "write_locations"],
)
def run(ctx, args):
    """Permanently delete a disabled location.

    The location must be disabled first and named exactly. Prefer leaving a
    venue disabled if you may go back there.
    """
    known = locations.load(ctx)
    target = locations.resolve(known, args.location, "LOCATION", partial=False)
    check(known, target)

    result = ctx.mutate(locations.DELETE_MUTATION, {"id": target["id"]},
                        field="locationDelete", check_errors=False,
                        describe=f"PERMANENTLY delete {target['name']!r} "
                                 f"(id {gid.numeric(target['id'])}, "
                                 f"{locations.one_line(target.get('address'))})")
    if not result:
        return
    locations.raise_for(locations.payload_errors(result), EXPLANATIONS,
                        heading=f"Could not delete {target['name']!r}:")
    ctx.log.info("Deleted %r.", target["name"])


def check(known: list[dict], target: dict) -> None:
    name = target["name"]
    if locations.is_last_online(known, target):
        raise TaskError(f"{name!r} is the only location fulfilling online orders.")
    if target["isActive"]:
        raise TaskError(f"{name!r} is still active. Disable it first:\n\n"
                        f"  ./shopify.sh disable-location \"{name}\" "
                        f"--move-stock-to Home --commit")
    if target.get("hasActiveInventory"):
        raise TaskError(f"{name!r} still has inventory.")
    if target.get("hasUnfulfilledOrders"):
        raise TaskError(f"{name!r} has unfulfilled orders.")
