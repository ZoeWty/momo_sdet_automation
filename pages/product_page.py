from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from parsers import ProductIdentity, parse_product_identity


CONTENT_TIMEOUT_MS = 60_000


class ProductPage:
    """The product detail page a search result hands off to.

    momo serves two templates behind three URL families, so the identity
    parsed from the URL decides which selectors apply. That branch lives here
    rather than in SearchPage on purpose: a change to the product page should
    not require touching the search page object.
    """

    def __init__(self, page: Page) -> None:
        self.page = page
        self.identity: ProductIdentity = parse_product_identity(page.url)

    def content(self) -> tuple[str, str]:
        """Return (name, price text) once both are actually rendered."""
        name, price = (
            self._catalog_content()
            if self.identity.namespace == "catalog"
            else self._storefront_content()
        )
        assert name, "product name is empty"
        assert re.search(r"\d", price), f"product price is missing: {price!r}"
        return name, price

    def _catalog_content(self) -> tuple[str, str]:
        expect(self.page.locator("[data-testid='goods-info']")).to_be_visible(
            timeout=CONTENT_TIMEOUT_MS
        )
        name = self.page.locator("[data-testid='goods-name-content']")
        price = self.page.locator("[data-testid='web-goods-price-container']")
        expect(name).to_be_visible()
        expect(price).to_be_visible()
        return name.inner_text().strip(), price.inner_text().strip()

    def _storefront_content(self) -> tuple[str, str]:
        expect(self.page.locator("main:visible")).to_be_visible(
            timeout=CONTENT_TIMEOUT_MS
        )
        name = self.page.locator("meta[property='og:title']")
        price = self.page.locator(".goods-detail-price:visible")
        expect(name).to_have_count(1)
        expect(price).to_be_visible()
        return (name.get_attribute("content") or "").strip(), price.inner_text().strip()
