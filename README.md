# VIN Decoder

A FastAPI service that decodes 17-character VINs using the [NHTSA vPIC API](https://vpic.nhtsa.dot.gov/api/),
with a SQLite cache in front of it so the same VIN is only ever fetched once.

`/underwrite` goes further: it adds NHTSA recall campaigns and NCAP safety
ratings, surfaces the structural and mechanical detail already present in the
decode, and turns all of it into an open-recall underwriting flag
(`BLOCK` / `REFER` / `CLEAR` / `INSUFFICIENT_DATA`).

```
client ──► /lookup ──► SQLite cache ──hit──► response (cached_result: true)
                            │
                           miss
                            │
                            ▼
                     vPIC DecodeVinValues ──► write through ──► response (cached_result: false)
```

## Quick start

Requires Python 3.11+.

```bash
git clone <repo-url> && cd vin-decoder

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements-dev.txt   # or requirements.txt for runtime only
uvicorn app.main:app --reload --port 8000
```

The service is now on `http://127.0.0.1:8000`. Open that root URL for a small
web client that makes the cache visible -- look up the same VIN twice and watch
the badge flip from LIVE to CACHED. Interactive API docs are at
`http://127.0.0.1:8000/docs`. The SQLite file (`vin_cache.db`) is created on
first start in the working directory; delete it to reset the cache.

```bash
curl "http://127.0.0.1:8000/lookup?vin=1HGCM82633A004352"
```

### With Docker

```bash
docker build -t vin-decoder .
docker run --rm -p 8000:8000 -v vin-cache:/data vin-decoder
```

The named volume keeps the cache across container restarts.

## API

Interactive documentation (OpenAPI) is served at `/docs` and `/redoc`.

### `GET /lookup?vin=...` · `POST /lookup`

Decodes a VIN. Checks SQLite first; on a miss it calls vPIC, stores the result,
and returns it.

```bash
curl "http://127.0.0.1:8000/lookup?vin=1HGCM82633A004352"

curl -X POST http://127.0.0.1:8000/lookup \
  -H 'content-type: application/json' \
  -d '{"vin": "1HGCM82633A004352"}'
```

```json
{
  "vin": "1HGCM82633A004352",
  "make": "HONDA",
  "model": "Accord",
  "model_year": "2003",
  "body_class": "Coupe",
  "cached_result": false
}
```

| JSON field       | Challenge field         |
| ---------------- | ----------------------- |
| `vin`            | Input VIN Requested     |
| `make`           | Make                    |
| `model`          | Model                   |
| `model_year`     | Model Year              |
| `body_class`     | Body Class              |
| `cached_result`  | Cached Result?          |

`cached_result` describes how *this* response was produced. A cache miss
returns `false` even though the row was just written.

| Status | When                                                              |
| ------ | ----------------------------------------------------------------- |
| `200`  | Decoded, from cache or upstream                                   |
| `422`  | VIN is not 17 alphanumeric characters, or vPIC cannot decode it    |
| `502`  | vPIC was unreachable, timed out, or returned nonsense after retries |

### `POST /remove` · `DELETE /remove?vin=...`

Evicts a VIN from the cache.

```bash
curl -X POST http://127.0.0.1:8000/remove \
  -H 'content-type: application/json' \
  -d '{"vin": "1HGCM82633A004352"}'
```

```json
{ "vin": "1HGCM82633A004352", "cache_delete_success": true }
```

Removing a VIN that was not cached returns `200` with
`cache_delete_success: false` rather than a `404`. The caller asked for the VIN
not to be cached, and after the call it is not cached.

### `GET /export`

Downloads the whole cache as a parquet file.

```bash
curl -OJ http://127.0.0.1:8000/export
# -> vin_cache_20260826T143012Z.parquet
```

Columns: `vin`, `make`, `model`, `model_year`, `body_class`, `fetched_at`
(ISO-8601 UTC). All strings. The schema is declared explicitly, so an empty
cache exports a valid zero-row file with the same schema as a populated one.

### `GET /underwrite?vin=...` · `POST /underwrite`

Decodes the VIN, then adds recall campaigns and NCAP safety ratings for its
year/make/model and returns an underwriting decision.

```bash
curl "http://127.0.0.1:8000/underwrite?vin=2C3KA53G86H123456"
```

```json
{
  "vin": "2C3KA53G86H123456",
  "make": "CHRYSLER", "model": "300", "model_year": "2006",
  "underwriting": {
    "decision": "BLOCK",
    "headline": "Do not bind — NHTSA stop-driving advisory in force (4 campaigns)",
    "open_recall_count": 4,
    "vin_level_verified": false,
    "flags": [
      {"code": "DO_NOT_DRIVE", "severity": "critical", "campaigns": ["16V352000", "15V313000"]},
      {"code": "AIRBAG_INFLATOR", "severity": "warning", "campaigns": ["16V352000", "15V313000"]}
    ]
  },
  "risk_profile": {
    "claims_routing": {
      "queue": "PASSENGER_AUTO", "label": "Personal lines — auto",
      "basis": "VehicleType=PASSENGER CAR", "commercial": false
    },
    "energy_source": {
      "kind": "ICE_GASOLINE", "label": "Petrol (internal combustion)", "flags": []
    }
  },
  "recalls": [...], "safety_rating": {...}, "mechanical": {...}, "data_gaps": []
}
```

#### `risk_profile` — classification, not judgement

Two questions answered from the decode alone, with no extra upstream call:

**Claims routing.** `VehicleType` plus GVWR class picks the handling queue —
`MOTORCYCLE`, `COMMERCIAL_TRUCK`, `LIGHT_TRUCK`, `PASSENGER_AUTO`, `BUS`,
`TRAILER`, or `UNCLASSIFIED`. The split between a light and a commercial truck
is GVWR class 3, which is where the federal definition of a commercial motor
vehicle begins: a Class 2 F-150 is personal lines, a Class 8 tractor is not.
Every answer carries a `basis` string naming the fields that produced it.

**Energy source.** `ElectrificationLevel` and fuel type give `BEV`, `PHEV`,
`HEV`, `MILD_HEV`, `FCEV`, `ICE_GASOLINE`, `ICE_DIESEL` or `UNKNOWN_FUEL`, plus
handling flags: lithium-ion thermal runaway, high-voltage salvage and disposal,
restricted EV repair networks, diesel spill exposure. Chemistry is never
assumed — a 2008 Prius has a high-voltage pack but vPIC does not report that it
is nickel-metal hydride, so it gets `HV_BATTERY_CHEMISTRY_UNKNOWN` rather than a
lithium flag it has not earned.

These deliberately **do not move the underwriting decision**. That decision is
about defects (open recalls); this is about characteristics. An EV is not a
worse risk than a petrol car, it is a differently handled one, and letting a
routing fact quietly shift an underwriting outcome would make both harder to
explain.

**What the flag can and cannot tell you.** NHTSA publishes recalls by
year/make/model — there is no public endpoint for VIN-level remedy status.
So a campaign shown here covers this vehicle's year, make and model; whether
*this* car was repaired is not knowable from public data. Every assessment
carries `vin_level_verified: false` and a caveat string saying so. The rules
lean conservative in one direction only: they will send a repaired car to
review, but they will not clear an unrepaired one.

| Decision            | Meaning                                                          |
| ------------------- | ---------------------------------------------------------------- |
| `BLOCK`             | NHTSA has issued a "Do Not Drive" or "Park Outside" advisory     |
| `REFER`             | Campaigns or risk signals present; needs a human                 |
| `CLEAR`             | No campaigns found for this year/make/model                      |
| `INSUFFICIENT_DATA` | Recall data could not be retrieved — **not** the same as clear   |

Only NHTSA's own advisories produce a `BLOCK`. Our own judgements (component
family, campaign volume, crash ratings) can only `REFER`.

A `503` on any route means the local SQLite cache is unreadable or unwritable
— most often a file-permissions problem — and the response body says which.
A read-only cache is the confusing case: cached VINs keep answering `200` and
only cache *misses* fail, so the message calls that out explicitly.

Recall and rating outages do not fail the request. They return 200 with the
reason in `data_gaps` and the decision degraded to `INSUFFICIENT_DATA`.

### `GET /health`

`{"status": "ok", "cached_vins": 7, "cached_profiles": 3}`, which also acts as a
readiness probe since answering it requires the database to respond.

## Configuration

All settings are environment variables; see `.env.example`. Defaults are usable
as-is.

| Variable               | Default                                  | Purpose                                              |
| ---------------------- | ---------------------------------------- | ---------------------------------------------------- |
| `VIN_DB_PATH`          | `vin_cache.db`                           | SQLite file location                                 |
| `VPIC_BASE_URL`        | `https://vpic.nhtsa.dot.gov/api/vehicles` | Upstream base URL (point at a stub for offline work) |
| `VPIC_TIMEOUT_SECONDS` | `8`                                      | Per-attempt upstream timeout                         |
| `VPIC_MAX_RETRIES`     | `2`                                      | Retries after the first attempt, transient errors only |
| `CACHE_TTL_SECONDS`    | `0`                                      | Entry lifetime; `0` means never expire               |
| `STRICT_VIN_CHARSET`   | `false`                                  | Also reject `I`, `O`, `Q` (true of real VINs)        |
| `NHTSA_RECALLS_BASE_URL` | `https://api.nhtsa.gov/recalls`        | Recalls API base URL                                 |
| `NHTSA_RATINGS_BASE_URL` | `https://api.nhtsa.gov/SafetyRatings`  | Safety ratings API base URL                          |
| `PROFILE_TTL_SECONDS`  | `86400`                                  | Recall profile lifetime; recalls change, so this expires |
| `UW_RECALL_COUNT_REFER` | `5`                                     | Campaign count that triggers manual review           |
| `UW_MIN_NCAP_STARS`    | `2`                                      | NCAP stars at or below which to refer                |
| `UW_ROLLOVER_POSSIBILITY_REFER` | `0.30`                          | Rollover probability at or above which to refer      |
| `UW_RECENT_RECALL_DAYS` | `365`                                   | Window for the "recently announced" flag; `0` disables |

## Project layout

```
app/
  config.py        environment-driven settings
  schemas.py       request/response models, VIN validation and normalization
  db.py            SQLite cache: schema, get/upsert/delete/export reads
  upstream.py      retrying JSON fetch shared by both NHTSA integrations
  vpic.py          decode client: response parsing and error types
  nhtsa.py         recalls + safety ratings client, and their cache serialization
  mechanical.py    structural/mechanical projection of the stored decode
  risk_profile.py  claims-routing queue and energy-source classification
  underwriting.py  open-recall flag rules (read the module docstring first)
  service.py       cache-aside flows and per-key request coalescing
  export.py        parquet serialization with an explicit schema
  routes.py        HTTP layer only
  dependencies.py  injection seam used by the app and by tests
  main.py          app factory, lifespan, exception -> status code mapping
tests/             unit + integration tests, no network required
scripts/smoke.sh   end-to-end check against the real vPIC API
```

## Tests

```bash
make test     # or: python -m pytest
make lint     # ruff check + format check
```

43 tests, no network access required: the vPIC client is swapped out through
FastAPI's `dependency_overrides` for route tests, and driven with httpx's
`MockTransport` for its own unit tests. Coverage includes cache hit/miss,
VIN normalization, every malformed-input case, upstream outage and
undecodable-VIN handling, parquet round-trips, and concurrent request
coalescing.

For an end-to-end check against the live API, run the server and then:

```bash
./scripts/smoke.sh
```

See [NOTES.md](NOTES.md) for design decisions, tradeoffs, and what would change
under real traffic.

## Deployment

Deployed as a container to Heroku, released by GitHub Actions on every green
push to `main`.

### The pipeline

`.github/workflows/ci-cd.yml` is one workflow with two jobs:

```
push / PR ──► test ──► deploy  (push to main only)
              │          │
              │          ├─ build image
              │          ├─ push to registry.heroku.com
              │          ├─ release via the Platform API
              │          └─ poll /health until 200
              │
              └─ ruff check · ruff format --check · pytest
```

`deploy` declares `needs: test`, so a lint failure or a single failing test
stops the pipeline before anything is built or pushed. Pull requests run `test`
only; nothing reaches Heroku that has not already gone green on `main`.

Two details worth calling out:

- **The suite needs no network.** Both upstream clients are replaced through
  `dependency_overrides` and httpx's `MockTransport`, so NHTSA being down never
  blocks a deploy of an unrelated fix.
- **The pipeline smoke-tests the deployed app, not the registry.** A successful
  `docker push` says nothing about whether the container can serve traffic. The
  final step polls the public `/health`, which touches the database, so a
  container that starts but cannot answer fails the build.

### First-time setup

```bash
heroku create <app-name> --stack container
heroku config:set --app <app-name> CACHE_TTL_SECONDS=0 PROFILE_TTL_SECONDS=86400
```

Then add two repository secrets under **Settings -> Secrets and variables ->
Actions**:

| Secret | Value |
| ------ | ----- |
| `HEROKU_API_KEY` | `heroku authorizations:create --description "github actions"` |
| `HEROKU_APP_NAME` | the app name from above |

Push to `main` and the pipeline takes it from there.

### Deploying by hand

Useful for a first release or when debugging the image itself:

```bash
heroku container:login
heroku container:push web --app <app-name>
heroku container:release web --app <app-name>
heroku logs --tail --app <app-name>
```

### The known limitation

**Heroku's filesystem is ephemeral.** It is wiped on every deploy, on every
dyno restart, and at least once every 24 hours regardless. The SQLite cache does
not survive any of those.

This does not break the service. It keeps answering correctly, because a cache
miss just means a call to vPIC. What it does is silently reset the hit rate to
zero, which is the more dangerous kind of failure: nothing errors, nothing
alerts, and the only symptom is a latency and upstream-call-volume increase that
nobody is watching for. Add a second dyno and it gets worse, because each one
keeps its own separate cache and its own separate request-coalescing lock.

Two ways out, depending on what the deployment is for:

- **A host with persistent volumes.** Fly.io or Render will keep the SQLite file
  across restarts. That fixes durability on one node and nothing about scaling
  horizontally.
- **A shared cache.** Move `VinCache` onto Postgres or Redis. The cache-aside
  logic in `service.py` does not change; `VinCache` gets a second implementation
  behind the same methods. Keeping it behind a class was most of the reason for
  that shape in the first place. This is the real answer, and it is also what
  makes the request coalescing work across replicas rather than per-process.

Deployed as-is, on one dyno, with the reset documented, this is an honest demo
of the service. It is not what would carry production traffic.
