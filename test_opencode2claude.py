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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opencode2claude as m


class StripJsoncComments(unittest.TestCase):
    def test_preserves_urls_with_double_slash(self):
        text = '{"url": "https://example.com/mcp"}'
        self.assertEqual(m.strip_jsonc_comments(text), text)

    def test_strips_line_comment(self):
        text = '{\n  "a": 1, // comment\n  "b": 2\n}'
        cleaned = m.strip_jsonc_comments(text)
        self.assertNotIn("comment", cleaned)
        self.assertEqual(json.loads(cleaned), {"a": 1, "b": 2})

    def test_strips_block_comment(self):
        text = '{ /* block\ncomment */ "a": 1 }'
        cleaned = m.strip_jsonc_comments(text)
        self.assertEqual(json.loads(cleaned), {"a": 1})

    def test_load_jsonc_strips_trailing_commas(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonc", delete=False) as f:
            f.write('{\n  "mcp": {\n    "x": {"type": "remote", "url": "https://a.com//b"},\n  },\n}')
            path = f.name
        try:
            cfg = m.load_jsonc(path)
            self.assertEqual(cfg["mcp"]["x"]["url"], "https://a.com//b")
        finally:
            os.unlink(path)

    def test_missing_file_returns_empty(self):
        self.assertEqual(m.load_jsonc("/nonexistent/path.jsonc"), {})


class FindOpencodeMcpServers(unittest.TestCase):
    def test_project_config_overrides_global(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as proj:
            global_path = os.path.join(home, "opencode.jsonc")
            with open(global_path, "w") as f:
                json.dump({"mcp": {"shared": {"type": "remote", "url": "https://global/x"}}}, f)
            with open(os.path.join(proj, "opencode.json"), "w") as f:
                json.dump({"mcp": {"shared": {"type": "remote", "url": "https://project/x"},
                                   "onlyproject": {"type": "local", "command": ["echo"]}}}, f)

            with mock.patch.object(m, "OPENCODE_GLOBAL_CONFIG_PATHS", [global_path]), \
                 mock.patch("os.path.expanduser", side_effect=lambda p: p.replace("~", home)):
                servers = m.find_opencode_mcp_servers(proj)

            self.assertEqual(servers["shared"]["url"], "https://project/x")
            self.assertIn("onlyproject", servers)


class FindSessionAgents(unittest.TestCase):
    def _row(self, id_, data):
        return {"id": id_, "data": json.dumps(data)}

    def test_collects_assistant_agent_and_task_subagents(self):
        messages = [
            self._row("m1", {"role": "user"}),
            self._row("m2", {"role": "assistant", "agent": "Sisyphus - ultraworker"}),
            self._row("m3", {"role": "assistant", "agent": "Sisyphus - ultraworker"}),
        ]
        parts_by_message = {
            "m2": [{"type": "tool", "tool": "task",
                    "state": {"input": {"subagent_type": "explore",
                                        "description": "Map price flow"}}}],
        }
        agents = m.find_session_agents(messages, parts_by_message)
        self.assertEqual(agents["Sisyphus - ultraworker"]["count"], 2)
        self.assertEqual(agents["explore"]["count"], 1)
        self.assertIn("Map price flow", agents["explore"]["descriptions"])

    def test_agent_lookup_key(self):
        self.assertEqual(m.agent_lookup_key("Sisyphus - ultraworker"), "sisyphus")
        self.assertEqual(m.agent_lookup_key("explore"), "explore")


class BuildMcpAddArgs(unittest.TestCase):
    def test_remote_http_with_masked_header(self):
        spec = {"type": "remote", "url": "https://api.example.com/mcp/",
                "headers": {"Authorization": "Bearer supersecrettoken123"}}
        real = m.build_mcp_add_args("gh", spec, "user", mask=False)
        masked = m.build_mcp_add_args("gh", spec, "user", mask=True)

        self.assertIn("supersecrettoken123", " ".join(real))
        self.assertNotIn("supersecrettoken123", " ".join(masked))
        self.assertIn("--transport", real)
        self.assertIn("http", real)
        self.assertIn("--scope", real)
        self.assertIn("user", real)

    def test_local_command_with_env(self):
        spec = {"type": "local", "command": ["npx", "-y", "some-server"], "env": {"KEY": "val"}}
        args = m.build_mcp_add_args("srv", spec, "local", mask=False)
        self.assertIn("npx", args)
        self.assertIn("-e", args)
        self.assertIn("KEY=val", args)


class WriteAgentStub(unittest.TestCase):
    def test_writes_frontmatter_and_body(self):
        with tempfile.TemporaryDirectory() as d:
            path = m.write_agent_stub(d, "Sisyphus - ultraworker", "desc here", model="claude-opus-4-8")
            self.assertTrue(os.path.exists(path))
            with open(path) as f:
                content = f.read()
            self.assertIn("name: sisyphus-ultraworker", content)
            self.assertIn("description: desc here", content)
            self.assertIn("model: claude-opus-4-8", content)
            self.assertIn("stub", content.lower())


class ImportWizards(unittest.TestCase):
    def test_mcp_wizard_declines_import_when_user_says_no(self):
        servers = {"gh": {"type": "remote", "url": "https://x", "headers": {"Authorization": "Bearer secrettok"}}}
        run_mock = mock.Mock()
        buf = io.StringIO()
        with mock.patch("builtins.input", side_effect=["n"]), redirect_stdout(buf):
            m.import_mcp_wizard(servers, run=run_mock)
        run_mock.assert_not_called()

    def test_mcp_wizard_runs_claude_mcp_add_and_never_prints_secret(self):
        servers = {"gh": {"type": "remote", "url": "https://x", "headers": {"Authorization": "Bearer secrettok"}}}
        run_mock = mock.Mock()
        buf = io.StringIO()
        # answers: import all? yes / import 'gh'? yes / scope? user
        with mock.patch("builtins.input", side_effect=["y", "y", "user"]), redirect_stdout(buf):
            m.import_mcp_wizard(servers, run=run_mock)
        run_mock.assert_called_once()
        called_args = run_mock.call_args[0][0]
        self.assertIn("secrettok", " ".join(called_args))
        self.assertNotIn("secrettok", buf.getvalue())

    def test_agents_wizard_writes_stub_at_chosen_scope(self):
        agents_info = {"explore": {"count": 3, "descriptions": {"Map stuff"}}}
        with tempfile.TemporaryDirectory() as project_dir:
            buf = io.StringIO()
            # answers: import agents? yes / scope? local / import 'explore'? yes
            with mock.patch("builtins.input", side_effect=["y", "local", "y"]), redirect_stdout(buf):
                m.import_agents_wizard(agents_info, project_dir, model_map={"explore": "claude-haiku-4-5"})
            expected = os.path.join(project_dir, ".claude", "agents", "explore.md")
            self.assertTrue(os.path.exists(expected))
            with open(expected) as f:
                self.assertIn("claude-haiku-4-5", f.read())


class ConvertSessionEndToEnd(unittest.TestCase):
    def _make_db(self, path):
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE session (
            id text PRIMARY KEY, project_id text, directory text, title text,
            time_created integer, time_updated integer)""")
        conn.execute("""CREATE TABLE message (
            id text PRIMARY KEY, session_id text, time_created integer,
            time_updated integer, data text)""")
        conn.execute("""CREATE TABLE part (
            id text PRIMARY KEY, message_id text, session_id text,
            time_created integer, time_updated integer, data text)""")

        conn.execute("INSERT INTO session VALUES (?,?,?,?,?,?)",
                     ("ses_1", "proj", "/tmp/nonexistent-project", "Test session", 1000, 2000))

        conn.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                     ("msg_u1", "ses_1", 1000, 1000,
                      json.dumps({"role": "user", "time": {"created": 1000}})))
        conn.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                     ("prt_u1", "msg_u1", "ses_1", 1000, 1000,
                      json.dumps({"type": "text", "text": "please help"})))

        conn.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                     ("msg_a1", "ses_1", 1100, 1100,
                      json.dumps({"role": "assistant", "agent": "explore", "modelID": "claude-opus-4-8",
                                  "finish": "tool-calls", "time": {"created": 1100},
                                  "tokens": {"input": 1, "output": 2, "cache": {"read": 3, "write": 4}}})))
        for pid, data in [
            ("prt_a1_0", {"type": "step-start"}),
            ("prt_a1_1", {"type": "text", "text": "On it."}),
            ("prt_a1_2", {"type": "tool", "tool": "bash", "callID": "call_1",
                          "state": {"status": "completed", "input": {"command": "ls"}, "output": "file.txt"}}),
            ("prt_a1_3", {"type": "step-finish"}),
        ]:
            conn.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                         (pid, "msg_a1", "ses_1", 1100, 1100, json.dumps(data)))

        # an empty assistant message (no parts) must be skipped, not break the chain
        conn.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                     ("msg_a_empty", "ses_1", 1150, 1150,
                      json.dumps({"role": "assistant", "modelID": "x", "time": {"created": 1150}})))

        conn.commit()
        conn.close()

    def test_convert_produces_valid_chained_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, "opencode.db")
            self._make_db(db_path)
            converter = m.Converter(db_path)
            self.addCleanup(converter.conn.close)
            result = m.convert_session(converter, "ses_1", os.path.join(d, "claude_projects"), dry_run=True)

            lines = result["lines"]
            self.assertGreater(len(lines), 0)

            prev = None
            for line in lines:
                self.assertEqual(line["parentUuid"], prev)
                prev = line["uuid"]
                json.dumps(line)  # must be JSON-serializable

            tool_use_ids = {c["id"] for l in lines if l["type"] == "assistant"
                             for c in l["message"]["content"] if c["type"] == "tool_use"}
            tool_result_ids = {c["tool_use_id"] for l in lines if l["type"] == "user"
                                and isinstance(l["message"]["content"], list)
                                for c in l["message"]["content"] if c["type"] == "tool_result"}
            self.assertEqual(tool_use_ids, tool_result_ids)

            agents = m.find_session_agents(result["messages"], result["parts_by_message"])
            self.assertIn("explore", agents)


if __name__ == "__main__":
    unittest.main()
