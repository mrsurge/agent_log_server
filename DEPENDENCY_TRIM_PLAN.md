# Native ALS dependency and stdio MCP cleanup

## Goal and ownership

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

mcp_agent_pty_server.py genuinely uses mcp.server.fastmcp from the Python SDK,
not the standalone fastmcp API. It exposes 38 tools and one kb://knowledge
resource. Codex and Copilot defaults launch it through stdio. ask_user checks
for a non-null SDK Context but does not use session operations: consent/results
already belong to ALS IPC. The SDK is therefore real current runtime usage.
Installed mcp 1.30.0 requires Pydantic, pydantic-settings and pyjwt[crypto].
Replacing the standalone distribution with that SDK is not final closure removal;
the current MCP dependency remains until the stdio replacement is qualified.

The embedded PyO3 adapter is dormant: no Cargo feature/dependency enables it.
Its Python facade is stale (close helper/constructor/dispatch contract mismatch).
Do not activate, repair or use it as a prerequisite for this migration. It was
left unchanged after inspection; active subprocess separation is intentional.

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

User-controlled live native ALS and both agent-client acceptance, optional real
uvloop FD/subprocess/Socket.IO behavior, updated FWS source/release adoption and
fresh Linux/Termux dependency resolution remain. Existing installed packages and
accepted wheels/locks are not modified by deleting requirement lines. ALS commits
require the version bump mandated by AGENTS.md; publishing is separately approved.
