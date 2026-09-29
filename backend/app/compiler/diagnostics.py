"""Compiler diagnostics (§4.3)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["ERROR", "WARNING", "INFO"]


class SourceRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class Diagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    severity: Severity = "ERROR"
    message: str
    step_id: str | None = None
    source_range: SourceRange | None = None
    field: str | None = None
    suggestion: str | None = None

    @classmethod
    def build(
        cls,
        code: str,
        message: str,
        *,
        severity: Severity = "ERROR",
        step_id: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        field_name: str | None = None,
        suggestion: str | None = None,
    ) -> Diagnostic:
        source_range = None
        if start_line is not None:
            source_range = SourceRange(start_line=start_line, end_line=end_line or start_line)
        return cls(
            code=code,
            message=message,
            severity=severity,
            step_id=step_id,
            source_range=source_range,
            field=field_name,
            suggestion=suggestion,
        )

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class DiagnosticList(list):
    """Collects diagnostics instead of raising so the API can return every problem at once."""

    def add(self, diagnostic: Diagnostic) -> None:
        self.append(diagnostic)

    @property
    def has_errors(self) -> bool:
        return any(item.severity == "ERROR" for item in self)

    def errors(self) -> list[Diagnostic]:
        return [item for item in self if item.severity == "ERROR"]

    def as_dicts(self) -> list[dict[str, Any]]:
        return [item.as_dict() for item in self]


class DslError(Exception):
    """Fatal structural error raised while the document cannot be split into steps at all."""

    def __init__(self, code: str, message: str, *, line: int | None = None, field_name: str | None = None) -> None:
        self.diagnostic = Diagnostic.build(code, message, start_line=line or 1, field_name=field_name)
        super().__init__(message)

    @property
    def code(self) -> str:
        return self.diagnostic.code

    @property
    def message(self) -> str:
        return self.diagnostic.message

    @property
    def line(self) -> int | None:
        return self.diagnostic.source_range.start_line if self.diagnostic.source_range else None
