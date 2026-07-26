"""Deciding what to remember, and how strongly - offline, deterministic.

Policy (Compartment remembers aggressively, and prioritizes the user + their
machine over world trivia):

* STORE NEARLY EVERYTHING. The only turns dropped are genuinely empty ones.
  Every answer the user gives - yes, no, "ok", a number, a preference, an
  opinion, a stray fact - is information and is stored. A bare "OK" in reply
  to "Can I edit the registry?" is a consent decision, often the most
  important thing in the whole session; it is captured together with the
  question that produced it (see integrations resolving prior_question).

* IMPORTANCE TIERS drive recall ranking (search boosts higher-importance
  memories), so completeness never buries what matters:
    0.90  decision / consent / explicit "remember this"
    0.80  personal facts and preferences (about the user)
    0.75  the user's machine / environment / configuration
    0.55  other substantive statements (incl. world facts the user states)
    0.20  pure social pleasantries ("thanks", "lol") - kept, ranked last

* DEDUP is exact-match only (Vault.store's 0.97 cosine guard). We do NOT
  drop "near-duplicates": a second yes/no to a *different* question is new
  information, and its stored text (which includes the question) is not a
  near-duplicate anyway.

Compartment still never calls an LLM - the host model curates explicitly via
compartment_store / compartment_forget when it wants to distill or delete.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Importance tiers
IMP_DECISION = 0.90
IMP_PERSONAL = 0.80
IMP_MACHINE = 0.75
IMP_SUBSTANTIVE = 0.55
IMP_PLEASANTRY = 0.20

# Short answers that are DECISIONS when they reply to a question.
_AFFIRM = {
    "yes", "y", "yeah", "yep", "yup", "sure", "ok", "okay", "k", "kk", "fine",
    "go", "go ahead", "do it", "please do", "please", "sounds good", "yes please",
    "approved", "approve", "confirmed", "confirm", "correct", "right", "agreed",
    "agree", "absolutely", "definitely", "of course", "affirmative", "yea", "ya",
}
_NEGATE = {
    "no", "n", "nope", "nah", "don't", "dont", "do not", "stop", "cancel",
    "negative", "never", "no thanks", "no thank you", "not now", "skip", "pass",
}
# Purely phatic - kept but ranked last. Never dropped.
_PLEASANTRY = {
    "thanks", "thank you", "thx", "ty", "cheers", "lol", "haha", "hah", "nice",
    "cool", "great", "awesome", "perfect", "ok thanks", "thanks!", "great thanks",
    "no problem", "np", "you're welcome", "welcome", "hi", "hello", "hey",
}

# Signals that a statement is about the USER (personal facts / preferences).
_PERSONAL = re.compile(
    r"\b(i|i'?m|i am|my|mine|me|myself|we|our|us|call me|i'?ve|i'?ll|i'?d"
    r"|prefer|favou?rite|i like|i love|i hate|i want|i need|i use|i work"
    r"|i live|i own|i have|remember|don'?t forget|note that|for future"
    r"|from now on|always|never|birthday|anniversary)\b", re.IGNORECASE)

# Signals that a statement is about the MACHINE / environment / config.
_MACHINE = re.compile(
    r"\b(registry|regedit|hk(?:lm|cu|cr)|path|drive|disk|volume|folder"
    r"|director(?:y|ies)|file|config|configuration|install(?:ed|ing)?|setup"
    r"|version|build|password|passphrase|api key|token|secret|port|host(?:name)?"
    r"|ip address|network|wifi|vpn|proxy|env(?:ironment)? var|sudo|admin"
    r"|permission|chmod|service|daemon|driver|kernel|os|operating system"
    r"|windows|macos|mac os|linux|ubuntu|shell|terminal|command|script"
    r"|repo|repository|branch|commit|deploy|server|database|schema|account"
    r"|login|credential|certificate|firewall|backup)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Assessment:
    store: bool
    importance: float
    kind: str            # decision | personal | machine | substantive | chatter
    is_decision: bool    # True → integration should attach the prior question
    polarity: str        # affirm | negate | ""  (for decisions)
    tags: tuple[str, ...]
    reason: str


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower()).rstrip(".!?…,")


def assess_turn(user_text: str, assistant_text: str = "") -> Assessment:
    u = (user_text or "").strip()
    if not u:
        return Assessment(False, 0.0, "empty", False, "", (), "empty user turn")
    norm = _norm(u)

    # 1) Decision? (a yes/no-style answer) - highest priority, needs context.
    if norm in _AFFIRM or norm in _NEGATE:
        polarity = "affirm" if norm in _AFFIRM else "negate"
        return Assessment(True, IMP_DECISION, "decision", True, polarity,
                          ("decision", polarity), f"decision ({polarity})")

    # 2) Classify substance for ranking (still always stored).
    personal = bool(_PERSONAL.search(u))
    machine = bool(_MACHINE.search(u))
    tags: list[str] = []
    if personal:
        tags.append("personal")
    if machine:
        tags.append("machine")

    if personal:
        return Assessment(True, IMP_PERSONAL, "personal", False, "",
                          tuple(tags), "personal fact/preference")
    if machine:
        return Assessment(True, IMP_MACHINE, "machine", False, "",
                          tuple(tags), "machine/environment info")

    # 3) Pure pleasantry - kept, but ranked last.
    if norm in _PLEASANTRY:
        return Assessment(True, IMP_PLEASANTRY, "chatter", False, "",
                          ("chatter",), "social pleasantry (kept, low priority)")

    # 4) Everything else the user says is substantive information.
    return Assessment(True, IMP_SUBSTANTIVE, "substantive", False, "",
                      (), "substantive statement")
