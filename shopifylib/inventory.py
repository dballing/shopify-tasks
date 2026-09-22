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

# The quantity state these tasks care about: what is sellable. on_hand includes
# units committed to unfulfilled orders, which are promised to whichever
# location will ship them and so must not be moved.
AVAILABLE = "available"

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
