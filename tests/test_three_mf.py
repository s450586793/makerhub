import unittest

from app.services.three_mf import (
    describe_three_mf_failure,
    merge_three_mf_failure,
    normalize_three_mf_failure_state,
)


class ThreeMfFailureTest(unittest.TestCase):
    def test_auth_required_wins_over_generic_verification_when_merging_failures(self):
        merged = merge_three_mf_failure(
            {
                "state": "auth_required",
                "message": "下载 3MF 需要有效登录态，请检查 Cookie / token 是否过期。",
            },
            {
                "state": "verification_required",
                "message": "MakerWorld 需要验证，前往官网任意下载一个模型。",
            },
        )

        self.assertEqual(merged["state"], "auth_required")

    def test_auth_required_message_is_not_reclassified_as_verification(self):
        message = describe_three_mf_failure("auth_required", source="global")

        self.assertEqual(
            normalize_three_mf_failure_state("missing", message),
            "auth_required",
        )

    def test_auth_required_message_overrides_stale_verification_state(self):
        self.assertEqual(
            normalize_three_mf_failure_state(
                "verification_required",
                "Please log in to download models.",
            ),
            "auth_required",
        )


if __name__ == "__main__":
    unittest.main()
