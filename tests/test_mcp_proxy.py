"""§13.3 / MCP-AC-22 / MCP-AC-29: the proxy contract, and what this process may believe about its peer.

Three things are checked here that an implementation can each get wrong in a way that only shows up in a
deployment:

* forwarding headers are trusted only from an address the operator named, and never from everyone;
* nothing an MCP client is shown - the resource URL, the metadata document, the console links - is rebuilt
  from the Host it happened to use;
* the shipped proxy and the shipped Compose file describe the same topology the code assumes.

The last group reads the real artifacts rather than a copy of them, because a sample configuration that
drifts from the settings it documents is the kind of bug an operator hits at 2 AM.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import uvicorn
import yaml
from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.main import main as serve
from backend.app.mcp.transport import METADATA_PREFIX
from starlette.testclient import TestClient

from .mcp_live import BASE, live_settings

REPO = Path(__file__).resolve().parents[1]
PROXY_CONF = REPO / "deploy" / "nginx-site.conf"
COMPOSE = REPO / "compose.yaml"
HOSTILE = "evil.example.com"
#: A container has to bind the interface the bridge network routes to; this string is asserted, never run.
BIND_ALL = "0.0.0.0"  # noqa: S104


def _default(name: str) -> Any:
    return Settings.model_fields[name].default


def _code(text: str) -> str:
    """The configuration minus its comments: a sentence about a directive is not the directive."""
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


def _blocks(text: str, keyword: str) -> list[str]:
    """Every top-level `keyword ... { ... }` body, matched on brace depth."""
    lines = text.splitlines()
    found: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if line.startswith(f"{keyword} ") and line.endswith("{"):
            start, depth = index, 0
            while index < len(lines):
                depth += lines[index].count("{") - lines[index].count("}")
                if depth == 0:
                    break
                index += 1
            found.append("\n".join(lines[start : index + 1]))
        index += 1
    return found


def _section(text: str, marker: str) -> str:
    for block in _blocks(text, marker.split()[0]):
        if marker in block:
            return block
    raise AssertionError(f"no block for {marker!r}")


def _directive(block: str, name: str) -> str:
    match = re.search(rf"^\s*{name}\s+(.+);$", block, re.MULTILINE)
    assert match is not None, f"{name} is not set"
    return match.group(1).strip()


def _bytes(value: str) -> int:
    return int(value[:-1]) * 1024 * 1024 if value.endswith("m") else int(value)


# --------------------------------------------------------------------------------------
# what the process is willing to believe about the peer in front of it
# --------------------------------------------------------------------------------------


def test_forwarding_headers_are_believed_by_no_one_until_an_operator_opts_in() -> None:
    assert _default("proxy_headers") is False
    assert _default("forwarded_allow_ips") == "127.0.0.1"


def test_trusting_every_peer_is_refused_before_the_process_starts() -> None:
    """`FORWARDED_ALLOW_IPS=*` is uvicorn's way of saying "believe any caller" (§13.3)."""
    with pytest.raises(ValueError, match="forwarded_allow_ips"):
        Settings(app_env="test", proxy_headers=True, forwarded_allow_ips="*").validate_runtime()

    # A list that is only partly a wildcard keeps the addresses and loses the wildcard, so the value that
    # reaches the server can never mean "everyone".
    mixed = Settings(app_env="test", proxy_headers=True, forwarded_allow_ips="10.0.0.5, *")
    mixed.validate_runtime()
    assert mixed.resolved_forwarded_allow_ips == "10.0.0.5"


def test_the_server_is_told_which_addresses_may_forward(monkeypatch: pytest.MonkeyPatch) -> None:
    """The trust list is passed explicitly, so uvicorn's own env fallback cannot quietly widen it."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(kwargs))
    settings = Settings(
        app_env="test",
        api_host=BIND_ALL,
        api_port=8001,
        proxy_headers=True,
        forwarded_allow_ips="10.0.0.5, *, 10.0.0.6",
    )
    monkeypatch.setattr("backend.app.main.get_settings", lambda: settings)
    serve()
    assert captured["host"] == BIND_ALL
    assert captured["proxy_headers"] is True
    assert captured["forwarded_allow_ips"] == "10.0.0.5,10.0.0.6"


def test_the_allowlist_has_to_name_the_host_the_clients_are_pointed_at() -> None:
    """A deployment whose public URL is not in its own Host allowlist refuses its own traffic."""
    matched = Settings(
        app_env="test",
        mcp_enabled=True,
        mcp_public_url="http://localhost:8000/mcp",
        mcp_allowed_hosts="localhost:8000,127.0.0.1:8000",
    )
    matched.validate_runtime()

    with pytest.raises(ValueError, match="mcp_allowed_hosts must name the host"):
        Settings(
            app_env="test",
            mcp_enabled=True,
            mcp_public_url="http://localhost:8000/mcp",
            mcp_allowed_hosts="127.0.0.1:8000",
        ).validate_runtime()


# --------------------------------------------------------------------------------------
# nothing a client is shown is built from the address it used
# --------------------------------------------------------------------------------------


def test_the_metadata_document_never_adopts_the_host_it_was_asked_about(database: Any, tmp_path: Any) -> None:
    """§5.3: `resource` comes from MCP_PUBLIC_URL, so a hostile Host injects nothing."""
    settings = live_settings(database, tmp_path)
    headers = {"Host": HOSTILE, "X-Forwarded-Host": HOSTILE, "X-Forwarded-Proto": "http"}
    with TestClient(create_app(settings), base_url=BASE) as client:
        for path in (METADATA_PREFIX, f"{METADATA_PREFIX}/mcp"):
            response = client.get(path, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["resource"] == settings.mcp_public_url.rstrip("/")
            assert HOSTILE not in response.text


# --------------------------------------------------------------------------------------
# the shipped proxy
# --------------------------------------------------------------------------------------


def test_the_proxy_forwards_the_public_paths_and_nothing_else() -> None:
    """The path list is closed: an extra `location` here is an extra route exposed to the internet."""
    code = _code(PROXY_CONF.read_text(encoding="utf-8"))
    blocks = _blocks(code, "location")
    paths = [block.splitlines()[0].strip().removeprefix("location").strip(" {") for block in blocks]
    assert sorted(paths) == sorted(["= /mcp", "/.well-known/", "/api/v1/", "/"])
    for path, block in zip(paths, blocks, strict=True):
        # The console is the fallback, so every route the API serves has to be named here on purpose.
        expected = "http://web:5173" if path == "/" else "http://aita_api"
        assert _directive(block, "proxy_pass") == expected, path


def test_the_mcp_location_keeps_the_path_and_streams_without_buffering() -> None:
    code = _code(PROXY_CONF.read_text(encoding="utf-8"))
    mcp = _section(code, "location = /mcp")
    # No URI part on `proxy_pass`: `http://aita_api/mcp` would rewrite, and the endpoint is the path.
    assert _directive(mcp, "proxy_pass") == "http://aita_api"
    assert _directive(mcp, "proxy_buffering") == "off"
    assert "proxy_read_timeout" in mcp


def test_the_proxy_body_limits_are_the_numbers_the_application_enforces() -> None:
    """Two files, one contract: a limit that only exists in nginx is a limit the API does not know."""
    code = _code(PROXY_CONF.read_text(encoding="utf-8"))
    assert _bytes(_directive(_section(code, "location = /mcp"), "client_max_body_size")) == _default(
        "mcp_max_request_bytes"
    )
    server = _blocks(code, "server")[-1]
    assert _bytes(_directive(server, "client_max_body_size")) == _default("max_request_body_bytes")


def test_the_proxy_sends_no_forwarded_host_and_logs_no_header_value() -> None:
    """It forwards the legitimate Host and nothing that would invite a rebuilt URL (§13.3)."""
    code = _code(PROXY_CONF.read_text(encoding="utf-8"))
    assert not [line for line in code.splitlines() if re.search(r"forwarded[-_]host", line, re.IGNORECASE)]
    assert re.search(r"^\s*proxy_set_header\s+Host\s+\$host;", code, re.MULTILINE), "the client's own Host is forwarded"
    log_format = _directive(code, "log_format")
    assert "$http_" not in log_format
    assert "$sent_http_" not in log_format
    assert "$query_string" not in log_format


def test_the_proxy_never_re_issues_a_request_it_received_over_plaintext() -> None:
    """The port-80 block only moves traffic to TLS: no upstream, no `$host` in the redirect target."""
    code = _code(PROXY_CONF.read_text(encoding="utf-8"))
    plain = [block for block in _blocks(code, "server") if "listen 80" in block]
    assert len(plain) == 1
    assert "proxy_pass" not in plain[0]
    redirect = _directive(plain[0], "return")
    assert redirect.startswith("301 https://")
    assert "$host" not in redirect
    tls = _blocks(code, "server")[-1]
    assert "listen 443 ssl" in tls
    assert "ssl_certificate" in tls


# --------------------------------------------------------------------------------------
# the shipped development topology
# --------------------------------------------------------------------------------------


def test_the_development_compose_reaches_the_api_it_publishes() -> None:
    """§13.2: the container binds the interface the network can route to; loopback is the published address."""
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    assert services["api"]["environment"]["API_HOST"] == BIND_ALL
    published = {name for name, service in services.items() if service.get("ports")}
    assert published == {"api", "web"}
    for name, service in services.items():
        for mapping in service.get("ports", []):
            assert mapping.startswith("127.0.0.1:"), (name, mapping)
