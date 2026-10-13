"""Small session observation memory and local speech decisions; no I/O or extra LLM."""
from collections import deque
from dataclasses import dataclass, replace
import json

MEMORY_SECONDS = 600
MEMORY_LIMIT = 6
CONTEXT_LIMIT = 2400
SCENES = {"coding", "video", "study", "chat", "game", "other", "unknown"}
EVENTS = {"none", "error", "resolved", "success", "surprise"}
INTENTS = {"WAIT", "REACT", "TEASE"}


@dataclass(frozen=True)
class Observation:
    scene: str
    event: str
    subject: str
    evidence: str
    confidence: str
    focused: bool
    intent: str
    reply: str

    @classmethod
    def parse(cls, text: str) -> "Observation":
        value = json.loads(text)
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("invalid observation fields")
        for name, limit in (("scene", 12), ("event", 12), ("subject", 64), ("evidence", 96),
                            ("confidence", 4), ("intent", 5), ("reply", 48)):
            item = value[name]
            if not isinstance(item, str) or len(item) > limit:
                raise ValueError("invalid observation text")
            value[name] = item.strip()
        if (value["scene"] not in SCENES or value["event"] not in EVENTS
                or value["intent"] not in INTENTS or value["confidence"] not in {"low", "high"}
                or type(value["focused"]) is not bool):
            raise ValueError("invalid observation category")
        return cls(**value)

    @property
    def key(self) -> tuple[str, str, str]:
        # Preserve operators and signs in error/task identities.
        return self.scene, self.event, " ".join(self.subject.split()).casefold()


@dataclass(frozen=True)
class Impression:
    at: float
    observation: Observation
    action: str
    announced: bool


class CharacterMemory:
    def __init__(self, entries=()):
        self.entries: deque[Impression] = deque(entries, maxlen=MEMORY_LIMIT)

    def snapshot(self, now: float) -> "CharacterMemory":
        return CharacterMemory(entry for entry in self.entries if 0 <= now - entry.at <= MEMORY_SECONDS)

    def context(self, now: float) -> str:
        # JSON escaping preserves boundaries; no candidate dialogue in observation memory.
        rows = [dict(age_s=int(now - item.at), scene=item.observation.scene,
                     event=item.observation.event, subject=item.observation.subject,
                     evidence=item.observation.evidence, confidence=item.observation.confidence,
                     action=item.action, announced=item.announced) for item in self.snapshot(now).entries]
        text = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        while len(text) > CONTEXT_LIMIT and rows:
            rows.pop(0)
            text = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        return text

    def accept(self, observation: Observation, now: float) -> str:
        self.entries = self.snapshot(now).entries
        grounded = (observation.confidence == "high" and observation.scene != "unknown"
                    and bool(observation.subject) and bool(observation.evidence))
        action = "OBSERVE" if not grounded else "WAIT"
        if grounded and not observation.focused and observation.event != "none":
            duplicate = False
            for old in reversed(self.entries):
                if (old.observation.key[0], old.observation.key[2]) != (observation.key[0], observation.key[2]):
                    continue
                # An uncertain frame or ordinary editing does not prove an error was fixed.
                if old.observation.key == observation.key and old.announced:
                    duplicate = True
                if (old.observation.confidence != "high" or not old.observation.evidence
                        or old.observation.event == "none"):
                    continue
                if old.observation.key != observation.key:
                    if observation.event == "error" and old.observation.event != "resolved":
                        continue
                    break
                duplicate |= old.announced
            if not duplicate and observation.intent != "WAIT" and observation.reply:
                action = observation.intent
        announced = action in {"REACT", "TEASE"}
        if self.entries and self.entries[-1].observation.key == observation.key:
            previous = self.entries[-1]
            announced |= previous.announced
            # Keep a change in evidence quality as a separate, dated observation.
            # A blurry frame must not erase or refresh the last confirmed state.
            if (previous.observation.confidence == observation.confidence
                    and bool(previous.observation.evidence) == bool(observation.evidence)):
                self.entries.pop()
        # Store observations even when silent, but never retain a discarded candidate.
        self.entries.append(Impression(now, replace(observation, reply=""), action, announced))
        return observation.reply if action in {"REACT", "TEASE"} else ""
