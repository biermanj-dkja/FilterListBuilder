# Filter List Builder

**v0.4.0**

A desktop utility that captures every domain a website contacts during a browsing session — including CDNs, authentication providers, iframes, and service workers — filters out known ad and tracking domains, and exports a clean CSV formatted for direct import into school content filtering products.

---

## Features

- **Manual Mode** — opens a headed browser you control; record traffic from any site by browsing normally
- **Batch Mode** — headless, reads a CSV of URLs and processes each one automatically
- **Scraper Mode** — extracts all href links from a single page and saves a `Domain + Link` CSV
- **Blocklist filtering** — uses the [StevenBlack hosts list](https://github.com/StevenBlack/hosts) to strip ad and tracker domains; cached locally for 4 hours
- **Wildcard toggle** — output `*.google.com` or exact subdomains depending on your target product; shared infrastructure domains (e.g. `cloudfront.net`, `amazonaws.com`) are never wildcarded regardless of toggle state
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
3. Configure your settings (wildcard, blocklist, export format, output folder)
4. Click **Start Session** — a browser window opens
5. Browse the site, log in, click through features you need to whitelist
6. Click **Stop & Save** when done — the CSV is written to your output folder

### Batch Mode

1. Prepare a CSV file with at least a `url` column (header required):
   ```
   url
   https://example.com
   https://another.com
   ```
2. Select **Batch Mode**, then click **Select URL List CSV**
3. Click **Start Session** — the tool processes each URL headlessly and waits 3 seconds per page for background traffic

### Scraper Mode

1. Select **Scraper Mode**
2. Enter the URL of the page you want to scrape
3. Optionally enable **Filter ad/tracking domains**
4. Click **Start Session** — the tool extracts all href links from the page DOM and saves a `scraped_links_{timestamp}.csv` with `Domain` and `Link` columns

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

> **Note:** This tool suggests allowlist candidates. It cannot guarantee that every captured domain is required or safe to allow. Wildcard mode can produce overly broad rules for shared infrastructure domains — the tool automatically suppresses wildcards for known shared platforms and logs a warning.

---

## Output Files

Files are saved to `~/Downloads` by default. You can change this with the **Select Output Folder** button.

| Mode | Filename pattern |
|---|---|
| Manual / Batch | `whitelist_{Product}_{DDMMYY-HHMM}.csv` |
| Scraper | `scraped_links_{DDMMYY-HHMM}.csv` |

---

## Blocklist Cache

The StevenBlack hosts list is cached at:

```
~/.cache/filter-list-builder/blocklist_cache.txt
```

It is refreshed automatically after 4 hours. To force a fresh download, delete this file.

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
