import unittest
from unittest.mock import Mock, patch

from slack_bolt.context.say import Say
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from main import handle_mention


class MentionTests(unittest.TestCase):
    def setUp(self):
        self.event = {"text": "<@BOT> Explain drive", "ts": "1.0", "channel": "C1"}
        self.say = Mock(return_value={"ts": "2.0"})
        self.client = Mock()
        self.logger = Mock()
        self.llm = Mock()
        self.llm.answer.return_value = "The drivetrain moves the robot."

    def handle(self):
        handle_mention(self.event, self.say, self.client, self.logger, self.llm)

    def test_status_is_posted_before_model_and_updated_with_answer(self):
        def answer(**kwargs):
            self.say.assert_called_once_with(text="I'm looking into that…", thread_ts="1.0")
            self.assertEqual(kwargs["question"], "Explain drive")
            return " Answer "

        self.llm.answer.side_effect = answer
        self.handle()
        self.client.chat_update.assert_called_once_with(channel="C1", ts="2.0", markdown_text="Answer")
        self.say.assert_called_once()

    def test_replies_stay_in_existing_thread(self):
        self.event["thread_ts"] = "0.5"
        self.handle()
        self.assertEqual(self.say.call_args.kwargs["thread_ts"], "0.5")

    def test_empty_and_non_text_model_answers_produce_error_message(self):
        for answer in (None, "", "  ", {"function_call": "read_file"}):
            with self.subTest(answer=answer):
                self.llm.answer.return_value = answer
                self.handle()
                text = self.client.chat_update.call_args.kwargs["markdown_text"]
                self.assertIsInstance(text, str)
                self.assertIn("couldn't complete", text)
                self.logger.exception.assert_called()

    def test_model_exception_replaces_status_with_error(self):
        self.llm.answer.side_effect = TimeoutError("Gemini timed out")
        self.handle()
        self.assertIn("couldn't complete", self.client.chat_update.call_args.kwargs["markdown_text"])
        self.logger.exception.assert_called()

    def test_status_failure_still_delivers_final_answer(self):
        self.say.side_effect = [RuntimeError("Slack unavailable"), {"ts": "3.0"}]
        self.handle()
        self.client.chat_update.assert_not_called()
        self.say.assert_called_with(
            {"markdown_text": self.llm.answer.return_value}, thread_ts="1.0",
        )

    def test_update_failure_falls_back_to_thread_reply(self):
        self.client.chat_update.side_effect = RuntimeError("Update failed")
        self.handle()
        self.assertEqual(self.say.call_count, 2)
        self.say.assert_called_with(
            {"markdown_text": self.llm.answer.return_value}, thread_ts="1.0",
        )

    def test_long_answer_updates_first_part_and_posts_remaining_parts_in_thread(self):
        self.llm.answer.return_value = ("Paragraph about the motor.\n\n" * 700).strip()
        self.event["thread_ts"] = "0.5"
        self.handle()
        first = self.client.chat_update.call_args.kwargs["markdown_text"]
        followups = self.say.call_args_list[1:]
        self.assertTrue(followups)
        chunks = [first] + [call.args[0]["markdown_text"] for call in followups]
        self.assertEqual("".join(chunks), self.llm.answer.return_value)
        self.assertTrue(all(len(chunk) <= 12_000 for chunk in chunks))
        self.assertTrue(all(call.kwargs["thread_ts"] == "0.5" for call in followups))

    def test_real_slack_sdk_sends_markdown_without_conflicting_text_field(self):
        answer = (
            "### Motor settings\n\n**Supply:** `25_A`\n\n"
            "- [Constants.h](https://github.com/team/robot/blob/main/Constants.h)\n\n"
            "```cpp\nif (current < limit && enabled) { apply(config); }\n```"
        )
        self.llm.answer.return_value = answer
        client = WebClient(token="unit-test")
        say = Say(client=client, channel="C1")
        for fail_update in (False, True):
            with self.subTest(fail_update=fail_update):
                payloads = []

                def send(api_url, req_args):
                    payloads.append(req_args["json"])
                    if fail_update and api_url.endswith("chat.update"):
                        raise RuntimeError("Update failed")
                    return {"ok": True, "ts": "2.0"}

                with patch.object(client, "_sync_send", side_effect=send):
                    handle_mention(self.event, say, client, self.logger, self.llm)
                replies = payloads[1:]
                self.assertEqual(len(replies), 2 if fail_update else 1)
                for payload in replies:
                    self.assertEqual(payload["markdown_text"], answer)
                    self.assertNotIn("text", payload)
                    self.assertNotIn("blocks", payload)
                if fail_update:
                    self.assertEqual(replies[-1]["thread_ts"], "1.0")

    def test_delivery_failure_is_logged_without_crashing_listener(self):
        self.say.side_effect = RuntimeError("Slack unavailable")
        self.handle()
        self.assertEqual(self.logger.exception.call_count, 2)
        self.logger.info.assert_called_once()

    def test_bare_mentions_and_bot_events_do_no_work(self):
        for change in ({"text": "<@BOT> "}, {"bot_id": "B1"}, {"subtype": "bot_message"}):
            with self.subTest(change=change):
                original = self.event.copy()
                self.event.update(change)
                self.handle()
                self.say.assert_not_called()
                self.llm.answer.assert_not_called()
                self.event = original

    def _answer_with_graph(self, **kwargs):
        functions = {tool.__name__: tool for tool in kwargs["tools"]}
        functions["read_file"]("Shot.cpp")
        result = functions["plot_lookup_tables"](
            title="RPM table", x_label="Distance (m)", y_label="Speed (RPM)",
            paths=["Shot.cpp"], tables=["flyMap"], labels=["Flywheel"],
        )
        self.assertEqual(result["status"], "graph_ready")
        return "The graph shows the configured RPM table."

    def _graph_source(self):
        return patch("main.read_file", return_value={
            "content": "auto flyMap = {{1, 1200}, {2, 1300}};",
            "url": "https://github.com/team/robot/blob/main/Shot.cpp",
        })

    def test_graph_is_uploaded_as_png_in_the_existing_thread(self):
        self.event["thread_ts"] = "0.5"
        self.llm.answer.side_effect = self._answer_with_graph
        with self._graph_source():
            self.handle()
        upload = self.client.files_upload_v2.call_args.kwargs
        self.assertEqual(upload["channel"], "C1")
        self.assertEqual(upload["thread_ts"], "0.5")
        self.assertTrue(upload["file"].startswith(b"\x89PNG"))
        self.assertEqual(upload["filename"], "lookup-tables-1.png")
        self.assertIn("Flywheel: 2 points", upload["alt_txt"])
        self.assertIn("configured RPM", self.client.chat_update.call_args.kwargs["markdown_text"])

    def test_graph_upload_failure_preserves_text_and_explains_missing_scope(self):
        self.llm.answer.side_effect = self._answer_with_graph
        self.client.files_upload_v2.side_effect = SlackApiError("missing scope", {"error": "missing_scope"})
        with self._graph_source():
            self.handle()
        answer = self.client.chat_update.call_args.kwargs["markdown_text"]
        self.assertIn("configured RPM", answer)
        self.assertIn("files:write", answer)
        self.assertIn("reinstall", answer)

    def test_other_upload_failure_is_reported_and_does_not_escape_listener(self):
        self.llm.answer.side_effect = self._answer_with_graph
        self.client.files_upload_v2.side_effect = TimeoutError("Slack timed out")
        with self._graph_source():
            self.handle()
        answer = self.client.chat_update.call_args.kwargs["markdown_text"]
        self.assertIn("configured RPM", answer)
        self.assertIn("upload failed", answer)

    def test_text_only_answer_does_not_upload_any_files(self):
        self.handle()
        self.client.files_upload_v2.assert_not_called()

    def test_thread_history_is_fetched_and_forwarded_to_model(self):
        self.event = {"text": "<@BOT> What gear ratio?", "ts": "2.0", "thread_ts": "1.0", "channel": "C1"}
        self.client.conversations_replies.return_value = {
            "ok": True,
            "messages": [
                {"ts": "1.0", "user": "U123", "text": "<@BOT> Explain drive"},
                {"ts": "1.1", "bot_id": "B123", "text": "I'm looking into that…"},
                {"ts": "1.1", "bot_id": "B123", "text": "Drive uses 4 swerve modules."},
                {"ts": "2.0", "user": "U123", "text": "<@BOT> What gear ratio?"},
            ],
        }
        self.handle()
        self.client.conversations_replies.assert_called_once_with(channel="C1", ts="1.0", limit=20)
        call_kwargs = self.llm.answer.call_args.kwargs
        self.assertEqual(call_kwargs["question"], "What gear ratio?")
        self.assertEqual(call_kwargs["history"], [
            {"role": "user", "content": "Explain drive"},
            {"role": "assistant", "content": "Drive uses 4 swerve modules."},
        ])

    def test_thread_history_failure_falls_back_to_empty_history(self):
        self.event["thread_ts"] = "0.5"
        self.client.conversations_replies.side_effect = SlackApiError("missing scope", {"error": "missing_scope"})
        self.handle()
        self.assertEqual(self.llm.answer.call_args.kwargs["history"], [])
        self.logger.warning.assert_called()
        self.assertIn("The drivetrain moves the robot.", self.client.chat_update.call_args.kwargs["markdown_text"])

    def test_top_level_mention_does_not_call_conversations_replies(self):
        self.handle()
        self.client.conversations_replies.assert_not_called()
        self.assertEqual(self.llm.answer.call_args.kwargs["history"], [])


if __name__ == "__main__":
    unittest.main()
