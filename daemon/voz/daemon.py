"""Daemon de voz: ouvido + fala, sem Claude. Para ser usado por um mod do Claude desktop.

Protocolo:
  stdout  — uma linha JSON por evento: {"type": "ready"|"state"|"level"|"utterance"|"discarded"|"spoken"|"note", ...}
  HTTP    — POST /cmd em http://127.0.0.1:<porta> com JSON:
              {"type":"say","text":...}      diz um texto (dividido por frases)
              {"type":"stop"}                cala já e limpa a fila
              {"type":"ptt","down":bool}     push-to-talk
              {"type":"mute","on":bool}
              {"type":"mode","mode":"auto"|"ptt"}
              {"type":"quit"}
  O painel web em GET / continua disponível na mesma porta.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from .config import load_config
from .fala import Fala
from .falante import limpar_markdown
from .ouvido import Ouvido
from .ui import UI

# O mod lê eventos JSON no stdout do processo; tudo o resto (prints de bibliotecas) vai para stderr.
_EVENTS = sys.stdout
sys.stdout = sys.stderr

log = logging.getLogger("voz.daemon")
_SENT_END = re.compile(r"(?<=[.!?…])\s+")
SAUDACAO = {
    "pt_PT": "Olá, agora falo com esta voz.", "pt_BR": "Olá, agora eu falo com esta voz.", "en": "Hello, this is my voice now.",
    "es": "Hola, ahora hablo con esta voz.", "fr": "Bonjour, je parle maintenant avec cette voix.", "de": "Hallo, jetzt spreche ich mit dieser Stimme.",
    "it": "Ciao, ora parlo con questa voce.", "nl": "Hallo, dit is nu mijn stem.", "pl": "Cześć, teraz mówię tym głosem.",
}
ACK = "/System/Library/Sounds/Tink.aiff"


class Daemon:
    def __init__(self, cfg: dict, port: int):
        self.cfg = cfg
        self.port = port
        self._hold = False
        self._pending: list[str] = []
        self._state = "idle"
        self._acumulado = ""
        self._segmentos: list = []  # áudio de cada frase da mensagem em curso (para a passagem final)
        self._gap_task: asyncio.Task | None = None
        self.reuniao = None          # (Oradores, Acta) enquanto em modo reunião
        self._mic_antes: str | None = None
        self._ditas: list[str] = []  # últimas frases ditas pela voz (filtro de eco)
        self._muted = False
        self._expect_answer = False
        from .intencao import Intencao

        kw = cfg.get("keywords", {})
        self._intencoes = Intencao(kw.get("model", "mlx-community/Qwen3-4B-4bit"), bool(kw.get("dynamic", True)), int(kw.get("max_words", 6)))
        self._interrompeu_voz = False
        self._state_antes_de_gravar = "listening"

    async def _enviar_apos_pausa(self) -> None:
        """Espera turn_gap_ms sem nova fala e só então entrega a frase inteira."""
        try:
            await asyncio.sleep(int(self.cfg["vad"].get("turn_gap_ms", 1200)) / 1000)
        except asyncio.CancelledError:
            return
        texto, self._acumulado, self._gap_task = self._acumulado, "", None
        segmentos, self._segmentos = self._segmentos, []
        if not texto:
            return
        if self.cfg["ui"]["ack_sound"]:
            asyncio.create_task(self._ack())
        # Passagem final (padrão superwhisper): com várias frases, retranscreve o áudio inteiro de uma vez,
        # para a pontuação e a coerência saírem melhores do que a soma dos pedaços.
        if len(segmentos) > 1:
            try:
                import numpy as np

                gap = np.zeros(int(self.ouvido.sr * 0.25), dtype=np.float32)
                todo = np.concatenate([x for seg in segmentos for x in (seg, gap)])
                if len(todo) / self.ouvido.sr <= 120:
                    r = await self.ouvido.transcrever_async(todo)
                    final = (r.get("text") or "").strip()
                    if final and len(final) >= 0.6 * len(texto):
                        texto = final
            except Exception as e:  # noqa: BLE001
                log.warning("passagem final falhou: %s", e)
        from .vozes import aplicar_substituicoes

        texto = aplicar_substituicoes(texto)
        texto = await self._limpar(texto)
        self.out("utterance", text=texto)

    def out(self, type_: str, **data) -> None:
        _EVENTS.write(json.dumps({"type": type_, **data}, ensure_ascii=False) + "\n")
        _EVENTS.flush()
        if type_ != "level" or True:
            self.ui.emit(type_, **data)

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.events: asyncio.Queue = asyncio.Queue()
        self.ouvido = Ouvido(self.cfg, self.loop, self.events)
        self.fala = Fala(self.cfg, on_state=self._on_speaking)
        self.ui = UI(self.port, self._on_cmd)
        await self.ui.start()
        self.fala.start()
        self.ouvido.start()
        await self.loop.run_in_executor(None, self.ouvido.warmup)
        if self._intencoes.dinamico:
            self._intencoes.pool.submit(self._intencoes.carregar)  # em fundo: pronto em ~20 s
        saida = self._saida_actual()
        try:
            (Path.home() / ".voz").mkdir(exist_ok=True)
            (Path.home() / ".voz" / "daemon.json").write_text(json.dumps({"port": self.port, "pid": os.getpid()}))
        except Exception:  # noqa: BLE001
            pass
        self.out("ready", mic=self.ouvido.device_name, output=saida, muted=False, tts=f"{self.cfg['tts']['backend']}/{self.fala.voice}",
                 mode=self.cfg["vad"]["mode"], port=self.port, settings=self._definicoes(), voices=self.fala.vozes())
        self._set_state("listening")
        try:
            await self._main_loop()
        finally:
            self.ouvido.stop()
            await self.fala.close()
            await self.ui.close()

    async def _main_loop(self) -> None:
        while True:
            kind, payload = await self.events.get()
            if kind == "level":
                self.out("level", v=round(payload, 3))
            elif kind == "speech_start":
                self._hold = True
                if self._gap_task:  # continuaste a falar: a frase anterior ainda não vai
                    self._gap_task.cancel(); self._gap_task = None
                self._set_state("recording")
                if self._state_antes_de_gravar == "speaking" or not self.fala.queue.empty() or not self.fala._play_q.empty():
                    # a voz estava a falar: pausa (pode ser eco dela própria); decide-se quando houver transcrição
                    self._interrompeu_voz = True
                    await self.fala.pause()
                else:
                    self._interrompeu_voz = False
                    await self.fala.stop()
            elif kind == "live":
                self.out("live", text=(self._acumulado + " " + payload).strip())
            elif kind == "rec_end":
                self._set_state("transcribing")
            elif kind == "speech_discarded":
                self._hold = False
                self.out("discarded", reason=payload)
                if self._interrompeu_voz:
                    self._interrompeu_voz = False
                    self.fala.resume()
                for frase in self._pending:
                    self.fala.dizer(frase)
                self._pending.clear()
                self._set_state("speaking" if (not self.fala.queue.empty() or self.fala.paused) else "listening")
            elif kind == "utterance" and self.reuniao is not None:
                await self._reuniao_entrada(payload)
            elif kind == "utterance" and self._expect_answer:
                texto = payload["text"] if isinstance(payload, dict) else str(payload)
                self._expect_answer = False
                self._hold = False; self._interrompeu_voz = False; self._pending.clear()
                if self._gap_task:
                    self._gap_task.cancel(); self._gap_task = None
                self._acumulado = ""; self._segmentos = []
                await self.fala.stop()
                fut = getattr(self, "_resposta_fut", None)
                if fut is not None and not fut.done():
                    fut.set_result(texto)
                else:
                    self.out("utterance", text=texto, answer=True)
                self._set_state("listening")
            elif kind == "utterance" and (accao := await self._intencao(payload["text"] if isinstance(payload, dict) else str(payload))):
                self._hold = False; self._interrompeu_voz = False; self._pending.clear()
                if self._gap_task:
                    self._gap_task.cancel(); self._gap_task = None
                self._acumulado = ""; self._segmentos = []
                if accao == "skip":
                    await self.fala.skip()
                elif accao == "stop_voice":
                    await self.fala.stop()
                else:
                    await self.fala.stop()
                    self.out("abort")
                self.out("keyword", action=accao)
                self._set_state("speaking" if accao == "skip" and not self.fala.queue.empty() else "listening")
            elif kind == "utterance":
                texto = payload["text"] if isinstance(payload, dict) else str(payload)
                if self._interrompeu_voz:
                    self._interrompeu_voz = False
                    motivo = self._interrupcao_falsa(payload, texto)
                    if motivo:
                        # não eras tu (eco, respiração, ruído, frase inventada): ignora e retoma a leitura
                        self._hold = False
                        self.out("discarded", reason=f"{motivo}: {texto[:60]}")
                        self.fala.resume()
                        self._set_state("speaking")
                        continue
                self._hold = False
                self._pending.clear()
                await self.fala.stop()
                # Junta frases seguidas: só envia quando parares de falar durante turn_gap_ms.
                if isinstance(payload, dict) and payload.get("audio") is not None:
                    self._segmentos.append(payload["audio"])
                self._acumulado = (self._acumulado + " " + texto).strip()
                self.out("partial", text=self._acumulado)
                self._set_state("listening")
                if self._gap_task:
                    self._gap_task.cancel()
                self._gap_task = asyncio.create_task(self._enviar_apos_pausa())
            elif kind == "quit":
                return

    # ---------- reunião ----------
    async def _reuniao_entrada(self, payload) -> None:
        from .reuniao import RE_NOME
        from .vozes import aplicar_substituicoes

        oradores, acta = self.reuniao
        texto = payload["text"] if isinstance(payload, dict) else str(payload)
        texto = aplicar_substituicoes(texto)
        audio = payload.get("audio") if isinstance(payload, dict) else None
        idx = len(acta.entradas)
        if audio is None:
            nome, sim, novo = (oradores.nomes() or ["Orador 1"])[-1], 0.0, False
        else:
            nome, sim, novo = await self.loop.run_in_executor(None, oradores.identificar, audio, self.ouvido.sr, idx)
        # "eu sou o João" renomeia o orador desta frase
        m = RE_NOME.search(texto)
        if m and nome.startswith("Orador"):
            novo_nome = m.group(1).strip()
            if oradores.renomear(nome, novo_nome):
                acta.renomear(nome, novo_nome)
                self.out("meeting_speakers", speakers=oradores.nomes())
                nome = novo_nome
        e = acta.adicionar(nome, texto)
        self.out("meeting_entry", t=e["t"], who=nome, text=texto, similarity=round(sim, 2), new=novo, speakers=oradores.nomes())
        # de 10 em 10 intervenções, reagrupa tudo: corrige fusões e divisões erradas
        if len(acta.entradas) % 10 == 0:
            await self._reagrupar()

    async def _reagrupar(self) -> None:
        if self.reuniao is None:
            return
        oradores, acta = self.reuniao
        try:
            mapa = await self.loop.run_in_executor(None, oradores.reagrupar)
            mudou = acta.reatribuir(mapa) if mapa else 0
            self.out("meeting_speakers", speakers=oradores.nomes(), relabeled=mudou, entries=[{"t": e["t"], "who": e["who"], "text": e["text"]} for e in acta.entradas[-200:]])
        except Exception as ex:  # noqa: BLE001
            log.warning("reagrupar falhou: %s", ex)

    async def _reuniao(self, on: bool, titulo: str = "", k: int | None = None) -> dict:
        if on and self.reuniao is None:
            from .reuniao import Acta, Oradores

            self.out("note", text="a preparar o modo de reunião (modelo de orador)…")
            oradores = await self.loop.run_in_executor(None, Oradores, float(self.cfg.get("meeting", {}).get("threshold", 0.7)))
            oradores.k = k if k and k > 0 else None
            acta = Acta(titulo)
            self.reuniao = (oradores, acta)
            await self.fala.stop()
            # microfone da reunião (o do portátil apanha a sala; os AirPods só apanham quem os usa)
            mic = self.cfg.get("meeting", {}).get("input_device", "")
            if mic:
                try:
                    self._mic_antes = self.cfg["audio"]["input_device"]
                    self.out("note", text=f"microfone da reunião: {self.ouvido.set_device(mic)}")
                except Exception as e:  # noqa: BLE001
                    self.out("note", text=f"não consegui mudar o microfone ({e}); fico com {self.ouvido.device_name}")
            # frases mais longas antes de fechar (as pessoas fazem pausas) e sem juntar
            self._vad_antes = dict(self.cfg["vad"])
            self.cfg["vad"]["silence_ms"] = int(self.cfg.get("meeting", {}).get("silence_ms", 900))
            self.cfg["vad"]["max_utterance_s"] = 30
            self.ouvido.recalc()
            self._set_state("listening")
            self.out("meeting", on=True, title=acta.titulo, path=str(acta.path), speakers=[])
            return {"ok": True, "on": True, "path": str(acta.path)}
        if not on and self.reuniao is not None:
            await self._reagrupar()  # passagem final: número de oradores e atribuições definitivas
            oradores, acta = self.reuniao
            self.reuniao = None
            path = acta.fechar()
            if self._mic_antes is not None:
                try:
                    self.ouvido.set_device(self._mic_antes)
                except Exception:  # noqa: BLE001
                    pass
                self._mic_antes = None
            if getattr(self, "_vad_antes", None):
                self.cfg["vad"].update(self._vad_antes); self.ouvido.recalc()
            self.out("meeting", on=False, title=acta.titulo, path=str(path), speakers=oradores.nomes(), entries=len(acta.entradas))
            return {"ok": True, "on": False, "path": str(path), "speakers": oradores.nomes(), "entries": len(acta.entradas)}
        return {"ok": True, "on": self.reuniao is not None}

    # ---------- permissões por voz (hook PreToolUse do plugin) ----------
    def _descrever(self, tool: str, inp: dict, project: str = "") -> str:
        lang = self.cfg["tts"].get("lang", "pt_PT")
        fam = lang.split("_")[0]
        nome = lambda v: str(v or "").split("/")[-1]
        cmdline = str(inp.get("command", "")).strip() if tool == "Bash" else ""
        primeira = (re.match(r"^[\w./-]+", cmdline) or re.match("", ""))
        primeira = primeira.group(0) if primeira else ""
        T = {
            "pt": {"bash": "correr um comando de terminal", "sudo": "correr um comando com privilégios de administrador", "edit": "alterar o ficheiro", "read": "ler o ficheiro", "web": "consultar uma página web", "search": "pesquisar na web", "tool": "usar a ferramenta", "q": "O Claude quer {a}. Permites?"},
            "en": {"bash": "run a terminal command", "sudo": "run a command with administrator privileges", "edit": "change the file", "read": "read the file", "web": "fetch a web page", "search": "search the web", "tool": "use the tool", "q": "Claude wants to {a}. Allow?"},
            "es": {"bash": "ejecutar un comando de terminal", "sudo": "ejecutar un comando con privilegios de administrador", "edit": "modificar el archivo", "read": "leer el archivo", "web": "consultar una página web", "search": "buscar en la web", "tool": "usar la herramienta", "q": "Claude quiere {a}. ¿Permites?"},
            "fr": {"bash": "exécuter une commande du terminal", "sudo": "exécuter une commande avec les droits administrateur", "edit": "modifier le fichier", "read": "lire le fichier", "web": "consulter une page web", "search": "chercher sur le web", "tool": "utiliser l'outil", "q": "Claude veut {a}. Tu permets ?"},
            "de": {"bash": "einen Terminal-Befehl ausführen", "sudo": "einen Befehl mit Administratorrechten ausführen", "edit": "die Datei ändern", "read": "die Datei lesen", "web": "eine Webseite abrufen", "search": "im Web suchen", "tool": "das Werkzeug benutzen", "q": "Claude möchte {a}. Erlaubst du?"},
            "it": {"bash": "eseguire un comando del terminale", "sudo": "eseguire un comando con privilegi di amministratore", "edit": "modificare il file", "read": "leggere il file", "web": "consultare una pagina web", "search": "cercare sul web", "tool": "usare lo strumento", "q": "Claude vuole {a}. Permetti?"},
        }
        t = T.get(fam, T["en"])
        if tool == "Bash":
            a = t["sudo"] if "sudo" in cmdline else (f"{t['bash']}, {primeira}" if primeira else t["bash"])
        elif tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            a = f"{t['edit']} {nome(inp.get('file_path'))}"
        elif tool == "Read":
            a = f"{t['read']} {nome(inp.get('file_path'))}"
        elif tool == "WebFetch":
            a = t["web"]
        elif tool == "WebSearch":
            a = t["search"]
        else:
            a = f"{t['tool']} {tool}"
        q = t["q"].format(a=a)
        if project:
            # pedido vindo de outra sessão: diz de que projecto é
            P = {"pt": "No projecto {p}: ", "en": "In project {p}: ", "es": "En el proyecto {p}: ", "fr": "Dans le projet {p} : ", "de": "Im Projekt {p}: ", "it": "Nel progetto {p}: "}
            q = P.get(fam, P["en"]).format(p=project) + q
        return q

    async def _perguntar_permissao(self, tool: str, inp: dict, timeout: float, project: str = "") -> dict:
        """Diz a pergunta, espera a próxima frase (modo resposta) e classifica sim/não. Devolve {decision, text}."""
        if self.reuniao is not None or self._muted:
            return {"decision": "none", "reason": "em reunião ou microfone em pausa"}
        pergunta = self._descrever(tool, inp, project)
        self.out("note", text=f"? {pergunta}" + (f"\n  {str(inp.get('command',''))[:300]}" if tool == "Bash" else ""))
        await self.fala.stop()
        self._resposta_fut = self.loop.create_future()
        self._expect_answer = True
        if self._gap_task:
            self._gap_task.cancel(); self._gap_task = None
            self._acumulado = ""; self._segmentos = []
        for frase in _SENT_END.split(pergunta):
            self.fala.dizer(frase)
        try:
            texto = await asyncio.wait_for(self._resposta_fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._expect_answer = False
            self.out("note", text="sem resposta por voz; o diálogo fica à tua espera")
            return {"decision": "none", "reason": "sem resposta"}
        finally:
            self._resposta_fut = None
        d = self._sim_ou_nao(texto)
        self.out("note", text={"allow": "permitido por voz", "deny": "recusado por voz"}.get(d, "não era uma resposta ao pedido; o diálogo fica à tua espera"))
        return {"decision": d or "none", "text": texto}

    @staticmethod
    def _sim_ou_nao(texto: str) -> str | None:
        SIM = ["sim", "pode", "permit", "autoriz", "aprov", "força", "avanç", "avanc", "ok", "claro", "deixa", "vai", "yes", "yeah", "yep", "allow", "approve", "go", "sure", "sí", "si", "vale", "oui", "ja", "okay", "va bene", "certo"]
        NAO = ["não", "nao", "nega", "recus", "cancel", "pára", "para", "nunca", "no", "nope", "deny", "nein", "non", "nee", "bloque", "proib"]
        t = re.sub(r"[.!?,;:¡¿]", " ", texto.lower())
        w = t.split()
        if not w or len(w) > 6:
            return None
        head = w[:2]
        bate = lambda x, raizes: any(x == r or (len(r) >= 4 and x.startswith(r)) for r in raizes)
        if any(bate(x, NAO) for x in head):
            return "deny"
        if any(bate(x, SIM) for x in head):
            return "allow"
        return None

    # ---------- comandos ----------
    async def _on_cmd(self, cmd: dict) -> None:
        t = cmd.get("type")
        if t == "meeting":
            k = cmd.get("speakers")
            return await self._reuniao(bool(cmd.get("on")), str(cmd.get("title", "")), int(k) if k else None)
        if t == "meeting_rename" and self.reuniao is not None:
            oradores, acta = self.reuniao
            de, para = str(cmd.get("speaker", "")), str(cmd.get("name", ""))
            ok = oradores.renomear(de, para)
            if ok:
                acta.renomear(de, para)
                self.out("meeting_speakers", speakers=oradores.nomes())
            return {"ok": ok, "speakers": oradores.nomes()}
        if t == "say":
            for s in _SENT_END.split(limpar_markdown(str(cmd.get("text", "")))):
                s = s.strip()
                if not s:
                    continue
                self._ditas.append(s)
                del self._ditas[:-12]
                self.out("spoken", text=s)
                if self._hold:
                    self._pending.append(s)
                else:
                    self.fala.dizer(s)
        elif t == "stop":
            self._pending.clear()
            await self.fala.stop()
            self._set_state("listening")
        elif t == "ptt":
            down = bool(cmd.get("down"))
            if down and not self.ouvido.ptt_mode:
                self.ouvido.set_ptt_mode(True); self._ptt_temp = True
            self.ouvido.ptt(down)
            if not down and getattr(self, "_ptt_temp", False) and self.cfg["vad"]["mode"] == "auto":
                self.loop.call_later(0.2, self.ouvido.set_ptt_mode, False); self._ptt_temp = False
        elif t == "mute":
            on = bool(cmd.get("on"))
            self._muted = on
            self.ouvido.set_muted(on)
            if on:
                # pausa a sério: nada do que estava a meio chega ao Claude
                if self._gap_task:
                    self._gap_task.cancel(); self._gap_task = None
                self._acumulado = ""; self._segmentos = []; self._pending.clear(); self._hold = False
                await self.fala.stop()
            self.out("mute", on=on)  # o daemon é a fonte de verdade sobre a pausa do microfone
            self._set_state("muted" if on else "listening")
        elif t == "mode":
            mode = "ptt" if cmd.get("mode") == "ptt" else "auto"
            self.cfg["vad"]["mode"] = mode; self.ouvido.set_ptt_mode(mode == "ptt"); self.out("mode", mode=mode)
        elif t == "text":  # do painel web: entrega como se fosse ditado
            self.out("utterance", text=str(cmd.get("text", "")))
        elif t == "ask":
            return await self._perguntar_permissao(str(cmd.get("tool", "")), cmd.get("input") or {}, float(cmd.get("timeout", 20)), str(cmd.get("project") or ""))
        elif t == "expect":
            # o mod fez uma pergunta (permissão): a próxima frase, mesmo curta, é resposta e sai de imediato
            self._expect_answer = bool(cmd.get("on"))
            if self._expect_answer and self._gap_task:
                self._gap_task.cancel(); self._gap_task = None
                self._acumulado = ""; self._segmentos = []
        elif t == "voices":
            return {"voices": self.fala.vozes(), "current": self.fala.voz_actual()}
        elif t == "voices_search":
            from .vozes import procurar

            lang = cmd.get("lang") or self.cfg["tts"].get("lang", "pt_PT")
            if cmd.get("all"):
                lang = None
            res = await self.loop.run_in_executor(None, procurar, lang, cmd.get("query"), self.fala.refs())
            return {"voices": res, "lang": lang}
        elif t == "voice_install":
            self.out("note", text=f"a instalar e activar a voz {cmd.get('id')}…")
            r = await self._definir({"voice": cmd.get("id")})
            return {**r, "voices": self.fala.vozes()}
        elif t == "languages":
            from .vozes import LINGUAS

            return {"languages": [{"code": k, "label": v} for k, v in LINGUAS.items()]}
        elif t == "devices":
            return self._dispositivos()
        elif t == "get":
            return self._definicoes()
        elif t == "set":
            return await self._definir(cmd)
        elif t == "quit":
            # reason "handover": outra sessão fica com a voz; o mod desta não deve mostrar erro
            self.out("bye", reason=str(cmd.get("reason") or ""))
            self.events.put_nowait(("quit", None))
        return None

    # ---------- definições ao vivo ----------
    SENSIBILIDADE = {  # start, end, min_speech_ms
        "baixa": (0.6, 0.4, 200),
        "media": (0.45, 0.3, 100),
        "alta": (0.3, 0.2, 60),
    }

    async def _limpar(self, texto: str) -> str:
        """Limpeza de muletas conforme a definição: off | regras | modelo."""
        from .limpeza import Modelo, regras

        modo = self.cfg.get("cleanup", {}).get("mode", "regras")
        if modo == "off":
            return texto
        limpo = regras(texto, self.cfg["tts"].get("lang", "pt_PT"))
        if modo == "modelo":
            if not hasattr(self, "_modelo_limpeza"):
                self._modelo_limpeza = Modelo(self.cfg.get("cleanup", {}).get("model", "mlx-community/Qwen3-4B-4bit"))
            try:
                limpo = await asyncio.wait_for(self.loop.run_in_executor(self._modelo_limpeza.pool, self._modelo_limpeza.limpar, limpo), timeout=4)
            except Exception as e:  # noqa: BLE001
                log.warning("limpeza por modelo falhou: %s", e)
        if limpo != texto:
            self.out("note", text=f"limpo: {limpo}")
        return limpo

    def _definicoes(self) -> dict:
        v = self.cfg["vad"]
        sens = "media"
        for k, (s, _, _) in self.SENSIBILIDADE.items():
            if abs(v["start_threshold"] - s) < 0.01:
                sens = k
        return {
            "voice": self.fala.voz_actual(),
            "sensitivity": sens,
            "turn_gap_ms": int(v.get("turn_gap_ms", 1200)),
            "silence_ms": int(v["silence_ms"]),
            "mode": v["mode"],
            "barge_in": bool(v["barge_in"]),
            "speed": float(self.cfg["tts"].get("speed", 1.0)),
            "language": self.cfg["tts"].get("lang", "pt_PT"),
            "cleanup": self.cfg.get("cleanup", {}).get("mode", "regras"),
            "input_device": str(self.cfg["audio"].get("input_device") or ""),
            "output_device": str(self.cfg["tts"].get("output_device") or ""),
        }

    # ---------- dispositivos de áudio ----------
    @staticmethod
    def _saida_actual() -> str:
        try:
            import sounddevice as sd

            idx = sd.default.device[1]
            info = sd.query_devices(idx) if idx is not None and idx >= 0 else sd.query_devices(kind="output")
            return str(info["name"])
        except Exception:  # noqa: BLE001
            return "?"

    def _definir_saida(self, spec: str) -> str:
        """Escolhe a saída de som para as vozes Qwen3/Piper (sounddevice) e Apple (say -a). '' = por omissão."""
        import sounddevice as sd

        if not spec:
            sd.default.device[1] = None
            self.cfg["tts"]["output_device"] = ""
            return self._saida_actual()
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] > 0 and spec.lower() in str(d["name"]).lower():
                sd.default.device[1] = i
                self.cfg["tts"]["output_device"] = str(d["name"])
                return str(d["name"])
        raise RuntimeError(f"saída {spec!r} não encontrada")

    def _dispositivos(self) -> dict:
        import sounddevice as sd

        virtual = ("blackhole", "loopback", "soundflower", "aggregate", "multi-output", "agregado")
        ins, outs = [], []
        for d in sd.query_devices():
            nome = str(d["name"])
            etiqueta = nome + (" (virtual)" if any(k in nome.lower() for k in virtual) else "")
            if d["max_input_channels"] > 0 and all(x["id"] != nome for x in ins):
                ins.append({"id": nome, "label": etiqueta})
            if d["max_output_channels"] > 0 and all(x["id"] != nome for x in outs):
                outs.append({"id": nome, "label": etiqueta})
        return {"inputs": ins, "outputs": outs, "input": self.ouvido.device_name, "output": self._saida_actual()}

    async def _definir(self, cmd: dict) -> dict:
        v = self.cfg["vad"]
        mudou = []
        if "sensitivity" in cmd and cmd["sensitivity"] in self.SENSIBILIDADE:
            s, e, m = self.SENSIBILIDADE[cmd["sensitivity"]]
            v["start_threshold"], v["end_threshold"], v["min_speech_ms"] = s, e, m
            mudou.append("sensibilidade")
        if "turn_gap_ms" in cmd:
            v["turn_gap_ms"] = max(300, min(5000, int(cmd["turn_gap_ms"]))); mudou.append("pausa")
        if "silence_ms" in cmd:
            v["silence_ms"] = max(300, min(3000, int(cmd["silence_ms"]))); mudou.append("silêncio")
        if "barge_in" in cmd:
            v["barge_in"] = bool(cmd["barge_in"]); mudou.append("interrupção")
        if "mode" in cmd:
            mode = "ptt" if cmd["mode"] == "ptt" else "auto"
            v["mode"] = mode; self.ouvido.set_ptt_mode(mode == "ptt"); self.out("mode", mode=mode); mudou.append("modo")
        self.ouvido.recalc()
        if "language" in cmd:
            from .vozes import LINGUAS, familia, voz_say_para

            lang = str(cmd["language"])
            if lang in LINGUAS:
                self.cfg["tts"]["lang"] = lang
                self.cfg["stt"]["language"] = familia(lang)
                if familia(lang) != "pt":
                    self.cfg["stt"]["initial_prompt"] = ""
                mudou.append("língua")
                # a voz tem de falar a língua. O Qwen3 (clone/preset) cobre 10 línguas; uma voz da Apple
                # ou Piper é de uma língua só, por isso troca-se: Apple se houver, senão a primeira Piper.
                if "voice" not in cmd:
                    from .vozes import QWEN_LANGS, procurar

                    backend = self.cfg["tts"]["backend"]
                    if not (backend == "mlx" and familia(lang) in QWEN_LANGS):
                        v = voz_say_para(lang)
                        if v:
                            cmd = {**cmd, "voice": f"say:{v}"}
                            self.out("note", text=f"voz da Apple para {LINGUAS[lang]}: {v}")
                        else:
                            piper = [x for x in procurar(lang, None, self.fala.refs()) if x["source"] == "piper"]
                            if piper:
                                cmd = {**cmd, "voice": piper[0]["id"]}
                                self.out("note", text=f"sem voz da Apple para {LINGUAS[lang]}; a instalar {piper[0]['label']}")
        if "cleanup" in cmd and cmd["cleanup"] in ("off", "regras", "modelo"):
            self.cfg.setdefault("cleanup", {})["mode"] = cmd["cleanup"]; mudou.append("limpeza")
            if cmd["cleanup"] == "modelo":
                from .limpeza import Modelo

                if not hasattr(self, "_modelo_limpeza"):
                    self._modelo_limpeza = Modelo(self.cfg.get("cleanup", {}).get("model", "mlx-community/Qwen3-4B-4bit"))
                self._modelo_limpeza.pool.submit(self._modelo_limpeza._carregar)  # pré-carrega
        quiet = bool(cmd.get("quiet"))  # repor definições guardadas: aplica sem dizer nada
        if "input_device" in cmd and str(cmd["input_device"] or "") != str(self.cfg["audio"].get("input_device") or ""):
            try:
                nome = self.ouvido.set_device(str(cmd["input_device"] or ""))
                self.cfg["audio"]["input_device"] = str(cmd["input_device"] or "")
                mudou.append("microfone")
                self.out("devices", mic=nome, output=self._saida_actual())
            except Exception as e:  # noqa: BLE001
                self.out("note", text=f"não consegui mudar o microfone: {e}")
        if "output_device" in cmd and str(cmd["output_device"] or "") != str(self.cfg["tts"].get("output_device") or ""):
            try:
                nome = self._definir_saida(str(cmd["output_device"] or ""))
                mudou.append("saída")
                self.out("devices", mic=self.ouvido.device_name, output=nome)
            except Exception as e:  # noqa: BLE001
                self.out("note", text=f"não consegui mudar a saída de som: {e}")
        if "speed" in cmd:
            self.cfg["tts"]["speed"] = max(0.6, min(1.6, float(cmd["speed"]))); mudou.append("velocidade")
            if "voice" not in cmd and not quiet:
                await self.fala.stop()
                self.fala.dizer("Agora falo a esta velocidade.")
        if "voice" in cmd:
            self.out("note", text="a mudar a voz…")
            try:
                desc = await self.fala.mudar_voz(str(cmd["voice"]))
                self.out("note", text=f"voz: {desc}"); mudou.append("voz")
                if not quiet:
                    self.fala.dizer(SAUDACAO.get(self.cfg["tts"].get("lang", "pt_PT"), SAUDACAO["en"]))
            except Exception as e:  # noqa: BLE001
                self.out("note", text=f"não consegui mudar a voz: {e}")
        defs = self._definicoes()
        if mudou:
            self.out("settings", **defs)  # o mod guarda-as: uma mudança feita por voz ou pelo painel web fica para a próxima sessão
        return {"ok": True, "changed": mudou, **defs}

    async def _ack(self) -> None:
        p = await asyncio.create_subprocess_exec("afplay", "-v", "0.4", ACK, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await p.wait()

    def _set_state(self, s: str) -> None:
        if s == "recording":
            self._state_antes_de_gravar = self._state
        if s == "listening" and self._muted:
            s = "muted"  # com o microfone em pausa nunca se mostra "a ouvir"
        self._state = s
        self.out("state", state=s)

    async def _intencao(self, texto: str) -> str | None:
        """skip | stop_voice | abort | None. Sementes multilingues de imediato; frases curtas sem semente vão ao modelo local."""
        a = self._intencoes.por_sementes(texto)
        if a or not self._intencoes.dinamico or len(texto.split()) > self._intencoes.max_palavras:
            return a
        try:
            return await asyncio.wait_for(self.loop.run_in_executor(self._intencoes.pool, self._intencoes.por_modelo, texto), timeout=3)
        except Exception as e:  # noqa: BLE001
            log.warning("intenção por modelo falhou: %s", e)
            return None

    def _interrupcao_falsa(self, payload, texto: str) -> str | None:
        """Enquanto a voz fala, só uma intervenção clara a interrompe: ≥ 3 palavras, ≥ 1 s de áudio,
        confiança razoável do Whisper, e diferente do que a voz estava a dizer."""
        if self._parece_eco(texto):
            return "eco da voz"
        if isinstance(payload, dict):
            dur = float(payload.get("dur") or 0)
            lp = float(payload.get("logprob") or 0)
            if dur and dur < float(self.cfg["vad"].get("interrupt_min_s", 1.0)):
                return f"curta demais para interromper ({dur:.1f}s)"
            if lp < -0.8:
                return f"pouca confiança para interromper ({lp:.2f})"
        if len(texto.split()) < int(self.cfg["vad"].get("interrupt_min_words", 3)):
            return "poucas palavras para interromper"
        return None

    def _parece_eco(self, texto: str) -> bool:
        """Verdadeiro se a transcrição se parece com o que a voz estava a dizer (palavras em comum ≥ 50 %)."""
        import re

        palavras = lambda t: set(w for w in re.findall(r"\w+", t.lower()) if len(w) > 2)
        p = palavras(texto)
        if not p:
            return True
        dito = palavras(" ".join(self._ditas[-6:]))
        if not dito:
            return False
        return len(p & dito) / len(p) >= 0.5

    def _on_speaking(self, speaking: bool) -> None:
        self.ouvido.speaking_out = speaking
        if speaking:
            self._set_state("speaking")
        elif self._state == "speaking":
            self._set_state("listening")


def main() -> None:
    p = argparse.ArgumentParser(prog="voz-daemon", description="Ouvido e fala locais para o mod do Claude desktop.")
    p.add_argument("--port", type=int, default=0, help="porta HTTP (default: ui.port do config)")
    p.add_argument("--config")
    p.add_argument("--ptt", action="store_true")
    p.add_argument("--tts", choices=["say", "mlx", "piper"])
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpx2", "httpcore", "huggingface_hub", "urllib3", "transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = load_config(Path(args.config) if args.config else None)
    if args.ptt:
        cfg["vad"]["mode"] = "ptt"
    if args.tts:
        cfg["tts"]["backend"] = args.tts
    try:
        asyncio.run(Daemon(cfg, args.port or int(cfg["ui"]["port"])).run())
    except KeyboardInterrupt:
        pass
    except Exception as e:  # noqa: BLE001
        logging.getLogger("voz").exception("o daemon terminou com erro")
        _EVENTS.write(json.dumps({"type": "fatal", "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False) + "\n")
        _EVENTS.flush()
        sys.exit(1)


if __name__ == "__main__":
    main()
