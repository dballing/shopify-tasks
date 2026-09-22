#!/usr/bin/env python3
"""The task framework: what a mini-project has to provide, and what it gets.

A task is one module in ``tasks/`` with a decorated ``run`` function::

    from shopifylib.task import task

    def add_arguments(parser):
        parser.add_argument("--days", type=int, default=180)

    @task(
        name="archive-stale-products",
        help="Archive products with no sales in N days",
        writes=True,
        scopes=["read_products", "write_products"],
    )
    def run(ctx, args):
        for product in ctx.paginate(QUERY, "products"):
            ctx.mutate(ARCHIVE, {"id": product["id"]},
                       field="productUpdate",
                       describe=f"archive {product['title']}")

That is the whole contract. The dispatcher supplies config, an authenticated
client, rate-limit pacing, logging, and dry-run enforcement, so a new
mini-project is a query and some domain logic rather than another round of
plumbing.

**Dry-run is enforced, not merely offered.** ``ctx.mutate()`` short-circuits when
``ctx.dry_run`` is set, and the underlying client refuses mutations outright
unless ``--commit`` was passed. A task that grows a write path and forgets to
check the flag gets an exception, not a surprise edit to a live storefront.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from .client import ShopifyClient
from .config import Shop
from .errors import TaskError
from .paging import DEFAULT_PAGE_SIZE, paginate

# name -> TaskSpec, populated by @task as tasks/ is imported.
REGISTRY: dict[str, "TaskSpec"] = {}


@dataclass
class TaskSpec:
    """Everything the dispatcher needs to know about one task."""

    name: str
    help: str
    run: Callable[["TaskContext", Any], int | None]
    writes: bool = False
    scopes: list[str] = field(default_factory=list)
    module: str = ""
    add_arguments: Callable | None = None

    @property
    def description(self) -> str:
        """Longer help for ``<task> --help``, taken from the run function's docstring."""
        return (self.run.__doc__ or self.help or "").strip()


def task(
    *,
    name: str,
    help: str,
    writes: bool = False,
    scopes: list[str] | None = None,
) -> Callable:
    """Register a function as a task.

    ``writes`` declares that the task mutates the store. It drives the dry-run
    default: a write task without ``--commit`` runs in preview mode, and its
    client will not let a mutation through. Read-only tasks skip all of that and
    just run.

    ``scopes`` is documentation that earns its keep at failure time -- Shopify's
    ACCESS_DENIED says which *field* was denied but never which scope to grant,
    so the client folds this list into the error message.
    """

    def decorator(func: Callable) -> Callable:
        if name in REGISTRY:
            raise RuntimeError(
                f"Two tasks are both named {name!r}: {REGISTRY[name].module} and "
                f"{func.__module__}. Task names must be unique."
            )
        REGISTRY[name] = TaskSpec(
            name=name,
            help=help,
            run=func,
            writes=writes,
            scopes=list(scopes or []),
            module=func.__module__,
        )
        return func

    return decorator


# Returned by ctx.mutate() in dry-run mode, so a task that inspects the result
# gets something obviously inert rather than a plausible-looking empty dict.
class _DryRunResult(dict):
    """Falsy stand-in for a mutation payload that was never sent."""

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "<dry-run: not sent>"


DRY_RUN = _DryRunResult()


@dataclass
class TaskContext:
    """What a task's ``run(ctx, args)`` receives.

    Holds the authenticated client, the resolved shop, the dry-run flag, and a
    logger -- plus thin wrappers (``query``, ``paginate``, ``mutate``) that are
    the ergonomic path for the 90% case.
    """

    client: ShopifyClient
    shop: Shop
    dry_run: bool
    log: logging.Logger
    json_output: bool = False
    spec: TaskSpec | None = None

    # Counters the dispatcher reports in the run summary. Tracking intended
    # writes separately from performed ones is what makes a dry run's output
    # directly comparable to the real run that follows it.
    writes_performed: int = 0
    writes_skipped: int = 0
    # Mutations that returned cleanly but reported userErrors, i.e. changed
    # nothing. Only reachable via check_errors=False.
    writes_failed: int = 0

    # -- reads -------------------------------------------------------------

    def query(
        self,
        document: str,
        variables: dict[str, Any] | None = None,
        *,
        operation_name: str | None = None,
    ) -> dict:
        """Run a read query and return its data payload."""
        return self.client.execute(document, variables, operation_name=operation_name)

    def paginate(
        self,
        document: str,
        connection_path: str,
        *,
        variables: dict[str, Any] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_items: int | None = None,
        **kwargs,
    ) -> Iterator[dict]:
        """Walk a connection, yielding every node. See paging.paginate."""
        return paginate(
            self.client,
            document,
            connection_path,
            variables=variables,
            page_size=page_size,
            max_items=max_items,
            **kwargs,
        )

    # -- writes ------------------------------------------------------------

    def mutate(
        self,
        document: str,
        variables: dict[str, Any] | None = None,
        *,
        field: str | None = None,
        describe: str | None = None,
        operation_name: str | None = None,
        check_errors: bool = True,
    ) -> dict:
        """Perform a mutation, or log it and skip when this is a dry run.

        ``describe`` is a short human phrase for what this call would do
        ("archive 'Blue Widget'"). It is what a dry run prints, so it should
        name the specific record -- "would update product" tells the reader
        nothing they can check.

        ``field`` names the mutation field so its userErrors can be verified;
        pass it whenever you know it, which is nearly always.

        ``check_errors=False`` hands back the payload with userErrors intact so
        a batch caller can report which entries failed and retry the rest; see
        ``ShopifyClient.mutate``. The caller then owns those errors.
        """
        label = describe or (field or "mutation")

        if self.dry_run:
            self.writes_skipped += 1
            self.log.info("DRY RUN would %s", label)
            if self.log.isEnabledFor(logging.DEBUG) and variables:
                self.log.debug("  variables: %s", variables)
            return DRY_RUN

        self.log.debug("%s", label)
        result = self.client.mutate(
            document, variables, field=field, operation_name=operation_name,
            check_errors=check_errors,
        )
        # With check_errors=False a mutation can return cleanly having changed
        # nothing, so counting every call as a change made the run summary
        # contradict the task's own report ("31 applied" against "created 6").
        # Inspecting the payload keeps the counter honest without raising.
        from .client import collect_user_errors
        if not check_errors and collect_user_errors(result):
            self.writes_failed += 1
        else:
            self.writes_performed += 1
        return result

    # -- convenience -------------------------------------------------------

    def fail(self, message: str) -> None:
        """Abort the task with a clean message and a non-zero exit code."""
        raise TaskError(message)

    def reader_for(self, shop_id: str):
        """A **read-only** client for a different configured shop.

        For tasks that copy between stores. The returned client always has
        writes disabled, whatever ``--commit`` said: a task operates on the one
        store ``--shop`` named, and a second store it merely reads from must not
        become a second write target by accident. The caller closes it.
        """
        from .client import ShopifyClient
        from .config import load_config

        other = load_config().get(shop_id)
        if other.domain == self.shop.domain:
            raise TaskError(
                f"--shop and the source store are both {shop_id!r} ({other.domain}); "
                f"a store cannot be copied onto itself."
            )
        return ShopifyClient(other, writes_enabled=False,
                             required_scopes=self.spec.scopes if self.spec else None)

    def require_development_store(self) -> None:
        """Abort unless the target is a Shopify development store.

        A guard for tasks that fabricate or destroy data wholesale. Dry-run
        protects against running the wrong task; this protects against running
        the right task against the wrong store, which no amount of previewing
        catches -- the preview looks perfectly correct, just aimed at a live
        storefront. There is deliberately no override flag.
        """
        plan = self.query(
            "query { shop { name plan { displayName partnerDevelopment } } }"
        )["shop"]
        if not (plan.get("plan") or {}).get("partnerDevelopment"):
            raise TaskError(
                f"{self.shop.id!r} ({plan.get('name')}, {self.shop.domain}) is not a "
                f"development store, and this task only runs against one.\n"
                f"It creates or deletes data wholesale, which would be destructive on "
                f"a real storefront.\n"
                f"Create a dev store at dev.shopify.com/dashboard, add it to "
                f"config.toml, and pass --shop <its id>."
            )

    @property
    def store_handle(self) -> str:
        """The bare store handle, for building admin URLs (see gid.admin_url)."""
        return self.shop.domain.removesuffix(".myshopify.com")

    def summary(self) -> dict:
        """Counters for the end-of-run line the dispatcher prints."""
        return {
            "shop": self.shop.id,
            "dry_run": self.dry_run,
            "writes_performed": self.writes_performed,
            "writes_skipped": self.writes_skipped,
            "writes_failed": self.writes_failed,
            "api_requests": self.client.request_count,
            "throttle": self.client.budget.snapshot(),
        }
