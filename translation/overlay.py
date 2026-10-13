"""Stable bottom overlay; original and translation remain separate from pet replies."""
from collections import OrderedDict
from html import escape
from PySide6.QtCore import Qt, Signal, QEvent, QRect
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QFrame,QLabel,QPushButton,QTextBrowser,QApplication,QSizePolicy
from .captions import Caption

class SubtitleOverlay(QWidget):
    stopped=Signal()
    ask=Signal(str,str)
    show_source=Signal()
    hide_source=Signal()
    diagnostics_requested=Signal()
    settings_requested=Signal()
    def __init__(self):
        super().__init__(None,Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint|Qt.WindowDoesNotAcceptFocus)
        self.setWindowTitle('Ativia · 实时翻译')
        self.setAttribute(Qt.WA_ShowWithoutActivating); self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.NoFocus); self.rows=OrderedDict(); self.count=3; self.epoch=0; self.show_original=True; self.show_partials=True; self.source_buttons=[]
        self._screen=None; self._drag=None; self._placed=False; self._serial=0
        self._resize_origin=None
        self.setMouseTracking(True)
        self.setMinimumSize(360,140)
        layout=QVBoxLayout(self); layout.setContentsMargins(0,0,0,0)
        panel=QFrame(); panel.setObjectName('captionPanel'); body=QVBoxLayout(panel)
        panel.setMouseTracking(True); panel.installEventFilter(self)
        top=QHBoxLayout(); self.status=QLabel('尚未开始'); self.status.setTextFormat(Qt.PlainText)
        self.status.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.status.setMinimumWidth(0)
        self.status.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        top.addWidget(self.status,1)
        for title,signal in [('Windows 字幕',self.show_source),('收起',self.hide_source),('诊断',self.diagnostics_requested),('设置',self.settings_requested),('停止 ×',self.stopped)]:
            button=QPushButton(title); button.setFocusPolicy(Qt.NoFocus); button.clicked.connect(signal.emit); top.addWidget(button)
            if signal in (self.show_source,self.hide_source):self.source_buttons.append(button)
        body.addLayout(top)
        self.text=QTextBrowser(); self.text.setOpenLinks(False); self.text.setOpenExternalLinks(False)
        self.text.setFocusPolicy(Qt.NoFocus); self.text.anchorClicked.connect(self._ask)
        body.addWidget(self.text,1); layout.addWidget(panel)
        self.setStyleSheet('QFrame#captionPanel{background:rgba(24,31,42,238);border:1px solid #607087;border-radius:12px;} QLabel{color:#b7c9dc;font:11px "Microsoft YaHei";} QTextBrowser{background:transparent;border:0;color:white;} QPushButton{background:transparent;border:0;color:#b7c9dc;padding:4px;} QPushButton:hover{color:white;}')
        self._grip=QLabel('◢',self)
        self._grip.setStyleSheet('color:#8da8c4;background:transparent;font-size:12px;')
        self._grip.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._grip.setFixedSize(14,14)
        self.setToolTip('拖动边缘或角落调整大小；拖动顶部空白处移动字幕框。')
        self.place_bottom()
    def place_bottom(self,screen=None):
        # Once placed, preserve the user-selected screen, size and position.
        if self._placed:
            screen=QApplication.screenAt(self.geometry().center()) or screen
        screen=screen or self._screen or QApplication.primaryScreen()
        if screen is not self._screen:
            if self._screen:
                try:self._screen.availableGeometryChanged.disconnect(self._reposition)
                except (RuntimeError,TypeError):pass
            self._screen=screen; screen.availableGeometryChanged.connect(self._reposition)
        area=screen.availableGeometry()
        if not self._placed:
            self.resize(max(360,min(1000,area.width()-64)),max(140,min(310,area.height()-100)))
            self.move(area.x()+(area.width()-self.width())//2,area.bottom()-self.height()-36)
            self._placed=True
        else:
            self.resize(min(self.width(),area.width()),min(self.height(),area.height()))
            self.move(max(area.left(),min(self.x(),area.right()-self.width()+1)),
                      max(area.top(),min(self.y(),area.bottom()-self.height()+1)))
    def _reposition(self,_):self.place_bottom()
    def configure(self,settings):
        self.count=int(settings.overlay_sentences)
        self.show_original=settings.show_original=='yes';self.show_partials=settings.show_partials=='yes'
        for button in self.source_buttons:button.setVisible(settings.backend=='windows')
        self.setWindowFlag(Qt.WindowTransparentForInput,settings.click_through=='yes')
    def start_listening(self,screen=None):
        self.epoch=0; self.rows.clear(); self.text.clear(); self.place_bottom(screen)
        self.set_status('正在连接字幕来源…',reveal=True)
    def set_status(self,message,*,reveal=False):
        self.status.setText(message); self.status.setToolTip(message)
        if reveal:self.show()
    def reset_epoch(self,epoch):
        self.epoch=epoch;self.rows.clear();self.text.clear()
    def upsert(self,c):
        if c.epoch<self.epoch:return
        if c.state=='MERGED':
            self.rows.pop(c.id,None);self._render();return
        if c.ignored:return
        previous=self.rows.get(c.id)
        if previous and previous.version>c.version:return
        ranks={'LIVE':0,'PARTIAL':0,'STABLE':1,'RAW_FINAL':1,'FINAL':2,'TRANSLATING':3,'TRANSLATED':4,'ERROR':4,'SKIPPED':4}
        if previous and previous.version==c.version and ranks.get(previous.state,0)>ranks.get(c.state,0):return
        self.rows[c.id]=c
        self.rows=OrderedDict(sorted(self.rows.items()))
        while len(self.rows)>self.count:self.rows.popitem(last=False)
        self._render()
    def _render(self):
        bar=self.text.verticalScrollBar(); bottom=bar.value()>=bar.maximum()-4; scroll=bar.value()
        blocks=[]
        labels={'PARTIAL':'正在识别（Partial）','RAW_FINAL':'片段定稿 · 等待合并','LIVE':'正在识别','STABLE':'正在断句','FINAL':'待翻译','TRANSLATING':'翻译中','ERROR':'翻译失败','SKIPPED':'已跳过'}
        for row in self.rows.values():
            color='#ffffff' if row.state=='TRANSLATED' else '#93abc3' if row.provisional_translation else '#c3d6eb'
            label=labels.get(row.state,'')
            if row.provisional_translation:label='临时译文'
            if row.state in ('LIVE','PARTIAL') and not self.show_partials:continue
            source=(f'<span style="color:#aabbd0;font-size:11pt">{escape(row.source)}</span><br>' if self.show_original else '')
            blocks.append(f'<div style="margin:4px 0 10px 0">{source}'
                f'<span style="color:{color};font-size:16pt">{escape(row.translation or row.provisional_translation or label)}</span> '+
                (' <span style="color:#849ab0;font-size:9pt">临时</span> ' if row.provisional_translation else '')+f'<a style="color:#9ac8ef;font-size:10pt" href="ask:{row.id}">问 Ativia</a></div>')
        self.text.setHtml(''.join(blocks))
        bar.setValue(bar.maximum() if bottom else scroll)
        self.show()
    def append_caption(self,source,translation):
        self._serial+=1
        self.upsert(Caption(self._serial,source,state='TRANSLATED',translation=translation))
    def _ask(self,url):
        try:c=self.rows[int(url.path())]
        except (KeyError,ValueError):return
        self.ask.emit(c.source,c.translation)
    def _resize_edges(self,pos):
        if self.windowFlags() & Qt.WindowTransparentForInput:
            return Qt.Edges()
        x,y=pos.x(),pos.y(); w,h=self.width(),self.height()
        # Wider corner targets make diagonal resizing easier without covering text.
        margin=14 if (x<14 or x>=w-14) and (y<14 or y>=h-14) else 7
        edges=Qt.Edges()
        if x<margin:edges|=Qt.LeftEdge
        elif x>=w-margin:edges|=Qt.RightEdge
        if y<margin:edges|=Qt.TopEdge
        elif y>=h-margin:edges|=Qt.BottomEdge
        return edges

    def _edge_cursor(self,pos):
        edges=self._resize_edges(pos)
        if edges in (Qt.LeftEdge|Qt.TopEdge,Qt.RightEdge|Qt.BottomEdge):
            self.setCursor(Qt.SizeFDiagCursor)
        elif edges in (Qt.RightEdge|Qt.TopEdge,Qt.LeftEdge|Qt.BottomEdge):
            self.setCursor(Qt.SizeBDiagCursor)
        elif edges & (Qt.LeftEdge|Qt.RightEdge):self.setCursor(Qt.SizeHorCursor)
        elif edges & (Qt.TopEdge|Qt.BottomEdge):self.setCursor(Qt.SizeVerCursor)
        else:self.unsetCursor()

    def _begin_resize(self,event):
        edges=self._resize_edges(self.mapFromGlobal(event.globalPosition().toPoint()))
        if event.button()!=Qt.LeftButton or not edges:return False
        self._resize_origin=(edges,event.globalPosition().toPoint(),QRect(self.geometry()))
        self._drag=None; self.grabMouse(); event.accept()
        return True

    def eventFilter(self,watched,event):
        # The background panel covers the frameless window's border. Handle its
        # border events here while leaving subtitle links, scrolling and buttons alone.
        if event.type()==QEvent.MouseButtonPress and self._begin_resize(event):return True
        if event.type()==QEvent.MouseMove and self._resize_origin is None:
            self._edge_cursor(self.mapFromGlobal(event.globalPosition().toPoint()))
        elif event.type()==QEvent.Leave and self._resize_origin is None:self.unsetCursor()
        return super().eventFilter(watched,event)

    def mousePressEvent(self,event):
        if self._begin_resize(event):return
        if event.button()==Qt.LeftButton:self._drag=event.globalPosition().toPoint()-self.pos()

    def mouseMoveEvent(self,event):
        if self._resize_origin is not None:
            edges,origin,rect=self._resize_origin
            delta=event.globalPosition().toPoint()-origin
            left,right,top,bottom=rect.left(),rect.right(),rect.top(),rect.bottom()
            if edges & Qt.LeftEdge:left=min(left+delta.x(),right-self.minimumWidth()+1)
            if edges & Qt.RightEdge:right=max(right+delta.x(),left+self.minimumWidth()-1)
            if edges & Qt.TopEdge:top=min(top+delta.y(),bottom-self.minimumHeight()+1)
            if edges & Qt.BottomEdge:bottom=max(bottom+delta.y(),top+self.minimumHeight()-1)
            self.setGeometry(left,top,right-left+1,bottom-top+1)
            event.accept()
        elif self._drag is not None:self.move(event.globalPosition().toPoint()-self._drag)
        else:self._edge_cursor(event.position().toPoint())

    def mouseReleaseEvent(self,event):
        if event.button()==Qt.LeftButton:
            self._resize_origin=None; self._drag=None
            if QWidget.mouseGrabber() is self:self.releaseMouse()
            self.unsetCursor()

    def hideEvent(self,event):
        self._resize_origin=None; self._drag=None
        if QWidget.mouseGrabber() is self:self.releaseMouse()
        self.unsetCursor(); super().hideEvent(event)

    def resizeEvent(self,event):
        if hasattr(self,'_grip'):
            self._grip.move(self.width()-18,self.height()-18); self._grip.raise_()
        super().resizeEvent(event)

    def closeEvent(self,event):self.stopped.emit(); super().closeEvent(event)
