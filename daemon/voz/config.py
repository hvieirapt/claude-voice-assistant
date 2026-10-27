"""Carregamento da configuração (config.toml) com defaults."""

from __future__ import annotations

import tomllib
from copy import deepcopy
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "audio": {"input_device": "", "sample_rate": 16000},
    "vad": {
        "mode": "auto",
        "start_threshold": 0.45,
        "end_threshold": 0.3,
        "min_speech_ms": 100,
        "silence_ms": 450,
        "turn_gap_ms": 1200,
        "pre_roll_ms": 450,
        "max_utterance_s": 60,
        "barge_in": True,
        "barge_in_threshold": 0.85,
        "barge_in_min_ms": 350,
        "interrupt_min_s": 1.0,
        "interrupt_min_words": 3,
    },
    "stt": {
        "model": "mlx-community/whisper-large-v3-turbo",
        "language": "pt",
        "min_duration_s": 0.3,
        "initial_prompt": "",
        "live": True,
        "live_model": "mlx-community/parakeet-tdt-0.6b-v3",
    },
    "falante": {
        "enabled": False,
        "base_url": "http://localhost:1234/v1",
        "model": "falante",
        "max_sentences": 4,
        "passthrough_max_chars": 350,
        "timeout_s": 60,
    },
    "tts": {
        "backend": "say",
        "voice": "Joana",
        "prefer_premium": True,
        "rate": 185,
        "speed": 1.0,
        "lang": "pt_PT",
        "mlx_model": "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit",
        "mlx_ref_audio": "",
        "mlx_ref_text": "",
        "mlx_voice": "Serena",
        "mlx_lang": "pt",
    },
    "claude": {"permission_mode": "acceptEdits", "model": "sonnet", "max_turns": 0},
    "cleanup": {"mode": "regras", "model": "mlx-community/Qwen3-4B-4bit"},
    "keywords": {"dynamic": True, "model": "mlx-community/Qwen3-4B-4bit", "max_words": 6},
    "meeting": {"input_device": "", "threshold": 0.7, "silence_ms": 900},
    "ui": {"enabled": True, "port": 8765, "ack_sound": True},
}


def _merge(base: dict, extra: dict) -> dict:
    out = deepcopy(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(explicit: Path | None = None) -> dict[str, Any]:
    """Procura config.toml: caminho explícito, pasta actual, pasta do pacote, ~/.voz."""
    candidates = [
        explicit,
        Path.cwd() / "config.toml",
        Path(__file__).resolve().parent.parent / "config.toml",
        Path.home() / ".voz" / "config.toml",
    ]
    cfg = deepcopy(DEFAULTS)
    for p in candidates:
        if p and p.is_file():
            with open(p, "rb") as f:
                cfg = _merge(cfg, tomllib.load(f))
            cfg["_path"] = str(p)
            # caminhos relativos no config resolvem-se em relação à pasta do config
            ref = cfg["tts"].get("mlx_ref_audio") or ""
            if ref and not ref.startswith(("/", "~")):
                cfg["tts"]["mlx_ref_audio"] = str(p.parent / ref)
            break
    # uma referência gravada pelo utilizador tem prioridade
    gravada = Path.home() / ".voz" / "ref.wav"
    if gravada.is_file() and not cfg["tts"].get("_ref_explicit"):
        cfg["tts"]["mlx_ref_audio"] = str(gravada)
    return cfg
