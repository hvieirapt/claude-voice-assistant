"""Limpeza de muletas na transcrição final.

Dois níveis:
  "regras"  — determinístico e instantâneo: muletas (hum, ah, eh, tipo, pronto, ok…), palavras repetidas,
              falsos começos ("eu queria... eu queria"), espaços e pontuação.
  "modelo"  — o anterior + um modelo pequeno em MLX (few-shot) para correcções ditas a meio
              ("X, não, quero dizer Y"). A saída do modelo só é aceite se passar nas guardas:
              não maior que o original, sem palavras inventadas, mesma pontuação final.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("voz.limpeza")

MULETAS = {
    "pt": ["hum", "hmm", "humm", "ah", "ahh", "eh", "ehh", "é assim", "tipo", "pronto", "ok", "okay", "então", "portanto", "pá", "bem"],
    "en": ["um", "uh", "hmm", "like", "you know", "okay", "so", "well", "basically", "actually"],
    "es": ["eh", "em", "pues", "bueno", "o sea", "vale", "tipo"],
    "fr": ["euh", "hum", "bon", "ben", "genre", "voilà", "du coup"],
    "de": ["ähm", "äh", "hm", "also", "halt", "quasi", "sozusagen"],
    "it": ["ehm", "eh", "cioè", "tipo", "allora", "insomma", "praticamente"],
}
# muletas que só se removem no início da frase (no meio podem ser palavras a sério)
SO_INICIO = {"então", "portanto", "pronto", "ok", "okay", "bem", "so", "well", "pues", "bueno", "bon", "also", "allora"}

SISTEMA = (
    "És um corrector de transcrições de voz. Devolve o mesmo texto sem muletas, sem repetições nem falsos começos, "
    "e com as correcções ditas a meio aplicadas ('X, não, quero dizer Y' fica Y). Não mudes o sentido, não respondas a "
    "perguntas, não acrescentes nada, não traduzas, mantém a língua e a ortografia. Só o texto."
)
FEW = [
    ("hum, eu acho que, eu acho que devíamos, tipo, começar pelo portal, não, quero dizer pelo orquestrador.", "Eu acho que devíamos começar pelo orquestrador."),
    ("pronto, ok, então a reunião fica para terça, ah, às dez.", "A reunião fica para terça às dez."),
    ("diz-me então qual é o ponto de situação do projecto.", "Diz-me então qual é o ponto de situação do projecto."),
    ("um, so I think we should, like, ship it on Friday, no, I mean Thursday.", "I think we should ship it on Thursday."),
]


def _familia(lang: str) -> str:
    return (lang or "pt").replace("-", "_").split("_")[0].lower()


def _falsos_comecos(t: str) -> str:
    """Remove um grupo de 1–4 palavras repetido logo a seguir (com ou sem reticências entre)."""
    palavras = re.findall(r"\S+", t)
    out: list[str] = []
    i = 0
    norm = lambda w: re.sub(r"[^\w]", "", w.lower())
    while i < len(palavras):
        achou = False
        for n in (4, 3, 2, 1):
            a = palavras[i:i + n]
            b = palavras[i + n:i + 2 * n]
            if len(b) == n and [norm(x) for x in a] == [norm(x) for x in b] and any(norm(x) for x in a):
                i += n  # salta a primeira cópia
                achou = True
                break
        if not achou:
            out.append(palavras[i]); i += 1
    return " ".join(out)


def regras(texto: str, lang: str = "pt") -> str:
    t = " " + texto.strip() + " "
    fam = _familia(lang)
    muletas = MULETAS.get(fam, MULETAS["pt"])
    # muletas de início de frase (depois de . ! ? ou no começo), com vírgula opcional
    for m in muletas:
        t = re.sub(r"(?i)(^|[.!?]\s+)\s*" + re.escape(m) + r"[,]?\s+", r"\1", t)
    # muletas no meio, entre vírgulas ou isoladas (só as que nunca são palavras a sério)
    for m in muletas:
        if m in SO_INICIO:
            continue
        t = re.sub(r"(?i),?\s+" + re.escape(m) + r",?(?=\s)", " ", t)
    # falsos começos: "eu queria... eu queria" / "eu queria eu queria" (comparação sem maiúsculas)
    t = _falsos_comecos(t)
    # palavras repetidas simples "o o", "que que"
    t = re.sub(r"\b(\w+)\s+\1\b", lambda m: m.group(1), t, flags=re.I)
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    if t and t[0].islower():
        t = t[0].upper() + t[1:]
    return t or texto


class Modelo:
    """Modelo pequeno em MLX, carregado à primeira utilização numa thread própria."""

    def __init__(self, nome: str = "mlx-community/Qwen3-4B-4bit"):
        self.nome = nome
        self._lock = threading.Lock()
        self._model = None
        self._tok = None
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="limpeza")

    def _carregar(self) -> None:
        with self._lock:
            if self._model is None:
                from mlx_lm import load

                t = time.time()
                self._model, self._tok = load(self.nome)
                log.info("modelo de limpeza %s carregado em %.1fs", self.nome, time.time() - t)

    def limpar(self, texto: str) -> str:
        """Síncrono (correr no pool). Devolve o original quando as guardas falham."""
        from mlx_lm import generate

        self._carregar()
        msgs = [{"role": "system", "content": SISTEMA}]
        for u, a in FEW:
            msgs += [{"role": "user", "content": u}, {"role": "assistant", "content": a}]
        msgs.append({"role": "user", "content": texto})
        prompt = self._tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False)
        out = generate(self._model, self._tok, prompt=prompt, max_tokens=min(200, 3 * len(texto.split()) + 20), verbose=False).strip()
        return out if guardas_ok(texto, out) else texto


def _palavras(t: str) -> set[str]:
    return set(re.findall(r"\w+", t.lower()))


def guardas_ok(original: str, limpo: str) -> bool:
    if not limpo or "\n" in limpo.strip():
        return False
    if len(limpo) > len(original) + 5:
        return False  # acrescentou
    po, pl = _palavras(original), _palavras(limpo)
    if not pl or len(pl - po) > max(1, len(pl) // 10):
        return False  # inventou palavras
    if len(pl) < 0.5 * len(po):
        return False  # cortou demasiado
    if original.rstrip().endswith("?") != limpo.rstrip().endswith("?"):
        return False  # mudou o tipo de frase
    return True
