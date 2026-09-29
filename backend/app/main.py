from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

from .parser import ParseError, compile_markdown

app = FastAPI(
    title="AI Test Agent API",
    version="0.1.0",
    description="Parse Markdown test cases into a validated, deterministic Test IR.",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Request-ID"],
)


class ParseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    markdown: Annotated[str, Field(min_length=1, max_length=262144)]


@app.get("/api/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "ai-test-agent-api", "version": app.version}


@app.get("/api/v1/capabilities")
def capabilities() -> dict[str, object]:
    return {
        "actions": ["open", "click", "input", "clear", "upload", "wait", "assert", "screenshot"],
        "browsers": ["chromium", "chrome"],
        "features": {
            "markdown_parser": "available",
            "test_ir": "1.0",
            "browser_execution": "planned",
            "ai_compilation": "planned",
            "human_handoff": "planned",
        },
    }


@app.post("/api/v1/cases/parse")
def parse_case(request: ParseRequest) -> dict[str, object]:
    try:
        ir, review_required = compile_markdown(request.markdown)
    except ParseError as exc:
        raise HTTPException(status_code=422, detail=exc.as_dict()) from exc
    return {
        "status": "NEEDS_REVIEW" if review_required else "SUCCEEDED",
        "ir": ir,
        "diagnostics": [],
    }
