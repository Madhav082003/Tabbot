"""Local analytics store: one in-memory DuckDB connection with a view per extracted table."""

from __future__ import annotations

import duckdb
import pandas as pd

from tabbot.data.hyper_reader import TableInfo


def qi(name: str) -> str:
    """Quote an identifier for DuckDB."""
    return '"' + name.replace('"', '""') + '"'


class Store:
    def __init__(self, tables: list[TableInfo]):
        self.con = duckdb.connect(":memory:")
        self.tables = {t.name: t for t in tables}
        self.views: dict[str, str] = {}
        for i, t in enumerate(tables):
            view = f"t{i}"
            path = t.parquet.replace("'", "''")
            self.con.execute(f"CREATE VIEW {view} AS SELECT * FROM read_parquet('{path}')")
            self.views[t.name] = view

    def view(self, table_name: str | None = None) -> str:
        if table_name is None:
            if len(self.views) != 1:
                raise ValueError(f"store has {len(self.views)} tables; name one")
            return next(iter(self.views.values()))
        return self.views[table_name]

    def columns(self, table_name: str | None = None) -> list[tuple[str, str]]:
        rows = self.con.execute(f"DESCRIBE {self.view(table_name)}").fetchall()
        return [(r[0], r[1]) for r in rows]

    def df(self, sql: str, params: list | None = None) -> pd.DataFrame:
        return self.con.execute(sql, params or []).df()

    def scalar(self, sql: str, params: list | None = None):
        row = self.con.execute(sql, params or []).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        self.con.close()
