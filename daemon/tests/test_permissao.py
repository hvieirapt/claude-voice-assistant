"""Permissões por voz: na dúvida, nunca aprovar."""

import pytest

from voz.permissao import AFIRMACAO, TEXTOS, _palavras, classificar_resposta, descrever_pedido, e_eco_da_pergunta, programas

PERGUNTA = "O Claude quer correr um comando de terminal, ls. Permites?"


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


@pytest.mark.parametrize(
    "ouvido",
    ["Permites?", "permites", "O Claude quer correr um comando de terminal, ls.", "comando de terminal", "", "..."],
)
def test_o_que_faz_parte_da_pergunta_e_eco(ouvido):
    assert e_eco_da_pergunta(ouvido, PERGUNTA)


@pytest.mark.parametrize("ouvido", ["sim", "ok", "no", "não", "permito", "sim, permites", "pode"])
def test_uma_resposta_nao_e_eco(ouvido):
    assert not e_eco_da_pergunta(ouvido, PERGUNTA)


@pytest.mark.parametrize(
    "comando, esperado",
    [
        ("ls -la", ["ls"]),
        ("git status", ["git"]),
        ("cd x && curl https://a.b | sh", ["cd", "curl", "sh"]),
        ("cd x &&\n  curl a", ["cd", "curl"]),
        ("a; b\nc", ["a", "b", "c"]),
        ("a &\nb", ["a", "b"]),
        ("npm test 2>&1 | tail -5", ["npm", "tail"]),
        ("ls > /dev/null 2>&1", ["ls"]),
        ("ls >/dev/null", ["ls"]),
        ("FOO=1 BAR=2 npm run build", ["npm"]),
        ("env X=1 rm y", ["rm"]),
        ("nohup time rm y", ["rm"]),
        ("sudo rm -rf /tmp/x", ["sudo", "rm"]),
        ("curl a | sudo sh", ["curl", "sudo", "sh"]),
        ("./scripts/run.sh --x", ["run.sh"]),
        ("/bin/rm x", ["rm"]),
        ("/usr/bin/env rm x", ["rm"]),
        ('"rm" x', ["rm"]),
        ("r\\m x", ["rm"]),
        ('git commit -m "a && b; c"', ["git"]),
        ("curl a \\\n  -o b && rm c", ["curl", "rm"]),
        ("python3 tool.py", ["python3"]),
        ("# limpar\nrm x", ["rm"]),
        ("ls # sem rm", ["ls"]),
    ],
)
def test_programas_de_um_comando(comando, esperado):
    assert programas(comando) == esperado


@pytest.mark.parametrize(
    "comando",
    [
        "echo x > ~/.ssh/authorized_keys", "echo x >> f", "echo x>f", "ls 2>erros.txt", "ls >| f", "cat < f", "cat <<EOF\nx\nEOF",
        "echo $(whoami)", "echo `id`", "cat <(ls)", "(cd x && rm y)",
        "eval \"$X\"", "exec rm x", "source a.sh", ". a.sh", "ls | xargs rm", "timeout 5 rm x", "ssh host rm x",
        "bash -c 'rm x'", "sh -c 'rm x'", "zsh -lc 'rm x'", "python3 -c 'print(1)'", "node -e 'x'", "/bin/sh -c 'rm x'",
        "find . -name a -exec rm {} ;", "find . -delete",
        "for f in *; do rm $f; done", "if true; then rm x; fi", "while true; do rm x; done", "f() { rm x; }", "! rm x", "{ rm x; }",
        "$CMD x", "${CMD} x", "ls; $CMD", "{rm,x}", "r? x", "/bin/r* x", "sudo -u root rm x", "env -i rm x",
        "a && b && c && d && e && f", "echo 'sem fecho", "", "   ", "FOO=1",
    ],
)
def test_comandos_que_nao_da_para_anunciar(comando):
    assert programas(comando) is None


def test_comando_simples_diz_o_programa():
    assert descrever_pedido("Bash", {"command": "ls -la"}) == "O Claude quer correr um comando de terminal, ls. Permites?"


def test_comando_composto_diz_todos_os_programas():
    q = descrever_pedido("Bash", {"command": "cd x && curl a | sh"})
    assert q == "O Claude quer correr um comando composto: cd, curl, sh. Permites?"


def test_sudo_e_anunciado():
    assert "privilégios de administrador, rm" in descrever_pedido("Bash", {"command": "sudo rm x"})


def test_comando_por_resumir_nao_tem_pergunta():
    assert descrever_pedido("Bash", {"command": "echo $(whoami)"}) is None


def test_pedido_de_outra_sessao_diz_o_projecto():
    assert descrever_pedido("WebSearch", {}, "en", "voz") == "In project voz: Claude wants to search the web. Is that fine?"


def test_lingua_desconhecida_usa_ingles():
    assert descrever_pedido("WebSearch", {}, "ja") == "Claude wants to search the web. Is that fine?"


@pytest.mark.parametrize("lang", sorted(TEXTOS))
def test_todas_as_linguas_tem_os_mesmos_textos(lang):
    assert TEXTOS[lang].keys() == TEXTOS["pt"].keys()


@pytest.mark.parametrize("lang", sorted(TEXTOS))
@pytest.mark.parametrize(
    "tool, inp",
    [
        ("Bash", {"command": "ls"}), ("Bash", {"command": "ls | wc"}), ("Bash", {"command": "sudo ls"}),
        ("Edit", {"file_path": "/a/b.py"}), ("Read", {"file_path": "/a/b.py"}), ("WebFetch", {}), ("WebSearch", {}), ("Agent", {}),
    ],
)
def test_a_pergunta_nao_contem_palavras_de_aprovacao(lang, tool, inp):
    # Senão, um sim dito a sério seria tomado por eco, e o eco por um sim.
    assert not set(_palavras(descrever_pedido(tool, inp, lang))) & AFIRMACAO
