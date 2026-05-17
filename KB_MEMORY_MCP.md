# KB Memory MCP — Structured Markdown Knowledge Tools

This repo includes an MCP-based knowledge base system that lets agents read, search, navigate, and edit markdown files as structured data. Markdown headings define the schema; content between headings is the data.

All tools return **plain text** — no JSON escaping, no backslash-newline artifacts. Agents read it as-is.

## How It Works

- A `.agent-pty.toml` config file in the repo root declares which markdown files are knowledge sources.
- The MCP server parses heading hierarchy (`#`, `##`, `###`, etc.) into a navigable tree.
- Agents use MCP tools to browse, search, read, and write sections — no raw file parsing needed.
- All edits are atomic (temp file + fsync + rename).

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

**`kb_schema(file?/target?, id?, max_depth?, root_depth?)`**
Browse the heading index. Output is a compact complete list with one line per ATX heading.
- No `id`: returns every ATX heading in the target file.
- With `id`: returns every ATX heading in that section subtree.
- `target` is an alias for `file`, useful when thinking in terms of a selected
  KB target from `kb_list`. If both are supplied, they must name the same file.
- `root_depth`: treat a specific heading depth as top-level (useful for single-H1 docs — set `root_depth=2` to skip the wrapper).
- `max_depth`: optional narrowing only. Omit it for the full outline, or set `max_depth=0` for only the selected root headings.
- Each heading line includes a stable section number, header level, heading line,
  visible heading text, and copyable `id`.

**`kb_info(file?/target?, sections?, id?, max_chars?)`**
Inspect selected section context before reading full bodies.
- `sections` accepts schema numbers, `L<line>`, full ids, unique visible titles,
  or unique trailing id suffixes. Separate multiple selectors with commas or
  newlines, or pass a JSON array string.
- Returns the selected section's parent chain, each parent/target body range,
  subtree range, immediate child headings, and body previews.
- `max_chars` caps each body preview.

**`kb_read(file?/target?, sections?, id?, include_children?, max_chars?)`**
Read selected section bodies with parent context.
- For each selected target, returns parent headings plus parent body text from
  the top level down to the target, then the target heading/body.
- `include_children=true` returns the selected target subtree instead of only
  the target's own body.
- `max_chars` caps the aggregate returned text.
- `section=""` is the mutation-tool file-root selector; read tools use no selector to read the file root.

**Typical navigation flow:**
```
kb_list()                          → see configured files
kb_schema(target="KNOWLEDGE.md")   → see every heading in that target file
kb_info(target="KNOWLEDGE.md", sections="12,L80") → inspect context/ranges/previews
kb_read(target="KNOWLEDGE.md", sections="12,L80") → read parent context + target bodies
kb_schema(target="KNOWLEDGE.md", id="Architecture") → list headings in that subtree
```

### Search

**`kb_search_headers(file?/target?, query, max_hits?)`**
Case-insensitive substring search across heading titles. Returns matching headings with their section IDs so you can navigate to them.

**`kb_search(file?/target?, query, regex?, max_hits?, preview_chars?, from_match?)`**
Section-aware body search.
- `regex=false` does a literal case-insensitive search.
- `regex=true` treats `query` as a Python regular expression.
- `max_hits` caps results.
- `preview_chars` caps each body preview.
- `from_match=true` starts previews at the match; `false` starts previews at the
  beginning of the section body.

**`kb_search_content(...)`**
Alias for `kb_search`.

Use search to find candidate sections, then `kb_info` or `kb_read` the selected section numbers.

### Writing

**`kb_write(target?, section?, content, mode?, heading_title?, heading_depth?, dry_run?)`**
Returns a status line + unified diff.
- `mode="append"` (default): appends content at the end of the section body.
- `mode="heading"`: creates a new child heading with content. **Auto-inferred** when `heading_title` is provided.
- `mode="child"` and `mode="create_child"` are aliases for `mode="heading"`.
- `heading_depth`: defaults to parent depth + 1 if omitted.
- `section` accepts schema numbers, `L<line>` / `line:<line>`, heading paths, unique visible titles, unique trailing path suffixes, or `section=""` for the file root.
- `dry_run=true`: returns the diff without writing.
- KB writes do not send repo-memory IPC notifications; they report only the durable file mutation result.

### Updating

**`kb_update(target?, section?, content, mode?, dry_run?)`**
Returns a status line + unified diff.
- `mode="body"` (default): replaces only the section body. The heading and any child headings are preserved.
- `mode="replace"`: alias for `mode="body"`; it is still body-only replacement.
- `mode="subtree"`: replaces the heading and all descendants.
- `section=""` targets the full file.
- Supports `dry_run`.

### Removing

**`kb_remove(target?, section?, mode?, dry_run?)`**
Returns a status line + unified diff.
- `mode="subtree"` (default): removes the heading and all its children.
- `mode="body"`: removes only the body content and keeps the heading and child headings.
- `section=""` targets the full file.
- Supports `dry_run`.

### Management

**`kb_help()`**
List valid KB modes, section-id shorthands, and examples.

**Resource `kb://knowledge`**
Generic MCP resource discovery exposes a static `kb://knowledge` resource with
the configured KB file list and `kb_help()` output. KB file content is still read
through the section-aware KB tools.

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
- The full path ID is authoritative, but a unique visible heading title is accepted as shorthand.
- A unique trailing path suffix is also accepted as shorthand, for example `Child` or `Parent > Child` without the top-level wrapper.
- `kb_schema` section numbers are accepted by `kb_info` / `kb_read` / search follow-ups.
- `L<line>` or `line:<line>` targets a heading by source line.
- If two sibling headings have the same title, they get disambiguated IDs: `Section Title@L42` (with the line number).
- `section=""` means the file root for mutation tools.
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
| `InvalidParameter` | Tool parameter is invalid, such as an unsupported mode |
| `SectionNotFound` | Section ID doesn't match any heading |
| `AmbiguousSection` | ID matches multiple headings (use disambiguated ID) |

## Mutation model

KB mutations are patch-style text edits:

- `kb_write`, `kb_update`, and `kb_remove` do not perform explicit hash/CAS conflict checks.
- The `hash` shown in read headers is informational only.
- Mutation tools return patch-style diffs and write directly unless `dry_run=true`.
- Use `dry_run=true` to preview any mutation as a unified diff before committing.

## Tips

- **Section IDs are full paths.** `kb_schema` returns copyable `id` values for every heading.
- Unique visible section titles work as shorthand. If the shorthand is ambiguous, KB returns `AmbiguousSection` with candidates.
- Use `section=""` when you want to patch the file root directly.
- Use `kb_schema(target="...")` for a full document outline after `kb_list`.
  Use `root_depth=2` only when you intentionally want the outline roots to start at H2.
- Use `kb_search_headers` when you already know a heading fragment and want a targeted lookup.
- Use `kb_info` after schema/search when you need ranges, child headings, or previews before reading.
- Use `kb_search` like grep — find the snippet, then `kb_info` or `kb_read` the selected section.
- The `file` parameter is optional when only one knowledge file is configured.
- Headings inside fenced code blocks are ignored — safe for docs with code examples.
- To target a different repo, launch the MCP server from that repo's harness `cwd` instead of switching roots through KB tools.
