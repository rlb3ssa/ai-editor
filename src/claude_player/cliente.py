"""Cliente do socket de controle da janela (usado pelo MCP e pela linha de comando)."""
from __future__ import annotations

import json
import socket

from .core import SOCKET_PATH


class JanelaFechada(ConnectionError):
    pass


def enviar(cmd: str, timeout: float = 10.0, **args) -> dict:
    if not SOCKET_PATH.exists():
        raise JanelaFechada("o player não está aberto")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(SOCKET_PATH))
            s.sendall((json.dumps({"cmd": cmd, **args}) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                parte = s.recv(65536)
                if not parte:
                    break
                buf += parte
    except (ConnectionRefusedError, FileNotFoundError) as e:
        raise JanelaFechada("o player não está aberto") from e
    resp = json.loads(buf.decode() or "{}")
    if not resp.get("ok", False):
        raise RuntimeError(resp.get("erro", "erro desconhecido no player"))
    return resp.get("dados", {})


def vivo() -> bool:
    try:
        enviar("ping", timeout=2)
        return True
    except Exception:  # noqa: BLE001
        return False
