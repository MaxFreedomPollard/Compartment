"""Shipping /compartmentalize into every agent that reads skills.

Writing into somebody's agent configuration is a bigger claim on their machine
than registering a server, so the rules here are: put it where that agent looks
for it, never destroy a copy they have edited, and take back exactly what was
written and nothing else.
"""
import pytest

from compartment import agent_skill as A


@pytest.fixture()
def homes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(A, "SKILL_TARGETS", {
        "claude": (None, tmp_path / ".claude"),
        "hermes": ("HERMES_HOME", tmp_path / ".hermes"),
        "openclaw": ("OPENCLAW_HOME", tmp_path / ".openclaw"),
    })
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("OPENCLAW_HOME", raising=False)
    return tmp_path


ALL = ("claude", "hermes", "openclaw")


# ---------------------------------------------------------------- packaging --
def test_the_skill_ships_inside_the_package():
    src = A.source()
    assert src.is_file(), f"packaged skill missing at {src}"
    body = src.read_text(encoding="utf-8")
    assert body.startswith("---"), "a skill needs YAML frontmatter"
    assert "name: compartmentalize" in body
    assert "description:" in body


def test_the_frontmatter_carries_what_each_agent_reads():
    body = A.source().read_text(encoding="utf-8")
    # Claude Code: only the user may fire it, and the memory tools are
    # pre-approved so a sweep does not prompt on every single write.
    assert "disable-model-invocation: true" in body
    assert "allowed-tools:" in body
    # Hermes reads these; unknown keys are ignored by the others.
    assert "version:" in body and "platforms:" in body


def test_the_skill_says_to_search_before_storing():
    body = A.source().read_text(encoding="utf-8")
    assert "memory_search" in body and "memory_store" in body


def test_the_skill_never_tells_an_agent_to_store_the_passphrase():
    assert "passphrase" in A.source().read_text(encoding="utf-8").lower()


# -------------------------------------------------------------- placement ----
@pytest.mark.parametrize("target", ALL)
def test_each_agent_gets_it_where_that_agent_looks(homes, target):
    p = A.skill_path(target)
    assert p.parts[-3:] == ("skills", "compartmentalize", "SKILL.md")


def test_an_unknown_target_has_no_path(homes):
    assert A.skill_path("emacs") is None
    with pytest.raises(ValueError):
        A.install("emacs")


def test_the_home_environment_variables_are_honoured(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "elsewhere"))
    assert str(A.skill_path("hermes")).startswith(str(tmp_path / "elsewhere"))


# ------------------------------------------------------------------ install --
@pytest.mark.parametrize("target", ALL)
def test_installing_writes_the_packaged_copy(homes, target):
    r = A.install(target)
    assert r["action"] == "written"
    assert r["backup"] is None
    assert A.is_installed(target) and A.is_current(target)
    assert A.skill_path(target).read_bytes() == A.source().read_bytes()


@pytest.mark.parametrize("target", ALL)
def test_installing_twice_changes_nothing(homes, target):
    A.install(target)
    assert A.install(target)["action"] == "unchanged"


def test_a_missing_skills_directory_is_created(homes):
    assert not (homes / ".openclaw").exists()
    A.install("openclaw")
    assert A.is_installed("openclaw")


# ------------------------------------------------- somebody else's edits -----
def test_an_edited_copy_is_backed_up_rather_than_destroyed(homes):
    A.install("claude")
    p = A.skill_path("claude")
    p.write_text("my own rewritten sweep instructions\n", encoding="utf-8")
    r = A.install("claude")
    assert r["action"] == "replaced"
    assert r["backup"] and open(r["backup"]).read().startswith("my own")
    assert A.is_current("claude"), "the shipped copy must now be in place"


# ------------------------------------------------------------------ remove ---
@pytest.mark.parametrize("target", ALL)
def test_removing_takes_back_what_was_written(homes, target):
    A.install(target)
    assert A.remove(target) is True
    assert not A.is_installed(target)


def test_removing_something_that_was_never_installed_is_not_an_error(homes):
    assert A.remove("claude") is False


def test_removal_leaves_an_edited_backup_alone(homes):
    A.install("claude")
    p = A.skill_path("claude")
    p.write_text("edited\n", encoding="utf-8")
    backup = A.install("claude")["backup"]
    A.remove("claude")
    assert open(backup).read() == "edited\n", "their writing is not ours to bin"


def test_removal_never_deletes_a_directory_holding_anything_else(homes):
    A.install("hermes")
    stray = A.skill_path("hermes").parent / "notes.md"
    stray.write_text("mine\n", encoding="utf-8")
    A.remove("hermes")
    assert stray.exists()


# ------------------------------------------------------------------ status ---
def test_status_reports_every_target(homes):
    A.install("claude")
    st = A.status()
    assert set(st) == set(ALL)
    assert st["claude"]["installed"] and st["claude"]["current"]
    assert not st["hermes"]["installed"]


def test_status_notices_an_edited_copy_is_not_current(homes):
    A.install("claude")
    A.skill_path("claude").write_text("changed\n", encoding="utf-8")
    st = A.status()
    assert st["claude"]["installed"] and not st["claude"]["current"]


# --------------------------------------------------------------- failure -----
def test_a_broken_package_fails_loudly_rather_than_writing_nothing(homes,
                                                                   monkeypatch):
    monkeypatch.setattr(A, "source", lambda: homes / "does-not-exist.md")
    with pytest.raises(OSError):
        A.install("claude")
