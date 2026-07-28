"""Regression cover for ACL grant precedence, vocabulary and isolation.

Four defects are pinned here:
  1. wildcard grants resolved first-match-wins in dict order
  2. non-"rw" grants ("none", typos) still permitted reads
  3. an empty caller entry fell back to the "*" entry and was promoted
  4. VaultConfig instances shared the module default's inner dicts
"""
import copy

import pytest

from compartment.acl import DEFAULT_CONFIG, AclError, VaultConfig


def _cfg(grants, caller="agent", default_ns="main"):
    cfg = VaultConfig()
    cfg.callers[caller] = {"default_namespace": default_ns, "grants": grants}
    return cfg


def _denied(cfg, caller, namespace, write):
    with pytest.raises(AclError):
        cfg.check(caller, namespace, write=write)


# -- 1. most specific match wins, regardless of dict order -------------------

@pytest.mark.parametrize("grants", [
    {"*": "rw", "secret/*": "ro"},
    {"secret/*": "ro", "*": "rw"},
])
def test_specific_wildcard_beats_broad_wildcard_in_both_orders(grants):
    cfg = _cfg(grants)
    assert cfg.grant_for("agent", "secret/keys") == "ro"
    _denied(cfg, "agent", "secret/keys", write=True)
    cfg.check("agent", "secret/keys", write=False)      # reads still fine
    # the broad grant still governs everything it is the best match for
    assert cfg.grant_for("agent", "main") == "rw"
    cfg.check("agent", "main", write=True)


@pytest.mark.parametrize("grants", [
    {"*": "rw", "a/*": "ro", "a/b/*": "rw"},
    {"a/b/*": "rw", "a/*": "ro", "*": "rw"},
    {"a/*": "ro", "*": "rw", "a/b/*": "rw"},
])
def test_longest_matching_prefix_wins(grants):
    cfg = _cfg(grants)
    assert cfg.grant_for("agent", "a/x") == "ro"        # a/* beats *
    assert cfg.grant_for("agent", "a/b/c") == "rw"      # a/b/* beats a/*
    assert cfg.grant_for("agent", "z") == "rw"          # only * matches


@pytest.mark.parametrize("grants", [
    {"*": "rw", "secret": "ro"},
    {"secret": "ro", "*": "rw"},
    {"secret/*": "rw", "secret": "ro", "*": "rw"},
])
def test_exact_namespace_key_beats_any_wildcard(grants):
    cfg = _cfg(grants)
    assert cfg.grant_for("agent", "secret") == "ro"
    _denied(cfg, "agent", "secret", write=True)


def test_exact_key_beats_wildcard_in_the_permissive_direction():
    cfg = _cfg({"secret/*": "ro", "secret/inbox": "rw"})
    assert cfg.grant_for("agent", "secret/inbox") == "rw"
    cfg.check("agent", "secret/inbox", write=True)
    assert cfg.grant_for("agent", "secret/other") == "ro"


def test_resolution_is_order_independent_across_permutations():
    import itertools
    pairs = [("*", "rw"), ("a/*", "ro"), ("a/b/*", "rw"), ("a/b/c", "none")]
    for perm in itertools.permutations(pairs):
        cfg = _cfg(dict(perm))
        assert cfg.grant_for("agent", "a/b/x") == "rw"
        assert cfg.grant_for("agent", "a/x") == "ro"
        _denied(cfg, "agent", "a/b/c", write=False)


# -- 2. grant vocabulary is enforced on reads too ----------------------------

def test_none_grant_denies_reads_and_writes():
    cfg = _cfg({"*": "rw", "secret": "none"})
    with pytest.raises(AclError):
        cfg.grant_for("agent", "secret")
    _denied(cfg, "agent", "secret", write=False)
    _denied(cfg, "agent", "secret", write=True)


def test_none_wildcard_denies_a_whole_subtree():
    cfg = _cfg({"*": "rw", "secret/*": "none"})
    _denied(cfg, "agent", "secret/keys", write=False)
    _denied(cfg, "agent", "secret/keys", write=True)
    cfg.check("agent", "main", write=True)


@pytest.mark.parametrize("bogus", ["RW", "read", "readwrite", "", "r w", True, 1])
def test_unrecognized_grant_values_deny_reads(bogus):
    cfg = _cfg({"secret": bogus})
    with pytest.raises(AclError):
        cfg.grant_for("agent", "secret")
    _denied(cfg, "agent", "secret", write=False)
    _denied(cfg, "agent", "secret", write=True)


def test_ro_still_reads_and_rw_still_writes():
    cfg = _cfg({"shared": "ro", "mine": "rw"})
    assert cfg.grant_for("agent", "shared") == "ro"
    cfg.check("agent", "shared", write=False)
    _denied(cfg, "agent", "shared", write=True)
    cfg.check("agent", "mine", write=True)


def test_unmatched_namespace_is_still_denied():
    cfg = _cfg({"mine": "rw"})
    with pytest.raises(AclError):
        cfg.grant_for("agent", "somewhere-else")


# -- 3. an existing caller entry never inherits "*" --------------------------

def test_empty_caller_entry_does_not_inherit_wildcard_grants():
    cfg = VaultConfig()                       # "*" entry grants rw everywhere
    cfg.callers["evil"] = {}                  # lock the caller down
    with pytest.raises(AclError):
        cfg.grant_for("evil", "main")
    _denied(cfg, "evil", "main", write=False)
    _denied(cfg, "evil", "main", write=True)
    # everyone else is untouched
    assert cfg.grant_for("someone-else", "main") == "rw"


def test_caller_entry_with_empty_grants_does_not_inherit_wildcard():
    cfg = VaultConfig()
    cfg.callers["evil"] = {"grants": {}}
    with pytest.raises(AclError):
        cfg.grant_for("evil", "main")


def test_caller_entry_grants_are_used_as_written_not_merged_with_wildcard():
    cfg = VaultConfig()
    cfg.callers["scoped"] = {"grants": {"mine": "rw"}}
    assert cfg.grant_for("scoped", "mine") == "rw"
    with pytest.raises(AclError):
        cfg.grant_for("scoped", "main")       # no inherited "*": "rw"


def test_empty_entry_still_reports_the_default_namespace():
    cfg = VaultConfig()
    cfg.callers["evil"] = {}
    assert cfg.default_namespace("evil") == "main"


def test_missing_caller_does_fall_back_to_wildcard():
    cfg = VaultConfig()
    assert cfg.grant_for("never-configured", "main") == "rw"


def test_no_wildcard_entry_denies_unknown_callers():
    cfg = VaultConfig()
    del cfg.callers["*"]
    cfg.callers["known"] = {"grants": {"*": "rw"}}
    with pytest.raises(AclError):
        cfg.grant_for("stranger", "main")


# -- 4. instances do not share mutable state ---------------------------------

def test_two_instances_do_not_share_grants():
    a, b = VaultConfig(), VaultConfig()
    assert a.callers is not b.callers
    assert a.callers["*"] is not b.callers["*"]
    assert a.callers["*"]["grants"] is not b.callers["*"]["grants"]
    a.callers["*"]["grants"]["secret/*"] = "none"
    assert "secret/*" not in b.callers["*"]["grants"]
    assert b.grant_for("anyone", "secret/keys") == "rw"


def test_mutating_an_instance_does_not_touch_the_module_default():
    before = copy.deepcopy(DEFAULT_CONFIG)
    cfg = VaultConfig()
    cfg.callers["*"]["grants"]["*"] = "none"
    cfg.callers["*"]["default_namespace"] = "elsewhere"
    cfg.settings["auto_lock_minutes"] = 1
    assert DEFAULT_CONFIG == before
    assert VaultConfig().grant_for("anyone", "main") == "rw"


def test_settings_are_not_shared_between_instances():
    a, b = VaultConfig(), VaultConfig()
    assert a.settings is not b.settings
    a.settings["auto_lock_minutes"] = 99
    assert b.settings["auto_lock_minutes"] == DEFAULT_CONFIG[
        "settings"]["auto_lock_minutes"]


def test_saved_and_reloaded_config_keeps_its_own_state(tmp_path):
    vault_path = str(tmp_path / "x.vault")
    cfg = VaultConfig()
    cfg.callers["scoped"] = {"grants": {"mine": "rw"}}
    cfg.save(vault_path)
    loaded = VaultConfig.load(vault_path)
    assert loaded.grant_for("scoped", "mine") == "rw"
    with pytest.raises(AclError):
        loaded.grant_for("scoped", "main")
    assert VaultConfig().grant_for("scoped", "main") == "rw"   # fresh default


# -- no-regression: the shipped default and packs ----------------------------

@pytest.mark.parametrize("namespace", ["main", "notes", "a/b/c", "", "*"])
def test_default_config_reads_and_writes_everywhere(namespace):
    cfg = VaultConfig()
    assert cfg.grant_for("anyone", namespace) == "rw"
    cfg.check("anyone", namespace, write=False)
    cfg.check("anyone", namespace, write=True)


def test_default_config_default_namespace_unchanged():
    assert VaultConfig().default_namespace("anyone") == "main"


@pytest.mark.parametrize("grants", [
    {"*": "rw"},
    {"*": "rw", "packs/*": "rw"},
    {"packs/starter": "rw", "*": "rw"},
])
def test_packs_stay_read_only_for_everyone(grants):
    cfg = _cfg(grants)
    assert cfg.grant_for("agent", "packs/starter") == "ro"
    cfg.check("agent", "packs/starter", write=False)
    _denied(cfg, "agent", "packs/starter", write=True)


def test_rw_only_configs_are_unaffected():
    """A config that only ever used "rw" must behave exactly as before."""
    cfg = _cfg({"mine": "rw", "team/*": "rw"}, default_ns="mine")
    for ns in ("mine", "team/a", "team/a/b"):
        assert cfg.grant_for("agent", ns) == "rw"
        cfg.check("agent", ns, write=True)
    with pytest.raises(AclError):
        cfg.grant_for("agent", "elsewhere")
    assert cfg.default_namespace("agent") == "mine"
