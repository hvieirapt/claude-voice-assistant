"""voz — conversa por voz com o Claude Code, tudo local excepto o Claude.

Microfone → Silero VAD → Whisper (MLX) → Claude Agent SDK (streaming) → TTS (say/mlx/piper).
Opcional: Falante (LM Studio) reescreve respostas longas. Painel web local com o estado em tempo real.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from .cerebro import Cerebro
from .config import load_config
from .fala import Fala
from .falante import Falante, frases, limpar_markdown
from .ouvido import Ouvido
from .ui import UI

log = logging.getLogger("voz")
ESTADO = Path.home() / ".voz"
ACK = "/System/Library/Sounds/Tink.aiff"

# Confirmações curtas ditas enquanto o Claude trabalha: não são uma mensagem nova.
BACKCHANNEL = {"ok", "okay", "sim", "hum", "hmm", "certo", "está bem", "ok continua", "continua", "boa", "exacto", "exato", "pois"}
_SENT_END = re.compile(r"(?<=[.!?…])\s+")


def _say(msg: str) -> None:
    print(msg, flush=True)


class App:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.cfg = load_config(Path(args.config) if args.config else None)
        if args.no_barge_in:
            self.cfg["vad"]["barge_in"] = False
        if args.falante:
            self.cfg["falante"]["enabled"] = True
        if args.ptt:
            self.cfg["vad"]["mode"] = "ptt"
        if args.permission_mode:
            self.cfg["claude"]["permission_mode"] = args.permission_mode
        if args.model:
            self.cfg["claude"]["model"] = args.model
        if args.tts:
            self.cfg["tts"]["backend"] = args.tts
        self.cwd = Path(args.cwd).resolve()
        self._turn: asyncio.Task | None = None
        self._pergunta: asyncio.Future | None = None
        self._hold = False
        self._pending: list[str] = []
        self._state = "idle"
        self._custo_acumulado = 0.0

    # ---------- arranque ----------
    def _resolve_resume(self) -> str | None:
        r = self.args.resume
        if r == "last":
            f = ESTADO / "sessions.json"
            return json.loads(f.read_text()).get(str(self.cwd)) if f.is_file() else None
        return r

    def _guardar_sessao(self) -> None:
        if not self.cerebro.session_id:
            return
        ESTADO.mkdir(exist_ok=True)
        f = ESTADO / "sessions.json"
        data = json.loads(f.read_text()) if f.is_file() else {}
        data[str(self.cwd)] = self.cerebro.session_id
        f.write_text(json.dumps(data, indent=2))

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.events: asyncio.Queue = asyncio.Queue()
        self.ouvido = Ouvido(self.cfg, self.loop, self.events)
        self.fala = Fala(self.cfg, on_state=self._on_speaking)
        self.falante = Falante(self.cfg)
        self.cerebro = Cerebro(self.cfg, self.cwd, self._resolve_resume(), self._perguntar)
        self.ui = UI(self.cfg["ui"]["port"], self._on_ui_cmd)

        _say(f"voz · pasta {self.cwd} · config {self.cfg.get('_path', 'defaults')}")
        if self.cfg["ui"]["enabled"]:
            await self.ui.start()
            _say(f"painel: http://localhost:{self.cfg['ui']['port']}")
        self.fala.start()
        if self.cfg["falante"]["enabled"] and not await self.falante.disponivel():
            self._note("falante: LM Studio sem o modelo carregado; desligado.", warn=True)
            self.cfg["falante"]["enabled"] = False
        t0 = time.time()
        self.ouvido.start()
        await self.loop.run_in_executor(None, self.ouvido.warmup)
        _say(f"whisper pronto em {time.time() - t0:.1f}s · microfone: {self.ouvido.device_name}")
        await self.cerebro.connect()
        _say("claude ligado" + (f" (sessão {self.cerebro.session_id})" if self.cerebro.session_id else ""))
        self.ui.emit("hello", mic=self.ouvido.device_name, model=self.cerebro.model, mode=self.cfg["vad"]["mode"],
                     session=self.cerebro.session_id, tts=f"{self.cfg['tts']['backend']}/{self.fala.voice}")
        self._set_state("listening")
        await self._stdin_reader()
        _say("a ouvir. Fala, ou escreve e Enter. Comandos: /sair /mudo /ouvir /parar /ptt /auto")

        if self.args.prompt_file or self.args.prompt:
            texto = Path(self.args.prompt_file).read_text() if self.args.prompt_file else self.args.prompt
            self._start_turn(texto)
        try:
            await self._main_loop()
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        self._guardar_sessao()
        self.ouvido.stop()
        await self.fala.close()
        await self.falante.aclose()
        await self.ui.close()
        try:
            await self.cerebro.close()
        except Exception:  # noqa: BLE001
            pass
        _say("até já.")

    # ---------- ciclo principal ----------
    async def _main_loop(self) -> None:
        while True:
            kind, payload = await self.events.get()
            if kind == "level":
                self.ui.emit("level", v=payload)
            elif kind == "speech_start":
                self._hold = True
                self._set_state("recording")
                await self.fala.stop()
            elif kind == "rec_end":
                self._set_state("transcribing")
            elif kind == "speech_discarded":
                self._hold = False
                self._note(f"ignorado ({payload})")
                for frase in self._pending:
                    self.fala.dizer(frase)
                self._pending.clear()
                self._set_state("speaking" if not self.fala.queue.empty() else ("thinking" if self._turno_activo() else "listening"))
            elif kind == "utterance":
                self._hold = False
                if self.cfg["ui"]["ack_sound"]:
                    asyncio.create_task(self._ack())
                await self._on_user_text(payload)
            elif kind == "typed":
                if await self._comando(payload):
                    return
                await self._on_user_text(payload)
            elif kind == "quit":
                return

    def _turno_activo(self) -> bool:
        return self._turn is not None and not self._turn.done()

    async def _on_user_text(self, texto: str) -> None:
        _say(f"\n› {texto}")
        if self._pergunta and not self._pergunta.done():
            self.ui.emit("user", text=texto)
            self._pergunta.set_result(texto)  # resposta a um pedido de permissão
            return
        if self._turno_activo() and texto.strip(" .!?,").lower() in BACKCHANNEL:
            self._note("confirmação; o Claude continua")
            for frase in self._pending:
                self.fala.dizer(frase)
            self._pending.clear()
            self._set_state("thinking")
            return
        self.ui.emit("user", text=texto)
        self._pending.clear()
        await self.fala.stop()
        if self._turno_activo():
            await self.cerebro.interrupt()
            try:
                await self._turn
            except Exception:  # noqa: BLE001
                pass
        self._start_turn(texto)

    def _start_turn(self, texto: str) -> None:
        self._turn = asyncio.create_task(self._run_turn(texto), name="turno")

    def _falar(self, frase: str) -> None:
        frase = frase.strip()
        if not frase:
            return
        _say(f"  ♪ {frase}")
        self.ui.emit("spoken", text=frase)
        if self._hold:
            self._pending.append(frase)
        else:
            self.fala.dizer(frase)

    async def _run_turn(self, texto: str) -> None:
        t0 = time.time()
        self._set_state("thinking")
        buf = ""  # texto em streaming ainda sem fim de frase
        directo = not self.cfg["falante"]["enabled"]
        try:
            async for kind, payload in self.cerebro.turno(texto):
                if kind == "delta":
                    self.ui.emit("delta", text=payload)
                    if directo:
                        buf += payload
                        parts = _SENT_END.split(buf)
                        if len(parts) > 1:
                            for s in parts[:-1]:
                                self._falar(limpar_markdown(s))
                            buf = parts[-1]
                elif kind == "block_end":
                    self.ui.emit("block_end")
                    if directo and buf.strip():
                        self._falar(limpar_markdown(buf))
                    buf = ""
                elif kind == "text":
                    _say(f"\n◆ {payload.strip()}\n")
                    if not directo:
                        async for frase in self.falante.falar(payload):
                            self._falar(frase)
                elif kind == "tool":
                    _say(f"  ⚙ {payload}")
                    self.ui.emit("tool", text=payload)
                elif kind == "done":
                    r = payload
                    # total_cost_usd é o acumulado da sessão, em equivalente API. Com login claude.ai
                    # (subscrição) não é cobrado; conta para os limites do plano.
                    total = getattr(r, "total_cost_usd", None) or 0.0
                    delta = total - self._custo_acumulado
                    self._custo_acumulado = total
                    custo = f"≈${delta:.3f} neste turno (${total:.2f} na sessão, estimativa; coberto pela subscrição)" if total else ""
                    _say(f"  ✓ {time.time() - t0:.1f}s · {r.num_turns} passos" + (f" · {custo}" if custo else ""))
                    self.ui.emit("done", secs=round(time.time() - t0, 1), turns=r.num_turns, cost=custo)
                    self._guardar_sessao()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("turno falhou")
            self._note(f"erro no turno: {e}", warn=True)
            self.fala.dizer("Deu erro a falar com o Claude. Vê o painel.")
        finally:
            if not self.fala.queue.empty() or self._state == "speaking":
                pass  # o estado passa a "listening" quando a voz acabar (on_state)
            else:
                self._set_state("listening")

    async def _perguntar(self, pergunta: str) -> str:
        """Pedido de permissão: diz a pergunta e espera a próxima frase."""
        _say(f"\n? {pergunta}")
        self.ui.emit("note", text=f"? {pergunta}")
        await self.fala.stop()
        self.fala.dizer(pergunta)
        self._pergunta = self.loop.create_future()
        try:
            return await asyncio.wait_for(self._pergunta, timeout=120)
        except asyncio.TimeoutError:
            return "não"
        finally:
            self._pergunta = None

    async def _ack(self) -> None:
        p = await asyncio.create_subprocess_exec("afplay", "-v", "0.4", ACK, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await p.wait()

    # ---------- estado / painel ----------
    def _set_state(self, s: str) -> None:
        self._state = s
        self.ui.emit("state", state=s)

    def _note(self, text: str, warn: bool = False) -> None:
        _say(("  ! " if warn else "  · ") + text)
        self.ui.emit("note", text=text, warn=warn)

    def _on_speaking(self, speaking: bool) -> None:
        self.ouvido.speaking_out = speaking
        if speaking:
            self._set_state("speaking")
        elif self._state == "speaking":
            self._set_state("thinking" if self._turno_activo() else "listening")

    async def _on_ui_cmd(self, cmd: dict) -> None:
        t = cmd.get("type")
        if t == "text" and cmd.get("text"):
            self.events.put_nowait(("typed", str(cmd["text"])))
        elif t == "ptt":
            down = bool(cmd.get("down"))
            if down and not self.ouvido.ptt_mode:
                self.ouvido.set_ptt_mode(True); self._ptt_temp = True
            self.ouvido.ptt(down)
            if not down and getattr(self, "_ptt_temp", False) and self.cfg["vad"]["mode"] == "auto":
                # volta ao automático depois de a gravação fechar
                self.loop.call_later(0.2, self.ouvido.set_ptt_mode, False); self._ptt_temp = False
        elif t == "mute":
            on = bool(cmd.get("on")); self.ouvido.set_muted(on); self._set_state("muted" if on else "listening")
        elif t == "stop":
            await self.fala.stop(); await self.cerebro.interrupt(); self._note("parado")

    # ---------- teclado ----------
    async def _stdin_reader(self) -> None:
        def ler() -> None:
            while True:
                line = sys.stdin.readline()
                if not line:
                    self.loop.call_soon_threadsafe(self.events.put_nowait, ("quit", None)); return
                line = line.strip()
                if line:
                    self.loop.call_soon_threadsafe(self.events.put_nowait, ("typed", line))

        threading.Thread(target=ler, name="stdin", daemon=True).start()

    async def _comando(self, line: str) -> bool:
        """Devolve True para sair."""
        if line in ("/sair", "/exit", "/quit"):
            return True
        if line == "/mudo":
            self.ouvido.set_muted(True); self._set_state("muted")
        elif line == "/ouvir":
            self.ouvido.set_muted(False); self._set_state("listening")
        elif line == "/parar":
            await self.fala.stop(); await self.cerebro.interrupt(); self._note("parado")
        elif line == "/ptt":
            self.cfg["vad"]["mode"] = "ptt"; self.ouvido.set_ptt_mode(True); self.ui.emit("mode", mode="ptt"); self._note("modo push-to-talk")
        elif line == "/auto":
            self.cfg["vad"]["mode"] = "auto"; self.ouvido.set_ptt_mode(False); self.ui.emit("mode", mode="auto"); self._note("modo automático")
        else:
            return False
        self.events.put_nowait(("noop", None))
        return False


TEXTO_REF = (
    "Boa tarde. Hoje vamos rever as decisões da proposta, uma de cada vez, e no fim registo tudo no documento. "
    "Diz-me o que preferes em cada ponto, e se tiveres dúvidas pergunta à vontade."
)


def gravar_referencia(cfg: dict, segundos: int = 15) -> None:
    """Grava a tua voz como referência para clonagem (pt-PT) em ~/.voz/ref.wav + ref.txt."""
    import numpy as np
    import sounddevice as sd
    import soundfile as sf

    dev = Ouvido._resolve_device(cfg["audio"]["input_device"])
    nome = sd.query_devices(dev if dev is not None else sd.default.device[0])["name"]
    _say(f"microfone: {nome}\nLê em voz alta, com naturalidade, durante {segundos} s:\n\n  «{TEXTO_REF}»\n")
    input("Enter para começar a gravar… ")
    sr = 24000
    audio = sd.rec(int(segundos * sr), samplerate=sr, channels=1, dtype="float32", device=dev)
    for i in range(segundos, 0, -1):
        print(f"\r  a gravar… {i:2d}s ", end="", flush=True); time.sleep(1)
    sd.wait(); print()
    a = audio[:, 0]
    # corta silêncio nas pontas
    nz = np.where(np.abs(a) > 0.01)[0]
    if len(nz):
        a = a[max(0, nz[0] - sr // 4): min(len(a), nz[-1] + sr // 4)]
    ESTADO.mkdir(exist_ok=True)
    sf.write(ESTADO / "ref.wav", a, sr)
    _say("a transcrever para confirmar…")
    import mlx_whisper
    txt = mlx_whisper.transcribe(ESTADO / "ref.wav", path_or_hf_repo=cfg["stt"]["model"], language=cfg["stt"]["language"], fp16=True)["text"].strip()
    (ESTADO / "ref.txt").write_text(txt)
    _say(f"guardado em {ESTADO / 'ref.wav'} ({len(a) / sr:.1f}s)\ntranscrição: {txt}\n"
         "Se a transcrição estiver errada, corrige ~/.voz/ref.txt. A partir de agora o backend mlx usa esta voz.")


def main() -> None:
    p = argparse.ArgumentParser(prog="voz", description="Conversa por voz com o Claude Code (STT/TTS locais).")
    p.add_argument("--gravar-ref", action="store_true", help="grava 15 s da tua voz como referência pt-PT para o TTS e sai")
    p.add_argument("--cwd", default=".", help="pasta do projecto (default: actual)")
    p.add_argument("--resume", help="id de sessão do Claude, ou 'last' para a última desta pasta")
    p.add_argument("--prompt", help="primeira mensagem")
    p.add_argument("--prompt-file", help="ficheiro com a primeira mensagem")
    p.add_argument("--config", help="caminho para config.toml")
    p.add_argument("--model", help="modelo do Claude (sonnet, opus, haiku ou id completo)")
    p.add_argument("--tts", choices=["say", "mlx", "piper"], help="backend de voz")
    p.add_argument("--permission-mode", choices=["default", "acceptEdits", "bypassPermissions", "plan"])
    p.add_argument("--ptt", action="store_true", help="push-to-talk: só grava enquanto carregas")
    p.add_argument("--no-barge-in", action="store_true")
    p.add_argument("--falante", action="store_true", help="reescrever respostas com o LM Studio")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpx2", "httpcore", "claude_agent_sdk", "huggingface_hub", "urllib3", "transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.gravar_ref:
        gravar_referencia(load_config(Path(args.config) if args.config else None))
        return
    try:
        asyncio.run(App(args).run())
    except KeyboardInterrupt:
        pass
