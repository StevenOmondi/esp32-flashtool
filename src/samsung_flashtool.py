#!/usr/bin/env python3
"""Desktop GUI for flashing Samsung devices via the native Odin/LOKE protocol.

A general-purpose Samsung firmware flasher (an open alternative to Odin) built
on top of :mod:`odin_protocol`, matching the look and feel of the ESP32 Flash
Tool. Flash individual partition images (AP/BL/CP/CSC components extracted from
a firmware TAR) to a device booted into Download / Odin mode.
"""

import json
import os
import shutil
import tarfile
import tempfile
import threading
from queue import Queue
from tkinter import BooleanVar, Menu, StringVar, Text, Tk, Toplevel, filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import odin_protocol
from odin_protocol import FlashPartition, OdinDevice, OdinError, PitData

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES

    HAVE_DND = True
except Exception:  # pragma: no cover - tkdnd optional at runtime
    HAVE_DND = False


# Common Samsung partition names, offered as suggestions. The authoritative
# list always comes from the device's PIT.
COMMON_PARTITIONS = [
    "BOOTLOADER",
    "BOOT",
    "RECOVERY",
    "SYSTEM",
    "CACHE",
    "USERDATA",
    "MODEM",
    "CP",
    "CSC",
    "PARAM",
    "RADIO",
    "UP_PARAM",
    "DTBO",
    "VBMETA",
    "SUPER",
]

# Map common firmware filenames (inside a TAR) to their PIT partition names.
FILENAME_TO_PARTITION = {
    "sboot": "BOOTLOADER",
    "cm": "CM",
    "boot": "BOOT",
    "recovery": "RECOVERY",
    "system": "SYSTEM",
    "cache": "CACHE",
    "userdata": "USERDATA",
    "modem": "MODEM",
    "cp": "CP",
    "param": "PARAM",
    "up_param": "UP_PARAM",
    "dtbo": "DTBO",
    "vbmeta": "VBMETA",
    "super": "SUPER",
}

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


IMAGE_EXTENSIONS = (".img", ".bin", ".mbn", ".lz4", ".ext4", ".md5")


def guess_partition(filename: str) -> str:
    """Guess the PIT partition name for a firmware image filename."""
    stem = os.path.basename(filename)
    changed = True
    while changed:
        changed = False
        for ext in IMAGE_EXTENSIONS:
            if stem.lower().endswith(ext):
                stem = stem[: -len(ext)]
                changed = True
    return FILENAME_TO_PARTITION.get(stem.lower(), stem.upper())


class SamsungFlasherApp:
    def __init__(self, master: Tk):
        self.master = master
        self.master.title("Samsung Flash Tool")
        self.master.geometry("860x760")
        self.master.minsize(760, 640)

        self.cmd_queue: Queue = Queue()
        self._running = threading.Event()
        self._stop_requested = threading.Event()
        self._temp_dirs: list[str] = []

        self.dark_mode = BooleanVar(value=True)
        self.repartition = BooleanVar(value=False)
        self.auto_reboot = BooleanVar(value=True)
        self.tflash = BooleanVar(value=False)
        self.partitions: list[dict] = []
        self.devices: list = []

        self._build_menu()
        self._build_widgets()
        self.master.protocol("WM_DELETE_WINDOW", self._on_close)
        self.poll_queue()
        self.refresh_devices()
        self.apply_theme()

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _build_menu(self):
        menubar = Menu(self.master)
        file_menu = Menu(menubar, tearoff=0)
        file_menu.add_command(label="Add firmware TAR...", command=self.add_from_tar)
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
        header = ttk.Frame(self.master, padding=(12, 10))
        header.pack(fill="x", padx=10, pady=(8, 0))
        ttk.Label(header, text="Samsung Flash Tool", style="Header.TLabel").pack(side="left")
        ttk.Separator(self.master, orient="horizontal", style="HSeparator.TSeparator").pack(
            fill="x", padx=10, pady=8
        )

        # Device / options frame
        settings = ttk.LabelFrame(self.master, text="Device", padding=(12, 8))
        settings.pack(fill="x", padx=12, pady=6)

        ttk.Label(settings, text="Device:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        self.device_var = StringVar()
        self.device_combo = ttk.Combobox(
            settings, textvariable=self.device_var, values=[], width=48, state="readonly"
        )
        self.device_combo.grid(row=0, column=1, sticky="we", padx=5)
        ttk.Button(settings, text="Refresh", command=self.refresh_devices).grid(
            row=0, column=2, sticky="w"
        )

        options = ttk.Frame(settings)
        options.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Checkbutton(options, text="Auto-reboot after flash", variable=self.auto_reboot).pack(
            side="left", padx=(0, 12)
        )
        ttk.Checkbutton(
            options, text="Repartition (needs PIT)", variable=self.repartition
        ).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(options, text="T-Flash (SD card)", variable=self.tflash).pack(
            side="left"
        )

        ttk.Label(settings, text="PIT file:").grid(row=2, column=0, sticky="w", pady=(8, 0), padx=(0, 4))
        self.pit_var = StringVar()
        ttk.Entry(settings, textvariable=self.pit_var).grid(row=2, column=1, sticky="we", padx=5, pady=(8, 0))
        ttk.Button(settings, text="Browse...", command=self.browse_pit).grid(
            row=2, column=2, sticky="w", pady=(8, 0)
        )

        settings.columnconfigure(1, weight=1)

        # Add partition frame
        primary = ttk.LabelFrame(self.master, text="Add Partition Image", padding=(12, 8))
        primary.pack(fill="x", padx=12, pady=6)

        ttk.Label(primary, text="Partition:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        self.partition_var = StringVar(value=COMMON_PARTITIONS[0])
        ttk.Combobox(
            primary, textvariable=self.partition_var, values=COMMON_PARTITIONS, width=18
        ).grid(row=0, column=1, sticky="w", padx=5)

        ttk.Label(primary, text="Image:").grid(row=1, column=0, sticky="w", pady=8, padx=(0, 4))
        self.file_var = StringVar()
        self.file_entry = ttk.Entry(primary, textvariable=self.file_var)
        self.file_entry.grid(row=1, column=1, columnspan=2, sticky="we", padx=5)
        ttk.Button(primary, text="Browse...", command=self.browse_image).grid(
            row=1, column=3, sticky="w", padx=(0, 6)
        )
        ttk.Button(primary, text="Add", command=self.add_partition).grid(
            row=1, column=4, sticky="w"
        )
        primary.columnconfigure(1, weight=1)

        # Partition list
        list_frame = ttk.LabelFrame(self.master, text="Partitions to Flash", padding=(10, 8))
        list_frame.pack(fill="both", expand=True, padx=12, pady=6)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        cols = ("partition", "file", "size")
        self.part_tree = ttk.Treeview(
            list_frame, columns=cols, show="headings", selectmode="browse"
        )
        self.part_tree.heading("partition", text="Partition")
        self.part_tree.heading("file", text="File")
        self.part_tree.heading("size", text="Size")
        self.part_tree.column("partition", width=140, anchor="w")
        self.part_tree.column("file", width=420, anchor="w")
        self.part_tree.column("size", width=90, anchor="e")
        self.part_tree.grid(row=0, column=0, sticky="nsew")

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.part_tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self.part_tree.configure(yscrollcommand=vsb.set)

        btn_frame = ttk.Frame(list_frame)
        btn_frame.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(btn_frame, text="Remove selected", command=self.remove_partition).pack(
            side="left", padx=(0, 4)
        )
        ttk.Button(btn_frame, text="Clear all", command=self.clear_partitions).pack(side="left")

        # Action buttons
        actions = ttk.Frame(self.master, padding=(0, 4))
        actions.pack(fill="x", padx=12, pady=(6, 0))
        for text, command in [
            ("Detect", self.detect_device),
            ("Print PIT", self.print_pit),
            ("Flash", self.flash_all),
            ("Reboot", self.reboot_device),
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
            ".", background=t["bg"], foreground=t["fg"], fieldbackground=t["field"], font=NORMAL_FONT
        )
        style.configure("TFrame", background=t["bg"])
        style.configure("TLabel", background=t["bg"], foreground=t["fg"])
        style.configure("Header.TLabel", background=t["bg"], foreground=t["accent"], font=HEADER_FONT)
        style.configure(
            "TButton", background=t["field"], foreground=t["fg"], bordercolor=t["accent"],
            font=NORMAL_FONT, padding=4,
        )
        style.map(
            "TButton",
            background=[("active", t["accent"]), ("pressed", t["accent"])],
            foreground=[("active", "#ffffff"), ("pressed", "#ffffff")],
        )
        style.configure("TEntry", fieldbackground=t["field"], foreground=t["fg"],
                        insertcolor=t["fg"], padding=3)
        style.configure("TCombobox", fieldbackground=t["field"], background=t["field"],
                        foreground=t["fg"], selectbackground=t["select"], arrowcolor=t["fg"], padding=2)
        style.map("TCombobox", fieldbackground=[("readonly", t["field"])])
        style.configure("TCheckbutton", background=t["bg"], foreground=t["fg"])
        style.configure("Horizontal.TProgressbar", troughcolor=t["field"], background=t["accent"],
                        darkcolor=t["accent"], lightcolor=t["accent"], thickness=14)
        style.configure("TLabelFrame", background=t["bg"], foreground=t["fg"])
        style.configure("TLabelFrame.Label", background=t["bg"], foreground=t["fg"])
        style.configure("Treeview", background=t["tree"], foreground=t["fg"],
                        fieldbackground=t["tree"], selectbackground=t["select"],
                        font=NORMAL_FONT, rowheight=24)
        style.configure("Treeview.Heading", background=t["field"], foreground=t["fg"],
                        font=(FONT_FAMILY, 9, "bold"))
        style.configure("HSeparator.TSeparator", background=t["accent"])

        self.master.configure(background=t["bg"])
        menu_config = {
            "background": t["bg"], "foreground": t["fg"],
            "activebackground": t["select"], "activeforeground": "#ffffff",
        }
        for attr in ("menubar", "file_menu", "settings_menu", "help_menu"):
            if hasattr(self, attr):
                getattr(self, attr).configure(**menu_config)

        self.log_text.configure(
            bg=t["text_bg"], fg=t["text_fg"], insertbackground=t["fg"],
            selectbackground=t["select"], font=NORMAL_FONT, borderwidth=0, padx=6, pady=6,
        )

        for dialog in self.master.winfo_children():
            if isinstance(dialog, Toplevel):
                dialog.configure(background=t["bg"])
                for child in dialog.winfo_children():
                    if isinstance(child, (Text, ScrolledText)):
                        child.configure(bg=t["text_bg"], fg=t["text_fg"],
                                        insertbackground=t["fg"], selectbackground=t["select"])

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
            elif kind == "done":
                self._running.clear()
        self.master.after(100, self.poll_queue)

    def _append_log(self, message: str):
        self.log_text["state"] = "normal"
        self.log_text.insert("end", message.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text["state"] = "disabled"

    # ------------------------------------------------------------------ #
    # Device / file handling
    # ------------------------------------------------------------------ #

    def refresh_devices(self):
        try:
            self.devices = odin_protocol.find_devices()
        except OdinError as exc:
            self.devices = []
            self.log_message(str(exc))
        labels = [d.label for d in self.devices]
        self.device_combo["values"] = labels
        if labels and not self.device_var.get():
            self.device_var.set(labels[0])
        if not labels:
            self.device_var.set("")
        self.set_status(f"Found {len(self.devices)} Samsung device(s)")

    def _selected_device(self):
        label = self.device_var.get()
        for dev in self.devices:
            if dev.label == label:
                return dev
        return None

    def browse_image(self):
        path = filedialog.askopenfilename(
            title="Select partition image",
            filetypes=[("Firmware images", "*.img *.bin *.mbn *.ext4 *.lz4"), ("All files", "*.*")],
        )
        if path:
            self.file_var.set(path)
            if not self.partition_var.get():
                self.partition_var.set(guess_partition(path))

    def browse_pit(self):
        path = filedialog.askopenfilename(
            title="Select PIT file",
            filetypes=[("PIT files", "*.pit"), ("All files", "*.*")],
        )
        if path:
            self.pit_var.set(path)

    def _on_file_drop(self, event):
        files = self.master.tk.splitlist(event.data) if hasattr(event, "data") else []
        if not files:
            return
        path = files[0]
        if os.path.isfile(path):
            self.file_var.set(path)
            self.partition_var.set(guess_partition(path))
            self.log_message(f"Dropped file: {path}")

    # ------------------------------------------------------------------ #
    # Partition list management
    # ------------------------------------------------------------------ #

    def add_partition(self):
        partition = self.partition_var.get().strip()
        path = self.file_var.get().strip()
        if not partition:
            messagebox.showerror("Missing partition", "Please enter a partition name or id.")
            return
        if not path:
            messagebox.showerror("Missing image", "Please select a partition image.")
            return
        if not os.path.isfile(path):
            messagebox.showerror("File not found", f"Image not found: {path}")
            return
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            messagebox.showerror("Error", f"Could not read file: {exc}")
            return
        self.partitions.append({"partition": partition, "path": path, "size": size})
        self._refresh_part_tree()
        self.set_status(f"Added partition {partition}")

    def add_from_tar(self):
        path = filedialog.askopenfilename(
            title="Select firmware TAR",
            filetypes=[("Firmware archives", "*.tar *.md5 *.tar.md5"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            tmp_dir = tempfile.mkdtemp(prefix="samsung_fw_")
            self._temp_dirs.append(tmp_dir)
            added = 0
            with tarfile.open(path, "r:*") as tar:
                for member in tar.getmembers():
                    if not member.isfile():
                        continue
                    name = os.path.basename(member.name)
                    if os.path.isabs(member.name) or ".." in member.name.split("/"):
                        self.log_message(f"Skipping unsafe archive member path: {member.name}")
                        continue
                    if name.lower().endswith((".lz4",)):
                        self.log_message(
                            f"Skipping LZ4-compressed member (decompress first): {name}"
                        )
                        continue
                    extracted = os.path.join(tmp_dir, name)
                    with tar.extractfile(member) as src, open(extracted, "wb") as dst:
                        while True:
                            block = src.read(1024 * 1024)
                            if not block:
                                break
                            dst.write(block)
                    self.partitions.append({
                        "partition": guess_partition(name),
                        "path": extracted,
                        "size": os.path.getsize(extracted),
                    })
                    added += 1
            self._refresh_part_tree()
            self.set_status(f"Added {added} image(s) from {os.path.basename(path)}")
            if added == 0:
                messagebox.showwarning(
                    "Nothing added",
                    "No flashable images were found (LZ4-compressed members are skipped).",
                )
        except (tarfile.TarError, OSError) as exc:
            messagebox.showerror("Error", f"Could not read TAR: {exc}")

    def remove_partition(self):
        selected = self.part_tree.selection()
        if not selected:
            return
        idx = int(selected[0])
        self.partitions.pop(idx)
        self._refresh_part_tree()

    def clear_partitions(self):
        self.partitions.clear()
        self._refresh_part_tree()

    def _refresh_part_tree(self):
        self.part_tree.delete(*self.part_tree.get_children())
        for idx, item in enumerate(self.partitions):
            self.part_tree.insert(
                "", "end", iid=str(idx),
                values=(item["partition"], item["path"], item["size"]),
            )

    # ------------------------------------------------------------------ #
    # Profiles
    # ------------------------------------------------------------------ #

    def save_profile(self):
        path = filedialog.asksaveasfilename(
            title="Save profile", defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        data = {
            "dark_mode": self.dark_mode.get(),
            "repartition": self.repartition.get(),
            "auto_reboot": self.auto_reboot.get(),
            "tflash": self.tflash.get(),
            "pit": self.pit_var.get(),
            "partitions": self.partitions,
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
            self.dark_mode.set(data.get("dark_mode", True))
            self.repartition.set(data.get("repartition", False))
            self.auto_reboot.set(data.get("auto_reboot", True))
            self.tflash.set(data.get("tflash", False))
            self.pit_var.set(data.get("pit", ""))
            self.partitions = data.get("partitions", [])
            self._refresh_part_tree()
            self.apply_theme()
            self.set_status(f"Profile loaded: {path}")
        except Exception as exc:
            messagebox.showerror("Error", f"Could not load profile: {exc}")

    def save_log(self):
        path = filedialog.asksaveasfilename(
            title="Save log", defaultextension=".log",
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
    # Worker plumbing
    # ------------------------------------------------------------------ #

    def _busy(self) -> bool:
        if self._running.is_set():
            messagebox.showinfo("Busy", "An operation is already in progress.")
            return True
        return False

    def _start_worker(self, target, status: str):
        self._running.set()
        self._stop_requested.clear()
        self.set_progress(0, "determinate")
        self.set_status(status)

        def wrapper():
            try:
                target()
            except OdinError as exc:
                self.log_message(f"Error: {exc}")
                self.set_status(f"Failed: {exc}")
            except Exception as exc:  # noqa: BLE001 - surface unexpected errors in the log
                self.log_message(f"Unexpected error: {exc}")
                self.set_status(f"Failed: {exc}")
            finally:
                self.set_progress(0, "determinate")
                self.cmd_queue.put(("done",))

        threading.Thread(target=wrapper, daemon=True).start()

    def _progress_cb(self, done: int, total: int):
        if total:
            self.set_progress(int(done * 100 / total))

    def _should_stop(self) -> bool:
        return self._stop_requested.is_set()

    # ------------------------------------------------------------------ #
    # High-level commands
    # ------------------------------------------------------------------ #

    def detect_device(self):
        if self._busy():
            return

        def task():
            device = OdinDevice(log=self.log_message)
            device.open(self._selected_device())
            try:
                device.handshake()
                device.begin_session()
                pit_bytes = device.download_pit()
                pit = PitData.unpack(pit_bytes)
                self.log_message(pit.describe())
                device.end_session(reboot=False)
                self.set_status("Detect complete")
            finally:
                device.close()

        self._start_worker(task, "Detecting device...")

    def print_pit(self):
        if self._busy():
            return
        pit_path = self.pit_var.get().strip()
        if pit_path:
            try:
                with open(pit_path, "rb") as fh:
                    pit = PitData.unpack(fh.read())
                self.log_message(f"Local PIT: {pit_path}")
                self.log_message(pit.describe())
                self.set_status("Printed local PIT")
            except (OSError, OdinError) as exc:
                messagebox.showerror("Error", f"Could not read PIT: {exc}")
            return
        # No local PIT: download from device.
        self.detect_device()

    def flash_all(self):
        if self._busy():
            return
        if not self.partitions:
            messagebox.showerror("Nothing to flash", "Add at least one partition image.")
            return
        for item in self.partitions:
            if not os.path.isfile(item["path"]):
                messagebox.showerror("File not found", f"Image not found: {item['path']}")
                return
        pit_path = self.pit_var.get().strip() or None
        if self.repartition.get() and not pit_path:
            messagebox.showerror("PIT required", "Repartition requires a PIT file.")
            return

        warning = "This will overwrite partitions on the connected device. Continue?"
        if not messagebox.askyesno("Confirm flash", warning):
            return

        parts = [FlashPartition(partition=p["partition"], path=p["path"]) for p in self.partitions]

        def task():
            odin_protocol.flash(
                parts,
                pit_path=pit_path,
                repartition=self.repartition.get(),
                tflash=self.tflash.get(),
                reboot=self.auto_reboot.get(),
                device_info=self._selected_device(),
                log_cb=self.log_message,
                progress_cb=self._progress_cb,
                should_stop=self._should_stop,
            )
            self.set_progress(100, "determinate")
            self.set_status("Flash complete")

        self._start_worker(task, "Flashing...")

    def reboot_device(self):
        if self._busy():
            return

        def task():
            device = OdinDevice(log=self.log_message)
            device.open(self._selected_device())
            try:
                device.handshake()
                device.begin_session()
                device.end_session(reboot=True)
                self.set_status("Reboot sent")
            finally:
                device.close()

        self._start_worker(task, "Rebooting device...")

    def stop(self):
        if self._running.is_set():
            self._stop_requested.set()
            self.log_message("-- stop requested; will halt after the current transfer --")
            self.set_status("Stopping...")

    # ------------------------------------------------------------------ #
    # Misc
    # ------------------------------------------------------------------ #

    def _on_close(self):
        for tmp_dir in self._temp_dirs:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        self._temp_dirs.clear()
        self.master.destroy()

    def _show_about(self):
        messagebox.showinfo(
            "About",
            "Samsung Flash Tool\n"
            "A desktop GUI that flashes Samsung devices using a native Python\n"
            "implementation of the Odin/LOKE download-mode protocol.\n",
        )


def main():
    if HAVE_DND:
        root = TkinterDnD.Tk()
    else:
        root = Tk()
    SamsungFlasherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
