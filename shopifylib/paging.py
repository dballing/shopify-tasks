#!/usr/bin/env python3
"""Cursor pagination over GraphQL connections.

Every list in the Admin API is a Relay connection: a page of ``edges`` plus a
``pageInfo`` saying whether more exist and where to resume. Walking one by hand
is four lines of boilerplate and one easy off-by-one (forgetting to thread the
cursor gives you page one forever, in an infinite loop that looks like progress).

The query supplies the cursor variables; this walks them::

    QUERY = '''
    query Products($first: Int!, $after: String) {
      products(first: $first, after: $after) {
        edges { node { id title } }
        pageInfo { hasNextPage endCursor }
      }
    }
    '''

    for product in paginate(client, QUERY, "products", page_size=100):
        ...

``page_size`` is an explicit argument rather than a constant because query cost
scales with it, and every plan caps a *single* query at 1000 points. A nested
connection multiplies: ``products(first: 250) { variants(first: 100) }`` is the
usual way to blow that cap and get a hard error rather than a slow response.
"""
from __future__ import annotations

import logging
from typing import Any, Iterator

from .errors import GraphQLError

log = logging.getLogger(__name__)

# Shopify's hard ceiling on `first:` for any connection. Asking for more is a
# document-level error, not a silent clamp.
MAX_PAGE_SIZE = 250

DEFAULT_PAGE_SIZE = 100


def paginate(
    client,
    document: str,
    connection_path: str,
    *,
    variables: dict[str, Any] | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    first_var: str = "first",
    after_var: str = "after",
    operation_name: str | None = None,
    max_items: int | None = None,
) -> Iterator[dict]:
    """Yield every node of a connection, following cursors until exhausted.

    ``connection_path`` is a dotted path locating the connection inside the
    response data -- ``"products"`` for a top-level one, ``"product.variants"``
    for a nested one.

    ``max_items`` stops early; useful for a ``--limit`` flag while developing a
    task against a large catalog, so you are not waiting on 40k products to
    check whether your field selection is right.
    """
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ValueError(
            f"page_size must be between 1 and {MAX_PAGE_SIZE} (Shopify's per-connection "
            f"cap), got {page_size}"
        )

    variables = dict(variables or {})
    cursor: str | None = None
    yielded = 0
    page = 0

    while True:
        variables[first_var] = page_size
        variables[after_var] = cursor

        data = client.execute(document, variables, operation_name=operation_name)
        connection = _resolve(data, connection_path, document)
        page += 1

        for node in _nodes(connection):
            yield node
            yielded += 1
            if max_items is not None and yielded >= max_items:
                log.debug("paginate(%s) stopped at max_items=%d", connection_path, max_items)
                return

        page_info = connection.get("pageInfo") or {}
        if not page_info:
            # A query that selects no pageInfo can only ever return its first
            # page. Silently stopping would look like a complete result set, so
            # say so -- this is a query bug, and a quiet one.
            raise GraphQLError(
                [{"message": (
                    f"Connection {connection_path!r} has no pageInfo, so pagination "
                    f"cannot continue past the first page. Add "
                    f"'pageInfo {{ hasNextPage endCursor }}' to the query."
                ), "path": connection_path.split(".")}],
                query=document,
            )

        if not page_info.get("hasNextPage"):
            log.debug("paginate(%s) finished: %d items over %d page(s)",
                      connection_path, yielded, page)
            return

        next_cursor = page_info.get("endCursor")
        if not next_cursor or next_cursor == cursor:
            # hasNextPage true with no usable cursor would loop forever on the
            # same page. Stop loudly instead of spinning.
            raise GraphQLError(
                [{"message": (
                    f"Connection {connection_path!r} reports hasNextPage but returned "
                    f"no new endCursor; refusing to loop."
                ), "path": connection_path.split(".")}],
                query=document,
            )
        cursor = next_cursor


def _resolve(data: dict, path: str, document: str) -> dict:
    """Walk a dotted path to the connection object."""
    node: Any = data
    walked: list[str] = []
    for part in path.split("."):
        walked.append(part)
        if not isinstance(node, dict) or part not in node:
            available = ", ".join(node.keys()) if isinstance(node, dict) else "(not an object)"
            raise GraphQLError(
                [{"message": (
                    f"No connection at {'.'.join(walked)!r} in the response. "
                    f"Available at that level: {available}"
                ), "path": walked}],
                query=document,
            )
        node = node[part]
        if node is None:
            raise GraphQLError(
                [{"message": (
                    f"{'.'.join(walked)!r} is null -- the parent object probably does "
                    f"not exist (check the id you passed)."
                ), "path": walked}],
                query=document,
            )
    if not isinstance(node, dict):
        raise GraphQLError(
            [{"message": f"{path!r} is not a connection object.", "path": path.split(".")}],
            query=document,
        )
    return node


def _nodes(connection: dict) -> Iterator[dict]:
    """Yield nodes from either connection style.

    Shopify supports both ``edges { node { ... } }`` and the shorthand
    ``nodes { ... }``. The shorthand is cheaper to write and read; edges are
    needed when you want per-edge cursors. Accept either so a task can use
    whichever suits, without this helper caring.
    """
    if "nodes" in connection:
        yield from (n for n in (connection.get("nodes") or []) if n is not None)
        return
    for edge in connection.get("edges") or []:
        if edge and edge.get("node") is not None:
            yield edge["node"]
