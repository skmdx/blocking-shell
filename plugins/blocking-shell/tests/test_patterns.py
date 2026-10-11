# /// script
# requires-python = ">=3.11"
# dependencies = ["tree-sitter>=0.25,<0.26", "tree-sitter-bash>=0.25,<0.26"]
# ///
"""Regression cases for the history experiment's concrete failure modes."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from command_patterns import HISTORY_LIMIT, MAX_BYTES, patterns
from session_state import SessionState


def python_command(offset=0):
    assignments = '\n'.join(f'part_{i} = {i + offset}' for i in range(32))
    return "python3 - <<'PY'\n" + assignments + "\nprint(part_0 + part_31)\nPY"


class PatternsTest(unittest.TestCase):
    def matches(self, first, second):
        return any(a.matches(b) for a in patterns(first) for b in patterns(second))

    def test_case_and_output_changes_keep_test_identity(self):
        prefix = 'python3 -m pytest -c tests/config.ini '
        suffix = ' --basetemp=/build/' + 'results/' * 45
        self.assertTrue(self.matches(prefix + "'tests/test_runtime.py::test_runtime[first]'" + suffix,
                                     prefix + "'tests/test_runtime.py::test_runtime[second]'" + suffix + 'other'))
        self.assertFalse(self.matches(prefix + 'tests/test_reader.py' + suffix,
                                      prefix + 'tests/test_writer.py' + suffix))

    def test_wrappers_compare_bodies_and_common_steps(self):
        tests = ' '.join(f'tests/integration/scenario_{i}.test.ts' for i in range(16))
        shared = 'pnpm test:unit && pnpm exec vitest run ' + tests
        a = "npx -p node -p pnpm -c '" + shared + " && pnpm build'"
        b = "npx -p node -p pnpm -c 'pnpm format:check && " + shared + "'"
        self.assertTrue(self.matches(a, b))
        for wrapper in ['npx -p node -c', 'flock lock docker exec builder bash -c', 'ssh target']:
            self.assertFalse(self.matches(wrapper + " 'pnpm build " + tests + "'",
                                          wrapper + " 'pnpm publish " + tests + "'"))

    def test_python_is_not_shell(self):
        first = python_command() + "\n"
        self.assertTrue(self.matches(first, python_command(20)))
        self.assertTrue(patterns(first.replace('print(part_0 + part_31)',
                                              "# a comment's unmatched quote\nprint(part_0 + part_31)")))
        self.assertFalse(self.matches(first, first.replace('print(', 'exec(')))
        self.assertEqual(patterns(first.replace('python3', 'node')), [])

    def test_limits_and_uninterpreted_payloads(self):
        self.assertEqual(patterns('printf short'), [])
        self.assertEqual(patterns('x' * (MAX_BYTES + 1)), [])
        self.assertEqual(patterns("printf '" + 'x' * 500), [])
        body = 'opaque payload ' * 40
        self.assertFalse(self.matches("custom-tool '" + body + "one'", "custom-tool '" + body + "two'"))

    def test_persistence_isolation_and_bounded_state(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, BLOCKING_SHELL_STATE_DIR=directory):
            state = SessionState('one')
            for i in range(2):
                self.assertIsNone(state.suggestion(python_command(i), '/build', '/bin/bash'))
            self.assertIsNotNone(SessionState('one').suggestion(python_command(3), '/build', '/bin/bash'))
            self.assertIsNone(state.suggestion(python_command(4), '/build', '/bin/bash'))
            self.assertIsNone(SessionState('two').suggestion(python_command(), '/build', '/bin/bash'))
            self.assertIsNone(state.suggestion(python_command(), '/other', '/bin/bash'))
            self.assertIsNone(state.suggestion(python_command(), '/build', '/bin/fish'))
            for i in range(HISTORY_LIMIT + 2):
                state.suggestion(python_command(), f'/build/{i}', '/bin/bash')
            with sqlite3.connect(state.path) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM command_patterns WHERE session='one'").fetchone()[0], HISTORY_LIMIT)

    def test_fragments_count_calls_not_occurrences(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, BLOCKING_SHELL_STATE_DIR=directory):
            state = SessionState('one')
            step = 'printf %s ' + 'argument ' * 30
            command = ' && '.join([step] * 5)
            self.assertIsNone(state.suggestion(command, '/build', '/bin/bash'))
            self.assertIsNone(state.suggestion(command, '/build', '/bin/bash'))
            self.assertIsNotNone(state.suggestion(command, '/build', '/bin/bash'))
            self.assertIsNone(state.suggestion(command, '/build', '/bin/bash'))


if __name__ == '__main__':
    unittest.main()
