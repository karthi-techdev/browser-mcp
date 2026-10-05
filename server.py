"""
High-Speed Browser Playwright MCP Server
========================================
Comprehensive, single-file, zero-env MCP server built with FastMCP and Playwright.

Capabilities:
- External Browser: Connect to running Chrome/Edge via CDP or launch persistent context
- High Speed: Instant actions, lazy init, smart timeouts, domcontentloaded defaults
- Session Persistence: Persistent profile directory + importable/exportable session state JSON
- Dedicated Evidence Management: Timestamped screenshots, element capture, audit log, HTML reports
- Visual Verification: Element visibility, text, URL, title verify + visual pixel diffing with Pillow
- Deep Control: Tabs, keyboard, mouse coords, iframes, batch forms, uploads, JS evaluation
- Network & Diagnostics: Request monitoring, resource/ad blocking, console logs, page errors, dialogs
- Storage & Emulation: Granular cookies, localStorage, sessionStorage, geolocation, offline, clipboard
"""

import asyncio
import os
from collections import deque
import datetime
import html
import json
import logging
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Set

from fastmcp import FastMCP
from PIL import Image, ImageChops
from playwright.async_api import (
    Browser,
    BrowserContext,
    Dialog,
    Frame,
    Page,
    Playwright,
    async_playwright,
)

# ---------------------------------------------------------------------------
# Directories & Logging (Zero external .env required)
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
EVIDENCE_DIR = BASE_DIR / "evidence"
SESSIONS_DIR = BASE_DIR / "sessions"
PROFILE_DIR = BASE_DIR / "browser_profile"

for directory in (EVIDENCE_DIR, SESSIONS_DIR, PROFILE_DIR):
    directory.mkdir(parents=True, exist_ok=True)

EVIDENCE_LOG_FILE = EVIDENCE_DIR / "evidence_log.json"
SESSIONS_INDEX_FILE = SESSIONS_DIR / "sessions_index.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("browser-mcp")

# ---------------------------------------------------------------------------
# Browser Configuration Defaults (Strictly External Browser)
# ---------------------------------------------------------------------------
DEFAULT_BROWSER_TYPE = os.environ.get("BROWSER_TYPE", "chrome")
DEFAULT_HEADLESS = os.environ.get("BROWSER_HEADLESS", "false").lower() in ("true", "1", "yes")
STRICT_EXTERNAL = os.environ.get("BROWSER_STRICT_EXTERNAL", "true").lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------
# Browser Manager Singleton
# ---------------------------------------------------------------------------
class BrowserManager:
    """
    Manages Playwright lifecycle, persistent context, external CDP attachments,
    active pages/tabs, listeners, diagnostic rings, and session storage.
    """

    def __init__(self) -> None:
        self.pw: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.active_page: Optional[Page] = None
        self.mode: str = "none"  # "persistent", "cdp", or "none"
        self.browser_type: str = DEFAULT_BROWSER_TYPE
        self.headless: bool = DEFAULT_HEADLESS
        self.strict_external: bool = STRICT_EXTERNAL
        self.lock = asyncio.Lock()

        # Diagnostics & Listeners
        self.console_logs: deque = deque(maxlen=200)
        self.page_errors: deque = deque(maxlen=100)
        self.network_activity: deque = deque(maxlen=200)
        self.dialog_history: deque = deque(maxlen=50)
        self.dialog_action: str = "accept"  # "accept" or "dismiss"
        self.dialog_prompt_text: str = ""
        self.blocked_resource_types: Set[str] = set()

    def _attach_page_listeners(self, page: Page) -> None:
        """Lightweight event hookups for console, dialogs, network, and uncaught errors."""
        try:
            page.on(
                "console",
                lambda msg: self.console_logs.append(
                    {
                        "timestamp": datetime.datetime.now().isoformat(),
                        "type": msg.type,
                        "text": msg.text,
                        "location": msg.location,
                    }
                ),
            )
            page.on(
                "pageerror",
                lambda err: self.page_errors.append(
                    {
                        "timestamp": datetime.datetime.now().isoformat(),
                        "error": str(err),
                    }
                ),
            )

            async def _on_dialog(dialog: Dialog) -> None:
                action = self.dialog_action
                prompt = self.dialog_prompt_text
                self.dialog_history.append(
                    {
                        "timestamp": datetime.datetime.now().isoformat(),
                        "type": dialog.type,
                        "message": dialog.message,
                        "default_value": dialog.default_value,
                        "action_taken": action,
                    }
                )
                try:
                    if action == "accept":
                        await dialog.accept(prompt_text=prompt or None)
                    else:
                        await dialog.dismiss()
                except Exception as ex:
                    logger.debug(f"Dialog action error: {ex}")

            page.on("dialog", lambda d: asyncio.create_task(_on_dialog(d)))

            page.on(
                "request",
                lambda req: self.network_activity.append(
                    {
                        "timestamp": datetime.datetime.now().isoformat(),
                        "direction": "request",
                        "url": req.url,
                        "method": req.method,
                        "resource_type": req.resource_type,
                    }
                ),
            )

            page.on(
                "response",
                lambda res: self.network_activity.append(
                    {
                        "timestamp": datetime.datetime.now().isoformat(),
                        "direction": "response",
                        "url": res.url,
                        "status": res.status,
                        "status_text": res.status_text,
                    }
                ),
            )
        except Exception as e:
            logger.debug(f"Error attaching page listeners: {e}")

    async def _apply_route_blocking(self, page: Page) -> None:
        """Route handler for blocking unwanted or heavy resource types."""
        if not self.blocked_resource_types:
            return

        async def _route_filter(route):
            if route.request.resource_type in self.blocked_resource_types:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", _route_filter)

    @staticmethod
    def _is_cdp_listening(host: str = "127.0.0.1", port: int = 9222, timeout: float = 0.2) -> bool:
        """Fast non-blocking check if an external browser is currently listening on CDP port."""
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except (OSError, ConnectionRefusedError):
            return False

    @staticmethod
    def _find_chrome_executable() -> Optional[str]:
        """Detect system Google Chrome or Microsoft Edge executable path on Windows."""
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
        for c in candidates:
            if Path(c).exists():
                return c
        return None

    def _start_external_chrome_process(self, port: int = 9222) -> bool:
        """Start the real system Chrome executable with remote debugging enabled (--remote-debugging-port=9222)."""
        if self._is_cdp_listening("127.0.0.1", port):
            return True

        chrome_exe = self._find_chrome_executable()
        if not chrome_exe:
            logger.warning("No Chrome/Edge executable found on system")
            return False

        cmd = [
            chrome_exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={PROFILE_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            "--start-maximized",
        ]
        try:
            logger.info(f"Spawning external Chrome with port {port}: {' '.join(cmd)}")
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(25):
                time.sleep(0.2)
                if self._is_cdp_listening("127.0.0.1", port):
                    logger.info(f"External Chrome listening on port {port}")
                    return True
        except Exception as e:
            logger.warning(f"Could not spawn external Chrome process: {e}")
        return False

    async def ensure_page(self) -> Page:
        """
        Fast resolution of an active, healthy page.
        Automatically recovers from closed pages or starts default browser in < 2ms if already running.
        Prioritizes attaching to running external browsers or launches visible Chrome (strictly non-headless).
        """
        async with self.lock:
            # Check if active page is still alive and responsive
            if self.active_page is not None:
                try:
                    if not self.active_page.is_closed():
                        return self.active_page
                except Exception:
                    pass

            # Check if context has any open pages
            if self.context is not None:
                pages = [p for p in self.context.pages if not p.is_closed()]
                if pages:
                    self.active_page = pages[-1]
                    self._attach_page_listeners(self.active_page)
                    return self.active_page
                # Create a new page in existing context
                try:
                    self.active_page = await self.context.new_page()
                    self._attach_page_listeners(self.active_page)
                    return self.active_page
                except Exception as e:
                    logger.warning(f"Could not create page in current context: {e}")

            # 1. Check if an external browser is already running on remote debugging port 9222
            if self._is_cdp_listening("127.0.0.1", 9222):
                logger.info("External browser detected on CDP port 9222. Connecting...")
                try:
                    res = await self._connect_cdp_internal("http://127.0.0.1:9222")
                    if res.get("success") and self.active_page is not None:
                        return self.active_page
                except Exception as e:
                    logger.warning(f"Failed to auto-connect to external CDP browser: {e}")

            # 2. Launch visible external Chrome / Edge browser (strictly non-headless)
            await self._launch_persistent_internal(browser_type="chrome", headless=False)
            if self.active_page is None:
                raise RuntimeError("Failed to initialize active browser page")
            return self.active_page

    async def _launch_persistent_internal(
        self,
        browser_type: str = "chrome",
        headless: bool = False,
        user_data_dir: Optional[Path] = None,
        session_name: str = "",
    ) -> None:
        """Internal launcher for persistent browser context."""
        if self.pw is None:
            self.pw = await async_playwright().start()

        target_dir = user_data_dir or PROFILE_DIR
        target_dir.mkdir(parents=True, exist_ok=True)

        # Close existing context if open
        if self.context is not None:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
            self.active_page = None

        # Enforce strictly external browser if configured
        if self.strict_external:
            headless = False

        launch_args = [
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--start-maximized",
        ]

        # Attempt to launch system Chrome or Edge channel first for true external browser behavior
        context = None
        channel_to_try = browser_type.lower()
        if channel_to_try in ("chrome", "google-chrome"):
            try:
                context = await self.pw.chromium.launch_persistent_context(
                    user_data_dir=str(target_dir),
                    channel="chrome",
                    headless=headless,
                    args=launch_args,
                    no_viewport=True,
                )
                self.browser_type = "chrome"
            except Exception as e:
                logger.info(f"System Chrome launch failed ({e}), trying system Edge...")
                try:
                    context = await self.pw.chromium.launch_persistent_context(
                        user_data_dir=str(target_dir),
                        channel="msedge",
                        headless=headless,
                        args=launch_args,
                        no_viewport=True,
                    )
                    self.browser_type = "msedge"
                except Exception as e2:
                    logger.info(f"System Edge launch failed ({e2}), falling back to bundled chromium")

        elif channel_to_try in ("msedge", "edge"):
            try:
                context = await self.pw.chromium.launch_persistent_context(
                    user_data_dir=str(target_dir),
                    channel="msedge",
                    headless=headless,
                    args=launch_args,
                    no_viewport=True,
                )
                self.browser_type = "msedge"
            except Exception as e:
                logger.info(f"System Edge launch failed ({e}), falling back to bundled chromium")

        # Fallback handling
        if context is None:
            if self.strict_external:
                raise RuntimeError(
                    "Strictly external browser is enabled, but neither system Google Chrome nor Microsoft Edge could be launched. "
                    "Please ensure an external browser (Chrome or Edge) is installed on your system."
                )
            context = await self.pw.chromium.launch_persistent_context(
                user_data_dir=str(target_dir),
                headless=headless,
                args=launch_args,
                viewport={"width": 1280, "height": 800},
            )
            self.browser_type = "chromium"

        self.context = context
        self.mode = "persistent"
        self.headless = headless

        # If a session name was requested, apply cookies from saved state
        if session_name:
            candidate = SESSIONS_DIR / f"{session_name}.json"
            if candidate.exists():
                try:
                    with open(candidate, "r", encoding="utf-8") as f:
                        s_data = json.load(f)
                    cookies = s_data.get("cookies", [])
                    if cookies:
                        await context.add_cookies(cookies)
                except Exception as e:
                    logger.warning(f"Could not apply session cookies: {e}")

        # Grab or create active page
        if self.context.pages:
            self.active_page = self.context.pages[0]
        else:
            self.active_page = await self.context.new_page()

        self._attach_page_listeners(self.active_page)

    async def _connect_cdp_internal(self, cdp_url: str = "http://127.0.0.1:9222") -> Dict[str, Any]:
        """Internal worker to attach to running external browser over CDP without locking."""
        if self.pw is None:
            self.pw = await async_playwright().start()

        # Close previous context/browser if open
        await self._cleanup()

        try:
            self.browser = await self.pw.chromium.connect_over_cdp(cdp_url)
            self.mode = "cdp"
            self.browser_type = "cdp-external"

            contexts = self.browser.contexts
            if contexts:
                self.context = contexts[0]
            else:
                self.context = await self.browser.new_context()

            pages = [p for p in self.context.pages if not p.is_closed()]
            if pages:
                self.active_page = pages[0]
            else:
                self.active_page = await self.context.new_page()

            self._attach_page_listeners(self.active_page)

            return {
                "success": True,
                "mode": "cdp",
                "cdp_url": cdp_url,
                "tabs_count": len(self.context.pages),
                "active_url": self.active_page.url,
                "active_title": await self.active_page.title(),
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "hint": (
                    f"Could not connect to {cdp_url}. Ensure your external browser is running with "
                    f"--remote-debugging-port=9222."
                ),
            }

    async def connect_cdp(self, cdp_url: str = "http://127.0.0.1:9222") -> Dict[str, Any]:
        """Connect directly to an already running external browser instance via Chrome DevTools Protocol."""
        async with self.lock:
            return await self._connect_cdp_internal(cdp_url=cdp_url)

    async def launch_browser(
        self,
        browser_type: str = "chrome",
        headless: bool = False,
        user_data_dir: str = "",
        session_name: str = "",
    ) -> Dict[str, Any]:
        """Explicitly launch external browser with persistent context and options."""
        async with self.lock:
            custom_dir = Path(user_data_dir).resolve() if user_data_dir else PROFILE_DIR
            await self._launch_persistent_internal(
                browser_type=browser_type,
                headless=headless,
                user_data_dir=custom_dir,
                session_name=session_name,
            )
            page = self.active_page
            return {
                "success": True,
                "mode": "persistent",
                "browser_type": self.browser_type,
                "headless": headless,
                "profile_dir": str(custom_dir),
                "active_url": page.url if page else "",
                "active_title": await page.title() if page else "",
            }

    async def _cleanup(self) -> None:
        """Internal cleanup of contexts and browsers without stopping playwright instance."""
        if self.context is not None:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None

        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None

        self.active_page = None
        self.mode = "none"

    async def close(self) -> Dict[str, Any]:
        """Close browser completely and shutdown playwright."""
        async with self.lock:
            await self._cleanup()
            if self.pw is not None:
                try:
                    await self.pw.stop()
                except Exception:
                    pass
                self.pw = None
            return {"success": True, "message": "Browser and Playwright stopped successfully"}


# Instantiate global singleton
manager = BrowserManager()


# ---------------------------------------------------------------------------
# Evidence & Logging Helpers
# ---------------------------------------------------------------------------
def _load_evidence_log() -> List[Dict[str, Any]]:
    if not EVIDENCE_LOG_FILE.exists():
        return []
    try:
        with open(EVIDENCE_LOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _record_evidence_entry(entry: Dict[str, Any]) -> None:
    log_entries = _load_evidence_log()
    log_entries.append(entry)
    try:
        with open(EVIDENCE_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(log_entries, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Failed to record evidence log: {e}")


def _sanitize_name(name: str) -> str:
    cleaned = re.sub(r"[^\w\-_]", "_", name.strip())
    return cleaned[:60] if cleaned else "screenshot"


async def _capture_evidence_screenshot(
    page: Page,
    name: str = "",
    selector: str = "",
    full_page: bool = False,
    verification_type: str = "screenshot",
    status: str = "CAPTURED",
    annotation: str = "",
) -> Dict[str, Any]:
    timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = _sanitize_name(name or f"{verification_type}_{status.lower()}")
    filename = f"evidence_{timestamp_str}_{safe_name}.png"
    filepath = EVIDENCE_DIR / filename

    url = page.url
    try:
        title = await page.title()
    except Exception:
        title = ""

    width, height = 0, 0
    if selector:
        try:
            loc = page.locator(selector).first
            await loc.screenshot(path=str(filepath), timeout=3000)
            box = await loc.bounding_box()
            if box:
                width, height = int(box["width"]), int(box["height"])
        except Exception as e:
            # Fallback to page screenshot if element screenshot fails
            logger.warning(f"Element screenshot failed for selector {selector}: {e}, capturing viewport")
            await page.screenshot(path=str(filepath), full_page=full_page)
    else:
        await page.screenshot(path=str(filepath), full_page=full_page)

    file_size = filepath.stat().st_size if filepath.exists() else 0

    evidence_entry = {
        "id": f"ev_{timestamp_str}_{int(datetime.datetime.now().timestamp() * 1000) % 10000:04d}",
        "timestamp": datetime.datetime.now().isoformat(),
        "filename": filename,
        "filepath": str(filepath),
        "url": url,
        "title": title,
        "type": verification_type,
        "status": status,
        "selector": selector,
        "annotation": annotation,
        "size_bytes": file_size,
        "width": width,
        "height": height,
        "full_page": full_page,
    }
    _record_evidence_entry(evidence_entry)
    return evidence_entry


# ---------------------------------------------------------------------------
# Sessions Helpers
# ---------------------------------------------------------------------------
def _load_sessions_index() -> Dict[str, Any]:
    if not SESSIONS_INDEX_FILE.exists():
        return {}
    try:
        with open(SESSIONS_INDEX_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_sessions_index(index_data: Dict[str, Any]) -> None:
    try:
        with open(SESSIONS_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(index_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Failed to save sessions index: {e}")


# ---------------------------------------------------------------------------
# FastMCP Server Definition
# ---------------------------------------------------------------------------
mcp = FastMCP(
    name="BrowserPlaywrightMCP",
    instructions=(
        "Ultra-fast Playwright browser automation MCP server. "
        "Supports external Chrome/Edge connections, persistent logins/sessions, "
        "evidence screenshot capture, automated verification, pixel diffing, "
        "network inspection, frame control, and complete browser automation."
    ),
)


# ===========================================================================
# GROUP 1: BROWSER LIFECYCLE & EXTERNAL CONNECTIONS
# ===========================================================================
@mcp.tool()
async def browser_launch(
    browser_type: str = DEFAULT_BROWSER_TYPE,
    headless: bool = DEFAULT_HEADLESS,
    user_data_dir: str = "",
    session_name: str = "",
) -> Dict[str, Any]:
    """
    Launch an external browser (channel: "chrome", "msedge", or "chromium") with persistent profile.
    All logins, cookies, localStorage, and cached credentials are saved automatically.

    Args:
        browser_type: "chrome" (default), "msedge", or "chromium".
        headless: False (default, visible window for interactive logins) or True.
        user_data_dir: Custom profile directory path, or defaults to ./browser_profile.
        session_name: Optional saved session state name to restore on startup.
    """
    return await manager.launch_browser(
        browser_type=browser_type,
        headless=headless,
        user_data_dir=user_data_dir,
        session_name=session_name,
    )


@mcp.tool()
async def browser_connect_cdp(cdp_url: str = "http://127.0.0.1:9222") -> Dict[str, Any]:
    """
    Connect to an existing running external browser (Chrome / Edge) via Chrome DevTools Protocol (CDP).
    Attaches to your live browser tabs, extensions, and logged-in accounts directly.

    Args:
        cdp_url: CDP endpoint URL (default: "http://127.0.0.1:9222").
    """
    return await manager.connect_cdp(cdp_url=cdp_url)


@mcp.tool()
async def browser_status() -> Dict[str, Any]:
    """
    Get current status of the browser instance, connection mode, active tab, and open pages count.
    """
    page = await manager.ensure_page()
    context = manager.context
    pages = context.pages if context else []
    return {
        "is_running": manager.context is not None,
        "mode": manager.mode,
        "browser_type": manager.browser_type,
        "headless": manager.headless,
        "open_tabs_count": len(pages),
        "active_url": page.url,
        "active_title": await page.title(),
        "profile_dir": str(PROFILE_DIR),
        "evidence_dir": str(EVIDENCE_DIR),
        "sessions_dir": str(SESSIONS_DIR),
    }


@mcp.tool()
async def browser_close() -> Dict[str, Any]:
    """
    Gracefully close all browser contexts, active tabs, and stop Playwright runtime.
    """
    return await manager.close()


# ===========================================================================
# GROUP 2: TAB & WINDOW MANAGEMENT
# ===========================================================================
@mcp.tool()
async def tab_list() -> Dict[str, Any]:
    """
    List all open browser tabs/pages with index, title, URL, and active status.
    """
    active_page = await manager.ensure_page()
    context = manager.context
    if not context:
        return {"tabs": []}

    tabs = []
    for idx, page in enumerate(context.pages):
        if page.is_closed():
            continue
        try:
            title = await page.title()
        except Exception:
            title = "(loading)"
        tabs.append(
            {
                "index": idx,
                "url": page.url,
                "title": title,
                "is_active": page == active_page,
            }
        )
    return {"tabs": tabs, "count": len(tabs)}


@mcp.tool()
async def tab_switch(index: int) -> Dict[str, Any]:
    """
    Switch active focus to a specific tab by its 0-based index.

    Args:
        index: Tab index to focus.
    """
    await manager.ensure_page()
    context = manager.context
    if not context or not context.pages:
        return {"success": False, "error": "No open tabs"}

    valid_pages = [p for p in context.pages if not p.is_closed()]
    if index < 0 or index >= len(valid_pages):
        return {"success": False, "error": f"Tab index {index} out of range (0..{len(valid_pages)-1})"}

    manager.active_page = valid_pages[index]
    await manager.active_page.bring_to_front()
    return {
        "success": True,
        "active_index": index,
        "url": manager.active_page.url,
        "title": await manager.active_page.title(),
    }


@mcp.tool()
async def tab_new(url: str = "") -> Dict[str, Any]:
    """
    Open a new tab, optionally navigate to a URL, and make it the active tab.

    Args:
        url: Optional URL to navigate to immediately.
    """
    await manager.ensure_page()
    context = manager.context
    if not context:
        raise RuntimeError("Browser context unavailable")

    new_page = await context.new_page()
    manager.active_page = new_page
    manager._attach_page_listeners(new_page)
    if url:
        await new_page.goto(url, wait_until="domcontentloaded", timeout=30000)

    return {
        "success": True,
        "tab_index": len(context.pages) - 1,
        "url": new_page.url,
        "title": await new_page.title(),
    }


@mcp.tool()
async def tab_close(index: int = -1) -> Dict[str, Any]:
    """
    Close a tab by index, or close currently active tab if index=-1.

    Args:
        index: Tab index to close (-1 for active tab).
    """
    active_page = await manager.ensure_page()
    context = manager.context
    if not context or not context.pages:
        return {"success": False, "error": "No open tabs"}

    valid_pages = [p for p in context.pages if not p.is_closed()]
    if index == -1:
        target_page = active_page
    else:
        if index < 0 or index >= len(valid_pages):
            return {"success": False, "error": f"Tab index {index} out of range"}
        target_page = valid_pages[index]

    await target_page.close()
    remaining = [p for p in context.pages if not p.is_closed()]
    if remaining:
        manager.active_page = remaining[-1]
        await manager.active_page.bring_to_front()
    else:
        manager.active_page = await context.new_page()
        manager._attach_page_listeners(manager.active_page)

    return {
        "success": True,
        "remaining_tabs": len([p for p in context.pages if not p.is_closed()]),
        "active_url": manager.active_page.url,
    }


# ===========================================================================
# GROUP 3: NAVIGATION & HISTORY
# ===========================================================================
@mcp.tool()
async def navigate(
    url: str,
    wait_until: str = "domcontentloaded",
    timeout_ms: int = 30000,
) -> Dict[str, Any]:
    """
    Navigate the active tab to a URL at maximum speed.

    Args:
        url: Destination URL (e.g. "https://example.com").
        wait_until: "domcontentloaded" (fastest, default), "load", "networkidle", or "commit".
        timeout_ms: Navigation timeout in milliseconds (default: 30000).
    """
    page = await manager.ensure_page()
    if not (url.startswith("http://") or url.startswith("https://") or url.startswith("file://")):
        url = "https://" + url

    start = asyncio.get_event_loop().time()
    response = await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
    elapsed_ms = int((asyncio.get_event_loop().time() - start) * 1000)

    status_code = response.status if response else 200
    return {
        "success": True,
        "url": page.url,
        "title": await page.title(),
        "status_code": status_code,
        "elapsed_ms": elapsed_ms,
    }


@mcp.tool()
async def go_back(wait_until: str = "domcontentloaded", timeout_ms: int = 10000) -> Dict[str, Any]:
    """
    Navigate back in browser history.

    Args:
        wait_until: "domcontentloaded" (default), "load", or "networkidle".
        timeout_ms: Timeout in milliseconds.
    """
    page = await manager.ensure_page()
    await page.go_back(wait_until=wait_until, timeout=timeout_ms)
    return {
        "success": True,
        "url": page.url,
        "title": await page.title(),
    }


@mcp.tool()
async def go_forward(wait_until: str = "domcontentloaded", timeout_ms: int = 10000) -> Dict[str, Any]:
    """
    Navigate forward in browser history.

    Args:
        wait_until: "domcontentloaded" (default), "load", or "networkidle".
        timeout_ms: Timeout in milliseconds.
    """
    page = await manager.ensure_page()
    await page.go_forward(wait_until=wait_until, timeout=timeout_ms)
    return {
        "success": True,
        "url": page.url,
        "title": await page.title(),
    }


@mcp.tool()
async def reload(wait_until: str = "domcontentloaded", timeout_ms: int = 15000) -> Dict[str, Any]:
    """
    Reload current page.

    Args:
        wait_until: "domcontentloaded" (default), "load", or "networkidle".
        timeout_ms: Timeout in milliseconds.
    """
    page = await manager.ensure_page()
    await page.reload(wait_until=wait_until, timeout=timeout_ms)
    return {
        "success": True,
        "url": page.url,
        "title": await page.title(),
    }


@mcp.tool()
async def get_page_info() -> Dict[str, Any]:
    """
    Get detailed information about the current page: URL, title, viewport, readyState, and cookies count.
    """
    page = await manager.ensure_page()
    viewport = page.viewport_size or {"width": 0, "height": 0}
    cookies = await manager.context.cookies() if manager.context else []
    return {
        "url": page.url,
        "title": await page.title(),
        "viewport": viewport,
        "cookies_count": len(cookies),
        "is_closed": page.is_closed(),
    }


# ===========================================================================
# GROUP 4: INTERACTION (CLICK, HOVER, DRAG, FOCUS)
# ===========================================================================
@mcp.tool()
async def click(
    selector: str,
    button: str = "left",
    click_count: int = 1,
    timeout_ms: int = 5000,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Fast click on an element matching selector.

    Args:
        selector: CSS selector, XPath, text selector (e.g. 'text=Log In'), or locator.
        button: "left", "right", or "middle".
        click_count: 1 for single click, 2 for double.
        timeout_ms: Timeout in milliseconds (default: 5000ms for high speed).
        force: Bypass actionability checks if True.
    """
    page = await manager.ensure_page()
    loc = page.locator(selector).first
    await loc.click(
        button=button,
        click_count=click_count,
        timeout=timeout_ms,
        force=force,
    )
    return {"success": True, "selector": selector, "clicked": True}


@mcp.tool()
async def double_click(selector: str, timeout_ms: int = 5000) -> Dict[str, Any]:
    """
    Double click on an element matching selector.

    Args:
        selector: Target element selector.
        timeout_ms: Timeout in milliseconds.
    """
    page = await manager.ensure_page()
    await page.locator(selector).first.dblclick(timeout=timeout_ms)
    return {"success": True, "selector": selector}


@mcp.tool()
async def hover(selector: str, timeout_ms: int = 5000) -> Dict[str, Any]:
    """
    Hover mouse pointer over an element matching selector.

    Args:
        selector: Target element selector.
        timeout_ms: Timeout in milliseconds.
    """
    page = await manager.ensure_page()
    await page.locator(selector).first.hover(timeout=timeout_ms)
    return {"success": True, "selector": selector}


@mcp.tool()
async def focus(selector: str, timeout_ms: int = 5000) -> Dict[str, Any]:
    """
    Set focus on an element matching selector.

    Args:
        selector: Target element selector.
        timeout_ms: Timeout in milliseconds.
    """
    page = await manager.ensure_page()
    await page.locator(selector).first.focus(timeout=timeout_ms)
    return {"success": True, "selector": selector}


@mcp.tool()
async def drag_and_drop(source_selector: str, target_selector: str, timeout_ms: int = 5000) -> Dict[str, Any]:
    """
    Drag source element and drop onto target element.

    Args:
        source_selector: Element to drag.
        target_selector: Element to drop onto.
        timeout_ms: Timeout in milliseconds.
    """
    page = await manager.ensure_page()
    await page.drag_and_drop(source_selector, target_selector, timeout=timeout_ms)
    return {"success": True, "source": source_selector, "target": target_selector}


# ===========================================================================
# GROUP 5: TYPING, KEYBOARD & FORM INPUT
# ===========================================================================
@mcp.tool()
async def type_text(
    selector: str,
    text: str,
    clear_first: bool = True,
    delay_ms: int = 0,
    timeout_ms: int = 5000,
) -> Dict[str, Any]:
    """
    Type text into an input or textarea element.
    If delay_ms == 0, uses instant fill for ultra-fast execution.

    Args:
        selector: Target element selector.
        text: Text to enter.
        clear_first: Clear existing text before typing.
        delay_ms: Delay between key presses in milliseconds (0 = instant fill).
        timeout_ms: Timeout in milliseconds.
    """
    page = await manager.ensure_page()
    loc = page.locator(selector).first

    if delay_ms == 0:
        if clear_first:
            await loc.fill(text, timeout=timeout_ms)
        else:
            current = await loc.input_value()
            await loc.fill(current + text, timeout=timeout_ms)
    else:
        if clear_first:
            await loc.fill("", timeout=timeout_ms)
        await loc.type(text, delay=delay_ms, timeout=timeout_ms)

    return {"success": True, "selector": selector, "chars_typed": len(text)}


@mcp.tool()
async def press_key(key: str) -> Dict[str, Any]:
    """
    Press a keyboard key or shortcut (e.g., 'Enter', 'Escape', 'Tab', 'Control+a', 'Backspace', 'ArrowDown').

    Args:
        key: Key name or combination (e.g. "Enter", "Control+a").
    """
    page = await manager.ensure_page()
    await page.keyboard.press(key)
    return {"success": True, "key_pressed": key}


@mcp.tool()
async def keyboard_down(key: str) -> Dict[str, Any]:
    """
    Dispatch a keydown event on the active page.

    Args:
        key: Key name to hold down.
    """
    page = await manager.ensure_page()
    await page.keyboard.down(key)
    return {"success": True, "key_down": key}


@mcp.tool()
async def keyboard_up(key: str) -> Dict[str, Any]:
    """
    Dispatch a keyup event on the active page.

    Args:
        key: Key name to release.
    """
    page = await manager.ensure_page()
    await page.keyboard.up(key)
    return {"success": True, "key_up": key}


# ===========================================================================
# GROUP 6: FORMS, DROPDOWNS & BATCH OPERATIONS
# ===========================================================================
@mcp.tool()
async def select_option(
    selector: str,
    value: str = "",
    label: str = "",
    index: int = -1,
    timeout_ms: int = 5000,
) -> Dict[str, Any]:
    """
    Select an option in a <select> element by value, visible text label, or 0-based index.

    Args:
        selector: Target <select> element selector.
        value: Option value to select.
        label: Option visible label to select.
        index: Option index to select (-1 if unused).
        timeout_ms: Timeout in milliseconds.
    """
    page = await manager.ensure_page()
    loc = page.locator(selector).first

    if value:
        res = await loc.select_option(value=value, timeout=timeout_ms)
    elif label:
        res = await loc.select_option(label=label, timeout=timeout_ms)
    elif index >= 0:
        res = await loc.select_option(index=index, timeout=timeout_ms)
    else:
        return {"success": False, "error": "Must provide one of 'value', 'label', or 'index'"}

    return {"success": True, "selector": selector, "selected_values": res}


@mcp.tool()
async def check_checkbox(selector: str, checked: bool = True, timeout_ms: int = 5000) -> Dict[str, Any]:
    """
    Set checkbox or radio button state.

    Args:
        selector: Target checkbox or radio input selector.
        checked: True to check, False to uncheck.
        timeout_ms: Timeout in milliseconds.
    """
    page = await manager.ensure_page()
    loc = page.locator(selector).first
    await loc.set_checked(checked, timeout=timeout_ms)
    return {"success": True, "selector": selector, "checked": checked}


@mcp.tool()
async def fill_form(
    fields: Dict[str, str],
    submit_selector: str = "",
    timeout_ms: int = 5000,
) -> Dict[str, Any]:
    """
    Ultra-fast batch form filler. Fills multiple inputs/textareas in sequence instantly,
    and optionally clicks a submit button.

    Args:
        fields: Dictionary mapping element selectors to string values (e.g. {"#email": "user@test.com", "#password": "secret"}).
        submit_selector: Optional button selector to click after filling all fields.
        timeout_ms: Timeout per field operation.
    """
    page = await manager.ensure_page()
    filled = []
    for sel, val in fields.items():
        loc = page.locator(sel).first
        await loc.fill(str(val), timeout=timeout_ms)
        filled.append(sel)

    submitted = False
    if submit_selector:
        await page.locator(submit_selector).first.click(timeout=timeout_ms)
        submitted = True

    return {
        "success": True,
        "fields_filled": filled,
        "count": len(filled),
        "submitted": submitted,
    }


@mcp.tool()
async def upload_file(selector: str, file_paths: List[str], timeout_ms: int = 5000) -> Dict[str, Any]:
    """
    Upload one or more files to an <input type="file"> element.

    Args:
        selector: Target file input selector.
        file_paths: List of absolute file paths to upload.
        timeout_ms: Timeout in milliseconds.
    """
    page = await manager.ensure_page()
    loc = page.locator(selector).first
    await loc.set_input_files(file_paths, timeout=timeout_ms)
    return {"success": True, "selector": selector, "files": file_paths}


# ===========================================================================
# GROUP 7: DOM INSPECTION, EXTRACTION & JAVASCRIPT
# ===========================================================================
@mcp.tool()
async def get_text(selector: str = "", timeout_ms: int = 5000) -> Dict[str, Any]:
    """
    Extract text content from an element or the entire page.

    Args:
        selector: Optional element selector (empty = whole body).
        timeout_ms: Timeout in milliseconds.
    """
    page = await manager.ensure_page()
    if selector:
        text = await page.locator(selector).first.inner_text(timeout=timeout_ms)
    else:
        text = await page.inner_text("body", timeout=timeout_ms)

    return {
        "text": text,
        "length": len(text),
        "selector": selector or "body",
    }


@mcp.tool()
async def get_attribute(selector: str, attribute: str, timeout_ms: int = 5000) -> Dict[str, Any]:
    """
    Get the value of a specified HTML attribute (e.g., href, src, placeholder, value, class).

    Args:
        selector: Target element selector.
        attribute: Attribute name.
        timeout_ms: Timeout in milliseconds.
    """
    page = await manager.ensure_page()
    val = await page.locator(selector).first.get_attribute(attribute, timeout=timeout_ms)
    return {"selector": selector, "attribute": attribute, "value": val}


@mcp.tool()
async def get_html(selector: str = "", outer: bool = True, timeout_ms: int = 5000) -> Dict[str, Any]:
    """
    Extract HTML markup of an element or the entire page.

    Args:
        selector: Target selector (empty for complete document content).
        outer: Return outerHTML if True, innerHTML if False.
        timeout_ms: Timeout in milliseconds.
    """
    page = await manager.ensure_page()
    if selector:
        loc = page.locator(selector).first
        if outer:
            html_content = await loc.evaluate("el => el.outerHTML", timeout=timeout_ms)
        else:
            html_content = await loc.inner_html(timeout=timeout_ms)
    else:
        html_content = await page.content()

    return {"length": len(html_content), "html": html_content}


@mcp.tool()
async def snapshot_dom(selector: str = "", max_items: int = 80) -> Dict[str, Any]:
    """
    High-speed structured DOM snapshot. Extracts interactive and meaningful elements
    (buttons, links, inputs, headings) in < 10ms for fast LLM reasoning without clutter.

    Args:
        selector: Optional root selector (defaults to body).
        max_items: Maximum items to return.
    """
    page = await manager.ensure_page()
    js_code = """
    (rootSel) => {
        const root = rootSel ? document.querySelector(rootSel) : document.body;
        if (!root) return [];
        const selectorList = 'a, button, input, select, textarea, [role="button"], [role="link"], h1, h2, h3';
        const elements = Array.from(root.querySelectorAll(selectorList));
        const results = [];
        for (const el of elements) {
            const rect = el.getBoundingClientRect();
            const isVisible = rect.width > 0 && rect.height > 0 && window.getComputedStyle(el).visibility !== 'hidden';
            if (!isVisible) continue;
            
            const tag = el.tagName.toLowerCase();
            const text = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || '').trim();
            const id = el.id ? '#' + el.id : '';
            const type = el.getAttribute('type') || '';
            const role = el.getAttribute('role') || '';
            const href = el.getAttribute('href') || '';
            
            results.push({
                tag: tag,
                id: id,
                type: type,
                role: role,
                text: text.slice(0, 100),
                href: href.slice(0, 150),
                disabled: el.disabled || false
            });
        }
        return results;
    }
    """
    items = await page.evaluate(js_code, selector)
    trimmed = items[:max_items]
    return {
        "url": page.url,
        "title": await page.title(),
        "total_found": len(items),
        "returned": len(trimmed),
        "elements": trimmed,
    }


@mcp.tool()
async def evaluate_script(script: str, arg: str = "") -> Dict[str, Any]:
    """
    Execute custom JavaScript in page context and return the result.

    Args:
        script: JavaScript string or arrow function (e.g. '() => document.title' or 'arg => document.querySelector(arg).textContent').
        arg: Optional argument passed to the script.
    """
    page = await manager.ensure_page()
    res = await page.evaluate(script, arg if arg else None)
    return {"result": res}


# ===========================================================================
# GROUP 8: SCROLLING & VIEWPORT
# ===========================================================================
@mcp.tool()
async def scroll(direction: str = "down", amount: int = 500, selector: str = "") -> Dict[str, Any]:
    """
    Scroll page or element in a specified direction.

    Args:
        direction: "down" (default), "up", "top", "bottom", "left", or "right".
        amount: Pixels to scroll (for down, up, left, right).
        selector: Optional scrollable element selector (empty = window).
    """
    page = await manager.ensure_page()
    js_scroll = """
    ({direction, amount, selector}) => {
        const target = selector ? document.querySelector(selector) : window;
        if (!target) return false;
        
        let dx = 0, dy = 0;
        if (direction === 'down') dy = amount;
        else if (direction === 'up') dy = -amount;
        else if (direction === 'right') dx = amount;
        else if (direction === 'left') dx = -amount;
        else if (direction === 'bottom') {
            if (target === window) window.scrollTo(0, document.body.scrollHeight);
            else target.scrollTop = target.scrollHeight;
            return true;
        } else if (direction === 'top') {
            if (target === window) window.scrollTo(0, 0);
            else target.scrollTop = 0;
            return true;
        }
        
        if (target === window) window.scrollBy(dx, dy);
        else {
            target.scrollTop += dy;
            target.scrollLeft += dx;
        }
        return true;
    }
    """
    res = await page.evaluate(js_scroll, {"direction": direction, "amount": amount, "selector": selector})
    return {"success": bool(res), "direction": direction, "amount": amount, "selector": selector}


@mcp.tool()
async def scroll_to(selector: str, timeout_ms: int = 5000) -> Dict[str, Any]:
    """
    Scroll an element directly into the visible viewport.

    Args:
        selector: Target element selector.
        timeout_ms: Timeout in milliseconds.
    """
    page = await manager.ensure_page()
    await page.locator(selector).first.scroll_into_view_if_needed(timeout=timeout_ms)
    return {"success": True, "selector": selector}


@mcp.tool()
async def set_viewport(width: int = 1280, height: int = 800) -> Dict[str, Any]:
    """
    Set browser viewport dimensions.

    Args:
        width: Viewport width in pixels.
        height: Viewport height in pixels.
    """
    page = await manager.ensure_page()
    await page.set_viewport_size({"width": width, "height": height})
    return {"success": True, "width": width, "height": height}


# ===========================================================================
# GROUP 9: SYNCHRONIZATION & WAITING
# ===========================================================================
@mcp.tool()
async def wait_for_selector(
    selector: str,
    state: str = "visible",
    timeout_ms: int = 5000,
) -> Dict[str, Any]:
    """
    Wait for an element to satisfy a state: "visible", "hidden", "attached", or "detached".

    Args:
        selector: Target selector to wait for.
        state: "visible" (default), "hidden", "attached", or "detached".
        timeout_ms: Timeout in milliseconds (default: 5000).
    """
    page = await manager.ensure_page()
    loc = page.locator(selector).first
    await loc.wait_for(state=state, timeout=timeout_ms)
    return {"success": True, "selector": selector, "state": state}


@mcp.tool()
async def wait_for_load_state(state: str = "domcontentloaded", timeout_ms: int = 10000) -> Dict[str, Any]:
    """
    Wait for page load state: "domcontentloaded" (fastest, default), "load", or "networkidle".

    Args:
        state: "domcontentloaded", "load", or "networkidle".
        timeout_ms: Timeout in milliseconds.
    """
    page = await manager.ensure_page()
    await page.wait_for_load_state(state=state, timeout=timeout_ms)
    return {"success": True, "state": state}


@mcp.tool()
async def wait_for_timeout(milliseconds: int = 1000) -> Dict[str, Any]:
    """
    Explicit asynchronous pause in milliseconds.

    Args:
        milliseconds: Duration to pause in milliseconds.
    """
    await asyncio.sleep(milliseconds / 1000.0)
    return {"success": True, "waited_ms": milliseconds}


# ===========================================================================
# GROUP 10: EVIDENCE CAPTURE & SCREENSHOT VERIFICATION
# ===========================================================================
@mcp.tool()
async def take_screenshot(
    name: str = "",
    selector: str = "",
    full_page: bool = False,
    annotate: str = "",
) -> Dict[str, Any]:
    """
    Capture a screenshot directly into the dedicated ./evidence folder with timestamp.
    Logs the capture in ./evidence/evidence_log.json for verification auditing.

    Args:
        name: Descriptive name for the screenshot (e.g. "homepage", "checkout_form").
        selector: Optional selector to capture only that specific element.
        full_page: True to capture the entire scrollable page height.
        annotate: Optional note or comment to attach in evidence log.
    """
    page = await manager.ensure_page()
    entry = await _capture_evidence_screenshot(
        page=page,
        name=name,
        selector=selector,
        full_page=full_page,
        verification_type="screenshot",
        status="CAPTURED",
        annotation=annotate,
    )
    return {
        "success": True,
        "evidence_id": entry["id"],
        "filename": entry["filename"],
        "filepath": entry["filepath"],
        "size_bytes": entry["size_bytes"],
        "url": entry["url"],
        "title": entry["title"],
    }


@mcp.tool()
async def verify_element_visible(
    selector: str,
    timeout_ms: int = 5000,
    take_evidence: bool = True,
    evidence_name: str = "",
) -> Dict[str, Any]:
    """
    Verify whether an element is visible on the page, and automatically capture
    screenshot evidence to the dedicated ./evidence folder with PASSED/FAILED audit status.

    Args:
        selector: Element selector to verify.
        timeout_ms: Max time to wait for element visibility.
        take_evidence: Save evidence screenshot and audit record.
        evidence_name: Custom name for the evidence file.
    """
    page = await manager.ensure_page()
    loc = page.locator(selector).first
    is_visible = False
    try:
        await loc.wait_for(state="visible", timeout=timeout_ms)
        is_visible = True
    except Exception:
        is_visible = False

    status = "PASSED" if is_visible else "FAILED"
    evidence_entry = None
    if take_evidence:
        name = evidence_name or f"verify_visible_{_sanitize_name(selector)}"
        evidence_entry = await _capture_evidence_screenshot(
            page=page,
            name=name,
            selector=selector if is_visible else "",
            verification_type="verify_element_visible",
            status=status,
            annotation=f"Verification of visibility for '{selector}': {status}",
        )

    return {
        "verified": is_visible,
        "status": status,
        "selector": selector,
        "evidence_file": evidence_entry["filepath"] if evidence_entry else None,
        "evidence_id": evidence_entry["id"] if evidence_entry else None,
    }


@mcp.tool()
async def verify_text_present(
    text: str,
    selector: str = "",
    timeout_ms: int = 5000,
    take_evidence: bool = True,
    evidence_name: str = "",
) -> Dict[str, Any]:
    """
    Verify whether specified text is present on the page or in an element,
    saving verified screenshot evidence to the ./evidence folder.

    Args:
        text: Text string to look for.
        selector: Optional element selector to search within (empty for full body).
        timeout_ms: Timeout in milliseconds.
        take_evidence: Save screenshot evidence to ./evidence folder.
        evidence_name: Custom name for the evidence file.
    """
    page = await manager.ensure_page()
    is_present = False
    try:
        if selector:
            content = await page.locator(selector).first.inner_text(timeout=timeout_ms)
        else:
            content = await page.inner_text("body", timeout=timeout_ms)
        is_present = text in content
    except Exception:
        is_present = False

    status = "PASSED" if is_present else "FAILED"
    evidence_entry = None
    if take_evidence:
        name = evidence_name or f"verify_text_{_sanitize_name(text[:20])}"
        evidence_entry = await _capture_evidence_screenshot(
            page=page,
            name=name,
            selector=selector,
            verification_type="verify_text_present",
            status=status,
            annotation=f"Text verification for '{text}': {status}",
        )

    return {
        "verified": is_present,
        "status": status,
        "search_text": text,
        "selector": selector or "body",
        "evidence_file": evidence_entry["filepath"] if evidence_entry else None,
        "evidence_id": evidence_entry["id"] if evidence_entry else None,
    }


@mcp.tool()
async def verify_url(
    expected_url: str,
    exact: bool = False,
    take_evidence: bool = True,
    evidence_name: str = "",
) -> Dict[str, Any]:
    """
    Verify current URL matches an expected URL or pattern, saving screenshot evidence.

    Args:
        expected_url: Expected URL substring or exact string.
        exact: If True, matches exact URL; if False, checks substring or regex.
        take_evidence: Save screenshot evidence.
        evidence_name: Custom name for evidence file.
    """
    page = await manager.ensure_page()
    current_url = page.url
    if exact:
        is_match = current_url == expected_url
    else:
        is_match = (expected_url in current_url) or bool(re.search(expected_url, current_url))

    status = "PASSED" if is_match else "FAILED"
    evidence_entry = None
    if take_evidence:
        name = evidence_name or f"verify_url_{status.lower()}"
        evidence_entry = await _capture_evidence_screenshot(
            page=page,
            name=name,
            verification_type="verify_url",
            status=status,
            annotation=f"URL verification against '{expected_url}': {status}",
        )

    return {
        "verified": is_match,
        "status": status,
        "current_url": current_url,
        "expected_url": expected_url,
        "evidence_file": evidence_entry["filepath"] if evidence_entry else None,
    }


@mcp.tool()
async def verify_title(
    expected_title: str,
    exact: bool = False,
    take_evidence: bool = True,
    evidence_name: str = "",
) -> Dict[str, Any]:
    """
    Verify page title matches expected title or pattern, saving screenshot evidence.

    Args:
        expected_title: Expected title substring or exact string.
        exact: If True, matches exact title; if False, checks substring or regex.
        take_evidence: Save screenshot evidence.
        evidence_name: Custom name for evidence file.
    """
    page = await manager.ensure_page()
    current_title = await page.title()
    if exact:
        is_match = current_title == expected_title
    else:
        is_match = (expected_title.lower() in current_title.lower()) or bool(
            re.search(expected_title, current_title, re.IGNORECASE)
        )

    status = "PASSED" if is_match else "FAILED"
    evidence_entry = None
    if take_evidence:
        name = evidence_name or f"verify_title_{status.lower()}"
        evidence_entry = await _capture_evidence_screenshot(
            page=page,
            name=name,
            verification_type="verify_title",
            status=status,
            annotation=f"Title verification against '{expected_title}': {status}",
        )

    return {
        "verified": is_match,
        "status": status,
        "current_title": current_title,
        "expected_title": expected_title,
        "evidence_file": evidence_entry["filepath"] if evidence_entry else None,
    }


@mcp.tool()
async def verify_screenshot_diff(
    baseline_image_path: str,
    tolerance_pct: float = 1.0,
    selector: str = "",
    evidence_name: str = "",
) -> Dict[str, Any]:
    """
    Compare current page or element visual appearance with a baseline image.
    Computes exact pixel difference percentage and generates a visual diff image
    with red highlights in ./evidence if changes exceed tolerance.

    Args:
        baseline_image_path: Path to reference/baseline image file.
        tolerance_pct: Maximum allowed difference percentage (default: 1.0%).
        selector: Optional selector to capture only that specific element.
        evidence_name: Custom identifier for evidence diff file.
    """
    baseline_path = Path(baseline_image_path).resolve()
    if not baseline_path.exists():
        return {
            "verified": False,
            "status": "ERROR",
            "error": f"Baseline image not found: {baseline_path}",
        }

    page = await manager.ensure_page()
    timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = _sanitize_name(evidence_name or "visual_diff")

    # Capture current view
    curr_filename = f"evidence_{timestamp_str}_{safe_name}_current.png"
    curr_path = EVIDENCE_DIR / curr_filename
    if selector:
        await page.locator(selector).first.screenshot(path=str(curr_path), timeout=5000)
    else:
        await page.screenshot(path=str(curr_path))

    # Compare with Pillow
    img_base = Image.open(baseline_path).convert("RGBA")
    img_curr = Image.open(curr_path).convert("RGBA")

    # Align dimensions
    max_w = max(img_base.width, img_curr.width)
    max_h = max(img_base.height, img_curr.height)
    canvas_base = Image.new("RGBA", (max_w, max_h), (255, 255, 255, 0))
    canvas_curr = Image.new("RGBA", (max_w, max_h), (255, 255, 255, 0))
    canvas_base.paste(img_base, (0, 0))
    canvas_curr.paste(img_curr, (0, 0))

    diff = ImageChops.difference(canvas_base, canvas_curr)
    raw_bytes = diff.convert("L").tobytes()
    diff_pixels = sum(1 for b in raw_bytes if b > 10)
    total_pixels = len(raw_bytes)
    diff_pct = round((diff_pixels / total_pixels) * 100.0, 2)

    passed = diff_pct <= tolerance_pct
    status = "PASSED" if passed else "FAILED"

    # Save visual diff image
    diff_filename = f"evidence_{timestamp_str}_{safe_name}_diff_{status.lower()}.png"
    diff_filepath = EVIDENCE_DIR / diff_filename

    mask = diff.convert("L").point(lambda p: 255 if p > 10 else 0)
    highlight = Image.new("RGBA", (max_w, max_h), (255, 0, 0, 180))
    diff_view = Image.composite(highlight, canvas_curr, mask)
    diff_view.save(diff_filepath)

    entry = {
        "id": f"ev_{timestamp_str}_{int(datetime.datetime.now().timestamp() * 1000) % 10000:04d}",
        "timestamp": datetime.datetime.now().isoformat(),
        "filename": diff_filename,
        "filepath": str(diff_filepath),
        "url": page.url,
        "title": await page.title(),
        "type": "verify_screenshot_diff",
        "status": status,
        "selector": selector,
        "annotation": f"Visual diff: {diff_pct}% changed (tolerance: {tolerance_pct}%)",
        "size_bytes": diff_filepath.stat().st_size,
    }
    _record_evidence_entry(entry)

    return {
        "verified": passed,
        "status": status,
        "difference_percentage": diff_pct,
        "tolerance_percentage": tolerance_pct,
        "diff_image": str(diff_filepath),
        "current_image": str(curr_path),
        "baseline_image": str(baseline_path),
    }


@mcp.tool()
async def list_evidence(limit: int = 25) -> Dict[str, Any]:
    """
    List all recorded evidence screenshots in the ./evidence folder with metadata,
    status (PASSED/FAILED/CAPTURED), URLs, timestamps, and file paths.

    Args:
        limit: Maximum number of recent evidence records to return (default: 25).
    """
    entries = _load_evidence_log()
    recent = list(reversed(entries))[:limit]
    return {
        "total_evidence_count": len(entries),
        "returned": len(recent),
        "evidence_dir": str(EVIDENCE_DIR),
        "records": recent,
    }


@mcp.tool()
async def get_latest_evidence() -> Dict[str, Any]:
    """
    Get full metadata and file path of the most recently captured evidence screenshot.
    """
    entries = _load_evidence_log()
    if not entries:
        return {"exists": False, "message": "No evidence captured yet"}
    return {"exists": True, "latest": entries[-1]}


@mcp.tool()
async def export_evidence_report(report_title: str = "Automated Browser Verification Report") -> Dict[str, Any]:
    """
    Generate an HTML and Markdown audit report of all evidence screenshots and verifications.
    Saves to ./evidence/evidence_report.html and ./evidence/evidence_report.md.
    """
    entries = _load_evidence_log()
    passed = sum(1 for e in entries if e.get("status") == "PASSED")
    failed = sum(1 for e in entries if e.get("status") == "FAILED")
    captured = sum(1 for e in entries if e.get("status") == "CAPTURED")

    # Generate Markdown report
    md_lines = [
        f"# {report_title}",
        f"\n**Generated at:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**Summary:** Total: {len(entries)} | Passed: {passed} | Failed: {failed} | Captured: {captured}\n",
        "| ID | Timestamp | Status | Type | URL | File | Annotation |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for e in entries:
        status_badge = f"**{e.get('status')}**"
        file_link = f"[{e.get('filename')}]({Path(e.get('filepath', '')).as_uri()})"
        md_lines.append(
            f"| {e.get('id')} | {e.get('timestamp')[:19]} | {status_badge} | {e.get('type')} | {e.get('url')} | {file_link} | {e.get('annotation')} |"
        )

    md_report_path = EVIDENCE_DIR / "evidence_report.md"
    with open(md_report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))

    # Generate HTML report
    html_cards = []
    for e in entries:
        st = e.get("status", "CAPTURED")
        color = "#10b981" if st == "PASSED" else "#ef4444" if st == "FAILED" else "#3b82f6"
        card = f"""
        <div style="background:#1e293b; border-left:4px solid {color}; border-radius:8px; padding:16px; margin-bottom:16px;">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <span style="font-weight:bold; color:#f8fafc;">{html.escape(e.get('id', ''))} - {html.escape(e.get('type', ''))}</span>
                <span style="background:{color}; color:#fff; padding:3px 10px; border-radius:12px; font-size:12px; font-weight:bold;">{st}</span>
            </div>
            <p style="color:#94a3b8; font-size:13px; margin:6px 0;">{html.escape(e.get('url', ''))} | {e.get('timestamp')}</p>
            <p style="color:#cbd5e1; font-size:14px; margin:6px 0;">{html.escape(e.get('annotation', ''))}</p>
            <a href="{html.escape(e.get('filename', ''))}" target="_blank" style="color:#38bdf8; font-size:13px;">View Image: {html.escape(e.get('filename', ''))}</a>
        </div>
        """
        html_cards.append(card)

    html_content = f"""<!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>{html.escape(report_title)}</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 24px; }}
            .container {{ max-width: 900px; margin: 0 auto; }}
            .stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 24px; }}
            .stat-box {{ background: #1e293b; padding: 16px; border-radius: 8px; text-align: center; }}
            .stat-num {{ font-size: 24px; font-weight: bold; margin-top: 4px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>{html.escape(report_title)}</h1>
            <p style="color:#94a3b8;">Generated on {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
            <div class="stats">
                <div class="stat-box"><div>Total</div><div class="stat-num">{len(entries)}</div></div>
                <div class="stat-box"><div style="color:#10b981;">Passed</div><div class="stat-num" style="color:#10b981;">{passed}</div></div>
                <div class="stat-box"><div style="color:#ef4444;">Failed</div><div class="stat-num" style="color:#ef4444;">{failed}</div></div>
                <div class="stat-box"><div style="color:#3b82f6;">Captured</div><div class="stat-num" style="color:#3b82f6;">{captured}</div></div>
            </div>
            {"".join(html_cards)}
        </div>
    </body>
    </html>
    """
    html_report_path = EVIDENCE_DIR / "evidence_report.html"
    with open(html_report_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    return {
        "success": True,
        "total_records": len(entries),
        "passed": passed,
        "failed": failed,
        "captured": captured,
        "html_report": str(html_report_path),
        "markdown_report": str(md_report_path),
    }


@mcp.tool()
async def clear_evidence() -> Dict[str, Any]:
    """
    Clean up all saved evidence screenshots and clear the evidence audit log.
    """
    deleted_files = 0
    for p in EVIDENCE_DIR.glob("*.png"):
        try:
            p.unlink()
            deleted_files += 1
        except Exception:
            pass

    for p in EVIDENCE_DIR.glob("*.pdf"):
        try:
            p.unlink()
            deleted_files += 1
        except Exception:
            pass

    if EVIDENCE_LOG_FILE.exists():
        EVIDENCE_LOG_FILE.unlink(missing_ok=True)

    return {"success": True, "deleted_files": deleted_files, "message": "Evidence folder cleared"}


# ===========================================================================
# GROUP 11: SESSION PERSISTENCE & REUSE (LOGINS FOR NEXT TIME)
# ===========================================================================
@mcp.tool()
async def save_session(session_name: str = "default") -> Dict[str, Any]:
    """
    Export all active cookies, localStorage, and session state to ./sessions/{session_name}.json.
    Use this to save logged-in accounts (Google, GitHub, dashboards, etc.) for future reuse.

    Args:
        session_name: Identifier for the session snapshot (default: "default").
    """
    await manager.ensure_page()
    context = manager.context
    if not context:
        return {"success": False, "error": "No active browser context to save"}

    safe_name = _sanitize_name(session_name)
    session_file = SESSIONS_DIR / f"{safe_name}.json"

    # Export Playwright storage state directly to disk
    await context.storage_state(path=str(session_file))

    # Read summary for metadata
    try:
        with open(session_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            cookies_count = len(data.get("cookies", []))
            origins_count = len(data.get("origins", []))
            domains = list({c.get("domain", "") for c in data.get("cookies", []) if c.get("domain")})
    except Exception:
        cookies_count, origins_count, domains = 0, 0, []

    # Update index
    index = _load_sessions_index()
    index[safe_name] = {
        "session_name": safe_name,
        "filepath": str(session_file),
        "saved_at": datetime.datetime.now().isoformat(),
        "cookies_count": cookies_count,
        "origins_count": origins_count,
        "domains": domains[:10],
        "size_bytes": session_file.stat().st_size if session_file.exists() else 0,
    }
    _save_sessions_index(index)

    return {
        "success": True,
        "session_name": safe_name,
        "filepath": str(session_file),
        "cookies_saved": cookies_count,
        "origins_saved": origins_count,
        "domains": domains[:10],
        "message": f"Session '{safe_name}' saved successfully. Logins and cookies can be reloaded next time.",
    }


@mcp.tool()
async def load_session(session_name: str = "default") -> Dict[str, Any]:
    """
    Load saved session cookies and state from ./sessions/{session_name}.json into the active browser.
    Immediately restores logged-in accounts and cookies.

    Args:
        session_name: Name of the session state file to load (default: "default").
    """
    await manager.ensure_page()
    context = manager.context
    if not context:
        return {"success": False, "error": "No active browser context"}

    safe_name = _sanitize_name(session_name)
    session_file = SESSIONS_DIR / f"{safe_name}.json"
    if not session_file.exists():
        return {
            "success": False,
            "error": f"Session file not found: {session_file}. Use save_session first.",
        }

    with open(session_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    cookies = data.get("cookies", [])
    if cookies:
        await context.add_cookies(cookies)

    # Inject localStorage if page URL matches any origin
    page = manager.active_page
    injected_origins = 0
    if page and not page.is_closed():
        for origin in data.get("origins", []):
            orig_url = origin.get("origin", "")
            if orig_url and page.url.startswith(orig_url):
                for item in origin.get("localStorage", []):
                    try:
                        k = json.dumps(item.get("name", ""))
                        v = json.dumps(item.get("value", ""))
                        await page.evaluate(f"localStorage.setItem({k}, {v})")
                        injected_origins += 1
                    except Exception:
                        pass

    return {
        "success": True,
        "session_name": safe_name,
        "cookies_restored": len(cookies),
        "origins_injected": injected_origins,
        "message": f"Session '{safe_name}' loaded successfully into active browser context.",
    }


@mcp.tool()
async def list_sessions() -> Dict[str, Any]:
    """
    List all saved session snapshots in ./sessions with creation dates, cookie counts, and domains.
    """
    index = _load_sessions_index()
    # Check disk for any unindexed session files
    for p in SESSIONS_DIR.glob("*.json"):
        if p.name == "sessions_index.json":
            continue
        s_name = p.stem
        if s_name not in index:
            index[s_name] = {
                "session_name": s_name,
                "filepath": str(p),
                "saved_at": datetime.datetime.fromtimestamp(p.stat().st_mtime).isoformat(),
                "size_bytes": p.stat().st_size,
            }

    return {
        "sessions_dir": str(SESSIONS_DIR),
        "total_sessions": len(index),
        "sessions": list(index.values()),
    }


@mcp.tool()
async def delete_session(session_name: str) -> Dict[str, Any]:
    """
    Delete a saved session state file from ./sessions.

    Args:
        session_name: Name of the session state file to remove.
    """
    safe_name = _sanitize_name(session_name)
    session_file = SESSIONS_DIR / f"{safe_name}.json"
    existed = session_file.exists()
    if existed:
        session_file.unlink(missing_ok=True)

    index = _load_sessions_index()
    if safe_name in index:
        del index[safe_name]
        _save_sessions_index(index)

    return {"success": True, "deleted": existed, "session_name": safe_name}


@mcp.tool()
async def clear_browser_data() -> Dict[str, Any]:
    """
    Clear all cookies and permissions from the active browser context.
    """
    await manager.ensure_page()
    if manager.context:
        await manager.context.clear_cookies()
        await manager.context.clear_permissions()
        return {"success": True, "message": "Cookies and permissions cleared"}
    return {"success": False, "error": "No active context"}


# ===========================================================================
# GROUP 12: MOUSE COORDINATES & PRECISION GESTURES
# ===========================================================================
@mcp.tool()
async def mouse_click_coords(x: int, y: int, button: str = "left", click_count: int = 1) -> Dict[str, Any]:
    """
    Click exact (x, y) pixel coordinates on the page.
    Essential for canvas, charts, maps, sliders, and elements without clear selectors.

    Args:
        x: X coordinate in pixels.
        y: Y coordinate in pixels.
        button: "left", "right", or "middle".
        click_count: Number of clicks (1 for single, 2 for double).
    """
    page = await manager.ensure_page()
    await page.mouse.click(x=x, y=y, button=button, click_count=click_count)
    return {"success": True, "x": x, "y": y, "button": button, "click_count": click_count}


@mcp.tool()
async def mouse_move(x: int, y: int) -> Dict[str, Any]:
    """
    Move the mouse cursor to exact (x, y) coordinates.

    Args:
        x: Target X coordinate.
        y: Target Y coordinate.
    """
    page = await manager.ensure_page()
    await page.mouse.move(x=x, y=y)
    return {"success": True, "x": x, "y": y}


@mcp.tool()
async def mouse_down(button: str = "left") -> Dict[str, Any]:
    """
    Press and hold a mouse button down.

    Args:
        button: "left", "right", or "middle".
    """
    page = await manager.ensure_page()
    await page.mouse.down(button=button)
    return {"success": True, "button": button, "state": "down"}


@mcp.tool()
async def mouse_up(button: str = "left") -> Dict[str, Any]:
    """
    Release a held mouse button.

    Args:
        button: "left", "right", or "middle".
    """
    page = await manager.ensure_page()
    await page.mouse.up(button=button)
    return {"success": True, "button": button, "state": "up"}


@mcp.tool()
async def mouse_wheel(delta_x: int = 0, delta_y: int = 0) -> Dict[str, Any]:
    """
    Dispatch a mouse wheel scroll event with exact horizontal and vertical pixel deltas.

    Args:
        delta_x: Horizontal scroll amount in pixels.
        delta_y: Vertical scroll amount in pixels.
    """
    page = await manager.ensure_page()
    await page.mouse.wheel(delta_x=delta_x, delta_y=delta_y)
    return {"success": True, "delta_x": delta_x, "delta_y": delta_y}


# ===========================================================================
# GROUP 13: FRAMES & IFRAMES
# ===========================================================================
@mcp.tool()
async def frame_list() -> Dict[str, Any]:
    """
    List all frames and iframes embedded in the active page with names and URLs.
    """
    page = await manager.ensure_page()
    frames = []
    for idx, f in enumerate(page.frames):
        frames.append(
            {
                "index": idx,
                "name": f.name,
                "url": f.url,
                "is_main_frame": f == page.main_frame,
            }
        )
    return {"frames_count": len(frames), "frames": frames}


@mcp.tool()
async def frame_click(frame_selector: str, selector: str, timeout_ms: int = 5000) -> Dict[str, Any]:
    """
    Click an element located inside an iframe.

    Args:
        frame_selector: Selector for the iframe element, or its name/url.
        selector: Target element selector inside the iframe.
        timeout_ms: Timeout in milliseconds.
    """
    page = await manager.ensure_page()
    frame = page.frame_locator(frame_selector)
    await frame.locator(selector).first.click(timeout=timeout_ms)
    return {"success": True, "frame_selector": frame_selector, "selector": selector}


@mcp.tool()
async def frame_type_text(
    frame_selector: str,
    selector: str,
    text: str,
    clear_first: bool = True,
    timeout_ms: int = 5000,
) -> Dict[str, Any]:
    """
    Type text into an input or textarea element inside an iframe.

    Args:
        frame_selector: Selector for the iframe.
        selector: Target element selector inside the iframe.
        text: Text to enter.
        clear_first: Clear existing text first.
        timeout_ms: Timeout in milliseconds.
    """
    page = await manager.ensure_page()
    loc = page.frame_locator(frame_selector).locator(selector).first
    if clear_first:
        await loc.fill(text, timeout=timeout_ms)
    else:
        current = await loc.input_value()
        await loc.fill(current + text, timeout=timeout_ms)
    return {"success": True, "frame_selector": frame_selector, "selector": selector, "chars_typed": len(text)}


@mcp.tool()
async def frame_get_text(frame_selector: str, selector: str = "", timeout_ms: int = 5000) -> Dict[str, Any]:
    """
    Extract text from an element or the body inside an iframe.

    Args:
        frame_selector: Selector for the iframe.
        selector: Optional selector inside iframe (empty = body).
        timeout_ms: Timeout in milliseconds.
    """
    page = await manager.ensure_page()
    frame = page.frame_locator(frame_selector)
    target_sel = selector or "body"
    text = await frame.locator(target_sel).first.inner_text(timeout=timeout_ms)
    return {"text": text, "frame_selector": frame_selector, "selector": target_sel, "length": len(text)}


@mcp.tool()
async def frame_evaluate(frame_selector: str, script: str) -> Dict[str, Any]:
    """
    Execute custom JavaScript inside the context of an iframe.

    Args:
        frame_selector: Name or URL substring of the frame.
        script: JavaScript string to evaluate.
    """
    page = await manager.ensure_page()
    target_frame: Optional[Frame] = None
    for f in page.frames:
        if f.name == frame_selector or frame_selector in f.url:
            target_frame = f
            break

    if not target_frame:
        return {"success": False, "error": f"Frame '{frame_selector}' not found"}

    result = await target_frame.evaluate(script)
    return {"success": True, "result": result}


# ===========================================================================
# GROUP 14: PDF GENERATION
# ===========================================================================
@mcp.tool()
async def save_as_pdf(
    filename: str = "",
    format: str = "A4",
    landscape: bool = False,
    print_background: bool = True,
) -> Dict[str, Any]:
    """
    Save the current page as a PDF file into the dedicated ./evidence folder.
    (Note: PDF printing is supported in Chromium / headless mode).

    Args:
        filename: Optional custom PDF file name.
        format: Paper format, e.g. "A4" (default), "Letter", "Legal".
        landscape: Print in landscape orientation if True.
        print_background: Print CSS backgrounds and colors.
    """
    page = await manager.ensure_page()
    timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = _sanitize_name(filename or "document")
    pdf_filename = f"evidence_{timestamp_str}_{safe_name}.pdf"
    pdf_filepath = EVIDENCE_DIR / pdf_filename

    try:
        await page.pdf(
            path=str(pdf_filepath),
            format=format,
            landscape=landscape,
            print_background=print_background,
        )
        file_size = pdf_filepath.stat().st_size
        return {
            "success": True,
            "filename": pdf_filename,
            "filepath": str(pdf_filepath),
            "size_bytes": file_size,
            "format": format,
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "hint": "PDF generation requires Chromium engine in headless mode. Try browser_launch(browser_type='chromium', headless=True).",
        }


# ===========================================================================
# GROUP 15: DIALOGS & ALERTS AUTO-HANDLING
# ===========================================================================
@mcp.tool()
async def dialog_set_handler(action: str = "accept", prompt_text: str = "") -> Dict[str, Any]:
    """
    Configure automatic dialog response for JavaScript alert(), confirm(), and prompt().
    Prevents browser automation from hanging on dialog prompts.

    Args:
        action: "accept" (click OK/Confirm, default) or "dismiss" (click Cancel).
        prompt_text: Optional text to enter into prompt dialogs.
    """
    manager.dialog_action = action.lower()
    manager.dialog_prompt_text = prompt_text
    return {"success": True, "action": manager.dialog_action, "prompt_text": prompt_text}


@mcp.tool()
async def dialog_get_history(limit: int = 20) -> Dict[str, Any]:
    """
    Get history of JavaScript dialogs (alerts, confirms, prompts) intercepted on the page.

    Args:
        limit: Max items to return.
    """
    items = list(manager.dialog_history)[-limit:]
    return {"dialog_count": len(items), "dialogs": items}


# ===========================================================================
# GROUP 16: CONSOLE LOGS & PAGE ERRORS
# ===========================================================================
@mcp.tool()
async def get_console_logs(limit: int = 50, log_type: str = "") -> Dict[str, Any]:
    """
    Retrieve recent JavaScript console logs (console.log, console.warn, console.error) from the page.

    Args:
        limit: Maximum number of recent log entries to return.
        log_type: Optional filter by type ("log", "warning", "error", "info").
    """
    logs = list(manager.console_logs)
    if log_type:
        logs = [entry for entry in logs if entry.get("type") == log_type]
    recent = logs[-limit:]
    return {"total_captured": len(manager.console_logs), "returned": len(recent), "logs": recent}


@mcp.tool()
async def get_page_errors(limit: int = 20) -> Dict[str, Any]:
    """
    Retrieve uncaught JavaScript runtime exceptions and page errors thrown by the active page.

    Args:
        limit: Maximum errors to return.
    """
    errors = list(manager.page_errors)[-limit:]
    return {"total_errors": len(manager.page_errors), "returned": len(errors), "errors": errors}


# ===========================================================================
# GROUP 17: NETWORK MONITORING & RESOURCE BLOCKING
# ===========================================================================
@mcp.tool()
async def network_get_activity(limit: int = 50, filter_failed_only: bool = False) -> Dict[str, Any]:
    """
    Retrieve recent HTTP network requests and responses made by the active page.

    Args:
        limit: Maximum recent items to return.
        filter_failed_only: Return only responses with HTTP status >= 400.
    """
    items = list(manager.network_activity)
    if filter_failed_only:
        items = [i for i in items if i.get("direction") == "response" and i.get("status", 0) >= 400]
    recent = items[-limit:]
    return {"total_activity": len(manager.network_activity), "returned": len(recent), "activity": recent}


@mcp.tool()
async def network_block_resources(resource_types: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Block heavy resources (e.g. image, media, font, stylesheet) to maximize page load speeds.

    Args:
        resource_types: List of resource types to block (default: ["image", "media", "font"]).
    """
    types_to_block = set(resource_types or ["image", "media", "font"])
    manager.blocked_resource_types = types_to_block
    page = await manager.ensure_page()
    await manager._apply_route_blocking(page)
    return {"success": True, "blocked_types": list(types_to_block)}


@mcp.tool()
async def network_unblock_resources() -> Dict[str, Any]:
    """
    Remove resource blocking rules and allow all images, media, and fonts to load normally.
    """
    manager.blocked_resource_types = set()
    page = await manager.ensure_page()
    try:
        await page.unroute("**/*")
    except Exception:
        pass
    return {"success": True, "message": "All resource blocking rules cleared"}


@mcp.tool()
async def network_set_headers(headers: Dict[str, str]) -> Dict[str, Any]:
    """
    Set extra HTTP headers for all subsequent network requests from the active context.

    Args:
        headers: Key-value dictionary of HTTP headers (e.g. {"Authorization": "Bearer ...", "Custom-Header": "value"}).
    """
    await manager.ensure_page()
    if manager.context:
        await manager.context.set_extra_http_headers(headers)
        return {"success": True, "headers_set": list(headers.keys())}
    return {"success": False, "error": "No active context"}


# ===========================================================================
# GROUP 18: GRANULAR COOKIES & STORAGE MANAGEMENT
# ===========================================================================
@mcp.tool()
async def cookies_get(urls: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Retrieve all browser cookies or cookies for specific URLs.

    Args:
        urls: Optional list of URLs to filter cookies.
    """
    await manager.ensure_page()
    if not manager.context:
        return {"cookies": []}
    cookies = await manager.context.cookies(urls=urls)
    return {"cookies_count": len(cookies), "cookies": cookies}


@mcp.tool()
async def cookies_set(cookies: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Add or update specific cookies in the active browser context.

    Args:
        cookies: List of cookie dictionaries with fields: name, value, domain, path, etc.
    """
    await manager.ensure_page()
    if not manager.context:
        return {"success": False, "error": "No active context"}
    await manager.context.add_cookies(cookies)
    return {"success": True, "cookies_added": len(cookies)}


@mcp.tool()
async def cookies_delete(name: str, domain: str = "", path: str = "") -> Dict[str, Any]:
    """
    Delete a specific cookie by name.

    Args:
        name: Name of cookie to delete.
        domain: Optional domain filter.
        path: Optional path filter.
    """
    await manager.ensure_page()
    if not manager.context:
        return {"success": False, "error": "No active context"}
    cookies = await manager.context.cookies()
    remaining = [
        c
        for c in cookies
        if not (
            c.get("name") == name
            and (not domain or c.get("domain") == domain)
            and (not path or c.get("path") == path)
        )
    ]
    await manager.context.clear_cookies()
    if remaining:
        await manager.context.add_cookies(remaining)
    return {"success": True, "deleted_cookie": name, "remaining_count": len(remaining)}


@mcp.tool()
async def storage_get(key: str = "", storage_type: str = "localStorage") -> Dict[str, Any]:
    """
    Read an individual key or all key-value pairs from localStorage or sessionStorage.

    Args:
        key: Specific key to read (empty = return all key-values).
        storage_type: "localStorage" (default) or "sessionStorage".
    """
    page = await manager.ensure_page()
    target = "localStorage" if storage_type.lower() == "localstorage" else "sessionStorage"
    if key:
        js = f"() => window.{target}.getItem({json.dumps(key)})"
        val = await page.evaluate(js)
        return {"storage_type": target, "key": key, "value": val}
    else:
        js = f"""() => {{
            const res = {{}};
            for (let i = 0; i < window.{target}.length; i++) {{
                const k = window.{target}.key(i);
                res[k] = window.{target}.getItem(k);
            }}
            return res;
        }}"""
        all_items = await page.evaluate(js)
        return {"storage_type": target, "count": len(all_items), "items": all_items}


@mcp.tool()
async def storage_set(key: str, value: str, storage_type: str = "localStorage") -> Dict[str, Any]:
    """
    Set a key-value pair in localStorage or sessionStorage.

    Args:
        key: Storage key name.
        value: Storage value string.
        storage_type: "localStorage" (default) or "sessionStorage".
    """
    page = await manager.ensure_page()
    target = "localStorage" if storage_type.lower() == "localstorage" else "sessionStorage"
    js = f"() => window.{target}.setItem({json.dumps(key)}, {json.dumps(value)})"
    await page.evaluate(js)
    return {"success": True, "storage_type": target, "key": key, "value": value}


@mcp.tool()
async def storage_clear(storage_type: str = "localStorage") -> Dict[str, Any]:
    """
    Clear all items in localStorage or sessionStorage on the active page.

    Args:
        storage_type: "localStorage" (default) or "sessionStorage".
    """
    page = await manager.ensure_page()
    target = "localStorage" if storage_type.lower() == "localstorage" else "sessionStorage"
    await page.evaluate(f"() => window.{target}.clear()")
    return {"success": True, "storage_type": target, "cleared": True}


# ===========================================================================
# GROUP 19: EMULATION, GEOLOCATION, PERMISSIONS & CLIPBOARD
# ===========================================================================
@mcp.tool()
async def set_geolocation(latitude: float, longitude: float, accuracy: float = 100.0) -> Dict[str, Any]:
    """
    Emulate geographic GPS coordinates for location-aware web applications.

    Args:
        latitude: Latitude in decimal degrees (e.g. 37.7749).
        longitude: Longitude in decimal degrees (e.g. -122.4194).
        accuracy: Accuracy radius in meters.
    """
    await manager.ensure_page()
    if manager.context:
        await manager.context.set_geolocation({"latitude": latitude, "longitude": longitude, "accuracy": accuracy})
        await manager.context.grant_permissions(["geolocation"])
        return {"success": True, "latitude": latitude, "longitude": longitude, "accuracy": accuracy}
    return {"success": False, "error": "No active context"}


@mcp.tool()
async def set_permissions(permissions: List[str], origin: str = "") -> Dict[str, Any]:
    """
    Grant browser permissions (e.g. "geolocation", "notifications", "clipboard-read", "clipboard-write").

    Args:
        permissions: List of permission strings.
        origin: Optional origin domain (empty = all origins).
    """
    await manager.ensure_page()
    if manager.context:
        kwargs: Dict[str, Any] = {}
        if origin:
            kwargs["origin"] = origin
        await manager.context.grant_permissions(permissions, **kwargs)
        return {"success": True, "granted_permissions": permissions}
    return {"success": False, "error": "No active context"}


@mcp.tool()
async def set_offline(offline: bool = True) -> Dict[str, Any]:
    """
    Simulate offline / network disconnection mode to test error states or offline PWA behavior.

    Args:
        offline: True to disconnect network, False to reconnect.
    """
    await manager.ensure_page()
    if manager.context:
        await manager.context.set_offline(offline)
        return {"success": True, "offline": offline}
    return {"success": False, "error": "No active context"}


@mcp.tool()
async def clipboard_get() -> Dict[str, Any]:
    """
    Read text currently stored in the browser clipboard.
    """
    page = await manager.ensure_page()
    try:
        text = await page.evaluate("() => navigator.clipboard.readText()")
        return {"success": True, "clipboard_text": text}
    except Exception as e:
        return {"success": False, "error": str(e), "hint": "Grant clipboard permissions via set_permissions(['clipboard-read'])"}


@mcp.tool()
async def clipboard_set(text: str) -> Dict[str, Any]:
    """
    Write text to the browser clipboard.

    Args:
        text: Text to copy to clipboard.
    """
    page = await manager.ensure_page()
    try:
        await page.evaluate(f"() => navigator.clipboard.writeText({json.dumps(text)})")
        return {"success": True, "chars_written": len(text)}
    except Exception as e:
        return {"success": False, "error": str(e), "hint": "Grant clipboard permissions via set_permissions(['clipboard-write'])"}


# ===========================================================================
# MAIN ENTRYPOINT
# ===========================================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="High-Speed Browser Playwright MCP Server")
    parser.add_argument("--transport", default="stdio", choices=["stdio", "sse", "streamable-http"], help="Transport mode")
    parser.add_argument("--port", type=int, default=8000, help="Port for SSE / HTTP transport")
    parser.add_argument("--host", default="127.0.0.1", help="Host for SSE / HTTP transport")
    args, unknown = parser.parse_known_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=args.transport, host=args.host, port=args.port)
