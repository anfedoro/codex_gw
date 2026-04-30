from __future__ import annotations

import hashlib

from fastapi import Request


def extract_bearer_token(authorization_header: str | None) -> str | None:
    if not authorization_header:
        return None
    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


def extract_api_key_from_request(request: Request, api_key_header: str) -> str | None:
    bearer = extract_bearer_token(request.headers.get("Authorization"))
    if bearer:
        return bearer
    if api_key_header:
        raw = request.headers.get(api_key_header)
        if raw:
            return raw.strip()
    return None


def token_fingerprint(token: str | None) -> str:
    if not token:
        return "none"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    return f"len={len(token)} sha256_12={digest}"

