# VIN Decoder

A FastAPI service that decodes 17-character VINs using the [NHTSA vPIC API](https://vpic.nhtsa.dot.gov/api/),
with a SQLite cache in front of it so the same VIN is only ever fetched once.

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

The service is now on `http://127.0.0.1:8000`, with interactive docs at
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

### `GET /health`

`{"status": "ok", "cached_vins": 7}`, which also acts as a readiness probe since
answering it requires the database to respond.

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

## Project layout

```
app/
  config.py        environment-driven settings
  schemas.py       request/response models, VIN validation and normalization
  db.py            SQLite cache: schema, get/upsert/delete/export reads
  vpic.py          NHTSA client: retries, backoff, response parsing, error types
  service.py       cache-aside flow and per-VIN request coalescing
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
