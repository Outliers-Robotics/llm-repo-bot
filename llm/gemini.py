import logging
import os
from concurrent.futures import ThreadPoolExecutor
from time import monotonic

from google import genai
from google.genai import types

from config import positive_int_env
from llm.base import LLMProvider


logger = logging.getLogger(__name__)
MAX_TOOL_CALLS = 12
TOOL_WORKERS = 4
FINAL_ANSWER_INSTRUCTION = """
The repository lookup budget is now exhausted. Give your final answer using
only evidence already returned by the tools. State what you could not verify
and ask for a narrower question if necessary. Do not invent missing facts.
"""


class GeminiProvider(LLMProvider):
    def __init__(self):
        self.max_tool_rounds = positive_int_env("GEMINI_MAX_TOOL_ROUNDS", 4)
        self.client = genai.Client(
            api_key=os.environ["GEMINI_API_KEY"],
            http_options=types.HttpOptions(
                timeout=positive_int_env("GEMINI_TIMEOUT_SECONDS", 30) * 1000,
                retry_options=types.HttpRetryOptions(
                    attempts=2,
                    initial_delay=1,
                    max_delay=2,
                ),
            ),
        )
        self.model = os.getenv(
            "GEMINI_MODEL",
            "gemini-3.8-flash",
        )

    def answer(self, *, question, system_prompt, tools) -> str:
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=tools,
            # Own the loop so the last tool result always gets an answer turn.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True,
            ),
        )
        final_config = config.model_copy(update={
            "system_instruction": system_prompt + FINAL_ANSWER_INSTRUCTION,
            "tool_config": types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="NONE"),
            ),
        })
        # A separate chat per mention keeps concurrent requests' histories apart.
        # The SDK preserves the original model parts and thought signatures.
        chat = self.client.chats.create(model=self.model)
        function_map = {tool.__name__: tool for tool in tools}
        message = question
        tool_calls = 0

        with ThreadPoolExecutor(max_workers=TOOL_WORKERS) as executor:
            for round_index in range(self.max_tool_rounds + 1):
                final_turn = (
                    round_index == self.max_tool_rounds
                    or tool_calls >= MAX_TOOL_CALLS
                )
                started = monotonic()
                try:
                    response = chat.send_message(
                        message,
                        config=final_config if final_turn else config,
                    )
                finally:
                    logger.info(
                        "Gemini model=%s round=%d final=%s elapsed=%.2fs",
                        self.model, round_index + 1, final_turn,
                        monotonic() - started,
                    )

                calls = response.function_calls
                if not calls:
                    return _answer_text(response)
                if final_turn:
                    raise RuntimeError("Gemini requested tools on the final answer turn")

                allowed = calls[:MAX_TOOL_CALLS - tool_calls]
                # All calls in one model response already have their arguments,
                # so these read-only lookups can run together. Keep result order.
                results = list(executor.map(
                    lambda call: _run_tool(call, function_map), allowed,
                ))
                tool_calls += len(allowed)
                results.extend(
                    {"error": "Repository lookup limit reached; this tool was not run."}
                    for _ in calls[len(allowed):]
                )
                message = [
                    types.Part(function_response=types.FunctionResponse(
                        name=call.name or "unknown",
                        id=call.id,
                        response=result,
                    ))
                    for call, result in zip(calls, results)
                ]


def _run_tool(call, function_map) -> dict:
    started = monotonic()
    try:
        function = function_map.get(call.name)
        if function is None:
            return {"error": f"Unknown repository tool: {call.name}"}
        return {"result": function(**(call.args or {}))}
    except Exception as error:
        logger.exception("Repository tool %s failed", call.name)
        return {
            "error": f"Repository lookup failed ({type(error).__name__}). "
                     "Do not assume facts that this lookup did not verify.",
        }
    finally:
        logger.info("Repository tool=%s elapsed=%.2fs", call.name, monotonic() - started)


def _answer_text(response) -> str:
    candidate = response.candidates[0] if response.candidates else None
    parts = candidate.content.parts if candidate and candidate.content else []
    # Read only answer text, avoiding response.text's warning for non-text parts
    # and excluding thinking text from the Slack message.
    answer = "".join(
        part.text for part in parts or [] if part.text and not part.thought
    ).strip()
    if not answer:
        finish_reason = candidate.finish_reason if candidate else None
        feedback = response.prompt_feedback
        block_reason = feedback.block_reason if feedback else None
        raise RuntimeError(
            f"Gemini returned no answer text "
            f"(finish_reason={finish_reason}, block_reason={block_reason})"
        )
    return answer
