# Copilot model reasoning-effort normalization note

The vendored Copilot Python SDK should stay identical to upstream except for the
local stdio process-injection patch needed by the FWS transport.

One local vendored-only change previously normalized `ModelInfo` reasoning effort
metadata by collecting values from both `supportedReasoningEfforts` and nested
`capabilities.supports.reasoning_effort` / `reasoningEffort`.

That behavior is still useful for the settings UI because model-list payloads may
expose reasoning-effort support in either shape, but it should live in the
agent-log-server adapter/settings layer rather than in `extensions/copilot_sdk/_vendor`.

When the vendored SDK is refreshed, drop the SDK-local normalization and preserve
the intended behavior by normalizing model metadata after `list_models()` returns
from the SDK/runtime boundary.
