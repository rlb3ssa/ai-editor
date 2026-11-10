"""Modal de metadados (glassmorphism): fundo desfocado da própria janela + cartão translúcido."""
from __future__ import annotations

import html
import json
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, QRunnable, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QTextBrowser, QToolButton,
                               QVBoxLayout, QWidget)

from . import core


# ---------------------------------------------------------------- dados → seções
def _achatar(obj, prefixo: str = "") -> list[tuple[str, str]]:
    """Transforma dicionários/listas aninhados em pares (chave.sub, valor)."""
    out: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += _achatar(v, f"{prefixo}.{k}" if prefixo else str(k))
    elif isinstance(obj, list):
        if all(not isinstance(x, (dict, list)) for x in obj):
            out.append((prefixo, ", ".join(str(x) for x in obj)))
        else:
            for i, x in enumerate(obj):
                out += _achatar(x, f"{prefixo}[{i}]")
    else:
        out.append((prefixo, "" if obj is None else str(obj)))
    return out


def secoes(rel: dict) -> list[tuple[str, list[tuple[str, str]]]]:
    """Organiza o relatório de core.metadados() em seções legíveis, sem perder nenhum campo."""
    s: list[tuple[str, list[tuple[str, str]]]] = []
    s.append(("Arquivo", [("caminho", rel.get("arquivo", ""))]))
    pistas = rel.get("pistas_de_origem") or []
    s.append(("Pistas de origem", [("•", p) for p in pistas] or [("", "nenhuma pista encontrada")]))
    resumo = dict(rel.get("resumo") or {})
    faixas = resumo.pop("faixas", []) or []
    linhas = [(k, v) for k, v in _achatar(resumo) if v not in ("", "None", "0")]
    for f in faixas:
        f = dict(f)
        nome = f"faixa {f.pop('indice', '?')} ({f.pop('tipo', '?')})"
        linhas.append((nome, " · ".join(str(v) for v in f.values() if v not in (None, ""))))
    s.append(("Resumo", linhas))
    s.append(("Sistema de arquivos", _achatar(rel.get("sistema_de_arquivos", {}))))

    fp = rel.get("ffprobe") or {}
    if isinstance(fp, dict):
        if fp.get("format"):
            s.append(("FFprobe · formato", _achatar(fp["format"])))
        for st in fp.get("streams", []):
            s.append((f"FFprobe · faixa {st.get('index')} ({st.get('codec_type', '?')})", _achatar(st)))
        for i, ch in enumerate(fp.get("chapters", [])):
            s.append((f"FFprobe · capítulo {i + 1}", _achatar(ch)))
        for i, pr in enumerate(fp.get("programs", [])):
            s.append((f"FFprobe · programa {i + 1}", _achatar(pr)))
        resto = {k: v for k, v in fp.items() if k not in ("format", "streams", "chapters", "programs")}
        if resto:
            s.append(("FFprobe · outros", _achatar(resto)))

    mi = rel.get("mediainfo") or {}
    faixas_mi = (mi.get("media") or {}).get("track") if isinstance(mi, dict) else None
    if faixas_mi:
        for t in faixas_mi:
            s.append((f"MediaInfo · {t.get('@type', '?')}", _achatar({k: v for k, v in t.items() if k != "@type"})))
    elif mi:
        s.append(("MediaInfo", _achatar(mi)))

    ex = rel.get("exiftool") or {}
    if isinstance(ex, dict):
        grupos: dict[str, list[tuple[str, str]]] = {}
        for k, v in ex.items():
            g, _, nome = k.rpartition(":")
            grupos.setdefault(g or "Geral", []).extend(_achatar(v, nome))
        for g, linhas in grupos.items():
            s.append((f"ExifTool · {g}", linhas))

    s.append(("C2PA / Content Credentials", _achatar(rel.get("c2pa", {}))))
    conhecidas = {"arquivo", "resumo", "pistas_de_origem", "sistema_de_arquivos", "ffprobe", "mediainfo",
                  "exiftool", "c2pa"}
    for k, v in rel.items():
        if k not in conhecidas:
            s.append((k, _achatar(v)))
    return [(t, l) for t, l in s if l]


def como_texto(sec) -> str:
    partes = []
    for titulo, linhas in sec:
        partes.append(f"== {titulo} ==")
        partes += [f"{k}: {v}" if k else v for k, v in linhas]
        partes.append("")
    return "\n".join(partes)


def como_html(sec) -> str:
    h = []
    for titulo, linhas in sec:
        h.append(f"<h3>{html.escape(titulo)}</h3><table width='100%' cellspacing='0' cellpadding='3'>")
        for k, v in linhas:
            h.append(f"<tr><td class='k' width='28%'>{html.escape(k)}</td>"
                     f"<td class='v'>{html.escape(v)}</td></tr>")
        h.append("</table>")
    return "".join(h)


_CSS = """
h3 { color: #8ec5ff; font-size: 13px; font-weight: 600; margin: 18px 0 6px 0; }
td.k { color: #9aa4b2; font-size: 12px; }
td.v { color: #eef2f7; font-family: 'JetBrains Mono','DejaVu Sans Mono',monospace; font-size: 12px; }
"""


# ---------------------------------------------------------------- coleta em segundo plano
class _Sinal(QObject):
    pronto = Signal(str, object)


class _Tarefa(QRunnable):
    def __init__(self, arquivo: str, sinal: _Sinal):
        super().__init__()
        self.arquivo, self.sinal = arquivo, sinal

    def run(self):
        try:
            r = core.metadados(self.arquivo)
        except Exception as e:  # noqa: BLE001
            r = {"arquivo": self.arquivo, "erro": str(e)}
        self.sinal.pronto.emit(self.arquivo, r)


def _desfocar(img: QImage) -> QPixmap:
    """Desfoque barato e suave: reduz muito e amplia em etapas (bilinear)."""
    w, h = img.width(), img.height()
    if w < 4 or h < 4:
        return QPixmap.fromImage(img)
    p = img.scaled(max(1, w // 16), max(1, h // 16), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    p = p.scaled(max(1, w // 4), max(1, h // 4), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    p = p.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return QPixmap.fromImage(p)


# ---------------------------------------------------------------- modal
class PainelMetadados(QWidget):
    """Cobre a janela inteira. Esc ou clique fora do cartão fecha."""

    def __init__(self, janela, icone):
        super().__init__(janela)
        self.janela = janela
        self._icone = icone
        self._fundo: QPixmap | None = None
        self._texto = ""
        self._arquivo = None
        self._sinal = _Sinal()
        self._sinal.pronto.connect(self._preencher)
        self.setFocusPolicy(Qt.StrongFocus)
        self.hide()

        self.cartao = QFrame(self)
        self.cartao.setObjectName("vidro")
        lc = QVBoxLayout(self.cartao)
        lc.setContentsMargins(22, 16, 14, 16)
        lc.setSpacing(10)

        topo = QHBoxLayout()
        tit = QVBoxLayout()
        tit.setSpacing(0)
        self.l_titulo = QLabel("Metadados")
        self.l_titulo.setObjectName("vidro_titulo")
        self.l_sub = QLabel("")
        self.l_sub.setObjectName("vidro_sub")
        tit.addWidget(self.l_titulo)
        tit.addWidget(self.l_sub)
        topo.addLayout(tit, 1)
        self.b_copiar = QToolButton()
        self.b_copiar.setObjectName("vidro_botao")
        self.b_copiar.setIcon(icone("content-copy", "#e6edf3"))
        self.b_copiar.setIconSize(QSize(18, 18))
        self.b_copiar.setText(" Copiar tudo")
        self.b_copiar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.b_copiar.setToolTip("Copiar todos os metadados como texto")
        self.b_copiar.setCursor(Qt.PointingHandCursor)
        self.b_copiar.clicked.connect(self.copiar)
        topo.addWidget(self.b_copiar, 0, Qt.AlignTop)
        lc.addLayout(topo)

        self.texto = QTextBrowser()
        self.texto.setObjectName("vidro_texto")
        self.texto.setOpenLinks(False)
        self.texto.document().setDefaultStyleSheet(_CSS)
        lc.addWidget(self.texto, 1)

        rod = QLabel("Esc ou clique fora para fechar")
        rod.setObjectName("vidro_sub")
        lc.addWidget(rod, 0, Qt.AlignRight)

    # ---- abrir/fechar
    def abrir(self, arquivo: Path | str):
        self._arquivo = str(arquivo)
        self._fundo = _desfocar(self.janela.grab().toImage())
        self.setGeometry(self.janela.rect())
        self._posicionar()
        self.l_sub.setText(Path(arquivo).name)
        self.l_sub.setToolTip(str(arquivo))
        self.texto.setHtml("<p style='color:#9aa4b2'>Lendo metadados (ffprobe, mediainfo, exiftool)…</p>")
        self._texto = ""
        self.b_copiar.setEnabled(False)
        self.show()
        self.raise_()
        self.setFocus()
        QThreadPool.globalInstance().start(_Tarefa(self._arquivo, self._sinal))

    def fechar(self):
        self.hide()
        self._fundo = None
        self.janela.setFocus()

    def _preencher(self, arquivo: str, rel: dict):
        if arquivo != self._arquivo or not self.isVisible():
            return
        sec = secoes(rel)
        self._texto = como_texto(sec)
        self.texto.setHtml(como_html(sec))
        self.b_copiar.setEnabled(True)

    def copiar(self):
        QApplication.clipboard().setText(self._texto)
        self.b_copiar.setText(" Copiado!")
        self.b_copiar.setIcon(self._icone("check", "#4ade80"))
        QTimer.singleShot(1600, self._restaurar_botao)

    def _restaurar_botao(self):
        self.b_copiar.setText(" Copiar tudo")
        self.b_copiar.setIcon(self._icone("content-copy", "#e6edf3"))

    # ---- geometria e desenho
    def _posicionar(self):
        w, h = self.width(), self.height()
        cw, ch = min(920, int(w * 0.86)), int(h * 0.84)
        self.cartao.setGeometry((w - cw) // 2, (h - ch) // 2, cw, ch)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._posicionar()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self._fundo:
            p.drawPixmap(self.rect(), self._fundo)
        p.fillRect(self.rect(), QColor(8, 10, 14, 120))
        # brilho suave atrás do cartão (realça o efeito de vidro)
        r = QRectF(self.cartao.geometry()).adjusted(-1, -1, 1, 1)
        caminho = QPainterPath()
        caminho.addRoundedRect(r, 17, 17)
        p.fillPath(caminho, QColor(255, 255, 255, 10))

    def mousePressEvent(self, ev):
        if not self.cartao.geometry().contains(ev.position().toPoint()):
            self.fechar()
        ev.accept()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape:
            self.fechar()
            return
        if ev.matches(QKeySequence.Copy) and not self.texto.textCursor().hasSelection():
            self.copiar()
            return
        super().keyPressEvent(ev)


QSS = """
QFrame#vidro {
    background: rgba(28, 33, 43, 150);
    border: 1px solid rgba(255, 255, 255, 38);
    border-radius: 16px;
}
QLabel#vidro_titulo { color: #ffffff; font-size: 17px; font-weight: 600; background: transparent; }
QLabel#vidro_sub { color: #9aa4b2; font-size: 11px; background: transparent; }
QTextBrowser#vidro_texto {
    background: rgba(255, 255, 255, 6);
    border: 1px solid rgba(255, 255, 255, 18);
    border-radius: 10px;
    padding: 4px 10px;
    selection-background-color: rgba(79, 163, 255, 120);
}
QToolButton#vidro_botao {
    color: #e6edf3; background: rgba(255, 255, 255, 18);
    border: 1px solid rgba(255, 255, 255, 40); border-radius: 8px; padding: 5px 10px;
}
QToolButton#vidro_botao:hover { background: rgba(255, 255, 255, 34); }
QToolButton#vidro_botao:disabled { color: #6e7681; }
"""
