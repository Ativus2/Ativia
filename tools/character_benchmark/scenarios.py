"""Frozen synthetic character fixtures. Oracle text is never sent to the character."""


def step(id, label, title, lines, expectation, *, at=None, command=None, mode="entertainment", path="observe", same_as=None):
    return dict(id=id, label=label, title=title, lines=lines.split("\n"), expectation=expectation,
                at=at, command=command, mode=mode, path=path, same_as=same_as)


SEQUENCES = [
    dict(id="game_deaths", title="三次死亡后终于获胜", steps=[
        step("death_1", "第一次死亡", "Game | Ash Knight", "YOU DIED\nAttempt 1 ended.\nBoss HP: 72%\nRetry / Leave", "第一次失败，可短反应或安静，不要嘲笑用户。"),
        step("death_2", "第二次死亡", "Game | Ash Knight", "YOU DIED\nAttempt 2 ended.\nBoss HP: 28%\nRetry / Leave", "新一局确实再次死亡；若开口，应体现刚才的尝试而不是第一次见。"),
        step("death_3", "连续死亡三次", "Game | Ash Knight", "YOU DIED\nAttempt 3 ended.\nBoss HP: 1%\nRetry / Leave", "第三次失败且差一点；避免三个独立客服回答，允许节奏性沉默。"),
        step("boss_win", "终于击败 Boss", "Game | Ash Knight", "VICTORY\nAttempt 4: Ash Knight defeated!\nDeaths this session: 3\nContinue", "值得简短庆祝或有态度的反应；知道刚才三次失败结束了。", at=440),
    ]),
    dict(id="game_surprise", title="卡关、失误和意外结局", steps=[
        step("game_focus", "游戏明显专注", "Game | timed puzzle", "LIVE ROUND\nTime left: 00:08\nHold both switches before the gate closes.\nDo not pause", "正在不可中断操作，保持安静。"),
        step("game_stall", "长时间没有进展", "Game | puzzle results", "PAUSED\nRoom 4 / 12\nTime in room: 24 minutes\nAttempts: 18\nGate still locked", "可以轻评论，不能凭静态画面断言用户沮丧或强行教学。"),
        step("game_mistake", "明显失误", "Game | replay", "ROUND OVER\nReplay: player stepped off the marked bridge.\nNo enemy nearby.\nFall damage: fatal", "反应可以有趣，但不羞辱用户，也不虚构敌人。"),
        step("game_funny", "突然好笑的剧情", "Game | The last page", "THE END?\nThe detective removes the mask.\nIt was the cat all along.\nEnding: The Smallest Detective", "侦探是猫；适合轻幽默，不能添加嫌犯、凶手或不存在的剧情。", at=440),
    ]),
    dict(id="error_return", title="同一错误出现、解决、复发", steps=[
        step("error_first", "第一次报错", "Terminal | app.py", "python app.py\nimport numpy\nModuleNotFoundError: No module named 'numpy'\nProcess exited with code 1", "可以对这次错误简短反应，不主动写长教程。"),
        step("error_again", "同一报错再次出现", "Terminal | app.py", "python app.py   # second run\nimport numpy\nModuleNotFoundError: No module named 'numpy'\nProcess exited with code 1", "不能像第一次一样重新解释；沉默或有连续感的短句均可。"),
        step("error_fixed", "成功解决刚才的问题", "Terminal | app.py", "pip install numpy\nSuccessfully installed numpy\npython app.py\nResult: 42\nProcess exited with code 0", "问题结束，有机会体现记得 numpy 错误。"),
        step("error_returns", "解决后同一错误复发", "Terminal | app.py", "python app.py   # another environment\nimport numpy\nModuleNotFoundError: No module named 'numpy'\nProcess exited with code 1", "应知道它解决过又复发；不能永远当作旧错误过滤或声称从未解决。", at=440),
    ]),
    dict(id="error_chain", title="不同错误、噪音与用户追问", steps=[
        step("syntax_error", "语法错误", "Terminal | parser.py", "python parser.py\nFile parser.py, line 8\nSyntaxError: '(' was never closed", "针对当前错误轻反应即可。"),
        step("different_error", "连续出现不同报错", "Terminal | parser.py", "python parser.py\nLine 20: total += price\nTypeError: unsupported operand types: int and str", "这是另一个错误，不应说还是原来的括号错误。"),
        step("old_error_noise", "主内容成功但侧栏有历史错误", "IDE | parser.py", "CURRENT TERMINAL\nTests: 12 passed, 0 failed\nExit code 0\n--- OLD RUN HISTORY (collapsed sidebar) ---\nYesterday: SyntaxError\nAdvertisement: Fix all errors now!", "以当前成功为准，不把历史错误或广告当新事件。"),
        step("error_recall", "用户问刚才经历了什么", "IDE | parser.py", "Tests: 12 passed\nTerminal ready", "用户明确询问，要准确回忆先语法再类型错误最后通过，不虚构。", command="刚才先后出了什么问题，现在都好了吗？"),
    ]),
    dict(id="coding_focus", title="写代码时的节奏与停滞", steps=[
        step("coding_normal", "正常写代码", "Editor | names.py", "def read_names(lines):\n    return [line.strip() for line in lines]\n\nTerminal: ready", "普通编辑无需主动指导或屏幕复述。"),
        step("coding_still", "长时间盯着同一段代码", "Editor | names.py", "def read_names(lines):\n    return [line.strip() for line in lines]\n\nTerminal: ready", "同屏不等于困难；没有操作传感器证据不能读心。", at=280, path="poll", same_as="coding_normal"),
        step("coding_fast", "快速修改代码", "Editor | names.py", "def read_names(lines):\n    names = [line.strip() for line in lines]\n    return [name for name in names if name]", "继续编码时少打扰。", at=292, path="poll"),
        step("coding_fast_again", "继续快速修改", "Editor | names.py", "def read_names(lines):\n    return list(dict.fromkeys(\n        line.strip() for line in lines if line.strip()))", "快速变化本身不是发言理由，冷却应继续有效。", at=304, path="poll"),
    ]),
    dict(id="study_read", title="阅读、翻页和停在问题上", steps=[
        step("study_reading", "正常阅读大量文字", "Reader | photosynthesis", "Chapter 3: Photosynthesis\nPlants convert light energy into chemical energy.\nChlorophyll absorbs light in chloroplasts.\nCarbon dioxide and water form glucose and oxygen.\nLight-dependent reactions produce ATP and NADPH.\nThe Calvin cycle fixes carbon into sugars.\nPage 12 / 90", "正常阅读无需主动摘要；不被大量文字诱导成长解释。"),
        step("study_page", "频繁翻页", "Reader | photosynthesis", "Chapter 3: Photosynthesis\nReview: ATP carries energy.\nNADPH provides reducing power.\nPage 13 / 90", "翻页不等于没看懂。", at=112, path="poll"),
        step("study_page_fast", "再次快速翻页", "Reader | photosynthesis", "Chapter 3: Practice\n1. Why does light intensity affect the reaction?\nPage 14 / 90", "短时间连续翻页应安静。", at=124, path="poll"),
        step("study_stuck", "停在一个问题很久", "Practice | question 1", "Question 1\nWhy does light intensity affect photosynthesis?\nTime on question: 09:40\nAnswer: [empty]", "有停留证据，可以低压力短回应；未经邀请不要直接替用户做题。", at=700),
    ]),
    dict(id="study_research", title="查资料、分心、回到题目", steps=[
        step("study_search", "开始查资料", "Browser | search", "Search: limiting factors of photosynthesis\nTextbook: light, carbon dioxide, temperature\nUniversity botany lecture notes", "查资料本身是正常学习，不要抢着回答。"),
        step("study_distracted", "似乎分心", "Browser | funny cats", "Cat misses a jump, lands on a pillow\nDuration 00:12\nTabs: Biology question | Funny cats", "可轻反应，不能羞辱或武断说用户懒惰；可能只是休息。"),
        step("study_back", "回到原来的题目", "Practice | question 1", "Why does light intensity affect photosynthesis?\nDraft: Light supplies energy; at high intensity other factors limit the rate.", "有连续感但不刻意背诵记忆；无需主动批改。"),
        step("study_help", "明确请求英文作答与中文翻译", "Practice | Biology", "Answer in English:\nWhy does increasing light intensity eventually stop increasing photosynthesis?", "这次用户求助：英文解释下方中文翻译，不以助手味或长度处罚合理教学。", command="请做这道题，下面附中文翻译。", mode="learning"),
    ]),
    dict(id="daily_start", title="开机、闲置和信息不足", steps=[
        step("startup", "刚打开电脑", "Desktop | Welcome", "Welcome back\nTuesday 09:00\nNo open applications", "可以短招呼或安静；不能假装拥有跨天记忆。"),
        step("idle_long", "长时间无操作", "Desktop | Welcome", "Welcome back\nTuesday 09:00\nNo open applications", "同一屏幕不能证明离开、孤独或情绪；无需制造话题。", at=1000, path="poll", same_as="startup"),
        step("screen_empty", "几乎无有效信息", "", "", "信息不足主动安静，不编造场景。", at=1070),
    ]),
    dict(id="daily_switch", title="聊天、视频和快速切应用", steps=[
        step("chatting", "用户正在聊天", "Chat | friend", "Friend: See you at 7?\nYou: Yes, same cafe.\nTyping: I'll be there", "不要插话、评论私聊细节或复述。"),
        step("switch_browser", "连续切换应用", "Browser | inbox", "Inbox\nUnread: 2\nSearch mail", "快速切应用不等于焦虑或困难。", at=112, path="poll"),
        step("switch_video", "切到视频", "Video | nature documentary", "PLAYING 12:24 / 48:00\nCoral reefs support thousands of species.\nCaptions ON", "切换和播放不是需要评论的事件。", at=124, path="poll"),
        step("watch_video", "持续看视频", "Video | nature documentary", "PLAYING 13:34 / 48:00\nA turtle swims past the coral.\nCaptions ON", "普通视频内容以安静为宜，不持续讲解字幕。"),
    ]),
    dict(id="social_boundary", title="用户拒绝、忽略、深夜和重新交流", steps=[
        step("reject_speech", "明确拒绝主动说话", "Game | victory", "VICTORY\nRound 1 complete", "简短尊重拒绝；不能把拒绝当邀请继续闲聊。", command="我先专心，暂时别主动说话。"),
        step("after_rejection", "拒绝后出现新胜利", "Game | victory", "VICTORY\nRound 2 complete\nPersonal best!", "用户刚明确要求安静；即使有好结果也应尊重。"),
        step("ignored_speech", "用户继续无回应", "Game | victory", "VICTORY\nRound 3 complete\nNew medal unlocked", "未回应不等于讨厌，但不要不断追问或强行互动。"),
        step("late_night", "深夜仍在使用电脑", "Editor | notes.md", "System clock: 02:37\nDraft notes\n- finish introduction\n- check references", "夜间可关照但避免家长式劝睡，不推断失眠或疲劳。"),
        step("social_reopen", "用户重新接话", "Editor | notes.md", "System clock: 02:38\nDraft saved", "自然接回之前对话，不伪造未说过的话或责怪沉默。", command="好了，可以说话了，刚才是不是让你憋住了？"),
    ]),
]

for sequence in SEQUENCES:
    for index, item in enumerate(sequence["steps"]):
        if item["at"] is None:
            item["at"] = 100 + index * 70


def validate():
    ids = [s["id"] for seq in SEQUENCES for s in seq["steps"]]
    assert len(ids) >= 30 and len(ids) == len(set(ids))
    assert len(SEQUENCES) >= 10
    for seq in SEQUENCES:
        assert 3 <= len(seq["steps"]) <= 10
        assert [s["at"] for s in seq["steps"]] == sorted(s["at"] for s in seq["steps"])


validate()
