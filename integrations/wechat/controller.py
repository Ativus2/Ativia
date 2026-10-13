"""Qt lifecycle only: the WeChat worker shares the existing app runtime and pause gate."""
from PySide6.QtCore import QObject,QTimer
from .config import WechatConfigStore

class WechatController(QObject):
    def __init__(self,overlay,store=None):
        super().__init__(overlay.control);self.overlay=overlay;self.store=store or WechatConfigStore()
        self.service=None;self.closing=False;self.reloading=False;self.error=''
        self.reload()
    def reload(self):
        if self.closing or self.reloading:return
        self.reloading=True
        if self.service:self.service.close()
        self._replace()
    def _replace(self):
        if self.closing:self.reloading=False;return
        if self.service and not self.service.closed_finished():QTimer.singleShot(50,self._replace);return
        self.service=None
        try:
            config=self.store.load()
            if config.enabled:
                from .service import WechatService
                o=self.overlay
                self.service=WechatService(o.companion,o._pause_gate,o.scheduler,lambda:o._settings,config)
                self.service.start()
            self.error=''
        except Exception as exc:self.error=type(exc).__name__
        self.reloading=False
    def snapshot(self):
        result=self.service.snapshot() if self.service else dict(enabled=False,status='configuration_error' if self.error else 'disabled')
        result.update(reloading=self.reloading,error_type=self.error,app_state=self.overlay._pause_gate.state)
        return result
    def quiescent(self):return not self.service or self.service.quiescent()
    def close(self):
        self.closing=True
        if self.service:self.service.close()
    def closed_finished(self):return not self.service or self.service.closed_finished()
    def command(self,message):
        action=message.get('action')
        if action=='wechat-status':return self.snapshot()
        if action=='wechat-reload':self.reload();return self.snapshot()
        memory=self.overlay.companion.memory
        if action=='wechat-memory':
            person=message.get('person');group=message.get('group')
            from .identity import group_id
            if group and not group.startswith('wechat:group:'):group=group_id(group)
            rows=[r for r in memory.snapshot if r.get('conversation_type')=='group' and
                  (not group or r.get('group_id')==group) and (not person or person in (r.get('subject_id'),r.get('subject_name')))]
            return {'memories':rows,'people':memory.people_snapshot}
        if action=='wechat-delete':
            identity=message.get('id')
            if not isinstance(identity,str) or not any(r['id']==identity for r in memory.snapshot):return {'error':'memory_not_found'}
            return memory.change('delete',identity)
        return {'error':'unknown_wechat_command'}
