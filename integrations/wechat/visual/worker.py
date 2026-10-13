"""Isolated input-free visual worker. Anonymous pipes only; no keys, model or Memory."""
from __future__ import annotations
import argparse,hashlib,json,math,os,queue,sys,threading,time
from dataclasses import asdict
import psutil
from .window import Win32Windows
from .capture import WindowCaptureProvider,CaptureUnavailable
from .ocr import LocalOCR
from .types import Rect,WindowIdentity,VisualFrame,TARGET_GROUP
from .binding import BindingManager
from .tracker import VisualMessageTracker
from .layout import Layout,OCRRow,recognize_header,scan_dark_bubbles,extract_messages,detect_layout
from .self_capture import SelfTestCapture,find_self_candidates,normalize_self_text,self_test_text,validate_self_token
from .desktop import probe_desktop

MAX_PACKET=65536
ALLOWED_COMMANDS=frozenset(('start','confirm','pause','resume','stop','inspect_send','end_send'))

def identity_record(window):
    # JSON pipes turn tuples in WindowSnapshot.data() into lists. Canonicalize
    # geometry on both sides; a genuine UI confirmation must survive IPC.
    result={k:window[k] for k in ('hwnd','pid','process_started','dpi')}
    result['client_rect']=tuple(window['client_rect'])
    result['client_screen']=tuple(window['client_screen'])
    return result

def same_confirmed_window(window,confirmation):
    try:
        return (isinstance(confirmation,dict) and confirmation.get('bottom_confirmed') is True and
                identity_record(window)==identity_record(confirmation['window']))
    except (KeyError,TypeError,ValueError):return False

def matches_confirmation(window,confirmation):
    try:
        stamp=float(confirmation['confirmed_at']);age=time.time()-stamp
        return math.isfinite(stamp) and 0<=age<1800 and same_confirmed_window(window,confirmation)
    except (KeyError,TypeError,ValueError):return False

def parse_command(line):
    if len(line)>MAX_PACKET:raise ValueError('IPC_packet_too_large')
    command=json.loads(line)
    if not isinstance(command,dict) or command.get('action') not in ALLOWED_COMMANDS:raise ValueError('IPC_command_not_allowed')
    if set(command)-{'action','paused','confirmation','calibration','send_request','reset_id'}:raise ValueError('IPC_unknown_fields')
    if 'reset_id' in command and (command['action'] not in ('pause','resume','confirm') or
            not isinstance(command['reset_id'],str) or not 1<=len(command['reset_id'])<=64):raise ValueError('IPC_reset_id')
    if command['action'] in ('inspect_send','end_send'):
        row=command.get('send_request')
        if not isinstance(row,dict):raise ValueError('send_observation_request_required')
        if command['action']=='inspect_send':
            if set(row)!={'request_id','transaction_id','binding_id','capture_session_id','capture_epoch','phase'}:raise ValueError('send_observation_fields')
            if any(not isinstance(row[k],str) or not row[k] or len(row[k])>256 for k in ('request_id','transaction_id','binding_id','capture_session_id')):raise ValueError('send_observation_identity')
            if type(row['capture_epoch']) is not int or row['capture_epoch']<0 or row['phase'] not in ('PREPARE','FOCUSED','PRE_SUBMIT','VERIFY'):raise ValueError('send_observation_phase')
        elif set(row)!={'transaction_id'} or not isinstance(row['transaction_id'],str):raise ValueError('send_end_identity')
    return command

def run(args):
    # This worker must not inherit provider credentials, even accidentally.
    if any(k.startswith(('OPENAI','ANTHROPIC','AZURE_OPENAI')) for k in os.environ):raise RuntimeError('worker_has_model_credentials')
    self_token=getattr(args,'self_test_token',None);self_issued=getattr(args,'self_test_issued_at',None)
    if (self_token is None)!=(self_issued is None):raise ValueError('self_test_token_and_issue_time_required_together')
    self_capture=None if self_token is None else SelfTestCapture(self_token,self_issued,clock=time.time,monotonic=time.monotonic)
    parent=psutil.Process(args.parent_pid)
    if abs(parent.create_time()-args.parent_start)>.001:raise RuntimeError('parent_identity_mismatch')
    worker_process=psutil.Process();worker_process.cpu_percent(None)  # Prime once; sample the same process object.
    commands=queue.Queue(maxsize=16);stop=threading.Event();pause=threading.Event();pause.set()
    def emit(stage,**values):
        sys.stdout.write(json.dumps(dict(stage=stage,**values),ensure_ascii=False)+'\n');sys.stdout.flush()
    def reader():
        try:
            for line in sys.stdin:
                try:command=parse_command(line)
                except (ValueError,TypeError):continue
                if command['action']=='stop':stop.set();return
                if command['action']=='pause':pause.set()
                try:commands.put_nowait(command)
                except queue.Full:stop.set();return
        finally:stop.set()
    threading.Thread(target=reader,daemon=True).start()
    first=commands.get(timeout=10)
    if first['action']!='start':raise RuntimeError('start_required')
    if first.get('paused',True) is False:pause.clear()
    confirmation=first.get('confirmation');calibration=first.get('calibration')
    windows=Win32Windows();provider=None;ocr=None;binding=BindingManager();tracker=VisualMessageTracker()
    epoch=0;sequence=0;last_time=None;last_hash=None;last_messages=();last_ocr_at=0;last_status='';ready=False;accepted_confirmation=False
    target=None;layout=None;invalidations=0;began=time.monotonic();last_heartbeat=0;last_self_candidates=()
    send_transaction=None;send_pending=None;send_deadline=0
    def invalidate(reason,*,forget_confirmation=True,reset_id=None):
        nonlocal epoch,last_hash,last_messages,last_time,confirmation,accepted_confirmation,ready,invalidations,last_self_candidates,last_status
        nonlocal send_transaction,send_pending
        send_transaction=send_pending=None
        epoch+=1;binding.invalidate(reason);tracker.reset(reason);last_hash=None;last_messages=();last_time=None;ready=False
        last_self_candidates=();last_status=''
        if self_capture is not None:self_capture.invalidate()
        if forget_confirmation:confirmation=None;accepted_confirmation=False
        invalidations+=1;emit('unbound',reason=reason,capture_epoch=epoch,reset_id=reset_id)
    try:
        while not stop.is_set():
            if not parent.is_running() or abs(parent.create_time()-args.parent_start)>.001:break
            while True:
                try:command=commands.get_nowait()
                except queue.Empty:break
                action=command['action']
                if action=='pause':invalidate('paused',reset_id=command.get('reset_id'));pause.set()
                elif action=='resume':invalidate('resumed',reset_id=command.get('reset_id'));pause.clear()
                elif action=='confirm':
                    confirmation=command.get('confirmation');accepted_confirmation=False
                    if command.get('calibration'):calibration=command['calibration'];layout=None
                    invalidate('manual_confirmation',forget_confirmation=False,reset_id=command.get('reset_id'))
                elif action=='inspect_send':
                    row=command['send_request'];bound=binding.binding
                    if (not getattr(args,'send_inspection',False) or self_capture is not None or pause.is_set() or not bound or
                            row['binding_id']!=bound.binding_id or row['capture_session_id']!=args.session or row['capture_epoch']!=epoch):
                        emit('send_observation_rejected',reason='binding_invalid');continue
                    if send_transaction is not None and send_transaction!=row['transaction_id']:
                        emit('send_observation_rejected',reason='binding_invalid');continue
                    if send_transaction is None:
                        if row['phase']!='PREPARE':emit('send_observation_rejected',reason='binding_invalid');continue
                        send_transaction=row['transaction_id'];send_deadline=time.monotonic()+30
                    send_pending=row
                elif action=='end_send':
                    if send_transaction==command['send_request']['transaction_id']:
                        send_transaction=send_pending=None;last_hash=None;last_time=time.monotonic()
            now=time.monotonic()
            if now-last_heartbeat>1:
                emit('heartbeat',paused=pause.is_set(),uptime_seconds=round(now-began,2));last_heartbeat=now
            if pause.is_set():stop.wait(.1);continue
            if not probe_desktop().available:
                if last_status!='desktop_unavailable':
                    invalidate('desktop_unavailable');emit('blocked',reason='desktop_unavailable');last_status='desktop_unavailable'
                stop.wait(.2);continue
            if send_transaction is not None and now>send_deadline:
                invalidate('capture_gap');continue
            if self_capture is not None and self_capture.expired():
                if last_status!='self_test_expired':emit('self_test_expired');last_status='self_test_expired'
                stop.wait(.2);continue
            if provider is None:
                candidates=windows.discover()
                if len(candidates)!=1:
                    emit('blocked',reason='window_not_unique',count=len(candidates));stop.wait(1);continue
                target=candidates[0]
                if target.minimized:emit('blocked',reason='minimized_pause');stop.wait(1);continue
                provider=WindowCaptureProvider(target.hwnd,args.backend);ocr=LocalOCR(args.resource_root,provider)
            frame_started=time.monotonic()
            try:
                image,stats=provider.capture()
                current=stats['window'];w,h=image.size
                if pause.is_set() or not probe_desktop().available:continue
                if send_transaction is None and last_time is not None and frame_started-last_time>5:invalidate('capture_gap')
                if layout is None:
                    if calibration:
                        layout=Layout(*(Rect(*calibration[k]) for k in ('header','message','input')),
                                      source=calibration.get('source','calibrated')).validate(image.size)
                    else:layout=detect_layout(image)
                # The exact geometry in the human confirmation prevents stale ROI reuse.
                if accepted_confirmation and not same_confirmed_window(current,confirmation):invalidate('window_geometry_or_confirmation_changed');layout=None;continue
                header_roi=layout.header
                if pause.is_set():continue
                rows=tuple(OCRRow.from_mapping(r) for r in ocr.recognize(image,(header_roi.x,header_roi.y,header_roi.width,header_roi.height)))
                if pause.is_set():continue
                header=recognize_header(rows,layout)
                win=WindowIdentity(current['hwnd'],current['pid'],str(current['process_started']),
                    Rect(*current['client_screen'],current['client_rect'][2],current['client_rect'][3]),current['dpi'])
                sequence+=1;observed=time.time()
                frame=VisualFrame(args.session,epoch,win,w,h,observed,header.title,header_complete=header.complete,
                                  frame_sequence=sequence,layout_stable=header.exact_match,at_bottom=False)
                observation=binding.observe(frame)
                if not header.exact_match:
                    if accepted_confirmation:invalidate('target_header_changed')
                    emit('blocked',reason=header.reason);stop.wait(.5);continue
                # Only header/layout may be processed before explicit session binding.
                if binding.binding is None:
                    emit('candidate',window=current,header=header.metadata(),layout=layout.metadata(),
                         confirmation_required=True,status=observation.status)
                    if observation.status=='READY_FOR_CONFIRMATION' and matches_confirmation(current,confirmation):
                        binding.confirm(frame);accepted_confirmation=True
                        emit('group_bound',binding_id=binding.binding.binding_id,capture_session_id=args.session,capture_epoch=epoch,
                             window=current,layout=layout.metadata(),group_identity_quality='manual_session_binding')
                    else:stop.wait(.4);continue
                if send_transaction is not None:
                    # An observation-only lease, never permission to input. Normal
                    # inbound delivery waits while the main process owns sending.
                    # Header, native identity, geometry and pause guards still run.
                    if send_pending is not None:
                        from .send_observe import send_observation
                        request=send_pending;send_pending=None
                        observation=send_observation(image,current,header,request,ocr,windows,time.time())
                        if not pause.is_set() and binding.binding is not None:emit('send_observation',observation=observation)
                    last_time=frame_started
                    stop.wait(.1);continue
                # Conservative interaction interlock: native mouse/foreground reads only.
                # User navigation invalidates bottom knowledge; leaving is not confirmation.
                from ctypes import wintypes as W
                import ctypes
                point=W.POINT();position_known=bool(windows.u.GetCursorPos(ctypes.byref(point)))
                screen_x,screen_y=current['client_screen'];message=layout.message
                hover=(not position_known or (screen_x+message.x<=point.x<screen_x+message.right and
                       screen_y+message.y<=point.y<screen_y+message.bottom and windows.pointer_over_window(current['hwnd'])))
                if current['foreground'] or stats.get('target_foreground_after',False) or hover:
                    invalidate('manual_interaction_bottom_uncertain');stop.wait(.3);continue
                crop=image.crop((message.x,message.y,message.right,message.bottom))
                digest=hashlib.sha256(crop.tobytes()).hexdigest();ocr_ms=0.0;scan_metadata=[]
                changed=digest!=last_hash
                if self_capture is not None:
                    # This branch follows all normal target/geometry/pause/hover
                    # guards on every frame, including after the one observation.
                    if not self_capture.captured and changed:
                        if pause.is_set():continue
                        began_ocr=time.perf_counter()
                        body_rows=tuple(OCRRow.from_mapping(r) for r in ocr.recognize(image,(message.x,message.y,message.width,message.height)))
                        self_scan=find_self_candidates(image,layout,body_rows)
                        expected=normalize_self_text(self_test_text(self_token))
                        last_self_candidates=tuple(item for item in self_scan.candidates if normalize_self_text(item.text)==expected)
                        scan_metadata=self_scan.metadata;body_rows=();self_scan=None
                        ocr_ms=(time.perf_counter()-began_ocr)*1000
                    last_hash=digest
                    if pause.is_set():continue
                    result=self_capture.observe(last_self_candidates,binding_id=binding.binding.binding_id,
                        capture_session_id=args.session,capture_epoch=epoch,frame_sequence=sequence,observed_at=observed)
                    last_time=frame_started
                    if result.observation is not None and not pause.is_set() and binding.binding is not None:
                        emit('self_test_observation',observation=result.observation)
                    if result.status!=last_status:
                        emit(result.status,reason=result.reason);last_status=result.status
                    emit('frame',sequence=sequence,status=result.status,visible_tracks=0,rejected=0,
                         changed=changed,message_count=len(last_self_candidates),capture_ms=stats['duration_ms'],
                         ocr_ms=round(ocr_ms,2),scan_candidates=len(scan_metadata),
                         cpu_percent=worker_process.cpu_percent(None),rss_bytes=worker_process.memory_info().rss,
                         cpu_scope='worker_process_one_logical_core_100_percent',source='real_self_test',
                         real_wechat_clicks=0,real_wechat_keys=0,real_wechat_pastes=0,real_wechat_sends=0)
                    # Never fall through to the incoming tracker or normal event.
                    stop.wait(max(.05,1.0-(time.monotonic()-frame_started)));continue
                if changed:
                    if pause.is_set():continue
                    began_ocr=time.perf_counter()
                    body_rows=tuple(OCRRow.from_mapping(r) for r in ocr.recognize(image,(message.x,message.y,message.width,message.height)))
                    scan=scan_dark_bubbles(image,layout,body_rows);extraction=extract_messages(body_rows,scan.detections,layout,image.size)
                    last_messages=extraction.messages;scan_metadata=scan.metadata;last_hash=digest;last_ocr_at=frame_started
                    ocr_ms=(time.perf_counter()-began_ocr)*1000
                if pause.is_set():continue
                frame=VisualFrame(args.session,epoch,win,w,h,observed,header.title,last_messages,
                                  at_bottom=True,layout_stable=True,frame_sequence=sequence,header_complete=True)
                result=tracker.process(frame,binding.binding);last_time=frame_started
                if result.status=='GAP':
                    invalidate(result.reason);continue
                if result.status=='BASELINE_COMPLETE':emit('baseline_complete',visible_tracks=result.visible_tracks,history_delivered=0)
                elif result.status=='LISTENER_READY' and not ready:
                    ready=True;emit('listener_ready',visible_tracks=result.visible_tracks)
                for event in result.events:
                    if not pause.is_set() and binding.binding is not None:emit('event',event=asdict(event))
                emit('frame',sequence=sequence,status=result.status,visible_tracks=result.visible_tracks,rejected=result.rejected,
                     changed=changed,message_count=len(last_messages),
                     capture_ms=stats['duration_ms'],ocr_ms=round(ocr_ms,2),scan_candidates=len(scan_metadata),
                     cpu_percent=worker_process.cpu_percent(None),rss_bytes=worker_process.memory_info().rss,
                     cpu_scope='worker_process_one_logical_core_100_percent',
                     source='real_window',real_wechat_clicks=0,real_wechat_keys=0,real_wechat_pastes=0,real_wechat_sends=0)
            except (CaptureUnavailable,ValueError,RuntimeError,psutil.Error) as exc:
                invalidate(type(exc).__name__);emit('blocked',reason=type(exc).__name__)
                if provider:provider.close()
                provider=None;ocr=None;layout=None;stop.wait(.5)
            stop.wait(max(.05,1.0-(time.monotonic()-frame_started)))
    finally:
        if provider:provider.close()
        emit('stopped',invalidations=invalidations,real_wechat_clicks=0,real_wechat_keys=0,real_wechat_pastes=0,real_wechat_sends=0)

def main():
    p=argparse.ArgumentParser();p.add_argument('--parent-pid',type=int,required=True);p.add_argument('--parent-start',type=float,required=True)
    p.add_argument('--resource-root',required=True);p.add_argument('--backend',choices=WindowCaptureProvider.BACKENDS,default='win32_printwindow')
    p.add_argument('--session',required=True);p.add_argument('--self-test-token');p.add_argument('--self-test-issued-at',type=float)
    p.add_argument('--send-inspection',action='store_true');args=p.parse_args()
    if args.send_inspection and args.self_test_token:raise ValueError('self_test_cannot_send')
    if not args.session or len(args.session)>128:raise ValueError('invalid_session')
    if args.self_test_token is not None:validate_self_token(args.self_test_token)
    run(args)

if __name__=='__main__':
    try:main()
    except Exception as exc:
        # Error types only; never echo provider exceptions containing OCR text.
        print(json.dumps(dict(stage='fatal',error_type=type(exc).__name__)),flush=True);raise SystemExit(1)
