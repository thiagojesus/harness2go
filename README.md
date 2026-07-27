# harness2go

A swiss-army knife for migrating coding-agent harness sessions — and their
MCP server / subagent config — between [OpenCode](https://opencode.ai),
Claude Code, and VS Code (GitHub Copilot Chat).

```bash
./harness2go.py opencode2claude <list|convert|import-global> ...
./harness2go.py claude2opencode <list|convert|import-global> ...
./harness2go.py vscode2opencode <list|convert> ...
./harness2go.py vscode2claude   <list|convert> ...
```

`harness2go.py` is a thin dispatcher over standalone scripts —
`opencode2claude.py`, `claude2opencode.py`, `vscode2opencode.py`,
`vscode2claude.py` — each of which also works on its own if you only ever
migrate one direction. They share small `harness_common.py` /
`vscode_common.py` modules (JSONC parsing, frontmatter parsing, secret
masking, interactive prompts, VS Code session decoding) so behavior stays
consistent across all of them.

VS Code support is currently **one-way** (reading Copilot Chat sessions
out into OpenCode/Claude Code); writing new VS Code sessions is a possible
follow-up.

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

## vscode2opencode.py / vscode2claude.py — VS Code (Copilot Chat) → OpenCode/Claude Code

VS Code (via the GitHub Copilot Chat extension) stores chat sessions as an
append-only "operation log" — not a simple flat file. The first line is a
full JSON snapshot (`kind: 0`); later lines are `Set`/`Push`/`Delete`
patches against explicit object paths (verified against VS Code's own
source, `chatSessionStore.ts`/`objectMutationLog.ts` in
`microsoft/vscode`). Sessions live in two places:

- No-folder ("empty window") chats: `~/Library/Application Support/Code/User/globalStorage/emptyWindowChatSessions/<uuid>.json[l]`
- Per-project chats: `~/Library/Application Support/Code/User/workspaceStorage/<hash>/chatSessions/<uuid>.json[l]` (the hash's `workspace.json` maps it to a folder)

`vscode_common.py` replays that log and normalizes it into the same
canonical "turns" shape `claude2opencode.py`'s transcript parser produces,
so both converters build on one shared reader.

```bash
./vscode2opencode.py list
./vscode2opencode.py convert <session-uuid> [--directory DIR] [--dry-run]

./vscode2claude.py list
./vscode2claude.py convert <session-uuid> [--directory DIR] [--dry-run]
```

`--directory` is required (or falls back to the current working directory
with a warning) for no-folder sessions, which have no natural project
association.

**Scope — core content only.** A single VS Code response can contain any
of ~35 distinct content-block kinds (progress messages,
`mcpServersStarting` notices, confirmations, todoList widgets, terminal/
notebook/file-edit blocks, pull requests, ...). Only the substantive ones
are mapped — text/markdown, `thinking`, and `toolInvocationSerialized` — the
same content OpenCode/Claude Code already model; everything else is UI
chrome, dropped the same way `opencode2claude.py` drops OpenCode's
`step-start`/`step-finish` structural markers.

Two things worth knowing:
- VS Code's own tool names (`run_in_terminal`, `copilot_readFile`, ...)
  don't match either target's tool vocabulary, so they pass through
  verbatim (lowercased on the OpenCode side, unchanged on the Claude Code
  side) rather than being misleadingly relabeled — an accepted limitation
  of "core content only," not a bug. Likewise, a tool's persisted
  "input" is often just VS Code's human-readable invocation message
  (e.g. "Reading file.py"), not the tool's real structured arguments —
  that's what's actually available in the serialized log.
- `thinking` blocks become plain `text` (Claude Code target) or
  `reasoning` (OpenCode target) — never a real Anthropic `thinking` block —
  since these turns frequently aren't even from an Anthropic model (VS
  Code routes through GPT/Gemini/etc. too), so a fabricated signature would
  be actively wrong, not just unnecessary.
- On the Claude Code side specifically: message ids are always freshly
  synthesized as `msg_<hex>`, never VS Code's own `request_<uuid>` — Claude
  Code sends the last assistant message's `id` back to the API as a
  `previous_message_id` diagnostic on resume, and the API rejects anything
  not shaped like a real response id. (Discovered by testing an actual
  resume against a converted session — the API returned a 400 until this
  was fixed.)

## Requirements

Python 3 standard library only (`sqlite3`, `argparse`, no pip installs).
Optionally uses `git`/`claude --version`/`opencode --version` if present on
`PATH`, all with safe fallbacks.
