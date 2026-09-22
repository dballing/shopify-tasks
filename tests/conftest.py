#!/usr/bin/env python3
"""Shared fixtures. The suite is hermetic: nothing here touches the network.

Two mechanisms keep it that way:

  * ``SHOPIFY_TASKS_CONFIG`` is pointed at a tmp_path config, so the loader can
    never read the developer's real config.toml (which holds a live token).
  * ``FakeSession`` stands in for ``requests.Session``, returning scripted
    responses. A client built by these fixtures has no socket to use even if a
    test asked it to.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shopifylib.client import ShopifyClient  # noqa: E402
from shopifylib.config import Shop, load_config  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """Point the config loader somewhere harmless for every test.

    Autouse and unconditional: a test that forgets to set this would otherwise
    silently fall through to the real config.toml.
    """
    monkeypatch.setenv("SHOPIFY_TASKS_CONFIG", str(tmp_path / "absent.toml"))
    # Any inherited token env vars would defeat the point of the fixture configs.
    for key in list(os.environ):
        if key.startswith("SHOPIFY_TOKEN_"):
            monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Fake HTTP transport
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, body, status: int = 200, headers: dict | None = None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body

    @property
    def text(self) -> str:
        return self._body if isinstance(self._body, str) else json.dumps(self._body)

    def json(self):
        if isinstance(self._body, str):
            return json.loads(self._body)  # raises JSONDecodeError, as requests does
        return self._body


class FakeSession:
    """Scripted stand-in for requests.Session.

    Hand it a list of responses (or callables taking the request payload) and it
    returns them in order, recording what it was asked. A callable lets a test
    assert on the variables it received -- which is how the pagination tests
    check that cursors are actually threaded rather than just that the right
    number of pages came back.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.headers: dict = {}
        self.closed = False

    def post(self, url, json=None, timeout=None, headers=None, **kwargs):
        # Deep-copy the payload: callers legitimately reuse and mutate one
        # variables dict across calls (paginate does, rebinding the cursor), and
        # real requests serializes at call time. Recording by reference would
        # show every request carrying the last call's values.
        self.requests.append({
            "url": url,
            "payload": copy.deepcopy(json),
            "timeout": timeout,
            # Per-request headers merge over the session's, as requests does.
            "headers": {**self.headers, **(headers or {})},
        })
        if not self.responses:
            raise AssertionError(
                f"FakeSession ran out of scripted responses after "
                f"{len(self.requests)} request(s)."
            )
        nxt = self.responses.pop(0)
        if callable(nxt):
            nxt = nxt(json or {})
        if isinstance(nxt, Exception):
            raise nxt
        return nxt if isinstance(nxt, FakeResponse) else FakeResponse(nxt)

    def close(self):
        self.closed = True

    # -- assertions helpers ------------------------------------------------

    @property
    def variables(self) -> list[dict]:
        return [(r["payload"] or {}).get("variables") or {} for r in self.requests]


def gql(data: dict, *, cost: dict | None = None, errors: list | None = None) -> dict:
    """Build a GraphQL response body with a plausible cost block."""
    body: dict = {}
    if errors is not None:
        body["errors"] = errors
    if data is not None:
        body["data"] = data
    body["extensions"] = {"cost": cost or {
        "requestedQueryCost": 10,
        "actualQueryCost": 10,
        "throttleStatus": {
            "maximumAvailable": 1000.0,
            "currentlyAvailable": 990.0,
            "restoreRate": 50.0,
        },
    }}
    return body


def throttled(currently_available: float = 0.0) -> dict:
    """A THROTTLED error response, as the API actually shapes it."""
    return {
        "errors": [{
            "message": "Throttled",
            "extensions": {"code": "THROTTLED"},
        }],
        "extensions": {"cost": {
            "requestedQueryCost": 100,
            "actualQueryCost": 0,
            "throttleStatus": {
                "maximumAvailable": 1000.0,
                "currentlyAvailable": currently_available,
                "restoreRate": 50.0,
            },
        }},
    }


# ---------------------------------------------------------------------------
# Config / client fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def shop() -> Shop:
    return Shop(
        id="test",
        domain="test-store.myshopify.com",
        access_token="shpat_fake",
        api_version="2026-07",
        plan="standard",
    )


@pytest.fixture
def sleeps():
    """Collects sleep durations instead of spending them.

    Every sleeping code path takes an injectable sleeper precisely so the tests
    can assert on the durations without a slow suite.
    """
    recorded: list[float] = []
    return recorded


@pytest.fixture
def make_client(shop, sleeps):
    """Build a client wired to a scripted FakeSession."""

    def _make(responses, *, writes_enabled=False, scopes=None, shop_override=None):
        session = FakeSession(responses)
        client = ShopifyClient(
            shop_override or shop,
            writes_enabled=writes_enabled,
            session=session,
            required_scopes=scopes,
            sleeper=sleeps.append,
        )
        # Budget shares the injected sleeper so pacing is recorded, not slept.
        client.budget._sleep = sleeps.append
        return client

    return _make


@pytest.fixture
def write_config(tmp_path, monkeypatch):
    """Write a config.toml into tmp_path and point the loader at it."""

    def _write(text: str):
        path = tmp_path / "config.toml"
        path.write_text(text)
        monkeypatch.setenv("SHOPIFY_TASKS_CONFIG", str(path))
        return path

    return _write


@pytest.fixture
def load(write_config):
    """Write a config and load it in one step."""

    def _load(text: str):
        write_config(text)
        return load_config()

    return _load


VALID_CONFIG = """
default_shop = "prod"
api_version = "2026-07"

[[shops]]
id = "prod"
domain = "acme.myshopify.com"
access_token = "shpat_prod"

[[shops]]
id = "dev"
domain = "acme-dev.myshopify.com"
access_token = "shpat_dev"
plan = "plus"
"""
