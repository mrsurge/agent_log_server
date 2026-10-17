# Composer input tracker

- [x] User approves read-only investigation and concrete frontend correction.
- [x] Reproduce extra/misplaced newlines using actual composer runtime in memory;
  no live conversation content or stored drafts changed.
- [x] Shared newline/mention/caret projection, preserving intentional blank lines.
- [x] Pair/render/restore/sync regression coverage and whitespace-only restoration.
- [x] Escape uses button arm/confirm state with repeat/IME/popup/terminal guards.
- [x] `npm run test:composer`: 43 tests pass, including existing pairing/focus tests.
- [x] Final `npm run typecheck`, `npm run build` and `git diff --check` pass.
  Generated frontend bundle/maps rebuilt; no native executable rebuild needed.
- [ ] Separately approved ALS/client asset activation and reload.
- [ ] User mobile reconnect/reload/pairing and desktop Escape live acceptance.

Unrelated user draft in agent_log_server_rs/codec.py and existing scratch/dist
directories are preserved. No backend/runtime restart, package install, native
build, TE2 asset publication, commit or push is part of this implementation pass.
