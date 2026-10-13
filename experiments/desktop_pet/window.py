"""Small transparent QWidget mascot. No capture, model inference or global input hooks."""
import json
from pathlib import Path
from PySide6.QtCore import Qt,QTimer,QPoint,QRect,QRectF,Signal
from PySide6.QtGui import QPainter,QColor,QPen,QRegion,QCursor,QPixmap,QActionGroup
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QWidget,QHBoxLayout,QToolButton,QMenu,QApplication,QSystemTrayIcon,QLabel,QTextBrowser
from .appearance import CHARACTER_PATH,FALLBACK_PATH,character_icon

HERE=Path(__file__).resolve().parent
LABELS={'IDLE':'未连接','CONNECTING':'连接中','RESUMING':'恢复中','RUNNING':'待机',
    'PAUSING':'正在暂停','PAUSED':'已完全暂停','FAILED':'错误','ENDED':'未连接','UNKNOWN':'失联 · 状态未知'}

def clamp_position(point,size,screens,*,bottom_visible=None):
    """Clamp in logical coordinates; pets may leave only a grab area above the bottom."""
    target=next((rect for rect in screens if rect.intersects(QRect(point,size))),screens[0])
    visible_height=size.height() if bottom_visible is None else min(size.height(),bottom_visible)
    return QPoint(max(target.left(),min(point.x(),target.right()-size.width()+1)),
                  max(target.top(),min(point.y(),target.bottom()-visible_height+1)))

def control_icon(name):
    from PySide6.QtGui import QIcon
    paths={'voice':'<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M6 10v2a6 6 0 0 0 12 0v-2M12 18v4M8 22h8"/>',
           'chat':'<path d="M3 3h18v14H10l-6 4v-4H3z"/>',
           'pause':'<path d="M8 5v14M16 5v14"/>',
           'resume':'<path d="M8 4l12 8-12 8z"/>'}
    data=('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"><g fill="none" stroke="#eaf5fc" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'+paths[name]+'</g></svg>').encode()
    pix=QPixmap(24,24);pix.fill(Qt.transparent);painter=QPainter(pix);QSvgRenderer(data).render(painter);painter.end()
    return QIcon(pix)

class HoverBar(QWidget):
    def __init__(self,pet):
        super().__init__(None,Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint)
        self.pet=pet;self.setAttribute(Qt.WA_ShowWithoutActivating);self.setFocusPolicy(Qt.NoFocus)
        self.setStyleSheet('QWidget{background:#24364b;border-radius:9px;}QToolButton{color:#f4f6ff;padding:8px;border:0;}QToolButton:hover{background:#41617b;}QToolButton:disabled{color:#65758c;}')
        layout=QHBoxLayout(self);layout.setContentsMargins(5,3,5,3)
        self.primary=QToolButton();self.stop=QToolButton();self.panel=QToolButton()
        for button in (self.primary,self.stop,self.panel):button.setFocusPolicy(Qt.NoFocus);layout.addWidget(button)
        self.primary.clicked.connect(pet.primary);self.stop.setText('停止回答');self.stop.setToolTip('停止当前回答，仍可继续提问')
        self.stop.clicked.connect(lambda:pet.command.emit('interrupt',{}));self.panel.setText('会话');self.panel.setToolTip('打开同一实例的语音调试页面')
        self.panel.clicked.connect(pet.open_panel.emit)
    def enterEvent(self,event):self.pet.hide_timer.stop();super().enterEvent(event)
    def leaveEvent(self,event):self.pet.hide_timer.start();super().leaveEvent(event)

class Bubble(QTextBrowser):
    def __init__(self,pet):
        super().__init__(None)
        self.setWindowFlags(Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint)
        self.pet=pet;self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setOpenLinks(False);self.setOpenExternalLinks(False)
        self.resize(340,230)
        self.setStyleSheet('QTextBrowser{color:#e9f4fc;background:#23384a;border:1px solid #6b92ab;border-radius:10px;padding:12px;font:14px "Microsoft YaHei";}')
        self.setViewportMargins(0,28,0,0)
        self.close_button=QToolButton(self);self.close_button.setText('×')
        self.close_button.setObjectName('closeExplanation')
        self.close_button.setAccessibleName('关闭解释对话框');self.close_button.setToolTip('关闭本次解释，保留聊天记录')
        self.close_button.setFixedSize(26,24);self.close_button.setFocusPolicy(Qt.NoFocus)
        self.close_button.setStyleSheet('QToolButton{color:#dcebf5;background:transparent;border:0;font-size:20px;}QToolButton:hover{background:#41617b;border-radius:4px;}')
        self.close_button.clicked.connect(pet.dismiss_bubble)
        self.setToolTip('可滚动阅读和复制；右上角关闭本次解释，聊天记录仍保留')
    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'close_button'):self.close_button.move(self.width()-36,5);self.close_button.raise_()
    def keyPressEvent(self,event):
        if event.key()==Qt.Key_Escape:self.pet.dismiss_bubble();event.accept()
        else:super().keyPressEvent(event)
    def mouseReleaseEvent(self,event):
        super().mouseReleaseEvent(event)
        if event.button()==Qt.LeftButton and self.pet.daily and not self.pet.state.get('learning') and not self.textCursor().hasSelection():
            self.pet.open_panel.emit()
    def setText(self,text):self.setPlainText(text)


class PetWindow(QWidget):
    command=Signal(str,dict);open_panel=Signal();quit_requested=Signal()
    def __init__(self,settings_path=None, *, daily=False):
        self.daily=daily
        super().__init__(None,Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint)
        self.settings_path=Path(settings_path or HERE/'logs'/'settings.json')
        self.preferences={'size':160,'topmost':True,'bubble':True,'static':False}
        try:self.preferences.update(json.loads(self.settings_path.read_text(encoding='utf-8')))
        except (OSError,ValueError):pass
        self.preferences['size']=self.preferences['size'] if self.preferences['size'] in (140,160,180) else 160
        self.setAttribute(Qt.WA_TranslucentBackground);self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.NoFocus);self.setMouseTracking(True);self.setWindowTitle('Ativia 原生语音桌宠')
        self.character=QPixmap(str(CHARACTER_PATH))
        self.renderer=QSvgRenderer(str(FALLBACK_PATH)) if self.character.isNull() else None
        self.phase=0
        self.state={'state':'UNKNOWN','mic_active':False,'mic_enabled':True,'speaking':False,'thinking':False,'caption':'','instance':'等待连接'}
        self._bubble_dismissed=False;self._dismiss_wait_for_end=False
        self.bar=HoverBar(self);self.bubble=Bubble(self);self.menu=None
        self.hide_timer=QTimer(self);self.hide_timer.setSingleShot(True);self.hide_timer.setInterval(280);self.hide_timer.timeout.connect(self.hide_hover)
        self.animation=QTimer(self);self.animation.setInterval(100);self.animation.timeout.connect(self.animate)
        self.bubble_timer=QTimer(self);self.bubble_timer.setSingleShot(True);self.bubble_timer.setInterval(10000);self.bubble_timer.timeout.connect(self.bubble.hide)
        self.press_point=None;self.dragging=False
        self.tray=QSystemTrayIcon(character_icon(),self)
        self.tray.activated.connect(lambda reason:self.reveal() if reason==QSystemTrayIcon.Trigger else None)
        self.resize_pet();self.set_topmost(self.preferences['topmost'],save=False)
        screens=[s.geometry() for s in QApplication.screens()]
        work_area=QApplication.screens()[0].availableGeometry()
        preferred=self.preferences.get('position',[work_area.right()-self.width()-22,work_area.bottom()-self.height()-20])
        self.move(clamp_position(QPoint(*preferred),self.size(),screens,bottom_visible=64))
        for screen in QApplication.screens():screen.geometryChanged.connect(self.keep_on_screen)
        QApplication.instance().screenAdded.connect(lambda screen:screen.geometryChanged.connect(self.keep_on_screen))
        QApplication.instance().screenRemoved.connect(self.keep_on_screen)
        self.update_state(self.state);self.tray.show()
        if daily:
            self.setWindowTitle('Ativia');self.bar.panel.setToolTip('展开完整聊天')
            self.bar.panel.setText('');self.bar.panel.setIcon(control_icon('chat'));self.bar.panel.setAccessibleName('聊天')
            self.bar.stop.clicked.disconnect()
            self.bar.stop.clicked.connect(lambda:self.command.emit('voice',{}))
            self.bar.stop.setText('');self.bar.stop.setIcon(control_icon('voice'));self.bar.stop.setAccessibleName('语音')
            self.bar.stop.setToolTip('语音输入与声音设置')

    def resize_pet(self):
        size=self.preferences['size'];self.setFixedSize(size,size+28)
        # Only the character and the small status pill intercept mouse input.
        if not self.character.isNull():
            # Cache at resize, preserve the full portrait and crisp pixel edges.
            self.sprite=self.character.scaled(size-4,size-4,Qt.KeepAspectRatio,Qt.FastTransformation)
            self.sprite_origin=QPoint((size-self.sprite.width())//2,size-2-self.sprite.height())
            region=QRegion(self.sprite.mask()).translated(self.sprite_origin)
        else:
            region=QRegion(QRect(int(size*.18),int(size*.03),int(size*.64),int(size*.68)),QRegion.Ellipse)
            region|=QRegion(QRect(int(size*.23),int(size*.57),int(size*.54),int(size*.38)),QRegion.Ellipse)
            region|=QRegion(QRect(int(size*.24),int(size*.04),int(size*.55),int(size*.25)))
        region|=QRegion(QRect(2,size-1,size-4,26))
        self.setMask(region);self.update()

    def display_state(self):
        s=self.state
        if self.daily:
            from pet_state import pet_state,PetState
            state=pet_state(s)
            return {PetState.IDLE:'·',PetState.LISTENING:'●',PetState.THINKING:'.'*(1+self.phase%3),
                PetState.SPEAKING:'♫',PetState.SCREEN_REACTION:'!',PetState.SLEEPING:'zZ',PetState.PAUSED:'Ⅱ'}[state]
        if s.get('state')=='RUNNING':
            if s.get('speaking'):return '说话中'
            if s.get('thinking'):return '思考中'
            if s.get('translation_active'):return '正在实时翻译'
            if s.get('text_only'):return '仅文字 · 未启用麦克风'
            return '正在收音' if s.get('mic_active') else '待机'
        return LABELS.get(s.get('state'),'状态未知')

    def update_state(self,state):
        previous=self.state;self.state=dict(state)
        label=self.display_state();mic='收音中' if state.get('mic_active') else '麦克风关闭' if state.get('mic_active') is False else '麦克风未知'
        engine='日常助手' if self.daily else '原生语音：模型内置声音'
        tip=f"Ativia · {engine} · 实例 {state.get('instance','?')}\n{label} · {mic}\n{state.get('voice_status','仅控制此实例')}"
        self.setToolTip(tip);self.tray.setToolTip(tip)
        # The supplied portrait keeps its own expression; SVG overlays are fallback-only.
        active=state.get('state')=='RUNNING' and state.get('mode') not in ('CLASS','SLEEP','FOCUS') and (state.get('speaking') or state.get('thinking'))
        if active and self.isVisible() and not self.preferences['static']:self.animation.start()
        else:self.animation.stop();self.phase=0
        self.bar.primary.setText('恢复' if state.get('state')=='PAUSED' else '查看会话' if state.get('state')=='UNKNOWN' else '开始' if state.get('state') in ('IDLE','ENDED') else '完全暂停')
        self.bar.primary.setToolTip('先停止全部输入、会话与播放；确认后才显示已暂停')
        self.bar.primary.setEnabled(state.get('state')!='PAUSING')
        self.bar.stop.setEnabled(state.get('state')=='RUNNING')
        if self.daily:
            self.bar.primary.setText('');self.bar.primary.setIcon(control_icon('resume' if state.get('state')=='PAUSED' else 'pause'))
            self.bar.primary.setAccessibleName('恢复' if state.get('state')=='PAUSED' else '完全暂停')
        caption=state.get('caption','')
        if self._bubble_dismissed:
            if state.get('thinking') and not previous.get('thinking'):
                self._bubble_dismissed=False
            elif self._dismiss_wait_for_end and not state.get('thinking'):
                self._dismiss_wait_for_end=False
            elif not self._dismiss_wait_for_end and not previous.get('thinking') and caption!=previous.get('caption'):
                self._bubble_dismissed=False
        if caption!=previous.get('caption'):
            if caption and not self._bubble_dismissed and self.preferences['bubble'] and self.isVisible() and state.get('state')=='RUNNING':
                from pet_state import bubble_preview
                self.bubble.setText(caption if state.get('learning') or not self.daily else bubble_preview(caption))
                self.bubble.setToolTip('可滚动阅读和复制；Esc 或 × 关闭解释' if state.get('learning') else '点击展开完整聊天；× 关闭气泡')
                self.bubble.resize(380,280) if state.get('learning') else self.bubble.resize(310,125)
                self.position_popups();self.bubble.show()
                if state.get('learning'):self.bubble_timer.stop()
                else:self.bubble_timer.start()
            else:self.bubble.hide()
        if state.get('state')!='RUNNING':self.bubble.hide();self.bubble_timer.stop()
        if not self.menu or not self.menu.isVisible():self.refresh_tray_menu()
        self.update()

    def dismiss_bubble(self):
        self._bubble_dismissed=True;self._dismiss_wait_for_end=bool(self.state.get('thinking'))
        self.bubble_timer.stop();self.bubble.hide()

    def animate(self):self.phase=(self.phase+1)%10;self.update()
    def paintEvent(self,event):
        painter=QPainter(self);painter.setRenderHint(QPainter.Antialiasing)
        size=self.preferences['size']
        if self.renderer is None:
            painter.setRenderHint(QPainter.SmoothPixmapTransform,False)
            painter.drawPixmap(self.sprite_origin,self.sprite)
        else:
            self.paint_svg(painter,size)
        painter.setPen(Qt.NoPen);painter.setBrush(QColor('#ef23354b'));painter.drawRoundedRect(QRectF(2,size-1,size-4,25),11,11)
        mic=self.state.get('mic_active');painter.setBrush(QColor('#fa837d' if mic else '#82d4bc' if mic is False else '#e3b96a'));painter.drawEllipse(QRectF(10,size+7,7,7))
        label=self.display_state()
        if mic and label!='正在收音' and not self.daily:label='收音 · '+label
        painter.setPen(QColor('#eff6ff'));painter.drawText(QRectF(21,size,size-26,23),Qt.AlignCenter,label)

    def paint_svg(self,painter,size):
        painter.save();painter.scale(size/180,size/180)
        self.renderer.render(painter,QRectF(0,0,180,180));painter.setPen(QPen(QColor('#354667'),2.4))
        paused=self.state.get('state')=='PAUSED';error=self.state.get('state') in ('FAILED','UNKNOWN')
        for x in (75,105):
            if paused:painter.drawArc(QRectF(x-5,80,10,7),180*16,180*16)
            else:
                painter.setBrush(QColor('#67aeb5' if not error else '#bb8395'));painter.drawEllipse(QRectF(x-4,77,8,13))
                painter.setBrush(QColor('white'));painter.setPen(Qt.NoPen);painter.drawEllipse(QRectF(x-2,78,3,4));painter.setPen(QPen(QColor('#354667'),2.4))
        if self.state.get('speaking') and self.phase%2==0:
            painter.setBrush(QColor('#bc6c7e'));painter.drawEllipse(QRectF(86,95,8,8))
        else:painter.drawArc(QRectF(85,95,10,7),180*16,180*16)
        if self.state.get('thinking'):
            painter.setBrush(QColor('#70cfbf'));painter.setPen(Qt.NoPen);painter.drawEllipse(QRectF(135,42-(self.phase%3),5,5))
        painter.restore()

    def primary(self):
        state=self.state.get('state')
        if state=='UNKNOWN':self.open_panel.emit();return
        command='resume' if state=='PAUSED' else 'start' if state in ('IDLE','ENDED','UNKNOWN') else 'pause'
        self.command.emit(command,{})
    def position_popups(self):
        screens=[s.availableGeometry() for s in QApplication.screens()]
        self.bar.adjustSize();self.bar.move(clamp_position(self.pos()+QPoint((self.width()-self.bar.width())//2,-self.bar.height()+3),self.bar.size(),screens))
        self.bubble.move(clamp_position(self.pos()+QPoint(-self.bubble.width()+12,-self.bubble.height()),self.bubble.size(),screens))
    def enterEvent(self,event):
        self.hide_timer.stop();self.position_popups();self.bar.show();super().enterEvent(event)
    def leaveEvent(self,event):self.hide_timer.start();super().leaveEvent(event)
    def hide_hover(self):
        point=QCursor.pos()
        if self.frameGeometry().contains(point) or (self.bar.isVisible() and self.bar.frameGeometry().contains(point)):return
        self.bar.hide()
    def mousePressEvent(self,event):
        if event.button()==Qt.LeftButton:self.press_point=event.globalPosition().toPoint()-self.pos();self.press_global=event.globalPosition().toPoint();self.dragging=False
    def mouseMoveEvent(self,event):
        if self.press_point is not None and event.buttons()&Qt.LeftButton:
            if (event.globalPosition().toPoint()-self.press_global).manhattanLength()>=QApplication.startDragDistance():self.dragging=True
            if self.dragging:self.move(event.globalPosition().toPoint()-self.press_point);self.position_popups()
    def mouseReleaseEvent(self,event):
        if event.button()==Qt.LeftButton:
            if self.dragging:self.keep_on_screen();self.save()
            elif self.daily:self.open_panel.emit()
            self.press_point=None;self.dragging=False # A body click never starts audio.
    def contextMenuEvent(self,event):
        self.menu=self.make_menu();self.menu.popup(event.globalPos())
    def make_menu(self):
        if self.daily:return self.make_daily_menu()
        menu=QMenu(self);menu.setStyleSheet('QMenu{background:#24364b;color:#edf3fc;border:1px solid #6d839e;padding:5px;}QMenu::item{padding:7px 16px;}QMenu::item:selected{background:#42627b;}QMenu::item:disabled{color:#8a9aab;}QMenu::separator{height:1px;background:#587089;margin:4px;}')
        engine='日常助手' if self.daily else '原生语音 · 模型内置声音'
        menu.addAction(f"{engine} · {self.state.get('instance','?')}").setEnabled(False)
        if self.daily:menu.addAction(self.state.get('voice_status','声音尚未播放')).setEnabled(False)
        menu.addAction(self.display_state()).setEnabled(False);menu.addSeparator()
        start=menu.addAction('开始会话',lambda:self.command.emit('start',{}));start.setEnabled(self.state.get('state') in ('IDLE','ENDED'))
        pause=menu.addAction('恢复助手' if self.state.get('state')=='PAUSED' else '完全暂停助手',lambda:self.command.emit('resume' if self.state.get('state')=='PAUSED' else 'pause',{}));pause.setEnabled(self.state.get('state')!='PAUSING')
        stop=menu.addAction('停止当前回答',lambda:self.command.emit('interrupt',{}));stop.setEnabled(self.state.get('state')=='RUNNING')
        mic=menu.addAction('麦克风（勾选为启用设置）');mic.setCheckable(True);mic.setChecked(self.state.get('mic_enabled',True));mic.triggered.connect(lambda checked:self.command.emit('mic',{'enabled':checked}))
        if self.state.get('text_only'):
            mic.setChecked(False);mic.setEnabled(False);mic.setText('仅文字 · 未启用麦克风')
        if self.daily:
            menu.addAction('聊天 / 设置' if self.state.get('text_only') else '屏幕与系统音频设置',self.open_panel.emit)
        else:
            for text in ('自动观察屏幕','系统音频'):menu.addAction(text+'：当前实验未接入').setEnabled(False)
        if self.daily:
            action=menu.addAction('停止实时翻译' if self.state.get('translation_active') else '开始学习实时翻译',lambda:self.command.emit('translation',{}))
            action.setEnabled(self.state.get('state')=='RUNNING')
            menu.addAction('实时翻译设置',lambda:self.command.emit('translation_settings',{}))
        menu.addAction('打开会话 / 设置',self.open_panel.emit);menu.addSeparator()
        bubble=menu.addAction('显示字幕气泡');bubble.setCheckable(True);bubble.setChecked(self.preferences['bubble']);bubble.triggered.connect(self.toggle_bubble)
        sizes=menu.addMenu('角色大小');group=QActionGroup(sizes);group.setExclusive(True)
        for value in (140,160,180):
            action=sizes.addAction(f'{value} 像素');action.setCheckable(True);action.setChecked(value==self.preferences['size']);group.addAction(action);action.triggered.connect(lambda checked,v=value:self.set_size(v))
        top=menu.addAction('窗口置顶');top.setCheckable(True);top.setChecked(self.preferences['topmost']);top.triggered.connect(self.set_topmost)
        static=menu.addAction('静态显示（关闭状态动画）');static.setCheckable(True);static.setChecked(self.preferences['static']);static.triggered.connect(self.set_static)
        menu.addAction('显示桌宠',self.reveal)
        menu.addAction('隐藏桌宠（助手继续运行）',self.hide_pet);menu.addSeparator()
        menu.addAction('退出本次助手',self.quit_requested.emit)
        return menu
    def make_daily_menu(self):
        menu=QMenu(self)
        menu.setStyleSheet('QMenu{background:#24364b;color:#edf3fc;padding:5px;}QMenu::item{padding:7px 16px;}QMenu::item:selected{background:#42627b;}')
        menu.addAction('聊天 / Chat',self.open_panel.emit)
        modes=menu.addMenu('模式 / Mode');group=QActionGroup(modes);group.setExclusive(True)
        for name,label in (('NORMAL','日常'),('GAME','游戏'),('CLASS','课堂'),('FOCUS','专注'),('VOICE','语音'),('SLEEP','休息')):
            action=modes.addAction(label);action.setCheckable(True);action.setChecked(self.state.get('mode','NORMAL')==name)
            group.addAction(action);action.triggered.connect(lambda checked,n=name:self.command.emit('mode',{'mode':n}))
        memory=menu.addMenu('记忆 / Memory')
        for index,label in enumerate(('关于我','最近记忆','固定记忆','记忆设置')):
            memory.addAction(label,lambda i=index:self.command.emit('memory',{'tab':i}))
        voice=menu.addMenu('语音 / Voice')
        voice.addAction('语音输入与声音设置',lambda:self.command.emit('voice',{}))
        voice.addAction('停止当前回答',lambda:self.command.emit('interrupt',{}))
        subtitle=menu.addMenu('字幕 / Subtitle')
        action=subtitle.addAction('停止实时翻译' if self.state.get('translation_active') else '开始实时翻译',lambda:self.command.emit('translation',{}))
        action.setEnabled(self.state.get('state')=='RUNNING')
        subtitle.addAction('实时翻译设置',lambda:self.command.emit('translation_settings',{}))
        practice=menu.addAction('外语口语练习',lambda:self.command.emit('practice',{}))
        practice.setEnabled(self.state.get('state')=='RUNNING')
        settings=menu.addMenu('设置 / Settings')
        settings.addAction('会话设置',lambda:self.command.emit('settings',{}))
        settings.addAction('开发者性能面板',lambda:self.command.emit('performance',{}))
        bubble=settings.addAction('显示聊天气泡');bubble.setCheckable(True);bubble.setChecked(self.preferences['bubble']);bubble.triggered.connect(self.toggle_bubble)
        sizes=settings.addMenu('角色大小')
        for value in (140,160,180):sizes.addAction(str(value)+' 像素',lambda v=value:self.set_size(v))
        top=settings.addAction('窗口置顶');top.setCheckable(True);top.setChecked(self.preferences['topmost']);top.triggered.connect(self.set_topmost)
        static=settings.addAction('关闭状态动画');static.setCheckable(True);static.setChecked(self.preferences['static']);static.triggered.connect(self.set_static)
        settings.addAction('微信群接入',lambda:self.command.emit('wechat',{}))
        settings.addAction('显示桌宠',self.reveal);settings.addAction('隐藏桌宠',self.hide_pet)
        pause=menu.addAction('恢复 Ativia' if self.state.get('state')=='PAUSED' else '完全暂停 Ativia',self.primary)
        pause.setEnabled(self.state.get('state') not in ('PAUSING','RESUMING'))
        menu.addAction('退出',self.quit_requested.emit)
        return menu

    def refresh_tray_menu(self):
        old=self.tray.contextMenu()
        if old and old.isVisible():return
        new=self.make_menu();new.aboutToHide.connect(lambda:QTimer.singleShot(0,self.refresh_tray_menu));self.tray.setContextMenu(new)
        if old:old.deleteLater()
    def toggle_bubble(self,value):self.preferences['bubble']=value;self.bubble.hide();self.save()
    def set_size(self,value):self.preferences['size']=value;self.resize_pet();self.keep_on_screen();self.position_popups();self.save()
    def set_static(self,value):self.preferences['static']=value;self.update_state(self.state);self.save()
    def set_topmost(self,value,save=True):
        self.preferences['topmost']=value
        for window in (self,self.bar,self.bubble):
            shown=window.isVisible();window.setWindowFlag(Qt.WindowStaysOnTopHint,value)
            if shown:window.show()
        if save:self.save()
    def keep_on_screen(self,*args):
        # Include the taskbar and allow the lower body below the display edge.
        # Keep enough of the head visible to drag it back; popups stay in the work area.
        self.move(clamp_position(self.pos(),self.size(),[s.geometry() for s in QApplication.screens()],bottom_visible=64))
    def save(self):
        self.preferences['position']=[self.x(),self.y()];self.settings_path.parent.mkdir(exist_ok=True,parents=True)
        self.settings_path.write_text(json.dumps(self.preferences),encoding='utf-8')
    def hide_pet(self):self.animation.stop();self.bar.hide();self.bubble.hide();self.hide()
    def reveal(self):
        # The tray's show action also recovers a pet tucked below the taskbar.
        self.move(clamp_position(self.pos(),self.size(),[s.availableGeometry() for s in QApplication.screens()]))
        self.show();self.raise_();self.position_popups();self.update_state(self.state);self.save()
    def closeEvent(self,event):event.ignore();self.quit_requested.emit()
    def dispose(self):self.save();self.animation.stop();self.hide_timer.stop();self.bubble_timer.stop();self.bar.close();self.bubble.close();self.tray.hide();self.hide()
