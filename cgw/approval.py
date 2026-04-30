from __future__ import annotations

from typing import Any


def map_approval_decision(
    decision: str,
    *,
    params: dict[str, Any] | None = None,
    scope_hint: str | None = None,
    guidance_text: str | None = None,
) -> tuple[dict[str, Any], str]:
    """
    Map gateway-level approval decisions to codex app-server wire payload.
    Returns (wire_payload, mapped_variant_name_for_logs).
    """
    params = params or {}
    normalized = (decision or "").strip().lower()

    if normalized == "approve_once":
        return {"decision": "accept"}, "accept"

    if normalized == "approve_all_similar":
        amendment = params.get("proposedExecpolicyAmendment")
        if isinstance(amendment, list) and amendment:
            return {
                "decision": {
                    "acceptWithExecpolicyAmendment": {
                        "execpolicy_amendment": amendment,
                    }
                }
            }, "acceptWithExecpolicyAmendment"
        return {"decision": "acceptForSession"}, "acceptForSession"

    if normalized == "guidance":
        # Current app-server protocol does not support free-form guidance payload.
        # Preserve guidance in logs/UI and send a safe explicit decline.
        _ = scope_hint, guidance_text
        return {"decision": "decline"}, "decline"

    # deny and unknown values map to decline.
    return {"decision": "decline"}, "decline"

