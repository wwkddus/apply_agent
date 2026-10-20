import unittest

from job_agent_core.models import PersonalInfo, ResumeProfile
from job_agent_core.names import (
    confirm_name_parts,
    infer_latin_name,
    infer_name_parts,
    name_components_usable,
    normalized_name,
)


class NameTests(unittest.TestCase):
    def test_simple_korean_name_is_inferred_but_not_confirmed(self):
        name = infer_name_parts("홍길동")
        self.assertEqual(name.full, "홍길동")
        self.assertEqual(name.family, "홍")
        self.assertEqual(name.given, "길동")
        self.assertEqual(name.order, "family_given")
        self.assertFalse(name.confirmed)
        self.assertGreater(name.split_confidence, 0.8)

    def test_compound_family_name_stays_unconfirmed(self):
        name = infer_name_parts("남궁민")
        self.assertEqual(name.family, "남궁")
        self.assertEqual(name.given, "민")
        self.assertLess(name.split_confidence, 0.8)
        self.assertFalse(name.confirmed)

    def test_latin_space_order_is_low_confidence(self):
        name = infer_name_parts("Gildong Hong")
        self.assertEqual(name.family, "Hong")
        self.assertEqual(name.given, "Gildong")
        self.assertLess(name.split_confidence, 0.8)

    def test_user_confirmation_returns_new_profile(self):
        original = ResumeProfile(personal=PersonalInfo(name={"full": "홍길동"}))
        confirmed = confirm_name_parts(original, family="홍", given="길동")
        self.assertFalse(original.personal.name.confirmed)
        self.assertTrue(confirmed.personal.name.confirmed)
        self.assertEqual(confirmed.personal.name.split_method, "user_confirmed")

    def test_only_verifiable_splits_are_usable_without_confirmation(self):
        self.assertTrue(name_components_usable(infer_name_parts("홍길동")))
        self.assertTrue(name_components_usable(infer_name_parts("이산")))
        self.assertFalse(name_components_usable(infer_name_parts("남궁민")))
        self.assertFalse(name_components_usable(infer_name_parts("김나라빛")))
        self.assertFalse(name_components_usable(infer_name_parts("Gildong Hong")))
        confirmed = confirm_name_parts(
            ResumeProfile(personal=PersonalInfo(name=infer_name_parts("남궁민"))), family="남궁", given="민"
        )
        self.assertTrue(name_components_usable(confirmed.personal.name))

    def test_normalized_name_ignores_safe_separators(self):
        self.assertEqual(normalized_name("Hong Gil-dong"), normalized_name("HongGildong"))


class LatinNameOrderTests(unittest.TestCase):
    def split(self, latin, native):
        name = infer_latin_name(latin, infer_name_parts(native))
        return name.family, name.given, name.order, name_components_usable(name)

    def test_order_follows_the_hangul_surname(self):
        self.assertEqual(self.split("Hong Gildong", "홍길동"), ("Hong", "Gildong", "family_given", True))
        self.assertEqual(self.split("Gildong Hong", "홍길동"), ("Hong", "Gildong", "given_family", True))
        self.assertEqual(self.split("HONG Gil Dong", "홍길동"), ("HONG", "Gil Dong", "family_given", True))
        self.assertEqual(self.split("ZHANG YAN", "장연"), ("ZHANG", "YAN", "family_given", True))
        self.assertEqual(self.split("Minsu Lee", "이민수"), ("Lee", "Minsu", "given_family", True))

    def test_comma_marks_the_family_name(self):
        self.assertEqual(self.split("Hong, Gildong", "홍길동")[:3], ("Hong", "Gildong", "family_given"))

    def test_unknown_order_is_left_for_the_user(self):
        self.assertFalse(self.split("Alex Smith", "홍길동")[3])
        self.assertFalse(self.split("Alex Smith", "")[3])


if __name__ == "__main__":
    unittest.main()

