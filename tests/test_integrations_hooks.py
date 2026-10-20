"""Claude Code's settings.json, and the capture hook's place in it."""
import pytest

from compartment import claude_hooks


@pytest.mark.parametrize("settings", [
    '{"hooks": []}',
    '{"hooks": {"PostToolUse": {"a": 1}}}',
    '{"hooks": {"PostToolUse": ["x"]}}',
    '{"hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": "x"}]}}',
])
def test_settings_whose_hooks_are_the_wrong_shape_are_refused_untouched(
        settings, tmp_path):
    """A list where the hooks object belongs, or a string where a matcher
    group does, was walked as if it were right and died with
    AttributeError, which `integrate claude`, `hook status` and the
    uninstaller do not catch: a traceback instead of the file's name and
    what is wrong with it. An event that was an object was replaced."""
    p = tmp_path / "settings.json"
    p.write_text(settings, encoding="utf-8")
    for call in (lambda: claude_hooks.install("compartment", settings=p),
                 lambda: claude_hooks.uninstall(settings=p),
                 lambda: claude_hooks.is_installed(settings=p)):
        with pytest.raises(ValueError, match="refusing to touch it"):
            call()
    assert p.read_text(encoding="utf-8") == settings
