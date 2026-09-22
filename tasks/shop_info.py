#!/usr/bin/env python3
"""Print basic facts about a configured store.

Doubles as the connectivity smoke test and as the worked example of the task
contract -- it is the shortest complete task in the repo, so start here when
writing a new one.
"""
from __future__ import annotations

from shopifylib import output
from shopifylib.task import task

QUERY = """
query ShopInfo {
  shop {
    name
    myshopifyDomain
    email
    currencyCode
    ianaTimezone
    plan { displayName partnerDevelopment shopifyPlus }
    billingAddress { countryCodeV2 }
  }
}
"""

# Counting locations verifies a second scope beyond read_products and tells you
# whether inventory tasks will need to disambiguate between locations.
LOCATIONS_QUERY = """
query LocationCount {
  locations(first: 250, includeInactive: false) {
    nodes { id name isActive }
  }
}
"""


def add_arguments(parser):
    parser.add_argument(
        "--locations",
        action="store_true",
        help="also list the store's active inventory locations",
    )


@task(
    name="shop-info",
    help="Show store name, plan, currency and API connectivity",
    scopes=["read_products"],
)
def run(ctx, args):
    """Show store name, plan, currency and API connectivity.

    Run this first after configuring a store: it proves the domain, the access
    token, the granted scopes and the pinned API version all work together,
    which is most of what can be wrong before a real task will run.
    """
    shop = ctx.query(QUERY)["shop"]
    plan = shop.get("plan") or {}

    result = {
        "config_id": ctx.shop.id,
        "name": shop.get("name"),
        "domain": shop.get("myshopifyDomain"),
        "email": shop.get("email"),
        "plan": plan.get("displayName"),
        "shopify_plus": plan.get("shopifyPlus"),
        "dev_store": plan.get("partnerDevelopment"),
        "currency": shop.get("currencyCode"),
        "timezone": shop.get("ianaTimezone"),
        "country": (shop.get("billingAddress") or {}).get("countryCodeV2"),
        "api_version": ctx.shop.api_version,
        "throttle": ctx.client.budget.snapshot(),
    }

    if args.locations:
        nodes = ctx.query(LOCATIONS_QUERY)["locations"]["nodes"]
        result["locations"] = [
            {"id": loc["id"], "name": loc["name"]} for loc in nodes
        ]

    if ctx.json_output:
        output.emit_json(result)
        return

    print(f"{result['name']}  ({result['domain']})")
    print(f"  config id   {result['config_id']}")
    print(f"  plan        {result['plan']}"
          + ("  [development store]" if result["dev_store"] else ""))
    print(f"  currency    {result['currency']}")
    print(f"  timezone    {result['timezone']}")
    print(f"  API version {result['api_version']}")

    bucket = result["throttle"]
    print(f"  rate limit  {bucket['available']:.0f}/{bucket['capacity']:.0f} points, "
          f"restoring at {bucket['restore_rate']:.0f}/s")

    if args.locations:
        print(f"\n  {len(result['locations'])} active location(s):")
        for loc in result["locations"]:
            print(f"    {loc['name']}")
