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
EASYLIST_CACHE_FILE = os.path.join(CACHE_DIR, "easylist_cache.txt")
EASYPRIVACY_CACHE_FILE = os.path.join(CACHE_DIR, "easyprivacy_cache.txt")
CACHE_TTL_SECONDS = 14400  # 4 hours, shared by all three blocklist sources

# Default-checked heuristic thresholds for the pre-export review table (see
# _default_include_for): a third-party domain must show up on more than one
# page, or be requested repeatedly on one page, before it's trusted by default.
REVIEW_DEFAULT_MIN_PAGE_COUNT = 2
REVIEW_DEFAULT_MIN_HIT_COUNT = 5

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


def parse_blocklist_lines(lines, allow_bare_domains=True):
    """Parses hosts-file and Adblock/EasyList-style (||domain^) rules into a
    set of blockable domains. Skips '@@' exception rules (an unblock, never
    a block) and cosmetic/element-hiding rules ('##', '#@#', '#?#', '#$#').
    allow_bare_domains=False disables the "any line containing a dot is a
    domain" fallback, which is needed for real EasyList/EasyPrivacy content —
    those lists are mostly path/regex filters that aren't domains at all."""
    domains = set()
    for raw in lines:
        line = raw.strip().lower()
        if not line or line.startswith("#") or line.startswith("!") or line.startswith("["):
            continue
        if line.startswith("@@"):
            continue
        if "##" in line or "#@#" in line or "#?#" in line or "#$#" in line:
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
            domain = re.split(r"[\^/:*?]", domain_part)[0]
            if domain.startswith("*."):
                domain = domain[2:]

        elif allow_bare_domains and "." in line and " " not in line and not line.startswith("/"):
            domain = line

        if domain and "." in domain:
            domains.add(domain)
    return domains


def resolve_page_context(request):
    """Returns (page_url, is_top_level_nav) for a Playwright request, used
    for first-party classification and page-count tracking. Queries
    Playwright's live frame state at request time rather than caching a
    "current page" separately, so there's no race between an outgoing page's
    in-flight requests and a navigation listener. Falls back to ("", False)
    for requests with no resolvable frame (e.g. service-worker fetches),
    which conservatively classifies as third-party."""
    try:
        frame = request.frame
        if frame is None:
            raise ValueError("no frame")
        main_frame = frame.page.main_frame
        is_top_level_nav = (request.resource_type == "document" and frame == main_frame)
        page_url = request.url if is_top_level_nav else (main_frame.url or "")
    except Exception:
        page_url, is_top_level_nav = "", False
    return page_url, is_top_level_nav


# One-line constraint summary shown under the export format selector (modern UI)
FORMAT_NOTES = {
    "Standard":   "Two columns: Domain, Source.",
    "GoGuardian": "Max 10,000 rows · 255 chars per URL · 3 MB file.",
    "Deledao":    "No header. Matches subdomains automatically.",
    "Lightspeed": "No header. Max 500 rows. Matches subdomains automatically.",
    "Securly":    "Best-effort format — pending vendor verification.",
    "Blocksi":    "Best-effort format — pending vendor verification.",
}

# Modern UI palette — (light mode, dark mode) tuples. One accent colour for
# the primary action and selection states; everything else stays neutral.
UI_ACCENT        = ("#3B6FD4", "#4A7BD9")
UI_ACCENT_HOVER  = ("#3260BA", "#3F6CC4")
UI_ACCENT_TEXT   = ("#2B5CB8", "#8AB4F8")
UI_OUTLINE       = ("gray72", "gray35")
UI_OUTLINE_HOVER = ("gray88", "gray26")
UI_TEXT          = ("gray15", "gray90")
UI_FIELD_BG      = ("white", "gray20")
UI_RADIUS        = 8


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
    export_raw_urls: bool = False


@dataclass
class DomainRecord:
    """Value type for self.captured_domains — one allow-listed (post
    wildcard/exact) domain string captured this session."""
    resource_type: str
    base_domain: str                              # eTLD+1, needed to reconstruct *.{base_domain} if wildcarded in review
    hit_count: int = 0
    page_urls: set = field(default_factory=set)   # distinct top-level page URLs that triggered a hit; len() = page count
    is_first_party: bool = False


@dataclass
class RequestRecord:
    """Value type for self.raw_requests — every unique URL requested this
    session, including blocked ones."""
    url: str
    hostname: str
    resource_type: str
    is_blocked: bool
    is_first_party: bool
    page_url: str


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
        self.raw_requests = {}
        self.root_domains = set()
        self.log_queue = queue.Queue()
        self.easylist_domains = set()
        self._domains_lock = threading.Lock()
        self._cleanup_called = False
        self._local_blocklist_path = ""
        # Wildcard state to restore after leaving a product that auto-matches
        # subdomains (Deledao, Lightspeed)
        self._wildcard_before_autosub = False
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

        self.wildcard_var = ctk.BooleanVar(value=False)
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

        self.export_raw_var = ctk.BooleanVar(value=False)
        self.export_raw_checkbox = ctk.CTkCheckBox(self.controls_frame, text="Also export raw URL data (CSV)",
                                                    variable=self.export_raw_var)
        self.export_raw_checkbox.pack(pady=(10, 0), padx=20, anchor="w")

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

    def _outline_button(self, parent, **kwargs):
        """Neutral outline-style button used for secondary actions (modern UI)."""
        return ctk.CTkButton(parent, fg_color="transparent", hover_color=UI_OUTLINE_HOVER,
                             border_width=1, border_color=UI_OUTLINE,
                             text_color=UI_TEXT, corner_radius=UI_RADIUS, **kwargs)

    def setup_ui_modern(self):
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # ================= HEADER BAR =================
        self.header_frame = ctk.CTkFrame(self, corner_radius=0)
        self.header_frame.grid(row=0, column=0, columnspan=2, sticky="ew")
        ctk.CTkLabel(self.header_frame, text="Filter List Builder",
                     text_color=("gray10", "gray92"),
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

        self.controls_frame = ctk.CTkScrollableFrame(self.left_container, width=300, corner_radius=12)
        self.controls_frame.grid(row=0, column=0, sticky="nsew")

        # --- Session ---
        self._section_header("Session", first=True)
        self.mode_var = ctk.StringVar(value="Manual Mode")
        self.mode_selector = ctk.CTkSegmentedButton(
            self.controls_frame, values=["Manual Mode", "Batch Mode", "Scraper Mode"],
            variable=self.mode_var, command=self.toggle_mode,
            corner_radius=UI_RADIUS, text_color=UI_TEXT,
            fg_color=("gray88", "gray25"),
            selected_color=("#C5D7F5", "#31517F"), selected_hover_color=("#B3CBF1", "#3A5D92"),
            unselected_color=("gray88", "gray25"), unselected_hover_color=("gray80", "gray30"))
        self.mode_selector.pack(padx=14, fill="x")

        self.dynamic_frame = ctk.CTkFrame(self.controls_frame, fg_color="transparent")
        self.dynamic_frame.pack(pady=(8, 0), padx=14, fill="x")

        self.url_entry = ctk.CTkEntry(self.dynamic_frame, placeholder_text="https://example.com",
                                      corner_radius=UI_RADIUS, border_width=1,
                                      border_color=UI_OUTLINE, fg_color=UI_FIELD_BG)
        self.manual_hint_label = ctk.CTkLabel(
            self.dynamic_frame, text="A visible browser opens — browse the site, then stop and save.",
            text_color="gray", font=("Arial", 10), wraplength=250, justify="left")
        self.url_entry.pack(fill="x", pady=(0, 2))
        self.manual_hint_label.pack(anchor="w")

        self.batch_btn = self._outline_button(self.dynamic_frame, text="Choose URL list CSV",
                                              command=self.select_batch_csv)
        self.batch_label = ctk.CTkLabel(self.dynamic_frame, text="No CSV selected", text_color="gray", font=("Arial", 10))
        self.batch_hint_label = ctk.CTkLabel(
            self.dynamic_frame, text="Each URL is loaded headlessly with a 3-second settle per page.",
            text_color="gray", font=("Arial", 10), wraplength=250, justify="left")

        self.scraper_url_entry = ctk.CTkEntry(self.dynamic_frame, placeholder_text="https://example.com/links",
                                              corner_radius=UI_RADIUS, border_width=1,
                                              border_color=UI_OUTLINE, fg_color=UI_FIELD_BG)
        self.scraper_filter_var = ctk.BooleanVar(value=False)
        self.scraper_filter_switch = ctk.CTkSwitch(self.dynamic_frame, text="Filter ad and tracking domains",
                                                   variable=self.scraper_filter_var,
                                                   progress_color=UI_ACCENT)

        # --- Domain handling ---
        self._section_header("Domain handling")
        self.wildcard_var = ctk.BooleanVar(value=False)
        self.wildcard_switch = ctk.CTkSwitch(self.controls_frame,
                                             text="Allow all subdomains of captured roots",
                                             variable=self.wildcard_var,
                                             progress_color=UI_ACCENT)
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
            text_color=UI_ACCENT_TEXT, font=("Arial", 10), wraplength=260, justify="left")
        self._wc_info_pack = dict(padx=14, anchor="w", pady=(2, 0))

        # --- Filtering ---
        self._section_header("Filtering")
        self.adlist_var = ctk.StringVar(value="Cloud Blocklist (Ads/Tracking)")
        self.adlist_dropdown = ctk.CTkComboBox(
            self.controls_frame,
            values=["Cloud Blocklist (Ads/Tracking)", "Local File", "None"],
            variable=self.adlist_var, command=self.on_adlist_change,
            state="readonly", corner_radius=UI_RADIUS, border_width=1,
            border_color=UI_OUTLINE, fg_color=UI_FIELD_BG, text_color=UI_TEXT,
            button_color=("gray82", "gray30"), button_hover_color=("gray72", "gray35"))
        self.adlist_dropdown.pack(padx=14, fill="x")
        self.cache_status_label = ctk.CTkLabel(self.controls_frame, text="",
                                               text_color="gray", font=("Arial", 10),
                                               wraplength=260, justify="left")
        self.cache_status_label.pack(padx=14, anchor="w", pady=(2, 0))

        self.local_blocklist_frame = ctk.CTkFrame(self.controls_frame, fg_color="transparent")
        self.local_blocklist_btn = self._outline_button(self.local_blocklist_frame,
                                                        text="Choose blocklist file",
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
                                corner_radius=UI_RADIUS,
                                fg_color="transparent", hover_color=UI_OUTLINE_HOVER,
                                command=lambda v=value: self._select_format(v))
            btn.grid(row=i // 2, column=i % 2, sticky="ew", padx=2, pady=2)
            self.format_buttons[value] = btn

        self.format_note_label = ctk.CTkLabel(self.controls_frame, text=FORMAT_NOTES["Standard"],
                                              text_color="gray", font=("Arial", 10),
                                              wraplength=260, justify="left")
        self.format_note_label.pack(padx=14, anchor="w", pady=(4, 0))

        # --- Output ---
        self._section_header("Output")
        self.output_btn = self._outline_button(self.controls_frame, text="Choose output folder",
                                               command=self.select_output_folder)
        self.output_btn.pack(padx=14, fill="x")
        self.output_label = ctk.CTkLabel(self.controls_frame, text=self.output_folder,
                                         text_color="gray", font=("Arial", 10),
                                         wraplength=260, justify="left")
        self.output_label.pack(padx=14, anchor="w", pady=(2, 0))

        self.export_raw_var = ctk.BooleanVar(value=False)
        self.export_raw_checkbox = ctk.CTkCheckBox(self.controls_frame, text="Also export raw URL data (CSV)",
                                                    variable=self.export_raw_var,
                                                    corner_radius=UI_RADIUS,
                                                    fg_color=UI_ACCENT, hover_color=UI_ACCENT_HOVER,
                                                    border_color=UI_OUTLINE)
        self.export_raw_checkbox.pack(padx=14, anchor="w", pady=(6, 0))

        # --- Session buttons (pinned below the scroll area) ---
        btn_frame = ctk.CTkFrame(self.left_container, fg_color="transparent")
        btn_frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        btn_frame.grid_columnconfigure((0, 1), weight=1, uniform="ss")
        self.start_btn = ctk.CTkButton(btn_frame, text="Start session", height=34,
                                       corner_radius=UI_RADIUS,
                                       fg_color=UI_ACCENT, hover_color=UI_ACCENT_HOVER,
                                       command=self.start_session)
        self.start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.stop_btn = self._outline_button(btn_frame, text="Stop and save", height=34,
                                             state="disabled", command=self.stop_session)
        self.stop_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        # ================= RIGHT PANEL (Network Log + status bar) =================
        self.log_frame = ctk.CTkFrame(self, corner_radius=12)
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
                btn.configure(border_width=2, border_color=UI_ACCENT,
                              text_color=UI_ACCENT_TEXT,
                              font=ctk.CTkFont(size=12, weight="bold"))
            else:
                btn.configure(border_width=1, border_color=UI_OUTLINE,
                              text_color=UI_TEXT,
                              font=ctk.CTkFont(size=12))

    def _update_cache_status(self):
        """Refresh the small helper line under the ad-list dropdown (modern UI).
        Runs on the main thread only."""
        if getattr(self, "cache_status_label", None) is None:
            return
        source = self.adlist_var.get()
        if source == "Cloud Blocklist (Ads/Tracking)":
            cache_files = [CACHE_FILE, EASYLIST_CACHE_FILE, EASYPRIVACY_CACHE_FILE]
            existing = [f for f in cache_files if os.path.exists(f)]
            if len(existing) == len(cache_files):
                age_h = max(time.time() - os.path.getmtime(f) for f in existing) / 3600
                if age_h < 4:
                    text = f"StevenBlack + EasyList + EasyPrivacy cached, oldest {age_h:.1f} h ago — reused until 4 h old."
                else:
                    text = "Cache is stale — fresh lists download at session start."
            else:
                text = "StevenBlack + EasyList + EasyPrivacy download at session start, then cached for 4 h."
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

        # Scraper Mode never populates captured_domains/raw_requests (it has
        # its own independent scraped_links_*.csv export), so the raw-data
        # toggle doesn't apply there.
        if getattr(self, "export_raw_checkbox", None) is not None:
            self.export_raw_checkbox.configure(state="disabled" if selected_mode == "Scraper Mode" else "normal")

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
            export_raw_urls=self.export_raw_var.get(),
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
            self.raw_requests.clear()
            self.root_domains.clear()
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

    def _fetch_cached_list(self, label, url, cache_path, sanity_token):
        """Downloads and caches one blocklist source, 4h TTL, atomic write,
        with graceful fallback to a stale cache (or None) on failure. Same
        pattern used for all three cloud sources — only label/url/cache
        path/sanity check differ."""
        os.makedirs(CACHE_DIR, exist_ok=True)
        needs_download = True

        if os.path.exists(cache_path):
            file_age = time.time() - os.path.getmtime(cache_path)
            if file_age < CACHE_TTL_SECONDS:
                self.write_log(f"[System] Using cached {label} (less than 4 hours old).")
                needs_download = False
            else:
                self.write_log(f"[System] Cached {label} is old. Updating...")

        if needs_download:
            self.write_log(f"[System] Downloading fresh {label}...")
            try:
                resp = requests.get(url, timeout=15)
                resp.raise_for_status()

                if sanity_token not in resp.text:
                    raise ValueError(f"Downloaded content does not look like {label}.")

                # Atomic write: write to a temp file then rename so a partial
                # download never corrupts the cache.
                tmp_fd, tmp_path = tempfile.mkstemp(dir=CACHE_DIR, suffix=".tmp")
                try:
                    with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                        f.write(resp.text)
                    shutil.move(tmp_path, cache_path)
                except Exception:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise

                self.write_log(f"[System] {label} downloaded and cached.")

            except Exception as e:
                if os.path.exists(cache_path):
                    self.write_log(f"[Warning] {label} download failed ({e}). Using older cached copy — ad filtering may be incomplete.")
                else:
                    self.write_log(f"[Warning] {label} download failed and no cache exists ({e}).")
                    return None

        with open(cache_path, "r", encoding="utf-8") as f:
            return f.readlines()

    def fetch_easylist(self, config: SessionConfig):
        """Downloads / loads the blocklist(s). Reads only from config (no
        Tkinter access). Cloud mode unions StevenBlack Hosts, EasyList, and
        EasyPrivacy so ad/tracking coverage isn't limited to StevenBlack's
        malware/ads-focused hosts file."""
        self.write_log("[System] Preparing Blocklist...")
        try:
            if config.adlist_source == "Cloud Blocklist (Ads/Tracking)":
                sources = [
                    ("StevenBlack Hosts", "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts",
                     CACHE_FILE, "0.0.0.0", True),
                    ("EasyList", "https://easylist.to/easylist/easylist.txt",
                     EASYLIST_CACHE_FILE, "[Adblock", False),
                    ("EasyPrivacy", "https://easylist.to/easylist/easyprivacy.txt",
                     EASYPRIVACY_CACHE_FILE, "[Adblock", False),
                ]
                combined = set()
                any_ok = False
                for label, url, cache_path, sanity_token, allow_bare in sources:
                    lines = self._fetch_cached_list(label, url, cache_path, sanity_token)
                    if lines is None:
                        continue
                    any_ok = True
                    combined |= parse_blocklist_lines(lines, allow_bare_domains=allow_bare)

                if not any_ok:
                    self.write_log("[Warning] All blocklist sources failed and no caches exist. Ad filtering is disabled for this session.")
                    return

                with self._domains_lock:
                    self.easylist_domains = combined
                self.write_log(f"[System] Loaded {len(combined)} unique ad/tracking domains "
                               f"into filter (StevenBlack + EasyList + EasyPrivacy).")
                return

            elif config.adlist_source == "Local File":
                if not config.local_blocklist_path:
                    self.write_log("[System] No local blocklist file selected, skipping ad-filter.")
                    return
                with open(config.local_blocklist_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                new_domains = parse_blocklist_lines(lines, allow_bare_domains=True)
                with self._domains_lock:
                    self.easylist_domains = new_domains
                self.write_log(f"[System] Loaded {len(new_domains)} unique ad/tracking domains into filter.")
            else:
                self.write_log("[System] Ad-list disabled.")
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

        scraper_root = ""
        seed = tldextract.extract(config.scraper_url)
        if seed.domain:
            scraper_root = f"{seed.domain}.{seed.suffix}".lower()

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

            party = "First-party" if base_domain == scraper_root else "Third-party"
            scraped_rows.append([hostname, href, party])
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
                    writer.writerow(["Domain", "Link", "Party"])
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

        page_url, is_top_level_nav = resolve_page_context(request)
        page_extracted = tldextract.extract(page_url) if page_url else None
        page_root = f"{page_extracted.domain}.{page_extracted.suffix}".lower() if page_extracted and page_extracted.domain else ""
        is_first_party = is_top_level_nav or (base_domain == page_root)

        with self._domains_lock:
            is_blocked = any(d in self.easylist_domains for d in domains_to_check)
            if is_blocked:
                newly_blocked = hostname not in self._blocked_domains
                self._blocked_domains.add(hostname)
            if page_root:
                self.root_domains.add(page_root)

            # Record every unique URL, blocked or not, before the early
            # return below — this feeds the (optional) raw-URL export.
            if config.export_raw_urls and url not in self.raw_requests:
                self.raw_requests[url] = RequestRecord(
                    url=url, hostname=hostname, resource_type=request.resource_type,
                    is_blocked=is_blocked, is_first_party=is_first_party,
                    page_url=page_url or url,
                )

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
            record = self.captured_domains.get(final_domain)
            first_insert = record is None
            if first_insert:
                record = DomainRecord(resource_type=request.resource_type, base_domain=base_domain)
                self.captured_domains[final_domain] = record
            record.hit_count += 1
            record.page_urls.add(page_url or url)
            if is_first_party:
                record.is_first_party = True
            if first_insert:
                if warning:
                    self.write_log(warning)
                self.write_log(f"[Captured] {final_domain}  <--  ({request.resource_type})")

    # ── Cleanup & save ───────────────────────────────────────────────────────

    def _trigger_cleanup(self, config: SessionConfig):
        """Called from the background thread; schedules the review/save step
        on the main thread via after(). The _cleanup_called flag prevents a
        second call if the browser was closed manually and stop_session()
        also fires."""
        if self._cleanup_called:
            return
        self._cleanup_called = True
        self.after(0, lambda: self._begin_review_or_save(config))

    def _begin_review_or_save(self, config: SessionConfig):
        """Runs on the main thread once the backend thread has fully exited.
        Snapshots the session's captured data, then either skips straight to
        saving (nothing captured — e.g. Scraper Mode, which never populates
        captured_domains) or opens the review dialog for the user to curate
        the allow list before it's written."""
        with self._domains_lock:
            domain_snapshot = dict(self.captured_domains)
            raw_snapshot = dict(self.raw_requests) if config.export_raw_urls else {}
        if not domain_snapshot:
            self.save_and_cleanup(config, {}, raw_snapshot)
            return
        self._open_review_dialog(config, domain_snapshot, raw_snapshot)

    def _default_include_for(self, record: DomainRecord) -> bool:
        """First-party domains default checked (opt-out). Third-party domains
        default checked only when they show up as a recurring, cross-page
        dependency or a heavily-requested resource on one page — the pattern
        typical of CDNs/API hosts/auth providers. Everything else (a single
        low-hit-count third-party domain — the classic tracker/incidental-
        resource noise behind an over-broad allow list) starts unchecked and
        must be opted in deliberately."""
        if record.is_first_party:
            return True
        return (len(record.page_urls) >= REVIEW_DEFAULT_MIN_PAGE_COUNT
                or record.hit_count >= REVIEW_DEFAULT_MIN_HIT_COUNT)

    def _open_review_dialog(self, config: SessionConfig, domain_snapshot: dict, raw_snapshot: dict):
        """Modal pre-export review table. Lets the user include/exclude each
        captured domain (and optionally wildcard it) before the curated CSV
        is written. Runs entirely on the main thread — safe to build widgets
        here since the backend thread has already finished and exited."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Review domains before export")
        dialog.geometry("900x600")
        dialog.transient(self)
        dialog.grab_set()
        dialog.attributes("-topmost", True)
        dialog.protocol("WM_DELETE_WINDOW",
                        lambda: self._finish_review(config, dialog, None, raw_snapshot))

        self._review_include_vars = {}
        self._review_wildcard_vars = {}
        self._review_rows = {}

        show_wildcard_col = config.export_format not in ("Deledao", "Lightspeed")

        top = ctk.CTkFrame(dialog, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(12, 4))
        self._review_search_var = ctk.StringVar()
        search_entry = ctk.CTkEntry(top, placeholder_text="Filter by domain...",
                                    textvariable=self._review_search_var)
        search_entry.pack(side="left", fill="x", expand=True)
        self._review_search_var.trace_add("write", lambda *_: self._apply_review_filter())
        self._review_party_filter = ctk.StringVar(value="All")
        ctk.CTkOptionMenu(top, values=["All", "First-party", "Third-party"],
                          variable=self._review_party_filter,
                          command=lambda _v: self._apply_review_filter()).pack(side="right", padx=(8, 0))

        header = ctk.CTkFrame(dialog, fg_color="transparent")
        header.pack(fill="x", padx=12)
        header_cols = ["Include", "Domain", "Type", "Hits", "Pages", "Party"] + (["Wildcard"] if show_wildcard_col else [])
        for i, text in enumerate(header_cols):
            ctk.CTkLabel(header, text=text, font=ctk.CTkFont(size=11, weight="bold")).grid(
                row=0, column=i, sticky="w", padx=6)

        body = ctk.CTkScrollableFrame(dialog)
        body.pack(fill="both", expand=True, padx=12, pady=(4, 4))

        ordered = sorted(domain_snapshot.items(),
                         key=lambda kv: (not kv[1].is_first_party, -kv[1].hit_count, kv[0]))
        for r, (domain, record) in enumerate(ordered):
            include_var = ctk.BooleanVar(value=self._default_include_for(record))
            self._review_include_vars[domain] = include_var
            row_widgets = []

            cb = ctk.CTkCheckBox(body, text="", variable=include_var)
            cb.grid(row=r, column=0, padx=6, pady=2, sticky="w")
            row_widgets.append(cb)

            values = [domain, record.resource_type, str(record.hit_count),
                     str(len(record.page_urls)), "First" if record.is_first_party else "Third"]
            for c, val in enumerate(values, start=1):
                lbl = ctk.CTkLabel(body, text=val)
                lbl.grid(row=r, column=c, sticky="w", padx=6)
                row_widgets.append(lbl)

            if show_wildcard_col:
                wc_var = ctk.BooleanVar(value=config.wildcard)
                self._review_wildcard_vars[domain] = wc_var
                wc_cb = ctk.CTkCheckBox(body, text="", variable=wc_var)
                wc_cb.grid(row=r, column=len(values) + 1, padx=6, pady=2)
                row_widgets.append(wc_cb)

            self._review_rows[domain] = {"widgets": row_widgets, "is_first_party": record.is_first_party}

        bottom = ctk.CTkFrame(dialog, fg_color="transparent")
        bottom.pack(fill="x", padx=12, pady=(0, 12))
        self._review_count_label = ctk.CTkLabel(bottom, text="")
        self._review_count_label.pack(side="left")
        self._refresh_review_count()
        for var in self._review_include_vars.values():
            var.trace_add("write", lambda *_: self._refresh_review_count())

        ctk.CTkButton(bottom, text="Cancel — export nothing", fg_color="transparent",
                     border_width=1, command=lambda: self._finish_review(
                         config, dialog, None, raw_snapshot)).pack(side="right", padx=(6, 0))
        ctk.CTkButton(bottom, text="Export all as captured", fg_color="transparent",
                     border_width=1, command=lambda: self._finish_review(
                         config, dialog, self._collect_all_as_captured(domain_snapshot), raw_snapshot)
                     ).pack(side="right", padx=(6, 0))
        ctk.CTkButton(bottom, text="Export selected",
                     command=lambda: self._finish_review(
                         config, dialog, self._collect_reviewed_domains(domain_snapshot), raw_snapshot)
                     ).pack(side="right")

        self.write_log(f"[System] Session capture complete — reviewing {len(domain_snapshot)} domain(s) before export.")

    def _apply_review_filter(self):
        """Show/hide pre-built rows (grid/grid_remove, not destroy/rebuild)
        so filtering stays cheap even with hundreds of captured domains."""
        query = self._review_search_var.get().strip().lower()
        party = self._review_party_filter.get()
        for domain, meta in self._review_rows.items():
            visible = query in domain.lower()
            if party == "First-party":
                visible = visible and meta["is_first_party"]
            elif party == "Third-party":
                visible = visible and not meta["is_first_party"]
            for w in meta["widgets"]:
                if visible:
                    w.grid()
                else:
                    w.grid_remove()

    def _refresh_review_count(self):
        total = len(self._review_include_vars)
        selected = sum(1 for v in self._review_include_vars.values() if v.get())
        self._review_count_label.configure(text=f"{selected} of {total} domains selected")

    def _collect_all_as_captured(self, domain_snapshot: dict) -> dict:
        """'Export all as captured' escape hatch — bypasses review entirely."""
        return {domain: record.resource_type for domain, record in domain_snapshot.items()}

    def _collect_reviewed_domains(self, domain_snapshot: dict) -> dict:
        """Converts the reviewed DomainRecord data back into the plain
        {domain: resource_type} shape format_for_product already expects —
        so format_for_product/save_and_cleanup's curated-write path need no
        changes at all."""
        final = {}
        for domain, record in domain_snapshot.items():
            if not self._review_include_vars[domain].get():
                continue
            wc_var = self._review_wildcard_vars.get(domain)
            key = f"*.{record.base_domain}" if (wc_var is not None and wc_var.get()) else domain
            final[key] = record.resource_type
        return final

    def _finish_review(self, config: SessionConfig, dialog, curated_domain_data, raw_snapshot: dict):
        """Single exit path for all three review buttons and window-close.
        curated_domain_data of None (Cancel/close) or {} (nothing selected)
        both skip the curated write; raw export still happens independently
        since it's an unfiltered audit dump, not the curated deliverable."""
        dialog.grab_release()
        dialog.destroy()
        self.save_and_cleanup(config, curated_domain_data, raw_snapshot)

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

    def format_raw_urls(self, raw_url_data: dict):
        """Builds header + rows for the raw URL CSV. raw_url_data is keyed by
        exact URL string, so it's already deduplicated by construction —
        only sorting/shaping is needed here."""
        headers = ["URL", "Domain", "Resource Type", "Blocked", "First-Party"]
        rows = []
        for url in sorted(raw_url_data.keys()):
            info = raw_url_data[url]
            rows.append([
                info.url, info.hostname, info.resource_type,
                "Yes" if info.is_blocked else "No",
                "Yes" if info.is_first_party else "No",
            ])
        return headers, rows

    def _save_raw_urls_csv(self, config: SessionConfig, raw_url_data: dict, timestamp: str):
        filename = f"raw_urls_{timestamp}.csv"
        filepath = os.path.join(config.output_folder, filename)
        headers, rows = self.format_raw_urls(raw_url_data)
        try:
            with open(filepath, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                writer.writerows(rows)
            blocked_count = sum(1 for r in rows if r[3] == "Yes")
            self.write_log(f"[System] Raw URL data saved: {filename} ({len(rows)} URLs, {blocked_count} blocked)")
        except Exception as e:
            self.write_log(f"[Error] Failed to save raw URL CSV: {e}")

    def save_and_cleanup(self, config: SessionConfig, curated_domain_data, raw_url_data):
        """Runs on the main thread (scheduled via after(), following the
        review dialog or its skip path). curated_domain_data is the final,
        already-reviewed {domain: resource_type} dict (or None/{} if the
        user cancelled/deselected everything); raw_url_data is the raw
        per-URL snapshot, written independently of curation review."""
        timestamp = time.strftime("%d%m%y-%H%M")

        if curated_domain_data and config.output_folder:
            product = config.export_format
            filename = f"whitelist_{product}_{timestamp}.csv"
            filepath = os.path.join(config.output_folder, filename)

            headers, rows = self.format_for_product(curated_domain_data, product)

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
                self.write_log(f"  Domains exported: {len(curated_domain_data)}")
                self.write_log(f"  Rows exported:    {len(rows)}")
                self.write_log(f"{'─' * 40}")

            except Exception as e:
                self.write_log(f"[Error] Failed to save CSV: {e}")
        elif curated_domain_data is None:
            self.write_log("[System] Curated export skipped — no whitelist file written.")

        if config.export_raw_urls and raw_url_data and config.output_folder:
            self._save_raw_urls_csv(config, raw_url_data, timestamp)
        elif config.export_raw_urls and not raw_url_data:
            self.write_log("[System] Raw export enabled but no URLs were captured — nothing to write.")

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
