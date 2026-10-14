# Dependency-trim tracker

- [x] Read repo workflow and native ownership contracts; preserve untracked dist/
  and .codex-scratch/, do not restart the active harness or resolve symlinks.
- [x] Audit native adapter, packaged stdio MCP and unused HTTP shim. Native adapter
  import blockers reject FastAPI/FastMCP/SDK/Pydantic/crypto imports successfully.
- [x] User-approved HTTP shim deletion and package module-list cleanup.
- [x] Remove stale FastAPI requirement; declare Starlette directly as still used
  by retained Python MCP SDK transport until that separate migration completes.
- [x] Separately approved retirement of codex-agent legacy console entrypoint.
- [x] Optional loop helper integrated at extension adapter and CLI; MCP stdio
  stays ordinary asyncio per user clarification.
  Absent/disabled/present/broken selection, coroutine closure, executor/task
  cleanup and owning-thread factory behavior are tested.
- [x] Optional performance extra has mutually exclusive Linux x86-64 vs Android
  aarch64 distributions. Pixel read-only marker probe: android/aarch64, 3.14.6.
- [x] Embedded adapter discovered dormant/stale; restored its original source,
  did not enable its unavailable Cargo feature or introduce PyO3.
- [x] 25 selected adapter/codec/loop/MCP-contract/dependency tests passed, with two
  subtests. Real disposable stdio SDK smoke: initialize, 38 tools, one resource,
  ping and shutdown pass without live conversation actions.
- [x] Full native-adapter suite: 111 passed, 38 subtests passed. Selected typing
  and diff checks pass for changed active loop/adapter/CLI source. Optional extra
  marker exclusivity and import blockers passed; no packages installed.
- [ ] User live activation and real optional-loop performance/FD behavior.
- [x] Record selected rmcp Rust executable / PyO3 Python tools architecture,
  with one persistent ordinary asyncio loop and no handler rewrite.
- [ ] Approve/implement/qualify that migration, then remove MCP/Pydantic closure.
- [x] Real-tool extraction scope approved: SDK-independent mcp_tools/registry,
  captured 38-tool/one-resource catalog, thin retained SDK launcher. Original
  secret derivation and provider command/environment contracts are preserved.
- [x] Real rmcp/PyO3 executable built in isolated crate: direct recursive object
  conversion, event-driven completion/cancellation, 32-call admission, depth 64
  and 4 MiB stdin-line bounds. No production launcher/SDK dependency change.
- [x] All tool argument/default/coercion and mocked result envelopes match SDK
  reference; existing ask_user tests plus embedded-runtime cancellation/cleanup
  pass. Native disposable discovery, read-only calls and KB resource read pass.
  Validation: 117 Python tests/114 subtests, two Rust tests, both SDK client
  qualification scripts, ty/basedpyright for registry/runtime/adapter, Cargo
  format and diff checks pass. Free disk remains about 9 GiB.
- [ ] Independent executable/runtime ABI and packaging/launcher implementation,
  Codex/Copilot live acceptance, then dependency removal (separate approval).
- [x] Thin stdlib exec bootstrap approved/implemented at unchanged invocation;
  SDK adapter is test-only, no fallback. Existing ALS artifact helpers are shared;
  interpreter/SOABI/GIL/platform/hash/source receipts gate selection.
- [x] Disposable unchanged-invocation client passed after actual source release
  build and with cached reuse. No live restart. Initial test-only 30s timeout was
  too short for a clean release build; qualification now allows 900s. Owned Cargo
  cancellation cleanup is explicit. 120 tests/114 subtests pass; bootstrap ty with
  explicit source search root and basedpyright pass. About 8.7 GiB free.
- [x] Wheel inventory/CPython-minor tags and both executable repair hashes updated.
- [ ] Actual Linux/Android wheel validation and Codex/Copilot live cutover;
  remove remaining SDK/Pydantic metadata only after acceptance.
- [x] User upgraded the TE2 .jitenv to local ALS 0.2.133 and restarted the
  harness. Live agent-pty-blocks ping, conversation identity and KB list passed;
  ask_user displayed and returned accepted=true / "Yes, working". Ping PID
  1475020 is the cached native als-mcp executable, not the SDK transport.
- [ ] Move source preparation out of MCP invocation into ALS bootstrap using a
  native-build JSON manifest (server + MCP, cache/fingerprint/ABI/artifact paths).
  Current cold invocation exceeded Codex's 30s timeout: installed native build
  completed ~113s after Codex spawn; restart succeeded once compilation was warm.
  This user-selected next direction is documented, not implemented in this commit.
- [x] User requested both checkpoints; ALS metadata bumped to 0.2.134 per policy.
- [x] User approved isolated rmcp/PyO3 proof; independent crate at
  rust/mcp_embedding_probe uses three synthetic tools, no ALS imports/credentials.
  Cargo build succeeds with selected CPython 3.14. SDK discovery captures the
  existing 38-tool/one-resource catalog; direct string/integer objects, interleaved
  wait/echo, explicit MCP cancellation and invalid argument handling pass.
  Two-millisecond future completion polling is proof-only, not production design.
  Live launch commands, SDK dependencies and dormant adapter remain unchanged.
- [ ] Coordinated FWS adoption, clean target dependencies, new wheels/releases.
- [x] User approved checkpoint; Python metadata bumped to 0.2.133.
- [x] Checkpoint prepared after 111 tests/38 subtests passed again; selected
  typing/diff checks passed. Python MCP remains active until replacement approval.
