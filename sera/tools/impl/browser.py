"""Browser automation tool via Playwright.

Outclass: semantic selectors (role:, text:, label:, placeholder:, alt:) are
stable across UI changes. Hermes uses raw Puppeteer XPath/CSS selectors that
break on redesigns. Playwright's locators survive structure changes.

Graceful degradation: Sera starts even when playwright is not installed.
Run `pip install playwright && playwright install chromium` to enable.
"""
from __future__ import annotations

import logging
from typing import Any

from sera.tools.base import Permission, Tool, ToolContext, ToolScope
from sera.tools.registry import register

log = logging.getLogger("sera.tools.browser")

try:
    from playwright.async_api import async_playwright
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

_NOT_INSTALLED = (
    "Playwright not installed. Run: "
    "pip install playwright && playwright install chromium"
)


# ---------------------------------------------------------------------------
# Semantic selector resolution — the outclass
# ---------------------------------------------------------------------------

def _resolve(page: Any, selector: str) -> Any:
    """Map semantic selector prefixes to Playwright locator methods.

    Supported prefixes:
        role:<aria-role>           → get_by_role
        text:<visible text>        → get_by_text
        label:<label text>         → get_by_label
        placeholder:<placeholder>  → get_by_placeholder
        alt:<alt text>             → get_by_alt_text
        title:<title attr>         → get_by_title
        <anything else>            → locator (CSS / XPath)
    """
    if selector.startswith("role:"):
        return page.get_by_role(selector[5:].strip())
    if selector.startswith("text:"):
        return page.get_by_text(selector[5:].strip())
    if selector.startswith("label:"):
        return page.get_by_label(selector[6:].strip())
    if selector.startswith("placeholder:"):
        return page.get_by_placeholder(selector[12:].strip())
    if selector.startswith("alt:"):
        return page.get_by_alt_text(selector[4:].strip())
    if selector.startswith("title:"):
        return page.get_by_title(selector[6:].strip())
    return page.locator(selector)


# ---------------------------------------------------------------------------
# Core page operations (pure — injectable for tests)
# ---------------------------------------------------------------------------

async def _navigate(page: Any, url: str, *, timeout: int = 30_000) -> str:
    """Navigate to url and return page title."""
    await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
    title = await page.title()
    return f"Navigated to {url!r} — title: {title!r}"


async def _get_text(page: Any, selector: str, *, timeout: int = 10_000) -> str:
    """Get text content of element matching selector."""
    loc = _resolve(page, selector)
    text = await loc.first.text_content(timeout=timeout)
    return (text or "").strip()


async def _get_all_text(page: Any, selector: str, *, timeout: int = 10_000) -> str:
    """Get text content of ALL elements matching selector, one per line."""
    loc = _resolve(page, selector)
    elements = await loc.all()
    parts: list[str] = []
    for el in elements:
        t = await el.text_content(timeout=timeout)
        if t and t.strip():
            parts.append(t.strip())
    return "\n".join(parts)


async def _click(page: Any, selector: str, *, timeout: int = 10_000) -> str:
    """Click element matching selector."""
    await _resolve(page, selector).first.click(timeout=timeout)
    return f"Clicked {selector!r}"


async def _fill(page: Any, selector: str, value: str, *, timeout: int = 10_000) -> str:
    """Fill input element matching selector with value."""
    await _resolve(page, selector).first.fill(value, timeout=timeout)
    return f"Filled {selector!r} with {value!r}"


async def _extract_content(page: Any, url: str, selector: str = "body") -> str:
    """Navigate to url and extract text from selector."""
    await page.goto(url, timeout=30_000, wait_until="domcontentloaded")
    loc = _resolve(page, selector)
    text = await loc.first.inner_text(timeout=15_000)
    # Trim to 8000 chars so LLM context doesn't explode
    text = (text or "").strip()
    if len(text) > 8_000:
        text = text[:8_000] + "\n…[truncated]"
    return text


# ---------------------------------------------------------------------------
# Browser session manager — lazy singleton per event loop
# ---------------------------------------------------------------------------

class _BrowserManager:
    def __init__(self) -> None:
        self._pw: Any = None
        self._browser: Any = None
        self._page: Any = None

    async def get_page(self) -> Any:
        if not _PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(_NOT_INSTALLED)
        if self._pw is None:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=True)
        if self._page is None or self._page.is_closed():
            self._page = await self._browser.new_page()
        return self._page

    async def close(self) -> None:
        if self._page and not self._page.is_closed():
            await self._page.close()
            self._page = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None

    async def reset_page(self) -> None:
        if self._page and not self._page.is_closed():
            await self._page.close()
            self._page = None


_manager = _BrowserManager()


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

async def _h_navigate(args: dict[str, Any], ctx: ToolContext) -> str:
    url = args.get("url", "")
    if not url:
        return "[browser_navigate: url is required]"
    page = args.get("_page") or await _manager.get_page()
    return await _navigate(page, url)


async def _h_get_text(args: dict[str, Any], ctx: ToolContext) -> str:
    selector = args.get("selector", "body")
    all_matches = args.get("all", False)
    page = args.get("_page") or await _manager.get_page()
    if all_matches:
        return await _get_all_text(page, selector)
    return await _get_text(page, selector)


async def _h_click(args: dict[str, Any], ctx: ToolContext) -> str:
    selector = args.get("selector", "")
    if not selector:
        return "[browser_click: selector is required]"
    page = args.get("_page") or await _manager.get_page()
    return await _click(page, selector)


async def _h_fill(args: dict[str, Any], ctx: ToolContext) -> str:
    selector = args.get("selector", "")
    value = args.get("value", "")
    if not selector:
        return "[browser_fill: selector is required]"
    page = args.get("_page") or await _manager.get_page()
    return await _fill(page, selector, value)


async def _h_extract(args: dict[str, Any], ctx: ToolContext) -> str:
    url = args.get("url", "")
    selector = args.get("selector", "body")
    if not url:
        return "[browser_extract: url is required]"
    page = args.get("_page") or await _manager.get_page()
    return await _extract_content(page, url, selector)


async def _h_close(args: dict[str, Any], ctx: ToolContext) -> str:
    await _manager.close()
    return "Browser session closed."


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def _t(name, description, params, handler, permission=Permission.EXECUTE) -> Tool:
    return Tool(
        name=name,
        description=description,
        parameters={"type": "object", "properties": params,
                    "required": [k for k, v in params.items() if v.get("required_field")]},
        permission=permission,
        scope=ToolScope.INTEGRATION,
        handler=handler,
    )


register(Tool(
    name="browser_navigate",
    description=(
        "Navigate the browser to a URL. Returns the page title. "
        "Must be called before other browser tools."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full URL including scheme (https://)."},
        },
        "required": ["url"],
    },
    permission=Permission.EXECUTE,
    scope=ToolScope.INTEGRATION,
    handler=_h_navigate,
))

register(Tool(
    name="browser_get_text",
    description=(
        "Extract text from an element. Supports semantic selectors: "
        "role:<aria-role>, text:<visible text>, label:<label>, "
        "placeholder:<placeholder>, alt:<alt text>, title:<title>, "
        "or raw CSS/XPath."
    ),
    parameters={
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "Semantic or CSS selector. E.g. 'role:heading', 'text:Submit', 'h1'.",
            },
            "all": {
                "type": "boolean",
                "description": "If true, return text of ALL matching elements (one per line).",
                "default": False,
            },
        },
        "required": ["selector"],
    },
    permission=Permission.READ_ONLY,
    scope=ToolScope.INTEGRATION,
    handler=_h_get_text,
))

register(Tool(
    name="browser_click",
    description="Click an element. Supports semantic selectors (role:, text:, label:, etc.).",
    parameters={
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "Semantic or CSS selector."},
        },
        "required": ["selector"],
    },
    permission=Permission.EXECUTE,
    scope=ToolScope.INTEGRATION,
    handler=_h_click,
))

register(Tool(
    name="browser_fill",
    description="Fill an input field. Supports semantic selectors.",
    parameters={
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "Semantic or CSS selector."},
            "value": {"type": "string", "description": "Text to enter."},
        },
        "required": ["selector", "value"],
    },
    permission=Permission.EXECUTE,
    scope=ToolScope.INTEGRATION,
    handler=_h_fill,
))

register(Tool(
    name="browser_extract",
    description=(
        "Navigate to a URL and extract readable text from a selector. "
        "Combines navigate + get_text in one call. Returns up to 8000 chars."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to navigate to."},
            "selector": {
                "type": "string",
                "description": "Selector for content area. Defaults to 'body'.",
                "default": "body",
            },
        },
        "required": ["url"],
    },
    permission=Permission.EXECUTE,
    scope=ToolScope.INTEGRATION,
    handler=_h_extract,
))

register(Tool(
    name="browser_close",
    description="Close the browser session and free resources.",
    parameters={"type": "object", "properties": {}, "required": []},
    permission=Permission.EXECUTE,
    scope=ToolScope.INTEGRATION,
    handler=_h_close,
))
