from fastapi import Request

from cgw.auth import extract_api_key_from_request


def _request_with_headers(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "method": "GET",
        "path": "/",
    }
    return Request(scope)


def test_extract_api_key_prefers_bearer() -> None:
    request = _request_with_headers(
        {
            "Authorization": "Bearer bearer-token",
            "x-api-key": "header-token",
        }
    )
    assert extract_api_key_from_request(request, "x-api-key") == "bearer-token"


def test_extract_api_key_from_custom_header() -> None:
    request = _request_with_headers({"x-api-key": "header-token"})
    assert extract_api_key_from_request(request, "x-api-key") == "header-token"

