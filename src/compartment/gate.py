"""The store gate: memory shape enforced in code, at the door.

An instruction asking for one-or-two-sentence memories shipped in the MCP
handshake and was measured not to work: against a real vault the median
organic memory was 1,938 characters - session logs, headed lists, narrated
paragraphs - because prose advice competes with everything else in a busy
context and loses. A structured refusal arrives at the exact moment of the
mistake, names what to do instead, and is obeyed.

The gate protects RETRIEVAL, not storage. A blob embeds as the centroid of
its topics, so it matches many queries weakly; when it does match, one
relevant sentence drags kilobytes into the reader's context. One claim per
record is what makes the vector index sharp, the near-duplicate guard honest,
and the opinion-update bands (vault.store) meaningful.

Enforced only where an author can rephrase: agent- and operator-authored
stores. Restore and capture paths - imports, journal replay, pack and seed
installs, the Claude Code hook, the Hermes auto-capture writer - pass
verbatim text they have no license to rewrite, and bypass with _gate=False.
"""
from __future__ import annotations

import re

from .crypto import CryptoError

#: One claim, with room for a path or a version string. Two English sentences
#: run about 180-300 characters; a memory that needs more is several memories.
#: The `max_memory_chars` vault setting overrides; 0 or less disables the
#: length and layout checks (the stored-elsewhere check is about content, not
#: length, and stays on).
DEFAULT_MAX_CHARS = 200


class MemoryShapeError(CryptoError):
    """A store refused for its shape, with the fix in the message."""


_SENTENCEISH = re.compile(r"[.!?;]+(?:\s|$)")

#: How the refusal tells the caller to split. Parameterized because the
#: remedy differs per surface: the MCP tools have memory_store_many, the
#: Hermes plugin has only compartment_store called once per claim.
DEFAULT_MANY_HINT = "memory_store_many takes them all in one call"

#: Layout that only a multi-fact text has: list markers, headings, bold,
#: paragraph breaks. Each is the typographic signature of "several memories
#: wearing a coat" - single claims need none of them.
_BULLET = re.compile(r"^\s*(?:[-*•]|\d{1,3}[.)])\s", re.MULTILINE)
#: Multi-hash headings are unambiguous anywhere; a single leading "#" is a
#: heading only when the text has more than one line, because a one-line
#: claim starting "# noqa: E501 disables ..." is quoting code, not titling
#: a document.
_HEADING_MULTI = re.compile(r"^#{2,6}\s", re.MULTILINE)
_HEADING_ONE = re.compile(r"^#\s", re.MULTILINE)
#: Bold must be a CLOSED pair standing at word boundaries with no slash
#: inside, so glob patterns (src/**/*.py, **/test/**) and exponent notation
#: (2**10) - both legitimate single claims about the machine - pass.
_BOLD = re.compile(r"(?<!\S)\*\*[^\s*/][^*/]*?\*\*(?!\S)")

#: Narration that a fact was written down somewhere else. Where a fact is
#: recorded is not the fact; a memory saying "added to the guide" recalls
#: nothing when the guide is what you are trying to avoid opening. The verb
#: must land on a document-like noun: "stored in ~/.config" is a real claim
#: about the machine and passes.
#: The trailing lookahead spares nouns that name a PLACE rather than a
#: document: "saved in the notes app" and "written to the docs folder" are
#: claims about where files live, not narration that a fact was written up.
_DOC_NOUN = (r"(?:the|his|her|their|our|my|its|a|an)\s+"
             r"(?:\w+\s+){0,3}?"
             r"(?:guide|handbook|playbook|runbook|readme|changelog|worklog|"
             r"wiki|docs?|documentation|notes?|memo|spec|ledger|workbook|"
             r"journal)\b"
             r"(?!\s*(?:app|application|folder|director(?:y|ies)|dir|repo|"
             r"repository|bucket|drive)\b)")
_STORED_ELSEWHERE = re.compile(
    r"\b(?:added|recorded|logged|noted|documented|written|saved|stored|"
    r"updated|filed)\s+(?:in|into|to)\s+" + _DOC_NOUN + r"|"
    r"\b(?:guide|handbook|spec|doc|readme)\s+section\s+\S+",
    re.IGNORECASE)

#: Narration of the act of remembering. The storer's identity already lives
#: in the provenance metadata every record carries; repeating it in the text
#: is noise to every future reader. Warned rather than refused: "the user
#: decided X" is often itself the fact, and no pattern can tell a decision
#: from narration reliably.
_WHO_STORED = re.compile(
    r"\b(?:i|we|claude|the\s+(?:agent|assistant|model))\s+"
    r"(?:stored|saved|recorded|noted|remembered|wrote\s+down)\b|"
    r"\bon\s+(?:the\s+user'?s?|\w+'s)\s+(?:orders?|instructions?|behalf)\b|"
    r"\b(?:the\s+user|user)\s+(?:asked|told|instructed)\s+"
    r"(?:me|us|claude|the\s+agent)\b",
    re.IGNORECASE)


def claim_estimate(text: str) -> int:
    """How many claims a refused text appears to hold, floor 2.

    Only read on the failure path, so the floor is honest: a text long enough
    to refuse holds at least two claims or one claim told too slowly."""
    parts = [p for p in _SENTENCEISH.split(text) if p.strip()]
    return max(2, len(parts))


def rejection(text: str, max_chars: int = DEFAULT_MAX_CHARS,
              many_hint: str | None = None) -> str | None:
    """The reason this text is refused, or None if it passes.

    Checked on the CLAIM only - the caller strips the provenance clause
    before asking, so the stamp compartment appends can never push a valid
    claim over the limit. `many_hint` names the surface's own way to store
    several claims; the default names the MCP batch tool."""
    hint = many_hint or DEFAULT_MANY_HINT
    body = text.replace("\r\n", "\n").strip()
    if max_chars > 0:
        if len(body) > max_chars:
            n = claim_estimate(body)
            return (f"{len(body)} characters refused: a memory is ONE claim "
                    f"of at most {max_chars}. This looks like ~{n} claims - "
                    f"store each as its own memory; {hint}.")
        if "\n\n" in body:
            return ("Paragraph break refused: a memory is one claim, and a "
                    "text in paragraphs is several. Store each as its own "
                    f"memory; {hint}.")
        if _BULLET.search(body):
            return ("List refused: each list item is its own memory, one "
                    f"claim per record; {hint}.")
        if _HEADING_MULTI.search(body) or ("\n" in body
                                           and _HEADING_ONE.search(body)):
            return ("Heading refused: a memory needs no title, and a text "
                    f"that does is several memories; {hint}.")
        if _BOLD.search(body):
            return ("Markdown emphasis refused: a memory is plain prose read "
                    "back by a model - store the claim without formatting.")
    m = _STORED_ELSEWHERE.search(body)
    if m:
        return (f"Refused ({m.group(0)!r}): where a fact is written down "
                f"elsewhere is not the fact. Store the fact itself - what is "
                f"true, not where it is recorded.")
    return None


def narration_warning(text: str) -> str | None:
    """A warning for who-stored narration, or None. The store proceeds."""
    m = _WHO_STORED.search(text)
    if m:
        return (f"Stored, but {m.group(0)!r} narrates the act of remembering "
                f"- compartment already records who stored what as metadata. "
                f"State the fact alone; if the narration adds nothing, "
                f"re-store the claim cleanly and forget this record.")
    return None
