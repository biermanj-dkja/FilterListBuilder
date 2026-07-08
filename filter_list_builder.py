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


# One-line constraint summary shown under the export format selector (modern UI)
FORMAT_NOTES = {
    "Standard":   "Two columns: Domain, Source.",
    "GoGuardian": "Max 10,000 rows · 255 chars per URL · 3 MB file.",
    "Deledao":    "No header. Matches subdomains automatically.",
    "Lightspeed": "No header. Max 500 rows. Matches subdomains automatically.",
    "Securly":    "Best-effort format — pending vendor verification.",
    "Blocksi":    "Best-effort format — pending vendor verification.",
}


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
    def __init__(self, classic: bool = False):
        super().__init__()

        # "modern" (default) or "classic" (previous v0.3.x layout, --classic flag)
        self.ui_style = "classic" if classic else "modern"

        self.title("Filter List Builder")
        self.geometry("1000x750" if classic else "1080x760")

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
        # Session counters (modern UI status bar). _blocked_domains is written
        # from the Playwright thread and must be accessed under _domains_lock.
        self._blocked_domains = set()
        self._warning_count = 0
        # Widgets that exist only in one UI style; handlers use getattr checks.
        self.manual_hint_label = None
        self.batch_hint_label = None

        self.output_folder = os.path.join(os.path.expanduser("~"), "Downloads")
        self.batch_csv_path = ""

        if classic:
            self.setup_ui_classic()
        else:
            self.setup_ui_modern()
        self.check_queue()

    # ── Classic UI (previous v0.3.x layout, available via --classic) ─────────

    def setup_ui_classic(self):
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

        # Holder frame keeps the hint/info labels anchored below the switch when
        # on_format_change() swaps them (packing directly into controls_frame
        # would append the re-packed label to the bottom of the panel).
        self.wildcard_text_frame = ctk.CTkFrame(self.controls_frame, fg_color="transparent")
        self.wildcard_text_frame.pack(fill="x")

        self.wildcard_hint_label = ctk.CTkLabel(
            self.wildcard_text_frame,
            text="e.g. cdn.example.com → *.example.com. Risky for shared hosts like cloudfront.net.",
            text_color="gray",
            font=("Arial", 10),
            wraplength=220,
            justify="left",
        )
        self._wc_hint_pack = dict(padx=20, anchor="w", pady=(0, 4))
        self.wildcard_hint_label.pack(**self._wc_hint_pack)

        # Info label shown when a product auto-matches subdomains (Deledao, Lightspeed)
        self.wildcard_info_label = ctk.CTkLabel(
            self.wildcard_text_frame,
            text="Disabled — this product matches subdomains automatically.",
            text_color="gray",
            font=("Arial", 10)
        )
        self._wc_info_pack = dict(padx=20, anchor="w", pady=(0, 12))
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

    # ── Modern UI (default layout since v0.4.0) ──────────────────────────────

    def _section_header(self, title, first=False):
        """Small uppercase section label with a hairline separator above it."""
        if not first:
            sep = ctk.CTkFrame(self.controls_frame, height=1, fg_color=("gray82", "gray28"))
            sep.pack(fill="x", padx=12, pady=(14, 0))
        lbl = ctk.CTkLabel(self.controls_frame, text=title.upper(),
                           text_color="gray", font=ctk.CTkFont(size=10, weight="bold"))
        lbl.pack(anchor="w", padx=14, pady=(10, 4))

    def setup_ui_modern(self):
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # ================= HEADER BAR =================
        self.header_frame = ctk.CTkFrame(self, corner_radius=0)
        self.header_frame.grid(row=0, column=0, columnspan=2, sticky="ew")
        ctk.CTkLabel(self.header_frame, text="Filter List Builder",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(side="left", padx=16, pady=8)
        self.status_label = ctk.CTkLabel(self.header_frame, text="●  Idle",
                                         text_color="gray", font=ctk.CTkFont(size=12))
        self.status_label.pack(side="right", padx=16)

        # ================= LEFT PANEL (Configuration) =================
        # Container splits into a scrollable settings area and a pinned button
        # row, so Start/Stop stay reachable regardless of scroll position.
        self.left_container = ctk.CTkFrame(self, fg_color="transparent")
        self.left_container.grid(row=1, column=0, padx=(10, 5), pady=10, sticky="nsew")
        self.left_container.grid_rowconfigure(0, weight=1)
        self.left_container.grid_columnconfigure(0, weight=1)

        self.controls_frame = ctk.CTkScrollableFrame(self.left_container, width=300)
        self.controls_frame.grid(row=0, column=0, sticky="nsew")

        # --- Session ---
        self._section_header("Session", first=True)
        self.mode_var = ctk.StringVar(value="Manual Mode")
        self.mode_selector = ctk.CTkSegmentedButton(
            self.controls_frame, values=["Manual Mode", "Batch Mode", "Scraper Mode"],
            variable=self.mode_var, command=self.toggle_mode)
        self.mode_selector.pack(padx=14, fill="x")

        self.dynamic_frame = ctk.CTkFrame(self.controls_frame, fg_color="transparent")
        self.dynamic_frame.pack(pady=(8, 0), padx=14, fill="x")

        self.url_entry = ctk.CTkEntry(self.dynamic_frame, placeholder_text="https://example.com")
        self.manual_hint_label = ctk.CTkLabel(
            self.dynamic_frame, text="A visible browser opens — browse the site, then stop and save.",
            text_color="gray", font=("Arial", 10), wraplength=250, justify="left")
        self.url_entry.pack(fill="x", pady=(0, 2))
        self.manual_hint_label.pack(anchor="w")

        self.batch_btn = ctk.CTkButton(self.dynamic_frame, text="Choose URL list CSV", command=self.select_batch_csv)
        self.batch_label = ctk.CTkLabel(self.dynamic_frame, text="No CSV selected", text_color="gray", font=("Arial", 10))
        self.batch_hint_label = ctk.CTkLabel(
            self.dynamic_frame, text="Each URL is loaded headlessly with a 3-second settle per page.",
            text_color="gray", font=("Arial", 10), wraplength=250, justify="left")

        self.scraper_url_entry = ctk.CTkEntry(self.dynamic_frame, placeholder_text="https://example.com/links")
        self.scraper_filter_var = ctk.BooleanVar(value=False)
        self.scraper_filter_switch = ctk.CTkSwitch(self.dynamic_frame, text="Filter ad and tracking domains",
                                                   variable=self.scraper_filter_var)

        # --- Domain handling ---
        self._section_header("Domain handling")
        self.wildcard_var = ctk.BooleanVar(value=True)
        self.wildcard_switch = ctk.CTkSwitch(self.controls_frame,
                                             text="Allow all subdomains of captured roots",
                                             variable=self.wildcard_var)
        self.wildcard_switch.pack(padx=14, anchor="w")

        self.wildcard_text_frame = ctk.CTkFrame(self.controls_frame, fg_color="transparent")
        self.wildcard_text_frame.pack(fill="x")
        self.wildcard_hint_label = ctk.CTkLabel(
            self.wildcard_text_frame,
            text="cdn.example.com → *.example.com. Shared hosts like cloudfront.net are never wildcarded.",
            text_color="gray", font=("Arial", 10), wraplength=260, justify="left")
        self._wc_hint_pack = dict(padx=14, anchor="w", pady=(2, 0))
        self.wildcard_hint_label.pack(**self._wc_hint_pack)
        self.wildcard_info_label = ctk.CTkLabel(
            self.wildcard_text_frame,
            text="Disabled — this product matches subdomains automatically.",
            text_color=("#1F6AA5", "#6FB1E8"), font=("Arial", 10), wraplength=260, justify="left")
        self._wc_info_pack = dict(padx=14, anchor="w", pady=(2, 0))

        # --- Filtering ---
        self._section_header("Filtering")
        self.adlist_var = ctk.StringVar(value="Cloud Blocklist (Ads/Tracking)")
        self.adlist_dropdown = ctk.CTkOptionMenu(
            self.controls_frame,
            values=["Cloud Blocklist (Ads/Tracking)", "Local File", "None"],
            variable=self.adlist_var, command=self.on_adlist_change)
        self.adlist_dropdown.pack(padx=14, fill="x")
        self.cache_status_label = ctk.CTkLabel(self.controls_frame, text="",
                                               text_color="gray", font=("Arial", 10),
                                               wraplength=260, justify="left")
        self.cache_status_label.pack(padx=14, anchor="w", pady=(2, 0))

        self.local_blocklist_frame = ctk.CTkFrame(self.controls_frame, fg_color="transparent")
        self.local_blocklist_btn = ctk.CTkButton(self.local_blocklist_frame, text="Choose blocklist file",
                                                 command=self.select_local_blocklist)
        self.local_blocklist_btn.pack(fill="x", pady=(4, 2))
        self.local_blocklist_label = ctk.CTkLabel(self.local_blocklist_frame, text="No file selected",
                                                  text_color="gray", font=("Arial", 10))
        self.local_blocklist_label.pack(fill="x")
        # Hidden until "Local File" is selected

        # --- Export format ---
        self._section_header("Export format")
        self.export_format_var = ctk.StringVar(value="Standard")
        self.format_grid = ctk.CTkFrame(self.controls_frame, fg_color="transparent")
        self.format_grid.pack(padx=14, fill="x")
        self.format_grid.grid_columnconfigure((0, 1), weight=1, uniform="fmt")

        # Securly and Blocksi are labelled experimental until vendor-confirmed
        formats = [
            ("Standard", "Standard"), ("GoGuardian", "GoGuardian"),
            ("Deledao", "Deledao"), ("Lightspeed", "Lightspeed"),
            ("Securly (exp.)", "Securly"), ("Blocksi (exp.)", "Blocksi"),
        ]
        self.format_buttons = {}
        for i, (label, value) in enumerate(formats):
            btn = ctk.CTkButton(self.format_grid, text=label, height=28,
                                fg_color="transparent", hover_color=("gray90", "gray25"),
                                command=lambda v=value: self._select_format(v))
            btn.grid(row=i // 2, column=i % 2, sticky="ew", padx=2, pady=2)
            self.format_buttons[value] = btn

        self.format_note_label = ctk.CTkLabel(self.controls_frame, text=FORMAT_NOTES["Standard"],
                                              text_color="gray", font=("Arial", 10),
                                              wraplength=260, justify="left")
        self.format_note_label.pack(padx=14, anchor="w", pady=(4, 0))

        # --- Output ---
        self._section_header("Output")
        self.output_btn = ctk.CTkButton(self.controls_frame, text="Choose output folder",
                                        command=self.select_output_folder)
        self.output_btn.pack(padx=14, fill="x")
        self.output_label = ctk.CTkLabel(self.controls_frame, text=self.output_folder,
                                         text_color="gray", font=("Arial", 10),
                                         wraplength=260, justify="left")
        self.output_label.pack(padx=14, anchor="w", pady=(2, 0))

        # --- Session buttons (pinned below the scroll area) ---
        btn_frame = ctk.CTkFrame(self.left_container, fg_color="transparent")
        btn_frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        btn_frame.grid_columnconfigure((0, 1), weight=1, uniform="ss")
        self.start_btn = ctk.CTkButton(btn_frame, text="Start session", fg_color="green",
                                       hover_color="darkgreen", command=self.start_session)
        self.start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.stop_btn = ctk.CTkButton(btn_frame, text="Stop and save", fg_color="red",
                                      hover_color="darkred", state="disabled", command=self.stop_session)
        self.stop_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        # ================= RIGHT PANEL (Network Log + status bar) =================
        self.log_frame = ctk.CTkFrame(self)
        self.log_frame.grid(row=1, column=1, padx=(5, 10), pady=10, sticky="nsew")
        self.log_frame.grid_rowconfigure(0, weight=1)
        self.log_frame.grid_columnconfigure(0, weight=1)

        self.log_textbox = ctk.CTkTextbox(self.log_frame, state="disabled", font=("Courier", 12))
        self.log_textbox.grid(row=0, column=0, padx=8, pady=(8, 0), sticky="nsew")
        self._configure_log_tags()

        self.status_bar = ctk.CTkFrame(self.log_frame, fg_color="transparent")
        self.status_bar.grid(row=1, column=0, sticky="ew", padx=12, pady=6)
        self.stat_captured = ctk.CTkLabel(self.status_bar, text="Captured: 0",
                                          text_color="gray", font=ctk.CTkFont(size=12))
        self.stat_captured.pack(side="left", padx=(0, 14))
        self.stat_blocked = ctk.CTkLabel(self.status_bar, text="Blocked: 0",
                                         text_color="gray", font=ctk.CTkFont(size=12))
        self.stat_blocked.pack(side="left", padx=(0, 14))
        self.stat_warnings = ctk.CTkLabel(self.status_bar, text="Warnings: 0",
                                          text_color="gray", font=ctk.CTkFont(size=12))
        self.stat_warnings.pack(side="left")
        self.stat_target = ctk.CTkLabel(self.status_bar, text="",
                                        text_color="gray", font=ctk.CTkFont(size=12))
        self.stat_target.pack(side="right")

        self._refresh_format_buttons()
        self._update_cache_status()

    def _configure_log_tags(self):
        """Colour tags for the modern log. Mid-brightness values stay readable
        in both light and dark appearance modes."""
        colors = {
            "captured": "#2FA463",
            "warning":  "#D9822B",
            "error":    "#E05252",
            "blocked":  "gray55",
            "system":   "gray60",
        }
        for tag, color in colors.items():
            try:
                self.log_textbox.tag_config(tag, foreground=color)
            except Exception:
                pass  # older customtkinter without tag passthrough — plain log

    LOG_TAG_PREFIXES = (
        ("[Captured]", "captured"), ("[Scraped]", "captured"),
        ("[Warning]", "warning"),
        ("[Error]", "error"), ("[Fatal", "error"),
        ("[Batch Error]", "error"), ("[Scraper Error]", "error"),
        ("[Blocked]", "blocked"),
        ("[System]", "system"), ("===", "system"),
        ("[Batch]", "system"), ("[Scraper]", "system"),
    )

    def _tag_for_message(self, message):
        stripped = message.lstrip()
        for prefix, tag in self.LOG_TAG_PREFIXES:
            if stripped.startswith(prefix):
                return tag
        return None

    def _select_format(self, value):
        self.export_format_var.set(value)
        self.on_format_change()

    def _refresh_format_buttons(self):
        selected = self.export_format_var.get()
        for value, btn in self.format_buttons.items():
            if value == selected:
                btn.configure(border_width=2, border_color=("#1F6AA5", "#6FB1E8"),
                              text_color=("#1F6AA5", "#6FB1E8"),
                              font=ctk.CTkFont(size=12, weight="bold"))
            else:
                btn.configure(border_width=1, border_color=("gray70", "gray35"),
                              text_color=("gray20", "gray85"),
                              font=ctk.CTkFont(size=12))

    def _update_cache_status(self):
        """Refresh the small helper line under the ad-list dropdown (modern UI).
        Runs on the main thread only."""
        if getattr(self, "cache_status_label", None) is None:
            return
        source = self.adlist_var.get()
        if source == "Cloud Blocklist (Ads/Tracking)":
            if os.path.exists(CACHE_FILE):
                age_h = (time.time() - os.path.getmtime(CACHE_FILE)) / 3600
                if age_h < 4:
                    text = f"Cached {age_h:.1f} h ago — reused until 4 h old."
                else:
                    text = "Cache is stale — a fresh list downloads at session start."
            else:
                text = "Downloads at session start, then cached for 4 h."
        elif source == "Local File":
            text = "Hosts, EasyList/AdGuard, or one-domain-per-line format."
        else:
            text = "No filtering — every contacted domain is captured."
        self.cache_status_label.configure(text=text)

    def _set_status(self, text, color):
        """Update the header status indicator (modern UI only)."""
        if getattr(self, "status_label", None) is not None:
            self.status_label.configure(text=f"●  {text}", text_color=color)

    def _refresh_status_bar(self):
        """Called from the main-thread queue poll; reads counters under lock."""
        with self._domains_lock:
            captured = len(self.captured_domains)
            blocked = len(self._blocked_domains)
        self.stat_captured.configure(text=f"Captured: {captured}")
        self.stat_blocked.configure(text=f"Blocked: {blocked}")
        self.stat_warnings.configure(text=f"Warnings: {self._warning_count}")

    # ── UI helpers ──────────────────────────────────────────────────────────

    def on_format_change(self):
        """Called when the export format selection changes (radio buttons in
        classic, button grid in modern). Deledao and Lightspeed force wildcard
        OFF (and disable the toggle) because both auto-match subdomains.
        Switching away restores the previous state."""
        product = self.export_format_var.get()
        if product in ("Deledao", "Lightspeed"):
            if self.wildcard_switch.cget("state") != "disabled":
                self._wildcard_before_autosub = self.wildcard_var.get()
            self.wildcard_var.set(False)
            self.wildcard_switch.configure(state="disabled")
            self.wildcard_hint_label.pack_forget()
            self.wildcard_info_label.pack(**self._wc_info_pack)
        else:
            self.wildcard_var.set(self._wildcard_before_autosub)
            self.wildcard_switch.configure(state="normal")
            self.wildcard_info_label.pack_forget()
            self.wildcard_hint_label.pack(**self._wc_hint_pack)

        if self.ui_style == "modern":
            self._refresh_format_buttons()
            self.format_note_label.configure(text=FORMAT_NOTES[product])

    def on_adlist_change(self, value):
        """Show the local file picker only when 'Local File' is selected."""
        if value == "Local File":
            self.local_blocklist_frame.pack(padx=20 if self.ui_style == "classic" else 14,
                                            fill="x", pady=(0, 10) if self.ui_style == "classic" else (4, 0))
        else:
            self.local_blocklist_frame.pack_forget()
        if self.ui_style == "modern":
            self._update_cache_status()

    def select_local_blocklist(self):
        """Called on the main thread — safe to open a file dialog."""
        path = filedialog.askopenfilename(title="Select Blocklist File", filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")])
        if path:
            self._local_blocklist_path = path
            self.local_blocklist_label.configure(text=os.path.basename(path))

    def toggle_mode(self, selected_mode):
        # Hide all dynamic widgets first (hint labels exist only in modern)
        widgets = [self.url_entry, self.batch_btn, self.batch_label,
                   self.scraper_url_entry, self.scraper_filter_switch,
                   self.manual_hint_label, self.batch_hint_label]
        for w in widgets:
            if w is not None:
                w.pack_forget()

        if selected_mode == "Manual Mode":
            self.url_entry.pack(fill="x", pady=(0, 2))
            if self.manual_hint_label is not None:
                self.manual_hint_label.pack(anchor="w")
        elif selected_mode == "Batch Mode":
            self.batch_btn.pack(fill="x", pady=(0, 2))
            self.batch_label.pack(fill="x")
            if self.batch_hint_label is not None:
                self.batch_hint_label.pack(anchor="w")
        else:  # Scraper Mode
            self.scraper_url_entry.pack(fill="x", pady=(0, 2))
            self.scraper_filter_switch.pack(anchor="w", pady=(4, 0))

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
            if self.ui_style == "modern":
                tag = self._tag_for_message(msg)
                if tag == "warning":
                    self._warning_count += 1
                if tag:
                    self.log_textbox.insert("end", msg + "\n", tag)
                else:
                    self.log_textbox.insert("end", msg + "\n")
            else:
                self.log_textbox.insert("end", msg + "\n")
            self.log_textbox.see("end")
            self.log_textbox.configure(state="disabled")
        if self.ui_style == "modern":
            self._refresh_status_bar()
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
        self._warning_count = 0
        with self._domains_lock:
            self.captured_domains.clear()
            self._blocked_domains.clear()
        self.easylist_domains.clear()

        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.mode_selector.configure(state="disabled")

        if self.ui_style == "modern":
            self._set_status("Recording", "#2FA463")
            self.stat_target.configure(
                text=f"{config.export_format} → {os.path.basename(config.output_folder) or config.output_folder}")
            self._update_cache_status()

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
                newly_blocked = hostname not in self._blocked_domains
                self._blocked_domains.add(hostname)

        if is_blocked:
            if newly_blocked and self.ui_style == "modern":
                self.write_log(f"[Blocked] {hostname} — on blocklist")
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
        if self.ui_style == "modern":
            self._set_status("Idle", "gray")
        self.write_log("=== SESSION ENDED ===")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Filter List Builder")
    parser.add_argument("--classic", action="store_true",
                        help="Use the previous (v0.3.x) interface layout")
    args = parser.parse_args()

    app = FilterListBuilderApp(classic=args.classic)
    app.mainloop()
