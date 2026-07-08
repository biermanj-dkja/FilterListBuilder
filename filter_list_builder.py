import customtkinter as ctk
from tkinter import filedialog, messagebox
import threading
import queue
import requests
import tldextract
import csv
import os
import time
import re
import tempfile
import shutil
from dataclasses import dataclass, field
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

# Path for blocklist cache in a stable user-writable location
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "filter-list-builder")
CACHE_FILE = os.path.join(CACHE_DIR, "blocklist_cache.txt")

# Domains where wildcard mode is unsafe because the domain is shared
# infrastructure used by many unrelated parties. Matching is by suffix:
# a captured hostname triggers the guardrail if it equals an entry or is a
# subdomain of one, so multi-label entries like 'blob.core.windows.net'
# work as written. For matched hosts, the exact subdomain is preserved and
# a warning is logged.
SHARED_INFRASTRUCTURE_DOMAINS = {
    "amazonaws.com",
    "cloudfront.net",
    "azureedge.net",
    "appspot.com",
    "herokuapp.com",
    "github.io",
    "netlify.app",
    "vercel.app",
    "firebaseapp.com",
    "googleusercontent.com",
    "blob.core.windows.net",
    "storage.googleapis.com",
    "ondigitalocean.app",
    "pages.dev",
}


def match_shared_infrastructure(hostname: str):
    """Return the matching SHARED_INFRASTRUCTURE_DOMAINS entry if `hostname`
    equals or is a subdomain of any entry, else None."""
    for entry in SHARED_INFRASTRUCTURE_DOMAINS:
        if hostname == entry or hostname.endswith("." + entry):
            return entry
    return None


@dataclass
class SessionConfig:
    """Snapshot of all GUI settings captured on the main thread before the
    backend thread starts. The backend reads only from this object — never
    from Tkinter variables or widgets."""
    mode: str
    target_url: str
    batch_csv_path: str
    scraper_url: str
    scraper_filter: bool
    wildcard: bool
    adlist_source: str
    local_blocklist_path: str
    export_format: str
    output_folder: str


class FilterListBuilderApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Filter List Builder")
        self.geometry("1000x750")

        # Application State
        self.is_running = False
        self.captured_domains = {}
        self.log_queue = queue.Queue()
        self.easylist_domains = set()
        self._domains_lock = threading.Lock()
        self._cleanup_called = False
        self._local_blocklist_path = ""
        # Wildcard state to restore after leaving a product that auto-matches
        # subdomains (Deledao, Lightspeed)
        self._wildcard_before_autosub = True

        self.output_folder = os.path.join(os.path.expanduser("~"), "Downloads")
        self.batch_csv_path = ""

        self.setup_ui()
        self.check_queue()

    def setup_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=2)
        self.grid_rowconfigure(0, weight=1)

        # ================= LEFT PANEL (Controls) =================
        self.controls_frame = ctk.CTkScrollableFrame(self)
        self.controls_frame.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")

        self.title_label = ctk.CTkLabel(self.controls_frame, text="Configuration", font=ctk.CTkFont(size=20, weight="bold"))
        self.title_label.pack(pady=(15, 20), padx=10)

        self.mode_var = ctk.StringVar(value="Manual Mode")
        self.mode_selector = ctk.CTkSegmentedButton(self.controls_frame, values=["Manual Mode", "Batch Mode", "Scraper Mode"],
                                                    variable=self.mode_var, command=self.toggle_mode)
        self.mode_selector.pack(pady=10, padx=20, fill="x")

        self.dynamic_frame = ctk.CTkFrame(self.controls_frame, fg_color="transparent")
        self.dynamic_frame.pack(pady=10, padx=20, fill="x")

        self.url_entry = ctk.CTkEntry(self.dynamic_frame, placeholder_text="Enter Target URL (e.g., https://example.com)")
        self.url_entry.pack(fill="x", pady=5)

        self.batch_btn = ctk.CTkButton(self.dynamic_frame, text="Select URL List CSV", command=self.select_batch_csv)
        self.batch_label = ctk.CTkLabel(self.dynamic_frame, text="No CSV selected", text_color="gray", font=("Arial", 10))

        # --- Scraper Mode Widgets ---
        self.scraper_url_entry = ctk.CTkEntry(self.dynamic_frame, placeholder_text="Enter URL to scrape (e.g., https://example.com)")
        self.scraper_filter_var = ctk.BooleanVar(value=False)
        self.scraper_filter_switch = ctk.CTkSwitch(self.dynamic_frame, text="Filter ad/tracking domains", variable=self.scraper_filter_var)

        self.wildcard_var = ctk.BooleanVar(value=True)
        self.wildcard_switch = ctk.CTkSwitch(self.controls_frame, text="Allow all subdomains of captured root domains",
                                              variable=self.wildcard_var)
        self.wildcard_switch.pack(pady=(15, 2), padx=20, anchor="w")
        self.wildcard_hint_label = ctk.CTkLabel(
            self.controls_frame,
            text="e.g. cdn.example.com → *.example.com. Risky for shared hosts like cloudfront.net.",
            text_color="gray",
            font=("Arial", 10),
            wraplength=220,
            justify="left",
        )
        self.wildcard_hint_label.pack(padx=20, anchor="w", pady=(0, 4))

        # Info label shown when a product auto-matches subdomains (Deledao, Lightspeed)
        self.wildcard_info_label = ctk.CTkLabel(
            self.controls_frame,
            text="Disabled — this product matches subdomains automatically.",
            text_color="gray",
            font=("Arial", 10)
        )
        # Hidden by default; shown when Deledao or Lightspeed is selected

        self.adlist_label = ctk.CTkLabel(self.controls_frame, text="Ad-list Source:")
        self.adlist_label.pack(padx=20, anchor="w")
        self.adlist_var = ctk.StringVar(value="Cloud Blocklist (Ads/Tracking)")
        self.adlist_dropdown = ctk.CTkOptionMenu(
            self.controls_frame,
            values=["Cloud Blocklist (Ads/Tracking)", "Local File", "None"],
            variable=self.adlist_var,
            command=self.on_adlist_change
        )
        self.adlist_dropdown.pack(pady=(0, 5), padx=20, fill="x")

        # Local file picker — shown only when "Local File" is selected
        self.local_blocklist_frame = ctk.CTkFrame(self.controls_frame, fg_color="transparent")
        self.local_blocklist_btn = ctk.CTkButton(self.local_blocklist_frame, text="Select Blocklist File",
                                                  command=self.select_local_blocklist)
        self.local_blocklist_btn.pack(fill="x", pady=(0, 2))
        self.local_blocklist_label = ctk.CTkLabel(self.local_blocklist_frame, text="No file selected",
                                                   text_color="gray", font=("Arial", 10))
        self.local_blocklist_label.pack(fill="x")
        self.local_blocklist_frame.pack_forget()

        self.export_label = ctk.CTkLabel(self.controls_frame, text="Export Format:")
        self.export_label.pack(padx=20, anchor="w", pady=(10, 0))

        self.export_format_var = ctk.StringVar(value="Standard")
        self.radio_frame = ctk.CTkFrame(self.controls_frame, fg_color="transparent")
        self.radio_frame.pack(padx=20, fill="x", pady=(0, 15))

        # Securly and Blocksi are labelled experimental until vendor-confirmed
        formats = [
            ("Standard",              "Standard"),
            ("Securly (experimental)", "Securly"),
            ("GoGuardian",             "GoGuardian"),
            ("Deledao",                "Deledao"),
            ("Blocksi (experimental)", "Blocksi"),
            ("Lightspeed",             "Lightspeed"),
        ]
        for i, (label, value) in enumerate(formats):
            rb = ctk.CTkRadioButton(
                self.radio_frame, text=label,
                variable=self.export_format_var, value=value,
                command=self.on_format_change
            )
            rb.grid(row=i // 2, column=i % 2, sticky="w", pady=5, padx=2)

        self.output_btn = ctk.CTkButton(self.controls_frame, text="Select Output Folder", command=self.select_output_folder)
        self.output_btn.pack(pady=10, padx=20, fill="x")
        self.output_label = ctk.CTkLabel(self.controls_frame, text=self.output_folder, text_color="gray", font=("Arial", 10))
        self.output_label.pack(padx=20, fill="x")

        self.start_btn = ctk.CTkButton(self.controls_frame, text="Start Session", fg_color="green",
                                        hover_color="darkgreen", command=self.start_session)
        self.start_btn.pack(pady=(20, 10), padx=20, fill="x")

        self.stop_btn = ctk.CTkButton(self.controls_frame, text="Stop & Save", fg_color="red",
                                       hover_color="darkred", state="disabled", command=self.stop_session)
        self.stop_btn.pack(pady=(0, 20), padx=20, fill="x")

        # ================= RIGHT PANEL (Network Log) =================
        self.log_frame = ctk.CTkFrame(self)
        self.log_frame.grid(row=0, column=1, padx=(0, 10), pady=10, sticky="nsew")

        self.log_label = ctk.CTkLabel(self.log_frame, text="Network Log (Real-time)", font=ctk.CTkFont(size=16, weight="bold"))
        self.log_label.pack(pady=(15, 10), padx=10, anchor="w")

        self.log_textbox = ctk.CTkTextbox(self.log_frame, state="disabled", font=("Courier", 12))
        self.log_textbox.pack(padx=10, pady=(0, 10), fill="both", expand=True)

    # ── UI helpers ──────────────────────────────────────────────────────────

    def on_format_change(self):
        """Called when the export format radio selection changes.
        Deledao and Lightspeed force wildcard OFF (and disable the toggle)
        because both auto-match subdomains. Switching away restores the previous state."""
        if self.export_format_var.get() in ("Deledao", "Lightspeed"):
            self._wildcard_before_autosub = self.wildcard_var.get()
            self.wildcard_var.set(False)
            self.wildcard_switch.configure(state="disabled")
            self.wildcard_hint_label.pack_forget()
            self.wildcard_info_label.pack(padx=20, anchor="w", pady=(0, 12))
        else:
            self.wildcard_var.set(self._wildcard_before_autosub)
            self.wildcard_switch.configure(state="normal")
            self.wildcard_info_label.pack_forget()
            self.wildcard_hint_label.pack(padx=20, anchor="w", pady=(0, 4))

    def on_adlist_change(self, value):
        """Show the local file picker only when 'Local File' is selected."""
        if value == "Local File":
            self.local_blocklist_frame.pack(padx=20, fill="x", pady=(0, 10))
        else:
            self.local_blocklist_frame.pack_forget()

    def select_local_blocklist(self):
        """Called on the main thread — safe to open a file dialog."""
        path = filedialog.askopenfilename(title="Select Blocklist File", filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")])
        if path:
            self._local_blocklist_path = path
            self.local_blocklist_label.configure(text=os.path.basename(path))

    def toggle_mode(self, selected_mode):
        # Hide all dynamic widgets first
        self.url_entry.pack_forget()
        self.batch_btn.pack_forget()
        self.batch_label.pack_forget()
        self.scraper_url_entry.pack_forget()
        self.scraper_filter_switch.pack_forget()

        if selected_mode == "Manual Mode":
            self.url_entry.pack(fill="x", pady=5)
        elif selected_mode == "Batch Mode":
            self.batch_btn.pack(fill="x", pady=5)
            self.batch_label.pack(fill="x")
        else:  # Scraper Mode
            self.scraper_url_entry.pack(fill="x", pady=5)
            self.scraper_filter_switch.pack(anchor="w", pady=(0, 5))

    def select_batch_csv(self):
        path = filedialog.askopenfilename(filetypes=[("CSV Files", "*.csv")])
        if path:
            self.batch_csv_path = path
            self.batch_label.configure(text=os.path.basename(path))

    def select_output_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.output_folder = folder
            self.output_label.configure(text=folder)

    def write_log(self, message):
        self.log_queue.put(message)

    def check_queue(self):
        while not self.log_queue.empty():
            msg = self.log_queue.get()
            self.log_textbox.configure(state="normal")
            self.log_textbox.insert("end", msg + "\n")
            self.log_textbox.see("end")
            self.log_textbox.configure(state="disabled")
        self.after(100, self.check_queue)

    # ── Session control ──────────────────────────────────────────────────────

    def build_session_config(self) -> SessionConfig:
        """Snapshot all Tkinter widget/variable state on the main thread.
        The backend thread reads only from the returned dataclass."""
        target_url = self.url_entry.get().strip()
        if target_url and not target_url.startswith(("http://", "https://")):
            target_url = "https://" + target_url

        scraper_url = self.scraper_url_entry.get().strip()
        if scraper_url and not scraper_url.startswith(("http://", "https://")):
            scraper_url = "https://" + scraper_url

        return SessionConfig(
            mode=self.mode_var.get(),
            target_url=target_url,
            batch_csv_path=self.batch_csv_path,
            scraper_url=scraper_url,
            scraper_filter=self.scraper_filter_var.get(),
            wildcard=self.wildcard_var.get(),
            adlist_source=self.adlist_var.get(),
            local_blocklist_path=self._local_blocklist_path,
            export_format=self.export_format_var.get(),
            output_folder=self.output_folder,
        )

    def start_session(self):
        if not self.output_folder:
            messagebox.showwarning("Warning", "Please select an Output Folder first.")
            return

        if self.mode_var.get() == "Manual Mode" and not self.url_entry.get():
            messagebox.showwarning("Warning", "Please enter a Target URL.")
            return

        if self.mode_var.get() == "Batch Mode" and not self.batch_csv_path:
            messagebox.showwarning("Warning", "Please select a URL List CSV.")
            return

        if self.mode_var.get() == "Scraper Mode" and not self.scraper_url_entry.get():
            messagebox.showwarning("Warning", "Please enter a URL to scrape.")
            return

        if (self.mode_var.get() == "Scraper Mode" and self.scraper_filter_var.get()
                and self.adlist_var.get() == "None"):
            messagebox.showwarning(
                "Warning",
                "'Filter ad/tracking domains' is enabled but the Ad-list Source is set to None.\n"
                "Select a blocklist source or turn off the filter."
            )
            return

        if self.adlist_var.get() == "Local File" and not self._local_blocklist_path:
            messagebox.showwarning("Warning", "Please select a Local Blocklist file.")
            return

        # Snapshot GUI state before the thread starts — the backend reads
        # only from this config object, never from Tkinter widgets directly.
        config = self.build_session_config()

        self.is_running = True
        self._cleanup_called = False
        self.captured_domains.clear()
        self.easylist_domains.clear()

        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.mode_selector.configure(state="disabled")

        self.log_textbox.configure(state="normal")
        self.log_textbox.delete("1.0", "end")
        self.log_textbox.configure(state="disabled")

        self.write_log("=== SESSION STARTED ===")
        threading.Thread(target=self.run_backend, args=(config,), daemon=True).start()

    def stop_session(self):
        self.write_log("[System] Stopping session...")
        self.is_running = False

    # ── Backend (runs in daemon thread) ──────────────────────────────────────

    def fetch_easylist(self, config: SessionConfig):
        """Downloads / loads the blocklist. Reads only from config (no Tkinter access)."""
        self.write_log("[System] Preparing Blocklist...")
        try:
            if config.adlist_source == "Cloud Blocklist (Ads/Tracking)":
                os.makedirs(CACHE_DIR, exist_ok=True)
                needs_download = True

                if os.path.exists(CACHE_FILE):
                    file_age = time.time() - os.path.getmtime(CACHE_FILE)
                    if file_age < 14400:
                        self.write_log("[System] Using cached Blocklist (less than 4 hours old).")
                        needs_download = False
                    else:
                        self.write_log("[System] Cached Blocklist is old. Updating...")

                if needs_download:
                    self.write_log("[System] Downloading fresh StevenBlack Hosts list...")
                    try:
                        resp = requests.get(
                            "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts",
                            timeout=15
                        )
                        resp.raise_for_status()

                        # Sanity-check: a valid hosts file contains "0.0.0.0" entries
                        if "0.0.0.0" not in resp.text:
                            raise ValueError("Downloaded content does not look like a hosts file.")

                        # Atomic write: write to a temp file then rename so a partial
                        # download never corrupts the cache.
                        tmp_fd, tmp_path = tempfile.mkstemp(dir=CACHE_DIR, suffix=".tmp")
                        try:
                            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                                f.write(resp.text)
                            shutil.move(tmp_path, CACHE_FILE)
                        except Exception:
                            try:
                                os.unlink(tmp_path)
                            except OSError:
                                pass
                            raise

                        self.write_log("[System] Blocklist downloaded and cached.")

                    except Exception as e:
                        if os.path.exists(CACHE_FILE):
                            self.write_log(f"[Warning] Blocklist download failed ({e}). Using older cached copy — ad filtering may be incomplete.")
                        else:
                            self.write_log(f"[Warning] Blocklist download failed and no cache exists ({e}). Ad filtering is disabled for this session.")
                            return

                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    lines = f.readlines()

            elif config.adlist_source == "Local File":
                if not config.local_blocklist_path:
                    self.write_log("[System] No local blocklist file selected, skipping ad-filter.")
                    return
                with open(config.local_blocklist_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            else:
                self.write_log("[System] Ad-list disabled.")
                return

            new_domains = set()
            for line in lines:
                line = line.strip().lower()
                if not line or line.startswith("#") or line.startswith("!") or line.startswith("["):
                    continue

                domain = ""

                if line.startswith("0.0.0.0 ") or line.startswith("127.0.0.1 "):
                    parts = line.split()
                    if len(parts) >= 2:
                        domain = parts[1]
                        if domain in ("0.0.0.0", "127.0.0.1", "localhost", "broadcasthost"):
                            continue

                elif line.startswith("||"):
                    rule = line.split("$")[0]
                    domain_part = rule[2:]
                    domain = re.split(r"[\^/:]", domain_part)[0]
                    if domain.startswith("*."):
                        domain = domain[2:]

                elif "." in line and " " not in line and not line.startswith("/"):
                    domain = line

                if domain:
                    new_domains.add(domain)

            with self._domains_lock:
                self.easylist_domains = new_domains

            self.write_log(f"[System] Loaded {len(new_domains)} unique ad/tracking domains into filter.")
        except Exception as e:
            self.write_log(f"[Error] Failed to load Blocklist: {e}")

    def run_backend(self, config: SessionConfig):
        # Manual/Batch need the blocklist before traffic starts flowing.
        # Scraper Mode loads it on demand inside run_scraper, and only when
        # the filter toggle is enabled — see design doc §4 (Scraper Mode Output).
        if config.adlist_source != "None" and config.mode != "Scraper Mode":
            self.fetch_easylist(config)

        try:
            headless = config.mode in ("Batch Mode", "Scraper Mode")

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=headless)
                context = browser.new_context()
                # Network interception only needed for Manual and Batch modes
                if config.mode != "Scraper Mode":
                    context.on("request", lambda req: self.handle_request(req, config))
                page = context.new_page()

                if config.mode == "Manual Mode":
                    self.write_log(f"[System] Navigating to {config.target_url}")
                    try:
                        page.goto(config.target_url, wait_until="domcontentloaded")
                    except Exception as e:
                        self.write_log(f"[Error] Navigation failed: {e}")

                    while self.is_running:
                        try:
                            page.wait_for_timeout(500)
                        except Exception:
                            # Browser was closed by the user — exit the loop cleanly
                            self.is_running = False
                            break

                elif config.mode == "Batch Mode":
                    self.write_log(f"[System] Starting Batch Mode from {os.path.basename(config.batch_csv_path)}")
                    try:
                        with open(config.batch_csv_path, newline="", encoding="utf-8") as csvfile:
                            reader = csv.DictReader(csvfile)
                            for row in reader:
                                if not self.is_running:
                                    break
                                url = row.get("url", row.get("URL", None))
                                if url:
                                    self.write_log(f"\n[Batch] Loading: {url}")
                                    try:
                                        page.goto(url, wait_until="domcontentloaded")
                                        page.wait_for_timeout(3000)
                                    except Exception as e:
                                        self.write_log(f"[Batch Error] Failed on {url}: {e}")
                    except Exception as e:
                        self.write_log(f"[Error] Could not read CSV: {e}")

                elif config.mode == "Scraper Mode":
                    self.run_scraper(page, config)

                browser.close()
                self.write_log("[System] Browser closed.")

        except Exception as e:
            self.write_log(f"[Fatal Error] Playwright crashed: {e}")
        finally:
            self._trigger_cleanup(config)

    def run_scraper(self, page, config: SessionConfig):
        """Scrapes all href links from a single page and saves a Domain + Link CSV."""
        self.write_log(f"[Scraper] Loading {config.scraper_url}")
        try:
            page.goto(config.scraper_url, wait_until="domcontentloaded")
        except Exception as e:
            self.write_log(f"[Scraper Error] Failed to load page: {e}")
            return

        hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
        self.write_log(f"[Scraper] Found {len(hrefs)} raw links.")

        if config.scraper_filter and not self.easylist_domains:
            self.write_log("[Scraper] Blocklist not loaded — fetching now...")
            self.fetch_easylist(config)

        scraped_rows = []
        skipped = 0

        for href in hrefs:
            href = href.strip()
            if not href or href.startswith("javascript:") or href.startswith("mailto:"):
                continue

            parsed = urlparse(href)
            hostname = parsed.hostname
            if not hostname:
                continue
            hostname = hostname.lower()

            extracted = tldextract.extract(href)
            if not extracted.domain:
                continue
            base_domain = f"{extracted.domain}.{extracted.suffix}".lower()

            if config.scraper_filter:
                domains_to_check = {hostname, base_domain}
                if extracted.subdomain:
                    sub_parts = extracted.subdomain.split(".")
                    for i in range(len(sub_parts)):
                        partial = ".".join(sub_parts[i:])
                        domains_to_check.add(f"{partial}.{base_domain}")
                with self._domains_lock:
                    if any(d in self.easylist_domains for d in domains_to_check):
                        skipped += 1
                        continue

            scraped_rows.append([hostname, href])
            self.write_log(f"[Scraped] {hostname}  <--  {href}")

        if config.scraper_filter:
            self.write_log(f"[Scraper] Filtered out {skipped} ad/tracking links.")
        self.write_log(f"[Scraper] Collected {len(scraped_rows)} links after filtering.")

        if scraped_rows and config.output_folder:
            timestamp = time.strftime("%d%m%y-%H%M")
            filename = f"scraped_links_{timestamp}.csv"
            filepath = os.path.join(config.output_folder, filename)
            try:
                with open(filepath, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(["Domain", "Link"])
                    writer.writerows(scraped_rows)
                self.write_log(f"[Scraper] Saved {len(scraped_rows)} links to {filename}")
            except Exception as e:
                self.write_log(f"[Scraper Error] Failed to save CSV: {e}")
        elif not scraped_rows:
            self.write_log("[Scraper] No links found — nothing to save.")

    def handle_request(self, request, config: SessionConfig):
        if not self.is_running:
            return

        url = request.url
        parsed_url = urlparse(url)
        hostname = parsed_url.hostname
        if not hostname:
            return
        hostname = hostname.lower()

        extracted = tldextract.extract(url)
        if not extracted.domain:
            return

        base_domain = f"{extracted.domain}.{extracted.suffix}".lower()

        domains_to_check = {hostname, base_domain}
        if extracted.subdomain:
            sub_parts = extracted.subdomain.split(".")
            for i in range(len(sub_parts)):
                domains_to_check.add(f"{'.'.join(sub_parts[i:])}.{base_domain}")

        with self._domains_lock:
            is_blocked = any(d in self.easylist_domains for d in domains_to_check)

        if is_blocked:
            return

        warning = None
        if config.wildcard:
            shared_entry = match_shared_infrastructure(hostname)
            if shared_entry:
                # Wildcarding shared infrastructure would be dangerously broad —
                # preserve the exact subdomain and warn once per captured host.
                final_domain = hostname
                warning = (
                    f"[Warning] Wildcard suppressed for shared infrastructure domain "
                    f"'{shared_entry}' — using exact host '{hostname}' instead."
                )
            else:
                final_domain = f"*.{base_domain}"
        else:
            if extracted.subdomain:
                final_domain = f"{extracted.subdomain}.{base_domain}"
            else:
                final_domain = base_domain

        # Membership test, warning, and insert happen under a single lock
        # acquisition so the warn-once behaviour has no race window.
        with self._domains_lock:
            if final_domain not in self.captured_domains:
                self.captured_domains[final_domain] = request.resource_type
                if warning:
                    self.write_log(warning)
                self.write_log(f"[Captured] {final_domain}  <--  ({request.resource_type})")

    # ── Cleanup & save ───────────────────────────────────────────────────────

    def _trigger_cleanup(self, config: SessionConfig):
        """Called from the background thread; schedules save_and_cleanup on the
        main thread via after(). The _cleanup_called flag prevents a second
        call if the browser was closed manually and stop_session() also fires."""
        if self._cleanup_called:
            return
        self._cleanup_called = True
        self.after(0, lambda: self.save_and_cleanup(config))

    def format_for_product(self, domain_data, product_type):
        formatted_rows = []
        skipped_long = 0

        for domain in sorted(domain_data.keys()):
            res_type = domain_data[domain]

            if product_type == "GoGuardian":
                if len(domain) > 255:
                    skipped_long += 1
                    self.write_log(f"[Warning] GoGuardian: skipping '{domain[:60]}...' — exceeds 255-character limit.")
                    continue
                formatted_rows.append(["allow", domain])
            elif product_type == "Standard":
                formatted_rows.append([domain, res_type])
            else:
                formatted_rows.append([domain])

        if skipped_long:
            self.write_log(f"[Warning] GoGuardian: {skipped_long} domain(s) skipped for exceeding 255-character limit.")

        headers = []
        if product_type == "Standard":
            headers = ["Domain", "Source"]
        elif product_type == "GoGuardian":
            headers = ["action", "url"]
        # All other products (Deledao, Lightspeed, Securly, Blocksi): no header row

        return headers, formatted_rows

    def save_and_cleanup(self, config: SessionConfig):
        """Runs on the main thread (scheduled via after()). Safe to touch GUI widgets."""
        with self._domains_lock:
            domain_snapshot = dict(self.captured_domains)

        if domain_snapshot and config.output_folder:
            product = config.export_format
            timestamp = time.strftime("%d%m%y-%H%M")
            filename = f"whitelist_{product}_{timestamp}.csv"
            filepath = os.path.join(config.output_folder, filename)

            headers, rows = self.format_for_product(domain_snapshot, product)

            # Per-product row limit warnings and truncation
            if product == "Lightspeed" and len(rows) > 500:
                self.write_log(f"[Warning] Lightspeed limit is 500 rows — captured {len(rows)}. Truncating to 500.")
                rows = rows[:500]
            elif product == "GoGuardian" and len(rows) > 10000:
                self.write_log(f"[Warning] GoGuardian limit is 10,000 rows — captured {len(rows)}. Truncating to 10,000.")
                rows = rows[:10000]

            # GoGuardian 3 MB file size warning (checked after truncation)
            if product == "GoGuardian":
                estimated_size = sum(len(",".join(str(c) for c in row)) + 2 for row in rows)
                if estimated_size > 3_000_000:
                    self.write_log(f"[Warning] GoGuardian: estimated file size ({estimated_size // 1024} KB) may exceed the 3 MB import limit.")

            try:
                with open(filepath, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    if headers:
                        writer.writerow(headers)
                    writer.writerows(rows)

                # Final session summary
                self.write_log(f"\n{'─' * 40}")
                self.write_log(f"  File:     {filename}")
                self.write_log(f"  Location: {config.output_folder}")
                self.write_log(f"  Domains captured: {len(domain_snapshot)}")
                self.write_log(f"  Rows exported:    {len(rows)}")
                self.write_log(f"{'─' * 40}")

            except Exception as e:
                self.write_log(f"[Error] Failed to save CSV: {e}")

        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.mode_selector.configure(state="normal")
        self.write_log("=== SESSION ENDED ===")


if __name__ == "__main__":
    app = FilterListBuilderApp()
    app.mainloop()
