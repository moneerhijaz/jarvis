"""Typed configuration for JARVIS.

Layered loading: packaged ``config/default.yaml`` < an optional user config file
(``JARVIS_CONFIG`` env var or ``config/local.yaml``) < environment variables
(``JARVIS__SECTION__KEY``). Validated into a single ``Settings`` object at startup.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

_PKG_ROOT = Path(__file__).resolve().parent
_PROJECT_ROOT = _PKG_ROOT.parent


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
class AppCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    data_dir: str = "./data"
    logs_dir: str = "./logs"
    artifacts_dir: str = "./artifacts"


class AutonomyCfg(BaseModel):
    mode: str = "full_local_autonomy"
    audit_all_tools: bool = True
    enable_kill_switch: bool = True
    rollback_where_possible: bool = True
    tripwires_enabled: bool = True
    delete_threshold: int = 50
    confirm_level: Literal["every", "risky", "never"] = "risky"  # which tool calls need approval
    confirm_timeout_s: int = 60                                  # no answer in this long => deny
    kill_hotkey: str = "ctrl+alt+esc"                            # global panic key (needs 'keyboard' pkg)
    max_depth: int = 2                                           # pipeline recursion depth cap (shared across the request tree)
    max_retries: int = 3                                         # v6: re-attempts before a stage fails with explanation (feasibility, match, validity)
    verification: Literal["auto", "single", "double"] = "auto"  # double = brain+vision cross-check; auto = double when a 2nd host exists; vision-tasks are always single
    agreement: Literal["facts", "model"] = "facts"              # how double-verify compares the two passes: facts = deterministic containment; model = an extra model-judge call
    question_timeout_s: int = 600                               # v6: a Multiresponse question unanswered this long fails the run (decision #4) instead of parking it forever
    max_replans: int = 2                                        # v6: after a step fails, how many times to re-plan the remainder (with the failure as context) before the run fails


class LimitsCfg(BaseModel):
    max_run_steps: int = 40
    max_tool_failures: int = 8
    max_wall_clock_s: int = 1800
    max_tokens: int = 0
    default_tool_timeout_s: int = 120
    shell_timeout_s: int = 300
    max_inline_output_bytes: int = 65536


class ProviderCfg(BaseModel):
    kind: str = "openai_compatible"
    base_url: str = "http://127.0.0.1:1234/v1"
    api_key: str = "lm-studio"
    timeout_s: int = 120


class RoleCfg(BaseModel):
    provider: str = "lmstudio"
    model: str = "auto"
    temperature: float = 0.2
    tool_strategy: Literal["native", "json", "native_or_json"] = "native_or_json"
    enabled: bool = True


class ModelsCfg(BaseModel):
    policy: str = "local-only"
    default_role: str = "brain"
    providers: dict[str, ProviderCfg] = Field(default_factory=dict)
    roles: dict[str, RoleCfg] = Field(default_factory=dict)
    # Auto-routing: pick a model per request based on task difficulty.
    auto: bool = False
    auto_simple: str = ""    # fast model for quick/conversational requests
    auto_complex: str = ""   # stronger model for hard/multi-step requests
    # Multimodal model used to locate things on screen (the "vision" half of hybrid
    # targeting). Blank => auto-detect a loaded model whose id looks like vision/VL.
    vision_model: str = ""
    # How JARVIS sees: "tool" = strong text brain consults the vision model via tools
    # (screen.look/ui.click); "context" = run the whole agent on the multimodal model so
    # it sees screenshots directly in its own context.
    vision_mode: Literal["tool", "context"] = "tool"
    # Distributed setup: point the brain and vision models at ANY machine on the LAN while
    # this PC keeps running the backend + control tools. Blank = use the brain provider's
    # base_url (local). e.g. "http://192.168.1.50:1234/v1".
    brain_base_url: str = ""
    brain_api_key: str = ""
    vision_base_url: str = ""
    vision_api_key: str = ""
    # Optional SEPARATE GUI-grounding model just for "where to click" (ui.find/ui.click
    # vision fallback), e.g. Qwen-GUI-3B. Blank = use the vision_model. base_url blank =
    # use the vision host. Lets you split: a general VLM for describing, a grounding model
    # for clicking.
    grounding_model: str = ""
    grounding_base_url: str = ""
    grounding_api_key: str = ""


class VaultEmbedCfg(BaseModel):
    mode: Literal["none", "in_process", "lmstudio"] = "none"
    model: str = "bge-small-en"


class VaultRetrievalCfg(BaseModel):
    graph_hops: int = 2
    max_snippets: int = 12
    always_load_profile: bool = True


class VaultGardenerCfg(BaseModel):
    enabled: bool = False
    schedule: str = "0 7 * * *"


class VaultScopeCfg(BaseModel):
    default: Literal["vault", "active_project"] = "vault"


class VaultCfg(BaseModel):
    path: str = "./data/JarvisBrain"
    git_versioning: bool = False
    auto_extract: bool = True   # Memory v2: after each run, extract durable facts into episodic memory
    embed: VaultEmbedCfg = Field(default_factory=VaultEmbedCfg)
    retrieval: VaultRetrievalCfg = Field(default_factory=VaultRetrievalCfg)
    gardener: VaultGardenerCfg = Field(default_factory=VaultGardenerCfg)
    scope: VaultScopeCfg = Field(default_factory=VaultScopeCfg)
    ignore: list[str] = Field(default_factory=list)


class ConnectorsCfg(BaseModel):
    default_access: Literal["read_only", "read_write"] = "read_only"
    enabled: list[str] = Field(default_factory=list)


class VoiceCfg(BaseModel):
    enabled: bool = True
    stt_model: str = "base"             # faster-whisper size: tiny|base|small|medium|...
    stt_device: str = "auto"            # auto|cpu|cuda
    stt_compute_type: str = "auto"      # auto|int8|float16|...
    tts_engine: str = "piper"           # piper | kokoro
    # Piper:
    tts_model_path: str = ""            # path to a Piper .onnx voice
    tts_config_path: str = ""           # defaults to <model>.json if blank
    # Kokoro (more natural, light):
    kokoro_voice: str = "bm_george"     # e.g. bm_george/bm_lewis (British male), am_michael (US male)
    kokoro_lang: str = ""               # 'a'=US, 'b'=British; blank = infer from voice prefix
    kokoro_device: str = "cpu"          # cpu keeps it off the GPU (won't fight LM Studio)
    kokoro_speed: float = 1.0           # speaking rate; >1 = faster (try 1.2-1.4)
    # Offline isolation: once the STT/TTS models are cached locally, force the HF
    # libraries to use the cache only and never touch the network. Set to false for a
    # one-time online download (or run scripts/fetch-voice).
    offline: bool = True
    models_dir: str = ""                # optional: keep HF model cache inside the project (portable)
    # Pronunciation fixes for TTS (Kokoro's G2P mishandles all-caps tokens — it spells some
    # out and mashes others into a word). say_as_word forces a spoken form (JARVIS -> "Jarvis");
    # spell_out forces letter-by-letter (IDE -> "I D E"). say_as_word matches case-insensitively;
    # spell_out only fires on ALL-CAPS tokens. Both are extendable from local.yaml.
    say_as_word: dict[str, str] = Field(default_factory=lambda: {"JARVIS": "Jarvis"})
    spell_out: list[str] = Field(default_factory=lambda: [
        "IDE", "API", "URL", "GPU", "CPU", "UI", "UX", "LLM", "SSD", "HDD", "USB",
        "HTTP", "HTTPS", "JSON", "HTML", "CSS", "SQL", "CLI", "SDK", "OS", "PC", "ID",
        "IP", "DNS", "VM", "AI", "TTS", "STT", "LAN", "VRAM", "GPT"])


class Settings(BaseModel):
    app: AppCfg = Field(default_factory=AppCfg)
    autonomy: AutonomyCfg = Field(default_factory=AutonomyCfg)
    limits: LimitsCfg = Field(default_factory=LimitsCfg)
    models: ModelsCfg = Field(default_factory=ModelsCfg)
    vault: VaultCfg = Field(default_factory=VaultCfg)
    connectors: ConnectorsCfg = Field(default_factory=ConnectorsCfg)
    voice: VoiceCfg = Field(default_factory=VoiceCfg)

    # Resolved absolute paths (filled in after load).
    project_root: str = str(_PROJECT_ROOT)

    # ---- convenience resolvers ----
    def _resolve(self, p: str) -> Path:
        path = Path(p)
        if not path.is_absolute():
            path = Path(self.project_root) / path
        return path

    @property
    def data_path(self) -> Path:
        return self._resolve(self.app.data_dir)

    @property
    def logs_path(self) -> Path:
        return self._resolve(self.app.logs_dir)

    @property
    def artifacts_path(self) -> Path:
        return self._resolve(self.app.artifacts_dir)

    @property
    def vault_path(self) -> Path:
        return self._resolve(self.vault.path)

    @property
    def db_path(self) -> Path:
        return self.data_path / "jarvis.sqlite"

    def role(self, name: str | None = None) -> RoleCfg:
        name = name or self.models.default_role
        return self.models.roles.get(name, RoleCfg())

    def provider_for(self, role_name: str | None = None) -> ProviderCfg:
        role = self.role(role_name)
        return self.models.providers.get(role.provider, ProviderCfg())

    def ensure_dirs(self) -> None:
        for p in (self.data_path, self.logs_path, self.artifacts_path):
            p.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _env_overrides(prefix: str = "JARVIS__") -> dict[str, Any]:
    """Map JARVIS__SECTION__KEY=value into nested dicts. Values are YAML-parsed."""
    out: dict[str, Any] = {}
    for key, raw in os.environ.items():
        if not key.startswith(prefix):
            continue
        parts = key[len(prefix):].lower().split("__")
        try:
            val = yaml.safe_load(raw)
        except Exception:
            val = raw
        node = out
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = val
    return out


def load_settings(
    config_file: str | os.PathLike | None = None,
    overrides: dict[str, Any] | None = None,
) -> Settings:
    """Load and validate settings from the layered sources."""
    data: dict[str, Any] = {}

    default_file = _PKG_ROOT.parent / "config" / "default.yaml"
    if default_file.exists():
        data = _deep_merge(data, yaml.safe_load(default_file.read_text()) or {})

    cfg = config_file or os.environ.get("JARVIS_CONFIG")
    if cfg is None:
        local = _PKG_ROOT.parent / "config" / "local.yaml"
        if local.exists():
            cfg = local
    if cfg and Path(cfg).exists():
        data = _deep_merge(data, yaml.safe_load(Path(cfg).read_text()) or {})

    data = _deep_merge(data, _env_overrides())
    if overrides:
        data = _deep_merge(data, overrides)

    return Settings(**data)


def active_config_path() -> Path:
    """The writable local config file load_settings layers on top of default.yaml."""
    cfg = os.environ.get("JARVIS_CONFIG")
    if cfg:
        return Path(cfg)
    return _PKG_ROOT.parent / "config" / "local.yaml"


def persist_setting(section: str, key: str, value: Any) -> None:
    """Patch ``section.key = value`` into the active YAML config as text, preserving the
    file's comments/formatting. Creates the file, section, or key as needed."""
    import re

    path = active_config_path()
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    kv = f"  {key}: {value}"
    sec_idx = next((i for i, l in enumerate(lines)
                    if re.match(rf"^{re.escape(section)}:\s*(#.*)?$", l)), None)
    if sec_idx is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [f"{section}:", kv]
    else:
        key_idx, end = None, len(lines)
        for j in range(sec_idx + 1, len(lines)):
            if re.match(r"^\S", lines[j]):   # reached the next top-level section
                end = j
                break
            if re.match(rf"^\s+{re.escape(key)}:\s*", lines[j]):
                key_idx = j
                break
        if key_idx is not None:
            lines[key_idx] = kv
        else:
            lines.insert(end, kv)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
