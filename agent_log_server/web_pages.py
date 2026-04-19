from pathlib import Path
from typing import Any, Dict, List
import hashlib

from fastapi.responses import HTMLResponse
from fastcore.xml import (
    Html, Head, Body, Div, Section, Header, Footer, Main, H1, H2, H3, P, Button,
    Span, Input, Textarea, Label, Small, Script, Link, Meta, Ul, Li, Img, to_xml,
    NotStr,
)

CODEX_AGENT_THEME_COLOR = "#0d0f13"
CODEX_AGENT_ICON_PATH = "/static/codexas-icon.svg"
CODEX_AGENT_START_URL = "/"
CODEX_AGENT_SCOPE = "/"


def _asset(package_root: Path, url: str) -> str:
    """Append a cache-busting query string based on file mtime."""
    if not url.startswith("/static/"):
        return url
    rel = url.lstrip("/")
    path = package_root / rel
    try:
        mtime = int(path.stat().st_mtime)
        return f"{url}?v={mtime}"
    except Exception:
        return url


def render_appserver_ui(package_root: Path) -> HTMLResponse:
    return HTMLResponse(
        to_xml(
            Html(
            Head(
                Link(rel="stylesheet", href=_asset(package_root, "/static/appserver.css")),
                Script(src=_asset(package_root, "/static/vendor/socket.io/socket.io.min.js"), defer=True),
                Script(src=_asset(package_root, "/static/appserver.js"), defer=True),
            ),
            Body(
                Div(
                    Header(
                        Div(
                            H1("App Server"),
                            Small("Codex JSON-RPC • Framework-Shells pipe"),
                            cls="brand"
                        ),
                        Div(
                            Div(
                                Span("Status"),
                                Span("disconnected", id="appserver-status", cls="pill warn"),
                                cls="status-pill"
                            ),
                            Button("Start", id="appserver-start", cls="btn"),
                            Button("Stop", id="appserver-stop", cls="btn ghost"),
                            cls="toolbar"
                        ),
                        cls="topbar"
                    ),
                    Main(
                        Section(
                            H2("Project"),
                            Label(
                                Span("Root"),
                                Input(type="text", id="project-root", placeholder="/data/data/..."),
                            ),
                            Label(
                                Span("Command"),
                                Input(type="text", id="appserver-command", placeholder="codex-app-server"),
                            ),
                            Div(
                                Button("Pick CWD", id="pick-cwd", cls="btn ghost"),
                                Button("Apply", id="apply-project", cls="btn"),
                                cls="row"
                            ),
                            H3("Threads"),
                            Div(
                                Button("Refresh", id="threads-refresh", cls="btn ghost"),
                                Button("New", id="thread-new", cls="btn"),
                                cls="row"
                            ),
                            Ul(
                                Li("No threads yet", cls="muted"),
                                id="thread-list",
                                cls="thread-list"
                            ),
                            cls="panel"
                        ),
                        Section(
                            H2("Conversation"),
                            Div(
                                Div(id="timeline", cls="timeline"),
                                cls="timeline-wrap"
                            ),
                            Div(
                                Textarea(
                                    id="prompt",
                                    placeholder="Type a prompt... (Shift+Enter for newline)",
                                ),
                                Button("Send", id="turn-send", cls="btn primary"),
                                cls="composer"
                            ),
                            cls="panel wide"
                        ),
                        Section(
                            H2("Approvals"),
                            Div(
                                P("No pending approvals", cls="muted"),
                                id="approvals-list"
                            ),
                            H2("Diffs"),
                            Div(
                                P("No diffs yet", cls="muted"),
                                id="diffs-list"
                            ),
                            H2("Policy"),
                            Label(
                                Span("Sandbox"),
                                Input(type="text", id="policy-sandbox", placeholder="Use runtime default"),
                            ),
                            Label(
                                Span("Approval"),
                                Input(type="text", id="policy-approval", placeholder="Use runtime default"),
                            ),
                            cls="panel"
                        ),
                        cls="grid"
                    ),
                    Footer(
                        Div(
                            Span("WS"),
                            Span("idle", id="ws-status", cls="pill"),
                            cls="status-pill"
                        ),
                        Div(
                            Span("Mode"),
                            Span("portrait-friendly", cls="pill ok"),
                            cls="status-pill"
                        ),
                        cls="footer"
                    ),
                    cls="appshell"
                )
            )
            )
        )
    )


def build_codex_agent_manifest(package_root: Path) -> Dict[str, Any]:
    version = _codex_agent_version(package_root)
    start_url = f"{CODEX_AGENT_START_URL}?v={version}"
    icon_url = _asset(package_root, CODEX_AGENT_ICON_PATH)
    return {
        "id": start_url,
        "name": "CodexAS-Extension",
        "short_name": "CodexAS",
        "start_url": start_url,
        "scope": CODEX_AGENT_SCOPE,
        "display": "standalone",
        "background_color": CODEX_AGENT_THEME_COLOR,
        "theme_color": CODEX_AGENT_THEME_COLOR,
        "icons": [
            {
                "src": icon_url,
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any",
            }
        ],
    }


def _codex_agent_version(package_root: Path) -> str:
    paths = [
        Path(__file__),
        package_root / "static" / "codex_agent.css",
        package_root / "static" / "dist" / "codex_agent.js",
        package_root / CODEX_AGENT_ICON_PATH.lstrip("/"),
    ]
    parts: List[str] = []
    for path in paths:
        try:
            parts.append(str(int(path.stat().st_mtime)))
        except Exception:
            continue
    raw = "|".join(parts)
    if not raw:
        return "v0"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def build_codex_agent_sw(package_root: Path) -> str:
    version = _codex_agent_version(package_root)
    start_url = f"{CODEX_AGENT_START_URL}?v={version}"
    css_url = _asset(package_root, "/static/codex_agent.css")
    js_url = _asset(package_root, "/static/dist/codex_agent.js")
    icon_url = _asset(package_root, CODEX_AGENT_ICON_PATH)
    return f"""const CACHE_NAME = 'codexas-extension-{version}';
const PRECACHE_URLS = [
  '{start_url}',
  '{css_url}',
  '{js_url}',
  '{icon_url}',
];
const DIRECT_FETCH_PATHS = new Set([
  '/sw.js',
  '/manifest.json',
  '/codex-agent/sw.js',
  '/codex-agent/manifest.json',
]);
const APP_CACHEABLE_PATHS = new Set(['/']);

self.addEventListener('install', (event) => {{
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting())
  );
}});

self.addEventListener('activate', (event) => {{
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
}});

self.addEventListener('fetch', (event) => {{
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;
  if (DIRECT_FETCH_PATHS.has(url.pathname)) {{
    event.respondWith(fetch(event.request));
    return;
  }}
  if (APP_CACHEABLE_PATHS.has(url.pathname) || url.pathname.startsWith('/static/')) {{
    event.respondWith(
      fetch(event.request)
        .then((response) => {{
          if (response.ok) {{
            const clone = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          }}
          return response;
        }})
        .catch(() => caches.match(event.request))
    );
    return;
  }}

  const key = url.pathname + url.search;
  if (PRECACHE_URLS.includes(key)) {{
    event.respondWith(
      caches.match(event.request).then((cached) => cached || fetch(event.request))
    );
    return;
  }}

  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
}});
"""


def render_codex_agent_ui(package_root: Path) -> HTMLResponse:
    version = _codex_agent_version(package_root)
    conversation_switcher = Div(
        Div(
            Span("Switch conversation", cls="conversation-mini-title"),
            Button("×", id="conversation-mini-close", cls="btn ghost tiny"),
            cls="conversation-mini-header",
        ),
        Div(id="conversation-mini-list", cls="conversation-mini-list"),
        cls="conversation-mini-drawer",
        id="conversation-mini-drawer",
        **{"aria-hidden": "true"},
    )
    conversation_main = Div(
        Div(
            Div(
                id="agent-timeline",
                cls="timeline",
            ),
            cls="timeline-wrap",
        ),
        Div(
            Span(cls="status-spinner"),
            Span("idle", id="status-label", cls="status-text"),
            Span("", id="status-reasoning", cls="status-reasoning"),
            Span(cls="status-dot", id="status-dot"),
            cls="status-ribbon",
            id="status-ribbon",
        ),
        Div(
            Div(
                id="agent-prompt",
                contenteditable="true",
                cls="prompt-input",
                **{"data-placeholder": "@ to mention files"},
            ),
            cls="composer",
        ),
        Footer(
            Div(id="footer-runtime-controls", cls="footer-runtime-controls"),
            Div(
                Span("context:"),
                Span("—", id="context-remaining", cls="pill"),
                cls="status-pill footer-cell",
            ),
            Div(
                Button("Send", id="agent-send", cls="btn primary"),
                cls="status-pill footer-cell footer-send",
            ),
            Div(
                Span("Scroll"),
                Button("Pinned", id="scroll-pin", cls="btn tiny toggle active"),
                cls="status-pill footer-cell",
            ),
            Div(
                Span("mention", id="mention-pill", cls="pill"),
                cls="status-pill footer-cell",
            ),
            Div(
                Span("Tokens"),
                Span("0", id="counter-tokens", cls="pill"),
                cls="status-pill footer-cell",
            ),
            Div(
                Button("Interrupt", id="turn-interrupt", cls="btn danger"),
                cls="status-pill footer-cell footer-end",
            ),
            cls="footer",
        ),
        cls="conversation-main",
    )
    conversation_drawer = Section(
        Div(
            Div(
                H2(
                    "Conversation",
                    id="conversation-title",
                    cls="conversation-title-trigger",
                    role="button",
                    tabindex="0",
                    **{"aria-controls": "conversation-mini-drawer", "aria-expanded": "false"},
                ),
                Div("—", id="conversation-label", cls="conversation-label"),
                cls="brand",
            ),
            Div(
                Label(
                    Input(type="checkbox", id="markdown-toggle", checked=True),
                    Span("MD"),
                    cls="toggle-label",
                ),
                Label(
                    Input(type="checkbox", id="track-edits-toggle"),
                    Span("📝"),
                    cls="toggle-label",
                ),
                Label(
                    Input(type="checkbox", id="line-numbers-toggle"),
                    Span("#"),
                    cls="toggle-label",
                ),
                Span("👎", id="agent-ws", cls="pill warn"),
                Button("Settings", id="conversation-settings", cls="btn"),
                Button("Back", id="conversation-back", cls="btn ghost"),
                Button("×", id="host-close-drawer", cls="btn ghost host-close-btn"),
                cls="drawer-actions",
            ),
            cls="drawer-header",
        ),
        Div(
            conversation_switcher,
            conversation_main,
            cls="conversation-body",
            id="conversation-body",
        ),
        cls="conversation-drawer",
        id="conversation-drawer",
    )
    return HTMLResponse(
        to_xml(
            Html(
            Head(
                Meta(name="viewport", content="width=device-width, initial-scale=1, viewport-fit=cover"),
                Link(rel="manifest", href=f"/manifest.json?v={version}"),
                Meta(name="theme-color", content=CODEX_AGENT_THEME_COLOR),
                Link(rel="icon", type="image/svg+xml", href=_asset(package_root, CODEX_AGENT_ICON_PATH)),
                Link(rel="stylesheet", href=_asset(package_root, "/static/vendor/fonts/jetbrains-mono.css")),
                Link(rel="stylesheet", href=_asset(package_root, "/static/vendor/highlight.js/github-dark.min.css")),
                Link(rel="stylesheet", href=_asset(package_root, "/static/vendor/tribute.css")),
                Link(rel="stylesheet", href=_asset(package_root, "/static/codex_agent.css")),
                Script(src=_asset(package_root, "/static/vendor/highlight.js/highlight.bundle.js")),
                Script(src=_asset(package_root, "/static/vendor/markdown-it/markdown-it.min.js")),
                Script(src=_asset(package_root, "/static/vendor/socket.io/socket.io.min.js")),
                Script(src=_asset(package_root, "/static/vendor/tribute.min.js")),
                Script(NotStr("window.addEventListener('load', () => console.log('socket.io', typeof io));"), defer=True),
                Script(src=_asset(package_root, "/static/modals/settings_modal.js"), defer=True),
                Script(src=_asset(package_root, "/static/modals/settings_schema.js"), defer=True),
                Script(src=_asset(package_root, "/static/modals/cwd_picker.js"), defer=True),
                Script(src=_asset(package_root, "/static/modals/rollout_picker.js"), defer=True),
                Script(src=_asset(package_root, "/static/modals/warning_modal.js"), defer=True),
                Script(src=_asset(package_root, "/static/ui/splash_settings.js"), defer=True),
                Script(src=_asset(package_root, "/static/ui/extension_settings.js"), defer=True),
                Script(src=_asset(package_root, "/static/dist/codex_agent.js"), type="module"),
                Script(NotStr(f"""import {{ initConsoleBridge }} from '{_asset(package_root, "/static/js/console_bridge.js")}';
try {{
  const isProxied = window.location.pathname.startsWith('/api/app/');
  if (isProxied) initConsoleBridge({{ workerLabel: 'codex_agent', uniquePerWindow: true }});
}} catch (e) {{
  console.warn('[console_bridge] init failed', e);
}}"""), type="module"),
            ),
            Body(
                Div(
	                    Main(
	                        # Threads panel intentionally removed for now.
	                        # NOTE: No native browser modals/dialogs/dropdowns allowed.
	                        # All future controls must be DOM-rendered.
                        Section(
                            Div(
                                Div(
	                                Div(
	                                    H1("CodexAS-Extension"),
	                                    Small("Unified Timeline"),
	                                    cls="brand"
	                                ),
	                                Div(
	                                    Button("Project", id="splash-tab-project", cls="btn tiny toggle"),
	                                    Button("All", id="splash-tab-all", cls="btn tiny toggle active"),
                                        Label(
                                            Input(type="checkbox", id="splash-rpc-toggle", checked=True),
                                            Span("Use RPC"),
                                            cls="toggle-label splash-rpc-toggle-label",
                                            title="Disable to keep migrated frontend slices on the legacy /appserver transport for this tab",
                                        ),
                                        Button(
                                            Img(
                                                src=_asset(package_root, "/static/images/green-right-arrow.png"),
                                                alt="",
                                                cls="splash-go-conversation-icon",
                                            ),
                                            id="splash-go-conversation",
                                            cls="btn ghost splash-go-conversation-btn",
                                            title="Go to active conversation",
                                            aria_label="Go to active conversation",
                                            disabled=True,
                                        ),
	                                    cls="splash-tabs",
	                                    id="splash-tabs",
	                                ),
	                                cls="splash-header-main",
	                            ),
	                            Div(
                                    Button("⚙", id="splash-settings", cls="btn ghost", title="Splash settings"),
                                    Button("×", id="host-close-top", cls="btn ghost host-close-btn"),
                                    cls="splash-header-actions",
                                ),
                                cls="splash-header",
                            ),
	                            Div(
                                Div(
	                                P("Pick or create a conversation", cls="muted"),
                                    cls="conversation-list-panel-header",
                                ),
                                Div(
	                                Div(id="conversation-list", cls="conversation-list"),
                                    cls="conversation-list-scroller",
                                ),
                                cls="conversation-list-panel"
                            ),
                            Footer(
                                Button("New Conversation", id="conversation-create", cls="btn primary"),
                                cls="splash-footer"
                            ),
                            cls="splash-view",
                            id="splash-view"
                        ),
                        Div(
                            id="widescreen-resizer",
                            cls="widescreen-resizer",
                            role="separator",
                            aria_orientation="vertical",
                            aria_label="Resize splash and conversation panels",
                            tabindex="0",
                        ),
                        conversation_drawer,
                        cls="grid"
                    ),
                    Div(
                        Div(
                            Div(
                                H3("Conversation Settings"),
                                Button("×", id="settings-close", cls="btn ghost"),
                                cls="settings-header"
                            ),
                            Div(
                                Label(
                                    Span("Agent"),
                                    Div(
                                        Input(type="text", id="settings-agent", placeholder="(select agent)", readonly=True),
                                        Button("▾", id="settings-agent-toggle", cls="btn ghost dropdown-toggle"),
                                        Div(id="settings-agent-options", cls="dropdown-list"),
                                        cls="dropdown-field"
                                    ),
                                    id="settings-agent-row",
                                ),
                                Label(
                                    Span("Conversation Label"),
                                    Input(type="text", id="settings-label", placeholder="label"),
                                ),
                                Label(
                                    Span("Assistant Alias"),
                                    Input(type="text", id="settings-alias", placeholder="assistant"),
                                ),
                                Div(
                                    Label(
                                        Span("CWD"),
                                        Div(
                                            Input(type="text", id="settings-cwd", placeholder="~/project"),
                                            Button("Browse", id="settings-cwd-browse", cls="btn ghost"),
                                            cls="settings-row"
                                        ),
                                    ),
                                    Label(
                                        Span("Approval Policy"),
                                        Div(
                                            Input(type="text", id="settings-approval", placeholder="Use runtime default"),
                                            Button("▾", id="settings-approval-toggle", cls="btn ghost dropdown-toggle"),
                                            Div(id="settings-approval-options", cls="dropdown-list"),
                                            cls="dropdown-field"
                                        ),
                                    ),
                                    Label(
                                        Span("Sandbox Policy"),
                                        Div(
                                            Input(type="text", id="settings-sandbox", placeholder="Use runtime default"),
                                            Button("▾", id="settings-sandbox-toggle", cls="btn ghost dropdown-toggle"),
                                            Div(id="settings-sandbox-options", cls="dropdown-list"),
                                            cls="dropdown-field"
                                        ),
                                    ),
                                    Label(
                                        Span("Model"),
                                        Div(
                                            Input(type="text", id="settings-model", placeholder="model id"),
                                            Button("▾", id="settings-model-toggle", cls="btn ghost dropdown-toggle"),
                                            Div(id="settings-model-options", cls="dropdown-list"),
                                            cls="dropdown-field"
                                        ),
                                    ),
                                    Label(
                                        Span("Effort"),
                                        Div(
                                            Input(type="text", id="settings-effort", placeholder="medium"),
                                            Button("▾", id="settings-effort-toggle", cls="btn ghost dropdown-toggle"),
                                            Div(id="settings-effort-options", cls="dropdown-list"),
                                            cls="dropdown-field"
                                        ),
                                    ),
                                    Label(
                                        Span("Summary"),
                                        Div(
                                            Input(type="text", id="settings-summary", placeholder="concise"),
                                            Button("▾", id="settings-summary-toggle", cls="btn ghost dropdown-toggle"),
                                            Div(id="settings-summary-options", cls="dropdown-list"),
                                            cls="dropdown-field"
                                        ),
                                    ),
                                    Label(
                                        Span("Developer Instructions"),
                                        Textarea(
                                            id="settings-developer-instructions",
                                            cls="settings-textarea",
                                            placeholder="Additional runtime instructions applied when the agent starts or resumes a thread",
                                        ),
                                    ),
                                    id="settings-codex-fields",
                                ),
                                Div(id="settings-extension-fields"),
                                Label(
                                    Span("Command Output Lines"),
                                    Input(type="number", id="settings-command-lines", placeholder="20", value="20", min="1", max="500"),
                                ),
                                Label(
                                    Span("Wrap view cards"),
                                    Input(type="checkbox", id="settings-view-wrap", checked=False),
                                    cls="settings-checkbox-row"
                                ),
                                Label(
                                    Span("Render Markdown"),
                                    Input(type="checkbox", id="settings-markdown", checked=True),
                                    cls="settings-checkbox-row"
                                ),
                                Label(
                                    Span("Syntax highlighting (diffs & terminal)"),
                                    Input(type="checkbox", id="settings-diff-syntax", checked=False),
                                    cls="settings-checkbox-row"
                                ),
                                Label(
                                    Span("Semantic shell ribbon (Tree-sitter)"),
                                    Input(type="checkbox", id="settings-semantic-shell-ribbon", checked=False),
                                    cls="settings-checkbox-row"
                                ),
                                Label(
                                    Span("TE2 MCP Integration"),
                                    Input(type="checkbox", id="settings-te2-mcp-integration", checked=False),
                                    cls="settings-checkbox-row"
                                ),
                                cls="settings-body"
                            ),
                            Div(
                                Button("Cancel", id="settings-cancel", cls="btn ghost"),
                                Button("Save", id="settings-save", cls="btn primary"),
                                cls="settings-footer"
                            ),
                            cls="settings-dialog"
                        ),
                        cls="settings-overlay hidden",
                        id="settings-modal"
                    ),
                    Div(
                        Div(
                            Div(
                                H3("Splash Settings"),
                                Button("×", id="splash-settings-close", cls="btn ghost"),
                                cls="settings-header"
                            ),
                            Div(
                                Label(
                                    Span("User Name"),
                                    Input(type="text", id="splash-settings-user-name", placeholder="User"),
                                ),
                                Label(
                                    Span("TE2 MCP Integration"),
                                    Input(type="checkbox", id="splash-settings-te2-mcp-integration", checked=False),
                                    cls="settings-checkbox-row"
                                ),
                                Div(
                                    H3("Extensions", cls="settings-subheading"),
                                    Small("Enable, disable, or install extension dependencies."),
                                    Div(id="splash-settings-extensions", cls="extension-settings-list"),
                                    cls="extension-settings-section"
                                ),
                                cls="settings-body"
                            ),
                            Div(
                                Button("Cancel", id="splash-settings-cancel", cls="btn ghost"),
                                Button("Save", id="splash-settings-save", cls="btn primary"),
                                cls="settings-footer"
                            ),
                            cls="settings-dialog"
                        ),
                        cls="settings-overlay hidden",
                        id="splash-settings-modal"
                    ),
                    Div(
                        Div(
                            Div(
                                H3("Pick CWD", id="picker-title"),
                                Button("×", id="picker-close", cls="btn ghost"),
                                cls="picker-header"
                            ),
                            Div(
                                Div(id="picker-path", cls="picker-path"),
                                Div(id="picker-list", cls="picker-list"),
                                cls="picker-body"
                            ),
                            Div(
                                Div(
                                    Input(type="text", id="picker-filter", placeholder="filter (regex)..."),
                                    cls="picker-footer-left"
                                ),
                                Div(
                                    Button("Up", id="picker-up", cls="btn ghost"),
                                    Button("Select Current", id="picker-select", cls="btn primary"),
                                    cls="picker-footer-right"
                                ),
                                cls="picker-footer"
                            ),
                            cls="picker-dialog"
                        ),
                        cls="picker-overlay hidden",
                        id="cwd-picker"
                    ),
                    Div(
                        Div(
                            Div(
                                H3("Pick Rollout"),
                                Button("×", id="rollout-close", cls="btn ghost"),
                                cls="picker-header"
                            ),
                            Div(
                                Div(id="rollout-list", cls="picker-list"),
                                cls="picker-body"
                            ),
                            cls="picker-dialog"
                        ),
                        cls="picker-overlay hidden",
                        id="rollout-picker"
                    ),
                    Div(
                        Div(
                            Div(
                                H3("Resume Session"),
                                Button("×", id="session-picker-close", cls="btn ghost"),
                                cls="picker-header"
                            ),
                            Div(
                                Div(id="session-picker-list", cls="picker-list"),
                                cls="picker-body"
                            ),
                            cls="picker-dialog"
                        ),
                        cls="picker-overlay hidden",
                        id="session-picker"
                    ),
                    Div(
                        Div(
                            Div(
                                H3("Confirm"),
                                Button("×", id="warning-close", cls="btn ghost"),
                                cls="settings-header"
                            ),
                            Div(
                                P("Are you sure?", id="warning-body"),
                                cls="settings-body"
                            ),
                            Div(
                                Button("Cancel", id="warning-cancel", cls="btn ghost"),
                                Button("Continue", id="warning-confirm", cls="btn danger"),
                                cls="settings-footer"
                            ),
                            cls="settings-dialog"
                        ),
                        cls="settings-overlay hidden",
                        id="warning-modal"
                    ),
                    Div(
                        Div(
                            Div(
                                H3("Plan"),
                                Button("×", id="plan-close", cls="btn ghost"),
                                cls="settings-header"
                            ),
                            Div(
                                Div(id="plan-body", cls="markdown-body plan-modal-body"),
                                cls="settings-body"
                            ),
                            Div(
                                Button("Close", id="plan-dismiss", cls="btn primary"),
                                cls="settings-footer"
                            ),
                            cls="settings-dialog"
                        ),
                        cls="settings-overlay hidden",
                        id="plan-modal"
                    ),
                    cls="appshell"
                )
                )
            )
            )
        )
