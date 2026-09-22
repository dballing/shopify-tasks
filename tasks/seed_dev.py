#!/usr/bin/env python3
"""Fill a development store with products to rehearse other tasks against.

    ./shopify.sh seed-dev --shop dev --commit                   # synthetic
    ./shopify.sh seed-dev --shop dev --reset --commit           # wipe first
    ./shopify.sh seed-dev --shop dev --clone-from prod --commit # real catalogue

**Synthetic is the default** because the point of a rehearsal store is to
exercise the cases that break things, and a real catalogue mostly contains the
case that works. The generated set deliberately includes untracked items,
zero-stock items, stock at one location only, multi-variant products with
uneven stock, and a quantity large enough to cross a batch boundary. It is
generated from a fixed seed, so ``--reset`` returns the store to a byte-identical
starting point and a rehearsal is repeatable.

``--clone-from`` copies real products instead, for when a specific corner case
only shows up in genuine data. It carries titles, options, variants, SKUs,
prices, tracking and per-location quantities -- but **not** images, metafields,
collections or SEO, which need staged uploads and a great deal more machinery
than a rehearsal warrants.

**This task refuses to run against anything but a development store.** It
fabricates and deletes data wholesale; dry-run cannot protect you from aiming
the right task at the wrong store, because the preview looks entirely correct.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from shopifylib import gid, output
from shopifylib.errors import TaskError
from shopifylib.paging import paginate
from shopifylib.task import task

# The two locations the other tasks in this repo assume. Created by hand -- see
# _require_locations for why this task doesn't make them itself.
HOME = "Home"
FAIR = "Craft Fair Inventory"

# Fixed so --reset reproduces the same store every time. A rehearsal you can't
# repeat is a rehearsal you can't trust.
SEED = 20260922

DEFAULT_COUNT = 30

PRODUCTS_QUERY = """
query AllProducts($first: Int!, $after: String) {
  products(first: $first, after: $after) {
    nodes { id title }
    pageInfo { hasNextPage endCursor }
  }
}
"""

CLONE_SOURCE_QUERY = """
query CloneSource($first: Int!, $after: String) {
  products(first: $first, after: $after) {
    nodes {
      title descriptionHtml productType vendor tags status
      options { name optionValues { name } }
      variants(first: 100) {
        nodes {
          title price sku
          selectedOptions { name value }
          inventoryItem {
            tracked
            inventoryLevels(first: 20) {
              nodes {
                location { name }
                quantities(names: ["available"]) { name quantity }
              }
            }
          }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

PRODUCT_SET_MUTATION = """
mutation Seed($input: ProductSetInput!) {
  productSet(input: $input, synchronous: true) {
    product { id title }
    userErrors { field message code }
  }
}
"""

PRODUCT_DELETE_MUTATION = """
mutation Wipe($input: ProductDeleteInput!) {
  productDelete(input: $input) {
    deletedProductId
    userErrors { field message }
  }
}
"""

LOCATIONS_QUERY = "{ locations(first: 50) { nodes { id name } } }"


@dataclass
class SeedVariant:
    option_values: list[tuple[str, str]]     # (option name, value)
    price: str
    sku: str
    tracked: bool
    quantities: dict[str, int] = field(default_factory=dict)   # location name -> qty


@dataclass
class SeedProduct:
    title: str
    options: list[tuple[str, list[str]]]     # (option name, values)
    variants: list[SeedVariant]
    product_type: str = "Beads"
    vendor: str = "Seed Data"
    tags: list[str] = field(default_factory=lambda: ["seed-data"])

    @property
    def total(self) -> int:
        return sum(sum(v.quantities.values()) for v in self.variants)


def add_arguments(parser):
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT, metavar="N",
                        help=f"how many synthetic products to create "
                             f"(default {DEFAULT_COUNT}); ignored with --clone-from")
    parser.add_argument("--clone-from", metavar="SHOP",
                        help="copy real products from another configured shop "
                             "instead of generating them (read-only on the source)")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="with --clone-from, copy at most N products")
    parser.add_argument("--reset", action="store_true",
                        help="delete every existing product first")


@task(
    name="seed-dev",
    help="Fill a development store with test products",
    writes=True,
    scopes=["read_products", "write_products", "read_locations",
            "read_inventory", "write_inventory"],
)
def run(ctx, args):
    """Fill a development store with test products.

    Generates synthetic products covering the awkward inventory cases, or
    copies real ones with --clone-from. Refuses to run anywhere but a
    development store.
    """
    # Before anything else, and before any preview that might lull you.
    ctx.require_development_store()

    locations = _require_locations(ctx)

    if args.reset:
        _wipe(ctx)

    if args.clone_from:
        products = _clone_source(ctx, args.clone_from, limit=args.limit)
        ctx.log.info("Cloning %d product(s) from %r", len(products), args.clone_from)
    else:
        products = _generate(args.count)
        ctx.log.info("Generating %d synthetic product(s)", len(products))

    if not products:
        ctx.log.info("Nothing to create.")
        return

    _preview(ctx, products)

    created, failed = [], []
    progress = output.Progress("creating products", total=len(products))
    for product in products:
        payload = ctx.mutate(
            PRODUCT_SET_MUTATION,
            {"input": _to_input(product, locations)},
            field="productSet",
            describe=f"create {product.title!r} ({product.total} units)",
            check_errors=False,
        )
        # Dry run returns the inert sentinel; there is nothing to inspect.
        if payload and isinstance(payload, dict):
            errors = payload.get("userErrors") or []
            if errors:
                failed.append((product.title,
                               "; ".join(e.get("message", "?") for e in errors)))
            else:
                created.append(product.title)
        progress.tick()
    progress.done()

    if ctx.dry_run:
        return

    ctx.log.info("Created %d product(s), %d unit(s) of stock.",
                 len(created), sum(p.total for p in products))
    if failed:
        ctx.log.warning("%d product(s) failed:", len(failed))
        for title, message in failed:
            ctx.log.warning("   %s: %s", title, message)

    if ctx.json_output:
        output.emit_json({"created": created,
                          "failed": [{"title": t, "error": e} for t, e in failed]})
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------

def _require_locations(ctx) -> dict[str, str]:
    """Map the two expected location names to ids, or explain how to make them.

    Creating them here would need the ``write_locations`` scope, which would
    mean editing the app, releasing a new version and re-authorising every
    store -- a lot of ceremony for two rows you create once by hand.
    """
    found = {l["name"]: l["id"] for l in ctx.query(LOCATIONS_QUERY)["locations"]["nodes"]}
    missing = [name for name in (HOME, FAIR) if name not in found]
    if missing:
        raise TaskError(
            f"{ctx.shop.id!r} is missing these locations: {', '.join(missing)}.\n\n"
            f"Create them in the store admin under Settings > Locations, named "
            f"exactly as above so the same commands work against both stores:\n"
            f"  - rename the default location to {HOME!r}\n"
            f"  - add {FAIR!r}, and uncheck 'fulfil online orders' to match production\n\n"
            f"Currently present: {', '.join(sorted(found)) or '(none)'}"
        )
    return {HOME: found[HOME], FAIR: found[FAIR]}


# ---------------------------------------------------------------------------
# Synthetic data
# ---------------------------------------------------------------------------

_COLOURS = ["Coral", "Jade", "Amber", "Indigo", "Rose", "Slate", "Mint", "Plum",
            "Sand", "Teal", "Ivory", "Copper"]
_KINDS = ["Bead", "Spacer", "Pendant", "Charm", "Necklace", "Bracelet"]
_SIZES = ["6mm", "8mm", "10mm"]


def _generate(count: int) -> list[SeedProduct]:
    """Build the synthetic set, weighted towards the cases that break things.

    Every seventh product is untracked and every fifth is a multi-variant, so a
    default run of 30 is guaranteed to contain several of each rather than
    leaving it to chance.
    """
    rng = random.Random(SEED)
    products: list[SeedProduct] = []

    for index in range(count):
        colour = _COLOURS[index % len(_COLOURS)]
        kind = _KINDS[(index // len(_COLOURS)) % len(_KINDS)]
        title = f"{colour} {kind} {index + 1:02d}"
        sku_base = f"SEED-{index + 1:03d}"

        # Untracked: no quantities exist, so inventory tasks must skip it.
        if index % 7 == 6:
            products.append(SeedProduct(
                title=f"{title} (untracked)",
                options=[],
                variants=[SeedVariant([], "4.00", sku_base, tracked=False)],
            ))
            continue

        # Multi-variant with uneven stock across locations.
        if index % 5 == 4:
            values = _SIZES
            variants = [
                SeedVariant(
                    option_values=[("Size", size)],
                    price=f"{4 + i}.50",
                    sku=f"{sku_base}-{size}",
                    tracked=True,
                    quantities={HOME: rng.randint(0, 6), FAIR: rng.randint(0, 2)},
                )
                for i, size in enumerate(values)
            ]
            products.append(SeedProduct(title=title,
                                        options=[("Size", list(values))],
                                        variants=variants))
            continue

        # Single variant, cycling through the location-stock permutations so a
        # run always covers home-only, fair-only, both, and neither.
        pattern = index % 4
        quantities = {
            0: {HOME: rng.randint(1, 9), FAIR: 0},        # source only
            1: {HOME: 0, FAIR: rng.randint(1, 4)},        # destination only
            2: {HOME: rng.randint(1, 5), FAIR: rng.randint(1, 3)},   # both
            3: {HOME: 0, FAIR: 0},                        # nothing anywhere
        }[pattern]
        products.append(SeedProduct(
            title=title,
            options=[],
            variants=[SeedVariant([], f"{3 + pattern}.25", sku_base,
                                  tracked=True, quantities=quantities)],
        ))

    # One deliberately large holding, so a move crosses the 50-item batch
    # boundary in units even when the product count is small.
    products.append(SeedProduct(
        title="Bulk Seed Mix (large stock)",
        options=[],
        variants=[SeedVariant([], "24.00", "SEED-BULK", tracked=True,
                              quantities={HOME: 250, FAIR: 0})],
    ))
    return products


# ---------------------------------------------------------------------------
# Cloning
# ---------------------------------------------------------------------------

def _clone_source(ctx, source_id: str, *, limit: int | None) -> list[SeedProduct]:
    """Read real products from another shop. The source client is read-only."""
    products: list[SeedProduct] = []
    with ctx.reader_for(source_id) as source:
        ctx.log.info("Reading products from %s (%s)", source_id, source.shop.domain)
        progress = output.Progress("reading source catalogue")
        for node in paginate(source, CLONE_SOURCE_QUERY, "products",
                             page_size=25, max_items=limit):
            progress.tick()
            product = _from_source(node)
            if product:
                products.append(product)
        progress.done()
    return products


def _from_source(node: dict) -> SeedProduct | None:
    options = [(o["name"], [v["name"] for v in o.get("optionValues") or []])
               for o in node.get("options") or []]
    # Shopify's placeholder option on a product with no real variants; sending
    # it back would create a literal "Title: Default Title" option.
    options = [(name, values) for name, values in options if name != "Title"]

    variants = []
    for variant in (node.get("variants") or {}).get("nodes") or []:
        selected = [(o["name"], o["value"]) for o in variant.get("selectedOptions") or []
                    if o["name"] != "Title"]
        item = variant.get("inventoryItem") or {}
        quantities: dict[str, int] = {}
        for level in (item.get("inventoryLevels") or {}).get("nodes") or []:
            name = (level.get("location") or {}).get("name")
            if name in (HOME, FAIR):
                for q in level.get("quantities") or []:
                    if q.get("name") == "available":
                        quantities[name] = int(q.get("quantity") or 0)
        variants.append(SeedVariant(
            option_values=selected,
            price=variant.get("price") or "0.00",
            sku=variant.get("sku") or "",
            tracked=bool(item.get("tracked")),
            quantities=quantities,
        ))

    if not variants:
        return None
    return SeedProduct(
        title=node["title"],
        options=options,
        variants=variants,
        product_type=node.get("productType") or "",
        vendor=node.get("vendor") or "",
        tags=list(node.get("tags") or []) + ["cloned"],
    )


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _to_input(product: SeedProduct, locations: dict[str, str]) -> dict:
    variants = []
    for variant in product.variants:
        entry: dict = {
            "price": variant.price,
            "optionValues": [{"optionName": name, "name": value}
                             for name, value in variant.option_values],
            "inventoryItem": {"tracked": variant.tracked},
        }
        if variant.sku:
            entry["inventoryItem"]["sku"] = variant.sku
        if variant.tracked:
            # Only tracked items may carry quantities; sending them for an
            # untracked one is a userError, not a no-op.
            entry["inventoryQuantities"] = [
                {"locationId": locations[name], "name": "available", "quantity": qty}
                for name, qty in variant.quantities.items()
                if name in locations
            ]
        variants.append(entry)

    payload: dict = {
        "title": product.title,
        "status": "ACTIVE",
        "variants": variants,
        "tags": product.tags,
    }
    if product.product_type:
        payload["productType"] = product.product_type
    if product.vendor:
        payload["vendor"] = product.vendor
    if product.options:
        payload["productOptions"] = [
            {"name": name, "values": [{"name": v} for v in values]}
            for name, values in product.options
        ]
    return payload


def _wipe(ctx) -> None:
    """Delete every product in the target store.

    Only reachable after require_development_store(), which is the only thing
    standing between this and a very bad afternoon.
    """
    existing = list(ctx.paginate(PRODUCTS_QUERY, "products", page_size=100))
    if not existing:
        ctx.log.info("Nothing to reset; the store has no products.")
        return

    ctx.log.warning("Deleting all %d existing product(s) from %s",
                    len(existing), ctx.shop.id)
    progress = output.Progress("deleting", total=len(existing))
    for node in existing:
        ctx.mutate(PRODUCT_DELETE_MUTATION, {"input": {"id": node["id"]}},
                   field="productDelete",
                   describe=f"delete {node['title']!r}")
        progress.tick()
    progress.done()


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _preview(ctx, products: list[SeedProduct]) -> None:
    if ctx.json_output:
        return
    rows = []
    for product in products[:12]:
        rows.append({
            "product": product.title[:38],
            "variants": len(product.variants),
            "tracked": "no" if not any(v.tracked for v in product.variants) else "yes",
            HOME: sum(v.quantities.get(HOME, 0) for v in product.variants),
            FAIR[:10]: sum(v.quantities.get(FAIR, 0) for v in product.variants),
        })
    print(output.format_table(rows))
    if len(products) > 12:
        print(f"... and {len(products) - 12} more")
    print(f"\n{len(products)} product(s), "
          f"{sum(p.total for p in products)} unit(s) of stock\n")
