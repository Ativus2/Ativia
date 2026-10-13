# Ativia 独立实时语音原型

这是比较实验，不替换桌宠。旧 Ativia 的人格、观察、记忆、模型与配置不变。

## 本轮固定云端诊断版

双击 `start-diagnostic.bat`，打开本机9883页面。此入口固定原LLM模型的`reasoning_effort=none`与OpenAI `gpt-4o-mini-tts / nova / soft / 1.1`，关闭Auto及口癖随机后处理，仅影响这个诊断进程。不会写回旧Ativia配置，也没有永久改聊天模型。页面可切换“实时上传转写”和“完整录音转写（对照）”；切换会关闭麦克风，再点开始即可。

实时输入使用官方`gpt-live-transcribe` WebSocket，在说话期间传音频。仅最终转写触发一次LLM；部分转写只展示。保留原Silero/AEC阈值与全部句尾音频，错误会明确显示。该通路仍需真人外放双讲与听感验收。

试听：暂停旧助手收音与主动发言，使用平时外放音量；问一个短问题，再在回答中途插话；保持安静检查回声误打断；用带停顿的长句检查漏字。页面下方“试听反馈”分别标记外放/耳机并保存本地`logs/human-feedback.jsonl`。没有反馈不会算验收通过。

本轮协议、原始数据、30条LLM回答和40轮语音回答另存`docs/quick-dialogue-diagnostic/`；历史`docs/realtime-benchmark/`不变。研发首音目标3秒/5秒不替代旧迁移标准。

## 本机运行

双击 `start-prototype.bat`，或在仓库根目录执行：

```powershell
experiments\realtime_prototype\.venv\Scripts\python.exe experiments\realtime_prototype\server.py --enable-local
```

用 Edge / Chrome 打开 http://127.0.0.1:9882，点击“开始语音对话”，允许麦克风。服务只监听本机。结束时关闭麦克风；命令行启动的服务用Ctrl+C停止，双击启动的后台服务用`stop-prototype.bat`停止。只测试云端时去掉 `--enable-local`。同时使用旧助手会造成两个应用收音/发声，正式试听请先暂停旧助手的麦克风和主动发言。

外放使用浏览器回声消除、降噪、自动增益与 CPU Silero V5。检测到有效起声会立即停止 Web Audio 输出并取消当前轮次。设备是否启用 AEC 显示在页面底部；启用不等于已通过真人双讲验收。

原型复用当前 `.env` 中的密钥和 LLM 模型，以及已保存的音色、语速、音量、口癖、GPU 阈值。页面模式仅用于本次会话，不写回旧配置。Auto 在 GPU 允许时调用原 GPT-SoVITS 服务（9881），否则直接使用 OpenAI；游戏模式始终使用 OpenAI。需要先按原方式启动本地音色服务；不可用会回退。没有训练或修改权重。

## 范围和结构

- `web/app.js`：MicVAD、浏览器 AEC、设备时钟 PCM 播放、即时打断。
- `server.py`：本机 WebSocket、唯一轮次、取消所有权、播放确认、短会话上下文。
- `core.py`：同一 async 客户端；Whisper 整段 STT → 流式 LLM → 上游分句器 → 有界队列 → 单 TTS worker → PCM。
- `vendor/sentence_divider.py`：官方 Open-LLM-VTuber MIT 源码，未修改。
- 不包含主动观察、屏幕评论、长期记忆、学习模式、游戏人格或动画。

输入可选whisper-1完整音频转写或gpt-live-transcribe实时上传转写。云端音频采用PCM增量早播，本地音色按句生成完整WAV后校验。常规GPU准入按整轮决定，资源保护/真实故障可明确降级到云端，后续分句保持降级；不重播已播放分句，只有一个本地推理worker。任何错误都显示在页面并记录，播放排空之前不宣布完成。流式云端中途断线不能撤回已播放部分，会标记失败并停止剩余输出。

`logs/turns.jsonl`包含本机对话与计时，已加入忽略规则。实际训练数据不会由原型上传；用户说话按选择的STT方案交给OpenAI转写，使用HTTPS官方SDK或官方Realtime WSS接口。浏览器VAD模型与脚本从本机加载。

## 重现测试

本机 `.venv` 已准备；原型环境借用原 Ativia 的 Py3.14 包路径，并仅在独立环境追加 aiohttp/loguru/pysbd/langdetect。此 `.pth` 是本机开发便利措施，不是可复制发行包；换机器应按 `environment.txt` 安装独立环境。JS 依赖由 `package.json` 与 `pnpm-lock.yaml` 固定。

仓库根目录：

```powershell
experiments\realtime_prototype\.venv\Scripts\python.exe -m unittest experiments.realtime_prototype.test_core experiments.realtime_prototype.test_upstream
.venv\Scripts\python.exe tools/realtime_benchmark/run.py --variants A,B --count 20 --label my-ab-run --mode auto
.venv\Scripts\python.exe tools/realtime_benchmark/behavior_probes.py --kind interruption
.venv\Scripts\python.exe tools/realtime_benchmark/behavior_probes.py --kind echo
```

每次使用新的 label，避免覆盖。时延测试播放固定合成录音；回声探针会开启真实麦克风并播放固定语音，只保存音量/检测概率汇总，不转写或上传麦克风内容。不要同时运行两种声学测试。完整协议和结果位于 `docs/realtime-benchmark/`。
