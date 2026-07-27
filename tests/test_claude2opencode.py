#!/usr/bin/env python3
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from harness2go import claude2opencode as m


def write_jsonl(path, lines):
    with open(path, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


class PeekAndListSessions(unittest.TestCase):
    def test_peek_extracts_cwd_title_timestamp(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.jsonl")
            write_jsonl(path, [
                {"type": "user", "cwd": "/proj", "timestamp": "2026-01-01T00:00:00.000Z",
                 "message": {"role": "user", "content": "<local-command-caveat>meta</local-command-caveat>"},
                 "isMeta": True},
                {"type": "user", "cwd": "/proj", "timestamp": "2026-01-01T00:00:01.000Z",
                 "message": {"role": "user", "content": "please help with X"}},
                {"type": "assistant", "cwd": "/proj", "timestamp": "2026-01-01T00:00:02.000Z",
                 "message": {"id": "m1", "model": "claude-opus-4-8", "content": [{"type": "text", "text": "ok"}],
                             "stop_reason": "end_turn", "usage": {}}},
            ])
            info = m.peek_claude_session(path)
            self.assertEqual(info["cwd"], "/proj")
            self.assertEqual(info["title"], "please help with X")
            self.assertEqual(info["last_timestamp"], "2026-01-01T00:00:02.000Z")

    def test_list_finds_sessions_across_project_dirs(self):
        with tempfile.TemporaryDirectory() as projects_dir:
            proj_dir = os.path.join(projects_dir, "-some-project")
            os.makedirs(proj_dir)
            path = os.path.join(proj_dir, "abc.jsonl")
            write_jsonl(path, [{"type": "user", "cwd": "/x", "timestamp": "t",
                                "message": {"role": "user", "content": "hi"}}])
            sessions = m.list_claude_sessions(projects_dir)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["session_id"], "abc")

    def test_find_session_path_by_uuid_and_direct_path(self):
        with tempfile.TemporaryDirectory() as projects_dir:
            proj_dir = os.path.join(projects_dir, "-some-project")
            os.makedirs(proj_dir)
            path = os.path.join(proj_dir, "abc.jsonl")
            open(path, "w").close()
            self.assertEqual(m.find_claude_session_path("abc", projects_dir), path)
            self.assertEqual(m.find_claude_session_path(path, projects_dir), path)
            with self.assertRaises(SystemExit):
                m.find_claude_session_path("nope", projects_dir)


class ParseClaudeTranscript(unittest.TestCase):
    def test_full_turn_reconstruction_with_tool_pairing(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.jsonl")
            write_jsonl(path, [
                {"type": "user", "cwd": "/proj", "timestamp": "2026-01-01T00:00:00.000Z",
                 "message": {"role": "user", "content": "please list files"}},
                {"type": "assistant", "cwd": "/proj", "timestamp": "2026-01-01T00:00:01.000Z",
                 "message": {"id": "m1", "model": "claude-opus-4-8", "stop_reason": "tool_use",
                             "usage": {"input_tokens": 5, "output_tokens": 10,
                                       "cache_creation_input_tokens": 1, "cache_read_input_tokens": 2},
                             "content": [{"type": "thinking", "thinking": "let me think"}]}},
                {"type": "assistant", "cwd": "/proj", "timestamp": "2026-01-01T00:00:02.000Z",
                 "message": {"id": "m1", "model": "claude-opus-4-8", "stop_reason": "tool_use", "usage": {},
                             "content": [{"type": "tool_use", "id": "call_1", "name": "Bash",
                                          "input": {"command": "ls"}}]}},
                {"type": "user", "cwd": "/proj", "timestamp": "2026-01-01T00:00:03.000Z",
                 "message": {"role": "user", "content": [
                     {"type": "tool_result", "tool_use_id": "call_1", "content": "file.txt"}]}},
                {"type": "assistant", "cwd": "/proj", "timestamp": "2026-01-01T00:00:04.000Z",
                 "message": {"id": "m2", "model": "claude-opus-4-8", "stop_reason": "end_turn", "usage": {},
                             "content": [{"type": "text", "text": "Here's the file."}]}},
            ])
            directory, turns = m.parse_claude_transcript(path)
            self.assertEqual(directory, "/proj")
            self.assertEqual([t["role"] for t in turns], ["user", "assistant", "assistant"])
            self.assertEqual(turns[0]["text"], "please list files")

            first_assistant = turns[1]
            self.assertEqual(first_assistant["message_id"], "m1")
            self.assertEqual(len(first_assistant["blocks"]), 2)
            tool_block = [b for b in first_assistant["blocks"] if b["type"] == "tool_use"][0]
            self.assertEqual(tool_block["_result_text"], "file.txt")
            self.assertFalse(tool_block["_is_error"])

            self.assertEqual(turns[2]["message_id"], "m2")

    def test_tool_result_content_as_block_list(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.jsonl")
            write_jsonl(path, [
                {"type": "assistant", "cwd": "/proj", "timestamp": "t1",
                 "message": {"id": "m1", "model": "x", "stop_reason": "tool_use", "usage": {},
                             "content": [{"type": "tool_use", "id": "c1", "name": "Read",
                                          "input": {"file_path": "/f"}}]}},
                {"type": "user", "cwd": "/proj", "timestamp": "t2",
                 "message": {"role": "user", "content": [
                     {"type": "tool_result", "tool_use_id": "c1", "is_error": True,
                      "content": [{"type": "text", "text": "not found"}]}]}},
            ])
            _, turns = m.parse_claude_transcript(path)
            block = turns[0]["blocks"][0]
            self.assertEqual(block["_result_text"], "not found")
            self.assertTrue(block["_is_error"])


class ReverseMapTool(unittest.TestCase):
    def test_bash_read_edit_task(self):
        self.assertEqual(m.reverse_map_tool("Bash", {"command": "ls"}), ("bash", {"command": "ls"}))
        self.assertEqual(m.reverse_map_tool("Read", {"file_path": "/f"}), ("read", {"filePath": "/f"}))
        name, inp = m.reverse_map_tool("Edit", {"file_path": "/f", "old_string": "a", "new_string": "b",
                                                 "replace_all": True})
        self.assertEqual(name, "edit")
        self.assertEqual(inp, {"filePath": "/f", "oldString": "a", "newString": "b", "replaceAll": True})

    def test_todowrite_synthesizes_priority(self):
        name, inp = m.reverse_map_tool("TodoWrite", {"todos": [
            {"content": "do x", "status": "pending", "activeForm": "Doing x"}]})
        self.assertEqual(name, "todowrite")
        self.assertEqual(inp["todos"][0]["priority"], "medium")

    def test_unknown_tool_passes_through_lowercased(self):
        name, inp = m.reverse_map_tool("WebFetch", {"url": "https://x"})
        self.assertEqual(name, "webfetch")
        self.assertEqual(inp, {"url": "https://x"})


class BuildOpencodeSession(unittest.TestCase):
    def test_message_chaining_and_step_markers(self):
        turns = [
            {"role": "user", "text": "hello", "created": "2026-01-01T00:00:00.000Z"},
            {"role": "assistant", "message_id": "m1", "model": "claude-opus-4-8",
             "usage": {"input_tokens": 1, "output_tokens": 2}, "stop_reason": "end_turn",
             "created": "2026-01-01T00:00:01.000Z",
             "blocks": [{"type": "text", "text": "hi there"}]},
        ]
        session_row, messages, parts = m.build_opencode_session("/proj", turns, "1.0.0")
        self.assertEqual(session_row["directory"], "/proj")
        self.assertEqual(session_row["title"], "hello")
        self.assertEqual(len(messages), 2)

        user_id, user_data = messages[0]
        assistant_id, assistant_data = messages[1]
        self.assertEqual(assistant_data["parentID"], user_id)
        self.assertEqual(assistant_data["modelID"], "claude-opus-4-8")
        self.assertEqual(assistant_data["finish"], "stop")

        assistant_parts = [p for p in parts if p[1] == assistant_id]
        types = [json.loads(json.dumps(p[2]))["type"] for p in assistant_parts]
        self.assertEqual(types[0], "step-start")
        self.assertEqual(types[-1], "step-finish")
        self.assertIn("text", types)

    def test_tool_use_becomes_single_tool_part_with_output(self):
        turns = [
            {"role": "assistant", "message_id": "m1", "model": "claude-opus-4-8",
             "usage": {}, "stop_reason": "tool_use", "created": "2026-01-01T00:00:00.000Z",
             "blocks": [{"type": "tool_use", "id": "call_1", "name": "Bash", "input": {"command": "ls"},
                         "_result_text": "file.txt", "_is_error": False}]},
        ]
        _, messages, parts = m.build_opencode_session("/proj", turns, "1.0.0")
        tool_parts = [p[2] for p in parts if p[2].get("type") == "tool"]
        self.assertEqual(len(tool_parts), 1)
        tool = tool_parts[0]
        self.assertEqual(tool["tool"], "bash")
        self.assertEqual(tool["callID"], "call_1")
        self.assertEqual(tool["state"]["output"], "file.txt")
        self.assertEqual(tool["state"]["status"], "completed")


class WriteOpencodeSession(unittest.TestCase):
    SCHEMA = """
        CREATE TABLE project (
            id text PRIMARY KEY, worktree text, vcs text, name text, icon_url text,
            icon_url_override text, icon_color text, time_created integer, time_updated integer,
            time_initialized integer, sandboxes text, commands text);
        CREATE TABLE session (
            id text PRIMARY KEY, project_id text, workspace_id text, parent_id text, slug text,
            directory text, path text, title text, version text, share_url text,
            summary_additions integer, summary_deletions integer, summary_files integer,
            summary_diffs text, metadata text, cost real, tokens_input integer, tokens_output integer,
            tokens_reasoning integer, tokens_cache_read integer, tokens_cache_write integer,
            revert text, permission text, agent text, model text,
            time_created integer, time_updated integer, time_compacting integer, time_archived integer);
        CREATE TABLE message (
            id text PRIMARY KEY, session_id text, time_created integer, time_updated integer, data text);
        CREATE TABLE part (
            id text PRIMARY KEY, message_id text, session_id text, time_created integer,
            time_updated integer, data text);
    """

    def _make_db(self, path):
        conn = sqlite3.connect(path)
        conn.executescript(self.SCHEMA)
        conn.commit()
        conn.close()

    def test_writes_rows_and_backs_up_existing_db(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, "opencode.db")
            self._make_db(db_path)

            session_row = {
                "id": "ses_test", "project_id": None, "workspace_id": None, "parent_id": None,
                "slug": "abc", "directory": "/proj", "path": "proj", "title": "hi", "version": "1.0.0",
                "share_url": None, "summary_additions": 0, "summary_deletions": 0, "summary_files": 0,
                "summary_diffs": None, "metadata": None, "cost": 0, "tokens_input": 1, "tokens_output": 2,
                "tokens_reasoning": 0, "tokens_cache_read": 0, "tokens_cache_write": 0,
                "revert": None, "permission": None, "agent": "build", "model": None,
                "time_created": 1000, "time_updated": 2000, "time_compacting": None, "time_archived": None,
            }
            messages = [("msg_1", {"role": "user", "time": {"created": 1000}})]
            parts = [("prt_1", "msg_1", {"type": "text", "text": "hi"})]

            backup_calls = []
            session_id = m.write_opencode_session(
                db_path, session_row, messages, parts,
                backup=lambda src, dst: backup_calls.append((src, dst)),
            )
            self.assertEqual(session_id, "ses_test")
            self.assertEqual(len(backup_calls), 1)

            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT project_id, title FROM session WHERE id='ses_test'").fetchone()
            self.assertEqual(row[1], "hi")
            self.assertEqual(row[0], "global")  # auto-created project row
            self.assertEqual(conn.execute("SELECT count(*) FROM message").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM part").fetchone()[0], 1)
            conn.close()

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, "opencode.db")
            self._make_db(db_path)
            session_row = {"id": "ses_x", "directory": "/proj", "title": "t"}
            backup_calls = []
            m.write_opencode_session(db_path, session_row, [], [], dry_run=True,
                                      backup=lambda s, d_: backup_calls.append(1))
            self.assertEqual(backup_calls, [])
            conn = sqlite3.connect(db_path)
            self.assertEqual(conn.execute("SELECT count(*) FROM session").fetchone()[0], 0)
            conn.close()


class ReverseMcpImport(unittest.TestCase):
    def test_build_opencode_mcp_entry_remote_and_local(self):
        remote = m.build_opencode_mcp_entry({"type": "http", "url": "https://x", "headers": {"Authorization": "Bearer secret123456"}})
        self.assertEqual(remote["type"], "remote")
        self.assertEqual(remote["headers"]["Authorization"], "Bearer secret123456")

        masked = m.build_opencode_mcp_entry({"type": "http", "url": "https://x", "headers": {"Authorization": "Bearer secret123456"}}, mask=True)
        self.assertNotIn("secret123456", masked["headers"]["Authorization"])

        local = m.build_opencode_mcp_entry({"type": "stdio", "command": "npx", "args": ["-y", "pkg"], "env": {"K": "v"}})
        self.assertEqual(local["type"], "local")
        self.assertEqual(local["command"], ["npx", "-y", "pkg"])
        self.assertEqual(local["environment"], {"K": "v"})

    def test_write_opencode_mcp_config_merges_and_backs_up(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "opencode.jsonc")
            with open(path, "w") as f:
                json.dump({"mcp": {"existing": {"type": "local", "command": ["x"]}}}, f)

            m.write_opencode_mcp_config(path, "newone", {"type": "remote", "url": "https://y"})

            with open(path) as f:
                cfg = json.load(f)
            self.assertIn("existing", cfg["mcp"])
            self.assertIn("newone", cfg["mcp"])
            backups = [f for f in os.listdir(d) if ".bak-" in f]
            self.assertEqual(len(backups), 1)

    def test_wizard_never_prints_secret_but_writes_it(self):
        servers = {"gh": {"type": "http", "url": "https://x", "headers": {"Authorization": "Bearer secrettoken99"}}}
        with tempfile.TemporaryDirectory() as d:
            target_path = os.path.join(d, "opencode.jsonc")
            buf = io.StringIO()
            with mock.patch("builtins.input", side_effect=["y", "y", "global"]), redirect_stdout(buf):
                m.import_mcp_wizard_reverse(servers, lambda scope: target_path)
            with open(target_path) as f:
                content = f.read()
            self.assertIn("secrettoken99", content)
            self.assertNotIn("secrettoken99", buf.getvalue())


class ReverseAgentImport(unittest.TestCase):
    def test_find_and_write_roundtrip(self):
        with tempfile.TemporaryDirectory() as agent_dir, tempfile.TemporaryDirectory() as out_dir:
            with open(os.path.join(agent_dir, "reviewer.md"), "w") as f:
                f.write("---\nname: reviewer\ndescription: reviews code\nmodel: claude-opus-4-8\n"
                        "tools: Read, Grep\n---\nYou are a meticulous reviewer.\n")

            defs = m.find_claude_agent_files(agent_dir)
            self.assertIn("reviewer", defs)

            path = m.write_opencode_agent_file(out_dir, "reviewer", defs["reviewer"]["frontmatter"],
                                                defs["reviewer"]["body"])
            with open(path) as f:
                content = f.read()
            self.assertIn("You are a meticulous reviewer.", content)
            self.assertIn("model: anthropic/claude-opus-4-8", content)
            self.assertIn("Read: true", content)
            self.assertIn("Grep: true", content)

    def test_wizard_writes_at_chosen_scope(self):
        defs = {"reviewer": {"display_name": "reviewer", "frontmatter": {"description": "d"}, "body": "body\n"}}
        with tempfile.TemporaryDirectory() as target:
            buf = io.StringIO()
            with mock.patch("builtins.input", side_effect=["y", "global", "y"]), redirect_stdout(buf):
                m.import_agents_wizard_reverse(defs, lambda scope: target)
            self.assertTrue(os.path.exists(os.path.join(target, "reviewer.md")))


if __name__ == "__main__":
    unittest.main()
