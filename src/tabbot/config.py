"""Settings from `.env` and `config/privacy.yaml`."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class FreeTextDetection(BaseModel):
    min_avg_length: int = 30
    min_distinct_ratio: float = 0.5


class PrivacySettings(BaseModel):
    min_cell_count: int = 1
    max_rows: int = 200
    max_chars: int = 12000
    mask_formula_literals: bool = False
    sensitive_column_patterns: list[str] = Field(default_factory=list)
    sensitive_columns: list[str] = Field(default_factory=list)
    free_text_detection: FreeTextDetection = Field(default_factory=FreeTextDetection)
    redact_connection_paths: bool = True


class Settings(BaseModel):
    llm_provider: str = "anthropic"
    anthropic_api_key: str | None = Field(default=None, repr=False)
    claude_model: str = "claude-opus-5-5"
    claude_effort: str = "medium"           # low | medium | high | xhigh | max
    use_fallback: bool = True               # server-side refusal fallback
    max_tool_rounds: int = 8                # API calls per question
    max_output_tokens: int = 4000           # per API call
    max_usd_per_question: float = 0.50      # estimated, all rounds of one question
    max_usd_per_day: float = 5.00           # estimated, all calls today on this machine
    max_calls_per_day: int = 200
    workspace_dir: Path = PROJECT_ROOT / "workspace"
    max_unzipped_mb: int = 2000
    dry_run: bool = False
    privacy: PrivacySettings = Field(default_factory=PrivacySettings)


def _bool(v: str | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(env_file: Path | None = None, privacy_file: Path | None = None) -> Settings:
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
    pfile = privacy_file or PROJECT_ROOT / "config" / "privacy.yaml"
    privacy = PrivacySettings(**(yaml.safe_load(pfile.read_text(encoding="utf-8")) or {})) if pfile.exists() \
        else PrivacySettings()

    ws = Path(os.getenv("WORKSPACE_DIR") or "./workspace")
    if not ws.is_absolute():
        ws = PROJECT_ROOT / ws

    return Settings(
        llm_provider=os.getenv("LLM_PROVIDER") or "anthropic",
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        claude_model=os.getenv("CLAUDE_MODEL") or "claude-opus-5-5",
        claude_effort=os.getenv("CLAUDE_EFFORT") or "medium",
        use_fallback=_bool(os.getenv("CLAUDE_FALLBACK"), True),
        max_tool_rounds=int(os.getenv("MAX_TOOL_ROUNDS") or 8),
        max_output_tokens=int(os.getenv("LLM_MAX_OUTPUT_TOKENS") or 4000),
        max_usd_per_question=float(os.getenv("LLM_MAX_USD_PER_QUESTION") or 0.50),
        max_usd_per_day=float(os.getenv("LLM_MAX_USD_PER_DAY") or 5.00),
        max_calls_per_day=int(os.getenv("LLM_MAX_CALLS_PER_DAY") or 200),
        workspace_dir=ws.resolve(),
        max_unzipped_mb=int(os.getenv("MAX_UNZIPPED_MB") or 2000),
        dry_run=_bool(os.getenv("DRY_RUN"), False),
        privacy=privacy,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
