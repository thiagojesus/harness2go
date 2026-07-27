#!/usr/bin/env python3
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


class Dispatcher(unittest.TestCase):
    def _run(self, args):
        return subprocess.run([sys.executable, os.path.join(HERE, "harness2go.py")] + args,
                               capture_output=True, text=True, timeout=15)

    def test_no_args_prints_usage_and_exits_zero(self):
        result = self._run([])
        self.assertEqual(result.returncode, 0)
        self.assertIn("Available directions", result.stdout)

    def test_unknown_direction_exits_nonzero(self):
        result = self._run(["not-a-direction"])
        self.assertNotEqual(result.returncode, 0)

    def test_dispatches_to_opencode2claude(self):
        result = self._run(["opencode2claude", "--help"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("import-global", result.stdout)

    def test_dispatches_to_claude2opencode(self):
        result = self._run(["claude2opencode", "--help"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("import-global", result.stdout)

    def test_claude2opencode_list_works_without_any_db(self):
        # claude2opencode never touches opencode.db for `list` — only the
        # Claude Code projects directory.
        result = self._run(["claude2opencode", "list", "--claude-projects", "/nonexistent/projects"])
        self.assertEqual(result.returncode, 0)

    def test_dispatches_to_vscode2opencode(self):
        result = self._run(["vscode2opencode", "--help"])
        self.assertEqual(result.returncode, 0)

    def test_dispatches_to_vscode2claude(self):
        result = self._run(["vscode2claude", "--help"])
        self.assertEqual(result.returncode, 0)

    def test_vscode_list_works_without_any_real_vscode_install(self):
        result = self._run(["vscode2opencode", "list", "--vscode-user-dir", "/nonexistent/vscode"])
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
