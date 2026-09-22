# shopify-tasks

Scripts/hacks for working with Shopify in an automated fashion.

A collection of small, single-purpose tasks that do things which are tedious or
impossible in the Shopify web admin — bulk variant edits, inventory corrections,
catalog exports. Each task is one file; they share a common layer that handles
authentication, pagination, rate limiting, and error handling.

Talks to the [GraphQL Admin API](https://shopify.dev/docs/api/admin-graphql)
using a custom-app access token.

## Setup

Python 3.11+ required.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.toml.example config.toml
```

### 1. Create an app

Shopify retired admin-created custom apps on 2026-01-01 — the ones that revealed
a permanent `shpat_` token in the admin UI. Apps are now created in the
[Dev Dashboard](https://dev.shopify.com/dashboard) and issue tokens via OAuth.

Create an app there and set:

| Field | Value |
|-------|-------|
| App URL | `http://localhost:5004` |
| Allowed redirection URL(s) | `http://localhost:5004/callback` |
| Embed app in Shopify admin | unchecked |
| Use legacy install flow | unchecked |
| Scopes | `read_products,write_products,read_inventory,write_inventory,read_locations` |

Both URLs must share a host — Shopify rejects the callback with
*"redirect_uri and application url must have matching hosts"* otherwise.

Then click **Release**. App configuration is versioned, and what you just
edited is a draft: until it's released, OAuth uses the previously-released
config and your callback comes back as `redirect_uri is not whitelisted`.

Grant the scopes you expect to need up front. Adding one later means editing
the app, releasing *another* version, approving the new scopes on the store,
and re-running `auth`.

### 2. Configure and authorize

Copy the app's **Client ID** and **Client secret** from
Dev Dashboard → your app → **Settings → Credentials** into `config.toml`,
alongside your `*.myshopify.com` domain. Then:

```bash
./shopify.sh auth              # opens your browser, approve the app
./shopify.sh shop-info         # verify it works
```

`auth` runs once per store. It captures the OAuth callback on localhost and
caches an **offline access token** — tied to the app and store rather than your
staff login, and it doesn't expire. The token is cached outside the repo (under
`~/.cache/shopify-tasks/`, mode 0600) so it never lands in Dropbox.

If your store is a development store in the *same Shopify organization* as the
app, you can skip `auth` entirely — the client credentials grant works directly
from `client_id`/`client_secret`, and tokens are fetched and refreshed
automatically.

Any secret can come from the environment instead of the file:
`SHOPIFY_TOKEN_<ID>`, `SHOPIFY_CLIENT_ID_<ID>`, `SHOPIFY_CLIENT_SECRET_<ID>`.

## Usage

```bash
./shopify.sh --help                     # list every available task
./shopify.sh <task> --help              # one task's flags
./shopify.sh <task> [flags]
```

Common flags work on either side of the task name:

| Flag | Effect |
|------|--------|
| `--shop ID` | which configured store to use |
| `--commit` | actually write — **write tasks preview by default** |
| `--json` | machine-readable output on stdout |
| `-v` | verbose logging |

### Dry run by default

Any task that writes to the store previews its changes unless you pass
`--commit`:

```bash
./shopify.sh some-write-task              # shows what it would do
./shopify.sh some-write-task --commit     # actually does it
```

This is enforced in the API client, not left to each task to remember — a
mutation cannot leave the process unless `--commit` was passed. Configuring a
second store (a free Shopify partner development store) and rehearsing against
it with `--shop dev` is worth the five minutes before any bulk edit.

## Available tasks

| Task | Description |
|------|-------------|
| `auth` | One-time browser login to get an access token for a store. |
| `shop-info` | Store name, plan, currency, API connectivity. Run this first. |
| `list-inventory` | Stock levels across every location. Read-only. |
| `move-inventory` | Move stock between two locations, additively. |
| `seed-dev` | Populate a development store with test products. |

## Adding a task

One file in `tasks/`; it is discovered automatically. See
[tasks/shop_info.py](tasks/shop_info.py) for the shortest complete example and
[CLAUDE.md](CLAUDE.md) for the conventions and the Shopify-specific gotchas
worth knowing before you start.

## Tests

```bash
./run_tests.sh
```

Hermetic — no network, no real store.

## License

Apache 2.0. See [LICENSE](LICENSE).
