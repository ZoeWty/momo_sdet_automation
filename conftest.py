import pytest


DEFAULT_BASE_URL = "https://www.momoshop.com.tw/"


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict) -> dict:
    """Desktop conditions from the test plan.

    The viewport is a functional precondition, not styling: momo serves a
    different component tree below roughly 1024px (`ul.goods-mobile-panel`
    instead of `ul.listAreaUl`), so every selector in pages/search_page.py
    resolves to nothing on a narrow viewport. SearchPage.open() asserts this
    up front so the failure names the cause instead of reporting an empty list.
    """
    return {
        **browser_context_args,
        "locale": "zh-TW",
        "viewport": {"width": 1440, "height": 900},
    }


@pytest.fixture(scope="session")
def base_url(base_url: str | None) -> str:
    """Origin of the site under test.

    Overrides pytest-base-url's fixture only to supply a default, so a bare
    `pytest` still runs. The site is switched without touching code:

        pytest --base-url https://www.momoshop.com.tw/
        PYTEST_BASE_URL=... pytest
    """
    return base_url or DEFAULT_BASE_URL
