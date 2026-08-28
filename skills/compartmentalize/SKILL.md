---
name: compartmentalize
description: Sweep this conversation and save everything potentially worth knowing again into Compartment, the encrypted memory vault. Run it before compacting or summarizing so nothing is lost to the summary, or on its own at any point to bank the session.
metadata:
  tags: "memory, compartment, context, session, recall"
  version: "1.0.0"
  platforms: "macos, linux, windows"
---

**Save to Compartment before compacting.**

Sweep the entire conversation, including any part already summarized, and store
to Compartment everything potentially worth knowing again later that is not
common public knowledge. This is encrypted storage, so when in doubt, store it.

For each item, `memory_search` first, then `memory_store`: update the existing
memory when one already covers it, create a new one when none does.

**Always store, when present:** people, contacts and addresses. Passwords, API
keys, tokens, account IDs, and where each one lives. URLs, hostnames, repo and
release locations.

**Then properly associate and store** any observation, decision, opinion and
any thought that is not publicly available. Anything that would be expensive or
impossible to work out again from scratch.

**Also store the session itself - as several one-claim memories, never one
narrative:** one for what was asked, one for what it turned into, one for
what changed by the end, one for what is still open. That the work happened
and what it did is information in its own right, sometimes more useful than
any single detail inside it, and each of those claims is recalled on its
own.

**Skip:** common public knowledge, anything already stored unless it is an
update with additional or changed information, and the Compartment vault
passphrase itself.

Write each memory to stand alone: ONE claim of at most 200 characters by
default - the vault enforces this and refuses lists, headings and
paragraphs - with no pronouns pointing back at this conversation and no "as
discussed above". Never leave out information that is necessary to
understand the memory on its own. Several facts go through
memory_store_many, one record each, in one call. Store preferences,
stances and judgement calls with kind='opinion': opinions update instead
of accumulate, and one resembling a live opinion comes back for an
explicit supersedes=[old id] resend. Set namespace, tags and importance.
When a fact was established before today, pass discovered=YYYY-MM-DD; the
vault stamps every memory's dates itself, so never type dates into the
text.

Do not stop early. Finish the sweep, then report how many were stored and how
many updated.
