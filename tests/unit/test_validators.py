"""Deterministic content checks."""

from __future__ import annotations

import pytest

from app.models.platforms import PLATFORM_RULES
from app.models.schemas import CheckResult
from app.services.validators import (
    check_duplicates,
    check_field_present,
    check_hashtags,
    check_length,
    check_malformed,
    check_platform_style,
    hashtags,
    jaccard_similarity,
    keywords,
    summarise_checks,
)

X_RULE = PLATFORM_RULES["x"]
INSTAGRAM_RULE = PLATFORM_RULES["instagram"]
DISCORD_RULE = PLATFORM_RULES["discord"]
TELEGRAM_RULE = PLATFORM_RULES["telegram"]


def test_hashtags_extract_and_ignore_inside_words():
    assert hashtags("hello #one and #two, email#notatag") == ["one", "two"]


def test_keywords_skip_stopwords_and_short_tokens():
    result = keywords("The team shipped a new content pipeline for the blog", limit=5)
    assert "the" not in result
    assert "team" in result
    assert len(result) <= 5


def test_field_present_variants():
    assert check_field_present("text").status == "pass"
    assert check_field_present(None).status == "fail"
    assert check_field_present("   ").status == "fail"
    assert check_field_present(None).severity == "error"


def test_length_rule_fails_over_platform_limit():
    result = check_length(X_RULE, "a" * 281)
    assert result.status == "fail"
    assert result.severity == "error"
    assert result.details["characters"] == 281


def test_length_rule_warns_when_below_minimum():
    result = check_length(INSTAGRAM_RULE, "short")
    assert (result.status, result.severity) == ("fail", "warning")


def test_length_rule_passes_within_limits():
    result = check_length(X_RULE, "x" * 100)
    assert result.status == "pass"


def test_hashtags_below_convention_is_a_warning():
    result = check_hashtags(INSTAGRAM_RULE, "no tags here")
    assert (result.status, result.severity) == ("fail", "warning")


def test_hashtags_above_maximum_is_a_warning():
    text = " ".join(f"#tag{i}" for i in range(9))
    result = check_hashtags(X_RULE, text)
    assert result.status == "fail"
    assert result.details["count"] == 9


def test_hashtags_zero_is_acceptable_when_platform_allows_it():
    result = check_hashtags(DISCORD_RULE, "community update without tags")
    assert result.status == "pass"


@pytest.mark.parametrize(
    "text",
    [
        "As an AI language model I cannot write that.",
        "TODO: write the caption here",
        "The post body is N/A",
        '{"x_post": "nested payload"}',
        "A cliffhanger ending that just cuts off...",
    ],
)
def test_malformed_content_is_rejected(text: str):
    result = check_malformed(text)
    assert result.status == "fail"


def test_clean_content_is_not_malformed():
    assert check_malformed("A normal caption with #hashtag and a link https://x.y").status == "pass"


def test_platform_style_requires_markdown_for_discord():
    assert check_platform_style(DISCORD_RULE, "plain broadcast").status == "fail"
    assert check_platform_style(DISCORD_RULE, "**bold** broadcast").status == "pass"


def test_platform_style_requires_multiline_for_telegram():
    assert check_platform_style(TELEGRAM_RULE, "one single line").status == "fail"
    assert check_platform_style(TELEGRAM_RULE, "headline\n- bullet").status == "pass"


def test_platform_style_none_for_x():
    assert check_platform_style(X_RULE, "short post") is None


def test_duplicates_detected_for_identical_copy():
    pairs = check_duplicates(
        {
            "x": "the same content for every platform here",
            "discord": "the same content for every platform here",
        },
        threshold=0.9,
    )
    assert len(pairs) == 1
    assert pairs[0].similarity == 1.0


def test_distinct_copy_is_not_duplicate():
    pairs = check_duplicates(
        {
            "x": "short punchy launch post about metrics",
            "instagram": "longer caption telling a story about dashboards and alerts",
        },
        threshold=0.9,
    )
    assert pairs == []


def test_jaccard_similarity_bounds():
    assert jaccard_similarity("alpha beta", "alpha beta") == 1.0
    assert jaccard_similarity("alpha beta", "gamma delta") == 0.0
    assert jaccard_similarity("", "anything") == 0.0


def test_summary_fails_on_error_check():
    score, status = summarise_checks(
        [
            CheckResult(name="a", status="pass", severity="error", message="ok"),
            CheckResult(name="b", status="fail", severity="error", message="bad"),
        ]
    )
    assert status == "failed"
    assert score == 50


def test_summary_warns_on_warning_level_failure():
    _, status = summarise_checks(
        [CheckResult(name="a", status="fail", severity="warning", message="meh")]
    )
    assert status == "warnings"


def test_summary_passes_with_info_only_checks():
    score, status = summarise_checks(
        [CheckResult(name="a", status="pass", severity="info", message="note")]
    )
    assert (score, status) == (100, "passed")
