# Notes

## What this is

Three routes over a cache-aside pattern. `/lookup` reads SQLite, falls back to
vPIC on a miss, writes through. `/remove` evicts. `/export` serializes the
table to parquet. Everything else in the repo exists to make those three
things correct under bad input, a flaky upstream, and concurrent callers.

## Decisions worth defending

**Layering.** `routes.py` only does HTTP. `service.py` owns the cache-aside
policy. `db.py` and `vpic.py` own their integrations and expose plain Python
types. The payoff is that the cache logic is testable without an HTTP client
and the vPIC parsing is testable without a database. The cost is more files
than a 200-line single-module version would need, which for a challenge this
size is a real tradeoff and not obviously the right call.

**Plain `sqlite3`, no ORM.** The persistence surface is one table and four
statements. SQLAlchemy would add a dependency, a session lifecycle, and a
mapping layer without deleting any code that matters here.

**`DecodeVinValues`, not `DecodeVin`.** The former returns one flat object.
The latter returns ~140 `{Variable, Value}` rows that you have to pivot
yourself. Same data, more code, more to get wrong.

**Store the full upstream record.** The table keeps a `raw_json` column
alongside the four promised fields. If someone later wants fuel type or drive
type, that is a migration plus a backfill from local data rather than
re-fetching every VIN from NHTSA.

**Errors are split into "your problem" and "our problem."** A malformed or
undecodable VIN is `422` and will never succeed on retry. An upstream timeout
or 5xx is `502` and might. That distinction is the only thing a caller can
actually act on. Mapping happens in exception handlers in `main.py`, so no
route contains a `try/except`.

**vPIC always answers 200.** Failure is signalled in the body via `ErrorCode`.
Some codes (a check-digit mismatch, for instance) still come back with a usable
make and model, so the client's test is "did we get anything identifying?"
rather than "was `ErrorCode` exactly 0?" Partial decodes get cached and
returned; entirely empty ones raise.

**Request coalescing.** Ten simultaneous requests for the same cold VIN would
otherwise produce ten calls to NHTSA. A per-VIN `asyncio` lock (`SingleFlight`
in `service.py`) collapses them into one fetch and nine cache hits. It is
reference-counted so the lock registry does not grow unboundedly; a naive
`defaultdict(asyncio.Lock)` leaks one lock per VIN ever seen.

**Both verbs on every route.** The spec says "the request should contain a
single string called vin" without naming a method, so `/lookup` and `/remove`
each accept a query-parameter form and a JSON-body form that share one handler.

## Known weak spots

- **The cache is per-process.** SQLite on local disk plus an in-process lock
  means two replicas keep two caches and coalesce independently. Fine for one
  box, wrong the moment this is horizontally scaled.
- **`/export` buffers the whole table in memory.** At a few hundred bytes per
  VIN even a million rows is manageable, but there is no streaming and no
  pagination, and no auth on an endpoint that dumps the entire dataset.
- **One shared SQLite connection behind a mutex** serializes reads that WAL
  would otherwise allow to run concurrently. Every query here is a primary-key
  hit measured in microseconds, so it has not been worth a pool, but that is an
  assertion I have not load-tested.
- **No circuit breaker.** If vPIC is down, every cold request still pays two
  retries and up to three timeouts before failing.
- **`strict_vin_charset` defaults to off.** Real VINs never contain I, O or Q,
  but the spec says "17 alphanumeric," so spec compliance won and the stricter
  rule is a config flag. Reasonable people would flip that default.
- **No check-digit validation.** Position 9 of a VIN is a checksum that could
  be verified locally, rejecting typos before they cost a network call. It is
  about fifteen lines and I left it out to keep the validation story simple.
- **The recall flag is a screening signal, not a decision.** NHTSA's public API
  indexes recalls by year/make/model; VIN-level remedy status lives behind each
  manufacturer's own lookup. So `/underwrite` cannot tell a repaired car from an
  unrepaired one, and every assessment says so via `vin_level_verified: false`
  and a caveat string carried in the response and rendered above the flags in
  the UI. The rules are asymmetric on purpose: a repaired car may be sent to
  review, but an unrepaired one is never cleared. Wiring in a manufacturer VIN
  API is what would upgrade this from screening to decision.
- **Model names do not always match between NHTSA services.** vPIC decodes to
  marketing names ("Street Glide"), while the recalls index uses factory codes
  ("FLHX"). That mismatch answers HTTP 400, which is reported as
  `INSUFFICIENT_DATA` with the reason rather than being read as "no recalls" --
  the campaigns exist, we just cannot address them by name. Resolving the name
  against `/products/vehicle/models` would close most of this gap and is the
  most valuable next piece of work on this feature.
- **Storage failures are 503, but only the operational ones.** SQLite raises
  `OperationalError` for conditions outside the code -- a read-only file, a lock
  it could not take, an unreachable disk -- and those become a 503 naming the
  likely cause. Its siblings (`IntegrityError`, `ProgrammingError`) mean the
  statement itself is wrong, and a 503 inviting a retry would be a lie about a
  bug that will fail identically every time, so those keep the default 500.
  This came out of a real incident: the cache file ended up owned by another
  user, cached VINs answered 200 and only misses failed, and the bare
  "Internal Server Error" gave nothing to go on.
- **Claims routing trusts `VehicleType` completely.** It was populated on all
  17 vehicles in the development cache, including the motorcycle and the Class 8
  tractor, so the routing is a lookup rather than an inference. If vPIC ever
  returns it blank the vehicle goes to `UNCLASSIFIED` and a human, which is the
  right failure but means a silent upstream change would show up as a queue of
  manual triage rather than as an error.
- **Energy-source flags are handling notes, not risk scores.** They sit beside
  the underwriting decision rather than inside it, on the view that a BEV is not
  a worse risk than an ICE car but is a differently handled one. Someone could
  reasonably argue an insurer *does* want EV status priced, not just routed;
  that would be a scoring model, and it would need loss data this service does
  not have.
- **Safety-rating variant matching is heuristic.** NCAP lists one row per body
  style and the descriptions share little vocabulary with vPIC's `BodyClass`,
  so `_best_variant` scores a few tokens and falls back to the first row. Worst
  case it reports a sibling trim's stars.

## If this had to take real traffic

1. **Move the cache to Postgres or Redis** so replicas share it. The cache-aside
   logic in `service.py` does not change; `VinCache` gets a new implementation
   behind the same four methods. That was most of the reason to keep it behind
   a class.
2. **Add a circuit breaker** around the vPIC client so an upstream outage fails
   fast instead of holding connections open, and **serve stale on error**: an
   expired row beats a 502 when the data is effectively immutable anyway.
3. **Rate limit and authenticate**, particularly `/export`.
4. **Instrument it.** Cache hit rate, upstream latency percentiles, and 502 rate
   are the three numbers that would tell you whether this is healthy. Structured
   JSON logs with a request ID, and `/metrics` for Prometheus.
5. **Batch decoding.** vPIC has a `DecodeVINValuesBatch` endpoint that takes up
   to 50 VINs per call. Any bulk consumer should use it.
6. **Deploy** as the container in the `Dockerfile`, behind a load balancer, with
   the cache on a managed database rather than a volume. `/health` is already
   wired for liveness and readiness probes. CI would run `make lint` and
   `make test` on every push; neither needs network access.

## On the AI question

I used an assistant while building this, the way I would on a normal workday.
The structure, the error taxonomy, the coalescing decision, and the tradeoffs
listed above are choices I made and can defend. The weak spots above are the
ones I would want to talk through first.
