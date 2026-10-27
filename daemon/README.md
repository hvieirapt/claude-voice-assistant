# voz

Conversa por voz com o Claude Code. Tudo corre localmente excepto o Claude.

```
microfone (só esse dispositivo) → Silero VAD → Whisper large-v3-turbo (MLX)
   → Claude Agent SDK (a tua sessão Claude Code, com ferramentas, em streaming)
   → TTS neural local: Qwen3-TTS (mlx-audio) a clonar uma voz pt-PT · ou `say` · ou Piper
   → painel web local (http://localhost:8765) com o estado em tempo real
```

Cada frase do Claude é dita assim que fecha, ainda com a resposta a ser gerada. Falar por cima
interrompe a voz (barge-in). Confirmações curtas ("ok", "sim", "continua") não interrompem.

## Requisitos

- macOS em Apple Silicon (MLX), Python 3.12 via `uv`.
- Claude Code com login feito (`claude` → `/login`). O SDK traz o CLI embutido e usa esse login.
- Microfone: na primeira execução o macOS pede permissão à app onde corre o terminal.
- Auscultadores recomendados para o barge-in. Com colunas, usa `--ptt` ou `--no-barge-in`.

## Usar

```bash
uv sync
.venv/bin/voz --cwd /caminho/do/projecto
```

Abre `http://localhost:8765` para ver o painel: estado (a ouvir / a gravar / a transcrever / Claude a pensar / a falar),
nível do microfone, transcrição, resposta do Claude em streaming, frases ditas, ferramentas usadas, tempo e custo por turno.
No painel há botão de carregar-para-falar (ou tecla Espaço), silenciar, parar, e uma caixa para escrever.

| Opção | Efeito |
|---|---|
| `--prompt-file f.md` / `--prompt "..."` | primeira mensagem (ex.: um guião de conversa) |
| `--resume last` / `--resume <id>` | retoma a última sessão desta pasta, ou uma sessão específica |
| `--model sonnet\|opus\|haiku` | modelo do Claude (default `sonnet`, o mais rápido para conversa) |
| `--tts mlx\|say\|piper` | backend de voz |
| `--ptt` | push-to-talk: só grava enquanto carregas (botão no painel ou Espaço) |
| `--permission-mode acceptEdits` | `default`, `acceptEdits`, `bypassPermissions`, `plan` |
| `--falante` | reescrever respostas longas com o LM Studio (ver abaixo) |
| `--no-barge-in` | não interromper a voz quando falas |
| `--gravar-ref` | grava 15 s da tua voz como referência pt-PT para o TTS e sai |
| `-v` | logs de VAD/STT |

No terminal: `/mudo`, `/ouvir`, `/parar` (cala a voz e interrompe o Claude), `/ptt`, `/auto`, `/sair`.

Quando o Claude precisa de permissão para uma ferramenta, a pergunta é dita em voz alta e a tua
próxima frase é a resposta ("sim" / "não").

## O que o microfone ouve

O app captura apenas o dispositivo de entrada em `audio.input_device` (por defeito "AirPods"). Nunca captura o
som do sistema nem de outras apps; isso só entraria se saísse pelas colunas e chegasse ao microfone. Se o
dispositivo configurado parecer virtual ou agregado (BlackHole, Loopback, "Aggregate"), o arranque avisa.
Para garantia total usa `--ptt`: só grava enquanto carregas.

## Voz em português de Portugal

Os modelos de TTS abertos foram treinados sobretudo com português do Brasil. Para pt-PT há dois caminhos:

1. **Clonagem (backend `mlx`, por defeito).** O Qwen3-TTS Base copia timbre, prosódia e sotaque de uma gravação
   de 10 a 20 s. Vem incluída uma referência sintética (`ref/joana-pt.wav`). Para muito melhor resultado grava a
   tua própria voz, ou a de alguém com a voz que queres, com:

   ```bash
   .venv/bin/voz --gravar-ref
   ```

   Fica em `~/.voz/ref.wav` + `ref.txt` e passa a ser usada automaticamente.

2. **Joana (Premium) da Apple (backend `say`).** Voz neural pt-PT, offline, instantânea. Descarrega-se em
   Definições do Sistema → Acessibilidade → Conteúdo falado → Voz do sistema → ⓘ → Português (Portugal) →
   Joana (Premium). Depois `--tts say` apanha-a sozinho.

## Falante (opcional)

Com `--falante`, as respostas longas ou com markdown são reescritas em prosa falada por um modelo local no
LM Studio (`lms server start && lms load openai/gpt-oss-20b --identifier falante`). Está desligado por defeito:
o Claude já responde em estilo falado, o streaming directo é mais rápido, e a reescrita por vezes alterava o sentido.

## Configuração

`config.toml` nesta pasta (ou `~/.voz/config.toml`). Os valores que mais interessam:

- `audio.input_device` — microfone fixo (substring do nome).
- `vad.mode` — `auto` ou `ptt`. `vad.silence_ms` (600) fecha a frase; sobe se te cortar a meio.
- `vad.barge_in_threshold` / `barge_in_min_ms` — quão certo tem de estar de que és tu enquanto a voz fala.
- `stt.model` — qualquer Whisper em `mlx-community/`.
- `tts.backend`, `tts.mlx_ref_audio`, `tts.mlx_model` — voz.
- `claude.model` — `sonnet` por defeito.

## Latência (M5 Pro)

| Passo | Tempo |
|---|---|
| fim da frase → texto | ~0,8 s (0,6 s de silêncio + 0,2 s Whisper) |
| Claude (sonnet), primeira frase | 2 a 4 s |
| Qwen3-TTS, por frase | ~1 s por cada 3 s de áudio (gera enquanto diz a anterior) |

## Estrutura

```
voz/
  app.py      ciclo principal, CLI, barge-in, permissões por voz, gravação de referência
  ouvido.py   microfone, VAD, push-to-talk, Whisper, filtro de alucinações (thread própria)
  cerebro.py  sessão Claude Agent SDK em streaming
  fala.py     fila de frases → mlx/say/piper, com paragem imediata
  falante.py  reescrita falada via LM Studio (opcional)
  ui.py       painel web local (HTTP + SSE), sem dependências
  static/     index.html do painel
  config.py   config.toml + defaults
ref/          referência de voz pt-PT incluída
```
