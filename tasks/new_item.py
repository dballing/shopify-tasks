#!/usr/bin/env python3
"""Create a product, stock it at the venues named, and publish it.

    ./shopify.sh new-item --name "Opal Wings" --category earrings --price 20 \\
        --stock Home=2 --stock "Market A=1"

    ./shopify.sh new-item --name "Sun Hoops" --category earrings --option Metal \\
        --variant "Gold=18.00" --variant "Silver=16.00" \\
        --stock "Gold@Home=2" --stock "Silver@Market A=1" --commit

The defaults match what the store's POS-only products actually carry (read
from prod in 2026-11, not assumed): vendor is the shop's name, inventory is
tracked, and the item is active and published to Point of Sale and the
Shopify GraphiQL App. ``--draft`` and ``--channels`` override those.

**Stock.** ``--stock LOCATION=QTY`` sets the available quantity, and in doing
so activates the item at that location. ``=0`` is meaningful: it activates the
item there with nothing on hand, which is not the same as leaving the location
out (not activated at all -- see shopifylib.inventory). With variants, prefix
the variant: ``--stock "Gold@Home=2"``. A disabled venue is refused, since a
disabled location cannot hold stock; ``enable-location`` it first.

**Category** is matched against the categories this store's products already
use, so ``necklace`` finds Necklaces. A taxonomy id such as ``aa-6-6`` is
accepted too, which is how to pick one nothing uses yet.

**All-or-nothing up to the first write.** Every problem is reported in one
go before anything is created. Publishing is a second call after the product
exists; if that one fails, the product is left in place (it is correct, just
not on every channel) and the run says which channels to fix and exits 1.

**Duplicates are refused** unless ``--allow-duplicate``: re-running a command
after ``--commit`` is the likeliest way to get one, and Shopify would happily
create a second product with a suffixed handle.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from shopifylib import catalog, gid, inventory, locations as locs, output, pricing
from shopifylib.errors import ShopifyTasksError, TaskError
from shopifylib.task import task

# What a POS-only product here is published to. "Shopify GraphiQL App" is
# deliberate, not a test leftover: it is how the store's own tooling reads
# products.
DEFAULT_CHANNELS = ("Point of Sale", "Shopify GraphiQL App")

# The store's existing admin-notes field (definition type on prod; dev has no
# definition, and Shopify accepts the explicit type without one).
NOTES_METAFIELD = {"namespace": "custom", "key": "admin_notes",
                   "type": "multi_line_text_field"}

SHOP_NAME_QUERY = "query ShopName { shop { name } }"

# Exact-title check. Shopify's search is tokenised, so this narrows the field
# and the exact comparison happens locally. Archived products are included on
# purpose: an archived twin is usually something to unarchive, not recreate.
SAME_TITLE_QUERY = """
query SameTitle($q: String!) {
  products(first: 50, query: $q) {
    nodes { id title status }
  }
}
"""

# synchronous: true so the response carries the product and its variants rather
# than a background-operation handle. No @idempotent: productSet with
# inventoryQuantities runs without one on 2026-07 (seed-dev relies on it).
PRODUCT_SET_MUTATION = """
mutation NewItem($input: ProductSetInput!) {
  productSet(input: $input, synchronous: true) {
    product {
      id title status
      category { name fullName }
      variants(first: 100) { nodes { id title price } }
    }
    userErrors { field message code }
  }
}
"""


@dataclass
class Variant:
    value: str | None                 # None for a single-variant product
    price: Decimal
    stock: dict[str, int] = field(default_factory=dict)   # location id -> qty

    @property
    def label(self) -> str:
        return self.value or inventory.DEFAULT_OPTION_VALUE


@dataclass
class Item:
    title: str
    status: str
    vendor: str
    category: dict
    channels: list[dict]
    option: str | None
    variants: list[Variant]
    locations: list[dict]            # every location named, in first-mention order
    description: str = ""
    notes: str | None = None

    @property
    def units(self) -> int:
        return sum(sum(v.stock.values()) for v in self.variants)


def add_arguments(parser):
    parser.add_argument("--name", required=True, help="product title")
    parser.add_argument("--description", metavar="TEXT",
                        help="plain text; a blank line starts a new paragraph")
    parser.add_argument("--price", metavar="AMOUNT",
                        help="the price, or with --variant the default for any "
                             "variant given without its own")
    parser.add_argument("--category", required=True, metavar="NAME",
                        help="a category the store already uses (necklace, "
                             "bracelet, earrings...) or a taxonomy id like aa-6-6")
    parser.add_argument("--channels", metavar="A,B",
                        help="comma-separated sales channels (default: "
                             + ", ".join(DEFAULT_CHANNELS) + ")")
    parser.add_argument("--draft", action="store_true",
                        help="create as a draft instead of active")
    parser.add_argument("--option", metavar="NAME",
                        help="the option the variants differ by, e.g. Metal or Size")
    parser.add_argument("--variant", action="append", metavar="VALUE[=PRICE]",
                        help="one variant (repeatable); needs --option")
    parser.add_argument("--stock", action="append", metavar="[VARIANT@]LOCATION=QTY",
                        help="stock at a location, activating it there (repeatable); "
                             "=0 activates with nothing on hand")
    parser.add_argument("--notes", metavar="TEXT",
                        help="admin notes (the custom.admin_notes field)")
    parser.add_argument("--allow-duplicate", action="store_true",
                        help="create even if a product with this exact title exists")


@task(
    name="new-item",
    help="Create a product with price, category, channels and stock",
    writes=True,
    scopes=["read_products", "write_products", "read_inventory", "write_inventory",
            "read_locations", "read_publications", "write_publications"],
)
def run(ctx, args):
    """Create a product with price, category, channels and stock.

    Active and on Point of Sale + Shopify GraphiQL App unless told otherwise.
    Everything is checked before anything is written.
    """
    title = args.name.strip()
    if not title:
        raise TaskError("--name is empty.")

    # Offline checks first: a typo in a price shouldn't cost five API calls.
    variants, problems = parse_variants(args.option, args.variant or [], args.price)
    stock_specs, stock_problems = parse_stocks(args.stock or [],
                                               has_variants=bool(args.variant))
    problems += stock_problems
    pricing.refuse(problems, heading="Nothing was created:")

    if not args.allow_duplicate:
        refuse_duplicate(ctx, title)

    item, problems = plan(
        ctx, title=title, args=args, variants=variants, stock_specs=stock_specs,
        known_locations=locs.load(ctx),
        categories=catalog.load_categories(ctx),
        channels=catalog.load_channels(ctx),
        vendor=ctx.query(SHOP_NAME_QUERY)["shop"]["name"],
    )
    pricing.refuse(problems, heading="Nothing was created:")

    preview(ctx, item)
    return create(ctx, item)


# ---------------------------------------------------------------------------
# Parsing (offline)
# ---------------------------------------------------------------------------

def _price(text: str, where: str) -> Decimal:
    try:
        return pricing.parse_price_arg(text)
    except TaskError as exc:
        raise TaskError(f"{where}: {exc}") from None


_AT_PRICE = re.compile(r"^(?P<value>.+)@\s*\$?(?P<price>\d+(?:\.\d+)?)\s*$")


def parse_variants(option: str | None, specs: list[str],
                   default_price: str | None) -> tuple[list[Variant], list[str]]:
    """Variants from ``--variant VALUE[=PRICE]``, or the one implicit variant.

    Split on the *last* ``=``, so a value may contain one ("Length=16in" would
    otherwise lose half its name).
    """
    problems: list[str] = []
    default = None
    if default_price is not None:
        try:
            default = _price(default_price, "--price")
        except TaskError as exc:
            problems.append(str(exc))

    if not specs:
        if option:
            problems.append("--option was given without any --variant.")
        if default_price is None:
            problems.append("--price is required.")
        return ([Variant(None, default)] if default else []), problems

    if not option or not option.strip():
        problems.append("--variant needs --option to say what the variants differ by "
                        "(e.g. --option Metal).")

    variants: list[Variant] = []
    seen: set[str] = set()
    for spec in specs:
        value, sep, price_text = spec.rpartition("=")
        if not sep:
            value, price_text = spec, ""
            # "VALUE@PRICE" is a natural slip, since --stock uses "@" -- but
            # there it separates variant from location. Point at the fix
            # rather than reporting a variant literally named "...@42.00".
            slip = _AT_PRICE.match(spec)
            if slip:
                problems.append(
                    f"--variant {spec!r}: the price goes after '=', not '@' "
                    f"(--variant '{slip['value'].strip()}={slip['price']}').")
                continue
        value = value.strip()
        if not value:
            problems.append(f"--variant {spec!r} has no value.")
            continue
        if value.lower() in seen:
            problems.append(f"--variant {value!r} is given twice.")
            continue
        seen.add(value.lower())
        if price_text.strip():
            try:
                price = _price(price_text, f"--variant {spec!r}")
            except TaskError as exc:
                problems.append(str(exc))
                continue
        elif default is not None:
            price = default
        else:
            problems.append(f"--variant {value!r} has no price; give {value}=PRICE "
                            f"or a default --price.")
            continue
        variants.append(Variant(value, price))
    return variants, problems


@dataclass
class StockSpec:
    text: str                 # as typed, for messages
    variant: str | None
    location: str
    quantity: int


def parse_stocks(specs: list[str], *, has_variants: bool) -> tuple[list[StockSpec], list[str]]:
    """``[VARIANT@]LOCATION=QTY``.

    Last ``=`` for the quantity, first ``@`` for the variant: location names
    here are plain words, and a variant value is the likelier of the two to
    hold odd characters, so neither split can cut through a location name.
    """
    parsed: list[StockSpec] = []
    problems: list[str] = []
    for spec in specs:
        target, sep, qty_text = spec.rpartition("=")
        if not sep or not target.strip():
            problems.append(f"--stock {spec!r}: expected LOCATION=QTY.")
            continue
        try:
            quantity = int(qty_text.strip())
        except ValueError:
            problems.append(f"--stock {spec!r}: {qty_text.strip()!r} is not a whole number.")
            continue
        if quantity < 0:
            problems.append(f"--stock {spec!r}: quantity cannot be negative.")
            continue

        variant, at, location = target.partition("@")
        if not at:
            variant, location = None, target
            if has_variants:
                problems.append(f"--stock {spec!r}: with --variant, say which one "
                                f"(e.g. --stock \"Gold@{target.strip()}={quantity}\").")
                continue
        elif not has_variants:
            problems.append(f"--stock {spec!r}: there are no variants, so drop the "
                            f"{variant.strip()!r}@ prefix.")
            continue
        else:
            variant = variant.strip()

        if not location.strip():
            problems.append(f"--stock {spec!r}: no location given.")
            continue
        parsed.append(StockSpec(spec, variant, location.strip(), quantity))
    return parsed, problems


# ---------------------------------------------------------------------------
# Planning (reads done; nothing written)
# ---------------------------------------------------------------------------

def refuse_duplicate(ctx, title: str) -> None:
    escaped = title.replace("\\", "\\\\").replace('"', '\\"')
    nodes = ctx.query(SAME_TITLE_QUERY, {"q": f'title:"{escaped}"'})["products"]["nodes"]
    twins = [n for n in nodes if n["title"].strip().lower() == title.lower()]
    if twins:
        lines = [f"  {gid.numeric(n['id'])}  {n['title']}"
                 + (f"  ({n['status'].lower()})" if inventory.status_label(n["status"]) else "")
                 for n in twins]
        raise TaskError(f"A product titled {title!r} already exists:\n"
                        + "\n".join(lines)
                        + "\nPass --allow-duplicate if a second one is really meant.")


def plan(ctx, *, title, args, variants, stock_specs, known_locations, categories,
         channels, vendor) -> tuple[Item | None, list[str]]:
    """Resolve every name to an id, collecting all problems rather than
    stopping at the first, so one run shows everything that needs fixing."""
    problems: list[str] = []

    category = None
    try:
        category = catalog.resolve_category(categories, args.category)
    except TaskError as exc:
        problems.append(str(exc))

    wanted_channels = ([c for c in args.channels.split(",") if c.strip()]
                       if args.channels else list(DEFAULT_CHANNELS))
    if not wanted_channels:
        problems.append("--channels was empty.")
    chosen_channels, channel_problems = catalog.resolve_channels(channels, wanted_channels)
    problems += channel_problems

    by_value = {v.label.lower(): v for v in variants}
    columns: list[dict] = []
    for spec in stock_specs:
        try:
            location = locs.resolve(known_locations, spec.location, "--stock")
        except TaskError as exc:
            problems.append(str(exc))
            continue
        if not location.get("isActive", True):
            problems.append(f"--stock {spec.text!r}: {location['name']!r} is disabled, "
                            f"and a disabled location cannot hold stock. Run "
                            f"enable-location first.")
            continue

        if spec.variant is None:
            target = variants[0] if variants else None
        else:
            target = by_value.get(spec.variant.lower())
            if target is None:
                problems.append(f"--stock {spec.text!r}: no --variant named "
                                f"{spec.variant!r}.")
                continue
        if target is None:
            continue          # no variant at all; already reported by parse_variants
        if location["id"] in target.stock:
            problems.append(f"--stock {spec.text!r}: {target.label} at "
                            f"{location['name']!r} is given twice.")
            continue
        target.stock[location["id"]] = spec.quantity
        if location["id"] not in {c["id"] for c in columns}:
            columns.append(location)

    if problems:
        return None, problems

    return Item(
        title=title,
        status="DRAFT" if args.draft else "ACTIVE",
        vendor=vendor,
        category=category,
        channels=chosen_channels,
        option=args.option.strip() if args.variant else None,
        variants=variants,
        locations=columns,
        description=catalog.description_html(args.description),
        notes=args.notes.strip() if args.notes and args.notes.strip() else None,
    ), []


def product_input(item: Item) -> dict:
    """The ProductSetInput for an item."""
    # productSet rejects variants without productOptions, even for a product
    # with no real options; Shopify's own form of "no options" is one Title
    # option valued "Default Title" (see seed-dev, where this was found).
    option = item.option or inventory.DEFAULT_OPTION
    variants = []
    for variant in item.variants:
        variants.append({
            "price": pricing.format_money(variant.price),
            "optionValues": [{"optionName": option, "name": variant.label}],
            # Tracked explicitly: every product in the store is, and an
            # untracked one can't hold the quantities below.
            "inventoryItem": {"tracked": True},
            # A location absent here stays not-activated; one present with 0 is
            # activated and empty. That distinction is the point of =0.
            "inventoryQuantities": [
                {"locationId": loc_id, "name": inventory.AVAILABLE, "quantity": qty}
                for loc_id, qty in variant.stock.items()
            ],
        })

    payload: dict = {
        "title": item.title,
        "status": item.status,
        "vendor": item.vendor,
        "category": item.category["id"],
        "productOptions": [{"name": option,
                            "values": [{"name": v.label} for v in item.variants]}],
        "variants": variants,
    }
    if item.description:
        payload["descriptionHtml"] = item.description
    if item.notes:
        payload["metafields"] = [{**NOTES_METAFIELD, "value": item.notes}]
    return payload


# ---------------------------------------------------------------------------
# Output and writing
# ---------------------------------------------------------------------------

def preview_rows(item: Item) -> list[dict]:
    rows = []
    for variant in item.variants:
        # Not variant.label: "Default Title" in a preview reads like a bug.
        row = {"variant": variant.value or "(only variant)",
               "price": pricing.format_money(variant.price)}
        for location in item.locations:
            qty = variant.stock.get(location["id"])
            row[location["name"]] = inventory.NOT_ACTIVATED if qty is None else qty
        rows.append(row)
    return rows


def preview(ctx, item: Item) -> None:
    if ctx.json_output:
        return
    print(f"\n{item.title}  ({item.status.lower()})")
    print(f"  category: {catalog.category_label(item.category)}")
    print(f"  channels: {', '.join(c['name'] for c in item.channels)}")
    print(f"  vendor:   {item.vendor}")
    if item.notes:
        print(f"  notes:    {item.notes}")
    print()
    print(output.format_table(preview_rows(item)))
    if not item.locations:
        # Verified on dev: with no inventoryQuantities at all, productSet
        # activates the item at the shop's default location with 0. Once any
        # are given, only those locations are activated.
        print("(no --stock given: Shopify will activate it at the default "
              "location with 0)")
    elif any(len(v.stock) < len(item.locations) for v in item.variants):
        print(inventory.legend()[0])
    print()


def create(ctx, item: Item) -> int:
    where = ", ".join(l["name"] for l in item.locations) or "no location"
    result = ctx.mutate(PRODUCT_SET_MUTATION, {"input": product_input(item)},
                        field="productSet",
                        describe=f"create {item.title!r} ({len(item.variants)} "
                                 f"variant(s), {item.units} unit(s) at {where})")
    product = result.get("product") if result else None
    product_id = product["id"] if product else None

    failed = publish(ctx, item, product_id)

    if ctx.dry_run:
        if ctx.json_output:
            output.emit_json({"dry_run": True, "title": item.title,
                              "variants": preview_rows(item)})
        return 0

    url = gid.admin_url(product_id, ctx.store_handle)
    if ctx.json_output:
        output.emit_json({
            "dry_run": False,
            "id": product_id,
            "title": product["title"],
            "status": product["status"],
            "url": url,
            "category": (product.get("category") or {}).get("fullName"),
            "variants": [{"id": v["id"], "title": v["title"], "price": v["price"]}
                         for v in product["variants"]["nodes"]],
            "channels": [c["name"] for c in item.channels if c["name"] not in failed],
            "channels_failed": failed,
        })
    else:
        print(f"Created {product['title']!r} (id {gid.numeric(product_id)}), "
              f"{product['status'].lower()}, "
              f"{(product.get('category') or {}).get('name') or 'no category'}.")
        print(f"  {url}")
    return 1 if failed else 0


def publish(ctx, item: Item, product_id: str | None) -> list[str]:
    """Publish to the chosen channels; the names that failed (none in a dry run).

    Never raises once the product exists: the product is real by now, and an
    exception would bury that under a traceback-shaped message. The person
    needs its id and which channels to fix instead.
    """
    names = [c["name"] for c in item.channels]
    describe = f"publish {item.title!r} to {', '.join(names)}"
    try:
        result = ctx.mutate(catalog.PUBLISH_MUTATION,
                            {"id": product_id or "(new product)",
                             "input": [{"publicationId": c["id"]} for c in item.channels]},
                            field="publishablePublish", describe=describe,
                            check_errors=False)
    except ShopifyTasksError as exc:
        _report_publish_failure(ctx, item, product_id, str(exc))
        return names
    if not result:            # dry run
        return []
    errors = locs.payload_errors(result)
    if errors:
        _report_publish_failure(ctx, item, product_id,
                                "; ".join(e.get("message", "?") for e in errors))
        return names
    return []


def _report_publish_failure(ctx, item: Item, product_id: str, why: str) -> None:
    ctx.log.error(
        "%r was created (id %s) but could not be published to %s: %s\n"
        "The product is otherwise complete. Add the channels in the admin: %s",
        item.title, gid.numeric(product_id),
        ", ".join(c["name"] for c in item.channels), why,
        gid.admin_url(product_id, ctx.store_handle))
