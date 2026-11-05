"""Permissões por voz: na dúvida, nunca aprovar."""

import pytest

from voz.permissao import classificar_resposta


@pytest.mark.parametrize(
    "frase",
    [
        "sim", "Sim.", "sim, pode", "pode", "claro", "claro que sim", "ok", "okay", "avança", "força", "pode avançar",
        "pode ser", "está bem", "faz isso", "sim, por favor",
        "yes", "Yes.", "yes please", "yeah", "sure", "allow", "allow it", "approve", "go", "go ahead", "do it",
        "sí", "sí, por favor", "vale", "adelante",
        "oui", "d'accord", "oui s'il te plaît",
        "ja", "ja bitte", "klar",
        "sì", "va bene", "certo",
    ],
)
def test_aprova(frase):
    assert classificar_resposta(frase) == "allow"


@pytest.mark.parametrize(
    "frase",
    [
        "não", "Não.", "nao", "não permito", "agora não", "nunca", "pára",
        "no", "nope", "never", "deny",
        "don't allow", "Don't allow.", "don’t allow", "do not allow", "not allowed", "never allow", "don't go", "won't allow",
        "not ok", "don't permit",
        "claro que não", "pode ser que não", "ok, mas não", "acho que não", "sim, não", "yes, no",
        "non", "nein", "nicht erlaubt",
    ],
)
def test_recusa(frase):
    assert classificar_resposta(frase) == "deny"


@pytest.mark.parametrize(
    "frase",
    [
        # hesitações e frases que não são uma aprovação
        "deixa estar", "deixa lá", "deixa", "pode esperar", "vai com calma", "go away", "go back", "ok espera", "espera", "acho que sim",
        "assim", "look", "bem", "por favor", "que",
        # o eco da nossa própria pergunta
        "Permites?", "Permites", "¿Permites?", "Allow?", "Erlaubst du?", "Tu permets ?", "Permetti?", "sim?",
        # frases longas são mensagens, mesmo com um sim ou um não
        "não é interessante fazer isso desta maneira agora", "sim mas primeiro vê se os testes passam",
        # vazio
        "", "   ", "...",
    ],
)
def test_vai_para_o_dialogo(frase):
    assert classificar_resposta(frase) == "none"
