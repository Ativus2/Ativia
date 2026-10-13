# Ativia

Windows 桌面 AI 助手，使用 Python / PySide6。包含文字对话、桌宠、截图问答、语音、实时翻译、记忆，以及外语口语陪练。

这是 2026-10-13 的源码快照，需要自行安装依赖和配置 API。不是打包后的独立程序。

## 快速开始

建议使用 Python 3.12。在项目目录执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

在 `.env` 中填写自己的 `OPENAI_API_KEY`，并把 `OPENAI_MODEL` 设置为账户可用的模型。不要将 `.env` 提交到仓库。

只启动外语口语陪练：

```powershell
.\.venv\Scripts\python.exe -m language_practice.launch
```

也可双击 `start-language-practice.bat`。这个入口可以单独使用，不需要启动微信功能。

启动文字助手窗口：

```powershell
.\.venv\Scripts\python.exe main.py --text-only --window
```

桌宠入口为 `start-desktop-pet.bat`。纯文字环境也可使用 `setup-text-only.bat` 和 `start-text-only.bat`；口语录音请安装上面的完整依赖。

## 外语口语陪练

- 支持韩语、英语、日语，可选择话题与难度。
- Ativia 提问，显示“我可以怎么回答”的第一人称参考字幕。参考回答不会朗读。
- 问题可选择朗读；练习者自己念参考回答或自由回答。
- 录音后先查看、修改识别文字，确认后生成衔接追问；也可直接输入回答。
- 追问依据实际提交的回答生成，参考字幕不会被当作已经说过的话。
- 支持简化参考回答、显示或隐藏中文和练习计时。单次录音上限 90 秒，练习不会在 5 分钟时自动结束。

详见 [口语练习说明](docs/language-practice.md)。

## 目录

| 目录 | 内容 |
| --- | --- |
| `language_practice/` | 外语口语陪练 |
| `translation/` | 实时翻译和识别后端 |
| `memory/`、`personality/` | 记忆服务与人格配置代码 |
| `integrations/wechat/` | 微信集成实验代码 |
| `experiments/` | 桌宠、原生语音与实时语音原型 |
| `local_tts/` | 本地语音工具代码，需要另行配置模型和运行环境 |
| `tests/` | 离线测试与合成夹具 |

微信开发目前暂停。相关代码保留，上传副本中的私人群名和账号已换成示例值。不要把离线测试结果当作真实微信收发验证；使用前需自行配置并验证目标窗口。恢复诊断入口的示例配置默认关闭发送、语音和模型调用。

## 测试

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_language_practice.py -v
```

`run-tests.bat` 可运行项目测试集合。部分实验需要额外的可选依赖；实时接口或设备脚本应单独按需运行。

## 本快照的范围

保留源码、依赖声明、应用素材、测试和第三方许可证。排除 API 密钥、本机配置、聊天与学习记录、截图和诊断证据、日志、缓存、虚拟环境、模型权重及独立上游研究副本。本机原项目和运行数据保持原样。

第三方代码来源和许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
