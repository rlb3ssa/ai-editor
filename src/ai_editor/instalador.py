"""`ai-editor --instalar` / `--desinstalar`: integra o AI Editor ao sistema e ao Claude.

O que o --instalar faz (tudo no diretório do usuário, sem sudo):
  - atalho `~/.local/bin/ai-editor` → AppImage (só quando roda de um AppImage); quando existe, é ele que vai
    para o menu e para o registro do MCP (trocar/atualizar o AppImage não quebra nada);
  - `~/.local/share/applications/<nome>.desktop` + ícone SVG (menu de aplicativos, "Abrir com");
  - tipos MIME de vídeo/áudio em `~/.config/mimeapps.list` ([Default Applications]) — opcional (--sem-mime);
  - registra o servidor MCP no Claude Code (`claude mcp add --scope user`, ou `~/.claude.json` se o comando
    `claude` não existir) e no Claude Desktop (`~/.config/Claude/claude_desktop_config.json`, se a pasta existir)
    — opcional (--sem-claude).

Segurança:
  - manifesto `~/.local/share/ai-editor/instalacao.json` gravado ANTES de cada alteração, com o valor anterior;
    uma instalação interrompida (erro, Ctrl+C) pode ser desfeita com --desinstalar ou refeita com --instalar;
  - o valor anterior registrado na primeira vez nunca é sobrescrito; se o manifesto se perdeu e o valor atual já é
    nosso, o anterior é procurado no backup;
  - backup `<arquivo>.ai-editor.bak` do conteúdo ORIGINAL (só é criado se não existir; removido no --desinstalar);
  - gravação atômica (arquivo temporário com as mesmas permissões + fsync + rename); configurações que são links
    simbólicos (dotfiles) são gravadas no arquivo de destino, preservando o link;
  - o --desinstalar só desfaz o que ainda é nosso (se o usuário trocou depois, fica como ele deixou).

Só biblioteca padrão (não importa Qt). Respeita o modo dev (`core.NOME`): rodando do código-fonte, o nome é
`ai-editor-dev` e o manifesto é `instalacao-dev.json`.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
SECAO = "Default Applications"
SUFIXO_BAK = ".ai-editor.bak"
MARCA_DESKTOP = "X-AI-Editor-Instalador=1"


class FalhaPasso(RuntimeError):
    """Um passo da instalação falhou (os demais continuam; o código de saída vira 1)."""


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


def _real(caminho: Path) -> Path:
    """Destino real de uma configuração (resolve links simbólicos de dotfiles, mesmo apontando para nada ainda)."""
    return Path(os.path.realpath(caminho))


def _comando_claude() -> str | None:
    achado = shutil.which("claude")
    if achado:
        return achado
    local = _home() / ".claude" / "local" / "claude"  # instalação "local" do Claude Code
    return str(local) if local.is_file() and os.access(local, os.X_OK) else None


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


# ---------------------------------------------------------------- arquivos
def _backup(caminho: Path, man: dict) -> None:
    """Guarda o conteúdo ORIGINAL (primeira vez) em `<arquivo>.ai-editor.bak`, ao lado do arquivo real."""
    real = _real(caminho)
    if not real.exists():
        return
    bak = real.with_name(real.name + SUFIXO_BAK)
    if not bak.exists():
        shutil.copy2(real, bak)
    backups = man.setdefault("backups", [])
    if str(bak) not in backups:
        backups.append(str(bak))


def _gravar(caminho: Path, texto: str, modo_novo: int = 0o644) -> None:
    """Gravação atômica no arquivo real (segue links simbólicos), com as permissões do original."""
    real = _real(caminho)
    real.parent.mkdir(parents=True, exist_ok=True)
    modo = (real.stat().st_mode & 0o7777) if real.exists() else modo_novo
    fd, tmp = tempfile.mkstemp(prefix=f".{real.name}.", suffix=".ai-editor.tmp", dir=real.parent)  # nasce 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(texto)
            f.flush()
            os.fchmod(f.fileno(), modo)
            os.fsync(f.fileno())
        os.replace(tmp, real)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _ler_json(caminho: Path) -> dict:
    real = _real(caminho)
    if not real.exists():
        return {}
    texto = real.read_text(encoding="utf-8")
    if not texto.strip():
        return {}
    dados = json.loads(texto)  # JSON inválido → erro: não arriscamos sobrescrever
    if not isinstance(dados, dict):
        raise ValueError(f"{caminho} não é um objeto JSON")
    return dados


def _gravar_json(caminho: Path, dados: dict, modo_novo: int = 0o600) -> None:
    _gravar(caminho, json.dumps(dados, indent=2, ensure_ascii=False) + "\n", modo_novo)


def _ambiente_limpo() -> dict:
    """Ambiente para programas externos: sem as variáveis do AppImage/PyInstaller."""
    env = dict(os.environ)
    for v in ("APPDIR", "APPIMAGE", "ARGV0", "OWD", "PYINSTALLER_RESET_ENVIRONMENT"):
        env.pop(v, None)
    return env


def _rodar(cmd: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_ambiente_limpo())
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"tempo esgotado ({timeout:.0f} s)")
    except OSError as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


# ---------------------------------------------------------------- manifesto
def _ler_manifesto(log: list[str]) -> dict:
    arq = _manifesto()
    if not arq.exists():
        return {}
    try:
        return _ler_json(arq)
    except Exception as e:  # noqa: BLE001
        destino = arq.with_name(arq.name + f".corrompido-{_dt.datetime.now():%Y%m%d-%H%M%S}")
        arq.rename(destino)
        log.append(f"  ! manifesto ilegível ({e}); guardei como {destino.name} e sigo sem ele "
                   f"(os valores anteriores serão procurados nos backups *{SUFIXO_BAK})")
        return {}


def _salvar_manifesto(man: dict) -> None:
    _gravar_json(_manifesto(), man, modo_novo=0o644)


# ---------------------------------------------------------------- mimeapps.list (INI simples, preservando o resto)
def _ini_linha(linhas: list[str], secao: str, chave: str) -> str | None:
    """Linha original (sem quebra) da chave na seção, ou None."""
    atual = None
    for l in linhas:
        s = l.strip()
        if s.startswith("[") and s.endswith("]"):
            atual = s[1:-1]
        elif atual == secao and "=" in s and not s.startswith("#") and s.split("=", 1)[0].strip() == chave:
            return l
    return None


def _ini_valor(linhas: list[str], secao: str, chave: str) -> str | None:
    l = _ini_linha(linhas, secao, chave)
    return l.split("=", 1)[1].strip() if l is not None else None


def _ini_definir(linhas: list[str], secao: str, chave: str, linha_nova: str | None) -> list[str]:
    """Põe `linha_nova` no lugar da chave em `[secao]` (ou acrescenta no fim da seção); None remove a chave."""
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
            if not feito and linha_nova is not None:
                saida.append(linha_nova)
                fim_secao = len(saida)
            feito = True
            continue
        saida.append(l)
        if atual == secao and s:
            fim_secao = len(saida)
    if not feito and linha_nova is not None:
        if fim_secao is None:
            if saida and saida[-1].strip():
                saida.append("")
            saida += [f"[{secao}]", linha_nova]
        else:
            saida.insert(fim_secao, linha_nova)
    return saida


def _ini_remover_secao_vazia(linhas: list[str], secao: str) -> list[str]:
    """Tira `[secao]` se ela não tem mais nenhuma linha (nem comentário)."""
    for i, l in enumerate(linhas):
        if l.strip() == f"[{secao}]":
            j = i + 1
            while j < len(linhas) and not linhas[j].strip():
                j += 1
            if j == len(linhas) or linhas[j].strip().startswith("["):
                antes = linhas[:i]
                while antes and not antes[-1].strip():
                    antes.pop()
                depois = linhas[j:]
                return antes + ([""] if antes and depois else []) + depois
    return linhas


def _juntar(linhas: list[str], original: Path) -> str:
    """Junta as linhas mantendo a quebra de linha final como o arquivo original estava."""
    if not linhas:
        return ""
    texto = original.read_text(encoding="utf-8") if original.exists() else "\n"
    return "\n".join(linhas) + ("\n" if (texto.endswith("\n") or not texto) else "")


def _restaurar_bytes(caminho: Path, texto: str, mesmo) -> str:
    """No --desinstalar: se o resultado equivale ao backup do original, usa o original byte a byte
    (formatação, espaços e quebra final de linha como estavam)."""
    real = _real(caminho)
    bak = real.with_name(real.name + SUFIXO_BAK)
    if bak.exists():
        original = bak.read_text(encoding="utf-8")
        try:
            if mesmo(original, texto):
                return original
        except Exception:  # noqa: BLE001
            pass
    return texto


def _ler_linhas(caminho: Path) -> list[str]:
    real = _real(caminho)
    return real.read_text(encoding="utf-8").splitlines() if real.exists() else []


def _linhas_do_backup(caminho: Path) -> list[str] | None:
    real = _real(caminho)
    bak = real.with_name(real.name + SUFIXO_BAK)
    return bak.read_text(encoding="utf-8").splitlines() if bak.exists() else None


# ---------------------------------------------------------------- .desktop
_RESERVADOS = set(' \t\n"\'\\><~|&;$*?#()`')


def _exec_arg(arg: str) -> str:
    """Um argumento do Exec segundo a especificação Desktop Entry (aspas, escapes e %%)."""
    arg = arg.replace("%", "%%")
    if any(c in _RESERVADOS for c in arg):
        arg = '"' + re.sub(r'(["`$\\])', r"\\\1", arg) + '"'
    return arg


def _valor_desktop(s: str) -> str:
    """Escapes do tipo string do .desktop (aplicados por cima das regras do Exec)."""
    return s.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t")


def _texto_desktop(comando: Path) -> str:
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        f"Name={TITULO}",
        "GenericName=Player e editor de vídeo",
        "Comment=Player e editor de vídeo e áudio controlado por IA (MCP)",
        "Exec=" + _valor_desktop(_exec_arg(str(comando)) + " %f"),
        "TryExec=" + _valor_desktop(str(comando)),
        f"Icon={NOME}",
        "Terminal=false",
        "Categories=AudioVideo;Video;Player;AudioVideoEditing;",
        "Keywords=video;audio;player;editor;ffmpeg;mcp;claude;",
        f"StartupWMClass={NOME}",
        "MimeType=" + ";".join(TIPOS_MIME) + ";",
        MARCA_DESKTOP,
        "",
    ])


# ---------------------------------------------------------------- passos da instalação
def _entrada_mcp(comando: Path, claude_code: bool) -> dict:
    e = {"command": str(comando), "args": ["--mcp"]}
    if claude_code:
        e = {"type": "stdio", **e, "env": {}}
    return e


def _eh_nossa(entrada, comando: str) -> bool:
    return isinstance(entrada, dict) and entrada.get("command") == comando and entrada.get("args") == ["--mcp"]


def _passo_atalho(exe: Path, man: dict, log: list[str]) -> Path:
    """Atalho ~/.local/bin/ai-editor → AppImage. Devolve o comando estável a registrar."""
    atalho = _atalho()
    if atalho.exists() and not atalho.is_symlink():
        log.append(f"  ! {atalho} já existe e não é um atalho; não mexi (registro o caminho do AppImage)")
        return exe
    if "atalho" not in man:
        man["atalho"] = {"caminho": str(atalho), "anterior": os.readlink(atalho) if atalho.is_symlink() else None}
        _salvar_manifesto(man)
    atalho.parent.mkdir(parents=True, exist_ok=True)
    tmp = atalho.with_name(f".{atalho.name}.ai-editor.tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(exe)
    os.replace(tmp, atalho)
    log.append(f"  ✔ atalho {atalho} → {exe}")
    if str(atalho.parent) not in os.environ.get("PATH", "").split(":"):
        log.append(f"    (adicione {atalho.parent} ao PATH para usar o comando `{NOME}`)")
    return atalho


def _passo_menu(comando: Path, man: dict, log: list[str]) -> None:
    arquivos = man.setdefault("arquivos", [])
    for p in (_desktop(), _icone()):
        if str(p) not in arquivos:
            arquivos.append(str(p))
    _salvar_manifesto(man)
    _gravar(_desktop(), _texto_desktop(comando))
    _icone().parent.mkdir(parents=True, exist_ok=True)
    _gravar(_icone(), _icone_fonte().read_text(encoding="utf-8"))
    log.append(f"  ✔ menu de aplicativos: {_desktop()}")


def _passo_mime(man: dict, log: list[str]) -> None:
    arq = _mimeapps()
    linhas = _ler_linhas(arq)
    reg = man.get("mime")
    if reg is None:
        reg = man["mime"] = {"arquivo": str(arq), "criado": not _real(arq).exists(),
                             "secao_criada": not any(l.strip() == f"[{SECAO}]" for l in linhas),
                             "anteriores": {}}
    nosso = f"{NOME}.desktop"
    backup = None
    for tipo in TIPOS_MIME:
        if tipo in reg["anteriores"]:
            continue  # o anterior da primeira instalação vale
        linha = _ini_linha(linhas, SECAO, tipo)
        if linha is not None and _ini_valor(linhas, SECAO, tipo) == nosso:
            # já é nosso e o manifesto não sabe o anterior: procura no backup do original
            if backup is None:
                backup = _linhas_do_backup(arq) or []
            linha = _ini_linha(backup, SECAO, tipo)
            if linha is not None and _ini_valor(backup, SECAO, tipo) == nosso:
                linha = None
        reg["anteriores"][tipo] = linha
    _backup(arq, man)
    _salvar_manifesto(man)  # anteriores gravados antes de mexer
    for tipo in TIPOS_MIME:
        linhas = _ini_definir(linhas, SECAO, tipo, f"{tipo}={nosso}")
    _gravar(arq, _juntar(linhas, _real(arq)))
    log.append(f"  ✔ {len(TIPOS_MIME)} tipos de vídeo/áudio abrem com o {TITULO} ({arq})")


def _anterior_json(arq: Path, chave_servidor: str, comando: str) -> tuple[bool, dict | None]:
    """(conseguiu_ler, entrada anterior) do servidor no JSON; se já é nossa, tenta o backup."""
    servidores = _ler_json(arq).get("mcpServers") or {}
    anterior = servidores.get(chave_servidor)
    if _eh_nossa(anterior, comando):
        real = _real(arq)
        bak = real.with_name(real.name + SUFIXO_BAK)
        anterior = None
        if bak.exists():
            try:
                cand = (json.loads(bak.read_text(encoding="utf-8")).get("mcpServers") or {}).get(chave_servidor)
                anterior = None if _eh_nossa(cand, comando) else cand
            except Exception:  # noqa: BLE001
                pass
    return True, anterior


def _passo_claude_code(comando: Path, man: dict, log: list[str]) -> None:
    arq = _claude_json()
    claude = _comando_claude()
    if not claude and not _real(arq).exists():
        log.append(f"  · Claude Code não encontrado (nem o comando `claude` nem {arq}); pulei")
        return
    if "claude_code" not in man:
        try:
            _, anterior = _anterior_json(arq, NOME, str(comando))
        except Exception as e:  # noqa: BLE001
            raise FalhaPasso(f"não consegui ler {arq}: {e}; Claude Code não registrado") from e
        man["claude_code"] = {"arquivo": str(arq), "anterior": anterior}
    reg = man["claude_code"]
    reg["comando"] = str(comando)
    _salvar_manifesto(man)

    if claude:
        reg["metodo"] = "claude"
        _salvar_manifesto(man)
        _rodar([claude, "mcp", "remove", "--scope", "user", NOME])
        r = _rodar([claude, "mcp", "add", "--scope", "user", NOME, "--", str(comando), "--mcp"])
        if r.returncode != 0:
            erro = (r.stderr or r.stdout).strip()
            if reg.get("anterior"):  # o remove já tirou o anterior: põe de volta
                v = _rodar([claude, "mcp", "add-json", "--scope", "user", NOME, json.dumps(reg["anterior"])])
                erro += "; registro anterior " + ("restaurado" if v.returncode == 0 else "NÃO restaurado: "
                                                  + (v.stderr or v.stdout).strip())
            raise FalhaPasso(f"`claude mcp add` falhou: {erro}")
        log.append(f"  ✔ Claude Code: servidor MCP '{NOME}' registrado (claude mcp add --scope user)")
        return

    reg["metodo"] = "json"
    _backup(arq, man)
    _salvar_manifesto(man)
    dados = _ler_json(arq)
    dados.setdefault("mcpServers", {})[NOME] = _entrada_mcp(comando, claude_code=True)
    _gravar_json(arq, dados)
    log.append(f"  ✔ Claude Code: servidor MCP '{NOME}' gravado em {arq}")
    log.append("    (o comando `claude` não foi encontrado; se houver sessões do Claude Code abertas, feche-as e "
               "abra de novo — elas regravam esse arquivo ao sair)")


def _passo_claude_desktop(comando: Path, man: dict, log: list[str]) -> None:
    arq = _desktop_config()
    if not arq.parent.is_dir():
        log.append(f"  · Claude Desktop não encontrado ({arq.parent} não existe); pulei")
        return
    if "claude_desktop" not in man:
        try:
            _, anterior = _anterior_json(arq, NOME, str(comando))
        except Exception as e:  # noqa: BLE001
            raise FalhaPasso(f"não consegui ler {arq}: {e}; Claude Desktop não registrado") from e
        man["claude_desktop"] = {"arquivo": str(arq), "anterior": anterior, "criado": not _real(arq).exists()}
    man["claude_desktop"]["comando"] = str(comando)
    _backup(arq, man)
    _salvar_manifesto(man)
    dados = _ler_json(arq)
    dados.setdefault("mcpServers", {})[NOME] = _entrada_mcp(comando, claude_code=False)
    _gravar_json(arq, dados)
    log.append(f"  ✔ Claude Desktop: servidor MCP '{NOME}' gravado em {arq} (reinicie o Claude Desktop)")


def _atualizar_bancos() -> None:
    apps = _desktop().parent
    if shutil.which("update-desktop-database") and apps.is_dir():
        _rodar(["update-desktop-database", str(apps)])
    temas = _dados() / "icons" / "hicolor"
    if shutil.which("gtk-update-icon-cache") and (temas / "index.theme").exists():
        _rodar(["gtk-update-icon-cache", "-q", "-t", str(temas)])


def _executar(passo, *args, log: list[str], falhas: list[str]) -> object:
    try:
        return passo(*args, log)
    except (FalhaPasso, OSError, ValueError) as e:
        log.append(f"  ✘ {e}")
        falhas.append(str(e))
        return None


# ---------------------------------------------------------------- instalar / desinstalar
def instalar(mime: bool = True, claude: bool = True) -> int:
    exe = executavel()
    log = [f"Instalando {TITULO} → {exe}"]
    falhas: list[str] = []
    man = _ler_manifesto(log)
    man.update({"nome": NOME, "dev": DEV, "executavel": str(exe),
                "data": _dt.datetime.now().isoformat(timespec="seconds")})
    try:
        _salvar_manifesto(man)
        comando = exe
        if os.environ.get("APPIMAGE"):
            comando = _executar(_passo_atalho, exe, man, log=log, falhas=falhas) or exe
        man["comando"] = str(comando)
        _executar(_passo_menu, comando, man, log=log, falhas=falhas)
        if mime:
            _executar(_passo_mime, man, log=log, falhas=falhas)
        if claude:
            _executar(_passo_claude_code, comando, man, log=log, falhas=falhas)
            _executar(_passo_claude_desktop, comando, man, log=log, falhas=falhas)
        _atualizar_bancos()
    except KeyboardInterrupt:
        log.append("  ✘ interrompido; rode de novo --instalar para completar ou --desinstalar para desfazer")
        falhas.append("interrompido")
    finally:
        try:
            _salvar_manifesto(man)
        except OSError as e:
            log.append(f"  ✘ não consegui gravar o manifesto: {e}")
            falhas.append("manifesto")
        print("\n".join(log))
    if falhas:
        print(f"Concluído com {len(falhas)} problema(s). O que foi feito está no manifesto {_manifesto()} "
              "(--desinstalar desfaz).", file=sys.stderr)
        return 1
    print(f"  ✔ manifesto: {_manifesto()}  (para desfazer: {NOME} --desinstalar)")
    if claude:
        print("Pronto. No Claude Code, rode /mcp (ou abra uma sessão nova) para conectar.")
    return 0


def _desfazer_mime(man: dict, nome: str, log: list[str]) -> None:
    reg = man["mime"]
    arq = Path(reg["arquivo"])
    if not _real(arq).exists():
        return
    linhas = _ler_linhas(arq)
    nosso = f"{nome}.desktop"
    for tipo, anterior in reg["anteriores"].items():
        if _ini_valor(linhas, SECAO, tipo) == nosso:  # só se ninguém trocou depois
            linhas = _ini_definir(linhas, SECAO, tipo, anterior)
    if reg.get("secao_criada"):
        linhas = _ini_remover_secao_vazia(linhas, SECAO)
    if reg.get("criado") and not any(l.strip() for l in linhas):
        _real(arq).unlink()
    else:
        texto = _restaurar_bytes(arq, _juntar(linhas, _real(arq)), lambda a, b: a.splitlines() == b.splitlines())
        _gravar(arq, texto)
    log.append(f"  ✔ tipos MIME restaurados em {arq}")


def _desfazer_json(reg: dict, nome: str, rotulo: str, log: list[str]) -> None:
    arq = Path(reg["arquivo"])
    if not _real(arq).exists():
        return
    dados = _ler_json(arq)
    servidores = dados.setdefault("mcpServers", {})
    if not _eh_nossa(servidores.get(nome), reg.get("comando", "")):
        log.append(f"  · {rotulo}: o registro '{nome}' não é mais o nosso; deixei como está")
        return
    if reg.get("anterior"):
        servidores[nome] = reg["anterior"]
    else:
        servidores.pop(nome, None)
    if reg.get("criado") and dados == {"mcpServers": {}}:
        _real(arq).unlink()
    else:
        texto = json.dumps(dados, indent=2, ensure_ascii=False) + "\n"
        _gravar(arq, _restaurar_bytes(arq, texto, lambda a, b: json.loads(a) == json.loads(b)), 0o600)
    log.append(f"  ✔ {rotulo}: registro '{nome}' {'restaurado' if reg.get('anterior') else 'removido'}")


def _desfazer_claude_code(man: dict, nome: str, log: list[str]) -> None:
    reg = man["claude_code"]
    claude = _comando_claude()
    if reg.get("metodo") == "claude" and claude:
        try:
            atual = (_ler_json(Path(reg["arquivo"])).get("mcpServers") or {}).get(nome)
        except Exception:  # noqa: BLE001
            atual = None
        if atual is not None and not _eh_nossa(atual, reg.get("comando", "")):
            log.append(f"  · Claude Code: o registro '{nome}' não é mais o nosso; deixei como está")
            return
        r = _rodar([claude, "mcp", "remove", "--scope", "user", nome])
        if r.returncode != 0 and atual is not None:
            raise FalhaPasso(f"`claude mcp remove` falhou: {(r.stderr or r.stdout).strip()}")
        if reg.get("anterior"):
            r = _rodar([claude, "mcp", "add-json", "--scope", "user", nome, json.dumps(reg["anterior"])])
            if r.returncode != 0:
                raise FalhaPasso(f"não consegui restaurar o registro anterior: {(r.stderr or r.stdout).strip()}")
        log.append(f"  ✔ Claude Code: registro '{nome}' {'restaurado' if reg.get('anterior') else 'removido'}")
    else:
        _desfazer_json(reg, nome, "Claude Code", log)


def _desfazer_arquivos(man: dict, nome: str, log: list[str]) -> None:
    for a in man.get("arquivos", []):
        p = Path(a)
        if p.suffix == ".desktop" and p.exists() and MARCA_DESKTOP not in p.read_text(encoding="utf-8"):
            log.append(f"  · {p} não é mais o nosso; deixei")
            continue
        if p.exists() or p.is_symlink():
            p.unlink()
            log.append(f"  ✔ removido {p}")


def _desfazer_atalho(man: dict, nome: str, log: list[str]) -> None:
    atalho = Path(man["atalho"]["caminho"])
    if atalho.is_symlink() and os.readlink(atalho) == man.get("executavel"):
        atalho.unlink()
        if man["atalho"].get("anterior"):
            atalho.symlink_to(man["atalho"]["anterior"])
        log.append(f"  ✔ atalho {atalho} {'restaurado' if man['atalho'].get('anterior') else 'removido'}")


def desinstalar() -> int:
    log: list[str] = []
    man = _ler_manifesto(log)
    if not man:
        print("\n".join(log + [f"Nada a desfazer: {_manifesto()} não existe."]))
        return 0
    nome = man.get("nome", NOME)
    log.insert(0, f"Desinstalando {nome}")
    falhas: list[str] = []
    passos = [("mime", _desfazer_mime), ("claude_code", _desfazer_claude_code),
              ("claude_desktop", lambda m, n, l: _desfazer_json(m["claude_desktop"], n, "Claude Desktop", l)),
              ("arquivos", _desfazer_arquivos), ("atalho", _desfazer_atalho)]
    try:
        for chave, desfazer in passos:
            if chave not in man:
                continue
            try:
                desfazer(man, nome, log)
                del man[chave]  # só sai do manifesto o que deu certo
                _salvar_manifesto(man)
            except (FalhaPasso, OSError, ValueError) as e:
                log.append(f"  ✘ {chave}: {e}")
                falhas.append(chave)
        _atualizar_bancos()
    finally:
        if falhas:
            _salvar_manifesto(man)
        print("\n".join(log))
    if falhas:
        print(f"Desinstalação incompleta ({', '.join(falhas)}); o manifesto {_manifesto()} guarda o que falta.",
              file=sys.stderr)
        return 1
    for b in man.get("backups", []):  # tudo restaurado: os backups do original não são mais necessários
        try:
            Path(b).unlink()
        except FileNotFoundError:
            pass
    _manifesto().unlink()
    print("Pronto.")
    return 0
