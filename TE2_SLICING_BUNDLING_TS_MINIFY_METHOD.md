# TE2 Slicing / Bundling / TypeScript / Minified-Code Method

## 1) Slicing Method (High-Safety Refactor Workflow)

### Goal
Refactor large frontend files without regressions by extracting behavior in small, verifiable slices.

### Core rules
1. **One bounded slice at a time** (or one approved 8-slice batch).
2. **Wrapper-first extraction**: keep public/orchestrator functions in the source file first, delegate internals to a new module.
3. **Behavior parity first, cleanup second**: preserve runtime behavior before any stylistic/structural cleanup.
4. **No cross-cutting rewrites in the same batch**.
5. **Immediate validation after each batch**.

### Slice pattern
1. Identify a self-contained cluster (e.g., active-file utils, watcher rel helpers, git footer controls).
2. Extract pure helpers first.
3. Extract stateful helpers next via `createXxxUtils(deps)` dependency injection.
4. Rewire old functions to delegate to extracted helpers.
5. Keep callsites unchanged when possible.
6. Build + typecheck.
7. Runtime smoke check.

### Why this works
- Keeps diff surface small.
- Reduces risk of hidden coupling regressions.
- Lets us ship continuously while decomposing large files.

---

## 2) Bundling Method (TE2)

### Practical architecture used
- Host/editor assets are rebuilt into `static/dist/*` via project build scripts.
- Source decomposition happens in `static/js/...` and/or `src/host/...`, then bundle output is regenerated.

### Bundling tools + exact arguments used
- **Bundler:** `esbuild` (via `node build.mjs`)
- **Type checker:** `typescript` (`tsc --noEmit`)
- **NPM scripts:**
  - `npm run build` → `node build.mjs`
  - `npm run build:watch` → `node build.mjs --watch`
  - `npm run typecheck` → `tsc --noEmit`

### build.mjs configuration details
- Shared esbuild options:
  - `bundle: true`
  - `sourcemap: true`
  - `minify: !isWatch` (minified in normal build, not in watch mode)
  - `logLevel: 'info'`
  - `external: ['/static/vendor/*']`
- Host bundle:
  - `entryPoints: ['main.js']`
  - `outfile: 'static/dist/host.js'`
  - `format: 'esm'`
- Editor iframe bundle:
  - `entryPoints: ['monaco_editor/m_editor_app.js']`
  - `outfile: 'static/dist/editor.js'`
  - `format: 'iife'`

### Operational rule
After frontend source edits under `app/apps/file_editor_cm6/`, run:

```bash
cd app/apps/file_editor_cm6
npm run build --silent
npm run typecheck --silent
```

### Bundle safety principles
- Keep module boundaries explicit and narrow.
- Avoid introducing new runtime loading assumptions during refactors.
- Validate built artifacts every batch to catch integration drift early.

---

## 3) TypeScript Conversion Method (Staged, Runtime-Safe)

### Current constraint
Some `.ts` is browser-loaded directly at runtime in this stack. That means TS-only syntax can cause runtime failures if it reaches the browser untransformed.

### Conversion strategy
1. **Convert module-by-module**, not file-system-wide in one shot.
2. **Start with extracted modules** (already cleaner boundaries).
3. Keep runtime-safe syntax where direct-load paths still apply.
4. Tighten typing iteratively (avoid giant strictness flips in one commit).
5. Validate every batch with build + typecheck + runtime smoke.

### Anti-chaos practices
- Define clear typed interfaces at module boundaries.
- Prefer shared types over ad-hoc shape duplication.
- Remove `@ts-nocheck` incrementally, not all at once.
- Track “typed boundary completion” by subsystem (Explorer, Host, UI wiring, etc.).

---

## 4) Minified-Code Analysis Method (No-Clutter, Deterministic)

### Fast path (known file)
```bash
prettier /path/to/file.js 2>/dev/null | nl -ba | rg -n "pattern" | head -3
```

### Deterministic context extraction
If hit line is `12345`, extract context:
```bash
prettier /path/to/file.js 2>/dev/null | nl -ba | sed -n '12320,12380p'
```

### Unknown-file path
1. Find candidate files:
```bash
rg -l --hidden --no-ignore -g'*.js' -g'!*.map' "pattern" /path/to/tree
```
2. Run prettify+number+search per candidate.

### Why this is preferred
- Pretty-printing makes minified logic readable.
- `nl -ba` gives stable line references.
- Keeps workflow stream-based with minimal workspace clutter.

---

## 5) Validation Gates (Required)

For each completed slice/batch:

1. `npm run build --silent`
2. `npm run typecheck --silent`
3. Targeted runtime check of touched behavior

No batch is considered complete until all three pass.

---

## 6) Recommended Next Step for Full TS Push

Use the same slicing discipline, but convert each extracted Explorer module to TS in order of isolation:

1. pure utils
2. renderer modules
3. controller modules
4. event wiring/orchestrator boundaries

This keeps velocity high while preventing “rewrite shock” and regression cascades.
