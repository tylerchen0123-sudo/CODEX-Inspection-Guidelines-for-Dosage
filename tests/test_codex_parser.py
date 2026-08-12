import json
import tempfile
import unittest
from pathlib import Path

import collector as collector_module
from collector import Call, Collector


class CodexParserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_state_file = collector_module.STATE_FILE
        self.old_store_db = collector_module.STORE_DB
        self.old_codex_session_dirs = collector_module.CODEX_SESSION_DIRS
        self.old_wb_projects = collector_module.WB_PROJECTS
        self.old_wb_db = collector_module.WB_DB

        collector_module.STATE_FILE = self.root / "scan-state.json"
        collector_module.STORE_DB = self.root / "monitor.db"
        collector_module.CODEX_SESSION_DIRS = [self.root / "codex-sessions"]
        collector_module.WB_PROJECTS = self.root / "workbuddy-projects"
        collector_module.WB_DB = self.root / "workbuddy.db"

    def tearDown(self):
        collector_module.STATE_FILE = self.old_state_file
        collector_module.STORE_DB = self.old_store_db
        collector_module.CODEX_SESSION_DIRS = self.old_codex_session_dirs
        collector_module.WB_PROJECTS = self.old_wb_projects
        collector_module.WB_DB = self.old_wb_db
        self.tmp.cleanup()

    def write_jsonl(self, *records):
        path = self.root / "rollout-sanitized.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            for record in records:
                if isinstance(record, str):
                    fh.write(record + "\n")
                else:
                    fh.write(json.dumps(record) + "\n")
        return path

    def test_scan_codex_file_reads_last_token_usage_fields(self):
        path = self.write_jsonl(
            {
                "type": "session_meta",
                "payload": {
                    "id": "session-1",
                    "cwd": "/tmp/sanitized-project",
                    "cli_version": "test",
                },
            },
            {
                "type": "turn_context",
                "payload": {"model": "gpt-test", "cwd": "/tmp/sanitized-project"},
            },
            {
                "timestamp": "2026-08-12T12:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 120,
                            "cached_input_tokens": 20,
                            "cache_write_input_tokens": 5,
                            "output_tokens": 30,
                            "reasoning_output_tokens": 7,
                        },
                        "total_token_usage": {"total_tokens": 155},
                    },
                },
            },
        )

        calls = Collector()._scan_codex_file(path)

        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call.source, "codex")
        self.assertEqual(call.session_id, "session-1")
        self.assertEqual(call.model, "gpt-test")
        self.assertEqual(call.project, "/tmp/sanitized-project")
        self.assertEqual(call.fresh_in, 100)
        self.assertEqual(call.cache_read, 20)
        self.assertEqual(call.cache_write, 5)
        self.assertEqual(call.out, 30)
        self.assertEqual(call.reasoning, 7)

    def test_scan_codex_file_uses_total_delta_when_last_usage_is_missing(self):
        path = self.write_jsonl(
            {
                "timestamp": "2026-08-12T12:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"total_tokens": 50}},
                },
            },
            {
                "timestamp": "2026-08-12T12:01:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"total_tokens": 75}},
                },
            },
        )

        calls = Collector()._scan_codex_file(path)

        self.assertEqual([call.fresh_in for call in calls], [50, 25])
        self.assertEqual([call.out for call in calls], [0, 0])

    def test_scan_codex_file_tolerates_unexpected_records(self):
        path = self.write_jsonl(
            "not json",
            {"type": "session_meta", "payload": "unexpected"},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {}}},
            {"type": "event_msg", "payload": {"type": "other"}},
        )

        calls = Collector()._scan_codex_file(path)

        self.assertEqual(calls, [])

    def test_build_summary_aggregates_input_output_and_cache_tokens(self):
        collector = Collector()
        collector.calls = [
            Call("codex", 1_786_502_400_000, "gpt-test", "session-a", "project-a", 10, 2, 3, 5, 1),
            Call("codex", 1_786_502_460_000, "gpt-test", "session-a", "project-a", 7, 4, 1, 8, 2),
        ]

        summary = collector.build_summary(days=1)

        self.assertEqual(summary["overall"]["codex"]["calls"], 2)
        self.assertEqual(summary["overall"]["codex"]["fresh_in"], 17)
        self.assertEqual(summary["overall"]["codex"]["cache_read"], 6)
        self.assertEqual(summary["overall"]["codex"]["cache_write"], 4)
        self.assertEqual(summary["overall"]["codex"]["out"], 13)
        self.assertEqual(summary["overall"]["codex"]["reasoning"], 3)
        self.assertEqual(summary["overall"]["codex"]["total"], 40)
