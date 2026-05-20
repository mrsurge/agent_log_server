# KB Tool Issues

Observed during Gemini app-server work from the `gemini-cli-rpc-v0412-port`
checkout on 2026-05-14.

## Fix Status

- `kb_schema` now returns a compact complete numbered ATX heading index for the
  targeted file; `target` is accepted as an alias for `file`, and `max_depth` is
  an explicit narrowing option.
- `kb_info` provides parent-chain context, line ranges, child headings, and body
  previews for selected schema numbers / line refs / ids.
- `kb_read` accepts multiple selected sections and returns parent heading/body
  context from the top level down to each target before returning target bodies.
- Section selectors now treat bare numbers as schema indexes and `L<line>` /
  `line:<line>` as source-line refs.
- `kb_search` is regex-capable and returns section-aware body previews with
  `max_hits`, `preview_chars`, and `from_match` controls.
- `kb_write` now accepts `mode="child"` and `mode="create_child"` as aliases for
  child-heading creation.
- Unsupported KB modes now return `InvalidParameter` with allowed modes instead
  of `SectionNotFound`.
- KB write/update/remove no longer call the obsolete repo-memory IPC
  notification route.
- `kb_help` and the static `kb://knowledge` MCP resource now expose KB
  availability and mode examples.

## Follow-up Observations

During the later Gemini app-server approval work, the KB tool was noticeably
more usable than during the first pass.

What worked well:

- `kb://knowledge` discovery correctly reported the configured KB files and repo
  root.
- `kb_schema` returned useful heading trees for all configured files, including
  `.repo_memory.md` and the planning docs.
- The displayed `id:` values were directly usable and removed most of the old
  section-id guessing.
- Parallel `kb_schema` calls were practical for a repo-memory/bootstrap pass.
- The old "where is the configured KB?" friction did not show up in this pass.

Remaining issues are mostly ergonomics rather than blockers.

## Issues Encountered

### `kb_read` Must Not Silently Fall Back To File Root

`kb_schema` and `kb_search` can expose the expected numbered section index and
search hits, but `kb_read` previously allowed a missing or lost selector to read
`<file-root>`.

Impact: an agent can ask for a schema number or unique heading title, accidentally
drop the selector through a parameter mismatch, and receive the whole file as if
the requested section resolved correctly.

Expected behavior:

- explicit section selectors such as schema numbers, `L<line>`, heading paths,
  unique titles, and unique suffixes resolve to the requested section;
- invalid selectors return a selector-resolution error;
- file-root reads are explicit (`section=""`, `sections="root"`, or
  `sections="<file-root>"`) and are not the default.

### `kb_read` ID Discovery Is Not Obvious

Attempting to read a top-level section by numeric id failed:

```text
kb_read(file="GEMINI.md", id="1")
[ERROR: SectionNotFound] Section '1' not found
```

The schema output showed `L1 # Gemini CLI Project Context`, but did not make it
obvious that the usable id was the heading path/string rather than a numeric
schema index.

Impact: agents may guess numeric ids from schema output and waste a call before
discovering the required path-style id.

### `kb_schema` Did Not Expose Nested Headings Reliably

For `.repo_memory.md`, `kb_schema` returned only:

```text
L1 # Repo Memory
```

even though the file contains nested `##` and `###` headings. A separate
`kb_search_headers` call did find nested ids such as:

```text
Repo Memory > Fork Target
```

Impact: `kb_schema` alone was not enough to discover valid section ids in a
large memory file.

### `kb_write` Mode Semantics Are Hard To Infer

An attempt to create a child section failed:

```text
kb_write(target=".repo_memory.md", section="Repo Memory > Fork Target", mode="child", ...)
[ERROR: SectionNotFound] Unsupported mode 'child'
```

The tool description says it can append content or create a child heading, but
the accepted `mode` values are not discoverable from the error or schema.

Impact: agents cannot reliably tell which `mode` value to use for child-heading
creation without trial and error.

A later attempt with the seemingly obvious `create_child` mode also failed:

```text
kb_write(target=".repo_memory.md", section="", heading_title="ALS-RS settings schema ownership", heading_depth=3, mode="create_child", ...)
[ERROR: SectionNotFound] Unsupported mode 'create_child'
```

The write succeeded only after switching to `mode="append"` while still passing
`heading_title` and `heading_depth`.

Impact: the tool description says it can create child headings, but the actual
operation is encoded as `append` plus heading parameters. That is not obvious
from either the schema or the error.

### Error Text Can Point At The Wrong Mental Model

The `kb_write` failure included `SectionNotFound` even though the section id had
already been discovered via `kb_search_headers`; the actual issue was an
unsupported mode.

Impact: this makes troubleshooting ambiguous. The caller may waste time
rechecking section ids when the actionable issue is the mode value.

The same happened for `mode="create_child"` against `section=""`: the section target
was valid for a file-root append, but the error still used `SectionNotFound`
while saying the mode was unsupported.

Impact: mixed error categories make it harder to decide whether to change the
section id, the mode, or both.

### `kb_write` Can Report Success And IPC Failure Together

A `.repo_memory.md` write returned both a successful write and an IPC error:

```text
[kb_write: WRITTEN  hash: ...]
[kb_ipc: ERROR  ]
```

The file patch was applied successfully, but the IPC notification apparently
failed.

Impact: callers need to treat this as a partial-success state. The tool should
separate "file write failed" from "post-write notification failed" and ideally
include the IPC error detail so the caller knows whether the durable write is
safe to rely on.

### KB IPC Notification Route Is Obsolete

The KB-to-IPC notification path exists because of the old dynamic pending-context
system. That system has since been deprecated/removed, so KB writes no longer
need to notify the app server over that route.

Impact: the IPC side effect should be neutered or removed from KB writes. A KB
write should report the durable file-write result directly instead of surfacing
obsolete pending-context IPC failures as partial-success noise.

### MCP Resource Discovery Does Not Reflect KB Availability

Generic `list_mcp_resources` returned no resources, while the KB-specific tools
were available and functional.

Impact: a generic MCP discovery pass can falsely suggest that no knowledge
resources exist. Agents need to know to call `kb_list` directly.

## Current Issue: KB Discovery / Info / Read Split

Agents were still encouraged to call `kb_schema(max_depth=2)` repeatedly or
drill schema calls instead of first getting the entire header index, then
deciding what body context to read.

Desired behavior:

- `kb_list()` should enumerate the configured KB files.
- `kb_schema(file="...")` or `kb_schema(target="...")` should return a
  compact, LLM-readable numbered index for the entire targeted markdown document.
- The outline should include every parsed ATX heading, not a shallow default.
- Each heading should expose the section number, header level, line ref, and
  actionable section id directly.
- `kb_info(...)` should be the range/preview layer for selected sections and
  their parent chains.
- `kb_read(...)` should be the full-body layer for selected sections and their
  parent chains.
- Search should return section identity and a preview of matching body text,
  with regex support and caller-controlled hit/preview limits.
- `max_depth` should be an explicit narrowing option, not the normal discovery
  path.

Follow-up fix:

- `.agent-pty.toml` had two stale KB paths:
  `worktrees/notes/PATCHED_APP_SERVER.md` and
  `acp/AGENT_EXTENSION_INTEGRATION.md`. They were corrected to the existing
  target files `worktrees/notes/old-notes/PATCHED_APP_SERVER.md` and
  `AGENT_EXTENSION_INTEGRATION.md`.
