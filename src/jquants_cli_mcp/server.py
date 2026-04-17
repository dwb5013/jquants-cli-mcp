"""MCP server that runs the local `jquants` CLI and serves its bundled skill.

Built on FastMCP 3.x:
- Tools (run_jquants, jquants_schema, jquants_version) execute the CLI locally.
- On startup we invoke `jquants skills add --dir <parent>` so the CLI writes
  out the skill that matches its OWN version into `<parent>/jquants-cli-usage/`.
  SkillsDirectoryProvider then publishes that subtree as MCP resources.

Rationale for sourcing from the CLI (not GitHub): the skill documents command
syntax, flags, plan gates, and endpoint names. If the skill is ahead of the
locally-installed CLI, generated commands will fail. Binding skill version to
CLI version eliminates that drift by construction.

The parent directory defaults to the process cwd, overridable via
JQUANTS_SKILLS_PARENT_DIR. With the shipped .mcp.json, cwd is the project
root, so the skill lands at `<project>/jquants-cli-usage/`.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

from fastmcp import FastMCP
from fastmcp.server.providers.skills import SkillsDirectoryProvider

_JQUANTS_BIN = shutil.which("jquants") or "/opt/homebrew/bin/jquants"
_MAX_OUTPUT_BYTES = 1_000_000  # 1 MiB cap per stream
_DEFAULT_TIMEOUT = 180
_SKILL_NAME = "jquants-cli-usage"


def _resolve_bin() -> str:
    if Path(_JQUANTS_BIN).exists():
        return _JQUANTS_BIN
    found = shutil.which("jquants")
    if found:
        return found
    raise RuntimeError(
        "jquants binary not found. Install it first (brew/cargo) or set PATH."
    )


def _fatal(reason: str, hint: str) -> NoReturn:
    """Print a clear two-line error to stderr and exit non-zero.

    We fail fast instead of degrading because a stale or missing skill paired
    with a working CLI (or vice versa) silently corrupts LLM command-building.
    """
    print(f"[jquants-cli-mcp] FATAL: {reason}", file=sys.stderr)
    print(f"[jquants-cli-mcp] hint:  {hint}", file=sys.stderr)
    sys.exit(1)


def _default_parent_dir() -> Path:
    """Writable location for the generated skill tree.

    cwd is NOT a safe default: when this server is launched via
    `uvx --from git+...` as an MCP, cwd is often `/` (read-only on macOS).
    The user cache dir is always writable and conventional for generated
    artifacts. Respects XDG_CACHE_HOME.
    """
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "jquants-cli-mcp"


def _install_skill() -> Path:
    """Ask the local CLI to emit its version-matched skill, or exit.

    Returns the parent directory that contains `jquants-cli-usage/` — pass this
    to SkillsDirectoryProvider(roots=...). Any failure is fatal.
    """
    override = os.environ.get("JQUANTS_SKILLS_PARENT_DIR")
    parent = Path(override or _default_parent_dir()).expanduser().resolve()
    skill_dir = parent / _SKILL_NAME

    try:
        bin_path = _resolve_bin()
    except RuntimeError as e:
        _fatal(
            f"jquants CLI not found ({e})",
            "Install the CLI (brew / cargo) so `jquants` is on PATH, then restart.",
        )

    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _fatal(
            f"cannot create skill parent dir {parent}: {e!r}",
            "Set JQUANTS_SKILLS_PARENT_DIR to a writable directory.",
        )

    try:
        result = subprocess.run(  # noqa: S603
            [bin_path, "skills", "add", "--dir", str(parent)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        _fatal(
            f"failed to invoke `{bin_path} skills add`: {e!r}",
            f"Verify the binary is executable and {parent} is writable.",
        )

    if result.returncode != 0:
        stderr = (result.stderr or "").strip() or "(no stderr)"
        _fatal(
            f"`jquants skills add --dir {parent}` exited with code {result.returncode}",
            f"CLI stderr:\n{stderr}",
        )

    if not (skill_dir / "SKILL.md").exists():
        _fatal(
            f"`jquants skills add` reported success but {skill_dir}/SKILL.md is missing",
            "The installed CLI may not bundle the jquants-cli-usage skill for this "
            "version. Upgrade the CLI or report upstream.",
        )
    return parent


_INSTRUCTIONS = """\
This server runs the local `jquants` CLI and serves its version-matched skill.

HOW TO USE:
1. FIRST call `get_skill_guide()` to load the full SKILL.md (command syntax,
   plan gates, rate limits, schema rules). Also call `list_skill_references()`
   then `read_skill_reference(name)` for deeper category-specific docs (eq, mkt,
   deriv, fins, idx, bulk, plans, data-update-schedule).
2. Use `jquants_schema(endpoint)` before `-f`/--fields to get exact PascalCase
   field names.
3. Run commands via `run_jquants(args=[...])`. Global options
   (--output/--save/-f) MUST come before the subcommand (clap requirement).
"""


def _build_server() -> FastMCP:
    parent = _install_skill()
    skill_dir = parent / _SKILL_NAME
    print(
        f"[jquants-cli-mcp] skill: installed from CLI -> {skill_dir}",
        file=sys.stderr,
    )

    server: FastMCP = FastMCP("jquants-cli", instructions=_INSTRUCTIONS)
    # reload=True re-scans the skill tree on every resources/list request.
    # Keeps the provider robust to filesystem changes (e.g. CLI upgrade
    # rewriting SKILL.md in place) without requiring a server restart.
    server.add_provider(SkillsDirectoryProvider(roots=parent, reload=True))
    _register_tools(server, skill_dir)
    return server


def _truncate(text: str, limit: int = _MAX_OUTPUT_BYTES) -> tuple[str, bool]:
    data = text.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return text, False
    return data[:limit].decode("utf-8", errors="replace"), True


async def _run(
    args: list[str],
    cwd: str | None,
    timeout_sec: int,
) -> dict[str, Any]:
    bin_path = _resolve_bin()

    if args and args[0] == "jquants":
        args = args[1:]

    env = {**os.environ, "NO_COLOR": "1"}
    workdir = cwd or os.getcwd()

    proc = await asyncio.create_subprocess_exec(
        bin_path,
        *args,
        cwd=workdir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_sec
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {
            "command": [bin_path, *args],
            "cwd": workdir,
            "timed_out": True,
            "timeout_sec": timeout_sec,
            "exit_code": None,
            "stdout": "",
            "stderr": f"Process killed after exceeding timeout of {timeout_sec}s.",
            "stdout_truncated": False,
            "stderr_truncated": False,
        }

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    stdout_cut, stdout_trunc = _truncate(stdout)
    stderr_cut, stderr_trunc = _truncate(stderr)

    return {
        "command": [bin_path, *args],
        "cwd": workdir,
        "timed_out": False,
        "exit_code": proc.returncode,
        "stdout": stdout_cut,
        "stderr": stderr_cut,
        "stdout_truncated": stdout_trunc,
        "stderr_truncated": stderr_trunc,
    }


def _register_tools(server: FastMCP, skill_dir: Path) -> None:
    @server.tool()
    async def run_jquants(
        args: list[str],
        cwd: str | None = None,
        timeout_sec: int = _DEFAULT_TIMEOUT,
    ) -> dict[str, Any]:
        """Run the local `jquants` CLI with the given argument list.

        BEFORE your first call: load `get_skill_guide()` — it documents command
        syntax rules, plan gates, schema lookup, and common pitfalls. For
        category-specific docs (eq/mkt/deriv/fins/idx/bulk/plans) use
        `list_skill_references()` + `read_skill_reference(name)`.

        Args:
            args: Arguments passed to jquants, split into a list. Do NOT include
                the leading "jquants". Global options (--output/--save/-f) must
                come BEFORE the subcommand (clap requirement).
                Example: ["--output", "csv", "--save", "out.csv",
                          "eq", "daily", "--code", "86970", "--from", "2026-01-01"]
            cwd: Working directory for the process. Relative `--save` paths
                resolve here. Defaults to the server's current directory.
            timeout_sec: Hard timeout in seconds (default 180).

        Returns:
            Dict with command, cwd, exit_code, stdout, stderr, and truncation
            flags. stdout/stderr are each capped at ~1 MiB; use --save for full data.
        """
        return await _run(args=args, cwd=cwd, timeout_sec=timeout_sec)

    @server.tool()
    async def jquants_schema(endpoint: str | None = None) -> dict[str, Any]:
        """Return the jquants API schema as JSON. Requires no authentication.

        Args:
            endpoint: Optional endpoint key in `category.command` form
                (e.g. "eq.daily", "fins.summary", "mkt.short-ratio").
                Omit to list every endpoint with field counts.

        Use this BEFORE building a `-f` field list — field names are PascalCase
        and must match exactly (the CLI rejects unknown names).
        """
        args = ["--output", "json", "schema"]
        if endpoint:
            args.append(endpoint)
        return await _run(args=args, cwd=None, timeout_sec=30)

    @server.tool()
    async def jquants_version() -> dict[str, Any]:
        """Return the installed `jquants` CLI version string. Useful for sanity checks."""
        return await _run(args=["--version"], cwd=None, timeout_sec=10)

    @server.tool()
    def get_skill_guide() -> str:
        """Return the main SKILL.md — command syntax rules, plan gates, schema
        workflow, rate limits, data freshness, common pitfalls.

        Call this FIRST before building any jquants command. The skill is
        sourced from the local CLI (`jquants skills add`) so it always
        matches the installed CLI version.

        For category-specific deep-dives (eq/mkt/deriv/fins/idx/bulk flags,
        plan availability, update schedule), use `list_skill_references()`
        and `read_skill_reference(name)`.
        """
        return (skill_dir / "SKILL.md").read_text(encoding="utf-8")

    @server.tool()
    def list_skill_references() -> list[str]:
        """List names of available reference files under the skill tree.

        Each file drills into one subcommand category or cross-cutting topic.
        Pass any returned name to `read_skill_reference()`.
        """
        refs = skill_dir / "references"
        if not refs.exists():
            return []
        return sorted(p.name for p in refs.glob("*.md"))

    @server.tool()
    def read_skill_reference(name: str) -> str:
        """Read one reference file from the skill tree.

        Args:
            name: Reference file name returned by `list_skill_references()`
                (e.g. "commands-eq.md", "plans.md", "data-update-schedule.md").

        Returns:
            The full markdown content of the file.
        """
        target = skill_dir / "references" / name
        if not target.is_file():
            available = sorted(
                p.name for p in (skill_dir / "references").glob("*.md")
            ) if (skill_dir / "references").exists() else []
            raise ValueError(
                f"Reference {name!r} not found. Available: {available}"
            )
        return target.read_text(encoding="utf-8")


mcp = _build_server()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
