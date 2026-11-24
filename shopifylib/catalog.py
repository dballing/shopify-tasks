#!/usr/bin/env python3
"""Shared vocabulary for describing a product: its category, its sales
channels, and its description.

new-item is the first task to set these, but they are store-wide ideas rather
than one task's details -- a later edit-item or a catalogue report would need
exactly the same name resolution -- so they live here rather than inline.
"""
from __future__ import annotations

import html
import re

from . import gid
from .errors import TaskError

# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

# Categories come from Shopify's standard product taxonomy, which has thousands
# of entries. This store uses four. Matching a typed name against the whole
# taxonomy would make "necklace" ambiguous across dozens of leaves; matching
# against what the catalogue already uses makes it exact. Hence "in use" is
# read from products, not from the taxonomy.
#
# Flat: one connection, no nesting, so a large page is cheap.
CATEGORIES_QUERY = """
query CategoriesInUse($first: Int!, $after: String) {
  products(first: $first, after: $after) {
    nodes { category { id name fullName } }
    pageInfo { hasNextPage endCursor }
  }
}
"""

CATEGORY_PAGE = 250

# Taxonomy ids look like "aa-6-6" (Apparel & Accessories > Jewelry > Earrings).
_SHORT_TAXONOMY_ID = re.compile(r"^[a-z]{2}(-\d+)*$")


def load_categories(ctx) -> list[dict]:
    """Every distinct category some product in this store uses, sorted by name."""
    seen: dict[str, dict] = {}
    for node in ctx.paginate(CATEGORIES_QUERY, "products", page_size=CATEGORY_PAGE):
        category = node.get("category")
        if category and category["id"] not in seen:
            seen[category["id"]] = category
    return sorted(seen.values(), key=lambda c: c["name"].lower())


def resolve_category(categories: list[dict], value: str) -> dict:
    """A category the catalogue already uses, by name, or any taxonomy id.

    Names are what a person types, and match case-insensitively, then by unique
    substring, so "necklace" finds "Necklaces". An id (``aa-6-6`` or the full
    GID) is accepted even when no product uses it yet. That covers a dev store,
    whose products have no categories at all, and a first item in a new
    category. Shopify validates the id when the product is written.
    """
    wanted = value.strip()
    lowered = wanted.lower()

    exact = [c for c in categories if c["name"].lower() == lowered]
    if len(exact) == 1:
        return exact[0]

    if wanted.startswith("gid://shopify/TaxonomyCategory/") or _SHORT_TAXONOMY_ID.match(wanted):
        target = wanted if wanted.startswith("gid://") else \
            f"gid://shopify/TaxonomyCategory/{wanted}"
        for category in categories:
            if category["id"] == target:
                return category
        return {"id": target, "name": wanted, "fullName": None}

    partial = [c for c in categories if lowered in c["name"].lower()]
    if len(partial) == 1:
        return partial[0]

    choices = "\n".join(f"  {c['name']:<12} {c['fullName']}" for c in categories) \
        or "  (no product in this store has a category yet)"
    if len(partial) > 1:
        raise TaskError(f"--category {wanted!r} matches more than one category:\n"
                        + "\n".join(f"  {c['name']}" for c in partial))
    raise TaskError(
        f"--category {wanted!r} is not a category this store uses. In use:\n"
        f"{choices}\n"
        f"To use a different one, give its taxonomy id (e.g. aa-6-6).")


def category_label(category: dict) -> str:
    return category.get("fullName") or category["name"]


# ---------------------------------------------------------------------------
# Sales channels
# ---------------------------------------------------------------------------

# A sales channel is published to through its *publication*. Naming one is
# less obvious than it looks:
#   * Publication.name is deprecated, pointing at Catalog.title instead;
#   * but Catalog.title reads "Channel Catalog 299090280739 for Point of Sale"
#     (seen live on dev and prod), which nobody would type;
#   * the publication's Channel carries the name the admin shows ("Point of
#     Sale", "Shopify GraphiQL App"). Each publication had exactly one.
# So the channel name is used, falling back to the catalog title.
#
# Needs read_publications, which a store authorised before new-item existed
# does not have -- see CLAUDE.md on re-running auth after scopes change.
PUBLICATIONS_QUERY = """
query Channels {
  publications(first: 50) {
    nodes {
      id
      catalog { title }
      channels(first: 1) { nodes { name } }
    }
  }
}
"""

PUBLISH_MUTATION = """
mutation PublishProduct($id: ID!, $input: [PublicationInput!]!) {
  publishablePublish(id: $id, input: $input) {
    userErrors { field message }
  }
}
"""


def load_channels(ctx) -> list[dict]:
    """``[{id, name}]`` for every publication, named as the admin names it."""
    result = []
    for n in ctx.query(PUBLICATIONS_QUERY)["publications"]["nodes"]:
        channels = (n.get("channels") or {}).get("nodes") or []
        name = (channels[0]["name"] if channels else None) \
            or (n.get("catalog") or {}).get("title") or gid.numeric(n["id"])
        result.append({"id": n["id"], "name": name})
    return result


def resolve_channels(channels: list[dict], wanted: list[str]) -> tuple[list[dict], list[str]]:
    """(matched channels, problems). Problems are collected, not raised, so a
    caller can report them alongside every other problem in one go."""
    matched, problems = [], []
    for value in wanted:
        lowered = value.strip().lower()
        hits = [c for c in channels if c["name"].lower() == lowered] or \
               [c for c in channels if lowered in c["name"].lower()]
        if len(hits) == 1:
            if hits[0] not in matched:
                matched.append(hits[0])
        elif hits:
            problems.append(f"channel {value!r} matches more than one: "
                            + ", ".join(c["name"] for c in hits))
        else:
            problems.append(f"no sales channel matches {value!r}. Available: "
                            + ", ".join(sorted(c["name"] for c in channels)))
    return matched, problems


# ---------------------------------------------------------------------------
# Descriptions
# ---------------------------------------------------------------------------

def description_html(text: str | None) -> str:
    """Plain text as the HTML Shopify stores in descriptionHtml.

    Escaped, because "Silver & opal <3" typed on a command line is prose, not
    markup. A blank line starts a paragraph and a single newline is a line
    break, which is how the admin's own editor renders typed text.
    """
    if not text or not text.strip():
        return ""
    paragraphs = re.split(r"\n\s*\n", text.strip())
    return "".join(
        "<p>" + "<br>".join(html.escape(line.strip()) for line in p.splitlines()) + "</p>"
        for p in paragraphs)
