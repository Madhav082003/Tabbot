"""Read .hyper extracts with Tableau's Hyper API and copy every table to Parquet.

Telemetry is always off. Hyper's own log files go to the workspace log folder.
"""

from __future__ import annotations

import re
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel
from tableauhyperapi import Connection, HyperProcess, Telemetry

CHUNK = 100_000


class TableInfo(BaseModel):
    name: str                  # '"Extract"."Extract"'  (schema.table, quoted as Hyper prints it)
    schema_name: str
    table: str
    source_file: str           # basename of the .hyper
    parquet: str               # absolute path of the Parquet copy
    rows: int
    columns: list[tuple[str, str]]   # (column name, Hyper SQL type)


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)


def _copy_fallback(con: Connection, table_sql: str, cols: list[str], out: Path) -> None:
    """Row-by-row export, used only if this Hyper version cannot COPY TO Parquet."""
    writer = None
    with con.execute_query(f"SELECT * FROM {table_sql}") as result:
        batch: list[list] = []

        def flush() -> None:
            nonlocal writer, batch
            if not batch:
                return
            data = {c: [_py(r[i]) for r in batch] for i, c in enumerate(cols)}
            t = pa.table(data)
            if writer is None:
                writer = pq.ParquetWriter(out, t.schema)
            writer.write_table(t.cast(writer.schema))
            batch = []

        for row in result:
            batch.append(list(row))
            if len(batch) >= CHUNK:
                flush()
        flush()
    if writer is not None:
        writer.close()


def _py(v):
    for attr in ("to_datetime", "to_date"):
        if hasattr(v, attr):
            return getattr(v, attr)()
    return v


def export_to_parquet(hyper_files: list[Path], out_dir: Path, log_dir: Path) -> list[TableInfo]:
    out_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    tables: list[TableInfo] = []
    with HyperProcess(Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": str(log_dir)}) as hp:
        for hf in hyper_files:
            with Connection(hp.endpoint, str(hf)) as con:
                for schema in con.catalog.get_schema_names():
                    for tname in con.catalog.get_table_names(schema):
                        definition = con.catalog.get_table_definition(tname)
                        cols = [(c.name.unescaped, str(c.type)) for c in definition.columns]
                        table_sql = str(tname)  # fully quoted, e.g. "Extract"."Extract"
                        schema_u = tname.schema_name.name.unescaped if tname.schema_name else ""
                        table_u = tname.name.unescaped
                        stem = _safe(f"{Path(hf).stem}.{schema_u}.{table_u}")
                        out = out_dir / f"{stem}.parquet"
                        try:
                            target = str(out.resolve()).replace("\\", "/").replace("'", "''")
                            con.execute_command(f"COPY {table_sql} TO '{target}' WITH (FORMAT PARQUET)")
                        except Exception:  # noqa: BLE001 - older Hyper versions
                            _copy_fallback(con, table_sql, [c for c, _ in cols], out)
                        rows = con.execute_scalar_query(f"SELECT COUNT(*) FROM {table_sql}")
                        name = f'"{schema_u}"."{table_u}"' if schema_u else f'"{table_u}"'
                        tables.append(TableInfo(name=name, schema_name=schema_u, table=table_u,
                                                source_file=Path(hf).name, parquet=str(out.resolve()),
                                                rows=int(rows), columns=cols))
    return tables
