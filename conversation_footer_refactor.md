# Conversation Footer Refactor (Phase 1 + Phase 3)

This document captures the full plan for refactoring the Codex Agent conversation footer and associated controls, based on the current requirements and the phased execution.

## Goals

- Make the footer layout clearer and more predictable on mobile by switching to a two-row grid.
- Remove confusing or obsolete labels and turn some elements into placeholders for upcoming features.
- Move websocket status into the header and repurpose footer cells for “context compact” and “mention”.
- Introduce a shared warning modal to support actions such as context compaction.
- Add “mention” behavior that inserts a path token into the message box and can also be invoked via REST.

## Phase 1 (UI only)

### 1. Approval pill defaults

- If no approval value is currently set in the SSOT (conversation settings), display the pill text as `default`.
- This affects the footer approval dropdown pill value only. It does *not* change the underlying setting.

### 2. Two-row footer grid

Replace the current footer flex layout with a 2-row, 4-column grid that uses consistent positions.

**Row 1**
- **Col A:** Approval
- **Col B:** Websocket placeholder
- **Col C:** Messages
- **Col D:** (empty)

**Row 2**
- **Col A:** Scroll pin
- **Col B:** JS placeholder
- **Col C:** Tokens
- **Col D:** Interrupt (alone, bottom-right)

**Notes**
- Keep everything edge-to-edge and consistent with the banded layout.
- Use the same typographic scale as existing pills and controls.
- The websocket and js pills are placeholders during Phase 1 (actual behavior changes in Phase 3).

## Phase 3 (Code + behavior changes)

### 3. Context compact control

**Footer changes**
- Replace the websocket placeholder pill with **Context**:
  - Label: `context:`
  - Pill value: `default` (or current context remaining value when available)
  - The pill becomes clickable and triggers a `context compact` flow.

**Header changes**
- Move websocket status pill into the header.
- The header pill has *no* label. It only shows `connected` / `disconnected`.

**Modal**
- Add a shared warning modal in `static/modals/` that can be reused.
- The context pill click opens this modal first.

### 4. Mention pill (footer)

Replace the JS pill with **Mention**:

- No label, only a pill.
- Clicking opens the file picker.
- Selecting a file/dir inserts a mention token into the message box:
  - The message text gets a backticked path: `` `path` ``
  - The UI renders that mention token with a light grey background.
  - The backticks are not visible in the rendered token.
  - The token can be deleted from the message input.

**REST endpoint**
- Add a REST endpoint that takes a path (absolute or relative).
- When called, it inserts the same mention token into the message input (like UI selection).

## UI Structure Overview (Target)

```
Footer grid (2 rows x 3 cols)

Row 1:
  Col A: Approval
  Col B: Context (placeholder in Phase 1; functional in Phase 3)
  Col C: Messages
  Col D: (empty)

Row 2:
  Col A: Scroll pin
  Col B: Mention (placeholder in Phase 1; functional in Phase 3)
  Col C: Tokens
  Col D: Interrupt
```

## Implementation Notes

- Use the existing custom dropdown styling for approval.
- Keep the footer pills aligned to the grid cells (no nested cards).
- Ensure the dropdown opens upward from the footer.
- Keep JS modular: new modal and mention logic should live in `static/modals/`.
- The warning modal should be shared (not context-specific) so it can be reused for other confirmations.

## Acceptance Checklist

- [ ] Footer uses 2-row grid layout with correct cell placements.
- [ ] Approval pill defaults to `default` when unset.
- [ ] Interrupt is alone in the bottom-right cell.
- [ ] Websocket + JS placeholders still visible in Phase 1.
- [ ] Context pill works (Phase 3) and moves ws status to header.
- [ ] Mention pill works (Phase 3) and inserts tokenized paths.
- [ ] Warning modal exists and is reusable.
- [ ] REST endpoint can insert mention tokens.
