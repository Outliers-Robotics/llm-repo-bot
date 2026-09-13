import unittest
from unittest.mock import Mock

from cpp_tables import TableError, extract_table
from tools.plots import PlotSession


# Synthetic C++ fixture with the same initializer/offset syntax as the robot.
SOURCE = """
double rpmBumpLow = 30;
double rpmBumpHigh = 75;
std::vector<std::pair<double, double>> flyMap{
    {3.0, 1600 + rpmBumpHigh}, // offset is part of the configured value
    {1.0, 1200 + rpmBumpLow}, {2.0, 1300 + rpmBumpLow}};
std::vector<std::pair<double, double>> passMap{
    {1.0, 900}, {2.0, 1000}, {3.0, 1100}};
"""


def source_result(path="Shot.cpp", content=SOURCE, **extra):
    return {"path": path, "content": content, "url": f"https://github.com/team/robot/blob/main/{path}", "truncated": False, **extra}


def graph_args(**extra):
    return {"title": "Flywheel lookup tables", "x_label": "Distance (m)",
            "y_label": "Flywheel speed (RPM)", "paths": ["Shot.cpp"],
            "tables": ["flyMap"], "labels": ["Shooting"], **extra}


class TableTests(unittest.TestCase):
    def test_offsets_and_all_rows_are_resolved_and_sorted(self):
        self.assertEqual(extract_table(SOURCE, "flyMap"), [(1, 1230), (2, 1330), (3, 1675)])
        self.assertEqual(extract_table(SOURCE, "passMap"), [(1, 900), (2, 1000), (3, 1100)])

    def test_scalar_dependencies_float_suffixes_and_cpp_integer_division(self):
        source = "double a = 2.5f; double b = a * 2; auto map = {{1, b + 5 / 2}, {2, -(5 / 2) + 2.5e1}};"
        self.assertEqual(extract_table(source, "map"), [(1, 7), (2, 23)])

    def test_integer_suffixes_constexpr_brace_init_and_array_syntax(self):
        source = (
            "constexpr double bump{30};\n"
            "const int extra = 5;\n"
            "std::pair<double, double> map[] = {{1, 1000u + bump}, {2, 1200L + extra}};"
        )
        self.assertEqual(extract_table(source, "map"), [(1, 1030), (2, 1205)])

    def test_comments_do_not_supply_rows_or_offsets(self):
        source = "// double bump = 999;\ndouble bump = 5; auto map = {/* {0, 0}, */ {1, bump}, {2, 10}};"
        self.assertEqual(extract_table(source, "map"), [(1, 5), (2, 10)])

    def test_unsupported_or_ambiguous_data_is_rejected_without_partial_plot(self):
        for source in (
            "auto map = {{1, 10}, {2, calculate()}};",
            "auto map = {{1, 10}, {2, missing}};",
            "auto map = {{1, 10}, {1, 20}};",
            "auto map = {{1, 10}, {2, 20}",
            "auto map = {{1, 10}, {2, 1e309}};",
            "auto map = {{1, 10}, {2, 1 / 0}};",
            "auto map = {{1, 10}, {2, 2 ** 8}};",
            "double bump = 2; bump += 3; auto map = {{1, bump}, {2, 20}};",
            "double bump = 2; ++bump; auto map = {{1, bump}, {2, 20}};",
            "double bump = 2; double bump = 3; auto map = {{1, bump}, {2, 20}};",
            "auto map = {{1_m, 10_rpm}, {2_m, 20_rpm}};",
        ):
            with self.subTest(source=source), self.assertRaises(TableError):
                extract_table(source, "map")


class PlotTests(unittest.TestCase):
    def setUp(self):
        self.reader = Mock(return_value=source_result())
        self.session = PlotSession(self.reader)

    def test_multiple_curves_are_rendered_from_read_source_with_provenance(self):
        self.session.read_file("Shot.cpp")
        result = self.session.plot_lookup_tables(**graph_args(
            paths=["Shot.cpp", "Shot.cpp"], tables=["flyMap", "passMap"], labels=["Shooting", "Passing"],
        ))
        self.assertEqual(result["status"], "graph_ready")
        self.assertEqual(result["series"][0]["points"][-1], (3, 1675))
        self.assertEqual(result["series"][1]["points"][0], (1, 900))
        self.assertTrue(result["series"][0]["url"].startswith("https://github.com/"))
        self.assertTrue(self.session.artifacts[0].png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn("Shooting: 3 points", self.session.artifacts[0].alt_text)
        self.reader.assert_called_once_with("Shot.cpp")  # Chart does not refetch source.

    def test_unread_truncated_and_unsupported_sources_return_useful_errors(self):
        self.assertIn("Read Shot.cpp", self.session.plot_lookup_tables(**graph_args())["error"])
        for source in (source_result(truncated=True), source_result(content="auto flyMap = {{1, 3}, {2, unknown}};")):
            self.reader.return_value = source
            self.session.read_file("Shot.cpp")
            self.assertIn("error", self.session.plot_lookup_tables(**graph_args()))
        self.assertEqual(self.session.artifacts, [])

    def test_sessions_do_not_share_source_or_attachments(self):
        self.session.read_file("Shot.cpp")
        other = PlotSession(self.reader)
        self.assertIn("error", other.plot_lookup_tables(**graph_args()))
        self.assertEqual(other.artifacts, [])

    def test_bad_series_lengths_are_rejected(self):
        self.session.read_file("Shot.cpp")
        result = self.session.plot_lookup_tables(**graph_args(labels=[]))
        self.assertIn("one path, table, and label", result["error"])
        self.assertEqual(self.session.artifacts, [])

    def test_single_string_arguments_are_normalized_to_single_series(self):
        self.session.read_file("Shot.cpp")
        result = self.session.plot_lookup_tables(
            title="RPM table",
            x_label="Distance (m)",
            y_label="Speed (RPM)",
            paths="Shot.cpp",
            tables="flyMap",
            labels="Flywheel",
        )
        self.assertEqual(result["status"], "graph_ready")
        self.assertEqual(len(self.session.artifacts), 1)
        self.assertEqual(len(result["series"][0]["points"]), 3)
