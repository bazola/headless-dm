"""Native hooks tested against a strict local provider; no realm or credentials."""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services"))
from accounting import RequestReader, recorder  # noqa: E402
from common import site  # noqa: E402


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


fleet = module("test_fleet", ROOT / "services/lore/fleet.py")
router = module("test_router", ROOT / "services/router/03-router.py")


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "private/requests.sqlite"
        self.addCleanup(patch.stopall)
        patch.object(site, "_VALUES", {}).start()
        patch.dict(
            os.environ, ACCOUNTING_ENABLED="1", ACCOUNTING_DB=str(self.database)
        ).start()
        recorder._store.cache_clear()
        self.calls = []
        calls = self.calls

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append(body)
                status = 200
                if "hdm_context" in body:
                    status, result = 400, {"error": "Unexpected metadata"}
                elif body["model"] == "fail":
                    status, result = 503, {"error": "Unavailable"}
                else:
                    result = {
                        "id": "fake-provider-id",
                        "usage": {
                            "cost": 0.02,
                            "prompt_tokens": 7,
                            "completion_tokens": 3,
                        },
                    }
                    if body["model"] != "invalid":
                        result["choices"] = [
                            {"message": {"content": "PRIVATE_RESPONSE"}}
                        ]
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def lane(self, model="batch"):
        return fleet.Lane("fixture", self.url, model, 1, {"character"})

    def backend(self, model):
        return dict(
            url=self.url + "/v1/chat/completions", model=model, timeout=5, sem=None
        )

    def test_batch_recording_and_export_work_without_the_local_proxy(self):
        result = self.lane().chat(
            [{"role": "user", "content": "PRIVATE_PROMPT"}],
            50,
            context={"purpose": "lore_backstory", "bot_guid": 12},
        )
        self.assertEqual(result, "PRIVATE_RESPONSE")
        reader = RequestReader(self.database)
        summary = reader.summary()
        self.assertEqual((summary["attempts"], summary["cost"]), (1, 0.02))
        self.assertEqual(summary["recent"][0]["purpose"], "lore_backstory")
        self.assertNotIn("hdm_context", self.calls[0])
        for private in [b"PRIVATE_PROMPT", b"PRIVATE_RESPONSE"]:
            self.assertNotIn(private, self.database.read_bytes())
        output = self.root / "public/accounting.json"
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "accounting.export",
                "--db",
                str(self.database),
                "--output",
                str(output),
            ],
            cwd=ROOT / "services",
            capture_output=True,
            text=True,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        data = json.loads(output.read_text())
        self.assertEqual(data["totals"]["cost"], 0.02)
        self.assertEqual(data["recent"][0]["botId"], "12")
        self.assertNotIn("PRIVATE_", output.read_text())

    def test_router_records_each_fallback_and_keeps_context_local(self):
        with patch.object(
            router, "ROUTES", {"fixture": [self.backend("fail"), self.backend("ok")]}
        ):
            self.assertEqual(
                router.route_call(
                    "fixture", [], {"hdm_context": {"purpose": "ambient_chat"}}
                ),
                "PRIVATE_RESPONSE",
            )
        rows = RequestReader(self.database).list()["records"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({r["parentId"] for r in rows}), 1)
        self.assertEqual({r["attempt"] for r in rows}, {"1", "2"})
        self.assertEqual({r["status"] for r in rows}, {"success", "http_error"})
        self.assertTrue(all(r["purpose"] == "ambient_chat" for r in rows))
        self.assertTrue(all("hdm_context" not in body for body in self.calls))
        summary = RequestReader(self.database).summary()
        self.assertEqual(summary["cost"], 0.02)
        self.assertEqual(summary["unknownCostCount"], 1)

    def test_disabled_recording_creates_no_files(self):
        with patch.dict(os.environ, ACCOUNTING_ENABLED="0"):
            self.assertEqual(self.lane().chat([], 50), "PRIVATE_RESPONSE")
        self.assertFalse(self.database.parent.exists())

    def test_database_failure_does_not_trigger_another_provider_request(self):
        self.database.mkdir(parents=True)
        with self.assertLogs(recorder._log, level="WARNING"):
            recorder._last_warning = float("-inf")
            self.assertEqual(self.lane().chat([], 50), "PRIVATE_RESPONSE")
        self.assertEqual(len(self.calls), 1)

    def test_billed_invalid_response_remains_a_charged_failure(self):
        with self.assertRaises(KeyError):
            self.lane("invalid").chat([], 50)
        record = RequestReader(self.database).list()["records"][0]
        self.assertEqual((record["status"], record["cost"]), ("invalid_response", 0.02))

    def test_recording_failure_after_response_does_not_retry_model(self):
        with patch.object(
            recorder.RequestStore,
            "finish",
            side_effect=sqlite3.OperationalError("disk error"),
        ):
            self.assertEqual(self.lane().chat([], 50), "PRIVATE_RESPONSE")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(RequestReader(self.database).summary()["pending"], 1)

    def test_locked_database_does_not_block_or_retry_provider(self):
        store = recorder._store(str(self.database))
        with store.connect() as lock:
            lock.execute("BEGIN EXCLUSIVE")
            with self.assertLogs(recorder._log, level="WARNING"):
                recorder._last_warning = float("-inf")
                self.assertEqual(self.lane().chat([], 50), "PRIVATE_RESPONSE")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(RequestReader(self.database).summary()["attempts"], 0)

    def test_interruption_is_not_marked_as_success(self):
        with self.assertRaises(KeyboardInterrupt):
            with recorder.record_attempt("test"):
                raise KeyboardInterrupt()
        record = RequestReader(self.database).list()["records"][0]
        self.assertEqual(record["status"], "interrupted")
        self.assertIsNone(record["cost"])

    def test_completed_request_commits_while_export_snapshot_is_open(self):
        reader = RequestReader(self.database)
        # Keep the snapshot open through record_attempt's final write, just as a
        # long export can outlive a provider response. No timing race or sleeps.
        with ExitStack() as reads:
            with recorder.record_attempt("test") as attempt:
                snapshot = reads.enter_context(reader.connect())
                before = snapshot.execute(
                    "SELECT status,actual_cost_usd FROM requests"
                ).fetchone()
                self.assertEqual(tuple(before), ("in_flight", None))
                attempt.result = {"usage": {"cost": 0.05}}
            current = reader.list()["records"][0]
            self.assertEqual((current["status"], current["cost"]), ("success", 0.05))
            # The exporter still sees one consistent snapshot, not half an update.
            held = snapshot.execute(
                "SELECT status,actual_cost_usd FROM requests"
            ).fetchone()
            self.assertEqual(tuple(held), tuple(before))

    def test_existing_rollback_ledger_keeps_history_when_opened_by_writer(self):
        self.lane().chat([], 50)
        with recorder._store(str(self.database)).connect() as db:
            db.execute("PRAGMA journal_mode=DELETE")
        recorder._store.cache_clear()
        self.lane().chat([], 50)
        summary = RequestReader(self.database).summary()
        self.assertEqual((summary["attempts"], summary["cost"]), (2, 0.04))
        with recorder._store(str(self.database)).connect() as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")


if __name__ == "__main__":
    unittest.main()
