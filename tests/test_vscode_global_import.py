#!/usr/bin/env python3
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from harness2go import claude2opencode as co
from harness2go import claude2vscode
from harness2go import opencode2vscode
from harness2go import vscode2claude
from harness2go import vscode2opencode
from harness2go import vscode_common as vc


class McpEntryConversions(unittest.TestCase):
    def test_opencode_local_to_vscode(self):
        spec = {"type": "local", "command": ["npx", "-y", "foo"], "environment": {"K": "v"}}
        entry = vc.opencode_mcp_entry_to_vscode(spec)
        self.assertEqual(entry, {"type": "stdio", "command": "npx", "args": ["-y", "foo"], "env": {"K": "v"}})

    def test_opencode_remote_to_vscode(self):
        spec = {"type": "remote", "url": "https://x", "headers": {"Authorization": "Bearer secrettoken99"}}
        entry = vc.opencode_mcp_entry_to_vscode(spec)
        self.assertEqual(entry["type"], "http")
        self.assertEqual(entry["headers"]["Authorization"], "Bearer secrettoken99")
        masked = vc.opencode_mcp_entry_to_vscode(spec, mask=True)
        self.assertNotIn("secrettoken99", masked["headers"]["Authorization"])

    def test_vscode_stdio_to_opencode(self):
        spec = {"type": "stdio", "command": "npx", "args": ["-y", "foo"], "env": {"K": "v"}}
        entry = vc.vscode_mcp_entry_to_opencode(spec)
        self.assertEqual(entry, {"type": "local", "command": ["npx", "-y", "foo"],
                                  "enabled": True, "environment": {"K": "v"}})

    def test_vscode_remote_to_opencode(self):
        spec = {"type": "http", "url": "https://x", "headers": {"Authorization": "Bearer secrettoken99"}}
        entry = vc.vscode_mcp_entry_to_opencode(spec)
        self.assertEqual(entry["type"], "remote")
        masked = vc.vscode_mcp_entry_to_opencode(spec, mask=True)
        self.assertNotIn("secrettoken99", masked["headers"]["Authorization"])

    def test_round_trip_opencode_vscode_opencode(self):
        original = {"type": "local", "command": ["npx", "-y", "foo", "--flag"], "environment": {"K": "v"}}
        back = vc.vscode_mcp_entry_to_opencode(vc.opencode_mcp_entry_to_vscode(original))
        self.assertEqual(back["command"], original["command"])
        self.assertEqual(back["environment"], original["environment"])


class BuildClaudeMcpAddArgsFromSpec(unittest.TestCase):
    def test_stdio_includes_command_args_and_masks(self):
        spec = {"type": "stdio", "command": "npx", "args": ["-y", "foo"], "env": {"KEY": "secretvalue123"}}
        real = vc.build_claude_mcp_add_args_from_spec("srv", spec, "user", mask=False)
        masked = vc.build_claude_mcp_add_args_from_spec("srv", spec, "user", mask=True)
        self.assertIn("npx", real)
        self.assertIn("foo", real)
        self.assertIn("secretvalue123", " ".join(real))
        self.assertNotIn("secretvalue123", " ".join(masked))
        self.assertIn("--scope", real)
        self.assertIn("user", real)

    def test_http_uses_transport_flag(self):
        spec = {"type": "http", "url": "https://x", "headers": {"Authorization": "Bearer tok"}}
        args = vc.build_claude_mcp_add_args_from_spec("srv", spec, "user")
        self.assertIn("--transport", args)
        self.assertIn("http", args)
        self.assertIn("https://x", args)


class VscodeGlobalMcpFile(unittest.TestCase):
    def test_read_and_write_round_trip(self):
        with tempfile.TemporaryDirectory() as user_dir:
            path = vc.write_vscode_global_mcp_config(
                "srv1", {"type": "stdio", "command": "npx", "args": []}, user_dir)
            self.assertTrue(os.path.exists(path))
            servers = vc.find_vscode_global_mcp_servers(user_dir)
            self.assertIn("srv1", servers)

    def test_write_backs_up_existing_file_and_preserves_other_entries(self):
        with tempfile.TemporaryDirectory() as user_dir:
            path = os.path.join(user_dir, "mcp.json")
            with open(path, "w") as f:
                json.dump({"servers": {"existing": {"type": "stdio", "command": "x"}}}, f)
            vc.write_vscode_global_mcp_config("new", {"type": "stdio", "command": "y"}, user_dir)
            with open(path) as f:
                cfg = json.load(f)
            self.assertIn("existing", cfg["servers"])
            self.assertIn("new", cfg["servers"])
            backups = [f for f in os.listdir(user_dir) if ".bak-" in f]
            self.assertEqual(len(backups), 1)


class VscodeAgentFiles(unittest.TestCase):
    def test_write_and_read_agent_md(self):
        with tempfile.TemporaryDirectory() as d:
            path = vc.write_vscode_agent_file(
                d, "reviewer", {"description": "reviews code", "model": "gpt-5", "tools": ["read", "grep"]},
                "You are a reviewer.\n")
            self.assertTrue(path.endswith("reviewer.agent.md"))
            defs = co.find_claude_agent_files(d)
            self.assertIn("reviewer", defs)
            self.assertEqual(defs["reviewer"]["frontmatter"]["model"], "gpt-5")
            self.assertIn("read", defs["reviewer"]["frontmatter"]["tools"])
            self.assertIn("You are a reviewer.", defs["reviewer"]["body"])

    def test_write_claude_agent_file_preserves_body_verbatim(self):
        with tempfile.TemporaryDirectory() as d:
            path = vc.write_claude_agent_file(
                d, "reviewer", {"description": "reviews code", "model": "gpt-5", "tools": ["read", "grep"]},
                "Full original body text.\n")
            with open(path) as f:
                content = f.read()
            self.assertIn("Full original body text.", content)
            self.assertIn("model: gpt-5", content)
            self.assertIn("tools: read, grep", content)


class OpencodeToVscodeWizards(unittest.TestCase):
    def test_mcp_wizard_writes_converted_entry(self):
        servers = {"weather": {"type": "local", "command": ["npx", "weather"], "environment": {"K": "secretXYZ"}}}
        with tempfile.TemporaryDirectory() as user_dir:
            buf = io.StringIO()
            with mock.patch("builtins.input", side_effect=["y", "y"]), redirect_stdout(buf):
                opencode2vscode.import_mcp_wizard_to_vscode(servers, user_dir)
            servers_written = vc.find_vscode_global_mcp_servers(user_dir)
            self.assertIn("weather", servers_written)
            self.assertEqual(servers_written["weather"]["type"], "stdio")
            self.assertNotIn("secretXYZ", buf.getvalue())

    def test_agents_wizard_writes_stub_for_plugin_agent(self):
        agent_defs = {"sisyphus": {"kind": "stub", "display_name": "sisyphus", "model": "claude-opus-4-8"}}
        with tempfile.TemporaryDirectory() as target:
            with mock.patch.object(vc, "VSCODE_GLOBAL_AGENT_DIR", target):
                buf = io.StringIO()
                with mock.patch("builtins.input", side_effect=["y", "y"]), redirect_stdout(buf):
                    opencode2vscode.import_agents_wizard_to_vscode(agent_defs)
            self.assertTrue(os.path.exists(os.path.join(target, "sisyphus.agent.md")))

    def test_agents_wizard_writes_full_definition_for_real_file(self):
        agent_defs = {"reviewer": {"kind": "file", "display_name": "reviewer",
                                    "frontmatter": {"description": "d", "model": "anthropic/claude-opus-4-8"},
                                    "body": "Real prompt body.\n"}}
        with tempfile.TemporaryDirectory() as target:
            with mock.patch.object(vc, "VSCODE_GLOBAL_AGENT_DIR", target):
                buf = io.StringIO()
                with mock.patch("builtins.input", side_effect=["y", "y"]), redirect_stdout(buf):
                    opencode2vscode.import_agents_wizard_to_vscode(agent_defs)
            path = os.path.join(target, "reviewer.agent.md")
            self.assertTrue(os.path.exists(path))
            with open(path) as f:
                content = f.read()
            self.assertIn("Real prompt body.", content)


class VscodeToOpencodeWizards(unittest.TestCase):
    def test_mcp_wizard_writes_into_opencode_config(self):
        servers = {"search": {"type": "http", "url": "https://x", "headers": {"Authorization": "Bearer secretABC"}}}
        with tempfile.TemporaryDirectory() as d:
            config_path = os.path.join(d, "opencode.jsonc")
            with mock.patch.object(co, "GLOBAL_OPENCODE_CONFIG_PATH", config_path):
                buf = io.StringIO()
                with mock.patch("builtins.input", side_effect=["y", "y"]), redirect_stdout(buf):
                    vscode2opencode.import_mcp_wizard_to_opencode(servers, "/unused")
                self.assertNotIn("secretABC", buf.getvalue())
            with open(config_path) as f:
                cfg = json.load(f)
            self.assertEqual(cfg["mcp"]["search"]["type"], "remote")

    def test_agents_wizard_writes_into_opencode_agent_dir(self):
        agent_defs = {"reviewer": {"display_name": "reviewer",
                                    "frontmatter": {"description": "d", "model": "gpt-5"},
                                    "body": "Body.\n"}}
        with tempfile.TemporaryDirectory() as target:
            with mock.patch.object(co, "GLOBAL_OPENCODE_AGENT_DIR", target):
                with mock.patch("builtins.input", side_effect=["y", "y"]):
                    vscode2opencode.import_agents_wizard_to_opencode(agent_defs)
            self.assertTrue(os.path.exists(os.path.join(target, "reviewer.md")))


class ClaudeToVscodeWizard(unittest.TestCase):
    def test_mcp_wizard_passthrough_write(self):
        servers = {"booking": {"type": "stdio", "command": "npx", "args": ["-y", "@x/booking"], "env": {}}}
        with tempfile.TemporaryDirectory() as user_dir:
            with mock.patch("builtins.input", side_effect=["y", "y"]):
                claude2vscode.import_mcp_wizard_to_vscode(servers, user_dir)
            written = vc.find_vscode_global_mcp_servers(user_dir)
            self.assertEqual(written["booking"]["command"], "npx")


class VscodeToClaudeWizard(unittest.TestCase):
    def test_mcp_wizard_runs_claude_mcp_add(self):
        servers = {"weather": {"type": "stdio", "command": "npx", "args": ["-y", "weather"], "env": {}}}
        run_mock = mock.Mock()
        with mock.patch("builtins.input", side_effect=["y", "y"]):
            vscode2claude.import_mcp_wizard_to_claude(servers, run=run_mock)
        run_mock.assert_called_once()
        called_args = run_mock.call_args[0][0]
        self.assertIn("--scope", called_args)
        self.assertIn("user", called_args)

    def test_agents_wizard_writes_claude_agent_file(self):
        agent_defs = {"reviewer": {"display_name": "reviewer",
                                    "frontmatter": {"description": "d", "model": "gpt-5"},
                                    "body": "Body.\n"}}
        with tempfile.TemporaryDirectory() as target:
            with mock.patch("harness2go.vscode2claude.GLOBAL_CLAUDE_AGENT_DIR", target):
                with mock.patch("builtins.input", side_effect=["y", "y"]):
                    vscode2claude.import_agents_wizard_to_claude(agent_defs)
            self.assertTrue(os.path.exists(os.path.join(target, "reviewer.md")))


if __name__ == "__main__":
    unittest.main()
