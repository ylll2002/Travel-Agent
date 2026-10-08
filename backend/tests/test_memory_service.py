import unittest
from types import SimpleNamespace

from app.services.memory_service import (
    FOOD_AVOID,
    FOOD_CUISINES,
    HOTEL_MUST,
    TRANSPORT_WORDS,
    _action_polarity,
    _learn,
    _merge,
    _preferred,
    _preferred_all,
)


def sig(action, target="", detail=""):
    return SimpleNamespace(user_id="u1", action=action, target=target, detail=detail)


class MemoryServiceTests(unittest.TestCase):
    def test_action_polarity(self):
        self.assertEqual(_action_polarity("add"), 1)
        self.assertEqual(_action_polarity("confirm"), 1)
        self.assertEqual(_action_polarity("remove"), -1)
        self.assertEqual(_action_polarity("avoid"), -1)
        self.assertEqual(_action_polarity("view"), 0)

    def test_preferred_picks_most_frequent_group(self):
        signals = [
            sig("add", detail="打车"),
            sig("add", detail="打车"),
            sig("add", detail="地铁"),
        ]
        self.assertEqual(_preferred(signals, TRANSPORT_WORDS), ["打车优先"])
        self.assertEqual(_preferred_all(signals, TRANSPORT_WORDS), ["打车优先", "地铁优先"])

    def test_learn_builds_complete_preference_structure(self):
        signals = [
            sig("add", detail="轻松"),
            sig("add", detail="打车"),
            sig("add", detail="地铁"),
            sig("add", detail="含早餐"),
            sig("add", detail="杭帮菜"),
            sig("add", target="西湖"),
            sig("avoid", detail="太辣"),
            sig("avoid", target="纯购物街区"),
        ] * 3  # 重复三次达到 min_count=3
        prefs = _learn(signals, min_count=3)
        self.assertEqual(prefs["pace"], "轻松")
        self.assertEqual(prefs["transport"], ["打车优先", "地铁优先"])
        self.assertEqual(prefs["food"]["cuisines"], ["杭帮菜"])
        self.assertIn("太辣", prefs["food"]["avoid"])
        self.assertTrue(any(x in prefs["hotel"]["must"] for x in ("含早", "早餐")))
        self.assertIn("西湖", prefs["liked"])
        self.assertIn("纯购物街区", prefs["avoided"])

    def test_learn_respects_min_count(self):
        signals = [sig("add", detail="含早餐")]
        prefs = _learn(signals, min_count=3)
        self.assertNotIn("hotel", prefs)

    def test_merge_preserves_manual_and_merges_lists_and_dicts(self):
        base = {
            "pace": "轻松",
            "transport": ["打车优先"],
            "food": {"avoid": ["太辣"]},
            "liked": ["西湖"],
        }
        learned = {
            "transport": ["地铁优先"],
            "food": {"cuisines": ["杭帮菜"], "avoid": ["不吃辣"]},
            "liked": ["西溪湿地"],
            "avoided": ["纯购物街区"],
        }
        merged = _merge(base, learned)
        self.assertEqual(merged["pace"], "轻松")
        self.assertEqual(merged["transport"], ["打车优先", "地铁优先"])
        self.assertEqual(merged["food"]["cuisines"], ["杭帮菜"])
        self.assertEqual(set(merged["food"]["avoid"]), {"太辣", "不吃辣"})
        self.assertEqual(merged["liked"], ["西湖", "西溪湿地"])
        self.assertEqual(merged["avoided"], ["纯购物街区"])

    def test_known_keyword_groups_have_expected_members(self):
        self.assertIn("杭帮菜", FOOD_CUISINES)
        self.assertIn("太辣", FOOD_AVOID)
        self.assertIn("含早", HOTEL_MUST)
        self.assertIn("早餐", HOTEL_MUST)
        self.assertIn("打车优先", TRANSPORT_WORDS)


if __name__ == "__main__":
    unittest.main()
