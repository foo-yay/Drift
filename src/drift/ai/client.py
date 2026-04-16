from __future__ import annotations

import logging
import os
from pathlib import Path

import anthropic

from drift.ai.prompt_builder import PromptBuilder
from drift.ai.response_parser import ResponseParser
from drift.config.models import LLMSection
from drift.models import GateReport, LLMDecision, MarketSnapshot

logger = logging.getLogger(__name__)


class LLMClient:
    """Anthropic Claude client for trade adjudication.

    Wraps the Anthropic messages API with retry logic and a hard NO_TRADE
    fallback. The caller never needs to handle exceptions — all failure modes
    degrade to a safe NO_TRADE decision.
    """

    def __init__(self, config: LLMSection, log_path: str | None = None) -> None:
        self._config = config
        self._log_path = Path(log_path) if log_path else None
        api_key = os.environ.get(config.api_key_env, "")
        if not api_key:
            logger.warning(
                "Environment variable %s is not set. LLM calls will fail.",
                config.api_key_env,
            )
        self._client = anthropic.Anthropic(api_key=api_key or "unset")
        self._prompt_builder = PromptBuilder()
        self._parser = ResponseParser()

    def adjudicate(
        self,
        snapshot: MarketSnapshot,
        gate_report: GateReport,
    ) -> tuple[LLMDecision, dict, str]:
        """Request a trading decision from Claude.

        Returns:
            (decision, raw_dict, raw_text) — raw_dict and raw_text are for
            logging; decision is always a valid LLMDecision.
        """
        self._inject_performance_context()

        messages = self._prompt_builder.build(snapshot, gate_report)
        system = self._prompt_builder.system_prompt
        raw_text = ""
        raw_dict: dict = {}

        for attempt in range(self._config.max_retries + 1):
            try:
                response = self._client.messages.create(
                    model=self._config.model,
                    max_tokens=2048,
                    temperature=self._config.temperature,
                    system=system,
                    messages=messages,
                    timeout=self._config.timeout_seconds,
                )
                raw_text = response.content[0].text
                decision, raw_dict = self._parser.parse(raw_text)
                if decision.decision != "NO_TRADE" or attempt == self._config.max_retries:
                    return decision, raw_dict, raw_text
                # If we got NO_TRADE but haven't exhausted retries, allow
                # the loop to complete — NO_TRADE is a valid outcome.
                return decision, raw_dict, raw_text

            except anthropic.APIStatusError as exc:
                logger.warning("Anthropic API error (attempt %d/%d): %s", attempt + 1, self._config.max_retries + 1, exc)
            except anthropic.APITimeoutError:
                logger.warning("Anthropic API timeout (attempt %d/%d)", attempt + 1, self._config.max_retries + 1)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Unexpected LLM error (attempt %d/%d): %s", attempt + 1, self._config.max_retries + 1, exc)

        # All retries exhausted
        from drift.ai.response_parser import _NO_TRADE_FALLBACK  # noqa: PLC0415
        logger.error("All %d LLM attempts failed. Returning NO_TRADE fallback.", self._config.max_retries + 1)
        return _NO_TRADE_FALLBACK, raw_dict, raw_text

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _inject_performance_context(self) -> None:
        """Build and push performance context into the prompt builder (if enabled)."""
        if not self._config.performance_context_enabled or self._log_path is None:
            self._prompt_builder.set_performance_context(None)
            return

        try:
            from drift.scoring.performance_context import build_performance_context  # noqa: PLC0415
            ctx = build_performance_context(
                self._log_path,
                lookback_days=self._config.performance_context_lookback_days,
                few_shot_examples=self._config.few_shot_examples,
            )
            self._prompt_builder.set_performance_context(ctx)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to build performance context: %s — proceeding without it.", exc)
            self._prompt_builder.set_performance_context(None)
