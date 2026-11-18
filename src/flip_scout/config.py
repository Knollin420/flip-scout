"""Loads settings and profiles (profiles.toml) and secrets (.env / environment)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Lower limit for the search interval (Vinted bot protection). 3 minutes since 2026-11-18.
MIN_INTERVAL_MINUTES = 3.0
# Single searches (profile.interval_minutes) may run more often, but never below this.
MIN_PROFILE_INTERVAL_MINUTES = 1.0


@dataclass
class Settings:
    domain: str = "at"
    interval_minutes: float = 5.0
    jitter_seconds: float = 30.0
    pause_between_searches: tuple[float, float] = (2.0, 5.0)
    pause_between_details: tuple[float, float] = (1.0, 2.0)
    max_age_minutes: float = 60.0
    heartbeat_hour: int = 9
    alarm_after_failed_runs: int = 3
    timezone: str = "Europe/Vienna"
    seen_retention_days: int = 30
    # Lowest price for every search (listings below are bait offers); None = no limit
    min_price_everywhere: float | None = None
    # Words that exclude a listing in every search (title and description, before the AI)
    exclude_everywhere: list[str] = field(default_factory=list)
    # Sellers without any rating: only listings up to this price (None = no limit)
    no_feedback_max_price: float | None = None

    @property
    def base_url(self) -> str:
        return f"https://www.vinted.{self.domain}"

    def local_time(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts, ZoneInfo(self.timezone))

    def day(self, ts: float) -> str:
        """Calendar day in local time, e.g. '2026-11-12' (key for daily counters)."""
        return self.local_time(ts).date().isoformat()


@dataclass
class AISettings:
    enabled: bool = True
    model: str = "claude-haiku-5-5"
    max_images: int = 3
    max_checks_per_day: int = 3000
    price_input_per_mtok: float = 0.10
    price_output_per_mtok: float = 0.50


@dataclass
class Profile:
    key: str
    label: str = ""
    emoji: str = "🛍"
    exclude: list[str] = field(default_factory=list)
    exclude_desc: list[str] = field(default_factory=list)
    must_include: list[str] = field(default_factory=list)
    any_include: list[str] = field(default_factory=list)
    require_any: list[str] = field(default_factory=list)
    min_price: float | None = None
    ai_check: str = ""
    briefing: bool = True
    ok_defects: list[str] = field(default_factory=list)
    no_go: list[str] = field(default_factory=list)
    check_icloud: bool = False
    max_prices: dict[str, float] = field(default_factory=dict)
    # Free model check in the title (before page load and AI): price keys are looked up
    # by name, title_skip_models are models that are never reported.
    title_models: bool = False
    title_skip_models: list[str] = field(default_factory=list)
    # Minimum number of seller ratings, regardless of price (unknown ratings never block)
    min_seller_feedback: int = 0
    # CPU check by fixed rules (cpu.py): "laptop" or "minipc" (mobile suffix required); "" = off
    cpu_check: str = ""
    # Own search interval for searches with this profile (None = settings.interval_minutes)
    interval_minutes: float | None = None

    def __post_init__(self) -> None:
        if not self.label:
            self.label = self.key


@dataclass
class Secrets:
    telegram_token: str = ""
    telegram_chat_id: str = ""
    anthropic_api_key: str = ""
    ntfy_topic: str = ""


@dataclass
class Config:
    settings: Settings = field(default_factory=Settings)
    ai: AISettings = field(default_factory=AISettings)
    profiles: dict[str, Profile] = field(default_factory=dict)
    seed_searches: list[dict] = field(default_factory=list)
    secrets: Secrets = field(default_factory=Secrets)
    data_dir: Path = Path("data")

    def tick_minutes(self) -> float:
        """How often the main loop looks for searches that are due."""
        own = [p.interval_minutes for p in self.profiles.values() if p.interval_minutes]
        return min([self.settings.interval_minutes, *own])

    def profile(self, key: str) -> Profile:
        return self.profiles.get(key) or self.profiles["standard"]

    @property
    def ai_available(self) -> bool:
        return self.ai.enabled and bool(self.secrets.anthropic_api_key)


def _pick(cls, raw: dict, **extra):
    names = set(cls.__dataclass_fields__)
    unknown = set(raw) - names
    if unknown:
        raise ValueError(f"Unbekannte Felder in [{cls.__name__}]: {', '.join(sorted(unknown))}")
    return cls(**{**raw, **extra})


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_config(
    profiles_path: Path, env_path: Path | None = None, data_dir: Path | None = None
) -> Config:
    raw = tomllib.loads(profiles_path.read_text(encoding="utf-8")) if profiles_path.exists() else {}

    settings_raw = dict(raw.get("settings", {}))
    for key in ("pause_between_searches", "pause_between_details"):
        if key in settings_raw:
            settings_raw[key] = tuple(settings_raw[key])
    settings = _pick(Settings, settings_raw)
    if settings.interval_minutes < MIN_INTERVAL_MINUTES:
        raise ValueError(
            f"interval_minutes muss mindestens {MIN_INTERVAL_MINUTES:g} sein "
            "(Bot-Schutz von Vinted)."
        )

    ai = _pick(AISettings, raw.get("ai", {}))

    profiles: dict[str, Profile] = {}
    for key, praw in raw.get("profile", {}).items():
        praw = dict(praw)
        praw["max_prices"] = {str(k): float(v) for k, v in praw.get("max_prices", {}).items()}
        profiles[key] = _pick(Profile, praw, key=key)
    for p in profiles.values():
        if p.interval_minutes is not None and p.interval_minutes < MIN_PROFILE_INTERVAL_MINUTES:
            raise ValueError(
                f"Profil {p.key!r}: interval_minutes muss mindestens "
                f"{MIN_PROFILE_INTERVAL_MINUTES:g} sein (Bot-Schutz von Vinted)."
            )
    profiles.setdefault("standard", Profile(key="standard", label="Normal"))

    seeds = list(raw.get("search", []))
    for s in seeds:
        if s.get("profile", "standard") not in profiles:
            raise ValueError(
                f"Suche {s.get('name')!r}: Profil {s.get('profile')!r} existiert nicht."
            )

    env = parse_env_file(env_path) if env_path else {}

    def secret(name: str) -> str:
        return os.environ.get(name) or env.get(name, "")

    secrets = Secrets(
        telegram_token=secret("TELEGRAM_TOKEN"),
        telegram_chat_id=secret("TELEGRAM_CHAT_ID"),
        anthropic_api_key=secret("ANTHROPIC_API_KEY"),
        ntfy_topic=secret("NTFY_TOPIC"),
    )
    data = data_dir or Path(os.environ.get("FLIP_SCOUT_DATA", "data"))
    return Config(
        settings=settings,
        ai=ai,
        profiles=profiles,
        seed_searches=seeds,
        secrets=secrets,
        data_dir=data,
    )
