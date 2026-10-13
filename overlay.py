from __future__ import annotations

import os
import hashlib
import re
from dataclasses import asdict, replace
from collections import deque
from time import monotonic
from typing import Literal

import numpy as np
from PySide6.QtCore import QRect, QThread, QTimer, Signal
from PySide6.QtWidgets import QApplication

from audio import (
    MicRecorder,
    SpeechPlayer,
    SystemAudioRecorder,
    has_speech,
    has_system_audio,
    synthesize_speech,
    transcribe_speech,
)
from controls import ControlBar
from assistant_pause import PauseGate, ResourceChange, running_only
from character import CharacterMemory, Observation
from character_trace import TraceCollector, capture_model
from persona import PROFILE, ConversationRhythm
from runtime_settings import RuntimeSettings, SettingsStore
from speech_activity import SpeechActivity
from voice_pipeline import VoiceWorker, PREVIEW_TEXT
from tts_pipeline import SpeechPipeline, SpeechJob
from tts_events import event as speech_event, new_reply_id
from gpu_monitor import get_monitor
from companion_policy import conversation_opportunity, unrequested_game_coaching
from hotkey import AltQWatcher
from region_select import LearningActionMenu, RegionSelectOverlay
from screenshot import (
    capture_primary_jpeg,
    capture_region_jpeg,
    hashes_similar,
    screenshot_hash,
)
from vision import ScreenshotAnalysisError, comment_on_screenshot

Mode = Literal["entertainment", "learning"]

HIDE_BEFORE_CAPTURE_MS = 180
DEFAULT_INTERVAL_SECONDS = 12
MIN_INTERVAL_SECONDS = 5
DEFAULT_VOLUME = 0.8
AUTO_ANALYSIS_COOLDOWN_SECONDS = 60
RECENT_CONTEXT_SECONDS = 180


def _reply_key(text: str) -> str:
    # Preserve minus signs, formula operators and internal punctuation.
    normalized = re.sub(r"\s+", " ", text).strip().rstrip("。！？!?").casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _observe_interval_ms() -> int:
    raw = os.getenv("OBSERVE_INTERVAL_SECONDS", str(DEFAULT_INTERVAL_SECONDS)).strip()
    try:
        seconds = float(raw)
    except ValueError:
        seconds = DEFAULT_INTERVAL_SECONDS
    return int(max(MIN_INTERVAL_SECONDS, seconds) * 1000)


class ObserveWorker(QThread):
    finished_result = Signal(str, str)
    failed = Signal(str)

    def __init__(
        self,
        image_jpeg: bytes,
        mic_samples: np.ndarray | None,
        system_samples: np.ndarray | None,
        sample_rate: int,
        system_active: bool,
        mode: Mode,
        speak: bool,
        user_command: str | None = None,
        previous_summary: str | None = None,
        automatic: bool = False,
        image_hash: int | None = None,
    ) -> None:
        super().__init__()
        self._image_jpeg = image_jpeg
        self._mic_samples = mic_samples
        self._system_samples = system_samples
        self._sample_rate = sample_rate
        self._system_active = system_active
        self._mode = mode
        self._speak = speak
        self.user_command = user_command
        self.previous_summary = previous_summary
        self.mode = mode
        self.image_jpeg = image_jpeg
        self.automatic = automatic
        self.image_hash = image_hash
        self.recent_context = ""
        self.recent_reply_keys: tuple[str, ...] = ()
        self.user_spoken = ""
        self.character_memory = CharacterMemory()
        self.observation_recorded = False
        self.conversation_open = True
        self.trace: dict | None = None
        self.settings = RuntimeSettings()
        self.silence_seconds = 0.0
        self.recent_speaking_count = 0
        self.reply_id = new_reply_id()
        self.valid = lambda: True
        self.companion = None

    def cancelled(self):
        return self.isInterruptionRequested() or not self.valid()

    def run(self) -> None:
        started = monotonic() if self.trace is not None else 0
        try:
            with capture_model(self.trace):
                self._observe()
        except ScreenshotAnalysisError as exc:
            if self.trace is not None:
                self.trace["error"] = dict(kind="analysis_error", message=str(exc))
            self.failed.emit(str(exc))
        except Exception:  # noqa: BLE001
            if self.trace is not None:
                self.trace["error"] = dict(kind="processing_error", message="处理失败了，请再试一次。")
            self.failed.emit("处理失败了，请再试一次。")
        finally:
            if self.trace is not None:
                self.trace["elapsed_ms"] = round((monotonic() - started) * 1000)
                self.trace["interrupted"] = self.cancelled()
                self.trace["worker_state_after"] = [asdict(item) for item in self.character_memory.entries]

    def _observe(self) -> None:
        if self.cancelled():
            return
        clues: list[str] = []

        if self._mic_samples is not None:
            try:
                spoken = transcribe_speech(self._mic_samples, self._sample_rate)
            except Exception:  # noqa: BLE001
                spoken = ""
            if spoken:
                self.user_spoken = spoken
                clues.append(f"用户说：{spoken}")

        if self.cancelled():return
        if self._system_samples is not None:
            try:
                system_text = transcribe_speech(self._system_samples, self._sample_rate)
            except Exception:  # noqa: BLE001
                system_text = ""
            if system_text:
                clues.append(f"电脑里在播：{system_text}")
            elif self._system_active:
                clues.append("电脑里有明显声音活动，但听不清具体内容")

        transcript = "；".join(clues)
        if self.trace is not None:
            self.trace["context"].update(transcript=transcript, user_spoken=self.user_spoken)
        if self.cancelled():
            return
        text = comment_on_screenshot(
            self._image_jpeg,
            transcript or None,
            mode=self._mode,
            user_command=self.user_command,
            previous_summary=self.previous_summary,
            automatic=self.automatic,
            recent_context=self.recent_context,
            observe_scene=self.automatic and self._mode == "entertainment" and not self.user_spoken,
            observation_context=self.character_memory.context(monotonic()),
            conversation_open=self.conversation_open or bool(self.user_spoken or self.user_command),
            user_expertise=self.settings.user_expertise,
            companion_context=self.companion.context(self.user_command or self.user_spoken,
                legacy_mode=self.mode,event='screen_scan' if self.automatic else 'none') if self.companion else None,
        )
        if self.trace is not None:
            self.trace["candidate"] = asdict(text) if isinstance(text, Observation) else text
        if self.cancelled():
            return
        if isinstance(text, Observation):
            if self.companion:
                from personality.engine import BehaviorContext
                plan=self.companion.planner.plan(BehaviorContext(mode='GAME' if text.scene=='game' else self.companion.mode,
                    screen_event=text.event,confident=text.confidence=='high',focused=text.focused,
                    initiative_tolerance=self.companion.relationship.initiative_tolerance))
                if plan.action=='stay_silent':text=replace(text,intent='WAIT',reply='')
            if unrequested_game_coaching(text):
                if self.trace is not None:
                    self.trace.setdefault("gates", []).append("unrequested_game_coaching")
                text = replace(text, intent="WAIT", reply="")
            opportunity = conversation_opportunity(text, self.character_memory, self.silence_seconds, self.recent_speaking_count)
            if self.trace is not None:
                self.trace["conversation_opportunity_score"] = opportunity.score
                self.trace["opportunity_reason"] = opportunity.reason
            if text.event in {"surprise", "success", "resolved"} and opportunity.score < 0.55:
                text = replace(text, intent="WAIT", reply="")
            if (not self.conversation_open or not PROFILE.allows(text.intent, text.event)
                    or (text.reply and _reply_key(text.reply) in self.recent_reply_keys)):
                if self.trace is not None:
                    self.trace["gates"] = [name for name, blocked in (
                        ("conversation_closed", not self.conversation_open),
                        ("persona_boundary", not PROFILE.allows(text.intent, text.event)),
                        ("recent_reply_duplicate", bool(text.reply and _reply_key(text.reply) in self.recent_reply_keys)),
                    ) if blocked]
                text = replace(text, intent="WAIT", reply="")
            if self.trace is not None:
                self.trace["accepted_observation"] = asdict(text)
            text = self.character_memory.accept(text, monotonic())
            self.observation_recorded = True
            if self.trace is not None:
                self.trace["decision"] = self.character_memory.entries[-1].action
        if self.automatic and not self.user_spoken and text and _reply_key(text) in self.recent_reply_keys:
            if self.trace is not None:
                self.trace.setdefault("gates", []).append("recent_reply_duplicate")
            text = ""
        if self.trace is not None:
            self.trace["decision"] = self.trace["decision"] or ("SPEAK" if text else "WAIT")
            self.trace["selected_response"] = text
        if not text:
            self.finished_result.emit("", "")
            return
        if self.cancelled():
            speech_event(self.reply_id, "TTS_CANCELLED", stage="before_text_delivery", reason="request_superseded")
            return
        self.finished_result.emit(text, "")


class OverlayApp:
    def __init__(self, start_paused=False) -> None:
        self._pause_gate = PauseGate(start_paused)
        self._resource_change = None
        self._system_enabled = True
        self.control = ControlBar()
        self.region_overlay = RegionSelectOverlay()
        self.action_menu = LearningActionMenu()
        self._worker: ObserveWorker | None = None
        self._mic = MicRecorder()
        self._system = SystemAudioRecorder()
        self._player = SpeechPlayer()
        self._speech = SpeechPipeline(self._player, self.control,
            synthesizer=lambda *args, **kwargs: synthesize_speech(*args, **kwargs))
        self._last_hash: int | None = None
        self._auto = False
        self._auto_epoch = 0
        self._next_auto_at = 0.0
        self._speaking_automatically = False
        self._system_ok = False
        self._pending_command: str | None = None
        self._pending_region: QRect | None = None
        self._hotkey: AltQWatcher | None = None
        self._request_in_progress = False
        self._selecting = False
        self._learning_image: bytes | None = None
        self._learning_summary: str | None = None
        self._learning_command: str | None = None
        # Only compact text and reply fingerprints, never screenshots or audio.
        self._recent_turns: deque[tuple[float, str, str, str]] = deque(maxlen=3)
        self._character_memory = CharacterMemory()
        self._conversation = ConversationRhythm()
        self._trace = TraceCollector()
        self._trace_dialog = None
        self._settings_store = SettingsStore()
        self._settings = self._settings_store.load()
        from companion_runtime import CompanionRuntime
        self.companion=CompanionRuntime();self.control.companion=self.companion
        from performance.scheduler import TaskScheduler
        self.scheduler=TaskScheduler()
        self._scheduler_timer=QTimer(self.control);self._scheduler_timer.setInterval(250)
        self._scheduler_timer.timeout.connect(self.scheduler.expire);self._scheduler_timer.start()
        self.companion.memory.scheduler=self.scheduler
        self._speech.scheduler=self.scheduler
        self._retired_voice=[];self._pending_user_text=None
        self._speech.allow_output=self.control.tts_allowed
        self.control._set_mode('learning' if self.companion.mode=='CLASS' else 'entertainment',persist=False)
        self._system_enabled = self._settings.system_audio_enabled
        self._speech.update_resource_settings(self._settings)
        self._gpu_monitor = get_monitor()
        self._vad = SpeechActivity(self._settings)
        self._voice_worker = None
        self._voice_queue = deque()
        self._retired_workers = []
        self._voice_dialog = None
        self._preview_worker = None
        self._preview_reply_id = None
        self._speech_error = None
        self._last_user_at = monotonic()
        self._echo_until = 0.0
        self._dropped_mic_chunks = 0
        self._playback_metrics = None
        self._latest_voice_metrics = None
        self._capture_generation = 0
        self._capture_probe=None;self._screen_meta={}
        from echo_reference import EchoReference
        self._echo_reference=EchoReference()
        self._quitting = False
        from live_translation import TranslationController
        self.translation=TranslationController(self.control,self._pause_gate)
        from language_practice.controller import PracticeController
        self.practice=PracticeController(self)
        self._practice_inputs=None
        self._voice_timer = QTimer(self.control)
        self._voice_timer.setInterval(50)
        self._voice_timer.timeout.connect(self._poll_microphone)
        if not start_paused:self._voice_timer.start()

        self._timer = QTimer()
        self._timer.setInterval(_observe_interval_ms())
        self._timer.timeout.connect(self._start_auto_observe)

        self.control.observe_requested.connect(self._start_observe)
        self.control.snip_requested.connect(self._on_snip)
        self.control.auto_toggled.connect(self._on_auto_toggled)
        self.control.quit_requested.connect(self._quit)
        self.control.set_volume(self._settings.voice_volume)
        self.control.volume_changed.connect(self._volume_changed)
        self.control.mode_changed.connect(self._on_mode_changed)
        self.control.speak_toggled.connect(self._on_speak_toggled)
        self.control.chat_submitted.connect(self._on_chat_submitted)
        self.control.explain_requested.connect(self._on_further_explanation)
        self.control.trace_requested.connect(self._open_character_trace)
        self.control.voice_settings_requested.connect(self._open_voice_settings)
        self.control.pause_requested.connect(self._toggle_pause)
        self.control.stop_reply_requested.connect(self._stop_current_reply)
        self.control.system_toggled.connect(self._set_system_enabled)
        self.control.mic_toggled.connect(lambda enabled:self._apply_settings(replace(self._settings,mic_enabled=enabled)))
        self.region_overlay.region_selected.connect(self._on_region_selected)
        self.region_overlay.cancelled.connect(self._on_region_cancelled)
        self.action_menu.action_chosen.connect(self._on_learning_action)
        self.action_menu.cancelled.connect(self._on_action_cancelled)
        self._player.set_volume(self._settings.voice_volume)
        self._player.finished.connect(self._on_speech_finished)
        self._player.started.connect(self._on_playback_started)
        self._speech.reply_started.connect(self._speech_started)
        self._speech.reply_finished.connect(self._speech_settled)
        self._speech.reply_cancelled.connect(lambda reply_id, reason: self._speech_settled(reply_id))
        self._speech.reply_failed.connect(self._speech_failed)
        self._speech.idle.connect(self._on_speech_finished)

        try:
            if not start_paused and self._settings.mic_enabled:self._mic.start()
            mic_name = self._mic.device_name or "默认麦克风"
            self.control.status_label.setText(f"麦：{mic_name}")
        except Exception:  # noqa: BLE001
            self.control.set_failure("麦克风打不开，请检查系统权限。仍可只看屏幕。")

        try:
            if not start_paused and self._system_enabled:
                self._system.start()
                self._system_ok = True
        except Exception:  # noqa: BLE001
            self.control.set_failure("系统声音监听打不开。仍可听麦克风并看屏幕。")

        self.control.show()
        if start_paused:self._speech.pause()
        self._pause_status()
        QTimer.singleShot(0, self._setup_hotkey)

    def _pause_status(self, note=''):
        mic_active = getattr(self._mic,'_stream',None) is not None and bool(getattr(self._mic,'_accepting',False))
        self.control.set_capture_state(mic_active,self._system_ok,self._settings.mic_enabled,self._system_enabled)
        if self._voice_dialog:self._voice_dialog.set_input_available(self._pause_gate.allows())
        if not note:
            note=f'麦克风：{"开" if self._settings.mic_enabled and getattr(self._mic,"_stream",None) is not None else "关/未启动"}；系统音频：{"开" if self._system_ok else "关/未启动"}'
            if self._pause_gate.state=='PAUSED':
                pending=sum(w is not None and w.isRunning() for w in [self._worker,self._voice_worker,*self._retired_workers])
                note=f'采集/播放已停止；{pending}个已发请求/计算待退出，结果作废。仅按钮恢复；其他实例不受影响。'
        self.control.set_lifecycle(self._pause_gate.state,note)

    def _cancel_pending(self, reason):
        self.scheduler.cancel_kinds(('voice','text','vision','proactive','tts','wechat'),reason)
        self.control.finish_stream()
        self._capture_generation+=1;self._auto_epoch+=1
        self._voice_queue.clear();self._vad.reset()
        self._pending_command=None;self._pending_region=None
        self._request_in_progress=False;self._selecting=False
        self.region_overlay.hide();self.action_menu.hide()
        self.control.chat_input.clear()
        workers=[self._worker,self._voice_worker,self._preview_worker,*self._retired_workers,*self._retired_voice]
        for worker in workers:
            if worker is not None:
                worker.abort() if hasattr(worker,'abort') else worker.requestInterruption()
                worker.trace=None
        speech_event('desktop',reason,in_flight_workers=sum(w is not None and w.isRunning() for w in workers),
                     note='Already admitted requests/computation may continue; late results invalidated.')
        if reason!='USER_PAUSED':self._speech.cancel(reason)

    def _stop_current_reply(self):
        if not self._pause_gate.allows():return
        if self.practice.engaged:self.practice.stop()
        self._pause_gate.epoch+=1
        self._cancel_pending('USER_CANCELLED')
        self._preempt_user();self.control.set_busy(False)

    def _stop_inputs(self):
        errors=[]
        try:self._player.wait_released(5)
        except Exception as exc:errors.append(type(exc).__name__+': audio_output_cleanup_incomplete')
        translation_worker=self.translation.worker
        if translation_worker and translation_worker.thread:
            translation_worker.thread.join(5)
            if translation_worker.thread.is_alive():
                errors.append('实时翻译麦克风尚未释放，请重试暂停。')
        for recorder in (self._mic,self._system):
            try:recorder.stop();recorder.clear()
            except Exception as exc:errors.append(type(exc).__name__+': '+str(exc))
        if errors:raise RuntimeError('; '.join(errors))

    def _hold_for_practice(self, hold):
        if hold:
            self._practice_inputs=(self._mic._stream is not None,self._system_ok,self._voice_timer.isActive())
            self._voice_timer.stop();self._voice_queue.clear();self._vad.reset()
            self._mic.stop();self._mic.clear();self._system.stop();self._system.clear();self._system_ok=False
        else:
            previous=self._practice_inputs;self._practice_inputs=None
            if previous and self._pause_gate.allows() and not self._quitting:
                self._mic.clear();self._system.clear();self._vad.reset();self._voice_queue.clear()
                if previous[0] and self._settings.mic_enabled:self._mic.start(allow_fallback=False)
                if previous[1] and self._system_enabled:self._system.start();self._system_ok=True
                if previous[2]:self._voice_timer.start()

    def _toggle_pause(self):
        state=self._pause_gate.state
        if state in ('RUNNING','FAILED'):self._pause_all()
        elif state=='PAUSED':self._resume_all()

    def _pause_all(self):
        if self._pause_gate.state in ('PAUSING','PAUSED','RESUMING'):return
        # Admission closes before any signal, cleanup or nested event loop.
        self._pause_gate.transition('PAUSING')
        self.companion.memory.paused=True
        self.practice.stop()
        self.translation.stop()
        self._mic.quiesce();self._system.quiesce()
        self._timer.stop();self._voice_timer.stop()
        self._speech.pause()
        self._player.stop(reason='USER_PAUSED')
        playback_error=getattr(self._player,'cleanup_error',None)
        if not isinstance(playback_error,str):playback_error=None
        self._cancel_pending('USER_PAUSED')
        if self._voice_dialog:
            self._voice_dialog.test_button.setChecked(False)
            self._voice_dialog.set_input_available(False)
        self._pause_status('正在释放本实例采集和播放资源；在途请求不能撤回。')
        change=ResourceChange(self._stop_inputs,self.control)
        self._resource_change=change
        def finished():
            if not self.practice.quiescent() or (getattr(self,'wechat',None) and not self.wechat.quiescent()):
                QTimer.singleShot(30,finished);return
            self._system_ok=False
            error=change.error or playback_error
            self._pause_gate.transition('FAILED' if error else 'PAUSED')
            self._pause_status(error or '')
            self._resource_change=None;change.deleteLater()
        change.finished.connect(finished);change.start()

    def _resume_all(self):
        if self._pause_gate.state!='PAUSED':return
        self._pause_gate.transition('RESUMING')
        self._capture_generation+=1;self._auto_epoch+=1
        self._pause_status('仅恢复暂停前的输入开关；不补发或补播。')
        def start_inputs():
            try:
                self._mic.clear();self._system.clear()
                if self._settings.mic_enabled:self._mic.start(allow_fallback=False)
                if self._system_enabled:self._system.start()
            except Exception:
                self._mic.quiesce();self._system.quiesce()
                self._stop_inputs()
                raise
        change=ResourceChange(start_inputs,self.control);self._resource_change=change
        def finished():
            if self._quitting:
                self._stop_inputs();self._resource_change=None;change.deleteLater();return
            if change.error:
                self._system_ok=False;self._pause_gate.transition('FAILED')
                self._pause_status('恢复失败，输入保持关闭：'+change.error)
            else:
                self._system_ok=self._system_enabled
                self._mic.clear();self._system.clear();self._vad.reset();self._voice_queue.clear()
                self._next_auto_at=monotonic()+AUTO_ANALYSIS_COOLDOWN_SECONDS
                self._pause_gate.transition('RUNNING');self.companion.memory.paused=False;self._speech.resume()
                self._voice_timer.start()
                if self._auto:self._timer.start()
                if self._voice_dialog:self._voice_dialog.set_input_available(True)
                self.control.set_busy(self._busy());self._pause_status()
            self._resource_change=None;change.deleteLater()
        change.finished.connect(finished);change.start()

    def _set_system_enabled(self, enabled):
        try:
            if self._pause_gate.allows():
                if enabled:self._system.start()
                else:self._system.stop()
                self._system_ok=enabled
            self._system_enabled=enabled
            self._settings=replace(self._settings,system_audio_enabled=enabled)
            self._settings_store.save(self._settings)
            self._pause_status()
        except Exception as exc:
            self.control.status_label.setText('系统音频切换失败：'+str(exc))

    def _setup_hotkey(self) -> None:
        self._hotkey = AltQWatcher(self.control)
        self._hotkey.activated.connect(self._on_snip)
        self._hotkey.status_changed.connect(self.control.set_hotkey_status)
        self.control.set_hotkey_status(self._hotkey.available,self._hotkey.error)

    def _busy(self) -> bool:
        worker_running = self._worker is not None and self._worker.isRunning()
        return (self._request_in_progress or self._selecting or worker_running or self._player.is_busy() or self._speech.is_busy()
                or self._voice_worker is not None or self._preview_worker is not None or self._capture_probe is not None or bool(self._voice_queue))

    def _open_voice_settings(self):
        from voice_settings import VoiceSettingsDialog
        if self._voice_dialog is None:
            self._voice_dialog = VoiceSettingsDialog(self._settings, self.control)
            self._voice_dialog.settings_changed.connect(self._apply_settings)
            self._voice_dialog.preview_requested.connect(self._preview_voice)
            self._voice_dialog.test_button.toggled.connect(lambda _: self._vad.reset())
            self._voice_dialog.finished.connect(lambda _: self._voice_dialog.test_button.setChecked(False))
        if self._latest_voice_metrics:
            self._show_voice_metrics(self._latest_voice_metrics)
        self._voice_dialog.show()
        self._voice_dialog.set_input_available(self._pause_gate.allows())
        self._voice_dialog.raise_()

    def _apply_settings(self, settings):
        settings=replace(settings,system_audio_enabled=self._system_enabled)
        # Saved preferences may change while paused; this must not acquire devices.
        if self._pause_gate.allows() and settings.mic_enabled != self._settings.mic_enabled:
            try:
                if settings.mic_enabled:self._mic.start(allow_fallback=False)
                else:self._mic.stop()
                self._vad.reset()
            except Exception as exc:
                self.control.status_label.setText('麦克风切换失败：'+str(exc));return
        try:
            self._settings_store.save(settings)
        except OSError:
            if self._voice_dialog:
                self._voice_dialog.status.setText("保存失败，未更改当前参数。")
            return
        self._settings = settings
        self._speech.update_resource_settings(settings)
        self._vad = SpeechActivity(settings)
        self.control.set_volume(settings.voice_volume)
        if self._voice_dialog:
            self._voice_dialog._base = settings
            self._voice_dialog.mic_enabled.setChecked(settings.mic_enabled)
            self._voice_dialog.status.setText("已保存并应用；停止阈值始终低于开始阈值。")
            self._voice_dialog.values["stop_threshold"].setValue(settings.stop_threshold)
            if not self._pause_gate.allows():self._voice_dialog.status.setText('偏好已保存，恢复后生效；采集仍已停止。')
        self._pause_status()

    def _volume_changed(self, value):
        self._player.set_volume(value)
        if value == self._settings.voice_volume:
            return
        self._settings = replace(self._settings, voice_volume=value)
        try:
            self._settings_store.save(self._settings)
        except OSError:
            self.control.status_label.setText("音量已调整，但设置保存失败。")

    @running_only
    def _preview_voice(self, settings):
        if self._speech.is_busy() or self._voice_worker is not None:
            self._voice_dialog.status.setText("正在处理语音，请稍后试听。")
            return
        job = SpeechJob(PREVIEW_TEXT, settings, source="preview")
        self._preview_reply_id = job.reply_id
        self._voice_dialog.preview_button.setEnabled(False)
        self._speech.submit(job)

    @running_only
    def _speech_started(self, reply_id):
        self._speech_error = None
        job = self._speech._active
        if job is not None:
            self._speaking_automatically = job.source == "automatic"
            if job.metrics is not None:
                self._latest_voice_metrics = job.metrics
                self._show_voice_metrics(job.metrics)

    def _speech_settled(self, reply_id):
        if self._preview_reply_id == reply_id:
            self._preview_reply_id = None
            if self._voice_dialog:
                self._voice_dialog.preview_button.setEnabled(True)
        if self._latest_voice_metrics:
            self._show_voice_metrics(self._latest_voice_metrics)

    @running_only
    def _speech_failed(self, reply_id, reason):
        self._speech_settled(reply_id)
        message = f"语音失败 [{reply_id[:8]}]：{reason}；回答文字已保留。"
        self._speech_error = message
        self.control.status_label.setText(message)
        if self._voice_dialog:
            self._voice_dialog.status.setText(message)

    @staticmethod
    def _delete_audio(path):
        if path:
            try:
                os.remove(path)
            except OSError:
                pass

    def _retire_screen_worker(self):
        worker = self._worker
        if worker is None:
            return
        # Keep displaced QThreads alive; bounded to two, with no new background work.
        if len(self._retired_workers) >= 2:
            return
        worker.requestInterruption()
        if worker.trace is not None:
            worker.trace.update(delivery=dict(status="cancelled", response=""), reason="user_interaction_preempted",
                                state_after=self._trace_state())
            self._trace.finish(worker.trace)
            worker.trace = None
        self._retired_workers.append(worker)
        self._worker = None
        self._request_in_progress = False

    def _preempt_user(self):
        began=monotonic()
        self.scheduler.cancel_kinds(('voice','text','vision','proactive','tts','wechat'),'new_user_input')
        self._speech.cancel('new_user_input')
        self.control.finish_stream()
        self._retire_screen_worker()
        worker=self._voice_worker
        if worker is not None:
            worker.abort();self._retired_voice.append(worker);self._voice_worker=None
        self._capture_generation+=1;self._auto_epoch+=1
        self._request_in_progress=False;self._selecting=False
        self._pending_command=None;self._pending_region=None
        self.region_overlay.hide();self.action_menu.hide()
        from performance.metrics import metrics
        metrics.mark('input','background_cancel_started',ms=(monotonic()-began)*1000)

    def _wire_voice_stream(self,worker,kind):
        worker.ticket=self.scheduler.admit(kind,timeout=45,request_id=worker.reply_id,cancel=worker.abort)
        self.scheduler.start(worker.ticket)
        epoch=self._pause_gate.epoch
        worker.valid=lambda:self._pause_gate.allows(epoch) and worker.ticket.valid()
        worker.ui_stream=False
        worker.transcription.connect(lambda text:self._on_transcription(worker,text))
        worker.delta.connect(lambda text:self._on_voice_delta(worker,text))
        worker.sentence.connect(lambda index,text:self._on_voice_sentence(worker,index,text))
        from performance.metrics import metrics
        metrics.mark(worker.reply_id,'agent_start',ms=(worker.ticket.started-worker.ticket.created)*1000)

    def _on_transcription(self,worker,text):
        if self._voice_worker is worker and not worker.cancelled() and worker.text_input is None:
            self.control.append_message('你',text);worker.user_displayed=True

    def _on_voice_delta(self,worker,text):
        if self._voice_worker is not worker or worker.cancelled():return
        if not worker.ui_stream:
            self.control.begin_stream();worker.ui_stream=True
            from performance.metrics import metrics
            metrics.mark(worker.reply_id,'agent_first_token',ms=worker.metrics.get('llm_first_token_ms') or 0)
        self.control.append_stream(text)

    def _on_voice_sentence(self,worker,index,text):
        if self._voice_worker is not worker or worker.cancelled() or not self.control.speak_enabled():return
        self._speech.submit(SpeechJob(text,worker.settings,reply_id=worker.reply_id+':'+str(index),source='voice',
            serious=worker.mode=='learning',context=worker.spoken,metrics=worker.metrics if index==1 else None,
            speech_end=worker.utterance.speech_end,parent_id=worker.reply_id))

    @running_only
    def _poll_microphone(self):
        if self.practice.engaged:self._mic.read_pending();self._vad.reset();return
        if getattr(self,"translation",None) and self.translation.worker and (self.translation.uses_microphone or self.companion.mode=="CLASS"):
            self._mic.read_pending();self._vad.reset();return
        if getattr(self._mic, "segmented_input", False) is not True:
            return
        chunks = self._mic.read_pending()
        testing = bool(self._voice_dialog and self._voice_dialog.test_button.isChecked())
        if not self._settings.mic_enabled and not testing:
            self._vad.reset()
            return
        dropped = self._mic.dropped_chunks
        if dropped != self._dropped_mic_chunks:
            self._vad.reset()
            self._dropped_mic_chunks = dropped
            self.control.status_label.setText("麦克风缓冲曾溢出；本段已重新开始检测。")
        for samples, end_at in chunks:
            if self._settings.echo_guard and self._player.is_busy():
                reference=self._player.echo_reference() if hasattr(self._player,'echo_reference') else None
                samples=self._echo_reference.filter(samples,reference)
            utterances = self._vad.feed(samples, end_at, monotonic())
            if self._vad.started and not testing:
                self._last_user_at = monotonic()
                if self._worker is not None and self._worker.automatic:
                    self._retire_screen_worker()
                self._preempt_user()
            if not testing:
                for utterance in utterances:
                    self._enqueue_voice(utterance)
        if self._voice_dialog:
            self._voice_dialog.update_meter(self._vad.level, self._vad.threshold, self._vad.active)
        if self._voice_queue and self._voice_worker is None:
            self._start_next_voice()

    @running_only
    def _enqueue_voice(self, utterance):
        if len(self._voice_queue) >= 2:
            self.control.set_failure("语音队列已满，请等当前回复后再说。")
            return
        self._last_user_at = monotonic()
        if self._voice_worker is not None or self._speech.is_busy():self._preempt_user()
        self._voice_queue.append(utterance)
        self._start_next_voice()

    @running_only
    def _start_next_voice(self):
        if self._quitting or self._voice_worker is not None or not self._voice_queue or len(self._retired_voice)>=4:
            return
        if self._worker is not None:self._retire_screen_worker()
        # Invalidate pending automatic capture callbacks before admitting voice.
        self._auto_epoch += 1
        self._capture_generation += 1
        self._request_in_progress = False
        self._selecting = False
        self._pending_command = None
        self._pending_region = None
        self.region_overlay.hide()
        self.action_menu.hide()
        began = monotonic()
        utterance = self._voice_queue.popleft()
        worker = VoiceWorker(utterance, self._settings, self._context_for_request(),
                             self._character_memory.context(monotonic()), self.control.speak_enabled(), self.control.current_mode())
        self.companion.refresh_environment()
        worker.companion=self.companion
        epoch=self._pause_gate.epoch
        worker.valid=lambda:self._pause_gate.allows(epoch)
        worker.metrics["context_assembly_ms"] = round((monotonic() - began) * 1000, 2)
        worker.trace = self._trace.begin("user_speech", worker.mode,
            dict(recent_context=worker.recent_context, observation_context=worker.observation_context,
                 forced_split=utterance.forced_split, runtime_settings=asdict(self._settings),
                 speech_end=utterance.speech_end, detected_at=utterance.detected_at,
                 dropped_mic_chunks=self._dropped_mic_chunks), self._trace_state())
        self._voice_worker = worker
        self._wire_voice_stream(worker,'voice')
        if not self.control.close_to_hide:self.control.show()
        self.control.set_busy(True)
        worker.text_ready.connect(lambda text: self._on_voice_text(worker, text))
        worker.failed.connect(lambda message: self._on_voice_failure(worker, message))
        worker.finished.connect(lambda: self._voice_finished(worker))
        worker.start()

    @running_only
    def _on_voice_text(self, worker, text):
        if self._voice_worker is not worker or worker.cancelled():
            speech_event(worker.reply_id, "TTS_CANCELLED", stage="before_text_delivery", reason="stale_llm_reply")
            return
        worker.metrics["speech_to_text_ms"] = round((monotonic() - worker.utterance.speech_end) * 1000, 2)
        self._conversation.engaged()
        self.companion.record(worker.spoken,text)
        if getattr(worker,'text_input',None) is None and not getattr(worker,'user_displayed',False):self.control.append_message('你',worker.spoken)
        self._recent_turns.append((monotonic(), worker.spoken[:120], text[:240], _reply_key(text)))
        if getattr(worker,"ui_stream",False):
            self.control.finish_stream(text)
            if worker.mode=="learning":self.control._has_learning_summary=False
        elif worker.mode == "learning":
            # A voice-only answer has no newly captured image for further explanation.
            self.control.learning_text.setPlainText(text)
            self.control.learning_title.setText("语音回答")
            self.control._has_learning_summary = False
            self.control.copy_button.setEnabled(True)
        else:
            self.control.set_entertainment_result(text)
        if worker.trace is not None:
            worker.trace["delivery"] = dict(status="displayed", response=text)
        if self.control.speak_enabled() and not worker.streamed:
            self._speech.submit(SpeechJob(text, worker.settings, reply_id=worker.reply_id, source="voice",
                serious=worker.mode == "learning", context="\n".join(filter(None, (worker.spoken, worker.recent_context))),
                metrics=worker.metrics, speech_end=worker.utterance.speech_end))
        elif not self.control.speak_enabled():
            speech_event(worker.reply_id, "TEXT_READY", source="voice", characters=len(text))
            speech_event(worker.reply_id, "TTS_CANCELLED", reason="voice_output_disabled")

    @running_only
    def _on_playback_started(self):
        if self._playback_metrics:
            metrics, speech_end = self._playback_metrics
            metrics["speech_to_audio_ms"] = round((monotonic() - speech_end) * 1000, 2)
            self._show_voice_metrics(metrics)

    @running_only
    def _on_voice_failure(self, worker, message):
        if self._voice_worker is worker and not worker.cancelled():
            self._speech_error = message
            self.control.set_failure(message)
            speech_event(worker.reply_id, 'REPLY_FAILED', reason=message, stage='stt_or_llm')
            if worker.trace is not None:
                worker.trace["delivery"] = dict(status="error", response="", error=message)

    def _voice_finished(self, worker):
        was_valid=worker.valid()
        if worker in self._retired_voice:self._retired_voice.remove(worker)
        self.scheduler.finish(getattr(worker,'ticket',None))
        from performance.metrics import metrics
        metrics.mark(worker.reply_id,'agent_done',ms=worker.metrics.get('llm_total_ms') or 0)
        if self._voice_worker is worker:
            self._voice_worker = None
        if not was_valid:
            worker.deleteLater();self._start_next_voice();self._pause_status()
            if self._pending_user_text and len(self._retired_voice)<4:
                text=self._pending_user_text;self._pending_user_text=None;self._on_chat_submitted(text)
            return
        self._latest_voice_metrics = worker.metrics
        self._show_voice_metrics(worker.metrics)
        trace = worker.trace
        if trace is not None:
            if trace["delivery"]["status"] == "pending":
                trace["delivery"] = dict(status="cancelled", response="")
            trace["state_after"] = self._trace_state()
            trace["latency"] = worker.metrics
            # Allow the player to report actual PlayingState; absent data stays null.
            epoch=self._pause_gate.epoch
            QTimer.singleShot(1000, self.control, lambda: self._save_voice_trace(trace) if self._pause_gate.allows(epoch) else None)
        worker.deleteLater()
        self.control.set_busy(self._busy())
        if self._speech_error:self.control.status_label.setText(self._speech_error)
        if worker.metrics.get("tts_error"):
            self.control.status_label.setText("回答已显示，但语音生成失败。可检查连接后再试。")
        self._start_next_voice()

    def _save_voice_trace(self, trace):
        self._trace.finish(trace)
        self._refresh_trace_ui()
        self._show_voice_metrics(trace["latency"])

    def _show_voice_metrics(self, metrics):
        if self._voice_dialog:
            self._voice_dialog.latency_label.setText("上次语音延迟（毫秒；未测到为 None）：\n" + str(metrics))

    @running_only
    def _on_mode_changed(self, _mode: str) -> None:
        if not self.control.tts_allowed():self._speech.cancel("mode_disables_tts")
        self._last_hash = None
        self._recent_turns.clear()

    def _on_speak_toggled(self, enabled: bool) -> None:
        if not enabled:
            self._speech.cancel("voice_output_disabled")

    @running_only
    def _on_auto_toggled(self, on: bool) -> None:
        self._auto = on
        self._auto_epoch += 1
        if on:
            self._timer.start()
            if not self._busy():
                epoch=self._auto_epoch
                QTimer.singleShot(800, lambda:self._start_auto_observe() if epoch==self._auto_epoch else None)
        else:
            self._timer.stop()
            if self._worker is not None and self._worker.automatic:
                self._worker.requestInterruption()
            self._speech.cancel("automatic_mode_disabled", source="automatic")

    @running_only
    def _start_auto_observe(self) -> None:
        if self.practice.engaged:return
        if self.companion.context('',legacy_mode=self.control.current_mode(),event='screen_scan')['decision']['action']=='stay_silent':
            return
        if not self._auto or self._busy() or self._retired_workers or self._vad.active:
            return
        if monotonic() < self._next_auto_at:
            # Keep the existing polling cadence for new user speech; no API needed.
            if (getattr(self._mic, "segmented_input", False) is True
                    or not has_speech(self._mic.snapshot(), self._mic.sample_rate)):
                return
        # Different modes share one identity but use independent observation rates.
        from personality.engine import MODES
        self._next_auto_at = monotonic() + max(AUTO_ANALYSIS_COOLDOWN_SECONDS,MODES[self.companion.mode].vision_seconds)
        self._start_observe(force=False, automatic=True)

    @running_only
    def _on_chat_submitted(self, command: str) -> None:
        if self.practice.engaged:self.practice.open();return
        if not command.strip():return
        self._preempt_user()
        if len(self._retired_voice)>=4:
            self._pending_user_text=command;self.control.set_busy(True);return
        self._last_user_at = monotonic()
        self.companion.refresh_environment()
        self._speech_error = None
        # Plain chat reuses the foreground worker, with no new screenshot/audio upload.
        from types import SimpleNamespace
        now = monotonic()
        utterance = SimpleNamespace(speech_end=now, detected_at=now, forced_split=False, samples=None)
        worker = VoiceWorker(utterance, self._settings, self._context_for_request(),
            self._character_memory.context(now), self.control.speak_enabled(), self.control.current_mode(),
            text_input=command)
        worker.companion=self.companion
        epoch = self._pause_gate.epoch
        worker.valid = lambda: self._pause_gate.allows(epoch)
        self._voice_worker = worker
        worker.trace=self._trace.begin('user_text',worker.mode,
            dict(user_command=command,recent_context=worker.recent_context,observation_context=worker.observation_context),
            self._trace_state())
        self._wire_voice_stream(worker,'text')
        self.control.set_busy(True)
        worker.text_ready.connect(lambda text: self._on_voice_text(worker, text))
        worker.failed.connect(lambda message: self._on_voice_failure(worker, message))
        worker.finished.connect(lambda: self._voice_finished(worker))
        worker.start()

    @running_only
    def _on_further_explanation(self) -> None:
        if self._busy() or self.control.current_mode() != "learning":
            return
        if self._learning_image is None or not self._learning_summary:
            return
        # Keep the explanation available to read until the user resumes auto mode.
        self.control.set_auto(False)
        self._on_auto_toggled(False)
        self._request_in_progress = True
        self.control.set_busy(True)
        self._launch_worker(ObserveWorker(
            self._learning_image,
            None,
            None,
            self._mic.sample_rate,
            False,
            "learning",
            self.control.speak_enabled(),
            user_command=self._learning_command,
            previous_summary=self._learning_summary,
        ))

    def _trace_state(self) -> dict:
        # Read state directly; do not call can_react/snapshot or mutate any decisions.
        return dict(memory=[asdict(item) for item in self._character_memory.entries],
                    reaction_times=list(self._conversation._reactions), monotonic=monotonic())

    def _open_character_trace(self) -> None:
        from trace_feedback import TraceFeedbackDialog
        if self._trace_dialog is None:
            self._trace_dialog = TraceFeedbackDialog(self._trace, self.control)
            self._trace_dialog.collection_changed.connect(self._refresh_trace_ui)
        self._trace_dialog.refresh()
        self._trace_dialog.show()
        self._trace_dialog.raise_()

    def _trace_local(self, reason: str, automatic: bool, error: str = "", context: dict | None = None) -> None:
        if not self._trace.enabled:
            return
        trace = self._trace.begin("automatic" if automatic else "capture", self.control.current_mode(), context or {}, self._trace_state())
        trace.update(decision="SKIP", reason=reason, model_called=False,
                     delivery=dict(status="error" if error else "skipped", response="", error=error))
        self._trace.finish(trace)
        self._refresh_trace_ui()

    def _refresh_trace_ui(self) -> None:
        self.control.trace_button.setText("记录异常" if self._trace.last_error else
                                          ("记录中 · 反馈" if self._trace.enabled else "记录与反馈"))
        self.control.trace_button.setToolTip(self._trace.last_error or "本地 Character Trace 与用户反馈")
        if self._trace_dialog is not None and self._trace_dialog.isVisible():
            self._trace_dialog.refresh()

    @running_only
    def _launch_worker(self, worker: ObserveWorker, source: str | None = None) -> None:
        worker.companion=self.companion
        if worker.user_command:self.companion.remember_input(worker.user_command)
        epoch=self._pause_gate.epoch
        worker.ticket=self.scheduler.admit('proactive' if worker.automatic else 'vision',timeout=40,request_id=worker.reply_id,cancel=worker.requestInterruption)
        self.scheduler.start(worker.ticket)
        worker.valid=lambda:self._pause_gate.allows(epoch) and worker.ticket.valid()
        worker.recent_context = self._context_for_request()
        worker.character_memory = self._character_memory.snapshot(monotonic())
        worker.conversation_open = self._conversation.can_react(monotonic())
        worker.recent_reply_keys = tuple(turn[3] for turn in self._recent_turns)
        worker.settings = self._settings
        worker.silence_seconds = max(0.0, monotonic() - self._last_user_at)
        worker.recent_speaking_count = len(self._conversation._reactions)
        if self._trace.enabled:
            worker.trace = self._trace.begin(
                source or ("further_explanation" if worker.previous_summary else
                           "automatic" if worker.automatic else "chat" if worker.user_command else "manual"),
                worker.mode,
                dict(user_command=worker.user_command, previous_summary=worker.previous_summary,
                     recent_context=worker.recent_context, image_hash=worker.image_hash,
                     system_active=worker._system_active),
                dict(**self._trace_state(), request_memory=[asdict(item) for item in worker.character_memory.entries],
                     conversation_open=worker.conversation_open, recent_reply_keys=list(worker.recent_reply_keys)))
        self._worker = worker
        self.control.set_busy(True, background=worker.automatic)
        worker.finished_result.connect(lambda text, path: self._on_comment(text, path) if self._worker is worker and worker.valid() else self._delete_audio(path))
        worker.failed.connect(lambda message: self._on_analysis_failed(message) if self._worker is worker and worker.valid() else None)
        worker.finished.connect(lambda: self._screen_finished(worker))
        worker.start()

    def _screen_finished(self, worker):
        ticket=getattr(worker,'ticket',None)
        if ticket:
            from performance.metrics import metrics
            metrics.mark(worker.reply_id,'vision_done',ms=(monotonic()-ticket.created)*1000)
            self.scheduler.finish(ticket)
        if worker in self._retired_workers:
            self._retired_workers.remove(worker)
            worker.deleteLater()
            self._start_next_voice()
        elif self._worker is worker:
            self._on_worker_finished()

    def _context_for_request(self) -> str:
        now = monotonic()
        while self._recent_turns and now - self._recent_turns[0][0] > RECENT_CONTEXT_SECONDS:
            self._recent_turns.popleft()
        return "\n".join(f"用户：{command}\nAtivia：{reply}" for _, command, reply, _ in self._recent_turns)

    @running_only
    def _on_analysis_failed(self, message: str) -> None:
        if self._worker is not None and self._worker.automatic and (
            not self._auto or self._worker.isInterruptionRequested()
        ):
            return
        self.control.set_failure(message)
        if self._worker is not None and self._worker.trace is not None:
            self._worker.trace["delivery"] = dict(status="error", response="", error=message)
        if self._worker is not None and self._worker.mode == "learning" and not self._worker.automatic:
            if self._worker.previous_summary:
                self.control.learning_title.setText("解释失败，可点击“进一步解释”重试")
            else:
                self.control.learning_text.blockSignals(True)
                self.control.learning_text.setPlainText(message)
                self.control.learning_text.blockSignals(False)

    def _on_worker_finished(self) -> None:
        # A result signal can arrive before run() returns; only release the thread now.
        if self._worker is not None:
            trace = self._worker.trace
            if trace is not None:
                if trace["delivery"]["status"] == "pending":
                    trace["delivery"] = dict(status="cancelled", response="")
                trace["state_after"] = self._trace_state()
                self._trace.finish(trace)
                self._refresh_trace_ui()
            self._worker.deleteLater()
            self._worker = None
        self._request_in_progress = False
        self.control.set_busy(self._player.is_busy())

    def _prepare_learning_capture(self) -> None:
        if self.control.current_mode() == "learning":
            self._learning_image = None
            self._learning_summary = None
            self._learning_command = None
            self.control.clear_learning_result()

    def _on_snip(self) -> None:
        if not self._pause_gate.allows():
            self.control.show_snip_notice('助手已暂停。请先点击恢复，再按 Alt+Q 框选。');return
        if self._busy():
            self.control.show_snip_notice('当前任务正在进行，请等待完成或点击停止回答后再框选。');return
        if self.region_overlay.isVisible():
            return
        if self._busy():
            return
        self._selecting = True
        self.control.set_busy(True)
        if self.action_menu.isVisible():
            self.action_menu.hide()
        self.control.hide()
        generation=self._capture_generation
        QApplication.processEvents()
        QTimer.singleShot(80, lambda:self._begin_region_selection() if generation==self._capture_generation else None)

    @running_only
    def _begin_region_selection(self) -> None:
        try:
            self.region_overlay.begin()
        except Exception:  # noqa: BLE001
            self._on_region_cancelled()
            self.control.set_failure("框选截屏失败了，请再试一次。")
            self._trace_local("region_capture_failed", False, "框选截屏失败了，请再试一次。")

    @running_only
    def _on_region_selected(self, rect: QRect) -> None:
        self._pending_region = rect
        self.control.show() if not self.control.close_to_hide else None
        if self.control.current_mode() == "learning":
            self.action_menu.popup_near(rect.center())
            return
        self._pending_command = "请根据框选区域的画面，简短说说现在在发生什么。"
        self._start_region_observe(rect)

    def _on_region_cancelled(self) -> None:
        self._selecting = False
        self.control.set_busy(False)
        self._pending_region = None
        self.control.show() if not self.control.close_to_hide else None

    def _on_action_cancelled(self) -> None:
        self._selecting = False
        self.control.set_busy(False)
        self._pending_region = None

    @running_only
    def _on_learning_action(self, command: str) -> None:
        if self._pending_region is None:
            return
        if self._request_in_progress:
            return
        self._pending_command = command
        self._start_region_observe(self._pending_region)

    @running_only
    def _start_region_observe(self, rect: QRect) -> None:
        self._selecting = False
        self._request_in_progress = True
        self._prepare_learning_capture()
        self.control.set_busy(True)
        self.control.hide()
        generation = self._capture_generation
        QApplication.processEvents()
        QTimer.singleShot(HIDE_BEFORE_CAPTURE_MS, lambda: self._capture_region_and_ask(rect)
                          if generation == self._capture_generation else None)

    @running_only
    def _capture_region_and_ask(self, rect: QRect) -> None:
        command = self._pending_command
        self._pending_command = None
        self._pending_region = None
        try:
            image_jpeg = self.region_overlay.selected_jpeg(rect)
        except Exception:  # noqa: BLE001
            self._request_in_progress = False
            self.control.show() if not self.control.close_to_hide else None
            self.control.set_busy(False)
            self.control.set_failure("框选截屏失败了，请再试一次。")
            self._trace_local("region_capture_failed", False, "框选截屏失败了，请再试一次。",
                              dict(user_command=command, capture_scope="region"))
            return

        self.control.show() if not self.control.close_to_hide else None
        self._launch_worker(ObserveWorker(
            image_jpeg,
            None,
            None,
            self._mic.sample_rate,
            False,
            self.control.current_mode(),
            self.control.speak_enabled(),
            command,
        ), source="region")

    @running_only
    def _start_observe(self, force: bool = True, automatic: bool = False) -> None:
        if self.practice.engaged:return
        if self._busy():
            return

        self._request_in_progress = True
        if not automatic:
            self._prepare_learning_capture()
        self.control.set_busy(True)
        self.control.hide()
        epoch = self._auto_epoch
        generation = self._capture_generation
        QApplication.processEvents()
        QTimer.singleShot(
            HIDE_BEFORE_CAPTURE_MS,
            lambda: self._capture_and_ask(force=force, automatic=automatic, epoch=epoch)
                    if generation == self._capture_generation else None,
        )

    @running_only
    def _capture_and_ask(self, force: bool = False, automatic: bool = False, epoch: int | None = None, captured=None) -> None:
        if self._voice_worker is not None or self._voice_queue or self._vad.active:
            self._request_in_progress = False
            self.control.show() if not self.control.close_to_hide else None
            return
        if automatic and (not self._auto or epoch != self._auto_epoch):
            self._request_in_progress = False
            self.control.show() if not self.control.close_to_hide else None
            self.control.set_busy(False)
            return
        if automatic and captured is None:
            if self._capture_probe is not None:return
            from screen_events import CaptureProbe
            probe=CaptureProbe(capture_primary_jpeg,screenshot_hash);self._capture_probe=probe
            generation=self._capture_generation
            def ready(row):
                if generation!=self._capture_generation or not self._pause_gate.allows():return
                if row.get('error'):
                    self._request_in_progress=False;self.control.set_busy(False);return
                from screen_events import classify_change
                meta=row['metadata'];event=classify_change(self._last_hash,row['image_hash'],self._screen_meta,meta,self.companion.mode)
                self._screen_meta=meta;self.companion.application=meta.get('application','')
                legacy_voice=getattr(self._mic,'segmented_input',False) is not True and has_speech(self._mic.snapshot(),self._mic.sample_rate)
                if event=='none' and not legacy_voice:
                    self._trace_local('unchanged_screen_without_user_speech' if self._last_hash==row['image_hash'] else 'screen_metadata_gate',True)
                    self._request_in_progress=False;self.control.set_busy(False);return
                plan=self.companion.context('',event=event,legacy_mode=self.control.current_mode())
                if plan['decision']['action']=='stay_silent' and not legacy_voice:
                    self._request_in_progress=False;self.control.set_busy(False);return
                self._capture_and_ask(force,automatic,epoch,captured=row)
            def finished():
                if self._capture_probe is probe:self._capture_probe=None
                probe.deleteLater()
            probe.ready.connect(ready);probe.finished.connect(finished);probe.start();return
        command = self._pending_command
        self._pending_command = None

        try:
            image_jpeg = captured['image'] if captured else capture_primary_jpeg()
        except Exception:  # noqa: BLE001
            self._request_in_progress = False
            self.control.show() if not self.control.close_to_hide else None
            self.control.set_busy(False)
            self.control.set_failure("截屏失败了，请再试一次。")
            self._trace_local("capture_failed", automatic, "截屏失败了，请再试一次。", dict(user_command=command))
            return

        mic_samples = (np.zeros(0, dtype=np.float32) if getattr(self._mic, "segmented_input", False) is True
                       else self._mic.snapshot())
        mic_speech = (getattr(self._mic, "segmented_input", False) is not True
                      and has_speech(mic_samples, self._mic.sample_rate))

        system_samples = None
        system_active = False
        if self._system_ok and not self._player.is_busy():
            system_samples = self._system.snapshot()
            system_active = has_system_audio(system_samples, self._system.sample_rate)

        image_hash = captured['image_hash'] if captured else screenshot_hash(image_jpeg)
        unchanged = self._last_hash is not None and hashes_similar(self._last_hash, image_hash)

        self.control.show() if not self.control.close_to_hide else None

        if automatic and not force and command is None and unchanged and not mic_speech:
            self._trace_local("unchanged_screen_without_user_speech", True, context=dict(image_hash=image_hash))
            self._request_in_progress = False
            self.control.set_busy(False)
            return

        self._launch_worker(ObserveWorker(
            image_jpeg,
            mic_samples if mic_speech else None,
            system_samples if system_active else None,
            self._mic.sample_rate,
            system_active,
            self.control.current_mode(),
            self.control.speak_enabled(),
            command,
            automatic=automatic,
            image_hash=image_hash,
        ))
        if mic_speech:
            self._mic.clear()

    def _on_comment(self, text: str, audio_path: str) -> None:
        worker = self._worker
        if not self._pause_gate.allows() or (worker is not None and not worker.valid()):
            self._delete_audio(audio_path);return
        if worker is not None and worker.automatic and (
            not self._auto or worker.isInterruptionRequested()
        ):
            if audio_path:
                try:
                    os.remove(audio_path)
                except OSError:
                    pass
            return
        if worker is not None and worker.image_hash is not None:
            self._last_hash = worker.image_hash
        if worker is not None and worker.observation_recorded:
            self._character_memory = worker.character_memory
        if worker is not None and worker.trace is not None:
            worker.trace["delivery"] = dict(status="displayed" if text else "silent", response=text)
        if not text:
            return
        if worker is not None:
            if not worker.automatic or worker.user_spoken or worker.user_command:
                self._conversation.engaged()
            elif worker.mode == "entertainment":
                self._conversation.reacted(monotonic())
            command = worker.user_command or worker.user_spoken or "（观察屏幕）"
            self._recent_turns.append((monotonic(), command[:120], text[:240], _reply_key(text)))
        if worker is not None and worker.mode == "learning":
            if worker.previous_summary:
                self.control.set_learning_result(worker.previous_summary, text)
            else:
                self._learning_image = worker.image_jpeg
                self._learning_summary = text
                self._learning_command = worker.user_command
                self.control.set_learning_result(text)
        else:
            self.control.set_entertainment_result(text)
        reply_id = worker.reply_id if worker is not None else new_reply_id()
        if self.control.speak_enabled():
            if self._system_ok:
                self._system.clear()
            self._speech.submit(SpeechJob(text, worker.settings if worker else self._settings, reply_id=reply_id,
                source="automatic" if worker is not None and worker.automatic else "screen",
                serious=worker is not None and worker.mode == "learning",
                context="\n".join(filter(None, (worker.user_command, worker.user_spoken, worker.recent_context))) if worker else ""))
        else:
            speech_event(reply_id, "TEXT_READY", source="screen", characters=len(text))
            speech_event(reply_id, "TTS_CANCELLED", reason="voice_output_disabled")
        self._delete_audio(audio_path)

    @running_only
    def _on_speech_finished(self) -> None:
        self._echo_until = monotonic() + 0.3
        self._playback_metrics = None
        self._player.set_volume(self._settings.voice_volume)
        self._speaking_automatically = False
        if self._system_ok:
            self._system.clear()
        self.control.set_busy(self._request_in_progress or self._voice_worker is not None or self._speech.is_busy())
        if self._speech_error:
            self.control.status_label.setText(self._speech_error)

    def _quit(self) -> None:
        self._quitting = True
        self._pause_gate.transition('PAUSING')
        self.practice.stop()
        if self.practice.window:self.practice.window.hide()
        self.translation.stop()
        self.translation.window.hide()
        self._quitting = True
        self._pause_gate.transition('PAUSING')
        if getattr(self,'wechat',None):self.wechat.close()
        self.companion.close()
        self._gpu_monitor.stop()
        self._timer.stop()
        self._voice_timer.stop()
        self._voice_queue.clear()
        for worker in [self._worker, self._voice_worker, self._preview_worker, *self._retired_workers,*self._retired_voice]:
            if worker is not None:
                worker.requestInterruption()
        self._speech.close()
        if self._resource_change is None:
            self._mic.stop()
            self._system.stop()
        if self._hotkey is not None:
            self._hotkey.stop()
            self._hotkey = None
        self.control.hide()
        self._finish_quit_when_idle()

    def _finish_quit_when_idle(self):
        workers = [self._worker, self._voice_worker, self._preview_worker, self._capture_probe, *self._retired_workers,*self._retired_voice]
        if not self.practice.quiescent() or (getattr(self,'wechat',None) and not self.wechat.closed_finished()) or self._player.has_cleanup() is True or not self.companion.memory.close_future.done() or self.translation.worker or self._resource_change is not None or self._speech.has_workers() or any(worker is not None and worker.isRunning() for worker in workers):
            QTimer.singleShot(100, self.control, self._finish_quit_when_idle)
        else:
            QApplication.instance().quit()
