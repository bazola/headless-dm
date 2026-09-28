"""Publish a metadata-only dashboard snapshot; never expose the private ledger."""

import argparse
import json
import logging
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from common import site

from .reader import RequestReader
from .recorder import database_path

TOTALS = (
    "attempts",
    "calls",
    "failures",
    "pending",
    "cost",
    "unknownCostCount",
    "input",
    "output",
    "reasoning",
    "unattributed",
)
# An allowlist is intentional: adding private fields to the reader must not publish them.
RECORD = (
    "requestId",
    "timestamp",
    "model",
    "purpose",
    "bot",
    "botId",
    "guild",
    "guildId",
    "stage",
    "status",
    "seconds",
    "input",
    "output",
    "reasoning",
    "cost",
)


def snapshot(reader):
    data = reader.summary(include_models=True)
    return {
        "apiVersion": 1,
        "generated": int(time.time()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "available": data["available"],
        "scope": "recorded_requests",
        "recentLimit": 100,
        "totals": {k: data[k] for k in TOTALS},
        "purposes": [
            {k: row[k] for k in ("purpose", *TOTALS)} for row in data["groups"]
        ],
        "models": [{k: row[k] for k in ("model", *TOTALS)} for row in data["models"]],
        "recent": [{k: row.get(k) for k in RECORD} for row in data["recent"]],
    }


def publish(database, output):
    database, output = Path(database).resolve(), Path(output).resolve()
    # The complete ledger must never sit beneath the dashboard's public data root.
    if database.is_relative_to(output.parent):
        raise ValueError(
            "The private ledger must be outside the published data directory"
        )
    data = snapshot(RequestReader(database))
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=".accounting-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(
                data, stream, ensure_ascii=True, allow_nan=False, separators=(",", ":")
            )
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default=str(database_path()), help="Private request ledger path"
    )
    parser.add_argument(
        "--output",
        default=site.get(
            "ACCOUNTING_EXPORT",
            str(
                Path(site.get("DATA_DIR", "/opt/wow/server/data"))
                / "dashboard-data/accounting.json"
            ),
        ),
        help="Public dashboard-data/accounting.json",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=0,
        help="Repeat every N seconds (minimum 5); default: once",
    )
    args = parser.parse_args()
    if args.interval and args.interval < 5:
        parser.error("--interval must be zero or at least 5 seconds")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    while True:
        try:
            publish(args.db, args.output)
        except (OSError, ValueError, sqlite3.Error):
            if not args.interval:
                raise
            logging.exception("Accounting export failed; keeping the previous snapshot")
        if not args.interval:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
