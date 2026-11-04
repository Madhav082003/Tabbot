# Prototype scripts (reference only)

These scripts were run by hand on one real Tableau workbook during a manual analysis. They worked, and they show
real, tested usage of:

- `xml.etree.ElementTree` on a `.twb` (03, 05, 06, 10, 16, 18): element paths, filters, shelves, sizes, thumbnails;
- `tableauhyperapi` (09, 11, 12, 15): `HyperProcess`, `Connection`, `catalog`, `execute_list_query`, SQL profiling;
- pandas for the dashboard rebuild and analyses (11, 12, 15): distinct counts, quarter periods, window comparisons,
  crosstabs, period bins.

Do **not** copy them as they are:

- They hard-code one workbook's names (sheet names, a list of 27 MedDRA SOCs, column names such as `CASEID`).
- They use plain `xml.etree`; the app must use `defusedxml`.
- `09_hyper_schema.py` has a hard-coded log folder from the original session.
- They print values to stdout; the app must never log data values.

Use them to see how the APIs behave, then write generic code per `docs/SPEC.md`.
