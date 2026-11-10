"""Janela do claude-player: vídeo (libmpv) + régua em ms + uma onda por faixa de áudio."""
from __future__ import annotations

import json
import locale
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "xcb")  # embutir o mpv via X11/XWayland
os.environ.setdefault("QT_API", "pyside6")

import numpy as np
from PySide6.QtCore import (QObject, QPointF, QRectF, QRunnable, QSize, Qt, QThreadPool, QTimer,
                            Signal)
from PySide6.QtGui import (QAction, QColor, QFont, QKeySequence, QPainter, QPalette, QPen,
                           QPolygonF, QShortcut)
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QMainWindow, QMenu, QMessageBox, QScrollArea,
                               QScrollBar, QSlider, QSizePolicy, QToolButton, QVBoxLayout,
                               QWidget)

import qtawesome as qta

from . import cliente
from .core import CACHE_DIR, RUNTIME_DIR, SOCKET_PATH, Documento, fmt_tempo, parse_tempo
from .ondas import PICOS_POR_SEG, picos

CAB = 180  # largura do cabeçalho à esquerda das faixas
COR_FUNDO = QColor("#15171c")
COR_TRILHA = QColor("#1f232b")
COR_ONDA = QColor("#4fa3ff")
COR_ONDA_MUDA = QColor("#4a4f5a")
COR_CABECA = QColor("#ff4757")
COR_INOUT = QColor(255, 196, 0, 50)
COR_MARCA = QColor("#ffc400")
COR_PROGRESSO = QColor("#2e6fbf")
COR_TEXTO = QColor("#c9d1d9")
ICONE = "#c9d1d9"
ICONE_ATIVO = "#ffffff"
DESTAQUE = "#4fa3ff"


# Estilo de ícones: "ph" (Phosphor) ou "mdi6" (Material Design). Os nomes no código são os do MDI;
# para Phosphor eles são traduzidos pela tabela abaixo.
ESTILO_ICONES = os.environ.get("CLAUDE_PLAYER_ICONES", "ph")
_PHOSPHOR = {
    "folder-open-outline": "folder-open",
    "skip-previous": "skip-back-fill",
    "skip-next": "skip-forward-fill",
    "step-backward": "caret-left-fill",
    "step-forward": "caret-right-fill",
    "play": "play-fill",
    "pause": "pause-fill",
    "ray-start-arrow": "arrow-line-left",
    "ray-end-arrow": "arrow-line-right",
    "crosshairs-gps": "crosshair-simple",
    "volume-high": "speaker-high",
    "volume-off": "speaker-x",
    "headphones": "headphones",
    "fullscreen": "corners-out",
    "undo-variant": "arrow-counter-clockwise",
    "delete-outline": "trash",
    "content-save-outline": "floppy-disk",
    "chevron-down": "caret-down",
}


def icone(nome: str, cor: str = ICONE):
    if ESTILO_ICONES == "ph":
        nome = "ph." + _PHOSPHOR.get(nome, nome)
    else:
        nome = "mdi6." + nome
    return qta.icon(nome, color=cor, color_active=ICONE_ATIVO, color_disabled="#4a4f5a")


# ====================================================================== visão
class Visao(QObject):
    """Intervalo de tempo visível, compartilhado por régua e ondas (zoom/rolagem)."""
    mudou = Signal()

    def __init__(self):
        super().__init__()
        self.dur = 0.0
        self.ini = 0.0
        self.fim = 1.0

    def definir_duracao(self, d: float):
        self.dur = max(d, 0.001)
        self.ini, self.fim = 0.0, self.dur
        self.mudou.emit()

    @property
    def span(self):
        return self.fim - self.ini

    def t_para_x(self, t, w):
        return (t - self.ini) / self.span * w

    def x_para_t(self, x, w):
        return min(max(self.ini + x / max(w, 1) * self.span, 0.0), self.dur)

    def zoom(self, fator, centro):
        novo = min(max(self.span * fator, 0.05), self.dur)
        rel = (centro - self.ini) / self.span if self.span else 0.5
        ini = centro - rel * novo
        self._por(ini, novo)

    def mover(self, ini):
        self._por(ini, self.span)

    def _por(self, ini, span):
        ini = min(max(ini, 0.0), max(self.dur - span, 0.0))
        self.ini, self.fim = ini, ini + span
        self.mudou.emit()

    def seguir(self, t):
        if self.span < self.dur and not (self.ini <= t <= self.fim):
            self.mover(t - self.span * 0.1)


# ====================================================================== área de tempo base
class AreaTempo(QWidget):
    """Base para régua e ondas: cliques/arraste = seek, roda = zoom, shift+roda = rolar."""

    def __init__(self, janela: "Janela"):
        super().__init__()
        self.j = janela
        self.v = janela.visao
        self.v.mudou.connect(self.update)
        self.setMouseTracking(True)
        self._arrastando = False

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._arrastando = True
            self.j.pular(self.v.x_para_t(e.position().x(), self.width()), exato=False)
        elif e.button() == Qt.RightButton:
            t = self.v.x_para_t(e.position().x(), self.width())
            m = QMenu(self)
            m.addAction(f"Marcar IN em {fmt_tempo(t)}", lambda: self.j.marcar(entrada=t))
            m.addAction(f"Marcar OUT em {fmt_tempo(t)}", lambda: self.j.marcar(saida=t))
            m.addAction("Limpar marcas", lambda: self.j.marcar(limpar=True))
            m.addSeparator()
            m.addAction("Ver tudo (zoom total)", lambda: self.v.zoom(1e9, 0))
            m.exec(e.globalPosition().toPoint())

    def mouseMoveEvent(self, e):
        t = self.v.x_para_t(e.position().x(), self.width())
        self.setToolTip(fmt_tempo(t))
        if self._arrastando:
            self.j.pular(t, exato=False)

    def mouseReleaseEvent(self, e):
        if self._arrastando:
            self._arrastando = False
            self.j.pular(self.v.x_para_t(e.position().x(), self.width()), exato=True)

    def wheelEvent(self, e):
        d = e.angleDelta()
        if e.modifiers() & Qt.ShiftModifier or abs(d.x()) > abs(d.y()):
            passo = (d.x() or d.y()) / 120 * self.v.span * 0.1
            self.v.mover(self.v.ini - passo)
        else:
            t = self.v.x_para_t(e.position().x(), self.width())
            self.v.zoom(0.8 if d.y() > 0 else 1.25, t)

    def sobreposicao(self, p: QPainter):
        w, h = self.width(), self.height()
        j = self.j
        if j.marca_in is not None or j.marca_out is not None:
            a = j.marca_in if j.marca_in is not None else 0.0
            b = j.marca_out if j.marca_out is not None else self.v.dur
            xa, xb = self.v.t_para_x(a, w), self.v.t_para_x(b, w)
            p.fillRect(QRectF(xa, 0, xb - xa, h), COR_INOUT)
            p.setPen(QPen(COR_MARCA, 1))
            for t in (j.marca_in, j.marca_out):
                if t is not None:
                    x = self.v.t_para_x(t, w)
                    p.drawLine(QPointF(x, 0), QPointF(x, h))
        x = self.v.t_para_x(j.pos, w)
        p.setPen(QPen(COR_CABECA, 1.5))
        p.drawLine(QPointF(x, 0), QPointF(x, h))


_PASSOS = [0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60,
           120, 300, 600, 900, 1800, 3600]


def _rotulo(t: float, passo: float, dur: float) -> str:
    s = fmt_tempo(t)
    if dur < 3600:
        s = s[3:]
    if passo >= 1:
        s = s[:-4]
    elif passo >= 0.1:
        s = s[:-2]
    elif passo >= 0.01:
        s = s[:-1]
    return s


class Regua(AreaTempo):
    def __init__(self, janela):
        super().__init__(janela)
        self.setFixedHeight(42)

    def paintEvent(self, _):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), COR_TRILHA)
        if self.v.dur <= 0.001:
            return
        # progresso
        xp = self.v.t_para_x(self.j.pos, w)
        p.fillRect(QRectF(0, h - 10, max(0, xp), 10), COR_PROGRESSO)
        # marcações
        passo = next((s for s in _PASSOS if s / self.v.span * w >= 90), _PASSOS[-1])
        p.setFont(QFont("monospace", 8))
        t = (self.v.ini // passo) * passo
        while t <= self.v.fim + passo:
            x = self.v.t_para_x(t, w)
            p.setPen(QPen(COR_TEXTO.darker(140), 1))
            p.drawLine(QPointF(x, h - 18), QPointF(x, h - 10))
            for k in range(1, 5):
                xs = self.v.t_para_x(t + passo * k / 5, w)
                p.drawLine(QPointF(xs, h - 14), QPointF(xs, h - 10))
            p.setPen(COR_TEXTO)
            p.drawText(QPointF(x + 3, 12), _rotulo(t, passo, self.v.dur))
            t += passo
        self.sobreposicao(p)
        # alça da cabeça de leitura
        p.setBrush(COR_CABECA)
        p.setPen(Qt.NoPen)
        p.drawPolygon(QPolygonF([QPointF(xp - 6, 0), QPointF(xp + 6, 0), QPointF(xp, 9)]))


class Onda(AreaTempo):
    def __init__(self, janela, faixa_idx: int):
        super().__init__(janela)
        self.faixa = faixa_idx
        self.dados: np.ndarray | None = None
        self.setMinimumHeight(48)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def paintEvent(self, _):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), COR_FUNDO)
        meio = h / 2
        p.setPen(QPen(COR_TRILHA, 1))
        p.drawLine(QPointF(0, meio), QPointF(w, meio))
        if self.dados is None:
            p.setPen(COR_TEXTO)
            p.drawText(self.rect(), Qt.AlignCenter, "calculando forma de onda…")
        elif w > 2:
            d = self.dados
            i0 = self.v.ini * PICOS_POR_SEG
            i1 = self.v.fim * PICOS_POR_SEG
            bordas = np.clip(np.linspace(i0, i1, w + 1).astype(np.int64), 0, len(d) - 1)
            if i1 - i0 >= w:
                vals = np.maximum.reduceat(d, bordas[:-1])
            else:
                vals = d[bordas[:-1]]
            vals = np.power(vals, 0.6) * (meio - 2)
            ativo = self.j.faixa_audivel(self.faixa)
            p.setPen(QPen(COR_ONDA if ativo else COR_ONDA_MUDA, 1))
            fora = (bordas[:-1] >= len(d) - 1) & (np.arange(w) > 0)
            linhas = [(x, float(a)) for x, a in enumerate(vals) if not fora[x]]
            for x, a in linhas:
                p.drawLine(QPointF(x, meio - a), QPointF(x, meio + a))
        self.sobreposicao(p)


class LinhaFaixa(QWidget):
    """Cabeçalho (nome, mudo, solo, volume) + onda de uma faixa de áudio."""

    def __init__(self, janela: "Janela", idx: int, info: dict):
        super().__init__()
        self.j, self.idx = janela, idx
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        cab = QWidget()
        cab.setFixedWidth(CAB)
        cl = QVBoxLayout(cab)
        cl.setContentsMargins(6, 3, 6, 3)
        cl.setSpacing(2)
        nome = f"A{idx + 1}"
        det = " ".join(str(x) for x in (info.get("lang"), info.get("codec"),
                                        f"{info.get('demux-channel-count', '?')}ch") if x)
        rot = QLabel(f"<b>{nome}</b> <span style='color:#8b949e'>{det}</span>")
        if info.get("title"):
            rot.setToolTip(info["title"])
        cl.addWidget(rot)
        lin = QHBoxLayout()
        self.b_mudo = self._botao("volume-off", "Mudo", "mudo")
        self.b_solo = self._botao("headphones", "Solo (ouvir só esta faixa)", "solo")
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 200)
        self.vol.setValue(100)
        self.vol.setFocusPolicy(Qt.NoFocus)
        self.vol.setToolTip("Volume desta faixa na reprodução (não altera o arquivo)")
        self.vol.valueChanged.connect(lambda _: self.j.atualizar_mix())
        lin.addWidget(self.b_mudo)
        lin.addWidget(self.b_solo)
        lin.addWidget(self.vol)
        cl.addLayout(lin)
        lay.addWidget(cab)
        self.onda = Onda(janela, idx)
        lay.addWidget(self.onda, 1)
        self.setFixedHeight(janela.altura_faixa)

    def _botao(self, nome_icone, dica, papel):
        b = QToolButton()
        b.setIcon(icone(nome_icone))
        b.setIconSize(QSize(16, 16))
        b.setObjectName(papel)
        b.setToolTip(dica)
        b.setCheckable(True)
        b.setFocusPolicy(Qt.NoFocus)
        b.setFixedSize(28, 24)
        b.toggled.connect(lambda _: self.j.atualizar_mix())
        return b


# ====================================================================== trabalho em segundo plano
class _SinalOnda(QObject):
    pronto = Signal(str, int, object)


class TarefaOnda(QRunnable):
    def __init__(self, arquivo: str, faixa: int, sinal: _SinalOnda):
        super().__init__()
        self.arquivo, self.faixa, self.sinal = arquivo, faixa, sinal

    def run(self):
        try:
            d = picos(self.arquivo, self.faixa)
        except Exception:  # noqa: BLE001
            d = np.zeros(1, np.float32)
        self.sinal.pronto.emit(self.arquivo, self.faixa, d)


class _SinaisMpv(QObject):
    faixas = Signal()


# ====================================================================== janela
class TelaVideo(QWidget):
    """Área nativa onde o mpv desenha. Roda = zoom no ponto do cursor; arrastar = mover a imagem;
    duplo clique = voltar ao tamanho normal. (O mpv roda com input-cursor=no, então os eventos de
    mouse da janela X dele sobem para este widget.)"""

    ZOOM_MAX = 4.0  # log2 → 16x

    def __init__(self, janela):
        super().__init__()
        self.janela = janela
        self.zoom = 0.0          # log2 da escala (video-zoom do mpv)
        self.pan = [0.0, 0.0]    # fração do tamanho do vídeo escalado (video-pan-x/y)
        self._arrasto = None
        self.setAttribute(Qt.WA_NativeWindow)
        self.setMouseTracking(False)

    def _tamanho_base(self):
        """Tamanho (px lógicos) do vídeo encaixado na área, sem zoom."""
        dw = self.janela._get("dwidth") or 16
        dh = self.janela._get("dheight") or 9
        ww, wh = max(1, self.width()), max(1, self.height())
        f = min(ww / dw, wh / dh)
        return dw * f, dh * f

    def _aplicar(self, avisar=True):
        if self.zoom <= 0:
            self.zoom, self.pan = 0.0, [0.0, 0.0]
        self.pan = [max(-0.5, min(0.5, v)) for v in self.pan]
        j = self.janela
        j._set("video-zoom", self.zoom)
        j._set("video-pan-x", self.pan[0])
        j._set("video-pan-y", self.pan[1])
        self.setCursor(Qt.OpenHandCursor if self.zoom > 0 else Qt.ArrowCursor)
        if not avisar:
            return
        if self.zoom > 0:
            j.statusBar().showMessage(f"zoom {2 ** self.zoom * 100:.0f}% · arraste para mover · duplo clique volta a 100%", 3000)
        else:
            j.statusBar().showMessage("zoom 100%", 1500)

    def resetar(self):
        self.zoom, self.pan = 0.0, [0.0, 0.0]
        self._arrasto = None
        self._aplicar(avisar=False)

    def definir(self, zoom: float, pan_x: float = 0.0, pan_y: float = 0.0):
        self.zoom = max(0.0, min(self.ZOOM_MAX, zoom))
        self.pan = [pan_x, pan_y]
        self._aplicar()

    def wheelEvent(self, ev):
        passos = ev.angleDelta().y() / 120
        if not passos:
            return
        bw, bh = self._tamanho_base()
        w0, h0 = bw * 2 ** self.zoom, bh * 2 ** self.zoom
        novo = max(0.0, min(self.ZOOM_MAX, self.zoom + passos * 0.25))
        w1, h1 = bw * 2 ** novo, bh * 2 ** novo
        # mantém sob o cursor o mesmo ponto do vídeo
        p = ev.position()
        dx, dy = p.x() - self.width() / 2, p.y() - self.height() / 2
        u, v = dx / w0 - self.pan[0], dy / h0 - self.pan[1]
        self.pan = [dx / w1 - u, dy / h1 - v]
        self.zoom = novo
        self._aplicar()
        ev.accept()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton and self.zoom > 0:
            self._arrasto = (ev.position(), list(self.pan))
            self.setCursor(Qt.ClosedHandCursor)
        ev.accept()

    def mouseMoveEvent(self, ev):
        if self._arrasto:
            ini, pan0 = self._arrasto
            bw, bh = self._tamanho_base()
            esc = 2 ** self.zoom
            d = ev.position() - ini
            self.pan = [max(-0.5, min(0.5, pan0[0] + d.x() / (bw * esc))),
                        max(-0.5, min(0.5, pan0[1] + d.y() / (bh * esc)))]
            self.janela._set("video-pan-x", self.pan[0])
            self.janela._set("video-pan-y", self.pan[1])
        ev.accept()

    def mouseReleaseEvent(self, ev):
        if self._arrasto:
            self._arrasto = None
            self.setCursor(Qt.OpenHandCursor if self.zoom > 0 else Qt.ArrowCursor)
        ev.accept()

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.resetar()
        ev.accept()


class Janela(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Claude Player")
        tela = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1100, int(tela.width() * 0.9)), min(760, int(tela.height() * 0.9)))
        self.setMinimumWidth(640)
        self.setAcceptDrops(True)
        self.visao = Visao()
        self.doc: Documento | None = None
        self.arquivo: Path | None = None
        self.pos = 0.0
        self.marca_in: float | None = None
        self.marca_out: float | None = None
        self.linhas: list[LinhaFaixa] = []
        self.faixas_mpv: list[dict] = []
        self._start_pendente = False
        self._sig_onda = _SinalOnda()
        self._sig_onda.pronto.connect(self._onda_pronta)
        self._sig_mpv = _SinaisMpv()
        self._sig_mpv.faixas.connect(self._faixas_mudaram)

        central = QWidget()
        self.setCentralWidget(central)
        raiz = QVBoxLayout(central)
        raiz.setContentsMargins(0, 0, 0, 0)
        raiz.setSpacing(0)

        # vídeo
        self.tela = TelaVideo(self)
        self.tela.setStyleSheet("background:black")
        self.tela.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.tela.setMinimumHeight(200)
        raiz.addWidget(self.tela, 1)

        # barra de controles
        barra = QWidget()
        barra.setObjectName("barra")
        bl = QHBoxLayout(barra)
        bl.setContentsMargins(10, 6, 10, 6)
        bl.setSpacing(2)

        def btn(nome, dica, fn, tam=20, papel="ctl", destino=bl):
            b = QToolButton()
            b.setIcon(icone(nome))
            b.setIconSize(QSize(tam, tam))
            b.setObjectName(papel)
            b.setToolTip(dica)
            b.setFocusPolicy(Qt.NoFocus)
            b.setAutoRaise(True)
            b.clicked.connect(fn)
            if destino is not None:
                destino.addWidget(b)
            return b

        def sep():
            f = QFrame()
            f.setObjectName("sep")
            f.setFixedSize(1, 22)
            bl.addSpacing(6)
            bl.addWidget(f)
            bl.addSpacing(6)

        btn("folder-open-outline", "Abrir (Ctrl+O)", self.dialogo_abrir)
        sep()
        btn("skip-previous", "Início (Home)", lambda: self.pular(0))
        btn("step-backward", "Quadro anterior (,)", lambda: self.quadros(-1))
        self.b_play = btn("play", "Tocar/Pausar (Espaço)", self.alternar, tam=26, papel="play")
        self._icone_play = None
        btn("step-forward", "Próximo quadro (.)", lambda: self.quadros(1))
        btn("skip-next", "Fim (End)", lambda: self.pular(self.visao.dur))
        sep()
        btn("ray-start-arrow", "Marcar entrada IN (I)", lambda: self.marcar(entrada=self.pos)).setIcon(icone("ray-start-arrow", "#ffc400"))
        btn("ray-end-arrow", "Marcar saída OUT (O)", lambda: self.marcar(saida=self.pos)).setIcon(icone("ray-end-arrow", "#ffc400"))
        sep()
        self.l_tempo = QLabel("--:--:--.--- / --:--:--.---")
        self.l_tempo.setObjectName("tempo")
        f = QFont("JetBrains Mono, DejaVu Sans Mono, monospace", 12)
        f.setStyleHint(QFont.Monospace)
        f.setWeight(QFont.DemiBold)
        self.l_tempo.setFont(f)
        self.l_tempo.setMinimumWidth(self.l_tempo.fontMetrics().horizontalAdvance("00:00:00.000 / 00:00:00.000") + 8)
        self.l_tempo.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bl.addWidget(self.l_tempo)
        self.l_quadro = QLabel("")
        self.l_quadro.setObjectName("sutil")
        bl.addWidget(self.l_quadro)
        bl.addSpacing(8)
        self.ir = QLineEdit()
        self.ir.setPlaceholderText("ir para 1:23.456")
        self.ir.addAction(icone("crosshairs-gps", "#8b949e"), QLineEdit.LeadingPosition)
        self.ir.setFixedWidth(150)
        self.ir.returnPressed.connect(self._ir_para)
        bl.addWidget(self.ir)
        bl.addStretch(1)
        self.vel = QComboBox()
        self.vel.addItems(["0.25x", "0.5x", "0.75x", "1x", "1.25x", "1.5x", "2x"])
        self.vel.setCurrentText("1x")
        self.vel.setFocusPolicy(Qt.NoFocus)
        self.vel.setToolTip("Velocidade")
        self.vel.currentTextChanged.connect(lambda s: self._set("speed", float(s[:-1])))
        bl.addWidget(self.vel)
        bl.addSpacing(8)
        self.b_vol = btn("volume-high", "Mudo geral (M)", self._alternar_mudo_geral, tam=18)
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 130)
        self.vol.setValue(100)
        self.vol.setFixedWidth(90)
        self.vol.setFocusPolicy(Qt.NoFocus)
        self.vol.setToolTip("Volume geral")
        self.vol.valueChanged.connect(lambda v: self._set("volume", v))
        bl.addWidget(self.vol)
        bl.addSpacing(4)
        btn("fullscreen", "Tela cheia (F)", self._tela_cheia, tam=18)
        raiz.addWidget(barra)

        # versão + desfazer/descartar/salvar ficam fixos no canto direito da barra de status
        self.l_versao = QLabel("")
        self.l_versao.setObjectName("versao")
        self.b_desfazer = btn("undo-variant", "Desfazer última edição (Ctrl+Z)", self.desfazer, 16, "ctl", None)
        self.b_descartar = btn("delete-outline", "Descartar todas as versões", self.descartar, 16, "ctl", None)
        self.b_salvar = btn("content-save-outline", "Salvar: a última versão substitui o original (Ctrl+S)",
                            self.salvar, 16, "salvar", None)
        self.b_salvar.setText(" Salvar")
        self.b_salvar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        for w in (self.l_versao, self.b_desfazer, self.b_descartar, self.b_salvar):
            self.statusBar().addPermanentWidget(w)

        # régua + faixas
        lr = QHBoxLayout()
        lr.setContentsMargins(0, 0, 0, 0)
        lr.setSpacing(0)
        canto = QLabel("  tempo")
        canto.setFixedWidth(CAB)
        canto.setStyleSheet("color:#8b949e")
        lr.addWidget(canto)
        self.regua = Regua(self)
        lr.addWidget(self.regua, 1)
        raiz.addLayout(lr)
        self.area_faixas = QWidget()
        self.lay_faixas = QVBoxLayout(self.area_faixas)
        self.lay_faixas.setContentsMargins(0, 1, 0, 0)
        self.lay_faixas.setSpacing(1)
        rolagem = QScrollArea()
        rolagem.setWidgetResizable(True)
        rolagem.setWidget(self.area_faixas)
        rolagem.setFrameShape(QScrollArea.NoFrame)
        rolagem.setMinimumHeight(66)
        self.rolagem = rolagem
        raiz.addWidget(rolagem)
        lsb = QHBoxLayout()
        lsb.setContentsMargins(CAB, 0, 0, 0)
        self.barra_h = QScrollBar(Qt.Horizontal)
        self.barra_h.valueChanged.connect(lambda v: self.visao.mover(v / 1000))
        lsb.addWidget(self.barra_h)
        raiz.addLayout(lsb)
        self.visao.mudou.connect(self._sinc_barra)

        self.statusBar().showMessage("Arraste um arquivo para cá ou use Ctrl+O · roda no vídeo/régua = zoom · arrastar = mover · botão direito = IN/OUT")

        # mpv
        import mpv  # import tardio: precisa do locale numérico C
        global _mpv
        _mpv = mpv
        self.mpv = mpv.MPV(wid=str(int(self.tela.winId())), vo="gpu", hwdec="auto-safe",
                           keep_open="always", hr_seek="yes", idle="yes", osc=False,
                           input_default_bindings=False, input_vo_keyboard=False,
                           input_cursor=False, cursor_autohide=False,
                           loglevel="warn",
                           log_handler=lambda nv, comp, msg: print(f"[mpv/{comp}] {nv}: {msg.rstrip()}", file=sys.stderr, flush=True))
        self.mpv.observe_property("track-list", lambda *_: self._sig_mpv.faixas.emit())

        # atalhos
        for tecla, fn in [
            ("Space", self.alternar), ("Left", lambda: self.pular(self.pos - 1)),
            ("Right", lambda: self.pular(self.pos + 1)), ("Shift+Left", lambda: self.pular(self.pos - 10)),
            ("Shift+Right", lambda: self.pular(self.pos + 10)), (",", lambda: self.quadros(-1)),
            (".", lambda: self.quadros(1)), ("I", lambda: self.marcar(entrada=self.pos)),
            ("O", lambda: self.marcar(saida=self.pos)), ("Home", lambda: self.pular(0)),
            ("End", lambda: self.pular(self.visao.dur)), ("Ctrl+O", self.dialogo_abrir),
            ("Ctrl+S", self.salvar), ("Ctrl+Z", self.desfazer),
            ("+", lambda: self.visao.zoom(0.5, self.pos)), ("-", lambda: self.visao.zoom(2, self.pos)),
            ("0", lambda: self.visao.zoom(1e9, 0)), ("F", self._tela_cheia), ("M", self._alternar_mudo_geral),
        ]:
            QShortcut(QKeySequence(tecla), self, fn)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(33)

        # servidor de controle (para o MCP)
        self.servidor = QLocalServer(self)
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        QLocalServer.removeServer(str(SOCKET_PATH))
        self.servidor.listen(str(SOCKET_PATH))
        self.servidor.newConnection.connect(self._nova_conexao)
        self._atualizar_versao()

    # ------------------------------------------------------------ mpv helpers
    def _get(self, prop, padrao=None):
        try:
            v = self.mpv._get_property(prop, _mpv.lazy_decoder)
            return padrao if v is None else v
        except Exception:  # noqa: BLE001
            return padrao

    def _set(self, prop, valor):
        try:
            self.mpv._set_property(prop, valor)
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage(f"mpv: {prop}: {e}", 5000)

    @property
    def fps(self) -> float:
        return float(self._get("container-fps") or self._get("estimated-vf-fps") or 0) or 0.0

    # ------------------------------------------------------------ ações
    def abrir(self, caminho: str | Path):
        p = Path(caminho).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(p)
        self.doc = Documento(p)
        self.tela.resetar()
        self.marca_in = self.marca_out = None
        self._carregar(self.doc.atual(), pos=0.0)
        self._set("pause", False)
        self.setWindowTitle(f"{self.doc.original.name} — Claude Player")

    def _carregar(self, arquivo: Path, pos: float = 0.0):
        self.arquivo = arquivo
        self._set("lavfi-complex", "")
        self._set("start", f"{pos:.3f}" if pos > 0 else "none")
        self._start_pendente = pos > 0
        self.mpv.loadfile(str(arquivo))
        self.pos = pos
        self._atualizar_versao()

    def recarregar(self, mensagem: str | None = None):
        if not self.doc:
            return
        pos = self.pos
        self._carregar(self.doc.atual(), pos=pos)
        if mensagem:
            self.statusBar().showMessage(mensagem, 15000)

    def dialogo_abrir(self):
        f, _ = QFileDialog.getOpenFileName(self, "Abrir vídeo", str(Path.home()),
                                           "Mídia (*.mp4 *.mkv *.mov *.webm *.avi *.m4v *.ts *.mts *.flv *.wmv *.mp3 *.wav *.flac *.m4a *.ogg *.opus);;Todos (*)")
        if f:
            self.abrir(f)

    def alternar(self):
        self._set("pause", not self._get("pause", True))

    def pular(self, t: float, exato: bool = True):
        if not self.arquivo:
            return
        t = min(max(t, 0.0), self.visao.dur)
        self.pos = t
        try:
            self.mpv.command("seek", f"{t:.3f}", "absolute+exact" if exato else "absolute+keyframes")
        except Exception:  # noqa: BLE001
            pass
        self.visao.seguir(t)
        self._repintar()

    def quadros(self, n: int):
        if not self.arquivo:
            return
        if n == 1:
            self.mpv.command("frame-step")
        elif n == -1:
            self.mpv.command("frame-back-step")
        else:
            fps = self.fps or 30
            self.pular(self.pos + n / fps)

    def marcar(self, entrada=None, saida=None, limpar=False):
        if limpar:
            self.marca_in = self.marca_out = None
        if entrada is not None:
            self.marca_in = entrada
        if saida is not None:
            self.marca_out = saida
        if self.marca_in is not None and self.marca_out is not None and self.marca_in > self.marca_out:
            self.marca_in, self.marca_out = self.marca_out, self.marca_in
        self._repintar()

    def _ir_para(self):
        try:
            self.pular(parse_tempo(self.ir.text(), self.estado()))
            self.ir.clear()
            self.setFocus()
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage(f"tempo inválido: {e}", 4000)

    def _alternar_mudo_geral(self):
        mudo = not self._get("mute", False)
        self._set("mute", mudo)
        self.b_vol.setIcon(icone("volume-off" if mudo else "volume-high"))

    def _tela_cheia(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    # ------------------------------------------------------------ versões
    def _atualizar_versao(self):
        vs = self.doc.versoes() if self.doc else []
        if vs:
            self.l_versao.setText(f"<span style='color:#ffc400'>v{len(vs)} não salva</span>")
            self.l_versao.setToolTip("\n".join(f"v{i + 1}: {v['descricao']}" for i, v in enumerate(vs)))
        else:
            self.l_versao.setText("original" if self.doc else "")
            self.l_versao.setToolTip("")
        for b in (self.b_desfazer, self.b_descartar, self.b_salvar):
            b.setEnabled(bool(vs))

    def desfazer(self):
        if self.doc and self.doc.desfazer():
            self.recarregar("Desfeito.")

    def descartar(self):
        if not self.doc or not self.doc.versoes():
            return
        if QMessageBox.question(self, "Descartar", "Apagar todas as versões temporárias e voltar ao original?") == QMessageBox.Yes:
            self.doc.descartar()
            self.recarregar("Versões descartadas.")

    def salvar(self, confirmar: bool = True) -> Path | None:
        if not self.doc or not self.doc.versoes():
            return None
        n = len(self.doc.versoes())
        if confirmar and QMessageBox.question(
                self, "Salvar",
                f"A versão v{n} vai substituir o original.\n\n"
                f"O original vai para a Lixeira e as {n} versões temporárias serão apagadas.\nContinuar?"
        ) != QMessageBox.Yes:
            return None
        pos = self.pos
        novo = self.doc.salvar()
        self.doc = Documento(novo)
        self._carregar(novo, pos=pos)
        self.setWindowTitle(f"{novo.name} — Claude Player")
        self.statusBar().showMessage(f"Salvo em {novo}", 10000)
        return novo

    # ------------------------------------------------------------ áudio / faixas
    def _faixas_mudaram(self):
        tl = self._get("track-list", []) or []
        auds = [t for t in tl if t.get("type") == "audio"]
        chave = (str(self.arquivo), tuple(t.get("id") for t in auds))
        if chave == self._chave_faixas or not tl:
            return
        self._chave_faixas = chave
        self.faixas_mpv = auds
        for l in self.linhas:
            l.setParent(None)
            l.deleteLater()
        self.linhas = []
        if getattr(self, "_aviso_sem_audio", None):
            self._aviso_sem_audio.setParent(None)
            self._aviso_sem_audio = None
        if not auds:
            self._aviso_sem_audio = QLabel("   este arquivo não tem faixas de áudio")
            self._aviso_sem_audio.setStyleSheet("color:#8b949e")
            self.lay_faixas.addWidget(self._aviso_sem_audio)
        tem_video = any(t.get("type") == "video" for t in tl)
        self.tela.setVisible(tem_video)
        self.altura_faixa = 64 if tem_video else 150
        livre = not self.isMaximized() and not self.isFullScreen()
        if not tem_video and livre:
            self.resize(self.width(), 200 + max(len(auds[:4]), 1) * 151)
        elif tem_video and livre and self.height() < 600:
            tela = QApplication.primaryScreen().availableGeometry()
            self.resize(self.width(), min(760, int(tela.height() * 0.9)))
        for i, info in enumerate(auds):
            l = LinhaFaixa(self, i, info)
            self.lay_faixas.addWidget(l)
            self.linhas.append(l)
            QThreadPool.globalInstance().start(TarefaOnda(str(self.arquivo), i, self._sig_onda))
        alt = min(max(len(auds), 1), 4) * (self.altura_faixa + 1) + 2
        if tem_video:
            self.rolagem.setFixedHeight(alt)
        else:  # só áudio: as ondas ocupam o espaço do vídeo
            self.rolagem.setMinimumHeight(alt)
            self.rolagem.setMaximumHeight(16777215)
        self.centralWidget().layout().setStretchFactor(self.rolagem, 0 if tem_video else 1)
        self.atualizar_mix()

    _chave_faixas = None
    altura_faixa = 64

    def _onda_pronta(self, arquivo, faixa, dados):
        if self.arquivo and str(self.arquivo) == arquivo and faixa < len(self.linhas):
            self.linhas[faixa].onda.dados = dados
            self.linhas[faixa].onda.update()

    def faixa_audivel(self, i: int) -> bool:
        if i >= len(self.linhas):
            return True
        algum_solo = any(l.b_solo.isChecked() for l in self.linhas)
        l = self.linhas[i]
        if algum_solo:
            return l.b_solo.isChecked()
        return not l.b_mudo.isChecked()

    def _ganho(self, i):
        return (self.linhas[i].vol.value() / 100) if self.faixa_audivel(i) else 0.0

    def atualizar_mix(self):
        n = len(self.faixas_mpv)
        if n == 0:
            return
        if n == 1:
            self._set("lavfi-complex", "")
            self._set("aid", self.faixas_mpv[0]["id"])
            self._set("af", f"lavfi=[volume={self._ganho(0):.3f}]")
        else:
            tl = self._get("track-list", []) or []
            vid = next((t["id"] for t in tl if t.get("type") == "video" and not t.get("albumart")), None)
            partes = []
            if vid is not None:
                partes.append(f"[vid{vid}] null [vo]")
            for i, t in enumerate(self.faixas_mpv):
                partes.append(f"[aid{t['id']}] volume={self._ganho(i):.3f} [a{i}]")
            entradas = "".join(f"[a{i}]" for i in range(n))
            partes.append(f"{entradas} amix=inputs={n}:normalize=0:duration=longest [ao]")
            try:
                self._set("af", "")
                self.mpv._set_property("lavfi-complex", "; ".join(partes))
            except Exception as e:  # noqa: BLE001
                self.statusBar().showMessage(f"mixagem de faixas falhou ({e}); tocando só a primeira audível", 8000)
                ativo = next((i for i in range(n) if self.faixa_audivel(i)), 0)
                self._set("lavfi-complex", "")
                self._set("aid", self.faixas_mpv[ativo]["id"])
        for l in self.linhas:
            l.onda.update()

    # ------------------------------------------------------------ atualização periódica
    def _tick(self):
        pausado = self._get("pause", True)
        if pausado != self._icone_play:
            self._icone_play = pausado
            self.b_play.setIcon(icone("play" if pausado else "pause", "#ffffff"))
        dur = float(self._get("duration", 0) or 0)
        if dur and abs(dur - self.visao.dur) > 0.0005:
            self.visao.definir_duracao(dur)
        if self._start_pendente and self._get("time-pos") is not None:
            self._start_pendente = False
            self._set("start", "none")
        t = self._get("time-pos")
        if t is not None:
            self.pos = float(t)
            if not pausado:
                self.visao.seguir(self.pos)
        self.l_tempo.setText(f"{fmt_tempo(self.pos)} / {fmt_tempo(self.visao.dur)}")
        fps = self.fps
        self.l_quadro.setText(f"q{int(round(self.pos * fps))}" if fps else "")
        self.l_quadro.setToolTip(f"quadro {int(round(self.pos * fps))} @ {fps:.3f} fps" if fps else "")
        self._repintar()

    def _repintar(self):
        self.regua.update()
        for l in self.linhas:
            l.onda.update()

    def _sinc_barra(self):
        v = self.visao
        self.barra_h.blockSignals(True)
        self.barra_h.setRange(0, int(max(v.dur - v.span, 0) * 1000))
        self.barra_h.setPageStep(int(v.span * 1000))
        self.barra_h.setSingleStep(max(int(v.span * 100), 1))
        self.barra_h.setValue(int(v.ini * 1000))
        self.barra_h.blockSignals(False)

    # ------------------------------------------------------------ estado
    def estado(self) -> dict:
        faixas = []
        for i, t in enumerate(self.faixas_mpv):
            l = self.linhas[i] if i < len(self.linhas) else None
            faixas.append({
                "faixa": i + 1, "idioma": t.get("lang"), "titulo": t.get("title"),
                "codec": t.get("codec"), "canais": t.get("demux-channel-count"),
                "audivel": self.faixa_audivel(i), "mudo": l.b_mudo.isChecked() if l else False,
                "solo": l.b_solo.isChecked() if l else False, "volume_pct": l.vol.value() if l else 100,
            })
        return {
            "documento": self.doc.resumo() if self.doc else None,
            "arquivo_tocando": str(self.arquivo) if self.arquivo else None,
            "posicao": round(self.pos, 3), "posicao_fmt": fmt_tempo(self.pos),
            "duracao": round(self.visao.dur, 3), "duracao_fmt": fmt_tempo(self.visao.dur),
            "pausado": bool(self._get("pause", True)), "velocidade": self._get("speed", 1.0),
            "fps": self.fps, "quadro": int(round(self.pos * self.fps)) if self.fps else None,
            "marca_in": self.marca_in, "marca_in_fmt": fmt_tempo(self.marca_in) if self.marca_in is not None else None,
            "marca_out": self.marca_out, "marca_out_fmt": fmt_tempo(self.marca_out) if self.marca_out is not None else None,
            "visao": [fmt_tempo(self.visao.ini), fmt_tempo(self.visao.fim)],
            "faixas_audio": faixas,
        }

    # ------------------------------------------------------------ socket de controle
    def _nova_conexao(self):
        c = self.servidor.nextPendingConnection()
        c.setProperty("buf", b"")
        c.readyRead.connect(lambda c=c: self._ler(c))
        c.disconnected.connect(c.deleteLater)

    def _ler(self, c):
        buf = bytes(c.property("buf") or b"") + bytes(c.readAll())
        while b"\n" in buf:
            linha, buf = buf.split(b"\n", 1)
            try:
                req = json.loads(linha)
                dados = self._executar(req.pop("cmd"), **req)
                resp = {"ok": True, "dados": dados if dados is not None else {}}
            except Exception as e:  # noqa: BLE001
                resp = {"ok": False, "erro": f"{type(e).__name__}: {e}"}
            c.write((json.dumps(resp, ensure_ascii=False, default=str) + "\n").encode())
            c.flush()
        c.setProperty("buf", buf)

    def _executar(self, cmd: str, **a):
        if cmd == "ping":
            return {"pong": True}
        if cmd == "estado":
            return self.estado()
        if cmd == "depurar":
            props = ["path", "idle-active", "core-idle", "eof-reached", "duration", "time-pos",
                     "track-list", "vo-configured", "current-vo", "hwdec-current", "lavfi-complex",
                     "aid", "vid", "af", "start", "pause", "mpv-version", "ffmpeg-version"]
            return {p: self._get(p, "<erro/None>") for p in props + a.get("extras", [])}
        if cmd == "abrir":
            self.abrir(a["arquivo"])
            self._mostrar()
            return {"aberto": str(self.doc.original), "tocando": str(self.arquivo)}
        if cmd == "sair":
            QTimer.singleShot(100, self.close)
            return {"saindo": True}
        if cmd == "mostrar":
            self._mostrar()
            return {}
        if cmd == "tocar":
            self._set("pause", False)
        elif cmd == "pausar":
            self._set("pause", True)
        elif cmd == "alternar":
            self.alternar()
        elif cmd == "pular":
            self.pular(float(a["t"]), exato=True)
        elif cmd == "quadros":
            self.quadros(int(a.get("n", 1)))
        elif cmd == "marcar":
            self.marcar(entrada=a.get("entrada"), saida=a.get("saida"), limpar=a.get("limpar", False))
        elif cmd == "velocidade":
            self._set("speed", float(a["v"]))
        elif cmd == "zoom":
            ini, fim = float(a["ini"]), float(a["fim"])
            self.visao._por(ini, max(fim - ini, 0.05))
        elif cmd == "zoom_video":
            self.tela.definir(float(a.get("zoom", 0)), float(a.get("x", 0)), float(a.get("y", 0)))
        elif cmd == "mix":
            i = int(a["faixa"]) - 1
            l = self.linhas[i]
            if "mudo" in a:
                l.b_mudo.setChecked(bool(a["mudo"]))
            if "solo" in a:
                l.b_solo.setChecked(bool(a["solo"]))
            if "volume" in a:
                l.vol.setValue(int(a["volume"]))
        elif cmd == "recarregar":
            if a.get("arquivo"):
                self.doc = Documento(a["arquivo"])
                self.setWindowTitle(f"{self.doc.original.name} — Claude Player")
            self.recarregar(a.get("mensagem"))
        elif cmd == "mensagem":
            self.statusBar().showMessage(a.get("texto", ""), 15000)
        else:
            raise ValueError(f"comando desconhecido: {cmd}")
        return self.estado()

    def _mostrar(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------ arrastar e soltar
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = e.mimeData().urls()
        if urls:
            self.abrir(urls[0].toLocalFile())

    def closeEvent(self, e):
        try:
            self.mpv.terminate()
        except Exception:  # noqa: BLE001
            pass
        self.servidor.close()
        QLocalServer.removeServer(str(SOCKET_PATH))
        super().closeEvent(e)


QSS = """
QMainWindow, QWidget { font-size: 10pt; }
QWidget#barra { background: #1f232b; border-top: 1px solid #2b313b; border-bottom: 1px solid #2b313b; }
QFrame#sep { background: #2f3540; }
QToolButton { border: none; border-radius: 6px; padding: 5px; background: transparent; }
QToolButton:hover { background: #2c323d; }
QToolButton:pressed { background: #353c48; }
QToolButton:disabled { background: transparent; }
QToolButton#play { background: #2e6fbf; border-radius: 19px; padding: 6px; margin: 0 4px; }
QToolButton#play:hover { background: #3a80d6; }
QToolButton#play:pressed { background: #255ea6; }
QToolButton#mudo:checked { background: #b33a3a; }
QToolButton#solo:checked { background: #c9a227; }
QToolButton#salvar { background: #2e6fbf; color: white; padding: 4px 12px 4px 8px; font-weight: 600; }
QToolButton#salvar:hover { background: #3a80d6; }
QToolButton#salvar:disabled { background: #262b34; color: #4a4f5a; }
QLabel#tempo { color: #e6edf3; padding: 0 4px; }
QLabel#sutil { color: #6e7681; }
QLabel#versao { color: #8b949e; padding-right: 6px; }
QLineEdit { background: #15171c; border: 1px solid #2f3540; border-radius: 6px; padding: 4px 6px; color: #c9d1d9; }
QLineEdit:focus { border-color: #4fa3ff; }
QComboBox { background: #15171c; border: 1px solid #2f3540; border-radius: 6px; padding: 3px 8px; min-width: 56px; }
QComboBox:hover { border-color: #4a5260; }
QComboBox::drop-down { border: none; width: 18px; }
QComboBox::down-arrow { image: url(@SETA@); width: 14px; height: 14px; }
QComboBox QAbstractItemView { background: #1f232b; border: 1px solid #2f3540; selection-background-color: #2e6fbf; }
QSlider::groove:horizontal { height: 4px; background: #2f3540; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #4fa3ff; border-radius: 2px; }
QSlider::handle:horizontal { background: #e6edf3; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; }
QSlider::handle:horizontal:hover { background: #ffffff; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }
QScrollBar:horizontal { height: 10px; background: #15171c; margin: 0; }
QScrollBar::handle:horizontal { background: #343b47; border-radius: 4px; min-width: 30px; margin: 2px; }
QScrollBar::handle:horizontal:hover { background: #4a5260; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; }
QScrollBar:vertical { width: 10px; background: #15171c; }
QScrollBar::handle:vertical { background: #343b47; border-radius: 4px; margin: 2px; }
QStatusBar { background: #15171c; color: #8b949e; border-top: 1px solid #2b313b; }
QStatusBar::item { border: none; }
QToolTip { background: #262b34; color: #e6edf3; border: 1px solid #3a414d; padding: 4px; }
QMenu { background: #1f232b; border: 1px solid #2f3540; padding: 4px; }
QMenu::item { padding: 5px 18px; border-radius: 4px; }
QMenu::item:selected { background: #2e6fbf; }
"""


def _tema(app: QApplication):
    app.setStyle("Fusion")
    pal = QPalette()
    for papel, cor in [(QPalette.Window, "#1b1e24"), (QPalette.WindowText, "#c9d1d9"),
                       (QPalette.Base, "#15171c"), (QPalette.AlternateBase, "#1f232b"),
                       (QPalette.Text, "#c9d1d9"), (QPalette.Button, "#262b34"),
                       (QPalette.ButtonText, "#c9d1d9"), (QPalette.Highlight, "#2e6fbf"),
                       (QPalette.HighlightedText, "#ffffff"), (QPalette.ToolTipBase, "#262b34"),
                       (QPalette.ToolTipText, "#c9d1d9")]:
        pal.setColor(papel, QColor(cor))
    app.setPalette(pal)
    seta = CACHE_DIR / "ui" / "seta.png"
    if not seta.exists():
        seta.parent.mkdir(parents=True, exist_ok=True)
        icone("chevron-down", "#8b949e").pixmap(28, 28).save(str(seta))
    app.setStyleSheet(QSS.replace("@SETA@", seta.as_posix()))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    # Já existe uma janela? Encaminha o arquivo para ela.
    if cliente.vivo():
        if args:
            cliente.enviar("abrir", arquivo=str(Path(args[0]).resolve()))
        else:
            cliente.enviar("mostrar")
        return
    app = QApplication(sys.argv)
    app.setApplicationName("Claude Player")
    app.setDesktopFileName("claude-player")
    locale.setlocale(locale.LC_NUMERIC, "C")
    _tema(app)
    j = Janela()
    j.show()
    if args:
        QTimer.singleShot(0, lambda: j.abrir(args[0]))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
