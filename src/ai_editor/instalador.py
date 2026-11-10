"""`ai-editor --instalar` / `--desinstalar`: integra o AI Editor ao sistema e ao Claude.

O que o --instalar faz (tudo no diretório do usuário, sem sudo):
  - atalho `~/.local/bin/ai-editor` → AppImage (só quando roda de um AppImage);
  - `~/.local/share/applications/<nome>.desktop` + ícone SVG (menu de aplicativos, "Abrir com");
  - tipos MIME de vídeo/áudio em `~/.config/mimeapps.list` ([Default Applications]) — opcional (--sem-mime);
  - registra o servidor MCP no Claude Code (`claude mcp add --scope user`, ou `~/.claude.json` se o comando
    `claude` não existir) e no Claude Desktop (`~/.config/Claude/claude_desktop_config.json`, se a pasta existir)
    — opcional (--sem-claude).

Tudo o que foi alterado (e o valor anterior de cada item) fica num manifesto em
`~/.local/share/ai-editor/instalacao.json`; o --desinstalar usa o manifesto para restaurar.
Instalar de novo é seguro: os valores anteriores registrados na primeira instalação são preservados.

Só biblioteca padrão (não importa Qt). Respeita o modo dev (`core.NOME`): rodando do código-fonte, o nome é
`ai-editor-dev` e o manifesto é `instalacao-dev.json`.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .core import DEV, NOME, TITULO

# Tipos que o AI Editor abre bem (lista explícita; nada de curingas no mimeapps.list).
TIPOS_MIME = [
    "video/mp4", "video/x-matroska", "video/webm", "video/quicktime", "video/x-msvideo", "video/mpeg",
    "video/ogg", "video/3gpp", "video/3gpp2", "video/x-flv", "video/mp2t", "video/x-ms-wmv",
    "video/x-m4v", "video/vnd.avi",
    "audio/mpeg", "audio/mp4", "audio/x-m4a", "audio/aac", "audio/ogg", "audio/opus", "audio/flac",
    "audio/x-flac", "audio/x-wav", "audio/wav", "audio/webm", "audio/x-matroska", "audio/x-vorbis+ogg",
    "audio/x-opus+ogg", "audio/x-ms-wma", "audio/x-aiff",
]


# ---------------------------------------------------------------- caminhos
def _home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def _config() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or _home() / ".config")


def _dados() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or _home() / ".local" / "share")


def _manifesto() -> Path:
    return _dados() / "ai-editor" / ("instalacao-dev.json" if DEV else "instalacao.json")


def _desktop() -> Path:
    return _dados() / "applications" / f"{NOME}.desktop"


def _icone() -> Path:
    return _dados() / "icons" / "hicolor" / "scalable" / "apps" / f"{NOME}.svg"


def _atalho() -> Path:
    return _home() / ".local" / "bin" / NOME


def _claude_json() -> Path:
    d = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(d) / ".claude.json" if d else _home() / ".claude.json"


def _desktop_config() -> Path:
    return _config() / "Claude" / "claude_desktop_config.json"


def _mimeapps() -> Path:
    return _config() / "mimeapps.list"


def _icone_fonte() -> Path:
    return Path(__file__).resolve().parent / "recursos" / "ai-editor.svg"


def executavel() -> Path:
    """Executável a registrar: o AppImage, o binário empacotado, ou o script `ai-editor` do ambiente."""
    if os.environ.get("APPIMAGE"):
        return Path(os.environ["APPIMAGE"]).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    arg0 = Path(sys.argv[0])
    if arg0.name in ("ai-editor", "ai-editor-dev") and arg0.exists():
        return arg0.absolute()
    # Rodando com `python -m ai_editor.cli`: o script fica ao lado do python do .venv (sem resolver o link).
    candidato = Path(sys.executable).parent / "ai-editor"
    if candidato.exists():
        return candidato.absolute()
    achado = shutil.which("ai-editor")
    if achado:
        return Path(achado).absolute()
    raise RuntimeError("não achei o executável do AI Editor para registrar")


# ---------------------------------------------------------------- utilitários de arquivo
def _gravar(caminho: Path, texto: str, backup: bool = True) -> None:
    """Grava de forma atômica, preservando as permissões; backup `<arquivo>.ai-editor.bak` do conteúdo anterior."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    modo = None
    if caminho.exists():
        modo = caminho.stat().st_mode & 0o777
        if backup:
            shutil.copy2(caminho, caminho.with_name(caminho.name + ".ai-editor.bak"))
    tmp = caminho.with_name(f".{caminho.name}.ai-editor.tmp")
    tmp.write_text(texto, encoding="utf-8")
    if modo is not None:
        os.chmod(tmp, modo)
    os.replace(tmp, caminho)


def _ler_json(caminho: Path) -> dict:
    if not caminho.exists() or not caminho.read_text(encoding="utf-8").strip():
        return {}
    dados = json.loads(caminho.read_text(encoding="utf-8"))  # JSON inválido → erro: não arriscamos sobrescrever
    if not isinstance(dados, dict):
        raise ValueError(f"{caminho} não é um objeto JSON")
    return dados


def _gravar_json(caminho: Path, dados: dict, backup: bool = True) -> None:
    _gravar(caminho, json.dumps(dados, indent=2, ensure_ascii=False) + "\n", backup)


def _rodar(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


# ---------------------------------------------------------------- mimeapps.list (INI simples, preservando o resto)
def _ini_ler(linhas: list[str], secao: str, chave: str) -> str | None:
    atual = None
    for l in linhas:
        s = l.strip()
        if s.startswith("[") and s.endswith("]"):
            atual = s[1:-1]
        elif atual == secao and "=" in s and not s.startswith("#"):
            k, v = s.split("=", 1)
            if k.strip() == chave:
                return v.strip()
    return None


def _ini_definir(linhas: list[str], secao: str, chave: str, valor: str | None) -> list[str]:
    """Define (ou remove, com valor None) `chave` em `[secao]`, mexendo só nessa linha."""
    saida: list[str] = []
    atual = None
    feito = False
    fim_secao = None  # índice (em saida) logo após a última linha não vazia da seção
    for l in linhas:
        s = l.strip()
        if s.startswith("[") and s.endswith("]"):
            atual = s[1:-1]
            saida.append(l)
            if atual == secao:
                fim_secao = len(saida)
            continue
        if atual == secao and "=" in s and not s.startswith("#") and s.split("=", 1)[0].strip() == chave:
            if not feito and valor is not None:
                saida.append(f"{chave}={valor}")
            feito = True
            if atual == secao:
                fim_secao = len(saida)
            continue
        saida.append(l)
        if atual == secao and s:
            fim_secao = len(saida)
    if not feito and valor is not None:
        if fim_secao is None:
            if saida and saida[-1].strip():
                saida.append("")
            saida += [f"[{secao}]", f"{chave}={valor}"]
        else:
            saida.insert(fim_secao, f"{chave}={valor}")
    return saida


# ---------------------------------------------------------------- partes da instalação
def _texto_desktop(exe: Path) -> str:
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        f"Name={TITULO}",
        "GenericName=Player e editor de vídeo",
        "Comment=Player e editor de vídeo e áudio controlado por IA (MCP)",
        f'Exec="{exe}" %f',
        f'TryExec={exe}',
        f"Icon={NOME}",
        "Terminal=false",
        "Categories=AudioVideo;Video;Player;AudioVideoEditing;",
        "Keywords=video;audio;player;editor;ffmpeg;mcp;claude;",
        f"StartupWMClass={NOME}",
        "MimeType=" + ";".join(TIPOS_MIME) + ";",
        "X-AI-Editor-Instalador=1",
        "",
    ])


def _entrada_mcp(exe: Path, claude_code: bool) -> dict:
    e = {"command": str(exe), "args": ["--mcp"]}
    if claude_code:
        e = {"type": "stdio", **e, "env": {}}
    return e


def _registrar_claude_code(exe: Path, man: dict, log: list[str]) -> None:
    arq = _claude_json()
    if not shutil.which("claude") and not arq.exists():
        log.append(f"  · Claude Code não encontrado (nem o comando `claude` nem {arq}); pulei")
        return
    try:
        anterior = (_ler_json(arq).get("mcpServers") or {}).get(NOME)
    except Exception as e:  # noqa: BLE001
        log.append(f"  ! não consegui ler {arq}: {e}; Claude Code não registrado")
        return
    reg = man.get("claude_code")
    if not reg:
        reg = man["claude_code"] = {"arquivo": str(arq), "anterior": anterior}
    claude = shutil.which("claude")
    if claude:
        _rodar([claude, "mcp", "remove", "--scope", "user", NOME])
        r = _rodar([claude, "mcp", "add", "--scope", "user", NOME, "--", str(exe), "--mcp"])
        if r.returncode != 0:
            log.append(f"  ! `claude mcp add` falhou: {(r.stderr or r.stdout).strip()}")
            return
        reg["metodo"] = "claude"
        log.append(f"  ✔ Claude Code: servidor MCP '{NOME}' registrado (claude mcp add --scope user)")
        return
    dados = _ler_json(arq)
    dados.setdefault("mcpServers", {})[NOME] = _entrada_mcp(exe, claude_code=True)
    _gravar_json(arq, dados)
    reg["metodo"] = "json"
    log.append(f"  ✔ Claude Code: servidor MCP '{NOME}' gravado em {arq}")


def _registrar_claude_desktop(exe: Path, man: dict, log: list[str]) -> None:
    arq = _desktop_config()
    if not arq.parent.is_dir():
        log.append(f"  · Claude Desktop não encontrado ({arq.parent} não existe); pulei")
        return
    try:
        dados = _ler_json(arq)
    except Exception as e:  # noqa: BLE001
        log.append(f"  ! não consegui ler {arq}: {e}; Claude Desktop não registrado")
        return
    servidores = dados.setdefault("mcpServers", {})
    if "claude_desktop" not in man:
        man["claude_desktop"] = {"arquivo": str(arq), "anterior": servidores.get(NOME), "criado": not arq.exists()}
    servidores[NOME] = _entrada_mcp(exe, claude_code=False)
    _gravar_json(arq, dados)
    log.append(f"  ✔ Claude Desktop: servidor MCP '{NOME}' gravado em {arq} (reinicie o Claude Desktop)")


def _associar_mime(man: dict, log: list[str]) -> None:
    arq = _mimeapps()
    linhas = arq.read_text(encoding="utf-8").splitlines() if arq.exists() else []
    anteriores = man.setdefault("mime", {"arquivo": str(arq), "anteriores": {}, "criado": not arq.exists()})["anteriores"]
    valor = f"{NOME}.desktop"
    for tipo in TIPOS_MIME:
        if tipo not in anteriores:
            ant = _ini_ler(linhas, "Default Applications", tipo)
            anteriores[tipo] = None if ant == valor else ant
        linhas = _ini_definir(linhas, "Default Applications", tipo, valor)
    _gravar(arq, "\n".join(linhas) + "\n")
    log.append(f"  ✔ {len(TIPOS_MIME)} tipos de vídeo/áudio abrem com o {TITULO} ({arq})")


def _atualizar_bancos(log: list[str]) -> None:
    apps = _desktop().parent
    if shutil.which("update-desktop-database"):
        _rodar(["update-desktop-database", str(apps)])
    temas = _dados() / "icons" / "hicolor"
    if shutil.which("gtk-update-icon-cache") and (temas / "index.theme").exists():
        _rodar(["gtk-update-icon-cache", "-q", "-t", str(temas)])


# ---------------------------------------------------------------- instalar / desinstalar
def instalar(mime: bool = True, claude: bool = True) -> int:
    exe = executavel()
    man_arq = _manifesto()
    man = _ler_json(man_arq) if man_arq.exists() else {}
    log = [f"Instalando {TITULO} → {exe}"]

    # Atalho em ~/.local/bin (só para AppImage; no código-fonte o uv já cuida do comando).
    if os.environ.get("APPIMAGE"):
        atalho = _atalho()
        if atalho.is_symlink() or not atalho.exists():
            if "atalho" not in man:
                man["atalho"] = {"caminho": str(atalho),
                                 "anterior": os.readlink(atalho) if atalho.is_symlink() else None}
            atalho.parent.mkdir(parents=True, exist_ok=True)
            if atalho.is_symlink():
                atalho.unlink()
            atalho.symlink_to(exe)
            log.append(f"  ✔ atalho {atalho} → {exe}")
            if str(atalho.parent) not in os.environ.get("PATH", "").split(":"):
                log.append(f"    (adicione {atalho.parent} ao PATH para usar o comando `{NOME}`)")
        else:
            log.append(f"  ! {atalho} já existe e não é um atalho; não mexi")

    # Menu de aplicativos + ícone.
    _gravar(_desktop(), _texto_desktop(exe), backup=False)
    _icone().parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_icone_fonte(), _icone())
    man["arquivos"] = sorted(set(man.get("arquivos", [])) | {str(_desktop()), str(_icone())})
    log.append(f"  ✔ menu de aplicativos: {_desktop()}")

    if mime:
        _associar_mime(man, log)
    if claude:
        _registrar_claude_code(exe, man, log)
        _registrar_claude_desktop(exe, man, log)

    _atualizar_bancos(log)
    man.update({"nome": NOME, "dev": DEV, "executavel": str(exe),
                "data": _dt.datetime.now().isoformat(timespec="seconds")})
    _gravar_json(man_arq, man, backup=False)
    log.append(f"  ✔ manifesto: {man_arq}  (para desfazer: ai-editor --desinstalar)")
    if claude:
        log.append("Pronto. No Claude Code, rode /mcp (ou abra uma sessão nova) para conectar.")
    print("\n".join(log))
    return 0


def desinstalar() -> int:
    man_arq = _manifesto()
    if not man_arq.exists():
        print(f"Nada a desfazer: {man_arq} não existe.")
        return 0
    man = _ler_json(man_arq)
    nome = man.get("nome", NOME)
    log = [f"Desinstalando {nome}"]

    if "mime" in man:
        arq = Path(man["mime"]["arquivo"])
        if arq.exists():
            linhas = arq.read_text(encoding="utf-8").splitlines()
            nosso = f"{nome}.desktop"
            for tipo, anterior in man["mime"]["anteriores"].items():
                if _ini_ler(linhas, "Default Applications", tipo) == nosso:  # só se ninguém trocou depois
                    linhas = _ini_definir(linhas, "Default Applications", tipo, anterior)
            if man["mime"].get("criado") and [l.strip() for l in linhas if l.strip()] == ["[Default Applications]"]:
                arq.unlink()  # fomos nós que criamos e ficou vazio
            else:
                _gravar(arq, "\n".join(linhas) + "\n")
            log.append(f"  ✔ tipos MIME restaurados em {arq}")

    if "claude_code" in man:
        reg = man["claude_code"]
        arq = Path(reg["arquivo"])
        claude = shutil.which("claude")
        if reg.get("metodo") == "claude" and claude:
            _rodar([claude, "mcp", "remove", "--scope", "user", nome])
            if reg.get("anterior"):
                _rodar([claude, "mcp", "add-json", "--scope", "user", nome, json.dumps(reg["anterior"])])
        elif arq.exists():
            dados = _ler_json(arq)
            servidores = dados.setdefault("mcpServers", {})
            if reg.get("anterior"):
                servidores[nome] = reg["anterior"]
            else:
                servidores.pop(nome, None)
            _gravar_json(arq, dados)
        log.append(f"  ✔ Claude Code: registro '{nome}' {'restaurado' if reg.get('anterior') else 'removido'}")

    if "claude_desktop" in man:
        reg = man["claude_desktop"]
        arq = Path(reg["arquivo"])
        if arq.exists():
            dados = _ler_json(arq)
            servidores = dados.setdefault("mcpServers", {})
            if reg.get("anterior"):
                servidores[nome] = reg["anterior"]
            else:
                servidores.pop(nome, None)
            if reg.get("criado") and dados == {"mcpServers": {}}:
                arq.unlink()
            else:
                _gravar_json(arq, dados)
            log.append(f"  ✔ Claude Desktop: registro '{nome}' {'restaurado' if reg.get('anterior') else 'removido'}")

    for a in man.get("arquivos", []):
        p = Path(a)
        if p.exists():
            p.unlink()
            log.append(f"  ✔ removido {p}")

    if "atalho" in man:
        atalho = Path(man["atalho"]["caminho"])
        if atalho.is_symlink():
            atalho.unlink()
            if man["atalho"].get("anterior"):
                atalho.symlink_to(man["atalho"]["anterior"])
            log.append(f"  ✔ atalho {atalho} {'restaurado' if man['atalho'].get('anterior') else 'removido'}")

    _atualizar_bancos(log)
    man_arq.unlink()
    log.append("Pronto.")
    print("\n".join(log))
    return 0
