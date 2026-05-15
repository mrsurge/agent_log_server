# KB Tool Issues

Observed during Gemini app-server work from the `gemini-cli-rpc-v0412-port`
checkout on 2026-05-14.

## Issues Encountered

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
kb_write(file=".repo_memory.md", id="Repo Memory > Fork Target", mode="child", ...)
[ERROR: SectionNotFound] Unsupported mode 'child'
```

The tool description says it can append content or create a child heading, but
the accepted `mode` values are not discoverable from the error or schema.

Impact: agents cannot reliably tell which `mode` value to use for child-heading
creation without trial and error.

A later attempt with the seemingly obvious `create_child` mode also failed:

```text
kb_write(file=".repo_memory.md", id="", heading_title="ALS-RS settings schema ownership", heading_depth=3, mode="create_child", ...)
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

The same happened for `mode="create_child"` against `id=""`: the section target
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

## Usability Quirks

- Section ids appear to be path-like heading labels, not numeric ids.
- `kb_search_headers` is currently more useful than `kb_schema` for finding
  nested section ids in large markdown files.
- When KB writes fail, the safest fallback is still a direct file patch, but
  that bypasses the nicer section-aware write surface.
- The tools are useful once the exact id and mode are known, but discovery of
  those inputs is currently brittle.

## Obviously Missing Features

- A `kb_modes` or `kb_help` call that lists valid write/update modes with short
  examples.
- `kb_schema` output that includes every discovered heading id, at least behind
  a depth option that works consistently for nested headings.
- Better error classification for unsupported modes versus missing section ids.
- A direct `create_child` operation, or an explicit documented `mode` for child
  heading creation.
- A dry-run example in errors when the requested write shape is close but
  invalid.
- Optional numeric aliases for schema-discovered headings, so callers can use
  either stable heading paths or short ids.
