# 🌐 Browser Playwright MCP Server

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://python.org)
[![FastMCP](https://img.shields.io/badge/FastMCP-v4.0%2B-green.svg)](https://github.com/jlowin/fastmcp)
[![Playwright](https://img.shields.io/badge/Playwright-v1.40%2B-orange.svg)](https://playwright.dev/python/)
[![License](https://img.shields.io/badge/License-MIT-purple.svg)](LICENSE)

An **ultra-fast, production-ready Playwright Browser MCP server** built with **FastMCP** in a single file with **zero `.env` dependencies**.

Designed for AI coding assistants (Claude Desktop, Cursor, Antigravity IDE, Windsurf, Copilot) to control **real, visible external desktop browsers (Google Chrome / Microsoft Edge)** or attach to live running instances via Chrome DevTools Protocol (CDP).

---

## ⚡ Highlights

* **🖥️ Strictly External Desktop Browser**: Controls your real system Google Chrome or Microsoft Edge browser visibly on screen. Never gets stuck in invisible background headless states.
* **🔌 Instant CDP Attachment**: Connects seamlessly to your already running Chrome browser (`--remote-debugging-port=9222`), inheriting all your existing logins, cookies, and extensions.
* **⚡ Blazing Fast Performance**: Defaults to `domcontentloaded` for sub-second navigation (~20–80ms), sub-millisecond JavaScript execution, zero input delays, and instant response times.
* **🔐 Double-Layered Session Persistence**:
  1. **Persistent Browser Profile**: Native Chrome user-data-dir preserves logins, accounts, and credentials automatically across restarts.
  2. **Exportable Session Snapshots**: Save and reload named session snapshots (`save_session` / `load_session`) as portable JSON files containing cookies and `localStorage`.
* **📸 Dedicated Evidence & Visual Verification**:
  * Saves timestamped screenshots into `./evidence/` with an audit log (`evidence_log.json`).
  * Automated visual pixel diffing using Pillow (`PIL`) with configurable sensitivity thresholds.
  * One-click generation of evidence audit reports in HTML and Markdown formats.
* **🛠️ 82 Comprehensive Tools**: Covers tab switching, batch form filling, file uploads, iframe handling, coordinate mouse actions, dialog auto-handling, network resource blocking, console/error monitoring, geolocation spoofing, and clipboard operations.
* **📦 Single-File Architecture**: Everything lives in [`server.py`](server.py). No external `.env` file required. All folder structures (`evidence/`, `sessions/`, `browser_profile/`) resolve automatically.

---

## 📁 Repository Structure

```text
browser-mcp/
├── server.py              # Complete MCP server with 82 tools (single file)
├── .gitignore             # Ignores runtime profiles, caches, and secrets
├── README.md              # Documentation & usage guide
├── evidence/              # Dedicated screenshot evidence & audit reports
│   └── .gitkeep
└── sessions/              # Exported session snapshots (cookies & storage)
    └── .gitkeep
```

---

## 🚀 Quickstart & Installation

### 1. Prerequisites
* Python 3.10 or higher
* Google Chrome or Microsoft Edge installed on your machine

### 2. Clone and Setup Environment

```bash
git clone https://github.com/your-username/browser-mcp.git
cd browser-mcp

# Create virtual environment
python -m venv .venv

# Activate virtual environment
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Linux / macOS:
source .venv/bin/activate

# Install dependencies
pip install fastmcp playwright pillow

# Ensure Playwright browser dependencies are available
playwright install chromium
```

---

## ⚙️ Connecting to AI IDEs & Clients

Add `browser-mcp` to your MCP configuration file:

### Claude Desktop
Add to your `claude_desktop_config.json`:
* **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
* **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "browser-mcp": {
      "command": "/path/to/browser-mcp/.venv/bin/python",
      "args": [
        "/path/to/browser-mcp/server.py"
      ],
      "env": {
        "BROWSER_TYPE": "chrome",
        "BROWSER_HEADLESS": "false",
        "BROWSER_STRICT_EXTERNAL": "true"
      }
    }
  }
}
```

> **Windows Note**: Use forward slashes or escaped backslashes for the Python executable:  
> `"command": "C:/path/to/browser-mcp/.venv/Scripts/python.exe"`  
> `"args": ["C:/path/to/browser-mcp/server.py"]`

### Cursor / Antigravity IDE / Windsurf
Add to your project's `.agents/mcp_config.json` or global configuration (`~/.gemini/config/mcp_config.json`):

```json
{
  "mcpServers": {
    "browser-mcp": {
      "command": "/path/to/browser-mcp/.venv/bin/python",
      "args": [
        "/path/to/browser-mcp/server.py"
      ],
      "env": {
        "BROWSER_TYPE": "chrome",
        "BROWSER_HEADLESS": "false",
        "BROWSER_STRICT_EXTERNAL": "true"
      }
    }
  }
}
```

*(Replace `/path/to/browser-mcp/` with the actual path to your cloned repository).*

---

## 🔧 Environment Configuration

You can customize `browser-mcp` via optional environment variables:

| Variable | Default | Description |
| :--- | :---: | :--- |
| `BROWSER_TYPE` | `"chrome"` | External browser to prioritize: `"chrome"` or `"msedge"`. |
| `BROWSER_HEADLESS` | `"false"` | `"false"` for visible desktop window; `"true"` for headless. |
| `BROWSER_STRICT_EXTERNAL` | `"true"` | When `"true"`, enforces real desktop browser launch and disables silent fallback to bundled headless chromium. |

---

## 📖 Complete Tool Catalog (82 Tools)

### 1. Browser Lifecycle & External Connections
* `browser_launch`: Launch external browser (Chrome / Edge) with persistent profile.
* `browser_connect_cdp`: Connect to existing running browser instance over CDP (`http://127.0.0.1:9222`).
* `browser_status`: Get active connection mode, browser type, tab count, and status.
* `browser_close`: Gracefully close active browser session and free resources.

### 2. Tab & Context Management
* `tab_list`: List all open tabs with indexes, URLs, and page titles.
* `tab_switch`: Switch focus to a tab by zero-based index.
* `tab_new`: Open a fresh tab, optionally navigating to an initial URL.
* `tab_close`: Close a tab by index (or current tab).

### 3. High-Speed Navigation & Page Control
* `navigate`: Navigate active page at high speed (`wait_until`: `"domcontentloaded"` / `"load"` / `"networkidle"`).
* `go_back`: Navigate backwards in browser history.
* `go_forward`: Navigate forwards in browser history.
* `reload`: Reload current page with optional cache bypass.
* `get_page_info`: Fetch current URL, title, viewport dimensions, and cookie count.

### 4. Interactions, Forms & Inputs
* `click`: Click an element with optional double/triple clicks and modifiers.
* `double_click`: Quick double-click shortcut.
* `hover`: Hover mouse over element to trigger tooltips and dropdowns.
* `focus`: Focus an input field or button.
* `drag_and_drop`: Drag source element onto target element.
* `type_text`: Fast input typing with optional millisecond delay and clearing.
* `press_key`: Send specific keyboard keys (`Enter`, `Escape`, `Tab`, `ArrowDown`, etc.).
* `keyboard_down`: Hold key down for shortcut sequences.
* `keyboard_up`: Release held key.
* `select_option`: Choose option in standard `<select>` dropdowns.
* `check_checkbox`: Check or uncheck a checkbox / radio input.
* `fill_form`: Batch-populate an entire form from a key-value dictionary in a single step.
* `upload_file`: Upload one or multiple files into file input selectors.

### 5. DOM Inspection & Execution
* `get_text`: Extract visible text content from selector.
* `get_attribute`: Read attribute value (`href`, `src`, `data-*`, `value`).
* `get_html`: Retrieve `innerHTML` or `outerHTML`.
* `snapshot_dom`: Capture a clean, token-efficient DOM snapshot for LLM analysis.
* `evaluate_script`: Execute arbitrary JavaScript expressions or functions in page context.

### 6. Scrolling & Viewport
* `scroll`: Scroll vertically or horizontally by pixel offsets.
* `scroll_to`: Scroll element into visible viewport view.
* `set_viewport`: Resize browser viewport dimensions.

### 7. Synchronization & Waiting
* `wait_for_selector`: Wait until an element is attached, visible, or detached.
* `wait_for_load_state`: Wait for page load state (`domcontentloaded`, `load`, `networkidle`).
* `wait_for_timeout`: Sleep for an exact duration in milliseconds.

### 8. Evidence Management & Visual Verification
* `take_screenshot`: Save timestamped screenshot into `./evidence/` and register in audit log.
* `verify_element_visible`: Assert element visibility with optional automatic screenshot evidence.
* `verify_text_present`: Assert text existence with optional evidence capture.
* `verify_url`: Assert current URL matches expected string or regex pattern.
* `verify_title`: Assert page title matches expected pattern.
* `verify_screenshot_diff`: Compare live screenshot against baseline image with Pillow pixel diffing.
* `list_evidence`: List all captured screenshots and audit entries.
* `get_latest_evidence`: Retrieve metadata and filepath of the latest evidence capture.
* `export_evidence_report`: Generate formatted HTML and Markdown audit test reports.
* `clear_evidence`: Clean up old screenshots and audit logs.

### 9. Session Persistence & Storage
* `save_session`: Export cookies and `localStorage` to `./sessions/{name}.json`.
* `load_session`: Restore saved session state and cookies into active browser.
* `list_sessions`: List all available saved session profiles.
* `delete_session`: Remove an obsolete saved session file.
* `clear_browser_data`: Clear cookies, cache, and origin storage.

### 10. Mouse & Coordinate Actions
* `mouse_click_coords`: Click exact `(x, y)` coordinate on screen.
* `mouse_move`: Move mouse cursor to coordinate.
* `mouse_down`: Press down mouse button.
* `mouse_up`: Release mouse button.
* `mouse_wheel`: Scroll mouse wheel by delta pixels.

### 11. Iframe & Embedded Content Control
* `frame_list`: List all iframes on page with names and URLs.
* `frame_click`: Click an element inside a specific frame.
* `frame_type_text`: Type text inside an element within a frame.
* `frame_get_text`: Read text from inside a frame.
* `frame_evaluate`: Execute custom JavaScript inside a specific frame.

### 12. PDF Export
* `save_as_pdf`: Export active page to PDF with custom margins and print settings.

### 13. Dialog & Alert Handling
* `dialog_set_handler`: Auto-accept or dismiss `alert()`, `confirm()`, and `prompt()` dialogs.
* `dialog_get_history`: View history of intercepted browser dialogs.

### 14. Network Inspection & Diagnostics
* `get_console_logs`: Retrieve recent browser `console.log/warn/error` entries.
* `get_page_errors`: Retrieve unhandled JavaScript runtime exceptions.
* `network_get_activity`: Inspect recent HTTP requests and responses.
* `network_block_resources`: Block heavy resource types (`image`, `media`, `font`, `stylesheet`) for maximum speed.
* `network_unblock_resources`: Clear all active resource blocks.
* `network_set_headers`: Set custom HTTP headers for upcoming requests.

### 15. Advanced Storage & Cookies
* `cookies_get`: Retrieve active cookies for current domain or specific names.
* `cookies_set`: Inject custom cookies into browser.
* `cookies_delete`: Remove specific cookies.
* `storage_get`: Retrieve `localStorage` or `sessionStorage` key values.
* `storage_set`: Set storage key values.
* `storage_clear`: Clear `localStorage` or `sessionStorage`.

### 16. Emulation, Sensors & System
* `set_geolocation`: Spoof GPS coordinates (`latitude`, `longitude`, `accuracy`).
* `set_permissions`: Grant browser permissions (`geolocation`, `notifications`, `clipboard`).
* `set_offline`: Emulate offline network conditions.
* `clipboard_get`: Read clipboard text from page context.
* `clipboard_set`: Write text to clipboard in page context.

---

## 💡 Practical Examples

### 1. Connecting to your daily Chrome with Logged-In Accounts
Launch Chrome from your terminal with remote debugging enabled:
```bash
# Windows:
chrome.exe --remote-debugging-port=9222 --remote-allow-origins=*

# macOS:
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --remote-allow-origins=*
```
Then in your AI assistant:
```text
"Connect to my running browser via browser_connect_cdp and extract my latest emails"
```

### 2. Auto-Saving Login Sessions for Reuse
```text
"Navigate to amazon.in and let me log in. Once logged in, save the session as 'amazon_personal' using save_session."
```
Next time:
```text
"Load session 'amazon_personal' and search for best laptops under 60000 rs."
```

### 3. Visual Regression Testing with Evidence
```text
"Navigate to https://example.com, take a screenshot called 'homepage_baseline', and verify the title contains 'Example Domain'. Then export an evidence report."
```

---

## 🤝 Contributing & License

Contributions, bug reports, and PRs are welcome! Feel free to open issues on GitHub.

Distributed under the **MIT License**. See `LICENSE` for details.
