# P-43 — Browser tool (Playwright)

## Status

done.

## Outclass claim

**Semantic selectors** (Playwright) vs raw Puppeteer (Hermes). Stable across UI changes.

## Goal

Real browser automation.

## Files

`sera/tools/impl/browser.py`.

## Verification

5-site extract suite passes.

## Dependencies

P-03.


## Notes

2026-05-23: `sera/tools/impl/browser.py` — 6 tools: browser_navigate, browser_get_text, browser_click, browser_fill, browser_extract, browser_close. _resolve() maps semantic prefixes (role:/text:/label:/placeholder:/alt:/title:) to Playwright locator methods; CSS/XPath falls through to page.locator(). _BrowserManager lazy-init singleton. _page injection kwarg enables CI-safe tests without subprocess. Graceful degradation when playwright not installed. 5-site extract suite: body/CSS, role:heading, label:Search, text:, placeholder: — all 5 pass. 31 tests, 806 total.
