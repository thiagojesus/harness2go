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
