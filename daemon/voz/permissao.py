"""Permissões por voz: o que conta como resposta a um pedido. Só usa a biblioteca padrão.

Na dúvida, nunca aprova: o que não for claramente um sim fica "none" e segue para o diálogo.
"""

from __future__ import annotations

import re
import shlex

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
    "sim", "pode", "podes", "autorizo", "aprovo", "confirmo", "força", "forca", "avança", "avanca", "avançar", "avancar",
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
    "sì", "autorizzo", "va",
})

# Fora de propósito: "permito" e "permetto", quase iguais às perguntas "Permites?" e "Permetti?": mal transcritas,
# um eco aprovaria. "deixa", porque "deixa estar" quer dizer o contrário.

# Só acompanham uma afirmação: "sim, por favor". Sozinhas não aprovam nada.
COMPLEMENTO = frozenset({
    "por", "favor", "ser", "lá", "está", "bem", "bom", "fazer", "isso",
    "please", "ahead", "it", "do", "thing",
    "bitte", "gerne",
    "bene",
})
COMPLEMENTOS_EM_FRASE = ("s'il te plaît", "s'il vous plaît")

# Frases inteiras que aprovam sem ter uma afirmação sozinha.
FRASES_AFIRMATIVAS = frozenset({"está bem", "tá bom", "tudo bem", "faz isso", "claro que sim", "do it"})

# Têm uma palavra de negação, mas não são uma recusa. Uma recusa é final; estas ficam para o diálogo.
NAO_E_RECUSA = frozenset({
    "não sei", "nao sei", "não tenho a certeza", "porque não", "por que não", "para a frente", "no problem", "no worries", "why not",
    "not sure", "i don't know", "don't know", "dont know", "no sé", "pas de problème", "je ne sais pas", "kein problem", "warum nicht",
    "ich weiß nicht", "weiß nicht", "perché no", "non lo so",
})


def _palavras(texto: str) -> list[str]:
    limpo = texto.lower().replace("’", "'")
    return [p for p in (w.strip("'-") for w in re.findall(r"[\w'-]+", limpo)) if p]


def classificar_resposta(texto: str) -> str:
    """Devolve "allow", "deny" ou "none". Uma negação em qualquer parte da frase recusa."""
    palavras = _palavras(texto)
    if not palavras or len(palavras) > MAX_PALAVRAS:
        return "none"
    frase = " ".join(palavras)
    if frase in NAO_E_RECUSA:
        return "none"
    if any(p in NEGACAO or p.endswith("n't") for p in palavras):
        return "deny"
    if re.search(r"[?？¿…]|\.\.", texto):
        return "none"  # uma pergunta ou uma hesitação não são um sim, e a pergunta pode ser o nosso próprio eco
    if frase in FRASES_AFIRMATIVAS:
        return "allow"
    for complemento in COMPLEMENTOS_EM_FRASE:
        frase = frase.replace(complemento, "")
    palavras = frase.split()
    tem_afirmacao = any(p in AFIRMACAO for p in palavras)
    so_conhecidas = all(p in AFIRMACAO or p in COMPLEMENTO for p in palavras)
    return "allow" if tem_afirmacao and so_conhecidas else "none"


def e_eco_da_pergunta(texto: str, pergunta: str) -> bool:
    """Verdadeiro se tudo o que foi ouvido faz parte da pergunta dita: é a voz a ouvir-se a si própria."""
    return set(_palavras(texto)) <= set(_palavras(pergunta))


# ---------- o que dizer em voz alta ----------
OPERADORES = frozenset(";&|<>()\n")
SEPARADORES = frozenset(";&|\n")
REDIRECCOES = frozenset({">", ">>", "&>", "&>>", ">&"})
DESTINOS_INOFENSIVOS = frozenset({"/dev/null", "1", "2"})
MAX_PROGRAMAS = 5  # mais do que isto já não se percebe ao ouvir

# Prefixos que só mudam a forma de correr o programa seguinte.
PREFIXOS = frozenset({"command", "nohup", "time", "env"})
# Palavras da shell, e programas que correm outro que assim não se ouviria.
ESCONDEM = frozenset({
    "if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done", "case", "esac", "select", "function", "coproc",
    "{", "}", "!", "[[", "alias", "builtin", "trap",
    "eval", "exec", "source", ".", "xargs", "parallel", "timeout", "gtimeout", "nice", "ionice", "watch", "ssh", "su", "doas",
    "caffeinate", "stdbuf", "unbuffer", "noglob", "npx", "bunx", "busybox", "flock", "chroot",
})
# Com uma opção antes do script, um interpretador pode estar a correr código escrito na própria linha.
INTERPRETE = re.compile(r"^(?:sh|bash|zsh|dash|ksh|fish|python|pypy|node|deno|bun|perl|ruby|php|lua|osascript)[\d.]*$")
PASTAS_DO_SISTEMA = frozenset({"/bin", "/sbin", "/usr/bin", "/usr/sbin", "/usr/local/bin", "/opt/homebrew/bin"})
ARGS_EXECUTAM = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete"})


def _sem_comentarios(comando: str) -> str:
    """Retira os comentários, respeitando as aspas: um apóstrofo num comentário não abre nada."""
    fora: list[str] = []
    aspas = ""
    i = 0
    while i < len(comando):
        c = comando[i]
        if c == "\\" and aspas != "'" and i + 1 < len(comando):
            fora.append(comando[i:i + 2])
            i += 2
            continue
        if aspas:
            aspas = "" if c == aspas else aspas
        elif c in "'\"":
            aspas = c
        elif c == "#" and (not fora or fora[-1][-1] in " \t\r\n;&|()<>"):
            while i < len(comando) and comando[i] != "\n":
                i += 1
            continue
        fora.append(c)
        i += 1
    return "".join(fora)


def programas(comando: str) -> list[str] | None:
    """Os programas de um comando de shell, por ordem, ou None se não der para os anunciar com confiança.

    Não é um interpretador de shell: reconhece as formas conhecidas de esconder o que corre.
    """
    comando = _sem_comentarios(comando).replace("\\\n", "")  # e a continuação de linha, como a shell a trata
    if re.search(r"\$\(|`|<\(|>\(", comando):
        return None
    lex = shlex.shlex(comando, posix=True, punctuation_chars="".join(OPERADORES))
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        toks = list(lex)
    except ValueError:
        return None  # aspas por fechar
    res: list[str] = []
    inicio = True  # o próximo token é o nome de um programa
    i = 0
    while i < len(toks):
        t = toks[i]
        if set(t) <= OPERADORES:
            if set(t) <= SEPARADORES:
                inicio = True
                i += 1
            elif t in REDIRECCOES and i + 1 < len(toks) and toks[i + 1] in DESTINOS_INOFENSIVOS:
                i += 2  # só 2>&1 e > /dev/null: escrever para um ficheiro ficaria por anunciar
            else:
                return None
            continue
        if inicio:
            pasta, _, nome = t.rpartition("/")
            if re.match(r"^\w+\+?=", t) or nome in PREFIXOS:
                i += 1
                continue
            if t.startswith(("/", "~")) and pasta not in PASTAS_DO_SISTEMA:
                return None  # /tmp/git diria "git", sem ser o git
            if not nome or nome.startswith("-") or re.search(r"[$*?\[\]{}]", nome) or nome in ESCONDEM:
                return None  # uma opção, ou um nome que só a shell sabe a que dá origem
            if INTERPRETE.match(nome) and i + 1 < len(toks) and toks[i + 1].startswith("-"):
                return None
            res.append(nome)
            inicio = nome == "sudo"
        elif t in ARGS_EXECUTAM:
            return None
        i += 1
    res = list(dict.fromkeys(res))  # "git, git, git" não diz mais do que "git"
    return res if 0 < len(res) <= MAX_PROGRAMAS else None


TEXTOS = {
    "pt": {"bash": "correr um comando de terminal", "composto": "correr um comando composto: {p}", "sudo": "correr um comando com privilégios de administrador", "edit": "alterar o ficheiro", "read": "ler o ficheiro", "web": "consultar uma página web", "search": "pesquisar na web", "tool": "usar a ferramenta", "q": "O Claude quer {a}. Permites?", "projecto": "No projecto {p}: "},
    "en": {"bash": "run a terminal command", "composto": "run a compound command: {p}", "sudo": "run a command with administrator privileges", "edit": "change the file", "read": "read the file", "web": "fetch a web page", "search": "search the web", "tool": "use the tool", "q": "Claude wants to {a}. Is that fine?", "projecto": "In project {p}: "},
    "es": {"bash": "ejecutar un comando de terminal", "composto": "ejecutar un comando compuesto: {p}", "sudo": "ejecutar un comando con privilegios de administrador", "edit": "modificar el archivo", "read": "leer el archivo", "web": "consultar una página web", "search": "buscar en la web", "tool": "usar la herramienta", "q": "Claude quiere {a}. ¿Permites?", "projecto": "En el proyecto {p}: "},
    "fr": {"bash": "exécuter une commande du terminal", "composto": "exécuter une commande composée : {p}", "sudo": "exécuter une commande avec les droits administrateur", "edit": "modifier le fichier", "read": "lire le fichier", "web": "consulter une page web", "search": "chercher sur le web", "tool": "utiliser l'outil", "q": "Claude veut {a}. Tu permets ?", "projecto": "Dans le projet {p} : "},
    "de": {"bash": "einen Terminal-Befehl ausführen", "composto": "einen zusammengesetzten Befehl ausführen: {p}", "sudo": "einen Befehl mit Administratorrechten ausführen", "edit": "die Datei ändern", "read": "die Datei lesen", "web": "eine Webseite abrufen", "search": "im Web suchen", "tool": "das Werkzeug benutzen", "q": "Claude möchte {a}. Erlaubst du?", "projecto": "Im Projekt {p}: "},
    "it": {"bash": "eseguire un comando del terminale", "composto": "eseguire un comando composto: {p}", "sudo": "eseguire un comando con privilegi di amministratore", "edit": "modificare il file", "read": "leggere il file", "web": "consultare una pagina web", "search": "cercare sul web", "tool": "usare lo strumento", "q": "Claude vuole {a}. Permetti?", "projecto": "Nel progetto {p}: "},
}


def descrever_pedido(tool: str, inp: dict, lang: str = "pt_PT", project: str = "") -> str | None:
    """A pergunta a dizer em voz alta, ou None se o comando não der para resumir: nesse caso decide o diálogo."""
    t = TEXTOS.get(lang.split("_")[0], TEXTOS["en"])
    nome = lambda v: str(v or "").split("/")[-1]
    if tool == "Bash":
        ps = programas(str(inp.get("command", "")))
        if ps is None:
            return None
        if len(ps) == 2 and ps[0] == "sudo":
            a = f"{t['sudo']}, {ps[1]}"
        elif len(ps) == 1:
            a = f"{t['bash']}, {ps[0]}"
        else:
            a = t["composto"].format(p=", ".join(ps))
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
    # pedido vindo de outra sessão: diz de que projecto é
    return t["projecto"].format(p=project) + q if project else q
