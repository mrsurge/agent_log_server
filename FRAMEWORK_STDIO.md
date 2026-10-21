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
there are no new Sidebar services, retries or fake-success responses.

## Bidirectional transport foundation (Stage A)

The native framework pipe now has a typed request/reply transport in
`rust/crates/als-server/src/framework_pipe/{protocol,runtime}.rs`. The controller
still lives in `framework_pipe.rs`; its client is installed in AppState before
router construction starts the existing CWD fetch. **No semantic service is
advertised or bound in production yet.** Current Sidebar operations, standalone
integration and frontend sockets remain unchanged; the new outbound API and
handler binding are exercised by isolated tests, not selected by live callers.

- One detached OS input thread decodes concatenated envelopes. Framework-origin,
  target and version checks precede admission. Replies match request ID,
  correlation ID and operation ID; foreign/late correlations cannot settle calls.
- A dedicated writer owns stdout. The async loop/intake never waits on stdout
  writes. Admission is nonblocking and bounded: 32 queued frames plus at most one
  in-flight write share 2 MiB of encoded bytes; frames are at most 1 MiB. Readiness waits
  for the writer's actual write/flush acknowledgement and is single-use.
- At most 16 outbound calls and 16 incoming queued/running handlers. Incoming
  requests additionally reserve from a 2 MiB encoded-byte budget, held until the
  handler completes. Control params are limited to 64 KiB serialized JSON before
  queueing; received control replies have a 1 MiB serialized-JSON ceiling. These
  are small-control defaults, not approved bulk draft/review budgets.
- Async handlers run independently of intake, so a handler can await a reverse
  app RPC while the same reader accepts its response. Completed tasks are drained
  ahead of new work; count permits survive until execution completes. Control
  handlers have a ten-second deadline; outbound callers provide a nonzero budget.
  Consent-bound project work needs separate deadline/receipt design in Stage B.
- Close fails pending calls, aborts admitted handlers, releases queue budgets and
  clears handler bindings (including closures capturing app state/client handles).
  Cancellation/timeouts do not undo accepted operations; nothing is replayed.
  Output failures close the pipe. Oversized handler results produce a small error
  when it can be enqueued, without invoking the handler twice.
- Concrete envelope, kind, error, app-call, delivery/source and handler-context
  types replace field-by-field transport guesses. Opaque JSON-compatible values
  are limited to generic payload/context/error-detail boundaries; future service
  handlers must validate operation DTOs before domain access. No `any`/`Any`,
  downcast escape hatch, new runtime dependency or warning suppression is added.

Service exports, socket selection changes and operation migration require Stage B.
Once migrated, explicit --stdio mode must not fall back to the Sidebar socket;
standalone's existing socket integration is retained separately. This foundation
does not implement reconnectable UDS or change EOF ownership.

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
Matching ALS installation and TE2 registry/worker refresh remain user-operated.
The wrapper health-gate removal needs matching OTA/reload, not an APK or Code TE2
mypyc rebuild. Build success is not runtime activation. Workstream status is in
the sibling TE2 docs/apps/sidebar_app_intents/TRACKER.md.
