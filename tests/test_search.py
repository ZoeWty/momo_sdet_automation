import pytest

from pages.product_page import ProductPage
from pages.search_page import SearchPage
from parsers import (
    assert_disjoint,
    assert_prices_in_range,
    assert_prices_sorted,
    assert_relevant,
    same_product,
)


KEYWORD = "耳機"
MIN_PRICE = 1000
MAX_PRICE = 2000
# A collapse canary, not a relevance metric. Measured 2026-09-22: 耳機 24/24,
# 咖啡 22/24 (both misses correct — "珈琲豆" is a variant spelling, "二合一" is
# instant coffee). The floor sits far below the worst measured value so that
# synonym-heavy result sets cannot block a submission; it only fires when the
# ranker has stopped working. It cannot catch subtler degradation, and a
# keyword-stuffed catalogue would satisfy it — see README "Known limits".
MIN_RELEVANCE = 0.5


@pytest.fixture
def search_page(page, base_url):
    return SearchPage(page, base_url)


@pytest.mark.smoke
@pytest.mark.parametrize("submit", ["button", "enter"], ids=["MM-FH-01-button", "MM-FH-01-enter"])
def test_mm_fh_01_home_search_returns_structured_relevant_results(search_page, submit):
    search_page.open()
    search_page.search(KEYWORD, submit=submit)

    search_page.assert_query_state(KEYWORD)
    assert search_page.current_page() == 1
    products = search_page.organic_products()
    assert search_page.empty_message.count() == 0
    assert_relevant(products, KEYWORD, minimum=MIN_RELEVANCE)


@pytest.mark.smoke
def test_mm_fh_02_search_can_change_from_a_result_query_to_b(search_page):
    search_page.open()
    search_page.search(KEYWORD)
    search_page.search("咖啡")

    search_page.assert_query_state("咖啡")
    products = search_page.organic_products()
    assert_relevant(products, "咖啡", minimum=MIN_RELEVANCE)


@pytest.mark.smoke
@pytest.mark.parametrize(
    ("direction", "descending"),
    [("ascending", False), ("descending", True)],
    ids=["MM-FH-03-ascending", "MM-FH-03-descending"],
)
def test_mm_fh_03_price_sort_direction(search_page, direction, descending):
    search_page.open_results(KEYWORD)
    search_page.set_price_sort(direction)

    prices = [product.price for product in search_page.organic_products()]
    assert_prices_sorted(prices, descending=descending)


@pytest.mark.smoke
def test_mm_fh_04_price_filter_survives_pagination(search_page):
    search_page.open_results(KEYWORD)
    search_page.apply_price_filter(MIN_PRICE, MAX_PRICE)

    page_one = search_page.organic_products()
    assert_prices_in_range([p.price for p in page_one], MIN_PRICE, MAX_PRICE)

    search_page.go_to_page(2)
    page_two = search_page.organic_products()
    assert_prices_in_range([p.price for p in page_two], MIN_PRICE, MAX_PRICE)

    # Same two pages already loaded, so this invariant is free. Ads are
    # excluded upstream, which is what makes it meaningful: ad creatives do
    # legitimately repeat across pages.
    assert_disjoint(page_one, page_two, label="filtered pages 1 and 2")


def test_mm_fh_05_price_filter_can_be_cleared(search_page):
    search_page.open_results(KEYWORD)
    initial_products = search_page.organic_products()
    assert any(
        product.price < MIN_PRICE or product.price > MAX_PRICE
        for product in initial_products
    ), "test data needs an initially visible product outside the filter range"

    search_page.apply_price_filter(MIN_PRICE, MAX_PRICE)
    assert_prices_in_range(
        [product.price for product in search_page.organic_products()],
        MIN_PRICE,
        MAX_PRICE,
    )

    search_page.clear_price_filter()

    assert "_advPriceS" not in search_page.url_params()
    assert "_advPriceE" not in search_page.url_params()
    cleared_products = search_page.organic_products()
    assert any(
        product.price < MIN_PRICE or product.price > MAX_PRICE
        for product in cleared_products
    ), "clearing the filter did not restore any visible out-of-range product"


def test_mm_fh_06_product_card_hands_off_to_matching_product_page(search_page):
    search_page.open_results(KEYWORD)

    source, destination_page = search_page.open_first_organic_product()

    product_page = ProductPage(destination_page)
    assert same_product(source.identity, product_page.identity)
    product_page.content()


def test_mm_fh_07_price_order_continues_across_pages(search_page):
    """Ordering that only holds within a page is a real, user-visible defect
    that no single-page assertion can see."""
    search_page.open_results(KEYWORD)
    search_page.set_price_sort("descending")

    page_one = [product.price for product in search_page.organic_products()]
    assert_prices_sorted(page_one, descending=True)

    search_page.go_to_page(2)
    assert search_page.url_params().get("searchType") == "3"
    page_two = [product.price for product in search_page.organic_products()]
    assert_prices_sorted(page_two, descending=True)

    # <= not <, because equal prices may legitimately straddle a page break.
    assert max(page_two) <= min(page_one), (
        f"descending order breaks across pages: page 1 min {min(page_one)} "
        f"< page 2 max {max(page_two)}"
    )


@pytest.mark.smoke
def test_mm_fn_01_no_results_can_recover_to_normal_search(search_page):
    missing = "zzzqqqxxxnotexist12345"
    search_page.open()
    search_page.search(missing)
    search_page.assert_empty_result(missing)

    search_page.search(KEYWORD)

    search_page.assert_query_state(KEYWORD)
    search_page.organic_products()  # raises unless the recovered page parses
    assert search_page.empty_message.count() == 0


@pytest.mark.parametrize(
    "operation_order",
    ["filter-then-sort", "sort-then-filter"],
    ids=["MM-FE-01-filter-then-sort", "MM-FE-01-sort-then-filter"],
)
def test_mm_fe_01_filter_and_sort_state_survive_both_operation_orders(search_page, operation_order):
    search_page.open_results(KEYWORD)

    if operation_order == "filter-then-sort":
        search_page.apply_price_filter(MIN_PRICE, MAX_PRICE)
        search_page.set_price_sort("ascending")
    else:
        search_page.set_price_sort("ascending")
        search_page.apply_price_filter(MIN_PRICE, MAX_PRICE)

    assert search_page.url_params().get("searchType") == "2"
    prices = [product.price for product in search_page.organic_products()]
    assert_prices_in_range(prices, MIN_PRICE, MAX_PRICE)
    assert_prices_sorted(prices)
