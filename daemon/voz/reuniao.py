"""Modo de reunião: identificação de orador por assinatura de voz e acta com horas.

Oradores: cada frase gera uma assinatura (sherpa-onnx, 3D-Speaker ERes2Net, 512 dims, ~40 ms).
Assinaturas parecidas (cosseno ≥ limiar) são a mesma pessoa; o centróide de cada orador vai sendo
afinado. Novas vozes recebem "Orador N" até alguém dizer "eu sou o João" / "este é o João", ou até
ser renomeado no painel. A acta é um ficheiro markdown com uma linha por intervenção.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np

log = logging.getLogger("voz.reuniao")

MODELO_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
MODELO = Path.home() / ".voz" / "models" / "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
ACTAS = Path.home() / ".voz" / "reunioes"

# "eu sou o João", "sou a Maria", "chamo-me Rui", "este é o Pedro", "aqui é a Ana"
RE_NOME = re.compile(
    r"(?i)\b(?:eu sou|sou|chamo-me|o meu nome é|aqui (?:é|fala)|este é|esta é|fala)\s+(?:o|a)?\s*([A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wÁÉÍÓÚÂÊÔÃÕÇáéíóúâêôãõç-]{1,30}(?:\s+[A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wáéíóúâêôãõç-]{1,30})?)\b"
)


def garantir_modelo() -> Path:
    if MODELO.is_file():
        return MODELO
    MODELO.parent.mkdir(parents=True, exist_ok=True)
    log.info("a descarregar modelo de orador (40 MB)…")
    with httpx.stream("GET", MODELO_URL, timeout=600, follow_redirects=True) as r:
        r.raise_for_status()
        with open(MODELO, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    return MODELO


class Oradores:
    def __init__(self, limiar: float = 0.7):
        import sherpa_onnx

        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(garantir_modelo()), num_threads=2)
        self._ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        self.limiar = limiar
        self.lista: list[dict] = []  # {name, centroid, n}
        self.assinaturas: list[tuple[int, np.ndarray, str]] = []  # (índice da intervenção, assinatura, nome atribuído)
        self.k: int | None = None    # número de oradores esperado, se o utilizador o disser

    def assinatura(self, audio: np.ndarray, sr: int = 16000) -> np.ndarray | None:
        if len(audio) < sr * 0.6:
            return None
        st = self._ex.create_stream()
        st.accept_waveform(sr, audio.astype(np.float32))
        st.input_finished()
        e = np.array(self._ex.compute(st), dtype=np.float32)
        n = np.linalg.norm(e)
        return e / n if n > 0 else None

    def identificar(self, audio: np.ndarray, sr: int = 16000, idx: int | None = None) -> tuple[str, float, bool]:
        """Devolve (nome, semelhança, é_novo). Sem assinatura possível → último orador ou 'Orador 1'."""
        e = self.assinatura(audio, sr)
        if e is None:
            nome = self.lista[-1]["name"] if self.lista else self._novo(None)
            return nome, 0.0, False
        melhor, sim = None, -1.0
        for o in self.lista:
            s = float(e @ o["centroid"])
            if s > sim:
                melhor, sim = o, s
        # com k conhecido e já k oradores, a intervenção vai sempre para o mais parecido
        aceita = melhor is not None and (sim >= self.limiar or (self.k is not None and len(self.lista) >= self.k))
        if aceita:
            melhor["centroid"] = melhor["centroid"] * melhor["n"] + e
            melhor["n"] += 1
            melhor["centroid"] /= np.linalg.norm(melhor["centroid"])
            nome, novo = melhor["name"], False
        else:
            nome, novo = self._novo(e), True
        if idx is not None:
            self.assinaturas.append((idx, e, nome))
        return nome, sim, novo

    def reagrupar(self, k: int | None = None) -> dict[int, str]:
        """Reagrupa todas as intervenções com assinatura (aglomerativo, ligação média, cosseno).
        Com k, fecha em exactamente k grupos; sem k, corta no limiar. Mantém os nomes já dados
        (o nome mais frequente em cada grupo). Devolve {índice da intervenção: nome}."""
        k = k or self.k
        if len(self.assinaturas) < 2:
            return {}
        idxs = [i for i, _, _ in self.assinaturas]
        E = np.stack([e for _, e, _ in self.assinaturas])
        grupos: list[list[int]] = [[i] for i in range(len(E))]
        cent = [E[i].copy() for i in range(len(E))]
        while len(grupos) > 1:
            melhor, a_, b_ = -1.0, -1, -1
            for a in range(len(grupos)):
                for b in range(a + 1, len(grupos)):
                    sim = float(cent[a] @ cent[b] / (np.linalg.norm(cent[a]) * np.linalg.norm(cent[b]) + 1e-9))
                    if sim > melhor:
                        melhor, a_, b_ = sim, a, b
            if k is not None:
                if len(grupos) <= k:
                    break
            elif melhor < self.limiar - 0.05:  # ligação média é mais exigente: margem pequena
                break
            grupos[a_] += grupos[b_]; cent[a_] = E[grupos[a_]].mean(axis=0)
            del grupos[b_]; del cent[b_]
        # nomes: o mais frequente entre os já atribuídos a cada grupo; preferir nomes dados pela pessoa
        nomes_antigos = [n for _, _, n in self.assinaturas]
        usados: set[str] = set()
        nova_lista: list[dict] = []
        mapa: dict[int, str] = {}
        ordem = sorted(range(len(grupos)), key=lambda g: min(grupos[g]))  # por ordem de aparição
        for g in ordem:
            contagem: dict[str, int] = {}
            for m in grupos[g]:
                contagem[nomes_antigos[m]] = contagem.get(nomes_antigos[m], 0) + 1
            candidatos = sorted(contagem, key=lambda n: (n.startswith("Orador"), -contagem[n]))
            nome = next((c for c in candidatos if c not in usados), None) or f"Orador {len(nova_lista) + 1}"
            usados.add(nome)
            c = E[grupos[g]].mean(axis=0); c /= np.linalg.norm(c)
            nova_lista.append({"name": nome, "centroid": c, "n": len(grupos[g])})
            for m in grupos[g]:
                mapa[idxs[m]] = nome
        self.lista = nova_lista
        self.assinaturas = [(i, e, mapa[i]) for i, e, _ in self.assinaturas]
        return mapa

    def _novo(self, e: np.ndarray | None) -> str:
        nome = f"Orador {len(self.lista) + 1}"
        self.lista.append({"name": nome, "centroid": e if e is not None else np.zeros(self._ex.dim, dtype=np.float32), "n": 1 if e is not None else 0})
        return nome

    def renomear(self, de: str, para: str) -> bool:
        """Renomeia; se o nome novo já existir, funde os dois oradores."""
        para = para.strip()
        if not para:
            return False
        alvo = next((o for o in self.lista if o["name"].lower() == de.lower()), None)
        if alvo is None:
            return False
        existente = next((o for o in self.lista if o["name"].lower() == para.lower() and o is not alvo), None)
        if existente is not None:
            tot = existente["n"] + alvo["n"]
            existente["centroid"] = existente["centroid"] * existente["n"] + alvo["centroid"] * alvo["n"]
            existente["centroid"] /= np.linalg.norm(existente["centroid"]) + 1e-9
            existente["n"] = tot
            self.lista.remove(alvo)
            para = existente["name"]
        else:
            alvo["name"] = para
        self.assinaturas = [(i, e, para if n.lower() == de.lower() else n) for i, e, n in self.assinaturas]
        return True

    def nomes(self) -> list[str]:
        return [o["name"] for o in self.lista]


class Acta:
    def __init__(self, titulo: str):
        ACTAS.mkdir(parents=True, exist_ok=True)
        self.inicio = datetime.now()
        slug = re.sub(r"[^\w-]+", "-", titulo.strip().lower()).strip("-") or "reuniao"
        self.path = ACTAS / f"{self.inicio:%Y-%m-%d-%H%M}-{slug}.md"
        self.titulo = titulo.strip() or "Reunião"
        self.entradas: list[dict] = []
        self._escrever()

    def adicionar(self, orador: str, texto: str) -> dict:
        e = {"t": datetime.now().strftime("%H:%M:%S"), "who": orador, "text": texto.strip()}
        self.entradas.append(e)
        with open(self.path, "a") as f:
            f.write(f"- **{e['t']}** — **{e['who']}**: {e['text']}\n")
        return e

    def renomear(self, de: str, para: str) -> None:
        for e in self.entradas:
            if e["who"] == de:
                e["who"] = para
        self._escrever()

    def reatribuir(self, mapa: dict[int, str]) -> int:
        """Aplica {índice: nome} às entradas; devolve quantas mudaram."""
        mudou = 0
        for i, e in enumerate(self.entradas):
            novo = mapa.get(i)
            if novo and e["who"] != novo:
                e["who"] = novo; mudou += 1
        if mudou:
            self._escrever()
        return mudou

    def _escrever(self) -> None:
        with open(self.path, "w") as f:
            f.write(f"# Acta — {self.titulo}\n\n")
            f.write(f"Início: {self.inicio:%Y-%m-%d %H:%M}\n\n## Intervenções\n\n")
            for e in self.entradas:
                f.write(f"- **{e['t']}** — **{e['who']}**: {e['text']}\n")

    def fechar(self) -> Path:
        with open(self.path, "a") as f:
            f.write(f"\nFim: {datetime.now():%H:%M}\n")
        return self.path
