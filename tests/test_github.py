import base64
import os
import unittest
from unittest.mock import Mock, patch

import requests

from tools.github import read_file, search_repo


class GitHubToolTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {
            "GITHUB_OWNER": "team", "GITHUB_REPO": "robot", "GITHUB_TOKEN": "unit-test",
        }).start()
        self.response = Mock()
        self.get = patch("tools.github.requests.get", return_value=self.response).start()

    def test_search_is_repo_scoped_and_has_network_timeouts(self):
        self.response.json.return_value = {
            "items": [{"path": "Robot.java", "html_url": "https://github.com/team/robot/Robot.java"}],
        }
        self.assertEqual(search_repo("Drive")["matches"][0]["path"], "Robot.java")
        self.assertEqual(self.get.call_args.kwargs["params"]["q"], "Drive repo:team/robot")
        self.assertEqual(self.get.call_args.kwargs["timeout"], (5, 10))

    def test_read_file_decodes_source_and_reports_truncation(self):
        for length in (100, 50_001):
            with self.subTest(length=length):
                self.response.json.return_value = {
                    "type": "file", "encoding": "base64",
                    "html_url": "https://github.com/team/robot/Robot.java",
                    "content": base64.b64encode(b"a" * length).decode(),
                }
                result = read_file("Robot.java")
                self.assertEqual(result["content"], "a" * min(length, 50_000))
                self.assertEqual(result["truncated"], length > 50_000)

    def test_directory_and_unsupported_file_are_reported(self):
        for data in ([], {"type": "file", "encoding": "none", "content": ""}):
            with self.subTest(data=data):
                self.response.json.return_value = data
                with self.assertRaises(ValueError):
                    read_file("src")

    def test_github_errors_propagate_to_tool_loop(self):
        self.response.raise_for_status.side_effect = requests.HTTPError("rate limited")
        with self.assertRaises(requests.HTTPError):
            search_repo("Drive")
        self.response.json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
