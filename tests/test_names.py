import unittest

from job_agent_core.models import PersonalInfo, ResumeProfile
from job_agent_core.names import confirm_name_parts, infer_name_parts, normalized_name


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

    def test_normalized_name_ignores_safe_separators(self):
        self.assertEqual(normalized_name("Hong Gil-dong"), normalized_name("HongGildong"))


if __name__ == "__main__":
    unittest.main()

