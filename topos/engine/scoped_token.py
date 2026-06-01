from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(raw: str) -> bytes:
    padding = "=" * ((4 - len(raw) % 4) % 4)
    return base64.urlsafe_b64decode((raw + padding).encode("ascii"))


def _sign(data: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), data.encode("utf-8"), hashlib.sha256).digest()
    return _b64url_encode(digest)


def create_scoped_invocation_token(claims: Dict[str, Any], *, secret: str) -> str:
    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True)
    encoded_payload = _b64url_encode(payload.encode("utf-8"))
    signature = _sign(encoded_payload, secret)
    return f"{encoded_payload}.{signature}"


class ScopedTokenValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_scoped_invocation_token(
    *,
    token: str,
    secret: str,
    resource_id: str,
    operation: str,
    request_id: Optional[str] = None,
    now_epoch_s: Optional[int] = None,
) -> Dict[str, Any]:
    if not token or "." not in token:
        raise ScopedTokenValidationError("TOKEN_INVALID", "missing or malformed invocation token")
    encoded_payload, signature = token.split(".", 1)
    expected = _sign(encoded_payload, secret)
    if not hmac.compare_digest(signature, expected):
        raise ScopedTokenValidationError("TOKEN_INVALID", "invalid token signature")
    try:
        claims = json.loads(_b64url_decode(encoded_payload).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ScopedTokenValidationError("TOKEN_INVALID", "token payload is not valid JSON") from exc
    if not isinstance(claims, dict):
        raise ScopedTokenValidationError("TOKEN_INVALID", "token payload must be an object")

    now_s = int(now_epoch_s if now_epoch_s is not None else time.time())
    exp = claims.get("exp")
    if not isinstance(exp, int):
        raise ScopedTokenValidationError("TOKEN_INVALID", "token exp claim is required")
    if now_s >= exp:
        raise ScopedTokenValidationError("TOKEN_EXPIRED", "invocation token has expired")

    token_resource_id = str(claims.get("resource_id") or "")
    if token_resource_id != resource_id:
        raise ScopedTokenValidationError("SCOPE_MISMATCH", "resource_id does not match token scope")

    allowed_ops = claims.get("allowed_operations")
    if not isinstance(allowed_ops, list) or not allowed_ops:
        raise ScopedTokenValidationError("TOKEN_INVALID", "allowed_operations claim is required")
    normalized_allowed = {str(v) for v in allowed_ops if str(v)}
    if "*" not in normalized_allowed and operation not in normalized_allowed:
        raise ScopedTokenValidationError("OPERATION_NOT_ALLOWED", "operation not allowed by token scope")

    token_request_id = str(claims.get("request_id") or "")
    if request_id and token_request_id and token_request_id != request_id:
        raise ScopedTokenValidationError("SCOPE_MISMATCH", "request_id does not match token scope")
    return claims
