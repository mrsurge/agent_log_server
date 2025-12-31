# Plan: Conversation Sidecar (Single-Conversation Mode)

Goal: introduce an internal conversation ID + sidecar directory so settings exist *before* a Codex thread is created. Bind the external `threadId` only after the first message, and store everything under the conversation sidecar. The UI is a **single page**: the splash view is the base, and the conversation view is a **full‑screen drawer overlay** that slides out and consumes the page.

## Plan

1) **Conversation sidecar + migration**
   - Create `~/.cache/app_server/conversations/<conversation_id>/`
   - Store `meta.json` (conversation_id, created_at, thread_id, settings, status)
   - Store `transcript.jsonl` (SSOT transcript)
   - Migrate the latest legacy transcript from `~/.cache/app_server/transcripts/*.jsonl` into the new sidecar and set `thread_id` from the legacy filename.

2) **SSOT config (active conversation + view state)**
   - Add `conversation_id` to `app_server_config.json` (active conversation)
   - Add `active_view` to `app_server_config.json` (`"splash"` or `"conversation"`)
   - Ensure both are created once and reused (single‑conversation mode for now)

3) **Thread ID binding**
   - Do **not** overwrite thread_id once set
   - Persist the thread_id into `meta.json` when the first `thread/start` response arrives

4) **Transcript logging**
   - Log transcripts by conversation_id (not thread_id)
   - Include diffs as `role: "diff"`

5) **Settings layering**
   - Only pass settings that were explicitly set in the sidecar (`meta.settings`)
   - If a setting is absent, omit it so codex app-server defaults apply

6) **Single-page UI (splash + drawer)**
   - Splash view is the base layout and lists conversations
   - Conversation view is a **drawer overlay** that slides out and fills the screen
   - The server decides which view is active based on SSOT (`active_view`, `conversation_id`)
   - Keep the drawer open state in sync with SSOT so refresh restores the same view

---

Status: pending implementation + verification.
