"""Rebuild statistics from raw measurements; never fill missing human ratings."""
import csv
import json
import math
from pathlib import Path
from statistics import mean

HERE=Path(__file__).resolve().parents[2]/'docs/realtime-benchmark'


def read(name): return json.loads((HERE/name).read_text(encoding='utf-8-sig'))


def stats(values):
    values=sorted(v for v in values if v is not None)
    return dict(n=len(values),mean=mean(values) if values else None,
                p95=values[math.ceil(len(values)*.95)-1] if values else None)


def main():
    data=read('ab-auto/result.json');a=data['summary']['A'];b=data['summary']['B']
    assert a['rounds']==b['rounds']==20,'Do not finalize an incomplete run'
    improvement=1-b['mean_first_audible_ms']/a['mean_first_audible_ms']
    saved=a['mean_first_audible_ms']-b['mean_first_audible_ms']
    interruption=read('interruption-probe.json')
    stop=stats([r.get('estimated_dac_stop_latency_ms') for r in interruption])
    command=stats([r.get('command_latency_ms') for r in interruption])
    gaming_delta=read('gaming-after.json')['stats']['inference_count']-read('gaming-before.json')['stats']['inference_count']
    grouped={}
    for variant in ('A','B'):
        rows=[r for r in data['rows'] if r['variant']==variant]
        breakdown={k:stats([r.get(k) for r in rows]) for k in (
            'endpoint_ms','speech_end_to_stt_final_ms','stt_final_to_first_token_ms','first_token_to_audio_ms','first_audible_ms')}
        for r in rows:
            events={}
            for e in r['events']:events.setdefault(e['event'],e['at'])
            r['stt_request_ms']=(events['stt_final']-events['stt_started'])*1000 if 'stt_final' in events else None
            r['audio_to_audible_ms']=(events['audible']-events['tts_first_audio'])*1000 if 'audible' in events else None
        breakdown['stt_request_ms']=stats([r['stt_request_ms'] for r in rows])
        breakdown['audio_to_audible_ms']=stats([r['audio_to_audible_ms'] for r in rows])
        routes={}
        for r in rows:routes[r.get('tts_backend','unknown')]=routes.get(r.get('tts_backend','unknown'),0)+1
        grouped[variant]=dict(latencies=breakdown,routes=routes,
            same_backend_local_only=stats([r['first_audible_ms'] for r in rows if r.get('tts_backend')=='local']),
            gap_count=sum(len(r.get('browser_gaps_ms',[])) for r in rows) if variant=='B' else None,
            largest_gap_ms=max([g for r in rows for g in r.get('browser_gaps_ms',[])],default=0) if variant=='B' else None)
    gates=dict(mean_reduction_at_least_25_percent=improvement>=.25,
        saving_at_least_500ms=saved>=500,p95_not_worse=b['p95_first_audible_ms']<=a['p95_first_audible_ms'],
        complete_and_not_silent=b['complete']==20 and b['silent_replies']==0 and b['failed']==0,
        interruption_p95_at_most_300ms=stop['n']==20 and stop['p95']<=300,
        gaming_no_local_inference=gaming_delta==0,
        external_speaker_double_talk_validated=False)
    output=dict(summary=data['summary'],breakdown=grouped,improvement_percent=improvement*100,
        saved_ms=saved,interruption_command_ms=command,interruption_estimated_dac_ms=stop,
        gaming_local_inference_delta=gaming_delta,gates=gates,
        subjective_fluency='not_rated_by_human',migrate_now=all(gates.values()))
    (HERE/'aggregate.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    fields=['variant','index','status','tts_backend','speech_end_to_stt_final_ms','endpoint_ms','stt_request_ms',
        'stt_final_to_first_token_ms','first_token_to_audio_ms','audio_to_audible_ms','first_audible_ms',
        'silent_reply','gpu_peak_percent','vram_peak_mb','error']
    with (HERE/'ab-rounds.csv').open('w',encoding='utf-8-sig',newline='') as file:
        writer=csv.DictWriter(file,fieldnames=fields,extrasaction='ignore');writer.writeheader();writer.writerows(data['rows'])
    def seconds(ms):return f'{ms/1000:.2f}'
    stage_lines=[]
    for title,key in [('尾静音/端点等待','endpoint_ms'),('speech end → STT final','speech_end_to_stt_final_ms'),
                      ('实际STT请求','stt_request_ms'),('STT final → first LLM token','stt_final_to_first_token_ms'),
                      ('first LLM token → first TTS audio','first_token_to_audio_ms'),('有效音频 → 设备首音','audio_to_audible_ms')]:
        values=[grouped[v]['latencies'][key]['mean'] for v in ('A','B')]
        stage_lines.append(f'- {title}：A {seconds(values[0])}秒，B {seconds(values[1])}秒。')
    report=f'''# Ativia 实时语音审计与原型验收

结论：本轮不迁移现有Ativia。独立原型首音平均改善{improvement*100:.1f}%，但未通过全部预设门槛，尤其是外放双讲和误打断验收。保留原型继续验证，不扩大到屏幕、人格、记忆或动画迁移。

## 当前架构问题

最主要的三个问题是：LLM流式token被积累到完整回复后才交给TTS、TTS整段下载完成才播放；600ms端点等待后才上传完整语音进行STT；播放期间echo_guard直接丢弃麦克风，无法语音打断。多个Qt线程、旧回复保护和分离队列使这些屏障更难维护。[完整审计和时序图](01-current-architecture.md)。

屏幕小字受1280宽/JPEG压缩影响，语音使用旧屏幕缓存；系统声音是loopback语音转写，不是持续环境声理解。它们应按需调用，继续离开基本对话关键路径。

## 最终交替A/B：各20轮

- A旧Ativia：平均首音 **{seconds(a['mean_first_audible_ms'])}秒**；P95 **{seconds(a['p95_first_audible_ms'])}秒**；完整播放{a['complete']}/20；silent {a['silent_replies']}/20；失败{a['failed']}/20。
- B独立原型：平均首音 **{seconds(b['mean_first_audible_ms'])}秒**；P95 **{seconds(b['p95_first_audible_ms'])}秒**；完整播放{b['complete']}/20；silent {b['silent_replies']}/20；失败{b['failed']}/20。
- 平均节省 **{seconds(saved)}秒（{improvement*100:.1f}%）**。P95使用nearest-rank；20样本不能证明统计显著性。
- GPU整机峰值：A {a['gpu_peak_percent']:.0f}%，B {b['gpu_peak_percent']:.0f}%；VRAM峰值：A {a['vram_peak_mb']:.1f}MiB，B {b['vram_peak_mb']:.1f}MiB。
- B设备时钟估算的语音打断：均值{stop['mean']:.1f}ms、P95 **{stop['p95']:.1f}ms**；20/20输出源停止。JS停止命令P95 {command['p95']:.1f}ms。A的echo_guard控制测试20/20丢弃插话，记“不支持”，无伪造时延。
- 主观流畅度：**未获得真人评分**。设备时钟、断句停顿与故障记录不能代替听感。

阶段平均延迟：

{chr(10).join(stage_lines)}

注意“speech end → STT final”已经包含端点等待，不能与端点耗时重复相加。A的first-token时间来自原生产计时，映射到STT final后，少量提示构造时间未计入；端到端首音独立实测不受这个近似影响。

实际路由：A {json.dumps(grouped['A']['routes'],ensure_ascii=False)}；B {json.dumps(grouped['B']['routes'],ensure_ascii=False)}。B本地GPU保护可能在后续分句切到云端，因此有同一回复换声的听感风险。不要把不同路由的全部收益归因于架构；分组数据见aggregate.json。LLM输出未固定随机种子，语句长度会自然变化。

B记录到{grouped['B']['gap_count']}次超过100ms的分句供应间隙，最大{grouped['B']['largest_gap_ms']:.0f}ms。这是输出队列不足的诊断值，不包含原始音频里的自然停顿。

慢样本没有剔除：B第13和16轮在首个LLM token前分别等待9.36秒和12.85秒。原型把token→首段音频均值从3.01秒缩短到1.50秒，但这部分收益被更长的端点等待与LLM首token长尾抵消。记录只能定位到请求/首token等待，不能区分网络与服务端内部原因。

## Open-LLM-VTuber对比

值得复用的是成熟Silero/MicVAD、异步分句器、会话取消与播放确认的接口思路、角色/服务配置分离。已克隆官方前后端到独立目录并固定提交；原型直接复用MIT分句器且SHA256与官方文件一致。

Ativia现有的自定义音色、GPU准入、单推理worker、音频校验、OpenAI回退、人格及按需框选/系统loopback应保留。不要照搬上游并行TTS、失败时发送无声字幕包、仅HTTP200校验、完整文件式OpenAI播放，也不要移植Live2D/多角色/直播功能。若以其为基础仍需重做这些接口，不能视为换框架即完成优化。[详细五项回答](03-open-llm-vtuber.md)。

## 原型与现有音色

独立原型只实现麦克风/AEC→CPU VAD→STT→流式LLM→分句串行TTS→PCM播放→打断。原人格与模型未修改；本地音色不训练、不重载权重。原服务实测最大并发1，模型加载计数1。

先完成云端20/20基础验收，再接入本地音色3/3；三类本地200无效音频均真实回退OpenAI并播放成功。启用本地适配器后的Gaming验收，本地推理计数差 **{gaming_delta}**，合成前就选择云端。CPU VAD与云端路由不意味着整机GPU为0；本次未运行真实游戏帧率对照，不能承诺已验证FPS影响。[原型说明](04-prototype.md)、[音色与回退验证](05-voice-integration.md)。

外放AEC实测使麦克风回放RMS约降低11.6dB，语音帧从74/189降到5/189，但仍有1次疑似起声，且没有真人双讲。此数字受环境噪声及自动增益影响，不能当作实验室回声抑制率。

## 迁移判断与下一步

预先门槛见PROTOCOL.md；逐项布尔值见aggregate.json。平均改善≥25%：{gates['mean_reduction_at_least_25_percent']}；节省≥500ms：{gates['saving_at_least_500ms']}；P95不恶化：{gates['p95_not_worse']}；20轮完整无silent：{gates['complete_and_not_silent']}；打断P95≤300ms：{gates['interruption_p95_at_most_300ms']}；Gaming零本地推理：{gates['gaming_no_local_inference']}；外放双讲完成：False。

不进入完整迁移。下一轮只在独立原型中处理外放误触发/真人双讲、缩短STT与端点等待、改善分句间隙和保护时换声，再跑同一套20轮。只有全部门槛通过并获得实际听感反馈后，才计划先替换语音会话内核，再以只读接口接回人格、按需屏幕和系统音频摘要；主动发言最后接入。

## 复现与边界

开始工作前基线已提交7a0d695，分支codex/realtime-prototype；现有应用业务文件未修改。新增experiments/realtime_prototype、tools/realtime_benchmark、docs/realtime-benchmark；根.gitignore增加独立上游目录和当前交互服务日志的忽略项。完整环境、日志、配置、20条固定SAPI语音及逐轮数据均保留；可能含后续私人对话的交互日志只留本机，不加入版本库。

本次A/B为固定合成录音的VAD重放 + 真实OpenAI STT/LLM/TTS、本地模型和真实输出设备计时，不是40轮真人麦克风对话。测量不含说话前麦克风启动；尾静音等待计入首音。A/B同模型{data['setup']['model']}、同原提示和预算、同保存音色/语速设置，Auto模式。没有上传私人训练音频。NVML以100ms采样整机GPU，可能漏掉更短的峰值。首音以有声PCM采样达到输出设备时钟为证据，不宣称麦克风声学首音测量。

收尾检查发现Web Audio的onended表示渲染结束，设备可能仍有约50ms尾缓冲。最终原型改为等待输出时钟到达最后PCM帧才确认完成；播放错误立即取消剩余任务并保留failed分类，并限定VAD加载WASM-only构建。A/B原始首音计时未改写，当时轮间等待1秒，未观察到截断；最终补测`b-final-smoke/result.json`两轮均收到“output timestamp reached final PCM end”证据。`stage6-focused-tests.log`19项测试通过。原始217项回归已在第一阶段通过，旧应用业务代码此后未修改。

启动：双击仓库内experiments/realtime_prototype/start-prototype.bat，用Edge/Chrome打开127.0.0.1:9882；点击开始后才开启麦克风。关闭用stop-prototype.bat。详见原型README。原Ativia继续按原方式运行。

原始数据：[逐轮JSON](ab-auto/result.json)、[CSV](ab-rounds.csv)、[分项统计](aggregate.json)、[初始A基线](a-baseline/result.json)、[云端20轮](b-cloud-foundation/result.json)。重新生成统计：`.venv\\Scripts\\python.exe tools/realtime_benchmark/report.py`。
'''
    (HERE/'REPORT.md').write_text(report,encoding='utf-8')
    print(json.dumps(output,ensure_ascii=True))


if __name__=='__main__':main()
