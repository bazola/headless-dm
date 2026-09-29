"""Validate the public export boundary and atomic publication."""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str((Path(__file__).resolve().parents[2] / "services")))
from accounting import RequestStore
from accounting.export import publish


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.db = root / "private/requests.sqlite"
        self.output = root / "public/accounting.json"
        self.writer = RequestStore(self.db, retain_content=True)

    def record(self, request_id, cost, purpose="chat_reply", model="example"):
        self.writer.create(
            dict(
                request_id=request_id,
                timestamp="2026-09-27T12:00:00Z",
                purpose=purpose,
                model=model,
                run_id="private-run",
            ),
            {"messages": [{"role": "user", "content": "PRIVATE_PROMPT"}]},
        )
        self.writer.dispatch(request_id)
        self.writer.finish(
            request_id,
            "success",
            result={
                "choices": [{"message": {"content": "PRIVATE_RESPONSE"}}],
                "usage": {"cost": cost, "prompt_tokens": 3, "completion_tokens": 2},
            },
            error={"message": "PRIVATE_ERROR"},
        )

    def test_export_reconciles_models_and_preserves_unknown_and_free(self):
        self.record("paid", 0.2)
        self.record("free", 0, model="other")
        self.record("unknown", None, purpose="unknown")
        data = publish(self.db, self.output)
        self.assertEqual(data, json.loads(self.output.read_text()))
        self.assertAlmostEqual(
            sum(row["cost"] for row in data["models"]), data["totals"]["cost"]
        )
        self.assertEqual(data["totals"]["unknownCostCount"], 1)
        self.assertEqual(data["totals"]["unattributed"], 1)
        costs = {r["requestId"]: r["cost"] for r in data["recent"]}
        self.assertIsNone(costs["unknown"])
        self.assertEqual(costs["free"], 0)
        text = self.output.read_text()
        for private in [
            "PRIVATE_PROMPT",
            "PRIVATE_RESPONSE",
            "PRIVATE_ERROR",
            "private-run",
            "responsePayload",
            "hasDetail",
        ]:
            self.assertNotIn(private, text)

    def test_missing_ledger_is_unavailable_and_never_created(self):
        absent = self.db.parent / "absent.sqlite"
        self.assertFalse(publish(absent, self.output)["available"])
        self.assertFalse(absent.exists())

    def test_failed_query_preserves_last_good_snapshot(self):
        self.record("paid", 0.2)
        publish(self.db, self.output)
        before = self.output.read_bytes()
        with patch(
            "accounting.export.RequestReader.summary",
            side_effect=sqlite3.OperationalError("locked"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                publish(self.db, self.output)
        self.assertEqual(before, self.output.read_bytes())

    def test_failed_replace_preserves_snapshot_and_removes_temporary_file(self):
        publish(self.db, self.output)
        before = self.output.read_bytes()
        with patch("accounting.export.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                publish(self.db, self.output)
        self.assertEqual(before, self.output.read_bytes())
        self.assertEqual(list(self.output.parent.iterdir()), [self.output])

    def test_ledger_cannot_be_beneath_public_directory(self):
        with self.assertRaises(ValueError):
            publish(self.db, self.db.parent / "accounting.json")


if __name__ == "__main__":
    unittest.main()
