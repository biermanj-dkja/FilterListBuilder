# Filter List Builder

**v0.5.0**

A desktop utility that captures every domain a website contacts during a browsing session — including CDNs, authentication providers, iframes, and service workers — tags each one first-party or third-party, filters out known ad and tracking domains, and exports a clean, curated CSV formatted for direct import into school content filtering products. An optional second CSV exports every raw URL requested (including blocked ones) for auditing.

---

## Features

- **Manual Mode** — opens a headed browser you control; record traffic from any site by browsing normally
- **Batch Mode** — headless, reads a CSV of URLs and processes each one automatically
- **Scraper Mode** — extracts all href links from a single page and saves a `Domain + Link + Party` CSV
- **Blocklist filtering** — unions the [StevenBlack hosts list](https://github.com/StevenBlack/hosts), [EasyList](https://easylist.to/), and [EasyPrivacy](https://easylist.to/) to strip ad, malware, and tracking domains; each source cached locally for 4 hours
- **First-party / third-party tagging** — every captured domain is compared against the page that requested it, with hit-count and page-count tracked as a confidence signal
- **Pre-export review table** — after a session, review every captured domain (type, hit count, page count, first/third-party) and choose what to include before the curated CSV is written; third-party domains seen on only one page start unchecked by default
- **Wildcard toggle** — off by default (exact hostnames only); optionally output `*.google.com` instead of exact subdomains per your target product. Shared infrastructure domains (e.g. `cloudfront.net`, `amazonaws.com`) are never wildcarded regardless of toggle state
- **Raw URL export (optional)** — a second, deduplicated CSV of every unique URL requested during the session, including ones that hit the blocklist, tagged Blocked Yes/No and First-Party Yes/No
- **Product-specific export formats** — GoGuardian, Deledao, Lightspeed, Securly, Blocksi, and Standard
- **Timestamped output files** — saved to `~/Downloads` by default, folder is configurable
- **Redesigned interface** — grouped settings, colour-coded log, and a live session status bar; the previous layout is available with `--classic`

---

## Requirements

- Python 3.10 or higher
- A Chromium browser installed by Playwright (installed automatically, see below)

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/biermanj-dkja/FilterListBuilder.git
cd FilterListBuilder
```

### 2. Create and activate a virtual environment (recommended)

**macOS / Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
```

**Windows:**
```bash
python -m venv venv
venv\Scripts\activate
```

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 4. Install Playwright's Chromium browser

```bash
playwright install chromium
```

This downloads a local Chromium binary (~150 MB). It is only needed once.

---

## Running the App

```bash
python filter_list_builder.py
```

The GUI will open. No additional arguments are required. To use the previous (v0.3.x) interface layout instead:

```bash
python filter_list_builder.py --classic
```

---

## Usage

### Manual Mode

1. Select **Manual Mode** in the mode selector
2. Enter a starting URL (e.g. `https://example.com`)
3. Configure your settings (wildcard, blocklist, export format, output folder, raw export toggle)
4. Click **Start Session** — a browser window opens
5. Browse the site, log in, click through features you need to whitelist
6. Click **Stop & Save** when done — a review window opens showing every captured domain
7. Check/uncheck domains (and, if applicable, per-domain wildcard) and click **Export selected** — the curated CSV (and the raw CSV, if enabled) are written to your output folder

### Batch Mode

1. Prepare a CSV file with at least a `url` column (header required):
   ```
   url
   https://example.com
   https://another.com
   ```
2. Select **Batch Mode**, then click **Select URL List CSV**
3. Click **Start Session** — the tool processes each URL headlessly and waits 3 seconds per page for background traffic
4. Once the whole batch finishes, the same review step as Manual Mode appears once (not per URL) before export

### Scraper Mode

1. Select **Scraper Mode**
2. Enter the URL of the page you want to scrape
3. Optionally enable **Filter ad/tracking domains**
4. Click **Start Session** — the tool extracts all href links from the page DOM and saves a `scraped_links_{timestamp}.csv` with `Domain`, `Link`, and `Party` (First-party/Third-party) columns. Scraper Mode has no review step and does not use the raw-export toggle — its output is always this one file.

---

## Export Formats

| Product | Notes |
|---|---|
| **Standard** | Two columns: `Domain`, `Source`. General purpose. |
| **GoGuardian** | `action` + `url` columns. Max 10,000 rows, 255 chars per URL, 3 MB file. |
| **Deledao** | No header. Single domain column. Wildcard mode auto-disabled (Deledao matches subdomains automatically). |
| **Lightspeed** | No header. Single domain column. Max 500 rows. Wildcard mode auto-disabled. |
| **Securly** *(experimental)* | No header. Single domain column. Format is best-effort until vendor-confirmed. |
| **Blocksi** *(experimental)* | No header. Single domain column. Format is best-effort until vendor-confirmed. |

> **Note:** This tool suggests allowlist candidates. It cannot guarantee that every captured domain is required or safe to allow. The pre-export review table and first/third-party tagging are there to help you judge borderline domains — always take a look before importing into a production filter. Wildcard mode (off by default) can produce overly broad rules for shared infrastructure domains — the tool automatically suppresses wildcards for known shared platforms and logs a warning.

---

## Output Files

Files are saved to `~/Downloads` by default. You can change this with the **Select Output Folder** button.

| Mode | Filename pattern | Notes |
|---|---|---|
| Manual / Batch | `whitelist_{Product}_{DDMMYY-HHMM}.csv` | The curated allow list, after review |
| Manual / Batch *(optional)* | `raw_urls_{DDMMYY-HHMM}.csv` | Every unique URL requested, including blocked ones; only written when "Also export raw URL data" is checked |
| Scraper | `scraped_links_{DDMMYY-HHMM}.csv` | `Domain`, `Link`, `Party` columns |

---

## Blocklist Cache

Three blocklist sources are cached independently, each refreshed after 4 hours:

```
~/.cache/filter-list-builder/blocklist_cache.txt       (StevenBlack Hosts)
~/.cache/filter-list-builder/easylist_cache.txt        (EasyList)
~/.cache/filter-list-builder/easyprivacy_cache.txt     (EasyPrivacy)
```

To force a fresh download of all three, delete these files. If one source fails to download but the others succeed, filtering still works with the sources that loaded (a warning is logged for the failed source).

---

## Project Structure

```
FilterListBuilder/
├── filter_list_builder.py   # Main application
├── design_document.md        # Authoritative spec
├── CHANGELOG.md
├── requirements.txt          # Python dependencies
├── README.md
├── .gitignore
└── LICENSE
```

---

## Contributing

Pull requests are welcome. For significant changes please open an issue first to discuss what you'd like to change.

---

## License

[MIT](LICENSE)
