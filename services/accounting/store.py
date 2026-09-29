"""Provider-independent request storage. No budgets, HTTP server or UI dependencies."""

import json
import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

CONTEXT_FIELDS = (
    "purpose",
    "bot",
    "bot_guid",
    "guild",
    "guild_id",
    "source",
    "route",
    "run_id",
    "job_id",
    "stage",
    "attempt",
    "parent_id",
)


def now():
    return datetime.now(timezone.utc).isoformat()


def sanitize_context(body):
    raw = body.get("hdm_context") or {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        field: "".join(c for c in str(raw.get(field) or "") if c.isprintable())[:120]
        for field in CONTEXT_FIELDS
    }


def redact(value, secret=""):
    """Keep useful local content without copying credentials into the ledger."""
    if isinstance(value, dict):
        return {
            str(k): "[redacted]"
            if str(k).lower()
            in {
                "authorization",
                "api_key",
                "openrouter_api_key",
                "access_token",
                "password",
            }
            else redact(v, secret)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v, secret) for v in value]
    if isinstance(value, str):
        if secret:
            value = value.replace(secret, "[redacted]")
        value = re.sub(r"sk-or-v1-[A-Za-z0-9_-]+", "[redacted]", value)
        return re.sub(
            r"(?i)\bBearer\s+[A-Za-z0-9_.~+/-]{8,}", "Bearer [redacted]", value
        )
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def normalize_usage(value):
    raw = value or {}
    raw = raw if isinstance(raw, dict) else {}
    usage = {}
    for name in ("cost", "prompt_tokens", "completion_tokens", "total_tokens"):
        value = raw.get(name)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
        ):
            usage[name] = value
    for name in ("completion_tokens_details", "prompt_tokens_details", "cost_details"):
        if isinstance(raw.get(name), dict):
            usage[name] = {
                k: v
                for k, v in raw[name].items()
                if isinstance(v, (int, float))
                and not isinstance(v, bool)
                and math.isfinite(v)
                and v >= 0
            }
    return usage


class RequestStore:
    """Explicit writer; constructing a reader never creates or migrates a database."""

    def content_json(self, value, secret=""):
        return json.dumps(
            redact(value, secret) if self.retain_content else None, ensure_ascii=False
        )

    def error_json(self, error, secret=""):
        if not error:
            return None
        if not self.retain_content:
            error = (
                {key: error[key] for key in ("code", "http_status") if key in error}
                if isinstance(error, dict)
                else {}
            )
        return json.dumps(redact(error, secret))

    def __init__(self, path, *, retain_content=False, timeout=1):
        self.path = Path(path)
        self.retain_content = retain_content
        self.timeout = timeout
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.touch(mode=0o600, exist_ok=True)
        with self.connect() as db:
            # Exporters hold a consistent read snapshot across several aggregates.
            # WAL lets request writes commit while that snapshot is still open.
            mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() != "wal":
                raise sqlite3.OperationalError("Request accounting requires WAL mode")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, finished_at TEXT,
                    model TEXT NOT NULL, reasoning_effort TEXT, purpose TEXT, bot TEXT,
                    bot_guid TEXT, guild TEXT, guild_id TEXT, source TEXT, route TEXT,
                    run_id TEXT, job_id TEXT, stage TEXT, attempt TEXT, parent_id TEXT,
                    status TEXT NOT NULL, seconds REAL, provider_id TEXT, http_status INTEGER,
                    prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER,
                    actual_cost_usd REAL, dispatched INTEGER NOT NULL DEFAULT 0,
                    request_json TEXT NOT NULL, response_json TEXT,
                    usage_json TEXT, error_json TEXT, metadata_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS requests_timestamp ON requests(timestamp DESC);
                CREATE INDEX IF NOT EXISTS requests_run ON requests(run_id, timestamp);
                CREATE INDEX IF NOT EXISTS requests_job ON requests(job_id, timestamp);
                CREATE INDEX IF NOT EXISTS requests_purpose_time ON requests(purpose, timestamp DESC);
                CREATE INDEX IF NOT EXISTS requests_model_time ON requests(model, timestamp DESC);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=self.timeout)
        try:
            db.row_factory = sqlite3.Row
            db.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
            with db:
                yield db
        finally:
            db.close()

    def create(self, record, request, secret=""):
        safe_record = {
            k: redact(record.get(k, ""), secret)
            for k in (
                "request_id",
                "timestamp",
                "model",
                "reasoning_effort",
                *CONTEXT_FIELDS,
            )
        }
        columns = (
            "request_id",
            "timestamp",
            "model",
            "reasoning_effort",
            *CONTEXT_FIELDS,
        )
        with self.connect() as db:
            db.execute(
                f"INSERT INTO requests ({','.join(columns)},status,request_json,metadata_json) "
                f"VALUES ({','.join('?' for _ in columns)},?,?,?)",
                [safe_record.get(k, "") for k in columns]
                + [
                    "received",
                    self.content_json(request, secret),
                    json.dumps(safe_record),
                ],
            )

    def dispatch(self, request_id):
        with self.connect() as db:
            changed = db.execute(
                "UPDATE requests SET dispatched=1,status='in_flight' WHERE request_id=? AND status='received' AND dispatched=0",
                (request_id,),
            ).rowcount
            if changed != 1:
                raise ValueError("Request is absent or already dispatched")

    def finish(
        self,
        request_id,
        status,
        *,
        result=None,
        error=None,
        seconds=0,
        http_status=None,
        secret="",
    ):
        """Record normalized OpenAI-style usage. Missing billing stays unknown.

        Other providers should map their reported USD cost and token counters into
        this small envelope. No price estimates or provider network calls occur here.
        """
        result = result if isinstance(result, dict) else {}
        usage = normalize_usage(result.get("usage"))
        with self.connect() as db:
            changed = db.execute(
                "UPDATE requests SET finished_at=?,status=?,seconds=?,http_status=?,provider_id=?,prompt_tokens=?,completion_tokens=?,total_tokens=?,actual_cost_usd=?,response_json=?,usage_json=?,error_json=? WHERE request_id=?",
                (
                    now(),
                    status,
                    seconds,
                    http_status,
                    redact(result.get("id"), secret),
                    *[
                        usage.get(k)
                        for k in (
                            "prompt_tokens",
                            "completion_tokens",
                            "total_tokens",
                            "cost",
                        )
                    ],
                    self.content_json(result, secret),
                    json.dumps(usage),
                    self.error_json(error, secret),
                    request_id,
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("Request not found")
