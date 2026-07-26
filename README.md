# harness2go

Convert coding-agent harness sessions so you can pick up your work in a different tool.

## opencode2claude.py

Converts an [OpenCode](https://opencode.ai) session into a Claude Code session
transcript (`.jsonl`) that Claude Code can list and `--resume`.

OpenCode's real conversation data lives in a SQLite DB at
`~/.local/share/opencode/opencode.db` (the `session`, `message`, and `part`
tables) — not in the small per-session status stub files some OpenCode
wrappers leave lying around. This script reads directly from that DB.

### Usage

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

### import-global — no session needed

```bash
./opencode2claude.py import-global
```

Same MCP/agent import wizard, but independent of any session or
`opencode.db`:

- MCP servers come from OpenCode's **global** config only
  (`~/.config/opencode/opencode.jsonc`).
- Agents come from `~/.config/opencode/agent/*.md` — real files the user
  actually authored, copied **verbatim** (frontmatter translated, body
  untouched) — plus any additional agent names known only via
  `oh-my-openagent.json`'s model-routing config (stub fidelity, same
  caveat as above; a real file always takes precedence over a stub for the
  same agent name).

### Design notes

- One OpenCode assistant `message` row maps to one Claude Code assistant API
  turn (`message.id`/`requestId`). Its `part` rows (reasoning/text/tool,
  bounded by `step-start`/`step-finish`) become one Claude Code JSONL line
  per content block, chained by `parentUuid` — the same shape a real Claude
  Code transcript has.
- OpenCode `reasoning` parts become plain assistant `text` blocks, not Claude
  `thinking` blocks. A real Anthropic `thinking` block carries a cryptographic
  signature tied to the exact API response that produced it; fabricating one
  would make the resumed session fail (or get silently stripped) the moment
  Claude Code sends it back to the API. Text keeps the information as context
  without that risk.
- Tool calls/results are paired: each OpenCode `tool` part becomes a
  `tool_use` block immediately followed by a `user`-role `tool_result` line.
  Known tools are mapped to their Claude Code equivalents (`bash`→`Bash`,
  `read`→`Read`, `edit`→`Edit`, `task`→`Task`, `question`→`AskUserQuestion`,
  `todowrite`→`TodoWrite`, `background_output`→`TaskOutput`); anything
  unrecognized passes through by name instead of being dropped.

### Requirements

Python 3 standard library only (`sqlite3`, `argparse`, no pip installs).
Optionally uses `git` (branch detection) and `claude --version` if present
on `PATH`, both with safe fallbacks.
