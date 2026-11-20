"""Deterministic content checks.

Everything in this module is cheap, repeatable and has no external dependency, so
it can run on every request. Semantic judgement is handled separately in
``app.services.semantic``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping

from app.models.platforms import PlatformRule
from app.models.schemas import CheckResult, DuplicatePair, ReportStatus

HASHTAG_RE = re.compile(r"(?<!\w)#([A-Za-z0-9_]{1,80})")
WORD_RE = re.compile(r"[a-z0-9][a-z0-9'\-]*")
URL_RE = re.compile(r"https?://\S+")

#: Signatures of a failed generation: model refusal, leftover template text,
#: serialised errors, or truncation markers. Matching any of these is a hard
#: failure because the content cannot be published.
MALFORMED_MARKERS: tuple[str, ...] = (
    "as an ai",
    "i cannot fulfill",
    "i'm sorry, but",
    "i am sorry, but",
    "lorem ipsum",
    "[insert",
    "{insert",
    "<insert",
    "todo:",
    "fixme",
    "placeholder text",
    "traceback (most recent call last)",
    "internal server error",
    "error: 500",
    "undefined",
    "n/a",
    "none provided",
)

STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "am",
        "an",
        "and",
        "any",
        "are",
        "aren't",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "cannot",
        "could",
        "couldn't",
        "did",
        "didn't",
        "do",
        "does",
        "doesn't",
        "doing",
        "don't",
        "down",
        "during",
        "each",
        "few",
        "for",
        "from",
        "further",
        "had",
        "hadn't",
        "has",
        "hasn't",
        "have",
        "haven't",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "himself",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "isn't",
        "it",
        "its",
        "itself",
        "just",
        "let's",
        "me",
        "more",
        "most",
        "mustn't",
        "my",
        "myself",
        "no",
        "nor",
        "not",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "ought",
        "our",
        "ours",
        "ourselves",
        "out",
        "over",
        "own",
        "same",
        "shan't",
        "she",
        "should",
        "shouldn't",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "themselves",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "very",
        "was",
        "wasn't",
        "we",
        "were",
        "weren't",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "won't",
        "would",
        "wouldn't",
        "you",
        "your",
        "yours",
        "yourself",
        "yourselves",
        "also",
        "via",
        "using",
        "one",
        "two",
        "new",
    }
)

_MIN_KEYWORD_LENGTH = 3
_MAX_SOURCE_KEYWORDS = 25


def hashtags(text: str) -> list[str]:
    return [match.group(1) for match in HASHTAG_RE.finditer(text)]


def normalise_for_comparison(text: str) -> str:
    """Lowercase, strip URLs and hashtag markers, collapse whitespace."""

    lowered = URL_RE.sub(" ", text.lower())
    lowered = lowered.replace("#", " ")
    lowered = re.sub(r"[^a-z0-9\s'\-]", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def tokens(text: str) -> list[str]:
    return WORD_RE.findall(normalise_for_comparison(text))


def keywords(text: str, limit: int = _MAX_SOURCE_KEYWORDS) -> list[str]:
    """Ordered, de-duplicated content words. Hashtag words count as keywords."""

    seen: dict[str, None] = {}
    for token in tokens(text):
        if len(token) < _MIN_KEYWORD_LENGTH or token in STOPWORDS:
            continue
        seen.setdefault(token, None)
    for tag in hashtags(text):
        tag_word = tag.lower()
        if len(tag_word) >= _MIN_KEYWORD_LENGTH and tag_word not in STOPWORDS:
            seen.setdefault(tag_word, None)
    return list(seen)[:limit]


def jaccard_similarity(left: str, right: str) -> float:
    left_tokens = {token for token in tokens(left) if token not in STOPWORDS}
    right_tokens = {token for token in tokens(right) if token not in STOPWORDS}
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = left_tokens & right_tokens
    union = left_tokens | right_tokens
    return len(intersection) / len(union)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------
def check_field_present(text: str | None) -> CheckResult:
    if text is None:
        return CheckResult(
            name="field_present",
            status="fail",
            severity="error",
            message="the generator did not return a value for this platform",
        )
    if not text.strip():
        return CheckResult(
            name="field_present",
            status="fail",
            severity="error",
            message="the returned value is empty or whitespace",
        )
    return CheckResult(
        name="field_present",
        status="pass",
        severity="error",
        message="value is present",
    )


def check_length(rule: PlatformRule, text: str) -> CheckResult:
    length = len(text)
    details = {"characters": length, "max": rule.max_chars, "min": rule.min_chars}
    if length > rule.max_chars:
        return CheckResult(
            name="length",
            status="fail",
            severity="error",
            message=(f"{length} characters exceeds the {rule.platform} limit of {rule.max_chars}"),
            details=details,
        )
    if length < rule.min_chars:
        return CheckResult(
            name="length",
            status="fail",
            severity="warning",
            message=(
                f"{length} characters is below the expected minimum of "
                f"{rule.min_chars} for {rule.platform}"
            ),
            details=details,
        )
    return CheckResult(
        name="length",
        status="pass",
        severity="error",
        message=f"{length} characters is within the platform limit",
        details=details,
    )


def check_hashtags(rule: PlatformRule, text: str) -> CheckResult:
    found = hashtags(text)
    count = len(found)
    details = {
        "count": count,
        "min": rule.min_hashtags,
        "max": rule.max_hashtags,
        "hashtags": found[:10],
    }
    if count < rule.min_hashtags:
        return CheckResult(
            name="hashtags",
            status="fail" if rule.min_hashtags > 0 else "pass",
            severity="warning",
            message=(
                f"{count} hashtags found, the {rule.platform} convention is "
                f"{rule.min_hashtags}-{rule.max_hashtags}"
            ),
            details=details,
        )
    if count > rule.max_hashtags:
        return CheckResult(
            name="hashtags",
            status="fail",
            severity="warning",
            message=(
                f"{count} hashtags found, more than the {rule.max_hashtags} "
                f"expected for {rule.platform}"
            ),
            details=details,
        )
    return CheckResult(
        name="hashtags",
        status="pass",
        severity="warning",
        message=f"{count} hashtags found",
        details=details,
    )


def check_malformed(text: str) -> CheckResult:
    lowered = text.lower()
    hits = [marker for marker in MALFORMED_MARKERS if marker in lowered]

    # A value that is itself a JSON document normally means the upstream service
    # returned a serialised envelope instead of a caption.
    looks_like_json = False
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
            looks_like_json = isinstance(parsed, (dict, list))
        except json.JSONDecodeError:
            looks_like_json = False

    if looks_like_json:
        return CheckResult(
            name="malformed_content",
            status="fail",
            severity="error",
            message=(
                "the value is a serialised JSON payload, not platform copy "
                "(likely double-encoded upstream)"
            ),
            details={"reason": "json_payload"},
        )

    if hits:
        return CheckResult(
            name="malformed_content",
            status="fail",
            severity="error",
            message="the value contains generation failure markers",
            details={"markers": hits},
        )

    if stripped.endswith(("...", "…")) and len(stripped) < 80:
        return CheckResult(
            name="malformed_content",
            status="fail",
            severity="warning",
            message="the value looks truncated",
            details={"reason": "truncated"},
        )

    return CheckResult(
        name="malformed_content",
        status="pass",
        severity="error",
        message="no malformed-content markers found",
    )


def check_platform_style(rule: PlatformRule, text: str) -> CheckResult | None:
    """Check the formatting conventions the generator is asked to follow."""

    if rule.expects_markdown:
        markers = ("**", "__", ">", "*", "- ")
        if not any(marker in text for marker in markers):
            return CheckResult(
                name="platform_style",
                status="fail",
                severity=rule.style_severity,
                message=(
                    f"{rule.platform} content is expected to use markdown formatting; none found"
                ),
                details={"expected": "markdown"},
            )
        return CheckResult(
            name="platform_style",
            status="pass",
            severity=rule.style_severity,
            message="markdown formatting present",
        )

    if rule.expects_multiline:
        lines = [line for line in text.splitlines() if line.strip()]
        if len(lines) < 2 and "•" not in text and "- " not in text:
            return CheckResult(
                name="platform_style",
                status="fail",
                severity=rule.style_severity,
                message=(
                    f"{rule.platform} content is expected to have a headline and a bulleted body"
                ),
                details={"expected": "multiline"},
            )
        return CheckResult(
            name="platform_style",
            status="pass",
            severity=rule.style_severity,
            message="multi-line structure present",
        )

    return None


def check_duplicates(texts: Mapping[str, str | None], threshold: float) -> list[DuplicatePair]:
    """Find platform pairs whose copy is nearly identical.

    Platform variations that are copies of each other (rather than adaptations)
    are a common generation failure and worth flagging.
    """

    pairs: list[DuplicatePair] = []
    items = [(platform, text) for platform, text in texts.items() if text and text.strip()]
    for index, (left_platform, left_text) in enumerate(items):
        for right_platform, right_text in items[index + 1 :]:
            similarity = jaccard_similarity(left_text, right_text)
            if similarity >= threshold:
                pairs.append(
                    DuplicatePair(
                        platforms=[left_platform, right_platform],
                        similarity=round(similarity, 4),
                    )
                )
    return pairs


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
_WEIGHTS = {"error": 3, "warning": 1, "info": 0}


def summarise_checks(checks: Iterable[CheckResult]) -> tuple[int, ReportStatus]:
    checks = list(checks)

    def variant_status() -> ReportStatus:
        blocking = [c for c in checks if c.status == "fail" and c.severity == "error"]
        if blocking:
            return "failed"
        if any(c.status in {"fail", "warning"} for c in checks):
            return "warnings"
        return "passed"

    total_weight = 0
    earned_weight = 0
    for check in checks:
        weight = _WEIGHTS[check.severity]
        if weight == 0:
            continue
        total_weight += weight
        if check.status == "pass":
            earned_weight += weight
        elif check.status == "fail" and check.severity == "warning":
            earned_weight += weight * 0.5

    score = 100 if total_weight == 0 else round(100 * earned_weight / total_weight)
    return max(0, min(100, score)), variant_status()
