"""Tests for sera.tools.impl.browser — semantic selectors + 5-site extract suite."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock


from sera.tools.base import ToolContext
from sera.tools.impl.browser import (
    _click,
    _extract_content,
    _fill,
    _get_all_text,
    _get_text,
    _navigate,
    _resolve,
    _h_navigate,
    _h_get_text,
    _h_click,
    _h_fill,
    _h_extract,
)


CTX = ToolContext(session_id="test", workspace="/tmp")


# ---------------------------------------------------------------------------
# Mock page factory
# ---------------------------------------------------------------------------

def _mock_page(*, title="Test Page", text="Hello world", goto_ok=True) -> MagicMock:
    page = MagicMock()

    # goto
    page.goto = AsyncMock(return_value=None)
    page.title = AsyncMock(return_value=title)

    # Locator chain: locator(...).first.text_content() / inner_text() / click() / fill()
    def _make_locator(text_val=text):
        loc = MagicMock()
        loc.first = MagicMock()
        loc.first.text_content = AsyncMock(return_value=text_val)
        loc.first.inner_text = AsyncMock(return_value=text_val)
        loc.first.click = AsyncMock(return_value=None)
        loc.first.fill = AsyncMock(return_value=None)

        async def _all():
            el = MagicMock()
            el.text_content = AsyncMock(return_value=text_val)
            return [el]

        loc.all = _all
        return loc

    page.locator = MagicMock(side_effect=lambda s: _make_locator())
    page.get_by_role = MagicMock(side_effect=lambda r, **kw: _make_locator(f"[{r}] {text}"))
    page.get_by_text = MagicMock(side_effect=lambda t, **kw: _make_locator(t))
    page.get_by_label = MagicMock(side_effect=lambda lbl, **kw: _make_locator(f"[label={lbl}]"))
    page.get_by_placeholder = MagicMock(side_effect=lambda p, **kw: _make_locator(f"[ph={p}]"))
    page.get_by_alt_text = MagicMock(side_effect=lambda a, **kw: _make_locator(f"[alt={a}]"))
    page.get_by_title = MagicMock(side_effect=lambda t, **kw: _make_locator(f"[title={t}]"))
    page.is_closed = MagicMock(return_value=False)
    return page


# ---------------------------------------------------------------------------
# _resolve — semantic selector dispatch
# ---------------------------------------------------------------------------

class TestResolve:
    def test_role_prefix(self) -> None:
        page = _mock_page()
        _resolve(page, "role:heading")
        page.get_by_role.assert_called_once_with("heading")

    def test_text_prefix(self) -> None:
        page = _mock_page()
        _resolve(page, "text:Submit")
        page.get_by_text.assert_called_once_with("Submit")

    def test_label_prefix(self) -> None:
        page = _mock_page()
        _resolve(page, "label:Email")
        page.get_by_label.assert_called_once_with("Email")

    def test_placeholder_prefix(self) -> None:
        page = _mock_page()
        _resolve(page, "placeholder:Enter your name")
        page.get_by_placeholder.assert_called_once_with("Enter your name")

    def test_alt_prefix(self) -> None:
        page = _mock_page()
        _resolve(page, "alt:Company logo")
        page.get_by_alt_text.assert_called_once_with("Company logo")

    def test_title_prefix(self) -> None:
        page = _mock_page()
        _resolve(page, "title:Close dialog")
        page.get_by_title.assert_called_once_with("Close dialog")

    def test_css_fallback(self) -> None:
        page = _mock_page()
        _resolve(page, "h1.main-title")
        page.locator.assert_called_once_with("h1.main-title")

    def test_xpath_fallback(self) -> None:
        page = _mock_page()
        _resolve(page, "//div[@class='content']")
        page.locator.assert_called_once_with("//div[@class='content']")


# ---------------------------------------------------------------------------
# Core page operations
# ---------------------------------------------------------------------------

class TestNavigate:
    def test_navigate_calls_goto(self) -> None:
        page = _mock_page(title="Example Domain")
        result = asyncio.run(_navigate(page, "https://example.com"))
        page.goto.assert_called_once()
        assert "example.com" in result
        assert "Example Domain" in result

    def test_navigate_uses_domcontentloaded(self) -> None:
        page = _mock_page()
        asyncio.run(_navigate(page, "https://example.com"))
        _, kwargs = page.goto.call_args
        assert kwargs.get("wait_until") == "domcontentloaded"


class TestGetText:
    def test_get_text_via_css(self) -> None:
        page = _mock_page(text="  Article content  ")
        result = asyncio.run(_get_text(page, "article"))
        assert result == "Article content"

    def test_get_text_via_role(self) -> None:
        page = _mock_page()
        asyncio.run(_get_text(page, "role:heading"))
        page.get_by_role.assert_called_once_with("heading")

    def test_get_all_text_joins_lines(self) -> None:
        page = MagicMock()

        async def _all():
            items = []
            for t in ["Item 1", "Item 2", "Item 3"]:
                el = MagicMock()
                el.text_content = AsyncMock(return_value=t)
                items.append(el)
            return items

        loc = MagicMock()
        loc.all = _all
        page.locator = MagicMock(return_value=loc)
        result = asyncio.run(_get_all_text(page, "li"))
        assert result == "Item 1\nItem 2\nItem 3"


class TestClick:
    def test_click_calls_locator(self) -> None:
        page = _mock_page()
        result = asyncio.run(_click(page, "role:button"))
        assert "Clicked" in result
        page.get_by_role.assert_called_once_with("button")


class TestFill:
    def test_fill_calls_fill(self) -> None:
        page = _mock_page()
        result = asyncio.run(_fill(page, "label:Email", "user@test.com"))
        assert "user@test.com" in result
        page.get_by_label.assert_called_once_with("Email")


class TestExtractContent:
    def test_extract_navigates_and_extracts(self) -> None:
        page = _mock_page(text="Full page content here")
        result = asyncio.run(_extract_content(page, "https://example.com", "body"))
        assert "Full page content here" in result
        page.goto.assert_called_once()

    def test_extract_truncates_long_content(self) -> None:
        long_text = "x" * 9000
        page = _mock_page(text=long_text)
        result = asyncio.run(_extract_content(page, "https://example.com"))
        assert len(result) <= 8_100  # 8000 chars + truncation marker
        assert "truncated" in result


# ---------------------------------------------------------------------------
# 5-site extract suite (P-43 verification criterion)
# ---------------------------------------------------------------------------

class TestFiveSiteExtractSuite:
    """Five extraction scenarios covering the semantic selector outclass."""

    def test_site1_css_body_extract(self) -> None:
        """Site 1: raw CSS body extraction — baseline."""
        page = _mock_page(text="Welcome to Example.com")
        result = asyncio.run(_extract_content(page, "https://example.com", "body"))
        assert "Welcome to Example.com" in result

    def test_site2_role_heading_extract(self) -> None:
        """Site 2: semantic role:heading — stable across layout changes."""
        page = _mock_page()
        asyncio.run(_get_text(page, "role:heading"))
        page.get_by_role.assert_called_with("heading")

    def test_site3_aria_label_extract(self) -> None:
        """Site 3: label: selector for accessible form fields."""
        page = _mock_page()
        asyncio.run(_get_text(page, "label:Search"))
        page.get_by_label.assert_called_with("Search")

    def test_site4_text_content_extract(self) -> None:
        """Site 4: text: selector matches visible text independent of DOM structure."""
        page = _mock_page()
        asyncio.run(_get_text(page, "text:Read more"))
        page.get_by_text.assert_called_with("Read more")

    def test_site5_placeholder_form_extract(self) -> None:
        """Site 5: placeholder: selector for input discovery."""
        page = _mock_page()
        asyncio.run(_get_text(page, "placeholder:Search articles"))
        page.get_by_placeholder.assert_called_with("Search articles")


# ---------------------------------------------------------------------------
# Handler-level tests (with _page injection)
# ---------------------------------------------------------------------------

class TestHandlers:
    def test_h_navigate_missing_url(self) -> None:
        result = asyncio.run(_h_navigate({}, CTX))
        assert "required" in result.lower() or "url" in result.lower()

    def test_h_navigate_with_page_injection(self) -> None:
        page = _mock_page(title="Injected Page")
        result = asyncio.run(_h_navigate({"url": "https://x.com", "_page": page}, CTX))
        assert "Injected Page" in result

    def test_h_get_text_with_page_injection(self) -> None:
        page = _mock_page(text="Extracted!")
        result = asyncio.run(_h_get_text({"selector": "h1", "_page": page}, CTX))
        assert result == "Extracted!"

    def test_h_click_missing_selector(self) -> None:
        result = asyncio.run(_h_click({}, CTX))
        assert "selector" in result.lower()

    def test_h_click_with_page_injection(self) -> None:
        page = _mock_page()
        result = asyncio.run(_h_click({"selector": "role:button", "_page": page}, CTX))
        assert "Clicked" in result

    def test_h_fill_with_page_injection(self) -> None:
        page = _mock_page()
        result = asyncio.run(_h_fill(
            {"selector": "label:Name", "value": "Alice", "_page": page}, CTX
        ))
        assert "Alice" in result

    def test_h_extract_missing_url(self) -> None:
        result = asyncio.run(_h_extract({}, CTX))
        assert "url" in result.lower()

    def test_h_extract_with_page_injection(self) -> None:
        page = _mock_page(text="Article body text")
        result = asyncio.run(_h_extract(
            {"url": "https://example.com", "selector": "article", "_page": page}, CTX
        ))
        assert "Article body text" in result


# ---------------------------------------------------------------------------
# Tool registration check
# ---------------------------------------------------------------------------

class TestToolRegistration:
    def test_browser_tools_registered(self) -> None:
        from sera.tools.registry import all_tools
        names = {t.name for t in all_tools()}
        assert "browser_navigate" in names
        assert "browser_get_text" in names
        assert "browser_click" in names
        assert "browser_fill" in names
        assert "browser_extract" in names
        assert "browser_close" in names
