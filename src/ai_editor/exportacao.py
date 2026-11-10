"""Painel de exportação (mesmo vidro do modal de metadados): predefinição, trecho, limite de tamanho, destino.

A exportação roda num QRunnable (core.exportar) e não trava a janela; o progresso aparece no painel e na
barra de status, inclusive com o painel fechado.
"""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, QRunnable, QSize, Qt, QThreadPool, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QProgressBar, QSpinBox, QToolButton, QVBoxLayout, QWidget)

from . import core
from .core import fmt_tempo
from .info import _desfocar

# ordem no combo (as demais predefinições continuam disponíveis pelo MCP)
ORDEM = ["whatsapp", "youtube", "instagram", "mp4", "menor", "webm", "gif", "transparente", "transparente_mov",
         "mp3", "m4a", "opus", "wav", "flac", "png", "jpg"]
NOMES_CORES = ["amarelo", "ciano", "rosa", "verde", "laranja", "violeta"]


class _Sinais(QObject):
    progresso = Signal(float)
    pronto = Signal(object)
    falhou = Signal(str)


class _Tarefa(QRunnable):
    def __init__(self, kw: dict, sinais: _Sinais, cancelar: threading.Event):
        super().__init__()
        self.kw, self.sinais, self.cancelar = kw, sinais, cancelar

    def run(self):
        try:
            r = core.exportar(progresso=self.sinais.progresso.emit, cancelar=self.cancelar.is_set, **self.kw)
            self.sinais.pronto.emit(r)
        except core.ExportacaoCancelada:
            self.sinais.falhou.emit("cancelada")
        except Exception as e:  # noqa: BLE001
            self.sinais.falhou.emit(str(e).strip().splitlines()[-1] if str(e).strip() else type(e).__name__)


class PainelExportar(QWidget):
    """Cobre a janela inteira. Esc ou clique fora do cartão fecha (a exportação continua)."""

    def __init__(self, janela, icone):
        super().__init__(janela)
        self.janela = janela
        self._icone = icone
        self._fundo: QPixmap | None = None
        self._cancelar: threading.Event | None = None
        self.exportando = False
        self.fracao = 0.0
        self.resultado: dict | None = None
        self.erro: str | None = None
        self._rotulo = ""
        self._sinais = _Sinais()
        self._sinais.progresso.connect(self._progresso)
        self._sinais.pronto.connect(self._pronto)
        self._sinais.falhou.connect(self._falhou)
        self.setFocusPolicy(Qt.StrongFocus)
        self.hide()

        self.cartao = QFrame(self)
        self.cartao.setObjectName("vidro")
        lc = QVBoxLayout(self.cartao)
        lc.setContentsMargins(22, 16, 22, 18)
        lc.setSpacing(12)

        tit = QVBoxLayout()
        tit.setSpacing(0)
        l = QLabel("Exportar")
        l.setObjectName("vidro_titulo")
        self.l_sub = QLabel("")
        self.l_sub.setObjectName("vidro_sub")
        tit.addWidget(l)
        tit.addWidget(self.l_sub)
        lc.addLayout(tit)

        g = QGridLayout()
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(8)
        g.setColumnStretch(1, 1)

        def rot(txt):
            r = QLabel(txt)
            r.setObjectName("vidro_rotulo")
            return r

        self.c_pred = QComboBox()
        for n in ORDEM:
            if n in core.PREDEFINICOES:
                self.c_pred.addItem(core.PREDEFINICOES[n]["rotulo"], n)
        self.c_pred.currentIndexChanged.connect(self._atualizar)
        g.addWidget(rot("Predefinição"), 0, 0)
        g.addWidget(self.c_pred, 0, 1)
        self.l_desc = QLabel("")
        self.l_desc.setObjectName("vidro_sub")
        self.l_desc.setWordWrap(True)
        g.addWidget(self.l_desc, 1, 1)

        self.r_trecho = rot("Trecho")
        self.c_trecho = QComboBox()
        self.c_trecho.currentIndexChanged.connect(self._atualizar)
        self.l_quadro = QLabel("")
        self.l_quadro.setObjectName("vidro_valor")
        g.addWidget(self.r_trecho, 2, 0)
        g.addWidget(self.c_trecho, 2, 1)
        g.addWidget(self.l_quadro, 2, 1)

        self.r_limite = rot("Tamanho máx.")
        self.s_limite = QSpinBox()
        self.s_limite.setRange(0, 100000)
        self.s_limite.setSpecialValueText("automático")
        self.s_limite.setSuffix(" MB")
        self.s_limite.setButtonSymbols(QSpinBox.NoButtons)
        self.s_limite.setToolTip("Até X MB: calcula o bitrate e codifica em 2 passes (0 = automático)")
        g.addWidget(self.r_limite, 3, 0)
        g.addWidget(self.s_limite, 3, 1, Qt.AlignLeft)

        g.addWidget(rot("Destino"), 4, 0)
        ld = QHBoxLayout()
        ld.setSpacing(6)
        self.e_destino = QLineEdit()
        self.e_destino.textChanged.connect(self._atualizar)
        b = QToolButton()
        b.setObjectName("vidro_botao")
        b.setIcon(icone("folder-open-outline", "#e6edf3"))
        b.setIconSize(QSize(16, 16))
        b.setToolTip("Escolher pasta")
        b.clicked.connect(self._escolher_pasta)
        ld.addWidget(self.e_destino, 1)
        ld.addWidget(b)
        g.addLayout(ld, 4, 1)
        self.l_nome = QLabel("")
        self.l_nome.setObjectName("vidro_sub")
        self.l_nome.setTextInteractionFlags(Qt.TextSelectableByMouse)
        g.addWidget(self.l_nome, 5, 1)
        lc.addLayout(g)

        self.barra = QProgressBar()
        self.barra.setObjectName("vidro_progresso")
        self.barra.setRange(0, 1000)
        self.barra.setTextVisible(False)
        self.barra.setFixedHeight(6)
        self.barra.hide()
        lc.addWidget(self.barra)
        self.l_status = QLabel("")
        self.l_status.setObjectName("vidro_status")
        self.l_status.setWordWrap(True)
        self.l_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.l_status.hide()
        lc.addWidget(self.l_status)

        lc.addStretch(1)
        rod = QHBoxLayout()
        self.b_pasta = QToolButton()
        self.b_pasta.setObjectName("vidro_botao")
        self.b_pasta.setText("Mostrar na pasta")
        self.b_pasta.clicked.connect(self._mostrar_pasta)
        self.b_pasta.hide()
        rod.addWidget(self.b_pasta)
        rod.addStretch(1)
        self.b_fechar = QToolButton()
        self.b_fechar.setObjectName("vidro_botao")
        self.b_fechar.setText("Fechar")
        self.b_fechar.clicked.connect(self.fechar)
        rod.addWidget(self.b_fechar)
        self.b_exportar = QToolButton()
        self.b_exportar.setObjectName("vidro_primario")
        self.b_exportar.setIcon(icone("export", "#ffffff"))
        self.b_exportar.setIconSize(QSize(16, 16))
        self.b_exportar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.b_exportar.setText(" Exportar")
        self.b_exportar.clicked.connect(self._exportar_ou_cancelar)
        rod.addWidget(self.b_exportar)
        lc.addLayout(rod)

    # ------------------------------------------------------------ abrir/fechar
    def abrir(self):
        j = self.janela
        if not j.doc:
            j.statusBar().showMessage("abra um arquivo para exportar", 3000)
            return
        self._fundo = _desfocar(j.grab().toImage())
        self.setGeometry(j.rect())
        nv = len(j.doc.versoes())
        self.l_sub.setText(j.doc.original.name + (f"  ·  a partir da v{nv} (não salva)" if nv else ""))
        self.l_sub.setToolTip(str(j.doc.atual()))
        if not self.e_destino.text() or self._doc_destino != j.doc.original:
            self.e_destino.setText(str(j.doc.original.parent))
            self._doc_destino = j.doc.original
        self._preencher_trechos()
        self._atualizar()
        self._posicionar()
        self.show()
        self.raise_()
        self.setFocus()

    _doc_destino = None

    def fechar(self):
        self.hide()
        self._fundo = None
        self.janela.setFocus()

    def _preencher_trechos(self):
        ms = list(self.janela.marcas)
        atual = self.c_trecho.currentData()
        self.c_trecho.blockSignals(True)
        self.c_trecho.clear()
        self.c_trecho.addItem("Arquivo inteiro", None)
        if len(ms) == 1:
            self.c_trecho.addItem(f"Da marca m1 ({fmt_tempo(ms[0])}) até o fim", (ms[0], None))
        if len(ms) >= 2:
            self.c_trecho.addItem(f"Trecho marcado m1→m{len(ms)}  ({fmt_tempo(ms[0])} → {fmt_tempo(ms[-1])})",
                                  (ms[0], ms[-1]))
            if len(ms) > 2:
                for i in range(len(ms) - 1):
                    self.c_trecho.addItem(f"m{i + 1}→m{i + 2} {NOMES_CORES[i % len(NOMES_CORES)]}  "
                                          f"({fmt_tempo(ms[i])} → {fmt_tempo(ms[i + 1])})", (ms[i], ms[i + 1]))
        i = next((k for k in range(self.c_trecho.count()) if atual and self.c_trecho.itemData(k) == atual), -1)
        # com marcas, o padrão é o trecho marcado ("só o trecho marcado")
        self.c_trecho.setCurrentIndex(i if i >= 0 else (1 if len(ms) >= 2 else 0))
        self.c_trecho.blockSignals(False)

    # ------------------------------------------------------------ estado da interface
    @property
    def predefinicao(self) -> str:
        return self.c_pred.currentData()

    def _atualizar(self, *_):
        n = self.predefinicao
        p = core.PREDEFINICOES.get(n, {})
        self.l_desc.setText(p.get("descricao", ""))
        foto = p.get("tipo") == "imagem"
        self.c_trecho.setVisible(not foto)
        self.l_quadro.setVisible(foto)
        self.r_trecho.setText("Quadro" if foto else "Trecho")
        if foto:
            self.l_quadro.setText(f"posição atual  {fmt_tempo(self.janela.pos)}")
        com_limite = (p.get("tipo") == "video" and p.get("video") != "prores") or \
                     (p.get("tipo") == "audio" and p.get("audio") in ("aac", "opus", "mp3"))
        self.r_limite.setVisible(com_limite)
        self.s_limite.setVisible(com_limite)
        if self.janela.doc:
            try:
                c = core.caminho_exportacao(self.janela.doc.original, n, None, self._destino(),
                                            self.janela.pos if foto else None)
                self.l_nome.setText(f"será salvo como  {c.name}")
            except Exception as e:  # noqa: BLE001
                self.l_nome.setText(str(e))
        self._botoes()

    def _destino(self) -> str | None:
        t = self.e_destino.text().strip()
        return (t.rstrip("/") + "/") if t else None  # sempre pasta

    def _botoes(self):
        if self.isVisible():
            self._posicionar()
        self.b_exportar.setText(" Cancelar" if self.exportando else " Exportar")
        self.b_exportar.setIcon(self._icone("close" if self.exportando else "export", "#ffffff"))
        for w in (self.c_pred, self.c_trecho, self.s_limite, self.e_destino):
            w.setEnabled(not self.exportando)

    def _escolher_pasta(self):
        d = QFileDialog.getExistingDirectory(self, "Pasta de destino", self.e_destino.text())
        if d:
            self.e_destino.setText(d)

    def _mostrar_pasta(self):
        if self.resultado:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.resultado["caminho"]).parent)))

    # ------------------------------------------------------------ exportação
    def configurar(self, predefinicao: str | None = None, trecho: int | None = None,
                   limite_mb: int | None = None, destino: str | None = None):
        """Usado pelo socket (testes): ajusta os campos como o usuário faria."""
        if predefinicao is not None:
            nome, _ = core.resolver_predefinicao(predefinicao)
            i = self.c_pred.findData(nome)
            if i < 0:
                raise ValueError(f"predefinição fora do painel: {nome}")
            self.c_pred.setCurrentIndex(i)
        if trecho is not None:
            self.c_trecho.setCurrentIndex(int(trecho))
        if limite_mb is not None:
            self.s_limite.setValue(int(limite_mb))
        if destino is not None:
            self.e_destino.setText(destino)

    def _exportar_ou_cancelar(self):
        if self.exportando:
            if self._cancelar:
                self._cancelar.set()
            return
        self.iniciar()

    def iniciar(self):
        j = self.janela
        if self.exportando or not j.doc:
            return
        n = self.predefinicao
        p = core.PREDEFINICOES[n]
        kw = {"arquivo": str(j.doc.original), "predefinicao": n, "destino": self._destino(),
              "estado": {"posicao": j.pos, "duracao": j.visao.dur}}
        if p["tipo"] == "imagem":
            kw["tempo"] = j.pos
        else:
            tr = self.c_trecho.currentData()
            if tr:
                kw["inicio"] = tr[0]
                kw["fim"] = tr[1] if tr[1] is not None else "fim"
        if self.s_limite.isVisible() and self.s_limite.value() > 0:
            kw["alvo_mb"] = self.s_limite.value()
        self.exportando = True
        self.fracao = 0.0
        self.resultado = self.erro = None
        self._rotulo = p["rotulo"]
        self._cancelar = threading.Event()
        self.barra.setValue(0)
        self.barra.show()
        self.b_pasta.hide()
        self.l_status.setText("Exportando…")
        self.l_status.show()
        self.l_status.setProperty("tipo", "")
        self._repolir(self.l_status)
        self._botoes()
        QThreadPool.globalInstance().start(_Tarefa(kw, self._sinais, self._cancelar))

    def _progresso(self, f: float):
        self.fracao = f
        self.barra.setValue(int(f * 1000))
        pct = int(f * 100)
        self.l_status.setText(f"Exportando… {pct}%")
        self.janela.statusBar().showMessage(f"Exportando ({self._rotulo})… {pct}%")

    def _pronto(self, r: dict):
        self.exportando = False
        self.resultado = r
        self.fracao = 1.0
        self.barra.setValue(1000)
        det = []
        if r.get("duracao_fmt"):
            det.append(r["duracao_fmt"])
        if r.get("video"):
            det.append(r["video"]["resolucao"])
        det.append(f"{r['tamanho_mb']} MB")
        txt = f"Pronto: {r['caminho']}\n{' · '.join(det)}"
        if r.get("avisos"):
            txt += "\n" + "\n".join(r["avisos"])
        self.l_status.setText(txt)
        self.l_status.setProperty("tipo", "ok")
        self._repolir(self.l_status)
        self.b_pasta.show()
        self.janela.statusBar().showMessage(f"Exportado: {r['caminho']} ({r['tamanho_mb']} MB)", 15000)
        self._botoes()
        self._atualizar()  # o próximo nome livre muda

    def _falhou(self, msg: str):
        self.exportando = False
        self.erro = msg
        self.barra.hide()
        cancelada = msg == "cancelada"
        self.l_status.setText("Exportação cancelada." if cancelada else f"Falhou: {msg}")
        self.l_status.setProperty("tipo", "" if cancelada else "erro")
        self._repolir(self.l_status)
        self.janela.statusBar().showMessage("Exportação cancelada" if cancelada else f"Exportação falhou: {msg}", 10000)
        self._botoes()

    def cancelar(self):
        if self.exportando and self._cancelar:
            self._cancelar.set()

    @staticmethod
    def _repolir(w):
        w.style().unpolish(w)
        w.style().polish(w)

    def estado(self) -> dict:
        tr = self.c_trecho.currentData()
        return {"visivel": self.isVisible(), "exportando": self.exportando, "progresso": round(self.fracao, 3),
                "predefinicao": self.predefinicao, "trecho": self.c_trecho.currentText() if tr else "arquivo inteiro",
                "trechos_disponiveis": [self.c_trecho.itemText(i) for i in range(self.c_trecho.count())],
                "limite_mb": self.s_limite.value() or None, "destino": self.e_destino.text(),
                "nome_previsto": self.l_nome.text(), "resultado": self.resultado, "erro": self.erro}

    # ------------------------------------------------------------ geometria e desenho
    def _posicionar(self):
        w, h = self.width(), self.height()
        cw = min(600, int(w * 0.9))
        lay = self.cartao.layout()
        lay.activate()  # aplica mudanças de visibilidade pendentes antes de medir
        alt = lay.totalHeightForWidth(cw) if lay.hasHeightForWidth() else lay.totalSizeHint().height()
        ch = min(max(alt, 120), int(h * 0.92))
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
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter) and not self.exportando:
            self.iniciar()
            return
        super().keyPressEvent(ev)


QSS = """
QLabel#vidro_rotulo { color: #c9d1d9; background: transparent; }
QLabel#vidro_valor { color: #eef2f7; background: transparent; font-family: 'JetBrains Mono','DejaVu Sans Mono',monospace; }
QLabel#vidro_status { color: #c9d1d9; background: transparent; }
QLabel#vidro_status[tipo="ok"] { color: #4ade80; }
QLabel#vidro_status[tipo="erro"] { color: #ff7b72; }
QFrame#vidro QSpinBox { background: #15171c; border: 1px solid #2f3540; border-radius: 6px; padding: 3px 6px; min-width: 110px; }
QToolButton#vidro_primario {
    color: white; background: #2e6fbf; border: none; border-radius: 8px; padding: 6px 14px 6px 10px; font-weight: 600;
}
QToolButton#vidro_primario:hover { background: #3a80d6; }
QProgressBar#vidro_progresso { background: rgba(255, 255, 255, 18); border: none; border-radius: 3px; }
QProgressBar#vidro_progresso::chunk { background: #4fa3ff; border-radius: 3px; }
"""
