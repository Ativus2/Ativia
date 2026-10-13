"""Human-readable explanations, keeping preference, admission and playback distinct."""
REASONS={
    'local_service_starting':'本地服务正在冷启动，本轮使用云端；就绪后下一轮可用本地',
    'gpu_idle':'GPU 空闲，允许本地', 'manual_gaming':'手动游戏模式', 'openai_only':'用户选择仅云端',
    'gpu_telemetry_unavailable':'GPU 状态未知或样本过期，按保护策略使用云端',
    'game_detection_unavailable':'无法检查配置的游戏进程', 'configured_game_running':'配置的游戏进程正在运行',
    'vram_headroom_low':'显存安全余量不足', 'gpu_high_pending':'GPU 当前负载较高，暂缓新推理',
    'gpu_high_sustained':'GPU 持续高负载', 'gpu_recovery_hold':'等待 GPU 连续空闲后恢复本地',
    'server_gpu_protected':'服务端资源保护拒绝本次推理','USER_PAUSED':'用户完全暂停','USER_CANCELLED':'用户停止回答',
    'voice_output_disabled':'用户关闭朗读','user_volume_zero':'用户将音量设为零',
}
def reason_text(reason):
    reason=str(reason or '')
    return REASONS.get(reason.removeprefix('GPU_PROTECTED:'),reason)

class VoiceStatus:
    def __init__(self):
        self.reply_id=None;self.actual='';self.phase='尚未播放';self.reason='';self.requested=''
    def apply(self,row):
        name=row['event']
        if name in ('INPUT_READY','TEXT_READY','TTS_ROUTE_SELECTED') and row['reply_id']!=self.reply_id:
            self.reply_id=row['reply_id'];self.actual='';self.phase='等待声音';self.reason=''
        if row['reply_id']!=self.reply_id:return
        if name=='TTS_ROUTE_SELECTED':self.reason=reason_text(row.get('reason'));self.requested=row.get('selected_voice','')
        if name in ('LOCAL_TTS_FAILED','LOCAL_TTS_INVALID_AUDIO','LOCAL_TTS_SKIPPED'):self.reason=reason_text(row.get('reason'))
        phases={'LOCAL_SERVICE_STARTING':'正在启动本地服务','LOCAL_MODEL_COLD':'首次加载本地音色',
            'LOCAL_MODEL_READY':'本地合成中','LOCAL_TTS_STARTED':'本地合成中','FALLBACK_TTS_STARTED':'正在回退到 OpenAI',
            'OPENAI_TTS_STARTED':'云端合成中','PLAYBACK_STARTED':'播放中','PLAYBACK_FINISHED':'已完整播放',
            'PLAYBACK_FAILED':'播放失败','ERROR':'失败','REPLY_FAILED':'回答失败',
            'TTS_CANCELLED':'已取消','LLM_REQUEST_STARTED':'等待模型回复','LLM_FIRST_TEXT':'正在接收回复'}
        if name in phases:self.phase=phases[name]
        if name=='LOCAL_TTS_SUCCESS':self.actual='本地 · '+row.get('voice','');self.phase='音频就绪'
        if name in ('FALLBACK_TTS_SUCCESS','OPENAI_TTS_SUCCESS'):self.actual='OpenAI · '+row.get('voice','');self.phase='音频就绪'
        if name in ('ERROR','REPLY_FAILED','PLAYBACK_FAILED','TTS_CANCELLED'):self.reason=reason_text(row.get('reason'))
    def summary(self):
        return (self.actual+' · ' if self.actual else '声音：')+self.phase
