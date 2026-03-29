"""
LLM Client — thin wrapper with OpenAI → Anthropic fallback for Layer 3.

Usage:
    from layer3_performance.llm_client import call_llm

    text = await call_llm(prompt, max_tokens=700, temperature=0.3)
    # Returns None if both providers are unavailable or fail.
"""
from __future__ import annotations

from typing import Optional

import structlog

logger = structlog.get_logger()

# Anthropic model used for fallback
_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
# OpenAI model used for all Layer 3 tasks
_OPENAI_MODEL = "gpt-4o-mini"


_LLM_TIMEOUT_SECONDS = 45   # max wait for any single LLM call


async def call_llm(
    prompt: str,
    max_tokens: int = 1000,
    temperature: float = 0.3,
    system: str = "You are a helpful assistant.",
) -> Optional[str]:
    """
    Call OpenAI gpt-4o-mini, falling back to Anthropic claude-haiku if
    OpenAI is unavailable or raises an error.

    Each provider attempt is capped at _LLM_TIMEOUT_SECONDS to prevent hangs.
    Returns the raw text content, or None if both providers fail/timeout.
    """
    import asyncio
    from config.settings import settings

    # Try OpenAI first if configured
    if settings.OPENAI_API_KEY:
        try:
            result = await asyncio.wait_for(
                _call_openai(prompt, max_tokens, temperature),
                timeout=_LLM_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("llm_client.openai_timeout", timeout_s=_LLM_TIMEOUT_SECONDS)
            result = None
        if result is not None:
            return result
        logger.warning("llm_client.openai_failed_trying_anthropic")

    # Fallback: Anthropic
    if settings.ANTHROPIC_API_KEY:
        try:
            result = await asyncio.wait_for(
                _call_anthropic(prompt, max_tokens, temperature, system),
                timeout=_LLM_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("llm_client.anthropic_timeout", timeout_s=_LLM_TIMEOUT_SECONDS)
            result = None
        if result is not None:
            return result
        logger.warning("llm_client.anthropic_failed")

    logger.warning("llm_client.no_provider_available")
    return None


async def _call_openai(prompt: str, max_tokens: int, temperature: float) -> Optional[str]:
    try:
        from config.settings import settings
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        response = await client.chat.completions.create(
            model=_OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content.strip()
    except Exception as exc:
        logger.warning("llm_client.openai_error", error=str(exc))
        return None


async def _call_anthropic(
    prompt: str, max_tokens: int, temperature: float, system: str
) -> Optional[str]:
    try:
        from config.settings import settings
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        message = await client.messages.create(
            model=_ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()
    except Exception as exc:
        logger.warning("llm_client.anthropic_error", error=str(exc))
        return None
