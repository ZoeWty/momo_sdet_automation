# momo Search UI Automation

Playwright + pytest against momo's **production** site. No mocks, no route
interception, no response rewriting — the suite is closer to a synthetic
monitor than to a conventional E2E suite, and every design choice below
follows from that.

## What this submission verifies

| Layer | Count | Runtime | Needs network? |
|---|---|---|---|
| Offline helpers | 36 | ~0.02 s | no |
| UI executions | 12 | ~80 s | yes (live site) |
| of which P0 smoke | 7 | ~45 s | yes |

**Stability evidence:** three consecutive full runs on 2026-09-22 —
`48 passed` / `48 passed` / `48 passed`, 79–82 s each, **zero retries and
zero reruns**. Samples from one machine on one day, not a guarantee.

## Setup

Verified on macOS 15.6.1 arm64, Python 3.14.7, pytest 9.1.1,
pytest-playwright 0.9.0, Playwright 1.63.0, Chromium 153.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## Run

Offline helpers only — no browser, no network, finishes instantly:

```bash
python -m pytest tests/test_parsers.py
```

The P0 smoke:

```bash
python -m pytest -m smoke
```

Everything:

```bash
python -m pytest
```

Add `--headed` for a live demo. Defaults are Chromium, a single worker and
zero retries. Failures keep a screenshot and a trace under `test-results/`:

```bash
python -m playwright show-trace test-results/<trace-file>.zip
```

## Test cases

| ID | Priority | What it pins down |
|---|---|---|
| `MM-FH-01` ×2 | P0 | Search from the home page by button and by Enter: URL, heading and input agree; every organic card carries id, name, parseable price, usable link; results are **relevant** to the keyword |
| `MM-FH-02` | P0 | A→B query change inside one tab leaves no state from A |
| `MM-FH-03` ×2 | P0 | Price ascending and descending are actually ordered, **after ads are excluded** |
| `MM-FH-04` | P0 | A price range holds across pagination, and the two pages share **no** organic product |
| `MM-FH-05` | P1 | Clearing the range restores an unfiltered result set |
| `MM-FH-06` | P1 | A result card hands off to the same product, across momo's three URL families |
| `MM-FH-07` | P1 | Price order **continues across a page boundary** — a defect no single-page assertion can see |
| `MM-FN-01` | P0 | No-result state renders, and the page recovers to a normal search |
| `MM-FE-01` ×2 | P1 | Filter→sort and sort→filter end in the same state |

Manual, deliberately not faked as automation: `MM-NH-01` (full keyboard path)
and `MM-NE-01` (three timing samples). There is no product SLA, so timings are
reported as samples, never graded.

## Design decisions worth knowing before reading the code

**Ads are excluded before any ordering or range assertion.** Roughly 6 of 30
cards are sponsored (`.sponsor-tag` / `ins.tenMaxAdTag`), they sit at the top,
and they do not obey the sort. On a descending sort the ad prices ran
790/399/414/999/99/1,490 while the organic results ran a clean
195,000 → 176,000. The same ad creative also reappears across pages, so an
unfiltered "no duplicates" check reports normal behaviour as a defect.
Filtering lives in `build_products()` — a pure function with its own unit
tests, because every other assertion depends on it being right.

**The 1440×900 viewport is a functional precondition, not styling.** Below
roughly 1024px momo serves a different component tree (`ul.goods-mobile-panel`
instead of `ul.listAreaUl`) and every selector here resolves to nothing.
`SearchPage.open()` asserts the width up front so the failure names the cause.

**Only the tests whose subject is the search box go through the home page.**
The rest deep-link into the result page. After a client-side search the result
page is a soft navigation and the price control loses roughly one interaction
in three; a full document load does not.

**The price panel is re-issued until the result set changes.** momo's 確認
control drops interactions while the list is still re-rendering. The fix is a
stability gate (`_wait_until_results_settle`) plus a bounded re-issue. This
retries an *interaction*, never an assertion — every range, ordering and URL
claim still has to hold on its first look, so no product defect can be hidden.

**Relevance is gated at 70%, not 100%.** Measured 2026-09-22: 耳機 scored
24/24 but 咖啡 scored 22/24, and both misses were correct behaviour — 「珈琲豆」
is a variant spelling and 「二合一」 is instant coffee. A 100% gate would fail
the product for working properly. The gate exists to catch a collapsed ranker.

**No `sleep`, no blanket reruns, no `networkidle`.** Ad and tracking requests
mean the network never goes idle. Every wait is a bounded condition on
something the user can see.

## CI

| Job | Trigger | Scope | Verified on a remote runner |
|---|---|---|---|
| `offline helpers` | every push / PR | `tests/test_parsers.py` — no browser, no network | **yes** — [run #1](https://github.com/ZoeWty/momo_sdet_automation/actions/runs/35636669977), `success` on `ubuntu-latest` / Python 3.13, 17 s |
| `live smoke (manual)` | `workflow_dispatch` only | `pytest -m smoke` — the 7 P0 executions, against production | **yes** — [run #3](https://github.com/ZoeWty/momo_sdet_automation/actions/runs/35686824709), `success` in 129 s |

The live smoke is deliberately **not** scheduled. The target is a production
site we do not own, so a recurring job against it is not ours to start — the
offline helpers carry the automatic gate instead.

On push-triggered runs, `live smoke (manual)` reports `skipped`, which is
exactly what its `workflow_dispatch` condition is for.

The P0 smoke passing on a GitHub-hosted **US** runner, against a Taiwanese
production site, on **Python 3.13** rather than the 3.14 used locally, is a
stronger result than the local runs alone: the waiting contract described above
is not tuned to one laptop's timing, one interpreter or one network path.

## Known limits

- Products, ads, stock and campaigns change. Nothing pins product counts,
  names, ranks or cross-run sets.
- Displayed price is the oracle for ordering and range. The meaning of "from"
  prices and already-discounted prices is unconfirmed with the product team.
- `1000–2000` must currently yield at least two pages of organic results. If
  that stops being true the test fails and leaves a trace; it never silently
  skips or swaps in different data.
- `MM-FH-06` exercises one product's URL family per run. The helpers cover
  `GoodsDetail.jsp`, the `/product/{id}` alias and `/TP/.../goodsDetail/...`
  identity parsing; the suite does not claim all three page types ran E2E.
