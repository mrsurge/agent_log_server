# ALS framework stdio serving contract

ALS remains independently runnable with `als-rs`. When a framework owns its
stdio lifecycle, launch `als-rs --stdio`. The bootstrap forwards the flag to
either explicit, packaged or fingerprinted source-built native binaries; it
preserves the real stdio FDs for that child. Python preparation diagnostics and
Cargo stdout go to stderr, as do native tracing and Rust main errors.

The wire format is concatenated MessagePack map envelopes, matching TE2's
framework pipe. It is NOT the length-prefixed extension-adapter protocol, MCP
stdio, JSONL or log scraping. After router preparation/listener bind, emit once:

```json
{"jsonrpc":"2.0","protocolVersion":1,"kind":"notification","method":"app.readiness","originNid":0,"originName":"als-rs","targetNid":1,"targetName":"framework.rust","params":{"status":"ready","phase":"serving"}}
```

Bind/startup failure emits no ready fact. The HTTP readiness poster is removed;
TE_APP_ID alone never enables readiness. Standalone mode neither reads stdin nor
emits framework frames. HTTP health remains available for diagnostics, not as a
readiness fallback. main.rs and library run() share the same server lifecycle.

Pipe EOF requests graceful shutdown; corrupt/truncated input fails with nonzero
exit. A detached process-owned reader avoids Tokio stdin blocking shutdown on
SIGTERM. Input uses the framework's 32 MiB frame ceiling. Unsupported requests
return a framed kind:error with string code method_not_found and retryable:false;
unsupported methods never fall through to another transport or fake success.

## Bidirectional transport and Sidebar services

The native framework pipe now has a typed request/reply transport in
`rust/crates/als-server/src/framework_pipe/{protocol,runtime}.rs`. The controller
still lives in `framework_pipe.rs`; its client is installed in AppState before
router construction binds service handlers. The initial CWD fetch starts only
after listener bind and the readiness write/flush acknowledgement, so readiness
is the first stdout fact and failed binds emit neither readiness nor CWD calls.
Explicit `--stdio` selects the pipe for all backend Sidebar integration. A closed
pipe, missing export or rejected request cannot create a Sidebar socket. Standalone
mode retains its existing socket integration; frontend sockets are unchanged.

- One detached OS input thread decodes concatenated envelopes. Framework-origin,
  target and version checks precede admission. Replies match request ID,
  correlation ID and operation ID; foreign/late correlations cannot settle calls.
- A dedicated writer owns stdout. The async loop/intake never waits on stdout
  writes. Admission is nonblocking and bounded: 32 queued frames plus at most one
  in-flight write share 18 MiB of encoded bytes; frames are at most 9 MiB, allowing
  8 MiB app payload budgets plus framing. Readiness waits
  for the writer's actual write/flush acknowledgement and is single-use.
- At most 16 outbound calls and 16 incoming queued/running handlers. Incoming
  requests additionally reserve from a 2 MiB encoded-byte budget, held until the
  handler completes. Control params are limited to 64 KiB serialized JSON before
  queueing; received control replies have a 1 MiB serialized-JSON ceiling. These
  remain small-control defaults. Selected outbound full-model review requests
  and draft/review replies opt into local 8 MiB budgets. Framework receiver
  manifests independently enforce their matching method policies and shared
  32 MiB route admission. Oversized operations fail whole, without truncation.
- Async handlers run independently of intake, so a handler can await a reverse
  app RPC while the same reader accepts its response. Completed tasks are drained
  ahead of new work; count permits survive until execution completes. Control
  handlers default to ten seconds; the bound Sidebar service selects sixty.
  Local outbound waits are 20 seconds for control, 65 for review/drafts and 185
  for project mutation, outside the broker's 15/60/180-second method deadlines.
  ALS project UI calls allow 190 seconds. User confirmation stays in the existing
  UI; Code TE2 revalidates the caller after project-switch lock waits. Timeouts
  report uncertain outcomes and never authorize mutation retries.
- Close fails pending calls, aborts admitted handlers, releases queue budgets and
  clears handler bindings (including closures capturing app state/client handles).
  Cancellation/timeouts do not undo accepted operations; nothing is replayed.
  Output failures close the pipe. Oversized handler results produce a small error
  when it can be enqueued, without invoking the handler twice.
- Concrete envelope, kind, error, app-call, delivery/source and handler-context
  types replace field-by-field transport guesses. Opaque JSON-compatible values
  are limited to generic payload/context/error-detail boundaries; future service
  handlers validate operation DTOs before domain access. No `any`/`Any`,
  downcast escape hatch, new runtime dependency or warning suppression is added.

`sidebar_ipc.rs` retains existing callers and selects a pipe/socket client by
explicit process mode. The pipe operation enum covers file open/edit, CWD/state,
project lookup/open/create, agent-edit publish/clear/list/document-state/decide and
the three existing authenticated MCP draft operations. No draft allowlist broadening.

`sidebar_ipc/pipe.rs` binds `sidebar` version 1, admitting only framework-derived
`code_te2` requests: mention, CWD/project/focus facts and agent document/decision
requests. These reuse composer, HostUi and review owners. Mentions require explicit
conversation identity; the receipt reports owner acceptance/queued state and its
operation ID, not a frontend-render acknowledgement. Exact main-page/presentation
proof travels through ALS file and project UI calls to Code TE2. Generic transport
metadata is never a substitute for that proof. This does not implement reconnectable
UDS or change EOF ownership.

Code TE2's pre-delivery route query distinguishes an absent managed app (eligible
for explicit standalone socket integration) from stale/starting managed routes
(errors). A route receipt does not prove an older binary implements the method;
actual handler failures never fall through or replay. Review requests exclude
their originating app to prevent echo. Projection failures are logged and do not
turn an already committed project change into a retryable mutation.

Source build packaging/fingerprinting already recurses through Rust modules
(`MANIFEST.in` graft, setup.py copytree, bootstrap._rust_fingerprint_paths); the
new nested module files therefore participate without a second build workflow.
No installed binary is replaced by Cargo validation. The exact fresh development
binary can be selected for the isolated stdio tests with ALS_TEST_SERVER_BIN;
never point that test at the active harness process.

TE2's wrapper manifest declares readiness_support:"pipe"; its shellspec declares
backend:pipe/native_pipe_testing and stdout:messagepack/stderr:text. The separate
Sidebar UI readiness callback stays intact. Its frontend wrapper relies on the
framework app_shell gate and no longer polls /api/health before mounting.

Validation entrypoints:

```bash
python -B -m pytest -q agent_log_server_rs/tests/test_bootstrap.py agent_log_server_rs/tests/test_mcp_bootstrap.py
cargo test --manifest-path rust/Cargo.toml --release -p als-server framework_pipe::tests -j 4
cargo build --manifest-path rust/Cargo.toml --release -p als-server -j 4
ALS_TEST_SERVER_BIN="${CARGO_TARGET_DIR:-rust/target}/release/als-server" python -B -m pytest -q agent_log_server_rs/tests/test_server_stdio.py
```

The last command requires the exact fresh Cargo output, not an old installed
binary; tests use disposable roots and a fake framework, never the live harness.
Matching ALS installation, framework/registry and Code TE2 domain activation are
required. Compiled Code TE2 needs a matching mypyc snapshot. ALS project caller-proof
frontend changes also need a checkpointed vendored snapshot and client OTA/reload;
no native mapping/APK change is introduced. No installation or live restart is
performed by these validation commands. Workstream status belongs to sibling TE2
docs/apps/release_0_2_356/TRACKER.md.
