"""Small, inspectable opportunity score. No new model call or personality state."""
from dataclasses import dataclass
import re


def help_context(expertise="unknown", direct=False):
    return (f"用户自设熟悉程度：{expertise}（unknown 不代表新手）。"
            "只有用户明确求助、问机制或主动表达困惑时提供教学；正常活动、危险和失败本身不是求助。"
            "游戏默认陪着评论、反应或庆祝，不给操作步骤、策略或基础教程。"
            + ("本轮直接交流优先理解用户说的话；确有求助就认真回答。" if direct else "本轮无人求助，只作陪伴评论。"))


@dataclass(frozen=True)
class Opportunity:
    score: float
    reason: str


def conversation_opportunity(observation, memory, silence_seconds, recent_speaking_count):
    if (observation.focused or observation.scene == "unknown" or observation.confidence != "high"
            or not observation.evidence or not observation.subject):
        return Opportunity(0.0, "focus_or_uncertain")
    if recent_speaking_count >= 3:
        return Opportunity(0.0, "recent_speaking_frequency")
    if observation.event not in {"surprise", "success", "resolved"}:
        return Opportunity(0.0, "no_conversation_event")
    repeated = any(item.observation.key == observation.key and item.observation.evidence == observation.evidence
                   and item.announced for item in memory.entries)
    if repeated:
        return Opportunity(0.0, "already_discussed")
    # Authored interests: completed difficulties and unexpected story/game turns.
    interest = observation.event in {"resolved", "surprise", "success"}
    continuity = any(item.observation.subject == observation.subject for item in memory.entries)
    score = (0.3 + 0.2 + (0.15 if interest else 0) + (0.1 if continuity else 0)
             + (0.15 if silence_seconds >= 120 else 0) - 0.1 * recent_speaking_count)
    return Opportunity(round(max(0.0, min(1.0, score)), 2), "grounded_new_event")


def unrequested_game_coaching(observation):
    """Conservative fallback for direct player instructions in unsolicited game output."""
    return observation.scene == "game" and bool(re.search(
        r"^(?:你(?:应该|可以|最好|需要)|建议你|先.{0,20}(?:然后|再))",
        observation.reply.lstrip("… 。")))
