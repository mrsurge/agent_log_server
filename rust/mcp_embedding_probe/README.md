# Isolated MCP embedding proof

**Bootstrap cutover:** the real executable is now named `als-mcp`; unchanged
`python mcp_agent_pty_server.py` invokes a stdlib-only bootstrap, builds/selects a
receipt-verified artifact and execs it. The SDK adapter is test-only. The notes
below record the earlier isolated qualification; production command selection is
now implemented in source but no active harness was restarted. Wheel inventory
uses CPython-minor platform tags, both binaries and MCP receipt/catalog. Actual
wheel/device and both-client live acceptance remain gates before dependency removal.

Independent synthetic rmcp stdio executable; no ALS tool imports, secrets,
provider launch changes, package fallback or live harness access. Rust owns MCP;
PyO3 submits direct string/integer objects to one persistent ordinary asyncio
loop in a Python-owned thread. Cancellation/drop cancels its concurrent future.
Loop shutdown uses asyncio.Runner cleanup. Two-millisecond completion polling
is a proof-only bridge, not the selected production wakeup design.

The second executable, `als-mcp-real-tools-probe`, uses the real 38-tool/one-resource
catalog and SDK-independent `agent_log_server_rs/mcp_tools.py` handlers. It requires
explicit `ALS_MCP_PROBE_PYTHON_ROOT` pointing at the checkout/package root. The
existing provider launch commands do not select it. Completion is callback/oneshot
driven (no polling); stdin lines are bounded at 4 MiB, nesting at 64 levels, and
concurrent tool/resource calls at 32 with explicit rejection rather than queuing.
Arguments/results cross PyO3 as objects, never dumped/decoded JSON. Rust still
uses serde to convert its in-memory MCP response DTO; that is not a byte parse.

The SDK adapter remains at mcp_agent_pty_server.py for current launch parity. The
versioned mcp_catalog.json was captured from that existing SDK contract, not
regenerated at runtime. Registry admission checks names, descriptions, annotations,
parameter types, defaults and required fields against source; parity tests compare
all input schemas and mocked results against the SDK used only as a test reference.
The low-volume tool loop uses ordinary asyncio, independent of optional uvloop on
the high-volume extension adapter. Live harness, provider launch and dependency
removal remain separate gates.

Build with an explicit compatible embedding interpreter:

```bash
PYO3_PYTHON=/absolute/path/to/python cargo build --manifest-path rust/mcp_embedding_probe/Cargo.toml
```

Keep Cargo caches. Check disk before/through builds; stop below 1 GiB free.
The executable is target/debug/als-mcp-embedding-probe in this crate. Do not
package it or select it as agent-pty-blocks. Production needs separately approved
runtime/ABI discovery, wheel inventory/launcher integration, complete catalog,
result/error/consent parity and bounded request admission. Optional uvloop stays
with the high-volume extension adapter; it is not imported by this proof.

Run the SDK client tests from the ALS root using the development interpreter:

```bash
python -B rust/mcp_embedding_probe/test_probe.py
python -B rust/mcp_embedding_probe/test_real_tools.py
python -B -m pytest -q agent_log_server_rs/tests
```

test_probe.py checks retained launcher descriptor parity and synthetic concurrency/
cancellation/EOF; test_real_tools.py uses a disposable cwd/knowledge file and only
read-only real calls. No shell actions, mutations, real approvals or active ALS
conversation are exercised. Generated baseline capture stays under ignored target/.
Direct tool context/cancellation tests mock IPC owners; they do not impersonate
a live MCP session. Exported native tools can perform real actions if deliberately
called, so this binary is not a sandbox—do not attach it to a live client yet.
