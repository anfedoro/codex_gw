from cgw.approval import map_approval_decision


def test_approve_once_maps_to_accept() -> None:
    payload, variant = map_approval_decision("approve_once")
    assert payload == {"decision": "accept"}
    assert variant == "accept"


def test_approve_all_similar_maps_to_session_accept_by_default() -> None:
    payload, variant = map_approval_decision("approve_all_similar")
    assert payload == {"decision": "acceptForSession"}
    assert variant == "acceptForSession"


def test_approve_all_similar_uses_execpolicy_amendment_when_present() -> None:
    payload, variant = map_approval_decision(
        "approve_all_similar",
        params={"proposedExecpolicyAmendment": ["git", "config", "--local"]},
    )
    assert variant == "acceptWithExecpolicyAmendment"
    assert payload["decision"]["acceptWithExecpolicyAmendment"]["execpolicy_amendment"] == [
        "git",
        "config",
        "--local",
    ]


def test_deny_maps_to_decline() -> None:
    payload, variant = map_approval_decision("deny")
    assert payload == {"decision": "decline"}
    assert variant == "decline"

