"""§11: the page cursor and the keyset boundary it points back at.

A cursor is the only state an assistant can carry between MCP calls, so these cases are about what it
may and may not conclude from one:

* a bookmark is only valid for the query that minted it - wrong kind, wrong tenant, wrong project, wrong
  filter set or a renumbered version all get the same refusal, because continuing any of them would
  answer page 3 of a question nobody asked;
* the token is decodable by whoever holds it, so it must carry identifiers and a digest only - never a
  raw filter value, never case content, never an identity;
* a malformed token is a caller error with a clear fix, never whichever exception the bytes happen to
  raise escaping as an INTERNAL answer;
* and the position it decodes to has to be *strictly* after the last row of the previous page: one row
  off in that comparison repeats a row or skips one, which is the failure a client cannot detect.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any

import pytest
from backend.app.mcp import cursors
from backend.app.mcp.cursors import CURSOR_VERSION, MAX_CURSOR_CHARS, Position, decode, encode, filter_digest
from backend.app.mcp.errors import ToolFailure
from backend.app.repositories.base import keyset_earlier, keyset_later
from sqlalchemy import Column, DateTime, Integer, String, create_engine, select
from sqlalchemy.orm import Session, declarative_base

KIND = cursors.KIND_PROJECTS
TENANT = "tenant-1"
STAMP = datetime(2026, 9, 1, 12, 30, 15, tzinfo=timezone.utc)
#: Naive, because that is what the development dialect hands back for a stored timestamp.
ANCHOR = datetime(2026, 9, 1, 12, 0, 0)


def _token(**overrides: Any) -> str:
    """A well-formed cursor for the default query, with whatever parts a case wants to change."""
    fields: dict[str, Any] = {
        "v": CURSOR_VERSION,
        "k": KIND,
        "t": TENANT,
        "f": filter_digest(project_id="project-1"),
        "j": "project-1",
        "p": [STAMP.isoformat(), "row-9"],
    }
    fields.update(overrides)
    text = json.dumps(fields, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _decode(value: str, **kwargs: Any) -> Position:
    arguments: dict[str, Any] = {"kind": KIND, "tenant_id": TENANT, "filters": filter_digest(project_id="project-1")}
    arguments.update(kwargs)
    arguments.setdefault("project_id", "project-1")
    return decode(value, **arguments)


# --------------------------------------------------------------------------------------
# the round trip, and what it must not contain
# --------------------------------------------------------------------------------------


def test_a_cursor_round_trips_the_position_it_minted():
    token = encode(
        kind=cursors.KIND_CASES,
        tenant_id=TENANT,
        filters=filter_digest(project_id="project-1", tag="smoke"),
        position=Position(key="2026-09-01T12:30:15+00:00", row_id="case-7"),
        project_id="project-1",
    )
    position = _decode(
        token,
        kind=cursors.KIND_CASES,
        filters=filter_digest(project_id="project-1", tag="smoke"),
    )
    assert position == Position(key="2026-09-01T12:30:15+00:00", row_id="case-7")


def test_a_cursor_carries_no_filter_value_no_content_and_no_identity():
    """The token is base64, not encrypted: anyone who reads one can open it (§11)."""
    secret = "the-login-password"
    token = encode(
        kind=cursors.KIND_CASES,
        tenant_id=TENANT,
        filters=filter_digest(search=secret),
        position=Position(key="42", row_id="case-7"),
    )
    padding = "=" * (-len(token) % 4)
    opened = json.loads(base64.urlsafe_b64decode((token + padding).encode("ascii")).decode("utf-8"))

    assert secret not in json.dumps(opened)
    # What it does carry: the query's shape, so a mismatch can be detected, and the position alone.
    assert opened["f"] == filter_digest(search=secret)
    assert opened["p"] == ["42", "case-7"]
    assert {"search", "subject", "issuer", "markdown"}.isdisjoint(opened)


def test_the_filter_digest_is_the_query_not_the_page_size():
    """Asking for 50 rows after 20 is the same question, so a wider page must still continue (§11)."""
    assert filter_digest(project_id="p", tag="smoke") == filter_digest(tag="smoke", project_id="p")
    assert filter_digest(project_id="p") != filter_digest(project_id="p", tag="smoke")
    assert filter_digest(project_id="p") != filter_digest(project_id="q")


def test_an_unknown_kind_is_a_build_fault_not_a_client_one():
    """A tool may not mint a cursor for a keyset that does not exist; that raises rather than refuses."""
    with pytest.raises(ValueError, match="unknown cursor kind"):
        encode(kind="widgets", tenant_id=TENANT, filters=filter_digest(), position=Position(key="1", row_id="2"))


# --------------------------------------------------------------------------------------
# match or refuse
# --------------------------------------------------------------------------------------


def test_a_matching_cursor_decodes():
    assert _decode(_token()).row_id == "row-9"


@pytest.mark.parametrize(
    "mismatch",
    [
        # A different keyset: two kinds can share a tenant and a filter digest and still mean different
        # positions, so the kind is part of the identity rather than a label.
        {"k": cursors.KIND_ENVIRONMENTS},
        # Another tenant's page, replayed here by a caller entitled to both.
        {"t": "tenant-2"},
        # Another project's rows: the same kind, the same tenant, a position in a different page.
        {"j": "project-2"},
        {"j": None},
        # A changed filter set - the search term came back different.
        {"f": filter_digest(project_id="other")},
        # A token from a build that numbered positions differently.
        {"v": CURSOR_VERSION + 1},
        {"v": 0},
        # Not a position at all.
        {"p": ["2026-09-01T12:30:15+00:00"]},
        {"p": ["", "row-9"]},
        {"p": ["2026-09-01T12:30:15+00:00", None]},
        {"p": [123, "row-9"]},
        {"p": "not-a-pair"},
    ],
)
def test_a_cursor_that_does_not_describe_this_query_is_refused(mismatch):
    token = _token(**mismatch)
    with pytest.raises(ToolFailure) as refused:
        _decode(token)
    failure = refused.value
    assert failure.code == "VALIDATION_ERROR"
    assert failure.details == {"reason": "cursor_does_not_match"}
    # The fix is the client's: read the first page again. Not a retry, and not a permission problem.
    assert failure.next_action.value == "narrow_request"


@pytest.mark.parametrize(
    "token",
    [
        "not-a-cursor-at-all",
        "!!!!not base64!!!!",
        base64.urlsafe_b64encode(b"just some bytes").decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(json.dumps(["a", "list"]).encode()).decode("ascii").rstrip("="),
        "",
    ],
)
def test_unparsable_bytes_are_the_same_refusal_as_a_mismatch(token):
    """Whatever the client sends back, it gets one answer: this is not a cursor for this query.

    A parse failure raised as an exception would surface as INTERNAL, which tells the caller the platform
    broke rather than that its bookmark is unusable - and the caller would retry forever.
    """
    with pytest.raises(ToolFailure) as refused:
        _decode(token)
    assert refused.value.details == {"reason": "cursor_does_not_match"}


def test_a_cursor_longer_than_the_wire_bound_is_refused_before_it_is_parsed():
    """The bound is the client's schema bound, so exceeding it is answered the same way as a bad token."""
    with pytest.raises(ToolFailure) as refused:
        _decode("A" * (MAX_CURSOR_CHARS + 1))
    assert refused.value.code == "VALIDATION_ERROR"
    assert "too long" in refused.value.message


# --------------------------------------------------------------------------------------
# reading the position as the query needs it
# --------------------------------------------------------------------------------------


def test_a_timestamp_page_reads_its_key_as_a_moment():
    assert Position(key=STAMP.isoformat(), row_id="row-9").as_timestamp() == STAMP


def test_a_naive_stored_moment_is_read_as_utc():
    """SQLite hands back naive datetimes, and a cursor must not inherit that ambiguity (§6.6)."""
    assert Position(key="2026-09-01T12:30:15", row_id="row-9").as_timestamp() == STAMP


@pytest.mark.parametrize(
    ("key", "reader"),
    [
        # A name key asked of a timestamp page: the client sent this project's cursor to that one, and
        # `decode` cannot tell because both are `key: str`.
        ("analytics", Position.as_timestamp),
        ("", Position.as_timestamp),
        ("2026-13-45", Position.as_timestamp),
        ("12.5", Position.as_integer),
        ("step-seven", Position.as_integer),
    ],
)
def test_a_key_that_is_not_the_position_the_page_needs_is_refused(key, reader):
    with pytest.raises(ToolFailure) as refused:
        reader(Position(key=key, row_id="row-9"))
    assert refused.value.details == {"reason": "cursor_does_not_match"}


# --------------------------------------------------------------------------------------
# the boundary itself
# --------------------------------------------------------------------------------------


#: Its own metadata: a test table must not appear in the platform's `create_schema()` output.
FixtureBase = declarative_base()


class _Pageable(FixtureBase):
    """A two-column table whose only job is to make the boundary comparison executable.

    The real pages order by `(created_at, id)` or `(step_no, id)`; the helpers take the column pair, so
    this is the same shape with fewer moving parts than a full project row. On its own metadata, so no
    other test's `create_schema()` ever sees it.
    """

    __tablename__ = "keyset_fixture"

    id = Column(String(36), primary_key=True)
    moment = Column(DateTime, nullable=False)
    step_no = Column(Integer, nullable=False)


@pytest.fixture
def pageable():
    engine = create_engine("sqlite://")
    _Pageable.metadata.create_all(engine)
    rows = [
        _Pageable(id="b", moment=datetime(2026, 9, 1, 12, 0, 0), step_no=2),
        _Pageable(id="a", moment=datetime(2026, 9, 1, 12, 0, 0), step_no=1),
        _Pageable(id="d", moment=datetime(2026, 9, 1, 11, 0, 0), step_no=4),
        _Pageable(id="c", moment=datetime(2026, 9, 1, 11, 0, 0), step_no=3),
        _Pageable(id="e", moment=datetime(2026, 9, 1, 10, 0, 0), step_no=5),
    ]
    with Session(engine) as session:
        session.add_all(rows)
        session.commit()
        yield session
    engine.dispose()


def _ids(statement, session) -> list[str]:
    return [str(value) for value, in session.execute(statement)]


def test_the_descending_keyset_is_strictly_after_its_anchor(pageable):
    """A shared timestamp is the whole reason for the tiebreaker: one row either side repeats or skips.

    Rows `b` and `a` (and `d`, `c`) have the same moment, so the page boundary has to be the pair, not
    the timestamp - `b, a, d, c, e` split after `a` must yield `d` and nothing else.
    """
    statement = (
        select(_Pageable.id)
        .where(keyset_earlier(_Pageable.moment, _Pageable.id, after=ANCHOR, after_id="a"))
        .order_by(_Pageable.moment.desc(), _Pageable.id.desc())
    )
    assert _ids(statement, pageable) == ["d", "c", "e"]


def test_the_descending_keyset_never_returns_the_anchor_itself(pageable):
    statement = (
        select(_Pageable.id)
        .where(keyset_earlier(_Pageable.moment, _Pageable.id, after=ANCHOR, after_id="b"))
        .order_by(_Pageable.moment.desc(), _Pageable.id.desc())
    )
    assert _ids(statement, pageable) == ["a", "d", "c", "e"]


def test_the_ascending_keyset_is_strictly_after_its_anchor(pageable):
    statement = (
        select(_Pageable.id)
        .where(keyset_later(_Pageable.step_no, _Pageable.id, after=3, after_id="c"))
        .order_by(_Pageable.step_no.asc(), _Pageable.id.asc())
    )
    assert _ids(statement, pageable) == ["d", "e"]


def test_the_boundary_is_a_pair_so_a_page_neither_repeats_nor_skips(pageable):
    """Walk the whole table one row at a time: every anchor must produce the exact remainder.

    This is the property a client cannot check for itself. A page that re-sends its anchor row shows up
    as a duplicated case in a listing, and one that skips shows up as a missing case - both look like
    platform data loss rather than a paging bug.
    """
    ordered = ["b", "a", "d", "c", "e"]
    moments = {
        "b": ANCHOR,
        "a": ANCHOR,
        "d": datetime(2026, 9, 1, 11, 0, 0),
        "c": datetime(2026, 9, 1, 11, 0, 0),
        "e": datetime(2026, 9, 1, 10, 0, 0),
    }
    for index, row_id in enumerate(ordered):
        statement = (
            select(_Pageable.id)
            .where(keyset_earlier(_Pageable.moment, _Pageable.id, after=moments[row_id], after_id=row_id))
            .order_by(_Pageable.moment.desc(), _Pageable.id.desc())
        )
        assert _ids(statement, pageable) == ordered[index + 1 :], f"after {row_id}"
