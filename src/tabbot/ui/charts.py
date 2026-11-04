"""Turn a RebuildResult into a Tableau-looking Plotly figure (or a table when a chart would mislead)."""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from tabbot.views.display import fmt_dim, join_dims, pivot, primary_measure
from tabbot.views.rebuild import RebuildResult

TABLEAU_10 = ["#4E79A7", "#F28E2B", "#E15759", "#76B7B2", "#59A14F", "#EDC948", "#B07AA1", "#FF9DA7",
              "#9C755F", "#BAB0AC"]
TABLEAU_BLUE = [[0.0, "#EEF3F9"], [0.25, "#C5D8EC"], [0.5, "#86AED6"], [0.75, "#4E79A7"], [1.0, "#1F4E79"]]
FONT = "Segoe UI, Benton Sans, Arial, sans-serif"
MAX_HEATMAP_ROWS = 60
MAX_FACETS = 24


def _style(fig: go.Figure, height: int) -> go.Figure:
    fig.update_layout(
        template="simple_white", height=height, font=dict(family=FONT, size=11, color="#333"),
        margin=dict(l=10, r=10, t=10, b=10), colorway=TABLEAU_10, paper_bgcolor="white", plot_bgcolor="white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, title_text="",
                    font=dict(size=10)),
        hoverlabel=dict(font_family=FONT),
    )
    fig.update_xaxes(showgrid=False, linecolor="#C8C8C8", ticks="", title_font=dict(size=11))
    fig.update_yaxes(showgrid=True, gridcolor="#EDEDED", linecolor="#C8C8C8", ticks="", title_font=dict(size=11))
    return fig


def figure_for(res: RebuildResult, height: int = 360) -> tuple[go.Figure | None, pd.DataFrame | None, list[str]]:
    """Return (figure, table, notes). Exactly one of figure/table is set when there is data."""
    notes: list[str] = []
    df = res.data
    if res.status == "failed" or df.empty or res.chart == "none":
        return None, None, notes
    kind = res.chart

    if kind in ("heatmap", "table"):
        pv, m, n = pivot(res)
        notes += n
        if kind == "table" or pv.empty:
            return None, pv, notes
        if len(pv) > MAX_HEATMAP_ROWS:
            notes.append(f"showing the {MAX_HEATMAP_ROWS} largest of {len(pv)} rows")
            pv = pv.loc[pv.sum(axis=1).sort_values(ascending=False).index[:MAX_HEATMAP_ROWS]]
        z = pv.to_numpy()
        text = [["" if pd.isna(v) else f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}" for v in row]
                for row in z]
        fig = go.Figure(go.Heatmap(
            z=z, x=list(pv.columns), y=list(pv.index), text=text, texttemplate="%{text}",
            textfont=dict(size=10), colorscale=TABLEAU_BLUE, xgap=1, ygap=1, showscale=False,
            hovertemplate="%{y}<br>%{x}<br>" + m.label + ": %{z:,}<extra></extra>"))
        fig.update_yaxes(autorange="reversed", showgrid=False, tickfont=dict(size=10))
        fig.update_xaxes(side="top", tickfont=dict(size=10), type="category")
        h = max(height, min(28 * len(pv) + 60, 1400))
        return _style(fig, h), None, notes

    if kind in ("line", "area"):
        shelf_m = res.measures_by_role("rows") or res.measures_by_role("cols")
        measures = shelf_m or res.measures[:1]
        axis_dims = res.dims_by_role("cols", "rows")
        x_dim = next((d for d in axis_dims if d.kind == "date_trunc"), None) or \
            next((d for d in reversed(axis_dims) if d.kind == "date_part"), None) or \
            (axis_dims[-1] if axis_dims else None)
        if x_dim is None:
            return None, pivot(res)[0], notes
        color_dims = res.dims_by_role("color")
        facet_dims = [d for d in res.dims if d is not x_dim and d not in color_dims]
        plot = pd.DataFrame(index=df.index)
        if x_dim.kind == "date_trunc":
            plot["x"] = fmt_dim(df[x_dim.alias], x_dim, for_axis=True)
        else:
            others_x = [d for d in axis_dims if d.kind == "date_part" and d is not x_dim]
            plot["x"] = join_dims(df, others_x + [x_dim], " ")
            facet_dims = [d for d in facet_dims if d not in others_x]
        plot["series"] = join_dims(df, color_dims, " | ") if color_dims else None
        plot["facet"] = join_dims(df, facet_dims, " | ") if facet_dims else None
        long = []
        for m in measures:
            part = plot.copy()
            part["value"] = pd.to_numeric(df[m.alias], errors="coerce")
            part["measure"] = m.label
            long.append(part)
        plot = pd.concat(long)
        color = "series" if color_dims else ("measure" if len(measures) > 1 else None)
        facet = "facet" if facet_dims else None
        if facet and plot["facet"].nunique() > MAX_FACETS:
            notes.append(f"{plot['facet'].nunique()} panels is too many to draw; showing the table instead")
            return None, pivot(res)[0], notes
        fn = px.area if kind == "area" else px.line
        fig = fn(plot.sort_values("x"), x="x", y="value", color=color, facet_col=facet,
                 facet_col_wrap=4 if facet else 0, markers=kind == "line",
                 labels={"x": x_dim.label, "value": measures[0].label if len(measures) == 1 else "Value"},
                 color_discrete_sequence=TABLEAU_10)
        if any("text" in m.roles or "label" in m.roles for m in measures) and not facet:
            fig.update_traces(text=plot.sort_values("x")["value"], textposition="top center",
                              mode="lines+markers+text", textfont=dict(size=9))
        fig.update_traces(line=dict(width=2), marker=dict(size=4))
        fig.for_each_annotation(lambda a: a.update(text=a.text.split("=")[-1]))
        return _style(fig, height if not facet else max(height, 220 * ((plot["facet"].nunique() + 3) // 4))), \
            None, notes

    if kind == "bar":
        m_cols, m_rows = res.measures_by_role("cols"), res.measures_by_role("rows")
        horizontal = bool(m_cols) and not m_rows
        m = (m_cols or m_rows or res.measures)[0]
        cat_dims = res.dims_by_role("rows" if horizontal else "cols") or [d for d in res.dims if d.role != "color"]
        color_dims = res.dims_by_role("color")
        plot = pd.DataFrame({"cat": join_dims(df, cat_dims, " | "),
                             "value": pd.to_numeric(df[m.alias], errors="coerce")})
        plot["series"] = join_dims(df, color_dims, " | ") if color_dims else None
        kw = dict(color="series") if color_dims else {}
        show_text = any(r in m.roles for r in ("text", "label")) or len(plot) <= 30
        fig = px.bar(plot, x="value" if horizontal else "cat", y="cat" if horizontal else "value",
                     orientation="h" if horizontal else "v", text="value" if show_text else None,
                     labels={"value": m.label, "cat": " | ".join(d.label for d in cat_dims)},
                     color_discrete_sequence=TABLEAU_10, **kw)
        if show_text:
            fig.update_traces(texttemplate="%{text:,}", textposition="outside", cliponaxis=False)
        if not color_dims:
            fig.update_traces(marker_color=TABLEAU_10[0])
        if horizontal:
            fig.update_yaxes(autorange="reversed", showgrid=False)
            fig.update_xaxes(showgrid=True, gridcolor="#EDEDED")
        return _style(fig, max(height, 26 * plot["cat"].nunique() + 80) if horizontal else height), None, notes

    if kind == "pie":
        m = primary_measure(res, "size", "text", "label")
        color_dims = res.dims_by_role("color") or res.dims[:1]
        facet_dims = [d for d in res.dims if d not in color_dims]
        plot = pd.DataFrame({"slice": join_dims(df, color_dims, " | "),
                             "value": pd.to_numeric(df[m.alias], errors="coerce")})
        plot["facet"] = join_dims(df, facet_dims, " | ") if facet_dims else None
        if facet_dims and plot["facet"].nunique() > MAX_FACETS:
            notes.append("too many pie panels to draw; showing the table instead")
            return None, pivot(res)[0], notes
        fig = px.pie(plot, names="slice", values="value", facet_col="facet" if facet_dims else None,
                     facet_col_wrap=4 if facet_dims else 0, color_discrete_sequence=TABLEAU_10)
        fig.update_traces(textinfo="value+label" if len(plot) <= 8 else "value", textfont=dict(size=10),
                          sort=False, marker=dict(line=dict(color="white", width=1)))
        fig.for_each_annotation(lambda a: a.update(text=a.text.split("=")[-1]))
        fig = _style(fig, height)
        fig.update_layout(showlegend=True)
        return fig, None, notes

    if kind == "scatter":
        mx, my = res.measures_by_role("cols"), res.measures_by_role("rows")
        if not (mx and my):
            return None, pivot(res)[0], notes
        plot = pd.DataFrame({"x": pd.to_numeric(df[mx[0].alias], errors="coerce"),
                             "y": pd.to_numeric(df[my[0].alias], errors="coerce"),
                             "label": join_dims(df, res.dims, " | ")})
        fig = px.scatter(plot, x="x", y="y", hover_name="label", labels={"x": mx[0].label, "y": my[0].label},
                         color_discrete_sequence=TABLEAU_10)
        return _style(fig, height), None, notes

    return None, pivot(res)[0], notes
