import pytest

from parsers import (
    Product,
    ProductIdentity,
    assert_disjoint,
    assert_prices_in_range,
    assert_prices_sorted,
    assert_relevant,
    build_products,
    parse_price,
    parse_product_identity,
    relevance_ratio,
    same_product,
)


GOODS = "https://www.momoshop.com.tw/goods/GoodsDetail.jsp?i_code="


def card(name="【SONY】藍牙耳機", price="1,490", i_code="8940866", is_ad=False):
    return {"isAd": is_ad, "name": name, "rawPrice": price, "href": f"{GOODS}{i_code}"}


def product(name="耳機", price=100, i_code="1"):
    return Product(
        name=name,
        raw_price=str(price),
        price=price,
        href=f"{GOODS}{i_code}",
        identity=ProductIdentity("goods-detail", "catalog", i_code),
    )


# ── price parsing ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1,490", 1490), ("99起", 99), ("1,850起", 1850), ("2", 2), ("0", 0)],
)
def test_parse_price_accepts_observed_formats(raw, expected):
    assert parse_price(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        ",",
        "1,2,3",
        "12.50",
        "",
        None,
        # Lives on the enclosing `.money` element, never on `.price`
        # (measured 2026-09-22: .money="834(售價已折)" while .price="834").
        # Seeing it here would mean the DOM contract moved, so reject loudly.
        "414(售價已折)",
    ],
)
def test_parse_price_rejects_ambiguous_formats(raw):
    with pytest.raises(ValueError):
        parse_price(raw)


# ── ad filtering and card validation (the load-bearing step) ─────────────


def test_build_products_drops_ads_and_keeps_order():
    products = build_products(
        [
            card(name="ad one", is_ad=True),
            card(name="organic one", price="1,000", i_code="111"),
            card(name="ad two", is_ad=True),
            card(name="organic two", price="2,000", i_code="222"),
        ]
    )
    assert [p.name for p in products] == ["organic one", "organic two"]
    assert [p.price for p in products] == [1000, 2000]


def test_build_products_fails_loudly_when_every_card_is_an_ad():
    with pytest.raises(AssertionError, match="flagged as ads"):
        build_products([card(is_ad=True), card(is_ad=True)])


def test_build_products_fails_loudly_on_empty_snapshot():
    """An empty list means the desktop result tree never rendered; silently
    returning [] would let an ordering assertion pass on zero products."""
    with pytest.raises(AssertionError, match="mobile|not rendered|0 cards"):
        build_products([])


@pytest.mark.parametrize(
    "broken",
    [
        {"isAd": False, "name": "", "rawPrice": "100", "href": f"{GOODS}1"},
        {"isAd": False, "name": "x", "rawPrice": "100", "href": ""},
    ],
)
def test_build_products_rejects_incomplete_cards(broken):
    with pytest.raises(AssertionError, match="incomplete organic card"):
        build_products([broken])


def test_build_products_reports_the_offending_card_on_bad_price():
    with pytest.raises(AssertionError, match="cannot parse organic card"):
        build_products([card(price="面議")])


# ── relevance ────────────────────────────────────────────────────────────


def test_relevance_ratio_counts_literal_name_matches():
    products = [product(name="藍牙耳機"), product(name="耳罩"), product(name="有線耳機")]
    assert relevance_ratio(products, "耳機") == pytest.approx(2 / 3)


def test_assert_relevant_allows_synonym_misses_but_catches_collapse():
    mostly = [product(name="耳機") for _ in range(9)] + [product(name="珈琲豆")]
    assert_relevant(mostly, "耳機", minimum=0.7)

    collapsed = [product(name="洗衣精") for _ in range(9)] + [product(name="耳機")]
    with pytest.raises(AssertionError, match="relevance"):
        assert_relevant(collapsed, "耳機", minimum=0.7)


def test_relevance_needs_at_least_one_product():
    with pytest.raises(ValueError):
        relevance_ratio([], "耳機")


# ── cross-page uniqueness ────────────────────────────────────────────────


def test_assert_disjoint_passes_for_distinct_pages():
    assert_disjoint([product(i_code="1")], [product(i_code="2")], label="p1/p2")


def test_assert_disjoint_names_the_repeated_products():
    with pytest.raises(AssertionError, match="repeated"):
        assert_disjoint(
            [product(i_code="1"), product(i_code="2")],
            [product(i_code="2")],
            label="p1/p2",
        )


# ── product URL identity ─────────────────────────────────────────────────


def test_parse_goods_detail_identity_ignores_query_order():
    first = parse_product_identity(f"{GOODS}8940866&Area=search")
    reordered = parse_product_identity(
        "https://www.momoshop.com.tw/goods/GoodsDetail.jsp?Area=search&i_code=8940866"
    )
    assert first == reordered == ProductIdentity("goods-detail", "catalog", "8940866")


def test_parse_tp_identity_preserves_storefront():
    identity = parse_product_identity(
        "https://www.momoshop.com.tw/TP/TP0007361/goodsDetail/TP00073610000038"
    )
    assert identity == ProductIdentity(
        "tp", "tp", "TP00073610000038", storefront="TP0007361"
    )


def test_goods_detail_and_product_route_are_known_aliases():
    """Measured 2026-09-22: GoodsDetail.jsp?i_code=N redirects to /product/N."""
    source = parse_product_identity(f"{GOODS}15687251")
    destination = parse_product_identity("https://www.momoshop.com.tw/product/15687251")
    assert source.family != destination.family
    assert same_product(source, destination)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/goods/GoodsDetail.jsp?i_code=1",
        "https://www.momoshop.com.tw/goods/GoodsDetail.jsp?i_code=",
        "https://www.momoshop.com.tw/unknown/8940866",
        "https://www.momoshop.com.tw/TP/TP0007361/goodsDetail/TP99990000001",
    ],
)
def test_parse_product_identity_rejects_unknown_or_incomplete_urls(url):
    with pytest.raises(ValueError):
        parse_product_identity(url)


# ── ordering and range preconditions ─────────────────────────────────────


@pytest.mark.parametrize(
    ("price", "expected"),
    [(999, False), (1000, True), (1001, True), (1999, True), (2000, True), (2001, False)],
)
def test_range_boundaries(price, expected):
    if expected:
        assert_prices_in_range([price], 1000, 2000)
    else:
        with pytest.raises(AssertionError):
            assert_prices_in_range([price], 1000, 2000)


def test_sort_allows_equal_values_but_rejects_missing_preconditions():
    assert_prices_sorted([1000, 1000])
    with pytest.raises(ValueError):
        assert_prices_sorted([])
    with pytest.raises(ValueError):
        assert_prices_sorted([1000])
    with pytest.raises(ValueError):
        assert_prices_in_range([], 1000, 2000)
