#!/usr/bin/env python3
"""Central configuration loader.

Every entry point reads config through :func:`load_config` so there is exactly
one place that knows the file's shape, one place that validates it, and one
place the test suite has to redirect.

The config names one or more stores. Most runs hit the default; ``--shop`` picks
another. The multi-store shape is the default even for a single store because
the alternative -- a flat ``[shop]`` stanza -- has to be migrated the first time
you want a dev store to rehearse a destructive task against, and that is exactly
the moment you least want to be editing config schemas.

Tokens may live in the file (gitignored) or in the environment. The environment
wins, so a token never has to touch disk on a shared or backed-up machine --
which this repo is, since it sits inside a Dropbox tree.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError

# Env var naming the config file, so tests (and one-off alternate configs) can
# redirect the loader without touching the real one.
CONFIG_ENV = "SHOPIFY_TASKS_CONFIG"

# Per-shop credential overrides: SHOPIFY_<WHAT>_<ID>, id uppercased with
# non-alphanumerics folded to underscore (shop id "client-a" -> ..._CLIENT_A).
TOKEN_ENV_PREFIX = "SHOPIFY_TOKEN_"
CLIENT_ID_ENV_PREFIX = "SHOPIFY_CLIENT_ID_"
CLIENT_SECRET_ENV_PREFIX = "SHOPIFY_CLIENT_SECRET_"

DEFAULT_CONFIG_NAME = "config.toml"

# Pinned rather than "latest": an unversioned or floating request silently
# changes behavior on Shopify's quarterly release, which is how a working script
# breaks without anyone touching it. Bump deliberately.
DEFAULT_API_VERSION = "2026-07"

# Admin API calls only work against the canonical myshopify.com domain. A store's
# custom storefront domain returns 404 here, and the resulting error points at
# the version rather than the domain, so it is worth rejecting upfront.
_MYSHOPIFY_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$", re.IGNORECASE)

_API_VERSION_RE = re.compile(r"^\d{4}-\d{2}$")

# Seeds the cost bucket before the first response reports the real figures.
#
# These are the published per-plan figures, but they are a conservative FLOOR,
# not the truth: a Basic store was observed getting 2000 points at 100/s, which
# is the documented Plus allocation. Shopify reports the real bucket in every
# response's extensions.cost, and throttle.Budget overrides these on the first
# one -- so being wrong here costs at most one needless sleep, and only ever in
# the safe direction (under-estimating capacity). Don't tune these upward to
# match an observation; the live figures already win.
# Keys are Shopify's own plan names as the admin and the Dev Dashboard spell
# them, so the value here is whatever the plan dropdown said. "standard" is kept
# as a synonym for the non-Plus tiers.
_PLAN_BUCKETS = {
    "basic": (1000.0, 50.0),
    "grow": (1000.0, 50.0),
    "shopify": (1000.0, 50.0),      # the former name of the Grow tier
    "advanced": (1000.0, 50.0),
    "standard": (1000.0, 50.0),     # generic synonym for any non-Plus tier
    "plus": (2000.0, 100.0),
    "enterprise": (2000.0, 100.0),
}


@dataclass(frozen=True)
class Shop:
    """One configured store, and how to authenticate as it.

    ``id`` is the handle typed at the command line (``--shop prod``). It is
    cosmetic -- nothing persists it -- so unlike the search ids in the jobsearch
    repo it can be renamed freely.

    Credentials come in two shapes, because Shopify retired admin-created custom
    apps (the ones that handed you a permanent ``shpat_`` token) on 2026-01-01:

      * ``access_token`` -- a static token. Either a legacy ``shpat_``, or a
        non-expiring offline token from ``./shopify.sh auth``. Used as-is.
      * ``client_id`` + ``client_secret`` -- the app's own credentials, which
        :mod:`shopifylib.auth` exchanges for a token and caches.

    At least one shape is required; a shop may carry both, in which case the
    static token wins (it needs no network round trip).
    """

    id: str
    domain: str
    api_version: str
    plan: str
    access_token: str | None = None
    client_id: str | None = None
    client_secret: str | None = None

    @property
    def endpoint(self) -> str:
        return f"https://{self.domain}/admin/api/{self.api_version}/graphql.json"

    @property
    def bucket(self) -> tuple[float, float]:
        """(capacity, restore_rate) seed for the throttle budget."""
        return _PLAN_BUCKETS[self.plan]

    @property
    def store_handle(self) -> str:
        """The bare handle, e.g. 'acme' for acme.myshopify.com."""
        return self.domain.removesuffix(".myshopify.com")

    def __repr__(self) -> str:
        # Defensive: a dataclass repr would put the token and client secret into
        # any traceback or debug log that happens to render a Shop.
        return (f"Shop(id={self.id!r}, domain={self.domain!r}, "
                f"api_version={self.api_version!r})")


@dataclass(frozen=True)
class AppConfig:
    shops: dict[str, Shop]
    default_shop: str
    path: Path

    def get(self, shop_id: str | None = None) -> Shop:
        """Resolve a ``--shop`` argument (or None) to a Shop."""
        if shop_id is None:
            return self.shops[self.default_shop]
        try:
            return self.shops[shop_id]
        except KeyError:
            known = ", ".join(sorted(self.shops)) or "(none)"
            raise ConfigError(
                f"Unknown shop {shop_id!r}. Configured shops: {known}"
            ) from None


def config_path(explicit: str | os.PathLike | None = None) -> Path:
    """Resolve which config file to load: argument, then env, then repo root."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get(CONFIG_ENV)
    if env:
        return Path(env)
    # The repo root is this file's grandparent (shopifylib/config.py -> repo/).
    return Path(__file__).resolve().parent.parent / DEFAULT_CONFIG_NAME


def _env_suffix(shop_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", shop_id).upper()


def token_env_var(shop_id: str) -> str:
    """Env var that can supply a shop's access token in place of the config file."""
    return TOKEN_ENV_PREFIX + _env_suffix(shop_id)


def client_id_env_var(shop_id: str) -> str:
    """Env var that can supply a shop's app client id."""
    return CLIENT_ID_ENV_PREFIX + _env_suffix(shop_id)


def client_secret_env_var(shop_id: str) -> str:
    """Env var that can supply a shop's app client secret."""
    return CLIENT_SECRET_ENV_PREFIX + _env_suffix(shop_id)


def load_config(path: str | os.PathLike | None = None) -> AppConfig:
    """Load and fully validate the config.

    Validation is strict and happens here rather than lazily at first use: a
    typo in a shop domain should fail before a task starts mutating, not on call
    number four hundred.
    """
    cfg_path = config_path(path)
    if not cfg_path.exists():
        raise ConfigError(
            f"No config file at {cfg_path}.\n"
            f"Copy config.toml.example to config.toml and fill in your store details.\n"
            f"(Or point {CONFIG_ENV} at a different file.)"
        )

    try:
        with cfg_path.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{cfg_path} is not valid TOML: {exc}") from exc

    global_version = raw.get("api_version", DEFAULT_API_VERSION)
    _validate_api_version(global_version, "api_version")

    entries = raw.get("shops")
    if not entries:
        raise ConfigError(
            f"{cfg_path} defines no shops. It needs at least one [[shops]] entry "
            f"with an id, domain, and access_token -- see config.toml.example."
        )
    if not isinstance(entries, list):
        raise ConfigError(
            f"{cfg_path}: 'shops' must be an array of tables -- use [[shops]] "
            f"(double brackets), not [shops]."
        )

    shops: dict[str, Shop] = {}
    for index, entry in enumerate(entries):
        shop = _build_shop(entry, index, global_version, cfg_path)
        if shop.id in shops:
            raise ConfigError(
                f"{cfg_path}: duplicate shop id {shop.id!r}. Each [[shops]] entry "
                f"needs a distinct id -- it is how --shop selects one."
            )
        shops[shop.id] = shop

    default_shop = raw.get("default_shop")
    if default_shop is None:
        if len(shops) > 1:
            raise ConfigError(
                f"{cfg_path}: more than one shop is configured, so default_shop must "
                f"say which one runs without an explicit --shop. "
                f"Add e.g. default_shop = \"{next(iter(shops))}\" at the top of the file."
            )
        default_shop = next(iter(shops))
    elif default_shop not in shops:
        known = ", ".join(sorted(shops))
        raise ConfigError(
            f"{cfg_path}: default_shop = {default_shop!r} does not match any "
            f"configured shop id. Configured: {known}"
        )

    return AppConfig(shops=shops, default_shop=default_shop, path=cfg_path)


def _build_shop(entry: dict, index: int, global_version: str, cfg_path: Path) -> Shop:
    """Validate one [[shops]] entry into a Shop."""
    where = f"{cfg_path}: [[shops]] entry #{index + 1}"

    if not isinstance(entry, dict):
        raise ConfigError(f"{where} is not a table.")

    shop_id = entry.get("id")
    if not shop_id or not isinstance(shop_id, str):
        raise ConfigError(f"{where} is missing a string 'id'.")

    domain = entry.get("domain")
    if not domain or not isinstance(domain, str):
        raise ConfigError(f"{where} ({shop_id}) is missing a string 'domain'.")
    domain = domain.strip().lower()
    # Tolerate a pasted admin URL, since that is what the browser hands you.
    domain = domain.removeprefix("https://").removeprefix("http://").rstrip("/")
    domain = domain.split("/")[0]
    if not _MYSHOPIFY_RE.match(domain):
        raise ConfigError(
            f"{where} ({shop_id}): domain {domain!r} is not a *.myshopify.com domain.\n"
            f"The Admin API only answers on the canonical myshopify domain -- a custom "
            f"storefront domain returns 404. Find it in Shopify admin under "
            f"Settings > Domains, or in the URL when you are logged into the admin."
        )

    api_version = entry.get("api_version", global_version)
    _validate_api_version(api_version, f"{where} ({shop_id}): api_version")

    plan = str(entry.get("plan", "standard")).strip().lower()
    if plan not in _PLAN_BUCKETS:
        known = ", ".join(sorted(_PLAN_BUCKETS))
        raise ConfigError(
            f"{where} ({shop_id}): plan {plan!r} is not recognized. Use one of: {known}. "
            f"This only seeds the rate-limit estimate; the real figures come from the API."
        )

    # Environment beats the file throughout, so no secret need ever be written
    # to disk -- which matters here, since this repo lives in a Dropbox tree.
    token = os.environ.get(token_env_var(shop_id)) or entry.get("access_token")
    client_id = os.environ.get(client_id_env_var(shop_id)) or entry.get("client_id")
    client_secret = (os.environ.get(client_secret_env_var(shop_id))
                     or entry.get("client_secret"))

    token = token.strip() if isinstance(token, str) else None
    client_id = client_id.strip() if isinstance(client_id, str) else None
    client_secret = client_secret.strip() if isinstance(client_secret, str) else None

    if token and token.startswith("shpss_"):
        raise ConfigError(
            f"{where} ({shop_id}): that looks like an API secret key (shpss_), not an "
            f"access token. If this is a Dev Dashboard app, you want client_id and "
            f"client_secret instead -- see config.toml.example."
        )

    if client_id and not client_secret:
        raise ConfigError(
            f"{where} ({shop_id}) has a client_id but no client_secret. "
            f"Both are needed, or neither."
        )
    if client_secret and not client_id:
        raise ConfigError(
            f"{where} ({shop_id}) has a client_secret but no client_id. "
            f"Both are needed, or neither."
        )

    if not token and not client_id:
        raise ConfigError(
            f"{where} ({shop_id}) has no credentials.\n"
            f"Give it either:\n"
            f"  * client_id + client_secret -- from your app in the Shopify Dev "
            f"Dashboard (dev.shopify.com/dashboard). Then run "
            f"'./shopify.sh auth --shop {shop_id}' once to get a token.\n"
            f"  * access_token -- a legacy shpat_ token, or one produced by "
            f"'./shopify.sh auth'.\n"
            f"Any of these may come from the environment instead: "
            f"{token_env_var(shop_id)}, {client_id_env_var(shop_id)}, "
            f"{client_secret_env_var(shop_id)}."
        )

    return Shop(
        id=shop_id,
        domain=domain,
        api_version=api_version,
        plan=plan,
        access_token=token,
        client_id=client_id,
        client_secret=client_secret,
    )


def _validate_api_version(value: object, label: str) -> None:
    if not isinstance(value, str) or not _API_VERSION_RE.match(value):
        raise ConfigError(
            f"{label} must look like YYYY-MM (e.g. {DEFAULT_API_VERSION!r}), got {value!r}. "
            f"Shopify ships a new version each quarter; pin one explicitly."
        )
