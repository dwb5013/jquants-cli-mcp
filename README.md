# jquants-cli-mcp

MCP server that wraps the local [`jquants`](https://github.com/J-Quants/jquants-cli) CLI (Japanese stock market data via J-Quants API V2) and serves the CLI's version-matched skill as MCP resources.

Built on [FastMCP 3.x](https://gofastmcp.com). Works with Claude Code and any other MCP client that supports tools + resources.

## Why

The `jquants-cli-usage` skill documents command syntax, flags, plan gates, and endpoint names. If the skill drifts ahead of the installed CLI, the LLM generates commands that fail. This server sources the skill directly from the local CLI on every startup (`jquants skills add`), so skill and binary are version-locked by construction.

## Prerequisites

- macOS or Linux
- [`jquants`](https://github.com/J-Quants/jquants-cli) CLI on PATH (`brew install j-quants/tap/jquants` or `cargo install jquants`)
- [`uv`](https://docs.astral.sh/uv/) (Python 3.11+)
- A J-Quants account — run `jquants login` once before using API tools

## Install

```sh
git clone https://github.com/dwb5013/jquants-cli-mcp.git
cd jquants-cli-mcp
uv sync
```

## Register with Claude Code

The repo ships with `.mcp.json` — Claude Code offers to load it when you open the directory. Approve the prompt and the server is available in that project.

For **global** availability across all projects:

```sh
claude mcp add -s user jquants-cli \
  -- uv --directory /absolute/path/to/jquants-cli-mcp run jquants-cli-mcp
```

Confirm with `/mcp`; you should see `jquants-cli` connected with three tools and the `skill://jquants-cli-usage/*` resources.

## Tools

### `run_jquants(args, cwd?, timeout_sec?)`

Generic entry point. Runs `jquants <args>` and returns structured output. Global options (`--output`, `--save`, `-f`) must come **before** the subcommand — this is a clap parser requirement on the CLI side.

Example:
```json
{
  "args": ["--output", "csv", "--save", "out.csv",
           "eq", "daily", "--code", "86970", "--from", "2026-01-01"]
}
```

Returns `{command, cwd, exit_code, stdout, stderr, stdout_truncated, stderr_truncated, timed_out}`. Each stream is capped at 1 MiB; use `--save` for bigger payloads.

### `jquants_schema(endpoint?)`

Field-schema discovery. No authentication required. Call with no args to list every endpoint, or with `endpoint="eq.daily"` (in `category.command` form) for a specific endpoint's fields. Use before building `-f` field lists — names are PascalCase and the CLI rejects mismatches.

### `jquants_version()`

Returns the installed `jquants` CLI version string. Useful for sanity checks.

## Skill resources

The server publishes one resource tree at startup:

| URI | Purpose |
|---|---|
| `skill://jquants-cli-usage/SKILL.md` | Entry point — command flow, plan gates, schema rules |
| `skill://jquants-cli-usage/_manifest` | File index with SHA256 hashes |
| `skill://jquants-cli-usage/{path*}` | Any file in the tree (e.g. `references/commands-eq.md`) |

Clients that auto-load MCP resources pick up the skill without extra setup. Content is regenerated from the installed CLI on every server start, written to `<parent>/jquants-cli-usage/`.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `JQUANTS_SKILLS_PARENT_DIR` | process cwd | Directory that will contain the generated `jquants-cli-usage/` folder. With the shipped `.mcp.json` this is the repo root. |

J-Quants CLI env vars (`JQUANTS_API_KEY`, `JQUANTS_BASE_URL`) are honored transparently — the MCP server just forwards them.

## Failure behavior

The server fails fast with `exit 1` and a two-line stderr message if it can't produce a skill tree matching the installed CLI:

```
[jquants-cli-mcp] FATAL: <reason>
[jquants-cli-mcp] hint:  <actionable fix>
```

Triggers: `jquants` binary missing, skill parent dir not writable, `jquants skills add` non-zero exit, or post-install `SKILL.md` missing. Degradation is intentionally avoided — a stale or empty skill silently corrupts LLM command-building, which is strictly worse than an obvious startup error.
