# Changelog

## v5.1 — 2026-07-06

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
- Design doc §8 Planned/Out of Scope table expanded and updated to reflect all v5.1 changes and newly tracked future items.

### Fixed
- Background thread was reading Tkinter `StringVar`, `BooleanVar`, and `Entry` widgets directly (e.g. `self.adlist_var.get()`, `self.wildcard_var.get()`, `self.url_entry.get()`). All such reads now happen exclusively on the main thread via `SessionConfig`.

---

## v5.0 — 2025-03-27

Initial tracked release.
