#!/usr/bin/env python3
"""Enhanced desktop GUI wrapper around esptool for flashing ESP32 devices."""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from queue import Queue
from tkinter import BooleanVar, Menu, StringVar, Text, Tk, Toplevel, filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from urllib.request import urlopen

from serial.tools.list_ports import comports

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES

    HAVE_DND = True
except Exception:  # pragma: no cover - tkdnd optional at runtime
    HAVE_DND = False

ESP32_BAUDS = ["921600", "460800", "230400", "115200", "74880"]
ESP32_CHIPS = [
    "auto",
    "esp8266",
    "esp32",
    "esp32s2",
    "esp32s3",
    "esp32c2",
    "esp32c3",
    "esp32c6",
    "esp32h2",
]
DEFAULT_ADDR = "0x1000"

THEMES = {
    "dark": {
        "bg": "#111827",
        "fg": "#e5e7eb",
        "field": "#1f2937",
        "accent": "#3b82f6",
        "text_bg": "#0f172a",
        "text_fg": "#e2e8f0",
        "select": "#2563eb",
        "tree": "#1f2937",
    },
    "light": {
        "bg": "#f8fafc",
        "fg": "#0f172a",
        "field": "#ffffff",
        "accent": "#2563eb",
        "text_bg": "#ffffff",
        "text_fg": "#0f172a",
        "select": "#3b82f6",
        "tree": "#ffffff",
    },
}

FONT_FAMILY = ("Helvetica", "Arial", "DejaVu Sans")
NORMAL_FONT = (FONT_FAMILY, 10)
SMALL_FONT = (FONT_FAMILY, 9)
HEADER_FONT = (FONT_FAMILY, 18, "bold")


def get_esptool_base():
    """Return the command base used to invoke esptool."""
    return [sys.executable, "-m", "esptool"]


class ESP32FlasherApp:
    def __init__(self, master: Tk):
        self.master = master
        self.master.title("ESP32 Flash Tool")
        self.master.geometry("820x720")
        self.master.minsize(720, 600)

        self.cmd_queue: Queue = Queue()
        self.proc = None
        self._running = threading.Event()

        self.dark_mode = BooleanVar(value=True)
        self.binaries: list[dict] = []

        self._build_menu()
        self._build_widgets()
        self.poll_queue()
        self.refresh_ports()
        self.apply_theme()

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _build_menu(self):
        menubar = Menu(self.master)
        file_menu = Menu(menubar, tearoff=0)
        file_menu.add_command(label="Load profile...", command=self.load_profile)
        file_menu.add_command(label="Save profile...", command=self.save_profile)
        file_menu.add_separator()
        file_menu.add_command(label="Save log...", command=self.save_log)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.master.quit)
        menubar.add_cascade(label="File", menu=file_menu)

        settings_menu = Menu(menubar, tearoff=0)
        settings_menu.add_checkbutton(
            label="Dark mode", variable=self.dark_mode, command=self.apply_theme
        )
        menubar.add_cascade(label="Settings", menu=settings_menu)

        help_menu = Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.master.config(menu=menubar)
        self.menubar = menubar
        self.file_menu = file_menu
        self.settings_menu = settings_menu
        self.help_menu = help_menu

    def _build_widgets(self):
        # Header
        header = ttk.Frame(self.master, padding=(12, 10))
        header.pack(fill="x", padx=10, pady=(8, 0))
        ttk.Label(header, text="ESP32 Flash Tool", style="Header.TLabel").pack(side="left")
        ttk.Separator(self.master, orient="horizontal", style="HSeparator.TSeparator").pack(
            fill="x", padx=10, pady=8
        )

        # Settings frame
        settings = ttk.LabelFrame(self.master, text="Settings", padding=(12, 8))
        settings.pack(fill="x", padx=12, pady=6)

        ttk.Label(settings, text="Port:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        self.port_var = StringVar()
        self.port_combo = ttk.Combobox(
            settings, textvariable=self.port_var, values=[], width=22, state="readonly"
        )
        self.port_combo.grid(row=0, column=1, sticky="we", padx=5)
        ttk.Button(settings, text="Refresh", command=self.refresh_ports).grid(
            row=0, column=2, sticky="w"
        )

        ttk.Label(settings, text="Baud:").grid(row=1, column=0, sticky="w", pady=8, padx=(0, 4))
        self.baud_var = StringVar(value=ESP32_BAUDS[0])
        ttk.Combobox(settings, textvariable=self.baud_var, values=ESP32_BAUDS, width=10).grid(
            row=1, column=1, sticky="w", padx=5
        )

        ttk.Label(settings, text="Chip:").grid(row=2, column=0, sticky="w", padx=(0, 4))
        self.chip_var = StringVar(value=ESP32_CHIPS[0])
        ttk.Combobox(settings, textvariable=self.chip_var, values=ESP32_CHIPS, width=12).grid(
            row=2, column=1, sticky="w", padx=5
        )

        ttk.Separator(settings, orient="vertical").grid(
            row=0, column=3, rowspan=3, sticky="ns", padx=14
        )

        ttk.Label(settings, text="OTA URL:").grid(row=0, column=4, sticky="w", padx=(0, 4))
        self.url_var = StringVar()
        url_entry = ttk.Entry(settings, textvariable=self.url_var)
        url_entry.grid(row=0, column=5, sticky="we", padx=5)
        ttk.Button(settings, text="Fetch & Add", command=self.fetch_and_add).grid(
            row=0, column=6, sticky="w"
        )

        settings.columnconfigure(1, weight=0)
        settings.columnconfigure(5, weight=1)

        # Primary binary frame
        primary = ttk.LabelFrame(self.master, text="Add Binary", padding=(12, 8))
        primary.pack(fill="x", padx=12, pady=6)

        ttk.Label(primary, text="Address:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        self.addr_var = StringVar(value=DEFAULT_ADDR)
        ttk.Entry(primary, textvariable=self.addr_var, width=12).grid(
            row=0, column=1, sticky="w", padx=5
        )

        ttk.Label(primary, text="Firmware:").grid(row=1, column=0, sticky="w", pady=8, padx=(0, 4))
        self.file_var = StringVar()
        self.file_entry = ttk.Entry(primary, textvariable=self.file_var)
        self.file_entry.grid(row=1, column=1, sticky="we", padx=5)
        ttk.Button(primary, text="Browse...", command=self.browse_firmware).grid(
            row=1, column=2, sticky="w", padx=(0, 6)
        )
        ttk.Button(primary, text="Add", command=self.add_binary).grid(
            row=1, column=3, sticky="w"
        )

        primary.columnconfigure(1, weight=1)

        # Binary list
        list_frame = ttk.LabelFrame(self.master, text="Binaries to Flash", padding=(10, 8))
        list_frame.pack(fill="both", expand=True, padx=12, pady=6)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        cols = ("address", "file", "size")
        self.bin_tree = ttk.Treeview(
            list_frame, columns=cols, show="headings", selectmode="browse"
        )
        self.bin_tree.heading("address", text="Address")
        self.bin_tree.heading("file", text="File")
        self.bin_tree.heading("size", text="Size")
        self.bin_tree.column("address", width=100, anchor="w")
        self.bin_tree.column("file", width=420, anchor="w")
        self.bin_tree.column("size", width=90, anchor="e")
        self.bin_tree.grid(row=0, column=0, sticky="nsew")

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.bin_tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self.bin_tree.configure(yscrollcommand=vsb.set)

        btn_frame = ttk.Frame(list_frame)
        btn_frame.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(btn_frame, text="Remove selected", command=self.remove_binary).pack(side="left", padx=(0, 4))
        ttk.Button(btn_frame, text="Clear all", command=self.clear_binaries).pack(side="left")

        # Action buttons
        actions = ttk.Frame(self.master, padding=(0, 4))
        actions.pack(fill="x", padx=12, pady=(6, 0))
        for text, command in [
            ("Connect / Info", self.get_info),
            ("Flash All", self.flash_all),
            ("Erase", self.erase),
            ("Read Flash", self.read_flash),
            ("Stop", self.stop),
        ]:
            ttk.Button(actions, text=text, command=command).pack(
                side="left", padx=(0, 8), expand=True, fill="x"
            )

        # Progress
        self.progress = ttk.Progressbar(
            self.master, mode="determinate", maximum=100, value=0
        )
        self.progress.pack(fill="x", padx=12, pady=(10, 6))

        # Log
        log_frame = ttk.LabelFrame(self.master, text="Output", padding=(8, 6))
        log_frame.pack(fill="both", expand=True, padx=12, pady=6)
        self.log_text = ScrolledText(log_frame, wrap="word", height=10, state="disabled")
        self.log_text.pack(fill="both", expand=True)

        # Status bar
        self.status_var = StringVar(value="Ready")
        self.status_label = ttk.Label(
            self.master, textvariable=self.status_var, relief="flat", anchor="w", padding=(6, 2)
        )
        self.status_label.pack(fill="x", padx=12, pady=(0, 6))

        # Drag-and-drop
        if HAVE_DND:
            self.file_entry.drop_target_register(DND_FILES)
            self.file_entry.dnd_bind("<<Drop>>", self._on_file_drop)
            self.master.drop_target_register(DND_FILES)
            self.master.dnd_bind("<<Drop>>", self._on_file_drop)

    # ------------------------------------------------------------------ #
    # Theme
    # ------------------------------------------------------------------ #

    def apply_theme(self, event=None):
        theme_name = "dark" if self.dark_mode.get() else "light"
        t = THEMES[theme_name]

        style = ttk.Style(self.master)
        style.theme_use("clam")
        style.configure(
            ".",
            background=t["bg"],
            foreground=t["fg"],
            fieldbackground=t["field"],
            font=NORMAL_FONT,
        )
        style.configure("TFrame", background=t["bg"])
        style.configure("TLabel", background=t["bg"], foreground=t["fg"])
        style.configure(
            "Header.TLabel",
            background=t["bg"],
            foreground=t["accent"],
            font=HEADER_FONT,
        )
        style.configure(
            "TButton",
            background=t["field"],
            foreground=t["fg"],
            bordercolor=t["accent"],
            font=NORMAL_FONT,
            padding=4,
        )
        style.map(
            "TButton",
            background=[("active", t["accent"]), ("pressed", t["accent"])],
            foreground=[("active", "#ffffff"), ("pressed", "#ffffff")],
        )
        style.configure(
            "TEntry",
            fieldbackground=t["field"],
            foreground=t["fg"],
            insertcolor=t["fg"],
            padding=3,
        )
        style.configure(
            "TCombobox",
            fieldbackground=t["field"],
            background=t["field"],
            foreground=t["fg"],
            selectbackground=t["select"],
            arrowcolor=t["fg"],
            padding=2,
        )
        style.map("TCombobox", fieldbackground=[("readonly", t["field"])])
        style.configure("TCheckbutton", background=t["bg"], foreground=t["fg"])
        style.configure(
            "Horizontal.TProgressbar",
            troughcolor=t["field"],
            background=t["accent"],
            darkcolor=t["accent"],
            lightcolor=t["accent"],
            thickness=14,
        )
        style.configure("TLabelFrame", background=t["bg"], foreground=t["fg"])
        style.configure("TLabelFrame.Label", background=t["bg"], foreground=t["fg"])
        style.configure(
            "Treeview",
            background=t["tree"],
            foreground=t["fg"],
            fieldbackground=t["tree"],
            selectbackground=t["select"],
            font=NORMAL_FONT,
            rowheight=24,
        )
        style.configure(
            "Treeview.Heading",
            background=t["field"],
            foreground=t["fg"],
            font=(FONT_FAMILY, 9, "bold"),
        )
        style.configure(
            "HSeparator.TSeparator",
            background=t["accent"],
        )

        self.master.configure(background=t["bg"])
        menu_config = {
            "background": t["bg"],
            "foreground": t["fg"],
            "activebackground": t["select"],
            "activeforeground": "#ffffff",
        }
        for attr in ("menubar", "file_menu", "settings_menu", "help_menu"):
            if hasattr(self, attr):
                getattr(self, attr).configure(**menu_config)

        self.log_text.configure(
            bg=t["text_bg"],
            fg=t["text_fg"],
            insertbackground=t["fg"],
            selectbackground=t["select"],
            font=NORMAL_FONT,
            borderwidth=0,
            padx=6,
            pady=6,
        )

        for dialog in self.master.winfo_children():
            if isinstance(dialog, Toplevel):
                dialog.configure(background=t["bg"])
                for child in dialog.winfo_children():
                    if isinstance(child, (Text, ScrolledText)):
                        child.configure(
                            bg=t["text_bg"],
                            fg=t["text_fg"],
                            insertbackground=t["fg"],
                            selectbackground=t["select"],
                        )

    # ------------------------------------------------------------------ #
    # Queue / log helpers
    # ------------------------------------------------------------------ #

    def log_message(self, message: str):
        self.cmd_queue.put(("log", message))

    def set_status(self, text: str):
        self.cmd_queue.put(("status", text))

    def set_progress(self, value: int, mode: str = "determinate"):
        self.cmd_queue.put(("progress", value, mode))

    def poll_queue(self):
        while not self.cmd_queue.empty():
            item = self.cmd_queue.get()
            if item is None:
                continue
            kind = item[0]
            if kind == "log":
                self._append_log(item[1])
            elif kind == "status":
                self.status_var.set(item[1])
            elif kind == "progress":
                _, value, mode = item
                self.progress["mode"] = mode
                if mode == "indeterminate":
                    self.progress.start()
                else:
                    self.progress.stop()
                    self.progress["value"] = value
            elif kind == "download_done":
                _, path, address = item
                self.file_var.set(path)
                self.addr_var.set(address)
                self.add_binary()
                self.set_status("Download complete — added to list")
        self.master.after(100, self.poll_queue)

    def _append_log(self, message: str):
        self.log_text["state"] = "normal"
        self.log_text.insert("end", message.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text["state"] = "disabled"

    # ------------------------------------------------------------------ #
    # Port / file handling
    # ------------------------------------------------------------------ #

    def refresh_ports(self):
        ports = [p.device for p in comports()]
        self.port_combo["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])
        if not ports:
            self.port_var.set("")
        self.set_status(f"Found {len(ports)} serial port(s)")

    def browse_firmware(self):
        path = filedialog.askopenfilename(
            title="Select firmware binary",
            filetypes=[("Binary files", "*.bin"), ("All files", "*.*")],
        )
        if path:
            self.file_var.set(path)

    def _on_file_drop(self, event):
        files = self.master.tk.splitlist(event.data) if hasattr(event, "data") else []
        if not files:
            return
        path = files[0]
        if os.path.isfile(path):
            self.file_var.set(path)
            self.log_message(f"Dropped file: {path}")

    # ------------------------------------------------------------------ #
    # Binary list management
    # ------------------------------------------------------------------ #

    def add_binary(self):
        addr = self.addr_var.get().strip() or DEFAULT_ADDR
        path = self.file_var.get().strip()
        if not path:
            messagebox.showerror("Missing firmware", "Please select a firmware binary.")
            return
        if not os.path.isfile(path):
            messagebox.showerror("File not found", f"Firmware not found: {path}")
            return
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            messagebox.showerror("Error", f"Could not read file: {exc}")
            return
        self.binaries.append({"address": addr, "path": path, "size": size})
        self._refresh_bin_tree()
        self.set_status(f"Added binary at {addr}")

    def remove_binary(self):
        selected = self.bin_tree.selection()
        if not selected:
            return
        idx = int(selected[0])
        self.binaries.pop(idx)
        self._refresh_bin_tree()

    def clear_binaries(self):
        self.binaries.clear()
        self._refresh_bin_tree()

    def _refresh_bin_tree(self):
        self.bin_tree.delete(*self.bin_tree.get_children())
        for idx, item in enumerate(self.binaries):
            self.bin_tree.insert(
                "",
                "end",
                iid=str(idx),
                values=(item["address"], item["path"], item["size"]),
            )

    # ------------------------------------------------------------------ #
    # Profiles
    # ------------------------------------------------------------------ #

    def save_profile(self):
        path = filedialog.asksaveasfilename(
            title="Save profile",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        data = {
            "port": self.port_var.get(),
            "baud": self.baud_var.get(),
            "chip": self.chip_var.get(),
            "dark_mode": self.dark_mode.get(),
            "address": self.addr_var.get(),
            "url": self.url_var.get(),
            "binaries": self.binaries,
        }
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            self.set_status(f"Profile saved: {path}")
        except Exception as exc:
            messagebox.showerror("Error", f"Could not save profile: {exc}")

    def load_profile(self):
        path = filedialog.askopenfilename(
            title="Load profile",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.port_var.set(data.get("port", ""))
            self.baud_var.set(data.get("baud", ESP32_BAUDS[0]))
            self.chip_var.set(data.get("chip", ESP32_CHIPS[0]))
            self.dark_mode.set(data.get("dark_mode", True))
            self.addr_var.set(data.get("address", DEFAULT_ADDR))
            self.url_var.set(data.get("url", ""))
            self.binaries = data.get("binaries", [])
            self._refresh_bin_tree()
            self.apply_theme()
            self.set_status(f"Profile loaded: {path}")
        except Exception as exc:
            messagebox.showerror("Error", f"Could not load profile: {exc}")

    # ------------------------------------------------------------------ #
    # Log to file
    # ------------------------------------------------------------------ #

    def save_log(self):
        path = filedialog.asksaveasfilename(
            title="Save log",
            defaultextension=".log",
            filetypes=[("Log files", "*.log"), ("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            text = self.log_text.get("1.0", "end")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            self.set_status(f"Log saved: {path}")
        except Exception as exc:
            messagebox.showerror("Error", f"Could not save log: {exc}")

    # ------------------------------------------------------------------ #
    # Network / OTA
    # ------------------------------------------------------------------ #

    def fetch_and_add(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showerror("Missing URL", "Please enter a firmware URL.")
            return
        try:
            int(self.addr_var.get().strip() or DEFAULT_ADDR, 0)
        except ValueError:
            messagebox.showerror("Invalid address", "Address must be a number or hex value.")
            return

        self._running.set()
        self.set_progress(0, "determinate")
        self.set_status("Downloading firmware...")

        def target():
            try:
                tmp_path = self._download_file(url)
                address = self.addr_var.get().strip() or DEFAULT_ADDR
                self.cmd_queue.put(("download_done", tmp_path, address))
            except Exception as exc:
                self.log_message(f"Download failed: {exc}")
                self.set_status(f"Download failed: {exc}")
                self._running.clear()
                self.set_progress(0, "determinate")

        threading.Thread(target=target, daemon=True).start()

    def _download_file(self, url: str) -> str:
        fd, tmp_path = tempfile.mkstemp(suffix=".bin")
        os.close(fd)
        with open(tmp_path, "wb") as out:
            with urlopen(url, timeout=60) as resp:
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                chunk_size = 64 * 1024
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        percent = int(downloaded * 100 / total)
                        self.set_progress(percent)
        return tmp_path

    # ------------------------------------------------------------------ #
    # Esptool execution
    # ------------------------------------------------------------------ #

    def _base_args(self):
        args = get_esptool_base()
        chip = self.chip_var.get()
        if chip and chip != "auto":
            args += ["--chip", chip]
        else:
            args += ["--chip", "auto"]
        args += ["--port", self.port_var.get(), "--baud", self.baud_var.get()]
        return args

    def _run_command(self, args: list, parse_progress: bool = True):
        """Run an esptool command and stream its output."""
        try:
            self.proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            regex = re.compile(r"(\d+)\s*%")
            for line in self.proc.stdout:
                if line:
                    self.log_message(line)
                    if parse_progress:
                        match = regex.search(line)
                        if match:
                            self.set_progress(int(match.group(1)))
            code = self.proc.wait()
            if code == 0:
                self.set_status("Finished successfully")
            else:
                self.set_status(f"Failed (exit code {code})")
                self.log_message(f"-- process exited with code {code} --")
        except Exception as exc:
            self.log_message(f"Error: {exc}")
            self.set_status(f"Error: {exc}")
        finally:
            self.proc = None
            self._running.clear()
            self.set_progress(100, "determinate")

    def _run_esptool(self, args: list, parse_progress: bool = True):
        """Start an esptool command in a background thread."""
        self._running.set()
        self.set_progress(0, "determinate")
        self.set_status("Running...")
        self.log_message("$ " + " ".join(args))
        threading.Thread(target=self._run_command, args=(args, parse_progress), daemon=True).start()

    # ------------------------------------------------------------------ #
    # High-level commands
    # ------------------------------------------------------------------ #

    def _get_flash_items(self):
        """Return list of (address, path) items to flash."""
        if not self.binaries:
            addr = self.addr_var.get().strip() or DEFAULT_ADDR
            path = self.file_var.get().strip()
            if not path:
                messagebox.showerror(
                    "Missing firmware", "Add at least one binary to the list."
                )
                return None
            if not os.path.isfile(path):
                messagebox.showerror("File not found", f"Firmware not found: {path}")
                return None
            return [(addr, path)]
        return [(b["address"], b["path"]) for b in self.binaries]

    def _validate_port(self):
        port = self.port_var.get().strip()
        if not port:
            messagebox.showerror("Missing port", "Please select a serial port.")
            return False
        return True

    def get_info(self):
        if not self._validate_port():
            return
        self._run_esptool(self._base_args() + ["chip_id"])

    def flash_all(self):
        if not self._validate_port():
            return
        items = self._get_flash_items()
        if items is None:
            return
        args = self._base_args() + ["write_flash"]
        for addr, path in items:
            args += [addr, path]
        self._run_esptool(args)

    def erase(self):
        if not self._validate_port():
            return
        if not messagebox.askyesno(
            "Confirm erase", "This will erase all flash contents. Continue?"
        ):
            return
        self._run_esptool(self._base_args() + ["erase_flash"])

    def read_flash(self):
        if not self._validate_port():
            return
        path = filedialog.asksaveasfilename(
            title="Save flash dump",
            defaultextension=".bin",
            filetypes=[("Binary files", "*.bin"), ("All files", "*.*")],
        )
        if not path:
            return
        dialog = Toplevel(self.master)
        dialog.title("Read size")
        dialog.transient(self.master)
        self.apply_theme()
        ttk.Label(dialog, text="Size in bytes (e.g. 0x100000):").pack(padx=10, pady=5)
        size_var = StringVar(value="0x100000")
        ttk.Entry(dialog, textvariable=size_var).pack(padx=10, fill="x")

        def do_read():
            size = size_var.get().strip()
            try:
                int(size, 0)
            except ValueError:
                messagebox.showerror("Invalid size", "Size must be a number or hex value.")
                return
            dialog.destroy()
            args = self._base_args() + ["read_flash", "0x0", size, path]
            self._run_esptool(args)

        ttk.Button(dialog, text="Read", command=do_read).pack(pady=10)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.log_message("-- stopped --")
            self.set_status("Stopped by user")

    # ------------------------------------------------------------------ #
    # Misc
    # ------------------------------------------------------------------ #

    def _show_about(self):
        messagebox.showinfo(
            "About",
            "ESP32 Flash Tool\nA desktop GUI wrapper around esptool.\n",
        )


def main():
    if HAVE_DND:
        root = TkinterDnD.Tk()
    else:
        root = Tk()
    ESP32FlasherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
