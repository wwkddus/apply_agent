import unittest

from job_agent_core.security import (
    URLPolicyError,
    domain_matches,
    is_safe_next_control,
    is_submit_like_control,
    require_approved_redirect,
    sanitize_url,
    validate_target_url,
)


class SecurityTests(unittest.TestCase):
    def test_public_https_url_is_allowed_and_query_is_hidden(self):
        result = validate_target_url("https://jobs.example.com/apply?token=secret#step")
        self.assertEqual(result.hostname, "jobs.example.com")
        self.assertEqual(result.display, "https://jobs.example.com/apply")

    def test_unsafe_schemes_and_local_targets_are_rejected(self):
        for url in (
            "http://jobs.example.com",
            "file:///tmp/resume.pdf",
            "https://localhost/apply",
            "https://127.0.0.1/apply",
            "https://10.0.0.1/apply",
            "https://jobs.example.com:8443/apply",
            "https://user:pass@jobs.example.com/apply",
        ):
            with self.subTest(url=url), self.assertRaises(URLPolicyError):
                validate_target_url(url)

    def test_subdomain_match_does_not_accept_suffix_attack(self):
        self.assertTrue(domain_matches("apply.example.com", "example.com"))
        self.assertFalse(domain_matches("example.com.evil.test", "example.com"))

    def test_unapproved_top_level_redirect_is_rejected(self):
        require_approved_redirect("https://login.example.com/sso", {"example.com"})
        with self.assertRaises(URLPolicyError):
            require_approved_redirect("https://evil.test/collect", {"example.com"})

    def test_tokenish_path_segment_is_redacted(self):
        shown = sanitize_url("https://jobs.example.com/apply/abcdefghijklmnopqrstuvwxyz123456")
        self.assertEqual(shown, "https://jobs.example.com/apply/…")

    def test_next_is_separate_from_submit(self):
        self.assertTrue(
            is_safe_next_control(tag="button", attr_type="button", form_type="", text="다음")
        )
        self.assertTrue(
            is_safe_next_control(tag="button", attr_type="button", form_type="", text="Next")
        )
        self.assertFalse(
            is_safe_next_control(tag="button", attr_type="submit", form_type="", text="Next")
        )
        self.assertFalse(
            is_safe_next_control(tag="button", attr_type="button", form_type="", text="지원하기")
        )
        self.assertTrue(
            is_submit_like_control(tag="button", attr_type="button", form_type="", text="최종 제출")
        )


if __name__ == "__main__":
    unittest.main()

