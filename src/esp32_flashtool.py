#!/usr/bin/env python3
"""A simple desktop GUI wrapper around esptool for flashing ESP32 devices."""

import os
import re
import subprocess
import sys
import threading
from queue import Queue
from tkinter import BooleanVar, StringVar, Tk, Toplevel, filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from serial.tools.list_ports import comports

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


def get_esptool_base():
    """Return the command base used to invoke esptool."""
    return [sys.executable, "-m", "esptool"]


class ESP32FlasherApp:
    def __init__(self, master: Tk):
        self.master = master
        self.master.title("ESP32 Flash Tool")
        self.master.geometry("720x560")
        self.master.minsize(640, 480)

        self.cmd_queue: Queue = Queue()
        self.proc = None
        self._running = threading.Event()

        self._build_widgets()
        self.poll_queue()
        self.refresh_ports()

    def _build_widgets(self):
        # Settings frame
        settings = ttk.LabelFrame(self.master, text="Settings", padding=(10, 5))
        settings.pack(fill="x", padx=10, pady=5)

        ttk.Label(settings, text="Port:").grid(row=0, column=0, sticky="w")
        self.port_var = StringVar()
        self.port_combo = ttk.Combobox(
            settings, textvariable=self.port_var, values=[], width=25, state="readonly"
        )
        self.port_combo.grid(row=0, column=1, sticky="we", padx=5)
        ttk.Button(settings, text="Refresh", command=self.refresh_ports).grid(
            row=0, column=2, sticky="w"
        )

        ttk.Label(settings, text="Baud:").grid(row=1, column=0, sticky="w", pady=5)
        self.baud_var = StringVar(value=ESP32_BAUDS[0])
        ttk.Combobox(
            settings, textvariable=self.baud_var, values=ESP32_BAUDS, width=10
        ).grid(row=1, column=1, sticky="w", padx=5)

        ttk.Label(settings, text="Chip:").grid(row=2, column=0, sticky="w")
        self.chip_var = StringVar(value=ESP32_CHIPS[0])
        ttk.Combobox(
            settings, textvariable=self.chip_var, values=ESP32_CHIPS, width=12
        ).grid(row=2, column=1, sticky="w", padx=5)

        ttk.Label(settings, text="Firmware:").grid(row=3, column=0, sticky="w", pady=5)
        self.file_var = StringVar()
        ttk.Entry(settings, textvariable=self.file_var, width=45).grid(
            row=3, column=1, sticky="we", padx=5
        )
        ttk.Button(settings, text="Browse...", command=self.browse_firmware).grid(
            row=3, column=2, sticky="w"
        )

        ttk.Label(settings, text="Address:").grid(row=4, column=0, sticky="w")
        self.addr_var = StringVar(value=DEFAULT_ADDR)
        ttk.Entry(settings, textvariable=self.addr_var, width=12).grid(
            row=4, column=1, sticky="w", padx=5
        )

        self.erase_before_var = BooleanVar(value=False)
        ttk.Checkbutton(
            settings, text="Erase before flash", variable=self.erase_before_var
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=5)

        settings.columnconfigure(1, weight=1)

        # Action buttons
        actions = ttk.Frame(self.master)
        actions.pack(fill="x", padx=10, pady=5)
        for text, command in [
            ("Connect / Info", self.get_info),
            ("Flash", self.flash),
            ("Erase", self.erase),
            ("Read Flash", self.read_flash),
            ("Stop", self.stop),
        ]:
            ttk.Button(actions, text=text, command=command).pack(
                side="left", padx=5, expand=True, fill="x"
            )

        # Progress
        self.progress = ttk.Progressbar(
            self.master, mode="determinate", maximum=100, value=0
        )
        self.progress.pack(fill="x", padx=10, pady=5)

        # Log
        log_frame = ttk.LabelFrame(self.master, text="Output", padding=(5, 5))
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)
        self.log_text = ScrolledText(log_frame, wrap="word", height=15, state="disabled")
        self.log_text.pack(fill="both", expand=True)

        # Status bar
        self.status_var = StringVar(value="Ready")
        ttk.Label(self.master, textvariable=self.status_var, relief="sunken").pack(
            fill="x", padx=10, pady=2
        )

    def log_message(self, message: str):
        """Append a line to the log widget."""
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
        self.master.after(100, self.poll_queue)

    def _append_log(self, message: str):
        self.log_text["state"] = "normal"
        self.log_text.insert("end", message.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text["state"] = "disabled"

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

    def _validate(self, require_file: bool = False):
        port = self.port_var.get().strip()
        if not port:
            messagebox.showerror("Missing port", "Please select a serial port.")
            return False
        if require_file:
            path = self.file_var.get().strip()
            if not path:
                messagebox.showerror(
                    "Missing firmware", "Please select a firmware binary."
                )
                return False
            if not os.path.isfile(path):
                messagebox.showerror("File not found", f"Firmware not found: {path}")
                return False
        return True

    def _base_args(self):
        args = get_esptool_base()
        chip = self.chip_var.get()
        if chip and chip != "auto":
            args += ["--chip", chip]
        else:
            args += ["--chip", "auto"]
        args += ["--port", self.port_var.get(), "--baud", self.baud_var.get()]
        return args

    def _run_esptool(self, args: list, progress: bool = True):
        self._running.set()
        self.set_progress(0, "determinate")
        self.set_status("Running...")
        self.log_message("$ " + " ".join(args))

        def target():
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
                        if progress:
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

        threading.Thread(target=target, daemon=True).start()

    def get_info(self):
        if not self._validate():
            return
        self._run_esptool(self._base_args() + ["chip_id"])

    def flash(self):
        if not self._validate(require_file=True):
            return
        if self.erase_before_var.get():
            if not messagebox.askyesno(
                "Confirm erase",
                "Erase will wipe the entire flash. Continue?",
            ):
                return
        addr = self.addr_var.get().strip() or DEFAULT_ADDR
        args = self._base_args() + ["write_flash"]
        if self.erase_before_var.get():
            args += ["--erase-all"]
        args += [addr, self.file_var.get()]
        self._run_esptool(args)

    def erase(self):
        if not self._validate():
            return
        if not messagebox.askyesno(
            "Confirm erase", "This will erase all flash contents. Continue?"
        ):
            return
        self._run_esptool(self._base_args() + ["erase_flash"])

    def read_flash(self):
        if not self._validate():
            return
        path = filedialog.asksaveasfilename(
            title="Save flash dump",
            defaultextension=".bin",
            filetypes=[("Binary files", "*.bin"), ("All files", "*.*")],
        )
        if not path:
            return
        # Ask for size via simple dialog
        dialog = Toplevel(self.master)
        dialog.title("Read size")
        ttk.Label(dialog, text="Size in bytes (e.g. 0x100000):").pack(padx=10, pady=5)
        size_var = StringVar(value="0x100000")
        ttk.Entry(dialog, textvariable=size_var).pack(padx=10, fill="x")

        def do_read():
            size = size_var.get().strip()
            try:
                int(size, 0)  # validate
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


def main():
    root = Tk()
    ESP32FlasherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
