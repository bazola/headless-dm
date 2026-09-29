"""Reusable accounting contract; no admin, budgets, credentials or paid calls."""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[2] / "services"
sys.path.insert(0, str(BASE))
from accounting import RequestReader, RequestStore  # noqa: E402


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "requests.sqlite"
        self.writer = RequestStore(self.path)
        self.reader = RequestReader(self.path)

    def record(
        self,
        request_id,
        cost=None,
        status="success",
        purpose="chat_reply",
        model="test",
        content=False,
    ):
        writer = RequestStore(self.path, retain_content=content)
        writer.create(
            dict(
                request_id=request_id,
                timestamp="2026-09-27T12:00:00+00:00",
                model=model,
                purpose=purpose,
                bot="Example",
                run_id="run",
            ),
            {"messages": [{"role": "user", "content": "private request"}]},
        )
        writer.dispatch(request_id)
        writer.finish(
            request_id,
            status,
            result={
                "id": "provider-" + request_id,
                "choices": [{"message": {"content": "private response"}}],
                "usage": {"cost": cost, "prompt_tokens": 10, "completion_tokens": 5},
            },
            seconds=2,
        )

    def test_accounting_does_not_require_or_create_budget_schema(self):
        self.record("one", 0.02)
        with sqlite3.connect(self.path) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(requests)")}
        self.assertNotIn("budget_charge_usd", columns)
        self.assertEqual(self.reader.summary()["cost"], 0.02)

    def test_unknown_free_failed_and_pending_calls_are_distinct(self):
        self.record("paid", 0.02)
        self.record("free", 0)
        self.record("unknown")
        self.record("failed", 0.01, status="provider_error")
        self.writer.create(
            dict(request_id="pending", timestamp="2026-09-27T13:00:00Z", model="test"),
            {},
        )
        self.writer.dispatch("pending")
        result = self.reader.summary()
        self.assertAlmostEqual(result["cost"], 0.03)
        self.assertEqual(
            (
                result["calls"],
                result["failures"],
                result["pending"],
                result["unknownCostCount"],
            ),
            (3, 1, 1, 1),
        )
        self.assertIsNone(self.reader.detail("unknown")["record"]["cost"])
        self.assertEqual(self.reader.detail("free")["record"]["cost"], 0)

    def test_content_is_not_retained_by_default(self):
        self.record("private", 0.01)
        detail = self.reader.detail("private", include_content=True)["record"]
        self.assertIsNone(detail["prompt"])
        self.assertIsNone(detail["response"])
        self.assertNotIn(b"private request", self.path.read_bytes())
        self.assertNotIn(b"private response", self.path.read_bytes())

    def test_retained_content_requires_explicit_detail_access(self):
        self.record("retained", 0.01, content=True)
        self.assertNotIn("prompt", self.reader.detail("retained")["record"])
        self.assertNotIn("private request", json.dumps(self.reader.list()))
        self.assertNotIn("private response", json.dumps(self.reader.summary()))
        self.assertEqual(
            self.reader.detail("retained", include_content=True)["record"]["response"],
            "private response",
        )

    def test_malformed_retained_response_remains_inspectable(self):
        self.record("malformed", 0.01, content=True)
        for payload in [
            [],
            {"choices": [None]},
            {"choices": "invalid"},
            {"choices": [{"message": None}]},
        ]:
            with self.subTest(payload=payload):
                with sqlite3.connect(self.path) as db:
                    db.execute(
                        "UPDATE requests SET response_json=? WHERE request_id=?",
                        (json.dumps(payload), "malformed"),
                    )
                record = self.reader.detail("malformed", include_content=True)["record"]
                self.assertEqual(record["responsePayload"], payload)
                self.assertEqual(record["response"], payload)

    def test_filters_and_pages_are_sql_backed_and_deterministic(self):
        for i in range(7):
            self.record(str(i), 0.01, purpose="chat_reply" if i % 2 else "lore_guild")
        result = self.reader.list(
            {"purpose": "lore_guild", "limit": "2", "offset": "1"}
        )
        self.assertEqual(result["total"], 4)
        self.assertEqual([r["requestId"] for r in result["records"]], ["4", "2"])
        self.assertEqual(self.reader.list({"q": "%' OR 1=1--"})["total"], 0)
        self.assertEqual(self.reader.summary({"purpose": "chat_reply"})["calls"], 3)

    def test_time_offsets_and_grouping(self):
        self.record("one", 0.02, model="model-a")
        self.record("two", 0.03, model="model-b")
        result = self.reader.summary(
            {
                "since": "2026-09-27T05:00:00-07:00",
                "until": "2026-09-27T12:00:01Z",
                "groupBy": "model",
            }
        )
        self.assertEqual({g["model"] for g in result["groups"]}, {"model-a", "model-b"})
        self.assertEqual(
            self.reader.summary({"until": "2026-09-27T12:00:00Z"})["attempts"], 0
        )

    def test_invalid_filters_and_pagination_are_rejected(self):
        for params in [
            {"limit": "NaN"},
            {"limit": "1.5"},
            {"limit": "201"},
            {"offset": "-1"},
            {"since": "2026-09-27"},
        ]:
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.reader.list(params)
        with self.assertRaises(ValueError):
            self.reader.summary({"groupBy": "model; DROP TABLE requests"})

    def test_missing_database_is_not_created(self):
        reader = RequestReader(self.path.parent / "absent.sqlite")
        self.assertFalse(reader.list()["available"])
        self.assertFalse(reader.summary()["available"])
        self.assertFalse(reader.path.exists())

    def test_invalid_provider_cost_remains_unknown_and_does_not_break_summary(self):
        self.record("invalid", float("nan"))
        self.assertEqual(self.reader.summary()["unknownCostCount"], 1)
        self.assertIsNone(self.reader.detail("invalid")["record"]["cost"])

    def test_cli_and_library_agree_without_a_web_server(self):
        query = chr(0x661F)
        self.record("cli", 0.01, model=query)
        process = subprocess.run(
            [sys.executable, "-B", "-m", "accounting", "--db", str(self.path)],
            cwd=BASE,
            input=json.dumps(
                {"operation": "list", "params": {"q": query, "limit": 10}},
                ensure_ascii=False,
            ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(
            json.loads(process.stdout), self.reader.list({"q": query, "limit": 10})
        )


if __name__ == "__main__":
    unittest.main()
