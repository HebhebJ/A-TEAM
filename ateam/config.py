"""Configuration loading from env, config.toml, and CLI args."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


# Mode presets — shorthand for setting checkpoints + review_mode together
MODES: dict[str, dict] = {
    # Standard: human checkpoints at arch/plan/phase, review every task
    "standard": {
        "human_checkpoints": ["architecture", "planning", "phase_complete"],
        "review_mode": "full",
    },
    # Auto: no human checkpoints, still reviews every task
    "auto": {
        "human_checkpoints": [],
        "review_mode": "full",
    },
    # Light: no checkpoints, reviews at halfway + end of each phase (batch)
    "light": {
        "human_checkpoints": [],
        "review_mode": "milestones",
    },
    # Yolo: no checkpoints, no review — straight through
    "yolo": {
        "human_checkpoints": [],
        "review_mode": "none",
    },
}


@dataclass
class Config:
    """A-TEAM configuration."""

    # LLM settings
    openrouter_api_key: str = ""
    api_base_url: str = "https://openrouter.ai/api/v1"
    default_model: str = "anthropic/claude-sonnet-4"

    # Per-agent model overrides (None = use default_model)
    agent_models: dict[str, str] = field(default_factory=dict)

    # Orchestration
    mode: str = "standard"
    max_review_retries: int = 3
    human_checkpoints: list[str] = field(
        default_factory=lambda: ["architecture", "planning", "phase_complete"]
    )
    # "full"       — review every task individually
    # "milestones" — review at halfway + end of each phase (batch)
    # "none"       — skip review entirely
    review_mode: Literal["full", "milestones", "none"] = "full"

    # Paths
    workspace_dir: Path = Path("./workspaces")
    project_root: Path = field(default_factory=lambda: Path.cwd())

    # Tools
    command_timeout: int = 30

    # Logging
    log_level: str = "INFO"

    def model_for_agent(self, agent_type: str) -> str:
        """Get the model to use for a specific agent type."""
        return self.agent_models.get(agent_type, self.default_model)

    def apply_mode(self, mode_name: str) -> None:
        """Apply a named mode preset, overriding checkpoints and review_mode."""
        preset = MODES.get(mode_name)
        if not preset:
            raise ValueError(f"Unknown mode '{mode_name}'. Choose from: {', '.join(MODES)}")
        self.mode = mode_name
        self.human_checkpoints = list(preset["human_checkpoints"])
        self.review_mode = preset["review_mode"]  # type: ignore[assignment]

    @classmethod
    def load(cls, project_root: Path | None = None, cli_overrides: dict | None = None) -> Config:
        """Load config from .env, config.toml, env vars, and CLI overrides."""
        root = project_root or Path.cwd()

        # Load .env file
        env_path = root / ".env"
        if env_path.exists():
            load_dotenv(env_path)

        # Load config.toml
        toml_config: dict = {}
        toml_path = root / "config.toml"
        if toml_path.exists():
            with open(toml_path, "rb") as f:
                toml_config = tomllib.load(f)

        # Build config with layered overrides
        llm_conf = toml_config.get("llm", {})
        orch_conf = toml_config.get("orchestration", {})
        tools_conf = toml_config.get("tools", {})

        config = cls(
            openrouter_api_key=os.environ.get(
                llm_conf.get("api_key_env", "OPENROUTER_API_KEY"), ""
            ),
            api_base_url=llm_conf.get("base_url", cls.api_base_url),
            default_model=os.environ.get(
                "ATEAM_MODEL", llm_conf.get("default_model", cls.default_model)
            ),
            agent_models=llm_conf.get("agent_models", {}),
            max_review_retries=orch_conf.get("max_review_retries", cls.max_review_retries),
            human_checkpoints=orch_conf.get("human_checkpoints", ["architecture", "planning", "phase_complete"]),
            review_mode=orch_conf.get("review_mode", "full"),
            workspace_dir=Path(
                os.environ.get("ATEAM_WORKSPACE_DIR", "./workspaces")
            ),
            project_root=root,
            command_timeout=tools_conf.get("command_timeout", cls.command_timeout),
        )

        # Apply mode preset from config.toml if set
        if "mode" in orch_conf:
            config.apply_mode(orch_conf["mode"])

        # Apply CLI overrides last (highest priority)
        if cli_overrides:
            mode = cli_overrides.pop("mode", None)
            for key, value in cli_overrides.items():
                if hasattr(config, key) and value is not None:
                    setattr(config, key, value)
            # Mode applied after other overrides so it can still be fine-tuned
            if mode:
                config.apply_mode(mode)

        return config
