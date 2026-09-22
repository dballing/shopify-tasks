#!/usr/bin/env python3
"""Config loading and validation.

Validation errors get a test each because they are the first thing a fresh
checkout hits, and a vague message there costs more time than the code saved.
"""
from __future__ import annotations

import pytest

from shopifylib.config import DEFAULT_API_VERSION, load_config, token_env_var
from shopifylib.errors import ConfigError
from tests.conftest import VALID_CONFIG


def test_loads_shops_and_default(load):
    config = load(VALID_CONFIG)
    assert set(config.shops) == {"prod", "dev"}
    assert config.get().id == "prod"
    assert config.get("dev").id == "dev"


def test_endpoint_is_versioned_graphql_url(load):
    shop = load(VALID_CONFIG).get("prod")
    assert shop.endpoint == "https://acme.myshopify.com/admin/api/2026-07/graphql.json"


def test_per_shop_api_version_overrides_global(load):
    config = load(VALID_CONFIG + '\n[[shops]]\nid = "rc"\n'
                  'domain = "acme-rc.myshopify.com"\naccess_token = "shpat_rc"\n'
                  'api_version = "2026-10"\n')
    assert config.get("rc").api_version == "2026-10"
    assert config.get("prod").api_version == "2026-07"


def test_plan_seeds_the_rate_limit_bucket(load):
    config = load(VALID_CONFIG)
    assert config.get("prod").bucket == (1000.0, 50.0)   # standard
    assert config.get("dev").bucket == (2000.0, 100.0)   # plus


@pytest.mark.parametrize("plan", ["basic", "grow", "shopify", "advanced",
                                  "standard", "plus", "enterprise"])
def test_shopify_plan_names_are_accepted(load, plan):
    """The value people type is whatever their plan dropdown says, so every
    real Shopify tier name has to be valid -- 'Basic' especially."""
    config = load(f'[[shops]]\nid = "a"\ndomain = "a.myshopify.com"\n'
                  f'access_token = "shpat_x"\nplan = "{plan}"\n')
    assert config.get("a").bucket[0] > 0


def test_plan_name_is_case_insensitive(load):
    """The dropdown shows 'Basic', capitalised."""
    config = load('[[shops]]\nid = "a"\ndomain = "a.myshopify.com"\n'
                  'access_token = "shpat_x"\nplan = "Basic"\n')
    assert config.get("a").plan == "basic"


def test_api_version_defaults_when_unset(load):
    config = load('[[shops]]\nid = "a"\ndomain = "a.myshopify.com"\n'
                  'access_token = "shpat_a"\n')
    assert config.get("a").api_version == DEFAULT_API_VERSION


def test_single_shop_needs_no_default_shop(load):
    config = load('[[shops]]\nid = "only"\ndomain = "only.myshopify.com"\n'
                  'access_token = "shpat_x"\n')
    assert config.get().id == "only"


# -- token sourcing --------------------------------------------------------

def test_env_var_supplies_token_when_file_omits_it(load, monkeypatch):
    monkeypatch.setenv("SHOPIFY_TOKEN_PROD", "shpat_from_env")
    config = load('[[shops]]\nid = "prod"\ndomain = "acme.myshopify.com"\n')
    assert config.get("prod").access_token == "shpat_from_env"


def test_env_var_beats_the_file(load, monkeypatch):
    """The environment wins so a token need never be the one on disk."""
    monkeypatch.setenv("SHOPIFY_TOKEN_PROD", "shpat_from_env")
    config = load(VALID_CONFIG)
    assert config.get("prod").access_token == "shpat_from_env"


def test_token_env_var_name_folds_punctuation():
    assert token_env_var("client-a") == "SHOPIFY_TOKEN_CLIENT_A"
    assert token_env_var("prod") == "SHOPIFY_TOKEN_PROD"


def test_repr_does_not_leak_the_token(load):
    """A Shop rendered into a traceback or debug log must not expose the token."""
    shop = load(VALID_CONFIG).get("prod")
    assert "shpat_prod" not in repr(shop)


# -- validation failures ---------------------------------------------------

def test_missing_file_names_the_template(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPIFY_TASKS_CONFIG", str(tmp_path / "nope.toml"))
    with pytest.raises(ConfigError, match="config.toml.example"):
        load_config()


def test_custom_domain_is_rejected(load):
    """The commonest first-run mistake: using the storefront domain."""
    with pytest.raises(ConfigError, match="myshopify.com"):
        load('[[shops]]\nid = "a"\ndomain = "shop.acme.com"\naccess_token = "shpat_a"\n')


def test_admin_url_is_accepted_as_a_domain(load):
    """Tolerate the pasted URL form, since that is what the browser gives you."""
    config = load('[[shops]]\nid = "a"\ndomain = "https://acme.myshopify.com/admin"\n'
                  'access_token = "shpat_a"\n')
    assert config.get("a").domain == "acme.myshopify.com"


def test_duplicate_shop_ids_are_rejected(load):
    with pytest.raises(ConfigError, match="duplicate shop id"):
        load('[[shops]]\nid = "a"\ndomain = "a.myshopify.com"\naccess_token = "x"\n'
             '[[shops]]\nid = "a"\ndomain = "b.myshopify.com"\naccess_token = "y"\n')


def test_missing_token_names_the_env_var(load):
    with pytest.raises(ConfigError, match="SHOPIFY_TOKEN_A"):
        load('[[shops]]\nid = "a"\ndomain = "a.myshopify.com"\n')


def test_secret_key_instead_of_access_token_is_diagnosed(load):
    """shpss_ vs shpat_ is easy to confuse and gives an opaque 401 otherwise."""
    with pytest.raises(ConfigError, match="secret key"):
        load('[[shops]]\nid = "a"\ndomain = "a.myshopify.com"\n'
             'access_token = "shpss_wrong_one"\n')


def test_multiple_shops_require_an_explicit_default(load):
    """Without a default, a bare command could silently hit the wrong store."""
    with pytest.raises(ConfigError, match="default_shop"):
        load('[[shops]]\nid = "a"\ndomain = "a.myshopify.com"\naccess_token = "x"\n'
             '[[shops]]\nid = "b"\ndomain = "b.myshopify.com"\naccess_token = "y"\n')


def test_unknown_default_shop_is_rejected(load):
    with pytest.raises(ConfigError, match="does not match"):
        load('default_shop = "nope"\n[[shops]]\nid = "a"\n'
             'domain = "a.myshopify.com"\naccess_token = "x"\n')


def test_unknown_shop_argument_lists_the_known_ones(load):
    config = load(VALID_CONFIG)
    with pytest.raises(ConfigError, match="dev, prod"):
        config.get("staging")


def test_bad_api_version_is_rejected(load):
    with pytest.raises(ConfigError, match="YYYY-MM"):
        load('api_version = "latest"\n[[shops]]\nid = "a"\n'
             'domain = "a.myshopify.com"\naccess_token = "x"\n')


def test_unknown_plan_is_rejected(load):
    with pytest.raises(ConfigError, match="plan"):
        load('[[shops]]\nid = "a"\ndomain = "a.myshopify.com"\n'
             'access_token = "x"\nplan = "gold"\n')


def test_single_bracket_shops_table_is_diagnosed(load):
    """[shops] instead of [[shops]] is a natural TOML slip with a confusing result."""
    with pytest.raises(ConfigError, match=r"\[\[shops\]\]"):
        load('[shops]\nid = "a"\n')


def test_no_shops_at_all(load):
    with pytest.raises(ConfigError, match="no shops"):
        load('default_shop = "a"\n')


def test_malformed_toml_is_reported_as_such(load):
    with pytest.raises(ConfigError, match="not valid TOML"):
        load("this is not toml [[[\n")
