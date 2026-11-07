#!/usr/bin/env python3
"""Shared vocabulary for sale pricing: what "on sale" means here, and how to change it.

This store marks a product down by moving its current price into
``compareAtPrice`` and setting ``price`` to the sale price; Shopify then draws
the strikethrough. Taking it off sale is the reverse. Three tasks read or write
that convention (list-on-sale, go-on-sale, off-sale), and they have to agree on
the awkward case: a compare-at that is set but **not above** the price.
Shopify ignores that for display -- no strikethrough, no sale -- so it is
leftover junk rather than a markdown, and treating it as "on sale" would let
off-sale "restore" a price that was never the regular one.

Prices are per *variant*, not per product, so everything here works on
variants and the tasks act on every variant of a named product.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from . import gid, inventory, output
from .errors import TaskError

# A variant's pricing state. See the module docstring for why STALE exists.
ON_SALE = "on sale"
REGULAR = "regular"
STALE = "stale compare-at"

# Variants fetched per product. A product beyond this is refused outright: a
# reprice that silently skipped variant 101 would leave one size at full price.
VARIANTS_PER_PRODUCT = 100

VARIANT_FIELDS = """
  nodes { id displayName price compareAtPrice }
  pageInfo { hasNextPage }
"""

PRODUCTS_BY_ID_QUERY = """
query PricedProducts($ids: [ID!]!, $variants: Int!) {
  nodes(ids: $ids) {
    ... on Product {
      id
      title
      status
      variants(first: $variants) { %s }
    }
  }
}
""" % VARIANT_FIELDS

# No compare-and-swap here, unlike inventory: productVariantsBulkUpdate takes no
# expected-value argument. Absolute prices make that tolerable -- a retried
# write sets the same values again rather than applying a delta twice -- and
# the tasks read prices immediately before writing, so the window is one run.
UPDATE_MUTATION = """
mutation Reprice($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants { id price compareAtPrice }
    userErrors { field message code }
  }
}
"""


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

_CENT = Decimal("0.01")


def parse_money(value: str | None) -> Decimal | None:
    """An API money string as a Decimal; None stays None. Never float."""
    if value is None or value == "":
        return None
    return Decimal(str(value))


def format_money(value: Decimal | None) -> str:
    """Two places, as the API takes prices and as a person reads them."""
    return "" if value is None else str(value.quantize(_CENT))


def parse_price_arg(text: str) -> Decimal:
    """Validate a price given on the command line.

    Refuses sub-cent values rather than rounding them: "12.345" is more likely
    a typo than an intent, and a silently rounded price is one nobody checked.
    """
    try:
        value = Decimal(text.strip())
    except (InvalidOperation, AttributeError):
        raise TaskError(f"--price {text!r} is not a number.") from None
    if not value.is_finite() or value <= 0:
        raise TaskError(f"--price must be greater than zero (got {text}).")
    if value != value.quantize(_CENT):
        raise TaskError(f"--price {text} has more than two decimal places.")
    return value.quantize(_CENT)


def state(price: Decimal | None, compare_at: Decimal | None) -> str:
    """ON_SALE, REGULAR or STALE for one variant.

    A 0.00 compare-at counts as unset. That is defensive rather than observed:
    CSV imports are known to leave zeros in optional price columns, and a zero
    could never be a meaningful "was" price.
    """
    if compare_at is None or compare_at == 0:
        return REGULAR
    if price is not None and compare_at > price:
        return ON_SALE
    return STALE


def variant_state(variant: dict) -> str:
    return state(parse_money(variant.get("price")),
                 parse_money(variant.get("compareAtPrice")))


def label(variant: dict, product: dict | None = None) -> str:
    name = variant.get("displayName") or (product or {}).get("title") or variant["id"]
    return inventory.clean_label(name)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def load_products(ctx, ids: list[str]) -> list[dict]:
    """Product nodes with their variants' prices, in the order asked for.

    Strict, because every caller is about to write: an id that resolves to
    nothing, or a product too big to read whole, stops the run rather than
    being skipped -- a cron job that reprices four of five products and exits 0
    is worse than one that refuses.
    """
    products: list[dict] = []
    for chunk in (ids[i:i + 25] for i in range(0, len(ids), 25)):
        data = ctx.query(PRODUCTS_BY_ID_QUERY,
                         {"ids": chunk, "variants": VARIANTS_PER_PRODUCT})
        for requested, node in zip(chunk, data["nodes"]):
            if node is None:
                raise TaskError(
                    f"No such product: {gid.numeric(requested)} "
                    f"(or that id is not a product).")
            if node["variants"]["pageInfo"]["hasNextPage"]:
                raise TaskError(
                    f"{node['title']!r} has more than {VARIANTS_PER_PRODUCT} "
                    f"variants; refusing to reprice only some of them.")
            if inventory.is_archived(node.get("status")):
                ctx.log.warning("%r is archived; repricing it anyway since it "
                                "was named explicitly.", node["title"])
            products.append(node)
    return products


def product_ids(text: str, flag: str) -> list[str]:
    ids = [gid.coerce(p, "Product") for p in text.split(",") if p.strip()]
    if not ids:
        raise TaskError(f"{flag} was empty.")
    # Order-preserving dedupe: naming a product twice must not write it twice.
    return list(dict.fromkeys(ids))


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def apply(ctx, product: dict, changes: list[dict], *, describe: str) -> dict:
    """Write one product's variant prices, then check they landed.

    ``changes`` are ProductVariantsBulkInput dicts. Leaving ``compareAtPrice``
    out of one leaves it untouched; sending it as None clears it.

    The read-back compares what Shopify returned with what was sent, so a write
    that came back clean but stored something else is an error rather than a
    success line in the log.
    """
    result = ctx.mutate(UPDATE_MUTATION,
                        {"productId": product["id"], "variants": changes},
                        field="productVariantsBulkUpdate", describe=describe)
    if not result:          # dry run
        return result

    stored = {v["id"]: v for v in result.get("productVariants") or []}
    for change in changes:
        got = stored.get(change["id"])
        if got is None:
            raise TaskError(f"{product['title']!r}: Shopify did not return "
                            f"variant {gid.numeric(change['id'])} after updating it.")
        for key in ("price", "compareAtPrice"):
            if key not in change:
                continue
            want, have = parse_money(change[key]), parse_money(got.get(key))
            if want != have and not (want is None and have == 0):
                raise TaskError(
                    f"{product['title']!r}: {key} for variant "
                    f"{gid.numeric(change['id'])} is {format_money(have) or 'unset'} "
                    f"after the update, expected {format_money(want) or 'unset'}.")
    return result


def refuse(problems: list[str], *, heading: str) -> None:
    """Raise one TaskError listing every problem, so a run is all-or-nothing."""
    if problems:
        raise TaskError(heading + "\n" + "\n".join(f"  {p}" for p in problems))


def preview(ctx, rows: list[dict]) -> None:
    """The before/after table a run prints ahead of writing (or not writing)."""
    if ctx.json_output or not rows:
        return
    print(output.format_table(rows))
    print()
