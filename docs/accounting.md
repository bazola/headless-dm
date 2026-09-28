# Model request accounting

Optional accounting records provider attempts from the live chat router and the
batch lore fleet in a private SQLite ledger. The exporter publishes a small JSON
snapshot for the companion **Costs** page in
[mod-dashboard](https://github.com/bazola/mod-dashboard). Recording and export can
run without the worldserver. Viewing the existing dashboard still requires it.

This uses Python's standard library and the existing `common.site` configuration
loader. It adds no proxy, HTTP server, framework or model calls.

## Enable

1. Add `ACCOUNTING_ENABLED=1` to `site/site.env`. Optionally set `ACCOUNTING_DB`
   to an absolute path; the default is `site/accounting/requests.sqlite` (relative
   to `SITE_DIR`, if configured). All recording services must use the same path
   and have permission to write there. Keep it outside every served directory.
2. Restart the Python router and any running Python generation services to load
   the setting. No worldserver rebuild or restart is required for recording.
3. From the repository's `services/` directory, run:

   ```sh
   python3 -m accounting.export --interval 30
   ```

   The default destination is `DATA_DIR/dashboard-data/accounting.json`. Set
   `ACCOUNTING_EXPORT` or pass `--output` if `Dashboard.DataRoot` differs. Explicit
   `--db` and `--output` arguments override the configured paths. Omit `--interval`
   to publish once. The exporter needs read access to the ledger and write access
   to the public data directory; the dashboard needs only the exported file.

For the existing systemd installation, `ops/systemd/install.sh --user` installs
the optional unit. Enable it with
`systemctl --user enable --now wow-accounting.service`. Installation alone does
not enable it. This is an independent user service, so it continues exporting
when the realm is stopped. On other deployments, supervise the same command
with `services/` as its working directory and the appropriate `SITE_DIR`.

Disable recording by setting `ACCOUNTING_ENABLED=0` and restarting the affected
Python services. Existing history remains readable. Stop the exporter separately
if desired. Nothing is enabled by default.

## What is counted

- Each dispatched provider attempt is one request. Router fallbacks receive
  separate rows with a shared parent ID. Waiting for a busy lane is not a call.
- Successes, failures, interrupted requests and pending attempts remain distinct.
  A billed response that fails content validation still contributes its charge.
- Token counts and USD costs come from the provider's OpenAI-compatible `usage`
  object: `prompt_tokens`, `completion_tokens`, optional
  `completion_tokens_details.reasoning_tokens`, and optional `cost`.
  **Missing cost is unknown, not zero.** A reported zero means free. There are no
  pricing estimates, provider balance requests, spending caps or budget policy.
- Existing lore, validation, memory, society and chronicle callers supply purpose
  and stage metadata, with actor IDs where known. Router clients may supply a
  top-level `hdm_context` object. It is consumed locally, never sent to the model
  provider. Calls without specific attribution use a general chat/batch purpose.
- Only calls through `lore.fleet.Lane.chat` and `router.call_backend` are covered.
  Other HTTP clients, speech services and calls made outside this installation
  are not counted. These totals are not the provider account's total bill.
  Use the normal topology: batch lanes call model backends directly, not the
  instrumented router, which would record both hops.

Recording failures warn without retrying or blocking the model operation beyond
the short SQLite lock timeout. They can leave missing or pending records, so this
is operational accounting rather than a billing guarantee. Abrupt process death
can also leave pending rows. HTTP errors and connection timeouts usually have no
usage payload and remain unknown. No retrospective provider reconciliation or
historical log import is performed.

## Privacy and storage

The native recorder never retains prompts, completions, headers or error bodies.
Its ledger includes purpose/stage, actor/model names, timestamps, status, token
usage and provider identifiers. New ledger files use mode `0600` on POSIX;
provision shared access deliberately if services run as different users.

The public exporter uses an explicit field allowlist. It excludes provider IDs,
run/job/parent IDs, raw usage, prompts, responses and errors, even when a custom
writer has opted into private content retention. Anyone with dashboard access
can see the published model and actor names and costs.

The writer enables SQLite WAL mode so exports do not block recording completed
requests. Keep the ledger on a local filesystem, not a network share, and run
recording and export under the same user or provision access to the database and
its `-wal`/`-shm` sidecar files. Opening an existing rollback-journal ledger with
the writer switches it to WAL without changing its rows.

Export uses a single SQLite read transaction and atomic file replacement. A
failed export preserves the previous snapshot. A missing database produces
`available: false` without creating a ledger. Existing databases with an
incompatible schema fail visibly; the reader never migrates them. Back up the
private SQLite database using SQLite's backup API while writers are running.
There is no automatic deletion or retention limit in this initial version.

## Public snapshot contract (version 1)

`accounting.json` contains:

| Field | Meaning |
|---|---|
| `apiVersion` | `1` |
| `generated`, `generated_at` | Export time, epoch seconds and ISO UTC |
| `available` | Whether a compatible ledger was read |
| `scope` | `recorded_requests` |
| `totals` | All recorded requests |
| `purposes`, `models` | Same measures, grouped by `purpose` or `model` |
| `recentLimit`, `recent` | `100` and up to 100 newest request records |

Totals and each group contain `attempts`, `calls` (successes), `failures`,
`pending`, `cost` (known USD charges including billed failures),
`unknownCostCount` (finished dispatched requests lacking billing), `input`,
`output`, `reasoning`, and `unattributed`. Pending requests are counted separately
from unknown finished charges. Reasoning tokens may already be included in output
tokens; do not add them to output totals.

Each recent record has `requestId`, `timestamp`, `model`, `purpose`, `bot`,
`botId`, `guild`, `guildId`, `stage`, `status`, `seconds`, `input`, `output`,
`reasoning`, and nullable `cost`. No private fields are added implicitly. The
dashboard filters only this recent window; totals cover the full ledger.

## Programmatic access and tests

With `services/` on the Python path, `accounting.RequestReader(path)` exposes
`summary(params=None)`, `list(params=None)` and `detail(request_id)`. Reads are
metadata-only unless a caller explicitly requests retained private content.
Filters include `purpose`, `model`, `runId`, `botId`, `status`, `q`, `since`
(inclusive ISO time with timezone) and `until` (exclusive). Lists accept `limit`
(1-200) and `offset`; summaries accept `groupBy` (`purpose` or `model`).

The same interface is available as JSON over stdin/stdout, without an HTTP layer:

```sh
cd services
printf '%s' '{"operation":"summary","params":{"groupBy":"purpose"}}' |
  python3 -m accounting --db /private/path/requests.sqlite
```

Run from the repository root:

```sh
python3 -m unittest discover -s tests/accounting
```

Tests use temporary databases and a loopback fake provider. They need no realm,
credentials, third-party Python packages or paid requests. They exercise fallback
accounting, disabled/broken recording, private-content exclusion, read-only
queries and atomic publication.
