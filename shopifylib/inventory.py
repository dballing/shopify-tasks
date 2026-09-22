#!/usr/bin/env python3
"""Shared vocabulary for talking about stock across locations.

Three tasks now render the same idea -- how much of a thing sits where -- and
they have to agree on the awkward bit: a location where an item holds nothing
is not the same as a location where the item was never activated. Both read as
"no stock there" in English and both would print as ``0``, but only the second
needs an ``inventoryActivate`` before anything can be written. Conflating them
is what let the activation path go untested for a while, so the distinction
lives here rather than being re-derived per task.

The shape everything agrees on is a list of per-variant
``{location name: quantity}`` maps, where **a location absent from the map
means the variant is not activated there**.
"""
from __future__ import annotations

from .paging import paginate

# The quantity state these tasks care about: what is sellable. on_hand includes
# units committed to unfulfilled orders, which are promised to whichever
# location will ship them and so must not be moved.
AVAILABLE = "available"

# Archiving a product is how a merchant puts it out of sight, so tasks exclude
# archived products unless asked otherwise. They are not inert, though: an
# archived product keeps its inventory levels and still appears in a location's
# inventoryLevels connection, so a location-first scan sees it and would move
# its stock. Filtering is a deliberate act, not something Shopify does for you.
ARCHIVED = "ARCHIVED"

# Server-side filter for a products(query:) argument.
EXCLUDE_ARCHIVED_QUERY = "-status:archived"


def is_archived(status: str | None) -> bool:
    return (status or "").upper() == ARCHIVED


def status_label(status: str | None) -> str:
    """How a product status prints in a table.

    Blank for ACTIVE: the overwhelming majority of rows are active, and
    labelling every one of them would be noise that buries the few that differ.
    """
    text = (status or "").upper()
    return "" if text in ("", "ACTIVE") else text.lower()


# Printed where an item is not stocked at a location at all.
NOT_ACTIVATED = "--"

# Appended when a product's variants disagree about a location -- some stocked
# there, some not. Worth flagging because a move of that product does two
# different things depending on the variant.
PARTIAL_MARKER = "*"

# Shopify's option name and value for a product with no real options. Not a
# placeholder to tidy away: productSet requires an option whenever variants are
# supplied, so this is the real structure of a single-variant product.
DEFAULT_OPTION = "Title"
DEFAULT_OPTION_VALUE = "Default Title"
_DEFAULT_SUFFIX = f" - {DEFAULT_OPTION_VALUE}"


def clean_label(name: str) -> str:
    """Drop Shopify's placeholder variant suffix from a display name.

    A product with no real options still has one variant, named "Default
    Title", so displayName comes back as "Blue Bead - Default Title". In a store
    where most products are single-variant that suffix is on nearly every row,
    pushing the part that identifies the item off the edge of the table.
    """
    return name.removesuffix(_DEFAULT_SUFFIX).strip() or name


def available_at(level: dict | None) -> int:
    """The ``available`` figure from an InventoryLevel's quantities list.

    Returns 0 for a missing level, which is correct arithmetically but does not
    mean the same thing -- use ``level is None`` to test activation.
    """
    if not level:
        return 0
    for entry in level.get("quantities") or []:
        if entry.get("name") == AVAILABLE:
            return int(entry.get("quantity") or 0)
    return 0


def location_cell(variant_quantities: list[dict[str, int]], location: str) -> str:
    """One location's column for a product, distinguishing empty from absent.

        6     stocked, six units
        0     stocked, empty
        --    not activated here at all
        4*    stocked, but some variants are not activated here

    ``variant_quantities`` is one map per variant; a location missing from a
    map means that variant is not activated there.
    """
    present = [q for q in variant_quantities if location in q]
    if not present:
        return NOT_ACTIVATED
    total = sum(q[location] for q in present)
    partial = PARTIAL_MARKER if len(present) != len(variant_quantities) else ""
    return f"{total}{partial}"


def needing_activation(variant_quantities: list[dict[str, int]], location: str) -> int:
    """How many variants would need activating before a write at ``location``."""
    return sum(1 for q in variant_quantities if location not in q)


def legend(*, indent: str = "  ") -> list[str]:
    """The two lines explaining the markers, so every table says the same thing."""
    return [
        f"{indent}{NOT_ACTIVATED} = not activated at that location "
        f"(a move must activate it first)",
        f"{indent} {PARTIAL_MARKER} = only some variants activated there",
    ]


# ---------------------------------------------------------------------------
# Reading stock
# ---------------------------------------------------------------------------

# Flat: every item stocked at ONE location. Deliberately not nested inside a
# products query -- `products -> variants -> inventoryLevels` multiplies the
# connection costs and blows the 1000-point per-query cap that applies on every
# plan. Two tasks hit that (list-inventory at 1007 points, seed-dev's clone at
# 1112), which is why this lives here instead of in either of them.
LEVELS_QUERY = """
query LevelsAt($id: ID!, $first: Int!, $after: String) {
  location(id: $id) {
    inventoryLevels(first: $first, after: $after) {
      nodes {
        item { id }
        quantities(names: ["available"]) { name quantity }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

# A location holds one row per stocked item, so this can be large; it is also a
# cheap connection, so a big page is fine.
LEVEL_PAGE = 250


def read_levels(client, locations: list[dict], *, page_size: int = LEVEL_PAGE,
                progress_factory=None) -> dict[str, dict[str, int]]:
    """``{inventory item id: {location name: available}}`` for some locations.

    One flat pass per location, joined to products by the caller. Takes a
    *client* rather than a TaskContext because a task that copies between
    stores reads its source through :meth:`TaskContext.reader_for`, where a
    context is not available.

    **A location absent from an item's inner map means the item is not
    activated there.** That must keep coming from a row's absence rather than a
    zero -- it is the distinction the whole 0/-- vocabulary rests on.

    ``progress_factory`` takes a label and returns something with ``tick()`` and
    ``done()``; omit it to read silently.
    """
    stock: dict[str, dict[str, int]] = {}
    for location in locations:
        progress = progress_factory(f"reading {location['name']}") if progress_factory else None
        for level in paginate(client, LEVELS_QUERY, "location.inventoryLevels",
                              variables={"id": location["id"]}, page_size=page_size):
            if progress:
                progress.tick()
            item_id = (level.get("item") or {}).get("id")
            if item_id:
                stock.setdefault(item_id, {})[location["name"]] = available_at(level)
        if progress:
            progress.done()
    return stock
