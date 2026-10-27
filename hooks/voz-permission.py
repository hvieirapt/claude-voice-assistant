#!/usr/bin/env python3
"""Hook PreToolUse do plugin voz: pergunta por voz ao daemon e devolve a decisão ao Claude Code.

Sem decisão (daemon parado, modo automático, sem resposta) não escreve nada: o diálogo normal aparece.
Só usa a biblioteca padrão, para correr em qualquer Mac sem instalar nada.
"""
import json
import os
import sys
import urllib.request

PERGUNTAVEIS = {"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch"}


def main() -> None:
    try:
        e = json.load(sys.stdin)
    except Exception:
        return
    modo = e.get("permission_mode") or "default"
    tool = e.get("tool_name", "")
    if modo not in ("default", "acceptEdits") or tool not in PERGUNTAVEIS:
        return
    if modo == "acceptEdits" and tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return
    try:
        info = json.load(open(os.path.expanduser("~/.voz/daemon.json")))
        port = int(info["port"])
    except Exception:
        return  # daemon não anunciou porta: a voz não está ligada
    projecto = os.path.basename((e.get("cwd") or "").rstrip("/"))
    body = json.dumps({"type": "ask", "tool": tool, "input": e.get("tool_input") or {}, "timeout": 20,
                       "project": projecto, "session": e.get("session_id", "")}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/cmd", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=28) as r:
            res = json.load(r)
    except Exception:
        return  # daemon em baixo ou sem resposta: diálogo normal
    d = res.get("decision")
    if d not in ("allow", "deny"):
        return
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": d,
            "permissionDecisionReason": "permitido por voz" if d == "allow" else f"recusado por voz: {res.get('text', '')}",
        }
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
