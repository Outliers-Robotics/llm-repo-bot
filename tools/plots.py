"""Per-mention chart artifacts rendered locally from verified repository reads or data series."""

from dataclasses import dataclass
from io import BytesIO
import math
from threading import Lock
import textwrap

from cpp_tables import TableError, extract_table


MAX_CHARTS = 3
_RENDER_LOCK = Lock()  # Matplotlib has process-wide font/rendering state.


@dataclass(frozen=True)
class ChartArtifact:
    filename: str
    title: str
    png: bytes
    alt_text: str


def _label(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 120:
        raise TableError("Chart titles, axis labels, and series labels must contain 1–120 characters.")
    return " ".join(value.split())


def render_chart(title, x_label, y_label, series) -> bytes:
    # No pyplot, display server, model-generated programs, or external chart service.
    with _RENDER_LOCK:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure

        figure = Figure(figsize=(10, 6.5), dpi=160, facecolor="#ffffff")
        FigureCanvasAgg(figure)
        axes = figure.add_subplot()
        figure.subplots_adjust(left=0.12, right=0.96, top=0.84, bottom=0.26)
        colors = ("#1764ab", "#cc651c", "#168578", "#9745a5", "#c13d55", "#656d24")
        for index, item in enumerate(series):
            x, y = zip(*item["points"])
            axes.plot(x, y, marker="o", markersize=4, linewidth=2,
                      color=colors[index % len(colors)], label=item["label"])
        axes.set_title(textwrap.fill(title, 65), fontsize=17, weight="bold", pad=18, parse_math=False)
        axes.set_xlabel(textwrap.fill(x_label, 65), fontsize=11, labelpad=10, parse_math=False)
        axes.set_ylabel(textwrap.fill(y_label, 45), fontsize=11, labelpad=10, parse_math=False)
        axes.ticklabel_format(style="plain", useOffset=False)
        axes.grid(alpha=0.2)
        axes.spines[["top", "right"]].set_visible(False)
        axes.legend(fontsize=9, loc="best")
        notes = []
        has_tables = any("table" in item and "path" in item for item in series)
        if has_tables:
            notes.append("Points are source table entries; connecting lines are visual guides.")
            notes.extend(f"{item['label']}: {item['table']} — {item['path']}" for item in series if "table" in item and "path" in item)
        else:
            notes.append("Connecting lines are visual guides.")
            notes.extend(f"{item['label']}: {item['source']}" for item in series if item.get("source"))
        footer = "\n".join(textwrap.fill(note, 115) for note in notes)
        if footer:
            figure.text(0.12, 0.025, footer, fontsize=8, color="#4b5563", va="bottom", parse_math=False)
        output = BytesIO()
        try:
            figure.savefig(output, format="png", bbox_inches="tight", facecolor="white")
            return output.getvalue()
        finally:
            figure.clear()


class PlotSession:
    def __init__(self, reader):
        self._reader = reader
        self._sources = {}
        self._lock = Lock()
        self.artifacts: list[ChartArtifact] = []

    def read_file(self, path: str) -> dict:
        """Read a repository file and make its source available for plotting.

        Args:
            path: Exact repository-relative file path from search results.
        """
        result = self._reader(path)
        with self._lock:
            self._sources[path] = dict(result)
        return result

    def plot_lookup_tables(
        self, title: str, x_label: str, y_label: str,
        paths: list[str] | str | None = None,
        tables: list[str] | str | None = None,
        labels: list[str] | str | None = None,
        series: list[dict] | dict | None = None,
    ) -> dict:
        """Create a PNG graph from named C++ {x, y} lookup tables already read.

        Args:
            title: Short descriptive chart title.
            x_label: X axis meaning and verified unit, such as Distance (m).
            y_label: Y axis meaning and verified unit, such as Flywheel speed (RPM).
            paths: Source path for each series; each must have been read_file'd first.
            tables: Exact variable names of brace-initialized pair tables, one per path.
            labels: Legend names, one per table. Compare only tables with the same units.
            series: (Optional) Direct data series if plotting arbitrary data points instead of lookup tables.

        Returns:
            Chart attachment details and resolved points, or an actionable error.
            Numeric values come from source, including basic arithmetic and local
            scalar offsets. Function calls, units wrappers, and runtime data are unsupported.
            The image is queued for Slack delivery; do not invent an image URL.
        """
        if series is not None and (not paths or not tables):
            return self.plot_data(title=title, x_label=x_label, y_label=y_label, series=series)

        with self._lock:
            try:
                if len(self.artifacts) >= MAX_CHARTS:
                    raise TableError(f"At most {MAX_CHARTS} graphs can be attached to one answer.")
                title, x_label, y_label = map(_label, (title, x_label, y_label))
                if isinstance(paths, str):
                    paths = [paths]
                if isinstance(tables, str):
                    tables = [tables]
                if isinstance(labels, str):
                    labels = [labels]
                if not (
                    isinstance(paths, (list, tuple))
                    and isinstance(tables, (list, tuple))
                    and isinstance(labels, (list, tuple))
                ):
                    raise TableError("Provide 1–6 series with one path, table, and label each.")
                if not (1 <= len(paths) <= 6 and len(paths) == len(tables) == len(labels)):
                    raise TableError("Provide 1–6 series with one path, table, and label each.")
                series_data = []
                for path, table, label in zip(paths, tables, labels):
                    source = self._sources.get(path)
                    if source is None:
                        raise TableError(f"Read {path} before requesting its graph.")
                    if source.get("truncated"):
                        raise TableError(f"{path} was truncated; a complete source file is required.")
                    series_data.append({
                        "path": path, "table": table, "label": _label(label),
                        "url": source.get("url"),
                        "points": extract_table(source["content"], table),
                    })
                png = render_chart(title, x_label, y_label, series_data)
                filename = f"lookup-tables-{len(self.artifacts) + 1}.png"
                alt_text = f"{title}. {x_label} versus {y_label}. " + "; ".join(
                    f"{item['label']}: {len(item['points'])} points from {item['table']}" for item in series_data
                )
                self.artifacts.append(ChartArtifact(filename, title, png, alt_text))
                return {"status": "graph_ready", "filename": filename, "series": series_data,
                        "note": "The bot will attach this graph to the answer thread. Cite the sources in your answer."}
            except TableError as error:
                return {"error": str(error), "note": "Explain this limitation; do not invent data or claim a graph was created."}

    def plot_data(
        self,
        title: str,
        x_label: str,
        y_label: str,
        series: list[dict] | dict | None = None,
        paths: list[str] | str | None = None,
        tables: list[str] | str | None = None,
        labels: list[str] | str | None = None,
        points: list[list[float]] | None = None,
    ) -> dict:
        """Create a PNG graph from arbitrary data series, mathematical equations, or calculated points.

        Args:
            title: Short descriptive chart title.
            x_label: X axis meaning and verified unit, such as Distance (m) or Time (s).
            y_label: Y axis meaning and verified unit, such as Flywheel speed (RPM) or Error.
            series: 1–6 data series to plot. Each series is a dictionary with:
                - 'label': Name of the curve for the legend.
                - 'points': List of [x, y] coordinates, such as [[1.0, 1200], [2.0, 1350], ...].
                            Alternatively, 'x': [x1, x2, ...] and 'y': [y1, y2, ...].
                - 'source' (optional): Source file, function, or equation description (e.g. 'ShotCalculator.cpp' or 'y = 2x + 1').
            paths: (Optional) Source file paths if delegating to lookup tables.
            tables: (Optional) Table variable names if delegating to lookup tables.
            labels: (Optional) Legend names if delegating to lookup tables.
            points: (Optional) Direct [x, y] points if plotting a single series without the series list.

        Returns:
            Chart attachment details, or an actionable error.
            The image is queued for Slack delivery; do not invent an image URL.
        """
        if paths and tables and not series and not points:
            return self.plot_lookup_tables(
                title=title, x_label=x_label, y_label=y_label,
                paths=paths, tables=tables, labels=labels or [title],
            )

        with self._lock:
            try:
                if len(self.artifacts) >= MAX_CHARTS:
                    raise TableError(f"At most {MAX_CHARTS} graphs can be attached to one answer.")
                title, x_label, y_label = map(_label, (title, x_label, y_label))

                if series is None:
                    if points is not None:
                        label_name = labels if isinstance(labels, str) else (labels[0] if labels else title)
                        series = [{"label": label_name, "points": points}]
                    else:
                        raise TableError("Provide at least one data series with 'points' or 'x' and 'y' values.")

                if isinstance(series, dict):
                    series = [series]

                if not isinstance(series, (list, tuple)) or not (1 <= len(series) <= 6):
                    raise TableError("Provide 1–6 data series to plot.")

                resolved_series = []
                for index, s in enumerate(series):
                    if not isinstance(s, dict):
                        raise TableError(f"Series #{index + 1} must be a dictionary.")
                    label = _label(s.get("label") or f"Series {index + 1}")
                    source_desc = s.get("source") or s.get("path") or ""

                    raw_points = s.get("points")
                    if raw_points is None and "x" in s and "y" in s:
                        xs = s["x"]
                        ys = s["y"]
                        if not isinstance(xs, (list, tuple)) or not isinstance(ys, (list, tuple)):
                            raise TableError(f"Series '{label}': 'x' and 'y' must be lists of numbers.")
                        if len(xs) != len(ys):
                            raise TableError(f"Series '{label}': 'x' and 'y' lists must have the same length.")
                        raw_points = list(zip(xs, ys))

                    if not isinstance(raw_points, (list, tuple)):
                        raise TableError(f"Series '{label}' must provide a list of [x, y] points.")

                    if not (2 <= len(raw_points) <= 500):
                        raise TableError(f"Series '{label}' must contain between 2 and 500 points.")

                    cleaned_points = []
                    for pt_idx, pt in enumerate(raw_points):
                        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
                            raise TableError(f"Series '{label}' point #{pt_idx + 1} must be an [x, y] pair.")
                        try:
                            x_val = float(pt[0])
                            y_val = float(pt[1])
                        except (ValueError, TypeError) as e:
                            raise TableError(f"Series '{label}' contains non-numeric values in point #{pt_idx + 1}: {e}") from e
                        if not math.isfinite(x_val) or not math.isfinite(y_val):
                            raise TableError(f"Series '{label}' point #{pt_idx + 1} must be finite numbers.")
                        if abs(x_val) > 1e12 or abs(y_val) > 1e12:
                            raise TableError(f"Series '{label}' point #{pt_idx + 1} values cannot exceed 1e12 in magnitude.")
                        cleaned_points.append((x_val, y_val))

                    cleaned_points.sort(key=lambda p: p[0])

                    resolved_series.append({
                        "label": label,
                        "points": cleaned_points,
                        "source": str(source_desc) if source_desc else "",
                    })

                png = render_chart(title, x_label, y_label, resolved_series)
                filename = f"plot-{len(self.artifacts) + 1}.png"
                alt_text = f"{title}. {x_label} versus {y_label}. " + "; ".join(
                    f"{item['label']}: {len(item['points'])} points" for item in resolved_series
                )
                self.artifacts.append(ChartArtifact(filename, title, png, alt_text))
                return {
                    "status": "graph_ready",
                    "filename": filename,
                    "series": resolved_series,
                    "note": "The bot will attach this graph to the answer thread. Explain the trends and cite any formulas or files in your answer.",
                }
            except TableError as error:
                return {"error": str(error), "note": "Explain this limitation; do not invent data or claim a graph was created."}
