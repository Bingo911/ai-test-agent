"""The public error contract (§13.1) and the compile policy defaults (§6.2).

These are wire-level promises a client builds against, so they are pinned here rather than left to
the drift of whatever a service happened to raise last.
"""

from __future__ import annotations

import inspect

from backend.app.api.cases import CompileRequest
from backend.app.domain.errors import ApiError, ErrorCode


def test_status_split_matches_the_documented_classes():
    """413 is size, 422 is syntax or semantics, 409 is a state conflict — never interchangeable."""
    assert ApiError(ErrorCode.CASE_TOO_LARGE, "too big").http_status == 413
    assert ApiError(ErrorCode.PAYLOAD_TOO_LARGE, "too big").http_status == 413
    assert ApiError(ErrorCode.SEMANTIC_ERROR, "no such variable").http_status == 422
    for code in (
        ErrorCode.DSL_AMBIGUOUS_SYNTAX,
        ErrorCode.TARGET_NEEDS_LOCATOR,
        ErrorCode.ACTION_UNSUPPORTED,
        ErrorCode.IR_VALIDATION_FAILED,
        ErrorCode.VARIABLE_UNDECLARED,
        ErrorCode.SECRET_FIELD_NOT_ALLOWED,
    ):
        assert ApiError(code, "case says something the compiler cannot honour").http_status == 422, code
    for code in (
        ErrorCode.CONFLICT,
        ErrorCode.VERSION_CONFLICT,
        ErrorCode.IDEMPOTENCY_CONFLICT,
        ErrorCode.COMPILE_REVIEW_REQUIRED,
        ErrorCode.COMPILE_STALE_DIGEST,
    ):
        assert ApiError(code, "the resource moved").http_status == 409, code


def test_envelope_carries_code_message_and_details():
    error = ApiError(
        ErrorCode.FORBIDDEN, "Permission 'case_write' is required", details={"permission": "case_write"}
    ).as_envelope("req_1")
    assert error == {
        "error": {
            "code": "FORBIDDEN",
            "message": "Permission 'case_write' is required",
            "request_id": "req_1",
            "details": {"permission": "case_write"},
        }
    }


def test_ai_compilation_is_opt_in_at_every_layer():
    """A client that names no policy gets the deterministic compiler, so no save or recompile is a model call."""
    from backend.app.services.cases import compile_now, request_compile

    assert CompileRequest().use_ai is False
    # The policy lives in the signatures the save and recompile paths call.
    assert inspect.signature(request_compile).parameters["use_ai"].default is False
    assert inspect.signature(compile_now).parameters["use_ai"].default is False
