#!/usr/bin/env python3
"""Mark a product down.

    ./shopify.sh go-on-sale --pid 123 --price 15.00
    ./shopify.sh go-on-sale --pid 123 --price 15.00 --commit
    ./shopify.sh go-on-sale --pid 123 --price 12.00 --deep-discount --commit
    ./shopify.sh go-on-sale --vids 456,789 --price 15.00 --commit

Moves each variant's current price into ``compareAtPrice`` and sets ``price``
to ``--price``, which is how this store marks a product down. Every variant of
the product gets the same sale price; ``--vids`` names individual variants
instead (list-on-sale's vid column), for marking down one size and not another.

A product already on sale is refused: going on sale twice would copy the sale
price into compare-at and lose the regular price for good. ``--deep-discount``
is the way to change the sale price of something already marked down -- it
leaves compare-at (the regular price) alone and only moves ``price``.

Every variant is checked before anything is written, so a run either fully
applies or leaves everything exactly as it was.
"""
from __future__ import annotations

from shopifylib import gid, output, pricing
from shopifylib.task import task


def add_arguments(parser):
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--pid", metavar="ID",
                        help="product id (numeric, GID, or admin URL); every "
                             "variant goes on sale")
    target.add_argument("--vids", metavar="A,B,C",
                        help="comma-separated variant ids; only these go on sale")
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
    if args.vids:
        products = pricing.load_variants(
            ctx, pricing.parse_ids(args.vids, "ProductVariant", "--vids"))
    else:
        products = pricing.load_products(ctx, [gid.coerce(args.pid, "Product")])

    planned, problems = [], []
    for product in products:
        changes, rows, bad = plan(product, new_price, deep=args.deep_discount)
        problems.extend(bad)
        planned.append((product, changes, rows))
    pricing.refuse(problems, heading="Cannot go on sale, so nothing was changed:")

    pricing.preview(ctx, [row for _, _, rows in planned for row in rows])
    before = "sale now" if args.deep_discount else "regular"
    for product, changes, rows in planned:
        action = (f"deepen the discount on {product['title']!r}" if args.deep_discount
                  else f"put {product['title']!r} on sale")
        pricing.apply(ctx, product, changes,
                      describe=f"{action}: " + ", ".join(
                          f"{r[before]} -> {r['sale after']}" for r in rows))

    if ctx.json_output:
        output.emit_json({"dry_run": ctx.dry_run, "products": [
            {"product_id": p["id"], "title": p["title"], "variants": rows}
            for p, _, rows in planned]})


def plan(product: dict, new_price, *, deep: bool) -> tuple[list[dict], list[dict], list[str]]:
    """(mutation inputs, preview rows, problems) for one product's variants."""
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

    return changes, rows, problems
