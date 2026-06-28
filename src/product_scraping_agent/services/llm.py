"""Minimal Azure OpenAI LLM service used by claims and vision stages."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from openai import AzureOpenAI

from ..config import Config, get_config
from ..log import logger


@dataclass
class LLMConfig:
    api_key: str
    api_version: str
    endpoint: str
    deployment: str
    consumer_id: str = ""
    max_tokens: int = 4096
    temperature: float = 0.0
    connect_timeout: float = 15.0
    read_timeout: float = 120.0
    max_retries: int = 4

    @classmethod
    def from_global(cls, cfg: Config | None = None) -> "LLMConfig":
        cfg = cfg or get_config()
        return cls(
            api_key=cfg.llm_api_key,
            api_version=cfg.llm_api_version,
            endpoint=cfg.llm_endpoint,
            deployment=cfg.llm_deployment,
            consumer_id=cfg.llm_consumer_id,
            max_tokens=cfg.llm_max_tokens,
            temperature=cfg.llm_temperature,
            connect_timeout=cfg.llm_connect_timeout,
            read_timeout=cfg.llm_read_timeout,
            max_retries=cfg.llm_max_retries,
        )

    @property
    def default_headers(self) -> dict[str, str]:
        return {"X-NIQ-CIS-Consumer": self.consumer_id} if self.consumer_id else {}


@dataclass
class LLMResponse:
    content: str
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""
    finish_reason: str = ""
    raw: Any = None


_DEFAULT_SERVICE: "LLMService | None" = None


def get_llm_service(config: LLMConfig | None = None) -> "LLMService":
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None or config is not None:
        _DEFAULT_SERVICE = LLMService(config)
    return _DEFAULT_SERVICE


class LLMService:
    """Thin wrapper around AzureOpenAI chat completions with image support."""

    _cumulative_prompt: int = 0
    _cumulative_completion: int = 0
    _cumulative_calls: int = 0

    def __init__(self, config: LLMConfig | None = None) -> None:
        self.config = config or LLMConfig.from_global()
        if not self.config.api_key or not self.config.endpoint:
            raise RuntimeError(
                "LLM is enabled, but PCA_LLM_API_KEY or PCA_LLM_ENDPOINT is missing. "
                "Set PCA_LLM_ENABLED=false to skip LLM synthesis."
            )
        self._client = AzureOpenAI(
            api_key=self.config.api_key,
            api_version=self.config.api_version,
            azure_endpoint=self.config.endpoint,
            azure_deployment=self.config.deployment,
            default_headers=self.config.default_headers,
            max_retries=self.config.max_retries,
            timeout=httpx.Timeout(
                connect=self.config.connect_timeout,
                read=self.config.read_timeout,
                write=self.config.read_timeout,
                pool=self.config.read_timeout,
            ),
        )

    def predict(
        self,
        text: str,
        *,
        system_prompt: str | None = None,
        image: str | bytes | None = None,
        images: list[str | bytes] | None = None,
        image_detail: str = "auto",
        max_tokens: int | None = None,
        temperature: float | None = None,
        response_format: dict[str, Any] | None = None,
        purpose: str = "",
    ) -> LLMResponse:
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if images:
            user_content = self._build_multi_image_content(text, images, image_detail=image_detail)
        else:
            user_content = self._build_user_content(text, image, image_detail=image_detail)
        messages.append({"role": "user", "content": user_content})
        return self._call(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format=response_format,
            purpose=purpose,
        )

    def _call(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        response_format: dict[str, Any] | None = None,
        purpose: str = "",
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.config.deployment,
            "messages": messages,
            "max_tokens": max_tokens or self.config.max_tokens,
            "temperature": self.config.temperature if temperature is None else temperature,
        }
        if response_format:
            kwargs["response_format"] = response_format
        try:
            completion = self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            body = getattr(getattr(exc, "response", None), "text", None)
            if body:
                logger.error("LLM [{}] failed: {} — body={}", purpose, exc, body[:1000])
            else:
                logger.exception("LLM [{}] failed", purpose)
            raise

        choice = completion.choices[0]
        usage: dict[str, int] = {}
        if completion.usage:
            usage = {
                "prompt_tokens": completion.usage.prompt_tokens,
                "completion_tokens": completion.usage.completion_tokens,
                "total_tokens": completion.usage.total_tokens,
            }
            LLMService._cumulative_prompt += completion.usage.prompt_tokens
            LLMService._cumulative_completion += completion.usage.completion_tokens
            LLMService._cumulative_calls += 1
            logger.info(
                "LLM [{}] prompt={} completion={} total={}",
                purpose,
                completion.usage.prompt_tokens,
                completion.usage.completion_tokens,
                completion.usage.total_tokens,
            )
        return LLMResponse(
            content=choice.message.content or "",
            usage=usage,
            model=completion.model or "",
            finish_reason=choice.finish_reason or "",
            raw=completion,
        )

    def _build_user_content(
        self,
        text: str,
        image: str | bytes | None,
        *,
        image_detail: str,
    ) -> str | list[dict[str, Any]]:
        if image is None:
            return text
        return [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": self._resolve_image(image), "detail": image_detail}},
        ]

    def _build_multi_image_content(
        self,
        text: str,
        images: list[str | bytes],
        *,
        image_detail: str,
    ) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for img in images:
            content.append({
                "type": "image_url",
                "image_url": {"url": self._resolve_image(img), "detail": image_detail},
            })
        return content

    @staticmethod
    def _resolve_image(image: str | bytes) -> str:
        if isinstance(image, bytes):
            return LLMService._bytes_to_data_url(image)
        if image.startswith(("http://", "https://", "data:")):
            return image
        path = Path(image)
        if path.is_file():
            return LLMService._bytes_to_data_url(path.read_bytes(), path.suffix)
        raise FileNotFoundError(f"Image file not found: {image}")

    @staticmethod
    def _bytes_to_data_url(data: bytes, suffix: str = ".png") -> str:
        mime_map = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
        }
        mime = mime_map.get(suffix.lower(), "image/png")
        b64 = base64.b64encode(data).decode("utf-8")
        return f"data:{mime};base64,{b64}"

    @classmethod
    def token_summary(cls) -> str:
        total = cls._cumulative_prompt + cls._cumulative_completion
        return (
            f"LLM totals: {cls._cumulative_calls} calls | "
            f"prompt={cls._cumulative_prompt:,} completion={cls._cumulative_completion:,} "
            f"total={total:,} tokens"
        )


__all__ = ["LLMConfig", "LLMResponse", "LLMService", "get_llm_service"]
