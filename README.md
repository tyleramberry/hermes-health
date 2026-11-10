# hermes-health

Apple Health data for AI agents. Your phone pushes; your agent can read it.

HealthKit is device-local: nothing on a Mac, server, or cloud can pull it. So the
only workable shape is **phone → host → agent**, and this project is the host and
agent half of that — an ingest endpoint, a store that aggregates on the way in,
tools to query it, and a dashboard.

Standard library only. No pip install, no build step, no cloud account, no
database server. It runs on the system Python (3.9+) that ships with macOS.

```
iPhone ──Shortcuts or Health Auto Export──▶ ingest endpoint ──▶ SQLite ──▶ agent tools
                                              (this repo)        (this repo)
```

## What you get

| Piece | What it does |
|---|---|
| `hermes-health serve` | Ingest endpoint for phone pushes. Accepts the **Shortcuts** JSON shape and the **Health Auto Export** REST v2 shape on the same URL. |
| `hermes-health import-export` | Streams a 150 MB `export.xml` backfill in seconds. |
| Store | SQLite: raw samples (deduplicated), hourly rollups, daily rollups, sleep intervals, workouts. |
| Hermes plugin | Seven tools — `health_summary`, `health_query`, `health_sleep`, `health_workouts`, `health_metrics`, `health_status`, `health_dashboard` — plus a `/health` command and the `health-brief` skill. |
| `health_mcp.py` | The same six read tools over stdio MCP, for Claude Code, Cursor, or anything else that speaks MCP. |
| `hermes-health widget` | A self-contained HTML dashboard: rings, resting-HR and HRV trends, sleep stages, recent workouts. |

## Install

**As a Hermes plugin** (gives you the tools, the slash command, and the skill):

```bash
hermes plugins install tyleramberry/hermes-health
hermes plugins enable hermes-health
```

**Or just as a CLI**, from a checkout:

```bash
git clone https://github.com/tyleramberry/hermes-health
cd hermes-health
./hermes-health status
```

**Or as an MCP server** for a non-Hermes harness (`hermes-health mcp-config`
prints the block with your paths already filled in):

```json
{
  "mcpServers": {
    "health": {
      "command": "python3",
      "args": ["/path/to/hermes-health/health_mcp.py"],
      "env": {"HERMES_HEALTH_DB": "/home/you/.hermes-health/health.db"}
    }
  }
}
```

## Get data in

### Fastest: the free Shortcuts path

No app, no Xcode, no signing, no expiry. Built into iOS.

1. Start the receiver on the host that is always on:

   ```bash
   hermes-health serve --host 0.0.0.0 --port 8787
   ```

   Keep that port on a VPN or LAN — never the open internet. Over Tailscale the
   network is the auth layer; otherwise set `HERMES_HEALTH_TOKEN` and send it as
   `Authorization: Bearer <token>`.

2. In Shortcuts, build one automation that reads Health and POSTs JSON:

   - **Find Health Samples** — Type = Steps (repeat per metric), sorted by start date
   - **Repeat with Each** → a text row: `StartDate | Value`
   - **Combine Text** with newlines
   - **Get Contents of URL** → POST `http://<host>:8787/ingest`, request body **JSON**,
     one field per metric

   ```json
   {"steps": "2026-11-08T08:00:00-07:00 | 8123",
    "resting_heart_rate": "2026-11-08T06:00:00-07:00 | 52",
    "sleep": "2026-11-07T23:10:00-07:00 | 2026-11-08T06:35:00-07:00 | Core"}
   ```

3. Automate it: Shortcuts → Automation → Time of Day → Daily → **Run Immediately**.

The endpoint answers with a receipt (`{"stored": 4, "duplicates": 0, ...}`), so
the last action of the shortcut can show it and "did it sync?" is answerable on
the phone.

**The one thing Shortcuts cannot do: workouts.** `Find Health Samples` returns
quantity and category samples only; workouts are a different HealthKit object.
Use the export below, or a Health Auto Export automation, for those.

### Complete: Health Auto Export

The paid app (one-time or lifetime IAP, and it does background sync). Add a REST
API automation pointing at `http://<host>:8787/ingest` with the default "REST v2"
payload — it is understood as-is, including workouts — plus a `Heads` presence in
the path is not needed. Nothing to configure on this side.

### Backfill: your Health export

Health app → tap your picture → Export All Health Data → `export.xml`.

```bash
hermes-health import-export ~/Downloads/apple_health_export/apple_health_export/export.xml
```

360,092 records in ~11 seconds on an M-series Mac, and re-running it changes
nothing. It also picks up your **ring goals** from `ActivitySummary` and your
profile from `Me`, so the dashboard compares against your real goals rather than
a guess.

### Ask the phone for a delta

`GET /cursor` returns the newest timestamp per metric. Have the shortcut read it
first and filter `Find Health Samples` to the window since then.

## Query it

```bash
hermes-health status                       # what is in the store, and how fresh
hermes-health brief --days 7               # plain-text brief, no model call
hermes-health summary --days 7 --json
hermes-health query resting_heart_rate --days 30
hermes-health sleep --days 14
hermes-health workouts --days 30
hermes-health widget --out /tmp/health.html
```

Metric names are flexible on purpose: `steps`, `Steps`, `step_count` and
`HKQuantityTypeIdentifierStepCount` all resolve to the same series, so it does not
matter which of the three sources filled the store.

**Aggregation happens at ingest, per metric**, because summing a heart rate is
meaningless:

| Rule | Metrics | Daily value |
|---|---|---|
| `sum` | steps, active/basal energy, distance, exercise/stand minutes, flights | total for the day |
| `avg` | heart rate, resting HR, HRV, SpO2, respiratory rate, wrist temp | mean of the day's samples |
| `last` | weight, VO₂ max, height, walking steadiness | last reading of the day |
| `max`/`min` | underwater depth | extreme of the day |

Inexact spellings and unknown metrics are not dropped: an unrecognized name gets a
slug and an aggregation rule guessed from its unit, so a metric Apple adds next
year still lands in the database.

## How the store is laid out

```
samples_raw     one row per distinct sample; PK (metric, ts, source)
                → re-sending a window stores nothing twice
samples_hourly  rollup per metric/source/hour, rebuilt from raw
daily           rollup per metric/source/local day, rebuilt from hourly
sleep           sleep-analysis intervals (start, end, stage)
workouts        duration, energy, distance, heart rate
ingest_log      one row per delivery: the "when did my phone last push?" query
meta            schema version, timezone, ring goals, profile
```

Raw rows are pruned after `raw_keep_days` (default 120); hourly and daily rollups
keep the full history, so a year of data stays a few megabytes while recent
re-sends stay exactly-once. A sample arriving for an already-pruned hour is merged
in as a delta rather than rebuilding that hour — which is why a late arrival can
never wipe rollup history.

Day attribution uses each **sample's own UTC offset** when the phone sends one
(what the phone thought the day was), falling back to the configured timezone
(`HERMES_HEALTH_TZ`, else the host's zone).

## Configuration

Everything is settings (plugin config), environment, or flags — in that order.

| Setting | Env | Default |
|---|---|---|
| `db_path` | `HERMES_HEALTH_DB` | `~/.hermes-health/health.db` |
| `timezone` | `HERMES_HEALTH_TZ` | host zone |
| `ingest_host` | `HERMES_HEALTH_HOST` | `127.0.0.1` |
| `ingest_port` | `HERMES_HEALTH_PORT` | `8787` |
| `ingest_token` | `HERMES_HEALTH_TOKEN` | none (network is the auth layer) |
| `raw_keep_days` | — | `120` |

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| POST | `/ingest` (also `/ingest/shortcuts`, `/ingest/hae`) | Both payload shapes; returns a receipt |
| GET | `/health`, `/status` | Counts, data range, last delivery, freshness |
| GET | `/cursor[?metrics=steps,hrv]` | Newest timestamp per metric, for delta sends |
| GET | `/metrics` | Metric catalog with units and aggregation rules |
| GET | `/summary?days=7[&end=YYYY-MM-DD]` | Rings, sleep, heart, workouts, changes |
| GET | `/query?metric=steps&days=30` | One daily series with trend |
| GET | `/sleep?days=14`, `/workouts?days=30` | Sleep nights, workout sessions |
| GET | `/dashboard?days=7` | The dashboard as HTML |

## Keep it running

Run the receiver as a background service so it survives logout and reboots:

```bash
hermes-health serve --host 100.x.y.z --port 8787   # bind the VPN address
```

Template at `deploy/launchd.plist` (macOS) and `deploy/hermes-health.service`
(systemd). Both call `healthd.py`, which needs nothing but a system Python.

## Tests

```bash
python3 -m unittest discover -v
```

107 tests, no test dependencies, and they include a real HTTP round trip, a real
stdio MCP handshake in a subprocess, and a synthetic Health export parsed end to
end.

## Limits worth knowing

- **Workouts need the export or Health Auto Export.** Shortcuts cannot read them.
- **The Shortcuts path runs on an unlocked phone.** It is not background sync;
  pick an hour you are actually using the device.
- **`Find Health Samples` can time out on huge ranges.** Prefer grouping by day.
- **Sleep stage names are localized.** The parser matches substrings, so a
  non-English phone works, but a brand-new phrasing needs a token added in
  `metrics.sleep_stage`.
- **No workout routes, no raw per-second series.** Both are deliberate: this store
  answers "how am I trending", not "replay my Tuesday".

## License

MIT — see `LICENSE`.
