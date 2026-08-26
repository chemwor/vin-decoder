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
  static/
    index.html     single-file demo client, served at / (no build step)
tests/             unit + integration tests, no network required
scripts/smoke.sh   end-to-end check against the real vPIC API
fly.toml           Fly.io machine, volume and health-check configuration
.github/workflows/
  build.yml        lint -> test -> image -> container smoke test -> push
  deploy.yml       manual promotion of one build number to Fly
```

## Tests

```bash
make test     # or: python -m pytest
make lint     # ruff check + format check
```

115 tests, no network access required: both upstream clients are swapped out
through FastAPI's `dependency_overrides` for route tests, and driven with
httpx's `MockTransport` for their own unit tests.

Coverage includes cache hit/miss, VIN normalization, every malformed-input
case, upstream outage and undecodable-VIN handling, parquet round-trips, and
concurrent request coalescing — plus, for the enrichment layer:

- the underwriting rules, including the one that matters most: a failed recall
  lookup must never read as a clean vehicle
- recall parsing, day-first dates, and the two-step safety-ratings lookup
- profile caching by year/make/model, and the fact that a failed fetch is not
  cached at all
- claims routing and energy-source classification, with cases named after the
  real vehicles they came from
- storage failures surfacing as a 503 that names the likely cause

For an end-to-end check against the live API, run the server and then:

```bash
./scripts/smoke.sh
```

See [NOTES.md](NOTES.md) for design decisions, tradeoffs, and what would change
under real traffic.

## Deployment

Containerised and deployed to [Fly.io](https://fly.io). GitHub Actions builds
and verifies the image; a human decides when it is released.

### The pipeline

```
push to main ──► Build ──► lint ──► tests ──► docker build ──► container smoke test ──► push
                                                                                         │
                                                                        registry.fly.io/vin-decoder
                                                                              :build-<run number>
                                                                                         │
                          Deploy (manual) ──► verify the build passed ──► fly deploy --image
                                                                                         │
                                                                            smoke-test public /health
```

Two workflows rather than one, because building and releasing are different
decisions. The first answers "is this image good?", the second "is now the time,
and is this the version?". Splitting them is also what makes rollback ordinary:
it is the deploy workflow again with an older build number.

### Build — `.github/workflows/build.yml`

Runs on every push to `main`, every pull request, and on demand.

The gate is one line: the `build` job declares `needs: test`. A lint violation
or a single failing test means no image is produced at all, so there is no path
to a deployable artefact that skips verification.

The image is built once, loaded into the local Docker daemon, smoke-tested, and
only then pushed. Building a second time for the push would leave a gap where
the tested image and the released image are merely *believed* to be the same.

Two details worth calling out:

- **The suite needs no network.** Both upstream clients are replaced through
  `dependency_overrides` and httpx's `MockTransport`, so NHTSA being down never
  blocks a build of an unrelated fix. A red build is always a real failure.
- **The container is smoke-tested, not just built.** A successful `docker build`
  says nothing about whether the thing can serve traffic. The step starts the
  container and checks that `/health` answers, that the data directory is
  genuinely writable, that a malformed VIN still gets a 422, and that the UI and
  `/docs` serve. Unit tests cannot catch a broken `CMD` or a user that cannot
  write its own cache — and a read-only cache is the nastiest version of that,
  because `/health` passes and only cache *misses* fail.

Pull requests are linted, tested and smoke-tested but push no image: verified is
not the same as reviewed.

**The build number is `github.run_number`.** The image is tagged `build-<n>`,
and again as `sha-<short-sha>` for traceability. The number appears in the run
summary; that is what the deploy workflow takes as input.

### Deploy — `.github/workflows/deploy.yml`

Triggered by hand (`workflow_dispatch`) with a build number.

It builds nothing. It resolves that number to the image the build produced and
releases those exact bytes, so what reaches production is what passed the tests
— not a rebuild that might pick up a different base layer or a dependency that
moved underneath it.

Before deploying, it asks the GitHub API whether the Build run bearing that
number actually concluded `success`. An image tag proves an image exists, not
that it earned its place.

Releases use Fly's rolling strategy, so health checks gate each machine
replacement and a container that will not start never takes the app down with
it. Afterwards the workflow polls the public `/health` — which covers the proxy,
TLS and routing, none of which the in-CI container test can see.

The `production` GitHub Environment is attached so protection rules (required
reviewers, a wait timer) can be switched on later without editing the workflow.

### First-time setup

```bash
fly auth login
fly apps create vin-decoder                          # must match `app` in fly.toml
fly volumes create vin_cache --region iad --size 1   # persists the SQLite cache
fly tokens create deploy -x 8760h                    # value for FLY_API_TOKEN
```

Then create a `production` environment under **Settings -> Environments** and
add the token to it:

| Secret / variable | Scope | Purpose |
| ----------------- | ----- | ------- |
| `FLY_API_TOKEN` | `production` environment | Push to `registry.fly.io`, and deploy |
| `FLY_APP_URL` | repository variable (optional) | Public hostname for the release smoke test, if not `vin-decoder.fly.dev` |

Push to `main` to get a build number, then run **Deploy** with it.

### Deploying by hand

Useful for a first release, or when debugging the image itself:

```bash
fly deploy                                              # builds locally, releases
fly deploy --image registry.fly.io/vin-decoder:build-42 # promote a CI build
fly logs --app vin-decoder
fly status --app vin-decoder
```

The first form bypasses everything the pipeline guarantees — it builds from the
working tree, tests included or not. Fine for debugging, wrong for a release.

### The known limitation

**A Fly volume is attached to one machine.** With the volume mounted at `/data`
the SQLite cache survives restarts and deploys, which is the durability problem
solved. Scaling is not.

Scale past a single machine and each one gets its own volume, so you have N
independent caches: N times the NHTSA traffic, no shared invalidation, and a
`/remove` that evicts from whichever machine happened to serve it and no other.
The request coalescing in `service.py` has the same shape of problem — the
`asyncio.Lock` is per-process, so it dedupes within a machine and not across
them.

Nothing about that errors. The service keeps answering correctly, because a
cache miss is only a call to vPIC. It just quietly costs more and gets slower,
which is the more dangerous kind of failure: nothing alerts, and the symptom is
a latency and upstream-call-volume increase nobody is watching for.

The real fix is a shared cache — move `VinCache` onto Postgres or Redis. The
cache-aside logic in `service.py` does not change; `VinCache` gains a second
implementation behind the same methods, which was most of the reason for
keeping it behind a class. That is also what makes coalescing work across
machines rather than per-process.

Deployed as-is on a single machine with a volume, this is an honest small
service. It is not yet what would carry production traffic, and the paragraph
above is what changes first.
