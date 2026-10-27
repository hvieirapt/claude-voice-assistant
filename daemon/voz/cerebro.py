"""Cérebro: sessão Claude Code via Agent SDK (corre o CLI embutido, reutiliza o teu login).

Faz streaming do texto do Claude por deltas, para a voz começar a falar na primeira frase.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
)

log = logging.getLogger("voz.cerebro")

ESTILO_FALADO = """
Estás numa conversa por voz: o utilizador dita as mensagens e cada frase tua é dita em voz alta assim que termina.
Responde em prosa falada, português de Portugal, frases curtas e completas, no máximo quatro por resposta.
Nunca uses listas, tabelas, cabeçalhos, negrito ou blocos de código na resposta; se tiveres de mostrar código ou
um comando, escreve-o num ficheiro e diz onde está. Não leias caminhos nem URLs; descreve-os.
Quando precisas de uma decisão, termina com uma única pergunta clara. Não afirmes decisões que o utilizador ainda não tomou.
Antes de uma acção demorada diz numa frase curta o que vais fazer.
""".strip()

Pergunta = Callable[[str], Awaitable[str]]


class Cerebro:
    def __init__(self, cfg: dict[str, Any], cwd: Path, resume: str | None, perguntar: Pergunta):
        c = cfg["claude"]
        self.perguntar = perguntar
        self.session_id: str | None = resume
        self.model = c["model"] or None
        opts = ClaudeAgentOptions(
            cwd=str(cwd),
            resume=resume,
            permission_mode=c["permission_mode"],
            model=self.model,
            max_turns=c["max_turns"] or None,
            setting_sources=["user", "project", "local"],
            system_prompt={"type": "preset", "preset": "claude_code", "append": ESTILO_FALADO},
            can_use_tool=self._can_use_tool,
            include_partial_messages=True,
        )
        self.client = ClaudeSDKClient(options=opts)
        self.busy = False

    async def connect(self) -> None:
        await self.client.connect()

    async def close(self) -> None:
        await self.client.disconnect()

    async def interrupt(self) -> None:
        if self.busy:
            await self.client.interrupt()

    async def turno(self, texto: str) -> AsyncIterator[tuple[str, Any]]:
        """Eventos: ("delta", str) texto em streaming · ("block_end", None) fim de um bloco de texto ·
        ("text", str) bloco completo · ("tool", str) · ("done", ResultMessage)."""
        self.busy = True
        try:
            await self.client.query(texto)
            async for msg in self.client.receive_response():
                if isinstance(msg, StreamEvent):
                    if msg.parent_tool_use_id:
                        continue
                    ev = msg.event
                    t = ev.get("type")
                    if t == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                        yield ("delta", ev["delta"]["text"])
                    elif t == "content_block_stop":
                        yield ("block_end", None)
                elif isinstance(msg, AssistantMessage):
                    if msg.parent_tool_use_id:
                        continue
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            yield ("text", block.text)
                        elif isinstance(block, ToolUseBlock):
                            yield ("tool", descrever_tool(block.name, block.input or {}))
                elif isinstance(msg, ResultMessage):
                    self.session_id = msg.session_id
                    yield ("done", msg)
        finally:
            self.busy = False

    async def _can_use_tool(self, tool: str, inp: dict, ctx):
        """Pedido de permissão: pergunta por voz e aceita sim/não."""
        resposta = await self.perguntar(f"O Claude quer {descrever_tool(tool, inp)}. Permito?")
        r = resposta.lower()
        if any(w in r for w in ("sim", "pode", "permit", "autoriz", "força", "avança", "ok", "claro")) and "não" not in r:
            return PermissionResultAllow(behavior="allow")
        return PermissionResultDeny(behavior="deny", message=f"O utilizador recusou por voz: {resposta}")


def descrever_tool(n: str, i: dict) -> str:
    if n == "Bash":
        cmd = str(i.get("command", ""))
        return f"correr o comando {cmd[:80]}" + ("…" if len(cmd) > 80 else "")
    if n in ("Read", "Glob", "Grep"):
        alvo = i.get("file_path") or i.get("pattern") or i.get("path") or ""
        return f"ler {Path(str(alvo)).name or alvo}".strip()
    if n in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return f"alterar o ficheiro {Path(str(i.get('file_path', ''))).name}"
    if n == "WebFetch":
        return f"consultar {i.get('url', '')}"
    if n == "WebSearch":
        return f"pesquisar na web por {i.get('query', '')}"
    if n in ("Agent", "Task"):
        return f"lançar um subagente: {i.get('description', '')}"
    return f"usar a ferramenta {n}"
