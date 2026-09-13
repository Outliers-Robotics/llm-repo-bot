from abc import ABC, abstractmethod
from collections.abc import Callable


class LLMProvider(ABC):

    @abstractmethod
    def answer(
        self,
        *,
        question,
        system_prompt,
        tools) -> str:
        pass
