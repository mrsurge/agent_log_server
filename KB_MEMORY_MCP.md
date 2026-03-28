# KB Memory MCP — Structured Markdown Knowledge Tools

This repo includes an MCP-based knowledge base system that lets agents read, search, navigate, and edit markdown files as structured data. Markdown headings define the schema; content between headings is the data.

All tools return **plain text** — no JSON escaping, no backslash-newline artifacts. Agents read it as-is.

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

- Paths are relative to the project root (the directory containing `.agent-pty.toml`).
- Absolute paths and `..` traversal are rejected.
- Files can be added at runtime with `kb_add_file` (auto-updates the config and hot-reloads).
- Files can be removed at runtime with `kb_remove_file` (auto-updates the config and hot-reloads).
- The config is reloadable with `kb_reload_config`.
- **Auto-discovery:** the server walks up from the harness launch `cwd` to find `.agent-pty.toml` (like git finds `.git`). No manual root config is needed when the MCP server is launched from inside the repo.
- **Harness-owned root:** KB follows the MCP server's launch `cwd` / repo. Cross-repo KB root switching is not supported through KB tools.

## Tools

All tools return plain text. Errors are formatted as `[ERROR: Type] Detail`.

### Navigation

**`kb_list()`**
List all configured knowledge files and the current project root.

**`kb_schema(file?, id?, max_depth?, root_depth?)`**
Browse the heading tree. Output is a plain text listing.
- No `id`: returns top-level headings (table of contents).
- With `id`: returns the **body text** of that section + its **child heading titles**.
- `root_depth`: treat a specific heading depth as top-level (useful for single-H1 docs — set `root_depth=2` to skip the wrapper).
- `max_depth`: how many levels of children to show (default 1). **Note:** `max_depth` only applies when drilling into a section with `id`. Without `id`, only headings at the `root_depth` level are listed — use `kb_schema(id="...")` to see children.
- When a heading's ID differs from its display title (due to unicode normalization or parent prefixing), the output shows `id: <full_id>` so you can copy it directly into `kb_read`.

**Typical navigation flow:**
```
kb_list()                          → see configured files
kb_schema(root_depth=2)            → see top-level sections
kb_schema(id="Architecture")       → read body + see children
kb_schema(id="Architecture > Auth")→ drill deeper
kb_read(id="Architecture > Auth > JWT Flow") → get full content
```

### Search

**`kb_search_headers(file?, query)`**
Case-insensitive substring search across heading titles. Returns matching headings with their section IDs so you can navigate to them.

**`kb_search_content(file?, query, max_results?, context_chars?)`**
Grep-like search across section bodies. Returns one line per match:
- Section title and ID
- Line number of the match
- Truncated snippet with the match centered

Use this to find content, then `kb_read` the section you want.

### Reading

**`kb_read(file?, id, include_children?)`**
Returns the section content as plain text with a metadata header line.
- Default: returns body text only (content between this heading and the next).
- `include_children=true`: returns the full subtree as raw markdown.
- The header line includes a `hash` of the content (use for conflict detection on writes).

### Writing

**`kb_write(file?, id, content, mode?, heading_title?, heading_depth?, dry_run?, confirm_hash?)`**
Returns a status line + unified diff.
- `mode="append"` (default): appends content at the end of the section body.
- `mode="heading"`: creates a new child heading with content. **Auto-inferred** when `heading_title` is provided.
- `heading_depth`: defaults to parent depth + 1 if omitted.
- `confirm_hash`: provide the hash from a prior `kb_read` — if the section changed since you read it, the write is rejected with a `Conflict` error.
- `dry_run=true`: returns the diff without writing.

### Updating

**`kb_update(file?, id, content, mode?, dry_run?, confirm_hash?)`**
Returns a status line + unified diff.
- `mode="body"` (default): replaces only the section body (preserves child headings).
- `mode="subtree"`: replaces the heading + all descendants.
- Supports `dry_run` and `confirm_hash`.

### Removing

**`kb_remove(file?, id, mode?, dry_run?, confirm_hash?)`**
Returns a status line + unified diff.
- `mode="subtree"` (default): removes the heading and all its children.
- `mode="body"`: removes only the body content, keeps the heading.
- Supports `dry_run` and `confirm_hash`.

### Management

**`kb_reload_config()`**
Reload `.agent-pty.toml` for the current project root and return the effective file list.

**`kb_add_file(abs_path)`**
Add a markdown file to the knowledge config by absolute path. The file must be inside the current project root. Auto-updates `.agent-pty.toml` and hot-reloads.

**`kb_remove_file(abs_path)`**
Remove a markdown file from the knowledge config by absolute path. The file must be inside the current project root and already registered in `knowledge.files`. Auto-updates `.agent-pty.toml` and hot-reloads.

## Section IDs

Sections are addressed by their heading path, delimited by ` > `:

```
Top Level Heading > Sub Heading > Sub-Sub Heading
```

- IDs are built from heading text with Unicode normalization (smart quotes → straight quotes, em dashes → hyphens, etc.) so they're always typeable.
- The resolver accepts **both** raw unicode titles and normalized IDs — so you can paste either.
- If two sibling headings have the same title, they get disambiguated IDs: `Section Title@L42` (with the line number).
- Fenced code blocks (``` ``` ``` and `~~~`) are respected — headings inside code are not parsed.

## Error Handling

Errors are returned as plain text in the format:

```
[ERROR: Type] Detail message
  key: value
```

| Error | Meaning |
|-------|---------|
| `NotConfigured` | No `[knowledge] files` in `.agent-pty.toml` |
| `FileNotAllowed` | File not in the configured allowlist |
| `SectionNotFound` | Section ID doesn't match any heading |
| `AmbiguousSection` | ID matches multiple headings (use disambiguated ID) |
| `Conflict` | `confirm_hash` doesn't match current content |

## Conflict Safety

For concurrent multi-agent use:

1. **Read** the section → note the `hash` from the header line
2. **Write/update** with `confirm_hash=<that hash>`
3. If another agent edited the section between your read and write, you get a `Conflict` error with the `current_hash`
4. Re-read, merge if needed, retry

Use `dry_run=true` to preview any mutation as a unified diff before committing.

## Tips

- **Section IDs are full paths.** `kb_schema` always shows the copyable `id:` when it differs from the title. Use that value with `kb_read`/`kb_write`.
- Use `kb_schema(root_depth=2)` for docs with a single H1 wrapper — jumps straight to the real sections.
- Use `kb_search_headers` before browsing — often faster than walking the tree.
- Use `kb_search_content` like grep — find the snippet, then `kb_read` the full section.
- The `file` parameter is optional when only one knowledge file is configured.
- Headings inside fenced code blocks are ignored — safe for docs with code examples.
- To target a different repo, launch the MCP server from that repo's harness `cwd` instead of switching roots through KB tools.
