"""Authored character identity and a small, causal conversation rhythm."""
from collections import deque
from dataclasses import dataclass
from personality.engine import core
_IDENTITY = core()["identity"]


@dataclass(frozen=True)
class CharacterProfile:
    name: str = _IDENTITY["name"]
    temperament: str = _IDENTITY["temperament"]
    interests: tuple[str, ...] = tuple(_IDENTITY["interests"])
    humor: str = _IDENTITY["humor"]
    tease_events: frozenset[str] = frozenset({"resolved", "success", "surprise"})
    reaction_limit: int = 3
    reaction_window_seconds: int = 300
    voice_direction: str = (
        "A calm feminine voice with quiet curiosity and restrained warmth. "
        "Conversational pacing and small, natural inflections; dry amusement only when the text invites it. "
        "Be clear for explanations. Avoid a robotic monotone, announcer delivery, exaggerated excitement or forced laughter."
    )

    def introduction(self) -> str:
        return (f"你是女性桌面角色 {self.name}，{self.temperament}。"
                f"偏好{'、'.join(self.interests)}；{self.humor}。")

    def allows(self, intent: str, event: str) -> bool:
        return intent != "TEASE" or event in self.tease_events


PROFILE = CharacterProfile()


class ConversationRhythm:
    """Count delivered unsolicited comments, never infer rejection or emotions."""
    def __init__(self, profile: CharacterProfile = PROFILE):
        self.profile = profile
        self._reactions: deque[float] = deque(maxlen=profile.reaction_limit)

    def can_react(self, now: float) -> bool:
        while self._reactions and now - self._reactions[0] >= self.profile.reaction_window_seconds:
            self._reactions.popleft()
        return len(self._reactions) < self.profile.reaction_limit

    def reacted(self, now: float) -> None:
        self.can_react(now)
        self._reactions.append(now)

    def engaged(self) -> None:
        self._reactions.clear()
