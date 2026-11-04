"""Local analytics store: one in-memory DuckDB database with a view per extracted table.

Thread safety: Streamlit can run two script threads at once (a click starts a new run while the previous one is
still drawing). A DuckDB connection refuses concurrent calls, so every thread gets its own cursor on the same
in-memory database (DuckDB's recommended pattern). `store.con` always returns the calling thread's cursor.
"""

from __future__ import annotations

import threading

import duckdb
import pandas as pd

from tabbot.data.hyper_reader import TableInfo


def qi(name: str) -> str:
    """Quote an identifier for DuckDB."""
    return '"' + name.replace('"', '""') + '"'


class Store:
    def __init__(self, tables: list[TableInfo]):
        self._root = duckdb.connect(":memory:")
        self._local = threading.local()
        self._cursors: list[duckdb.DuckDBPyConnection] = []
        self._lock = threading.Lock()
        self._columns: dict[str, list[tuple[str, str]]] = {}
        self.tables = {t.name: t for t in tables}
        self.views: dict[str, str] = {}
        for i, t in enumerate(tables):
            view = f"t{i}"
            path = t.parquet.replace("'", "''")
            self._root.execute(f"CREATE VIEW {view} AS SELECT * FROM read_parquet('{path}')")
            self.views[t.name] = view

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        """This thread's cursor (created on first use)."""
        cur = getattr(self._local, "cursor", None)
        if cur is None:
            with self._lock:
                cur = self._root.cursor()
                self._cursors.append(cur)
            self._local.cursor = cur
        return cur

    def view(self, table_name: str | None = None) -> str:
        if table_name is None:
            if len(self.views) != 1:
                raise ValueError(f"store has {len(self.views)} tables; name one")
            return next(iter(self.views.values()))
        return self.views[table_name]

    def columns(self, table_name: str | None = None) -> list[tuple[str, str]]:
        view = self.view(table_name)
        if view not in self._columns:
            rows = self.con.execute(f"DESCRIBE {view}").fetchall()
            self._columns[view] = [(r[0], r[1]) for r in rows]
        return self._columns[view]

    def df(self, sql: str, params: list | None = None) -> pd.DataFrame:
        return self.con.execute(sql, params or []).df()

    def scalar(self, sql: str, params: list | None = None):
        row = self.con.execute(sql, params or []).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        with self._lock:
            for c in self._cursors:
                try:
                    c.close()
                except duckdb.Error:
                    pass
            self._cursors.clear()
        self._root.close()
