# Changelog

> **Version renumbering notice (2026-07-07):** The project previously versioned against design-document revisions (v5.x), which overstated the maturity of the code. Versions were reset one time to reflect the codebase: v5.0 → v0.2.0 and v5.1 → v0.3.0. The entries below were renumbered as part of this reset; their content is unchanged. This is a documented one-time exception to the "never edit a past entry" rule.

## v0.4.0 — 2026-07-07

### Added
- Redesigned default interface: grouped configuration sections (Session, Domain handling, Filtering, Export format, Output), a header bar with an Idle/Recording status indicator, and Start/Stop buttons pinned below the scroll area. See design doc §5 — GUI Layout.
- Export format selector is now a 2×3 button grid with a per-product constraint note (row limits, char limits, subdomain behaviour) shown on selection, from the new `FORMAT_NOTES` constant.
- Colour-coded network log: captured = green, warnings = amber, errors = red, blocked = dim, system = grey; degrades to plain text on older CustomTkinter versions.
- Live status bar under the log: unique captured, blocked, and warning counts refreshed by the main-thread queue poll, plus the session target (product → folder).
- Blocked domains are now visible: the first request to each blocked hostname logs a dimmed `[Blocked]` line (modern layout only; classic keeps silent discarding).
- Cache status helper line under the ad-list dropdown showing blocklist cache age or local-file format hints.
- `--classic` command-line flag preserving the previous v0.3.x layout unchanged; removal candidate after one stable minor version. See design doc §5.

### Fixed
- Wildcard hint/info labels re-packed at the bottom of the configuration panel when switching to/from Deledao or Lightspeed; they now live in a holder frame and swap in place (fixed in both layouts).
- Switching between two auto-subdomain products (Deledao → Lightspeed) overwrote the remembered wildcard state with the forced-off value; the previous state is now only saved when the toggle is active.

---

## v0.3.1 — 2026-07-07

### Changed
- Project renamed to **Filter List Builder** everywhere (app window title, README, design doc, script filename `filter_list_builder.py`) to match the GitHub repository. See design doc title.
- Version scheme reset to `0.minor.patch` — minor bumps for features, patch bumps for fixes; v1.0 reserved for a stable release with all export formats vendor-verified. Version now appears in `README.md` and `CHANGELOG.md`.
- Blocklist cache directory moved from `~/.cache/network-whitelister/` to `~/.cache/filter-list-builder/` (old directory can be deleted; the list re-downloads once).
- Shared infrastructure guardrail now uses **suffix matching**: a hostname triggers the guardrail if it equals or is a subdomain of any `SHARED_INFRASTRUCTURE_DOMAINS` entry. See design doc §2B.
- Bare URLs entered without a scheme now default to `https://` instead of `http://`.
- Securly and Blocksi best-effort output (no header, single domain column) is now documented in design doc §4; formats remain pending vendor verification.

### Fixed
- Multi-label guardrail entries (`blob.core.windows.net`, `storage.googleapis.com`) never matched under the old exact-base-domain check, so Azure blob and GCS hosts were wildcarded to dangerously broad rules. Suffix matching fixes this; the redundant `s3.amazonaws.com` entry was removed (covered by `amazonaws.com`).
- Scraper Mode eagerly downloaded the full blocklist at session start even with the filter toggle OFF. The blocklist now loads on demand inside the scraper, only when filtering is enabled. See design doc §4 — Scraper Mode Output.
- Starting a Scraper session with "Filter ad/tracking domains" ON while the ad-list source is "None" silently did nothing; this combination is now blocked at session start with an explanatory warning.
- The "Loaded N ad/tracking domains" log line counted raw parsed lines, overstating the filter size when the source contained duplicates; it now reports the deduplicated count.
- Scheme detection used `startswith("http")`, which false-positived on hostnames like `httpbin.org`; it now checks for `http://` / `https://` exactly.
- The shared-infrastructure warn-once check and the domain insert used separate lock acquisitions, allowing a rare duplicate warning; membership test, warning, and insert now run under a single lock.
- Design doc §5 mode selector row omitted Scraper Mode; corrected.

---

## v0.3.0 — 2026-07-06 *(formerly v5.1)*

### Added
- `SessionConfig` dataclass: all Tkinter widget/variable state is now snapshotted on the main thread via `build_session_config()` before the backend thread starts. The background thread reads only from this object — no Tkinter variables are accessed from worker threads. See design doc §6 — Threading & Safety Model.
- Shared infrastructure domain guardrail: when wildcard mode is ON, domains in the `SHARED_INFRASTRUCTURE_DOMAINS` set (e.g. `cloudfront.net`, `amazonaws.com`, `azureedge.net`) are never wildcarded. The exact captured hostname is preserved and a `[Warning]` is written to the log. A configurable warn-only vs auto-downgrade option is tracked for a future release. See design doc §2B — Domain Processing Strategy.
- GoGuardian 255-character-per-URL limit: domains exceeding the limit are now skipped at export time with a per-domain warning and a final skipped-count summary.
- GoGuardian 3 MB file size warning: estimated file size is checked after row-limit truncation and a warning is logged if the limit may be exceeded.
- Session summary block: the log footer now shows filename, output path, domains captured, and rows exported after every successful save.
- Blocklist download hardening: `raise_for_status()` is called on the HTTP response; a basic content sanity check confirms the response looks like a hosts file; the cache file is written atomically (temp file + `shutil.move`); if a download fails but an older cache exists, the older copy is used with a warning; if no cache exists at all, filtering is disabled for the session with a warning.

### Changed
- Batch Mode CSV picker button renamed from "Select Credentials CSV" to "Select URL List CSV" in the UI and all documentation.
- Wildcard toggle label changed from "Wildcard Mode (*.domain.com)" to "Allow all subdomains of captured root domains" with a helper hint line explaining the behaviour and risk.
- Securly and Blocksi export format radio buttons now labelled "Securly (experimental)" and "Blocksi (experimental)" in the UI. README and design doc updated to match.
- Lightspeed §4 example output in the design doc corrected: the previous example showed wildcard-prefixed domains (`*.google.com`), which contradicted the documented behaviour of forcing wildcard mode OFF for Lightspeed. Example now shows exact subdomains.
- Design doc §8 Planned/Out of Scope table expanded and updated to reflect all changes and newly tracked future items.

### Fixed
- Background thread was reading Tkinter `StringVar`, `BooleanVar`, and `Entry` widgets directly (e.g. `self.adlist_var.get()`, `self.wildcard_var.get()`, `self.url_entry.get()`). All such reads now happen exclusively on the main thread via `SessionConfig`.

---

## v0.2.0 — 2025-03-27 *(formerly v5.0)*

Initial tracked release.
