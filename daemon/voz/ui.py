"""Painel web local: estado em tempo real por SSE e comandos por POST. Sem dependências.

Só aceita o próprio painel e clientes locais sem browser (hook, mod). O bind a 127.0.0.1 não chega:
quem faz o pedido pode ser o browser do utilizador, a mando de uma página qualquer.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger("voz.ui")
INDEX = Path(__file__).with_name("static") / "index.html"
ESTADOS = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 415: "Unsupported Media Type"}


class UI:
    def __init__(self, port: int, on_cmd: Callable[[dict], Awaitable[None]]):
        self.port = port
        self.on_cmd = on_cmd
        self._clients: set[asyncio.StreamWriter] = set()
        self._server: asyncio.AbstractServer | None = None
        self._history: list[dict] = []  # últimos eventos, para quem liga depois

    async def start(self) -> None:
        # Num reload do mod, o daemon anterior ainda pode estar a largar a porta: espera até 15 s.
        for tentativa in range(30):
            try:
                self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
                break
            except OSError as e:
                if tentativa == 29:
                    raise RuntimeError(f"a porta {self.port} está ocupada ({e.strerror}); fecha o outro voz-daemon") from e
                if tentativa == 0:
                    log.info("porta %d ocupada; a aguardar que o daemon anterior termine…", self.port)
                await asyncio.sleep(0.5)
        log.info("painel em http://localhost:%d", self.port)

    async def close(self) -> None:
        for w in list(self._clients):
            w.close()
        if self._server:
            self._server.close()

    def emit(self, type_: str, **data: Any) -> None:
        ev = {"type": type_, **data}
        if type_ != "level":
            self._history.append(ev)
            self._history = self._history[-300:]
        line = f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode()
        for w in list(self._clients):
            try:
                w.write(line)
            except Exception:  # noqa: BLE001
                self._clients.discard(w)

    # ---------- HTTP mínimo ----------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        except Exception:  # noqa: BLE001
            writer.close(); return
        lines = head.decode(errors="replace").split("\r\n")
        try:
            method, path, _ = lines[0].split(" ", 2)
        except ValueError:
            writer.close(); return
        headers = {k.lower(): v for k, _, v in (l.partition(":") for l in lines[1:] if ":" in l)}
        headers = {k: v.strip() for k, v in headers.items()}
        recusa = self._recusa(method, path, headers)
        if recusa:
            log.warning("pedido recusado (%d): %s %s host=%r origin=%r content-type=%r", recusa, method, path, headers.get("host"), headers.get("origin"), headers.get("content-type"))
            self._respond(writer, recusa, "text/plain", b"recusado"); await writer.drain(); writer.close()
            return
        body = b""
        n = int(headers.get("content-length", "0") or 0)
        if n:
            body = await reader.readexactly(n)

        if method == "GET" and path == "/":
            self._respond(writer, 200, "text/html; charset=utf-8", INDEX.read_bytes()); await writer.drain(); writer.close()
        elif method == "GET" and path == "/events":
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: keep-alive\r\n\r\n")
            for ev in self._history[-80:]:
                writer.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
            await writer.drain()
            self._clients.add(writer)
            try:
                while True:
                    await asyncio.sleep(15)
                    writer.write(b": ping\n\n"); await writer.drain()
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._clients.discard(writer); writer.close()
        elif method == "POST" and path == "/cmd":
            try:
                cmd = json.loads(body or b"{}")
                result = await self.on_cmd(cmd)
                payload = result if isinstance(result, dict) else {"ok": True}
                self._respond(writer, 200, "application/json", json.dumps(payload, ensure_ascii=False).encode())
            except Exception as e:  # noqa: BLE001
                self._respond(writer, 400, "application/json", json.dumps({"erro": str(e)}).encode())
            await writer.drain(); writer.close()
        else:
            self._respond(writer, 404, "text/plain", b"nada aqui"); await writer.drain(); writer.close()

    def _recusa(self, method: str, path: str, headers: dict[str, str]) -> int | None:
        """Devolve o código de erro se o pedido não puder ser servido, ou None se puder."""
        hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        if headers.get("host", "").lower() not in hosts:
            return 403  # DNS rebinding: a página fala com o servidor sob outro nome
        origin = headers.get("origin")
        if origin is not None and origin.lower() not in {f"http://{h}" for h in hosts}:
            return 403  # outra página; sem Origin são clientes sem browser, como o hook
        if method == "POST" and path == "/cmd":
            tipo = headers.get("content-type", "").split(";")[0].strip().lower()
            if tipo != "application/json":
                return 415  # outro tipo não dispara o preflight do browser
        return None

    @staticmethod
    def _respond(writer: asyncio.StreamWriter, status: int, ctype: str, body: bytes) -> None:
        writer.write(
            f"HTTP/1.1 {status} {ESTADOS.get(status, 'OK')}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
            f"X-Frame-Options: DENY\r\nConnection: close\r\n\r\n".encode() + body
        )
