"""Token estimator fallback and special-token regression tests."""

from __future__ import annotations

import pytest

from app.agent.memory.token_estimator import TokenEstimator


class _SpecialLiteralEncoder:
    """Model tiktoken's default rejection of literal special-token strings."""

    def encode(self, text: str, *, disallowed_special) -> list[int]:
        if disallowed_special != ():
            raise ValueError("disallowed special token")
        assert text == "<|endoftext|>"
        return list(range(7))


def _estimator_with_encoder(encoder) -> TokenEstimator:
    estimator = TokenEstimator.__new__(TokenEstimator)
    estimator._encoder = encoder
    estimator.is_fallback = False
    estimator._calibration_ratio = 1.0
    return estimator


def _fallback_estimator() -> TokenEstimator:
    estimator = TokenEstimator.__new__(TokenEstimator)
    estimator._encoder = None
    estimator.is_fallback = True
    estimator._calibration_ratio = 1.0
    return estimator


def test_special_token_literals_are_counted_as_plain_text():
    estimator = _estimator_with_encoder(_SpecialLiteralEncoder())

    token_count, used_fallback = estimator.estimate_string_with_fallback(
        "<|endoftext|>"
    )

    assert token_count == 7
    assert used_fallback is False


@pytest.mark.parametrize(
    ("text", "expected_tokens"),
    [
        ("abc", 1),
        ("abcd", 1),
        ("人工智能", 4),
        ("人工智能正在改变软件开发的方式。" * 20, 320),
    ],
)
def test_fallback_uses_ascii_and_cjk_aware_heuristics(text, expected_tokens):
    estimator = _fallback_estimator()

    token_count, used_fallback = estimator.estimate_string_with_fallback(text)

    assert token_count == expected_tokens
    assert used_fallback is True


def test_fallback_estimator_marks_empty_text_without_fallback():
    estimator = _fallback_estimator()

    assert estimator.estimate_string_with_fallback("") == (0, False)
