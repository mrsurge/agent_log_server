# Embedded extension adapter: preserve the serialized DTO boundary

Recorded 2026-10-14 at source checkpoint 5cf58ff. Hypothesis and future design
constraint only; no embedded-adapter activation or implementation is approved
by this note.

## Evidence boundary

The user reports earlier direct embedded extension-adapter attempts could not be
made to work and crashed the whole application, not merely an extension. The
user reports extensions themselves are ordinarily stable. No traces, reproducer or working earlier configuration
are available. Premature message unwrapping is a hypothesis, not a demonstrated
cause. Do not turn this report into a confirmed PyO3 lifetime/type bug or infer
that an arbitrary extension object necessarily caused the crash.

The supported subprocess path is the reference. The dormant embedded adapter is
not a proven equivalent or a prerequisite for the independent native stdio MCP
runtime, which is a different boundary and remains unchanged.

## Hypothesis

An embedded path may unwrap/convert extension messages earlier than the working
pipe path does, bypassing the existing normalization/serialization chokepoint.
The desired conversion is primarily a transport substitution, not a rewrite of
the adapter's DTO production, application dispatch or schemas.

Keep one canonical envelope-production/serialization chokepoint before Python
hands messages across PyO3. Extension-native objects remain behind that point.
Cross the PyO3 boundary as the **identical serialized envelope bytes**, not as
arbitrary Python objects for Rust to recursively interpret without a declared
wire contract. Decode only once the bytes are on the Rust side, using the
**exact existing pipe codec/Serde methods and subsequent dispatch**.

The adapter still loads extensions into its interpreter, owns their harness-shell
integration, multiplexes reads/writes across those shells and aggregates the
outbound stream. Preserve those roles and the harness-shell FDs. Remove only the
outer adapter shell/process and kernel-mediated pipe handoff. Python serialization
and a memory handoff remain; the benefit is not eliminating all copies or codecs.
Do not retry the earlier strategy of treating arbitrary Python objects as already
decoded Rust DTOs. PyO3 transports bytes; the existing Rust codec interprets them.

## Current source anchors to reuse

- `agent_log_server_rs/codec.py`: `encode_frame`, including existing
  `to_json_compatible` normalization, MessagePack/JSON selection and frame bound.
  Preserve this policy rather than inventing a second normalizer or Python
  encode/decode sanitization round trip.
- `agent_log_server_rs/adapters/extension_adapter.py`: existing writer
  serialization/order and `write_all_fd`/stream handoff. Replace the final byte
  delivery operation, not the logic that prepares the envelope.
- `rust/crates/als-server/src/adapter_codec.rs`: `Codec::encode` and
  `Decoder::feed`/`finish`. MessagePack uses
  `Value::deserialize(&mut rmp_serde::Deserializer::new(...))`; JSON uses
  `serde_json::from_slice`. Preserve map admission, codec rules and frame limits.
- `rust/crates/als-server/src/adapter_process.rs`: `handle_adapter_value`,
  `handle_response` and `handle_notification`; the latter two retain existing
  `serde_json::from_value` decoding into `Response`/`Notification`. Existing
  nested `Value` fields are the wire contract, not an arbitrary-Python-object ABI.

## Proposed minimum conversion

Working outbound path:

`extension → existing envelope normalization/encoding → pipe/FWS byte delivery
→ existing Rust decoder → existing response/notification dispatch`

Candidate embedded outbound path:

`extension → same envelope normalization/encoding → PyO3 bytes handoff
→ Rust-owned byte buffer → same Rust decoder → same dispatch`

Preserve the reverse direction too: Rust produces the same encoded request;
Python receives bytes through the narrow bridge and uses the existing decode/
dispatch path. No early unwrapping just because the transport is in-process.

Only the handoff should differ. Do not insert recursive PyAny-to-Value coercion,
provider-specific conversions, a parallel schema/dispatcher, new field defaults,
or extra interpretation before the existing decoder. Own/copy bytes while
attached to Python before queuing Rust async work; borrowed Python references
must not become the transport queue's payload. Preserve ordering, bounded
backpressure, exact request correlation, cancellation and shutdown ownership.

The FD itself is not the validator: existing serialization/decoding creates the
data boundary. A bytes-only bridge can preserve that property but cannot replace
subprocess crash isolation. Arbitrary extension/native code could still crash an
embedded interpreter independently of any message conversion.

## Investigation and acceptance gates (not implementation authorization)

1. Trace the working envelope from extension production through the final write,
   Rust decode and dispatch. Compare the dormant embedded path and identify any
   actual early-unwrapping divergence before claiming this hypothesis is proven.
2. Keep the working process mode as reference; do not opportunistically repair
   its unrelated dormant embedded facade or change the native MCP transport.
3. Reuse codec fixtures to prove identical encoded envelopes and resulting DTOs
   for replies, errors, live events and transcript records, including Unicode,
   null/scalar nested fields and large-but-valid payloads.
4. Prove malformed/truncated/oversized envelopes retain rejection semantics,
   without partial dispatch, mutation retry or object-coercion fallback.
5. Test concurrent requests/events, cancellation, reconnect/close, shutdown and
   repeated adapter lifecycle. Capture native backtraces if the crash returns.
6. Measure latency/throughput/memory before making a production switch. Debug
   observation can tap the same serialized bytes; it need not dictate transport.
   Retain process mode where crash isolation is desired.
