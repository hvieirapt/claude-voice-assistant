"""Servidor local: só o próprio painel e os clientes locais sem browser. Testa o servidor real, numa porta livre."""

import asyncio
import json
import logging
import socket

import pytest

from voz.ui import UI

CMD = json.dumps({"type": "text", "text": "ola"}).encode()


def porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _pedir(porta: int, metodo: str, caminho: str, cabecalhos: dict[str, str], corpo: bytes = b"", ate_ao_fim: bool = True) -> tuple[int, str]:
    r, w = await asyncio.open_connection("127.0.0.1", porta)
    linhas = [f"{metodo} {caminho} HTTP/1.1"] + [f"{k}: {v}" for k, v in cabecalhos.items()]
    if corpo:
        linhas.append(f"Content-Length: {len(corpo)}")
    w.write(("\r\n".join(linhas) + "\r\n\r\n").encode() + corpo)
    await w.drain()
    # /events nunca fecha: lê só até ao fim do primeiro evento, que pode chegar depois dos cabeçalhos
    dados = await asyncio.wait_for(r.read(-1) if ate_ao_fim else r.readuntil(b"}\n\n"), timeout=3)
    w.close()
    texto = dados.decode(errors="replace")
    return int(texto.split(" ", 2)[1]), texto


def correr(metodo: str, caminho: str, cabecalhos=None, corpo: bytes = b"", ate_ao_fim: bool = True):
    """Liga o servidor, faz um pedido e devolve (estado, resposta, comandos recebidos)."""
    porta = porta_livre()
    recebidos: list[dict] = []

    async def on_cmd(cmd: dict) -> None:
        recebidos.append(cmd)

    async def main():
        ui = UI(porta, on_cmd)
        await ui.start()
        ui.emit("spoken", text="evento de teste")
        try:
            cab = {"Host": f"127.0.0.1:{porta}"}
            cab.update({k: v.format(porta=porta, outra=porta + 1) for k, v in (cabecalhos or {}).items()})
            return await _pedir(porta, metodo, caminho, {k: v for k, v in cab.items() if v != "-"}, corpo, ate_ao_fim)
        finally:
            await ui.close()

    estado, resposta = asyncio.run(main())
    return estado, resposta, recebidos


JSON = {"Content-Type": "application/json"}


@pytest.mark.parametrize(
    "cabecalhos",
    [
        pytest.param(JSON, id="hook e mod, sem Origin"),
        pytest.param({**JSON, "Host": "localhost:{porta}"}, id="por localhost"),
        pytest.param({**JSON, "Host": "LOCALHOST:{porta}"}, id="host em maiusculas"),
        pytest.param({**JSON, "Origin": "http://127.0.0.1:{porta}"}, id="painel em 127.0.0.1"),
        pytest.param({**JSON, "Host": "localhost:{porta}", "Origin": "http://localhost:{porta}"}, id="painel em localhost"),
        pytest.param({"Content-Type": "application/json; charset=utf-8"}, id="json com charset"),
    ],
)
def test_clientes_legitimos_chegam_ao_comando(cabecalhos):
    estado, _, recebidos = correr("POST", "/cmd", cabecalhos, CMD)
    assert estado == 200
    assert recebidos == [{"type": "text", "text": "ola"}]


@pytest.mark.parametrize(
    "cabecalhos, esperado",
    [
        pytest.param({**JSON, "Host": "evil.example:{porta}"}, 403, id="host de outro nome"),
        pytest.param({**JSON, "Host": "127.0.0.1:{outra}"}, 403, id="host com outra porta"),
        pytest.param({**JSON, "Host": "-"}, 403, id="sem host"),
        pytest.param({**JSON, "Origin": "https://evil.example"}, 403, id="origin externa"),
        pytest.param({**JSON, "Origin": "null"}, 403, id="origin null, de iframe isolado"),
        pytest.param({**JSON, "Origin": "http://127.0.0.1:{outra}"}, 403, id="origin com outra porta"),
        pytest.param({**JSON, "Origin": "https://localhost:{porta}"}, 403, id="origin com outro esquema"),
        pytest.param({"Content-Type": "text/plain"}, 415, id="text/plain, sem preflight"),
        pytest.param({"Content-Type": "application/x-www-form-urlencoded"}, 415, id="formulario"),
        pytest.param({}, 415, id="sem content-type"),
    ],
)
def test_pedidos_recusados_nao_chegam_ao_comando(cabecalhos, esperado):
    estado, _, recebidos = correr("POST", "/cmd", cabecalhos, CMD)
    assert estado == esperado
    assert recebidos == []


def test_recusa_fica_registada(caplog):
    with caplog.at_level(logging.WARNING, logger="voz.ui"):
        correr("POST", "/cmd", {**JSON, "Origin": "https://evil.example"}, CMD)
    assert "https://evil.example" in caplog.text


def test_painel_e_servido_e_nao_pode_ser_embebido():
    estado, resposta, _ = correr("GET", "/")
    assert estado == 200
    assert "X-Frame-Options: DENY" in resposta


def test_painel_com_host_de_outro_nome_e_recusado():
    estado, resposta, _ = correr("GET", "/", {"Host": "evil.example:{porta}"})
    assert estado == 403
    assert "<html" not in resposta.lower()


def test_eventos_para_o_painel_sem_cors_aberto():
    estado, resposta, _ = correr("GET", "/events", ate_ao_fim=False)
    assert estado == 200
    assert "Access-Control-Allow-Origin" not in resposta
    assert "evento de teste" in resposta


@pytest.mark.parametrize(
    "cabecalhos",
    [{"Origin": "https://evil.example"}, {"Host": "evil.example:{porta}"}],
    ids=["origin externa", "host de outro nome"],
)
def test_eventos_recusados_nao_revelam_nada(cabecalhos):
    estado, resposta, _ = correr("GET", "/events", cabecalhos)
    assert estado == 403
    assert "evento de teste" not in resposta
