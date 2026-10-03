"""MCP adapter layer: the second transport in front of the shared application (§3.1).

Nothing in this package holds a browser, publishes to a queue, or decides a permission by itself; it
translates the MCP wire into the same application use cases the REST console calls.
"""
