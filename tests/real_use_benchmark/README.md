# Iteration 4 固定验收场景

scenarios.json 列出 10 个用户场景和可执行的自动契约。自动测试不等于真人麦克风、游戏、外放或人工人格验收。

运行全部离线契约：

    .\.venv\Scripts\python.exe -m unittest discover -s tests

30 条真实模型回复（使用现有 .env 模型与 Key；不启用 TTS、不截图、不录音）：

    .\.venv\Scripts\python.exe tools\iteration4_benchmark.py --cloud

40 条已标注 Memory 样本及相关性检查：

    .\.venv\Scripts\python.exe tools\iteration4_benchmark.py --memory

human-review.md 是待人工评分表。未经人工评分不得报告“自然度提升率”或“准确率”。
