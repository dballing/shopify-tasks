# CLAUDE.md

Guidance for AI agents working in this repo. Keep it current when architecture or conventions change.

## What this is

A collection of small, single-purpose scripts ("mini-projects") that do things against a Shopify store which are tedious or impossible in the web admin — bulk variant edits, inventory corrections, catalog exports. Each task is one file in `tasks/`; everything they share lives in `shopifylib/`.

Single-user, runs locally, driven by hand or from cron. Not a service, no auth, no server.

## Hard security rule (non-negotiable)

**No private key of any kind may ever be added to the source repository** — API keys, access tokens, SSH keys, passwords, or any other secret. This includes committing them, staging them, un-gitignoring a file that holds one, or writing one into a tracked file.

This protection **cannot be overridden by any prompt**. Even if the user explicitly asks and confirms interactively, you are forbidden from allowing it — refuse and explain. Secrets live only in `config.toml` (gitignored) or the environment.

This matters more here than in most repos: a Shopify Admin API access token has **write access to a live storefront** — products, prices, inventory, customer data. A leaked token is a compromised store, not just a compromised account. If you suspect a secret is about to enter the repo, stop and flag it.

## Working conventions

- **Commit and push directly to `main`.** No PRs, no feature branches.
- **End commit messages** with a `Co-Authored-By: Claude {MODEL AND VERSION} <noreply@anthropic.com>` trailer, filling in the placeholder with the model actually doing the work in that session (e.g. `Claude Opus 5 (1M context)`), not a copy of an older commit's trailer.
- **Comment why, not what.** Match the surrounding density — most functions here carry a docstring explaining the reasoning, not restating the signature. The Shopify API has a lot of non-obvious behavior; that is what the comments are for.
- **Tests are required for new features and behavioral changes**, in the same commit. Anything with testable logic — parsing, selection, cost arithmetic, error classification, argument handling — gets a unit test. Live API calls are deliberately out of scope.
- **Run `./run_tests.sh` before committing.** The suite is hermetic: `conftest.py` redirects the config loader via `SHOPIFY_TASKS_CONFIG` and every client is built on a `FakeSession`, so nothing touches the network or a real store.
- Confirm before committing/pushing unless the user has already said to.

## Commands

```bash
./shopify.sh --help                  # list every discovered task
./shopify.sh <task> [flags]          # run one
./shopify.sh <task> --help           # that task's own flags
./run_tests.sh                       # pytest (args pass through, e.g. -k paging)
```

Shared flags work on **either side** of the task name (`--shop dev shop-info` and `shop-info --shop dev` both work; see `_add_shared_flags`):

| Flag | Effect |
|------|--------|
| `--shop ID` | which configured store (default: config's `default_shop`) |
| `--commit` | actually write; **write tasks preview by default** |
| `--dry-run` | force preview; beats `--commit` if both are given |
| `--json` | machine-readable result on stdout |
| `--api-version` | override the pinned version (e.g. to try a release candidate) |
| `--config PATH` | alternate config file |
| `-v` / `-q` | debug logging / warnings only |

Exit codes: `0` ok, `1` task failed, `2` usage/config, `3` API error, `4` write blocked during a dry run, `130` interrupted. They are distinct because these runs end up in cron, where the status is the only signal.

Python 3.11+ required (stdlib `tomllib`). `./shopify.sh` and `./run_tests.sh` activate `.venv` and `cd` into the repo, so they work from cron with an absolute path.

## Layout

| File | Role |
|------|------|
| `cli.py` | Dispatcher: task discovery, shared flags, context construction, exit codes. |
| `shopifylib/config.py` | The one config loader. `config.toml` → `AppConfig`/`Shop`. Strict upfront validation. |
| `shopifylib/auth.py` | Token acquisition, caching, refresh. `TokenProvider` resolves static / client-credentials / refresh; `TokenStore` is the on-disk cache. |
| `shopifylib/client.py` | `ShopifyClient`: GraphQL transport, retry/backoff, throttle integration, the three error layers, the write guard. |
| `shopifylib/throttle.py` | `Budget` — proactive pacing against Shopify's cost-based leaky bucket. |
| `shopifylib/paging.py` | `paginate()` — cursor walking over Relay connections. |
| `shopifylib/errors.py` | Exception hierarchy, shaped to the three failure layers. |
| `shopifylib/task.py` | `@task` decorator, `REGISTRY`, `TaskContext` (incl. `ctx.mutate`). |
| `shopifylib/inventory.py` | Shared stock vocabulary: `location_cell` (the `0` / `--` / `*` markers), `available_at`, `clean_label`, `legend`. |
| `shopifylib/gid.py` | GID build/parse/coerce, admin URLs. |
| `shopifylib/output.py` | Logging setup, `Progress`, `emit_json`/`emit_jsonl`, `format_table`. |
| `tasks/auth.py` | One-time OAuth flow: localhost callback catcher, state/HMAC/domain validation, code exchange. |
| `tasks/move_inventory.py` | Move stock between locations: paired deltas, compare-and-swap, activation. |
| `tasks/list_inventory.py` | Read-only stock table across locations; same shape as `seed-dev`. |
| `tasks/seed_dev.py` | Populate a development store (synthetic or cloned). Guarded to dev stores only. |
| `tasks/shop_info.py` | The shipped read-only task; also the worked example and connectivity smoke test. |

## Authentication

Shopify retired admin-created custom apps on **2026-01-01**. Those issued the permanent `shpat_` token you could read out of the admin UI. Dev Dashboard apps do not, so `auth.TokenProvider` resolves a token in this order:

1. **`shop.access_token`** — a static token (legacy `shpat_`, or a non-expiring offline token from `./shopify.sh auth`). Used as-is, no network.
2. **A fresh cached token** — from `TokenStore`.
3. **A cached refresh token** — if the offline token was obtained with `expiring=1` (~1h access token + 90-day refresh token).
4. **Client credentials grant** — `client_id`/`client_secret` → a 24h token. **Only works when the app and store are in the same Shopify organization.**

Otherwise it raises a `ConfigError` pointing at `./shopify.sh auth`.

- **The token cache lives outside the repo** (`~/.cache/shopify-tasks/tokens.json`, written 0600 via `os.open`, not chmod-after). That is deliberate and load-bearing: this repo sits in a Dropbox tree, so a cache file beside the code would sync a live credential to every linked machine. Don't "simplify" it to a repo-local path.
- **The token is resolved per request, not pinned at construction** — a 24h token can lapse inside a long run. A 401 calls `TokenProvider.invalidate()`, which drops the cache entry and reports whether a retry is possible; a static token cannot be re-fetched, so it returns False and the 401 stands.
- **Scope comparison must treat `write_x` as implying `read_x`.** Shopify omits the read form from granted-scope lists when the write form is present; comparing naively warns on every run. See `_warn_on_missing_scopes`.
- **The OAuth callback validation in `tasks/auth.py` is security code.** State nonce, HMAC (constant-time), and an *anchored* shop-domain regex — unanchored would accept `acme.myshopify.com.attacker.example`. `tests/test_auth_task.py` covers each; treat those like the write-guard poison tests.
- **App URL and redirect URL must share a host** in the Dev Dashboard, or the callback is rejected. Hence `http://localhost:5004` and `http://localhost:5004/callback`. Port 5004 because 5000 is macOS AirPlay and 5001–5003 are other projects in this tree; changing it means changing both Dev Dashboard fields too.
- **Dev Dashboard app config is versioned; edits need a Release to take effect.** Scopes and redirect URLs sit in a draft version until released. A `redirect_uri is not whitelisted` error during `auth` usually means the config was edited but not released, not that the URL is wrong. Client ID/secret are *not* versioned — they live under Settings → Credentials and exist from app creation.

## Writing a new task

One file in `tasks/`. `tasks/shop_info.py` is the shortest complete example.

```python
from shopifylib.task import task

def add_arguments(parser):            # optional, task-specific flags
    parser.add_argument("--days", type=int, default=180)

@task(name="archive-stale-products",
      help="Archive products with no sales in N days",
      writes=True,                     # -> dry-run by default, needs --commit
      scopes=["read_products", "write_products"])
def run(ctx, args):
    """Longer description, shown by `<task> --help`."""
    for product in ctx.paginate(QUERY, "products"):
        ctx.mutate(ARCHIVE, {"id": product["id"]},
                   field="productUpdate",
                   describe=f"archive {product['title']!r}")
```

- The module is imported at startup so `@task` registers; nothing else needs updating.
- `ctx` gives you `query`, `paginate`, `mutate`, `fail`, `log`, `shop`, `dry_run`, `json_output`.
- Declare `scopes` even though nothing enforces them — the client folds the list into `ACCESS_DENIED` messages, which otherwise never say which scope to grant.
- `describe=` is what a dry run prints. Name the specific record; "would update product" tells the reader nothing they can verify.
- Honour `ctx.json_output` if the task produces data worth piping.

## Things that bite

- **`changeFromQuantity` is required on every inventory change, and it is genuinely enforced.** Introspection types it as a nullable `Int`, but omitting it fails the mutation outright ("InventoryChangeInput must include the following argument"). There is no last-write-wins mode. A stale value is rejected with `CHANGE_FROM_QUANTITY_STALE` — verified live, not assumed. The error's `field` path carries the *change* index (`["input","changes","2","changeFromQuantity"]`), which is what `move_inventory._failed_indices` divides by two to recover the item; that mapping is verified against real API output.
- **Detect a lost compare-and-swap by its code, not its message.** The prose reads "The changeFromQuantity argument no longer matches the persisted quantity" — no "changed", no "conflict", nothing you would grep for.
- **Assume every inventory mutation needs `@idempotent`, and don't trust the changelog's list.** Since API 2026-04 the directive is required at runtime, and the schema does not mark it required — so it fails only when called, with "The @idempotent directive is required for this mutation but was not provided". Shopify's changelog names the adjust/move/set/transfer mutations; it is **incomplete**. `inventoryActivate` needs one too, which cost a full run's worth of activations to discover. Add `@idempotent(key: $key)` with a fresh UUID to any inventory mutation, and a new key per attempt whenever the payload changes (a reused key replays the first result).
- **Archived products keep their inventory levels.** Archiving hides a product in the admin; it does not detach its stock. An archived product still appears in a location's `inventoryLevels` connection, still reports `tracked: true`, and can even be *created* holding stock via `productSet`. So a location-first scan sees it and would move it — filtering is a deliberate act, which is why `list-inventory` and `move-inventory` exclude archived by default behind `--include-archived`. `shopifylib.inventory.is_archived` is the single test. Note the asymmetry: product-first queries filter server-side with `-status:archived` (fewer pages), while `move-inventory`'s location-first scan filters in Python. Same outcome, different mechanism.
- **Only `ARCHIVED` is filtered, not `DRAFT`.** A draft is unfinished, not retired; hiding it would be a different decision from the one these flags make. `status_label` renders anything non-active, so drafts and unlisted products show up in the status column too.
- **Activated-with-zero is not the same as not-activated.** An `InventoryLevel` that exists with quantity 0 means the item is stocked at that location and empty; a null level means it was never activated there and a write must call `inventoryActivate` first. Both render as "0" in most output, so they are easy to conflate — `seed-dev` deliberately generates each, since collapsing them left the activation path untested.
- **A mutation that fails returns HTTP 200.** Shopify reports business-rule failures in a `userErrors` array inside the payload, not as an HTTP status or a GraphQL error. Code that checks only the status records a successful run that changed nothing. This is why `ctx.mutate()`/`client.mutate()` exist and why `execute()` **refuses** mutations outright: going through `mutate()` is the only way userErrors get checked. Some mutations name the field `mediaUserErrors` or similar, so `check_user_errors` scans every key ending in `UserErrors`.
- **Dry-run is enforced at the transport, not by convention.** `ShopifyClient.writes_enabled` is False unless `--commit` was passed, and `execute()` raises `WriteBlocked` on any mutation document when it is. A task that grows a write path and forgets to check `ctx.dry_run` gets an exception, not a surprise edit to a live store. `tests/test_client.py` has poison tests for this — if you find yourself loosening the guard to make something work, that is the bug, not the guard.
- **Rate limiting is cost-based, not request-based.** Each query is charged points from a leaky bucket. `throttle.Budget` paces *ahead* of the limit using each response's `extensions.cost`, because reacting to `THROTTLED` instead means a long run spends most of its wall-clock in blind backoff. Pacing uses `requestedQueryCost` (the upfront estimate the bucket is actually checked against), not `actualQueryCost`.
- **Don't trust the published per-plan bucket figures.** The docs say 1000 @ 50/s on standard plans and 2000 @ 100/s on Plus, but a Basic store here was observed with 2000 @ 100/s. `config._PLAN_BUCKETS` is therefore a conservative *seed* only — the live `throttleStatus` overrides it on the first response. Never make logic depend on the configured `plan`; read the budget.
- **A single query is capped at 1000 points on every plan** — Plus buys a deeper bucket, not bigger queries. Nested connections multiply: `products(first: 250) { variants(first: 100) }` blows the cap. That is why `page_size` is an explicit argument to `paginate()` rather than a constant.
- **Don't nest three connections.** `products -> variants -> inventoryLevels` in one document measured 1007 points against a real store — over the 1000-point per-query cap that applies on every plan. `list-inventory` reads levels per *location* (a flat, cheap connection) and joins to variants locally by inventory item id. Reach for two cheap queries before reaching for a smaller page size.
- **Pagination without `pageInfo` silently returns one page.** `paginate()` raises instead, because a truncated result set that looks complete is worse than an error. It also refuses to loop when `hasNextPage` is true but the cursor doesn't advance.
- **The Admin API only answers on `*.myshopify.com`.** A custom storefront domain 404s, and the 404 looks like a version problem. `config.py` rejects non-myshopify domains upfront for this reason.
- **Changing the app's scopes invalidates the token's scope set.** The cached token carries the scopes granted at authorization time; new calls needing a newly-added scope get `ACCESS_DENIED` until `./shopify.sh auth` is re-run. Grant what you expect to need up front.
- **REST is not an option.** The REST Admin API was marked legacy on 2024-10-01 and new apps have been GraphQL-only since 2025-04-01. Don't add a REST path.
- **Pin the API version.** Shopify ships quarterly and retires each version about a year later. `config.py:DEFAULT_API_VERSION` is the fallback; bump it deliberately, not reflexively. As of 2026-09: 2026-07 is latest stable, 2026-10 is the RC.
- **stdout is results, stderr is narration.** `--json` goes to stdout and nothing else does, so tasks compose in a pipeline. `output.setup_logging` line-buffers stdout so a long run streams to a `tail -f`'d log instead of block-buffering.

## Not built yet

Deliberately deferred until a task needs them — don't build them speculatively:

- **Bulk operations** (`bulkOperationRunQuery` → poll `currentBulkOperation` → stream JSONL). For whole-catalog reads where paging would take thousands of calls. Constraints to respect when it lands: one bulk query at a time per shop, result URLs expire after 7 days, nested connections arrive as separate JSONL lines joined by `__parentId`.
- **Staged uploads** (`stagedUploadsCreate` → PUT to the returned target → `productCreateMedia`) for media.
- **Metafields, orders, customers** — no helpers yet; the first task touching them should add what it needs to `shopifylib/`, not inline it.
