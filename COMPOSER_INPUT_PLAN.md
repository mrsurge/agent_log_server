# Composer input correctness and armed interrupt

## Scope

User reports mobile-authored extra newlines during bracket/quote completion and
newline multiplication after composer resync/reload. Add Escape interrupt using
the existing button's two-step, ten-second arm state. Read-only investigation and
the concrete frontend fix/test/build scope were explicitly approved. No runtime
restart, install, TE2 vendor refresh, OTA, APK or existing-draft cleanup is included.

## Source-backed defect

`composer/runtime.ts` previously appended a newline after every DIV/P and counted
every BR as a newline, including contenteditable caret placeholders. Direct
execution of the actual runtime reproduced:

- `hello<div><br></div>` serialized as `hello\n\n`.
- `<div>hello<br></div><div>world<br></div>` serialized as
  `hello\n\nworld\n\n`.
- `hello<div>world</div>` serialized as `helloworld\n`.

Pairing serializes/rebuilds the whole composer; persistence and synchronization
use the same serializer. Caret offsets were separately calculated by serializing
a cloned prefix, so partial blocks could introduce phantom trailing newlines.
These are reproduced defects and a plausible explanation of the reported mobile
symptoms, not a claim that every device-specific input sequence has been captured.

## Implemented direction

Pre-release timing follow-up: the shared client-local pairing/deletion gesture
window is now 1,100 ms rather than 800 ms (+37.5%). Exact caret/pair identity and
content-change cancellation remain mandatory; delayed input does not extend a
stale gesture indefinitely. Current validation/live gates are in the consolidated
TE2 pre-release tracker, not a new independent composer checklist.

1. `composer/text_projection.ts` owns block separators, terminal BR placeholders,
   mention atoms and UTF-16 DOM/text point mapping. Send text and draft envelopes
   supply different mention serializers to the same projection.
2. Draft render uses literal newline text under existing pre-wrap CSS. A final
   caret-placeholder BR follows trailing newlines without adding draft content.
   Save, pair, selection, remote application and restoration use the same text
   geometry. Preserve intentional blank lines and whitespace-only draft restores.
3. Escape and the existing interrupt button share the same arming/confirmation
   function and timeout. First Escape arms; a fresh second press confirms. Repeat,
   composing, modified or already-consumed events cannot confirm. Capture popup
   ownership before dismissal; act on bubble so closing a popup cannot also arm.
   Terminal input, visible dialogs/menus/pickers/mention lists and unavailable
   interrupt controls retain their own behavior. An arm from another conversation
   cannot confirm interruption of the newly selected conversation.
4. Regress actual runtime serialization/pairing/restoration plus text/caret maps,
   Unicode, mentions, blank lines and cross-client projections. Test button/key
   interoperation, expiry, held keys, conversation changes and dismissal ownership.
   Run `npm run test:composer`, `npm run typecheck`, `npm run build`.

## Safety and activation

Do not normalize stored drafts by collapsing repeated newlines: their intent is
unknowable. No Rust persistence/author-epoch or extension protocol change is
needed. Source frontend build is not activation: install/serve matching ALS
assets, refresh TE2's pinned ALS frontend snapshot and publish matching client
assets if interception is enabled, then reload the affected client. Those steps
require separate approval. No Code TE2 mypyc rebuild is needed for this change.

Live gate: mobile tail pairing, typing intentional blank lines, remote reconnect,
page reload and two-client consistency; desktop input/paste and armed Escape.
Track validation and acceptance in COMPOSER_INPUT_TRACKER.md.
