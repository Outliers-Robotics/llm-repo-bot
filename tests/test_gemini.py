import json
import os
import unittest
from collections import deque
from threading import Barrier
from unittest.mock import patch

import httpx
from google import genai

from llm.base import normalize_history
from llm.gemini import GeminiProvider, INCOMPLETE_ANSWER
from tools.plots import PlotSession


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
        call = tool_call(path="Robot.cpp")
        call["functionCall"]["id"] = "call-1"
        call["thoughtSignature"] = "c2lnbmF0dXJl"
        self.responses.extend([
            model_response({"text": "I'll check the code."}, call),
            model_response({"text": "Robot.cpp drives the robot."}),
        ])
        self.assertEqual(self.answer(), "Robot.cpp drives the robot.")
        history = self.requests[1]["contents"]
        self.assertEqual(history[1]["parts"][1], call)
        result = history[2]["parts"][0]["functionResponse"]
        self.assertEqual(result["id"], "call-1")
        self.assertEqual(result["response"]["result"]["path"], "Robot.cpp")

    def test_round_limit_reserves_final_answer_with_all_results(self):
        for index in range(4):
            self.responses.append(model_response(tool_call(path=f"File{index}.cpp")))
        self.responses.append(model_response({"text": "Answer based on four files."}))
        self.assertEqual(self.answer(), "Answer based on four files.")
        self.assertEqual(len(self.requests), 5)
        final_request = self.requests[-1]
        self.assertNotIn("tools", final_request)
        self.assertNotIn("toolConfig", final_request)
        self.assertEqual(len(final_request["contents"]), 1)
        parts = final_request["contents"][0]["parts"]
        self.assertEqual(list(parts[0]), ["text"])
        evidence = json.loads(parts[0]["text"])
        self.assertEqual(evidence["question"], "Explain the robot code")
        paths = [
            lookup["result"]["path"] for lookup in evidence["repository_lookups"]
        ]
        self.assertEqual(paths, [f"File{i}.cpp" for i in range(4)])

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
        evidence = json.loads(final_request["contents"][0]["parts"][0]["text"])
        results = evidence["repository_lookups"]
        self.assertEqual(len(results), 12)
        self.assertTrue(all("result" in result for result in results))
        self.assertNotIn("tools", final_request)

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
            model_response(tool_call("unavailable", path="Robot.cpp")),
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
            model_response(tool_call(path="Robot.cpp")),
            model_response(tool_call(path="Other.cpp")),
        ])
        with self.assertLogs("llm.gemini", level="WARNING"):
            self.assertEqual(self.answer(), INCOMPLETE_ANSWER)
        self.assertEqual(len(self.requests), 2)

    def test_four_searches_then_unexpected_tool_request_returns_readable_fallback(self):
        def search_repo(query: str) -> dict:
            """Search for robot code."""
            return {"matches": []}

        self.responses.extend(
            model_response(tool_call("search_repo", query=query))
            for query in ("shooter current", "shooter motor", "shooter", "current limit")
        )
        self.responses.append(model_response(tool_call("read_file", path="Shooter.cpp")))
        with self.assertLogs("llm.gemini", level="WARNING"):
            answer = self.provider.answer(
                question="What is the current shooter current limits for all motors?",
                system_prompt="Use source code to verify motor current limits.",
                tools=[search_repo],
            )
        self.assertEqual(answer, INCOMPLETE_ANSWER)
        self.assertEqual(len(self.requests), 5)
        final_request = self.requests[-1]
        self.assertNotIn("tools", final_request)
        evidence = json.loads(final_request["contents"][0]["parts"][0]["text"])
        self.assertEqual(len(evidence["repository_lookups"]), 4)
        self.assertTrue(all(item["result"]["matches"] == [] for item in evidence["repository_lookups"]))

    def test_empty_or_failed_final_response_returns_readable_fallback(self):
        self.provider.max_tool_rounds = 1
        for responses in ([{}], [httpx.ReadTimeout("timeout"), httpx.ReadTimeout("retry timeout")]):
            with self.subTest(responses=responses):
                self.responses.append(model_response(tool_call(path="Robot.cpp")))
                self.responses.extend(responses)
                with self.assertLogs("llm.gemini", level="ERROR"):
                    self.assertEqual(self.answer(), INCOMPLETE_ANSWER)

    def test_duplicate_lookup_reuses_evidence_and_ends_search_loop_early(self):
        called = []

        def read(path: str) -> dict:
            """Read a source file."""
            called.append(path)
            return {"path": path, "content": "source"}

        self.responses.extend([
            model_response(tool_call("read", path="A"), tool_call("read", path="A")),
            model_response(tool_call("read", path="A")),
            model_response({"text": "Answer from file A."}),
        ])
        self.assertEqual(self.answer(tools=[read]), "Answer from file A.")
        self.assertEqual(called, ["A"])
        # Both initial calls get responses, but only one lookup was executed.
        self.assertEqual(len(self.requests[1]["contents"][-1]["parts"]), 2)
        self.assertEqual(len(self.requests), 3)
        evidence = json.loads(self.requests[-1]["contents"][0]["parts"][0]["text"])
        self.assertEqual(len(evidence["repository_lookups"]), 1)

    def test_shooter_question_can_search_and_read_source_with_tools_enabled(self):
        def search_repo(query: str) -> dict:
            """Search for robot code."""
            return {"matches": [{"path": "Shooter.cpp"}]}

        def read_file(path: str) -> dict:
            """Read a source file."""
            return {"path": path, "content": "// Synthetic fixture, not real robot settings\nlimit = 25;"}

        self.responses.extend([
            model_response(tool_call("search_repo", query="shooter")),
            model_response(tool_call("read_file", path="Shooter.cpp")),
            model_response({"text": "The fixture sets the current limit to 25 A."}),
        ])
        answer = self.provider.answer(
            question="What is the current shooter current limits for all motors?",
            system_prompt="Use source code to verify motor current limits.",
            tools=[search_repo, read_file],
        )
        self.assertIn("25 A", answer)
        self.assertTrue(all(request["tools"] for request in self.requests))
        read_request = self.requests[1]
        self.assertEqual(
            [declaration["name"] for tool in read_request["tools"]
             for declaration in tool["functionDeclarations"]],
            ["read_file"],
        )
        self.assertEqual(
            read_request["toolConfig"]["functionCallingConfig"],
            {"mode": "ANY", "allowedFunctionNames": ["read_file"]},
        )
        declaration = read_request["tools"][0]["functionDeclarations"][0]
        self.assertEqual(declaration["parameters"]["properties"]["path"]["enum"], ["Shooter.cpp"])
        self.assertNotIn("toolConfig", self.requests[2])
        last_result = self.requests[-1]["contents"][-1]["parts"][0]["functionResponse"]
        self.assertEqual(last_result["name"], "read_file")
        self.assertIn("limit = 25", last_result["response"]["result"]["content"])

    def test_read_step_rejects_invented_paths_and_can_recover(self):
        paths_read = []

        def search_repo(query: str) -> dict:
            """Search robot code."""
            return {"matches": [{"path": "src/subsystem/flywheel/FlywheelConstants.h"}]}

        def read_file(path: str) -> dict:
            """Read robot code."""
            paths_read.append(path)
            return {"path": path, "content": "source"}

        actual_path = "src/subsystem/flywheel/FlywheelConstants.h"
        self.responses.extend([
            model_response(tool_call("search_repo", query="shooter")),
            model_response(tool_call("read_file", path="src/subsystems/Shooter.h")),
            model_response(tool_call("read_file", path=actual_path)),
            model_response({"text": "Answer from the actual source."}),
        ])
        self.assertEqual(self.answer(tools=[search_repo, read_file]), "Answer from the actual source.")
        self.assertEqual(paths_read, [actual_path])
        error = self.requests[2]["contents"][-1]["parts"][0]["functionResponse"]["response"]
        self.assertIn("error", error)
        self.assertEqual(error["available_paths"], [actual_path])
        self.assertIn("toolConfig", self.requests[2])
        self.assertNotIn("toolConfig", self.requests[3])

    def test_mentions_have_separate_chat_histories(self):
        self.responses.extend([
            model_response({"text": "First answer"}),
            model_response({"text": "Second answer"}),
        ])
        self.answer()
        self.answer()
        self.assertEqual(len(self.requests[1]["contents"]), 1)

    def test_plot_tool_schema_and_read_then_render_work_through_real_sdk(self):
        session = PlotSession(lambda path: {
            "content": "double offset = 30; auto flyMap = {{1, 1200 + offset}, {2, 1300 + offset}};",
            "url": "https://github.com/team/robot/blob/main/Shot.cpp",
        })
        self.responses.extend([
            model_response(tool_call("read_file", path="Shot.cpp")),
            model_response(tool_call(
                "plot_lookup_tables", title="RPM table", x_label="Distance (m)",
                y_label="Speed (RPM)", paths=["Shot.cpp"], tables=["flyMap"], labels=["Flywheel"],
            )),
            model_response({"text": "The graph shows the configured RPM."}),
        ])
        self.assertIn("configured RPM", self.answer(tools=[session.read_file, session.plot_lookup_tables]))
        self.assertEqual(len(session.artifacts), 1)
        result = self.requests[2]["contents"][-1]["parts"][0]["functionResponse"]["response"]["result"]
        self.assertEqual(result["series"][0]["points"], [[1, 1230], [2, 1330]])
        self.assertNotIn("png", result)  # Binary images never enter the model context.

    def test_graph_gets_local_render_turn_after_repository_round_budget(self):
        self.provider.max_tool_rounds = 1
        session = PlotSession(lambda path: {"content": "auto flyMap = {{1, 1200}, {2, 1300}};"})
        self.responses.extend([
            model_response(tool_call("read_file", path="Shot.cpp")),
            model_response(tool_call(
                "plot_lookup_tables", title="RPM table", x_label="Distance (m)",
                y_label="Speed (RPM)", paths=["Shot.cpp"], tables=["flyMap"], labels=["Flywheel"],
            )),
            model_response({"text": "The graph is ready."}),
        ])
        self.assertEqual(self.provider.answer(
            question="Graph the flywheel RPM table", system_prompt="Use source.",
            tools=[session.read_file, session.plot_lookup_tables],
        ), "The graph is ready.")
        declarations = [d["name"] for tool in self.requests[1]["tools"] for d in tool["functionDeclarations"]]
        self.assertEqual(declarations, ["plot_lookup_tables"])
        self.assertNotIn("tools", self.requests[-1])
        evidence = json.loads(self.requests[-1]["contents"][0]["parts"][0]["text"])["repository_lookups"]
        self.assertEqual(evidence[-1]["result"]["status"], "graph_ready")

    def test_non_graph_answer_does_not_get_extra_render_turn(self):
        self.provider.max_tool_rounds = 1
        session = PlotSession(lambda path: {"content": "source"})
        self.responses.extend([
            model_response(tool_call("read_file", path="Shot.cpp")),
            model_response({"text": "Text answer."}),
        ])
        self.assertEqual(self.answer(tools=[session.read_file, session.plot_lookup_tables]), "Text answer.")
        self.assertEqual(len(self.requests), 2)
        self.assertNotIn("tools", self.requests[-1])

    def test_graph_request_triggers_render_turn_even_if_model_returns_text_after_read(self):
        session = PlotSession(lambda path: {"content": "auto flyMap = {{1, 1200}, {2, 1300}};"})
        self.responses.extend([
            model_response(tool_call("read_file", path="Shot.cpp")),
            model_response({"text": "I read the file and found the tables."}),
            model_response(tool_call(
                "plot_lookup_tables", title="RPM table", x_label="Distance (m)",
                y_label="Speed (RPM)", paths=["Shot.cpp"], tables=["flyMap"], labels=["Flywheel"],
            )),
            model_response({"text": "The graph has been generated."}),
        ])
        answer = self.provider.answer(
            question="Plot the flywheel RPM table", system_prompt="Use source.",
            tools=[session.read_file, session.plot_lookup_tables],
        )
        self.assertIn("The graph has been generated.", answer)
        self.assertEqual(len(session.artifacts), 1)
        self.assertEqual(session.artifacts[0].filename, "lookup-tables-1.png")

    def test_plot_data_called_during_graph_turn(self):
        session = PlotSession(lambda path: {"content": ""})
        self.responses.extend([
            model_response({"text": "I will plot the mathematical trajectory."}),
            model_response(tool_call(
                "plot_data",
                title="Ballistic Trajectory",
                x_label="Distance (m)",
                y_label="Height (m)",
                series=[{"label": "Arc", "points": [[0, 0], [1, 2], [2, 0]], "source": "Physics formula"}],
            )),
            model_response({"text": "The trajectory graph is generated."}),
        ])
        answer = self.provider.answer(
            question="Plot the ballistic trajectory curve",
            system_prompt="Use physics.",
            tools=[session.read_file, session.plot_lookup_tables, session.plot_data],
        )
        self.assertIn("The trajectory graph is generated.", answer)
        self.assertEqual(len(session.artifacts), 1)
        self.assertEqual(session.artifacts[0].filename, "plot-1.png")
        declarations = [d["name"] for tool in self.requests[1]["tools"] for d in tool["functionDeclarations"]]
        self.assertEqual(declarations, ["plot_lookup_tables", "plot_data"])

    def test_history_is_passed_to_chat_session_as_model_and_user_turns(self):
        self.responses.append(model_response({"text": "The ratio is 6.75:1."}))
        answer = self.provider.answer(
            question="What gear ratio?",
            system_prompt="Use repository evidence.",
            tools=[lookup],
            history=[
                {"role": "user", "content": "Explain drive"},
                {"role": "assistant", "content": "Drive uses 4 swerve modules."},
            ],
        )
        self.assertEqual(answer, "The ratio is 6.75:1.")
        self.assertEqual(len(self.requests), 1)
        contents = self.requests[0]["contents"]
        self.assertEqual(len(contents), 3)
        self.assertEqual(contents[0]["role"], "user")
        self.assertEqual(contents[0]["parts"][0]["text"], "Explain drive")
        self.assertEqual(contents[1]["role"], "model")
        self.assertEqual(contents[1]["parts"][0]["text"], "Drive uses 4 swerve modules.")
        self.assertEqual(contents[2]["role"], "user")
        self.assertEqual(contents[2]["parts"][0]["text"], "What gear ratio?")

    def test_history_is_included_in_final_synthesis(self):
        self.provider.max_tool_rounds = 1
        self.responses.extend([
            model_response(tool_call("lookup", path="Drive.cpp")),
            model_response({"text": "Drive synthesis answer."}),
        ])
        history = [
            {"role": "user", "content": "Explain drive"},
            {"role": "assistant", "content": "Drive uses 4 swerve modules."},
        ]
        answer = self.provider.answer(
            question="What gear ratio?",
            system_prompt="Use repository evidence.",
            tools=[lookup],
            history=history,
        )
        self.assertEqual(answer, "Drive synthesis answer.")
        self.assertEqual(len(self.requests), 2)
        final_request = self.requests[-1]
        payload = json.loads(final_request["contents"][0]["parts"][0]["text"])
        self.assertEqual(payload["question"], "What gear ratio?")
        self.assertEqual(payload["conversation_history"], history)


class NormalizeHistoryTests(unittest.TestCase):
    def test_empty_or_none_history_returns_empty_list_and_original_question(self):
        for empty in (None, [], [{"role": "user", "content": "  "}]):
            with self.subTest(empty=empty):
                hist, q = normalize_history(empty, "What is drive?")
                self.assertEqual(hist, [])
                self.assertEqual(q, "What is drive?")

    def test_consecutive_messages_with_same_role_are_merged(self):
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "user", "content": "How does shooter work?"},
            {"role": "assistant", "content": "Part 1"},
            {"role": "assistant", "content": "Part 2"},
        ]
        hist, q = normalize_history(history, "What is the speed?")
        self.assertEqual(hist, [
            {"role": "user", "content": "Hello\n\nHow does shooter work?"},
            {"role": "assistant", "content": "Part 1\n\nPart 2"},
        ])
        self.assertEqual(q, "What is the speed?")

    def test_trailing_user_message_is_prepended_to_question(self):
        history = [
            {"role": "user", "content": "Question 1"},
            {"role": "assistant", "content": "Answer 1"},
            {"role": "user", "content": "Question 2 with no reply"},
        ]
        hist, q = normalize_history(history, "Current question")
        self.assertEqual(hist, [
            {"role": "user", "content": "Question 1"},
            {"role": "assistant", "content": "Answer 1"},
        ])
        self.assertEqual(q, "Question 2 with no reply\n\nCurrent question")

    def test_only_user_messages_in_history_are_folded_into_question(self):
        history = [
            {"role": "user", "content": "Context comment 1"},
            {"role": "user", "content": "Context comment 2"},
        ]
        hist, q = normalize_history(history, "Current question")
        self.assertEqual(hist, [])
        self.assertEqual(q, "Context comment 1\n\nContext comment 2\n\nCurrent question")

    def test_leading_assistant_message_context_is_preserved(self):
        history = [
            {"role": "assistant", "content": "Announcement: Code updated"},
            {"role": "user", "content": "What changed?"},
            {"role": "assistant", "content": "Shooter updated."},
        ]
        hist, q = normalize_history(history, "What is the speed?")
        self.assertEqual(hist[0]["role"], "user")
        self.assertIn("Announcement: Code updated", hist[0]["content"])
        self.assertIn("What changed?", hist[0]["content"])
        self.assertEqual(hist[1]["role"], "assistant")
        self.assertEqual(hist[1]["content"], "Shooter updated.")

    def test_max_history_turns_truncates_older_turns_cleanly(self):
        history = []
        for i in range(15):
            history.extend([
                {"role": "user", "content": f"Q{i}"},
                {"role": "assistant", "content": f"A{i}"},
            ])
        hist, q = normalize_history(history, "Latest question", max_history_turns=6)
        self.assertEqual(len(hist), 6)
        self.assertEqual(hist[0]["role"], "user")
        self.assertEqual(hist[-1]["role"], "assistant")
        self.assertEqual(hist[-1]["content"], "A14")


if __name__ == "__main__":
    unittest.main()
