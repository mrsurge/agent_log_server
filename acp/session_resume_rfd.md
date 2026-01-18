# Resuming of existing sessions (RFD)

* Author(s): @josevalim
* Champion: @benbrandt

## Elevator pitch

Proposing adding the ability to resume existing sessions. Similar to "session/load" except it does not return previous messages.

## Status quo

While the spec provides a "session/load" command, not all coding agents implement it. This means once you close your editor/browser, you can't resume the conversation.

This is particularly a problem for agents that do not directly implement ACP and the functionality is implemented via a wrapper.

## What we propose

Add a "session/resume" command and a capability `{ session: { resume: {} }`.

## Implementation

- `session/resume` - resumes without replaying history
- `session/load` - resumes AND replays history

If agent supports `session/load`, use it directly.
If agent only supports `session/resume`, a proxy/adapter can provide `session/load` on top by storing messages locally.

## Key insight

`session/resume` is the basic primitive which `session/load` builds on top of.
