#!/usr/bin/env python3
"""Show what stock sits where, across every location.

    ./shopify.sh list-inventory
    ./shopify.sh list-inventory --shop dev
    ./shopify.sh list-inventory --with-stock --at "Craft Fair"
    ./shopify.sh list-inventory --needs-activation
    ./shopify.sh list-inventory --json | jq ...

Read-only. The table is the same shape ``seed-dev`` prints, so a rehearsal
store and the real one can be compared side by side, and the markers mean the
same thing in both:

    6     stocked, six units
    0     stocked, empty
    --    not activated there at all
    4*    stocked, but some variants are not activated there

The ``0`` versus ``--`` distinction is the point of this task. Both mean "none
here", but only ``--`` requires an ``inventoryActivate`` before stock can be
written, and the admin's inventory screen does not draw the difference.
"""
from __future__ import annotations

from dataclasses import dataclass

from shopifylib import gid, inventory, output
from shopifylib.errors import TaskError
from shopifylib.task import task

# Two cheap queries beat one expensive one. Asking for products -> variants ->
# inventoryLevels in a single document multiplies the connection costs and blows
# the 1000-point per-query cap that applies on every plan (a 20/100/25 nesting
# came out at 1007). So levels are read per LOCATION instead -- a flat, cheap
# connection -- and joined to variants locally by inventory item id.
PRODUCT_PAGE = 50
VARIANTS_PER_PRODUCT = 100
LEVEL_PAGE = 250

LOCATIONS_QUERY = """
query Locations {
  locations(first: 250, includeInactive: true) {
    nodes { id name isActive fulfillsOnlineOrders }
  }
}
"""

# Flat: every item stocked at one location. Presence of a row here IS the
# activation flag for that item at that location.
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

_VARIANT_FIELDS = """
  nodes {
    displayName
    sku
    inventoryItem { id tracked }
  }
  pageInfo { hasNextPage }
"""

SCAN_ALL_QUERY = """
query ListInventory($first: Int!, $after: String, $variants: Int!) {
  products(first: $first, after: $after) {
    nodes {
      id
      title
      status
      variants(first: $variants) { %s }
    }
    pageInfo { hasNextPage endCursor }
  }
}
""" % _VARIANT_FIELDS

SCAN_PRODUCTS_QUERY = """
query ListInventoryByIds($ids: [ID!]!, $variants: Int!) {
  nodes(ids: $ids) {
    ... on Product {
      id
      title
      status
      variants(first: $variants) { %s }
    }
  }
}
""" % _VARIANT_FIELDS


@dataclass
class Row:
    """One product, with each variant's stock keyed by location name."""

    product_id: str
    title: str
    status: str
    variant_quantities: list[dict[str, int]]
    tracked: bool
    skus: list[str]

    @property
    def variants(self) -> int:
        return len(self.variant_quantities)

    @property
    def total(self) -> int:
        return sum(sum(q.values()) for q in self.variant_quantities)

    def at(self, location: str) -> int:
        return sum(q.get(location, 0) for q in self.variant_quantities)


def add_arguments(parser):
    parser.add_argument("--pids", metavar="A,B,C",
                        help="comma-separated product ids (numeric, GID, or admin URL)")
    parser.add_argument("--at", metavar="LOC", action="append",
                        help="only show these locations as columns (repeatable); "
                             "matches on name, prefix, or id")
    parser.add_argument("--with-stock", action="store_true",
                        help="only products holding stock somewhere")
    parser.add_argument("--needs-activation", action="store_true",
                        help="only products not activated at every shown location")
    parser.add_argument("--untracked", action="store_true",
                        help="only products with inventory tracking off")
    parser.add_argument("--sku", action="store_true",
                        help="add a SKU column (single-variant products only)")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="stop after N products")


@task(
    name="list-inventory",
    help="Show stock levels across locations",
    scopes=["read_products", "read_inventory", "read_locations"],
)
def run(ctx, args):
    """Show stock levels across locations.

    Read-only. Prints the same table shape as seed-dev, distinguishing a
    location holding zero from one where the item was never activated.
    """
    locations = _columns(ctx, args.at)
    stock = _read_levels(ctx, locations)

    if args.pids:
        rows = _scan_products(ctx, args.pids, stock)
    else:
        rows = _scan_all(ctx, stock, limit=args.limit)

    rows = _filter(rows, locations, args)
    rows.sort(key=lambda r: r.title.lower())

    if ctx.json_output:
        output.emit_json({
            "locations": [l["name"] for l in locations],
            "products": [{
                "id": r.product_id, "title": r.title, "status": r.status,
                "variants": r.variants, "tracked": r.tracked, "total": r.total,
                "stock": {
                    l["name"]: {
                        "available": r.at(l["name"]),
                        # Explicit, because 0 and "absent" are different states
                        # and JSON consumers shouldn't have to infer it.
                        "activated_variants": sum(
                            1 for q in r.variant_quantities if l["name"] in q),
                    } for l in locations
                },
            } for r in rows],
        })
        return

    if not rows:
        print("(no products match)")
        return

    _print_table(rows, locations, show_sku=args.sku)
    _print_summary(rows, locations)


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

def _columns(ctx, wanted: list[str] | None) -> list[dict]:
    """Which locations become columns, in a stable order."""
    locations = ctx.query(LOCATIONS_QUERY)["locations"]["nodes"]
    if not locations:
        raise TaskError("This store has no locations.")
    if not wanted:
        # Active first, then by name, so the interesting columns come first and
        # the order doesn't wander between runs.
        return sorted(locations, key=lambda l: (not l["isActive"], l["name"].lower()))

    chosen: list[dict] = []
    for value in wanted:
        match = _resolve(locations, value)
        if match["id"] not in {c["id"] for c in chosen}:
            chosen.append(match)
    return chosen


def _resolve(locations: list[dict], value: str) -> dict:
    """Match a location by exact name, then id, then unique prefix."""
    wanted = value.strip()

    exact = [l for l in locations if l["name"].lower() == wanted.lower()]
    if len(exact) == 1:
        return exact[0]

    if gid.is_gid(wanted) or wanted.isdigit():
        target = gid.coerce(wanted, "Location")
        for loc in locations:
            if loc["id"] == target:
                return loc

    partial = [l for l in locations if wanted.lower() in l["name"].lower()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise TaskError(
            f"--at {wanted!r} matches more than one location:\n"
            + "\n".join(f"  {l['name']}" for l in partial))

    raise TaskError(
        f"--at: no location matches {wanted!r}. This store has:\n"
        + "\n".join(f"  {l['name']}" for l in locations))


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _read_levels(ctx, locations: list[dict]) -> dict[str, dict[str, int]]:
    """{inventory item id: {location name: available}} for the shown locations.

    One flat pass per location. A location missing from an item's inner map
    means the item is not activated there -- the distinction the whole table is
    built on, so it must come from a row's absence rather than a zero.
    """
    stock: dict[str, dict[str, int]] = {}
    for location in locations:
        progress = output.Progress(f"reading {location['name']}")
        for level in ctx.paginate(LEVELS_QUERY, "location.inventoryLevels",
                                  variables={"id": location["id"]},
                                  page_size=LEVEL_PAGE):
            progress.tick()
            item_id = (level.get("item") or {}).get("id")
            if item_id:
                stock.setdefault(item_id, {})[location["name"]] = \
                    inventory.available_at(level)
        progress.done()
    return stock


def _scan_all(ctx, stock: dict, *, limit: int | None) -> list[Row]:
    progress = output.Progress("reading products")
    rows = []
    for node in ctx.paginate(SCAN_ALL_QUERY, "products",
                             variables={"variants": VARIANTS_PER_PRODUCT},
                             page_size=PRODUCT_PAGE, max_items=limit):
        progress.tick()
        rows.append(_to_row(ctx, node, stock))
    progress.done()
    return rows


def _scan_products(ctx, pids: str, stock: dict) -> list[Row]:
    ids = [gid.coerce(p, "Product") for p in pids.split(",") if p.strip()]
    if not ids:
        raise TaskError("--pids was empty.")

    rows = []
    for chunk in (ids[i:i + 25] for i in range(0, len(ids), 25)):
        data = ctx.query(SCAN_PRODUCTS_QUERY,
                         {"ids": chunk, "variants": VARIANTS_PER_PRODUCT})
        for requested, node in zip(chunk, data["nodes"]):
            if node is None:
                ctx.log.warning("No such product: %s", gid.numeric(requested))
                continue
            rows.append(_to_row(ctx, node, stock))
    return rows


def _to_row(ctx, node: dict, stock: dict) -> Row:
    variants = node["variants"]
    if variants["pageInfo"]["hasNextPage"]:
        ctx.log.warning("%r has more than %d variants; showing the first %d.",
                        node["title"], VARIANTS_PER_PRODUCT, VARIANTS_PER_PRODUCT)

    quantities: list[dict[str, int]] = []
    skus: list[str] = []
    tracked = False
    for variant in variants["nodes"]:
        item = variant.get("inventoryItem") or {}
        if item.get("tracked"):
            tracked = True
        if variant.get("sku"):
            skus.append(variant["sku"])
        # dict() so a later mutation can't reach back into the shared map.
        quantities.append(dict(stock.get(item.get("id"), {})))

    return Row(
        product_id=node["id"],
        title=inventory.clean_label(node["title"]),
        status=node.get("status") or "",
        variant_quantities=quantities,
        tracked=tracked,
        skus=skus,
    )


def _filter(rows: list[Row], locations: list[dict], args) -> list[Row]:
    names = [l["name"] for l in locations]
    if args.with_stock:
        rows = [r for r in rows if any(r.at(n) > 0 for n in names)]
    if args.untracked:
        rows = [r for r in rows if not r.tracked]
    if args.needs_activation:
        rows = [r for r in rows
                if any(inventory.needing_activation(r.variant_quantities, n)
                       for n in names)]
    return rows


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _print_table(rows: list[Row], locations: list[dict], *, show_sku: bool) -> None:
    # Truncate long location names to keep columns narrow, but only as much as
    # is needed to stay distinct.
    headers = _short_names([l["name"] for l in locations])

    table = []
    for row in rows:
        entry = {"product": row.title[:38], "var": row.variants,
                 "tracked": "yes" if row.tracked else "no"}
        if show_sku:
            entry["sku"] = (row.skus[0] if len(row.skus) == 1 else "")[:16]
        for loc, header in zip(locations, headers):
            entry[header] = inventory.location_cell(
                row.variant_quantities, loc["name"])
        table.append(entry)

    print(output.format_table(table))


def _short_names(names: list[str], width: int = 16) -> list[str]:
    """Shorten column headers, falling back to full names if that collides."""
    short = [n[:width] for n in names]
    return names if len(set(short)) != len(names) else short


def _print_summary(rows: list[Row], locations: list[dict]) -> None:
    variants = sum(r.variants for r in rows)
    print(f"\n{len(rows)} product(s), {variants} variant(s), "
          f"{sum(r.total for r in rows)} unit(s)")

    for loc in locations:
        name = loc["name"]
        units = sum(r.at(name) for r in rows)
        stocked = sum(
            1 for r in rows for q in r.variant_quantities if name in q)
        note = "" if loc["fulfillsOnlineOrders"] else "  (no online orders)"
        print(f"  {name:<24} {units:>6} unit(s), {stocked}/{variants} "
              f"variant(s) stocked{note}")

    for line in inventory.legend():
        print(line)
