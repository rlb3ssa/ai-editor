"""Lixeira no padrão freedesktop (sem depender do `gio`).

Arquivos no mesmo disco da pasta pessoal vão para ~/.local/share/Trash. Em outro disco
(ex.: /mnt/armazenamento) vão para a lixeira daquele disco: <ponto de montagem>/.Trash/<uid>
(se existir e for segura) ou <ponto de montagem>/.Trash-<uid>. Assim nada é copiado entre discos
e o arquivo aparece normalmente na Lixeira do gerenciador de arquivos.
Especificação: https://specifications.freedesktop.org/trash-spec/latest/
"""
from __future__ import annotations

import datetime as _dt
import os
import stat
from pathlib import Path
from urllib.parse import quote


def _lixeira_pessoal() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "Trash"


def _ponto_de_montagem(p: Path) -> Path:
    p = p.resolve()
    dev = p.stat().st_dev
    while p.parent != p and p.parent.stat().st_dev == dev:
        p = p.parent
    return p


def _lixeira_do_disco(topo: Path) -> Path:
    uid = os.getuid()
    admin = topo / ".Trash"
    try:
        st = admin.lstat()
        if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode) and st.st_mode & stat.S_ISVTX:
            return admin / str(uid)
    except FileNotFoundError:
        pass
    return topo / f".Trash-{uid}"


def mover_para_lixeira(arquivo: Path | str) -> Path:
    """Move o arquivo para a Lixeira e devolve o novo caminho."""
    arq = Path(arquivo).absolute()
    if not arq.exists():
        raise FileNotFoundError(arq)
    pessoal = _lixeira_pessoal()
    pessoal.mkdir(parents=True, exist_ok=True)
    if arq.stat().st_dev == pessoal.stat().st_dev:
        lixeira, caminho_info = pessoal, str(arq)
    else:
        topo = _ponto_de_montagem(arq)
        lixeira = _lixeira_do_disco(topo)
        caminho_info = str(arq.relative_to(topo))  # caminho relativo ao disco, como manda a spec
    (lixeira / "files").mkdir(parents=True, exist_ok=True, mode=0o700)
    (lixeira / "info").mkdir(parents=True, exist_ok=True, mode=0o700)

    # nome único: reserva o .trashinfo de forma atômica (O_EXCL)
    nome, n = arq.name, 1
    while True:
        info = lixeira / "info" / f"{nome}.trashinfo"
        try:
            fd = os.open(info, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            n += 1
            nome = f"{arq.stem}.{n}{arq.suffix}"
    with os.fdopen(fd, "w") as f:
        f.write("[Trash Info]\n"
                f"Path={quote(caminho_info)}\n"
                f"DeletionDate={_dt.datetime.now():%Y-%m-%dT%H:%M:%S}\n")
    destino = lixeira / "files" / nome
    try:
        os.rename(arq, destino)
    except OSError:
        info.unlink(missing_ok=True)
        raise
    return destino
