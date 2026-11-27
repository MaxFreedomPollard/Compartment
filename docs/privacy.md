---
title: Privacy Policy
permalink: /privacy
---

# Compartment Privacy Policy

Effective 2026-08-26.

Compartment is offline software. This policy is short because the design
leaves almost nothing to disclose.

## Data collection

Compartment collects no data. There is no telemetry, no analytics, no crash
reporting, no account, and no sign-in. The developer receives nothing when
you install, run, or uninstall it.

## Usage and storage

Memories you or your AI agent store are written to an encrypted vault on
your own computer, AEAD-encrypted at rest including the vector index. The
encryption passphrase is chosen by you, never transmitted, and never stored
in plaintext; if it is lost, the vault contents are cryptographically
unrecoverable.

## Network access

The engine performs no network operations at runtime: embedding runs on a
local model that ships with the software, and the MCP server speaks stdio
only, opening no ports. The only commands that use the network are explicit
and user-invoked: installation itself, `compartment update`, and the
`compartment setup download-*` commands. Each downloads public files and
transmits no personal data.

## Third-party sharing

No data is shared with anyone, because none is collected. Nothing leaves
your machine unless you export it yourself with `compartment export`.

## Data retention and deletion

Everything lives in vault files on your machine, under your control. Delete
one memory with `memory_forget` or `compartment forget` (crypto-shredding
available); memories given an expiry date are swept according to your
settings; deleting the vault deletes everything. Uninstalling per the
README removes the software.

## Changes

Changes to this policy appear on this page with a new effective date.

## Contact

Max Freedom Pollard, via
[GitHub issues](https://github.com/MaxFreedomPollard/Compartment/issues).
