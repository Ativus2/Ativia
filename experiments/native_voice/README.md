# Ativia 原生语音对照

独立的官方 Agents SDK + WebRTC 页面；不是旧 Ativia 的迁移版。固定 `gpt-realtime-2.1-mini` / `marin`，没有文字模型、外部 STT 或独立 TTS、屏幕、记忆、主动观察、工具、训练上传和自动重连。

## Windows 启动

2026-09-12 增加桌宠共用入口：根目录 `start-desktop-pet.bat` 首次只显示小人物；`start-native-voice-panel.bat` 打开/复用同一实例的独立语音页面；`stop-desktop-pet.bat` 结束本次启动器及其服务和专用 Edge。使用桌宠时不要再并行运行本目录 start.bat。完整说明见 `../desktop_pet/README.md`。

本机双击本目录 `start.bat`，打开 **http://127.0.0.1:9884**，点击“开始语音”后才申请麦克风。当前页面已构建。先自行暂停其他助手实例；此页的暂停只控制此页。

- 停止当前回答：官方 SDK interrupt，保留麦克风和当前连接。
- 完全暂停：先禁止新工作，再停止应用持有的音轨、远端音轨、audio 元素和 SDK/RTCPeerConnection。等待中的浏览器授权不能强行关闭，保持“正在暂停”，授权返回后立即释放。失败不伪报暂停。
- 恢复：仅点击按钮，新建连接；没有语音恢复口令。
- 结束：释放本页资源；关闭页面也执行清理。独立运行本目录 start.bat 时，先结束页面，再在服务终端按 Ctrl+C。新增本机控制通道失联后浏览器会关闭自身音轨和 WebRTC（心跳兜底约 7 秒），不能将网络连接丢失瞬间说成声学上已经停止。
- 每次服务启动最多 3 次连接尝试，包括失败。测试预算已用完后停止了测试代理；下次用户手动启动是自己的新测试，不自动运行。

## 配置和复现

- 长期 API key 仅从根目录 `.env` 在 Python 服务读取；没有修改原 `.env`。固定官方 `api.openai.com`，不使用旧应用的模型配置或代理路由。
- 浏览器只接收官方有效期 60 秒的临时凭据。仅绑定 127.0.0.1，POST 必须同源，日志不含凭据。
- Python 复用 `../realtime_prototype/.venv` 已装好的 aiohttp/python-dotenv，没有升级旧环境或导入旧 conversation pipeline。该路径也是 start.bat 的运行依赖。
- npm 锁定 `@openai/agents 0.18.0`，esbuild `0.25.9`，详见 package-lock.json。新 checkout 用 Node/npm 执行 `npm ci` 和 `npm run build`；本机亦可运行 `build.bat`。没有重建第三套音频调度器。
- `server.py` 只读导入 `vision.ENTERTAINMENT_PROMPT`（含原 PROFILE.introduction），原文作为 Realtime instructions，未改写人格。屏幕相关原句仍保留，但此页没有屏幕输入；不添加新的角色设定。人格 hash 由 /config 返回。
- SDK 的默认独立转写被明确设为 `transcription: null`；实际 session.created/updated 均确认 null。原生模型直接理解音频。单个连接内由 SDK/服务维护上下文，不按句重连。
- 暂停前完成的 SDK history 只保留在本页内存；不尝试重放音频历史到新连接。恢复**不保证上下文连续**，刷新页面清空本地历史。未采用不完整的文本伪造音频上下文。
- 默认不保存音频、转写或私人对话，不开启 SDK tracing。诊断事件仅在浏览器内存；代理仅保存请求状态、模型、request_id 等元数据。logs/ 和构建依赖均 gitignore。
- 此模型没有自动 fallback：故障显示错误并关闭输入，需要用户操作。旧 Ativia 的 OpenAI fallback 完整保留。

## 共用控制与单实例

桌宠/托盘与本页面通过 /bridge 共用确认状态。浏览器仅一个页面获得 voice lease；其他页面只能控制已有会话，不再采集。临时令牌端点检查 lease、generation 和暂停准入，完全暂停先封锁新令牌再清理。控制 key 是仅用于 localhost 的随机凭据，与长期 OpenAI key 不同，日志不保存它。

麦克风开关实际结束/重新取得 MediaStreamTrack；重新打开时复用现有 RTCRtpSender，不创建第二个会话。暂停前若已关麦，恢复保持无采集、无云端会话的待机，直到明确开启麦克风。第一次来自桌宠的开始操作须在浏览器确认授权。字幕独立更新；桌宠说话状态依据有声入站音频统计及正在前进的播放时钟，不依据字幕或固定延时；它仍不构成真人声学验收。

## 测试

离线：`node --test tests/lifecycle.test.js`（9 项）、Python `-m unittest discover -s tests -p test_server.py`（4 项）。`tests/browser-offline.mjs` 还验证 3 个真实页面流程，屏蔽所有 OpenAI 调用。

`tests/smoke.mjs` 和 `tests/audible_smoke.py` **会产生费用，不随启动运行**。本轮只运行 3 个会话，详见 ../../docs/pause-native-voice/RESULT.md。回环测试仅瞬时读取声卡混音并保存 RMS 数字，不保存声音。自动化输入为已有合成夹具，未打开真实麦克风，不能代替外放双讲验收。

## 真人检查（未完成）

用外放，先暂停其他助手。①短问题连续聊几句；②她说话时插话，检查是否立刻停止、是否被自身回声误打断；③自己说话、等待响应、她说话三个阶段分别完全暂停；④暂停后继续说话，确认无新回复；⑤恢复问新问题，再结束，检查麦克风指示关闭。耳机测试另写，不能替代外放。

展开页面“真人测试与反馈”，填写外放/耳机、听感、回声/误打断、暂停是否彻底、错误提示，点击保存到本机 `logs/feedback.jsonl`。只保存主动填写的内容。未收到真人结果，不能声称通过原迁移门槛。

官方依据：[原生 Realtime Quickstart](https://developers.openai.com/api/docs/guides/realtime#build-a-speech-to-speech-voice-agent)、[WebRTC 临时凭据](https://developers.openai.com/api/docs/guides/voice-webrtc?api=realtime)、[指定模型](https://developers.openai.com/api/docs/models/gpt-realtime-2.1-mini)、[原生中断](https://developers.openai.com/api/docs/guides/realtime-conversations)。2026-09-11 查验，账户和实际连接另有本机证据。
