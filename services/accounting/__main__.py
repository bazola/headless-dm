"""JSON over stdin/stdout; a host application supplies its own HTTP/auth layer."""

import argparse
import json
import sqlite3
import sys

from .reader import RequestReader


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--include-content", action="store_true")
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError("Query is too large")
        query = json.loads(raw.decode("utf-8"))
        if not isinstance(query, dict) or not isinstance(query.get("params", {}), dict):
            raise ValueError("Expected a query object")
        reader = RequestReader(args.db)
        operation = query.get("operation")
        if operation == "summary":
            result = reader.summary(query.get("params"))
        elif operation == "list":
            result = reader.list(query.get("params"))
        elif operation == "detail":
            result = reader.detail(
                query.get("id"), include_content=args.include_content
            )
        else:
            raise ValueError("Unknown accounting operation")
        print(json.dumps(result, allow_nan=False))
    except (ValueError, TypeError, LookupError) as error:
        print(json.dumps({"error": str(error)}))
        return 2
    except sqlite3.Error:
        print(json.dumps({"error": "Request ledger is unavailable or incompatible"}))
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
