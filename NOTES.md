# Notes

## What this is

Three routes over a cache-aside pattern. `/lookup` reads SQLite, falls back to
vPIC on a miss, writes through. `/remove` evicts. `/export` serializes the table
to parquet. Everything else exists to keep those three correct under bad input,
a flaky upstream, and concurrent callers.

Beyond the brief, `/underwrite` adds NHTSA recalls and NCAP ratings and turns
them into an open-recall flag. It is the part I would most want to talk through,
and it carries the sharpest caveat below.

## Three decisions

**Layering.** `routes.py` does HTTP, `service.py` owns the cache-aside policy,
`db.py` and `vpic.py` own their integrations. The cost is more files than a
200-line single module; the payoff is that cache logic tests without an HTTP
client and vPIC parsing tests without a database.

**Store the whole upstream record.** `raw_json` keeps all ~150 vPIC fields, not
just the four promised. Surfacing structural and mechanical detail later was a
projection over data already cached, not 100k re-fetches from NHTSA.

**Errors split into "your problem" and "our problem."** A malformed or
undecodable VIN is 422 and will never succeed on retry; an upstream timeout is
502 and might. That distinction is the only thing a caller can act on. Mapping
lives in exception handlers, so no route contains a try/except.

## Known weak spots

- **The recall flag is a screening signal, not a decision.** NHTSA indexes
  recalls by year/make/model; VIN-level remedy status sits behind each
  manufacturer's own lookup, so `/underwrite` cannot tell a repaired car from an
  unrepaired one. Every assessment says so (`vin_level_verified: false`), and
  the rules are asymmetric on purpose: a repaired car may be sent to review, an
  unrepaired one is never cleared.
- **Model names differ between NHTSA services.** vPIC decodes marketing names
  ("Street Glide"); the recalls index files factory codes ("FLHX"). That
  mismatch answers HTTP 400, reported as `INSUFFICIENT_DATA` with the reason
  rather than read as "no recalls". Resolving names against
  `/products/vehicle/models` is the most valuable next work on this feature.
- **The cache is per-process.** SQLite on local disk plus an in-process lock
  means two replicas keep two caches and coalesce independently. Fine on one
  box, wrong the moment it scales horizontally.
- **Settings injection is partial.** `create_app` takes a `Settings`, but
  validation, the TTLs and the underwriting thresholds read the module
  singleton, so passing a non-default value for those silently does nothing.
  Correct in production, where the environment is read once at startup -- and a
  trap for whoever writes the next test.
- **`/export` has no auth and no streaming.** It buffers the whole table in
  memory and hands the entire dataset to anyone who asks.
- **No check-digit validation.** Position 9 of a VIN is a checksum that would
  reject typos before they cost a network call. About fifteen lines, left out to
  keep the validation story simple.

## If this had to take real traffic

Move the cache behind Postgres or Redis so replicas share it -- `VinCache` gets
a second implementation behind the same methods, which was most of the reason to
keep it a class. Add a circuit breaker around vPIC and serve stale on error, as
an expired row beats a 502 for data this immutable. Rate limit and authenticate,
`/export` first. Instrument cache hit rate, upstream latency percentiles and 502
rate. Use vPIC's batch endpoint for any bulk consumer.

## On the AI question

I used an assistant while building this, the way I would on a normal workday.
The structure, the error taxonomy, the coalescing decision and the tradeoffs
above are choices I made and can defend. The weak spots are what I would want to
talk through first.
