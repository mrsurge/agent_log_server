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
