# harness2go

A swiss-army knife for migrating coding-agent harness sessions — and their
MCP server / subagent config — between [OpenCode](https://opencode.ai) and
Claude Code.

```bash
./harness2go.py opencode2claude <list|convert|import-global> ...
./harness2go.py claude2opencode <list|convert|import-global> ...
```

`harness2go.py` is a thin dispatcher over two standalone scripts —
`opencode2claude.py` and `claude2opencode.py` — each of which also works on
its own if you only ever migrate one direction. Both share a small
`harness_common.py` module (JSONC parsing, frontmatter parsing, secret
masking, interactive prompts) so behavior stays identical on both sides.

## opencode2claude.py — OpenCode → Claude Code

Converts an OpenCode session into a Claude Code session transcript
(`.jsonl`) that Claude Code can list and `--resume`.

OpenCode's real conversation data lives in a SQLite DB at
`~/.local/share/opencode/opencode.db` (the `session`, `message`, and `part`
tables) — not in the small per-session status stub files some OpenCode
wrappers leave lying around. This script reads directly from that DB
(read-only — it never writes to opencode.db).

```bash
# List every OpenCode session available to convert
./opencode2claude.py list

# Convert one session (writes into ~/.claude/projects/<slug>/<new-uuid>.jsonl)
./opencode2claude.py convert ses_XXXXXXXXXXXX

# Convert everything
./opencode2claude.py convert --all

# Preview without writing anything
./opencode2claude.py convert ses_XXXXXXXXXXXX --dry-run
```

After converting, resume the session from the project directory it belongs to:

```bash
cd /path/to/project && claude --resume <printed-uuid>
```

After converting a single session (interactively, not `--all`), a wizard
offers to also import:

- **MCP servers** configured for that OpenCode project (global +
  project-level `opencode.json(c)`) via the real `claude mcp add` CLI, at
  `local`/`project`/`user` scope, one server at a time.
- **Agents** referenced in the session (assistant turns + `task` tool
  `subagent_type` calls) as Claude Code subagent stubs
  (`.claude/agents/*.md`, `local` or `global` scope). These are honest
  placeholders — OpenCode's real system-prompt text for built-in/plugin
  agents (Sisyphus, Oracle, Librarian, ...) is compiled into a third-party
  plugin bundle and isn't recoverable from the session, so the stub only
  carries what's genuinely observable (name, usage count, task
  descriptions, best-effort model from `oh-my-openagent.json`) and says so.

Control it with `convert ... --wizard {auto,always,never}` (default `auto`:
runs only for a single, non-dry-run conversion in an interactive terminal).

```bash
./opencode2claude.py import-global
```

Same MCP/agent import wizard, but independent of any session or
`opencode.db`: MCP servers come from OpenCode's **global** config only
(`~/.config/opencode/opencode.jsonc`); agents come from
`~/.config/opencode/agent/*.md` — real files the user actually authored,
copied **verbatim** — plus any additional agent names known only via
`oh-my-openagent.json`'s model-routing config (stub fidelity; a real file
always takes precedence over a stub for the same agent name).

## claude2opencode.py — Claude Code → OpenCode

The mirror image. Converts a Claude Code session (`.jsonl` transcript) into
a **real OpenCode session** — inserted into `opencode.db` — and imports
Claude Code's MCP servers / subagent definitions into OpenCode's own
config.

```bash
# List every Claude Code session available to convert
./claude2opencode.py list

# Convert one session (by UUID, searched across ~/.claude/projects/*,
# or pass a direct .jsonl path)
./claude2opencode.py convert <session-uuid>

# Preview without writing anything
./claude2opencode.py convert <session-uuid> --dry-run
```

**This one writes to a live application database**, which is a materially
riskier operation than writing a plain session file — so `convert` always
takes a timestamped backup of `opencode.db` (`opencode.db.bak-<epoch>`)
before inserting anything, and does the whole insert in a single
transaction.

Like the other direction, converting a single session offers a wizard to
import the MCP servers configured for that Claude Code project (Claude's
local + project scope, from `~/.claude.json` and `.mcp.json`) and any
project-level Claude agents (`.claude/agents/*.md`) into OpenCode's
project-level config. `import-global` does the session-independent
version: Claude Code's **user**-scope MCP servers (`~/.claude.json` top-level
`mcpServers`) and **global** agents (`~/.claude/agents/*.md`, copied
verbatim — these are real files the user wrote, so unlike the
Claude-Code-agent-stub direction there's no lesser-fidelity fallback needed
here).

Writing into OpenCode's `opencode.jsonc` also takes a backup first, and
warns if the existing file has `//`/`/* */` comments — comments aren't
preserved across a rewrite (no dependency-free JSONC writer exists to keep
them), so the backup is the safety net.

### Design notes

- Claude Code's transcript is per-content-block JSONL lines chained by
  `parentUuid`; OpenCode is structured SQLite rows. `claude2opencode`
  regroups consecutive assistant lines sharing the same `message.id` back
  into one OpenCode `message` (one step, bounded by synthesized
  `step-start`/`step-finish` parts), and reunites each `tool_use` block with
  its later `tool_result` line into a single OpenCode `tool` part —
  the exact inverse of how `opencode2claude` split things apart.
- `thinking` blocks become OpenCode `reasoning` parts (the cryptographic
  signature is dropped — it isn't meaningful outside Anthropic's API and
  OpenCode has no equivalent concept).
- Tool name mapping is the literal inverse of `opencode2claude`'s
  (`Bash`→`bash`, `Read`→`read`, `Edit`→`edit`, `Task`→`task`,
  `AskUserQuestion`→`question`, `TodoWrite`→`todowrite`,
  `TaskOutput`→`background_output`); anything else passes through
  lowercased by name.
- The session row reuses OpenCode's existing `project` row (OpenCode
  appears to key everything off a single project per install rather than
  one per directory — a fresh `project` row is only created if none
  exists at all).

## Requirements

Python 3 standard library only (`sqlite3`, `argparse`, no pip installs).
Optionally uses `git`/`claude --version`/`opencode --version` if present on
`PATH`, all with safe fallbacks.
