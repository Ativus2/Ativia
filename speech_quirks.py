"""Conservative, local text decoration for speech only; no model or API calls.

Protection applies to the whole reply so mixed prose/code is never partly rewritten.
Seriousness is a rule-based guard plus an explicit caller override, not a semantic
classifier. Callers should set serious=True for known formal contexts.
"""
from collections.abc import Callable
import math
import random
import re


_CHINESE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_OTHER_EAST_ASIAN_SCRIPT = re.compile(r"[\u3040-\u30ff\u1100-\u11ff\u3130-\u318f\uac00-\ud7a3]")
# Quotes, markup, operators, separators and indentation are deliberately conservative.
_TECHNICAL = re.compile(
    r"[`/\\{}\[\]<>|=:$%\"'“”‘’「」『』\t]|~~~|"
    r"(?im:^[ \t]*(?:[#>*+\-]\s|\d+[.)、]\s*|[A-Za-z_\u4e00-\u9fff][\w .-]*[：:]))|"
    r"(?m:^(?: {4}|\t)\S)|"
    r"(?m:^[ \t]*(?:[一二三四五六七八九十]+[、）.]|（[一二三四五六七八九十0-9]+）))|"
    r"代码|代碼|命令行|文件路径|檔案路徑|(?i:\b(?:json|yaml|csv|tsv|xml|sql)\b)|"
    r"(?i:\b[\w-]+(?:\.[\w-]+)+\b|\b[\w.+-]+@[\w.-]+\b|--[a-z])|"
    r"(?i:\b(?:python\d*|pip\d*|uv|npm|npx|pnpm|yarn|git|curl|wget|ssh|sudo|"
    r"powershell|pwsh|cmd|bash|sh|ffmpeg|conda|docker|kubectl|node|echo|printf|"
    r"cd|ls|dir|rm|del|mkdir|chmod|chown|cat|type|copy|move|start|set)\b)|"
    r"(?im:\b(?:def|class|import|from|return|print|if|else|for|while|const|let|var|"
    r"function|select|insert|update|delete|create|alter)\b)|"
    r"\b[A-Za-z_]\w*\s*\(|[+*^≤≥≠]|\d\s*[-−]\s*\d|"
    r"(?m:^.*[,，].*\n.*[,，])"
)
_SERIOUS = re.compile(
    r"严肃|嚴肅|严謹|严谨|嚴謹|认真(?:回答|一点|一點|点)|正经|正經|"
    r"原样|原樣|逐字|一字不差|不要改|照着念|照著念|"
    r"(?:不要|别|別|禁止|停止).{0,8}(?:玩笑|卖萌|賣萌|口癖|加喵|闹|鬧)|"
    r"医疗|醫療|医生|醫生|就医|就醫|诊断|診斷|用药|用藥|剂量|劑量|处方|處方|"
    r"胸痛|呼吸困难|呼吸困難|急救|报警|報警|自杀|自殺|自伤|自傷|不想活|"
    r"生病|发烧|發燒|住院|手术|手術|疼痛|流血|出血|受伤|受傷|恐慌|"
    r"抑郁|抑鬱|去世|过世|過世|逝世|葬礼|葬禮|家暴|性侵|虐待|遇害|"
    r"难过|難過|难受|難受|伤心|傷心|悲伤|悲傷|节哀|節哀|哀悼|痛苦|崩溃|崩潰|分手|抱歉|对不起|對不起|"
    r"失业|失業|裁员|裁員|法律|诉讼|訴訟|合同|诈骗|詐騙|转账|轉賬|"
    r"投资|投資|贷款|貸款|债务|債務|密码|密碼|泄露|洩露|"
    r"危险|危險|紧急|緊急|警告|故障|错误|錯誤|删除|刪除|数据丢失|資料遺失|"
    r"考试|考試|翻译|翻譯|证明|證明|推导|推導|解题|解題|"
    r"(?i:\b(?:serious|seriously|emergency|medical|suicide|grief|condolences|"
    r"diagnosis|dosage|legal|lawsuit|no jokes|no joking)\b)"
)
_SENTENCE = re.compile(
    r"(?P<body>[^。.!?！？~～…\r\n]+)(?P<end>[。.!?！？~～…]+|(?=\r?$))", re.MULTILINE
)


def apply_speech_quirk(text: str, *, enabled: bool = True, probability: float = 0.8,
                       serious: bool = False, context: str = "",
                       random_value: Callable[[], float] | None = None) -> str:
    """Add at most one 喵 per eligible Chinese sentence, preserving punctuation.

    Probability is independent per eligible sentence, in [0, 1]. Invalid values
    leave input unchanged. Existing sentence-final 喵, including its punctuation
    variants, is retained. The original reply/subtitles are not modified by callers.
    """
    if (not enabled or serious or not text or type(probability) not in (int, float)
            or not math.isfinite(probability) or not 0 < probability <= 1):
        return text
    if _TECHNICAL.search(text) or _SERIOUS.search(text) or _SERIOUS.search(context):
        return text
    draw = random_value if random_value is not None else random.random

    def decorate(match: re.Match) -> str:
        body, ending = match.group("body", "end")
        content = body.rstrip()
        # Preserve incomplete phrases, foreign sentences, and already decorated ends.
        if (not _CHINESE.search(content) or _OTHER_EAST_ASIAN_SCRIPT.search(content) or content.endswith("喵")
                or content.endswith(tuple(",，;；:：、—-"))):
            return match.group(0)
        if probability < 1 and draw() >= probability:
            return match.group(0)
        spacing = body[len(content):]
        return content + "喵" + spacing + ending

    return _SENTENCE.sub(decorate, text)
