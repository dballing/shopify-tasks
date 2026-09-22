#!/usr/bin/env python3
"""Cursor pagination.

The important assertions are about cursor *threading*: a paginator that ignores
endCursor returns page one forever, which looks like a working infinite loop
rather than a bug.
"""
from __future__ import annotations

import pytest

from shopifylib.errors import GraphQLError
from shopifylib.paging import MAX_PAGE_SIZE, paginate
from tests.conftest import gql

QUERY = """
query Products($first: Int!, $after: String) {
  products(first: $first, after: $after) {
    edges { node { id } }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def page(ids, *, has_next, cursor=None, key="products"):
    return gql({key: {
        "edges": [{"node": {"id": i}} for i in ids],
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
    }})


def test_walks_every_page(make_client):
    client = make_client([
        page([1, 2], has_next=True, cursor="c1"),
        page([3, 4], has_next=True, cursor="c2"),
        page([5], has_next=False),
    ])
    assert [n["id"] for n in paginate(client, QUERY, "products", page_size=2)] == [1, 2, 3, 4, 5]


def test_threads_the_cursor(make_client):
    """Each request after the first must carry the previous page's endCursor."""
    client = make_client([
        page([1], has_next=True, cursor="c1"),
        page([2], has_next=True, cursor="c2"),
        page([3], has_next=False),
    ])
    list(paginate(client, QUERY, "products", page_size=1))
    assert [v["after"] for v in client.session.variables] == [None, "c1", "c2"]
    assert all(v["first"] == 1 for v in client.session.variables)


def test_stops_without_an_extra_request(make_client):
    """hasNextPage false must end the walk, not trigger one more call."""
    client = make_client([page([1], has_next=False)])
    list(paginate(client, QUERY, "products"))
    assert len(client.session.requests) == 1


def test_nodes_shorthand_is_supported(make_client):
    client = make_client([gql({"products": {
        "nodes": [{"id": 1}, {"id": 2}],
        "pageInfo": {"hasNextPage": False},
    }})])
    assert len(list(paginate(client, QUERY, "products"))) == 2


def test_dotted_path_resolves_a_nested_connection(make_client):
    client = make_client([gql({"product": {"variants": {
        "nodes": [{"id": "v1"}],
        "pageInfo": {"hasNextPage": False},
    }}})])
    got = list(paginate(client, QUERY, "product.variants"))
    assert [n["id"] for n in got] == ["v1"]


def test_max_items_stops_early(make_client):
    client = make_client([page([1, 2, 3], has_next=True, cursor="c1")])
    got = list(paginate(client, QUERY, "products", page_size=3, max_items=2))
    assert [n["id"] for n in got] == [1, 2]
    assert len(client.session.requests) == 1


def test_extra_variables_are_preserved(make_client):
    client = make_client([page([1], has_next=False)])
    list(paginate(client, QUERY, "products", variables={"query": "status:active"}))
    assert client.session.variables[0]["query"] == "status:active"


def test_null_nodes_are_skipped(make_client):
    """Shopify nulls out nodes the token cannot read rather than omitting them."""
    client = make_client([gql({"products": {
        "edges": [{"node": {"id": 1}}, {"node": None}, {"node": {"id": 2}}],
        "pageInfo": {"hasNextPage": False},
    }})])
    assert [n["id"] for n in paginate(client, QUERY, "products")] == [1, 2]


# -- failure modes ---------------------------------------------------------

def test_missing_page_info_is_loud(make_client):
    """Silently returning one page would look like a complete result set."""
    client = make_client([gql({"products": {"edges": [{"node": {"id": 1}}]}})])
    with pytest.raises(GraphQLError, match="pageInfo"):
        list(paginate(client, QUERY, "products"))


def test_has_next_without_a_cursor_refuses_to_loop(make_client):
    client = make_client([page([1], has_next=True, cursor=None)])
    with pytest.raises(GraphQLError, match="refusing to loop"):
        list(paginate(client, QUERY, "products"))


def test_repeated_cursor_refuses_to_loop(make_client):
    client = make_client([
        page([1], has_next=True, cursor="c1"),
        page([2], has_next=True, cursor="c1"),
    ])
    with pytest.raises(GraphQLError, match="refusing to loop"):
        list(paginate(client, QUERY, "products"))


def test_bad_connection_path_lists_what_was_available(make_client):
    client = make_client([gql({"products": {"nodes": [], "pageInfo": {}}})])
    with pytest.raises(GraphQLError, match="products"):
        list(paginate(client, QUERY, "widgets"))


def test_null_parent_is_diagnosed(make_client):
    """A bad id gives a null parent; say so rather than 'not a connection'."""
    client = make_client([gql({"product": None})])
    with pytest.raises(GraphQLError, match="check the id"):
        list(paginate(client, QUERY, "product.variants"))


@pytest.mark.parametrize("size", [0, -1, MAX_PAGE_SIZE + 1])
def test_page_size_is_bounds_checked(make_client, size):
    """Shopify hard-errors above 250; catching it locally is a clearer message."""
    client = make_client([])
    with pytest.raises(ValueError, match="page_size"):
        list(paginate(client, QUERY, "products", page_size=size))
