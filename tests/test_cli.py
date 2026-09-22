#!/usr/bin/env python3
"""Dispatcher wiring: discovery, flag precedence, and exit codes.

These run main() end to end with the network stubbed at the session layer, so
they cover the thing unit tests miss -- that --commit actually reaches the
client's writes_enabled flag.
"""
from __future__ import annotations

import pytest

import cli
from shopifylib.task import REGISTRY
from tests.conftest import FakeResponse, FakeSession, VALID_CONFIG, gql


@pytest.fixture
def run_cli(write_config, monkeypatch):
    """Run main() with a config in place and a scripted transport.

    Captures the client the dispatcher built so tests can assert on how it was
    configured, which is where the --commit plumbing either works or doesn't.
    """
    def _run(argv, responses=None, config=VALID_CONFIG):
        write_config(config)
        built = {}
        real_init = cli.ShopifyClient.__init__

        def spy_init(self, shop, **kwargs):
            kwargs["session"] = FakeSession(list(responses or []))
            kwargs["sleeper"] = lambda _s: None
            real_init(self, shop, **kwargs)
            built["client"] = self

        monkeypatch.setattr(cli.ShopifyClient, "__init__", spy_init)
        code = cli.main(argv)
        return code, built.get("client")

    return _run


SHOP_RESPONSE = gql({"shop": {
    "name": "Acme", "myshopifyDomain": "acme.myshopify.com", "email": "a@b.c",
    "currencyCode": "USD", "ianaTimezone": "America/New_York",
    "plan": {"displayName": "Basic", "partnerDevelopment": False, "shopifyPlus": False},
    "billingAddress": {"countryCodeV2": "US"},
}})


# -- discovery -------------------------------------------------------------

def test_discovery_finds_the_shipped_task():
    assert "shop-info" in cli.discover_tasks()


def test_help_lists_tasks(capsys):
    parser = cli.build_parser(cli.discover_tasks())
    parser.print_help()
    assert "shop-info" in capsys.readouterr().out


def test_write_tasks_are_marked_in_help(capsys, clean_specs):
    parser = cli.build_parser(clean_specs)
    parser.print_help()
    assert "[writes]" in capsys.readouterr().out


@pytest.fixture
def clean_specs():
    """A registry holding one read task and one write task."""
    from shopifylib.task import TaskSpec
    return {
        "reader": TaskSpec(name="reader", help="reads", run=lambda ctx, a: None),
        "writer": TaskSpec(name="writer", help="writes", run=lambda ctx, a: None, writes=True),
    }


# -- running ---------------------------------------------------------------

def test_shop_info_runs(run_cli, capsys):
    code, _ = run_cli(["shop-info"], [SHOP_RESPONSE])
    assert code == cli.EXIT_OK
    assert "Acme" in capsys.readouterr().out


def test_json_output_goes_to_stdout(run_cli, capsys):
    import json
    code, _ = run_cli(["shop-info", "--json"], [SHOP_RESPONSE])
    assert code == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["name"] == "Acme"


def test_shop_flag_selects_the_store(run_cli):
    _, client = run_cli(["shop-info", "--shop", "dev"], [SHOP_RESPONSE])
    assert client.shop.domain == "acme-dev.myshopify.com"


def test_default_shop_is_used_without_the_flag(run_cli):
    _, client = run_cli(["shop-info"], [SHOP_RESPONSE])
    assert client.shop.id == "prod"


def test_shared_flags_work_before_the_task_name(run_cli):
    """argparse's native ordering must keep working alongside the natural one."""
    _, client = run_cli(["--shop", "dev", "shop-info"], [SHOP_RESPONSE])
    assert client.shop.id == "dev"


def test_shared_flags_work_after_the_task_name(run_cli):
    """The ordering people actually type: task first, flags qualifying it."""
    _, client = run_cli(["shop-info", "--shop", "dev"], [SHOP_RESPONSE])
    assert client.shop.id == "dev"


def test_flag_after_the_task_beats_one_before_it(run_cli):
    """Both given: the more specific (closer to the task) position wins."""
    _, client = run_cli(["--shop", "prod", "shop-info", "--shop", "dev"], [SHOP_RESPONSE])
    assert client.shop.id == "dev"


def test_task_flags_and_shared_flags_coexist(run_cli, capsys):
    """A task's own flags must not be shadowed by the shared parent parser."""
    responses = [SHOP_RESPONSE, gql({"locations": {"nodes": [
        {"id": "gid://shopify/Location/1", "name": "Warehouse", "isActive": True},
    ]}})]
    code, _ = run_cli(["shop-info", "--locations", "--shop", "dev"], responses)
    assert code == cli.EXIT_OK
    assert "Warehouse" in capsys.readouterr().out


def test_api_version_override(run_cli):
    _, client = run_cli(["shop-info", "--api-version", "2026-10"], [SHOP_RESPONSE])
    assert client.shop.api_version == "2026-10"
    assert "2026-10" in client.shop.endpoint


# -- write safety ----------------------------------------------------------

def _register_writer(monkeypatch, recorder):
    """Install a throwaway write task for the flag-precedence tests."""
    from shopifylib.task import TaskSpec

    def run(ctx, args):
        recorder["dry_run"] = ctx.dry_run
        recorder["writes_enabled"] = ctx.client.writes_enabled

    specs = dict(REGISTRY)
    specs["writer"] = TaskSpec(name="writer", help="writes", run=run, writes=True)
    monkeypatch.setattr(cli, "discover_tasks", lambda: specs)
    return recorder


def test_write_task_defaults_to_dry_run(run_cli, monkeypatch):
    rec = _register_writer(monkeypatch, {})
    code, _ = run_cli(["writer"])
    assert code == cli.EXIT_OK
    assert rec["dry_run"] is True
    assert rec["writes_enabled"] is False, "the client must refuse writes too"


def test_commit_enables_writes(run_cli, monkeypatch):
    rec = _register_writer(monkeypatch, {})
    run_cli(["writer", "--commit"])
    assert rec["dry_run"] is False
    assert rec["writes_enabled"] is True


def test_dry_run_beats_commit(run_cli, monkeypatch):
    """Both flags present: assume the safety flag was the deliberate addition."""
    rec = _register_writer(monkeypatch, {})
    run_cli(["writer", "--commit", "--dry-run"])
    assert rec["dry_run"] is True
    assert rec["writes_enabled"] is False


def test_read_task_is_never_dry_run(run_cli):
    """Dry run would make a read-only task pointless."""
    _, client = run_cli(["shop-info", "--commit"], [SHOP_RESPONSE])
    assert client.writes_enabled is False


# -- exit codes ------------------------------------------------------------

def test_no_task_prints_help(run_cli):
    code, _ = run_cli([])
    assert code == cli.EXIT_USAGE


def test_config_error_exits_usage(run_cli, capsys):
    code, _ = run_cli(["shop-info", "--shop", "nope"], [SHOP_RESPONSE])
    assert code == cli.EXIT_USAGE
    assert "Unknown shop" in capsys.readouterr().err


def test_api_error_exits_three(run_cli, capsys):
    code, _ = run_cli(["shop-info"], [FakeResponse({}, status=401)])
    assert code == cli.EXIT_API
    assert "access token" in capsys.readouterr().err


def test_task_error_exits_one(run_cli, monkeypatch):
    from shopifylib.errors import TaskError
    from shopifylib.task import TaskSpec

    def run(ctx, args):
        raise TaskError("nothing to do")

    specs = {"boom": TaskSpec(name="boom", help="boom", run=run)}
    monkeypatch.setattr(cli, "discover_tasks", lambda: specs)
    code, _ = run_cli(["boom"])
    assert code == cli.EXIT_TASK_FAILED


def test_write_blocked_exits_four(run_cli, monkeypatch):
    """A task with an unguarded write path: distinct code, since it's a bug."""
    from shopifylib.task import TaskSpec

    def run(ctx, args):
        ctx.client.mutate("mutation X { productUpdate { userErrors { message } } }")

    specs = {"leaky": TaskSpec(name="leaky", help="leaky", run=run, writes=True)}
    monkeypatch.setattr(cli, "discover_tasks", lambda: specs)
    code, _ = run_cli(["leaky"])
    assert code == cli.EXIT_WRITE_BLOCKED
