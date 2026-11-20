"""Platform rules used by the deterministic checks.

The four platform fields and their documented intent come from the existing
Variant Generator's response schema (``POST /generate-variants``). Limits are the
public limits of each platform; hashtag ranges follow the generator's own schema
descriptions so both systems judge the same contract.

These limits are intentionally data, not code: extending the service to a new
platform is a matter of adding a ``PlatformRule``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Severity = Literal["error", "warning", "info"]


@dataclass(frozen=True)
class PlatformRule:
    platform: str
    field: str
    max_chars: int
    min_chars: int
    min_hashtags: int
    max_hashtags: int
    #: Severity used when a platform-style expectation is not met.
    style_severity: Severity = "warning"
    expects_markdown: bool = False
    expects_multiline: bool = False
    notes: str = ""


PLATFORM_RULES: dict[str, PlatformRule] = {
    "x": PlatformRule(
        platform="x",
        field="x_post",
        max_chars=280,
        min_chars=15,
        min_hashtags=1,
        max_hashtags=5,
        notes="Short post; the generator may also return a two-part thread.",
    ),
    "instagram": PlatformRule(
        platform="instagram",
        field="instagram_caption",
        max_chars=2200,
        min_chars=60,
        min_hashtags=5,
        max_hashtags=8,
        notes="Generator contract asks for a hook, paragraphs and 5-8 hashtags.",
    ),
    "discord": PlatformRule(
        platform="discord",
        field="discord_message",
        max_chars=2000,
        min_chars=40,
        min_hashtags=0,
        max_hashtags=5,
        expects_markdown=True,
        notes="Community broadcast using Discord markdown.",
    ),
    "telegram": PlatformRule(
        platform="telegram",
        field="telegram_post",
        max_chars=4096,
        min_chars=40,
        min_hashtags=0,
        max_hashtags=5,
        expects_multiline=True,
        notes="Headline, bulleted takeaways and a call to action.",
    ),
}

DEFAULT_PLATFORMS: tuple[str, ...] = tuple(PLATFORM_RULES)


def all_fields() -> tuple[str, ...]:
    return tuple(rule.field for rule in PLATFORM_RULES.values())
