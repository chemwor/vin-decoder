"""Parquet export of the cache table.

The schema is declared explicitly rather than inferred from the rows. Inference
would produce a different parquet schema for an empty cache (all-null columns)
than for a populated one, which quietly breaks any consumer reading a directory
of these files.
"""

from __future__ import annotations

import io

import pyarrow as pa
import pyarrow.parquet as pq

from .db import EXPORT_COLUMNS, VinCache

PARQUET_SCHEMA = pa.schema(
    [
        ("vin", pa.string()),
        ("make", pa.string()),
        ("model", pa.string()),
        ("model_year", pa.string()),
        ("body_class", pa.string()),
        ("fetched_at", pa.string()),
    ]
)

# Sanity check that the two definitions cannot drift apart unnoticed.
assert tuple(PARQUET_SCHEMA.names) == EXPORT_COLUMNS


def build_parquet(cache: VinCache) -> bytes:
    """Serialize the whole cache to an in-memory parquet file.

    In-memory is fine at this size: a few hundred bytes per VIN means even a
    million cached VINs is well under a gigabyte, and this is a cache, not a
    warehouse. NOTES.md covers what to do if that stops being true.
    """
    rows = cache.all_rows()
    columns = {name: [row.get(name) for row in rows] for name in EXPORT_COLUMNS}
    table = pa.Table.from_pydict(columns, schema=PARQUET_SCHEMA)

    buffer = io.BytesIO()
    pq.write_table(table, buffer, compression="snappy")
    return buffer.getvalue()
