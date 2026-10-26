"""Token and call accounting shared by all model features."""

from __future__ import annotations

from typing import Any


class LLMUsageTracker:
    """Track token counts, calls, and failures by model name."""

    def __init__(self) -> None:
        self.call_count = 0
        self.failure_count = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.model_counts: dict[str, dict[str, int]] = {}

    def record_usage(
        self,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        *,
        emit_event: bool = True,
    ) -> None:
        self.call_count += 1
        self.input_tokens += prompt_tokens
        self.output_tokens += completion_tokens
        stats = self.model_counts.setdefault(
            model,
            {"calls": 0, "failures": 0, "input_tokens": 0, "output_tokens": 0},
        )
        stats["calls"] += 1
        stats["input_tokens"] += prompt_tokens
        stats["output_tokens"] += completion_tokens
        if emit_event:
            self._log_call(model, "response_received", prompt_tokens, completion_tokens)

    def record_cached_usage(
        self,
        model: str,
        *,
        calls: int,
        failures: int,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Restore provider usage associated with a cached extraction result."""
        if calls <= 0:
            return
        self.call_count += calls
        self.failure_count += failures
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        stats = self.model_counts.setdefault(
            model,
            {"calls": 0, "failures": 0, "input_tokens": 0, "output_tokens": 0},
        )
        stats["calls"] += calls
        stats["failures"] += failures
        stats["input_tokens"] += input_tokens
        stats["output_tokens"] += output_tokens

    def record_failure(
        self,
        model: str,
        *,
        count_call: bool = True,
        error_type: str = "model_error",
    ) -> None:
        if count_call:
            self.call_count += 1
        self.failure_count += 1
        stats = self.model_counts.setdefault(
            model,
            {"calls": 0, "failures": 0, "input_tokens": 0, "output_tokens": 0},
        )
        if count_call:
            stats["calls"] += 1
        stats["failures"] += 1
        self._log_call(model, "failed", error_type=error_type)

    @staticmethod
    def _log_call(
        model: str,
        result: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        error_type: str = "",
    ) -> None:
        from code.run_logging import record_event

        event_fields = {
            "tool_name": "gemini",
            "model": model,
            "result": result,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        if error_type:
            event_fields["error_type"] = error_type
        record_event("llm_call", **event_fields)

    def get_summary(self) -> dict[str, Any]:
        return {
            "total_calls": self.call_count,
            "failed_calls": self.failure_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "model_breakdown": {name: stats.copy() for name, stats in self.model_counts.items()},
        }

    def reset(self) -> None:
        self.call_count = 0
        self.failure_count = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.model_counts.clear()


USAGE_TRACKER = LLMUsageTracker()
