"""Falante: reescreve a resposta do Claude em prosa falada, com um modelo local (LM Studio).

Faz streaming de tokens e entrega frases completas à medida que fecham,
para a voz começar a falar antes de o texto acabar.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, AsyncIterator

import httpx

log = logging.getLogger("voz.falante")

SISTEMA = """És a camada de voz de um assistente de programação. Recebes a resposta escrita do assistente e reescreve-la para ser dita em voz alta numa conversa real.

Regras:
- Português de Portugal, tom natural de conversa entre colegas.
- No máximo {n} frases curtas. Vai directo ao essencial; corta detalhes que não mudam a decisão.
- Nunca leias código, comandos, caminhos de ficheiros ou URLs; refere-os por descrição ("o ficheiro de configuração").
- Sem listas, títulos, símbolos de markdown ou emojis. Só prosa.
- Se a resposta termina com uma pergunta ou pede uma decisão, mantém essa pergunta como última frase, inalterada no sentido.
- Se a resposta já é curta e falada, devolve-a quase igual.
- Responde só com o texto a dizer, sem introduções."""

_MD = re.compile(r"(```|^\s*[-*+#>|]|\[[^\]]+\]\([^)]+\)|`)", re.M)
_SENT_END = re.compile(r"(?<=[.!?…])\s+")


def precisa_reescrita(text: str, max_chars: int) -> bool:
    return len(text) > max_chars or bool(_MD.search(text))


def limpar_markdown(t: str) -> str:
    t = re.sub(r"```.*?```", " bloco de código. ", t, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"[*_]{1,3}([^*_\n]+)[*_]{1,3}", r"\1", t)
    t = re.sub(r"^\s*([-*+]|\d+\.)\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*>\s?", "", t, flags=re.M)
    t = re.sub(r"^\s*\|?[\s:|-]+\|\s*$", "", t, flags=re.M)
    t = t.replace("|", ", ").replace(" ", " ")
    return re.sub(r"[ \t]+", " ", t).strip()


def frases(texto: str) -> list[str]:
    return [s.strip() for s in _SENT_END.split(texto) if s.strip()]


class Falante:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg["falante"]
        self.client = httpx.AsyncClient(base_url=self.cfg["base_url"], timeout=self.cfg["timeout_s"])

    async def disponivel(self) -> bool:
        try:
            r = await self.client.get("/models")
            ids = [m["id"] for m in r.json().get("data", [])]
            ok = self.cfg["model"] in ids
            if not ok:
                log.warning("modelo %r não está carregado no LM Studio (há: %s)", self.cfg["model"], ids)
            return ok
        except Exception as e:  # noqa: BLE001
            log.warning("LM Studio indisponível em %s: %s", self.cfg["base_url"], e)
            return False

    async def falar(self, texto: str) -> AsyncIterator[str]:
        """Gera frases prontas a dizer. Se não for preciso reescrever, devolve o texto limpo por frases."""
        if not self.cfg["enabled"] or not precisa_reescrita(texto, self.cfg["passthrough_max_chars"]):
            for s in frases(limpar_markdown(texto)):
                yield s
            return
        try:
            async for s in self._stream(texto):
                yield s
        except Exception as e:  # noqa: BLE001
            log.warning("falante falhou (%s); a usar limpeza simples", e)
            for s in frases(limpar_markdown(texto))[: self.cfg["max_sentences"] + 2]:
                yield s

    async def _stream(self, texto: str) -> AsyncIterator[str]:
        body = {
            "model": self.cfg["model"],
            "temperature": 0.3,
            "max_tokens": 400,
            "reasoning_effort": "low",
            "stream": True,
            "messages": [
                {"role": "system", "content": SISTEMA.format(n=self.cfg["max_sentences"])},
                {"role": "user", "content": texto},
            ],
        }
        buf = ""
        async with self.client.stream("POST", "/chat/completions", json=body) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                delta = json.loads(data)["choices"][0].get("delta", {})
                piece = delta.get("content") or ""
                if not piece:
                    continue
                buf += piece
                parts = _SENT_END.split(buf)
                if len(parts) > 1:
                    for s in parts[:-1]:
                        if s.strip():
                            yield limpar_markdown(s)
                    buf = parts[-1]
        if buf.strip():
            yield limpar_markdown(buf)

    async def aclose(self) -> None:
        await self.client.aclose()
