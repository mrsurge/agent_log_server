# Safety Protocol

## Unsandboxed Execution

- **Mandate:** I operate in an unsandboxed environment ("YOLO mode"). All actions that modify the file system or execute commands are performed directly on the user's system.
- **Express Consent Required:** I will **NEVER** make any changes to the codebase or file system without the user's explicit, expressed consent for a specific, detailed plan. There is no implied consent.

# Agent Standard Workflow

I will follow a structured, multi-step, approval-based workflow for every new task to ensure clarity, accuracy, and user control.

## Step 1: Restate & Confirm Understanding

1. When a new task is given, my first action is to restate the prompt in a clear, structured format to confirm my understanding. This is the **"Prompt Approval"** stage.
2. For bug fixes/issues, I will summarize the reported issue.
3. For new features/changes, I will outline the requested functionality.
4. For instructions from a Markdown file, I will provide a concise summary of the document's goals and the actions it implies, pending approval.

*I will not proceed until I receive explicit approval for this restatement.*

## Step 2: Investigate & Propose a Plan

1. Once the restated prompt is approved, I will analyze the codebase and relevant files to determine the best course of action.
2. My goal is to formulate a detailed, multi-step, actionable plan to address the request.
3. This is the **"Final Approval"** stage. I will present this plan to the user for their review.
4. I will not proceed to execute the plan until I receive explicit approval.

## Approval Tool Hierarchy

When requesting prompt approval or final plan approval for work on this repo, I will use this order:

1. built-in harness user-input or approval tool, when available
2. MCP user-input or approval tool, when no built-in tool is available
3. plain assistant message only when no approval tool is available

If a higher-priority approval tool is available, I will actually use it. I will not skip to a lower-priority method just because it is simpler or more convenient to write.

I should prefer in-turn approval tools because they preserve reasoning, investigation context, and plan state that would otherwise be lost across turns.

When using an approval or user-input tool such as `ask_user`, I will include at least one explicit button/choice option. Freeform input may be allowed in addition to that, but freeform alone does not satisfy the approval prompt requirement when a choice-capable tool is available.

## Step 3: Execute Approved Plan

- After receiving final approval for the detailed plan, I will execute the steps using the available tools.

## Step 4: Subsequent Interactions

- After the initial three-step workflow for a task is complete, our interaction for that same task can become more fluid and relaxed.
- However, the core principle of **Express Consent** always applies. I will always seek explicit approval before making any further changes.

# Inquiries

1. Inquiries (questions) are to be handled on a case-by-case basis.
2. If the answer to the question is already known, just answer it. No consent is needed.
3. If the question requires reading files/code, I will restate the question to make sure that I am pointed in the right direction before I continue.

## Agent Workflow Summary

1. **Restate & Confirm Understanding**
2. **Investigate & Propose Plan**
3. **Execute Approved Plan**
4. **Subsequent Interactions**
5. Sometimes inquiries

For Step 1 prompt approval and Step 2 final plan approval, I will use the approval-tool hierarchy in this order:
1. built-in harness user-input or approval tool
2. MCP user-input or approval tool
3. plain assistant end-of-turn message only if no approval tool is available

If I use a choice-capable approval tool in Step 1 or Step 2, the prompt must include at least one explicit button/choice option. Optional freeform input may supplement the prompt, but it will not replace the button choice.

# Workflow Scope

- This workflow governs work on this repo.
- This repo is harness infrastructure used to work on many other repos.
- I will not assume downstream target repos inherit this repo's workflow or approval rules unless those repos explicitly define them.

# Invariant: Platform-Agnostic Core Files — Zero Extension-Specific Code

**This is the single most important architectural rule in this repo. Violating it WILL break things and WILL get your work reverted.**

The following files are **platform-agnostic**. They must contain **ZERO** direct imports of, or hardcoded references to, any specific extension handler module (for example `copilot_sdk_client`, `acp_client`, or any future `*_client`):

- **`server.py`** — The backend. All extension interaction MUST go through `extensions/__init__.py` (imported as `ext_loader`). Pattern: `ext_loader.method_name(extension_id, ...)`. **NEVER** `from extensions.some_client import something`.
- **`static/codex_agent.js`** — The frontend agent harness. No SDK-specific logic, event names, or branching. The existing Codex logic is the **working reference** — it does NOT get changed. New extensions plug in alongside it via the schema system.
- **`static/modals/settings_schema.js`** — Schema-driven settings UI. Renders fields from `settings_schema.json`. No hardcoded extension IDs or SDK-specific event names.

## Where Extension-Specific Code Does Go

- `extensions/<ext_name>_client.py` — All SDK-specific logic (session management, message handling, approvals, etc.)
- `extensions/<ext_name>_router.py` — Event translation from SDK format to internal format.
- `extensions/<ext_name>/` — Manifests, settings schemas, static assets.
- `extensions/__init__.py` (`ext_loader`) — Generic pass-through routing. Every method follows: `get_handler(ext_id) → hasattr(handler, "method") → handler.method(...)`.

## The Pattern — Always

```python
# In server.py — CORRECT:
import extensions as ext_loader
result = await ext_loader.list_models(extension_id)

# In server.py — WRONG (will be reverted on sight):
from extensions.copilot_sdk_client import list_models
result = await list_models()
```

## HTTP Routes Are Generic

- `GET /api/extensions/{extension_id}/models`
- `GET /api/extensions/{extension_id}/sessions`
- `POST /api/extensions/{extension_id}/sessions/resume`
- `GET /api/extensions/{extension_id}/debug/raw`

## SIO Handlers Are Generic

- `get_sessions` — takes `extension_id` in data payload
- `session_resume` — takes `extension_id` in data payload
- `approval_response` — reads agent type from conversation meta, routes via `ext_loader`
- `get_extension_models` — takes `extension_id` in data payload

## Socket.IO Contract Rule

- Do not add HTTP fallback paths (`fallbackUrl`, fallback `fetch`, ad hoc POST/GET/PUT backup flows) for runtime UI/backend contracts unless the user explicitly approves that fallback in advance.

**If you are adding a new extension, you add handler files in `extensions/` and a manifest in `extensions/extensions.json`. You do NOT touch `server.py`, `codex_agent.js`, or `settings_schema.js` with extension-specific code.**

---

# Invariant: Existing Harness Conversation Reload Is Transcript-First, Extension Resume Is Lazy

**THIS IS A REPO-WIDE SESSION LIFECYCLE CONTRACT.**

- Reloading/selecting a conversation that already exists on our harness replays the local `transcript.jsonl` only.
- If a remote `thread_id` / `session_id` is already bound, the extension/backend stays cold until the first new send.
- That first send may fail against the cold backend; the extension then resumes/reattaches and retries the buffered message.
- Resume/load history noise must be suppressed until the backend ack because the local transcript is already present.
- New port-in/import flows are different: they may materialize transcript entries before the response returns.

See also:

- `acp/AGENT_EXTENSION_INTEGRATION.md` — canonical extension hook/lifecycle contract
- `CODEX_APP_SERVER_EXTENSION.md` — architecture/reference implementation manual

---

# Invariant: Every `_emit()` Must Have a Matching `_record()` — Replay Is a Mirror of Live

**THIS IS NON-NEGOTIABLE. VIOLATING THIS WILL BREAK PLAYBACK AND WASTE HOURS OF DEBUGGING.**

In any extension router (`extensions/*/router.py`), every call to `self._emit(event)` that sends data to the live frontend **MUST** have a corresponding `self._record(entry)` that writes the **SAME fields** to the transcript log. The transcript is the **sole source** for replay. If a field exists in the live event but not in the transcript record, **it will not exist on playback**.

## The Rule

- `_emit()` sends to the live frontend via SIO
- `_record()` writes to `transcript.jsonl` for replay
- **BOTH must carry the same keys and values** — `path`, `line`, `subagent_id`, `command`, `output`, all of them
- If you add a field to `_emit()`, you add it to `_record()` in the same function, same block, no exceptions
- If you add a field to `shell_begin`, it must also appear in the `role: "command"` transcript record

## Why

The frontend renders cards identically for live and replay. If the transcript is missing `path`, the replay card has no file-link click handler. If it's missing `subagent_id`, the replay card won't nest under its subagent. **The transcript must be a complete serialization of the live event stream.**

---

# Transcript Card Contract

- We do **not** create a platform-specific card type for transcript cards.
- We do create a **generic view/read card contract** that any router can target.
- The routers are responsible for emitting/filling that card shape correctly.

## Render That Generic Card in the Frontend

- Preserve the normal collapsible/header behavior.
- Use a generic renderer, not one tied to a specific extension.

## Frontend Validation

- Any change that affects the bundled frontend must be followed by `npm run build` before handoff.
- Frontend changes must also be validated with `npm run typecheck` before handoff.
- Do not assume source edits under `static/` are live until the bundle has been rebuilt.

---

# Directory Policy

- **`android/` is read-only by default:** I may inspect and reference files under `android/`, but I will not modify, add, delete, move, or auto-format anything under `android/` unless you explicitly approve that specific change for that directory.

## Temp Directory Reminder

- Do not hardcode `/tmp` in manual commands or in code unless the target platform explicitly guarantees it.
- Prefer `TMPDIR` when available, or otherwise use the platform-appropriate temp directory.

## Symlink Rule

UNDER NO CIRCUMSTANCES WILL I EVER USE `resolve()` or ANY SYMLINK RESOLOVING METHOD. EVER. (unless I AM EXPLICITLY AUTHORIZED TO)

# Search Discipline

## Do not blindly content-search high-noise roots

- Do **not** run blind recursive content searches (`rg`, `grep`, `ripgrep`, `grep -R`, etc.) from broad high-noise roots such as the repo root or the package root when the query could walk bundled/generated/vendor content.
- Name-only discovery in those places is fine (`find`, `glob`, `rg --files`, directory listings). The restriction is on blind **file-content** searching.
- Narrow to specific source directories first, then search content only inside the targeted source tree.

## Generated, bundled, minified, and vendor-heavy paths

- Treat the following as **no blind content-search** zones unless the user explicitly asks for one of them:
  - `node_modules/`
  - `build/`
  - `scripts/schema-extract/node_modules/`
  - `worktrees/**/build/`
  - `agent_log_server/static/dist/`
- Also avoid blind content searches in obvious bundled/minified artifacts anywhere in the repo, especially:
  - `*.min.js`
  - `*.min.css`
  - `*.bundle.js`
  - `*.map`
- Static source files are fine to inspect and search **when they are not minified/bundled**. If unsure, check the filename and file size first before searching contents.

## Conversations and framework-shell logs

- Do **not** blindly `rg`/`grep` conversation transcripts or framework-shell logs for content.
- The main log/cache roots to treat this way are:
  - `~/.cache/app_server/conversations/`
  - `~/.cache/framework_shells/runtimes/**/logs/`
- For those roots:
  - file-name listing and path discovery are fine
  - targeted content inspection must use a Python heredoc heuristic/parser tailored to the file format and the question being asked
  - prefer JSON-aware or line-scoped Python extraction over raw text grep so you do not drown in minified/noisy output or miss the real structured event boundary

# Agent Log

- The agent log is to be used to check to see if there are other agents working, and to communicate with other agents. The user may request that I interact with other agents using this system. (I will use the mcp tool if it is available to me, if it is, disregard the following agent log information)

## Agent Log CLI Usage

The server is running on `http://127.0.0.1:12356`. I can interact with it using `curl`.

### Post a Message

To send a message, use a `POST` request with a JSON body containing `who` (your pseudonym) and `message`.

```bash
curl -X POST -H "Content-Type: application/json" \
     -d '{"who": "your-name", "message": "your message here"}' \
     http://127.0.0.1:12356/api/messages
```

### Read Messages

To fetch the log of messages:

```bash
# Get all messages
curl http://127.0.0.1:12359/api/messages

# Get only the last n messages
curl "http://127.0.0.1:12359/api/messages?limit=n"
```

### Initial Acknowledgement

I will make the user aware that I have read this agent log usage message upon my initial interaction with him. Best effort.

### Usage Notes

It is always a good idea for me to at least check the last few messages before beginning to work on the repo. The user may also message the log, and will make himself known when he does so. This is to be treated authoritatively upon confirmation that it was the user.

# Execution Principle

**There is no "we can't do this unless we do that, so we're not doing it". There is only, "we can't do this unless we do that... so we're going to do that".**

**FOR TE2 AGENTS (THIS PROBABLY MEANS YOU) IN `CODE CM6`: DO NOT USE CHEAP NATIVE BROWSER DROP-DOWNS. USE THE DROP DOWN CLASS DEFINED IN `fe-menubar` in `file_editor_cm6`'s `template.html`.**

# Agent Log MCP Tool Exception

Requests from the user to interact with the agent log (posting messages, reading messages, deleting messages, etc.) do not require the confirmation-of-understanding workflow. I have permission to execute agent log MCP tool calls immediately to the best of my understanding without seeking prior approval.

# `Lets go` Policy

**Policy:** treat "lets go" from the user as approved.

## Agent Log Summaries

After making a round of successful edits that have been verified by the user, I will post a short summary of the edits made with files and line numbers (each with a short, one-line justification for each line number) onto the agent log (with the MCP tool if available). I will also identify the repo in this message.

## KB Memory MCP

- Prefer the KB MCP tools for repo-scoped durable contracts/workflows when a document is configured in KB.
- Use KB for stable shared knowledge; use the agent log for coordination, progress, and short-lived handoff messages.
- The KB tool guide for this repo is `KB_MEMORY_MCP.md`.
- Important KB quirks:
  - all KB tool output is plain text
  - `kb_schema` only shows child headings when drilling with an `id`
  - KB writes are patch-style; header hashes are informational only and `confirm_hash` is ignored
  - `kb_reload_config()` only reloads the current repo; KB root follows the harness cwd/repo root
  - `kb_add_file(abs_path)` only works for files inside the current project root
- For third-party extension install/update workflow, use `THIRD_PARTY_EXTENSION_WORKFLOW.md` as the contract doc and prefer KB reads/writes when it is loaded into KB.

## Agents working in this repo
- Disregard the developer instructions to restart framework shells and refresh front ends.  Ask user, then continue. (if I am working on this repo, then the harness I am running on is a child process of an agent extension)
