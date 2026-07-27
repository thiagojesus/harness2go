# harness2go

[![Tests](https://github.com/thiagojesus/harness2go/actions/workflows/tests.yml/badge.svg)](https://github.com/thiagojesus/harness2go/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/harness2go.svg)](https://pypi.org/project/harness2go/)

A swiss-army knife for migrating coding-agent harness sessions — and their
MCP server / subagent config — between [OpenCode](https://opencode.ai),
Claude Code, and VS Code (GitHub Copilot Chat).

```bash
h2go opencode2claude <list|convert|import-global> ...
h2go claude2opencode <list|convert|import-global> ...
h2go vscode2opencode <list|convert> ...
h2go vscode2claude   <list|convert> ...
h2go opencode2vscode <list|convert> ...
h2go claude2vscode   <list|convert> ...
```

`h2go` is a thin dispatcher over the six directions, each of which is also
installed as its own standalone command (`opencode2claude`,
`claude2opencode`, `vscode2opencode`, `vscode2claude`, `opencode2vscode`,
`claude2vscode`) for anyone who only ever migrates one way. They share
small `harness_common.py` / `vscode_common.py` modules (JSONC parsing,
frontmatter parsing, secret masking, interactive prompts, VS Code session
decoding/encoding) so behavior stays consistent across all of them.

## Installation

Zero runtime dependencies (plain standard library) — a regular Python
package, installable with either `pip` or `uv`.

**From PyPI** (once published — see [Publishing](#publishing) below):

```bash
uv tool install harness2go
# or
pip install harness2go
# or, without installing anything persistently:
uvx --from harness2go h2go opencode2claude list
```

**From source, without waiting on a release** — as a global CLI tool
(puts `h2go` and the six standalone commands on your `PATH`):

```bash
uv tool install .          # from a clone of this repo
# or, without cloning:
uv tool install git+https://github.com/thiagojesus/harness2go
```

`pip`-only equivalent (installs into whichever environment is currently
active — a fresh `pipx install .` also works if you have `pipx`):

```bash
pip install .
```

**For development** (editable install, so local edits take effect
immediately without reinstalling):

```bash
uv venv && source .venv/bin/activate && uv pip install -e .
# or: python3 -m venv .venv && source .venv/bin/activate && pip install -e .

python -m unittest discover -s tests -p "test_*.py"
```

Then from anywhere:

```bash
h2go opencode2claude list
h2go claude2vscode convert <session-uuid>
```

## opencode2claude — OpenCode → Claude Code

Converts an OpenCode session into a Claude Code session transcript
(`.jsonl`) that Claude Code can list and `--resume`.

OpenCode's real conversation data lives in a SQLite DB at
`~/.local/share/opencode/opencode.db` (the `session`, `message`, and `part`
tables) — not in the small per-session status stub files some OpenCode
wrappers leave lying around. This script reads directly from that DB
(read-only — it never writes to opencode.db).

```bash
# List every OpenCode session available to convert
opencode2claude list

# Convert one session (writes into ~/.claude/projects/<slug>/<new-uuid>.jsonl)
opencode2claude convert ses_XXXXXXXXXXXX

# Convert everything
opencode2claude convert --all

# Preview without writing anything
opencode2claude convert ses_XXXXXXXXXXXX --dry-run
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
opencode2claude import-global
```

Same MCP/agent import wizard, but independent of any session or
`opencode.db`: MCP servers come from OpenCode's **global** config only
(`~/.config/opencode/opencode.jsonc`); agents come from
`~/.config/opencode/agent/*.md` — real files the user actually authored,
copied **verbatim** — plus any additional agent names known only via
`oh-my-openagent.json`'s model-routing config (stub fidelity; a real file
always takes precedence over a stub for the same agent name).

## claude2opencode — Claude Code → OpenCode

The mirror image. Converts a Claude Code session (`.jsonl` transcript) into
a **real OpenCode session** — inserted into `opencode.db` — and imports
Claude Code's MCP servers / subagent definitions into OpenCode's own
config.

```bash
# List every Claude Code session available to convert
claude2opencode list

# Convert one session (by UUID, searched across ~/.claude/projects/*,
# or pass a direct .jsonl path)
claude2opencode convert <session-uuid>

# Preview without writing anything
claude2opencode convert <session-uuid> --dry-run
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

## vscode2opencode / vscode2claude — VS Code (Copilot Chat) → OpenCode/Claude Code

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
vscode2opencode list
vscode2opencode convert <session-uuid> [--directory DIR] [--dry-run]

vscode2claude list
vscode2claude convert <session-uuid> [--directory DIR] [--dry-run]
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

## opencode2vscode / claude2vscode — OpenCode/Claude Code → VS Code (Copilot Chat)

The reverse of `vscode2opencode.py`/`vscode2claude.py`: writes a **real,
openable VS Code chat session**.

```bash
opencode2vscode convert ses_XXXXXXXXXXXX [--dry-run]
claude2vscode convert <session-uuid> [--dry-run]
```

**This one needs more than a session file.** VS Code's Chat view discovers
sessions purely through an index kept in `state.vscdb` (its
general-purpose settings store) — confirmed by reading
`chatSessionStore.ts`'s actual load path, which has no fallback that scans
the session folder. A session file dropped in without a matching index
entry is invisible to the UI. So `convert`:

- Writes the session file (`.json`) in the right location.
- Merges a matching entry into `state.vscdb`'s `chat.ChatSessionStore.index`
  key (an `ItemTable` row, standard VS Code storage-service SQLite), always
  taking a timestamped backup of `state.vscdb` first.
- **Refuses to run if VS Code is currently open.** That index is cached in
  memory by a running window and only read fresh on cold start — writing it
  underneath a live VS Code process risks the change being silently
  discarded the next time that window saves any chat state at all. Close
  VS Code first.
- Only targets an **existing** VS Code workspace, matched by directory (the
  same `workspace.json` lookup `vscode2opencode.py`/`vscode2claude.py` use
  for reading). If the target project has never been opened in VS Code, the
  session is written as a no-folder ("empty window") chat instead of
  inventing a new workspace — the same "never fabricate what isn't already
  there" discipline as the MCP/agent import directions.

Content mapping mirrors the read direction in reverse: text/reasoning
(OpenCode) or text/thinking (Claude Code) become VS Code `markdownContent`/
`thinking` blocks, and tool calls become `toolInvocationSerialized` blocks
— with the tool's original input rendered as a human-readable summary
(VS Code's serialized format has no slot for raw structured arguments) and
its output carried in `resultDetails` when there is one.

Validated with a real three-way round trip: a real VS Code session was
converted to Claude Code (previous PR), then converted back to VS Code
with this code (against a throwaway copy of `state.vscdb`, never the real
one) — replaying the written file reproduced the original conversation
content, and the index entry showed up correctly alongside the
pre-existing real entries, untouched.

## Requirements

Python 3.9+, standard library only at runtime (`sqlite3`, `argparse`) —
`pyproject.toml` declares zero dependencies. Optionally uses
`git`/`claude --version`/`opencode --version` if present on `PATH`, all
with safe fallbacks.

## Project layout

```
harness2go/
├── pyproject.toml          # build config + console_scripts entry points
├── src/harness2go/         # the actual package
│   ├── cli.py              # h2go dispatcher
│   ├── harness_common.py   # shared JSONC/frontmatter/prompt helpers
│   ├── vscode_common.py    # shared VS Code session decode/encode
│   ├── opencode2claude.py
│   ├── claude2opencode.py
│   ├── vscode2opencode.py
│   ├── vscode2claude.py
│   ├── opencode2vscode.py
│   └── claude2vscode.py
└── tests/                  # unittest suite, run against the installed package
```

## Publishing

Releases publish to PyPI automatically via
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/) — no API
token stored anywhere; GitHub's OIDC identity authenticates the publish
(`.github/workflows/publish.yml`). One-time setup (do this once, on PyPI
and GitHub — not something a CI job can do for you):

1. Create a PyPI account at [pypi.org](https://pypi.org) if you don't have
   one (2FA is required).
2. On PyPI: **Your projects → Publishing → Add a new pending publisher**,
   and fill in:
   - PyPI project name: `harness2go`
   - Owner: `thiagojesus`
   - Repository name: `harness2go`
   - Workflow filename: `publish.yml`
   - Environment name: `pypi`
3. On GitHub: **Settings → Environments → New environment**, name it
   `pypi` (matching step 2; optionally add protection rules like required
   reviewers).
4. Cut a [GitHub Release](https://github.com/thiagojesus/harness2go/releases/new)
   (tag it e.g. `v0.1.0`, matching the version in `pyproject.toml`) — the
   `publish.yml` workflow triggers on release publish, builds the
   sdist/wheel, and uploads them to PyPI automatically.

For each subsequent release: bump `version` in `pyproject.toml`, commit,
tag, and cut a new GitHub Release — no PyPI-side setup needed again after
the first time.
