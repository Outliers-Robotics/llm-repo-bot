import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools import game_manual
from tools.game_manual import (
    GAME_MANUAL_URL,
    INDEX_SCHEMA_VERSION,
    GameManualIndex,
    _stem,
    _tokenize,
    read_game_manual_page,
    read_game_manual_rule,
    search_game_manual,
)


# Mirrors the real PDF layout: a "Section N Name Version: TU22 P of 166" header
# line on each page, rules as "<ID> [*]<Title>. <body>", numbered subsections.
FAKE_PAGES = [
    " \nVersion: TU22 \n2026 FIRST Robotics Competition \nGame Manual \n",
    (
        " \nSection 7 Game Rules (G) Version: TU22 2 of 4 \n"
        "7.3 Pre-MATCH \n"
        "G401 *Behind the lines. In AUTO, each DRIVE TEAM member must remain in their \n"
        "staged areas and may not contact anything in front of the HUMAN STARTING LINE. \n"
        "Violation: MINOR FOUL regardless of the number of items contacted. \n"
        "G402 Stay in your lane. ROBOTS may not cross the CENTER LINE during AUTO. \n"
        "Violation: MAJOR FOUL. \n"
    ),
    (
        " \nSection 8 ROBOT Construction Rules (R) Version: TU22 3 of 4 \n"
        "8.2 ROBOT Weight \n"
        "R103 ROBOT weight limit. The ROBOT weight must not exceed 115 lbs, excluding \n"
        "the BUMPERS and battery. \n"
        "R501 *Allowable motors. The only motors permitted include the following: \n"
        "REV Robotics NEO Brushless REV-21-1650 \n"
        "CTR Electronics Falcon 500 217-6515 \n"
    ),
    (
        " \nSection 15 Glossary Version: TU22 4 of 4 \n"
        "BUMPER a required assembly which attaches to the ROBOT frame perimeter. \n"
    ),
]


def build_index(pages=None, url=GAME_MANUAL_URL) -> GameManualIndex:
    """Build an index directly from page text, with no network or PDF involved."""
    index = GameManualIndex(url=url)
    index._install(index._build_payload(pages if pages is not None else FAKE_PAGES))
    return index


class TokenizerTests(unittest.TestCase):
    def test_stem_folds_plurals_and_participles(self):
        self.assertEqual(_stem("motors"), "motor")
        self.assertEqual(_stem("batteries"), "battery")
        self.assertEqual(_stem("scoring"), "scor")
        self.assertEqual(_stem("allowed"), "allow")
        # Short words and double-s words are left alone.
        self.assertEqual(_stem("cas"), "cas")
        self.assertEqual(_stem("class"), "class")

    def test_tokenize_drops_stopwords_and_stems(self):
        tokens = _tokenize("Can we use a NEO motor?")
        self.assertIn("neo", tokens)
        self.assertIn("motor", tokens)
        for stopword in ("can", "we", "use", "a"):
            self.assertNotIn(stopword, tokens)

    def test_tokenize_matches_singular_to_plural(self):
        self.assertEqual(_tokenize("motor"), _tokenize("motors"))


class ManualParsingTests(unittest.TestCase):
    def setUp(self):
        self.index = build_index()

    def test_extracts_version_and_page_count(self):
        self.assertEqual(self.index.metadata["version"], "TU22")
        self.assertEqual(self.index.metadata["page_count"], 4)
        self.assertEqual(self.index.metadata["season"], "2026")

    def test_indexes_rules_by_id(self):
        self.assertEqual(
            sorted(self.index._by_rule), ["G401", "G402", "R103", "R501"],
        )

    def test_rule_captures_title_section_and_page(self):
        rule = self.index.get_rule("G401")
        self.assertEqual(rule["title"], "Behind the lines")
        self.assertEqual(rule["section"], "Game Rules (G)")
        self.assertEqual(rule["page"], 2)
        self.assertTrue(rule["headline_violation"])
        self.assertIn("HUMAN STARTING LINE", rule["text"])
        # The rule body stops at the next rule, and does not absorb G402.
        self.assertNotIn("Stay in your lane", rule["text"])

    def test_rule_without_asterisk_is_not_flagged(self):
        self.assertFalse(self.index.get_rule("G402")["headline_violation"])

    def test_page_header_is_stripped_from_body(self):
        # Every page carrying a "Section N ... Version: X P of N" header must
        # have it removed; the cover page has no such header to strip.
        for entry in self.index._entries:
            if entry["page"] == 1:
                continue
            self.assertNotIn("Version: TU22", entry["text"])
            self.assertNotIn("of 4", entry["text"])

    def test_numbered_subsections_become_entries(self):
        ids = {e["id"] for e in self.index._entries if e["kind"] == "section"}
        self.assertIn("7.3", ids)
        self.assertIn("8.2", ids)

    def test_part_numbers_are_not_mistaken_for_rules(self):
        # REV-21-1650 and 217-6515 are part numbers inside R501's table.
        self.assertIn("REV-21-1650", self.index.get_rule("R501")["text"])
        self.assertNotIn("REV", self.index._by_rule)

    def test_page_url_points_at_the_pdf_page(self):
        self.assertEqual(
            self.index.page_url(2), f"{GAME_MANUAL_URL}#page=2",
        )


class ManualSearchTests(unittest.TestCase):
    def setUp(self):
        self.index = build_index()

    def test_explicit_rule_id_is_returned_first(self):
        results = self.index.search("what does G401 say")
        self.assertEqual(results[0]["rule_id"], "G401")

    def test_lowercase_rule_id_still_matches(self):
        self.assertEqual(self.index.search("g401")[0]["rule_id"], "G401")

    def test_keyword_search_finds_the_right_rule(self):
        results = self.index.search("ROBOT weight limit")
        self.assertEqual(results[0]["rule_id"], "R103")

    def test_search_matches_plural_title_from_singular_query(self):
        rule_ids = [r["rule_id"] for r in self.index.search("NEO motor")]
        self.assertIn("R501", rule_ids)

    def test_results_carry_citable_url_and_source(self):
        result = self.index.search("ROBOT weight limit")[0]
        self.assertEqual(result["url"], f"{GAME_MANUAL_URL}#page=3")
        self.assertEqual(result["source"], "2026 FRC Game Manual")
        self.assertEqual(result["section"], "ROBOT Construction Rules (R)")

    def test_snippet_is_populated_and_bounded(self):
        result = self.index.search("ROBOT weight limit")[0]
        self.assertIn("115 lbs", result["snippet"])
        self.assertLessEqual(len(result["snippet"]), game_manual.SNIPPET_CHARS + 2)

    def test_max_results_is_respected(self):
        self.assertLessEqual(len(self.index.search("ROBOT", max_results=2)), 2)

    def test_unmatched_query_returns_nothing(self):
        self.assertEqual(self.index.search("quantum entanglement propulsion"), [])

    def test_no_duplicate_entries(self):
        results = self.index.search("G401 behind the lines AUTO")
        keys = [(r["page"], r["title"]) for r in results]
        self.assertEqual(len(keys), len(set(keys)))


class ManualToolTests(unittest.TestCase):
    def setUp(self):
        self.patcher = patch.object(game_manual, "MANUAL", build_index())
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_search_tool_returns_matches_and_metadata(self):
        result = search_game_manual("ROBOT weight limit")
        self.assertEqual(result["matches"][0]["rule_id"], "R103")
        self.assertEqual(result["manual"]["version"], "TU22")

    def test_search_tool_rejects_empty_query(self):
        self.assertIn("error", search_game_manual(""))
        self.assertIn("error", search_game_manual("   "))

    def test_search_tool_clamps_max_results(self):
        self.assertLessEqual(len(search_game_manual("ROBOT", max_results=99)["matches"]), 10)
        self.assertTrue(search_game_manual("ROBOT", max_results="bad")["matches"])

    def test_search_tool_notes_when_nothing_matches(self):
        result = search_game_manual("quantum entanglement propulsion")
        self.assertEqual(result["matches"], [])
        self.assertIn("note", result)

    def test_read_rule_returns_full_text(self):
        result = read_game_manual_rule("R103")
        self.assertEqual(result["rule_id"], "R103")
        self.assertIn("115 lbs", result["content"])
        self.assertEqual(result["url"], f"{GAME_MANUAL_URL}#page=3")

    def test_read_rule_normalizes_input(self):
        for raw in ("r103", " R103 ", "R 103", "rule R103"):
            with self.subTest(raw=raw):
                self.assertEqual(read_game_manual_rule(raw)["rule_id"], "R103")

    def test_read_rule_rejects_malformed_id(self):
        for raw in ("", "   ", "hello", "ZZ9", "12345"):
            with self.subTest(raw=raw):
                self.assertIn("error", read_game_manual_rule(raw))

    def test_read_rule_reports_unknown_rule(self):
        result = read_game_manual_rule("G999")
        self.assertIn("error", result)
        self.assertIn("G999", result["error"])

    def test_read_page_returns_its_rules(self):
        result = read_game_manual_page(3)
        self.assertEqual(result["page"], 3)
        self.assertIn("R103", result["rules"])
        self.assertIn("R501", result["rules"])
        self.assertIn("115 lbs", result["content"])

    def test_read_page_accepts_numeric_strings_and_rejects_junk(self):
        self.assertEqual(read_game_manual_page("3")["page"], 3)
        self.assertIn("error", read_game_manual_page("not-a-page"))
        self.assertIn("error", read_game_manual_page(999))


class ManualLoadingTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        patcher = patch.dict(os.environ, {"FRC_MANUAL_CACHE_DIR": self.tmpdir.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _index_with_download(self, pdf_bytes=b"%PDF-1.7 fake"):
        response = Mock()
        response.raise_for_status = Mock()
        response.iter_content = Mock(return_value=[pdf_bytes])
        session = Mock()
        session.get.return_value = response
        return GameManualIndex(session=session), session

    def test_download_and_parse_populates_and_caches(self):
        index, session = self._index_with_download()
        with patch.object(GameManualIndex, "_extract_pages", return_value=FAKE_PAGES):
            self.assertTrue(index.ensure_loaded())
        self.assertEqual(index.get_rule("G401")["title"], "Behind the lines")
        session.get.assert_called_once()
        self.assertTrue(os.path.exists(index._cache_path))

    def test_second_index_reads_the_disk_cache_without_downloading(self):
        first, _ = self._index_with_download()
        with patch.object(GameManualIndex, "_extract_pages", return_value=FAKE_PAGES):
            first.ensure_loaded()

        second, session = self._index_with_download()
        self.assertTrue(second.ensure_loaded())
        session.get.assert_not_called()
        self.assertEqual(second.get_rule("R103")["page"], 3)

    def test_stale_schema_cache_is_rebuilt(self):
        index, session = self._index_with_download()
        with patch.object(GameManualIndex, "_extract_pages", return_value=FAKE_PAGES):
            index.ensure_loaded()

        payload = json.loads(open(index._cache_path).read())
        payload["schema"] = INDEX_SCHEMA_VERSION - 1
        with open(index._cache_path, "w") as handle:
            json.dump(payload, handle)

        fresh, session = self._index_with_download()
        with patch.object(GameManualIndex, "_extract_pages", return_value=FAKE_PAGES):
            self.assertTrue(fresh.ensure_loaded())
        session.get.assert_called_once()

    def test_cache_for_a_different_url_is_not_reused(self):
        first, _ = self._index_with_download()
        with patch.object(GameManualIndex, "_extract_pages", return_value=FAKE_PAGES):
            first.ensure_loaded()

        other = GameManualIndex(url="https://firstfrc.blob.core.windows.net/frc2027/m.pdf")
        self.assertIsNone(other._read_disk_cache())

    def test_oversized_download_is_rejected(self):
        index, _ = self._index_with_download()
        with patch.object(game_manual, "MAX_PDF_BYTES", 4):
            self.assertFalse(index.ensure_loaded())

    def test_failed_download_falls_back_to_stale_cache(self):
        index, _ = self._index_with_download()
        with patch.object(GameManualIndex, "_extract_pages", return_value=FAKE_PAGES):
            index.ensure_loaded()

        broken = Mock()
        broken.get.side_effect = RuntimeError("network down")
        stale = GameManualIndex(session=broken)
        with patch.object(game_manual, "_cache_ttl_seconds", return_value=0):
            # TTL 0 forces a refresh attempt; the download fails, so the on-disk
            # copy must still be served rather than losing the tool entirely.
            self.assertTrue(stale.ensure_loaded())
        self.assertEqual(stale.get_rule("G401")["title"], "Behind the lines")

    def test_tools_report_unavailable_when_manual_cannot_load(self):
        broken = Mock()
        broken.get.side_effect = RuntimeError("network down")
        with patch.object(game_manual, "MANUAL", GameManualIndex(session=broken)):
            self.assertIn("error", search_game_manual("bumper"))
            self.assertIn("error", read_game_manual_rule("G401"))
            self.assertIn("error", read_game_manual_page(2))


if __name__ == "__main__":
    unittest.main()
