"""Gemini model provider implementation using the Google GenAI SDK."""

from collections.abc import Callable
import json
import logging
import os
from time import monotonic

from google import genai
from google.genai import types

from config import positive_int_env
from llm.base import (
    BaseResearchProvider,
    FINAL_ANSWER_INSTRUCTION,
    GRAPH_TURN_INSTRUCTION,
    INCOMPLETE_ANSWER,
    ModelTurn,
    ProviderSession,
    ToolCall,
    execute_tool,
)


logger = logging.getLogger(__name__)

# Re-export for backward compatibility with existing tests
_run_tool = execute_tool


def _answer_text(response) -> str:
    candidate = response.candidates[0] if response.candidates else None
    parts = candidate.content.parts if candidate and candidate.content else []
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


def _to_model_turn(response) -> ModelTurn:
    calls = response.function_calls or []
    if not calls:
        return ModelTurn(text=_answer_text(response))
    return ModelTurn(
        tool_calls=[
            ToolCall(
                name=call.name,
                args=call.args or {},
                call_id=call.id,
                native_call=call,
            )
            for call in calls
        ]
    )


class GeminiChatSession(ProviderSession):
    def __init__(
        self,
        client: genai.Client,
        model: str,
        system_prompt: str,
        tools: list[Callable],
        history: list[dict[str, str]] | None = None,
    ):
        self.client = client
        self.model = model
        self.system_prompt = system_prompt
        self.tools = tools
        self.function_map = {tool.__name__: tool for tool in tools}
        self.base_config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=tools,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True,
            ),
        )
        gemini_history = None
        if history:
            gemini_history = [
                types.Content(
                    role="model" if item["role"] == "assistant" else "user",
                    parts=[types.Part.from_text(text=item["content"])],
                )
                for item in history
            ]
        self.chat = self.client.chats.create(model=self.model, history=gemini_history)
        self.round_index = 0

    def _build_config(self, read_paths: list[str] | None = None) -> types.GenerateContentConfig:
        if read_paths and "read_file" in self.function_map:
            return self.base_config.model_copy(update={
                "tools": [types.Tool(function_declarations=[
                    types.FunctionDeclaration(
                        name="read_file",
                        description=(
                            "Read a matching repository file. Read all relevant "
                            "files in this turn using parallel calls. Select "
                            "exact paths from the search results; do not guess."
                        ),
                        parameters=types.Schema(
                            type="OBJECT",
                            properties={"path": types.Schema(
                                type="STRING", enum=read_paths,
                            )},
                            required=["path"],
                        ),
                    ),
                ])],
                "tool_config": types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(
                        mode="ANY", allowed_function_names=["read_file"],
                    ),
                ),
            })
        return self.base_config

    def send_initial(
        self,
        question: str,
        read_paths: list[str] | None = None,
    ) -> ModelTurn:
        self.round_index += 1
        config = self._build_config(read_paths)
        started = monotonic()
        try:
            response = self.chat.send_message(question, config=config)
            return _to_model_turn(response)
        finally:
            logger.info(
                "Gemini model=%s round=%d final=False elapsed=%.2fs",
                self.model, self.round_index, monotonic() - started,
            )

    def send_tool_results(
        self,
        calls: list[ToolCall],
        results: list[dict],
        read_paths: list[str] | None = None,
    ) -> ModelTurn:
        self.round_index += 1
        config = self._build_config(read_paths)
        message = [
            types.Part(function_response=types.FunctionResponse(
                name=call.name or "unknown",
                id=call.call_id,
                response=result,
            ))
            for call, result in zip(calls, results)
        ]
        started = monotonic()
        try:
            response = self.chat.send_message(message, config=config)
            return _to_model_turn(response)
        finally:
            logger.info(
                "Gemini model=%s round=%d final=False elapsed=%.2fs",
                self.model, self.round_index, monotonic() - started,
            )

    def request_graph_turn(
        self,
        calls: list[ToolCall],
        results: list[dict],
        system_prompt: str,
    ) -> ModelTurn:
        message = [
            types.Part(function_response=types.FunctionResponse(
                name=call.name or "unknown",
                id=call.call_id,
                response=result,
            ))
            for call, result in zip(calls, results)
        ]
        plot_config = self.base_config.model_copy(update={
            "tools": [self.function_map["plot_lookup_tables"]],
            "tool_config": types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode="AUTO", allowed_function_names=["plot_lookup_tables"],
                ),
            ),
            "system_instruction": system_prompt + GRAPH_TURN_INSTRUCTION,
        })
        started = monotonic()
        try:
            response = self.chat.send_message(message, config=plot_config)
            return _to_model_turn(response)
        finally:
            logger.info("Gemini graph turn elapsed=%.2fs", monotonic() - started)


class GeminiProvider(BaseResearchProvider):
    def __init__(self):
        super().__init__(
            max_tool_rounds=positive_int_env("GEMINI_MAX_TOOL_ROUNDS", 4),
            logger=logger,
        )
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

    def create_session(
        self,
        system_prompt: str,
        tools: list[Callable],
        history: list[dict[str, str]] | None = None,
    ) -> ProviderSession:
        return GeminiChatSession(
            client=self.client,
            model=self.model,
            system_prompt=system_prompt,
            tools=tools,
            history=history,
        )

    def synthesize_final_answer(
        self,
        question: str,
        system_prompt: str,
        evidence: list[dict],
        round_number: int,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        started = monotonic()
        payload = {
            "question": question,
            "repository_lookups": evidence,
        }
        if history:
            payload["conversation_history"] = history
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=json.dumps(payload),
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt + FINAL_ANSWER_INSTRUCTION,
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True,
                    ),
                ),
            )
            if response.function_calls:
                logger.warning("Gemini requested tools during text-only synthesis")
                return INCOMPLETE_ANSWER
            return _answer_text(response)
        except Exception:
            logger.exception("Gemini could not synthesize a final repository answer")
            return INCOMPLETE_ANSWER
        finally:
            logger.info(
                "Gemini model=%s round=%d final=True elapsed=%.2fs",
                self.model, round_number, monotonic() - started,
            )
