#!/usr/bin/env python3
"""list-on-sale: what counts as on sale, and the flags that shape the scan."""
from __future__ import annotations

import json

from tasks import list_on_sale as los
from tests.conftest import gql
from tests.test_pricing import make_ctx, product_node, variant


def page(*nodes):
    return gql({"products": {"nodes": list(nodes),
                             "pageInfo": {"hasNextPage": False, "endCursor": None}}})


def args(**kw):
    defaults = dict(include_archived=False, limit=None)
    defaults.update(kw)
    return type("A", (), defaults)()


CATALOGUE = [
    product_node(variant(1, price="15.00", compare_at="20.00", name="Coral"),
                 title="Coral", pid=1),
    product_node(variant(1, price="20.00", name="Plain"), title="Plain", pid=2),
    product_node(variant(1, price="20.00", compare_at="18.00", name="Leftover"),
                 title="Leftover", pid=3),
    product_node(variant(1, price="10.00", compare_at="12.00", name="Amber"),
                 variant(2, price="10.00", name="Amber"), title="Amber", pid=4),
]


def test_only_marked_down_variants_are_listed(make_client, capsys):
    los.run(make_ctx(make_client([page(*CATALOGUE)])), args())
    out = capsys.readouterr().out
    assert "Coral" in out and "20.00" in out and "15.00" in out
    assert "Plain" not in out
    # Per-variant: Amber's first variant is on sale, its second is not.
    assert "Amber" in out and "Amber - V2" not in out
    assert "2 variant(s) on sale" in out


def table(capsys) -> list[list[str]]:
    lines = capsys.readouterr().out.split("\n\n")[0].splitlines()
    return [line.split() for line in lines]


def test_rows_lead_with_the_pid_and_are_sorted(make_client, capsys):
    los.run(make_ctx(make_client([page(*CATALOGUE)])), args())
    header, _, *rows = table(capsys)
    assert header == ["pid", "product", "regular", "price", "sale", "price"]
    # The bare numeric id, ready to paste into --pid.
    assert [r[:2] for r in rows] == [["4", "Amber"], ["1", "Coral"], ["3", "Leftover"]]


def test_stale_compare_ats_are_listed_and_marked(make_client, capsys):
    """Not a sale, so marked rather than shown as one -- but listed, since
    they are the rows that need someone to look at them."""
    los.run(make_ctx(make_client([page(*CATALOGUE)])), args())
    out = capsys.readouterr().out
    leftover = next(line for line in out.splitlines() if "Leftover" in line)
    assert leftover.split()[-2:] == ["18.00", "20.00*"]
    coral = next(line for line in out.splitlines() if "Coral" in line)
    assert not coral.rstrip().endswith("*")
    assert "1 variant(s) marked *" in out
    assert "2 variant(s) on sale" in out, "stale rows are not counted as on sale"


def test_only_stale_still_prints_the_table(make_client, capsys):
    los.run(make_ctx(make_client([page(CATALOGUE[2])])), args())
    out = capsys.readouterr().out
    assert "nothing on sale" not in out
    assert "20.00*" in out


def test_long_names_are_not_truncated(make_client, capsys):
    name = "Extraordinarily Long Handmade Lampwork Glass Bead With A Very Descriptive Name"
    node = product_node(variant(1, price="5.00", compare_at="8.00", name=name),
                        title=name)
    los.run(make_ctx(make_client([page(node)])), args())
    assert name in capsys.readouterr().out


def test_archived_are_excluded_server_side_by_default(make_client):
    client = make_client([page(), page()])
    los.run(make_ctx(client), args())
    los.run(make_ctx(client), args(include_archived=True))
    assert client.session.variables[0]["query"] == "-status:archived"
    assert client.session.variables[1]["query"] is None


def test_json_shape(make_client, capsys):
    los.run(make_ctx(make_client([page(*CATALOGUE)]), json_output=True), args())
    payload = json.loads(capsys.readouterr().out)
    assert payload["on_sale"][1] == {
        "product_id": "gid://shopify/Product/1",
        "variant_id": "gid://shopify/ProductVariant/1",
        "product": "Coral", "regular_price": "20.00", "sale_price": "15.00"}
    assert [r["product"] for r in payload["stale"]] == ["Leftover"]


def test_task_is_read_only():
    from shopifylib.task import REGISTRY
    assert REGISTRY["list-on-sale"].writes is False
