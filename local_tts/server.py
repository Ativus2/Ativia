"""Persistent FP16 voice service with admission checks and ONE inference worker."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict
import gc
import io
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading
import time

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / 'vendor/GPT-SoVITS'
sys.path.insert(0, str(ROOT.parent))
from gpu_monitor import get_monitor
from gpu_policy import GpuPolicy, resource_settings
from runtime_settings import RuntimeSettings, SettingsStore

os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')
os.environ.setdefault('TOKENIZERS_PARALLELISM', 'false')
os.environ.setdefault('NLTK_DATA', str(ROOT / 'models/nltk_data'))
os.environ.setdefault('OMP_NUM_THREADS', '2')
os.environ.setdefault('MKL_NUM_THREADS', '2')
os.environ['PATH'] = str(ROOT / 'bin') + os.pathsep + os.environ['PATH']
os.chdir(VENDOR)
sys.path[:0] = [str(VENDOR), str(VENDOR / 'GPT_SoVITS')]
import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field
import uvicorn
from GPT_SoVITS.TTS_infer_pack.TTS import TTS, TTS_Config

logging.basicConfig(force=True, level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(ROOT / 'logs/service-events.log', encoding='utf-8'), logging.StreamHandler()])
log = logging.getLogger('ativia.tts')
if not torch.cuda.is_available():
    raise RuntimeError('CUDA unavailable; refusing to silently use CPU')
torch.set_num_threads(2)
torch.set_num_interop_threads(1)
MONITOR = get_monitor()
POLICY = GpuPolicy()
CONFIG = TTS_Config(str(ROOT / 'configs/inference.yaml'))
ENGINE = None  # Loading GPU weights also requires admission; no load in Gaming Mode.
LOCK = threading.Lock()
WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix='tts-inference')
STATS = dict(inference_count=0, rejected_before_inference=0, active=0, max_active=0,
             pressure_cache_releases=0, model_load_count=0, last=None, cold_load_seconds=None)


class Synthesis(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    voice: str = Field(default='dataset_voice', pattern=r'^[A-Za-z0-9_-]{1,64}$')
    language: str = 'auto'
    speed: float = Field(default=1.2, ge=0.75, le=1.35)
    resource_policy: dict = Field(default_factory=dict)


def profiles():
    return json.loads((ROOT / 'configs/voices.json').read_text(encoding='utf-8'))


def settings_for(request=None):
    saved = SettingsStore().load()
    data = asdict(saved)
    if request is not None:
        data.update({k: v for k, v in request.resource_policy.items() if k in resource_settings(saved)})
    if not data.get('tts_mode'):
        data['tts_mode'] = 'auto'
    # A queued/unsaved request cannot undo the user's newly saved protection mode.
    if saved.tts_mode in ('gaming', 'openai'):
        data['tts_mode'] = saved.tts_mode
    return RuntimeSettings.parse(data)


def admit(request=None, *, own_inference=False):
    decision = POLICY.decide(settings_for(request), MONITOR.history(), own_inference=own_inference)
    if decision.backend != 'local':
        STATS['rejected_before_inference'] += int(not own_inference)
        log.info('GPU_PROTECTED reason=%s gpu=%s vram_mb=%s game=%s', decision.reason,
                 decision.gpu_percent, decision.used_mb, decision.game)
        raise HTTPException(409, 'GPU_PROTECTED:' + decision.reason)


def trim_cache():
    try:
        gc.collect()
        if ENGINE is not None:
            torch.cuda.empty_cache()
            STATS['pressure_cache_releases'] += 1
        log.info('GPU_PRESSURE_CACHE_RELEASE model_weights_kept=true')
    finally:
        LOCK.release()


async def maintain():
    trimmed = False
    while True:
        await asyncio.sleep(2)
        if LOCK.locked():
            continue
        route = POLICY.decide(settings_for(), MONITOR.history())
        if route.backend == 'local':
            trimmed = False
        elif not trimmed and LOCK.acquire(blocking=False):
            try:
                future = asyncio.get_running_loop().run_in_executor(WORKER, trim_cache)
            except Exception:
                LOCK.release()
                raise
            await asyncio.shield(future)
            trimmed = True


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(maintain())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        WORKER.shutdown(wait=True, cancel_futures=True)
        MONITOR.stop()


APP = FastAPI(title='Ativia Local TTS', docs_url=None, redoc_url=None, lifespan=lifespan)


@APP.get('/health')
def health():
    samples = MONITOR.history()
    sample = samples[-1] if samples else None
    return dict(status='ready', project_root=str(ROOT.parent), pid=os.getpid(),
        model='GPT-SoVITS ' + CONFIG.version, model_loaded=ENGINE is not None,
        device=str(CONFIG.device), voices=list(profiles()), cuda=torch.cuda.is_available(),
        weights=str(CONFIG.vits_weights_path), method='acoustic-finetune',
        runtime=dict(batch_size=1, parallel_infer=False, inference_mode=True, fp16=CONFIG.is_half,
                     workers=1, torch_threads=2, interop_threads=1, empty_cache_per_request=False),
        stats=STATS.copy(), gpu=None if sample is None else dict(utilization=sample.utilization,
                      used_mb=sample.used_mb, total_mb=sample.total_mb, error=sample.error))


def generate(request):
    global ENGINE
    iterator = None
    started = time.monotonic()
    try:
        # Second check AFTER acquiring the one-worker gate, before any model call.
        admit(request)
        voices = profiles()
        if request.voice not in voices:
            raise HTTPException(404, 'Unknown local voice')
        profile = voices[request.voice]
        reference = (ROOT / profile['reference']).resolve()
        if not reference.is_relative_to(ROOT / 'data') or not reference.is_file():
            raise HTTPException(503, 'Missing or invalid reference audio')
        if request.language not in ('auto', 'zh', 'en', 'ko', 'ja', 'yue'):
            raise HTTPException(422, 'Unsupported language')
        if ENGINE is None:
            load_began = time.monotonic()
            ENGINE = TTS(CONFIG)
            STATS['model_load_count'] += 1
            STATS['cold_load_seconds'] = time.monotonic() - load_began
            # Release one-time FP32 -> FP16 conversion buffers, not live weights.
            gc.collect()
            torch.cuda.empty_cache()
            # The load itself can produce a high-utilization sample. Still check
            # actual games, manual protection, telemetry and VRAM after loading.
            admit(request, own_inference=True)
        language = request.language
        if language == 'auto':
            korean = bool(re.search(r'[\uac00-\ud7a3]', request.text))
            chinese = bool(re.search(r'[\u4e00-\u9fff]', request.text))
            language = 'auto' if korean and chinese else 'ko' if korean else 'zh' if chinese else 'en'
        params = dict(text=request.text, text_lang=language, ref_audio_path=str(reference),
            prompt_text=profile['text'], prompt_lang=profile.get('language', 'zh'),
            top_k=15, top_p=1.0, temperature=1.0, text_split_method='cut2', batch_size=1,
            speed_factor=request.speed, fragment_interval=.2, seed=20260910, parallel_infer=False,
            repetition_penalty=1.35, streaming_mode=False, return_fragment=True, split_bucket=False,
            empty_cache_after_request=False, reload_on_error=False)
        STATS['inference_count'] += 1
        STATS['active'] += 1
        STATS['max_active'] = max(STATS['max_active'], STATS['active'])
        torch.cuda.reset_peak_memory_stats()
        output = []
        with torch.inference_mode():
            iterator = ENGINE.run(params)
            while True:
                # Complete one fragment before beginning another, and notice a
                # newly launched game or saved manual mode at every boundary.
                admit(request, own_inference=True)
                try:
                    sr, chunk = next(iterator)
                except StopIteration:
                    break
                if not chunk.size or not np.isfinite(chunk).all():
                    raise RuntimeError('Empty or nonfinite audio')
                output.append((sr, chunk))  # CPU audio only; GPU fragments are not accumulated.
        if not output or len({sr for sr, _ in output}) != 1:
            raise RuntimeError('Missing audio or inconsistent sample rates')
        sr = output[0][0]
        data = output[0][1] if len(output) == 1 else np.concatenate([chunk for _, chunk in output])
        buffer = io.BytesIO()
        sf.write(buffer, data, sr, format='WAV', subtype='PCM_16')
        STATS['last'] = dict(elapsed_seconds=time.monotonic()-started, audio_seconds=data.size/sr,
            peak_allocated_mb=torch.cuda.max_memory_allocated()/1048576,
            peak_reserved_mb=torch.cuda.max_memory_reserved()/1048576, fragments=len(output))
        log.info('synthesis voice=%s chars=%d runtime=%s', request.voice, len(request.text), STATS['last'])
        return Response(buffer.getvalue(), media_type='audio/wav')
    except HTTPException:
        raise
    except torch.cuda.OutOfMemoryError:
        log.exception('GPU OOM; releasing unused cache, no model reload')
        if iterator is not None:
            iterator.close(); iterator = None
        gc.collect()
        torch.cuda.empty_cache()
        raise HTTPException(503, 'Local GPU memory exhausted; use OpenAI fallback')
    except Exception:
        log.exception('Local synthesis failed; model weights retained')
        raise HTTPException(500, 'Local synthesis failed; inspect service log')
    finally:
        try:
            if iterator is not None:
                iterator.close()
        finally:
            STATS['active'] = 0
            LOCK.release()


@APP.post('/tts')
async def tts(request: Synthesis):
    admit(request)
    if not LOCK.acquire(blocking=False):
        raise HTTPException(503, 'TTS busy; one inference worker only')
    try:
        future = asyncio.get_running_loop().run_in_executor(WORKER, generate, request)
    except Exception:
        LOCK.release()
        raise
    # A disconnected client cannot release the gate while CUDA is still running.
    return await asyncio.shield(future)


if __name__ == '__main__':
    uvicorn.run(APP, host='127.0.0.1', port=9881, access_log=False, workers=1)
