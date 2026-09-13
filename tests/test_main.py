import unittest
from unittest.mock import Mock

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
        self.client.chat_update.assert_called_once_with(channel="C1", ts="2.0", text="Answer")
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
                text = self.client.chat_update.call_args.kwargs["text"]
                self.assertIsInstance(text, str)
                self.assertIn("couldn't complete", text)
                self.logger.exception.assert_called()

    def test_model_exception_replaces_status_with_error(self):
        self.llm.answer.side_effect = TimeoutError("Gemini timed out")
        self.handle()
        self.assertIn("couldn't complete", self.client.chat_update.call_args.kwargs["text"])
        self.logger.exception.assert_called()

    def test_status_failure_still_delivers_final_answer(self):
        self.say.side_effect = [RuntimeError("Slack unavailable"), {"ts": "3.0"}]
        self.handle()
        self.client.chat_update.assert_not_called()
        self.assertEqual(self.say.call_args.kwargs, {
            "text": self.llm.answer.return_value, "thread_ts": "1.0",
        })

    def test_update_failure_falls_back_to_thread_reply(self):
        self.client.chat_update.side_effect = RuntimeError("Update failed")
        self.handle()
        self.assertEqual(self.say.call_count, 2)
        self.assertEqual(self.say.call_args.kwargs, {
            "text": self.llm.answer.return_value, "thread_ts": "1.0",
        })

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


if __name__ == "__main__":
    unittest.main()
