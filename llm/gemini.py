from google import genai
from google.genai import types
import os


class GeminiProvider:
    def __init__(self):
        self.client = genai.Client(
            api_key=os.environ["GEMINI_API_KEY"]
        )
        self.model = os.getenv(
            "GEMINI_MODEL",
            "gemini-3.8-flash",
        )

    def answer(self, question, system_prompt, tools):
        chat = self.client.chats.create(
            model=self.model,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=tools,
                automatic_function_calling=
                    types.AutomaticFunctionCallingConfig(
                        maximum_remote_calls=4
                    ),
            ),
        )

        response = chat.send_message(question)

        if response.text:
            return response.text

        if response.function_calls:
            raise RuntimeError(
                f"Gemini returned unfinished function calls: "
                f"{response.function_calls}"
            )

        raise RuntimeError("Gemini returned no text")
