# Codex semantic command cards

## Ownership

`extensions/codex_ext/router.py::_command_card_specs` prefers the App Server
CommandExecution `commandActions` DTO. The checked local protocol schema is
`my-schema/ts/v2/CommandAction.ts`; Codex's
[TUI conversion](https://github.com/openai/codex/blob/main/codex-rs/tui/src/chatwidget/exec_state.rs)
also consumes command actions rather than inferring their semantics from the
display shell wrapper. The
[TUI rendering implementation](https://github.com/openai/codex/blob/main/codex-rs/tui/src/exec_cell/render.rs)
distinguishes reads/listing/search from unknown commands.

For one valid action, `read` becomes the existing View card; `search` and
`listFiles` use the existing Search card (glob/plain listing mode for listFiles).
Paths resolve lexically against the command DTO's cwd, not the local shell or
current browser project. Neither bash/sh login flags nor executable names choose
these cards. Normalized card fields are mirrored in live and transcript records,
including stable card ID, path, content, title, range and structured lines.

## Conservative boundaries

- A present commandActions field is authoritative. Empty, malformed, unknown or
  mixed/multiple actions remain Run; no fallback to a guessed read/new-file card.
- Aggregated multi-action output has no per-file partition in this DTO. It stays
  one Run result; do not duplicate it into fake per-file View bodies. A richer
  grouped exploration UI is separate work, not implied by this conversion.
- A semantic read has no line range. Preserve actual numbered output; otherwise
  show plain content without fabricating line 1+ for a partial read.
- Semantic search/list output stays unchanged. Do not prepend a directory as
  though it were a known matched filename.
- Older peers with no commandActions field retain existing parser compatibility.
  Classification is retained from item/started through output deltas/completion;
  a completion-only item is classified with the same owner. Do not reclassify a
  running card halfway through its lifecycle from contradictory late metadata.
- Failed reads retain the command error result. Explicit fileChange DTOs remain
  the structured edit/add/delete lane; an Unknown shell heredoc is not an invented
  fileChange merely because its command text resembles a write.

## Validation and activation

`agent_log_server_rs/tests/test_codex_command_actions.py` exercises actual router
notifications, shell independence, live/replay parity, malformed/unknown/multiple
actions, line identity, deltas, sparse/completion-only results and legacy peers.
Existing shell/fileChange/protocol/resume/compaction tests remain in the suite.

This Python extension-router change requires a fresh extension-adapter process
loading the new source, not a Rust server/MCP build or Code TE2 mypyc rebuild.
Do not restart the shared ALS harness without user approval. The concurrent
composer cooldown change does need matching ALS frontend/client assets. Current
status and live acceptance belong only to the sibling TE2 pre-release tracker:
docs/apps/release_0_2_354/TRACKER.md.
