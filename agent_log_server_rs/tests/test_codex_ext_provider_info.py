import unittest
from unittest.mock import AsyncMock, patch
from collections.abc import Awaitable, Callable
from typing import Dict, List, cast

from extensions.codex_ext import client


class _FakeTransport:
    def __init__(self, payload: Dict[str, object]) -> None:
        self._payload = payload

    async def rpc_request(self, method: str, *, timeout: float) -> Dict[str, object]:
        del method, timeout
        return self._payload


class CodexProviderInfoTests(unittest.IsolatedAsyncioTestCase):
    async def test_usage_includes_spend_control_and_reset_credits(self) -> None:
        payload: Dict[str, object] = {
            "rateLimits": {
                "limitId": "codex",
                "primary": {
                    "usedPercent": 75,
                    "windowDurationMins": 300,
                    "resetsAt": 1788748008,
                },
                "individualLimit": {
                    "limit": "100.00",
                    "remainingPercent": 40,
                    "resetsAt": 1788748008,
                    "used": "60.00",
                },
                "spendControlReached": False,
            },
            "rateLimitResetCredits": {
                "availableCount": 1,
                "credits": [
                    {
                        "status": "available",
                        "title": "Full reset",
                        "expiresAt": 1789949785,
                    }
                ],
            },
            "rateLimitUpsell": None,
        }
        transport = _FakeTransport(payload)
        auth_status: Dict[str, object] = {
            "status": "authenticated",
            "requires_openai_auth": True,
            "account_type": "chatgpt",
        }

        with patch.object(client, "_ensure_transport_ready", AsyncMock(return_value=transport)):
            usage = await client.get_usage_info("codex-ext", auth_status=auth_status)

        self.assertEqual(usage["state"], "available")
        detail = str(usage["detail"])
        self.assertIn("Spend control: 40% remaining", detail)
        self.assertIn("60.00 of 100.00 used", detail)
        self.assertIn("Rate-limit resets available: 1", detail)
        self.assertIn("Full reset: expires", detail)

    async def test_rate_limit_notification_invalidates_provider_info(self) -> None:
        handler = cast(
            Callable[..., Awaitable[List[Dict[str, object]]]],
            getattr(client, "_handle_auth_transport_event"),
        )
        with patch.object(client, "_registered_extension_ids", {"codex-ext"}):
            events = await handler(
                label="account/rateLimits/updated",
                payload={"rateLimits": {"limitId": "codex"}},
            )

        self.assertEqual(
            events,
            [
                {
                    "type": "provider_info_updated",
                    "extension_id": "codex-ext",
                    "provider": "codex",
                    "reason": "rate_limits_updated",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
