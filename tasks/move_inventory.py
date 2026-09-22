#!/usr/bin/env python3
"""Move inventory from one location to another.

    ./shopify.sh move-inventory --from Home --to "Craft Fair Inventory" --all
    ./shopify.sh move-inventory --from Home --to "Craft Fair" --pids 123,456

The move is additive, not a copy: the source is decremented by exactly what it
held and the destination is incremented by the same amount, so Home=2 /
Fair=1 becomes Home=0 / Fair=3. An item with nothing at the source is skipped.

**Which quantity moves.** ``available`` -- the sellable count -- not ``on_hand``.
The difference matters when stock is committed to an unfulfilled order: those
units are promised and must stay at the location that will ship them. Adjusting
``available`` moves ``on_hand`` with it, so the physical total follows.

**Concurrency.** Every adjustment carries ``changeFromQuantity``, the quantity
we read during the scan. If a sale lands between the scan and the write,
Shopify rejects that entry with ``CHANGE_FROM_QUANTITY_STALE`` rather than
applying a delta to a number that moved underneath us. Rejected items are
reported and the rest of the batch still goes through.

Note that ``changeFromQuantity`` is **required**, despite introspection typing
it as a nullable ``Int``: omitting it fails the whole mutation with
"InventoryChangeInput must include the following argument". So this is not an
optional safety belt that could be switched off -- the API has no
last-write-wins mode.

**Fulfillment.** Items are activated at the destination (``inventoryActivate``)
so the quantity is genuinely stocked there rather than stranded. Location-level
settings are never touched: whether a location fulfils online orders is a
store-wide decision, and flipping it would affect every item there, not just
the ones being moved. When the destination doesn't fulfil online orders the run
says so, because moved stock then stops being sellable on the storefront --
usually the point when the goods are physically going to a fair, but never
something that should happen silently.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field as dc_field

from shopifylib import gid, inventory, output
from shopifylib.errors import TaskError
from shopifylib.task import task

# The quantity state that moves. See the module docstring for why not on_hand.
STATE = inventory.AVAILABLE

# Shown in the store's inventory adjustment history. Shopify takes a free string
# here (the schema types it String!, not an enum), but only its documented
# vocabulary renders as a readable label in the admin; an invented value comes
# back as a userError.
DEFAULT_REASON = "movement_created"

# Shopify's code for a lost compare-and-swap: the quantity we read during the
# scan no longer matches what is stored, i.e. something changed underneath us.
STALE_CODE = "CHANGE_FROM_QUANTITY_STALE"

# Item pairs per adjustment call. Each pair is two changes, so 50 is 100 changes
# -- comfortably inside query cost limits while keeping a few hundred items to a
# handful of round trips.
BATCH_SIZE = 50

# Variants fetched per product in --pids mode. Beyond this the task warns rather
# than silently moving part of a product.
VARIANTS_PER_PRODUCT = 100

LOCATIONS_QUERY = """
query Locations {
  locations(first: 250, includeInactive: true) {
    nodes { id name isActive fulfillsOnlineOrders }
  }
}
"""

# Scan every item stocked at the source. The destination level is fetched in the
# same pass via the item's own inventoryLevel(locationId:) so one query answers
# both "how much is here" and "is it activated there".
SCAN_ALL_QUERY = """
query ScanAll($fromId: ID!, $toId: ID!, $first: Int!, $after: String) {
  location(id: $fromId) {
    inventoryLevels(first: $first, after: $after) {
      nodes {
        quantities(names: ["available"]) { name quantity }
        item {
          id
          sku
          tracked
          variant { id displayName product { id title status } }
          toLevel: inventoryLevel(locationId: $toId) {
            id
            quantities(names: ["available"]) { name quantity }
          }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

SCAN_PRODUCTS_QUERY = """
query ScanProducts($ids: [ID!]!, $fromId: ID!, $toId: ID!, $variants: Int!) {
  nodes(ids: $ids) {
    ... on Product {
      id
      title
      status
      variants(first: $variants) {
        nodes {
          id
          displayName
          inventoryItem {
            id
            sku
            tracked
            fromLevel: inventoryLevel(locationId: $fromId) {
              quantities(names: ["available"]) { name quantity }
            }
            toLevel: inventoryLevel(locationId: $toId) {
              id
              quantities(names: ["available"]) { name quantity }
            }
          }
        }
        pageInfo { hasNextPage }
      }
    }
  }
}
"""

# inventoryActivate needs @idempotent too, even though Shopify's changelog for
# the 2026-04 change lists only the adjust/move/set/transfer mutations. That
# list is incomplete: without a key this fails at runtime with "The @idempotent
# directive is required for this mutation but was not provided". Assume any
# inventory mutation needs one rather than trusting the list.
ACTIVATE_MUTATION = """
mutation Activate($inventoryItemId: ID!, $locationId: ID!, $key: String!) {
  inventoryActivate(inventoryItemId: $inventoryItemId, locationId: $locationId)
    @idempotent(key: $key) {
    inventoryLevel { id }
    userErrors { field message }
  }
}
"""

# @idempotent became mandatory at runtime for inventory mutations in API version
# 2026-04 -- the schema does not mark it required, so omitting it fails only when
# called. The key also does real work: the client retries 5xx and connection
# failures, and without it a retried adjustment could double-apply.
ADJUST_MUTATION = """
mutation Move($input: InventoryAdjustQuantitiesInput!, $key: String!) {
  inventoryAdjustQuantities(input: $input) @idempotent(key: $key) {
    inventoryAdjustmentGroup { id createdAt reason }
    userErrors { field message code }
  }
}
"""


@dataclass
class Move:
    """One variant's pending move."""

    item_id: str
    label: str
    sku: str
    qty: int                      # how much to move = available at source
    from_available: int
    to_available: int
    needs_activation: bool
    skipped: str | None = None    # why this one won't move, if it won't
    failed: str | None = dc_field(default=None)
    # True when the failure was specifically a lost compare-and-swap, which is
    # the one kind worth re-running for -- the quantity simply moved on.
    stale: bool = dc_field(default=False)

    @property
    def new_from(self) -> int:
        return self.from_available - self.qty

    @property
    def new_to(self) -> int:
        return self.to_available + self.qty


def add_arguments(parser):
    parser.add_argument("--from", dest="from_location", required=True, metavar="LOC",
                        help="source location: name (e.g. Home) or id")
    parser.add_argument("--to", dest="to_location", required=True, metavar="LOC",
                        help="destination location: name or id")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true",
                       help="move every item that has stock at the source")
    group.add_argument("--pids", metavar="A,B,C",
                       help="comma-separated product ids (numeric, GID, or admin URL); "
                            "every variant of each product moves")
    parser.add_argument("--reason", default=DEFAULT_REASON,
                        help=f"adjustment reason recorded in the store's inventory "
                             f"history (default: {DEFAULT_REASON})")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="only consider the first N items (for trying things out)")
    parser.add_argument("--include-archived", action="store_true",
                        help="also move archived products (skipped by default; "
                             "an archived product keeps its inventory levels, so "
                             "it is visible to this scan and would otherwise move)")
    parser.add_argument("--no-activate", action="store_true",
                        help="skip activating items at the destination; items not "
                             "already stocked there will fail instead")


@task(
    name="move-inventory",
    help="Move stock between two locations",
    writes=True,
    scopes=["read_products", "read_locations", "read_inventory", "write_inventory"],
)
def run(ctx, args):
    """Move stock between two locations.

    Additive: the source drops to zero and the destination gains that amount.
    Items with nothing at the source are skipped. Uses compare-and-swap so a
    sale landing mid-run skips that item rather than corrupting its count.
    """
    locations = _load_locations(ctx)
    source = _resolve_location(locations, args.from_location, "--from")
    dest = _resolve_location(locations, args.to_location, "--to")

    if source["id"] == dest["id"]:
        ctx.fail(f"--from and --to are the same location ({source['name']}).")

    ctx.log.info("Moving %s -> %s", source["name"], dest["name"])

    if args.all:
        moves = _scan_all(ctx, source["id"], dest["id"], limit=args.limit,
                          include_archived=args.include_archived)
    else:
        moves = _scan_products(ctx, args.pids, source["id"], dest["id"],
                               limit=args.limit,
                               include_archived=args.include_archived)

    movable = [m for m in moves if not m.skipped]
    skipped = [m for m in moves if m.skipped]

    if not movable:
        _report_skips(ctx, skipped)
        ctx.log.info("Nothing to move.")
        if ctx.json_output:
            output.emit_json({"moved": [], "skipped": [_row(m) for m in skipped]})
        return

    _preview(ctx, movable, source, dest)

    # Moved stock stops being sellable online when the destination doesn't
    # fulfil online orders. Usually intended -- the goods are physically leaving
    # -- but never something to discover afterwards.
    if not dest["fulfillsOnlineOrders"]:
        ctx.log.warning(
            "%s does not fulfil online orders, so the moved stock will NOT be "
            "sellable on the storefront. (Location settings are not changed by "
            "this task.)", dest["name"],
        )

    if ctx.dry_run:
        for move in movable:
            ctx.mutate(ADJUST_MUTATION, None,
                       describe=f"move {move.qty} x {move.label}")
        _report_skips(ctx, skipped)
        return

    if not args.no_activate:
        _activate(ctx, movable, dest)

    _adjust(ctx, movable, source["id"], dest["id"], reason=args.reason)

    moved = [m for m in movable if not m.failed]
    failed = [m for m in movable if m.failed]

    ctx.log.info("Moved %d item(s), %d unit(s).",
                 len(moved), sum(m.qty for m in moved))
    _report_skips(ctx, skipped)
    if failed:
        ctx.log.warning("%d item(s) did not move:", len(failed))
        for move in failed:
            ctx.log.warning("   %s: %s", move.label, move.failed)
        # Only blame concurrency when concurrency was actually the problem --
        # an activation failure told to "re-run to pick them up" sends you
        # looking for a phantom sale instead of at the real error above.
        if any(m.stale for m in failed):
            ctx.log.warning(
                "Some quantities changed mid-run; re-running will pick those up.")
        else:
            ctx.log.warning("Fix the errors above, then re-run.")

    if ctx.json_output:
        output.emit_json({
            "from": source["name"], "to": dest["name"],
            "moved": [_row(m) for m in moved],
            "failed": [_row(m) | {"error": m.failed} for m in failed],
            "skipped": [_row(m) for m in skipped],
        })

    return 1 if failed else 0


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

def _load_locations(ctx) -> list[dict]:
    return ctx.query(LOCATIONS_QUERY)["locations"]["nodes"]


def _resolve_location(locations: list[dict], value: str, flag: str) -> dict:
    """Accept a location name, numeric id, or GID.

    Names are the useful form here -- they are what the admin shows and what a
    person remembers -- so match those case-insensitively, then fall back to ids.
    """
    wanted = value.strip()

    exact = [l for l in locations if l["name"].lower() == wanted.lower()]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise TaskError(
            f"{flag}: more than one location is named {wanted!r}. Use its id instead:\n"
            + "\n".join(f"  {gid.numeric(l['id'])}  {l['name']}" for l in exact)
        )

    if gid.is_gid(wanted) or wanted.isdigit():
        target = gid.coerce(wanted, "Location")
        for loc in locations:
            if loc["id"] == target:
                return loc

    # A unique prefix is enough; "Craft Fair" should find "Craft Fair Inventory".
    partial = [l for l in locations if wanted.lower() in l["name"].lower()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise TaskError(
            f"{flag}: {wanted!r} matches more than one location:\n"
            + "\n".join(f"  {l['name']}" for l in partial)
        )

    raise TaskError(
        f"{flag}: no location matches {wanted!r}. Configured locations:\n"
        + "\n".join(
            f"  {l['name']}" + ("" if l["isActive"] else "  (inactive)")
            for l in locations
        )
    )


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

# Shared with list-inventory and seed-dev so all three agree on what a missing
# level means; see shopifylib.inventory.
_available = inventory.available_at


def _scan_all(ctx, from_id: str, to_id: str, *, limit: int | None,
              include_archived: bool) -> list[Move]:
    progress = output.Progress("scanning source location")
    moves: list[Move] = []

    levels = ctx.paginate(
        SCAN_ALL_QUERY, "location.inventoryLevels",
        variables={"fromId": from_id, "toId": to_id},
        page_size=50,          # nested quantities + item make this query costly
        max_items=limit,
    )
    for level in levels:
        progress.tick()
        item = level["item"]
        moves.append(_build_move(
            item=item,
            from_available=_available(level),
            to_level=item.get("toLevel"),
            label=_label_from_item(item),
            status=((item.get("variant") or {}).get("product") or {}).get("status"),
            include_archived=include_archived,
        ))
    progress.done()
    return moves


def _scan_products(ctx, pids: str, from_id: str, to_id: str,
                   *, limit: int | None, include_archived: bool) -> list[Move]:
    ids = [gid.coerce(p, "Product") for p in pids.split(",") if p.strip()]
    if not ids:
        raise TaskError("--pids was empty.")

    moves: list[Move] = []
    # Chunked because each product drags in up to VARIANTS_PER_PRODUCT variants,
    # each with two nested inventory levels -- a big ask in one query.
    for chunk in _chunks(ids, 25):
        data = ctx.query(SCAN_PRODUCTS_QUERY, {
            "ids": chunk, "fromId": from_id, "toId": to_id,
            "variants": VARIANTS_PER_PRODUCT,
        })
        for requested, node in zip(chunk, data["nodes"]):
            if node is None:
                # nodes() returns null for an id that doesn't exist or isn't a
                # Product -- naming it beats a silent omission.
                moves.append(Move(
                    item_id="", label=f"product {gid.numeric(requested)}", sku="",
                    qty=0, from_available=0, to_available=0, needs_activation=False,
                    skipped="no such product (or not a product id)",
                ))
                continue

            variants = node["variants"]
            if variants["pageInfo"]["hasNextPage"]:
                ctx.log.warning(
                    "%r has more than %d variants; only the first %d were considered.",
                    node["title"], VARIANTS_PER_PRODUCT, VARIANTS_PER_PRODUCT,
                )
            for variant in variants["nodes"]:
                item = variant["inventoryItem"]
                moves.append(_build_move(
                    item=item,
                    from_available=_available(item.get("fromLevel")),
                    to_level=item.get("toLevel"),
                    label=_clean_label(variant.get("displayName") or node["title"]),
                    status=node.get("status"),
                    include_archived=include_archived,
                ))

    return moves[:limit] if limit else moves


def _build_move(*, item: dict, from_available: int, to_level: dict | None,
                label: str, status: str | None = None,
                include_archived: bool = True) -> Move:
    move = Move(
        item_id=item["id"],
        label=label,
        sku=item.get("sku") or "",
        qty=max(0, from_available),
        from_available=from_available,
        to_available=_available(to_level),
        # A null level means the item was never activated at the destination;
        # adjusting it would fail, so it needs inventoryActivate first.
        needs_activation=to_level is None,
    )
    # Archived first: an archived product with stock is the case worth naming,
    # and reporting it as "nothing at source" would hide why it was left behind.
    if not include_archived and inventory.is_archived(status):
        move.skipped = "product archived"
    elif not item.get("tracked"):
        move.skipped = "inventory not tracked"
    elif from_available <= 0:
        move.skipped = "nothing at source"
    return move


_clean_label = inventory.clean_label


def _label_from_item(item: dict) -> str:
    variant = item.get("variant") or {}
    if variant.get("displayName"):
        return _clean_label(variant["displayName"])
    product = variant.get("product") or {}
    return product.get("title") or item["id"]


def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _activate(ctx, moves: list[Move], dest: dict) -> None:
    """Stock each item at the destination so its quantity isn't stranded."""
    pending = [m for m in moves if m.needs_activation]
    if not pending:
        return

    ctx.log.info("Activating %d item(s) at %s", len(pending), dest["name"])
    for move in pending:
        try:
            ctx.mutate(
                ACTIVATE_MUTATION,
                {"inventoryItemId": move.item_id, "locationId": dest["id"],
                 "key": str(uuid.uuid4())},
                field="inventoryActivate",
                describe=f"activate {move.label} at {dest['name']}",
            )
        except Exception as exc:
            # One unactivatable item shouldn't stop the rest of the move.
            move.failed = f"could not activate at {dest['name']}: {exc}"


def _adjust(ctx, moves: list[Move], from_id: str, to_id: str, *, reason: str) -> None:
    """Apply the paired deltas, dropping entries that lost a compare-and-swap."""
    todo = [m for m in moves if not m.failed]
    progress = output.Progress("moving", total=len(todo))

    for batch in _chunks(todo, BATCH_SIZE):
        _adjust_batch(ctx, list(batch), from_id, to_id, reason=reason)
        progress.tick(len(batch))
    progress.done()


def _adjust_batch(ctx, batch: list[Move], from_id: str, to_id: str,
                  *, reason: str, attempt: int = 1) -> None:
    """One adjustment call, retried without whichever entries Shopify rejected.

    A compare-and-swap failure fails the whole mutation, not just its entry, so
    "skip that item and continue" means identifying the rejected entries from
    the userError field paths and resending the remainder. Bounded so a
    persistently-failing batch can't loop.
    """
    if not batch:
        return

    changes = []
    for move in batch:
        # Order matters: the two changes per item are adjacent, so a userError
        # index maps back to an item by integer division.
        changes.append({
            "inventoryItemId": move.item_id, "locationId": from_id,
            "delta": -move.qty, "changeFromQuantity": move.from_available,
        })
        changes.append({
            "inventoryItemId": move.item_id, "locationId": to_id,
            "delta": move.qty, "changeFromQuantity": move.to_available,
        })

    payload = ctx.mutate(
        ADJUST_MUTATION,
        {
            "input": {"reason": reason, "name": STATE, "changes": changes},
            # A fresh key per attempt: the payload differs once entries are
            # dropped, and reusing a key across different payloads would make
            # Shopify return the first attempt's result.
            "key": str(uuid.uuid4()),
        },
        field="inventoryAdjustQuantities",
        describe=f"move {len(batch)} item(s)",
        # This call owns its userErrors -- a rejected entry is an expected
        # outcome here, handled below rather than raised.
        check_errors=False,
    )

    errors = payload.get("userErrors") or []
    if not errors:
        return

    bad = _failed_indices(errors, len(changes))
    if not bad or attempt > 3:
        # Couldn't attribute the failure, or we've retried enough: fail the
        # whole batch rather than guess or spin.
        message = "; ".join(e.get("message", "?") for e in errors)
        for move in batch:
            move.failed = message
        return

    for index in sorted(bad):
        move = batch[index]
        move.failed, move.stale = _error_for(errors, index)

    remaining = [m for i, m in enumerate(batch) if i not in bad]
    if remaining:
        _adjust_batch(ctx, remaining, from_id, to_id,
                      reason=reason, attempt=attempt + 1)


def _failed_indices(errors: list[dict], n_changes: int) -> set[int]:
    """Map userError field paths back to item indices.

    Shopify points at the offending entry with a path like
    ``["input", "changes", "3", "changeFromQuantity"]``. Changes are emitted two
    per item, so index 3 is item 1.
    """
    found: set[int] = set()
    for err in errors:
        for part in err.get("field") or []:
            text = str(part)
            if text.isdigit():
                position = int(text)
                if 0 <= position < n_changes:
                    found.add(position // 2)
                break
    return found


def _error_for(errors: list[dict], item_index: int) -> tuple[str, bool]:
    """(message, was-it-a-stale-quantity) for one item, else all the messages.

    The staleness flag comes from the error code rather than the prose: the
    message reads "The changeFromQuantity argument no longer matches the
    persisted quantity", which contains none of the words you would grep for.
    """
    for err in errors:
        for part in err.get("field") or []:
            if str(part).isdigit() and int(str(part)) // 2 == item_index:
                return (err.get("message", "rejected"),
                        err.get("code") == STALE_CODE)
    return "; ".join(e.get("message", "?") for e in errors), False


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _row(move: Move) -> dict:
    return {
        "inventory_item_id": move.item_id,
        "label": move.label,
        "sku": move.sku,
        "qty": move.qty,
        "from_before": move.from_available, "from_after": move.new_from,
        "to_before": move.to_available, "to_after": move.new_to,
        "activated": move.needs_activation,
        "skipped": move.skipped,
    }


def _preview(ctx, moves: list[Move], source: dict, dest: dict) -> None:
    if ctx.json_output:
        return
    show_sku = any(m.sku for m in moves)
    show_activate = any(m.needs_activation for m in moves)
    rows = []
    for m in moves:
        row = {"item": m.label[:58], "move": m.qty}
        if show_sku:
            row["sku"] = m.sku[:16]
        row[source["name"][:16]] = f"{m.from_available} -> {m.new_from}"
        row[dest["name"][:16]] = f"{m.to_available} -> {m.new_to}"
        if show_activate:
            row["activate"] = "yes" if m.needs_activation else ""
        rows.append(row)
    print(output.format_table(rows))
    print(f"\n{len(moves)} item(s), {sum(m.qty for m in moves)} unit(s)"
          f" — {source['name']} -> {dest['name']}\n")


def _report_skips(ctx, skipped: list[Move]) -> None:
    if not skipped:
        return
    reasons: dict[str, int] = {}
    for move in skipped:
        reasons[move.skipped] = reasons.get(move.skipped, 0) + 1
    ctx.log.info("Skipped %d item(s): %s", len(skipped),
                 ", ".join(f"{count} {reason}" for reason, count in sorted(reasons.items())))
