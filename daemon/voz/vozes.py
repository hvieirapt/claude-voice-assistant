"""Catálogo de vozes por língua: Apple (say), Qwen3 (presets e clones) e Piper (Hugging Face).

ids: say:<Nome> · mlx:voice:<preset> · mlx:ref:<ficheiro.wav> · piper:<chave do catálogo>
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

log = logging.getLogger("voz.vozes")

VOZ_DIR = Path.home() / ".voz"
PIPER_DIR = VOZ_DIR / "voices"
PIPER_INDEX = VOZ_DIR / "piper-voices.json"
PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
QWEN_LANGS = {"zh", "en", "de", "it", "pt", "es", "ja", "ko", "fr", "ru"}
QWEN_PRESETS = ["Serena", "Ryan", "Vivian", "Aiden", "Eric", "Dylan", "Uncle_Fu", "Ono_Anna", "Sohee"]

# línguas que o Whisper transcreve e que mostramos como escolha (código → nome)
LINGUAS = {
    "pt_PT": "Português (Portugal)", "pt_BR": "Português (Brasil)", "en": "English", "es": "Español",
    "fr": "Français", "de": "Deutsch", "it": "Italiano", "nl": "Nederlands", "pl": "Polski", "ru": "Русский",
    "uk": "Українська", "cs": "Čeština", "sv": "Svenska", "da": "Dansk", "no": "Norsk", "fi": "Suomi",
    "el": "Ελληνικά", "tr": "Türkçe", "ro": "Română", "hu": "Magyar", "ca": "Català", "zh": "中文",
    "ja": "日本語", "ko": "한국어", "ar": "العربية", "hi": "हिन्दी", "id": "Bahasa Indonesia", "vi": "Tiếng Việt",
}


def familia(code: str) -> str:
    """'pt_PT' → 'pt'; 'en' → 'en'."""
    return (code or "").replace("-", "_").split("_")[0].lower()


def piper_bin() -> str:
    cand = Path(sys.executable).parent / "piper"
    return str(cand) if cand.exists() else "piper"


# ---------- Apple ----------
def vozes_say() -> list[dict]:
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return []
    res = []
    for line in out.splitlines():
        m = re.match(r"^(.*?)\s+([a-z]{2}_[A-Z]{2})\s", line)  # nomes longos ficam a um só espaço da língua
        if not m:
            continue
        nome, loc = m.group(1).strip(), m.group(2)
        # "Joana (Enhanced)" → "Joana melhorada (Apple)"; "Joana (Premium)" → "Joana Premium (Apple)"
        base, _, var = nome.partition(" (")
        var = var.rstrip(")")
        qual = {"Enhanced": "melhorada", "Premium": "Premium"}.get(var, var)
        label = f"{base} {qual} (Apple)" if qual else f"{base} (Apple)"
        res.append({"id": f"say:{nome}", "label": label, "lang": loc, "source": "apple", "installed": True, "_base": base, "_var": var})
    # com a versão melhorada/Premium instalada, a compacta do mesmo nome deixa de interessar
    melhores = {(r["_base"], r["lang"]) for r in res if r["_var"] in ("Enhanced", "Premium")}
    res = [r for r in res if r["_var"] or (r["_base"], r["lang"]) not in melhores]
    for r in res:
        r.pop("_base"); r.pop("_var")
    return res


# ---------- Piper ----------
def piper_index(max_age_s: int = 86400) -> dict:
    VOZ_DIR.mkdir(exist_ok=True)
    if PIPER_INDEX.is_file() and time.time() - PIPER_INDEX.stat().st_mtime < max_age_s:
        return json.loads(PIPER_INDEX.read_text())
    try:
        r = httpx.get(PIPER_BASE + "voices.json", timeout=60, follow_redirects=True)
        r.raise_for_status()
        PIPER_INDEX.write_text(r.text)
        return r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("catálogo Piper indisponível: %s", e)
        return json.loads(PIPER_INDEX.read_text()) if PIPER_INDEX.is_file() else {}


def piper_model_path(key: str) -> Path:
    return PIPER_DIR / f"{key}.onnx"


def vozes_piper() -> list[dict]:
    res = []
    for key, e in piper_index().items():
        lang = e.get("language", {})
        res.append({
            "id": f"piper:{key}",
            "label": f"{e.get('name', key)} ({e.get('quality', '?')}, Piper, {lang.get('name_english', '')}/{lang.get('country_english', '')})",
            "lang": lang.get("code", ""),
            "source": "piper",
            "installed": piper_model_path(key).is_file(),
            "size_mb": round(sum(f.get("size_bytes", 0) for f in e.get("files", {}).values()) / 1e6),
        })
    return res


def instalar_piper(key: str) -> Path:
    """Descarrega o modelo .onnx e o .json para ~/.voz/voices/. Devolve o caminho do .onnx."""
    idx = piper_index()
    e = idx.get(key)
    if not e:
        raise ValueError(f"voz Piper desconhecida: {key}")
    PIPER_DIR.mkdir(parents=True, exist_ok=True)
    dest = piper_model_path(key)
    for rel in e["files"]:
        if rel.endswith(".onnx") or rel.endswith(".onnx.json"):
            target = dest if rel.endswith(".onnx") else dest.with_suffix(".onnx.json")
            if target.is_file():
                continue
            with httpx.stream("GET", PIPER_BASE + rel, timeout=600, follow_redirects=True) as r:
                r.raise_for_status()
                with open(target, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
    return dest


# ---------- Qwen3 ----------
def vozes_qwen(refs: list[Path]) -> list[dict]:
    res = []
    gravada = VOZ_DIR / "ref.wav"
    for r in refs:
        nome = "a tua voz" if r == gravada else r.stem
        res.append({"id": f"mlx:ref:{r}", "label": f"{nome} (clone neural, Qwen3)", "lang": "*", "source": "qwen-clone", "installed": True})
    for v in QWEN_PRESETS:
        res.append({"id": f"mlx:voice:{v}", "label": f"{v} (Qwen3, neural)", "lang": "*", "source": "qwen", "installed": True})
    return res


# ---------- pesquisa ----------
def procurar(lang: str | None, query: str | None, refs: list[Path]) -> list[dict]:
    fam = familia(lang) if lang else ""
    q = (query or "").strip().lower()
    todas = vozes_say() + vozes_qwen(refs) + vozes_piper()
    out = []
    for v in todas:
        vl = v["lang"]
        if fam:
            if vl == "*":
                if fam not in QWEN_LANGS:
                    continue
            elif lang and "_" in lang and vl != lang and familia(vl) == fam:
                # pediu uma região específica: mostra a região primeiro, as outras depois
                v = {**v, "_rank": 1}
            elif familia(vl) != fam:
                continue
        if q and q not in v["label"].lower() and q not in v["id"].lower() and q not in vl.lower() and q not in LINGUAS.get(vl, "").lower():
            continue
        out.append(v)
    ordem = {"apple": 0, "qwen-clone": 1, "piper": 2, "qwen": 3}
    out.sort(key=lambda v: (
        v.get("_rank", 0), 0 if v["installed"] else 1,
        1 if v["source"] == "apple" and _novelty(v["id"].split(":", 1)[1]) else 0,
        ordem.get(v["source"], 9), v["label"],
    ))
    for v in out:
        v.pop("_rank", None)
    return out[:40]


# vozes "novelty" da Apple: existem para todas as línguas mas não servem para conversa
NOVELTY = {
    "Albert", "Bad News", "Bahh", "Bells", "Boing", "Bubbles", "Cellos", "Deranged", "Good News", "Jester", "Organ",
    "Superstar", "Trinoids", "Whisper", "Wobble", "Zarvox", "Eddy", "Flo", "Grandma", "Grandpa", "Reed", "Rocko",
    "Sandy", "Shelley", "Junior", "Ralph", "Kathy", "Fred",
}


def _novelty(nome: str) -> bool:
    return nome.split(" (")[0] in NOVELTY


def voz_say_para(lang: str, instaladas: list[str] | None = None) -> str | None:
    """Primeira voz do say para a língua (prefere Premium/Enhanced e a região pedida; evita as novelty)."""
    cands = [v for v in vozes_say() if familia(v["lang"]) == familia(lang)]
    if not cands:
        return None
    cands.sort(key=lambda v: (
        1 if _novelty(v["id"].split(":", 1)[1]) else 0,
        0 if v["lang"] == lang else 1,
        0 if "Premium" in v["id"] else 1 if "Enhanced" in v["id"] else 2,
        v["id"],
    ))
    return cands[0]["id"].split(":", 1)[1]


# Prompt inicial do Whisper por língua: puxa a ortografia e a pontuação certas (pt-PT vs pt-BR, etc.).
PROMPTS = {
    "pt_PT": "Olá, tudo bem? Vou enviar o relatório ainda hoje. Está bem, combinado. Vamos rever o projecto, a autenticação e o deploy.",
    "pt_BR": "Olá, tudo bem? Vou enviar o relatório ainda hoje. Tá bom, combinado. Vamos revisar o projeto, a autenticação e o deploy.",
    "en": "Hi, how are you? I'll send the report today. Okay, agreed. Let's review the project, the authentication and the deployment.",
    "es": "Hola, ¿qué tal? Enviaré el informe hoy. Vale, de acuerdo. Revisemos el proyecto, la autenticación y el despliegue.",
    "fr": "Bonjour, ça va ? J'enverrai le rapport aujourd'hui. D'accord. Revoyons le projet, l'authentification et le déploiement.",
    "de": "Hallo, wie geht's? Ich schicke den Bericht heute. Okay, abgemacht. Lass uns das Projekt, die Authentifizierung und das Deployment durchgehen.",
    "it": "Ciao, come va? Invierò il rapporto oggi. Va bene, d'accordo. Rivediamo il progetto, l'autenticazione e il deploy.",
}


def prompt_para(lang: str) -> str:
    return PROMPTS.get(lang) or PROMPTS.get(familia(lang), "")


SUBSTITUICOES = VOZ_DIR / "substituicoes.csv"


def substituicoes() -> list[tuple[str, str]]:
    """Pares `de;para`, uma linha cada, em ~/.voz/substituicoes.csv (ignora linhas vazias e #)."""
    if not SUBSTITUICOES.is_file():
        return []
    pares = []
    for line in SUBSTITUICOES.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ";" not in line:
            continue
        de, _, para = line.partition(";")
        if de.strip():
            pares.append((de.strip(), para.strip()))
    return pares


def aplicar_substituicoes(texto: str) -> str:
    for de, para in substituicoes():
        texto = re.sub(r"(?i)\b" + re.escape(de) + r"\b", para, texto)
    return texto
