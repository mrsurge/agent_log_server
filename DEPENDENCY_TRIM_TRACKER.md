# Dependency-trim tracker

## 2026-10-27 ALS RS 0.2.140 companion release preparation

- [x] User approves synchronized Python/Cargo 0.2.140 metadata, source checkpoint,
  VPS staging under /mnt/vdb, frontend/release checks and a Linux wheel with
  bundled ordinary CPython 3.14. Current PyPI stable is 0.2.139; do not republish it.
- [x] Preserve existing untracked dist/ and calculator APK, live harnesses and
  protected Debian installations. No tags/uploads or Android build-route decision
  are authorized by this preparation step.
- [x] Stage clean source 3137509384a96784788bc90de70aa72ed23a24c0 on VPS under
  /mnt/vdb/te2-release-0.2.356/sources/als-0.2.140; exact Ferrous 4cdf5db tree.
  Preserve prior source/build roots; reuse copied caches, not live installations.
- [x] Frontend typecheck/build pass with no tracked output drift; four subagent
  UI tests, 35 focused Python tests and all 180 Rust tests pass.
- [x] Build Linux native server/MCP and repaired private-Python wheel, strictly
  below 100,000,000 bytes, with runtime-only dependency closure.
  Python 3.14.6/Cargo 1.94.0, previously validated manylinux image a9cc5bf581bf;
  fresh binary-only runtime closure includes FWS 0.2.65 and uvloop 0.23.0.
  Final wheel: 57,348,437 bytes (54.69 MiB), SHA256
  1e10fbe276ab38171ee49b843bc07f418446e945b2d2a013198dd08c679f71c7.
  Path: /mnt/vdb/te2-release-0.2.356/als-0.2.140/output/.
- [x] Twine/auditwheel checks and fresh container Python 3.14 plus Debian-host
  Python 3.13 venv installs pass. Native MCP initializes, lists 38 tools/one
  resource and exits cleanly on EOF; actual bundled 3.14 libpython is mapped.
  Dependency checks and retired-stack absence checks pass. Build-only Twine was
  installed separately after the existing build venv lacked it; not a runtime dep.
  Logs: /mnt/vdb/te2-release-0.2.356/logs/als-0.2.140-{linux-build-v3,
  linux-validation,debian-validation}.log. No publication or shared-runtime change.
- [x] User explicitly selects isolated Pixel build as an exception to VPS-only
  compilation, then authorizes isolated dependencies after noting the device wipe.
  Python 3.14.8 / extracted Rust 1.99.1, four jobs; no global package install.
  Fresh Termux headers require versioned C/C++ target aarch64-linux-android24;
  build-only environment adjustment, no ALS source modification. Preserve initial
  failed log; resumed cached build and MCP preparation succeed, exit 0.
- [x] Android wheel: 9,794,152 bytes (9.34 MiB), SHA256
  2f71db6948b0cddce439b2f4a1ea2738448de89de9540b6d8e27cfd7aeccdd70.
  Pixel root ~/projects/als-0.2.140-release (moved out of .cache at user request);
  entire workspace retained, venvs refreshed, artifact hash unchanged. Historical
  build logs retain their original paths. Artifact/logs copied with
  matching hash to /mnt/vdb/te2-release-0.2.356/als-0.2.140/android/. Twine passes.
  All 26 target Python regressions pass. Fresh acceptance venv passes pip check,
  actual native MCP 38 tools/one resource/EOF, system Python 3.14 ABI, uvloop-te2
  and native FWS pipe. No retired dependency stack or private Linux Python.
  Acceptance excludes isolated compiler paths/libraries. Server reaches its own
  argument parser with stock Termux loader environment (unsupported --help
  returns its explicit error); this is loader proof, not server live acceptance.
- [x] User confirms ALS 0.2.140 live acceptance on both desktop and Termux.
- [x] User authorizes publishing both validated ALS 0.2.140 wheels.
- [x] Publish both companion wheels to PyPI 0.2.140; public PyPI JSON reports
  both exact filenames, byte-identical SHA-256 hashes to the accepted artifacts.
  Joint TE2 final-candidate/vendor validation remains separate from ALS acceptance.
- Release tag 0.2.140 targets build source 3137509, not the later documentation-only
  acceptance receipt commit. GitHub release assets use the same two PyPI wheels
  and SHA-256 values above; no rebuild or version bump is required.

## 2026-10-18 release preparation

- [x] Final clean source d10a16c pushed/tagged 0.2.139; Linux/Android wheels
  published to PyPI and public SHA-256 verified. Final Linux wheel: 56,739,877
  bytes, SHA-256 0409d0f0d51ca7985dc90a425e815d866fb1d443de4b2dcdd75b55eb37753ee8.
  Final Android wheel: 9,596,571 bytes, SHA-256
  14c317cb96f28e1dfe8278dc2aad9506e64f44e5fb780891a36fb140690cfbeb.
  Both reuse accepted native binaries with byte-identical ELF payloads; Linux
  retains private CPython 3.14.6, Android uses system 3.14.6. Twine/size checks
  and fresh local-wheel/PyPI-only joint TE2 .354 + ALS .139 + FWS .65 installs
  pass on Debian/Pixel, including native MCP 38 tools/one resource/clean EOF,
  bundled libpython (Linux), target uvloop and absent retired dependency stack.
  Earlier candidate dirty-source/no-tag gates below are historical, now closed.
  TE2 GitHub release remains draft pending its three APKs; no shared runtime
  restart or installed developer environment replacement. Complete receipts in
  sibling TE2 docs/apps/release_0_2_354/BUILD_STATUS.md.

- [x] User confirms all Termux wheel live validations passed for the isolated
  TE2 .354 / ALS .139 / FWS .65 stack. Android wheel acceptance is closed;
  clean release provenance, tags/uploads and separate APK acceptance remain.

- [x] Native Pixel Android .139 wheel completes with system CPython 3.14.6,
  no bundled Linux interpreter: 9,596,571 bytes, SHA-256
  1e2f3820633948e8c80c244cfd731163b4940521a533d3e676ba62df4efdd8f9.
  Joint installed Termux venv with TE2 .354/FWS .65/uvloop-te2 .23.0.post1 passes
  dependency check, native FWS pipe load, uvloop selection, compiled editor and
  WBA imports, native MCP 38-tool/one-resource discovery and EOF shutdown.
  Pixel build root ~/.cache/te2-release-build/native-0.2.354-als-0.2.139;
  validation.log/.exit retain evidence. User will launch the isolated candidate
  manually for live acceptance. No public upload/tag or existing-install change.

- [x] User live-validates the complete joint Linux TE2/ALS/FWS stack, then
  approves finalizing ALS as stable 0.2.139 (not another dev suffix), rebuilding
  the wheel and repeating isolated packaging smoke checks. Keep the accepted
  joint venv unchanged; stable validation uses a separate fresh environment.
  No tags or public uploads in this step.
- [x] Stable 0.2.139 Linux wheel rebuild and isolated install/MCP/size checks.
  Container als-linux-private-0.2.139-stable exits 0; 56,739,875 bytes, SHA-256
  cd373da2ade305baf849b9c08c3e9dfe14fa68f98d361764c3da1528bf1b9ca6.
  Both native binaries and the complete private runtime are byte-identical to
  live-accepted dev2 r4; only outer metadata/server manifest changes.
  Fresh local Python 3.14.4 venv and separate Debian joint Python 3.13.5 venv
  pass inventory, actual bundled-libpython mapping, MCP 38 tools/one resource,
  clean shutdown, pip check and no retired dependency stack. Joint Debian
  als-rs --build-only reuses packaged artifacts and te2 --version reports .354.
  Local stable root: TE2 .codex-scratch/als-private-wheel-0.2.139/;
  remote stable root: /root/.cache/te2-release-acceptance/joint-stable-0.2.354-als-0.2.139/.

- [x] User corrects target to uv CPython 3.14 only and approves private Linux
  runtime packaging (libpython, stdlib and runtime dependencies), followed by a
  fresh local-device venv test and strict <100,000,000-byte check. Android/source
  paths remain unchanged; no uploads or shared runtime changes.
- [x] Initial container server compilation succeeds. Candidate packaging exposed
  direct-script import discovery, missing shared Python in the unused cp313
  lane, and auditwheel removing the libpython link. Baseline cp314 wheel is
  diagnostic only and must not be published.
- [x] Private-runtime source regressions pass (23 tests); new runtime/build helper
  strict basedpyright checks pass. Guard source inputs, hashes, traversal and
  symlinks; preserve capability env while isolating Python/loader search paths.
- [x] Private-runtime candidate r4 builds on Debian; 56,739,963 bytes (54.11 MiB),
  below 100,000,000 by 43,260,037 bytes. SHA-256:
  ec13d5e09164f920dafcfd04ea0699635fa52eba1204ca8c88549e78d077c442.
  Container als-linux-private-0.2.139-dev2-r4 exited 0, no OOM.
- [x] Fresh container venv and fresh local-device venv pass full runtime inventory,
  native MCP initialize/tools/list/resources/list (38 tools/one resource), clean
  EOF shutdown and dependency checks. Local outer Python 3.14.4 loads private
  3.14.6; /proc of the isolated MCP child confirms the bundled libpython path.
  Local retained root: TE2 .codex-scratch/als-private-wheel-0.2.139.dev2/.
- [x] Fresh joint TE2 .354 + ALS private-wheel install on Debian Python 3.13.5
  passes native MCP discovery under private 3.14.6, FWS .65 resolution, pip check
  and te2 --version. No retired FastAPI/FastMCP/MCP/Pydantic/crypto/watchfiles.
  TE2's separately owned HTTPX remains intentional; ALS private closure omits it.
- [x] 23 focused regressions pass; basedpyright reports zero errors/warnings in
  mcp_bootstrap/private_runtime and both release build helpers. The failed r3
  artifact exposed strip-after-patchelf corruption of Python PIE metadata; r4
  preserves the trusted pre-stripped interpreter unchanged when RPATH matches.
  Nested private dependency RECORD files are not mistaken for the outer RECORD.
- [ ] Final clean-source checkpoint/release provenance and stable publication;
  current stable candidate honestly records dirty source and remains unpublished.
  Public pip still selects old stable ALS until a separately approved release.

- [x] User approves 0.2.139.dev2 cleanup checkpoint and Debian-only CPython
  3.13/3.14 Linux candidate builds, no tags or uploads. Local disk is constrained;
  preserve installed binaries, codec.py draft and dist/. TE2 Linux .354 native
  import acceptance passes but default fresh dependencies still select old ALS
  .132/FWS .0.64, blocking intended dependency closure.

- [x] Remove stale Jinja2/Fastcore/HTTPX direct runtime requirements; remaining
  references belong only to deprecated Python page/shim code. Metadata checks
  reject them and active adapter/Codex import probes block their imports.
  Deprecated source, installed packages and codec.py editor draft are untouched.
  Validation: 36 focused tests and 2 subtests pass, including blocked-import
  probes; authored whitespace checks pass.
  No version change, commit, push, build, publication or runtime restart.

- [x] User policy: latest compatible first-party releases with minimums only.
  ALS now requires FWS>=0.2.65; regression admits newer versions and rejects
  exact/upper constraints. TE2 now uses bare ALS/FWS requirements. No version bump,
  commit, push, installed-package change or publication in this source slice.

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
- [ ] Build/validate/publish cleaned ALS native wheels and regenerate test locks.
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
