#!/usr/bin/env python3
"""List everything currently marked down.

    ./shopify.sh list-on-sale
    ./shopify.sh list-on-sale --include-archived
    ./shopify.sh list-on-sale --json | jq ...

Read-only. "On sale" means the store's markdown convention: ``compareAtPrice``
holds the regular price and is above ``price``, the sale price. One row per
variant, since prices live on variants; a single-variant product reads as just
its name.

A compare-at at or below the price is not a sale -- Shopify draws no
strikethrough for it -- but those variants are listed anyway with their sale
price marked ``*``, as leftovers worth cleaning up. The ``pid`` column is the
numeric id go-on-sale and off-sale take.
"""
from __future__ import annotations

from shopifylib import gid, inventory, output, pricing
from shopifylib.task import task

PRODUCT_PAGE = 50

# Appended to the sale price of a variant whose compare-at is not above its
# price -- listed so it can be found, flagged because it is not really a sale.
STALE_MARKER = "*"

SCAN_QUERY = """
query ListOnSale($first: Int!, $after: String, $variants: Int!, $query: String) {
  products(first: $first, after: $after, query: $query) {
    nodes {
      id
      title
      status
      variants(first: $variants) { %s }
    }
    pageInfo { hasNextPage endCursor }
  }
}
""" % pricing.VARIANT_FIELDS


def add_arguments(parser):
    parser.add_argument("--include-archived", action="store_true",
                        help="also scan archived products (hidden by default)")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="stop after N products")


@task(
    name="list-on-sale",
    help="List variants marked down via compare-at price",
    scopes=["read_products"],
)
def run(ctx, args):
    """List variants marked down via compare-at price.

    Read-only. A variant is on sale when its compare-at price is set and above
    its price; the table shows the regular (compare-at) and sale prices.
    """
    on_sale, stale = _scan(ctx, limit=args.limit,
                           include_archived=args.include_archived)
    on_sale.sort(key=lambda r: r["product"].lower())
    stale.sort(key=lambda r: r["product"].lower())

    if ctx.json_output:
        output.emit_json({"on_sale": on_sale, "stale": stale})
        return

    if not on_sale and not stale:
        print("(nothing on sale)")
        return

    # Stale rows are shown inline rather than hidden: they are exactly the ones
    # that need a human to look, and a separate count sent you to --json to
    # find out which they were.
    rows = sorted([(r, "") for r in on_sale] + [(r, STALE_MARKER) for r in stale],
                  key=lambda pair: pair[0]["product"].lower())
    print(output.format_table([
        # The bare numeric id, since that is what --pid/--pids and the admin
        # URL take; the full GID is in --json.
        {"pid": gid.numeric(r["product_id"]), "product": r["product"],
         "regular price": r["regular_price"],
         "sale price": r["sale_price"] + marker} for r, marker in rows]))
    print(f"\n{len(on_sale)} variant(s) on sale")
    if stale:
        print(f"{len(stale)} variant(s) marked {STALE_MARKER}: compare-at at or below "
              f"the price, which Shopify does not show as a sale.\n"
              f"  go-on-sale overwrites these; off-sale refuses them.")


def _scan(ctx, *, limit: int | None, include_archived: bool) -> tuple[list, list]:
    search = None if include_archived else inventory.EXCLUDE_ARCHIVED_QUERY
    progress = output.Progress("reading products")
    on_sale, stale = [], []
    for node in ctx.paginate(SCAN_QUERY, "products",
                             variables={"variants": pricing.VARIANTS_PER_PRODUCT,
                                        "query": search},
                             page_size=PRODUCT_PAGE, max_items=limit):
        progress.tick()
        on, off = _classify(ctx, node)
        on_sale.extend(on)
        stale.extend(off)
    progress.done()
    return on_sale, stale


def _classify(ctx, node: dict) -> tuple[list[dict], list[dict]]:
    """(on-sale rows, stale rows) for one product node."""
    variants = node["variants"]
    if variants["pageInfo"]["hasNextPage"]:
        ctx.log.warning("%r has more than %d variants; only the first %d were checked.",
                        node["title"], pricing.VARIANTS_PER_PRODUCT,
                        pricing.VARIANTS_PER_PRODUCT)
    on, off = [], []
    for variant in variants["nodes"]:
        current = pricing.variant_state(variant)
        if current == pricing.REGULAR:
            continue
        row = {
            "product_id": node["id"],
            "variant_id": variant["id"],
            "product": pricing.label(variant, node),
            "regular_price": pricing.format_money(
                pricing.parse_money(variant["compareAtPrice"])),
            "sale_price": pricing.format_money(pricing.parse_money(variant["price"])),
        }
        (on if current == pricing.ON_SALE else off).append(row)
    return on, off
