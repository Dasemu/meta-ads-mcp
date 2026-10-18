# Deployment

The server is built and started via [Nixpacks](https://nixpacks.com)
(`nixpacks.toml`):

```
[start]
cmd = "python server.py"
```

It listens on `BIND_HOST:BIND_PORT` (defaults `127.0.0.1:8086`) using the
streamable-http MCP transport.

## Environment

Runtime configuration is read from environment variables — see
`.env.example` for the full list. In production these are supplied via a
`metaads-mcp.env` file (excluded from git by `.gitignore`), not a generic
`.env`.

## Required permissions

Some tools require specific Meta Graph API permissions on the access token
(e.g. `list_leads` requires `leads_retrieval`). Check the relevant tool's
docstring in `server.py` before relying on it.
