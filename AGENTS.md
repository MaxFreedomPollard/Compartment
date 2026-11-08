# Repository Guidelines

## Language and Framing

Always use language that demonstrates what Compartment **provides**, and not what its specialization prevents.

- **Do say:** Compartment runs **100% offline, with zero network calls of any kind**, and stores memories in a portable, encrypted `.vault` file that users own and can copy, sync, or back up anywhere — local drives, USB, NAS, or any cloud storage provider — just like any other file.
- **Do not say:** *"zero cloud uploads"* or similar phrasing in release notes, docs, UI strings, or commit/PR descriptions. That wording makes it sound as if Compartment blocks or prevents users from uploading their vault to the cloud, when in fact a `.vault` is a self-contained encrypted file designed to be safely stored anywhere.

See [README.md](README.md) and [FORMAT.md](FORMAT.md) for architecture and storage format details.
