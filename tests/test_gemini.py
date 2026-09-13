import json
import os
import unittest
from collections import deque
from threading import Barrier
from unittest.mock import patch

import httpx
from google import genai

from llm.gemini import GeminiProvider


def model_response(*parts):
    return {
        "candidates": [{
            "content": {"role": "model", "parts": list(parts)},
            "finishReason": "STOP",
        }],
    }


def tool_call(name="lookup", **args):
    return {"functionCall": {"name": name, "args": args}}


def lookup(path: str) -> dict:
    """Look up a repository file.

    Args:
        path: Repository-relative file path.
    """
    return {"path": path, "content": "source code"}


class GeminiProviderTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {
            "GEMINI_API_KEY": "unit-test",
            "GEMINI_MODEL": "gemini-3.8-flash",
            "GEMINI_MAX_TOOL_ROUNDS": "4",
            "GEMINI_TIMEOUT_SECONDS": "30",
        }).start()
        self.responses = deque()
        self.requests = []
        real_client = genai.Client

        def create_client(**kwargs):
            self.http_options = kwargs["http_options"]
            self.http_options.client_args = {
                "transport": httpx.MockTransport(self.respond),
            }
            return real_client(**kwargs)

        patch("llm.gemini.genai.Client", side_effect=create_client).start()
        self.provider = GeminiProvider()
        self.addCleanup(self.provider.client.close)

    def respond(self, request):
        self.requests.append(json.loads(request.content))
        self.assertEqual(request.extensions["timeout"]["read"], 30)
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return httpx.Response(200, json=response)

    def answer(self, tools=None):
        return self.provider.answer(
            question="Explain the robot code",
            system_prompt="Use repository evidence.",
            tools=[lookup] if tools is None else tools,
        )

    def test_plain_answer_excludes_thoughts_and_joins_text(self):
        self.responses.append(model_response(
            {"text": "Internal reasoning", "thought": True},
            {"text": " The robot "}, {"text": "drives. "},
        ))
        self.assertEqual(self.answer(), "The robot drives.")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.http_options.retry_options.attempts, 2)

    def test_tool_only_response_is_executed_before_answering(self):
        call = tool_call(path="Robot.java")
        call["functionCall"]["id"] = "call-1"
        call["thoughtSignature"] = "c2lnbmF0dXJl"
        self.responses.extend([
            model_response({"text": "I'll check the code."}, call),
            model_response({"text": "Robot.java drives the robot."}),
        ])
        self.assertEqual(self.answer(), "Robot.java drives the robot.")
        history = self.requests[1]["contents"]
        self.assertEqual(history[1]["parts"][1], call)
        result = history[2]["parts"][0]["functionResponse"]
        self.assertEqual(result["id"], "call-1")
        self.assertEqual(result["response"]["result"]["path"], "Robot.java")

    def test_round_limit_reserves_final_answer_with_all_results(self):
        for index in range(4):
            self.responses.append(model_response(tool_call(path=f"File{index}.java")))
        self.responses.append(model_response({"text": "Answer based on four files."}))
        self.assertEqual(self.answer(), "Answer based on four files.")
        self.assertEqual(len(self.requests), 5)
        final_request = self.requests[-1]
        self.assertEqual(
            final_request["toolConfig"]["functionCallingConfig"]["mode"], "NONE",
        )
        paths = [
            part["functionResponse"]["response"]["result"]["path"]
            for content in final_request["contents"] for part in content["parts"]
            if "functionResponse" in part
        ]
        self.assertEqual(paths, [f"File{i}.java" for i in range(4)])

    def test_independent_tools_run_concurrently_in_original_order(self):
        barrier = Barrier(2, timeout=5)

        def read(path: str) -> dict:
            """Read a source file."""
            barrier.wait()
            return {"path": path}

        self.responses.extend([
            model_response(tool_call("read", path="A"), tool_call("read", path="B")),
            model_response({"text": "Both files read."}),
        ])
        self.assertEqual(self.answer(tools=[read]), "Both files read.")
        results = self.requests[1]["contents"][-1]["parts"]
        self.assertEqual(
            [part["functionResponse"]["response"] for part in results],
            [{"result": {"path": "A"}}, {"result": {"path": "B"}}],
        )

    def test_call_budget_limits_large_batches_and_reports_unexecuted_calls(self):
        called = []

        def read(path: str) -> dict:
            """Read a source file."""
            called.append(path)
            return {"path": path}

        self.responses.extend([
            model_response(*(tool_call("read", path=str(i)) for i in range(14))),
            model_response({"text": "Partial answer."}),
        ])
        self.assertEqual(self.answer(tools=[read]), "Partial answer.")
        self.assertEqual(len(called), 12)
        final_request = self.requests[1]
        results = final_request["contents"][-1]["parts"]
        self.assertEqual(len(results), 14)
        self.assertIn("error", results[-1]["functionResponse"]["response"])
        self.assertEqual(
            final_request["toolConfig"]["functionCallingConfig"]["mode"], "NONE",
        )

    def test_unknown_tool_and_invalid_arguments_return_errors_to_model(self):
        self.responses.extend([
            model_response(tool_call("missing"), tool_call()),
            model_response({"text": "I couldn't verify that."}),
        ])
        with self.assertLogs("llm.gemini", level="ERROR"):
            self.assertEqual(self.answer(), "I couldn't verify that.")
        results = self.requests[1]["contents"][-1]["parts"]
        self.assertTrue(all("error" in part["functionResponse"]["response"] for part in results))

    def test_tool_timeout_can_be_explained_by_model(self):
        def unavailable(path: str) -> dict:
            """Read a source file."""
            raise TimeoutError("GitHub did not respond")

        self.responses.extend([
            model_response(tool_call("unavailable", path="Robot.java")),
            model_response({"text": "The repository lookup timed out."}),
        ])
        with self.assertLogs("llm.gemini", level="ERROR"):
            self.assertEqual(self.answer(tools=[unavailable]), "The repository lookup timed out.")
        result = self.requests[1]["contents"][-1]["parts"][0]
        self.assertIn("TimeoutError", result["functionResponse"]["response"]["error"])

    def test_empty_blocked_and_thought_only_responses_raise(self):
        for response in (
            {},
            {"promptFeedback": {"blockReason": "SAFETY"}},
            model_response(),
            model_response({"text": "  "}),
            model_response({"text": "Thinking only", "thought": True}),
        ):
            with self.subTest(response=response):
                self.responses.append(response)
                with self.assertRaisesRegex(RuntimeError, "no answer text"):
                    self.answer()

    def test_model_timeout_propagates_to_listener(self):
        self.responses.extend([
            httpx.ReadTimeout("Model took too long"),
            httpx.ReadTimeout("Retry also timed out"),
        ])
        with self.assertRaises(httpx.ReadTimeout):
            self.answer()
        self.assertEqual(len(self.requests), 2)

    def test_transient_timeout_can_recover_on_one_retry(self):
        self.responses.extend([
            httpx.ReadTimeout("Temporary timeout"),
            model_response({"text": "Recovered answer"}),
        ])
        self.assertEqual(self.answer(), "Recovered answer")
        self.assertEqual(len(self.requests), 2)

    def test_final_turn_does_not_execute_more_tools(self):
        self.provider.max_tool_rounds = 1
        self.responses.extend([
            model_response(tool_call(path="Robot.java")),
            model_response(tool_call(path="Other.java")),
        ])
        with self.assertRaisesRegex(RuntimeError, "final answer turn"):
            self.answer()
        self.assertEqual(len(self.requests), 2)

    def test_mentions_have_separate_chat_histories(self):
        self.responses.extend([
            model_response({"text": "First answer"}),
            model_response({"text": "Second answer"}),
        ])
        self.answer()
        self.answer()
        self.assertEqual(len(self.requests[1]["contents"]), 1)


if __name__ == "__main__":
    unittest.main()
