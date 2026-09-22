#!/usr/bin/env python3
"""Shopify global ID (GID) helpers.

The GraphQL API identifies everything by an opaque-looking but entirely regular
string::

    gid://shopify/ProductVariant/43219876543210

The admin UI, CSV exports, and the legacy REST API all speak the bare numeric id
instead. Every task that takes ids from a spreadsheet or a browser URL therefore
has to convert, and doing it with f-strings scattered through task code is how
you end up passing a Product gid to a variant argument and getting a userError
that says nothing useful.
"""
from __future__ import annotations

import re

_GID_RE = re.compile(r"^gid://shopify/(?P<type>[A-Za-z][A-Za-z0-9_]*)/(?P<id>[^/?]+)(?:\?.*)?$")


class GidError(ValueError):
    """A string is not a usable GID, or is the wrong resource type."""


def build(resource: str, numeric_id: str | int) -> str:
    """Make a GID from a resource type and a numeric id.

    >>> build("Product", 123)
    'gid://shopify/Product/123'
    """
    ident = str(numeric_id).strip()
    if not ident:
        raise GidError(f"Empty id for {resource} GID")
    return f"gid://shopify/{resource}/{ident}"


def parse(gid: str) -> tuple[str, str]:
    """Split a GID into (resource_type, numeric_id)."""
    match = _GID_RE.match((gid or "").strip())
    if not match:
        raise GidError(
            f"{gid!r} is not a Shopify GID. Expected the form "
            f"'gid://shopify/Product/123'."
        )
    return match.group("type"), match.group("id")


def numeric(gid: str) -> str:
    """The trailing numeric id, for CSV output or an admin URL."""
    return parse(gid)[1]


def resource_type(gid: str) -> str:
    """The resource type, e.g. ``'ProductVariant'``."""
    return parse(gid)[0]


def is_gid(value: str) -> bool:
    return bool(_GID_RE.match((value or "").strip()))


def coerce(value: str | int, resource: str) -> str:
    """Accept whatever the user pasted and return a GID of the expected type.

    Handles the three things that realistically arrive from a spreadsheet, a
    browser URL bar, or a colleague's message:

      * a bare numeric id            -> built into a GID
      * an already-correct GID       -> returned unchanged
      * an admin URL                 -> trailing id extracted

    A GID of the *wrong* resource type is rejected rather than rebuilt, because
    a Product id and a ProductVariant id are both plausible-looking numbers and
    silently reinterpreting one as the other would target the wrong record.

    >>> coerce("123", "Product")
    'gid://shopify/Product/123'
    >>> coerce("https://admin.shopify.com/store/acme/products/123", "Product")
    'gid://shopify/Product/123'
    """
    text = str(value).strip()
    if not text:
        raise GidError(f"Empty value where a {resource} id was expected")

    if text.startswith("gid://"):
        found_type, ident = parse(text)
        if found_type != resource:
            raise GidError(
                f"Expected a {resource} id but got a {found_type} id ({text}). "
                f"These look alike and are not interchangeable."
            )
        return build(resource, ident)

    if text.isdigit():
        return build(resource, text)

    # An admin URL: .../products/123 or .../products/123/variants/456
    tail = text.rstrip("/").split("?")[0].split("/")[-1]
    if tail.isdigit():
        return build(resource, tail)

    raise GidError(
        f"Cannot read a {resource} id from {text!r}. Give a numeric id, a "
        f"gid://shopify/{resource}/... GID, or an admin URL ending in the id."
    )


def admin_url(gid: str, store_handle: str) -> str:
    """A clickable admin URL for a GID, for human-readable task output.

    ``store_handle`` is the part before '.myshopify.com'. Only the common
    resource types are mapped; anything else falls back to a search-free path
    that still lands somewhere sensible.
    """
    resource, ident = parse(gid)
    segment = {
        "Product": "products",
        "ProductVariant": "products/variants",
        "Order": "orders",
        "Customer": "customers",
        "DraftOrder": "draft_orders",
        "Collection": "collections",
        "InventoryItem": "products/inventory",
        "Location": "settings/locations",
    }.get(resource)
    if not segment:
        return f"https://admin.shopify.com/store/{store_handle}"
    return f"https://admin.shopify.com/store/{store_handle}/{segment}/{ident}"
