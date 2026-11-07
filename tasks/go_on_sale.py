#!/usr/bin/env python3
"""Mark a product down.

    ./shopify.sh go-on-sale --pid 123 --price 15.00
    ./shopify.sh go-on-sale --pid 123 --price 15.00 --commit
    ./shopify.sh go-on-sale --pid 123 --price 12.00 --deep-discount --commit

Moves each variant's current price into ``compareAtPrice`` and sets ``price``
to ``--price``, which is how this store marks a product down. Every variant of
the product gets the same sale price.

A product already on sale is refused: going on sale twice would copy the sale
price into compare-at and lose the regular price for good. ``--deep-discount``
is the way to change the sale price of something already marked down -- it
leaves compare-at (the regular price) alone and only moves ``price``.

Every variant is checked before anything is written, so a product either fully
goes on sale or is left exactly as it was.
"""
from __future__ import annotations

from shopifylib import gid, output, pricing
from shopifylib.task import task


def add_arguments(parser):
    parser.add_argument("--pid", required=True, metavar="ID",
                        help="product id (numeric, GID, or admin URL)")
    parser.add_argument("--price", required=True, metavar="AMOUNT",
                        help="the sale price, e.g. 15.00")
    parser.add_argument("--deep-discount", action="store_true",
                        help="the product is already on sale: change the sale price "
                             "and leave compare-at (the regular price) alone")


@task(
    name="go-on-sale",
    help="Mark a product down: compare-at <- price, price <- --price",
    writes=True,
    scopes=["read_products", "write_products"],
)
def run(ctx, args):
    """Mark a product down: compare-at <- price, price <- --price.

    Refuses a product already on sale unless --deep-discount is given, which
    changes only the sale price.
    """
    new_price = pricing.parse_price_arg(args.price)
    [product] = pricing.load_products(ctx, [gid.coerce(args.pid, "Product")])

    changes, rows = plan(product, new_price, deep=args.deep_discount)
    pricing.preview(ctx, rows)

    if args.deep_discount:
        action, before = f"deepen the discount on {product['title']!r}", "sale now"
    else:
        action, before = f"put {product['title']!r} on sale", "regular"
    pricing.apply(ctx, product, changes,
                  describe=f"{action}: " + ", ".join(
                      f"{r[before]} -> {r['sale after']}" for r in rows))

    if ctx.json_output:
        output.emit_json({"product_id": product["id"], "title": product["title"],
                          "dry_run": ctx.dry_run, "variants": rows})


def plan(product: dict, new_price, *, deep: bool) -> tuple[list[dict], list[dict]]:
    """(mutation inputs, preview rows), or a TaskError naming every bad variant."""
    changes, rows, problems = [], [], []
    for variant in product["variants"]["nodes"]:
        name = pricing.label(variant, product)
        price = pricing.parse_money(variant["price"])
        compare_at = pricing.parse_money(variant["compareAtPrice"])
        current = pricing.state(price, compare_at)
        shown = pricing.format_money

        if deep:
            if current != pricing.ON_SALE:
                problems.append(f"{name}: not on sale ({current}); drop --deep-discount")
                continue
            if new_price >= compare_at:
                problems.append(f"{name}: {shown(new_price)} is not below the regular "
                                f"price {shown(compare_at)}")
                continue
            # compareAtPrice deliberately absent: omitted means untouched, and
            # the regular price is exactly what must survive a deeper cut.
            changes.append({"id": variant["id"], "price": shown(new_price)})
            regular = compare_at
        else:
            if current == pricing.ON_SALE:
                problems.append(
                    f"{name}: already on sale (regular {shown(compare_at)}, sale "
                    f"{shown(price)}); use --deep-discount to change the sale price")
                continue
            if new_price >= price:
                # The result would be a compare-at at or below the price, which
                # Shopify shows as no sale at all.
                problems.append(f"{name}: {shown(new_price)} is not a markdown from "
                                f"{shown(price)}")
                continue
            # A STALE compare-at is overwritten here, which is the fix for it.
            changes.append({"id": variant["id"], "price": shown(new_price),
                            "compareAtPrice": shown(price)})
            regular = price

        rows.append({"variant": name[:58], "regular": shown(regular),
                     "sale now": shown(price) if current == pricing.ON_SALE else "-",
                     "sale after": shown(new_price)})

    pricing.refuse(problems, heading=f"{product['title']!r} cannot go on sale:")
    return changes, rows
