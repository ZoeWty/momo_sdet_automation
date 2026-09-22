from __future__ import annotations

import re
from typing import Callable
from urllib.parse import parse_qs, quote, unquote, urlparse

from playwright.sync_api import Locator, Page, Request, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from parsers import Product, build_products, parse_product_identity, same_product


# Canonical result path. The searchType/curPage/viewport triple is exactly what
# momo itself puts in the address bar when a result page is opened directly.
# The origin is injected (see the base_url fixture in conftest.py) so the site
# under test is switched with --base-url or the PYTEST_BASE_URL environment
# variable, never by editing code.
RESULTS_PATH = "search/{keyword}?searchType=1&curPage=1&viewport=desktop&_isFuzzy=0"
RESULT_TIMEOUT_MS = 30_000
NAVIGATION_TIMEOUT_MS = 60_000
PRICE_COMMIT_ATTEMPTS = 4
PRICE_COMMIT_TIMEOUT_MS = 6_000

# momo serves a different component tree to narrow viewports: below roughly
# 1024px the result list is `ul.goods-mobile-panel` and `ul.listAreaUl` does
# not exist at all. Every selector in this class targets the desktop tree, so
# the 1440x900 viewport pinned in conftest.py is a functional precondition,
# not a cosmetic choice. Overriding it makes every locator here resolve to
# nothing; build_products() says so in its failure message.
MIN_DESKTOP_WIDTH = 1024

# One definition of the organic content currently on screen. Ads are excluded
# because their refresh must never prove that search results changed.
# Href/name/price keep the fingerprint useful even when viewProdId is absent.
_FINGERPRINT_EXPR = (
    "(() => {"
    "  const vis = e => Boolean(e && (e.offsetWidth || e.offsetHeight || e.getClientRects().length));"
    "  const list = [...document.querySelectorAll('ul.listAreaUl')].find(vis);"
    "  return list"
    "    ? [...list.querySelectorAll(':scope > li')]"
    "        .filter(card => !card.querySelector('.sponsor-tag, ins.tenMaxAdTag'))"
    "        .map(card => ["
    "          card.querySelector('a.prdName')?.href || '',"
    "          card.querySelector('a.prdName')?.textContent?.trim() || '',"
    "          card.querySelector('.price')?.textContent?.trim() || ''"
    "        ].join('\\u001f')).join('\\u001e')"
    "    : '';"
    "})()"
)


class SearchPage:
    """The user-facing search surface plus its result-completion contract."""

    def __init__(self, page: Page, base_url: str) -> None:
        self.page = page
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        page.set_default_timeout(15_000)
        page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)

    @property
    def search_input(self) -> Locator:
        return self.page.locator(
            "#header-search-input:visible, input[name='search-input']:visible"
        )

    @property
    def search_button(self) -> Locator:
        return self.page.get_by_role("button", name="搜尋", exact=True)

    @property
    def query_heading(self) -> Locator:
        return self.page.locator("h1 .keyword-placeholder:visible")

    @property
    def visible_cards(self) -> Locator:
        return self.page.locator("ul.listAreaUl:visible > li")

    @property
    def empty_message(self) -> Locator:
        return self.page.locator(".noResultText:visible")

    @property
    def price_sort_control(self) -> Locator:
        return self.page.locator("#searchType li").filter(
            has_text=re.compile(r"^價格$")
        )

    @property
    def price_min_input(self) -> Locator:
        return self.page.locator("#priceS")

    @property
    def price_max_input(self) -> Locator:
        return self.page.locator("#priceE")

    @property
    def price_confirm_button(self) -> Locator:
        return self.page.locator("a.priceBtn[title='確認']")

    @property
    def visible_pagination(self) -> Locator:
        return self.page.locator("ul.pagination:visible")

    def _assert_desktop_viewport(self) -> None:
        width = self.page.viewport_size["width"] if self.page.viewport_size else 0
        assert width >= MIN_DESKTOP_WIDTH, (
            f"viewport width {width} is below {MIN_DESKTOP_WIDTH}; momo renders its "
            "mobile result list and none of this page object's selectors exist. "
            "See browser_context_args in conftest.py."
        )

    def open_results(self, keyword: str) -> None:
        """Enter the result page directly, bypassing the home page.

        Only the tests whose subject IS the search box go through the home
        page. Everything else deep-links, for a measured reason: after a
        client-side search the result page is a soft navigation and the price
        filter control loses roughly one click in three (2026-09-22: 2/3 via
        the search box vs 3/3 via a deep link). A full document load binds the
        handlers before the test can reach them. It is also one fewer page
        load per test.
        """
        self._assert_desktop_viewport()
        self.page.goto(
            self.base_url + RESULTS_PATH.format(keyword=quote(keyword)),
            wait_until="domcontentloaded",
        )
        self.page.wait_for_load_state("load", timeout=RESULT_TIMEOUT_MS)
        self._finish_update(keyword, {"searchType": "1", "curPage": "1"}, set())

    def open(self) -> None:
        self._assert_desktop_viewport()
        self.page.goto(self.base_url, wait_until="domcontentloaded")
        # The header is visible before React has necessarily finished hydrating.
        # Waiting for the browser load event keeps an early click from being
        # handled by markup that is about to be replaced.
        self.page.wait_for_load_state("load", timeout=RESULT_TIMEOUT_MS)
        expect(self.search_input).to_be_visible()

    def search(self, keyword: str, *, submit: str = "button") -> None:
        expect(self.search_input).to_have_count(1)
        self.search_input.fill(keyword)
        expect(self.search_input).to_have_value(keyword)

        def action() -> None:
            if submit == "button":
                expect(self.search_button).to_have_count(1)
                self.search_button.click()
            elif submit == "enter":
                self.search_input.press("Enter")
            else:
                raise ValueError(f"unsupported submit method: {submit!r}")

        self._perform_update(action, keyword=keyword, expected_params={})

    def query_from_url(self) -> str:
        keyword, _ = self._query_state(self.page.url)
        if keyword is None:
            raise AssertionError(f"not a search URL: {self.page.url}")
        return keyword

    def url_params(self) -> dict[str, str]:
        _, params = self._query_state(self.page.url)
        return params

    def assert_query_state(self, keyword: str) -> None:
        assert self.query_from_url() == keyword
        expect(self.query_heading).to_have_count(1)
        expect(self.query_heading).to_have_text(keyword)
        expect(self.search_input).to_have_count(1)
        expect(self.search_input).to_have_value(keyword)

    def current_page(self) -> int:
        url_page = int(self.url_params().get("curPage", "1"))
        selected = self.visible_pagination.locator("a.pagination-link.selected")
        expect(selected).to_have_count(1)
        ui_page = int(selected.inner_text().strip())
        assert ui_page == url_page, f"URL page {url_page} != visible page {ui_page}"
        return ui_page

    def organic_products(self) -> list[Product]:
        """One atomic DOM read, then pure parsing.

        Reading every field in a single evaluate_all avoids stitching together
        cards from different render passes, which matters because momo
        re-renders the list while ads settle.
        """
        raw_cards = self.visible_cards.evaluate_all(
            """
            cards => cards.map(card => ({
              isAd: Boolean(card.querySelector('.sponsor-tag, ins.tenMaxAdTag')),
              name: (card.querySelector('a.prdName')?.textContent || '').trim(),
              rawPrice: (card.querySelector('.price')?.textContent || '').trim(),
              href: card.querySelector('a.prdName')?.href || ''
            }))
            """
        )
        return build_products(raw_cards)

    def set_price_sort(self, direction: str) -> None:
        target = {"ascending": "2", "descending": "3"}.get(direction)
        if target is None:
            raise ValueError(f"unsupported direction: {direction!r}")

        expect(self.price_sort_control).to_have_count(1)
        for _ in range(2):
            current = self.url_params().get("searchType", "1")
            if current == target:
                break

            retained = {
                key: value
                for key, value in self.url_params().items()
                if key in {"_advPriceS", "_advPriceE"}
            }

            def predicate(request: Request) -> bool:
                params = self._request_params(request)
                return (
                    self._request_matches(request, self.query_from_url(), retained, set())
                    and params.get("searchType") in {"2", "3"}
                    and params.get("searchType") != current
                )

            with self.page.expect_request(predicate, timeout=RESULT_TIMEOUT_MS) as pending:
                self.price_sort_control.click()
            request = pending.value
            next_code = self._request_params(request)["searchType"]
            expected = {**retained, "searchType": next_code}
            self._finish_update(self.query_from_url(), expected, set())
            self._wait_for_price_order(descending=next_code == "3")

        assert self.url_params().get("searchType") == target, (
            f"price sort did not reach searchType={target} after two toggles"
        )
        classes = set((self.price_sort_control.get_attribute("class") or "").split())
        expected_class = "up" if target == "2" else "down"
        assert {"selected", expected_class} <= classes, (
            f"price direction state mismatch: target={target}, classes={classes}"
        )

    def apply_price_filter(self, minimum: int, maximum: int) -> None:
        keyword = self.query_from_url()
        sort = self.url_params().get("searchType", "1")
        self._commit_price_panel(str(minimum), str(maximum), label=f"apply {minimum}-{maximum}")
        # Only the sort is required to survive in the address bar. Measured
        # 2026-09-22: after a correct apply the URL gained _advPriceS in 3 of 4
        # runs and the range inputs were sometimes left empty, even though the
        # result set was filtered correctly every time. Those two are momo's
        # own chrome, not a contract. The range itself is proved by the request
        # _commit_price_panel insists on, plus the prices the caller asserts.
        self._finish_update(keyword, {"searchType": sort}, set())

    def clear_price_filter(self) -> None:
        keyword = self.query_from_url()
        sort = self.url_params().get("searchType", "1")
        self._commit_price_panel("", "", label="clear")
        self._finish_update(keyword, {"searchType": sort}, {"_advPriceS", "_advPriceE"})

    def _commit_price_panel(self, low: str, high: str, *, label: str) -> None:
        """Re-issue the range until the request leaving the browser carries it.

        Two separate momo behaviours make this necessary, both measured on
        2026-09-22:

        * A press is sometimes dropped entirely -- no request at all -- while
          the list is still re-rendering. Waiting longer never recovers a lost
          press, so the whole interaction (fill + press) is re-issued.
        * A press sometimes sends a *partial* range. One observed request
          carried only `_advPriceE=2000` and dropped the floor. The result
          count still changed, so any success condition based on "the page
          changed" accepts that half-applied filter -- and the page then
          showed $473 under a $1000 floor. That is the bug this method used
          to have.

        Success is therefore the request itself, never a side effect of it.

        This retries an *interaction*, never an assertion: every range and
        ordering claim downstream still has to hold on its first look, so a
        real product defect cannot be papered over here.
        """
        keyword = self.query_from_url()
        if low or high:
            expected: dict[str, str] = {"_advPriceS": low, "_advPriceE": high}
            forbidden: set[str] = set()
        else:
            expected, forbidden = {}, {"_advPriceS", "_advPriceE"}

        self._wait_until_results_settle()
        for _ in range(PRICE_COMMIT_ATTEMPTS):
            stale = self._list_fingerprint()
            self._type_range(low, high)
            try:
                with self.page.expect_request(
                    lambda request: self._request_matches(
                        request, keyword, expected, forbidden
                    ),
                    timeout=PRICE_COMMIT_TIMEOUT_MS,
                ):
                    self.price_confirm_button.click()
            except PlaywrightTimeoutError:
                continue
            # The request proves the range was dispatched; the cards still lag
            # it and arrive over more than one paint, so a read taken now can
            # mix the old and new result sets (measured: 14 of 30 cards from
            # the previous set). Wait for the list to actually turn over.
            self._wait_for_list_change(stale)
            return
        raise AssertionError(
            f"price panel ({label}) never dispatched a request carrying the exact "
            f"range after {PRICE_COMMIT_ATTEMPTS} attempts; wanted "
            f"{expected or 'no range parameters'}"
        )

    def _wait_until_results_settle(self) -> None:
        """Wait for the result summary to hold still before touching a control.

        The list re-renders while sponsored slots resolve, and the filter panel
        re-renders with it; acting inside that window is what loses a press.
        A stability condition, not a sleep: it returns as soon as the summary
        stops changing. Markers are cleared first so a previous timeout cannot
        let the next call finish early.
        """
        self.page.evaluate("() => { delete window.__momoSettle; delete window.__momoSettleAt; }")
        self.page.wait_for_function(
            """() => {
              const nodes = [...document.querySelectorAll('span.page-number')]
                .map(node => node.textContent.trim()).filter(Boolean);
              const now = nodes[0] || '';
              if (!now) return false;
              if (window.__momoSettle !== now) {
                window.__momoSettle = now;
                window.__momoSettleAt = Date.now();
                return false;
              }
              return Date.now() - window.__momoSettleAt >= 1000;
            }""",
            timeout=RESULT_TIMEOUT_MS,
        )

    def _type_range(self, low: str, high: str) -> None:
        """Enter the range with real key events.

        fill() writes the DOM value in one shot; momo's inputs are React
        controlled, and a 確認 press that follows too closely was observed
        submitting the *previous* range. Typing character by character and
        then blurring gives the component the events it listens for.
        """
        for field, value in ((self.price_min_input, low), (self.price_max_input, high)):
            field.click()
            field.press("ControlOrMeta+a")
            if value:
                field.press_sequentially(value, delay=25)
            else:
                field.press("Delete")
            expect(field).to_have_value(value)
        self.price_max_input.blur()

    def _list_fingerprint(self) -> str:
        """Visible organic result content, in order. Empty string if none."""
        return self.page.evaluate("() => " + _FINGERPRINT_EXPR)

    def _wait_for_list_change(self, stale: str) -> None:
        """Block until the organic result content differs from `stale` and holds still."""
        self.page.evaluate("() => { delete window.__momoList; delete window.__momoListAt; }")
        self.page.wait_for_function(
            "stale => {"
            "  const now = " + _FINGERPRINT_EXPR + ";"
            "  if (now === stale) return false;"
            "  if (window.__momoList !== now) {"
            "    window.__momoList = now; window.__momoListAt = Date.now(); return false;"
            "  }"
            "  return Date.now() - window.__momoListAt >= 400;"
            "}",
            arg=stale,
            timeout=RESULT_TIMEOUT_MS,
        )

    def _wait_for_price_order(self, *, descending: bool) -> None:
        """Wait until the visible organic prices satisfy the requested order."""
        self.page.wait_for_function(
            """descending => {
              const visible = element => Boolean(
                element && (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
              );
              const list = [...document.querySelectorAll('ul.listAreaUl')].find(visible);
              if (!list) return false;
              const texts = [...list.querySelectorAll(':scope > li')]
                .filter(card => !card.querySelector('.sponsor-tag, ins.tenMaxAdTag'))
                .map(card => card.querySelector('.price')?.textContent?.trim() || '');
              const prices = texts.map(text => {
                const match = text.match(/^(0|[1-9]\\d*|[1-9]\\d{0,2}(?:,\\d{3})+)(?:起)?$/);
                return match ? Number(match[1].replaceAll(',', '')) : NaN;
              });
              if (prices.length < 2 || prices.some(Number.isNaN)) return false;
              return prices.every((price, index) => index === 0 || (
                descending ? prices[index - 1] >= price : prices[index - 1] <= price
              ));
            }""",
            arg=descending,
            timeout=RESULT_TIMEOUT_MS,
        )

    def go_to_page(self, page_number: int) -> None:
        expect(self.visible_pagination).to_have_count(1)
        stale = self._list_fingerprint()
        link = self.visible_pagination.locator("a.pagination-link").filter(
            has_text=re.compile(rf"^{page_number}$")
        )
        expect(link).to_have_count(1)
        retained = {
            key: value
            for key, value in self.url_params().items()
            if key in {"searchType", "_advPriceS", "_advPriceE"}
        }
        expected = {**retained, "curPage": str(page_number)}
        self._perform_update(
            link.click,
            keyword=self.query_from_url(),
            expected_params=expected,
        )
        self._wait_for_list_change(stale)
        assert self.current_page() == page_number

    def assert_empty_result(self, keyword: str) -> None:
        expect(self.empty_message).to_have_count(1)
        expect(self.empty_message).to_contain_text("查無")
        expect(self.empty_message).to_contain_text(keyword)
        assert self.visible_cards.count() == 0

    def open_first_organic_product(self) -> tuple[Product, Page]:
        source = self.organic_products()[0]
        # Locate by the href captured in that same snapshot rather than by
        # position: re-reading "the first organic card" would be a second,
        # independent read of a list momo may have re-rendered in between,
        # which turns a normal re-render into a spurious test failure.
        link = self.page.locator(
            f'ul.listAreaUl:visible > li a.prdName[href="{source.href}"]'
        ).first
        expect(link).to_be_visible()

        if (link.get_attribute("target") or "").lower() == "_blank":
            with self.page.context.expect_page() as pending_page:
                link.click()
            destination_page = pending_page.value
            destination_page.wait_for_load_state("domcontentloaded")
        else:
            link.click()
            destination_page = self.page

        def correct_destination(url: str) -> bool:
            try:
                return same_product(source.identity, parse_product_identity(url))
            except ValueError:
                return False

        destination_page.wait_for_url(
            correct_destination,
            wait_until="domcontentloaded",
            timeout=NAVIGATION_TIMEOUT_MS,
        )
        return source, destination_page

    def _perform_update(
        self,
        action: Callable[[], None],
        *,
        keyword: str,
        expected_params: dict[str, str],
        forbidden_params: set[str] | None = None,
    ) -> None:
        forbidden = forbidden_params or set()
        predicate = lambda request: self._request_matches(
            request, keyword, expected_params, forbidden
        )
        # A Next.js RSC request may remain pending even after the UI has
        # committed, and Response.finished() on that stream can wait forever.
        # Request start proves the action was dispatched; the bounded UI
        # contract below proves the user-visible result completed.
        with self.page.expect_request(predicate, timeout=RESULT_TIMEOUT_MS):
            action()
        self._finish_update(keyword, expected_params, forbidden)

    def _finish_update(
        self,
        keyword: str,
        expected_params: dict[str, str],
        forbidden_params: set[str],
    ) -> None:
        # URL state is judged by the same Python predicate the request matcher
        # uses, so the rule lives in exactly one place. The in-page function
        # below is reserved for the one thing Python cannot observe atomically:
        # that the visible heading and the visible result outcome belong to the
        # same render pass.
        self.page.wait_for_url(
            lambda url: self._state_matches(
                url, keyword, expected_params, forbidden_params
            ),
            wait_until="commit",
            timeout=RESULT_TIMEOUT_MS,
        )
        self.page.wait_for_function(
            r"""
            keyword => {
              const visible = element => Boolean(
                element && (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
              );
              const heading = [...document.querySelectorAll('h1 .keyword-placeholder')].find(visible);
              if (heading?.textContent.trim() !== keyword) return false;
              const list = [...document.querySelectorAll('ul.listAreaUl')].find(visible);
              const cards = list ? list.querySelectorAll(':scope > li').length : 0;
              const empty = [...document.querySelectorAll('.noResultText')].find(visible);
              return (cards > 0 && !empty) || (cards === 0 && Boolean(empty));
            }
            """,
            arg=keyword,
            timeout=RESULT_TIMEOUT_MS,
        )
        self.assert_query_state(keyword)

    @staticmethod
    def _query_state(url: str) -> tuple[str | None, dict[str, str]]:
        """Decode a momo search URL into (keyword, last-value-wins params)."""
        parsed = urlparse(url)
        params = {
            key: values[-1]
            for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
        }
        if not parsed.path.startswith("/search/"):
            return None, params
        return unquote(parsed.path.removeprefix("/search/").rstrip("/")), params

    @classmethod
    def _state_matches(
        cls,
        url: str,
        keyword: str,
        expected_params: dict[str, str],
        forbidden_params: set[str],
    ) -> bool:
        found, params = cls._query_state(url)
        if found != keyword:
            return False
        if any(params.get(key) != value for key, value in expected_params.items()):
            return False
        return not any(key in params for key in forbidden_params)

    @staticmethod
    def _request_params(request: Request) -> dict[str, str]:
        return SearchPage._query_state(request.url)[1]

    @classmethod
    def _request_matches(
        cls,
        request: Request,
        keyword: str,
        expected_params: dict[str, str],
        forbidden_params: set[str],
    ) -> bool:
        if request.resource_type not in {"document", "fetch"}:
            return False
        return cls._state_matches(request.url, keyword, expected_params, forbidden_params)
