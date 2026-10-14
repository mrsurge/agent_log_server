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
- [ ] Coordinated FWS adoption, clean target dependencies, new wheels/releases.
- [x] User approved checkpoint; Python metadata bumped to 0.2.133.
- [x] Checkpoint prepared after 111 tests/38 subtests passed again; selected
  typing/diff checks passed. Python MCP remains active until replacement approval.
