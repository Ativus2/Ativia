from dataclasses import FrozenInstanceError, replace
import unittest
from persona import PROFILE, ConversationRhythm


class PersonaStateTests(unittest.TestCase):
    def test_custom_rhythm_policy_controls_open_and_recovery(self):
        rhythm = ConversationRhythm(replace(PROFILE, reaction_limit=1, reaction_window_seconds=10))
        self.assertTrue(rhythm.can_react(100))
        rhythm.reacted(100)
        self.assertFalse(rhythm.can_react(109))
        self.assertTrue(rhythm.can_react(110))
        rhythm.reacted(110)
        rhythm.engaged()
        self.assertTrue(rhythm.can_react(111))

    def test_core_profile_is_not_mutated_by_conversation_state(self):
        rhythm = ConversationRhythm()
        before = PROFILE.introduction()
        for at in (100, 170, 240):
            rhythm.reacted(at)
        self.assertFalse(rhythm.can_react(250))
        self.assertEqual(PROFILE.introduction(), before)
        with self.assertRaises(FrozenInstanceError):
            PROFILE.temperament = "每次随机换人格"

    def test_teasing_preference_changes_policy_without_changing_observation(self):
        reserved = replace(PROFILE, tease_events=frozenset())
        self.assertTrue(PROFILE.allows("TEASE", "surprise"))
        self.assertFalse(reserved.allows("TEASE", "surprise"))
        self.assertTrue(reserved.allows("REACT", "error"))
