import json
import unittest
from unittest.mock import Mock, patch
import requests

from tools.game_manual import GAME_MANUAL_URL
from tools.frc_docs import (
    FRCDocsRegistry,
    APPROVED_FRC_DOMAINS,
    is_approved_frc_domain,
    search_frc_docs,
    read_frc_doc,
    _clean_html_to_markdown,
)


SAMPLE_SPHINX_INDEX = json.dumps({
    "titles": ["PID Control in WPILib", "Swerve Drive Kinematics", "Talon FX Current Limits"],
    "docnames": [
        "docs/software/controllers/pid",
        "docs/software/kinematics/swerve",
        "docs/hardware/talonfx/current_limits",
    ],
    "titleterms": {
        "pid": [0],
        "control": [0],
        "swerve": [1],
        "kinemat": [1],
        "talonfx": [2],
        "current": [2],
        "limit": [2],
    },
    "terms": {
        "pidcontroller": [0],
        "feedback": [0],
        "odometry": [1],
        "stator": [2],
        "supply": [2],
    },
})

SAMPLE_CHIEF_DELPHI_SEARCH = {
    "topics": [
        {"id": 478346, "title": "Reefscape Rule Questions"},
        {"id": 450123, "title": "TalonFX Stator Current Limits Tuning"},
    ],
    "posts": [
        {
            "topic_id": 478346,
            "post_number": 1,
            "blurb": "Questions about robot starting height and extension limits under rule G401.",
        },
        {
            "topic_id": 450123,
            "post_number": 2,
            "blurb": "Configuring stator current limit to 60A helps prevent brownouts and motor overheating.",
        },
    ],
}

SAMPLE_CHIEF_DELPHI_TOPIC = {
    "title": "Reefscape Rule Questions",
    "post_stream": {
        "posts": [
            {
                "username": "LeadMentor",
                "cooked": "<p>Can the robot extend beyond the starting perimeter during autonomous? See <code>rule G401</code>.</p>",
            },
            {
                "username": "InspectorBob",
                "cooked": "<p>According to rule G401, extensions outside the frame perimeter are prohibited in AUTO.</p>",
            },
        ]
    },
}

SAMPLE_HTML_DOC = """
<!DOCTYPE html>
<html>
<head><title>PIDController — WPILib</title><script>var x = 1;</script><style>.bad{}</style></head>
<body>
<nav><a href="/">Home</a></nav>
<div role="main">
  <h1>PIDController Class</h1>
  <p>The <code>PIDController</code> calculates a control loop output based on error.</p>
  <h2>Constructor</h2>
  <pre><code>frc::PIDController controller{0.1, 0.0, 0.01};</code></pre>
  <ul>
    <li>Kp: Proportional gain</li>
    <li>Ki: Integral gain</li>
  </ul>
</div>
<footer>Copyright 2025</footer>
</body>
</html>
"""


class FRCDocsTests(unittest.TestCase):
    def test_domain_whitelist_enforcement(self):
        # Whitelisted domains
        for url in (
            "https://docs.wpilib.org/en/stable/docs/pid.html",
            "https://v6.docs.ctr-electronics.com/en/stable/index.html",
            "https://api.ctr-electronics.com/phoenix6/stable/cpp/",
            "https://www.chiefdelphi.com/t/reefscape/478346",
            "https://firstinspires.org/robotics/frc/game-and-season",
            "https://frc-qa.firstinspires.org/questions",
            "https://docs.revrobotics.com/sparkmax/",
            "https://pathplanner.dev/home.html",
            "https://thebluealliance.com/team/5687",
        ):
            with self.subTest(url=url):
                self.assertTrue(is_approved_frc_domain(url))

        # Blocked domains
        for url in (
            "https://google.com",
            "https://reddit.com/r/FRC",
            "https://example.com/exploit",
            "http://malicious-site.net/hack",
            "ftp://docs.wpilib.org/files",
            "file:///etc/passwd",
            "",
            "not-a-url",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_approved_frc_domain(url))

    def test_read_frc_doc_rejects_unapproved_domains(self):
        result = read_frc_doc("https://external-search.com/leak")
        self.assertIn("error", result)
        self.assertIn("Access denied", result["error"])

    def test_read_frc_doc_rejects_empty_url(self):
        self.assertIn("error", read_frc_doc(""))
        self.assertIn("error", read_frc_doc("   "))

    @patch("tools.frc_docs.search_game_manual")
    @patch("tools.frc_docs.REGISTRY.search_sphinx")
    @patch("tools.frc_docs.REGISTRY.search_chief_delphi")
    def test_search_frc_docs_dispatches_to_sources(self, mock_cd, mock_sphinx, mock_manual):
        mock_sphinx.return_value = [
            {"title": "PID Controller", "url": "https://docs.wpilib.org/pid.html", "source": "WPILib Docs"}
        ]
        mock_cd.return_value = [
            {"title": "Tuning PID", "url": "https://www.chiefdelphi.com/t/123", "snippet": "Tune Kp first", "source": "Chief Delphi"}
        ]
        mock_manual.return_value = {"matches": [
            {"title": "G401 Behind the lines", "url": f"{GAME_MANUAL_URL}#page=64",
             "snippet": "In AUTO...", "rule_id": "G401", "page": 64,
             "source": "2026 FRC Game Manual"}
        ]}

        result = search_frc_docs("PID Controller", source="all")
        titles = [m["title"] for m in result["matches"]]
        # WPILib is searched twice (WPILib + CTRE indexes share the mock), and
        # both the Game Manual and Chief Delphi contribute once.
        self.assertIn("PID Controller", titles)
        self.assertIn("G401 Behind the lines", titles)
        self.assertIn("Tuning PID", titles)
        mock_manual.assert_called_once()

    @patch("tools.frc_docs.search_game_manual")
    @patch("tools.frc_docs.REGISTRY.search_sphinx")
    def test_search_frc_docs_filters_by_source(self, mock_sphinx, mock_manual):
        mock_sphinx.return_value = [
            {"title": "Swerve Kinematics", "url": "https://docs.wpilib.org/swerve.html", "source": "WPILib Docs"}
        ]
        result = search_frc_docs("swerve", source="wpilib")
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["source"], "WPILib Docs")
        # A WPILib-only search must not download the Game Manual.
        mock_manual.assert_not_called()

    @patch("tools.frc_docs.REGISTRY.search_chief_delphi", return_value=[])
    @patch("tools.frc_docs.search_game_manual")
    def test_search_frc_docs_game_manual_source(self, mock_manual, mock_cd):
        mock_manual.return_value = {"matches": [
            {"title": "R103 ROBOT weight limit", "url": f"{GAME_MANUAL_URL}#page=78",
             "snippet": "must not exceed", "rule_id": "R103", "page": 78,
             "source": "2026 FRC Game Manual"}
        ]}
        result = search_frc_docs("robot weight limit", source="game_manual")
        self.assertEqual(result["matches"][0]["rule_id"], "R103")
        mock_manual.assert_called_once()

    def test_game_manual_host_is_approved(self):
        self.assertTrue(is_approved_frc_domain(GAME_MANUAL_URL))
        self.assertIn("firstfrc.blob.core.windows.net", APPROVED_FRC_DOMAINS)

    @patch("tools.frc_docs.read_game_manual_page")
    def test_read_frc_doc_routes_manual_page_fragment(self, mock_page):
        mock_page.return_value = {"page": 64, "content": "G401 Behind the lines."}
        result = read_frc_doc(f"{GAME_MANUAL_URL}#page=64")
        mock_page.assert_called_once_with(64)
        self.assertEqual(result["page"], 64)

    @patch("tools.frc_docs.read_game_manual_rule")
    def test_read_frc_doc_routes_manual_rule_fragment(self, mock_rule):
        mock_rule.return_value = {"rule_id": "G401"}
        result = read_frc_doc(f"{GAME_MANUAL_URL}#rule=G401")
        mock_rule.assert_called_once_with("G401")
        self.assertEqual(result["rule_id"], "G401")

    @patch("tools.frc_docs.MANUAL")
    def test_read_frc_doc_manual_without_fragment_describes_manual(self, mock_manual):
        mock_manual.season = "2026"
        mock_manual.metadata = {"season": "2026", "version": "TU22"}
        result = read_frc_doc(GAME_MANUAL_URL)
        self.assertNotIn("error", result)
        self.assertIn("search_game_manual", result["note"])

    def test_search_frc_docs_empty_query_returns_error(self):
        self.assertIn("error", search_frc_docs(""))
        self.assertIn("error", search_frc_docs("   "))

    def test_sphinx_scoring_and_ranking(self):
        mock_session = Mock()
        mock_resp = Mock()
        mock_resp.text = f"Search.setIndex({SAMPLE_SPHINX_INDEX});"
        mock_resp.raise_for_status = Mock()
        mock_session.get.return_value = mock_resp

        registry = FRCDocsRegistry(session=mock_session)
        results = registry.search_sphinx(
            "https://test.index.js", "https://docs.wpilib.org", "WPILib", "pid control",
        )
        self.assertTrue(len(results) > 0)
        self.assertEqual(results[0]["title"], "PID Control in WPILib")
        self.assertEqual(results[0]["url"], "https://docs.wpilib.org/docs/software/controllers/pid.html")

    def test_chief_delphi_search_parsing(self):
        mock_session = Mock()
        mock_resp = Mock()
        mock_resp.json.return_value = SAMPLE_CHIEF_DELPHI_SEARCH
        mock_resp.raise_for_status = Mock()
        mock_session.get.return_value = mock_resp

        registry = FRCDocsRegistry(session=mock_session)
        results = registry.search_chief_delphi("reefscape rules")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["title"], "Reefscape Rule Questions")
        self.assertIn("rule G401", results[0]["snippet"])
        self.assertEqual(results[0]["url"], "https://www.chiefdelphi.com/t/478346/1")

    @patch("tools.frc_docs.REGISTRY.session.get")
    def test_read_chief_delphi_topic_json(self, mock_get):
        mock_resp = Mock()
        mock_resp.json.return_value = SAMPLE_CHIEF_DELPHI_TOPIC
        mock_resp.raise_for_status = Mock()
        mock_get.return_value = mock_resp

        doc = read_frc_doc("https://www.chiefdelphi.com/t/reefscape-rules/478346")
        self.assertEqual(doc["title"], "Reefscape Rule Questions")
        self.assertIn("LeadMentor", doc["content"])
        self.assertIn("InspectorBob", doc["content"])
        self.assertIn("rule G401", doc["content"])
        self.assertEqual(doc["source"], "Chief Delphi")

    @patch("tools.frc_docs.REGISTRY.session.get")
    def test_read_html_doc_cleaning(self, mock_get):
        mock_resp = Mock()
        mock_resp.text = SAMPLE_HTML_DOC
        mock_resp.raise_for_status = Mock()
        mock_get.return_value = mock_resp

        doc = read_frc_doc("https://docs.wpilib.org/en/stable/docs/software/controllers/pid.html")
        self.assertIn("PIDController", doc["title"])
        self.assertNotIn("<script>", doc["content"])
        self.assertNotIn(".bad{}", doc["content"])
        self.assertIn("# PIDController Class", doc["content"])
        self.assertIn("frc::PIDController controller{0.1, 0.0, 0.01};", doc["content"])
        self.assertIn("Kp: Proportional gain", doc["content"])

    def test_html_to_markdown_cleaner(self):
        raw_html = "<div role='main'><h1>Title</h1><p>Para with <code>code</code>.</p></div>"
        cleaned = _clean_html_to_markdown(raw_html)
        self.assertIn("# Title", cleaned)
        self.assertIn("`code`", cleaned)


if __name__ == "__main__":
    unittest.main()
