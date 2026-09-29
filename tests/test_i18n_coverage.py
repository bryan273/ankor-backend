"""The fixed strings must stay translated, and must fail soft when they are not.

Every user-visible string the agent does NOT write with a model is a literal in the
source, and a literal is exactly the kind of thing that gets added without anyone
thinking about the Chinese storefront. Before this, the whole progress trail and the
fallback suggestion chips were English underneath a Chinese answer.

No network, no DB.
"""
from app.agent import i18n, tools
from app.agent.graph import STEPS, fallback_suggestions
from app.schemas.agent import AgentState, Perception


def test_every_stage_name_and_description_has_chinese():
    missing = [s for _k, (_o, label, desc) in STEPS.items()
               for s in (label, desc) if s not in i18n.ZH]
    assert not missing, f"untranslated stage strings: {missing}"


def test_every_static_tool_label_has_chinese():
    missing = [spec.label for spec in tools.REGISTRY.values()
               if getattr(spec, "label", None) and spec.label not in i18n.ZH]
    assert not missing, f"untranslated tool labels: {missing}"


def test_an_untranslated_string_degrades_to_english_not_to_a_placeholder():
    assert i18n.ui("no such string here", "zh") == "no such string here"


def test_a_language_with_no_table_keeps_english():
    # Indonesian, French and the rest still get a translated REPLY, because a model
    # writes that. Only this furniture stays English.
    for lang in ("id", "fr", "ja", "", None):
        assert i18n.ui("Investigating", lang) == "Investigating"


def test_chinese_is_matched_on_the_language_or_the_locale():
    for lang in ("zh", "zh-CN", "ZH", "zh_TW"):
        assert i18n.ui("Investigating", lang) == "正在排查"


def test_a_chip_with_a_placeholder_is_translated_and_then_filled():
    out = i18n.ui("What if none of that fixes {product}?", "zh", product="Omni S2")
    assert "Omni S2" in out and "{product}" not in out
    assert out != "What if none of that fixes Omni S2?"


def test_fallback_chips_follow_the_reply_language():
    """A Chinese safety warning under English chips was the worst case: that is the
    turn the customer is most alarmed on."""
    state = AgentState(session_id="s", message_id="m", locale="zh")
    state.perception = Perception(safety_concern=True, language="zh")
    chips = fallback_suggestions(state)
    assert chips, "safety turn produced no chips at all"
    assert all(any("一" <= ch <= "鿿" for ch in c) for c in chips), chips


def test_fallback_chips_stay_english_for_an_english_turn():
    state = AgentState(session_id="s", message_id="m", locale="en")
    state.perception = Perception(safety_concern=True, language="en")
    chips = fallback_suggestions(state)
    assert chips and not any("一" <= ch <= "鿿" for c in chips for ch in c)


def test_every_customer_facing_prompt_carries_the_language_placeholder():
    """`str.format` ignores a keyword the template never mentions, so a prompt that
    lost its `{language}` keeps formatting cleanly and silently answers in English.
    That is exactly how the picker kept asking "Which S1 Pro do you have?" in English
    underneath a Chinese progress trail.
    """
    from app.agent import prompts

    must = ("PLAN_SYSTEM", "PICKER_QUESTION", "PICKER_FROM_PHOTO", "COMPOSE_SYSTEM")
    # getattr with a default would make a renamed constant look like a passing test,
    # which is the same silent-success trap one level up.
    unknown = [n for n in must if not hasattr(prompts, n)]
    assert not unknown, f"no such prompt: {unknown}"
    missing = [n for n in must if "{language}" not in getattr(prompts, n)]
    assert not missing, f"prompts that would answer in English regardless: {missing}"
