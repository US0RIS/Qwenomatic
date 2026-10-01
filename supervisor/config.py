"""Supervisor-owned configuration: farm.yaml, policy.yaml, fitness.yaml."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from storage.events.canonical import digest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"


class ConfigNotFound(FileNotFoundError):
    pass


def find_config_dir(explicit: str | Path | None = None) -> Path:
    """--config-dir, then $QWENOMATIC_CONFIG_DIR, then ./config, then the checkout's config/."""
    if explicit is not None:
        if not (Path(explicit) / "farm.yaml").is_file():
            raise ConfigNotFound(f"no farm.yaml in {explicit}")
        return Path(explicit).resolve()
    candidates = [os.environ.get("QWENOMATIC_CONFIG_DIR"), Path.cwd() / "config", DEFAULT_CONFIG_DIR]
    for c in candidates:
        if c and (Path(c) / "farm.yaml").is_file():
            return Path(c).resolve()
    raise ConfigNotFound("no farm.yaml found: pass --config-dir, set QWENOMATIC_CONFIG_DIR, "
                         "or run from a directory containing config/")


def deep_merge(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def config_hash(cfg: dict[str, Any]) -> str:
    return digest(cfg)


@dataclass
class FarmConfig:
    farm: dict[str, Any]
    policy: dict[str, Any]
    fitness: dict[str, Any]
    security: dict[str, Any]
    config_dir: Path
    data_dir: Path

    @classmethod
    def load(
        cls,
        config_dir: str | Path | None = None,
        *,
        data_dir: str | Path | None = None,
        overrides: dict[str, dict[str, Any]] | None = None,
    ) -> "FarmConfig":
        config_dir = find_config_dir(config_dir)
        overrides = overrides or {}
        farm = deep_merge(_read_yaml(config_dir / "farm.yaml"), overrides.get("farm"))
        policy = deep_merge(_read_yaml(config_dir / "policy.yaml"), overrides.get("policy"))
        fitness = deep_merge(_read_yaml(config_dir / "fitness.yaml"), overrides.get("fitness"))
        security = deep_merge(_read_yaml(config_dir / "security.yaml"), overrides.get("security"))
        if data_dir is None:
            data_dir = Path(farm["farm"].get("data_dir", "var"))
            if not data_dir.is_absolute():
                data_dir = config_dir.parent / data_dir  # relative to the farm root
        return cls(farm=farm, policy=policy, fitness=fitness, security=security, config_dir=config_dir, data_dir=Path(data_dir))

    # Convenience accessors -------------------------------------------------
    @property
    def seed(self) -> int:
        return int(self.farm["farm"]["seed"])

    @property
    def population_size(self) -> int:
        return int(self.farm["farm"]["population_size"])

    @property
    def tick_seconds(self) -> float:
        return float(self.farm["generation"]["tick_seconds"])

    @property
    def ticks_per_generation(self) -> int:
        return max(1, round(float(self.farm["generation"]["duration_hours"]) * 3600 / self.tick_seconds))

    @property
    def segments(self) -> dict[str, dict[str, Any]]:
        return self.farm["economy"]["segments"]

    def archetype_of(self, segment: str) -> str:
        return self.segments.get(segment, {}).get("archetype", "standard")

    def hashes(self) -> dict[str, str]:
        return {
            "farm": config_hash(self.farm),
            "policy": config_hash(self.policy),
            "fitness": config_hash(self.fitness),
            "security": config_hash(self.security),
        }


def _read_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}
