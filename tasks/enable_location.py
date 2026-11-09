#!/usr/bin/env python3
"""Enable (reactivate) a disabled location, ahead of a show there.

    ./shopify.sh enable-location "Market A" --commit

A re-enabled location comes back **empty** -- disabling removed its inventory
records -- so stock goes in afterwards with move-inventory, which sets items up
at the destination as it moves them.
"""
from __future__ import annotations

from shopifylib import locations
from shopifylib.errors import TaskError
from shopifylib.task import task

EXPLANATIONS = {
    "LOCATION_LIMIT": "The plan's active-location limit is reached; disable another "
                      "location first.",
    "HAS_NON_UNIQUE_NAME": "An active location already has this name; rename one of "
                           "them in the admin.",
    "HAS_ONGOING_RELOCATION": "Shopify is still moving this location's stock from "
                              "an earlier change; try again shortly.",
}


def add_arguments(parser):
    parser.add_argument("location", metavar="LOCATION", help="location name or id")


@task(
    name="enable-location",
    help="Enable a disabled location (e.g. before a show)",
    writes=True,
    scopes=["read_locations", "write_locations"],
)
def run(ctx, args):
    """Enable a disabled location (e.g. before a show).

    Refuses when the active locations already fill the plan's limit, and lists
    them so you can pick one to disable.
    """
    known = locations.load(ctx)
    target = locations.resolve(known, args.location, "LOCATION")
    if target["isActive"]:
        raise TaskError(f"{target['name']!r} is already active.")
    locations.require_free_slot(ctx, known, doing=f"enable {target['name']!r}")

    result = ctx.mutate(locations.ACTIVATE_MUTATION,
                        {"id": target["id"], "key": locations.new_key()},
                        field="locationActivate", check_errors=False,
                        describe=f"enable {target['name']!r} "
                                 f"({locations.one_line(target.get('address'))})")
    if not result:
        return
    locations.raise_for(locations.payload_errors(result), EXPLANATIONS,
                        heading=f"Could not enable {target['name']!r}:")

    print(f"Enabled {target['name']!r}. Next:")
    print(f"  * switch the POS device to {target['name']!r}")
    print(f"  * move stock in:  ./shopify.sh move-inventory --from Home "
          f"--to \"{target['name']}\" --pids ... --commit")
