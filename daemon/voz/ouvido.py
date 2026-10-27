"""Ouvido: microfone → VAD (Silero) → Whisper (MLX).

Captura APENAS o dispositivo de entrada configurado (um microfone). Nunca o som do sistema
nem de outras aplicações: isso só entraria se saísse pelas colunas e chegasse ao microfone.

Corre numa thread própria e entrega eventos ao loop asyncio:
  ("speech_start", None)      começaste a falar (ou carregaste no push-to-talk)
  ("rec_end", segundos)       a gravação fechou; vai transcrever
  ("utterance", texto)        frase transcrita
  ("speech_discarded", str)   gravação descartada (curta, ruído, alucinação) e porquê
  ("level", float)            nível do microfone 0..1, ~10 vezes por segundo
"""

from __future__ import annotations

import asyncio
import collections
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np
import sounddevice as sd
from pysilero_vad import SileroVoiceActivityDetector

log = logging.getLogger("voz.ouvido")

# Frases que o Whisper inventa em silêncio ou ruído (treino em legendas de vídeos).
ALUCINACOES_RE = re.compile(
    r"(obrigad[oa] por (assistir|ver|ouvir)|legendas? (pela|por|da)|amara\.org|subscre|inscreva|"
    r"até (à|a) próxima|tchau|bye|thank you|thanks for watching|até logo|boa noite a todos|"
    r"^(obrigad[oa]|e|a|o|um|hum|\.{2,})$)",  # "sim", "não" e "ok" são respostas legítimas, nunca alucinações
    re.I,
)


class Ouvido:
    def __init__(self, cfg: dict[str, Any], loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        self.cfg = cfg
        self.loop = loop
        self.queue = queue
        self.sr = int(cfg["audio"]["sample_rate"])
        self.vad = SileroVoiceActivityDetector()
        self.chunk = SileroVoiceActivityDetector.chunk_samples()  # 512 amostras a 16 kHz (32 ms)
        self.ptt_mode = cfg["vad"]["mode"] == "ptt"
        self._ptt_down = False
        self._stream: sd.InputStream | None = None
        self._raw: collections.deque = collections.deque()
        self._raw_lock = threading.Lock()
        self._stop = threading.Event()
        self._muted = False
        self.speaking_out = False  # True enquanto a voz (TTS) está a falar; sobe o limiar (barge-in)
        self._stt = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")
        self._live = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live")
        self._parakeet = None          # modelo Parakeet (texto ao vivo), carregado no warmup se stt.live
        self._live_stream = None
        self._live_pending = 0
        self._thread: threading.Thread | None = None
        self.device_name = "?"

    # ---------- controlo ----------
    def start(self) -> None:
        import mlx_whisper  # import tardio: carrega Metal

        self._mlx = mlx_whisper
        device = self._resolve_device(self.cfg["audio"]["input_device"])
        self._stream = sd.InputStream(
            samplerate=self.sr, channels=1, dtype="int16", blocksize=self.chunk,
            device=device, callback=self._on_audio,
        )
        self._stream.start()
        info = sd.query_devices(self._stream.device)
        self.device_name = str(info["name"])
        if info["max_output_channels"] > 0 or any(k in self.device_name.lower() for k in ("blackhole", "loopback", "soundflower", "aggregate", "multi-output")):
            log.warning("ATENÇÃO: %r parece um dispositivo virtual/agregado; pode captar o áudio de outras apps.", self.device_name)
        log.info("microfone: %s (só entrada; o som de outras apps não é capturado)", self.device_name)
        self._thread = threading.Thread(target=self._run, name="ouvido", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._stream:
            self._stream.stop(); self._stream.close()
        self._stt.shutdown(wait=False)
        self._live.shutdown(wait=False)

    def set_device(self, spec: str | int | None) -> str:
        """Troca o microfone ao vivo (ex.: AirPods ↔ microfone do portátil). Devolve o nome em uso."""
        device = self._resolve_device(spec if spec is not None else "")
        novo = sd.InputStream(samplerate=self.sr, channels=1, dtype="int16", blocksize=self.chunk, device=device, callback=self._on_audio)
        antigo, self._stream = self._stream, novo
        novo.start()
        if antigo:
            try:
                antigo.stop(); antigo.close()
            except Exception:  # noqa: BLE001
                pass
        with self._raw_lock:
            self._raw.clear()
        self.device_name = str(sd.query_devices(novo.device)["name"])
        log.info("microfone: %s", self.device_name)
        return self.device_name

    def set_muted(self, muted: bool) -> None:
        self._muted = muted

    def set_ptt_mode(self, on: bool) -> None:
        self.ptt_mode = on
        self._ptt_down = False

    def ptt(self, down: bool) -> None:
        self._ptt_down = down

    def prompt_inicial(self) -> str | None:
        from .vozes import prompt_para

        return self.cfg["stt"].get("initial_prompt") or prompt_para(self.cfg["tts"].get("lang", "pt_PT")) or None

    def transcrever(self, a) -> dict:
        """Transcreve um array float32 16 kHz (síncrono; chamar na thread de STT).

        Clips curtos (< 1,5 s: um "sim", um "não") vão sem prompt inicial e com um segundo de silêncio
        à frente: com o prompt, o Whisper devolve pedaços dele ("está bem, combinado") ou completa a
        palavra ("simplesmente") em vez do que foi dito."""
        curto = len(a) < 1.5 * self.sr
        if curto:
            a = np.concatenate([np.zeros(self.sr, dtype=np.float32), a, np.zeros(self.sr // 2, dtype=np.float32)])
        r = self._mlx.transcribe(
            a, path_or_hf_repo=self.cfg["stt"]["model"], language=self.cfg["stt"]["language"],
            fp16=True, condition_on_previous_text=False, initial_prompt=None if curto else self.prompt_inicial(),
            temperature=0.0 if curto else (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
        )
        if not curto:
            # nunca aceitar um pedaço do prompt inicial como transcrição
            prompt = (self.prompt_inicial() or "").lower()
            texto = (r.get("text") or "").strip().lower().strip(" .!?,")
            if prompt and texto and len(texto) > 8 and texto in prompt:
                r["text"] = ""
        return r

    def transcrever_async(self, a):
        """Versão para o loop: corre na thread de STT e devolve um Future."""
        return self.loop.run_in_executor(self._stt, self.transcrever, a)

    def warmup(self) -> None:
        """Carrega o Whisper (e o Parakeet para texto ao vivo) antes da primeira frase."""
        silence = np.zeros(self.sr, dtype=np.float32)
        self._mlx.transcribe(silence, path_or_hf_repo=self.cfg["stt"]["model"], language=self.cfg["stt"]["language"], fp16=True)
        if self.cfg["stt"].get("live", True):
            # tudo o que toca no Parakeet corre na thread "live": os streams do MLX são por thread
            def carregar() -> None:
                try:
                    from parakeet_mlx import from_pretrained

                    self._parakeet = from_pretrained(self.cfg["stt"].get("live_model", "mlx-community/parakeet-tdt-0.6b-v3"))
                    log.info("texto ao vivo: Parakeet pronto")
                except Exception as e:  # noqa: BLE001
                    log.warning("texto ao vivo indisponível (%s)", e)
                    self._parakeet = None

            self._live.submit(carregar).result()

    # ---------- texto ao vivo (Parakeet em streaming) ----------
    def _live_start(self) -> None:
        if self._parakeet is None:
            return
        self._live.submit(self._live_open)

    def _live_open(self) -> None:
        try:
            self._live_stream = self._parakeet.transcribe_stream(context_size=(256, 256), depth=1).__enter__()
        except Exception as e:  # noqa: BLE001
            log.debug("live open: %s", e); self._live_stream = None

    def _live_feed(self, chunks: list[bytes]) -> None:
        """Entrega ~0,5 s de áudio ao Parakeet e emite o parcial (salta se ainda houver um pedido pendente)."""
        if self._parakeet is None or self._live_pending > 0:
            return
        self._live_pending += 1
        raw = b"".join(chunks)

        def run() -> None:
            try:
                st = self._live_stream
                if st is None:
                    return
                import mlx.core as mx

                a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                st.add_audio(mx.array(a))
                text = (st.result.text or "").strip()
                if text:
                    self._emit("live", text)
            except Exception as e:  # noqa: BLE001
                log.debug("live feed: %s", e)
            finally:
                self._live_pending -= 1

        self._live.submit(run)

    def _live_close(self) -> None:
        def run() -> None:
            st, self._live_stream = self._live_stream, None
            if st is not None:
                try:
                    st.__exit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass

        self._live.submit(run)

    # ---------- áudio ----------
    @staticmethod
    def _resolve_device(spec: str | int):
        if spec == "" or spec is None:
            return None
        if isinstance(spec, int) or str(spec).isdigit():
            return int(spec)
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and str(spec).lower() in d["name"].lower():
                return i
        log.warning("microfone %r não encontrado; a usar o dispositivo por omissão", spec)
        return None

    def _on_audio(self, indata, frames, time_info, status):
        if status:
            log.debug("audio status: %s", status)
        with self._raw_lock:
            self._raw.append(bytes(indata))

    def _next_chunk(self) -> bytes | None:
        with self._raw_lock:
            return self._raw.popleft() if self._raw else None

    # ---------- máquina de estados ----------
    def recalc(self) -> None:
        """Recalcula os parâmetros derivados do VAD; chamar depois de alterar cfg['vad']."""
        v = self.cfg["vad"]
        chunk_ms = 1000 * self.chunk / self.sr
        self._p = {
            "pre_roll_n": max(1, int(v["pre_roll_ms"] / chunk_ms)),
            "min_speech_n": max(1, int(v["min_speech_ms"] / chunk_ms)),
            "barge_min_n": max(1, int(v["barge_in_min_ms"] / chunk_ms)),
            "silence_n": max(1, int(v["silence_ms"] / chunk_ms)),
            "max_n": int(v["max_utterance_s"] * 1000 / chunk_ms),
            "start": float(v["start_threshold"]),
            "end": float(v["end_threshold"]),
            "barge_thr": float(v["barge_in_threshold"]),
            "barge_in": bool(v["barge_in"]),
        }

    def _run(self) -> None:
        self.recalc()
        chunk_ms = 1000 * self.chunk / self.sr
        pre_roll: collections.deque = collections.deque(maxlen=self._p["pre_roll_n"])
        recording = False
        buf: list[bytes] = []
        speech_run = 0
        silence_run = 0
        level_tick = 0
        live_mark = 0

        while not self._stop.is_set():
            chunk = self._next_chunk()
            if chunk is None:
                time.sleep(0.005)
                continue
            if len(chunk) != self.chunk * 2:  # o VAD exige chunks exactos de 512 amostras int16
                chunk = (chunk + b"\x00" * (self.chunk * 2))[: self.chunk * 2]

            level_tick += 1
            if level_tick % 3 == 0:  # ~10 Hz
                a = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
                self._emit("level", float(min(1.0, np.sqrt(np.mean(a * a)) / 6000.0)))

            if self._muted:
                if recording:
                    self._live_close()
                recording = False; buf = []; speech_run = 0
                continue

            # ----- push-to-talk: grava só enquanto está carregado -----
            if self.ptt_mode:
                if self._ptt_down and not recording:
                    recording = True; buf = list(pre_roll); live_mark = 0; self._emit("speech_start", None); self._live_start()
                elif not self._ptt_down and recording:
                    self._live_close(); self._finish(buf, chunk_ms); recording = False; buf = []
                if recording:
                    buf.append(chunk)
                    if len(buf) - live_mark >= 16:
                        self._live_feed(buf[live_mark:]); live_mark = len(buf)
                else:
                    pre_roll.append(chunk)
                continue

            # ----- automático: VAD -----
            p = self._p
            prob = self.vad(chunk)
            if not recording:
                pre_roll.append(chunk)
                if self.speaking_out and not p["barge_in"]:
                    speech_run = 0
                    continue
                thr = p["barge_thr"] if self.speaking_out else p["start"]
                need = p["barge_min_n"] if self.speaking_out else p["min_speech_n"]
                speech_run = speech_run + 1 if prob >= thr else 0
                if speech_run >= need:
                    recording = True; buf = list(pre_roll); silence_run = 0; live_mark = 0
                    self._emit("speech_start", None)
                    self._live_start(); self._live_feed(buf); live_mark = len(buf)
                continue

            buf.append(chunk)
            if len(buf) - live_mark >= 16:  # ~0,5 s de áudio novo
                self._live_feed(buf[live_mark:]); live_mark = len(buf)
            silence_run = silence_run + 1 if prob < p["end"] else 0
            if silence_run >= p["silence_n"] or len(buf) >= p["max_n"]:
                self._live_close()
                self._finish(buf, chunk_ms)
                recording = False; buf = []; speech_run = 0

    def _finish(self, buf: list[bytes], chunk_ms: float) -> None:
        self.vad.reset()
        audio = b"".join(buf)
        self._emit("rec_end", len(buf) * chunk_ms / 1000)
        self._stt.submit(self._transcribe, audio)

    def _transcribe(self, raw: bytes) -> None:
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        dur = len(a) / self.sr
        if dur < self.cfg["stt"]["min_duration_s"]:
            self._emit("speech_discarded", f"curta ({dur:.1f}s)")
            return
        t0 = time.time()
        try:
            r = self.transcrever(a)
        except Exception as e:  # noqa: BLE001
            log.error("whisper: %s", e)
            self._emit("speech_discarded", f"erro: {e}")
            return
        text = (r.get("text") or "").strip()
        segs = r.get("segments") or []
        motivo = self._suspeito(text, segs, dur)
        if motivo:
            log.info("descartado (%s): %r", motivo, text)
            self._emit("speech_discarded", f"{motivo}: {text}")
            return
        log.info("stt %.2fs (%.1fs áudio): %s", time.time() - t0, dur, text)
        lp = (sum(s.get("avg_logprob", 0) for s in segs) / len(segs)) if segs else 0.0
        self._emit("utterance", {"text": text, "audio": a, "dur": dur, "logprob": lp})

    @staticmethod
    def _suspeito(text: str, segs: list, dur: float) -> str | None:
        if not text:
            return "vazio"
        if segs and all(s.get("no_speech_prob", 0) > 0.6 for s in segs):
            return "sem voz"
        if segs:
            lp = sum(s.get("avg_logprob", 0) for s in segs) / len(segs)
            if lp < -1.0:
                return f"pouca confiança ({lp:.2f})"
            if any(s.get("compression_ratio", 0) > 2.4 for s in segs):
                return "repetitivo"
        limpo = text.strip(" .!?,").lower()
        if ALUCINACOES_RE.search(limpo):
            return "alucinação"
        if dur < 2.0 and len(limpo.split()) >= 6:
            return "demasiadas palavras para o tempo"
        return None

    def _emit(self, kind: str, payload: Any) -> None:
        self.loop.call_soon_threadsafe(self.queue.put_nowait, (kind, payload))
