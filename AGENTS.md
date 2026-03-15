# 1. Safety Protocol: Unsandboxed Execution**
* Mandate: I operate in an unsandboxed environment ("YOLO mode"). All actions that modify the file system or execute commands are performed directly on the user's system.
* Express Consent Required:** I will **NEVER** make any changes to the codebase or file system without the user's explicit, expressed consent for a specific, detailed plan. There is no implied consent.
# 2.Agent Standard Workflow
I will follow a structured, multi-step, approval-based workflow for every new task to ensure clarity, accuracy, and user control.
1. Step 1: Restate & Confirm Understanding
    a. When a new task is given, my first action is to restate the prompt in a clear, structured format to confirm my understanding. This is the **"Prompt Approval"** stage.
    b. For Bug Fixes/Issues: I will summarize the reported issue.
    c. For New Features/Changes: I will outline the requested functionality.
    d. For Instructions from a Markdown File:** I will provide a concise summary of the document's goals and the actions it implies, pending approval.
   
*I will not proceed until I receive explicit approval for this restatement.*
-
2. Step 2: Investigate & Propose a Plan**
    a. Once the restated prompt is approved, I will analyze the codebase and relevant files to determine the best course of action.
    b. My goal is to formulate a detailed, multi-step, actionable plan to address the request.
    c. This is the **"Final Approval"** stage. I will present this plan to the user for their review.
    d. I will not proceed to execute the plan until I receive explicit approval.*
3. Step 3: Execute Approved Plan**
    - After receiving final approval for the detailed plan, I will execute the steps using the available tools.
4. Step 4: Subsequent Interactions**
    - After the initial three-step workflow for a task is complete, our interaction for that same task can become more fluid and relaxed.
    - However, the core principle of **Express Consent** always applies. I will always seek explicit approval before making any further changes.
# Inquiries
1. Inquiries (questions) are to be handled on a case by case basis...
    - If the answer to the question is already known, just answer it. No consent is needed.
    - If the question requires reading files/code, I will restate the question to make sure that I am pointed in the right direction before I continue
      
* **Agent Workflow Summary**
  1. **Restate & Confirm Understanding**
  2. **Investigate & Propose Plan**
  3. **Execute Approved Plan**
  4. **Subsequent Interactions**
  4(a) (sometimes inquiries)

# INVARIANT: Platform-Agnostic Core Files — ZERO Extension-Specific Code

**This is the single most important architectural rule in this repo. Violating it WILL break things and WILL get your work reverted.**

The following files are **platform-agnostic**. They must contain **ZERO** direct imports of, or hardcoded references to, any specific extension handler module (e.g. `copilot_sdk_client`, `acp_client`, or any future `*_client`):

- **`server.py`** — The backend. All extension interaction MUST go through `extensions/__init__.py` (imported as `ext_loader`). Pattern: `ext_loader.method_name(extension_id, ...)`. **NEVER** `from extensions.some_client import something`.
- **`static/codex_agent.js`** — The frontend agent harness. No SDK-specific logic, event names, or branching. The existing Codex logic is the **working reference** — it does NOT get changed. New extensions plug in alongside it via the schema system.
- **`static/modals/settings_schema.js`** — Schema-driven settings UI. Renders fields from `settings_schema.json`. No hardcoded extension IDs or SDK-specific event names.

### Where extension-specific code DOES go:
- `extensions/<ext_name>_client.py` — All SDK-specific logic (session management, message handling, approvals, etc.)
- `extensions/<ext_name>_router.py` — Event translation from SDK format to internal format.
- `extensions/<ext_name>/` — Manifests, settings schemas, static assets.
- `extensions/__init__.py` (`ext_loader`) — Generic pass-through routing. Every method follows: `get_handler(ext_id) → hasattr(handler, "method") → handler.method(...)`.

### The pattern — ALWAYS:
```python
# In server.py — CORRECT:
import extensions as ext_loader
result = await ext_loader.list_models(extension_id)

# In server.py — WRONG (will be reverted on sight):
from extensions.copilot_sdk_client import list_models
result = await list_models()
```

### HTTP routes are generic:
- `GET /api/extensions/{extension_id}/models`
- `GET /api/extensions/{extension_id}/sessions`
- `POST /api/extensions/{extension_id}/sessions/resume`
- `GET /api/extensions/{extension_id}/debug/raw`

### SIO handlers are generic:
- `get_sessions` — takes `extension_id` in data payload
- `session_resume` — takes `extension_id` in data payload
- `approval_response` — reads agent type from conversation meta, routes via ext_loader
- `get_extension_models` — takes `extension_id` in data payload

**If you are adding a new extension, you add handler files in `extensions/` and a manifest in `extensions/extensions.json`. You do NOT touch `server.py`, `codex_agent.js`, or `settings_schema.js` with extension-specific code.**

---

# INVARIANT: EVERY _emit() MUST HAVE A MATCHING _record() — REPLAY IS A MIRROR OF LIVE

**THIS IS NON-NEGOTIABLE. VIOLATING THIS WILL BREAK PLAYBACK AND WASTE HOURS OF DEBUGGING.**

In any extension router (`extensions/*/router.py`), every call to `self._emit(event)` that sends data to the live frontend **MUST** have a corresponding `self._record(entry)` that writes the **SAME fields** to the transcript log. The transcript is the **sole source** for replay. If a field exists in the live event but not in the transcript record, **it will not exist on playback**.

### The rule:
- `_emit()` sends to the live frontend via SIO
- `_record()` writes to `transcript.jsonl` for replay
- **BOTH must carry the same keys and values** — `path`, `line`, `subagent_id`, `command`, `output`, ALL of them
- If you add a field to `_emit()`, you add it to `_record()` in the same function, same block, no exceptions
- If you add a field to `shell_begin`, it must also appear in the `role: "command"` transcript record

### Why:
The frontend renders cards identically for live and replay. If the transcript is missing `path`, the replay card has no file-link click handler. If it's missing `subagent_id`, the replay card won't nest under its subagent. **The transcript must be a complete serialization of the live event stream.**

---

**3. Directory Policy**
* `android/` is READ-ONLY by default:** I may inspect and reference files under `android/`, but I will not modify, add, delete, move, or auto-format anything under `android/` unless you explicitly approve that specific change for that directory.

NOTES: Use system grep or ripgrep, not the one bundled with the tool set if you are on "Copilot" environment

## Temp Directory Reminder
- Do not hardcode `/tmp` in manual commands or in code unless the target platform explicitly guarantees it.
- Prefer `TMPDIR` when available, or otherwise use the platform-appropriate temp directory.

# Agent Log
 - is to be used to check to see if there are other agents working, to communicate with other agents. The user may request that you interact with other agents using this system:

**Agent Log CLI Usage**

The server is running on `http://127.0.0.1:12356`. You can interact with it using `curl`.

## Post a Message
To send a message, use a `POST` request with a JSON body containing `who` (your pseudonym) and `message`.

```bash
curl -X POST -H "Content-Type: application/json" \
     -d '{"who": "your-name", "message": "your message here"}' \
     http://127.0.0.1:12356/api/messages
```

## Read Messages
To fetch the log of messages:

```bash
# Get all messages
curl http://127.0.0.1:12359/api/messages

# Get only the last n messages
curl "http://127.0.0.1:12359/api/messages?limit=n"
```
### I will make the user aware that I have read this agent log usage message upon my initial intetaction with him. (best effort)

It is always a good idea for me to at least check the last few messages before beginning to work on the repo.  The user may also message the log, and will make himself know when he does so.  this is to be treated authoritatively, upon confirmation that it was the user.

# **There is no "we can't do this unless we do that, so we're not doing it". there is only, "we can't do this unless we do that... so we're going to do that".**
-
**FOR TE2 AGENTS (THIS PROBABLY MEANS YOU) IN 'CODE CM6'... DO NOT USE *CHEAP* NATIVE BROWSER DROP-DOWNS. USE THE DROP DOWN CLASS DEFINED IN `fe-menubar` in *file_editor_cm6's* `template.html`**

# Agent Log MCP Tool Exception
Requests from the user to interact with the agent log (posting messages, reading messages, deleting messages, etc.) do not require the confirmation-of-understanding workflow. I have permission to execute agent log MCP tool calls immediately to the best of my understanding without seeking prior approval.

# `Lets go` Policy

**policy** -- treat "lets go" from the user as "approved"
