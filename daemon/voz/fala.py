"""Fala: fila de frases → TTS local, com paragem imediata (barge-in).

Backends:
  say    — voz do macOS. Usa automaticamente "<voz> (Premium)"/"(Enhanced)" se estiver instalada
           (Definições → Acessibilidade → Conteúdo falado → Voz do sistema → gerir vozes).
  mlx    — Qwen3-TTS via mlx-audio, neural, gerado localmente por frase e tocado com afplay.
  piper  — `piper` CLI com um modelo .onnx; toca com afplay.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("voz.fala")


def vozes_say() -> list[str]:
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return []
    nomes = []
    for line in out.splitlines():
        # formato: "Joana (Premium)     pt_PT    # Olá..."
        nome = line.split("  ")[0].strip()
        if nome:
            nomes.append(nome)
    return nomes


class Fala:
    def __init__(self, cfg: dict[str, Any], on_state: Callable[[bool], None] | None = None):
        self.cfg = cfg["tts"]
        self.on_state = on_state or (lambda speaking: None)
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._gen = 0  # geração: frases de gerações antigas são descartadas após stop()
        self._idle = asyncio.Event(); self._idle.set()
        self._paused = False
        self._skip = False
        self._resume_evt = asyncio.Event(); self._resume_evt.set()
        self.voice = str(self.cfg["voice"])
        self._mlx = None
        self._mlx_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")

    def start(self) -> None:
        # pipeline: a fila de frases alimenta um produtor (gera áudio à frente) e um leitor (toca por ordem)
        self._play_q: asyncio.Queue = asyncio.Queue(maxsize=3)
        if self.cfg["backend"] == "say" and self.cfg.get("prefer_premium", True):
            instaladas = vozes_say()
            for suf in (" (Premium)", " (Enhanced)"):
                if self.voice + suf in instaladas:
                    self.voice = self.voice + suf
                    break
            log.info("voz: %s", self.voice)
        elif self.cfg["backend"] == "mlx":
            try:
                self._carregar_mlx()
            except Exception as e:  # noqa: BLE001
                log.error("TTS mlx falhou (%s); a usar `say`", e)
                self.cfg["backend"] = "say"
                self.voice = str(self.cfg["voice"])
        self._task = asyncio.create_task(self._producer(), name="fala-gera")
        self._player = asyncio.create_task(self._consumer(), name="fala-toca")

    # ---------- escolha de voz ao vivo ----------
    def refs(self) -> list[Path]:
        refs = []
        gravada = Path.home() / ".voz" / "ref.wav"
        if gravada.is_file():
            refs.append(gravada)
        ref_dir = Path(__file__).resolve().parent.parent / "ref"
        if ref_dir.is_dir():
            refs += sorted(ref_dir.glob("*.wav"))
        return refs

    def vozes(self, lang: str | None = None) -> list[dict]:
        """Vozes para a língua actual (ou dada), instaladas primeiro."""
        from .vozes import procurar

        return procurar(lang or self.cfg.get("lang", "pt_PT"), None, self.refs())

    def voz_actual(self) -> str:
        b = self.cfg["backend"]
        if b == "mlx":
            return f"mlx:ref:{self.cfg.get('mlx_ref_audio')}" if self.cfg.get("mlx_ref_audio") else f"mlx:voice:{self.cfg['mlx_voice']}"
        if b == "piper":
            return f"piper:{Path(str(self.cfg['voice'])).stem}"
        return f"say:{self.voice}"

    async def mudar_voz(self, voz_id: str) -> str:
        """Muda a voz ao vivo (instala a voz Piper se faltar). Devolve uma descrição curta."""
        from .vozes import instalar_piper, piper_model_path

        await self.stop()
        kind, _, rest = voz_id.partition(":")
        if kind == "say":
            self.cfg["backend"] = "say"
            self.voice = rest or str(self.cfg["voice"])
            return f"say/{self.voice}"
        if kind == "piper":
            modelo = piper_model_path(rest)
            if not modelo.is_file():
                modelo = await asyncio.get_running_loop().run_in_executor(None, instalar_piper, rest)
            self.cfg["backend"] = "piper"
            self.cfg["voice"] = str(modelo)
            self.voice = rest
            return f"piper/{rest}"
        sub, _, valor = rest.partition(":")
        self.cfg["backend"] = "mlx"
        if sub == "ref":
            self.cfg["mlx_ref_audio"] = valor
            self.cfg["mlx_ref_text"] = ""
        else:
            self.cfg["mlx_ref_audio"] = ""
            self.cfg["mlx_voice"] = valor
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._mlx_pool, self._carregar_mlx)
        return f"mlx/{self.voice}"

    def _carregar_mlx(self) -> None:
        """Carrega o Qwen3-TTS. Com `mlx_ref_audio` clona essa voz (modelo Base); senão usa `mlx_voice` (CustomVoice).
        Faz uma geração de aquecimento para a primeira frase real não demorar."""
        import logging as _l

        from mlx_audio.tts.utils import load_model  # import tardio

        _l.getLogger("transformers").setLevel(_l.ERROR)
        ref = self.cfg.get("mlx_ref_audio") or ""
        self._ref_audio = str(Path(ref).expanduser()) if ref else None
        self._ref_text = self.cfg.get("mlx_ref_text") or ""
        if self._ref_audio and not self._ref_text:
            txt = Path(self._ref_audio).with_suffix(".txt")
            if txt.is_file():
                self._ref_text = txt.read_text().strip()
        log.info("a carregar TTS %s%s…", self.cfg["mlx_model"], f" (clone de {Path(self._ref_audio).name})" if self._ref_audio else "")
        # o modelo Base clona; os presets (CustomVoice) são outro checkpoint
        modelo = self.cfg["mlx_model"] if self._ref_audio else self.cfg["mlx_model"].replace("-Base-", "-CustomVoice-")
        if getattr(self, "_mlx_modelo", None) != modelo:
            self._mlx = load_model(modelo)
            self._mlx_modelo = modelo
        for _ in self._gerar_mlx("Olá."):
            break
        self.voice = Path(self._ref_audio).stem if self._ref_audio else str(self.cfg["mlx_voice"])

    def _gerar_mlx(self, frase: str):
        from .vozes import familia

        kw = dict(text=frase, lang_code=familia(self.cfg.get("lang", "pt")) or "auto", verbose=False, speed=self.speed)
        if self._ref_audio:
            kw.update(ref_audio=self._ref_audio, ref_text=self._ref_text)
        else:
            kw.update(voice=self.cfg["mlx_voice"])
        return self._mlx.generate(**kw)

    async def close(self) -> None:
        await self.stop()
        for t in (self._task, getattr(self, "_player", None)):
            if t:
                t.cancel()
        self._mlx_pool.shutdown(wait=False)

    def dizer(self, frase: str) -> None:
        frase = frase.strip()
        if frase:
            self._idle.clear()
            self.queue.put_nowait(f"{self._gen}\x00{frase}")

    async def esperar(self) -> None:
        await self._idle.wait()

    async def skip(self) -> None:
        """Salta a frase que está a tocar e continua com a seguinte."""
        self._skip = True
        p = self._proc
        if p and p.returncode is None:
            try:
                p.kill()
            except ProcessLookupError:
                pass
        try:
            import sounddevice as sd

            sd.stop()
        except Exception:  # noqa: BLE001
            pass

    async def pause(self) -> None:
        """Interrompe o que está a tocar mas guarda a fila: resume() repete a frase cortada e continua."""
        if self._paused:
            return
        self._paused = True
        self._resume_evt.clear()
        p = self._proc
        if p and p.returncode is None:
            try:
                p.kill()
            except ProcessLookupError:
                pass
        try:
            import sounddevice as sd

            sd.stop()
        except Exception:  # noqa: BLE001
            pass

    def resume(self) -> None:
        """Retoma depois de um pause(): a frase interrompida volta a tocar do início."""
        if not self._paused:
            return
        self._paused = False
        self._resume_evt.set()

    @property
    def paused(self) -> bool:
        return self._paused

    async def stop(self) -> None:
        """Pára já: mata o processo actual, pára o áudio e descarta as filas."""
        self._paused = False
        self._resume_evt.set()
        self._gen += 1
        for q in (self.queue, getattr(self, "_play_q", None)):
            while q is not None and not q.empty():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break
        p = self._proc
        if p and p.returncode is None:
            try:
                p.kill()
            except ProcessLookupError:
                pass
        try:
            import sounddevice as sd

            sd.stop()
        except Exception:  # noqa: BLE001
            pass
        self._idle.set()

    # ---------- interno ----------
    async def _producer(self) -> None:
        """Converte frases em áudio à frente do leitor (até 2 em espera)."""
        while True:
            item = await self.queue.get()
            gen_s, frase = item.split("\x00", 1)
            gen = int(gen_s)
            if gen != self._gen:
                continue
            try:
                if self.cfg["backend"] == "mlx":
                    audio = await asyncio.get_running_loop().run_in_executor(self._mlx_pool, self._gerar_audio_mlx, frase)
                    if gen != self._gen or audio is None:
                        continue
                    await self._play_q.put((gen, "audio", audio))
                elif self.cfg["backend"] == "piper":
                    audio = await asyncio.get_running_loop().run_in_executor(self._mlx_pool, self._gerar_audio_piper, frase)
                    if gen != self._gen or audio is None:
                        continue
                    await self._play_q.put((gen, "audio", audio))
                else:
                    await self._play_q.put((gen, "text", frase))
            except Exception as e:  # noqa: BLE001
                # nunca ficar em silêncio: se a voz escolhida falhar, a frase sai pela voz do sistema
                log.error("tts gerar (%s): %s — a usar say", self.cfg["backend"], e)
                if gen == self._gen:
                    await self._play_q.put((gen, "text", frase))

    async def _consumer(self) -> None:
        """Toca por ordem o que o produtor preparou."""
        while True:
            gen, kind, payload = await self._play_q.get()
            if gen != self._gen:
                continue
            self.on_state(True)
            try:
                while True:
                    await self._resume_evt.wait()
                    if gen != self._gen:
                        break
                    if kind == "audio":
                        await self._play_audio(payload)
                    else:
                        await self._speak_say(payload)
                    if self._skip:
                        self._skip = False
                        break  # saltada: segue para a próxima frase
                    if not self._paused:
                        break  # tocou até ao fim; se foi pausada a meio, repete depois do resume()
            except Exception as e:  # noqa: BLE001
                log.error("tts: %s", e)
            finally:
                if self.queue.empty() and self._play_q.empty():
                    self.on_state(False)
                    self._idle.set()

    def _gerar_audio_mlx(self, frase: str):
        """Gera e corta o silêncio das pontas. Devolve (np.ndarray float32, sample_rate) ou None."""
        import numpy as np

        for r in self._gerar_mlx(frase):
            a = np.asarray(r.audio, dtype=np.float32).reshape(-1)
            sr = int(r.sample_rate)
            # corta o silêncio das pontas (o modelo deixa até meio segundo) e encurta pausas
            # internas longas (mais de 450 ms) para 220 ms, para o ponto final soar natural
            loud = np.where(np.abs(a) > 0.008)[0]
            if len(loud):
                margem = int(sr * 0.04)
                a = a[max(0, loud[0] - margem): min(len(a), loud[-1] + margem)]
                a = self._encurtar_pausas(a, sr)
            return a, sr
        return None

    @staticmethod
    def _encurtar_pausas(a, sr: int, max_ms: int = 450, keep_ms: int = 220, thr: float = 0.008):
        """Reduz silêncios internos maiores que max_ms para keep_ms, por blocos de 10 ms."""
        import numpy as np

        hop = int(sr * 0.01)
        if len(a) < hop * 4:
            return a
        n = len(a) // hop
        rms = np.sqrt(np.mean(a[: n * hop].reshape(n, hop) ** 2, axis=1))
        quiet = rms < thr
        out = []
        i = 0
        max_n, keep_n = max_ms // 10, keep_ms // 10
        while i < n:
            if not quiet[i]:
                out.append(a[i * hop: (i + 1) * hop]); i += 1; continue
            j = i
            while j < n and quiet[j]:
                j += 1
            run = j - i
            if run > max_n:
                out.append(a[i * hop: (i + keep_n) * hop])
            else:
                out.append(a[i * hop: j * hop])
            i = j
        out.append(a[n * hop:])
        return np.concatenate(out) if out else a

    async def _play_audio(self, payload) -> None:
        import sounddevice as sd

        audio, sr = payload

        def tocar() -> None:
            sd.play(audio, sr, blocking=False)
            sd.wait()

        await asyncio.get_running_loop().run_in_executor(None, tocar)

    @property
    def speed(self) -> float:
        return float(self.cfg.get("speed", 1.0))

    async def _speak_say(self, frase: str) -> None:
        saida = [] if not self.cfg.get("output_device") else ["-a", str(self.cfg["output_device"])]
        self._proc = await asyncio.create_subprocess_exec(
            "say", "-v", self.voice, "-r", str(int(float(self.cfg["rate"]) * self.speed)), *saida, frase,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await self._proc.wait()

    async def _play(self, wav: str) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            "afplay", wav, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await self._proc.wait()

    async def _speak_mlx(self, frase: str, gen: int) -> None:
        import soundfile as sf

        def gerar() -> str | None:
            fd, wav = tempfile.mkstemp(suffix=".wav", prefix="voz-"); os.close(fd)
            for r in self._gerar_mlx(frase):
                sf.write(wav, r.audio, r.sample_rate)
                return wav
            return None

        wav = await asyncio.get_running_loop().run_in_executor(self._mlx_pool, gerar)
        if not wav:
            return
        try:
            if gen == self._gen:  # pode ter havido stop() durante a geração
                await self._play(wav)
        finally:
            try:
                os.unlink(wav)
            except OSError:
                pass

    def _gerar_audio_piper(self, frase: str):
        """Piper pela API Python (o CLI do pacote tem o caminho do espeak-ng partido). Devolve (float32, sr)."""
        import io
        import wave

        import numpy as np
        from piper import PiperVoice, SynthesisConfig

        modelo = str(self.cfg["voice"])
        if getattr(self, "_piper_modelo", None) != modelo:
            self._piper = PiperVoice.load(modelo)
            self._piper_modelo = modelo
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            self._piper.synthesize_wav(frase, w, syn_config=SynthesisConfig(length_scale=1.0 / max(0.5, self.speed)))
        buf.seek(0)
        with wave.open(buf, "rb") as w:
            sr = w.getframerate()
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        return a, sr

    async def _speak_piper(self, frase: str) -> None:
        fd, wav = tempfile.mkstemp(suffix=".wav", prefix="voz-"); os.close(fd)
        try:
            from .vozes import piper_bin

            self._proc = await asyncio.create_subprocess_exec(
                piper_bin(), "--model", str(self.cfg["voice"]), "--length_scale", f"{1.0 / self.speed:.2f}", "--output_file", wav,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await self._proc.communicate(frase.encode())
            if self._proc.returncode == 0:
                await self._play(wav)
        finally:
            try:
                os.unlink(wav)
            except OSError:
                pass
