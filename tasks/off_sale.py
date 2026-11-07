#!/usr/bin/env python3
"""Take products off sale.

    ./shopify.sh off-sale --pids 123
    ./shopify.sh off-sale --pids 123,456 --commit

The reverse of go-on-sale: each variant's ``price`` goes back to its
``compareAtPrice`` (the regular price) and compare-at is cleared.

A product that is not on sale is refused rather than skipped. That includes a
compare-at at or below the price: "restoring" that would set the price to a
number that was never the regular one. Every named product is checked before
anything is written, so a run either takes all of them off sale or none.
"""
from __future__ import annotations

from shopifylib import output, pricing
from shopifylib.task import task


def add_arguments(parser):
    parser.add_argument("--pids", required=True, metavar="A,B,C",
                        help="comma-separated product ids (numeric, GID, or admin URL)")


@task(
    name="off-sale",
    help="Take products off sale: price <- compare-at, compare-at cleared",
    writes=True,
    scopes=["read_products", "write_products"],
)
def run(ctx, args):
    """Take products off sale: price <- compare-at, compare-at cleared.

    Refuses any product with a variant that is not on sale, and writes nothing
    in that case.
    """
    products = pricing.load_products(ctx, pricing.product_ids(args.pids, "--pids"))

    planned, problems = [], []
    for product in products:
        changes, rows, bad = plan(product)
        problems.extend(bad)
        planned.append((product, changes, rows))
    pricing.refuse(problems, heading="Not on sale, so nothing was changed:")

    pricing.preview(ctx, [row for _, _, rows in planned for row in rows])
    for product, changes, rows in planned:
        pricing.apply(ctx, product, changes,
                      describe=f"take {product['title']!r} off sale: "
                               + ", ".join(f"{r['sale now']} -> {r['price after']}"
                                           for r in rows))

    if ctx.json_output:
        output.emit_json({"dry_run": ctx.dry_run, "products": [
            {"product_id": p["id"], "title": p["title"], "variants": rows}
            for p, _, rows in planned]})


def plan(product: dict) -> tuple[list[dict], list[dict], list[str]]:
    """(mutation inputs, preview rows, problems) for one product."""
    changes, rows, problems = [], [], []
    for variant in product["variants"]["nodes"]:
        name = pricing.label(variant, product)
        price = pricing.parse_money(variant["price"])
        compare_at = pricing.parse_money(variant["compareAtPrice"])
        current = pricing.state(price, compare_at)
        if current != pricing.ON_SALE:
            detail = (f"compare-at {pricing.format_money(compare_at)}, price "
                      f"{pricing.format_money(price)}" if compare_at
                      else f"price {pricing.format_money(price)}")
            problems.append(f"{name}: {current} ({detail})")
            continue
        regular = pricing.format_money(compare_at)
        # None, not omitted: omitting would leave the compare-at in place.
        changes.append({"id": variant["id"], "price": regular, "compareAtPrice": None})
        rows.append({"variant": name[:58], "sale now": pricing.format_money(price),
                     "price after": regular})
    return changes, rows, problems
