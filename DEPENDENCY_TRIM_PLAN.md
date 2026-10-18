# Native ALS dependency and stdio MCP cleanup

## Goal and ownership

2026-10-18 approved preparation: push source as 0.2.139.dev1; reserve 0.2.139 for
a separately approved tagged release. Runtime requirements now select uvloop on
Linux/x86_64 and uvloop-te2 on Android/aarch64, not only the performance extra.
Preserve loop disable/fallback/error policy and MCP's ordinary asyncio ownership.
FWS 0.2.65 already has both target wheels on PyPI; do not republish it. ALS native
CPython-minor wheels and fresh closure still need validation/publication before
TE2's bare ALS requirement can resolve the cleaned stable release by default. User requires latest compatible
first-party releases: ALS uses FWS>=0.2.65, with no exact runtime pin or upper
cap. Exact tested versions belong in validation locks, not runtime metadata.
No build, installed-package change, tag or upload here.

2026-10-18 follow-up approval: checkpoint pending dependency cleanup as
0.2.139.dev2 and prepare Linux candidates on Debian in isolated
manylinux containers. No local native builds, tags or public uploads. TE2's fresh
default pip install currently selects public ALS .132 and the retired stack;
candidate-wheel testing is separate from stable publication/default resolution.
Preserve codec.py's meaningful editor draft and existing dist outputs.

User correction: use the uv CPython 3.14 build venv, not an extra CPython 3.13
candidate driven by the acceptance host. Linux wheels now bundle private CPython
3.14, its shared library/stdlib and a freshly installed runtime-only dependency
closure from that build environment. Test the finished wheel in an isolated venv
on the local development device and enforce size strictly below 100,000,000 bytes.
Termux and editable installs retain their existing system/invoking Python model.

The follow-up source audit found no remaining watchfiles consumer in native
bootstrap, adapter, Codex extension or retained Python sources. Remove its stale
runtime requirement; no replacement watcher or application behavior change is
needed. This removes that direct Android wheel gap, not the msgspec requirement
or the need to verify the complete fresh target dependency closure.

Jinja2/Fastcore/HTTPX are also removed from direct runtime metadata: their only
remaining source consumers are the retired Python page routes/HTML generator
and HTTP MCP-shim lifecycle helper. Retain those deprecated files without
promising that their old dependencies are installed. Active adapter/Codex import
probes explicitly block all three; TE2's independent HTTPX requirement is intact.

Remove unsupported Python web/schema/MCP dependency edges without rewriting
the native ALS server or existing typed tool/application logic. Preserve the
generic extension adapter, conversation-scoped MCP identity, tool/resource
schemas, Socket.IO IPC, consent/ask_user cancellation and transcript ownership.
No TE2 runtime dependency is introduced into ALS. Source is authority.

## Approved first slice

- Remove stale FastAPI metadata and the unused HTTP MCP shim (user explicitly
  authorized deletion, not migration). Retire the old codex-agent console script
  as separately approved; als-rs remains the supported server launcher.
- Add optional uvloop selection at active Python loop owners: extension stdio
  adapter and extension dependency CLI. The low-volume MCP loop stays ordinary
  asyncio, separate from the high-volume extension adapter. Use
  ALS_DISABLE_UVLOOP=1 to disable. Missing top-level uvloop falls back; nested
  missing modules, ABI failures and other installed-import failures propagate.
- The optional performance extra selects uvloop==0.23.0 on Linux x86-64 and
  uvloop-te2==0.23.0.post1 on Android aarch64. Both expose import uvloop; never
  install both. Pixel probe confirmed android/aarch64 CPython 3.14.6 markers.
- No package install/uninstall, native build, live harness restart, wheel
  publication or dirty/untracked cleanup is part of this slice. User approved
  checkpointing; bump Python package metadata to 0.2.133 per repo commit policy.

## Verified remaining dependency boundary

Native ALS serving is Rust. The active Python extension adapter imports none of
FastAPI, standalone FastMCP, Python MCP SDK, Pydantic or cryptography. Isolated
import blockers validate that boundary. Deprecated modules remain source history,
not justification for retaining unsupported console launchers or dependencies.

The migrated mcp_agent_pty_server.py execs native als-mcp. It exposes 38 tools and
one kb://knowledge resource without Python SDK transport. The SDK 1.30.0 is now
declared only in the test extra, preserving argument/schema reference tests.
Direct FastMCP/Pydantic runtime requirements are removed after live acceptance.
Published Framework-Shells 0.2.65 is now pinned. Fresh ALS requirements resolve
without FastAPI/FastMCP/Python MCP/Pydantic/cryptography. Installed environments
and new ALS native wheels still require separate upgrades/releases.

The embedded PyO3 adapter is dormant: no Cargo feature/dependency enables it.
Its Python facade is stale (close helper/constructor/dispatch contract mismatch).
Do not activate, repair or use it as a prerequisite for this migration. It was
left unchanged after inspection; active subprocess separation is intentional.

Future embedded-adapter investigation is recorded separately in
`docs/EMBEDDED_ADAPTER_ENVELOPE_HYPOTHESIS.md`: preserve the identical serialized
envelope across PyO3 and reuse the current Rust pipe codec/Serde dispatch only
after the crossing. Earlier crashes have no retained evidence; premature
unwrapping is a hypothesis, not a diagnosis. This does not authorize activation
or alter the independent native stdio MCP boundary.

## Next coherent slice: Rust rmcp owner with PyO3 Python tools

The user clarified this is much smaller than TE2's HTTP/server cutover. Keep
existing typed Python tools; replace only SDK transport, registry and validation.
The selected direction is an independent Rust executable using rmcp for public
stdio MCP, embedding the existing Python tool logic through PyO3. Rust owns the
protocol, admission, descriptors, dispatch and cancellation; Python retains tool
semantics and ALS IPC. A persistent ordinary asyncio loop runs in its owning
thread. Optional uvloop remains an extension-adapter concern, not an MCP need.
Pass arguments/results as Python objects, without JSON/MessagePack round trips
across PyO3. No per-request loops/threads or holding Python attachment while
waiting for Rust I/O. Long approvals must not block unrelated requests.
Do not activate the stale embedded extension adapter or rewrite 38 handlers.
Investigate runtime/ABI discovery, packaging and an isolated embedding proof
before approving production implementation or changing provider launch commands.

1. Capture current initialize capabilities, all 38 descriptors/annotations/defaults,
   resource metadata and representative success/error envelopes in disposable roots.
   Do not import MCP against the active conversation/credentials or execute mutations.
2. Qualify rmcp-owned bounded stdio ingress/egress and a typed tool
   registry: initialize/initialized, ping, tools/list/call, resources/list/read,
   unknown methods, malformed/invalid params, exact request IDs and cancellation.
   Export only implemented capabilities. Keep stdout protocol-only.
3. Dispatch DTOs directly to the existing Python functions; preserve conversation/
   cwd/IPC admission and exact ask_user begin/cancel/ack behavior. Context becomes
   an explicit request context, not a fake SDK session. Never retry mutations.
4. Qualify with real MCP SDK clients used only in tests, captured schema/result
   fixtures, interleaved long/short calls, cancellation, EOF, malformed/oversized
   lines, resource access and mocked ask_user admission/cleanup. Verify stdio-only
   configuration and no SDK/Pydantic/FastAPI/crypto imports in the child runtime.
5. Only then remove standalone FastMCP, MCP SDK and explicit Pydantic requirements
   where source confirms no other supported consumer. Preserve arbitrary user MCP
   configuration; this changes the built-in provider, not third-party servers.

## Later activation/release gates

### Approved thin bootstrap cutover

**Implemented after separate approval:** native MCP source preparation now runs
in ALS bootstrap before server launch. native_mcp_build.json declares Cargo source,
features, Python ABI, cache/target/publication paths and fingerprint inputs. Server
build/cache policy is unchanged. als-rs --build-only prepares/verifies both without
launching ALS. Atomic selectors bind source/config and Python ABI to a verified
artifact. MCP invocation performs validate/select/exec only—no Cargo or rustc on
the handshake clock. Missing/stale source selection fails with preparation guidance.
The earlier installed build exceeded Codex's 30s timeout; this flow removes that
compilation from startup instead of increasing the timeout. Source tests are not
package upgrade, shared-runtime restart or live adoption of this new build flow.

The user subsequently confirmed live acceptance of the bootstrap preparation
flow. This task did not reinstall packages or restart any running process.

### Copilot retirement and runtime metadata cleanup

User explicitly chose retirement rather than modifying the vendored SDK's eager
Pydantic import. Before deletion the full extensions/copilot_sdk tree was copied
to /home/mrsurge/knowhere/als-copilot-sdk and compared recursively with no
differences. The external copy also retains the old registry, pyproject and
Copilot integration regression for an independent future fork.

Remove the builtin registry entry, tree, Copilot-only serializer test and specific
package-data/typechecker declarations. Codex remains registered; generic extension
loading stays unchanged. Do not delete user settings/conversations, install or
uninstall packages, restart the harness, or create/publish the future fork here.
The copy is source preservation, not a standalone packaged extension yet.

Validate runtime imports with MCP/FastAPI/Pydantic/crypto blocked, the complete
native Python suite, generated metadata/source inventory, and a no-install pip
resolver report. No direct SDK/schema requirements should remain; existing FWS
transitive dependencies are reported honestly, not bypassed with --no-deps.

The existing `python mcp_agent_pty_server.py` invocation now imports only the
stdlib-only mcp_bootstrap and execs `als-mcp`. The SDK adapter is test-only in
agent_log_server_rs/tests/mcp_sdk_reference.py; no runtime SDK fallback remains.
No current harness process is restarted by this source cutover.

Editable/Android selection checks invoking CPython minor/SOABI/GIL mode/platform/architecture,
artifact SHA-256 and source fingerprint for cached builds. Native --runtime-info
verifies the loaded interpreter before exec. PYTHONHOME comes from invoking base
prefix; its package search paths are passed explicitly. It uses that interpreter's
compatible shared library/stdlib. Packaged Linux instead execs the verified private
Python first; its own ABI receipt validates the native MCP artifact. Cwd/identity/stdio/
environment survive exec; build diagnostics stay stderr. Existing ALS lock/atomic
publication/target helpers are shared with an independent MCP cache; owned Cargo
process-group cancellation is explicit. No pruning or shared runtime termination.

Source installs build for their invoking interpreter. Binary wheels must build on
the target host/interpreter, include both binaries plus MCP receipt/catalog and
use CPython-minor platform tags for Android. Linux private-runtime wheels use
py3-none platform tags, preserving the existing outer Requires-Python >=3.11.
The stdlib-only launcher uses -I/-S/-B, clears inherited Python/loader overrides,
and places private native dependencies ahead of the outer site-packages. The
private file inventory is refreshed after ELF repair; only the deliberately
bundled libpython is excluded from auditwheel repair. Linux builders require an
explicit --private-dependencies input. Unsupported ABI/incomplete packaged payload fails
without source/interpreted fallback. ELF repair refreshes both executable hashes.
Actual Linux/Android wheel and both-client live acceptance remain; no publication,
package uninstall or active harness restart occurred here.

Isolated qualification now lives in rust/mcp_embedding_probe (own Cargo workspace,
rmcp 3.5.1/PyO3 0.28.3). User approved the real-tool extraction slice: handlers
are in mcp_tools.py, schemas/metadata in mcp_catalog.json, and mcp_registry.py
provides neutral typed admission. The SDK reference is now test-only; the Python
launcher execs native MCP. That executable submits object DTOs through PyO3 to one ordinary
asyncio loop with event-driven completion, 32-call admission and bounded stdin.
Tests cover all schema/default/coercion/result contracts, mocked ask_user cleanup,
and disposable native read-only tool/resource calls. No live commands change yet.

The interpreter/runtime/ABI and launcher policy is implemented by the approved
thin bootstrap cutover above. setup.py now inventories both executables with MCP
receipt/catalog. It does not promise a private bundled interpreter; artifact ABI
must match the invoking interpreter. Actual target-wheel validation is still due.
Preserve cross-platform install paths and consent/conversation/environment identity;
qualify both Codex/Copilot stdio launches before removing the SDK/Pydantic closure.

User-controlled live native ALS and both agent-client acceptance, optional real
uvloop FD/subprocess/Socket.IO behavior, updated FWS source/release adoption and
fresh Linux/Termux dependency resolution remain. Existing installed packages and
accepted wheels/locks are not modified by deleting requirement lines. Local ALS
commits do not require version bumps; an authorized code/build push prepares one
development version for its batch, while documentation-only pushes are exempt.
See AGENTS.md Package Version Policy; tagging/publishing is separately approved.
