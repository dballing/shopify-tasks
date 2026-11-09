#!/usr/bin/env python3
"""Shared vocabulary for locations: loading them, naming them, and guarding them.

The store has one real location (Home, which fulfils online orders) and a set
of market venues used through POS. Venues are kept *disabled* between shows --
Shopify's "deactivated" -- because only active locations count toward the
plan's location limit. Several tasks need to find a location by the name a
person types, and the destructive ones need to agree on what must never be
touched, so both live here rather than being re-derived per task.
"""
from __future__ import annotations

import uuid

from . import gid
from .errors import TaskError

# What a person types and Shopify stores. Deliberately excludes addressVerified:
# on a dev store it reported "99999 Nowhere Road" as verified within seconds, so
# it is not shown or trusted anywhere.
ADDRESS_FIELDS = "address1 address2 city provinceCode zip countryCode"

# Everything any location task reads, in one flat query: no nested connections,
# so it is cheap at any number of locations. includeInactive because disabled
# venues are the normal state here, not an edge case.
LOCATIONS_QUERY = """
query Locations {
  locations(first: 250, includeInactive: true) {
    nodes {
      id
      name
      isActive
      fulfillsOnlineOrders
      hasActiveInventory
      hasUnfulfilledOrders
      deactivatable
      deletable
      address { %s }
    }
  }
}
""" % ADDRESS_FIELDS

# The plan's limit, read live. Published per-plan figures have been wrong here
# before (see the throttle notes in CLAUDE.md), so never hard-code it.
LIMIT_QUERY = "query LocationLimit { shop { resourceLimits { locationLimit } } }"


def load(ctx) -> list[dict]:
    return ctx.query(LOCATIONS_QUERY)["locations"]["nodes"]


def location_limit(ctx) -> int:
    return int(ctx.query(LIMIT_QUERY)["shop"]["resourceLimits"]["locationLimit"])


def active(locations: list[dict]) -> list[dict]:
    return [l for l in locations if l.get("isActive")]


def sort_key(location: dict) -> tuple:
    """Active first, then by name, so output order doesn't wander between runs."""
    return (not location.get("isActive"), location["name"].lower())


def resolve(locations: list[dict], value: str, flag: str, *,
            partial: bool = True) -> dict:
    """Accept a location name, numeric id, or GID.

    Names are the useful form -- they are what the admin shows and what a
    person remembers -- so match those case-insensitively, then fall back to
    ids, then (when ``partial``) a unique substring, so "Craft Fair" finds
    "Craft Fair Inventory".

    ``partial=False`` is for tasks that destroy things. "Market" uniquely
    matching "Market B" is a convenience when moving stock and an accident
    when deleting a location.
    """
    wanted = value.strip()

    exact = [l for l in locations if l["name"].lower() == wanted.lower()]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise TaskError(
            f"{flag}: more than one location is named {wanted!r}. Use its id instead:\n"
            + "\n".join(f"  {gid.numeric(l['id'])}  {l['name']}" for l in exact)
        )

    if gid.is_gid(wanted) or wanted.isdigit():
        target = gid.coerce(wanted, "Location")
        for loc in locations:
            if loc["id"] == target:
                return loc

    if partial:
        matches = [l for l in locations if wanted.lower() in l["name"].lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise TaskError(
                f"{flag}: {wanted!r} matches more than one location:\n"
                + "\n".join(f"  {l['name']}" for l in matches)
            )

    hint = "" if partial else " (give the full name or id)"
    raise TaskError(
        f"{flag}: no location matches {wanted!r}{hint}. Configured locations:\n"
        + "\n".join(
            f"  {l['name']}" + ("" if l.get("isActive", True) else "  (disabled)")
            for l in locations
        )
    )


def is_last_online(locations: list[dict], location: dict) -> bool:
    """True when ``location`` is the only active one fulfilling online orders.

    That is Home. Disabling or deleting it would leave the online store with
    nowhere to ship from, so tasks refuse it outright rather than leaning on
    Shopify's own CANNOT_DISABLE_ONLINE_ORDER_FULFILLMENT, whose wording does
    not say which location is the problem.
    """
    if not (location.get("isActive") and location.get("fulfillsOnlineOrders")):
        return False
    return not any(l["id"] != location["id"] and l.get("isActive")
                   and l.get("fulfillsOnlineOrders") for l in locations)


def one_line(address: dict | None) -> str:
    """'123 Main St, Suite 4, Anytown VA 22201' -- for tables and messages."""
    address = address or {}
    street = ", ".join(p for p in (address.get("address1"), address.get("address2")) if p)
    region = " ".join(p for p in (address.get("city"), address.get("provinceCode"),
                                  address.get("zip")) if p)
    country = address.get("countryCode")
    parts = [p for p in (street, region) if p]
    if country and country != "US":
        parts.append(country)
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

# suggestedAddresses is Shopify's address check: one entry is a correction it
# is confident of (in practice a ZIP+4 or "Road" -> "Rd"). It was populated in
# the locationAdd response itself during testing, not later.
ADD_MUTATION = """
mutation AddLocation($input: LocationAddInput!) {
  locationAdd(input: $input) {
    location { id name address { %s } suggestedAddresses { %s } }
    userErrors { field message code }
  }
}
""" % (ADDRESS_FIELDS, ADDRESS_FIELDS)

EDIT_ADDRESS_MUTATION = """
mutation EditLocationAddress($id: ID!, $input: LocationEditInput!) {
  locationEdit(id: $id, input: $input) {
    location { id address { %s } }
    userErrors { field message code }
  }
}
""" % ADDRESS_FIELDS

# @idempotent is required on activate and deactivate -- verified live: without
# it both fail at runtime with "The @idempotent directive is required for this
# mutation but was not provided", and the schema does not mark it. Same trap as
# inventoryActivate. locationDelete does not need it (also verified).
ACTIVATE_MUTATION = """
mutation EnableLocation($id: ID!, $key: String!) {
  locationActivate(locationId: $id) @idempotent(key: $key) {
    location { id isActive }
    locationActivateUserErrors { field message code }
  }
}
"""

# destinationLocationId is not only for moving units. Any inventory *record*
# at the location blocks deactivation -- including items sitting at 0 -- with
# HAS_ACTIVE_INVENTORY_ERROR. With a destination, Shopify moves the units and
# removes those records synchronously, in this one call (verified: units were
# at the destination by the first read afterwards).
DEACTIVATE_MUTATION = """
mutation DisableLocation($id: ID!, $destination: ID, $key: String!) {
  locationDeactivate(locationId: $id, destinationLocationId: $destination)
    @idempotent(key: $key) {
    location { id isActive }
    locationDeactivateUserErrors { field message code }
  }
}
"""

DELETE_MUTATION = """
mutation DeleteLocation($id: ID!) {
  locationDelete(locationId: $id) {
    deletedLocationId
    locationDeleteUserErrors { field message code }
  }
}
"""


def require_free_slot(ctx, known: list[dict], *, doing: str) -> None:
    """Refuse when the active locations already fill the plan's limit.

    Lists the active ones, since the fix is to disable one of them -- and which
    one is a person's decision, not something this should guess.
    """
    current = active(known)
    limit = location_limit(ctx)
    if len(current) >= limit:
        raise TaskError(
            f"Cannot {doing}: {len(current)} of {limit} locations are already "
            f"active. Disable one first:\n"
            + "\n".join(f"  {l['name']}" for l in sorted(current, key=sort_key)))


def raise_for(errors: list[dict], explanations: dict[str, str], *, heading: str) -> None:
    """Turn userErrors into one TaskError, explained by code where possible.

    Keyed on the code rather than the message because Shopify's prose names the
    mechanism ("without specifying where to relocate inventory") rather than
    what the person should do about it.
    """
    if not errors:
        return
    lines = []
    for err in errors:
        lines.append(explanations.get(err.get("code"))
                     or err.get("message") or str(err))
    raise TaskError(heading + "\n" + "\n".join(f"  {line}" for line in lines))


def payload_errors(payload: dict) -> list[dict]:
    """The userErrors list, whatever this mutation happens to call it."""
    for key, value in (payload or {}).items():
        if key.endswith("serErrors") and value:
            return value
    return []


def new_key() -> str:
    """A fresh @idempotent key. One per attempt: a reused key replays the
    first call's result instead of performing the second."""
    return str(uuid.uuid4())
