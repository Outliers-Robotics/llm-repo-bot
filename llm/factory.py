import os

from .base import LLMProvider
from .gemini import GeminiProvider


def get_llm() -> LLMProvider:

    provider = os.getenv(
        "LLM_PROVIDER",
        "gemini",
    ).lower()

    if provider == "gemini":
        return GeminiProvider()

    raise ValueError(
        f"Unknown LLM provider: {provider}"
    )
