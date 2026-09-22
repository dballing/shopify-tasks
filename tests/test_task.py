#!/usr/bin/env python3
"""The task framework: registry, dry-run semantics, and GID helpers."""
from __future__ import annotations

import logging

import pytest

from shopifylib import gid
from shopifylib.errors import TaskError, WriteBlocked
from shopifylib.task import DRY_RUN, REGISTRY, TaskContext, TaskSpec, task
from tests.conftest import gql

WRITE = "mutation Update($id: ID!) { productUpdate(input: {id: $id}) { userErrors { field message } } }"


@pytest.fixture
def clean_registry():
    """@task registers globally; keep tests from leaking into each other."""
    saved = dict(REGISTRY)
    REGISTRY.clear()
    yield REGISTRY
    REGISTRY.clear()
    REGISTRY.update(saved)


def make_ctx(client, *, dry_run: bool) -> TaskContext:
    return TaskContext(
        client=client,
        shop=client.shop,
        dry_run=dry_run,
        log=logging.getLogger("test"),
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_task_registers_its_metadata(clean_registry):
    @task(name="demo", help="A demo", writes=True, scopes=["write_products"])
    def run(ctx, args):
        """Long description."""

    spec = clean_registry["demo"]
    assert (spec.writes, spec.scopes) == (True, ["write_products"])
    assert spec.description == "Long description."


def test_read_only_is_the_default(clean_registry):
    @task(name="demo", help="A demo")
    def run(ctx, args):
        pass

    assert clean_registry["demo"].writes is False


def test_duplicate_task_names_are_rejected(clean_registry):
    @task(name="demo", help="first")
    def run_a(ctx, args):
        pass

    with pytest.raises(RuntimeError, match="unique"):
        @task(name="demo", help="second")
        def run_b(ctx, args):
            pass


def test_description_falls_back_to_help():
    spec = TaskSpec(name="x", help="Short help", run=lambda ctx, args: None)
    assert spec.description == "Short help"


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def test_dry_run_sends_nothing(make_client):
    ctx = make_ctx(make_client([], writes_enabled=False), dry_run=True)
    result = ctx.mutate(WRITE, {"id": "gid://shopify/Product/1"},
                        field="productUpdate", describe="archive 'Blue Widget'")
    assert result is DRY_RUN
    assert ctx.client.session.requests == []


def test_dry_run_result_is_falsy(make_client):
    """So `if ctx.mutate(...)` reads as 'did it actually happen'."""
    ctx = make_ctx(make_client([], writes_enabled=False), dry_run=True)
    assert not ctx.mutate(WRITE, field="productUpdate")


def test_dry_run_logs_what_it_would_do(make_client, caplog):
    ctx = make_ctx(make_client([], writes_enabled=False), dry_run=True)
    with caplog.at_level(logging.INFO):
        ctx.mutate(WRITE, field="productUpdate", describe="archive 'Blue Widget'")
    assert "DRY RUN would archive 'Blue Widget'" in caplog.text


def test_dry_run_counts_intended_writes(make_client):
    """A dry run's count should be directly comparable to the real run's."""
    ctx = make_ctx(make_client([], writes_enabled=False), dry_run=True)
    for _ in range(3):
        ctx.mutate(WRITE, field="productUpdate")
    assert (ctx.writes_skipped, ctx.writes_performed) == (3, 0)


def test_committed_run_writes_and_counts(make_client):
    client = make_client([gql({"productUpdate": {"userErrors": []}})], writes_enabled=True)
    ctx = make_ctx(client, dry_run=False)
    ctx.mutate(WRITE, field="productUpdate")
    assert (ctx.writes_performed, ctx.writes_skipped) == (1, 0)
    assert len(client.session.requests) == 1


def test_reads_still_work_in_a_dry_run(make_client):
    """Dry run means 'change nothing', not 'do nothing' -- the preview needs reads."""
    ctx = make_ctx(make_client([gql({"shop": {"name": "Acme"}})]), dry_run=True)
    assert ctx.query("query { shop { name } }")["shop"]["name"] == "Acme"


def test_a_task_bypassing_ctx_mutate_still_cannot_write(make_client):
    """The guard is what makes dry-run structural rather than a convention."""
    ctx = make_ctx(make_client([gql({})], writes_enabled=False), dry_run=True)
    with pytest.raises(WriteBlocked):
        ctx.client.mutate(WRITE, field="productUpdate")


def test_summary_reports_the_run(make_client):
    client = make_client([gql({"productUpdate": {"userErrors": []}})], writes_enabled=True)
    ctx = make_ctx(client, dry_run=False)
    ctx.mutate(WRITE, field="productUpdate")
    summary = ctx.summary()
    assert summary["writes_performed"] == 1
    assert summary["api_requests"] == 1
    assert summary["dry_run"] is False


def test_fail_raises_task_error(make_client):
    ctx = make_ctx(make_client([]), dry_run=False)
    with pytest.raises(TaskError, match="no input rows"):
        ctx.fail("no input rows")


def test_store_handle_strips_the_domain(make_client):
    ctx = make_ctx(make_client([]), dry_run=False)
    assert ctx.store_handle == "test-store"


# ---------------------------------------------------------------------------
# GID helpers
# ---------------------------------------------------------------------------

def test_build_and_parse_round_trip():
    assert gid.build("Product", 123) == "gid://shopify/Product/123"
    assert gid.parse("gid://shopify/Product/123") == ("Product", "123")
    assert gid.numeric("gid://shopify/ProductVariant/456") == "456"
    assert gid.resource_type("gid://shopify/ProductVariant/456") == "ProductVariant"


@pytest.mark.parametrize("value", [
    "123",
    "gid://shopify/Product/123",
    "https://admin.shopify.com/store/acme/products/123",
    "https://admin.shopify.com/store/acme/products/123?variant=9",
])
def test_coerce_accepts_what_people_actually_paste(value):
    assert gid.coerce(value, "Product") == "gid://shopify/Product/123"


def test_coerce_refuses_the_wrong_resource_type():
    """A Product id and a Variant id are both plausible numbers; never guess."""
    with pytest.raises(gid.GidError, match="not interchangeable"):
        gid.coerce("gid://shopify/Product/123", "ProductVariant")


@pytest.mark.parametrize("value", ["", "not-an-id", "gid://shopify/"])
def test_coerce_rejects_junk(value):
    with pytest.raises(gid.GidError):
        gid.coerce(value, "Product")


def test_is_gid():
    assert gid.is_gid("gid://shopify/Product/1")
    assert not gid.is_gid("123")


def test_admin_url_is_clickable():
    url = gid.admin_url("gid://shopify/Product/123", "acme")
    assert url == "https://admin.shopify.com/store/acme/products/123"


def test_admin_url_falls_back_for_unmapped_types():
    assert gid.admin_url("gid://shopify/Weird/1", "acme").endswith("/store/acme")
