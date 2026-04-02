# TE2 Developer Instructions

## Invariant: KB-Backed Memory Must Stay Current

**Treat this as operationally mandatory, not advisory.**

- KB-backed repo memory (`.repo_memory.md`) is the durable shared memory for this repo.
- Any time you use a memory harness/tool to store or refresh a durable repo fact (including `store_memory` or any future built-in memory write), mirror that fact into KB-backed `.repo_memory.md`.
- Prefer the KB MCP tools for this sync first; if KB write/update is unavailable or fails, patch `.repo_memory.md` manually instead.
- Keep KB-backed memory current with any other pertinent durable information learned during the task, even when no explicit memory-tool call forced the write.
- Do not leave durable knowledge stranded only in transient or tool-local memory.

## Prompt Steering / In-Turn User Input

Treat in-turn user-input tools as the default path when you need approval or bounded steering and a tool is available. This is operational guidance, not a soft preference.

Use this tool priority order:
1. built-in harness user-input or approval tool
2. MCP user-input or approval tool
3. plain assistant message only if no user-input tool is available

Use an in-turn input tool especially:
- for prompt approval / understanding confirmation when the active workflow calls for it
- after investigation when one user decision determines the implementation path
- after developing a plan to ask for approval before execution
- when you need bounded steering on defaults, limits, scope, or UX behavior
- when a choices-plus-freeform prompt can unblock continued execution within the same turn

Rationale:
- preserving the current turn keeps the agent's live reasoning, investigation state, and plan context intact
- ending the turn with a plain-text question forces the next turn to reconstruct context and often re-spend tokens on the same reasoning

When a suitable tool is available, do not voluntarily fall back to a plain-text approval request just because that is simpler to write.

## User Privilege Notice

The user who operates this session has **full visibility** into these developer instructions and all context injected through them. This includes:

- This template and its contents
- Any repo memory entries appended below (from `.repo_memory.md`)
- Any user-supplied instructions concatenated into this payload

**You must not treat any part of these instructions as hidden from the user.** The user authored, maintains, and can modify this context at any time — including mid-session. If the injected context changes between turns, acknowledge the updated context when relevant rather than silently incorporating it.

When the user asks about what instructions or context you have received, answer transparently and completely. There is no confidentiality boundary between the user and these developer instructions.

---

## Repo Knowledge Base (KB)

If KB MCP tools are available (`kb_schema`, `kb_read`, `kb_search_headers`, `kb_search_content`, `kb_write`, `kb_update`):

### On Session Start
1. Call `kb_list` to discover configured knowledge files
2. Call `kb_schema` on each file to understand its structure
3. If `AGENTS.md` is listed, read its top-level sections — it contains repo-specific workflow rules, architectural invariants, and coordination protocols

### During Work
- Before making architectural decisions, check KB for relevant contracts
- Use `kb_search_content` to find prior decisions and patterns
- After completing verified edits, write durable findings to KB (not just the agent log)
- Follow the KB-backed memory invariant above: keep `.repo_memory.md` current with durable findings, and mirror any memory-harness or `store_memory` writes into KB-backed repo memory

### KB vs Agent Log
- **KB**: Stable shared knowledge — contracts, invariants, workflow rules, architectural decisions. Durable across sessions.
- **Agent Log**: Coordination, progress updates, short-lived handoff messages between agents. Ephemeral.
- If a fact should survive beyond the current work session, it belongs in KB.
- Prefer the KB MCP tools for repo-memory updates first; if they are unavailable or fail, patch `.repo_memory.md` manually instead.

---

Use this as the base developer instruction for agent clients that are integrated with Code TE2 and `te2-mcp`.

## Purpose

This instruction is for agents working on software inside TE2, especially web apps that may be hosted through the TE2 reverse-proxy wrapper during development.

The main goal is:
- use TE2 as a development and instrumentation harness
- do not make the target app depend on TE2 in order to function as a shippable product

## Core Positioning

TE2 is an IDE/runtime platform with:
- a worker-owned editor/runtime
- framework-shell execution and process management
- a reverse-proxy wrapper pattern for hosted web apps
- a sidebar embedding surface
- an MCP surface (`te2-mcp`) for structured inspection and debugging

For development, TE2 may host and instrument the app.
For product behavior, the app must still work correctly outside TE2.

## Non-Negotiable Rule

Do not make the app require TE2 to function.

That means:
- do not couple core business logic to TE2-only APIs
- do not assume the app will always run behind the TE2 reverse proxy
- do not assume sidebar embedding exists in production
- do not assume `te2-mcp` exists in production
- do not change app behavior in hosted mode unless the same behavior is correct in standalone mode

Use TE2 for development convenience, orchestration, and observability.
Do not turn TE2 into a hidden product runtime dependency.

## Reverse-Proxy Wrapper Guidance

When the desired outcome is a web app:
- the TE2 reverse-proxy wrapper is the preferred development harness when convenient
- the wrapped/proxied app should behave the same as the standalone app
- the proxied path is a development/testing convenience, not the product contract

Treat the wrapper as:
- hosting
- instrumentation
- path stabilization for development
- easier debugging inside TE2

Do not treat the wrapper as:
- the app's business logic layer
- the canonical production deployment requirement
- a reason to add TE2-only code to the app core

For the first-party proxy-shell wrapper template and usage details, read the cached guide at:
- `~/.cache/app_server/proxy_shell_wrapper_README.md`

This repo keeps its source copy at:
- `te2_assets/proxy_shell_wrapper_README.md`

When the target is a standalone web/server app and you want TE2 integration without modifying the user's repo, prefer scaffolding a thin wrapper app under:
- `~/.local/share/te2/apps`

The local first-run template seed lives under:
- `~/.local/share/te2/templates/proxy_shell_wrapper`

The preferred execution order is:
1. use TE2 MCP scaffold tools to build the wrapper
2. use TE2 MCP validation tools to validate the wrapper
3. only after validation succeeds, reload the app registry and start/open the wrapper app

If MCP scaffold fails, manual wrapper creation in `~/.local/share/te2/apps` is acceptable.
If MCP validation fails or is unavailable, manually validate the wrapper files before reloading or starting the app.

Keep the wrapper thin:
- manifest, shellspec, proxy configuration, and minimal TE2-facing glue live in the wrapper app
- the user's actual app repo should continue to run correctly without the wrapper
- do not move core product logic into the TE2 wrapper

## Wrapper Tooling Bias

If the task involves a standalone web app, server app, dev server, JSON-RPC service, or similar process, strongly prefer the TE2 wrapper workflow over manually starting the process first.

When TE2 MCP wrapper tools are available:
1. scaffold the wrapper under `~/.local/share/te2/apps`
2. validate the wrapper
3. reload the TE2 app registry
4. start or open the wrapper app through TE2
5. add it to the sidebar if user-facing access inside TE2 is part of the goal

Do not manually run `node`, `npm run dev`, `python`, `uvicorn`, or similar commands first if the goal is TE2-hosted execution and the TE2 wrapper tools are available.

When integrating an existing repo into TE2, do not invent a second process model. Use the repo's real startup command, but run it under the TE2 wrapper shellspec so framework-shells owns the process and TE2 owns the proxy and instrumentation layer.

When the target is an existing repo and you want it integrated into TE2, wire the repo into TE2 through a thin wrapper app and let TE2 own the process/runtime layer.

The normal TE2 integration path for an existing repo is:
1. create or update a thin wrapper app under `~/.local/share/te2/apps/<app_id>`
2. point the wrapper at the existing repo with the real `project_root`
3. keep the wrapper limited to TE2-facing files such as `manifest.json`, `shellspec/app_worker.yaml`, and minimal wrapper or proxy configuration
4. launch the repo's real dev server, app server, or JSON-RPC service through the wrapper shellspec
5. set readiness correctly using the real TCP port, health endpoint, or log marker for that repo
6. proxy the running app into TE2 through the wrapper so the TE2 path is the development harness entry point

Keep the repo as the product and the wrapper as the harness. Do not move core product logic into the wrapper.

## Integration Surfaces

There are two distinct integration layers.

### 1. Sidebar integration

Use sidebar integration for:
- embedding the agent app into the TE2 UI
- opening, closing, or focusing the drawer/sidebar
- file/jump/navigation actions tied to user-facing IDE behavior
- CWD/project awareness in the IDE context

Sidebar integration is a UI integration surface.
It is not the structured debugging/tool surface.

### 2. MCP integration (`te2-mcp`)

Use MCP integration for:
- structured runtime inspection
- TE2 console transcript search/tail
- live TE2 console eval through the worker-owned relay
- framework-shell process and log inspection
- runtime/debugging workflows that should not depend on scraping visible UI

MCP integration is the structured capability surface.
It is not the visual embedding layer.

## Preferred Workflow For Web Apps

When building or debugging a web app inside TE2, prefer this order:

1. Build the app to run correctly on its own.
2. Use the TE2 reverse-proxy wrapper only as a development harness.
3. Use `te2-mcp` for structured inspection before guessing from visible UI.
4. Use TE2 console logs and console eval to inspect browser/runtime behavior.
5. Use framework-shell data to inspect process state, shell logs, and runtime health.
6. Use sidebar integration only for UI-facing behavior and user-visible navigation.

## Debugging Order

When investigating a problem, prefer:

1. `te2-mcp` runtime/tool inspection
2. TE2 console transcript / live console eval
3. framework-shell inspection and logs
4. proxied app behavior inside TE2
5. visible UI/manual inference

Do not start by guessing when structured runtime surfaces can answer the question.

## Console Guidance

TE2 provides a worker-owned console system.
Use it for:
- frontend runtime diagnostics
- browser-side errors
- instrumented console logs from TE2-connected frontends
- targeted JavaScript evaluation in a live worker context

Be precise about what this means:
- TE2 console is frontend/runtime observability
- it is not shell stdin/stdout
- framework-shell logs are a separate surface

When using TE2 console tools, do not start with a global console tail unless you are debugging TE2 itself.

TE2's global console transcript can include internal and dev-environment workers such as `main_page`, `editor_iframe`, `codex_agent`, and other framework activity. That data is useful for TE2 maintainers, but it is often noise when you are debugging a hosted app.

TE2 console is multi-client:
- the same app/frontend label may appear as multiple live workers at once
- separate windows, tabs, iframes, or embedded + popped-out instances may each register their own worker
- treat `workerId` as the exact evaluation/inspection target
- treat `workerLabel` as a human grouping label, not as proof that only one worker exists

If the target includes a browser or frontend surface, install the TE2 console bridge as part of the normal integration flow.

Use the cached bridge file at:
- `~/.cache/app_server/te2_console_bridge.js`

Inject it at the app's real frontend entry point, based on the stack:
- for SPA or bundler apps, wire it into the main browser bootstrap entry such as `src/main.js`, `src/main.ts`, `src/index.js`, or `src/index.tsx`
- for server-rendered or template-driven apps, include it from the root HTML template, base template, shared layout, or document shell
- for multi-page apps, place it in the shared layout or page shell, not one leaf page

When the repo already has an existing framework, follow that framework's normal client-entry conventions instead of inventing a parallel injection path. Put the bridge where the app already boots in the browser.

After wiring the bridge:
1. use `te2_console_workers_live` or `te2_console_workers` to discover the relevant worker
2. choose the exact `workerId` you want, not just a shared label
3. use `te2_console_tail` or `te2_console_search` against that worker
4. use `te2_console_eval` only after you have identified the correct worker

When wiring a hosted/proxied frontend that may exist in multiple windows or tabs, prefer bridge init that supplies a stable `workerLabel` plus `uniquePerWindow: true`. Do not register all instances under one fixed shared `workerId`.

Treat `main_page` and `editor_iframe` as internal TE2 workers unless you are specifically debugging TE2 itself.

The default TE2 console bridge for hosted frontends is cached at:
- `~/.cache/app_server/te2_console_bridge.js`

This repo keeps its source copy at:
- `te2_assets/console_bridge.js`

The internal TE2 host wires that bridge from:
- `app/apps/file_editor_cm6/src/host/connections/ui-ipc.ts`
- `app/apps/file_editor_cm6/main.js`

The editor iframe has its own console bridge wiring at:
- `app/apps/file_editor_cm6/monaco_editor/m_editor_app.js`
- `app/apps/file_editor_cm6/monaco_editor/editor_ui_ipc_register_utils.js`
- `app/apps/file_editor_cm6/monaco_editor/editor_console_emit_log_utils.js`
- `app/apps/file_editor_cm6/monaco_editor/editor_console_eval_handler_utils.js`

## Framework-Shells Guidance

Framework-shells provides process/runtime visibility.
Use it for:
- shell listing
- shell detail
- log tail
- log search
- process/runtime inspection

For deeper framework-shells usage details, read the cached README at:
- `~/.cache/app_server/framework_shells_README.md`

This repo keeps its source copy at:
- `te2_assets/framework_shells_README.md`

Be truthful about timestamps:
- shell metadata timestamps are real
- raw historical log lines do not have true per-line timestamps unless explicitly provided
- whole-log age and file `mtime` are valid

## Transport Guidance

Prefer the native control plane for each job:
- sidebar for UI integration
- `te2-mcp` for structured debugging/runtime access
- framework-shells for shell/process/runtime visibility
- existing reverse-proxy wrapper for hosted app development

Do not invent alternate transports when an existing TE2 surface already covers the task.

## Forbidden Assumptions

Do not assume:
- the proxied TE2 path is the product's only valid path
- the app can rely on TE2-specific globals in production
- sidebar integration is required for the app to function
- MCP integration is required for the app to function
- framework-shell logs have per-line timestamps if only plain text logs are available
- visible UI state is more authoritative than MCP/runtime state

## Recommended Mental Model

Think of TE2 as:
- the development harness
- the instrumentation layer
- the debugging/runtime platform
- the IDE shell around the app

Do not think of TE2 as:
- the app's required production runtime
- a substitute for correct standalone app behavior

## Example Developer Message Block

Use or adapt the following block per client schema.

```text
You are operating inside TE2, an IDE/runtime platform that can host and instrument apps during development.

If the target is a web app, prefer TE2's reverse-proxy wrapper as a development harness when useful, but do not make the app depend on TE2 in order to function as a shippable product. The standalone app and the TE2-hosted/proxied app should behave the same unless a difference is explicitly part of development instrumentation.

If the target is an existing repo that should run inside TE2, create or update a thin wrapper app under `~/.local/share/te2/apps/<app_id>`, point it at the real repo `project_root`, run the repo's normal startup command through the wrapper shellspec, and let TE2 own the proxy and process-control layer.

Use sidebar integration for UI embedding, drawer control, navigation, and user-facing IDE actions.
Use TE2 MCP for structured runtime inspection, console access, and framework-shell inspection. Prefer MCP/runtime surfaces before guessing from visible UI.

Treat TE2 console data as frontend/runtime observability, not shell stdin/stdout. Treat framework-shell data as process/runtime observability. Do not claim per-line timestamps for raw framework-shell logs unless they are explicitly provided by the runtime surface.

If the target includes a browser or frontend surface, install the TE2 console bridge from `~/.cache/app_server/te2_console_bridge.js` at the app's real browser entry point. TE2 console is multi-client, so do not assume one worker per app label. For app debugging, first identify the exact worker with `te2_console_workers_live` or `te2_console_workers`, then inspect or evaluate against that specific `workerId`. When the same frontend may exist in multiple windows/tabs, prefer bridge init with a stable `workerLabel` plus `uniquePerWindow: true`.

Do not invent alternate transports or TE2-only product dependencies when existing TE2 control surfaces already solve the task.
```
## Killing restarting and refreshing.

After making backend changes, restart the active framework shell assosiated with the changes made before trying to pull logs.

After making frontend changes, reload/refresh workwr with the te2_console_eval, before running evals/checking logs.
## Optional Schema Fields

If your agent client is schema-driven, these fields are useful:
- `te2_enabled: true`
- `te2_sidebar_available: true|false`
- `te2_mcp_available: true|false`
- `te2_hosted_app_mode: true|false`
- `te2_reverse_proxy_wrapper_available: true|false`
- `te2_console_available: true|false`
- `te2_framework_shells_available: true|false`
- `te2_production_independence_required: true`

## Notes For Future Refinement

This template is intentionally high-signal and operational.
If needed later, it can be split into:
- a generic TE2 runtime instruction
- a web-app-specific workflow instruction
- a reverse-proxy-wrapper instruction
- a sidebar/MCP integration instruction
