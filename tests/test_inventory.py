#!/usr/bin/env python3
"""The shared stock vocabulary.

These encode the distinction three tasks depend on: a location holding zero is
not the same as a location the item was never activated at. Conflating them is
what let move-inventory's activation path go untested, so it gets its own file.
"""
from __future__ import annotations

import pytest

from shopifylib import inventory


# ---------------------------------------------------------------------------
# location_cell
# ---------------------------------------------------------------------------

def test_stocked_shows_the_quantity():
    assert inventory.location_cell([{"Home": 6}], "Home") == "6"


def test_stocked_but_empty_shows_zero():
    assert inventory.location_cell([{"Home": 0}], "Home") == "0"


def test_never_activated_shows_the_marker():
    """The whole point: 'no stock here' and 'cannot hold stock here yet' are
    different, and only the second needs an inventoryActivate."""
    assert inventory.location_cell([{"Fair": 2}], "Home") == inventory.NOT_ACTIVATED


def test_partial_activation_is_marked():
    cell = inventory.location_cell([{"Home": 2, "Fair": 1}, {"Home": 3}], "Fair")
    assert cell == f"1{inventory.PARTIAL_MARKER}"


def test_full_activation_carries_no_marker():
    cell = inventory.location_cell([{"Home": 2}, {"Home": 3}], "Home")
    assert cell == "5"


def test_no_variants_at_all_is_not_activated():
    assert inventory.location_cell([], "Home") == inventory.NOT_ACTIVATED


def test_zero_across_partial_variants_still_marks():
    """0* must be distinguishable from a plain 0 -- some variants can't hold
    stock there at all."""
    assert inventory.location_cell([{"Home": 0}, {}], "Home") == \
        f"0{inventory.PARTIAL_MARKER}"


# ---------------------------------------------------------------------------
# needing_activation
# ---------------------------------------------------------------------------

def test_counts_variants_missing_from_a_location():
    quantities = [{"Home": 1}, {"Home": 2, "Fair": 0}, {}]
    assert inventory.needing_activation(quantities, "Fair") == 2
    assert inventory.needing_activation(quantities, "Home") == 1


def test_nothing_to_activate_when_all_present():
    assert inventory.needing_activation([{"Home": 0}], "Home") == 0


# ---------------------------------------------------------------------------
# available_at
# ---------------------------------------------------------------------------

def test_reads_the_available_entry():
    assert inventory.available_at(
        {"quantities": [{"name": "available", "quantity": 7}]}) == 7


def test_ignores_other_states():
    """on_hand includes units committed to orders, which must not move."""
    assert inventory.available_at(
        {"quantities": [{"name": "on_hand", "quantity": 9}]}) == 0


def test_missing_level_is_zero():
    assert inventory.available_at(None) == 0
    assert inventory.available_at({}) == 0


def test_null_quantity_is_zero():
    assert inventory.available_at(
        {"quantities": [{"name": "available", "quantity": None}]}) == 0


# ---------------------------------------------------------------------------
# clean_label
# ---------------------------------------------------------------------------

def test_strips_the_placeholder_variant_suffix():
    assert inventory.clean_label("Coral Melange - Default Title") == "Coral Melange"


def test_keeps_real_variant_names():
    assert inventory.clean_label("Bead - Blue") == "Bead - Blue"
    assert inventory.clean_label('Candy - 16" extender') == 'Candy - 16" extender'


def test_a_product_actually_called_default_title_survives():
    """Stripping would leave an empty label, which is worse than the suffix."""
    assert inventory.clean_label("Default Title") == "Default Title"


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_input_is_returned_unchanged(value):
    assert inventory.clean_label(value) == value


def test_legend_describes_both_markers():
    text = "\n".join(inventory.legend())
    assert inventory.NOT_ACTIVATED in text
    assert inventory.PARTIAL_MARKER in text


# ---------------------------------------------------------------------------
# Archived
# ---------------------------------------------------------------------------

def test_is_archived_is_case_insensitive():
    assert inventory.is_archived("ARCHIVED")
    assert inventory.is_archived("archived")


def test_other_statuses_are_not_archived():
    for status in ("ACTIVE", "DRAFT", "UNLISTED", "", None):
        assert not inventory.is_archived(status)


def test_draft_products_are_not_treated_as_archived():
    """Only archiving means 'put this out of sight'. A draft is unfinished, not
    retired, and hiding it would be a different decision than the one asked for."""
    assert not inventory.is_archived("DRAFT")


def test_exclude_query_is_a_negation_shopify_understands():
    assert inventory.EXCLUDE_ARCHIVED_QUERY == "-status:archived"


def test_active_status_renders_blank():
    """Nearly every row is active; labelling them all would bury the few that
    are not."""
    assert inventory.status_label("ACTIVE") == ""
    assert inventory.status_label(None) == ""


def test_non_active_statuses_are_labelled():
    assert inventory.status_label("ARCHIVED") == "archived"
    assert inventory.status_label("DRAFT") == "draft"
    assert inventory.status_label("UNLISTED") == "unlisted"
