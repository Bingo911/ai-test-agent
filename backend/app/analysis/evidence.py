"""A bounded local projection of a raw Playwright trace, with no page content or input values."""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections import OrderedDict
from typing import Any

_API_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,79}$")
_READ_BUDGET = 4 * 1024 * 1024
_LINE_LIMIT = 64 * 1024


def trace_summary(data: bytes) -> list[dict[str, Any]]:
    """Read action names/timings only; never extract archives or copy params, errors or snapshots."""
    actions: OrderedDict[str, dict[str, Any]] = OrderedDict()
    remaining = _READ_BUDGET
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            if not member.filename.endswith(".trace") or remaining <= 0:
                continue
            with archive.open(member) as stream:
                while remaining > 0:
                    line = stream.readline(min(_LINE_LIMIT, remaining) + 1)
                    remaining -= len(line)
                    if not line or remaining < 0:
                        break
                    if len(line) > _LINE_LIMIT:
                        break
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    call_id = event.get("callId")
                    if not isinstance(call_id, str):
                        continue
                    if event.get("type") == "before":
                        name = event.get("apiName")
                        if not isinstance(name, str) or not _API_NAME.fullmatch(name):
                            continue
                        actions[call_id] = {"action": name, "start_ms": event.get("startTime")}
                        if len(actions) > 12:
                            actions.popitem(last=False)
                    elif event.get("type") == "after" and call_id in actions:
                        actions[call_id]["failed"] = bool(event.get("error"))
                        end = event.get("endTime")
                        start = actions[call_id].pop("start_ms", None)
                        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                            actions[call_id]["duration_ms"] = max(0, int(end - start))
    return [{key: value for key, value in action.items() if key != "start_ms"} for action in actions.values()]
