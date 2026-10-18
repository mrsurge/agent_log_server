# Dependency-trim tracker

## 2026-10-18 release preparation

- [x] Remove unused watchfiles runtime requirement after a source/import audit;
  metadata regression rejects its reintroduction. No installed-package changes,
  runtime restart or release/version change. msgspec remains required.
  Validation: 36 dependency/loop/bootstrap/native-wheel tests and 2 subtests pass;
  authored whitespace checks pass. User approved the local cleanup checkpoint.

- [x] Prepare 0.2.139.dev1 for the approved source push; no release tag/publication.
- [x] Promote target-exclusive uvloop/uvloop-te2 requirements to runtime metadata;
  retain the performance extra for compatibility and existing loop policy.
- [x] 36 focused dependency/bootstrap/native-wheel/loop tests and 2 subtests pass.
- [x] Confirm PyPI FWS 0.2.65 Android/Linux wheels already exist; no republishing.
- [ ] Build/validate/publish cleaned ALS native wheels, then update TE2 pin/locks.
  Current public ALS remains 0.2.132. Existing dist output and codec.py editor
  draft are preserved; no live installation or harness restart was performed.

## Deferred extension-adapter transport investigation

- [x] Record user hypothesis and minimum-conversion constraint in
  docs/EMBEDDED_ADAPTER_ENVELOPE_HYPOTHESIS.md. Earlier direct embedded attempts
  never worked; no retained crash evidence. No cause claimed or code changed.
- [x] Clarify whole-application crash versus extension failure, retained adapter
  multiplexing/harness-shell roles, and removal of only the outer kernel pipe
  handoff. User approved documentation commit/push; version bumped to 0.2.136
  per repo policy. No runtime implementation, activation or publication.
- [ ] Separately scope/approve tracing the working serialized-envelope chokepoint
  and dormant embedded path. Preserve codec/dispatch identity across PyO3;
  no arbitrary-Python-object decoding or premature unwrapping.

## Migration and validation history

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
- [x] Native MCP migration qualified and direct MCP/Pydantic metadata removed;
  complete transitive closure still depends on the coordinated FWS upgrade.
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
- [x] Independent executable/runtime ABI and packaging/launcher implemented;
  native Codex harness accepted. Copilot subsequently retired by user request.
- [x] Thin stdlib exec bootstrap approved/implemented at unchanged invocation;
  SDK adapter is test-only, no fallback. Existing ALS artifact helpers are shared;
  interpreter/SOABI/GIL/platform/hash/source receipts gate selection.
- [x] Disposable unchanged-invocation client passed after actual source release
  build and with cached reuse. No live restart. Initial test-only 30s timeout was
  too short for a clean release build; qualification now allows 900s. Owned Cargo
  cancellation cleanup is explicit. 120 tests/114 subtests pass; bootstrap ty with
  explicit source search root and basedpyright pass. About 8.7 GiB free.
- [x] Wheel inventory/CPython-minor tags and both executable repair hashes updated.
- [ ] Actual Linux/Android wheel validation. Native harness cutover was accepted;
  direct SDK/Pydantic runtime metadata is now removed. Copilot is retired.
- [x] User upgraded the TE2 .jitenv to local ALS 0.2.133 and restarted the
  harness. Live agent-pty-blocks ping, conversation identity and KB list passed;
  ask_user displayed and returned accepted=true / "Yes, working". Ping PID
  1475020 is the cached native als-mcp executable, not the SDK transport.
- [x] Move source preparation out of MCP invocation into ALS bootstrap using a
  native-build JSON manifest (server + MCP, cache/fingerprint/ABI/artifact paths).
  Current cold invocation exceeded Codex's 30s timeout: installed native build
  completed ~113s after Codex spawn; restart succeeded once compilation was warm.
  Separately approved source cutover is now implemented; installed/live adoption
  of this build-flow slice remains separate (no restart or package upgrade).
- [x] Packaged native_mcp_build.json config, ALS bootstrap preparation ordering,
  --build-only no-launch behavior, source/ABI atomic selection, retained-cache
  reuse and no-Cargo/rustc invocation tests pass. Server build/cache unchanged.
  125 tests/114 subtests plus ty/basedpyright/diff checks pass. Disposable native
  client completed explicit preparation before handshake, then discovered/called
  real read-only tools/resource. No installed-package change or live restart.
- [x] User confirmed the new ALS bootstrap build flow passes live acceptance.
- [x] User requested both checkpoints; ALS metadata bumped to 0.2.134 per policy.
- [x] User approved isolated rmcp/PyO3 proof; independent crate at
  rust/mcp_embedding_probe uses three synthetic tools, no ALS imports/credentials.
  Cargo build succeeds with selected CPython 3.14. SDK discovery captures the
  existing 38-tool/one-resource catalog; direct string/integer objects, interleaved
  wait/echo, explicit MCP cancellation and invalid argument handling pass.
  Two-millisecond future completion polling is proof-only, not production design.
  Live launch commands, SDK dependencies and dormant adapter remain unchanged.
- [ ] Coordinated FWS adoption, clean target dependencies, new wheels/releases.

## Runtime metadata and Copilot retirement — 2026-10-14

- [x] User confirmed bootstrap preparation live acceptance.
- [x] Preserve the complete Copilot tree at ~/knowhere/als-copilot-sdk, recursive
  comparison matched before removal. Preserve registry/packaging/test references
  in its als-integration-reference/ directory; no fork repo initialized.
- [x] Remove builtin Copilot registration, source tree and packaging declarations;
  keep Codex registration and generic loader unchanged. User state untouched.
- [x] Remove ALS's direct fastmcp/Pydantic requirements; mcp==1.30.0 declared only
  in the test extra for existing schema/validation/transport reference tests.
- [x] 125 tests and 114 subtests passed. Import blocker covers active adapter,
  Codex handler and native MCP runtime without SDK/schema/crypto imports.
- [x] Generate isolated egg-info: no retired direct runtime requirements or
  extensions/copilot_sdk source inventory. No actual wheel/release built.
  Existing setuptools warnings include old nosetests/imp discovery and missing
  legacy te2_runtime.py module; not caused or repaired by this cleanup.
- [x] No-install fresh resolver succeeds: no FastMCP/Python MCP SDK/cryptography;
  FastAPI/Pydantic/pydantic-core remain exclusively through the current FWS pin.
  Evidence: .codex-scratch/dependency-cleanup-N2eSZI/ (metadata and resolver report).
- [x] FWS 0.2.65 published with Linux/Android native wheels and matching PyPI
  digests, then pinned here. Fresh requirements resolution excludes all six
  retired-stack distributions (FastAPI/FastMCP/mcp/Pydantic/pydantic-core/crypto).
  Evidence: resolution-fws-0.2.65.json in the same scratch directory. ALS tests
  still pass 125 tests/114 subtests. No installed environment was changed.
- [ ] Release new ALS native wheels, then update TE2's old ALS pin; TE2 currently
  has a resolver conflict between ALS 0.2.132's FWS 0.0.64 pin and new FWS 0.2.65.
- [x] User approved source-only checkpoint/push; Python metadata bumped to
  0.2.135 per commit policy. No wheel publication or new release tag requested.
- [ ] Install/activate this cleanup when separately requested. Existing installed
  packages and processes are unchanged; bootstrap flow has prior live acceptance.
- [x] User approved checkpoint; Python metadata bumped to 0.2.133.
- [x] Checkpoint prepared after 111 tests/38 subtests passed again; selected
  typing/diff checks passed. Python MCP remains active until replacement approval.
