#!/usr/bin/env python3
"""new-item: argument grammar, name resolution, the productSet payload, and
what happens when publishing fails after the product already exists.

The payload tests carry the most weight. ``=0`` versus an absent location is
the activated-but-empty / never-activated distinction the inventory tasks
depend on, and it is decided entirely by which entries this task sends.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal

import pytest

from shopifylib import catalog
from shopifylib.errors import TaskError
from shopifylib.task import TaskContext
from tasks import new_item as ni
from tests.conftest import gql
from tests.test_list_locations import loc

HOME = loc(1, "Home", online=True)
FAIR = loc(3, "Craft Fair Inventory")
MARKET_A = loc(2, "Market A", active=False)

NECKLACES = {"id": "gid://shopify/TaxonomyCategory/aa-6-8", "name": "Necklaces",
             "fullName": "Apparel & Accessories > Jewelry > Necklaces"}
EARRINGS = {"id": "gid://shopify/TaxonomyCategory/aa-6-6", "name": "Earrings",
            "fullName": "Apparel & Accessories > Jewelry > Earrings"}
JEWELRY = {"id": "gid://shopify/TaxonomyCategory/aa-6", "name": "Jewelry",
           "fullName": "Apparel & Accessories > Jewelry"}

def publication(n, name):
    """Shaped as prod returns it: the catalog title is not the usable name."""
    return {"id": f"gid://shopify/Publication/{n}",
            "catalog": {"title": f"Channel Catalog {n} for {name}"},
            "channels": {"nodes": [{"name": name}]}}


POS = publication(1, "Point of Sale")
GRAPHIQL = publication(2, "Shopify GraphiQL App")
ONLINE = publication(3, "Online Store")

PRODUCT = "gid://shopify/Product/99"


def make_ctx(client, *, dry_run=False, json_output=False) -> TaskContext:
    return TaskContext(client=client, shop=client.shop, dry_run=dry_run,
                       log=logging.getLogger("test"), json_output=json_output)


def args(**kw):
    base = dict(name="Opal Wings", description=None, price="20", category="earrings",
                channels=None, draft=False, option=None, variant=None, stock=None,
                notes=None, allow_duplicate=False)
    base.update(kw)
    return type("A", (), base)()


def same_title(*titles):
    return gql({"products": {"nodes": [
        {"id": f"gid://shopify/Product/{i}", "title": t, "status": "ACTIVE"}
        for i, t in enumerate(titles, 1)]}})


def reads(*, locations=(HOME, FAIR, MARKET_A), categories=(NECKLACES, EARRINGS, JEWELRY),
          publications=(POS, GRAPHIQL, ONLINE), shop_name="Acme Beads"):
    """The four reads plan() makes, in order: locations, categories (one page,
    with a duplicate and a null to prove the dedupe), channels, shop name."""
    nodes = [{"category": c} for c in categories] + [{"category": None}]
    if categories:
        nodes.append({"category": categories[0]})
    return [
        gql({"locations": {"nodes": list(locations)}}),
        gql({"products": {"nodes": nodes,
                          "pageInfo": {"hasNextPage": False, "endCursor": None}}}),
        gql({"publications": {"nodes": list(publications)}}),
        gql({"shop": {"name": shop_name}}),
    ]


def created(title="Opal Wings", status="ACTIVE", variants=(("Default Title", "20.00"),)):
    return gql({"productSet": {"product": {
        "id": PRODUCT, "title": title, "status": status,
        "category": {"name": "Earrings", "fullName": EARRINGS["fullName"]},
        "variants": {"nodes": [{"id": f"gid://shopify/ProductVariant/{i}",
                                "title": t, "price": p}
                               for i, (t, p) in enumerate(variants, 1)]}},
        "userErrors": []}})


def published(errors=()):
    return gql({"publishablePublish": {"userErrors": list(errors)}})


def sent(client, field) -> list[dict]:
    return [r["payload"]["variables"] for r in client.session.requests
            if field + "(" in r["payload"]["query"]]


# ---------------------------------------------------------------------------
# --variant grammar
# ---------------------------------------------------------------------------

def test_single_variant_takes_price():
    variants, problems = ni.parse_variants(None, [], "20")
    assert problems == []
    assert [(v.value, v.price) for v in variants] == [(None, Decimal("20.00"))]


def test_single_variant_needs_a_price():
    _, problems = ni.parse_variants(None, [], None)
    assert problems == ["--price is required."]


def test_zero_price_refused():
    """Prod has a product at 0.00 that was almost certainly a slip."""
    _, problems = ni.parse_variants(None, [], "0")
    assert any("greater than zero" in p for p in problems)


def test_variants_with_own_and_default_prices():
    variants, problems = ni.parse_variants("Metal", ["Gold=18", "Silver"], "16.00")
    assert problems == []
    assert [(v.value, str(v.price)) for v in variants] == [("Gold", "18.00"),
                                                           ("Silver", "16.00")]


def test_variant_value_may_contain_equals():
    variants, problems = ni.parse_variants("Length", ["16=in=12.00"], None)
    assert problems == []
    assert variants[0].value == "16=in"


def test_variant_without_any_price():
    _, problems = ni.parse_variants("Metal", ["Gold"], None)
    assert problems == ["--variant 'Gold' has no price; give Gold=PRICE or a default --price."]


def test_variant_needs_option():
    _, problems = ni.parse_variants(None, ["Gold=18"], None)
    assert any("--option" in p for p in problems)


def test_option_without_variants():
    _, problems = ni.parse_variants("Metal", [], "18")
    assert problems == ["--option was given without any --variant."]


def test_duplicate_variant_case_insensitive():
    _, problems = ni.parse_variants("Metal", ["Gold=18", "gold=19"], None)
    assert problems == ["--variant 'gold' is given twice."]


def test_at_instead_of_equals_points_at_the_fix():
    """Real slip: --stock uses '@', so '@' for the price came naturally."""
    _, problems = ni.parse_variants("Length", ['18"+3" extender@42.00'], None)
    assert problems == ["--variant '18\"+3\" extender@42.00': the price goes after "
                        "'=', not '@' (--variant '18\"+3\" extender=42.00')."]


def test_quotes_and_plus_in_variant_values():
    variants, problems = ni.parse_variants(
        "Length", ['18"+3" extender=42.00', '16"+3" extender'], "40")
    assert problems == []
    assert [(v.value, str(v.price)) for v in variants] == [
        ('18"+3" extender', "42.00"), ('16"+3" extender', "40.00")]
    specs, problems = ni.parse_stocks(['18"+3" extender@Home=1'], has_variants=True)
    assert problems == [] and specs[0].variant == '18"+3" extender'


def test_bad_variant_price_names_the_variant():
    _, problems = ni.parse_variants("Metal", ["Gold=1.234"], None)
    assert problems and problems[0].startswith("--variant 'Gold=1.234':")


# ---------------------------------------------------------------------------
# --stock grammar
# ---------------------------------------------------------------------------

def test_stock_plain():
    specs, problems = ni.parse_stocks(["Home=2", " Market A = 0 "], has_variants=False)
    assert problems == []
    assert [(s.variant, s.location, s.quantity) for s in specs] == [
        (None, "Home", 2), (None, "Market A", 0)]


def test_stock_with_variant_prefix():
    specs, problems = ni.parse_stocks(["Gold@Craft Fair=3"], has_variants=True)
    assert problems == []
    assert (specs[0].variant, specs[0].location, specs[0].quantity) == \
        ("Gold", "Craft Fair", 3)


def test_stock_needs_prefix_when_there_are_variants():
    _, problems = ni.parse_stocks(["Home=2"], has_variants=True)
    assert "say which one" in problems[0]


def test_stock_prefix_refused_without_variants():
    _, problems = ni.parse_stocks(["Gold@Home=2"], has_variants=False)
    assert "drop the 'Gold'@ prefix" in problems[0]


@pytest.mark.parametrize("spec, fragment", [
    ("Home", "expected LOCATION=QTY"),
    ("=2", "expected LOCATION=QTY"),
    ("Home=two", "not a whole number"),
    ("Home=-1", "cannot be negative"),
    ("Gold@=2", "no location"),
])
def test_stock_errors(spec, fragment):
    _, problems = ni.parse_stocks([spec], has_variants="@" in spec)
    assert fragment in problems[0]


# ---------------------------------------------------------------------------
# Categories and channels
# ---------------------------------------------------------------------------

CATS = [EARRINGS, JEWELRY, NECKLACES]


@pytest.mark.parametrize("typed, want", [
    ("Earrings", EARRINGS), ("earrings", EARRINGS),
    ("necklace", NECKLACES),           # the plural/singular case that matters
    ("Jewelry", JEWELRY),              # exact beats "Jewelry" being a substring of nothing else
])
def test_category_by_name(typed, want):
    assert catalog.resolve_category(CATS, typed) == want


def test_category_ambiguous_substring():
    with pytest.raises(TaskError, match="more than one"):
        catalog.resolve_category(CATS, "e")


def test_category_unknown_lists_what_is_in_use():
    with pytest.raises(TaskError) as exc:
        catalog.resolve_category(CATS, "brooch")
    assert "Necklaces" in str(exc.value) and "aa-6-6" in str(exc.value)


def test_category_by_short_id_not_in_use():
    """How a dev store, with no categorised products, gets one at all."""
    got = catalog.resolve_category([], "aa-6-3")
    assert got["id"] == "gid://shopify/TaxonomyCategory/aa-6-3"


def test_category_by_gid_in_use_returns_its_name():
    assert catalog.resolve_category(CATS, EARRINGS["id"]) == EARRINGS


def test_categories_are_deduplicated(make_client):
    ctx = make_ctx(make_client(reads()[1:2]))
    assert [c["name"] for c in catalog.load_categories(ctx)] == \
        ["Earrings", "Jewelry", "Necklaces"]


def test_channels_named_by_channel_not_catalog_title(make_client):
    ctx = make_ctx(make_client([gql({"publications": {"nodes": [POS, GRAPHIQL]}})]))
    assert catalog.load_channels(ctx) == [
        {"id": POS["id"], "name": "Point of Sale"},
        {"id": GRAPHIQL["id"], "name": "Shopify GraphiQL App"}]


def test_channel_name_falls_back_to_catalog_title(make_client):
    bare = {**POS, "channels": {"nodes": []}}
    ctx = make_ctx(make_client([gql({"publications": {"nodes": [bare]}})]))
    assert catalog.load_channels(ctx)[0]["name"] == "Channel Catalog 1 for Point of Sale"


def test_channel_resolution():
    channels = [{"id": "1", "name": "Point of Sale"}, {"id": "2", "name": "Online Store"},
                {"id": "3", "name": "Shopify GraphiQL App"}]
    got, problems = catalog.resolve_channels(channels, ["point of sale", "graphiql", "pos"])
    assert [c["id"] for c in got] == ["1", "3"]
    assert "no sales channel matches 'pos'" in problems[0]
    assert "Online Store" in problems[0]          # lists what exists


def test_channel_ambiguous():
    channels = [{"id": "1", "name": "Online Store"}, {"id": "2", "name": "Online Store B"}]
    _, problems = catalog.resolve_channels(channels, ["online"])
    assert "more than one" in problems[0]


# ---------------------------------------------------------------------------
# Description
# ---------------------------------------------------------------------------

def test_description_escapes_and_paragraphs():
    assert catalog.description_html("Silver & opal <3\nhandmade\n\nOne of a kind") == \
        "<p>Silver &amp; opal &lt;3<br>handmade</p><p>One of a kind</p>"


def test_description_blank():
    assert catalog.description_html("   ") == ""
    assert catalog.description_html(None) == ""


# ---------------------------------------------------------------------------
# The whole run
# ---------------------------------------------------------------------------

def test_single_variant_payload(make_client, capsys):
    client = make_client([same_title(), *reads(), created(), published()],
                         writes_enabled=True)
    status = ni.run(make_ctx(client), args(
        stock=["Home=2", "craft fair=0"], description="Opal & silver",
        notes="72.00 normally"))
    assert status == 0

    [variables] = sent(client, "productSet")
    payload = variables["input"]
    assert payload["title"] == "Opal Wings"
    assert payload["status"] == "ACTIVE"
    assert payload["vendor"] == "Acme Beads"
    assert payload["category"] == EARRINGS["id"]
    assert payload["descriptionHtml"] == "<p>Opal &amp; silver</p>"
    assert payload["metafields"] == [{"namespace": "custom", "key": "admin_notes",
                                      "type": "multi_line_text_field",
                                      "value": "72.00 normally"}]
    # productSet requires an option even for a single variant.
    assert payload["productOptions"] == [{"name": "Title",
                                          "values": [{"name": "Default Title"}]}]
    [variant] = payload["variants"]
    assert variant["price"] == "20.00"
    assert variant["inventoryItem"] == {"tracked": True}
    # =0 is sent (activates, empty); Market A was never named, so is absent
    # (not activated). This is the distinction the whole stock vocabulary uses.
    assert variant["inventoryQuantities"] == [
        {"locationId": HOME["id"], "name": "available", "quantity": 2},
        {"locationId": FAIR["id"], "name": "available", "quantity": 0}]

    [pub] = sent(client, "publishablePublish")
    assert pub == {"id": PRODUCT, "input": [{"publicationId": POS["id"]},
                                            {"publicationId": GRAPHIQL["id"]}]}
    out = capsys.readouterr().out
    assert "Created 'Opal Wings' (id 99), active, Earrings." in out
    assert "admin.shopify.com/store/test-store/products/99" in out


def test_multi_variant_payload(make_client):
    client = make_client([same_title(), *reads(),
                          created(variants=(("Gold", "18.00"), ("Silver", "16.00"))),
                          published()], writes_enabled=True)
    status = ni.run(make_ctx(client), args(
        price="16", option="Metal", variant=["Gold=18", "Silver"],
        stock=["Gold@Home=2", "silver@Craft Fair=1"]))
    assert status == 0

    payload = sent(client, "productSet")[0]["input"]
    assert payload["productOptions"] == [{"name": "Metal", "values": [
        {"name": "Gold"}, {"name": "Silver"}]}]
    gold, silver = payload["variants"]
    assert gold["optionValues"] == [{"optionName": "Metal", "name": "Gold"}]
    assert gold["price"] == "18.00" and silver["price"] == "16.00"
    # Each variant is activated only where it was stocked.
    assert [q["locationId"] for q in gold["inventoryQuantities"]] == [HOME["id"]]
    assert [q["locationId"] for q in silver["inventoryQuantities"]] == [FAIR["id"]]


def test_draft_and_channel_override(make_client):
    client = make_client([same_title(), *reads(), created(status="DRAFT"), published()],
                         writes_enabled=True)
    ni.run(make_ctx(client), args(draft=True, channels="Online Store"))
    assert sent(client, "productSet")[0]["input"]["status"] == "DRAFT"
    assert sent(client, "publishablePublish")[0]["input"] == [
        {"publicationId": ONLINE["id"]}]


def test_every_problem_reported_together_and_nothing_written(make_client):
    client = make_client([same_title(), *reads()], writes_enabled=True)
    with pytest.raises(TaskError) as exc:
        ni.run(make_ctx(client), args(
            category="brooch", channels="Point of Sale,Etsy",
            stock=["Market A=1", "Nowhere=1"]))
    text = str(exc.value)
    assert text.startswith("Nothing was created:")
    assert "'brooch'" in text
    assert "'Etsy'" in text
    assert "'Market A' is disabled" in text and "enable-location" in text
    assert "no location matches 'Nowhere'" in text
    assert sent(client, "productSet") == [] and sent(client, "publishablePublish") == []


def test_unknown_variant_in_stock(make_client):
    client = make_client([same_title(), *reads()])
    with pytest.raises(TaskError, match="no --variant named 'Bronze'"):
        ni.run(make_ctx(client), args(option="Metal", variant=["Gold=18"],
                                      stock=["Bronze@Home=1"]))


def test_same_variant_and_location_twice(make_client):
    client = make_client([same_title(), *reads()])
    with pytest.raises(TaskError, match="given twice"):
        ni.run(make_ctx(client), args(stock=["Home=1", "home=2"]))


def test_offline_problems_stop_before_any_read(make_client):
    client = make_client([])            # any request would run out of responses
    with pytest.raises(TaskError, match="--price is required"):
        ni.run(make_ctx(client), args(price=None))
    assert client.session.requests == []


def test_duplicate_title_refused(make_client):
    client = make_client([same_title("Opal Wings Deluxe", "opal wings")])
    with pytest.raises(TaskError) as exc:
        ni.run(make_ctx(client), args())
    assert "already exists" in str(exc.value)
    assert "opal wings" in str(exc.value) and "Deluxe" not in str(exc.value)
    assert "--allow-duplicate" in str(exc.value)


def test_duplicate_search_is_quoted(make_client):
    client = make_client([same_title()])
    with pytest.raises(Exception):      # runs out of responses after the check
        ni.run(make_ctx(client), args(name='The "Big" One'))
    assert client.session.variables[0] == {"q": 'title:"The \\"Big\\" One"'}


def test_allow_duplicate_skips_the_check(make_client):
    client = make_client([*reads(), created(), published()], writes_enabled=True)
    assert ni.run(make_ctx(client), args(allow_duplicate=True)) == 0
    assert "SameTitle" not in client.session.requests[0]["payload"]["query"]


def test_dry_run_writes_nothing(make_client, capsys):
    client = make_client([same_title(), *reads()])     # writes disabled at transport
    ctx = make_ctx(client, dry_run=True)
    assert ni.run(ctx, args(stock=["Home=2"])) == 0
    assert ctx.writes_skipped == 2                     # productSet + publish
    assert sent(client, "productSet") == []
    out = capsys.readouterr().out
    assert "Point of Sale, Shopify GraphiQL App" in out
    assert "(only variant)" in out and "Default Title" not in out


def test_publish_failure_after_create_exits_1_naming_the_product(make_client, caplog):
    client = make_client([same_title(), *reads(), created(),
                          published([{"field": ["input"], "message": "nope"}])],
                         writes_enabled=True)
    with caplog.at_level(logging.ERROR):
        status = ni.run(make_ctx(client), args())
    assert status == 1
    assert "was created (id 99)" in caplog.text
    assert "nope" in caplog.text
    assert "products/99" in caplog.text


def test_json_output(make_client, capsys):
    client = make_client([same_title(), *reads(), created(), published()],
                         writes_enabled=True)
    ni.run(make_ctx(client, json_output=True), args())
    data = json.loads(capsys.readouterr().out)
    assert data["id"] == PRODUCT
    assert data["channels"] == ["Point of Sale", "Shopify GraphiQL App"]
    assert data["channels_failed"] == []


def test_declares_publication_scopes():
    """auth only requests scopes some task declares; without these, re-running
    auth on prod would never grant them."""
    from shopifylib.task import REGISTRY
    scopes = REGISTRY["new-item"].scopes
    assert "read_publications" in scopes and "write_publications" in scopes
