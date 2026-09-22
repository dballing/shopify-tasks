#!/usr/bin/env python3
"""Static guard against queries that will blow the per-query cost cap.

Shopify caps a *single* query at 1000 points on every plan -- Plus buys a
deeper bucket, not bigger queries -- and nested connection costs multiply. This
repo has shipped that mistake twice:

    list-inventory       products -> variants -> inventoryLevels   1007 points
    seed-dev --clone-from  products -> variants -> inventoryLevels   1112 points

Both were documented in CLAUDE.md and paging.py before the second one shipped,
which is the point: a rule nothing checks is a rule that gets broken. This test
checks it, hermetically and at commit time, instead of against a live store.

It is deliberately crude. It counts connection-shaped selections by nesting
depth in every GraphQL document defined in the package, and fails on three or
more. That will occasionally be wrong -- a shallow third connection can be
perfectly affordable -- so the fix when it fires is to look at the real cost in
`extensions.cost` and add an explicit exemption with the measured number, not
to delete the test.
"""
from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

# A connection selection, anchored to end right before the brace it opens:
# `name(... first: N ...)`. The `first:` argument is what distinguishes a
# paginated connection from a plain object field.
#
# The negative lookbehind is load-bearing: an operation signature like
# `query Foo($first: Int!, $after: String) {` also contains "first:", and
# without it every document counted its own signature as an outer connection
# and reported one level too many.
_CONNECTION_BEFORE_BRACE = re.compile(
    r'\w+\s*\([^()]*(?<![$\w])first\s*:[^()]*\)\s*$')

MAX_NESTED_CONNECTIONS = 2

# Documents allowed to exceed the limit, each with the measured cost that
# justifies it. Add to this only after seeing the real number in a response's
# extensions.cost -- never to silence the test.
EXEMPT: dict[str, str] = {}


def _documents() -> list[tuple[str, str, str]]:
    """(file, constant name, document) for every GraphQL string in the repo."""
    found = []
    for path in sorted((REPO / "tasks").glob("*.py")) + \
            sorted((REPO / "shopifylib").glob("*.py")):
        text = path.read_text()
        # Module-level triple-quoted constants: NAME = """...""".
        for match in re.finditer(r'^(\w+)\s*=\s*"""(.*?)"""', text,
                                 re.S | re.M):
            name, body = match.group(1), match.group(2)
            if "query " in body or "mutation " in body or body.strip().startswith("{"):
                found.append((path.name, name, body))
    return found


def _max_nesting(document: str) -> int:
    """Deepest run of connections nested inside one another.

    Walks brace depth, recording for each open brace whether the field that
    opened it was a connection. The answer is the largest number of connection
    braces open at once -- sibling connections don't stack, only nesting does.
    """
    stack: list[bool] = []
    deepest = 0
    for index, char in enumerate(document):
        if char == "{":
            preceding = document[max(0, index - 200):index]
            stack.append(bool(_CONNECTION_BEFORE_BRACE.search(preceding)))
            deepest = max(deepest, sum(stack))
        elif char == "}" and stack:
            stack.pop()
    return deepest


def test_the_detector_finds_known_bad_shapes():
    """The shape that cost 1112 points, so the guard is known to work."""
    bad = """
    query CloneSource($first: Int!, $after: String) {
      products(first: $first, after: $after) {
        nodes {
          variants(first: 100) {
            nodes {
              inventoryItem {
                inventoryLevels(first: 20) { nodes { location { name } } }
              }
            }
          }
        }
      }
    }
    """
    assert _max_nesting(bad) == 3


def test_the_detector_accepts_the_flattened_shape():
    ok = """
    query ListInventory($first: Int!, $after: String, $variants: Int!) {
      products(first: $first, after: $after) {
        nodes { variants(first: $variants) { nodes { inventoryItem { id } } } }
      }
    }
    """
    assert _max_nesting(ok) == 2


def test_the_detector_ignores_sibling_connections():
    """Two connections side by side don't multiply; only nesting does."""
    siblings = """
    { a(first: 10) { nodes { x } } b(first: 10) { nodes { y } } }
    """
    assert _max_nesting(siblings) == 1


def test_documents_are_actually_discovered():
    """A guard that silently finds nothing would pass forever."""
    docs = _documents()
    assert len(docs) > 10
    assert any(name == "SCAN_ALL_QUERY" for _, name, _ in docs)


@pytest.mark.parametrize("path,name,document",
                         _documents(), ids=lambda v: v if isinstance(v, str) else "")
def test_no_query_nests_too_many_connections(path, name, document):
    key = f"{path}:{name}"
    depth = _max_nesting(document)
    if key in EXEMPT:
        pytest.skip(f"exempt: {EXEMPT[key]}")
    assert depth <= MAX_NESTED_CONNECTIONS, (
        f"{key} nests {depth} connections. Shopify caps a single query at 1000 "
        f"points on every plan and nested connection costs multiply -- this shape "
        f"has failed twice in this repo (1007 and 1112 points).\n"
        f"Read the inner connection separately and join locally, as "
        f"shopifylib.inventory.read_levels does. If this query is genuinely "
        f"affordable, measure it in extensions.cost and add it to EXEMPT with "
        f"the number."
    )
