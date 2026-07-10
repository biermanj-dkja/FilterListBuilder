# Design Document: Filter List Builder (v0.6.0)

---

## 1. High-Level Concept

A desktop utility that acts as a network "sniffer" during web sessions. It identifies every domain required for a website to function — including CDNs, authentication providers, redirects, and nested iframes — filters out known advertisers and trackers, and exports a clean, product-formatted CSV for firewall whitelisting.

The tool supports two operational modes: a **Manual Mode** for interactive browsing sessions and a **Batch Mode** for automated, headless processing of a URL list. Output files are named with a timestamp and the target product name, and saved by default to the user's Downloads folder.

---

## 2. Core Logic & Technical Requirements

### A. Deep Interception (Playwright)

- **Context-level monitoring:** Use `browser_context.on('request', ...)` instead of page-level monitoring. This ensures capture of requests from iframes, service workers, and background processes such as embedded video players or OAuth popups.
- **Data capture:** For every request, extract the full URL and the resource type (e.g., `document`, `script`, `xhr`).
- **Thread safety:** All shared state (`captured_domains`, `raw_requests`, `root_domains`, `easylist_domains`) must be protected by a threading lock, as the Playwright request callback fires from a background thread.

### B. Domain Processing Strategy

- **Library:** Use `tldextract` to accurately separate subdomains from root domains (correctly handles multi-part TLDs such as `bbc.co.uk`).
- **Wildcard toggle:**
  - OFF (default since v0.5.0): Preserve the exact subdomain: `api.services.google.com`
  - ON (opt-in): Transform `api.services.google.com` → `*.google.com`
  - The default was flipped from ON to OFF in v0.5.0 because it was the single largest contributor to overly broad allow lists — one captured subdomain used to silently allow-list every subdomain of that registrable domain, including ones never observed.
- **First-party / third-party tagging:** Every captured domain is tagged by comparing its registrable domain (eTLD+1) against the registrable domain of the top-level page that triggered the request, resolved live via Playwright's frame chain (`request.frame` → `frame.page.main_frame` → `.url`) rather than a separately cached "current page" value — this avoids a race between an outgoing page's in-flight requests and a navigation event. A domain's top-level document request is always first-party by definition. Tagging uses OR-across-observations: a domain seen as first-party on one page and third-party on another (e.g. in a multi-site Manual session) is tagged first-party overall, biasing toward fewer false third-party flags.
- **Hit-count and page-count tracking:** Each captured domain tracks how many requests resolved to it (`hit_count`) and the set of distinct top-level page URLs that triggered a hit (`page_urls`, whose length is the page count). These are the confidence signals shown in the pre-export review table (§4) — a third-party domain seen once on one page is far more likely to be incidental tracker/widget noise than one seen repeatedly across many pages.
- **Shared infrastructure guardrail:** When wildcard mode is ON, hostnames on shared hosting platforms used by many unrelated parties are excluded from wildcarding. Matching is by **suffix**: a captured hostname triggers the guardrail if it equals an entry in `SHARED_INFRASTRUCTURE_DOMAINS` or is a subdomain of one, so multi-label entries such as `blob.core.windows.net` and `storage.googleapis.com` work as written (e.g. `myaccount.blob.core.windows.net` matches; `login.microsoftonline.com` does not; `fonts.googleapis.com` does not match `storage.googleapis.com` and is still wildcarded normally). For matched hosts, the exact captured hostname is preserved and a warning is logged. The current guardrail list is defined in `SHARED_INFRASTRUCTURE_DOMAINS` in the source code and includes: `amazonaws.com`, `cloudfront.net`, `azureedge.net`, `appspot.com`, `herokuapp.com`, `github.io`, `netlify.app`, `vercel.app`, `firebaseapp.com`, `googleusercontent.com`, `blob.core.windows.net`, `storage.googleapis.com`, `ondigitalocean.app`, `pages.dev`. A future option to let users choose between automatic downgrade (current behaviour) and a warning-only mode is tracked in §8.
- **Deduplication:** Maintain a running set of unique final-form domains to prevent repeats in the export.
- **Blocklist matching:** Before deduplicating, check the captured hostname and all of its subdomain variants against the blocklist. For example, for `ad.tracker.google.com`, check `ad.tracker.google.com`, `tracker.google.com`, and `google.com`. If any variant matches, discard the domain entirely from the curated allow list (it's still recorded in the raw URL data, tagged `Blocked=Yes`, if raw export is enabled — see §4).

### C. Filtering (Blocklist Integration)

- **Default sources (unioned):** [StevenBlack Hosts](https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts), [EasyList](https://easylist.to/easylist/easylist.txt), and [EasyPrivacy](https://easylist.to/easylist/easyprivacy.txt). StevenBlack alone (the only source before v0.5.0) is malware/ads-focused and misses many tracking/analytics domains that EasyList/EasyPrivacy catch; the three are downloaded, parsed, and unioned into one combined blocklist set.
- **Local file option:** User can supply a local `.txt` file in Hosts format, EasyList/AdGuard format, or plain-domain-per-line format.
- **No filtering option:** User can disable the blocklist entirely.
- **Caching:** Each of the three cloud sources is cached independently and reused for 4 hours before a fresh download is triggered for that source: `~/.cache/filter-list-builder/blocklist_cache.txt` (StevenBlack), `easylist_cache.txt`, `easyprivacy_cache.txt`. One source being stale or unreachable does not force re-fetching the others.
- **Download hardening:** The download call uses `raise_for_status()` and validates that the response looks like the expected format (a hosts-file sanity check for StevenBlack, an `[Adblock` header check for EasyList/EasyPrivacy) before accepting it. The cache file is written atomically (temp file + rename) so a partial download never corrupts the cache. If a download fails but an older cache exists, the older cache is used with a warning. If a source fails with no cache, that source is skipped; if all three fail with no caches, filtering is disabled for the session with a warning.
- **Parsing:** A shared parser (`parse_blocklist_lines()`) handles:
  1. Hosts file format: `0.0.0.0 domain.com` or `127.0.0.1 domain.com`
  2. EasyList / AdGuard network filter format: `||domain.com^` (options after `$` stripped)
  3. Plain domain format: `domain.com` — only enabled for StevenBlack Hosts and Local File sources, not EasyList/EasyPrivacy, which are mostly path/regex filters that would otherwise be misread as hostnames
  - `@@` exception rules (an explicit *unblock*, never a block) and cosmetic/element-hiding rules (`##`, `#@#`, `#?#`, `#$#`) are skipped — required for real EasyList/EasyPrivacy content, which is full of both.

---

## 3. Operational Modes

| Mode | Trigger | Browser Behavior |
|---|---|---|
| **Manual Mode** | "Start Session" button | **Headed.** Opens a visible browser. The user navigates and interacts manually. Recording continues until the user clicks "Stop & Save." |
| **Batch Mode** | "Start Session" button (with CSV selected) | **Headless.** Reads a CSV with a `url` column, navigates to each URL in sequence, waits 3 seconds for background traffic to settle, then moves to the next. |
| **Scraper Mode** | "Start Session" button (with URL entered) | **Headless.** Loads a single URL, extracts all `href` links from anchor tags in the DOM, and saves a `Domain` + `Link` + `Party` CSV. Does not capture network traffic, has no review step, and does not use the raw-URL export toggle (its own CSV is the complete output). Optional blocklist filtering available via toggle. |

> **Note:** Batch mode login automation (auto-filling `username`/`password` fields from the CSV) is a planned future feature. The CSV may include `username` and `password` columns for forward-compatibility, but they are not currently consumed.

---

## 4. Output & Export

### Scraper Mode Output

Scraper Mode produces a separate CSV file independent of the export format selector (which applies only to traffic capture modes). The file is always named:

```
scraped_links_{DDMMYY-HHMM}.csv
```

It contains three columns with a header row (the `Party` column was added in v0.5.0 — a breaking format change from the previous `Domain, Link` header):

| Column | Content |
|---|---|
| `Domain` | The hostname of the linked URL (e.g., `www.example.com`) |
| `Link` | The full resolved href (e.g., `https://www.example.com/about`) |
| `Party` | `First-party` if the link's registrable domain matches the scraped page's registrable domain, else `Third-party` |

Links are included as-is from the DOM — no wildcard processing is applied. The following link types are excluded automatically: `javascript:` pseudo-links, `mailto:` links, and any href with no parseable hostname.

If the "Filter ad/tracking domains" toggle is ON, the same blocklist logic used in traffic capture is applied — the blocklist is downloaded/cached on demand inside the scraper run, not eagerly at session start. Starting a Scraper session with the filter ON while the ad-list source is set to "None" is blocked by input validation with an explanatory warning.

---

### Pre-Export Review Table (Manual / Batch Mode)

Once a Manual or Batch session finishes (for Batch, this is once at the very end of the whole run, not per URL — the review step never blocks per-row headless automation mid-run), a modal review window opens on the main thread before the curated CSV is written. It lists every domain in `captured_domains` with:

- An **Include** checkbox — first-party domains default checked; third-party domains default checked only if seen on 2+ distinct pages or hit 5+ times (`REVIEW_DEFAULT_MIN_PAGE_COUNT` / `REVIEW_DEFAULT_MIN_HIT_COUNT`), otherwise unchecked and requiring deliberate opt-in
- Resource type, hit count, page count, and First/Third-party tag
- A per-row **Wildcard** checkbox (hidden for Deledao/Lightspeed, which force wildcard off), seeded from the session's wildcard setting, letting the user broaden a specific domain without re-running the session
- A search box and a First-party/Third-party/All filter, since large sessions can capture hundreds of domains
- Three actions: **Export selected** (applies the checkboxes), **Export all as captured** (bypasses review, today's-pre-v0.5.0 behaviour), **Cancel — export nothing** (also triggered by closing the window)

If nothing was captured (including every Scraper Mode run, which never populates `captured_domains`), the review step is skipped automatically and cleanup proceeds straight to saving. Raw URL export (below) is independent of this review — it still writes if enabled, even if the user cancels the curated export, since it's an unfiltered audit dump rather than the curated deliverable.

### Raw URL Export (optional, Manual / Batch Mode)

A checkbox in the Output section ("Also export raw URL data (CSV)"), off by default, controls whether every unique URL requested during the session is recorded and exported to a second file, `raw_urls_{DDMMYY-HHMM}.csv`:

| Column | Content |
|---|---|
| `URL` | The exact, full request URL (deduplicated — one row per unique URL string) |
| `Domain` | The request's hostname |
| `Resource Type` | Playwright's resource type for the request (`document`, `script`, `xhr`, `image`, etc.) |
| `Blocked` | `Yes` if the URL matched the blocklist (and was therefore excluded from the curated allow list), else `No` |
| `First-Party` | `Yes`/`No`, using the same page-comparison tagging as the curated domain data |

Unlike the curated allow list, this file includes blocked URLs — the point is visibility into everything the tool saw, not just what it decided to allow. Population is gated behind the toggle (not recorded at all if off) to avoid the memory cost on long sessions when the feature isn't wanted.

---

### File Naming

Output files are named using the pattern:

```
whitelist_{Product}_{DDMMYY-HHMM}.csv
raw_urls_{DDMMYY-HHMM}.csv          (optional, same timestamp as the curated file from the same session)
```

For example: `whitelist_Securly_270325-1430.csv`

### Default Save Location

The output folder defaults to the user's Downloads directory (`~/Downloads`). The user can override this with a folder picker in the UI. The selection persists for the duration of the session.

### Product Export Formats

The user selects one target product per session. The output CSV is formatted to be directly importable into that product's admin console.

| Product | Header Row | Columns | Notes |
|---|---|---|---|
| **Standard** | `Domain`, `Source` | `domain`, `resource_type` | General-purpose. Includes resource type in second column. |
| **GoGuardian** | `action`, `url` | `action` = `allow`, `url` = domain | Verified format. Domain written as captured (no `http://` prefix needed). Max 10,000 rules, 3 MB file, 255 chars per URL. Wildcard rules are supported under `url`. Optional third column `type` for YouTube video entries — not used by this tool. |
| **Securly** *(experimental)* | None *(best-effort, pending verification)* | Single domain column *(best-effort)* | Vendor format not yet confirmed — needs vendor docs or sample file. Current best-effort output: no header, one domain per row. Labelled experimental in UI. |
| **Blocksi** *(experimental)* | None *(best-effort, pending verification)* | Single domain column *(best-effort)* | Vendor format not yet confirmed — needs vendor docs or sample file. Current best-effort output: no header, one domain per row. Labelled experimental in UI. |
| **Lightspeed** | None | Single domain column | Verified format. No header row. One URL per row. Maximum 500 rows — tool will warn and truncate if exceeded. Auto-matches all subdomains, so wildcard mode is forced OFF when selected (same behaviour as Deledao). |
| **Deledao** | None | Single domain column | Verified format. No header row. One domain per line. Wildcard mode is forced OFF when Deledao is selected — Deledao auto-matches all subdomains, making wildcards unnecessary. |

> **To do:** Obtain vendor-confirmed format specs for Securly and Blocksi. All other formats are verified.

### GoGuardian Format Detail

GoGuardian's bulk import requires exactly two columns with headers:

```
action,url
allow,*.google.com
allow,*.googleapis.com
allow,*.gstatic.com
```

Key constraints to enforce at export time:
- Maximum **10,000 rows** — warn the user and truncate if the captured domain count exceeds this.
- Maximum **255 characters** per URL — skip any domain exceeding this limit and log a warning.
- Maximum **3 MB** file size — warn if the output file approaches this limit.
- The `action` column is always `allow` for this tool's use case.
- The optional `type` column (for YouTube video entries) is omitted.
- Domain format is flexible: `google.com` and `http://www.google.com` are treated identically by GoGuardian, so domains are written as captured without added prefixes.

### Deledao Format Detail

Deledao uses automatic subdomain matching, which means the domain format for export is simpler than other products — no wildcards are needed or recommended.

**Matching behavior (from vendor documentation):**
- `example.com` automatically matches every page in the domain, including `www.example.com` and `videos.example.com`. This is the correct form to export.
- `www.example.com` matches only pages on that specific subdomain — too narrow for this tool's use case.
- Wildcards (`*`) are supported but behave non-standardly depending on position and adjacent characters, and are explicitly described as "usually not necessary." This tool should **never output wildcard-prefixed domains** for Deledao.

**Domain transformation rule for Deledao export:**
Selecting Deledao automatically disables wildcard mode for the session. Because Deledao auto-matches all subdomains, exact subdomain output (wildcard OFF) is sufficient — `videos.example.com` is as complete as `*.example.com` for their purposes. This avoids any need for a special re-parsing step at export time.

UI behavior when Deledao is selected:
- Wildcard toggle is forced OFF and disabled (greyed out).
- A small info label appears near the toggle: *"Disabled — Deledao matches subdomains automatically."*
- The previous wildcard state is stored in memory.
- When the user switches to any other product, the toggle is re-enabled and restored to its previous state.

**CSV structure — confirmed:**
No header row. Single column. One domain per line. Example output:

```
videos.example.com
api.googleapis.com
cdn.cloudfront.net
```

**Still needed from Deledao:** Nothing — format is fully confirmed.

### Lightspeed Format Detail

Lightspeed's Custom Allow List import accepts one domain per row with no header. Because Lightspeed auto-matches all subdomains, wildcard mode is forced OFF when this product is selected — exact subdomains are written as captured.

Example output:

```
videos.example.com
api.googleapis.com
cdn.cloudfront.net
```

Key constraints to enforce at export time:
- Maximum **500 rows** — the tightest limit of any supported product. Tool warns and truncates if exceeded.
- No header row.
- Single domain column.
- **Wildcard mode is forced OFF** when Lightspeed is selected, identical to Deledao behaviour. The previous wildcard state is stored and restored when the user switches to another product. The UI toggle is greyed out with the label: *"Disabled — this product matches subdomains automatically."*
- No special domain transformation required — exact subdomains are written as captured.

---

## 5. GUI Layout (CustomTkinter)

Since v0.4.0 the application ships with two layouts in the same source file:

- **Modern layout (default):** grouped, sectioned configuration panel with a header status indicator, per-format constraint hints, a colour-coded log, and a live status bar.
- **Classic layout:** the previous v0.3.x layout, preserved unchanged behind the `--classic` command-line flag. It is a removal candidate once the modern layout has been stable for one minor version (tracked in §8).

Both layouts create the same widget and variable names, so all shared handlers (`toggle_mode`, `on_format_change`, `on_adlist_change`, session control, save/cleanup) run unmodified against either. Widgets that exist in only one layout (hint labels, status bar, header) are initialised to `None` and guarded with attribute checks.

### Modern Layout

**Visual style (v0.4.1).** The modern layout uses a restrained palette defined in the `UI_*` constants at the top of the source file: neutral outline-style secondary controls (CSV/blocklist/folder pickers, Stop), bordered input fields, and a single accent colour reserved for the primary action (Start), the selected mode segment, the selected export-format button, and the auto-subdomain info label. The selected mode segment is a light accent pill with dark text so unselected labels stay readable. All colours are (light mode, dark mode) tuples verified in both appearance modes; corner radius is unified via `UI_RADIUS`. The ad-list selector is a bordered read-only `CTkComboBox`. The classic layout keeps its original stock colours.

**Header bar** — application name on the left; a status indicator on the right showing `● Idle` (grey) or `● Recording` (green), updated at session start and end.

**Left panel — Configuration.** A `CTkScrollableFrame` divided into labelled sections separated by hairlines, with a pinned button row *below* the scroll area so Start/Stop remain reachable at any scroll position or window size.

| Section | Contents |
|---|---|
| **Session** | Mode segmented button (Manual / Batch / Scraper); per-mode inputs with a one-line helper hint (Manual: URL entry + "a visible browser opens" hint; Batch: CSV picker + filename label + settle-time hint; Scraper: URL entry + filter toggle) |
| **Domain handling** | Wildcard switch with a hint line explaining the transformation and the shared-host guardrail. When Deledao or Lightspeed is selected, the hint is replaced in place by an accent-coloured info line: *"Disabled — this product matches subdomains automatically."* Both labels live in a dedicated holder frame so swapping them never reorders the panel. |
| **Filtering** | Ad-list source dropdown; a live helper line beneath it showing cache age ("Cached 1.2 h ago — reused until 4 h old"), staleness, or the accepted local-file formats; local file picker shown only for "Local File" |
| **Export format** | 2×3 grid of selectable buttons (Standard, GoGuardian, Deledao, Lightspeed, Securly *(exp.)*, Blocksi *(exp.)*). The selected button gets an accent border; a note line beneath the grid shows that product's constraints (row limits, char limits, subdomain behaviour) from the `FORMAT_NOTES` constant. |
| **Output** | Folder picker button + current path label (defaults to `~/Downloads`); a checkbox for "Also export raw URL data (CSV)" (off by default), disabled automatically when Scraper Mode is selected since it never populates raw URL data |
| **Pinned row** | Start session (green) and Stop and save (red), side by side, always visible |

### Pre-Export Review Dialog

A modal `CTkToplevel` (see §4 — Pre-Export Review Table) appears after a Manual/Batch session finishes and before the curated CSV is written, unless nothing was captured. It contains a search box, a First-party/Third-party/All filter dropdown, a scrollable table (Include checkbox, Domain, Type, Hits, Pages, Party, and — for products that don't force wildcard off — a Wildcard checkbox), and three action buttons. It is shared, unstyled-by-layout code (independent of modern vs. classic), so both layouts get identical review behaviour; only the triggering checkbox in the Output section differs cosmetically between them.

**Right panel — Network log + status bar.** The read-only monospaced `CTkTextbox` colour-codes lines by prefix using text tags: captured/scraped = green, warnings = amber, errors = red, blocked = dim grey, system = grey. Tag colours are mid-brightness values chosen to stay readable in both light and dark appearance modes; if the installed CustomTkinter lacks tag passthrough, the log degrades gracefully to plain text. Below the log, a status bar shows live counters — **Captured** (unique final-form domains), **Blocked** (unique blocked hostnames), **Warnings** — refreshed on the main thread by the 100 ms queue poll, plus the session target on the right (`GoGuardian → Downloads`).

**Blocked-domain visibility (modern only):** the first request to each blocked hostname writes a dimmed `[Blocked] hostname — on blocklist` log line and increments the blocked counter. Repeat requests to the same hostname are counted once and not re-logged. The classic layout keeps the old behaviour (blocked domains silently discarded).

### Classic Layout (`--classic`)

The v0.3.x single-column layout: title, mode selector, dynamic mode inputs, wildcard switch + hint, ad-list dropdown, export format radio group, output picker, Start/Stop stacked at the bottom of the scrollable panel, and a plain uncoloured log on the right. Preserved for users who prefer it during the transition; one fix was applied to both layouts — the wildcard hint/info labels now live in a holder frame so swapping them on product change no longer re-packs the label at the bottom of the panel.

Sample log output (modern):
```
=== SESSION STARTED ===
[System] Using cached Blocklist (less than 4 hours old).
[System] Loaded 142831 unique ad/tracking domains into filter.
[System] Navigating to https://example.com
[Captured] *.example.com  <--  (document)
[Warning] Wildcard suppressed for shared infrastructure domain 'cloudfront.net' — using exact host 'xyz.cloudfront.net' instead.
[Captured] xyz.cloudfront.net  <--  (script)
[Captured] *.googleapis.com  <--  (xhr)
[Blocked] ads.doubleclick.net — on blocklist
────────────────────────────────────────
  File:     whitelist_GoGuardian_070726-1430.csv
  Location: /Users/name/Downloads
  Domains captured: 47
  Rows exported:    47
────────────────────────────────────────
=== SESSION ENDED ===
```

---

## 6. Threading & Safety Model

The GUI must remain responsive at all times. All Playwright operations run in a daemon thread. The following rules apply:

- **GUI updates from background threads are forbidden.** All widget `.configure()` calls, log writes, and session cleanup must execute on the main thread via the queue or `self.after()`.
- **All session settings are snapshotted before the thread starts.** `build_session_config()` is called on the main thread and produces a `SessionConfig` dataclass. The backend thread receives this object and never reads from Tkinter variables or widgets directly.
- **Shared state is protected by a lock.** `captured_domains`, `raw_requests`, `root_domains`, and `easylist_domains` are read and written under `threading.Lock()`.
- **`filedialog` is called only from the main thread.** Local blocklist file selection happens in the UI before the session thread is started. The resolved path is stored in `SessionConfig` and passed to the thread as a plain string.
- **Cleanup runs exactly once, but is no longer a single step.** A `_cleanup_called` flag (set on the backend thread, before anything is scheduled) still prevents the whole review/save chain from being triggered twice if the user manually closes the browser and also clicks Stop. Once triggered, `_trigger_cleanup()` schedules `_begin_review_or_save()` on the main thread via `self.after()`; this snapshots the captured data under lock, then either calls `save_and_cleanup()` directly (nothing captured) or opens the pre-export review dialog (§4/§5), which itself calls `save_and_cleanup()` once the user picks an action. No new thread is created for the dialog — it's built entirely on the main thread, since the backend thread has already exited by the time it appears.
- **Browser close is handled gracefully.** If the user closes the browser window during Manual Mode, the `page.wait_for_timeout()` call will throw. This is caught, `is_running` is set to False, and the session exits cleanly.

---

## 7. Required Python Libraries

```
playwright      # Browser automation and network interception
customtkinter   # Modern GUI framework
tldextract      # Accurate domain/suffix parsing
requests        # Fetching and caching the cloud blocklist
```

---

## 8. Planned / Out of Scope

| Feature | Status |
|---|---|
| CLI interface (run core logic without GUI) | Planned — not yet implemented |
| Batch mode login automation (auto-fill username/password) | Planned — CSV columns reserved |
| Progress bar for batch processing | Planned |
| Domain review table before export (classify, select, then export) | Verified — implemented in v0.5.0 (see §4/§5); was tracked here as "not before v1.0" since v0.3.0 |
| Scraper Mode per-page scrape trigger (scrape each visited page rather than one URL) | Planned — current Start/Stop model retained intentionally for multi-site sessions |
| First-party / third-party domain tagging with hit/page-count confidence signal | Verified — implemented in v0.5.0 |
| Raw URL export (deduplicated per-URL CSV, includes blocked URLs, opt-in toggle) | Verified — implemented in v0.5.0 |
| EasyList / EasyPrivacy as additional blocklist sources (unioned with StevenBlack) | Verified — implemented in v0.5.0 |
| Wildcard mode default changed from ON to OFF | Verified — implemented in v0.5.0 (breaking behaviour change) |
| Scraper Mode `Party` (first/third-party) column | Verified — implemented in v0.5.0 (breaking format change to `scraped_links_*.csv`) |
| Shared infrastructure guardrail extended beyond the current 14-entry list (e.g. Shopify, Wix, WordPress.com, Render.com, Fly.dev) | Planned — noted as a known gap; current list is not exhaustive |
| Root-domain-relative first-party detection (comparing against the user-entered target URL rather than the live per-request page) | Considered and rejected in v0.5.0 in favour of live per-request page comparison — more precise for Batch Mode (each CSV row is its own first-party root) and multi-site Manual sessions |
| Shared infrastructure wildcard guardrail: user-configurable mode (auto-downgrade vs warn-only) | Planned — current behaviour is auto-downgrade with log warning |
| GoGuardian export format | Verified — implemented |
| GoGuardian 255-char per URL limit | Verified — implemented (domains over limit skipped with warning) |
| GoGuardian 3 MB file size warning | Verified — implemented (estimated size checked after truncation) |
| Lightspeed export format | Verified — no header, single column, 500-row limit |
| Lightspeed wildcard forced OFF | Verified — implemented; example output in §4 corrected to show exact subdomains |
| Vendor format verification for Securly and Blocksi | Needs vendor docs or sample CSV; labelled experimental in UI; best-effort output documented in §4 |
| Deledao wildcard toggle behavior | Verified — auto-disable on select, restore on deselect |
| Deledao export format | Verified — no header, single domain column |
| SessionConfig dataclass (GUI state snapshot before thread start) | Verified — implemented; no Tkinter reads from background thread |
| Blocklist download hardening (raise_for_status, atomic write, fallback to old cache) | Verified — implemented |
| Shared infrastructure domain guardrail (suffix matching, wildcard suppression) | Verified — implemented; suffix match makes multi-label entries like `blob.core.windows.net` effective |
| Scraper Mode lazy blocklist load (on demand, only when filter enabled) | Verified — implemented |
| Scraper filter + ad-list "None" combination blocked at session start | Verified — implemented |
| Deduplicated blocklist domain count in log | Verified — implemented |
| Bare URLs default to `https://` with exact scheme detection | Verified — implemented |
| UI redesign (grouped sections, header status indicator, format button grid with constraint notes, coloured log, status bar) | Verified — implemented as default layout in v0.4.0 |
| `--classic` flag preserving the v0.3.x layout | Verified — implemented; removal candidate after modern layout is stable for one minor version |
| Blocked-domain log lines and session counters (captured/blocked/warnings) | Verified — implemented, modern layout only |
| Wildcard hint/info label holder frame (labels no longer re-pack at panel bottom) | Verified — implemented in both layouts |
| Modern layout polish pass (outline controls, unified accent palette, light/dark verified) | Verified — implemented in v0.4.1 |
| Classic layout removal | Planned — after one stable minor version on the modern layout |
| Batch CSV button relabelled from "Credentials CSV" to "URL List CSV" | Verified — implemented |
| Standalone build tooling (`build.py` + PyInstaller onedir spec, bundled Chromium, frozen-app browser path override) | Verified — implemented in v0.6.0; see `BUILD-INSTRUCTIONS.md` |
| Securly / Blocksi labelled experimental in UI | Verified — implemented |
| Session summary block in log footer | Verified — implemented |
| Bark support | Removed — no bulk upload feature |
