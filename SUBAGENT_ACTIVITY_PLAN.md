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
  agent name/path, status, truncated collapsed preview and latest completed
  assistant message in a fixed-height scroll region. This slice does not stream
  every token or render a nested child transcript. Message text is literal, not HTML.
- [x] Child notifications do not update parent plan/token/status/turn metadata.
  Child turn identity is `agent_turn_id`, never the conversation-level `turn_id`
  consumed by Rust's active-turn rotation. Approval requests retain the existing
  request path. Completed cards retain their own content when another turn starts.
- [x] Reuse existing `subagent_start` upserts / `subagent_end` events and their
  durable card index. Record matching snapshot fields for live and replay. Shared
  compact renderer handles replay/window replacement without stale live DOM.
  Per-turn cards currently retain their original transcript anchors; moving a
  complete nested transcript to the end-of-turn position belongs to the next slice.
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

## Following release, alongside TE2 .357

- [ ] Separately approve a full child transcript per active agent turn, hosted in
  an iframe with its own bounded scrollback/projection state machine.
- [ ] Keep the child view at its bottom while following; preserve user scroll
  detachment and define idle/follow-up lifecycle, disposal and history hydration.
- [ ] Freeze the completed turn's view into transcript history chronologically
  where that turn ended. Preserve identity and anchor consistency across live,
  replay, concurrent children, nested agents and reconnects.
- [ ] Reuse the existing transcript projection contracts rather than inventing an
  unbounded nested timeline or allowing child state to control the parent.

The iframe stage is deferred, not implemented or automatically approved here.
