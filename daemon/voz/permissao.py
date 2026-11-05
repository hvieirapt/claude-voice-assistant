"""Permissões por voz: o que conta como resposta a um pedido. Só usa a biblioteca padrão.

Na dúvida, nunca aprova: o que não for claramente um sim fica "none" e segue para o diálogo.
"""

from __future__ import annotations

import re

MAX_PALAVRAS = 6  # frases mais longas são mensagens, não respostas

NEGACAO = frozenset({
    # pt
    "não", "nao", "nunca", "nem", "jamais", "nega", "negar", "negado", "negativo", "recuso", "recusa", "recusar",
    "cancela", "cancelar", "pára", "para", "parar", "bloqueia", "bloquear", "proíbo", "proibido",
    # en
    "no", "not", "nope", "never", "deny", "denied", "dont", "wont", "cant", "cannot", "stop", "cancel", "block", "reject",
    # es
    "niego", "deniego", "alto",
    # fr
    "non", "pas", "refuse", "annule", "arrête", "arrete",
    # de
    "nein", "nee", "nicht", "nie", "niemals", "kein", "keine", "abbrechen", "stopp", "ablehnen",
    # it
    "mai", "annulla", "ferma", "rifiuto",
})

AFIRMACAO = frozenset({
    # pt
    "sim", "pode", "podes", "permito", "autorizo", "aprovo", "confirmo", "força", "forca", "avança", "avanca", "avançar", "avancar",
    "claro", "certo", "ok", "okay", "vai",
    # en
    "yes", "yeah", "yep", "sure", "allow", "approve", "approved", "go",
    # es
    "sí", "si", "vale", "adelante",
    # fr
    "oui", "d'accord", "vas-y", "autorise",
    # de
    "ja", "klar", "genau", "erlaube",
    # it
    "sì", "permetto", "autorizzo", "va",
})

# Só acompanham uma afirmação: "sim, por favor". Sozinhas não aprovam nada.
COMPLEMENTO = frozenset({
    "por", "favor", "ser", "que", "lá", "está", "bem", "bom", "fazer", "isso",
    "please", "ahead", "it", "do", "thing",
    "s'il", "te", "plaît", "vous",
    "bitte", "gerne",
    "bene",
})

# Frases inteiras que aprovam sem ter uma afirmação sozinha.
FRASES_AFIRMATIVAS = frozenset({"está bem", "tá bom", "tudo bem", "faz isso", "do it"})


def _palavras(texto: str) -> list[str]:
    limpo = texto.lower().replace("’", "'")
    return [p for p in (w.strip("'-") for w in re.findall(r"[\w'-]+", limpo)) if p]


def classificar_resposta(texto: str) -> str:
    """Devolve "allow", "deny" ou "none". Uma negação em qualquer parte da frase recusa."""
    palavras = _palavras(texto)
    if not palavras or len(palavras) > MAX_PALAVRAS:
        return "none"
    if any(p in NEGACAO or p.endswith("n't") for p in palavras):
        return "deny"
    if texto.rstrip(" \"'»”").endswith(("?", "？")):
        return "none"  # uma pergunta não é um sim, e pode ser o eco da nossa própria pergunta
    if " ".join(palavras) in FRASES_AFIRMATIVAS:
        return "allow"
    tem_afirmacao = any(p in AFIRMACAO for p in palavras)
    so_conhecidas = all(p in AFIRMACAO or p in COMPLEMENTO for p in palavras)
    return "allow" if tem_afirmacao and so_conhecidas else "none"
