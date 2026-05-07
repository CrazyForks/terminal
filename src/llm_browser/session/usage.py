from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import requests

from llm_browser.events import Event


PRICING_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
CACHE_DURATION_S = 24 * 60 * 60


@dataclass(frozen=True)
class ModelTokenUsage:
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    provider_total_tokens: Optional[int] = None

    @property
    def input_total_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.reasoning_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    @property
    def context_tokens(self) -> int:
        return self.total_tokens

    def to_dict(self) -> dict[str, int | None]:
        return {
            "input_tokens": self.input_tokens,
            "input_total_tokens": self.input_total_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.total_tokens,
            "provider_total_tokens": self.provider_total_tokens,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelTokenUsage":
        return cls(
            input_tokens=_non_negative_int(data.get("input_tokens")),
            output_tokens=_non_negative_int(data.get("output_tokens")),
            reasoning_tokens=_non_negative_int(data.get("reasoning_tokens")),
            cache_read_tokens=_non_negative_int(data.get("cache_read_tokens")),
            cache_write_tokens=_non_negative_int(data.get("cache_write_tokens")),
            provider_total_tokens=_optional_non_negative_int(data.get("provider_total_tokens")),
        )

    @classmethod
    def from_openai_usage(cls, usage: Any) -> Optional["ModelTokenUsage"]:
        if not isinstance(usage, dict):
            return None

        input_total = _non_negative_int(usage.get("input_tokens") or usage.get("prompt_tokens"))
        output_total = _non_negative_int(usage.get("output_tokens") or usage.get("completion_tokens"))
        input_details = _dict_or_empty(usage.get("input_tokens_details") or usage.get("prompt_tokens_details"))
        output_details = _dict_or_empty(usage.get("output_tokens_details") or usage.get("completion_tokens_details"))
        cache_read = _non_negative_int(
            input_details.get("cached_tokens")
            or input_details.get("cache_read_tokens")
            or usage.get("cached_input_tokens")
            or usage.get("prompt_cached_tokens")
        )
        cache_write = _non_negative_int(
            input_details.get("cache_write_tokens")
            or input_details.get("cache_creation_tokens")
            or usage.get("cache_creation_input_tokens")
            or usage.get("prompt_cache_creation_tokens")
        )
        reasoning = _non_negative_int(output_details.get("reasoning_tokens") or usage.get("reasoning_tokens"))
        input_uncached = max(0, input_total - cache_read - cache_write)
        output = max(0, output_total - reasoning)
        provider_total = _optional_non_negative_int(usage.get("total_tokens"))

        parsed = cls(
            input_tokens=input_uncached,
            output_tokens=output,
            reasoning_tokens=reasoning,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            provider_total_tokens=provider_total,
        )
        if parsed.total_tokens <= 0 and not parsed.provider_total_tokens:
            return None
        return parsed


@dataclass(frozen=True)
class ModelPricing:
    input_cost_per_token: float
    output_cost_per_token: float
    cache_read_input_token_cost: float = 0.0
    cache_creation_input_token_cost: float = 0.0
    max_tokens: Optional[int] = None
    max_input_tokens: Optional[int] = None
    max_output_tokens: Optional[int] = None


@dataclass(frozen=True)
class UsageCost:
    total_cost_usd: float
    input_cost_usd: float
    output_cost_usd: float
    reasoning_cost_usd: float
    cache_read_cost_usd: float
    cache_write_cost_usd: float
    pricing_source: str

    def to_dict(self) -> dict[str, float | str]:
        return {
            "total_cost_usd": self.total_cost_usd,
            "input_cost_usd": self.input_cost_usd,
            "output_cost_usd": self.output_cost_usd,
            "reasoning_cost_usd": self.reasoning_cost_usd,
            "cache_read_cost_usd": self.cache_read_cost_usd,
            "cache_write_cost_usd": self.cache_write_cost_usd,
            "pricing_source": self.pricing_source,
        }


@dataclass
class UsageSummary:
    entries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_cost_usd: float = 0.0
    has_cost: bool = False
    latest_usage: Optional[ModelTokenUsage] = None
    latest_model: Optional[str] = None
    by_model: dict[str, dict[str, float | int | bool]] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.reasoning_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


_FALLBACK_PRICING: dict[str, ModelPricing] = {
    "gpt-5.5": ModelPricing(
        input_cost_per_token=5.00 / 1_000_000,
        output_cost_per_token=30.00 / 1_000_000,
        cache_read_input_token_cost=0.50 / 1_000_000,
    ),
    "gpt-5.5-pro": ModelPricing(
        input_cost_per_token=30.00 / 1_000_000,
        output_cost_per_token=180.00 / 1_000_000,
        cache_read_input_token_cost=30.00 / 1_000_000,
    ),
    "gpt-5.4": ModelPricing(
        input_cost_per_token=2.50 / 1_000_000,
        output_cost_per_token=15.00 / 1_000_000,
        cache_read_input_token_cost=0.25 / 1_000_000,
    ),
    "gpt-5.4-pro": ModelPricing(
        input_cost_per_token=30.00 / 1_000_000,
        output_cost_per_token=180.00 / 1_000_000,
        cache_read_input_token_cost=30.00 / 1_000_000,
    ),
    "gpt-5.4-mini": ModelPricing(
        input_cost_per_token=0.75 / 1_000_000,
        output_cost_per_token=4.50 / 1_000_000,
        cache_read_input_token_cost=0.075 / 1_000_000,
    ),
    "gpt-5.4-nano": ModelPricing(
        input_cost_per_token=0.20 / 1_000_000,
        output_cost_per_token=1.25 / 1_000_000,
        cache_read_input_token_cost=0.02 / 1_000_000,
    ),
    "gpt-5.3-codex-spark": ModelPricing(
        input_cost_per_token=1.75 / 1_000_000,
        output_cost_per_token=14.00 / 1_000_000,
        cache_read_input_token_cost=0.175 / 1_000_000,
    ),
    "gpt-5.3-codex": ModelPricing(
        input_cost_per_token=1.75 / 1_000_000,
        output_cost_per_token=14.00 / 1_000_000,
        cache_read_input_token_cost=0.175 / 1_000_000,
    ),
    "gpt-5.2": ModelPricing(
        input_cost_per_token=1.75 / 1_000_000,
        output_cost_per_token=14.00 / 1_000_000,
        cache_read_input_token_cost=0.175 / 1_000_000,
    ),
    "gpt-5.2-codex": ModelPricing(
        input_cost_per_token=1.75 / 1_000_000,
        output_cost_per_token=14.00 / 1_000_000,
        cache_read_input_token_cost=0.175 / 1_000_000,
    ),
}
_LITELLM_PREFIXES = ("openai/", "anthropic/", "google/", "gemini/", "azure/", "bedrock/", "openrouter/")
_pricing_cache: Optional[dict[str, Any]] = None


def calculate_usage_cost(model: Optional[str], usage: ModelTokenUsage) -> Optional[UsageCost]:
    pricing, source = get_model_pricing(model)
    if pricing is None:
        return None

    input_cost = usage.input_tokens * pricing.input_cost_per_token
    output_cost = usage.output_tokens * pricing.output_cost_per_token
    reasoning_cost = usage.reasoning_tokens * pricing.output_cost_per_token
    cache_read_cost = usage.cache_read_tokens * pricing.cache_read_input_token_cost
    cache_write_cost = usage.cache_write_tokens * pricing.cache_creation_input_token_cost
    total = input_cost + output_cost + reasoning_cost + cache_read_cost + cache_write_cost
    return UsageCost(
        total_cost_usd=total,
        input_cost_usd=input_cost,
        output_cost_usd=output_cost,
        reasoning_cost_usd=reasoning_cost,
        cache_read_cost_usd=cache_read_cost,
        cache_write_cost_usd=cache_write_cost,
        pricing_source=source,
    )


def summarize_usage_events(events: Iterable[Event]) -> UsageSummary:
    summary = UsageSummary()
    for event in events:
        if event.type != "model.usage":
            continue
        usage_data = event.payload.get("usage")
        if not isinstance(usage_data, dict):
            continue
        usage = ModelTokenUsage.from_dict(usage_data)
        model = str(event.payload.get("model") or "unknown")
        summary.entries += 1
        summary.input_tokens += usage.input_tokens
        summary.output_tokens += usage.output_tokens
        summary.reasoning_tokens += usage.reasoning_tokens
        summary.cache_read_tokens += usage.cache_read_tokens
        summary.cache_write_tokens += usage.cache_write_tokens
        summary.latest_usage = usage
        summary.latest_model = model

        cost = _optional_float(event.payload.get("cost_usd"))
        if cost is not None:
            summary.total_cost_usd += cost
            summary.has_cost = True

        model_stats = summary.by_model.setdefault(
            model,
            {"tokens": 0, "cost_usd": 0.0, "has_cost": False, "entries": 0},
        )
        model_stats["tokens"] = int(model_stats["tokens"]) + usage.total_tokens
        model_stats["entries"] = int(model_stats["entries"]) + 1
        if cost is not None:
            model_stats["cost_usd"] = float(model_stats["cost_usd"]) + cost
            model_stats["has_cost"] = True
    return summary


def format_tokens(tokens: int) -> str:
    if tokens >= 1_000_000_000:
        return f"{tokens / 1_000_000_000:.1f}B"
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 10_000:
        return f"{tokens / 1_000:.1f}k"
    return f"{tokens:,}"


def format_cost(cost: float) -> str:
    if cost < 0.01:
        return f"${cost:.4f}"
    return f"${cost:.2f}"


def get_model_pricing(model: Optional[str]) -> tuple[Optional[ModelPricing], str]:
    if not model:
        return None, "unknown"
    normalized = _normalize_model_name(model)
    fallback = _FALLBACK_PRICING.get(normalized)
    if fallback is not None:
        return fallback, "fallback"

    data = _load_litellm_pricing()
    raw = _find_litellm_model(data, model)
    if raw is None:
        return None, "unknown"
    return _pricing_from_litellm(raw), "litellm"


def _normalize_model_name(model: str) -> str:
    value = model.strip().lower()
    if "/" in value:
        value = value.rsplit("/", 1)[1]
    return value


def _load_litellm_pricing() -> dict[str, Any]:
    global _pricing_cache
    if _pricing_cache is not None:
        return _pricing_cache

    cache_path = _pricing_cache_path()
    cached = _read_json(cache_path)
    if isinstance(cached, dict) and _non_negative_float(cached.get("fetched_at")) + CACHE_DURATION_S > time.time():
        data = cached.get("data")
        _pricing_cache = data if isinstance(data, dict) else {}
        return _pricing_cache

    if os.environ.get("LLM_BROWSER_FETCH_PRICING", "").lower() in {"1", "true", "yes"}:
        try:
            response = requests.get(PRICING_URL, timeout=3)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                _write_json(cache_path, {"fetched_at": time.time(), "data": data})
                _pricing_cache = data
                return _pricing_cache
        except Exception:
            pass

    if isinstance(cached, dict) and isinstance(cached.get("data"), dict):
        _pricing_cache = cached["data"]
        return _pricing_cache

    _pricing_cache = {}
    return _pricing_cache


def _find_litellm_model(data: dict[str, Any], model: str) -> Optional[dict[str, Any]]:
    candidates = [model, model.strip().lower(), _normalize_model_name(model)]
    candidates.extend(f"{prefix}{_normalize_model_name(model)}" for prefix in _LITELLM_PREFIXES)
    if "/" in model:
        candidates.append(model.split("/", 1)[1])
    for candidate in candidates:
        raw = data.get(candidate)
        if isinstance(raw, dict):
            return raw
    return None


def _pricing_from_litellm(data: dict[str, Any]) -> ModelPricing:
    return ModelPricing(
        input_cost_per_token=_non_negative_float(data.get("input_cost_per_token")),
        output_cost_per_token=_non_negative_float(data.get("output_cost_per_token")),
        cache_read_input_token_cost=_non_negative_float(data.get("cache_read_input_token_cost")),
        cache_creation_input_token_cost=_non_negative_float(data.get("cache_creation_input_token_cost")),
        max_tokens=_optional_non_negative_int(data.get("max_tokens")),
        max_input_tokens=_optional_non_negative_int(data.get("max_input_tokens")),
        max_output_tokens=_optional_non_negative_int(data.get("max_output_tokens")),
    )


def _pricing_cache_path() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME")
    root = Path(xdg).expanduser() if xdg and Path(xdg).is_absolute() else Path.home() / ".cache"
    return root / "llm_browser" / "token_pricing.json"


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh)
            fh.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        pass


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _non_negative_int(value: Any) -> int:
    parsed = _optional_non_negative_int(value)
    return parsed if parsed is not None else 0


def _optional_non_negative_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, parsed)


def _non_negative_float(value: Any) -> float:
    parsed = _optional_float(value)
    return max(0.0, parsed or 0.0)


def _optional_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:
        return None
    return parsed
