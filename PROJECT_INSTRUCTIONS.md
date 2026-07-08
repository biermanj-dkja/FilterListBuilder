# Project Instructions for Claude — Filter List Builder

## Project Name

The project is named **Filter List Builder** — one name, everywhere. It matches the GitHub repository: https://github.com/biermanj-dkja/FilterListBuilder

- App window title: `Filter List Builder`
- Main script: `filter_list_builder.py`
- Design doc heading: `Design Document: Filter List Builder (vX.Y.Z)`
- Cache directory: `~/.cache/filter-list-builder/`
- Do not reintroduce "Network Traffic Whitelister" or "Dynamic Whitelist Generator" in any file. Output filename patterns (`whitelist_*.csv`, `scraped_links_*.csv`) describe file contents, not the app, and are unaffected.

---

## Version Numbering

The project uses a `v{0}.{minor}.{patch}` scheme (e.g. `v0.3.1`). The `0.` prefix signals pre-stable software. Rules:

- Bump the **patch** version (`v0.3.0 → v0.3.1`) for fixes, corrections, renames, and doc-sync changes that add no new capability.
- Bump the **minor** version (`v0.3.x → v0.4.0`) for any additive change: new feature, new export format, new UI control or redesign, new mode.
- Reserve **v1.0.0** for a stable release: all export formats vendor-verified (currently Securly and Blocksi are pending) and no known correctness bugs.
- Architectural changes (replacing the GUI framework, changing the threading model, overhauling network interception) still get a minor bump while pre-1.0; after 1.0 they bump the major version.
- The version number appears in **three places, kept in sync**: the top of `README.md` (bold line under the title), the newest `CHANGELOG.md` entry, and the design document title heading. Do not put a version number in `filter_list_builder.py`.
- History note: versions were reset once on 2026-07-07 (v5.0 → v0.2.0, v5.1 → v0.3.0) because the old numbers tracked design-doc revisions rather than code maturity. This is recorded at the top of `CHANGELOG.md`. Do not renumber again.

---

## Changelog

The project uses a `CHANGELOG.md` file at the repo root. Format:

```markdown
# Changelog

## v0.3.1 — YYYY-MM-DD
### Added
- ...
### Changed
- ...
### Fixed
- ...

## v0.3.0 — 2026-07-06 (formerly v5.1)
...
```

Rules:
- One `##` entry per version. Never edit a past entry; only append new ones at the top. (The one-time renumbering of 2026-07-07 is the sole documented exception and must not be repeated.)
- Use the three sub-headings `Added`, `Changed`, and `Fixed` — omit any that have no entries for that version.
- Keep each bullet to one line where possible. Link to the relevant section of the design document if the change is architectural (e.g. `See design doc §4 — Lightspeed Format Detail`).
- The design document's `§8 Planned / Out of Scope` table is the source of truth for what is and isn't implemented. When a planned item ships, move it to the CHANGELOG entry and update the `§8` row to `Verified — implemented`.

---

## README Discipline

The README is a user-facing quick-start document, not a spec. Keep it short enough that a new user can read it in two minutes. Rules:

- The version line (`**vX.Y.Z**`) sits directly under the title and is updated with every release.
- The **Features**, **Installation**, **Running the App**, and **Usage** sections are always present and always up to date.
- The **Export Formats** table lists every supported product with a one-line note. It does not reproduce detailed format specs — those live in the design document (`§4`).
- Do **not** add a "Planned features" or "Roadmap" section to the README. That information lives in `design_document.md §8`.
- If a section grows beyond ~15 lines, look for content that belongs in the design document instead and move it there.
- The **Output Files** and **Blocklist Cache** sections should stay as-is: short, factual, path-specific.
- When a new mode or export format is added, add exactly one row to the relevant table and one bullet to the Features list. Nothing more.

---

## Design Document Ownership

`design_document.md` is the authoritative spec. Its filename is stable — the version lives in the title heading only, so the file is never renamed for a version bump. When making any non-trivial change:

1. Update the relevant section in the design document first.
2. Update the version in the title heading to match the release.
3. The `§8 Planned / Out of Scope` table must be kept current. If something ships, mark it `Verified — implemented`. If something is descoped, mark it `Removed` with a one-line reason.
4. Pending vendor verification items (currently Securly and Blocksi) are marked `*(pending verification)*` in the format table. The current best-effort output may be documented, but do not invent or assume a vendor-confirmed format — leave the marker until a sample file or vendor docs confirm it.

---

## Code File Rules

- There is one source file: `filter_list_builder.py`. Do not split it into modules unless the file exceeds ~600 lines and a split is explicitly requested.
- The UI redesign (when it ships) is the default; the previous layout remains available behind a `--classic` command-line flag in the same file. The classic layout is removal-candidate once the new UI has been stable for one minor version.
- Fix comments in the code use the pattern `# FIX N:` with a short description. When a fix is no longer experimental (i.e. it has been stable for a version), the comment can be simplified to a normal inline comment — don't keep accumulating `FIX N` labels indefinitely.
- `requirements.txt` lists only direct dependencies with minimum version pins (`>=`). Do not add transitive dependencies or pin to exact versions unless a specific compatibility issue is known.
