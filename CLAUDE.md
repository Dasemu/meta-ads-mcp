# CLAUDE.md

## Overview
MCP server exposing the Meta (Facebook/Instagram) Marketing Graph API as tools —
campaigns, ad sets, ads, creatives, custom/lookalike audiences, insights,
custom conversions, lead forms, and raw Graph API passthrough. Single-file
Python server (`server.py`) built on the `mcp` SDK, served over
streamable-http.

## Commands
- Run: `python server.py` (reads `META_ACCESS_TOKEN` etc. from the environment)
- Install deps: `pip install -r requirements.txt` (or `venv/bin/pip install -r requirements.txt`)
- Lint/test: none configured yet. CI only does a Python syntax check
  (`python -m py_compile server.py`) until a linter/test suite is added.

## Architecture
- `server.py`: all tools, grouped by Graph API resource (campaigns, ad sets,
  ads, creatives, audiences, insights, labels, lead forms) plus shared
  helpers (`_request`, `_paged`, `_form`, `_query_value`, `_merge`) and the
  custom-audience PII hashing helpers (`_PII_NORMALIZERS`).
- No persistence layer: every tool call hits the Graph API directly with
  `META_ACCESS_TOKEN`.
- See `docs/deployment.md` for how this process is run in production.

## Conventions
- Keep all 55 tools in `server.py` grouped by resource with `# ---` section
  banners, matching the existing style — do not split into multiple modules
  without a stated reason.
- Tool docstrings are the only user-facing documentation (shown to the MCP
  client); keep them accurate and concise when a tool's behavior changes.
- Graph API POST/DELETE bodies go through `_form()`; GET query params go
  through `_query_value()` — don't hand-roll JSON-encoding for nested params.

## Workflow quirks
- No `development` branch exists; the default branch is `master`. Branch
  per task from `master`.
- `venv/` is checked into the working tree but is local tooling, not part of
  the shipped app (see `.gitignore`: only `metaads-mcp.env` is excluded).

## Deployment
- Deployed via Nixpacks (`nixpacks.toml`: `python server.py`), not Docker and
  not the standard systemd/LXC setup described in the global CLAUDE.md.
- Runtime secrets live in `metaads-mcp.env` (not `.env`); see
  `.env.example` for the variable names.

## Gotchas
- `META_ACCESS_TOKEN` is required (`os.environ[...]`, no default) — the
  process will not start without it.
- `META_DEFAULT_AD_ACCOUNT_ID` is optional; tools that need an ad account ID
  fall back to it via `_act()` and raise if neither is set.
