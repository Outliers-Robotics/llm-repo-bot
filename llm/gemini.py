import os

from google import genai
from google.genai import types

from llm.base import LLMProvider

class GeminiProvider(LLMProvider):
    def __init__(self):
        self.client = genai.Client(
            api_key = os.environ["GEMINI_API_KEY"]
        )

        self.model = os.getenv(
            "GEMINI_MODEL",
            "gemini-3.8-flash"
        )

    def answer(
        self,
        *,
        question,
        system_prompt,
        tools) -> str:

        chat = self.client.chats.create(
            model=self.model,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=tools,
            ),
        )

        response = chat.send_message(question)

        return response.text
