from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Literal, Mapping, Sequence
from urllib.parse import parse_qs, urlparse


# Observed on live search cards: plain digits, thousands separators, and a
# "from" suffix (e.g. "1,850起"). The "(售價已折)" note lives on the enclosing
# `.money` element, not on `.price`, so it is deliberately NOT accepted here —
# seeing it would mean the DOM contract moved and we want a loud failure.
_PRICE = re.compile(r"^\s*(?P<amount>(?:0|[1-9]\d*|[1-9]\d{0,2}(?:,\d{3})+))(?:起)?\s*$")
_TP_PATH = re.compile(
    r"^/TP/(?P<store>TP\d+)/goodsDetail/(?P<product>TP\d+)$",
    re.IGNORECASE,
)
_PRODUCT_PATH = re.compile(r"^/product/(?P<product>\d+)$", re.IGNORECASE)


@dataclass(frozen=True)
class ProductIdentity:
    family: Literal["goods-detail", "product", "tp"]
    namespace: Literal["catalog", "tp"]
    value: str
    storefront: str | None = None


@dataclass(frozen=True)
class Product:
    name: str
    raw_price: str
    price: int
    href: str
    identity: ProductIdentity


def parse_price(raw_price: str | None) -> int:
    """Parse only the price formats observed in momo search cards."""
    if not isinstance(raw_price, str):
        raise ValueError(f"price must be text, got {raw_price!r}")

    match = _PRICE.fullmatch(raw_price)
    if not match:
        raise ValueError(f"unsupported price format: {raw_price!r}")
    return int(match.group("amount").replace(",", ""))


def parse_product_identity(url: str) -> ProductIdentity:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname != "www.momoshop.com.tw":
        raise ValueError(f"unsupported product host: {url!r}")

    if parsed.path.lower() == "/goods/goodsdetail.jsp":
        ids = parse_qs(parsed.query, keep_blank_values=True).get("i_code", [])
        if len(ids) != 1 or not ids[0].isdigit():
            raise ValueError(f"missing or invalid i_code: {url!r}")
        return ProductIdentity("goods-detail", "catalog", ids[0])

    product_match = _PRODUCT_PATH.fullmatch(parsed.path)
    if product_match:
        return ProductIdentity("product", "catalog", product_match.group("product"))

    tp_match = _TP_PATH.fullmatch(parsed.path)
    if tp_match:
        store = tp_match.group("store").upper()
        product = tp_match.group("product").upper()
        if not product.startswith(store):
            raise ValueError(f"TP product does not belong to storefront: {url!r}")
        return ProductIdentity("tp", "tp", product, storefront=store)

    raise ValueError(f"unsupported product path: {url!r}")


def same_product(source: ProductIdentity, destination: ProductIdentity) -> bool:
    """Compare known route aliases without discarding their URL family."""
    return (
        source.namespace == destination.namespace
        and source.value == destination.value
        and source.storefront == destination.storefront
    )


def build_products(raw_cards: Sequence[Mapping[str, object]]) -> list[Product]:
    """Drop ad cards, then parse and validate every remaining card.

    Pure function: it takes the DOM snapshot the page object already captured
    and performs no I/O of its own. Ad exclusion is the load-bearing step for
    every ordering and range assertion, so it is unit-tested here rather than
    only exercised indirectly through the browser.
    """
    ads = sum(1 for card in raw_cards if card.get("isAd"))
    organic = [card for card in raw_cards if not card.get("isAd")]
    if not organic:
        raise AssertionError(
            f"no organic products in snapshot: {len(raw_cards)} cards, {ads} flagged as ads. "
            f"If those numbers are equal the ad markers may have changed; "
            f"if both are 0 the desktop result list was not rendered "
            f"(momo serves a different layout below ~1024px wide). Cards: {list(raw_cards)!r}"
        )

    products: list[Product] = []
    for index, card in enumerate(organic):
        name = card.get("name")
        href = card.get("href")
        if not name or not href:
            raise AssertionError(f"incomplete organic card {index}: {card!r}")
        try:
            price = parse_price(card.get("rawPrice"))  # type: ignore[arg-type]
            identity = parse_product_identity(href)  # type: ignore[arg-type]
        except ValueError as error:
            raise AssertionError(
                f"cannot parse organic card {index}: {card!r}; {error}"
            ) from error
        products.append(
            Product(
                name=str(name),
                raw_price=str(card.get("rawPrice")),
                price=price,
                href=str(href),
                identity=identity,
            )
        )
    return products


def relevance_ratio(products: Sequence[Product], keyword: str) -> float:
    """Share of organic product names literally containing the keyword."""
    if not products:
        raise ValueError("relevance needs at least 1 product")
    hits = sum(1 for product in products if keyword in product.name)
    return hits / len(products)


def assert_relevant(products: Sequence[Product], keyword: str, *, minimum: float) -> None:
    """Assert the result set is about the keyword, with room for synonyms.

    Deliberately not 1.0: on 2026-09-22 "耳機" scored 24/24 but "咖啡" scored
    22/24, and both misses were correct behaviour ("珈琲豆" is a variant
    spelling, "二合一" is instant coffee). A 1.0 gate would fail the product
    for working. The gate exists to catch a collapsed ranker, not to freeze
    today's catalogue.
    """
    ratio = relevance_ratio(products, keyword)
    misses = [product.name for product in products if keyword not in product.name]
    assert ratio >= minimum, (
        f"relevance {ratio:.0%} < {minimum:.0%} for {keyword!r} "
        f"across {len(products)} organic results; non-matching names: {misses!r}"
    )


def assert_prices_sorted(prices: Iterable[int], *, descending: bool = False) -> None:
    values = list(prices)
    if len(values) < 2:
        raise ValueError(f"sorting needs at least 2 prices, got {values!r}")
    expected = sorted(values, reverse=descending)
    assert values == expected, f"prices are not {'descending' if descending else 'ascending'}: {values!r}"


def assert_prices_in_range(prices: Iterable[int], minimum: int, maximum: int) -> None:
    values = list(prices)
    if minimum > maximum:
        raise ValueError(f"invalid range: {minimum} > {maximum}")
    if not values:
        raise ValueError("range check needs at least 1 price")
    outside = [price for price in values if not minimum <= price <= maximum]
    assert not outside, f"prices outside [{minimum}, {maximum}]: {outside!r}; all={values!r}"


def assert_disjoint(first: Sequence[Product], second: Sequence[Product], *, label: str) -> None:
    """Two result pages must not repeat the same organic product.

    Ads are already excluded by build_products, which matters: the same ad
    creative legitimately reappears across pages, so an unfiltered comparison
    reports momo's normal behaviour as a defect.
    """
    left = {product.identity for product in first}
    right = {product.identity for product in second}
    overlap = left & right
    assert not overlap, f"{label}: {len(overlap)} organic product(s) repeated: {overlap!r}"
