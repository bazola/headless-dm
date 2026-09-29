"""Read-only, bounded SQL queries returning a versioned, presentation-free API."""

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ACTIVE = "('received','ready','reserved','in_flight','pending')"
FIELDS = {
    "requestId": "request_id",
    "timestamp": "timestamp",
    "model": "model",
    "reasoningEffort": "reasoning_effort",
    "purpose": "purpose",
    "bot": "bot",
    "botId": "bot_guid",
    "guild": "guild",
    "guildId": "guild_id",
    "source": "source",
    "route": "route",
    "runId": "run_id",
    "jobId": "job_id",
    "stage": "stage",
    "attempt": "attempt",
    "parentId": "parent_id",
    "status": "status",
    "seconds": "seconds",
    "providerId": "provider_id",
    "httpStatus": "http_status",
    "input": "prompt_tokens",
    "output": "completion_tokens",
    "cost": "actual_cost_usd",
}


def parse(value, default=None):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def filters(params):
    clauses, values = [], []
    for key, column in {
        "purpose": "purpose",
        "model": "model",
        "runId": "run_id",
        "botId": "bot_guid",
        "status": "status",
    }.items():
        value = params.get(key)
        if value:
            if not isinstance(value, str) or len(value) > 200:
                raise ValueError("Invalid filter: " + key)
            clauses.append(f"{column}=?")
            values.append(value)
    for key, operator in [("since", ">="), ("until", "<")]:
        if params.get(key):
            value = params[key]
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.utcoffset() is None:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ValueError(
                    key + " must be an ISO timestamp with timezone"
                ) from None
            clauses.append(f"julianday(timestamp){operator}julianday(?)")
            values.append(value)
    if params.get("q"):
        query = params["q"]
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError("Search must be at most 200 characters")
        # instr treats % and _ literally, unlike SQL LIKE.
        columns = [
            "bot",
            "guild",
            "purpose",
            "stage",
            "model",
            "request_id",
            "run_id",
            "job_id",
        ]
        clauses.append(
            "("
            + " OR ".join(f"instr(lower(COALESCE({c},'')),lower(?))>0" for c in columns)
            + ")"
        )
        values.extend([query] * len(columns))
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), values


def integer(value, default, minimum, maximum):
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError("Invalid pagination value") from None
    if (
        isinstance(value, bool)
        or str(value) != str(parsed)
        or not minimum <= parsed <= maximum
    ):
        raise ValueError("Invalid pagination value")
    return parsed


class RequestReader:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        if not self.path.is_file():
            yield None
            return
        db = sqlite3.connect(
            self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5
        )
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            yield db
        finally:
            db.close()

    @staticmethod
    def normalize(row, content=False):
        r = dict(row)
        result = {key: r.get(column) for key, column in FIELDS.items()}
        usage = parse(r.get("usage_json"), {}) or {}
        usage = usage if isinstance(usage, dict) else {}
        reasoning = usage.get("completion_tokens_details") or {}
        result.update(
            purpose=r.get("purpose") or "unknown",
            reasoning=reasoning.get("reasoning_tokens", 0)
            if isinstance(reasoning, dict)
            else 0,
            hasDetail=bool(r.get("has_content"))
            or (
                content
                and any(
                    r.get(k) not in (None, "null")
                    for k in ("request_json", "response_json")
                )
            ),
        )
        if content:
            request, response = (
                parse(r.get("request_json")),
                parse(r.get("response_json")),
            )
            choices = response.get("choices") if isinstance(response, dict) else None
            choice = choices[0] if isinstance(choices, list) and choices else None
            envelope = choice.get("message") if isinstance(choice, dict) else None
            message = envelope.get("content") if isinstance(envelope, dict) else None
            result.update(
                prompt=request.get("messages", request)
                if isinstance(request, dict)
                else request,
                response=message if message is not None else response,
                request=request,
                responsePayload=response,
                usage=usage,
                error=parse(r.get("error_json")),
                validation=message
                if any(s in (r.get("stage") or "") for s in ("check", "judge"))
                else None,
            )
            if request is None and response is None:
                result["detailUnavailable"] = (
                    "Prompt and response content was not retained."
                )
        return result

    @staticmethod
    def columns():
        return ",".join(
            [
                *FIELDS.values(),
                "usage_json",
                "(request_json IS NOT NULL AND request_json<>'null' OR response_json IS NOT NULL AND response_json<>'null') AS has_content",
            ]
        )

    def list(self, params=None):
        params = params or {}
        limit = integer(params.get("limit"), 100, 1, 200)
        offset = integer(params.get("offset"), 0, 0, 2_147_483_647)
        where, values = filters(params)
        with self.connect() as db:
            if db is None:
                return dict(
                    apiVersion=1,
                    available=False,
                    records=[],
                    total=0,
                    limit=limit,
                    offset=offset,
                )
            total = db.execute(
                "SELECT COUNT(*) FROM requests" + where, values
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT {self.columns()} FROM requests{where} ORDER BY timestamp DESC,request_id DESC LIMIT ? OFFSET ?",
                [*values, limit, offset],
            )
            return dict(
                apiVersion=1,
                available=True,
                records=[self.normalize(r) for r in rows],
                total=total,
                limit=limit,
                offset=offset,
            )

    def detail(self, request_id, *, include_content=False):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 120:
            raise ValueError("Invalid request ID")
        with self.connect() as db:
            if db is None:
                raise LookupError("Request ledger is unavailable")
            columns = "*" if include_content else self.columns()
            row = db.execute(
                f"SELECT {columns} FROM requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise LookupError("Request not found")
            return dict(
                apiVersion=1, record=self.normalize(row, content=include_content)
            )

    def summary(self, params=None, *, include_models=False):
        params = params or {}
        where, values = filters(params)
        group_by = params.get("groupBy", "purpose")
        if group_by not in {"purpose", "model"}:
            raise ValueError("groupBy must be purpose or model")
        measures = f"""COUNT(*) attempts,
            COALESCE(SUM(status='success'),0) calls,
            COALESCE(SUM(status IN {ACTIVE}),0) pending,
            COALESCE(SUM(status<>'success' AND status NOT IN {ACTIVE}),0) failures,
            COALESCE(SUM(actual_cost_usd),0) cost,
            COALESCE(SUM((dispatched=1 OR status='success') AND actual_cost_usd IS NULL AND status NOT IN {ACTIVE}),0) unknownCostCount,
            COALESCE(SUM(status='success' AND actual_cost_usd IS NULL),0) missingCost,
            COALESCE(SUM(prompt_tokens),0) input,
            COALESCE(SUM(completion_tokens),0) output,
            COALESCE(SUM(json_extract(usage_json,'$.completion_tokens_details.reasoning_tokens')),0) reasoning,
            COALESCE(SUM(json_extract(usage_json,'$.cost_details.upstream_inference_prompt_cost')),0) inputCost,
            COALESCE(SUM(json_extract(usage_json,'$.cost_details.upstream_inference_completions_cost')),0) outputCost,
            COALESCE(AVG(CASE WHEN dispatched=1 THEN seconds END),0) latency,
            COALESCE(SUM(purpose IS NULL OR purpose='' OR purpose='unknown'),0) unattributed"""
        with self.connect() as db:
            if db is None:
                # Same aggregate schema for a missing database, without creating a file.
                with sqlite3.connect(":memory:") as empty:
                    empty.row_factory = sqlite3.Row
                    empty.execute(
                        "CREATE TABLE requests(status,actual_cost_usd,dispatched,prompt_tokens,completion_tokens,usage_json,seconds,purpose)"
                    )
                    result = dict(
                        empty.execute(
                            "SELECT " + measures + " FROM requests"
                        ).fetchone()
                    )
                return dict(
                    result,
                    apiVersion=1,
                    available=False,
                    time=int(time.time() * 1000),
                    groups=[],
                    runIds=[],
                    recent=[],
                    **({"models": []} if include_models else {}),
                )
            result = dict(
                db.execute(
                    "SELECT " + measures + " FROM requests" + where, values
                ).fetchone()
            )
            groups = [
                dict(r)
                for r in db.execute(
                    f"SELECT COALESCE(NULLIF({group_by},''),'unknown') AS {group_by},{measures} FROM requests{where} GROUP BY 1 ORDER BY cost DESC,1",
                    values,
                )
            ]
            run_ids = [
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT run_id FROM requests"
                    + where
                    + (" AND " if where else " WHERE ")
                    + "run_id IS NOT NULL AND run_id<>'' ORDER BY run_id",
                    values,
                )
            ]
            recent = [
                self.normalize(r)
                for r in db.execute(
                    f"SELECT {self.columns()} FROM requests{where} ORDER BY timestamp DESC,request_id DESC LIMIT 100",
                    values,
                )
            ]
            return dict(
                result,
                apiVersion=1,
                available=True,
                time=int(time.time() * 1000),
                groups=groups,
                runIds=run_ids,
                recent=recent,
                **(
                    {
                        "models": [
                            dict(r)
                            for r in db.execute(
                                f"SELECT COALESCE(NULLIF(model,''),'unknown') AS model,{measures} FROM requests{where} GROUP BY 1 ORDER BY cost DESC,1",
                                values,
                            )
                        ]
                    }
                    if include_models
                    else {}
                ),
            )
