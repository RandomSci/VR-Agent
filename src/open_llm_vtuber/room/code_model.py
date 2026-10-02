"""An optional, stronger model used ONLY for writing and repairing code.

Mika and Luna keep chatting with the model in conf.yaml (GPT-4o-mini). When
VR_CODE_MODEL is set (for example gpt-5-mini), the expensive "write the
program" and "repair it" calls go to that model instead. Off by default.

    VR_CODE_MODEL=gpt-5-mini          the model name
    VR_CODE_REASONING=low             for reasoning models: minimal, low,
                                      medium or high (lower is faster)
    VR_CODE_BASE_URL=                 optional, an OpenAI-compatible endpoint
    VR_CODE_API_KEY=                  optional, defaults to OPENAI_API_KEY

If the code model fails, the normal model writes the code instead, so a
build never fails just because of this setting.
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator, Optional

REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")


class CodeModel:
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "",
        reasoning: str = "low",
        client: Any = None,
    ):
        self.model = model
        self.reasoning = (
            reasoning if reasoning in ("minimal", "low", "medium", "high") else "low"
        )
        self._client = client
        self._api_key = api_key
        self._base_url = base_url or None

    @property
    def reasoning_model(self) -> bool:
        return self.model.lower().startswith(REASONING_PREFIXES)

    def request(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if self.reasoning_model:
            kwargs["reasoning_effort"] = self.reasoning
        else:
            kwargs["temperature"] = 0.6
        return kwargs

    async def chat_completion(
        self, messages, system=None, tools=None
    ) -> AsyncIterator[str]:
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=self._api_key, base_url=self._base_url)
        response = await self._client.chat.completions.create(**self.request(messages))
        yield response.choices[0].message.content or ""


def code_model_from_env() -> Optional[CodeModel]:
    model = (os.environ.get("VR_CODE_MODEL") or "").strip()
    key = (
        os.environ.get("VR_CODE_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    ).strip()
    if not model or not key:
        return None
    return CodeModel(
        model,
        key,
        base_url=(os.environ.get("VR_CODE_BASE_URL") or "").strip(),
        reasoning=(os.environ.get("VR_CODE_REASONING") or "low").strip().lower(),
    )
