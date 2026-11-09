#!/usr/bin/env python3
"""shopifylib.locations: finding a location by name, and what must not be touched.

The resolution tests moved here from test_move_inventory when the resolver
became shared; the contract is unchanged.
"""
from __future__ import annotations

import pytest

from shopifylib import locations
from shopifylib.errors import TaskError


def loc(n, name, *, active=True, online=False, **extra):
    return {"id": f"gid://shopify/Location/{n}", "name": name, "isActive": active,
            "fulfillsOnlineOrders": online, **extra}


HOME = loc(1, "Home", online=True)
FAIR = loc(2, "Craft Fair Inventory")
LOCATIONS = [FAIR, HOME]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def test_resolves_by_exact_name():
    assert locations.resolve(LOCATIONS, "Home", "--from")["id"] == HOME["id"]


def test_name_match_is_case_insensitive():
    assert locations.resolve(LOCATIONS, "home", "--from")["id"] == HOME["id"]


def test_resolves_by_unique_partial_name():
    """'Craft Fair' should find 'Craft Fair Inventory'."""
    assert locations.resolve(LOCATIONS, "Craft Fair", "--to")["id"] == FAIR["id"]


def test_resolves_by_numeric_id_and_gid():
    assert locations.resolve(LOCATIONS, "1", "--from")["id"] == HOME["id"]
    assert locations.resolve(
        LOCATIONS, "gid://shopify/Location/2", "--to")["id"] == FAIR["id"]


def test_exact_name_beats_a_partial_match():
    found = [loc(3, "Home Annex", online=True), HOME]
    assert locations.resolve(found, "Home", "--from")["id"] == HOME["id"]


def test_ambiguous_partial_is_an_error_listing_candidates():
    found = [FAIR, loc(3, "Craft Fair Backup")]
    with pytest.raises(TaskError, match="more than one location"):
        locations.resolve(found, "Craft", "--to")


def test_unknown_location_lists_what_exists():
    with pytest.raises(TaskError, match="Craft Fair Inventory"):
        locations.resolve(LOCATIONS, "Warehouse", "--from")


def test_unknown_location_marks_disabled_ones():
    found = [HOME, loc(3, "Market A", active=False)]
    with pytest.raises(TaskError, match=r"Market A  \(disabled\)"):
        locations.resolve(found, "Warehouse", "--from")


def test_partial_false_refuses_a_substring_match():
    """A destructive task must not let 'Craft' quietly mean 'Craft Fair Inventory'."""
    with pytest.raises(TaskError, match="full name or id"):
        locations.resolve(LOCATIONS, "Craft", "LOCATION", partial=False)


def test_partial_false_still_takes_exact_names_and_ids():
    assert locations.resolve(LOCATIONS, "craft fair inventory", "L",
                             partial=False)["id"] == FAIR["id"]
    assert locations.resolve(LOCATIONS, "2", "L", partial=False)["id"] == FAIR["id"]


# ---------------------------------------------------------------------------
# Guards and formatting
# ---------------------------------------------------------------------------

def test_home_is_the_last_online_location():
    assert locations.is_last_online(LOCATIONS, HOME)
    assert not locations.is_last_online(LOCATIONS, FAIR)


def test_not_last_online_when_another_active_location_ships():
    other = loc(3, "Warehouse", online=True)
    assert not locations.is_last_online([HOME, other], HOME)


def test_a_disabled_online_location_does_not_count_as_a_backup():
    other = loc(3, "Warehouse", online=True, active=False)
    assert locations.is_last_online([HOME, other], HOME)


def test_one_line_address():
    assert locations.one_line({"address1": "123 Main St", "address2": "Suite 4",
                               "city": "Anytown", "provinceCode": "VA",
                               "zip": "22201", "countryCode": "US"}) == \
        "123 Main St, Suite 4, Anytown VA 22201"


def test_one_line_shows_country_only_outside_the_us():
    assert locations.one_line({"address1": "1 Rue", "city": "Paris",
                               "countryCode": "FR"}) == "1 Rue, Paris, FR"
    assert locations.one_line(None) == ""


def test_sort_puts_active_first_then_name():
    found = [loc(3, "aardvark", active=False), HOME, FAIR]
    assert [l["name"] for l in sorted(found, key=locations.sort_key)] == \
        ["Craft Fair Inventory", "Home", "aardvark"]
