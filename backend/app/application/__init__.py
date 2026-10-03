"""The shared application layer both adapters sit on (§3.1, §3.2).

REST and MCP are transports; an application case is one thing. This package owns permission checks,
business validation, the transaction and idempotency, and it must not know about `Request`,
`Response`, JSON-RPC or an MCP envelope - those belong to the adapters above it.
"""
