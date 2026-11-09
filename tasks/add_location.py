#!/usr/bin/env python3
"""Add a location -- normally a market venue.

    ./shopify.sh add-location --name "Market A" --address1 "123 Main St" \\
        --city Anytown --state VA --zip 22201 --disabled --commit

POS charges sales tax against a location's address, so a venue added once with
its real address needs no address edits before each show. Add it ``--disabled``
to keep it on file without spending one of the plan's active-location slots.

New locations do **not** fulfil online orders unless ``--fulfill-online`` is
given: stock moved to a venue is meant for the venue, and quietly offering it
online would sell things that are sitting in a box at a craft fair.

**Address suggestions.** Shopify checks an address only once a location exists,
so a dry run cannot preview it. After creating the location:

  * one suggestion  -- accepted automatically (in practice a ZIP+4, or
    "Road" -> "Rd"), and both addresses are printed so you can check it;
  * several         -- none is accepted, since their order isn't guaranteed
    to be stable between calls; they are listed and the run exits 1.

Shopify's ``addressVerified`` flag is not used: on a dev store it reported a
made-up street as verified. Read the address printed at the end.
"""
from __future__ import annotations

from shopifylib import gid, locations, output
from shopifylib.errors import TaskError
from shopifylib.task import task


def add_arguments(parser):
    parser.add_argument("--name", required=True, help="location name, e.g. \"Market A\"")
    parser.add_argument("--address1", required=True, metavar="STREET")
    parser.add_argument("--address2", metavar="LINE2", help="suite, unit, etc.")
    parser.add_argument("--city", required=True)
    parser.add_argument("--state", required=True, metavar="XX",
                        help="state/province code, e.g. VA or DC")
    parser.add_argument("--zip", required=True)
    parser.add_argument("--country", default="US", metavar="XX",
                        help="country code (default: US)")
    parser.add_argument("--phone")
    parser.add_argument("--fulfill-online", action="store_true",
                        help="let stock here be sold on the online store "
                             "(off by default; venues are POS-only)")
    parser.add_argument("--disabled", action="store_true",
                        help="disable it straight after creating it, keeping it on "
                             "file without using an active-location slot")


@task(
    name="add-location",
    help="Add a location (market venue) with its address",
    writes=True,
    scopes=["read_locations", "write_locations"],
)
def run(ctx, args):
    """Add a location (market venue) with its address.

    Not online-fulfilling unless --fulfill-online. --disabled keeps it on file
    without using one of the plan's active slots.
    """
    address = build_address(args)
    known = locations.load(ctx)

    clash = [l for l in known if l["name"].strip().lower() == args.name.strip().lower()]
    if clash:
        state = "active" if clash[0]["isActive"] else "disabled"
        raise TaskError(f"A location named {clash[0]['name']!r} already exists "
                        f"({state}). Location names must be unique.")
    # locationAdd creates an *active* location, so even --disabled needs a free
    # slot for the moment between the two calls.
    locations.require_free_slot(ctx, known, doing=f"add {args.name!r}")

    online = "fulfils online orders" if args.fulfill_online else "POS only"
    then = ", then disabled" if args.disabled else ""
    payload = ctx.mutate(
        locations.ADD_MUTATION,
        {"input": {"name": args.name.strip(), "address": address,
                   "fulfillsOnlineOrders": bool(args.fulfill_online)}},
        field="locationAdd",
        describe=f"add {args.name!r} at {locations.one_line(address)} ({online}{then})")
    if not payload:
        ctx.log.info("Shopify checks the address only after the location is created, "
                     "so any suggested correction will show on the --commit run.")
        return

    created = payload["location"]
    status = _apply_suggestion(ctx, created, address)

    if args.disabled:
        result = ctx.mutate(locations.DEACTIVATE_MUTATION,
                            {"id": created["id"], "destination": None,
                             "key": locations.new_key()},
                            field="locationDeactivate", check_errors=False,
                            describe=f"disable {created['name']!r}")
        locations.raise_for(locations.payload_errors(result), {},
                            heading=f"{created['name']!r} was created but could "
                                    f"not be disabled:")

    if ctx.json_output:
        output.emit_json({"id": created["id"], "name": created["name"],
                          "address": created["address"],
                          "disabled": bool(args.disabled), "ok": status == 0})
    else:
        print(f"\nAdded {created['name']!r} (id {gid.numeric(created['id'])}), "
              f"{'disabled' if args.disabled else 'active'}, {online}.")
        print(f"  Address on file: {locations.one_line(created['address'])}")
    return status


def build_address(args) -> dict:
    address = {
        "address1": args.address1.strip(),
        "city": args.city.strip(),
        "provinceCode": args.state.strip().upper(),
        "zip": args.zip.strip(),
        "countryCode": args.country.strip().upper(),
    }
    if args.address2:
        address["address2"] = args.address2.strip()
    if args.phone:
        address["phone"] = args.phone.strip()
    return address


def _apply_suggestion(ctx, created: dict, typed: dict) -> int:
    """Accept a single suggested address, refuse to choose among several.

    Mutates ``created['address']`` to what is stored afterwards, so the caller
    reports the address actually on file. Returns the exit status.
    """
    suggestions = created.get("suggestedAddresses") or []
    if not suggestions:
        return 0

    if len(suggestions) > 1:
        ctx.log.error(
            "Shopify suggested %d different addresses for %r, so none was applied "
            "(their order isn't guaranteed, so picking one would be a guess). "
            "The address is as you typed it: %s\nSuggestions:\n%s\n"
            "Fix it in the admin, or delete-location and add it again with the "
            "address you mean.",
            len(suggestions), created["name"], locations.one_line(typed),
            "\n".join(f"  {locations.one_line(s)}" for s in suggestions))
        return 1

    [suggested] = suggestions
    fields = {k: v for k, v in suggested.items() if v is not None}
    result = ctx.mutate(locations.EDIT_ADDRESS_MUTATION,
                        {"id": created["id"], "input": {"address": fields}},
                        field="locationEdit",
                        describe=f"use Shopify's suggested address for "
                                 f"{created['name']!r}")
    created["address"] = result["location"]["address"]
    ctx.log.warning(
        "Your address %s was suggested by Shopify's address verification to be "
        "changed to: %s, and we did so. Please verify this for accuracy before "
        "using this location.",
        locations.one_line(typed), locations.one_line(created["address"]))
    return 0
