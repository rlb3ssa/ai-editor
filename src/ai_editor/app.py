"""Janela do AI Editor: vídeo (libmpv) + régua em ms + uma onda por faixa de áudio."""
from __future__ import annotations

import json
import locale
import os
import sys
from pathlib import Path

# Em X11 o Qt usa EGL (e não GLX): é o que permite ao mpv decodificar por hardware (VAAPI) sem cópia.
# Em Wayland o Qt roda nativo (o vídeo vai pela render API, não depende mais de X11).
os.environ.setdefault("QT_XCB_GL_INTEGRATION", "xcb_egl")
os.environ.setdefault("QT_API", "pyside6")

import numpy as np
from PySide6.QtCore import (QEvent, QFileSystemWatcher, QObject, QPointF, QRectF, QRunnable, QSize, Qt, QThreadPool, QTimer,
                            Signal)
from PySide6.QtGui import (QAction, QGuiApplication, QOpenGLContext, QColor, QFont, QKeySequence, QPainter, QPalette, QPen,
                           QPolygonF, QShortcut)
from PySide6.QtNetwork import QLocalServer
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QMainWindow, QMenu, QMessageBox, QScrollArea,
                               QScrollBar, QSlider, QSizePolicy, QToolButton, QVBoxLayout,
                               QWidget)

import qtawesome as qta

from . import binarios, cliente
from . import exportacao as _exportacao
from . import info as _info
from .core import CACHE_DIR, DEV, FONTE_DIR, NOME, RUNTIME_DIR, SOCKET_PATH, TITULO, Documento, fmt_tempo, parse_tempo
from .ondas import PICOS_POR_SEG, picos

CAB = 196  # largura do cabeçalho à esquerda das faixas
COR_FUNDO = QColor("#15171c")
COR_TRILHA = QColor("#1f232b")
COR_ONDA = QColor("#4fa3ff")
COR_ONDA_MUDA = QColor("#4a4f5a")
COR_CABECA = QColor("#ff4757")
# cores dos trechos entre marcas consecutivas (1→2, 2→3, ...), em ciclo
CORES_TRECHOS = ["#ffc400", "#22d3ee", "#f472b6", "#4ade80", "#fb923c", "#a78bfa"]
NOMES_CORES = ["amarelo", "ciano", "rosa", "verde", "laranja", "violeta"]


def cor_trecho(i: int, alfa: int = 255) -> QColor:
    c = QColor(CORES_TRECHOS[i % len(CORES_TRECHOS)])
    c.setAlpha(alfa)
    return c
COR_PROGRESSO = QColor("#2e6fbf")
COR_TEXTO = QColor("#c9d1d9")
ICONE = "#c9d1d9"
ICONE_ATIVO = "#ffffff"
DESTAQUE = "#4fa3ff"


# Estilo de ícones: "ph" (Phosphor) ou "mdi6" (Material Design). Os nomes no código são os do MDI;
# para Phosphor eles são traduzidos pela tabela abaixo.
ESTILO_ICONES = os.environ.get("AI_EDITOR_ICONES", "ph")
_PHOSPHOR = {
    "folder-open-outline": "folder-open",
    "skip-previous": "skip-back-fill",
    "skip-next": "skip-forward-fill",
    "step-backward": "caret-left-fill",
    "step-forward": "caret-right-fill",
    "play": "play-fill",
    "pause": "pause-fill",
    "arrow-collapse-up": "arrow-line-up",
    "crosshairs-gps": "crosshair-simple",
    "volume-high": "speaker-high",
    "volume-off": "speaker-x",
    "headphones": "headphones",
    "fullscreen": "corners-out",
    "undo-variant": "arrow-counter-clockwise",
    "delete-outline": "trash",
    "content-save-outline": "floppy-disk",
    "chevron-down": "caret-down",
    "information-outline": "info",
    "content-copy": "copy",
    "check": "check",
    "export": "export",
    "close": "x",
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
            m.addAction(icone("arrow-collapse-up", CORES_TRECHOS[0]), f"Adicionar marca em {fmt_tempo(t)}",
                        lambda: self.j.marcar(adicionar=[t]))
            if self.j.marcas:
                # marca mais próxima do clique
                i = min(range(len(self.j.marcas)), key=lambda k: abs(self.j.marcas[k] - t))
                m.addAction(f"Remover marca {i + 1} ({fmt_tempo(self.j.marcas[i])})",
                            lambda: self.j.marcar(remover=[i + 1]))
                m.addAction("Limpar todas as marcas", lambda: self.j.marcar(limpar=True))
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
        ms = j.marcas
        # trechos entre marcas consecutivas, cada um com uma cor
        for i in range(len(ms) - 1):
            xa, xb = self.v.t_para_x(ms[i], w), self.v.t_para_x(ms[i + 1], w)
            p.fillRect(QRectF(xa, 0, xb - xa, h), cor_trecho(i, 48))
        rotulos = isinstance(self, Regua)
        if rotulos:
            p.setFont(QFont("DejaVu Sans", 7, QFont.Bold))
        for i, t in enumerate(ms):
            x = self.v.t_para_x(t, w)
            cor = cor_trecho(min(i, max(len(ms) - 2, 0)))
            p.setPen(QPen(cor, 1))
            p.drawLine(QPointF(x, 0), QPointF(x, h))
            if rotulos:
                r = QRectF(x - 8, h - 13, 16, 12)
                p.setBrush(cor)
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(r, 3, 3)
                p.setPen(QColor("#0d1117"))
                p.drawText(r, Qt.AlignCenter, str(i + 1))
                p.setBrush(Qt.NoBrush)
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
        # [ A1 ] | [ Codec/Channels ]
        #        | [ mudo solo volume ]
        hl = QHBoxLayout(cab)
        hl.setContentsMargins(0, 0, 6, 0)  # sem margem vertical: o divisor vai de ponta a ponta
        hl.setSpacing(0)
        nome = QLabel(f"A{idx + 1}")
        nome.setObjectName("nome_faixa")
        nome.setAlignment(Qt.AlignCenter)
        nome.setFixedWidth(36)
        nome.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        nome.setToolTip(info.get("title") or f"Faixa de áudio {idx + 1}")
        hl.addWidget(nome)
        divisor = QFrame()
        divisor.setObjectName("divisor_faixa")
        divisor.setFixedWidth(1)
        hl.addWidget(divisor)
        hl.addSpacing(6)
        cl = QVBoxLayout()
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(1)
        hl.addLayout(cl, 1)
        cl.addStretch(1)  # stretch acima e abaixo: o bloco fica centralizado na vertical
        # linha 1: detalhes centralizados ("Codec: aac    Channels: 2")
        campos = [("Codec", info.get("codec")), ("Channels", info.get("demux-channel-count")),
                  ("Language", info.get("lang"))]
        det = "&nbsp;&nbsp;&nbsp;".join(
            f"<span style='color:#6e7681'>{k}:</span> <span style='color:#c9d1d9'>{v}</span>"
            for k, v in campos if v not in (None, ""))
        rot = QLabel(det)
        rot.setObjectName("detalhe_faixa")
        rot.setAlignment(Qt.AlignCenter)
        if info.get("title"):
            rot.setToolTip(info["title"])
        cl.addWidget(rot)
        # linha 2: mudo, solo e volume
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
        cl.addStretch(1)
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
def _ponteiro_display() -> dict:
    """Display nativo (X11 ou Wayland) do Qt, para o mpv usar decodificação por hardware com interop."""
    try:
        ni = QGuiApplication.instance().nativeInterface()
        nome = type(ni).__name__
        if nome == "QX11Application" and ni.display():
            return {"x11_display": int(ni.display())}
        if nome == "QWaylandApplication" and ni.display():
            return {"wl_display": int(ni.display())}
    except Exception as e:  # noqa: BLE001
        print(f"[video] sem display nativo: {e}", file=sys.stderr)
    return {}


class TelaVideo(QOpenGLWidget):
    """Área onde o mpv desenha via render API (OpenGL): funciona em X11 e Wayland.
    Roda = zoom no ponto do cursor; arrastar = mover a imagem; duplo clique = voltar ao tamanho normal."""

    _novo_quadro = Signal()

    ZOOM_MAX = 4.0  # log2 → 16x

    def __init__(self, janela):
        super().__init__()
        self.janela = janela
        self.zoom = 0.0          # log2 da escala (video-zoom do mpv)
        self.pan = [0.0, 0.0]    # fração do tamanho do vídeo escalado (video-pan-x/y)
        self._arrasto = None
        self.ctx = None
        self._novo_quadro.connect(self.update, Qt.QueuedConnection)
        # contador de quadros fixo no canto superior direito (não acompanha o zoom)
        self.l_quadro = QLabel("", self)
        self.l_quadro.setObjectName("quadro")
        f = QFont("JetBrains Mono, DejaVu Sans Mono, monospace", 10)
        f.setStyleHint(QFont.Monospace)
        self.l_quadro.setFont(f)
        self.l_quadro.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.l_quadro.hide()

    def mostrar_quadro(self, n: int | None, fps: float = 0.0):
        if n is None:
            self.l_quadro.hide()
            return
        self.l_quadro.setText(f"Frame: {n}")
        self.l_quadro.adjustSize()
        self._posicionar_quadro()
        self.l_quadro.show()

    def _posicionar_quadro(self):
        self.l_quadro.move(self.width() - self.l_quadro.width() - 10, 10)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._posicionar_quadro()

    # ---- render API do mpv
    def initializeGL(self):
        import mpv as _m

        def gpa(_, nome):
            c = QOpenGLContext.currentContext()
            return int(c.getProcAddress(nome.decode())) if c else 0

        self._gpa = _m.MpvGlGetProcAddressFn(gpa)  # manter referência (callback C)
        self.ctx = _m.MpvRenderContext(self.janela.mpv, "opengl",
                                       opengl_init_params={"get_proc_address": self._gpa},
                                       **_ponteiro_display())
        # chamado numa thread do mpv: só agenda a repintura na thread da interface
        self.ctx.update_cb = self._novo_quadro.emit

    def paintGL(self):
        if not self.ctx:
            return
        r = self.devicePixelRatioF()
        fbo = self.defaultFramebufferObject()
        self.ctx.render(flip_y=True, opengl_fbo={"w": round(self.width() * r), "h": round(self.height() * r),
                                                 "fbo": fbo})
        # O mpv termina com o framebuffer 0 ligado. Em seguida o Qt descarta depth/stencil do FBO do widget
        # (glInvalidateFramebuffer com GL_DEPTH_ATTACHMENT), o que é inválido no framebuffer 0 →
        # INVALID_ENUM, que o mpv lia depois como "after creating texture". Religar o FBO do widget resolve.
        self.context().functions().glBindFramebuffer(0x8D40, fbo)  # GL_FRAMEBUFFER

    def liberar(self):
        """Precisa ser chamado antes de mpv.terminate()."""
        if self.ctx:
            self.makeCurrent()
            self.ctx.update_cb = None
            self.ctx.free()
            self.ctx = None
            self.doneCurrent()

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
        self.setWindowTitle(TITULO)
        tela = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1100, int(tela.width() * 0.9)), min(760, int(tela.height() * 0.9)))
        self.setMinimumWidth(640)
        self.setAcceptDrops(True)
        self.visao = Visao()
        self.doc: Documento | None = None
        self.arquivo: Path | None = None
        self.pos = 0.0
        self.marcas: list[float] = []  # pontos marcados (s), sempre em ordem
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
        self.tela.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.tela.setMinimumHeight(200)
        raiz.addWidget(self.tela, 1)

        # barra de controles
        barra = QWidget()
        barra.setObjectName("barra")
        lb = QHBoxLayout(barra)
        lb.setContentsMargins(10, 6, 10, 6)
        lb.setSpacing(0)
        alvo = [lb]  # layout onde btn() coloca os botões por padrão

        def btn(nome, dica, fn, tam=20, papel="ctl", destino="atual"):
            b = QToolButton()
            b.setIcon(icone(nome))
            b.setIconSize(QSize(tam, tam))
            b.setObjectName(papel)
            b.setToolTip(dica)
            b.setFocusPolicy(Qt.NoFocus)
            b.setAutoRaise(True)
            b.clicked.connect(fn)
            if destino == "atual":
                destino = alvo[0]
            if destino is not None:
                destino.addWidget(b)
            return b

        def sep(destino):
            f = QFrame()
            f.setObjectName("sep")
            f.setFixedSize(1, 22)
            destino.addSpacing(6)
            destino.addWidget(f)
            destino.addSpacing(6)

        def grupo():
            w = QWidget()
            lay = QHBoxLayout(w)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(2)
            return w, lay

        # três grupos: esquerda (pasta) | centro (reprodução + marca) | direita (velocidade, volume, tela cheia).
        # Esquerda e direita ganham a mesma largura mínima para o centro ficar realmente centralizado.
        g_esq, esq = grupo()
        g_cen, cen = grupo()
        g_dir, dir_ = grupo()
        btn("folder-open-outline", "Abrir (Ctrl+O)", self.dialogo_abrir, destino=esq)
        btn("export", "Exportar / converter: WhatsApp, GIF, MP3, foto do quadro... (Ctrl+E)",
            self.mostrar_exportar, destino=esq)
        # botão de metadados sobre o vídeo, no canto superior esquerdo (fixo, como o contador de quadros)
        self.b_info = btn("information-outline", "Metadados do arquivo (Ctrl+I)", self.mostrar_info,
                          tam=18, papel="info_video", destino=None)
        self.b_info.setParent(self.tela)
        self.b_info.setAutoRaise(False)
        self.b_info.setCursor(Qt.PointingHandCursor)
        self.b_info.move(10, 10)
        self.b_info.show()
        esq.addStretch(1)
        bl = alvo[0] = cen
        btn("skip-previous", "Início (Home)", lambda: self.pular(0))
        btn("step-backward", "Quadro anterior (,)", lambda: self.quadros(-1))
        self.b_play = btn("play", "Tocar/Pausar (Espaço)", self.alternar, tam=26, papel="play")
        self._icone_play = None
        btn("step-forward", "Próximo quadro (.)", lambda: self.quadros(1))
        btn("skip-next", "Fim (End)", lambda: self.pular(self.visao.dur))
        sep(cen)
        btn("arrow-collapse-up", "Marcar ponto (I) · botão direito na régua remove", lambda: self.marcar(adicionar=[self.pos])).setIcon(
            icone("arrow-collapse-up", CORES_TRECHOS[0]))
        bl = alvo[0] = dir_
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
        lado = max(g_esq.sizeHint().width(), g_dir.sizeHint().width())
        g_esq.setMinimumWidth(lado)
        g_dir.setMinimumWidth(lado)
        lb.addWidget(g_esq)
        lb.addStretch(1)
        lb.addWidget(g_cen)
        lb.addStretch(1)
        lb.addWidget(g_dir)
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
        # canto à esquerda da régua: tempo atual centralizado; atual / total na dica.
        # Duplo clique (ou Ctrl+G) transforma o tempo num campo para ir a outro instante.
        canto = QWidget()
        canto.setObjectName("canto")
        canto.setFixedWidth(CAB)
        canto.setAttribute(Qt.WA_StyledBackground)
        lc = QHBoxLayout(canto)
        lc.setContentsMargins(4, 0, 4, 0)
        lc.setSpacing(6)
        mono = QFont("JetBrains Mono, DejaVu Sans Mono, monospace")
        mono.setStyleHint(QFont.Monospace)
        self.l_tempo = QLabel("--:--:--.---")
        self.l_tempo.setObjectName("tempo")
        f = QFont(mono)
        f.setPointSize(12)
        f.setWeight(QFont.DemiBold)
        self.l_tempo.setFont(f)
        self.l_tempo.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.l_tempo.setAlignment(Qt.AlignCenter)
        self.l_tempo.installEventFilter(self)
        canto.installEventFilter(self)
        self.ir = QLineEdit()
        self.ir.setObjectName("ir")
        self.ir.setFont(f)
        self.ir.setAlignment(Qt.AlignCenter)
        self.ir.setToolTip("Digite o tempo e Enter: 83.456 · 1:23.456 · 1500ms · in · out · atual-2 (Esc cancela)")
        self.ir.returnPressed.connect(self._ir_para)
        self.ir.installEventFilter(self)
        self.ir.hide()
        lc.addWidget(self.l_tempo, 1)
        lc.addWidget(self.ir, 1)
        self.canto = canto
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

        self.statusBar().showMessage("Arraste um arquivo para cá ou use Ctrl+O · roda no vídeo/régua = zoom · arrastar = mover · I = marcar ponto · botão direito na régua = marcas")

        # mpv
        binarios.preparar_libmpv()  # usa a libmpv do pacote, se houver
        import mpv  # import tardio: precisa do locale numérico C
        global _mpv
        _mpv = mpv
        self.mpv = mpv.MPV(vo="libmpv", hwdec="auto-safe",
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
            (".", lambda: self.quadros(1)), ("I", lambda: self.marcar(adicionar=[self.pos])), ("Home", lambda: self.pular(0)),
            ("End", lambda: self.pular(self.visao.dur)), ("Ctrl+O", self.dialogo_abrir),
            ("Ctrl+S", self.salvar), ("Ctrl+Z", self.desfazer),
            ("+", lambda: self.visao.zoom(0.5, self.pos)), ("-", lambda: self.visao.zoom(2, self.pos)),
            ("0", lambda: self.visao.zoom(1e9, 0)), ("F", self._tela_cheia), ("M", self._alternar_mudo_geral), ("Ctrl+G", self.editar_tempo), ("Ctrl+I", self.mostrar_info),
            ("Ctrl+E", self.mostrar_exportar),
        ]:
            QShortcut(QKeySequence(tecla), self, fn)

        self.painel_info = _info.PainelMetadados(self, icone)
        self.painel_exportar = _exportacao.PainelExportar(self, icone)

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
        if DEV:
            self._iniciar_recarga_auto()

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
        self.marcas = []
        self._carregar(self.doc.atual(), pos=0.0)
        self._set("pause", False)
        self.setWindowTitle(f"{self.doc.original.name} — {TITULO}")

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
        pausado = self._get("pause", True)
        # no fim o mpv para no início do último quadro (não na duração): tolerância de 1,5 quadro
        margem = max(0.05, 1.5 / self.fps) if self.fps else 0.25
        if pausado and self.arquivo and (self._get("eof-reached", False) or self.pos >= self.visao.dur - margem):
            self.pular(0)  # acabou: play recomeça do início
        self._set("pause", not pausado)

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

    def marcar(self, adicionar=None, remover=None, limpar=False):
        """adicionar: tempos (s); remover: números das marcas (1 = a mais cedo); limpar: tira todas."""
        if limpar:
            self.marcas = []
        if remover:
            fora = {int(n) - 1 for n in remover}
            self.marcas = [t for i, t in enumerate(self.marcas) if i not in fora]
        for t in adicionar or []:
            t = round(max(0.0, min(float(t), self.visao.dur or float(t))), 3)
            if all(abs(t - m) > 0.0005 for m in self.marcas):
                self.marcas.append(t)
        self.marcas.sort()
        self._repintar()
        return self.info_marcas()

    def info_marcas(self) -> dict:
        ms = self.marcas
        return {
            "marcas": [{"n": i + 1, "tempo": t, "tempo_fmt": fmt_tempo(t)} for i, t in enumerate(ms)],
            "trechos_marcados": [
                {"de": f"m{i + 1}", "ate": f"m{i + 2}", "inicio_fmt": fmt_tempo(ms[i]), "fim_fmt": fmt_tempo(ms[i + 1]),
                 "duracao": round(ms[i + 1] - ms[i], 3), "cor": NOMES_CORES[i % len(NOMES_CORES)]}
                for i in range(len(ms) - 1)
            ],
        }

    def mostrar_info(self):
        if not self.doc:
            self.statusBar().showMessage("abra um arquivo para ver os metadados", 3000)
            return
        if self.painel_info.isVisible():
            self.painel_info.fechar()
        else:
            self.painel_info.abrir(self.doc.atual())

    def mostrar_exportar(self):
        if self.painel_exportar.isVisible():
            self.painel_exportar.fechar()
        else:
            self.painel_exportar.abrir()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        for painel in (getattr(self, "painel_info", None), getattr(self, "painel_exportar", None)):
            if painel is not None and painel.isVisible():
                painel.setGeometry(self.rect())

    def editar_tempo(self):
        """Troca o tempo do canto da régua por um campo editável já preenchido e selecionado."""
        self.ir.setText(fmt_tempo(self.pos))
        self.l_tempo.hide()
        self.ir.show()
        self.ir.setFocus()
        self.ir.selectAll()

    def _fechar_edicao_tempo(self):
        if self.ir.isVisible():
            self.ir.hide()
            self.l_tempo.show()
            self.setFocus()

    def _ir_para(self):
        try:
            self.pular(parse_tempo(self.ir.text(), self.estado()))
            self._fechar_edicao_tempo()
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage(f"tempo inválido: {e}", 4000)
            self.ir.selectAll()

    def eventFilter(self, obj, ev):
        t = ev.type()
        if obj in (self.l_tempo, getattr(self, "canto", None)) and t == QEvent.MouseButtonDblClick:
            self.editar_tempo()
            return True
        if obj is self.ir:
            if t == QEvent.KeyPress and ev.key() == Qt.Key_Escape:
                self._fechar_edicao_tempo()
                return True
            if t == QEvent.FocusOut:
                QTimer.singleShot(0, self._fechar_edicao_tempo)
        return super().eventFilter(obj, ev)

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
        self.setWindowTitle(f"{novo.name} — {TITULO}")
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
        self.altura_faixa = 50 if tem_video else 150
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
    altura_faixa = 50

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
        self.l_tempo.setText(fmt_tempo(self.pos))
        dica = f"{fmt_tempo(self.pos)} / {fmt_tempo(self.visao.dur)}"
        if self.l_tempo.toolTip() != dica:
            self.l_tempo.setToolTip(dica)
            self.canto.setToolTip(dica)
        fps = self.fps
        self.tela.mostrar_quadro(int(round(self.pos * fps)) if fps else None, fps)
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
            **self.info_marcas(),
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
            return self.marcar(adicionar=a.get("adicionar"), remover=a.get("remover"), limpar=a.get("limpar", False))
        elif cmd == "velocidade":
            self._set("speed", float(a["v"]))
        elif cmd == "zoom":
            ini, fim = float(a["ini"]), float(a["fim"])
            self.visao._por(ini, max(fim - ini, 0.05))
        elif cmd == "capturar":
            # print da janela renderizado pelo Qt (inclui o vídeo; funciona mesmo atrás de outras janelas)
            destino = a.get("arquivo") or str(RUNTIME_DIR / "captura.png")
            self.grab().save(destino)
            return {"arquivo": destino}
        elif cmd == "info":
            self.mostrar_info()
        elif cmd == "exportar":
            # painel de exportação. acao: abrir (padrão) | iniciar | cancelar | fechar | estado;
            # predefinicao/trecho (índice no combo)/limite_mb/destino ajustam os campos antes.
            pe = self.painel_exportar
            acao = a.get("acao", "abrir")
            if acao in ("abrir", "iniciar"):
                if not pe.isVisible():
                    pe.abrir()
                pe.configurar(a.get("predefinicao"), a.get("trecho"), a.get("limite_mb"), a.get("destino"))
                if acao == "iniciar":
                    pe.iniciar()
            elif acao == "cancelar":
                pe.cancelar()
            elif acao == "fechar":
                pe.fechar()
            elif acao != "estado":
                raise ValueError(f"acao desconhecida: {acao}")
            return pe.estado()
        elif cmd == "editar_tempo":
            # abre o campo de tempo do canto da régua; com "valor", digita e confirma (como Enter)
            self.editar_tempo()
            if "valor" in a:
                self.ir.setText(str(a["valor"]))
                self._ir_para()
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
                self.setWindowTitle(f"{self.doc.original.name} — {TITULO}")
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

    # ------------------------------------------------------------ modo dev: recarga automática
    def _iniciar_recarga_auto(self):
        """Ao salvar um .py da janela, reinicia o processo no mesmo arquivo/posição/zoom."""
        self._vigia = QFileSystemWatcher([str(FONTE_DIR)], self)
        self._mtimes = self._ler_mtimes()
        self._t_recarga = QTimer(self, singleShot=True, interval=400, timeout=self._talvez_recarregar)
        self._vigia.directoryChanged.connect(lambda _: self._t_recarga.start())
        self._vigia.fileChanged.connect(lambda _: self._t_recarga.start())
        self._vigia.addPaths([str(f) for f in FONTE_DIR.glob("*.py")])

    @staticmethod
    def _ler_mtimes():
        return {f.name: f.stat().st_mtime_ns for f in FONTE_DIR.glob("*.py")}

    def _talvez_recarregar(self):
        novos = self._ler_mtimes()
        mudou = {n for n in novos if novos[n] != self._mtimes.get(n)}
        self._mtimes = novos
        # editores que salvam por substituição tiram o arquivo da vigia: recoloca
        faltando = [str(f) for f in FONTE_DIR.glob("*.py") if str(f) not in self._vigia.files()]
        if faltando:
            self._vigia.addPaths(faltando)
        mudou -= {"mcp_server.py"}  # mudança só no MCP não afeta a janela
        if not mudou:
            return
        import py_compile
        for nome in sorted(mudou):
            try:
                py_compile.compile(str(FONTE_DIR / nome), doraise=True)
            except py_compile.PyCompileError as e:
                self.statusBar().showMessage(f"recarga cancelada: erro em {nome}: {e.msg.strip().splitlines()[-1]}", 15000)
                print(e.msg, file=sys.stderr, flush=True)
                return
        print(f"[dev] recarregando por mudança em: {', '.join(sorted(mudou))}", file=sys.stderr, flush=True)
        self.reiniciar_processo()

    def _estado_retomada(self) -> dict:
        g = self.geometry()
        return {
            "arquivo": str(self.doc.original) if self.doc else None,
            "pos": self.pos, "pausado": bool(self._get("pause", True)),
            "marcas": self.marcas,
            "zoom": self.tela.zoom, "pan": self.tela.pan,
            "geo": [g.x(), g.y(), g.width(), g.height()],
            "max": self.isMaximized(),
        }

    def reiniciar_processo(self):
        estado = self._estado_retomada()
        self.painel_exportar.encerrar()  # o execv não mata o ffmpeg de uma exportação em andamento
        try:
            self.tela.liberar()
            self.mpv.terminate()
        except Exception:  # noqa: BLE001
            pass
        self.servidor.close()
        QLocalServer.removeServer(str(SOCKET_PATH))
        os.environ["AI_EDITOR_RETOMAR"] = json.dumps(estado)
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable, "-m", "ai_editor.app"])

    def retomar(self, e: dict):
        if e.get("max"):
            self.showMaximized()
        elif e.get("geo"):
            self.setGeometry(*e["geo"])
        if not e.get("arquivo") or not Path(e["arquivo"]).exists():
            return
        self.doc = Documento(Path(e["arquivo"]))
        self._carregar(self.doc.atual(), pos=float(e.get("pos") or 0))
        self._set("pause", bool(e.get("pausado", True)))
        self.setWindowTitle(f"{self.doc.original.name} — {TITULO}")
        self.marcas = sorted(e.get("marcas") or [])
        self.tela.definir(e.get("zoom", 0), *(e.get("pan") or [0, 0]))
        self.statusBar().showMessage("[dev] código recarregado", 3000)

    def dropEvent(self, e):
        urls = e.mimeData().urls()
        if urls:
            self.abrir(urls[0].toLocalFile())

    def closeEvent(self, e):
        self.painel_exportar.encerrar()
        try:
            self.tela.liberar()
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
QWidget#canto { background: #0d1117; border-right: 1px solid #21262d; }
QLabel#tempo { color: #e6edf3; }
QLabel#nome_faixa { color: #e6edf3; font-size: 16px; font-weight: 700; }
QLabel#detalhe_faixa { font-size: 11px; }
QFrame#divisor_faixa { background: #30363d; }
QLineEdit#ir { background: #15171c; border: 1px solid #4fa3ff; border-radius: 4px; padding: 1px 2px; color: #e6edf3; }
QLabel#sutil { color: #6e7681; }
QToolButton#info_video { background: rgba(13, 17, 23, 170); border: none; border-radius: 4px; padding: 3px; }
QToolButton#info_video:hover { background: rgba(48, 54, 61, 210); }
QLabel#quadro { color: #e6edf3; background: rgba(13, 17, 23, 170); border-radius: 4px; padding: 3px 7px; }
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
    app.setStyleSheet(QSS.replace("@SETA@", seta.as_posix()) + _info.QSS + _exportacao.QSS)


def main():
    if "--diagnostico" in sys.argv[1:]:
        sys.exit(binarios.imprimir_diagnostico())
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    # Já existe uma janela? Encaminha o arquivo para ela.
    if cliente.vivo():
        if args:
            cliente.enviar("abrir", arquivo=str(Path(args[0]).resolve()))
        else:
            cliente.enviar("mostrar")
        return
    app = QApplication(sys.argv)
    app.setApplicationName(TITULO)
    app.setDesktopFileName(NOME)
    locale.setlocale(locale.LC_NUMERIC, "C")
    _tema(app)
    j = Janela()
    j.show()
    retomada = os.environ.pop("AI_EDITOR_RETOMAR", None)
    if retomada:
        QTimer.singleShot(0, lambda: j.retomar(json.loads(retomada)))
    elif args:
        QTimer.singleShot(0, lambda: j.abrir(args[0]))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
