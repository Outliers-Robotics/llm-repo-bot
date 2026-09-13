"""Per-mention chart artifacts rendered locally from verified repository reads."""

from dataclasses import dataclass
from io import BytesIO
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
        notes = ["Points are source table entries; connecting lines are visual guides."]
        notes.extend(f"{item['label']}: {item['table']} — {item['path']}" for item in series)
        footer = "\n".join(textwrap.fill(note, 115) for note in notes)
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
        paths: list[str], tables: list[str], labels: list[str],
    ) -> dict:
        """Create a PNG graph from named C++ {x, y} lookup tables already read.

        Args:
            title: Short descriptive chart title.
            x_label: X axis meaning and verified unit, such as Distance (m).
            y_label: Y axis meaning and verified unit, such as Flywheel speed (RPM).
            paths: Source path for each series; each must have been read_file'd first.
            tables: Exact variable names of brace-initialized pair tables, one per path.
            labels: Legend names, one per table. Compare only tables with the same units.

        Returns:
            Chart attachment details and resolved points, or an actionable error.
            Numeric values come from source, including basic arithmetic and local
            scalar offsets. Function calls, units wrappers, and runtime data are unsupported.
            The image is queued for Slack delivery; do not invent an image URL.
        """
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
                series = []
                for path, table, label in zip(paths, tables, labels):
                    source = self._sources.get(path)
                    if source is None:
                        raise TableError(f"Read {path} before requesting its graph.")
                    if source.get("truncated"):
                        raise TableError(f"{path} was truncated; a complete source file is required.")
                    series.append({
                        "path": path, "table": table, "label": _label(label),
                        "url": source.get("url"),
                        "points": extract_table(source["content"], table),
                    })
                png = render_chart(title, x_label, y_label, series)
                filename = f"lookup-tables-{len(self.artifacts) + 1}.png"
                alt_text = f"{title}. {x_label} versus {y_label}. " + "; ".join(
                    f"{item['label']}: {len(item['points'])} points from {item['table']}" for item in series
                )
                self.artifacts.append(ChartArtifact(filename, title, png, alt_text))
                return {"status": "graph_ready", "filename": filename, "series": series,
                        "note": "The bot will attach this graph to the answer thread. Cite the sources in your answer."}
            except TableError as error:
                return {"error": str(error), "note": "Explain this limitation; do not invent data or claim a graph was created."}
