"""The write-decision function: store nearly everything, prioritize the user
and their machine, and treat every yes/no as a decision."""
from compartment import salience


def test_bare_yes_no_are_decisions_and_stored():
    for msg in ("OK", "yes", "no", "sure", "go ahead", "nope", "do it", "approved"):
        v = salience.assess_turn(msg, "Understood.")
        assert v.store, msg
        assert v.is_decision, msg
        assert v.importance == salience.IMP_DECISION
        assert v.polarity in ("affirm", "negate")


def test_ok_to_registry_question_is_a_high_value_decision():
    # The exact scenario: assistant asked to edit the registry, user said OK.
    v = salience.assess_turn("OK", "Can I edit the registry to accomplish this?")
    assert v.store and v.is_decision and v.polarity == "affirm"
    assert v.importance == salience.IMP_DECISION  # highest tier


def test_personal_facts_ranked_high():
    for msg in ("My timezone is Eastern", "I prefer tabs over spaces",
                "I hate popups", "call me Max", "I use a Mac"):
        v = salience.assess_turn(msg, "Noted.")
        assert v.store and v.importance == salience.IMP_PERSONAL
        assert "personal" in v.tags


def test_machine_facts_ranked_high():
    for msg in ("The staging server IP is 10.0.0.4",
                "The SSH key is stored in the ops folder",
                "The registry key changed", "Postgres runs on port 5432"):
        v = salience.assess_turn(msg, "Got it.")
        assert v.store and v.importance == salience.IMP_MACHINE
        assert "machine" in v.tags


def test_dual_signal_prefers_personal_tier_but_keeps_machine_tag():
    # "my ... key/folder" is both personal and machine; personal tier wins
    # (higher), and both tags are recorded.
    v = salience.assess_turn("My API key is in the vault folder", "Noted.")
    assert v.importance == salience.IMP_PERSONAL
    assert "personal" in v.tags and "machine" in v.tags


def test_pleasantries_kept_but_lowest_priority():
    v = salience.assess_turn("thanks", "You're welcome.")
    assert v.store  # NEVER dropped
    assert v.importance == salience.IMP_PLEASANTRY


def test_world_statement_stored_as_substantive():
    v = salience.assess_turn("The Eiffel Tower is in Paris", "Correct.")
    assert v.store and v.importance == salience.IMP_SUBSTANTIVE


def test_only_empty_is_dropped():
    assert not salience.assess_turn("", "hi").store
    assert not salience.assess_turn("   ", "").store


def test_importance_tiers_are_ordered():
    assert (salience.IMP_DECISION > salience.IMP_PERSONAL >
            salience.IMP_MACHINE > salience.IMP_SUBSTANTIVE >
            salience.IMP_PLEASANTRY)
