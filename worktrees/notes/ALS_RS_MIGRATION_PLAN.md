# ALS-RS migration plan

## Purpose

`als-rs` is a non-introspective Rust-server branch for Agent Log Server. The
goal is to use the existing TE2/FWS/ALS harness the same way a downstream app
would use it: as an external development host for a separate app root, cache
root, port, process tree, and TE2 proxy wrapper.

This branch keeps the current frontend checkout/contract intact while moving the
main server/runtime logic from Python/FastAPI/python-socketio to
Rust/Axum/socketioxide.

## Non-negotiable invariants

1. **Separate app identity**
   - This branch is not the live Python harness instance.
   - It must run as its own app with its own TE2 proxy wrapper, process tree,
     port, cache root, data root, and install identity.

2. **Separate roots**
   - Data root: `${XDG_DATA_HOME:-~/.local/share}/als-rs`
   - Cache root: `${XDG_CACHE_HOME:-~/.cache}/als-rs`
   - Config root: same as the data root unless a later explicit migration tool
     chooses to split it.
    - Runtime state, conversations, logs, ALS-RS-owned secrets, extension install
      metadata, pid files, socket paths, and ALS-RS-owned framework-shell
      fingerprints should derive from these roots.
    - When ALS-RS is launched inside an existing framework-shell environment, the
      Python extension adapter may intentionally inherit that FWS control
      environment so extension-owned provider processes remain observable and
      controllable through the active FWS manager.
   - No normal runtime path should read or write `~/.cache/app_server` or the
     Python harness data roots, except explicit import/migration tools.

3. **Python remains the install/bootstrap surface**
   - `pip install git+...@als-rs` should still install a Python package.
   - The console entrypoint should be a thin Python bootstrap, not the server.
   - Python bootstrap responsibilities:
     - resolve and create ALS-RS roots
     - set `ALS_RS_*` environment variables
     - locate/build/launch the Rust server binary
     - launch/monitor Python extension adapter processes
     - forward signals and exit status
   - Python bootstrap non-responsibilities:
     - no FastAPI app
     - no ASGI server
     - no Python HTTP routing
     - no Python Socket.IO server

4. **Rust owns networking**
   - Main HTTP server: Axum.
   - Main Socket.IO server: socketioxide.
   - Rust owns static serving, HTTP API routes, Socket.IO namespaces, JSON-RPC
     namespaces, live fanout, health/readiness, and process lifecycle.
   - Python networking should be removed from the ALS-RS runtime path.

5. **Extensions stay behind an adapter boundary**
   - Existing Python extensions/FWS integration remain Python-owned initially.
   - Rust must not import extension internals or parse provider-specific event
     streams.
   - Adapter output must be normalized into Rust-owned DTOs before Rust sees it.
   - The adapter protocol should be DTO-first and transport-disposable. JSON-RPC
     is the preferred shape to debate because requests, responses, and
     notifications can each map directly onto versioned DTOs while leaving bytes,
     strings, stdio, Unix sockets, or PyO3/shared-memory as transport choices.

6. **Frontend checkout is invariant**
   - The frontend remains identical to the current checkout unless a change is
     explicitly required to preserve the same contract against the Rust runtime.
   - Preserve the current routes/namespaces/contracts before redesigning them.
   - Contract parity is more important than backend elegance in early slices.

7. **Transcript system is invariant**
   - Keep the existing transcript shape/system. It works and should be ported,
     not redesigned, during the initial Rust runtime migration.
   - Rust may own the implementation, but the persisted/replay contract should
     remain compatible with the current frontend and extension router behavior.

## Target architecture

```text
TE2 sidebar / browser
        |
        | proxied HTTP + Socket.IO
        v
TE2 proxy-shell wrapper: als-rs
        |
        | launches command in ~/test-projects/als_rs
        v
Python bootstrap entrypoint
        |
        | sets ALS_RS_* env, starts subprocesses
        v
Rust server process
  - axum HTTP/static/API
  - socketioxide namespaces
  - isolated data/cache roots
  - transcript/conversation persistence
  - live event fanout
        |
        | JSON-RPC adapter protocol over disposable transport
        v
Python extension adapter process
  - loads selected Python extension modules
  - owns framework-shell integration
  - normalizes provider events into DTOs
        |
        v
Existing extension runtime/provider process
```

## Runtime and environment particulars

Recommended runtime environment variables:

| Variable | Meaning |
| --- | --- |
| `ALS_RS_DATA_DIR` | Durable app data root. Default: `${XDG_DATA_HOME:-~/.local/share}/als-rs`. |
| `ALS_RS_CACHE_DIR` | Cache/runtime root. Default: `${XDG_CACHE_HOME:-~/.cache}/als-rs`. |
| `ALS_RS_CONFIG_DIR` | Optional override; default behavior is to use `ALS_RS_DATA_DIR`. |
| `ALS_RS_HOST` | Bind host. Default: `127.0.0.1`. |
| `ALS_RS_PORT` | Standalone HTTP port. Recommended default: `12459`. |
| `ALS_RS_STATIC_DIR` | Static/frontend asset root, normally package-managed. |
| `ALS_RS_EXTENSIONS_DIR` | Builtin extension registry root. Default: repo-local `extensions/` in development. |
| `ALS_RS_PYTHON_BIN` | Python executable used by Rust to launch adapter subprocesses. |
| `ALS_RS_EXTENSION_ADAPTER` | Reserved adapter executable/module override; current Rust path launches the generic adapter module directly. |
| `ALS_RS_LOG_LEVEL` | Rust/Python adapter log verbosity. |

Recommended local paths:

```text
~/.local/share/als-rs/
  conversations/
  extensions/
  registry/
  secrets/

~/.cache/als-rs/
  runtime/
  framework_shells/
  logs/
  sockets/
  tmp/
```

Runtime rules:

- Use `TMPDIR` or an ALS-RS cache subdirectory for temp files; do not hardcode
  `/tmp`.
- ALS-RS-owned secrets should be generated under the ALS-RS data/cache roots, not
  reused from the Python harness. Framework-shell control secrets are different:
  when ALS-RS is launched inside FWS, adapter child processes must preserve the
  parent `FRAMEWORK_SHELLS_*` environment so existing extension-owned
  framework-shell process management and observability keep working.
- Health endpoint should be Rust-owned, for example `GET /api/health`.
- TE2 wrapper readiness should use the ALS-RS port and health route.

## Python package and bootstrap particulars

Initial package shape can stay setuptools-based while adding Rust build support.
Package and console script identity:

```toml
[project]
name = "als-rs"

[project.scripts]
als-rs = "agent_log_server_rs.bootstrap:main"
als-rs-extension-adapter = "agent_log_server_rs.adapters.extension_adapter:main"
```

The installed console script must not collide with the current `codex-agent`
command. The older `als-rs-copilot-adapter` script remains as a focused pilot
entrypoint, but the Rust server now uses the generic extension adapter module.

Bootstrap responsibilities:

1. Parse CLI options/env for host, port, root overrides, foreground mode, and
   adapter selection.
2. Resolve isolated roots and export `ALS_RS_*`.
3. Ensure required directories exist.
4. Locate the Rust server source/binary:
   - explicit `ALS_RS_SERVER_BIN` / `--server-bin` when provided
   - source-checkout `rust/Cargo.toml` during development
   - packaged `agent_log_server_rs/rust/Cargo.toml` inside pip wheels
   - development binary under the active Cargo target dir when present
   - otherwise `cargo run -p als-server`
   Packaged wheel launches set `CARGO_TARGET_DIR` under the ALS-RS cache root so
   cargo does not write build output into site-packages.
   Bootstrap also exports `ALS_RS_EXTENSIONS_DIR` to the packaged/source
   builtin `extensions/` root so Rust discovery does not rely on Cargo's
   rust-workspace-relative default.
5. Launch the Rust server as a subprocess.
6. Launch extension adapter subprocesses only when requested by Rust or by
   startup configuration.
7. Forward SIGINT/SIGTERM to child processes.
8. Exit with the Rust server exit code unless bootstrap startup fails first.

Bootstrap should stay intentionally thin. If logic starts looking like routing,
state management, or event translation, it belongs in Rust or the adapter.

## Rust workspace particulars

Recommended workspace shape:

```text
rust/
  Cargo.toml
  crates/
    als-server/
      src/main.rs
      src/http.rs
      src/socketio.rs
      src/state.rs
      src/static_assets.rs
    als-dto/
      src/lib.rs
    als-jsonrpc/
      src/lib.rs
    als-adapter-protocol/
      src/lib.rs
    ferrous_framework/
      src/lib.rs
```

Suggested crate roles:

- `als-server`
  - owns Axum server, socketioxide server, static serving, app state, process
    supervision, persistence, and TE2-facing health/readiness.
- `als-dto`
  - owns JSON-serializable frontend/backend DTOs for conversations, transcript
    entries, live events, settings, runtime options, approvals, and errors.
- `als-jsonrpc`
  - owns internal JSON-RPC 2.0 envelope helpers, request/response correlation,
    typed errors, and dispatch utilities shared by browser-facing RPC and the
    extension adapter boundary.
- `als-adapter-protocol`
  - owns request/response/event envelopes exchanged with Python extension
    adapter processes.
- `ferrous_framework`
  - owns the optional PyO3/framework-shell native-pipe adapter transport so
    observed adapter read/write plumbing stays outside `als-server`.

Early Rust server surfaces to mirror:

- `GET /`
- static asset routes
- `GET /manifest.json`
- `GET /api/health`
- `/rpc/conversations` Socket.IO namespace
- `/rpc/settings` Socket.IO namespace
- `/rpc/ui` Socket.IO namespace
- live conversation notification fanout

## Execution strategy

Keep the existing Python directory as the reference implementation and build the
Rust source beside it under `rust/`. Do not rewrite Python files in place while
the Rust runtime is being born. The Python implementation is the parity oracle
until each Rust slice has its own tests and live frontend validation.

The migration should use a hybrid execution model:

1. **Hand-build the Rust spine first.**
   - Add the Rust workspace and crate layout.
   - Get Axum and socketioxide compiling in `als-server`.
   - Serve health/static routes.
   - Wire isolated ALS-RS roots.
   - Add the JSON-RPC and DTO crates.
   - Do not add real extension behavior in this first slice.

2. **Port contracts before business logic.**
   - Treat `agent_log_server/conversations_rpc_contract.py`,
     `agent_log_server/settings_ui_rpc_contract.py`, and the frontend
     `static/js/codex_agent/rpc/**/contract.ts` files as the contract sources.
   - Define Rust DTOs and JSON-RPC method/notification names before wiring the
     handlers that execute them.
   - The existing browser-facing contracts stay intact; JSON-RPC at the adapter
     boundary must not become a frontend redesign.

3. **Use sub-agents only after the spine exists.**
   - Sub-agents should work against the shared Rust layout and produce bounded
     source slices plus parity notes.
   - They should not invent architecture, crate layout, transport semantics, or
     adapter method names.
   - The extension adapter boundary should stay with the main agent/user loop
     until the JSON-RPC DTO shape is settled.

4. **Avoid a big-bang generated rewrite.**
   - Do not ask one agent to translate the whole server.
   - Generate or port one contract/module boundary at a time.
   - Keep every slice compiling and testable.

Recommended sub-agent lanes after the Rust spine exists:

| Lane | Source references | Output |
| --- | --- | --- |
| Contract DTO lane | `conversations_rpc_contract.py`, `settings_ui_rpc_contract.py`, frontend `rpc/**/contract.ts` | Rust DTOs, method constants, notification constants, parity notes |
| Conversation store lane | `conversation_store.py`, transcript/replay call sites | Rust conversation index/meta/transcript persistence under ALS-RS roots |
| Socket.IO namespace lane | `appserver_socketio.py`, frontend socket/RPC clients | socketioxide namespace registration and JSON-RPC dispatch skeletons |
| Static/page lane | `index.py`, `web_pages.py`, `build.mjs`, static asset paths | Axum static/page/manifest route parity without Python ASGI |
| Settings/UI lane | `settings_ui_rpc_contract.py`, schema/settings frontend code | Settings and UI RPC methods that do not require provider adapter behavior |
| Adapter scout lane | `extensions/copilot_sdk/**`, loader contracts | Copilot adapter feasibility map and DTO requirements, without Rust architecture edits |

The safest first implementation order:

1. `rust/` workspace and `als-server` health endpoint.
2. Root/config resolver for ALS-RS data/cache/config directories.
3. Static frontend hosting from the current checkout.
4. JSON-RPC envelope and shared DTO crate.
5. Conversation list/meta/transcript persistence under ALS-RS roots.
6. Minimal `/rpc/conversations`, `/rpc/settings`, and `/rpc/ui` Socket.IO
   namespace skeletons needed for frontend boot.
7. Python adapter protocol draft and one `copilot-sdk` pilot path.

## Extension adapter particulars

There is no built-in Codex runtime left to fall back to. ALS-RS cannot be a real
agent app without at least one extension path, so the extension shape is a core
architecture decision rather than a follow-up feature.

The first adapter milestone should pick one extension and make it run behind a
small standalone adapter surface.

Selected first protocol/transport: newline-delimited JSON-RPC 2.0 over adapter
stdin/stdout. The protocol shape remains transport-disposable: stdio can be
replaced later by Unix sockets, framework-shell pipes, PyO3/shared-memory, or
another byte channel without changing DTO method/event names.

Current observed transport direction: keep the JSON-RPC DTO shape, but move the
adapter process under framework-shell native pipes through the optional
`ferrous_framework` crate. Rust talks to that crate through in-process PyO3
calls, while `framework_shells` owns/tees adapter stdout/stderr for observability.
The adapter JSON-RPC stream must never be forwarded through the Rust CLI stdout.
Default ALS-RS builds retain the direct child stdin/stdout fallback. The PyO3
path is enabled with `als-server --features ferrous-framework-pyo3`; in this
Termux environment, build that feature with the non-`t` Python 3.13 interpreter
instead of the ambient Python 3.14t PyO3 config.

The adapter shellspec is:

```text
agent_log_server_rs/shellspec/extension_adapter.yaml
```

For source-checkout launches, the Python bootstrap enables
`ferrous-framework-pyo3` automatically when framework-shell context is present,
clears ambient `PYO3_CONFIG_FILE`, and sets `PYO3_PYTHON` to the bootstrap
interpreter so the build targets Python 3.13.

Reasons to prefer JSON-RPC shape:

- request/response correlation is standard and already maps to DTOs
- notifications are a natural fit for live events
- each method/notification name can correspond to a concrete versioned DTO
- stdio bytes, stdio strings, Unix sockets, framework-shell pipes, or future
  PyO3/shared-memory can all be treated as replaceable transports underneath the
  same protocol
- the protocol can be tested with recorded frames independently of transport

Durable generic adapter launch shape:

```bash
python -m agent_log_server_rs.adapters.extension_adapter
```

Use a real module/entrypoint for branch architecture. Python heredoc launches
are acceptable for local spikes, but not as the durable adapter surface.

### Adapter protocol envelope

Requests from Rust:

```json
{"jsonrpc":"2.0","id":"1","method":"extension.initialize","params":{"extension_id":"copilot-sdk","extensions_dir":"/repo/extensions","cwd":"/repo","data_dir":"...","cache_dir":"..."}}
{"jsonrpc":"2.0","id":"2","method":"extension.list_models","params":{"extension_id":"copilot-sdk"}}
{"jsonrpc":"2.0","id":"3","method":"conversation.start","params":{"extension_id":"copilot-sdk","conversation_id":"...","settings":{}}}
{"jsonrpc":"2.0","id":"4","method":"conversation.send","params":{"extension_id":"copilot-sdk","conversation_id":"...","text":"hello"}}
{"jsonrpc":"2.0","id":"5","method":"conversation.resume","params":{"extension_id":"copilot-sdk","conversation_id":"...","provider_session_id":"..."}}
{"jsonrpc":"2.0","id":"6","method":"conversation.compact","params":{"conversation_id":"..."}}
{"jsonrpc":"2.0","id":"7","method":"approval.respond","params":{"extension_id":"copilot-sdk","conversation_id":"...","request_id":"...","decision":"accept","result":{"decision":"accept"}}}
```

Responses from adapter:

```json
{"jsonrpc":"2.0","id":"2","result":{"models":[]}}
{"jsonrpc":"2.0","id":"4","result":{"accepted":true}}
{"jsonrpc":"2.0","id":"4","error":{"code":-32001,"message":"provider_unavailable","data":{"detail":"..."}}}
```

Async events from adapter:

```json
{"jsonrpc":"2.0","method":"event.assistant_delta","params":{"conversation_id":"...","item_id":"...","text":"hi"}}
{"jsonrpc":"2.0","method":"event.transcript_record","params":{"conversation_id":"...","entry":{"role":"assistant","item_id":"...","text":"hi"}}}
{"jsonrpc":"2.0","method":"event.live","params":{"type":"approval","conversation_id":"...","request_id":"...","kind":"command","request_method":"provider.method","request_params":{},"payload":{}}}
{"jsonrpc":"2.0","method":"event.token_usage","params":{"conversation_id":"...","tokens":123,"context_window":200000}}
```

Adapter invariants:

- Adapter owns provider-specific parsing.
- Adapter emits normalized DTOs only.
- Every live event shape that needs replay must have a matching transcript DTO
  when appropriate.
- Adapter errors should be explicit DTO errors, not success-shaped fallbacks.
- Adapter must not write into the Python harness cache/data roots.

### Adapter DTO boundary

The first concrete adapter DTO surface lives in:

- Rust: `rust/crates/als-adapter-protocol/src/lib.rs`
- Python mirror: `agent_log_server_rs/adapter_protocol.py`

The Rust crate is authoritative for the server side. The Python mirror exists so
adapter authors can import the same method/event names and emit matching JSON
without copying string constants by hand.

Current method groups:

| Group | Methods |
| --- | --- |
| Extension lifecycle | `extension.initialize`, `extension.shutdown` |
| Discovery | `extension.list_models`, `extension.list_sessions` |
| Conversation | `conversation.start`, `conversation.resume`, `conversation.send`, `conversation.interrupt`, `conversation.compact` |
| Approval | `approval.respond` |

Current adapter notifications:

| Group | Events |
| --- | --- |
| Generic envelopes | `event.live`, `event.transcript_record`, `event.status` |
| Message/reasoning | `event.user_message`, `event.assistant_delta`, `event.assistant_finalize`, `event.reasoning_delta`, `event.reasoning_finalize` |
| Tools/shell | `event.tool_begin`, `event.tool_delta`, `event.tool_end`, `event.shell_begin`, `event.shell_delta`, `event.shell_end` |
| UX/runtime | `event.approval_request`, `event.token_usage`, `event.error`, `event.warning` |

Important DTO choices:

- `AdapterLiveEvent` preserves the frontend live event contract by serializing the
  event type under the exact key `type`.
- `AdapterTranscriptRecord` preserves replay by serializing the transcript role
  under the exact key `role`.
- Both DTOs include common identity and target fields (`conversation_id`, `id`,
  `turn_id`, `subagent_id`, `path`, `line`, `column`) plus an `extra` map for
  provider-specific normalized fields that still belong in the generic contract.
- `AdapterCapabilities` declares what the adapter supports before Rust tries to
  call optional functionality such as sessions, approvals, compaction, or
  interruption.
- Model metadata includes `context_window`,
  `supported_reasoning_efforts`, structured `capabilities`, and optional raw
  provider metadata so settings UIs do not lose provider capabilities during
  normalization.
- `ConversationSendParams` includes optional `toast_context`, matching the
  existing conversations RPC send contract for reply-capable toast flows.

Validation currently covers:

- Rust unit tests for live event and transcript record serialization.
- Python mirror smoke test for the same `assistant_delta` and `assistant`
  transcript shapes.

## Generic extension adapter and Copilot pilot

The original focused Copilot adapter pilot still lives in:

- `agent_log_server_rs/adapters/copilot_sdk_adapter.py`
- console script: `als-rs-copilot-adapter`

The current Rust runtime path uses the generic adapter:

- `agent_log_server_rs/adapters/extension_adapter.py`
- console script: `als-rs-extension-adapter`
- spawned by Rust as `python -m agent_log_server_rs.adapters.extension_adapter`

The generic adapter initializes the existing Python extension loader
(`extensions.load_extensions(...)`) with ALS-RS callbacks and dispatches methods
by `extension_id`. Runtime conversation/model support is intentionally limited to
extension type `copilot_sdk` in the current slice. Codex/Codex-exp are visible in
metadata but return explicit unsupported errors for conversation/model methods
until their adapter runtime paths are wired.

Supported Copilot scope:

| Method | Behavior |
| --- | --- |
| `extension.initialize` | Initializes the loader, returns capabilities for the selected extension, and reports unsupported runtime capabilities for non-Copilot types. |
| `extension.list_models` | For `copilot-sdk`, calls the existing Copilot SDK extension `list_models()` and normalizes each model into `AdapterModelInfo`. |
| `extension.package.validate` | Validates a path/zip/git extension source through the existing Python installer helpers. |
| `extension.package.install` | Installs a third-party extension into `${ALS_RS_DATA_DIR}/extensions`, updates that root's `extensions.json`, then reloads Rust and adapter registry state. |
| `extension.package.update` | Updates a user-installed extension from an explicit or recorded source, then reloads Rust and adapter registry state. |
| `extension.package.remove` | Removes only user-installed extensions; builtin extensions are refused. |
| `conversation.start` | For `copilot-sdk`, calls the existing Copilot SDK extension `init_session()` and returns `ConversationAckResult`. |
| `conversation.send` | For `copilot-sdk`, calls the existing Copilot SDK extension `handle_message()` and returns `ConversationAckResult`. |
| `extension.shutdown` | Stops supported extension handlers. |

First physical transport:

- newline-delimited JSON-RPC 2.0 over adapter stdin/stdout
- one JSON object per line
- Rust can later replace this transport without changing adapter DTO names
- live events from the existing Copilot router are forwarded as `event.live`
  notifications
- transcript writes are forwarded as `event.transcript_record` notifications

The pilot intentionally reuses the existing Copilot extension client through the
generic extension loader instead of calling the vendored SDK directly. This keeps
the first Rust migration slice focused on process/DTO boundaries while preserving
existing Copilot runtime behavior, event routing, lazy session creation, model
metadata normalization, and transcript generation.

The pilot uses an in-memory meta store for now. That is sufficient for a single
adapter process smoke path, but Rust should provide durable conversation metadata
when it owns the conversation store.

## Rust adapter runtime wiring

The first Rust-side adapter runtime lives in:

- `rust/crates/als-server/src/adapter_process.rs`
- `rust/crates/als-server/src/adapter_routes.rs`

Runtime behavior:

- Rust spawns the Python adapter module with:
  - `${ALS_RS_PYTHON_BIN:-python} -m agent_log_server_rs.adapters.extension_adapter`
- The Python bootstrap exports `ALS_RS_PYTHON_BIN=sys.executable` so development
  launches reuse the same interpreter/environment that launched ALS-RS.
- Rust injects the repo root into `PYTHONPATH` when launching the adapter from a
  source checkout so direct `target/debug/als-server` launches can import the
  Python package.
- Rust injects the resolved `ALS_RS_DATA_DIR`, `ALS_RS_CACHE_DIR`,
  `ALS_RS_CONFIG_DIR`, and `ALS_RS_STATIC_DIR` into adapter children so Python
  extension code sees the same isolated roots even when `als-server` was launched
  directly rather than through the Python bootstrap.
- Rust explicitly forwards the framework-shell environment used by the existing
  Python extension path:
  - `FRAMEWORK_SHELLS_BASE_DIR`
  - `FRAMEWORK_SHELLS_SECRET`
  - `FRAMEWORK_SHELLS_FWS_SOCKETIO_SERVER_PID`
  - `FRAMEWORK_SHELLS_SECRET_FINGERPRINT`, derived from
    `FRAMEWORK_SHELLS_REPO_FINGERPRINT` when present
- `extension.initialize` passes ordered `extensions_dirs` so the Python loader
  and Rust registry use the same multi-root extension view. Root 0 is builtin;
  root 1 is the ALS-RS user-installed extension root at
  `${ALS_RS_DATA_DIR}/extensions`.
- Adapter transport is newline-delimited JSON-RPC 2.0 on stdin/stdout.
- If `ferrous-framework-pyo3` is enabled and framework-shell config is present,
  `als-server` prefers the `ferrous_framework` transport: a PyO3-backed
  in-process wrapper over `framework_shells.spawn_shell_pipe()`,
  `subscribe_output_bytes()`, and `write_to_shell()` that starts the adapter as
  an observed native-pipe shell labeled `als-rs-extension-adapter`.
  The shell shape comes from `agent_log_server_rs/shellspec/extension_adapter.yaml`.
- Rust keeps stdout as protocol-only and the Python adapter redirects provider
  logs to stderr so Copilot client `print(...)` output cannot corrupt JSON-RPC.
- Requests are correlated by JSON-RPC `id`; when adapter stdout closes, pending
  waiters are failed explicitly.
- Adapter child processes are `kill_on_drop(true)` so smoke tests and server
  shutdowns do not leave orphan adapter processes.
- `event.live` and `event.transcript_record` notifications flow through the
  adapter event sink. A Rust background fanout task maps known live event `type`
  values to `/rpc/conversations` `rpc.notify` methods and persists transcript
  records into the Rust conversation store.

Smoke routes:

| Route | Purpose |
| --- | --- |
| `POST /api/adapter/copilot/initialize` | Spawn/initialize the Copilot adapter and return capabilities. |
| `GET /api/adapter/copilot/models` | Call `extension.list_models` through the adapter. |
| `POST /api/adapter/copilot/conversations/{conversation_id}/start` | Call `conversation.start` through the adapter. |
| `POST /api/adapter/copilot/conversations/{conversation_id}/send` | Call `conversation.send` through the adapter. |
| `GET /api/adapter/copilot/events` | Inspect the temporary live/transcript notification sink. |

Validation currently covers:

- Rust unit tests for JSON-RPC response correlation by request id.
- Rust unit tests for `event.live` and `event.transcript_record` capture.
- Focused Python py_compile/basedpyright checks for the adapter executable and
  DTO mirror.
- Live Rust smoke tests against temporary ALS-RS servers:
  - `POST /api/adapter/copilot/initialize`
  - `GET /api/adapter/copilot/events`
  - `/rpc/settings` registry metadata, request cards, and UI features
  - `/rpc/conversations` Copilot-backed send with assistant finalization and
    persisted replay rows

## Extension registry and lifecycle bridge

The first Rust registry bridge lives in:

- `rust/crates/als-server/src/extension_registry.rs`
- `rust/crates/als-server/src/settings_rpc.rs`
- `agent_log_server_rs/adapters/extension_adapter.py`

Current behavior:

| Surface | Behavior |
| --- | --- |
| Rust registry | Reads `extensions/extensions.json` and each extension `manifest.json` at startup, producing metadata with id/name/type/path/enabled/active/version/dependency fields, capabilities, manifest, and UI maps. |
| Settings RPC | `extensions.list` returns the Rust registry snapshot. `extensions.reload` rescans the registry from disk and asks a running adapter loader to reload. Request-card and UI-feature RPC methods use registry metadata and preserve frontend parity fields such as `module_url`. |
| Python adapter loader | The generic adapter initializes `extensions.load_extensions(...)` once in the adapter process with ALS-RS callbacks and the same `extensions_dir` root. |
| Runtime selection | `conversation.send` chooses the selected extension from request params, then persisted conversation metadata/settings (`extension_id`, `agent_type`, `settings.agent`), then the first active registry entry. The frontend normally sends only `conversation_id` and `text`, so persisted metadata is the routing source of truth. |
| Runnable support | `copilot-sdk` is the only supported runtime type for model/conversation methods in this slice. Non-Copilot extension types are metadata-visible but return explicit unsupported errors for runtime methods. |
| Enablement overlay | `extension.enabled.set` persists ALS-RS-local enablement state in `${ALS_RS_CONFIG_DIR}/extensions_state.json`, updates Rust registry active state, and asks a running adapter loader to reload. It does not mutate extension package manifests or `extensions.json`. |
| Dependency/readiness mediation | A running adapter reload applies Rust enablement overrides to the Python loader, calls extension-owned dependency checks, merges `dependency_status`, `dependency_ok`, `dependency_message`, and `dependency_details` back into Rust registry state, and waits for readiness when enabling an active extension. |
| Dependency install | `extension.install` routes through the generic adapter to extension-owned `install_extension_dependencies()`, then rechecks dependencies and merges the resulting runtime state back into the Rust registry. |
| Codex model discovery | The generic adapter now supplies a real framework-shell manager callback to the Python extension loader, so `codex-ext`/`codex-ext-exp` model listing can use their existing FWS-owned app-server transport instead of being metadata-only. |
| FWS runtime adoption | The TE2 bootstrap now passes framework-shell runtime identity to `als-server` as explicit CLI arguments, Rust stores those values in `ServerConfig`, and adapter spawn forwards that config-backed environment to Python. Env fallback remains for standalone/manual launches. |
| Extension debug probe | `/rpc/settings` method `extension.debug.probe` now returns a redacted Rust/adapter/FWS diagnostic snapshot, with optional `ensure_manager: true` to force framework-shell manager creation/adoption without launching provider model commands. |
| Extension shell grouping | ALS-RS-launched extension shellspecs now use the `als-rs` framework-shell subgroup instead of the legacy `codex_agent` subgroup, while preserving provider-specific groups like `codex`, `codex_exp`, and `copilot`. |
| Extension warmup | ALS-RS starts a non-blocking background warmup task at router startup. The task initializes the Python adapter, applies Rust enablement overrides, runs dependency checks, and calls extension-owned `warm_up_extensions()` for active extensions. |
| Copilot pipe readiness | Copilot FWS pipe adoption now waits briefly for framework-shell pipe state/stdin after shell launch instead of failing immediately with `copilot sdk pipe not available`. |
| Copilot shell adoption | Copilot FWS shell adoption now ignores stale shell records whose `app_id`/subgroups do not match the current TE app, preventing ALS-RS from attaching to the older `codex_agent` Copilot stdio shell. |

Current lifecycle limitations:

- `extension.enabled.set`, `extension.install`, and `extension.session.bind` are
  still explicit not-implemented responses in ALS-RS.
- `extensions.reload` now rescans disk into the mutable Rust registry and asks a
  running Python adapter loader to `extension.reload` with `force=true`. If the
  adapter has not been spawned yet, the next adapter initialize loads from the
  current extension root.
- Rust does not run extension dependency checks yet. It treats manifest
  version/presence as the basic active/readiness gate.
- Python extension handlers are initialized coarsely inside one long-lived adapter
  process rather than per-extension process ownership.
- Codex/Codex-exp runtime methods are not wired through ALS-RS yet.

## Rust conversation store groundwork

The first Rust-owned conversation store lives in:

- `rust/crates/als-server/src/conversation_store.rs`
- `rust/crates/als-server/src/conversation_routes.rs`

Persistence layout:

```text
${ALS_RS_DATA_DIR}/conversations/<safe-conversation-id>/
  meta.json
  transcript.jsonl
```

Store behavior:

- conversation IDs are normalized to path-safe ASCII IDs containing only
  alphanumeric characters, `.`, `_`, and `-`
- store code never resolves symlinks
- metadata includes `conversation_id`, optional `title`, canonical
  `extension_id`/compat `agent_type`, provider `thread_id` /
  `provider_session_id`, `cwd`, `label`, `alias`, ordered `pinned` state,
  `pending_approvals`, optional `last_preview`, `status`, timestamps,
  `settings`, draft text, and transcript counters
- transcript append assigns `conversation_id`, monotonically increasing
  `order_id`, and `ts` when missing
- transcript rows remain JSONL objects so the existing transcript contract can be
  mapped directly in the next `/rpc/conversations` phase

Smoke routes:

| Route | Purpose |
| --- | --- |
| `POST /api/conversations` | Create conversation metadata. |
| `GET /api/conversations` | List conversation summaries. |
| `GET /api/conversations/{conversation_id}/meta` | Load or create conversation metadata. |
| `POST /api/conversations/{conversation_id}/transcript` | Append one transcript row. |
| `GET /api/conversations/{conversation_id}/transcript` | Read transcript rows. |

Validation currently covers:

- Rust unit test for metadata creation, safe ID normalization, transcript append,
  transcript readback, and conversation listing
- live route smoke test against a temporary ALS-RS server with isolated data and
  cache roots

## Conversations RPC namespace

The first Rust-owned conversations Socket.IO namespace lives in:

- `rust/crates/als-server/src/conversation_rpc.rs`
- registered from `rust/crates/als-server/src/socketio.rs`

Transport contract:

- namespace: `/rpc/conversations`
- request event: `rpc`
- server ack payload: JSON-RPC 2.0 success/error envelope
- notification event: `rpc.notify`

Implemented methods:

| Method | Behavior |
| --- | --- |
| `conversation.create` | Creates Rust conversation metadata. |
| `conversation.list` | Lists splash-card-ready Rust conversation metadata as `{ items, active_conversation, active_conversation_id, active_view, pinned_conversations, revision, transport }`. Pinned conversations are ordered first from persisted metadata. |
| `conversation.get` | Loads or creates metadata for an explicit conversation ID; when called without an ID during boot, hydrates the persisted active conversation from app UI state and returns an explicit `ok: false` result instead of creating missing metadata when no active conversation exists. |
| `conversation.select` | Loads metadata and returns `active_view`. |
| `conversation.update` | Merges settings updates into `meta.json`, normalizes extension/cwd/label/alias card fields, and persists provider thread/session ids. |
| `conversation.delete` | Deletes a Rust conversation directory. |
| `conversation.pins.set` | Persists ordered pin state directly onto conversation metadata and returns the normalized valid pin list. |
| `conversation.draft.set` | Stores draft text on conversation metadata. |
| `conversation.replay.getChunk` | Reads transcript JSONL rows and returns the frontend replay chunk frame shape. |
| `conversation.send` | Appends/broadcasts the user message, resolves the selected extension from persisted metadata/settings, initializes that extension through the generic adapter, and sends the turn through the adapter. Adapter provider session ids are persisted back to metadata. |

Backend-driven list sync:

- ALS-RS emits `conversation.list.updated` on `/rpc/conversations` after
  accepted create/select/update/session-bind/delete/pin and provider metadata
  mutations that affect splash/minibar state.
- The payload is the full `conversation.list` snapshot plus `reason`,
  `changed_conversation_id` or `deleted_conversation_id`, and a monotonic
  `revision`; frontend clients replace their splash/minibar list from this
  snapshot and drop stale revisions.
- High-frequency draft typing remains on the narrower
  `conversation.draft.updated` row-level channel instead of broadcasting a full
  list snapshot on every keystroke.

Known follow-up:

- Full async assistant fanout from adapter `event.live` into `/rpc/conversations`
  `rpc.notify` is implemented for the Copilot pilot. The fanout task also
  persists adapter transcript records under ALS-RS conversation roots and skips
  duplicate adapter user echoes because Rust owns local user rows.
- Replay hydration now supports JSONL chunk frames over RPC without changing the
  persisted `transcript.jsonl` format.
- `conversation.interrupt`, `conversation.compact`,
  `conversation.approval.respond`, and `conversation.shell.exec` return explicit
  not-implemented control results for now.
- Legacy Python intent was used as a parity guide, not a logic template: the
  Python server rendered splash cards from full `meta.json`, stored
  `pinned_conversations` as ordered app config, and resolved the selected
  extension from `meta.settings.agent`. ALS-RS streamlines this by keeping the
  same card-visible fields while storing pin order and routing state in the
  conversation metadata itself.

## ALS-RS app UI state and TE2 sidebar IPC

Rust owns the minimal app UI state required to restore the active view and active
conversation across ALS-RS restarts:

```text
${ALS_RS_CACHE_DIR}/app_state.json
```

The persisted JSON intentionally stays smaller than legacy Python
`app_server_config.json`:

```json
{
  "active_conversation": "conversation-id-or-null",
  "active_view": "splash-or-conversation",
  "user_name": "optional splash display name",
  "show_console_worker_id": false
}
```

Current behavior:

- `UiSelectionStore` loads this file during `AppState::new(...)` and writes it on
  conversation selection, active-view changes, active conversation deletion, and
  splash app-config updates.
- `/rpc/settings` `config.get` / `config.update` read and write the same
  persisted `user_name` and `show_console_worker_id`, while also returning
  active-view/conversation fields for compatibility.
- `/rpc/ui` `view.get` / `view.set`, `hostUi.get`, and `/rpc/conversations`
  list/get responses expose both `active_conversation` and the existing
  `active_conversation_id` / `conversation_id` compatibility names.
- The Rust router starts a best-effort startup task that connects to TE2's legacy
  `/sidebar_ipc` namespace and calls `sidebar:cwd_get`, so `hostUi.projectRoot`
  can be available before the frontend waits for a lazy sidebar project change.
  `hostUi.recheck` remains the explicit recovery/update path and also reissues
  `sidebar:cwd_get` on an already-connected sidebar client instead of merely
  reporting connected state. Rust Socket.IO ack payloads can arrive as arrays, so
  the CWD ack parser unwraps the same response shape Python `client.call(...)`
  returned directly.
- The ALS-RS splash header can optionally show the TE2 console bridge worker ID
  beside the settings gear. The toggle is stored as `show_console_worker_id` in
  `app_state.json`, and the browser reads the worker ID published by
  `console_bridge.js`.
- Direct-port introspection must not depend on TE2 proxy path rewriting:
  - ALS-RS `/rpc/*` frontend calls use `/socket.io` on the direct server origin
    and the proxy mount's `/socket.io` path when hosted through TE2.
  - The TE2 console bridge starts on direct ALS-RS pages by connecting to the TE2
    console origin instead of skipping bridge init outside `/api/app/.../proxy`.
  - Schema dynamic sources call the `/rpc/settings` client directly; they do not
    wait for the conversations live socket before loading provider models,
    sessions, or runtime options.
- ALS-RS owns the repo namesake agent-log surface:
  - JSONL path: `${ALS_RS_CACHE_DIR}/agent-log/agent_chat.log.jsonl`.
  - HTTP parity routes: `GET/POST /api/messages`,
    `GET/DELETE /api/messages/{msg_num}`, and `POST /api/messages/await`.
  - WebSocket parity route: `/ws`, broadcasting appended records as JSON text.
  - Socket.IO compatibility: `/appserver` handles `get_log_messages` and
    `post_log_message`, and broadcasts `agent_log_message` when new rows append.
  - Browser page parity: `/agent-log` and `/agent-log/` serve an ALS-RS page shell
    using the existing `static/app.js` and `static/style.css`; the legacy Quit
    Server control is omitted in the Rust page.
  - `static/js/socketio_runtime.js` resolves proxy-aware Socket.IO paths for
    legacy-style pages, so `/agent-log` works from both the direct ALS-RS origin
    and the TE2 proxy mount.
  - The `agent-pty-blocks` MCP agent-log tools derive `/api/messages` from
    `AGENT_LOG_SERVER_ORIGIN`, so ALS-RS conversations target the Rust origin
    while non-ALS launches keep the legacy Python default.
- `codex-ext` schema cache lookup now prefers
  `${ALS_RS_CACHE_DIR}/codex_app_server_schema` when that env var is present,
  falling back to the legacy Python harness cache at
  `~/.cache/app_server/codex_app_server_schema` outside ALS-RS.

Validation currently covers:

- live Socket.IO smoke test against a temporary ALS-RS server with isolated data
  and cache roots
- JSON-RPC ack calls for create, list, replay chunk, draft set, and get
- isolated and TE2-wrapper Copilot send smokes that verified live assistant final
  events and persisted replay rows through the Rust fanout path

## First extension pilot

Recommendation: start with `copilot-sdk` for the adapter protocol pilot while
keeping the frontend/backend DTOs provider-neutral.

Rationale:

- There is no built-in Codex runtime to lean on anymore, so one extension path is
  mandatory.
- Copilot SDK can plausibly run as a pure Python extension surface that returns
  contract DTOs.
- The current local SDK patch already supports feeding stdio from an externally
  managed binary, but that behavior is gated and should not prevent a simpler
  internally managed Python path for the adapter pilot.
- The first pilot should validate the extension boundary, not preserve historical
  Codex-specific assumptions.

## Milestones and acceptance criteria

### Milestone 1: isolated bootstrap skeleton

Deliverables:

- Python package entrypoint for ALS-RS.
- Separate default port.
- Separate ALS-RS data/cache roots.
- TE2 proxy wrapper can launch the bootstrap.
- Rust workspace exists under `rust/`.
- Rust binary can start and answer `GET /api/health`.

Acceptance:

- Running ALS-RS does not touch Python harness runtime/cache roots.
- TE2 can open ALS-RS as a separate app.
- The live Python harness remains unaffected.

### Milestone 2: Rust static/frontend host

Deliverables:

- Rust serves the existing frontend bundle and static assets.
- `GET /`, manifest, static paths, and health work through TE2 proxy.

Acceptance:

- Frontend loads from the Rust server on the ALS-RS port.
- No ASGI/FastAPI server is running for ALS-RS.

### Milestone 3: conversation storage and replay

Deliverables:

- Rust-owned implementation of the existing conversation index and transcript
  system under ALS-RS data root.
- Rust-owned replay endpoint or RPC method using the current transcript shape.
- `/rpc/conversations` can satisfy initial UI boot/replay.

Acceptance:

- Frontend can load a conversation list.
- Selecting a conversation replays transcript rows from ALS-RS storage.

### Milestone 4: adapter protocol pilot

Deliverables:

- Python adapter process/surface for one extension.
- JSON-RPC request/response/notification DTO protocol over the selected first
  transport.
- Rust process supervisor for adapter lifecycle.
- DTO definitions for model listing, send, live events, transcript records, and
  visible errors.

Acceptance:

- Rust can list models through the adapter.
- Rust can send a message through the adapter.
- Adapter events appear in the frontend through Rust/socketioxide.
- Transcript records persist under ALS-RS roots.

### Milestone 5: live interaction parity slice

Deliverables:

- User send.
- Assistant streaming/finalization.
- Tool/shell/card events required by the chosen pilot extension.
- Approval/ask-user round trip if the pilot extension needs it.
- Interrupt/compact only if supported by the chosen pilot extension.

Acceptance:

- A real provider-backed conversation can run inside ALS-RS through TE2.
- Reload/select is transcript-first.
- The extension session remains adapter-owned and provider-specific.

Current status:

- User send, assistant streaming/finalization, token updates, and persisted replay
  rows work for the Copilot adapter pilot.
- Tool/shell cards, approvals/ask-user, interrupt, and compact remain follow-up
  parity work.

### Milestone 5.5: schema settings modal and lifecycle mediation

Deliverables:

- `extension.settingsSchema.get` serves real extension settings schema payloads
  from the registry/manifest-backed extension root.
- `extension.splashSchema.get` serves real splash/setup schema payloads.
- `extension.models.list` routes through the generic adapter for supported
  extensions so schema-driven dynamic model/reasoning fields work.
- Extension lifecycle RPCs mediate enable/disable, reload/rescan, install/update,
  dependency status, and Python loader synchronization instead of returning
  stubs.

Acceptance:

- The existing schema settings modal works against ALS-RS without an ALS-RS-only
  frontend path.
- Dynamic select/model fields use ALS-RS RPC/adapter data.
- Extension availability reflects the same `enabled && dependency_ok` contract as
  the Python loader.
- Registry reload updates both Rust metadata and Python adapter loader state.

Current status:

- `extension.settingsSchema.get` and `extension.splashSchema.get` now route from
  ALS-RS `/rpc/settings` through the generic Python adapter, so extensions can
  synthesize dynamic schema JSON instead of requiring Rust to read only static
  files.
- The adapter falls back to loader-owned static `settings_schema.json` only when
  the extension does not provide a dynamic settings schema.
- `extension.models.list` now routes through the generic adapter so schema modal
  dynamic model fields can use the same RPC path as the existing frontend.
- `copilot-sdk` schema/model smoke returned a real schema with 13 fields and 14
  models through ALS-RS RPC.
- The adapter spawn path now explicitly preserves the active framework-shell
  environment for extension-owned FWS process management/observability. This
  keeps the Copilot extension on its existing framework-shell pipe model instead
  of moving CLI process ownership into Rust.
- `extensions.reload` now performs a real Rust registry disk rescan and
  synchronizes the running Python adapter loader when one exists.
- `extension.enabled.set` now persists enablement in an ALS-RS config overlay and
  synchronizes a running Python adapter loader when one exists.
- Dependency checks and dependency installation now run through the existing
  Python loader/extension `dependencies.py` contract, and Rust merges the
  resulting readiness fields back into registry entries.
- `codex-ext` model listing now works through the adapter/FWS path; a direct
  adapter smoke returned six Codex models from `model/list`.
- The ALS-RS TE2 bootstrap now makes FWS runtime adoption explicit by passing
  framework-shell identity values as `als-server` CLI arguments. Rust parses
  those arguments into config and forwards them into the Python adapter, so the
  adapter no longer depends only on ambient Rust process env.
- Added a generic `extension.debug_probe` adapter method surfaced as
  `/rpc/settings` `extension.debug.probe`, allowing frontend console evals to
  compare Rust config, adapter env, loader state, handler state, and optional
  framework-shell manager/store/shell metadata.
- Updated Codex, Codex-exp, and Copilot extension shellspec subgroups so
  ALS-RS-launched provider runtimes group under `als-rs` in framework-shells
  instead of being grouped with the legacy Python `codex_agent` app.
- Added generic adapter `extension.warm_up` and ALS-RS background startup warmup
  so extension runtimes are warmed eagerly instead of only being touched by
  schema/model/send paths.
- Hardened Copilot's FWS pipe wrapper against startup races where the shell
  record exists before `get_pipe_state(...).process.stdin` is available.
- Hardened Copilot's FWS shell adoption so ALS-RS ignores stale `codex_agent`
  Copilot shell records and starts/adopts an `als-rs`-owned stdio shell.
- Removed the disabled `codex-ext-testing` compatibility shim from
  `extensions/extensions.json`; it was not a real extension and schema-modal
  iteration could request it as if it were one, closing the adapter stdio path.
- Lifecycle mediation remains pending: deeper adapter-loader lifecycle ownership
  is not yet implemented.

### Schema-declared picker/source contract

The settings schema is the authority for dynamic settings UI behavior. Each
dynamic field must answer three questions without requiring ALS-RS or the shared
frontend to know provider-specific payload shapes:

1. **What field should be rendered?**
   - The extension-owned schema declares the field type, label, current value,
     dependencies, and any picker/select behavior.
2. **For that field, what backend method/source should be called?**
   - The schema declares the extension-facing source/method that can populate or
     validate the field. ALS-RS may route that method through `/rpc/settings` and
     the generic adapter, but it must not invent a Codex-only or Copilot-only
     server method.
3. **What response shape/path/type should the UI read?**
   - The schema declares the response path(s) for options, labels, values,
     defaults, and any item identity fields. The renderer follows those paths
     instead of hardcoding provider metadata normalization.

This is the same rule used by the dynamic reasoning-effort fix:
`dynamic_options_from` tells the shared renderer which model/source field to
watch, which extension-facing method output to inspect, and where options and
defaults live in that response. The session/rollout picker should follow the
same pattern. Codex should expose its normalized extension-facing registry
methods/schema declarations; Copilot can use official SDK-backed methods, but
still describes them through the same schema/source/path contract.

Provider session/rollout selection is a local conversation creation mode, not a
fresh provider-start mode:

- **Fresh draft conversation:** ALS-RS may keep the local conversation in memory
  and show it in the list while it is unbound, but it should remain a draft until
  the provider returns a thread/session id. Durable `meta.json` and
  `transcript.jsonl` should be written when the conversation is bound to a
  provider id. If implementation simplicity requires persisted draft cleanup
  during migration, deleting unbound draft conversations on launch is acceptable
  because it produces the same user-visible lifetime after restart.
- **Existing provider session/rollout import:** the schema picker selects a
  provider-native id, ALS-RS stores that as `thread_id` /
  `provider_session_id`, and Rust calls the generic adapter resume/hydrate hook.
  The extension owns the provider-specific behavior. Copilot resumes and reads
  the provider hydration stream. Codex imports rollout JSONL through its
  extension-owned hydration path and ignores normal `thread/resume` history
  noise for that import flow. Rust must only persist normalized adapter transcript
  records and provider binding ids; it must not parse Codex rollout semantics.

Current ALS-RS gap for this contract:

- `/rpc/settings` `extension.sessions.list` still needs to route through the
  generic adapter `extension.list_sessions` DTO path instead of returning a
  placeholder empty list.
- Extension schemas should be reviewed so session/rollout picker fields declare
  the method/source/path details explicitly, matching the three-question
  contract above.

### Milestone 5.6: conversation metadata persistence and routing

Deliverables:

- Rust `meta.json` read/write persistence for extension id, provider session id,
  labels, aliases, cwd, pins, pending approvals, previews, settings, draft, and
  transcript counters.
- Splash conversation list items contain the same card-visible data the frontend
  expects from Python: `settings.agent`, `settings.cwd`, `settings.label`,
  `settings.alias`, `pending_approvals`, `last_preview`, and `pinned`.
- `conversation.send` honors the selected extension persisted on the conversation
  instead of falling back to Copilot when the send payload has no extension id.
- `conversation.pins.set` persists ordered pin state and `conversation.list`
  returns `pinned_conversations`.

Acceptance:

- Starting a conversation from the schema settings modal and choosing Codex,
  Codex-exp, or Copilot routes the first send to that selected extension.
- Splash and drawer cards show label/alias/agent/cwd/pin state from ALS-RS
  metadata without an ALS-RS-specific frontend path.
- Rust tests cover metadata persistence/list serialization and selected-extension
  routing precedence.

Current status:

- Rust conversation metadata now mirrors selected-extension state into
  `extension_id`, compat `agent_type`, and `settings.agent`.
- Rust settings updates merge into existing metadata like the Python route did,
  removing null/empty fields and normalizing cwd/label/alias for cards.
- Ordered pins are persisted directly on conversation metadata with
  `pinned`/`pinned_order`.
- `conversation.send` resolves the selected extension from request params,
  metadata fields, `settings.agent`, then active registry state; it no longer
  hardcodes Copilot as the normal fallback.
- Transcript appends now project provider thread/session ids, status, and
  preview-worthy user/assistant text back into conversation `meta.json`.
- ALS-RS emits `conversation.meta.updated` after Rust-owned user appends,
  adapter ACK thread binding, and adapter transcript persistence so splash/drawer
  cards can update without waiting for a manual list reload.
- The frontend RPC event router patches `conversationList` and active
  `conversationMeta` from `conversation.meta.updated` and live `status`
  notifications.
- A live-debug correction: a visible `codex_agent` app-server shell belonged to
  a different harness. ALS-RS provider shell grouping/tagging is working; do not
  treat that other-harness shell as evidence of ALS-RS grouping failure.
- Remaining follow-up: schema settings modal requests can still crash/close the
  extension adapter path; the next reproduction should use the new diagnostics
  to identify the failing request/stdio line.
- Rust now pipes adapter stderr, tees it back to Rust stderr with an
  `[extension-adapter]` prefix, and captures bounded adapter stderr/stdout
  protocol diagnostics in the adapter event snapshot.
- `extension.debug.probe` now includes recent adapter event snapshots so frontend
  evals can inspect adapter stderr, invalid stdout protocol lines, stdout close,
  and read failures.
- ALS-RS now mirrors the Python provider-binding path for restored sessions:
  schema `settings.session` is stripped during `conversation.create` /
  `conversation.update`, persisted as normalized `thread_id` /
  `provider_session_id`, then sent through the adapter `conversation.resume`
  method. The adapter delegates to extension-owned `resume_session_with_history()`
  and `hydrate_transcript()`, emitting hydrated transcript rows back to Rust.
- `conversation.send` now includes any existing Rust-owned provider binding in
  adapter params so Codex/Copilot lazy resume can see the selected provider
  thread/session after adapter restart or reload.
- Added the `ferrous_framework` adapter transport slice:
  - new Rust crate `rust/crates/ferrous_framework` owns the optional PyO3 bridge
    to framework-shell native pipes.
  - new Python helper `agent_log_server_rs/ferrous_framework.py` owns the
    background event loop and native-pipe read/write calls.
  - `als-server` prefers that transport when framework-shell context is present
    and the feature is enabled, emits `adapter.transport.started` /
    `adapter.transport.fallback` diagnostics for that path, and otherwise keeps
    the direct child transport as the default.
  - The source-checkout bootstrap skips stale `target/debug/als-server` binaries
    when framework-shell context is present and launches `cargo run --features
    ferrous-framework-pyo3` so the adapter framework shell can actually appear.
  - The PyO3 embed feature is gated as `ferrous-framework-pyo3`; optional feature
    validation targets Python 3.13 because the ambient `PYO3_CONFIG_FILE` points
    at a separate Python 3.14t config.
- Added the ALS-RS cwd-picker backend slice:
  - `/rpc/ui` `filesystem.list` now returns picker-compatible directory entries
    with `path`, `parent`, sorted `items`, `type`, and `is_symlink`.
  - `/rpc/ui` `filesystem.search` now supports the mention search contract with a
    case-insensitive regex, git-root `rg --files` discovery, and a directory-walk
    fallback.
  - The Rust implementation avoids symlink-resolving path canonicalization and
    keeps the frontend contract unchanged.
- Tightened the schema settings dynamic-options contract:
  - dependent selects can declare `dynamic_options_from` with source field, source
    match path, option path, option value/label paths, and default path.
  - Codex and Codex-exp generated schemas now declare reasoning-effort options
    from their extension-facing model-list response shape instead of relying on
    renderer-side Codex metadata guesses.
  - Copilot's static schema uses the same contract, so provider differences stay
    in extension schema declarations rather than shared UI code.
  - Conversation Info now appears for any existing provider-bound conversation
    with a `thread_id` or `provider_session_id`, including completed/successful
    conversations, not only `status == active`.
  - Live user smoke confirmed the schema settings behavior is working. The
    extension adapter can still exit occasionally while using the schema modal;
    this is tracked as a non-blocking stability follow-up rather than part of
    the dynamic-options contract fix.
- Clarified the next session/rollout picker contract:
  - schema picker fields must declare what to render, which extension-facing
    source/method to call, and which response paths/types the renderer should
    consume.
  - Fresh local conversations are drafts until the first provider thread/session
    id binds. ALS-RS may keep unbound drafts in memory and list them during the
    current runtime, but durable `meta.json` / `transcript.jsonl` should begin
    when the provider binding exists. During migration, startup cleanup of any
    unbound persisted drafts is an acceptable equivalent behavior.
  - Restored provider sessions/rollouts are not fresh starts. Rust should call the
    generic adapter resume/hydrate hook, persist the normalized binding and
    transcript rows it receives, and leave Copilot/Codex hydration-ignore/import
    details inside the extensions.
- Clarified the active-view parity issue:
  - The frontend can keep an active conversation in browser memory and filters
    multiplexed live events by that id, so live operation can appear correct.
  - `conversation.list`, `conversation.select`, `/rpc/ui view.get`, and
    `/rpc/ui view.set` still need one ALS-RS source of truth for open-last-state
    restoration. Without that, refresh/new-client boot can report splash/null
    even though `conversation.get` can load a real active conversation.
  - This is a generic correctness/restoration issue, not a provider/session
    issue. The intended fix is small Rust-side UI selection state, not
    extension-specific routing.
- Landed the session/rollout picker and Codex MCP stability snapshot:
  - shared schema session picker now has generic MRU/Created controls, a CWD
    filter button, richer row rendering, `$HOME`-relative CWD display, and active
    selected/filter accent state.
  - ALS-RS `/rpc/ui` exposes `filesystem.home` for the frontend home-prefix cache.
  - ALS-RS adapter session DTOs now include `created_at` and `updated_at`; the
    adapter promotes provider `context.cwd` to top-level `cwd` for shared picker
    filtering.
  - `codex-ext-exp` has been removed from the registry/package/runtime surface.
  - `codex-ext` no longer starts or warms the agent-pty HTTP shim; it injects
    `agent-pty-blocks` through stdio with `command`/`args`/`cwd`/`env`, while
    preserving the generated `enabled_tools` allowlist and 1-hour MCP timeout.
  - `codex-ext` session timestamps are normalized through the loaded
    `thread/list` response schema. `createdAt` is a schema-declared Unix
    timestamp and is converted to an ISO string for the shared session DTO.
- Added the tracked root contract document `NEW_CONVERSATION_CONTRACT.md`:
  - `provider_session_id` is the canonical provider binding field for fresh send
    and port-in resume ACKs.
  - ACKs now carry both `ok` and `accepted`; ALS-RS only binds provider ids from
    successful ACKs.
  - `conversation_id` remains the local harness id and must never be substituted
    for a provider session id.
  - Copilot and Codex now return canonical `provider_session_id` on successful
    bind/send paths while the adapter still accepts legacy `session_id` /
    `thread_id` aliases as compatibility input.
  - Live smoke clarified that normal Copilot fresh-session start/bind and
    port-in `resume_session_with_history()` both work under the unified ACK
    contract. The earlier unbound Copilot draft was caused by enabling the
     still-unpinned TE2 MCP integration feature, which fails before provider
     session creation with `TE2 base URL is required`.
- Restored ALS-RS footer runtime-options routing:
  - `/rpc/settings` `extension.runtimeOptions.get` now resolves the selected
    extension and conversation settings from Rust-owned conversation metadata,
    then forwards through the Python extension adapter.
  - The adapter protocol includes `extension.get_runtime_options`, implemented
    by delegating to the existing extension loader's schema/provider runtime
    option merger.
  - Footer controls remain a settings/runtime-options surface; saving quick
    footer selections still persists through the conversation update path.
- Restored ALS-RS footer interrupt routing:
  - `/rpc/conversations` `conversation.interrupt` now resolves the selected
    extension from Rust-owned conversation metadata and calls the generic
    adapter `conversation.interrupt` method.
  - The Python extension adapter delegates to the extension loader's
    `interrupt_session(extension_id, conversation_id)` hook, so Codex and Copilot
    keep their provider-specific abort implementations inside their extension
    clients.
  - The frontend receives the same `{ ok, conversation_id, transport: "rpc" }`
    control-result shape as other conversation controls; there is no
    provider-specific frontend or Rust branch.
- Added the ALS-RS TE2 sidebar IPC client slice:
  - Rust uses a best-effort Socket.IO client to the current TE2 legacy
    `/sidebar_ipc` namespace at `/ui_ipc_ws/socket.io/`, matching the existing
    Python host/sidebar lane while TE2's sidebar RPC transition is unfinished.
  - `/rpc/ui` `hostUi.recheck` triggers connection if needed, calls
    `sidebar:cwd_get`, and `sidebar:cwd_set` updates Rust host UI state; the
    existing frontend project-tab filter and new-conversation CWD prefill consume
    that `hostUi.projectRoot`.
  - `/rpc/ui` `file.open` emits `sidebar:agent_open` for TE2 file/line/column
    navigation.
  - Incoming `sidebar:mention` events now use Rust conversation selection/meta
    state: mentions for the active open conversation emit
    `conversation.mention.inserted`, while mentions for inactive conversations
    append the legacy draft mention token to `meta.draft`.
  - Follow-up: live-smoke project CWD sync, incoming mentions, and file-open
    navigation through the TE2 sidebar wrapper.

Current session/rollout picker issues to investigate next:

- Codex picker CWD filtering is still not correct in ALS-RS. Compare the ALS-RS
  adapter/request path against legacy Python `get_sessions` / session-picker
  behavior before changing semantics.
- Copilot picker rows currently show the same timestamp for MRU and Created and
  are not sorting correctly by time. Do not invent new sorting semantics; compare
  against legacy Python/Copilot extension behavior and the provider DTOs.
- Copilot sessions are not binding/hydrating for draft-new conversations or
  port-in/restored conversations. This is a blocker for treating Copilot picker
  display as complete and should be investigated against the old Python
  conversation create/update/resume paths.
- `agent-pty-blocks` stdio injection and Codex eager tool exposure have passed
  user smoke; keep them in the live regression set when touching MCP contracts.

### Milestone 6: dependency minimization

Deliverables:

- Remove FastAPI/uvicorn/python-socketio from ALS-RS runtime dependencies.
- Keep only Python dependencies required for bootstrap and adapter execution.

Acceptance:

- `pip install` installs the bootstrap and adapter dependencies only.
- Rust server owns all ALS-RS networking.

## Decisions made

1. Python package name and console script: `als-rs`.
2. Default port: `12459` is acceptable unless implementation discovers a direct
   conflict.
3. Config root: same as ALS-RS data root by default.
4. Frontend: current checkout/frontend is invariant.
5. Transcript system: current transcript system is invariant.
6. Extension pilot direction: start by shaping a provider-neutral contract around
   `copilot-sdk`.
7. Adapter transport: newline-delimited JSON-RPC 2.0 over direct child stdio for
   the default fallback, with optional `ferrous_framework` PyO3/native-pipe
   transport for observed framework-shell launches; DTOs remain
   transport-disposable.
8. Extension registry direction: read the existing `extensions/extensions.json`
   registry plus manifests rather than creating an ALS-RS-only registry format.

## Open design decisions

1. Whether Rust should reuse the current frontend bundle path exactly or only the
   current frontend checkout/contract while packaging it under an ALS-RS-specific
   static layout.
2. Extension lifecycle mediation boundaries.
   - Rust should own registry state, settings RPC, install/reload/dependency
     state, and adapter process supervision.
   - The Python adapter should own provider-specific handler initialization and
     event translation.
   - The exact reload/synchronization handshake between Rust registry state and
     the adapter loader still needs to be implemented.
3. How to wire Codex/Codex-exp runtime support through the generic adapter without
   reintroducing server-owned Codex fallback logic.

## Risks

- **Contract drift:** Rust DTOs may accidentally diverge from the frontend's
  existing event contracts. Mitigation: port contracts before porting internals.
- **Adapter scope creep:** Python adapter can become a second server. Mitigation:
  keep it request/event translation only.
- **Provider leakage:** Rust may start parsing provider-specific payloads.
  Mitigation: normalize inside adapter and keep DTOs provider-neutral.
- **Root leakage:** old paths may sneak into runtime helpers. Mitigation: route
  all path construction through ALS-RS root config.
- **TE2 coupling:** branch may accidentally depend on TE2 for product behavior.
  Mitigation: TE2 remains a development wrapper; standalone ALS-RS port must work.
- **Transport lock-in:** choosing a bespoke pipe shape could make later PyO3 or
  Unix-socket transport harder. Mitigation: make JSON-RPC DTOs the stable layer
  and treat bytes/strings/stdin/stdout/sockets/shared-memory as replaceable.

## Immediate next steps

1. Complete conversation metadata persistence and routing:
   - Rust meta/list/pin/routing implementation is complete for the local
     persistence path and now updates card-visible meta from transcript rows.
   - Provider binding from schema session/rollout selection is wired through
     Rust meta persistence and the generic adapter resume/hydration hook.
   - Follow-up: adjust draft semantics so unbound conversations stay memory-only
     until a provider thread/session id binds, or clear any unbound persisted
     draft records on launch during the migration window.
   - Follow-up: wire `/rpc/settings` `extension.sessions.list` through the
     adapter `extension.list_sessions` DTO path and review extension picker
     schemas for the three-question method/source/path contract.
   - Follow-up: live TE2 smoke with schema-modal-created conversations for each
     active extension.
   - Follow-up: persist or remember active conversation/view state through
     `conversation.select`, `conversation.list`, `/rpc/ui view.get`, and
     `/rpc/ui view.set` so refresh/open-last-state works coherently.
2. Implement schema settings modal support:
   - Completed for adapter-backed schema and model retrieval.
   - Follow-up: schema-driven splash actions still return explicit not-implemented
     results.
3. Implement extension lifecycle mediation:
   - Completed first sub-slice: explicit framework-shell env preservation for
     adapter-spawned Python extension processes.
   - Bootstrap now preserves inherited `FRAMEWORK_SHELLS_*` values from TE2/FWS
     launches, and standalone launches seed a stable directory-fingerprinted
     framework-shell secret before Rust/adapter startup so extension adapter
     framework-shell imports do not require a manual dummy secret.
   - `als-rs extension ...` now provides an offline-first package operator CLI
     that installs/updates/removes/validates/list/reloads against the ALS-RS
     second root (`${ALS_RS_DATA_DIR}/extensions`) without requiring a running
     server, supports path/zip/git sources, and best-effort notifies a running
     ALS-RS server to reload after successful mutations.
   - SIGINT/SIGTERM now runs bounded graceful shutdown: stop extension handlers
     through adapter `extension.shutdown`, close/terminate direct or
     ferrous-framework adapter transport, then notify Axum graceful shutdown.
   - real `extensions.reload` disk rescan and running-adapter loader reload:
     completed
   - enable/disable state: completed with an ALS-RS config overlay
   - dependency/readiness status and dependency install: completed through the
     Python adapter/loader contract
   - Python adapter loader synchronization
   - bounded adapter stderr/stdout-parse diagnostics exposed through
     `extension.debug.probe`: completed
   - Follow-up: reproduce the schema modal crash and inspect the adapter event
     snapshot for the failing request/stdio trail.
4. Continue live parity:
   - approvals/ask-user: first approval contract slice is implemented and
     documented in `APPROVAL_CONTRACT.md`. ALS-RS now persists
     `pending_approvals` from live `type: "approval"` adapter events, routes
     frontend `conversation.approval.respond` through adapter
     `approval.respond`, appends legacy-shaped `role: "approval"` handoff
     transcript rows, removes stale/resolved pending descriptors, and emits
     `conversation.approval.handoff`. The slice covers provider-native
     user-input request methods such as Copilot `copilot/user_input/request`
     and Codex `item/tool/requestUserInput`, not only MCP ask-user. ALS-RS
     also now serves extension-local `ui/...` and `static/...` assets through
     `/api/extensions/{extension_id}/assets/{asset_path}` so custom approval
     request-card modules declared in extension manifests can load. Live
     approval fanout now persists the pending descriptor first, emits
     `conversation.approval.request` as the first frontend-visible approval
     signal, then follows with `conversation.meta.updated` for restore/list
     state.
   - provider binding / history hydration: verified for both Copilot and Codex.
     ALS-RS now rejects any adapter/provider binding id that equals the harness
     `conversation_id`, so cold-resume/send retries cannot poison
     `provider_session_id` with a `conv_*` value.
    - schema-modal port-in history import now preserves hydrated `role: "user"`
      transcript rows while still suppressing normal live adapter user echoes.
      Rust keeps ownership of user rows for live `conversation.send`, but
      adapter-driven `conversation.resume` / `resume_session_with_history()`
      imports mark hydrated history rows so they persist into transcript replay.
    - large provider transcript import now has a source-level transaction path:
      the Python adapter emits `event.import_*` notifications and transcript
      batches around `hydrate_transcript()`, Rust persists those batches through
      bulk transcript append without per-row live fanout, and the frontend source
      reacts to `conversation.import.*` notifications with a conversation-scoped
      busy overlay plus coarse activity/status updates. The current slice was
      intentionally not rebuilt/transpiled before user smoke testing.
    - generic MCP contract: implemented first ALS-RS-to-extension DTO slice.
      `als-adapter-protocol` now carries a generic `mcp_context` on
     conversation start/resume/send, Rust builds a provider-neutral default
     context (`conversation_id`, `cwd`, requested `mcp_servers`, always-on
     stdio `agent-pty-blocks` with eager tool exposure intent, and optional
     `te2-mcp` intent when enabled),
     the Python extension adapter passes that through as `settings.mcp_context`,
     and extension-owned `mcp_contract.py` modules now map that generic DTO into
     provider-native Copilot/Codex MCP config without adding provider-specific
     logic to ALS-RS itself.
   - ask-user IPC bridge: ALS-RS now owns the `/ipc` Socket.IO namespace used by
     `mcp_agent_pty_server.py` when `agent-pty-blocks` runs over stdio. The MCP
     process remains the Socket.IO client, authenticates with the shared
     `~/.cache/app_server/ipc_secret`, receives `ask_user_response`, and acks
     with `ask_user_ack`; Rust injects `AGENT_LOG_SERVER_ORIGIN` through
     `mcp_context.defaults.agent-pty-blocks.appserver_origin` so the client
     connects to the Rust server port instead of the legacy Python default.
     `conversation.approval.respond` special-cases
     `request_method == "agent-pty/ask-user"` to emit the submitted response
     over IPC and only writes/removes the approval handoff after the MCP ack.
   - repo-memory/dev-instruction refresh: ALS-RS does not implement the old
     `pending_context` queue from the removed `codex-ext-exp` dynamic-devins
     fork. `.repo_memory.md` is bundled through the TE2 integration prompt
     context path at provider config boundaries instead: Codex rebuilds
     `developerInstructions` on each `turn/start`, while Copilot recomputes its
     runtime signature/config during `handle_message()` and re-resumes when the
     effective prompt context changes. That gives next-turn/resume/compact
     freshness without a mid-turn push.
    - TE2 sidebar IPC/RPC: Rust now speaks the typed JSON-RPC contract over the
      existing `/sidebar_ipc` logical namespace and `/ui_ipc_ws/socket.io`
      physical path. UI RPC `hostUi.recheck` best-effort calls
      `sidebar.cwd.get` and syncs CWD, `file.open` calls `sidebar.file.open`,
      and `rpc.notify` handles `sidebar.cwd.set` / `sidebar.mention`; narrow
      legacy `sidebar:*` fallback remains only for older TE2 instances during
      the cutover.
    - synced composer drafts: ALS-RS now broadcasts
      `conversation.draft.updated` after `conversation.draft.set` and after
      queued sidebar mentions update an inactive conversation draft, so other
      clients can hydrate live draft changes through the existing frontend
      `draft_update` path.
    - conversation splash/minibar sync: ALS-RS now broadcasts backend-owned
      `conversation.list.updated` snapshots with monotonic revisions after
      create/select/update/session-bind/delete/pin and provider metadata
      mutations, while keeping high-frequency composer draft edits on the
      narrower `conversation.draft.updated` channel.
   - interrupt
   - compact
   - shell/tool card parity for the Copilot pilot
5. Keep this migration plan current after each ALS-RS phase lands.
