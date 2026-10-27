# Sub-agent activity restoration

## Current release: compact cards

User-approved hotfix, 2026-10-26. No version bump, publication or shared-runtime
restart is authorized by this implementation. Existing highlighting hotfix and
untracked root `dist/` are preserved.

- [x] Inspect the Debian session's actual Codex stdout and generated protocol.
  Server: `100.74.145.70`; installed TE2 .354 and ALS .139 remain unchanged.
  Source log: `fs_1792897267_ceb58483.stdout.log`, under
  `~/.cache/te2/framework_shells/runtimes/82ebe4a102b840f7/638e02494d90f738/logs/`.
  Matching Codex .163.0 schema lives under
  `~/.cache/als-rs/codex_app_server_schema/0.163.0/`.
- [x] Handle generated `ThreadItem` variants `subAgentActivity` and
  `collabAgentToolCall`. Activity `kind` and agent `agentsStates` are distinct
  from outer item-start/item-complete and tool-call status. Preserve old
  collaboration-event support, not a replacement for older protocol routing.
- [x] Bind child threads to the parent conversation, resolving again at ordered
  dispatch if the IO reader queued a child event before its binding was processed.
- [x] Compact projection: independent stable card identity per child turn;
  agent name/path, status, truncated collapsed preview and latest shell/MCP/tool
  activity or assistant message in a fixed-height scroll region. Empty active
  cards say “Agent running.” Text is literal, not HTML. Output is bounded to
  16,384 trailing characters with an omission marker; streaming snapshots are
  throttled to 10 Hz, with item completion always flushed. This is not a nested
  child transcript or one event per token.
- [x] Child notifications do not update parent plan/token/status/turn metadata.
  Child turn identity is `agent_turn_id`, never the conversation-level `turn_id`
  consumed by Rust's active-turn rotation. Approval requests retain the existing
  request path. Completed cards retain their own content when another turn starts.
- [x] Reuse existing `subagent_start` upserts / `subagent_end` events and their
  durable card index. Record matching snapshot fields for live and replay. Shared
  compact renderer handles replay/window replacement without stale live DOM.
  Progress records stay in the active projection, surviving parent turn changes;
  only completion creates the durable chronological anchor. Running cards stay
  at the transcript tail while parent rows arrive. Their outer headers are not
  sticky; completed cards stop following the tail.
- [x] Validation: all 124 Node tests; 51 focused Python tests / 50 subtests;
  frontend typecheck/lint and minified bundle rebuild. New projection module and
  Python regression file pass strict mypy with disallow-any expression/explicit/
  generic checks. Broader router/transport strict check reports 18 router errors;
  the unchanged HEAD router reproduces the same 18 errors, not a whole-tree pass.
- [x] Read-only remote-log replay through the new local projection: 48,760
  relevant events handled, 70 agents, 420 turn cards, 417 completed / 3 interrupted;
  417 cards have latest message text and zero projected parent-turn mutations.
  Message bodies were truncated to 256 characters and deltas replaced for this
  test; this is routing evidence, not a throughput benchmark or live deployment.
- [ ] Build/install the matching ALS runtime and obtain live validation: concurrent
  agents, completed/interrupted turns, follow-ups, reload and bounded history.
  ALS embeds frontend assets in its Rust binary; `npm run build` alone does not
  update an already running packaged runtime. No remote runtime was changed.

### Compact activity correction after the first live test

- [x] Inspect current local adapter/app-server logs and both connected ALS clients.
  The test child emitted four shell commands but only one assistant message (the
  final answer); message-only filtering concealed its work. Parent-directed
  collaboration notifications did not carry the missing commentary text.
- [x] Project command execution, MCP and dynamic-tool starts/results, bounded
  output deltas and assistant-message deltas without changing parent metadata.
- [x] Correct transient/durable placement and exclude sub-agent outer headers
  from the sticky-header overlay. Regressions interleave parent work and multiple
  children, then verify completion placement.
- [x] Validation: 126 Node tests, 180 Rust library tests, 54 focused Python tests
  with 50 subtests; frontend typecheck and minified rebuild; strict mypy plus
  disallow-any checks for the new projection module and its regression tests.
  Existing Rust cfg/dead-code warnings remain. This is not a whole-tree Python
  typecheck claim.
- [x] User confirms the concurrent live test works (2026-10-26): a delegated
  GeckoView/build review runs while the parent reads the full repo README.
  Activity appears before the final answer and the running card follows the tail.
  No agent-triggered runtime restart. This acceptance does not separately prove
  every MCP variant, interruption, follow-up or reload scenario above.
- [x] Refresh TE2's vendored ALS snapshot from checkpoint b77e7d5: all 150
  resource hashes and three interception/OTA tests pass. No APK build or push.

## Following release, alongside TE2 .357

- [ ] Separately approve a full child transcript per active agent turn, hosted in
  an iframe with its own bounded scrollback/projection state machine.
- [ ] Keep the child view at its bottom while following; preserve user scroll
  detachment and define idle/follow-up lifecycle, disposal and history hydration.
- [ ] Extend the compact completion-anchor lifecycle to the full iframe view.
  Preserve identity and anchor consistency across live,
  replay, concurrent children, nested agents and reconnects.
- [ ] Reuse the existing transcript projection contracts rather than inventing an
  unbounded nested timeline or allowing child state to control the parent.

The iframe stage is deferred, not implemented or automatically approved here.
