# KB Memory MCP — Structured Markdown Knowledge Tools

This repo includes an MCP-based knowledge base system that lets agents read, search, navigate, and edit markdown files as structured data. Markdown headings define the schema; content between headings is the data.

## How It Works

- A `.agent-pty.toml` config file in the repo root declares which markdown files are knowledge sources.
- The MCP server parses heading hierarchy (`#`, `##`, `###`, etc.) into a navigable tree.
- Agents use MCP tools to browse, search, read, and write sections — no raw file parsing needed.
- All edits are atomic (temp file + fsync + rename) and support conflict detection.

## Configuration

Create `.agent-pty.toml` in the repo root:

```toml
[knowledge]
files = ["KNOWLEDGE.md", "ARCHITECTURE.md"]
```

- Paths are relative to the repo root.
- Absolute paths and `..` traversal are rejected.
- Files can be added at runtime with `kb_add_file` (auto-updates the config and hot-reloads).
- The config is also reloadable with `kb_reload_config`.

## Tools

### Navigation

**`kb_list()`**
List all configured knowledge files.

**`kb_schema(file?, id?, max_depth?, root_depth?)`**
Browse the heading tree.
- No `id`: returns top-level headings (table of contents).
- With `id`: returns the **body text** of that section + its **child heading titles**.
- `root_depth`: treat a specific heading depth as top-level (useful for single-H1 docs — set `root_depth=2` to skip the wrapper).
- `max_depth`: how many levels of children to show (default 1).

**Typical navigation flow:**
```
kb_schema()                        → see top-level sections
kb_schema(id="Architecture")       → read body + see children
kb_schema(id="Architecture > Auth")→ drill deeper
kb_read(id="Architecture > Auth > JWT Flow") → get full content
```

### Search

**`kb_search_headers(file?, query)`**
Case-insensitive substring search across heading titles. Returns matching section IDs so you can navigate to them.

**`kb_search_content(file?, query, max_results?, context_chars?)`**
Grep-like search across section bodies. Returns:
- Section ID and title (so you know *where* the match is)
- Line number of the match
- Truncated snippet with the match centered
- Character offsets for precise highlighting

Use this to find content, then `kb_read` the section you want.

### Reading

**`kb_read(file?, id, include_children?)`**
- Default: returns body text only (content between this heading and the next).
- `include_children=true`: returns the full subtree as raw markdown.
- Returns a `hash` of the content (use for conflict detection on writes).

### Writing

**`kb_write(file?, id, content, mode?, heading_title?, heading_depth?, dry_run?, confirm_hash?)`**
- `mode="append"` (default): appends content at the end of the section body.
- `mode="heading"`: creates a new child heading with content.
- `confirm_hash`: provide the hash from a prior `kb_read` — if the section changed since you read it, the write is rejected with a `Conflict` error.
- `dry_run=true`: returns a unified diff without writing.

### Updating

**`kb_update(file?, id, content, mode?, dry_run?, confirm_hash?)`**
- `mode="body"` (default): replaces only the section body (preserves child headings).
- `mode="subtree"`: replaces the heading + all descendants.
- Supports `dry_run` and `confirm_hash`.

### Removing

**`kb_remove(file?, id, mode?, dry_run?, confirm_hash?)`**
- `mode="subtree"` (default): removes the heading and all its children.
- `mode="body"`: removes only the body content, keeps the heading.
- Supports `dry_run` and `confirm_hash`.

## Section IDs

Sections are addressed by their heading path, delimited by ` > `:

```
Top Level Heading > Sub Heading > Sub-Sub Heading
```

- IDs are built from heading text with Unicode normalization (smart quotes → straight quotes, em dashes → hyphens, etc.) so they're always typeable.
- If two sibling headings have the same title, they get disambiguated IDs: `Section Title@L42` (with the line number).
- Fenced code blocks (``` ``` ``` and `~~~`) are respected — headings inside code are not parsed.

## Error Handling

All tools return structured errors:

| Error | Meaning |
|-------|---------|
| `NotConfigured` | No `[knowledge] files` in `.agent-pty.toml` |
| `FileNotAllowed` | File not in the configured allowlist |
| `SectionNotFound` | Section ID doesn't match any heading |
| `AmbiguousSection` | ID matches multiple headings (use disambiguated ID) |
| `Conflict` | `confirm_hash` doesn't match current content |

## Conflict Safety

For concurrent multi-agent use:

1. **Read** the section → get its `hash`
2. **Write/update** with `confirm_hash=<that hash>`
3. If another agent edited the section between your read and write, you get a `Conflict` error with the `current_hash`
4. Re-read, merge if needed, retry

Use `dry_run=true` to preview any mutation as a unified diff before committing.

## Tips

- Use `kb_schema(root_depth=2)` for docs with a single H1 wrapper — jumps straight to the real sections.
- Use `kb_search_headers` before browsing — often faster than walking the tree.
- Use `kb_search_content` like grep — find the snippet, then `kb_read` the full section.
- The `file` parameter is optional when only one knowledge file is configured.
- Headings inside fenced code blocks are ignored — safe for docs with code examples.
