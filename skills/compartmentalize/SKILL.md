---
name: compartmentalize
description: Sweep this conversation and save everything potentially worth knowing again into Compartment, the encrypted memory vault. Run it before compacting or summarizing so nothing is lost to the summary, or on its own at any point to bank the session.
version: 1.0.0
platforms: [macos, linux, windows]
disable-model-invocation: true
metadata:
  tags: "memory, compartment, context, session, recall"
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

**Also store the session itself, as its own memory:** what was asked, what it
actually turned into, roughly how long it ran, what changed by the end, and
what is still open. That the work happened and what it did is information in
its own right, sometimes more useful than any single detail inside it.

**Skip:** common public knowledge, anything already stored unless it is an
update with additional or changed information, and the Compartment vault
passphrase itself.

Write each memory to stand alone: one fact or one narrative, dated where a date
matters, no pronouns pointing back at this conversation and no "as discussed
above". Never leave out information that is necessary to understand the memory
on its own. Do include any and all relevant metadata or succinct associative
information. Set namespace, tags and importance. Put the time of the events
themselves in the text, as absolute dates; the record's own timestamp is
written automatically and only records when the memory was saved.

Do not stop early. Finish the sweep, then report how many were stored and how
many updated.
