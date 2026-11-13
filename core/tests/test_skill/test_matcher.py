"""Activation rules: mentions, triggers, explicit disable, the 3-skill cap."""
from __future__ import annotations

from dataclasses import dataclass

from vade.skill.matcher import find_mentions, select


@dataclass(frozen=True)
class Fake:
    name: str
    triggers: tuple[str, ...] = ()
    status: str = "active"
    available: bool = True


def test_mention_is_case_insensitive() -> None:
    states = [Fake("kb-guide")]
    assert select("@KB-Guide 查一下", states).names == ("kb-guide",)
    assert select("no mention here", states).names == ()


def test_email_like_text_is_not_a_mention() -> None:
    states = [Fake("kb-guide")]
    assert select("write to foo@kb-guide.com", states).names == ()
    assert find_mentions("user@host") == ((), ())


def test_trigger_is_a_case_insensitive_substring() -> None:
    states = [Fake("kb-guide", triggers=("之前定过", "Decision Log"))]
    assert select("我们之前定过这个吗", states).names == ("kb-guide",)
    assert select("check the decision log", states).names == ("kb-guide",)
    assert select("unrelated", states).names == ()


def test_explicit_disable_beats_triggers() -> None:
    states = [Fake("kb-guide", triggers=("之前定过",))]
    assert select("@!kb-guide 之前定过吗", states).names == ()
    assert select("@kb-guide 之前定过吗", states).names == ("kb-guide",)


def test_hit_count_outranks_alphabetical_order() -> None:
    states = [
        Fake("alpha", triggers=("one",)),
        Fake("beta", triggers=("one", "two")),
    ]
    activation = select("one two", states)
    assert activation.names == ("beta", "alpha")
    assert activation.ordered == ("alpha", "beta")
    assert activation.hit_count("beta") == 2


def test_mention_outranks_a_high_hit_count() -> None:
    states = [
        Fake("alpha", triggers=("one", "two", "three")),
        Fake("zeta", triggers=()),
    ]
    assert select("@zeta one two three", states).names[0] == "zeta"


def test_cap_is_three_and_the_rest_stay_in_the_catalog() -> None:
    states = [Fake(name, triggers=("go",)) for name in ("aa", "bb", "cc", "dd", "ee")]
    activation = select("go", states)
    assert activation.names == ("aa", "bb", "cc")
    assert activation.ordered == ("aa", "bb", "cc")


def test_ineligible_skills_are_never_activated() -> None:
    states = [
        Fake("stale", triggers=("go",), status="stale"),
        Fake("off", triggers=("go",), status="disabled"),
        Fake("missing", triggers=("go",), available=False),
        Fake("fine", triggers=("go",)),
    ]
    assert select("go", states).names == ("fine",)


def test_duplicate_mentions_activate_once() -> None:
    states = [Fake("kb-guide")]
    assert select("@kb-guide @kb-guide", states).names == ("kb-guide",)


def test_no_message_activates_nothing() -> None:
    assert select("", [Fake("kb-guide", triggers=("x",))]).names == ()
