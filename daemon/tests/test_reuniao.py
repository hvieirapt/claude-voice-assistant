"""RE_NOME: quem se apresenta ganha um nome; uma frase normal de reunião não muda o orador."""

import pytest

from voz.reuniao import RE_NOME


def nome(frase: str) -> str | None:
    m = RE_NOME.search(frase)
    return m.group(1) if m else None


@pytest.mark.parametrize(
    "frase, esperado",
    [
        ("Eu sou o João.", "João"),
        ("eu sou o João Silva", "João Silva"),
        ("Eu sou a Inês Costa.", "Inês Costa"),
        ("Sou a Maria.", "Maria"),
        ("Sou Maria.", "Maria"),
        ("Chamo-me Rui.", "Rui"),
        ("O meu nome é Olga.", "Olga"),
        ("Este é o Pedro.", "Pedro"),
        ("Esta é a Marta.", "Marta"),
        ("Aqui fala o Tiago.", "Tiago"),
        ("Aqui é a Ana.", "Ana"),
        ("Fala o Rui.", "Rui"),
        # a meio da frase: é assim que as pessoas se apresentam
        ("Bom dia, eu sou o João Silva.", "João Silva"),
        ("Olá, sou a Marta.", "Marta"),
        # a frase de apresentação não depende de maiúsculas
        ("EU SOU o João", "João"),
        ("Sou O Pedro", "Pedro"),
    ],
)
def test_apresentacoes_dao_nome(frase, esperado):
    assert nome(frase) == esperado


@pytest.mark.parametrize(
    "frase, esperado",
    [
        ("Chamo-me Ana", "Ana"),
        ("Sou Olga", "Olga"),
        ("Eu sou Ana Silva", "Ana Silva"),
        ("Sou Óscar", "Óscar"),
    ],
)
def test_o_artigo_nao_come_a_inicial_do_nome(frase, esperado):
    assert nome(frase) == esperado


@pytest.mark.parametrize(
    "frase",
    [
        "eu sou contra isso",
        "Eu sou contra isso.",
        "quem fala primeiro",
        "Quem fala primeiro?",
        "Este é o problema.",
        "Esta é a questão.",
        "aqui é a minha opinião",
        "Sou de opinião que não.",
        "Este é eu, Pedro.",
        "assou o frango",
    ],
)
def test_frases_normais_nao_dao_nome(frase):
    assert nome(frase) is None


def test_nome_em_minuscula_nao_conta():
    # Custo aceite: o Whisper escreve os nomes com maiúscula; o painel continua a permitir renomear.
    assert nome("eu sou o joão") is None
