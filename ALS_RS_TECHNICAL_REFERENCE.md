# ALS-RS Technical Reference

## Purpose and maintenance contract

This is the canonical detailed technical reference for ALS-RS. It began as the
migration plan from the Python harness, but ALS-RS is now the active runtime and
the document describes its current architecture, contracts, operational rules,
known limitations, and migration history.

Keep this document evergreen:

- patch it manually when a durable implementation contract changes
- describe current behavior in the main chapters
- move completed or superseded migration detail into the history appendix
- cite tracked source whenever possible
- do not use deleted source trees, temporary worktrees, cache logs, or old user
  messages as the sole authority for current behavior
- keep `.repo_memory.md` compact by storing only high-frequency invariants there
  and linking agents back to the relevant chapters in this reference

Both files are registered with the repo KB. Agents should use `kb_search`,
`kb_info`, and `kb_read` to retrieve detail from this reference rather than
injecting the whole document into every prompt.

## Source hierarchy

Current implementation authority is ordered as follows:

1. `rust/crates/als-server/src/` for the active server, persistence, Socket.IO,
   TE2 integration, and browser source under `static/`.
2. `rust/crates/als-adapter-protocol/`, `rust/crates/als-dto/`, and
   `rust/crates/als-jsonrpc/` for shared Rust DTO/protocol contracts.
3. `agent_log_server_rs/` for Python bootstrap and the generic extension adapter.
4. `extensions/` for provider-owned behavior, schemas, manifests, and assets.
5. `als_deprecated/` only when explicitly documenting legacy parity or the
   retained `codex-agent` compatibility entrypoint.

Paths under the removed `agent_log_server/` package are not active ALS-RS
sources. Temporary `worktrees/` content and runtime cache/log paths may explain
history, but they are not current contract authority.

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
     Python harness data roots, except explicit import/migration tools and the
     deliberate shared IPC credential at `~/.cache/app_server/ipc_secret` used
     by `agent-pty-blocks.ask_user`.

3. **Python remains the install/bootstrap surface**
   - `pip install git+...@als-rs` installs the Python package and packaged Rust
     workspace.
   - The console entrypoint is a thin Python bootstrap, not the server.
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
   - Python networking is absent from the ALS-RS runtime path.

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
   - Rust owns the transcript implementation while preserving the generic
     persisted/live/replay card contract used by extension routers and the
     frontend.
   - Performance work may change projection, indexing, and browser retention,
     but not the semantic transcript rows or live/replay parity contract.

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
        | sets ALS_RS_* env, starts Rust
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
- ALS-RS-owned secrets should be generated under the ALS-RS data/cache roots.
  The ask-user IPC secret is an intentional cross-harness exception at
  `~/.cache/app_server/ipc_secret`. Framework-shell control secrets are also
  different:
  when ALS-RS is launched inside FWS, adapter child processes must preserve the
  parent `FRAMEWORK_SHELLS_*` environment so existing extension-owned
  framework-shell process management and observability keep working.
- Health is Rust-owned at `GET /api/health`.
- TE2 wrapper readiness uses the ALS-RS port and health route.

## Python package and bootstrap particulars

The package remains setuptools-based and packages the Rust workspace for
source-build launches. Package and console script identity:

```toml
[project]
name = "agent-log-server"

[project.scripts]
als-rs = "agent_log_server_rs.bootstrap:main"
als-rs-extension-adapter = "agent_log_server_rs.adapters.extension_adapter:main"
```

The installed console script must not collide with the current `codex-agent`
command. ALS-RS exposes only the generic `als-rs-extension-adapter` entrypoint;
provider-specific adapter console scripts are not part of the current runtime.

Bootstrap responsibilities:

1. Parse CLI options/env for host, port, root overrides, foreground mode, and
   adapter selection.
2. Resolve isolated roots and export `ALS_RS_*`.
3. Ensure required directories exist.
4. Locate the Rust server source/binary:
   - explicit `ALS_RS_SERVER_BIN` / `--server-bin` when provided
   - source-checkout `rust/Cargo.toml` during development
   - packaged `agent_log_server_rs/rust/Cargo.toml` copied into built wheels
     (this generated package path is not present in a source checkout)
   - development binary under the active Cargo target dir when present
   - otherwise `cargo run -p als-server`
   Packaged wheel launches set `CARGO_TARGET_DIR` under the ALS-RS cache root so
   cargo does not write build output into site-packages.
   Future release/optimized binary support should be an explicit opt-in
   bootstrap mode such as `--release` or `ALS_RS_CARGO_PROFILE=release`, not
   the default development path. The first release build will create a
   separate `target/release` tree and can take materially longer on Termux, so
   keep the initial implementation profile-only and avoid enabling LTO or
   heavier size-optimization settings until the compile-time and disk impact
   is measured in this checkout.
   Bootstrap also exports `ALS_RS_EXTENSIONS_DIR` to the packaged/source
   builtin `extensions/` root so Rust discovery does not rely on Cargo's
   rust-workspace-relative default.
5. Launch the Rust server as a subprocess.
6. Let Rust launch and own the extension adapter subprocess. The bootstrap
   supplies the interpreter, roots, and inherited framework-shell environment.
7. Forward SIGINT/SIGTERM to child processes.
8. Exit with the Rust server exit code unless bootstrap startup fails first.

Bootstrap should stay intentionally thin. If logic starts looking like routing,
state management, or event translation, it belongs in Rust or the adapter.

## Rust workspace particulars

Current workspace shape:

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
  - owns the optional native stdio transport/introspection/orchestration layer
    so observed adapter process lifecycle and pipe plumbing stay outside
    `als-server`.

Core Rust server surfaces:

- `GET /`
- static asset routes
- `GET /manifest.json`
- `GET /api/health`
- `/rpc/conversations` Socket.IO namespace
- `/rpc/settings` Socket.IO namespace
- `/rpc/ui` Socket.IO namespace
- live conversation notification fanout

## Migration execution strategy (historical)

This section records the strategy used to create the Rust runtime. It is
historical guidance, not the current source hierarchy. References below to
Python contract files that no longer exist describe the parity inputs used
during migration; current work must follow the active sources listed above.

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

## Extension adapter protocol and transport

There is no built-in provider runtime fallback. Active provider behavior comes
from the registered `codex-ext` and `copilot-sdk` packages behind the generic
adapter boundary.

Selected first protocol/transport: newline-delimited JSON-RPC 2.0 over adapter
stdin/stdout. The protocol shape remains transport-disposable: stdio can be
replaced later by Unix sockets, framework-shell pipes, PyO3/shared-memory, or
another byte channel without changing DTO method/event names.

Current observed transport direction: keep the JSON-RPC DTO shape, but move the
adapter process under the native `ferrous_framework` stdio transport
introspection/orchestration layer when framework-shell context is present. ALS-RS
uses Ferrous' async native pipe facade for adapter writes and a single
ALS-owned reader task over Ferrous stdout bytes/chunks for JSON-RPC line
framing. The adapter JSON-RPC stream must never be forwarded through the Rust
CLI stdout. Default ALS-RS builds retain the direct child stdin/stdout fallback
when Ferrous cannot start.

The adapter shellspec is:

```text
agent_log_server_rs/shellspec/extension_adapter.yaml
```

For source-checkout launches, the Python bootstrap still forces `cargo run` when
framework-shell context is present so a stale `target/debug/als-server` binary
does not hide adapter transport changes. There is no ALS-side PyO3 feature gate
or PyO3 interpreter selection for this path anymore.

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
- Conversation start/resume/send params include a server-owned
  `devins_context` DTO. Rust builds the effective developer-instructions string
  from the request settings, TE2 developer template, and fresh `.repo_memory.md`
  snapshot; the Python adapter injects that DTO into extension settings under
  `__als_devins_context__`, and extensions use local `devins_contract.py`
  modules to place the already-computed string into provider-native fields.
Validation currently covers:

- Rust unit tests for live event and transcript record serialization.
- Python mirror smoke test for the same `assistant_delta` and `assistant`
  transcript shapes.

## Generic extension adapter and runtime support

The Rust runtime path uses the generic adapter:

- `agent_log_server_rs/adapters/extension_adapter.py`
- console script: `als-rs-extension-adapter`
- spawned by Rust as `python -m agent_log_server_rs.adapters.extension_adapter`

The generic adapter initializes the existing Python extension loader
(`extensions.load_extensions(...)`) with ALS-RS callbacks and dispatches methods
by `extension_id`. Runtime support is provider-neutral at the adapter boundary:
`copilot-sdk` and `codex-ext` both use the same JSON-RPC DTO lane, while
provider-native session, model, plan, approval, interrupt, MCP, and
event-translation behavior remains inside extension packages. `codex-ext-exp`
has been removed from the active registry/runtime surface.

Supported runtime scope:

| Method | Behavior |
| --- | --- |
| `extension.initialize` | Initializes the loader, returns capabilities/runtime metadata for the selected extension, and applies the current ALS-RS registry/enablement view. |
| `extension.list_models` | Delegates to the selected extension's `list_models()` hook through the generic loader and returns normalized model metadata. |
| `extension.list_sessions` | Delegates to the selected extension's `list_sessions()` hook through the generic loader for schema session/rollout picker data. |
| `extension.get_runtime_options` | Delegates to the loader's schema/provider runtime-option merger for footer controls and quick runtime state. |
| `extension.get_provider_info` | Delegates provider status/usage reads to extension-owned account/quota logic. |
| `extension.schema_interaction.run` | Runs provider-owned schema interactions such as provider auth, model search, and config writes. |
| `extension.get_plan` | Reads provider-owned planning/todo state through the generic plan contract and returns the frontend header/overlay shape. |
| `extension.package.validate` | Validates a path/zip/git extension source through the existing Python installer helpers. |
| `extension.package.install` | Installs a third-party extension into `${ALS_RS_DATA_DIR}/extensions`, updates that root's `extensions.json`, then reloads Rust and adapter registry state. |
| `extension.package.update` | Updates a user-installed extension from an explicit or recorded source, then reloads Rust and adapter registry state. |
| `extension.package.remove` | Removes only user-installed extensions; builtin extensions are refused. |
| `conversation.start` | Starts provider sessions through the selected extension's `init_session()` hook and returns `ConversationAckResult`. |
| `conversation.resume` | Resumes/imports a provider-bound conversation through extension-owned resume/hydration hooks. |
| `conversation.fork` | Forks a provider conversation when the extension advertises the capability, then lets Rust copy local transcript state. |
| `conversation.send` | Sends turns through the selected extension's `handle_message()` hook and returns `ConversationAckResult`. |
| `conversation.interrupt` | Delegates interrupt/abort to extension-owned runtime logic. |
| `approval.respond` | Delegates provider approval responses through the extension loader; MCP ask-user responses are bridged by ALS-RS IPC before handoff persistence. |
| `extension.shutdown` | Stops supported extension handlers. |

First physical transport:

- newline-delimited JSON-RPC 2.0 over adapter stdin/stdout
- one JSON object per line
- Rust can later replace this transport without changing adapter DTO names
- live events from extension routers are forwarded as `event.live`
  notifications
- transcript writes are forwarded as `event.transcript_record` notifications

The original Copilot pilot proved the process/DTO boundary by reusing an existing
extension client through the loader instead of calling the provider SDK directly.
That pattern is now the live contract: Rust owns durable conversation metadata
and transcript storage, while the adapter receives conversation/provider
identifiers from Rust and emits normalized live events / transcript records back
to Rust.

## Rust adapter runtime wiring

The first Rust-side adapter runtime lives in:

- `rust/crates/als-server/src/adapter_process.rs`
- `rust/crates/als-server/src/settings_rpc.rs`
- `rust/crates/als-server/src/conversation_rpc.rs`
- `rust/crates/als-server/src/extension_routes.rs`

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
- If framework-shell config is present, `als-server` prefers the
  `ferrous_framework` transport: a native async manager/pipe facade that starts
  the adapter as an observed native-pipe shell and uses direct async
  `write_to_shell(..., append_newline=true)` for JSON-RPC requests.
  The shell shape comes from `agent_log_server_rs/shellspec/extension_adapter.yaml`.
- ALS-RS owns JSON-RPC line framing over the Ferrous byte/chunk read path;
  Ferrous owns process launch, stdin writes, stdout/stderr logs, FWS-compatible
  records/capabilities, and shell termination.
- The current Ferrous pin also exposes an FWS Socket.IO peer/control-plane lane
  (`native_peer`, `peer_protocol`, `/fws_ws/socket.io` on namespace `/fws`).
  ALS-RS does not use that lane for extension-adapter JSON-RPC; it remains an
  introspection/control-plane capability of Ferrous, separate from ALS's adapter
  protocol.
- The current Ferrous pin is `5f9e0de` / `0.2.7`; it also includes native
  lifecycle event subscriptions and procfs-backed tree shutdown. Those are
  Ferrous/FWS control-plane semantics and do not change ALS-RS adapter
  request/response framing.
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

Current route/RPC surfaces:

| Surface | Purpose |
| --- | --- |
| `/rpc/settings` | Extension registry, schema, runtime options, provider info, session/model lists, schema interactions, package operations, and plan reads. |
| `/rpc/conversations` | Conversation create/list/get/select/update/delete/pin/draft/replay/send/fork/approval/interrupt and live event fanout. |
| `POST /api/extensions/{extension_id}/reload` | Targeted extension reload through the safe adapter reload path after registry verification. |
| `extension.debug.probe` | Redacted Rust/adapter/FWS diagnostics, including recent adapter event snapshots. |

Validation currently covers:

- Rust unit tests for JSON-RPC response correlation by request id.
- Rust unit tests for `event.live` and `event.transcript_record` capture.
- Focused Python py_compile/basedpyright checks for the adapter executable and
  DTO mirror.
- Live Rust smoke tests against temporary ALS-RS servers:
  - `/rpc/settings` registry metadata, request cards, and UI features
  - `/rpc/settings` extension schema/model/session/runtime-option/plan surfaces
  - `/rpc/conversations` extension-backed send with assistant finalization and
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
| Runnable support | `copilot-sdk` and `codex-ext` are live through the generic adapter for model/conversation/session/runtime-option/plan surfaces. Unsupported extension types remain metadata-visible and return generic unsupported errors for runtime methods they do not implement. |
| Enablement overlay | `extension.enabled.set` persists ALS-RS-local enablement state in `${ALS_RS_CONFIG_DIR}/extensions_state.json`, updates Rust registry active state, and asks a running adapter loader to reload. It does not mutate extension package manifests or `extensions.json`. |
| Dependency/readiness mediation | A running adapter reload applies Rust enablement overrides to the Python loader, calls extension-owned dependency checks, merges `dependency_status`, `dependency_ok`, `dependency_message`, and `dependency_details` back into Rust registry state, and waits for readiness when enabling an active extension. |
| Dependency install | `extension.install` routes through the generic adapter to extension-owned `install_extension_dependencies()`, then rechecks dependencies and merges the resulting runtime state back into the Rust registry. |
| Codex model discovery | The generic adapter now supplies a real framework-shell manager callback to the Python extension loader, so `codex-ext` model listing can use its existing FWS-owned app-server transport instead of being metadata-only. |
| FWS runtime adoption | The TE2 bootstrap now passes framework-shell runtime identity to `als-server` as explicit CLI arguments, Rust stores those values in `ServerConfig`, and adapter spawn forwards that config-backed environment to Python. Env fallback remains for standalone/manual launches. |
| Extension debug probe | `/rpc/settings` method `extension.debug.probe` now returns a redacted Rust/adapter/FWS diagnostic snapshot, with optional `ensure_manager: true` to force framework-shell manager creation/adoption without launching provider model commands. |
| Extension shell grouping | ALS-RS-launched extension shellspecs use the `als-rs` framework-shell subgroup instead of the legacy `codex_agent` subgroup, while preserving active provider groups such as `codex` and `copilot`. |
| Extension warmup | ALS-RS starts a non-blocking background warmup task at router startup. The task initializes the Python adapter, applies Rust enablement overrides, runs dependency checks, and calls extension-owned `warm_up_extensions()` for active extensions. |
| Copilot pipe readiness | Copilot FWS pipe adoption now waits briefly for framework-shell pipe state/stdin after shell launch instead of failing immediately with `copilot sdk pipe not available`. |
| Copilot shell adoption | Copilot FWS shell adoption now ignores stale shell records whose `app_id`/subgroups do not match the current TE app, preventing ALS-RS from attaching to the older `codex_agent` Copilot stdio shell. |

Remaining lifecycle limitations:

- `extensions.reload` now rescans disk into the mutable Rust registry and asks a
  running Python adapter loader to `extension.reload` with `force=true`. If the
  adapter has not been spawned yet, the next adapter initialize loads from the
  current extension root.
- `extension.enabled.set`, `extension.install`, dependency checks/readiness, and
  targeted reload are implemented through the Rust registry plus Python adapter
  loader contract.
- `extension.session.bind` still returns an explicit not-implemented response.
  Provider session binding currently happens through conversation create/update
  normalization and adapter resume/hydration.
- Python extension handlers are initialized coarsely inside one long-lived adapter
  process rather than per-extension process ownership.

## Rust conversation persistence

The Rust-owned conversation store lives in:

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
| `conversation.replay.getChunk` | Returns raw JSONL chunks for explicit raw reads, or backend-owned 75-card projection recipes plus runtime state for transcript UI navigation. |
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

Current implementation:

- Full async assistant fanout from adapter `event.live` into `/rpc/conversations`
  `rpc.notify` is implemented through the generic adapter event sink. The fanout
  task persists adapter transcript records under ALS-RS conversation roots and
  skips duplicate adapter user echoes because Rust owns local live user rows.
- Adapter fanout now uses a lossless subscription for the main conversation
  persistence path, so debug snapshots can stay bounded without becoming the
  authoritative event stream.
- Replay hydration now supports JSONL chunk frames over RPC without changing the
  persisted `transcript.jsonl` format.
- `conversation.approval.respond` is implemented for provider approvals and the
  ALS-RS `/ipc` MCP ask-user bridge.
- `conversation.interrupt` is implemented through the generic adapter
  `conversation.interrupt` hook and extension-owned abort logic.
- `conversation.compact` and `conversation.shell.exec` still return explicit
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
- The Rust router owns one best-effort TE2 `/sidebar_ipc` client. It registers
  the ALS-RS app/capabilities and speaks typed JSON-RPC over `rpc` /
  `rpc.notify`: `hostUi.recheck` calls `sidebar.cwd.get`, file navigation calls
  `sidebar.file.open`, and incoming notifications include `sidebar.cwd.set`,
  `sidebar.mention`, `sidebar.window.focused`, and inline agent-edit document
  state. Narrow legacy `sidebar:*` handlers remain only as a cutover fallback.
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

## Current capability and limitation matrix

| Area | Current owner and status |
| --- | --- |
| HTTP/static/networking | Rust/Axum/socketioxide. Browser and private IPC namespaces default to MessagePack Socket.IO. |
| Conversations | Rust owns metadata, list snapshots, transcript persistence/replay, projection cursors, draft state, approvals, and provider binding. |
| Provider runtimes | `codex-ext` and `copilot-sdk` are active through one generic Python adapter process. |
| Settings | Rust reads extension `settings_schema.json`; provider data and actions route through generic adapter methods. |
| TE2 integration | Rust owns readiness, typed sidebar IPC, stateful window checkpoints, file navigation, mentions, drafts, and inline edit projections. |
| Project/edit review | Rust owns git summaries/actions, tracked agent edits, reverse patching, sequential reject-all, and inline review state. |
| Compaction | `conversation.compact` remains an explicit ALS-RS not-implemented control. Provider protocol support alone does not make the harness method available. |
| Shell execution | `conversation.shell.exec` remains explicitly not implemented. Provider shell/tool cards still arrive through normalized router events. |
| Provider isolation | One long-lived adapter currently initializes all active Python extension handlers. Per-extension adapter processes remain an optional future isolation design. |
| Embedded Python adapter | `embedded_adapter.rs` is retained behind `cfg(feature = "embedded-python-adapter")`, but no Cargo feature/dependency enables it. The active path is the subprocess adapter; the dormant source is a cleanup or future-design artifact. |

## Browser transport and RPC contracts

ALS-RS uses Socket.IO namespaces as its browser control plane with
JSON-RPC-shaped DTO envelopes:

- request event: `rpc`
- response: Socket.IO ack containing JSON-RPC success/error
- notification event: `rpc.notify`
- primary namespaces: `/rpc/conversations`, `/rpc/settings`, `/rpc/ui`
- private MCP bridge: `/ipc`

The Socket.IO parser defaults to MessagePack for all server/browser/adapter
clients. `AGENT_LOG_SOCKETIO_SERIALIZER=json` is the launch-owned debugging
override and must switch the entire lane together. MessagePack changes the
Socket.IO payload codec; the request, result, error, and notification objects
remain JSON-RPC shaped.

Transcript transfer is a separate binary data plane. By default, the browser
opens `/ws/transcript` with one page-lifetime client ID shared with composer
authorship. The WebSocket accepts only versioned tagged MessagePack frames and
serves transcript windows plus selected-conversation live/projection events.
Large server frames use negotiated gzip. One logical client owns one current
stream connection and one selected conversation; a newer connection supersedes
the old one, and bounded outbound queues force reconnect/resync if a client
falls behind. The browser uses `reconnecting-websocket` with no stale send
queue.

`ALS_RS_TRANSCRIPT_TRANSPORT=rpc` is the explicit transcript debugging mode. It
routes projection requests and transcript notifications through the existing
Socket.IO JSON-RPC lane. There is no runtime fallback or duplicate delivery
between the two modes. Conversation lists, drafts/selections, mentions, metadata,
settings, UI controls, and private MCP IPC remain on their existing Socket.IO
namespaces in stream mode. Setting both transcript transport to `rpc` and
`AGENT_LOG_SOCKETIO_SERIALIZER=json` provides a fully JSON-readable browser
debug lane.

Frontend params are normalized to the JSON value domain before MessagePack
encoding: undefined object fields are omitted and undefined array values become
null. Request builders should omit absent optional fields. Parser selection is
rendered into browser pages, propagated to the extension adapter, and copied into
extension-launched `agent-pty-blocks` environments.

Source anchors:

- `rust/crates/als-server/src/config.rs`
- `rust/crates/als-server/src/http.rs`
- `rust/crates/als-server/src/socketio.rs`
- `rust/crates/als-server/src/transcript_stream.rs`
- `rust/crates/als-server/src/static/js/codex_agent/transcript_stream.ts`
- `rust/crates/als-server/src/static/js/codex_agent/client_identity.ts`
- `rust/crates/als-server/src/static/js/codex_agent/rpc/transport.ts`
- `rust/crates/als-server/src/static/js/socketio_runtime.js`

## Conversation, transcript, and lifecycle contracts

### Transcript-first reload and provider resume

Existing harness conversations hydrate from local `transcript.jsonl`. Merely
selecting or reloading a conversation does not eagerly attach the bound provider
session. The first new live action probes the provider; a cold-session miss
triggers extension-owned resume, resume success is the ack boundary, and the
original action retries once.

Codex reattach uses `thread/resume` with `excludeTurns: true`. Provider transcript
hydration is a separate import path used when a new local conversation explicitly
binds an existing provider session. Copilot follows the same generic cold-miss,
resume, ack, and retry lifecycle with provider-native calls.

`provider_session_id` is the canonical provider binding. Compatibility
`thread_id` / `session_id` aliases may be accepted at adapter boundaries, but
`conversation_id` is the local harness identity and must never be persisted as a
provider session id.

Provider branching is exposed through generic `conversation.fork`. Rust
allocates the target local conversation, asks the selected extension to fork its
provider session, copies local transcript rows while rewriting conversation
identity, and clears draft/pending approval state. Codex implements the provider
leg with `thread/fork` and `excludeTurns: true`.

Source anchors:

- `rust/crates/als-server/src/conversation_rpc.rs`
- `rust/crates/als-server/src/conversation_store.rs`
- `agent_log_server_rs/adapters/extension_adapter.py`
- `extensions/codex_ext/client.py`
- `extensions/copilot_sdk/client.py`
- `AGENT_EXTENSION_INTEGRATION.md`

### Transcript mirror and visible cards

Extension routers must preserve live/replay parity: every replayable live event
has a transcript record carrying the same fields. Visible user-facing errors use
live `type: "error"` and transcript `role: "error"`; `rpc_error` is reserved for
pending RPC failures. Token/footer metadata is backend-owned and includes the
context window.

Apply-patch/file-change tool rows are summary/outcome/path cards. Standalone
generic `diff` rows own visible patch bodies. Raw new-file content received in a
provider field named `diff` must be synthesized into a real `/dev/null` unified
diff before emit or persistence; arbitrary text must never be header-wrapped into
a fake patch.

Canonical card details live in `TRANSCRIPT_CARD_CONTRACTS.md`; approvals live in
`APPROVAL_CONTRACT.md`; planning DTOs live in
`EXTENSION_PLANNING_CONTRACT.md`.

### Rust-owned transcript projection

ALS-RS owns durable transcript position as a process-local card cursor keyed
by logical page client and conversation. The browser requests `tail`, `older`,
`newer`, or `current`; Rust serves a bounded 75-card window with 20-card
shifts. These units are deliberately distinct:

- a raw transcript record is one append-only JSONL object
- a transcript card is one user-visible semantic item and may reduce several
  records; state-only and hidden records produce no card
- a layout row is a mounted card root or virtual placeholder and is only browser
  geometry

A cached line-offset plus card index classifies records as create, update,
runtime state, or hidden. It preserves and updates explicit card IDs, generates
stable IDs when needed, reduces repeated family/source snapshots to their
latest record, groups agent-PTY and subagent lifecycles, and includes required
parent cards for nested recipes. Raw line counts remain diagnostics only.
Projection responses use `projection.unit: "transcript_card"`; they carry
card-unit bounds, total and window counts, authoritative `at_start`/`at_tail`
flags, ordered recipes, and separate `runtime_state[]`. Every durable recipe has
a monotonic per-card `version`, and every durable recipe event carries
authoritative card ID/index/version/operation/scope metadata. Process-local turn
recipes retain the card ID/index/operation/scope envelope and use scope `active`.
A byte-budget overflow is an explicit error, never a partial card window.

The Socket.IO RPC debug mode returns complete `frame.format: "card_recipes"`
windows. The default binary stream returns one complete snapshot for initial
hydration or forced resync, then sends deltas containing authoritative ordered
card IDs/indexes/versions, removed card IDs, and only new or version-changed
recipes. A normal 20-card shift therefore does not resend the 55 overlapping
recipes in a 75-card window. Reconnect sends the last known bounds and card
versions; `current` preserves a detached cursor, and a server restart or sequence
gap requests an authoritative snapshot at that position. Transcript reads batch
the selected indexed JSONL lines in file order and avoid serializing every event
back to JSON merely to estimate the response budget.

Durable `transcript.jsonl` remains append-only and final-event-only. Rust also
owns a separate process-local active-turn projection for in-flight assistant,
reasoning, command, tool, subagent, diff, view, search, plan, notice, and
compaction events:

- `conversation.send` starts a new projection generation.
- Deltas reduce into stable item recipes with family-qualified render keys.
  Family-neutral turn/subagent/provider-item lineage lets a specialized family
  replace a provisional generic tool alias without moving its sequence. Shell
  deltas synthesize a replayable `shell_begin + shell_delta` recipe when needed.
- Active snapshots and outgoing live events carry the same authoritative card
  identity envelope as durable recipes. Nested active items identify their
  parent card.
- The store is bounded to 96 active items, 512 KiB per accumulated delta stream,
  and 256 recent durable lineages used to suppress transcript-first/live-final
  reordering.
- Adapter transcript append and card projection take the same per-conversation
  lock. Replay therefore pairs one durable card window with the matching
  `live_projection` snapshot.
- A matching durable row retires every active family alias sharing its lineage.
  Late generic aliases remain tombstoned, while unfinished items remain
  available for hydration. A durable terminal status clears orphaned items.
- `conversation.projection.updated` carries only generation, revision, item
  count, and truncation state. Full active recipes travel only in replay.

The frontend consumes recipes rather than reparsing raw projection JSONL. It
requires exactly one rendered card root for each durable recipe. A renderer
mismatch becomes one visible projection-error root at that card index instead
of aborting a window replacement. Search/view replay preserves projection
metadata. History navigation uses the backend `at_start`/`at_tail` state;
browser code never advances the server cursor from DOM counts, raw order IDs,
or pruning.

While pinned, the browser remains a normal live view. Events append in arrival
order and completed card roots prune toward the 75-card display budget without
mutating the backend cursor; unresolved approvals and active rows are retained.
While detached, ordinary live cards buffer, but status, token, mode, and
blocking approval lifecycle surfaces remain current. Initial/reconnect
hydration, detached history, newer-to-tail, and true re-pin combine the durable
card window with active recipes.

Within that bounded card projection, the Pretext hybrid virtualizer:

- prepares finalized user/assistant Markdown geometry once fonts are ready
- recomputes predicted heights when conversation width changes
- uses prefix-summed row positions and viewport overscan
- parks stable offscreen rows in fixed-height identity-preserving placeholders
- keeps streaming messages, unresolved approvals, plans, and expanded subagents
  mounted
- parks diffs, terminals, tools, and other stable cards only after measurement
- preserves a card-ID scroll anchor numerically during relayout or window shifts
- does not restore collapse state from localStorage; reconstructed normal and
  synthetic subagents start collapsed
- treats collapse/expand as an anchored measurement transaction and retains
  width-and-state-specific heights only across projection replacement, clearing
  them on a full conversation reset

Source anchors:

- `rust/crates/als-server/src/transcript_card_projection.rs`
- `rust/crates/als-server/src/turn_projection.rs`
- `rust/crates/als-server/src/conversation_store.rs`
- `rust/crates/als-server/src/conversation_rpc.rs`
- `rust/crates/als-server/src/transcript_stream.rs`
- `rust/crates/als-server/src/static/js/codex_agent/transcript_stream.ts`
- `rust/crates/als-server/src/static/js/codex_agent/transcript_loader.ts`
- `rust/crates/als-server/src/static/js/codex_agent/rpc/conversations/contract.ts`
- `rust/crates/als-server/src/static/js/codex_agent/timeline/virtualizer.ts`
- `rust/crates/als-server/src/static/js/codex_agent/timeline/replay.ts`
- `rust/crates/als-server/src/static/js/codex_agent/subagents/collapsible.ts`

#### Frontend window motion

The frontend shifts durable history only when both the visible durable
card-index range and measured prefix-summed geometry reach an edge runway.
Active-turn and unscoped live-only rows remain in layout but do not participate
in durable cursor math; hidden descendants of collapsed cards are not treated
as visible. The six-card trigger zone is separated from its 12-card re-arm
boundary. Edge and live-tail runways are two viewports with a 640px floor; a
four-viewport/1280px physical distance can also re-arm a direction. This keeps
small upward motion inside the live-tail buffer from requesting history while
still preserving the 20-card server shift and 75-card window.

A projection transition remains both loading and programmatic through recipe
rendering, card-ID anchor restoration, and settled virtualizer frames. Scroll
deltas produced by that transition are discarded before another edge can
trigger. The structural top spacer remains zero-height; the bounded Rust card
window is the history buffer.

Projection replacement preserves the IDs of cards currently expanded in
memory and reopens only matching cards that remain in the next window. This
state does not survive conversation reset or reload and is never stored in
localStorage. The virtualizer debug snapshot is exposed as
`CodexAgent.helpers.getTranscriptProjectionDebugSnapshot()` for console
inspection of server bounds, logical records, mounted/parked counts, visible
card indices, scroll geometry, selected transport, stream connection/cache
state, and the last received stream sequence.

### Conversation list resynchronization

Rust broadcasts complete `conversation.list.updated` snapshots with monotonic
revisions after list-affecting mutations. Frontends replace local splash/drawer
state and drop stale revisions. Reconnect starts a new local revision epoch,
fetches canonical `conversation.list`, and lets a newer live snapshot win races.
This permits a restarted Rust process whose revision reset to replace stale
browser state.

Unreadable conversation `meta.json` files become non-actionable
`integrity: "corrupt"` diagnostics while healthy conversations remain visible.
A whole-list RPC failure renders an explicit list error instead of pretending
the list is empty or mutating damaged data.

### Multi-client composer ownership

Rust owns canonical draft text, persisted draft revision, and selection state.
A focused client claims authorship through `conversation.draft.author.claim`.
Draft and selection mutations are accepted only from the owning socket/client,
the current server-issued author epoch, and a newer client sequence.

Selections are serialized against canonical text and restored after rendering.
Clients suppress stale same-origin updates and remote-apply selection echoes.
Disconnect or server-side fallback invalidates ownership.

Sidebar mentions target one live author socket with a unique operation id. With
no live author, Rust appends one canonical mention to persisted draft state,
moves the stored selection to its end, and broadcasts the authoritative update.

Source anchors:

- `rust/crates/als-server/src/composer_sync.rs`
- `rust/crates/als-server/src/conversation_rpc.rs`
- `rust/crates/als-server/src/sidebar_ipc.rs`
- `rust/crates/als-server/src/static/js/codex_agent/composer/runtime.ts`

## Approval and MCP interaction contracts

### Generic approvals

Live generic `type: "approval"` events persist into
`meta.pending_approvals`. `conversation.approval.respond` routes provider
approvals through adapter `approval.respond`; successful resolution appends a
`role: "approval"` handoff row, removes the pending descriptor, and emits
`conversation.approval.handoff`.

`pending_approvals_revision` advances on every insert, replacement, update, or
removal. Descriptors, nested render events, live requests, and invalidations carry
that revision. Frontends reconcile pending restore and live state by backend
revision rather than arrival order.

Extension `manifest.json` `ui.requestCards[]` declarations own custom approval
modules. Rust serves only manifest-local `ui/...` or `static/...` assets and the
frontend loads them through the generic request-card runtime.

### `agent-pty-blocks.ask_user`

ALS-RS owns the private `/ipc` lifecycle. The MCP process authenticates with
`~/.cache/app_server/ipc_secret`, registers through `ask_user_begin`, receives
`ask_user_response` or `ask_user_terminal`, and completes successful delivery
with `ask_user_ack`.

- `requestor_id` is stable conversation identity from `CONVERSATION_ID`
- `request_id` is random invocation identity
- one live request is reserved per requestor in the MCP process
- replacing an abandoned request invalidates the old card and terminates its
  waiter
- only the exact live owner/request can accept a response
- pending descriptors are backend-owned and do not depend on frontend
  availability
- unresolved cards use backend-owned unresolved ordering and receive their real
  positive transcript order only on handoff

Codex injection forces `supports_parallel_tool_calls = false`, advertises
`readOnlyHint = false`, and applies at least a one-hour tool timeout. This is the
strongest available standalone-tool scheduling guardrail, but it is not a
protocol-level semantic barrier; developer instructions still direct agents not
to parallelize `ask_user`.

Codex and Copilot suppress the generic MCP tool row for
`agent-pty-blocks.ask_user`; Rust owns the actual pending card. Provider-native
request-user-input tools remain normal provider approvals.

The same private IPC surface exposes TE2 sidebar draft preflight tools. Agents
should list project drafts before multi-file edits, inspect only relevant dirty
files, and never clear user draft state without explicit approval.

Source anchors:

- `rust/crates/als-server/src/ipc.rs`
- `rust/crates/als-server/src/conversation_rpc.rs`
- `mcp_agent_pty_server.py`
- `extensions/codex_ext/runtime_protocol.py`
- `APPROVAL_CONTRACT.md`

## Settings, schema, and provider runtime contracts

Rust reads each registered extension's `settings_schema.json` directly from the
entry-owned extension root. Missing files return an empty schema with
`schema_source: "missing_file"`. Rust also serves guarded extension-root-relative
schema fragments; absolute paths, parent traversal, invalid prefixes, and symlink
escape are rejected.

The shared schema renderer is provider-neutral:

- dynamic fields declare source methods, params, response paths, option
  value/label/detail paths, dependencies, and defaults
- dependent selects use `dynamic_options_from`; provider model shapes are not
  hardcoded in the frontend
- `write_back.on_select` can copy selected option metadata into ordinary fields
  and is non-destructive unless clearing is explicitly enabled
- `visible_if`, `enabled_if`, and `clear_when_hidden` control conditional rows
- `submenu` / `group` fields can contain inline fields or lazy schema fragments
- `interaction` fields submit extension-owned actions through
  `extension.schemaInteraction.run`
- explicit `inputs: []` means a button-only interaction
- interaction result actions can refresh/upsert/select options, apply
  write-back, change submenu state, mark dirty, or open validated HTTP(S) URLs
- `persist: false`, transient, secret, and sensitive fields never persist their
  values into conversation metadata
- large selects use the shared searchable option picker, not a native browser
  dropdown

Runtime quick controls, provider status, and provider usage are schema-declared
semantics. Rust resolves active conversation metadata and calls generic adapter
methods such as `extension.get_runtime_options` and
`extension.get_provider_info`; extension code owns provider-native calls and
returns normalized DTOs.

Provider session import is extension-owned. The schema picker persists the
provider binding and Rust calls generic resume/hydration. Rust stores normalized
transcript rows and IDs but never parses provider-native rollout/session
semantics.

Conversation Settings and Project share one static tabbed modal shell. The
shell controller owns visibility, tab accessibility, and close routing while the
settings schema renderer and Project renderer retain ownership of their panel
content and actions. Existing `openSettingsModal()` and `openProjectModal()`
entry points select the corresponding tab. Settings form values remain mounted
across tab switches, and Project is disabled while new-conversation setup is
pending so the required agent/CWD flow cannot be bypassed.

Source anchors:

- `rust/crates/als-server/src/settings_rpc.rs`
- `rust/crates/als-server/src/static/js/codex_agent/conversation_modal.ts`
- `rust/crates/als-server/src/static/modals/settings_schema.ts`
- `rust/crates/als-server/src/static/js/codex_agent/runtime_footer.ts`
- `agent_log_server_rs/adapters/extension_adapter.py`
- `extensions/codex_ext/settings_schema.json`
- `extensions/copilot_sdk/settings_schema.json`
- `worktrees/notes/SCHEMA_SETTINGS_CONTRACT.md`

## TE2, frontend, and project UI contracts

### Frontend ownership and build

The active entrypoint is
`rust/crates/als-server/src/static/codex_agent.ts`; `build.mjs` bundles it to
`rust/crates/als-server/src/static/dist/codex_agent.js`. The root file is
orchestration glue; behavior belongs in the extracted modules under
`static/js/codex_agent/`.

TypeScript is strict and first-party source disallows explicit `any`. Frontend
changes are not live until both validation commands succeed:

```bash
npm run typecheck --quiet
npm run build --quiet
```

Finalized assistant/user Markdown is rendered through markdown-it; SMD is only
the live assistant streaming renderer. Markdown file links route through TE2
file open with line/column parsing. HTTP(S) links route through the validated
backend `url.open`/`xdg-open` path. Shell cards use the shared ANSI renderer only
when escape sequences are present. Highlight.js registers `highlightjs-diff` on
the shared core instance after inner languages.

### Stateful sidebar windows

ALS-RS supports TE2 stateful conversation windows only when TE2 supplies
`te2_host_id`. Cold slot hydration reads query state, performs explicit
`conversation.get`, and replays locally without calling global
`conversation.select`.

User-driven switching can publish a new checkpoint through `/rpc/ui`
`app.windowState.publish`; Rust validates host/conversation identity and asks TE2
to store the restore URL. `sidebar.window.focused` updates only a process-local
focus hint used for mention targeting. App readiness and window checkpointing are
separate contracts.

### Project modal and inline agent edits

`/rpc/ui` `project.summary.get` computes git summary/status/diff data with
`git2` in `spawn_blocking`. The Project modal exposes generic stage, unstage,
restore, and commit RPCs and tracks category/diff expansion state in browser
storage. Live project notifications schedule summary refresh only while the
shared modal's Project tab is active.

Conversation-scoped agent edits are Rust-owned and process-local. Canonical live
`type: "diff"` events feed:

1. the Project modal tracked-diff ledger
2. the URI-scoped inline agent-edit ledger
3. TE2 `sidebar.agentEdits.publish`
4. optional `sidebar.file.edit` navigation/tracking

TE2 remains stateless for review data and requests
`sidebar.agentEdits.documentState.get` whenever it opens a file, regardless of
tracking settings. `sidebar.file.open`, inline review state, and optional
file-edit tracking are separate contracts.

Accept/reject in either UI routes through the same Rust implementation. Reject
uses the internal unified-diff reverse applier, verifies modified-side lines with
whitespace-tolerant matching, preserves actual context whitespace and line
endings, and rejects missing/mismatched or multi-file hunks. Strict metadata
mismatch requires explicit force confirmation before content-search fallback.

Reject-all is one Rust RPC. Rust snapshots tracked edits newest-to-oldest and
applies reverse patches sequentially, publishing per-diff removal/clear/current
document state before acking the frontend. The frontend shows one blocking
spinner rather than racing a barrage of undo requests.

Source anchors:

- `rust/crates/als-server/src/project_summary.rs`
- `rust/crates/als-server/src/project_git.rs`
- `rust/crates/als-server/src/agent_edits.rs`
- `rust/crates/als-server/src/inline_agent_edits.rs`
- `rust/crates/als-server/src/reverse_patch.rs`
- `rust/crates/als-server/src/sidebar_ipc.rs`
- `rust/crates/als-server/src/static/js/codex_agent/project_modal.ts`

## Extension packages and provider notes

### Package and lifecycle ownership

Every extension manifest has a non-empty version. ALS-RS loads ordered builtin
and user roots; later roots override duplicate IDs and registry entries retain
their source root/kind. Assets and schemas are resolved from the entry-owned
root.

Package lifecycle RPCs are `extension.package.validate/install/update/remove`.
`extension.install` remains dependency installation for an already registered
extension. The `als-rs extension ...` CLI performs offline-first file mutation
under `${ALS_RS_DATA_DIR}/extensions` and only best-effort notifies a running
server to reload.

Install/update does not itself imply provider readiness. Runtime reload stops the
affected handler, terminates matching framework-shell transport shells by
shellspec/current app scope, clears modules, reloads registry/handler state, and
waits for readiness where requested.

### Codex

- Runtime protocol response registries use lowercase method keys and rebuild
  missing schema entries from the loaded generated bundle where possible.
- Runtime schema cache is under `${ALS_RS_CACHE_DIR}/codex_app_server_schema`;
  generation uses `codex app-server generate-json-schema --experimental`.
- `initialize.capabilities.experimentalApi` is negotiated when required by
  collaboration-mode calls.
- App-server stdout is bytes-first: `msgspec.json.Decoder` resolves RPC response
  waiters immediately and places non-response events on a Codex-owned async
  router queue so settings/provider-info calls do not wait behind transcript
  processing.
- `agent-pty-blocks` is injected as stdio with the full generated enabled-tool
  list, per-turn config, nonparallel scheduling, and the one-hour timeout. The
  retained `mcp_agent_pty_http_shim.py` is not the active Codex injection path.
- Resume is event-first and excludes turns; authentication is notification
  driven rather than account-read polling.

### Copilot

- Vendored SDK drift is isolated behind `protocol_adapter.py` and
  `protocol_registry.py`; schema and generated/public RPC surfaces are both
  inspected because they can disagree.
- The FWS pipe wrapper uses framework-shell-owned output subscriptions and
  writes, not concurrent direct stdout readers.
- Stable transcript identity uses provider-native message/reasoning IDs rather
  than router-local counters that reset after resume.
- Model effort metadata merges top-level and nested capability sources so
  `xhigh` is not lost. Shared settings UIs do not invent generic effort options.
- Lazy resume is `send -> cold miss -> resume ack -> retry once`; unchanged bound
  sends do not redundantly reapply mode.

Provider-specific logic stays inside its extension package. Rust and the shared
frontend consume only generic adapter/schema/card contracts.

## Repository operations and validation

- Run `basedpyright` directly from `PATH`.
- Use targeted source searches. Do not blindly content-search generated,
  bundled, vendor, transcript, or framework-shell log trees.
- Inspect framework-shell JSON/mixed logs through structured FWS inspection
  first; inspect conversation JSONL with targeted JSON-aware scripts.
- Do not resolve symlinks in repo tooling or runtime path handling.
- Use `TMPDIR` or platform-appropriate temp paths, never a hardcoded `/tmp`.
- The live harness server must not be restarted as part of ordinary validation;
  it owns the current agent connection.
- After frontend source changes, typecheck and build before live testing.
- After Rust changes, run the narrow crate tests/checks first, then broader
  workspace validation proportional to the change.
- After Python changes, run focused tests and `basedpyright`.
- Keep `pyproject.toml` and Rust crate versions synchronized when a release or
  commit requires a version bump.
- Changes to the `ferrous_framework` submodule are committed/pushed in its own
  repository first; the parent then records the gitlink update.

## Migration history

The remaining sections preserve the original milestone record and implementation
ledger. They are historical evidence, not the current source of truth. When a
historical statement conflicts with an earlier current-contract chapter, the
current chapter and tracked source win.

### Initial extension pilot

The adapter protocol was first proven with `copilot-sdk` while keeping the DTO
boundary provider-neutral. The pilot established that Rust could own durable
state and networking while reusing extension-owned Python SDK/runtime behavior.

### Original milestones and acceptance criteria

#### Milestone 1: isolated bootstrap skeleton

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

#### Milestone 2: Rust static/frontend host

Deliverables:

- Rust serves the existing frontend bundle and static assets.
- `GET /`, manifest, static paths, and health work through TE2 proxy.

Acceptance:

- Frontend loads from the Rust server on the ALS-RS port.
- No ASGI/FastAPI server is running for ALS-RS.

#### Milestone 3: conversation storage and replay

Deliverables:

- Rust-owned implementation of the existing conversation index and transcript
  system under ALS-RS data root.
- Rust-owned replay endpoint or RPC method using the current transcript shape.
- `/rpc/conversations` can satisfy initial UI boot/replay.

Acceptance:

- Frontend can load a conversation list.
- Selecting a conversation replays transcript rows from ALS-RS storage.

#### Milestone 4: adapter protocol pilot

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

#### Milestone 5: live interaction parity slice

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

#### Milestone 5.5: schema settings modal and lifecycle mediation

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
- Updated Codex and Copilot extension shellspec subgroups so
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

#### Schema-declared picker/source contract

Canonical contract doc: `worktrees/notes/SCHEMA_SETTINGS_CONTRACT.md`.

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

Schema ownership is file-first. ALS-RS should read
`settings_schema.json` from the registered extension source root/path.
Extension Python remains responsible for provider-native request translation and
dynamic data sources, but the schema file is the contract that translates user
intent into extension settings.

This is the same rule used by the dynamic reasoning-effort fix:
`dynamic_options_from` tells the shared renderer which model/source field to
watch, which extension-facing method output to inspect, and where options and
defaults live in that response. The session/rollout picker should follow the
same pattern. Codex should expose its normalized extension-facing registry
methods/schema declarations; Copilot can use official SDK-backed methods, but
still describes them through the same schema/source/path contract.

Shared conversation controls are also schema/extension-declared semantics, not
hardcoded field names. In particular, the footer corresponds to an extension's
approval policy and mode for the active conversation. The schema or runtime
descriptor should declare "this field is the approval policy" or "this field is
the mode"; ALS-RS and the shared UI can then render those semantic controls in
the footer without requiring a separate "footer" placement flag.

Provider status and usage are the same kind of schema-declared semantic
surface. A schema `info` field declares `semantic.role = provider_status` or
`provider_usage` plus a `semantic.runtime_key` (`status` or `usage`). The shared
settings modal then calls `/rpc/settings` method `extension.providerInfo.get`,
Rust forwards through adapter method `extension.get_provider_info`, and the
extension returns one DTO with `status` and `usage` members. Unsupported provider
usage is represented inside that DTO with `usage.supported = false`; there is no
separate provider-specific modal route.

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

Current ALS-RS status for this contract:

- `/rpc/settings` `extension.sessions.list` routes through the generic adapter
  `extension.list_sessions` DTO path instead of returning a placeholder empty
  list.
- Extension schemas should be reviewed so session/rollout picker fields declare
  the method/source/path details explicitly, matching the three-question
  contract above.

KB tool bug tracking moved to `kb_tool_issues.md` so migration planning does not
become the issue ledger for MCP tool ergonomics.

#### Milestone 5.6: conversation metadata persistence and routing

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

- Starting a conversation from the schema settings modal and choosing Codex or
  Copilot routes the first send to that selected extension.
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
- Updated the `ferrous_framework` adapter transport slice:
  - ALS-RS now pins the standalone submodule at `5f9e0de` / `0.2.7`, the native
    facade plus FWS peer/control-plane and lifecycle/tree-shutdown commit, and
    no longer builds a PyO3 bridge feature.
  - `als-server` prefers the Ferrous native async manager when framework-shell
    context is present, emits `adapter.transport.started` /
    `adapter.transport.fallback` diagnostics for that path, and otherwise keeps
    the direct child transport as the default.
  - The source-checkout bootstrap skips stale `target/debug/als-server` binaries
    when framework-shell context is present and launches plain `cargo run`; no
    `ferrous-framework-pyo3`, `PYO3_CONFIG_FILE`, or `PYO3_PYTHON` handling is
    needed.
  - The deleted Python helper `agent_log_server_rs/ferrous_framework.py` is no
    longer part of ALS-RS runtime. Ferrous owns native process/pipe lifecycle;
    ALS owns JSON-RPC line framing, request matching, and DTO routing.
  - The crate is now ALS-agnostic and framework-shells-compatible without being
    an ALS-specific bridge. ALS-RS remains a consumer that supplies its adapter
    command, roots/env, shellspec entry, and protocol handling at the call site.
  - The latest pinned crate also includes FWS Socket.IO peer/control-plane
    interop (`native_peer` / `peer_protocol`) for controller-peer testing and
    introspection. That does not change the ALS extension-adapter protocol,
    which stays newline-delimited JSON-RPC over Ferrous-owned stdio pipes.
  - The same pin exposes native lifecycle events and procfs-backed tree
    shutdown for FWS orchestration. ALS-RS does not need new adapter call-site
    code for those surfaces.
  - The crate now has a public standalone origin at
    `https://github.com/mrsurge/ferrous-framework.git`, and ALS-RS consumes it
    as a submodule at `rust/crates/ferrous_framework`. Future crate edits land
    in the submodule repo first, then ALS-RS updates the gitlink pointer.
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
  - Codex schema declarations now declare reasoning-effort options
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
- Landed active-view parity:
  - `UiSelectionStore` is the ALS-RS source of truth for
    `active_conversation` / `active_view` in `${ALS_RS_CACHE_DIR}/app_state.json`.
  - `conversation.select`, `conversation.list`, `/rpc/ui view.get`, and
    `/rpc/ui view.set` all read/write the shared store, and
    `conversation.get` without an explicit id hydrates the persisted active
    conversation when one exists.
  - The fix is generic Rust UI state, not extension-specific routing.
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
- Restored ALS-RS extension plan reads:
  - `/rpc/settings` `extension.plan.get` no longer returns the old hardcoded
    false/empty stub; Rust now resolves the target conversation/extension and
    forwards the request through the Python extension adapter.
  - The adapter protocol includes `extension.get_plan`, implemented by
    delegating to the existing extension loader `read_plan(...)` hook so the
    authoritative provider plan/todo state stays extension-owned.
  - The frontend plan overlay/modal contract stays unchanged: manifest/runtime
    options still provide `has_plan` / `has_todo` capability flags, while
    `extension.plan.get` supplies the actual `plan_exists`, `plan_content`, and
    `plan_steps` state for the selected conversation.
- Hardened ALS-RS extension hot reload after package mutation:
  - Rust package install/update/remove finalization now passes the mutated
    extension id through the adapter reload call and waits for ready when the
    extension still exists after reload.
  - The Python extension adapter now stops the affected handler/runtime before
    `extension.reload` clears module cache and re-imports modules. This uses the
    existing extension-owned `stop_client` / `shutdown_client` hooks instead of
    `importlib.reload()`.
  - Targeted reload now also terminates matching framework-shell transport shells
    by the changed extension manifest's `agent.shellspec` spec id plus current
    app scope before re-import, so observed app-server shells like
    `gemini-app-server` do not survive reload just because the old transport no
    longer has a live `_shell_id` cached.
  - Reload targeting is by handler type, not raw extension id, because handler
    modules are shared per extension type in the current loader.
- Added an ALS-RS HTTP entrypoint for targeted extension reload:
  - `POST /api/extensions/{extension_id}/reload` now reloads a single extension
    id through the same safe adapter flow used by package mutation.
  - The route reloads the Rust registry, verifies the extension exists, then
    calls the adapter reload with `changed_extension_ids = [extension_id]` and
    `wait_ready_extension_id = extension_id`.
  - The response includes the targeted extension entry, the full extension list,
    and the adapter reload payload so HTTP callers can inspect ready/runtime
    state without going through the Socket.IO settings RPC lane.
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
- Fixed Codex lazy resume event delivery:
  - `codex-ext` resume now waits for provider `thread/status/changed` with
    `status.type == "idle"` before releasing the buffered retrying `turn/start`
    after a cold `thread/resume`.
  - The idle wait has no artificial timeout in the extension path; transport
    stop/restart/failure still cancels the waiter with an explicit failure.
  - The Python adapter dispatches unordered JSON-RPC requests concurrently so a
    long resume/read operation cannot block unrelated event delivery.
  - Rust conversation fanout subscribes to a lossless adapter event stream before
    bounded debug snapshots, preserving `event.live` and
    `event.transcript_record` notifications for transcript persistence and
    frontend delivery.
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
- Codex prompt-context usage now flows through the server-owned `devins_context`
  adapter DTO. Codex also localizes the TE2 MCP config helper logic and
  `agent-pty-blocks.ask_user` constants/tool detection in extension-owned
  contract modules, so `codex-ext` no longer imports deprecated prompt-context,
  TE2 MCP config, typing-helper, or ask-user-interaction modules. The remaining
  extension-side deprecated import is the dynamic server callback module used by
  Codex client code; treat that as the next boundary-cleanup slice.

#### Milestone 6: dependency minimization

Deliverables:

- Remove FastAPI/uvicorn/python-socketio from ALS-RS runtime dependencies.
- Keep only Python dependencies required for bootstrap and adapter execution.

Acceptance:

- `pip install` installs the bootstrap and adapter dependencies only.
- Rust server owns all ALS-RS networking.

### Decisions recorded during migration

1. Python package name and console script: `als-rs`.
2. Default port: `12459` is acceptable unless implementation discovers a direct
   conflict.
3. Config root: same as ALS-RS data root by default.
4. Frontend: current checkout/frontend is invariant.
5. Transcript system: current transcript system is invariant.
6. Extension pilot direction: start by shaping a provider-neutral contract around
   `copilot-sdk`.
7. Adapter transport: newline-delimited JSON-RPC 2.0 over direct child stdio for
   the default fallback, with optional native `ferrous_framework` pipe transport
   for observed framework-shell launches; DTOs remain transport-disposable and
   ALS owns protocol framing.
8. Extension registry direction: read the existing `extensions/extensions.json`
   registry plus manifests rather than creating an ALS-RS-only registry format.

### Open decisions at the end of the migration snapshot

1. Extension process ownership granularity.
   - Current state: one long-lived Python adapter process owns provider-specific
     handler initialization and event translation for all active extensions.
   - Open question: whether future isolation requires per-extension adapter
     processes or whether the current shared adapter plus targeted reload remains
     sufficient.
2. Remaining conversation controls.
   - `conversation.compact` and `conversation.shell.exec` still need generic
     adapter/Rust/frontend contracts before they can be enabled in ALS-RS.
3. Static packaging policy.
   - Current source-checkout builds serve the Rust-owned static tree and bundled
     frontend from `rust/crates/als-server/src/static`.
   - Future packaged builds should keep the source-checkout vs packaged static
     layout explicit so install-time assets do not drift from build-time assets.

### Migration risks

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

### Historical next-step ledger

1. Complete conversation metadata persistence and routing:
   - Rust meta/list/pin/routing implementation is complete for the local
     persistence path and now updates card-visible meta from transcript rows.
   - Provider binding from schema session/rollout selection is wired through
     Rust meta persistence and the generic adapter resume/hydration hook.
   - Follow-up: adjust draft semantics so unbound conversations stay memory-only
     until a provider thread/session id binds, or clear any unbound persisted
     draft records on launch during the migration window.
   - Follow-up: review extension picker schemas for the three-question
     method/source/path contract now that `/rpc/settings`
     `extension.sessions.list` routes through the adapter DTO path.
   - Follow-up: live TE2 smoke with schema-modal-created conversations for each
     active extension.
   - Active conversation/view restore is implemented through `UiSelectionStore`;
     keep it in the regression set when changing stateful-window or splash boot.
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
     `als-adapter-protocol` now carries a generic `mcp_context` on conversation
     start/resume/send, Rust builds a provider-neutral default context
     (`conversation_id`, `cwd`, requested `mcp_servers`, always-on stdio
     `agent-pty-blocks` with eager tool exposure intent, and optional `te2-mcp`
     intent when enabled), the Python extension adapter passes that through as
     `settings.mcp_context`, and extension-owned `mcp_contract.py` modules now
     map that generic DTO into provider-native Copilot/Codex MCP config without
     adding provider-specific logic to ALS-RS itself.
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
     fork. Rust owns prompt-context construction at adapter request boundaries:
     every conversation start/resume/send gets a fresh `devins_context`
     assembled from the raw `developer_instructions` setting, TE2 developer
     template when `te2_mcp_integration` is enabled, and the current
     `.repo_memory.md` snapshot discovered from the effective cwd. The adapter
     passes that context through as `settings.__als_devins_context__`; Copilot
     and Codex extension-local `devins_contract.py` modules consume the
     already-computed `effective` string and no longer import the deprecated
     Python prompt-context helper. This gives next-turn/resume/compact freshness
     without a mid-turn push and keeps repo memory concatenation server-owned.
   - TE2 sidebar IPC/RPC: Rust now speaks the typed JSON-RPC contract over the
     existing `/sidebar_ipc` logical namespace and `/ui_ipc_ws/socket.io`
     physical path. UI RPC `hostUi.recheck` best-effort calls `sidebar.cwd.get`
     and syncs CWD, `file.open` calls `sidebar.file.open`, and `rpc.notify`
     handles `sidebar.cwd.set` / `sidebar.mention`; narrow legacy `sidebar:*`
     fallback remains only for older TE2 instances during the cutover.
   - synced composer drafts: ALS-RS now broadcasts `conversation.draft.updated`
     after `conversation.draft.set` and after queued sidebar mentions update an
     inactive conversation draft, so other clients can hydrate live draft changes
     through the existing frontend `draft_update` path.
   - conversation splash/minibar sync: ALS-RS now broadcasts backend-owned
     `conversation.list.updated` snapshots with monotonic revisions after
     create/select/update/session-bind/delete/pin and provider metadata
     mutations, while keeping high-frequency composer draft edits on the narrower
     `conversation.draft.updated` channel.
   - interrupt: implemented through the generic adapter `conversation.interrupt`
     path.
   - compact: still needs the generic ALS-RS control contract.
   - shell/tool card parity: continue validating via provider-owned router events
     and transcript mirror rules instead of Copilot-pilot-only assumptions.
5. Keep this migration plan current after each ALS-RS phase lands.
