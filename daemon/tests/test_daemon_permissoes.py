"""O ciclo real do daemon a responder a pedidos de permissão, com falsos no lugar do microfone e da voz."""

import asyncio

import pytest

from voz.daemon import Daemon


class FalaFalsa:
    def __init__(self):
        self.queue = asyncio.Queue()
        self._play_q = asyncio.Queue()
        self.paused = False
        self.retomou = 0

    async def stop(self):
        pass

    async def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False
        self.retomou += 1

    def dizer(self, frase):
        pass


def criar_daemon() -> tuple[Daemon, list]:
    d = Daemon.__new__(Daemon)
    d.cfg = {"tts": {"lang": "pt_PT"}, "ui": {"ack_sound": False}, "vad": {}}
    d._hold = False
    d._pending = []
    d._state = "listening"
    d._acumulado = ""
    d._segmentos = []
    d._gap_task = None
    d.reuniao = None
    d._ditas = []
    d._muted = False
    d._expect_answer = False
    d._pergunta_dita = ""
    d._resposta_fut = None
    d._pedido_lock = asyncio.Lock()
    d._pedido_em = 0.0
    d._inicio_gravacao = None
    d._interrompeu_voz = False
    d._state_antes_de_gravar = "listening"
    d.loop = asyncio.get_running_loop()
    d.events = asyncio.Queue()
    d.fala = FalaFalsa()
    eventos: list = []
    d.out = lambda tipo, **k: eventos.append((tipo, k))
    d._set_state = lambda estado: None
    return d, eventos


async def passo() -> None:
    await asyncio.sleep(0.01)


async def fala(d: Daemon, frase: str) -> None:
    """Alguém começa a falar agora e a frase é transcrita."""
    d.events.put_nowait(("speech_start", None))
    d.events.put_nowait(("utterance", {"text": frase}))
    await passo()


def cenario(corpo):
    async def main():
        d, eventos = criar_daemon()
        ciclo = asyncio.create_task(d._main_loop())
        try:
            return await corpo(d, eventos)
        finally:
            d.events.put_nowait(("quit", None))
            await ciclo

    return asyncio.run(main())


def pedir(d: Daemon, comando: str = "ls", timeout: float = 0.3, project: str = "") -> asyncio.Task:
    return asyncio.create_task(d._perguntar_permissao("Bash", {"command": comando}, timeout, project))


def descartados(eventos: list) -> list[str]:
    return [k["reason"] for tipo, k in eventos if tipo == "discarded"]


@pytest.fixture(autouse=True)
def menos_tempo_minimo(monkeypatch):
    monkeypatch.setattr("voz.daemon.MIN_TEMPO_RESPOSTA_S", 0.05)


@pytest.mark.parametrize(
    "frases, esperado",
    [
        (["sim"], "allow"),
        (["ok"], "allow"),
        (["não"], "deny"),
        (["deixa estar"], "none"),
        (["Permites?", "sim"], "allow"),
        (["O Claude quer correr um comando de terminal, ls.", "não"], "deny"),
        (["Permites?"], "none"),
    ],
)
def test_resposta_ao_pedido(frases, esperado):
    async def corpo(d, _):
        p = pedir(d)
        await passo()
        for f in frases:
            await fala(d, f)
        return (await p)["decision"]

    assert cenario(corpo) == esperado


def test_o_eco_nao_gasta_a_resposta():
    async def corpo(d, eventos):
        p = pedir(d)
        await passo()
        await fala(d, "Permites?")
        assert not p.done()
        assert d._expect_answer
        assert descartados(eventos)
        await fala(d, "sim")
        return (await p)["decision"]

    assert cenario(corpo) == "allow"


def test_frase_comecada_antes_da_pergunta_nao_e_resposta():
    async def corpo(d, eventos):
        d.events.put_nowait(("speech_start", None))
        await passo()
        p = pedir(d)
        await passo()
        d.events.put_nowait(("utterance", {"text": "vai lá"}))
        await passo()
        assert not p.done()
        assert "começou antes da pergunta" in descartados(eventos)[0]
        await fala(d, "não")
        return (await p)["decision"]

    assert cenario(corpo) == "deny"


def test_o_que_a_voz_diz_durante_o_pedido_nao_e_resposta():
    async def corpo(d, _):
        p = pedir(d)
        await passo()
        await d._on_cmd({"type": "say", "text": "Okay."})
        await fala(d, "Okay.")
        assert not p.done()
        await fala(d, "não")
        return (await p)["decision"]

    assert cenario(corpo) == "deny"


def test_frases_em_espera_repostas_durante_o_pedido_nao_sao_resposta():
    async def corpo(d, _):
        p = pedir(d)
        await passo()
        d._pending.append("Okay.")
        await fala(d, "Permites?")  # o descarte repõe o que estava em espera
        assert d._pending == []
        await fala(d, "Okay.")
        assert not p.done()
        await fala(d, "não")
        return (await p)["decision"]

    assert cenario(corpo) == "deny"


def test_o_que_a_voz_disse_noutro_pedido_nao_fica_para_o_seguinte():
    async def corpo(d, _):
        p = pedir(d)
        await passo()
        await d._on_cmd({"type": "say", "text": "Okay."})
        await fala(d, "sim")
        await p
        p = pedir(d)
        await passo()
        await fala(d, "Okay.")
        return (await p)["decision"]

    assert cenario(corpo) == "allow"


def test_voz_em_pausa_retoma_depois_do_eco():
    async def corpo(d, _):
        p = pedir(d)
        await passo()
        d._state_antes_de_gravar = "speaking"  # a voz estava a falar quando o microfone a apanhou
        await fala(d, "Permites?")
        retomou, em_pausa = d.fala.retomou, d.fala.paused
        d._state_antes_de_gravar = "listening"
        await fala(d, "sim")
        await p
        return retomou, em_pausa

    assert cenario(corpo) == (1, False)


def test_pedidos_simultaneos_um_de_cada_vez():
    async def corpo(d, _):
        a = pedir(d, "ls", timeout=0.5, project="a")
        await passo()
        b = pedir(d, "rm x", timeout=0.5, project="b")
        await passo()
        assert "No projecto a" in d._pergunta_dita
        await fala(d, "sim")  # só pode ser para o pedido que está a ser ouvido
        assert (await a)["decision"] == "allow"
        await passo()
        assert not b.done()
        assert "No projecto b" in d._pergunta_dita
        await fala(d, "não")
        return (await b)["decision"]

    assert cenario(corpo) == "deny"


def test_o_fim_de_um_pedido_nao_estraga_o_seguinte():
    async def corpo(d, _):
        a = pedir(d, "ls", timeout=0.1, project="a")
        await passo()
        b = pedir(d, "rm x", timeout=0.5, project="b")
        assert (await a)["decision"] == "none"
        await passo()
        assert d._expect_answer
        await fala(d, "não")
        return (await b)["decision"]

    assert cenario(corpo) == "deny"


def test_pedido_sem_tempo_para_a_vez_segue_para_o_dialogo():
    async def corpo(d, _):
        a = pedir(d, "ls", timeout=0.2)
        await passo()
        b = pedir(d, "rm x", timeout=0.05)
        assert (await b)["decision"] == "none"
        return (await a)["decision"]

    assert cenario(corpo) == "none"


def test_comando_por_resumir_nao_e_perguntado():
    async def corpo(d, _):
        r = await pedir(d, "echo $(id)")
        return r["decision"], d._expect_answer

    assert cenario(corpo) == ("none", False)
