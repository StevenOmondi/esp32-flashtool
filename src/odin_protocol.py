#!/usr/bin/env python3
"""Native Python implementation of the Samsung Odin / LOKE download-mode protocol.

This module speaks the same USB bulk protocol that Samsung's Odin (and the
open-source Heimdall) use to flash firmware to a device booted into
Download / Odin mode. It is a clean-room re-implementation in pure Python on
top of ``pyusb`` (libusb) and carries no code from those projects.

The protocol at a glance::

    host                              device (LOKE bootloader)
    ---- "ODIN" ------------------->
    <--- "LOKE" --------------------
    ---- BeginSession ------------->
    <--- default packet size -------
    (optionally) FilePartSize / TotalBytes / EnableTFlash
    ---- DumpPit / FlashPit / FlashPartition ...
    ---- EndSession (+reboot) ----->

All control packets are 1024-byte little-endian buffers whose first word is
the control type; responses are 8-byte little-endian buffers whose first word
echoes the response type and whose second word carries a result value.
"""

from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass, field

try:
    import usb.core
    import usb.util

    HAVE_PYUSB = True
except Exception:  # pragma: no cover - pyusb optional at import time
    HAVE_PYUSB = False


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

SAMSUNG_VID = 0x04E8

# Known Samsung download / Odin mode product IDs. Modern devices almost always
# enumerate as 0x685D, but we keep the historical ids too. Detection also falls
# back to "any Samsung device exposing a CDC-data interface with two bulk
# endpoints", so unlisted ids still work.
DOWNLOAD_MODE_PIDS = (0x6601, 0x685D, 0x68C3, 0x6860)

USB_CLASS_CDC_DATA = 0x0A

# Control packet types (first word of an outbound control packet).
CTL_SESSION = 0x64
CTL_PIT_FILE = 0x65
CTL_FILE_TRANSFER = 0x66
CTL_END_SESSION = 0x67

# Response types (first word of an 8-byte inbound response).
RSP_SEND_FILE_PART = 0x00
RSP_SESSION_SETUP = 0x64
RSP_PIT_FILE = 0x65
RSP_FILE_TRANSFER = 0x66
RSP_END_SESSION = 0x67

# Session-setup sub-requests (second word of a session control packet).
SESSION_BEGIN = 0
SESSION_DEVICE_TYPE = 1
SESSION_TOTAL_BYTES = 2
SESSION_FILE_PART_SIZE = 5
SESSION_ENABLE_TFLASH = 8

# PIT file sub-requests.
PIT_FLASH = 0
PIT_DUMP = 1
PIT_PART = 2
PIT_END_TRANSFER = 3

# File-transfer sub-requests.
FT_FLASH = 0
FT_DUMP = 1
FT_PART = 2
FT_END = 3

# EndFileTransfer destinations.
DEST_PHONE = 0
DEST_MODEM = 1

# EndSession sub-requests.
END_SESSION = 0
REBOOT_DEVICE = 1

CONTROL_PACKET_SIZE = 1024
RESPONSE_SIZE = 8
PIT_FILE_PART_SIZE = 500  # bytes per PIT dump chunk

# Defaults (overridden after BeginSession negotiates a larger packet size).
DEFAULT_TIMEOUT_SEND = 3000
DEFAULT_TIMEOUT_RECEIVE = 3000
DEFAULT_TIMEOUT_EMPTY = 100
DEFAULT_FILE_PART_SIZE = 131072  # 128 KiB
DEFAULT_SEQUENCE_MAX_LENGTH = 800
DEFAULT_SEQUENCE_TIMEOUT = 30000


class OdinError(Exception):
    """Raised for any protocol- or transport-level failure."""


def _check_u32(value: int, what: str) -> None:
    """Sizes are carried as 32-bit words, so anything larger cannot be expressed."""
    if value > 0xFFFFFFFF:
        raise OdinError(f"{what} ({value} bytes) exceeds the protocol's 4 GiB limit.")


# --------------------------------------------------------------------------- #
# PIT (Partition Information Table) parsing / packing
# --------------------------------------------------------------------------- #

PIT_MAGIC = 0x12349876
PIT_HEADER_SIZE = 28
PIT_ENTRY_SIZE = 132
PIT_NAME_LEN = 32
PIT_PADDING_MULTIPLE = 4096

BINARY_TYPE_AP = 0  # Application processor
BINARY_TYPE_CP = 1  # Communication processor (modem)


def _cstr(data: bytes, offset: int, length: int) -> str:
    raw = bytes(data[offset : offset + length])
    return raw.split(b"\x00", 1)[0].decode("latin-1")


@dataclass
class PitEntry:
    binary_type: int = 0
    device_type: int = 0
    identifier: int = 0
    attributes: int = 0
    update_attributes: int = 0
    block_size_or_offset: int = 0
    block_count: int = 0
    file_offset: int = 0
    file_size: int = 0
    partition_name: str = ""
    flash_filename: str = ""
    fota_filename: str = ""

    @property
    def is_flashable(self) -> bool:
        return bool(self.partition_name)

    @property
    def is_modem(self) -> bool:
        return self.binary_type == BINARY_TYPE_CP


@dataclass
class PitData:
    entry_count: int = 0
    unknown1: int = 0
    unknown2: int = 0
    unknown3: int = 0
    unknown4: int = 0
    unknown5: int = 0
    unknown6: int = 0
    unknown7: int = 0
    unknown8: int = 0
    entries: list = field(default_factory=list)

    @classmethod
    def unpack(cls, data: bytes) -> "PitData":
        if len(data) < PIT_HEADER_SIZE:
            raise OdinError("PIT data too small to contain a header.")
        magic = struct.unpack_from("<I", data, 0)[0]
        if magic != PIT_MAGIC:
            raise OdinError(f"Invalid PIT magic 0x{magic:08X} (expected 0x{PIT_MAGIC:08X}).")

        pit = cls()
        pit.entry_count = struct.unpack_from("<I", data, 4)[0]
        pit.unknown1 = struct.unpack_from("<I", data, 8)[0]
        pit.unknown2 = struct.unpack_from("<I", data, 12)[0]
        (pit.unknown3, pit.unknown4, pit.unknown5,
         pit.unknown6, pit.unknown7, pit.unknown8) = struct.unpack_from("<6H", data, 16)

        for i in range(pit.entry_count):
            base = PIT_HEADER_SIZE + i * PIT_ENTRY_SIZE
            if base + PIT_ENTRY_SIZE > len(data):
                raise OdinError("PIT data truncated while reading entries.")
            (binary_type, device_type, identifier, attributes, update_attributes,
             block_size_or_offset, block_count, file_offset,
             file_size) = struct.unpack_from("<9I", data, base)
            pit.entries.append(
                PitEntry(
                    binary_type=binary_type,
                    device_type=device_type,
                    identifier=identifier,
                    attributes=attributes,
                    update_attributes=update_attributes,
                    block_size_or_offset=block_size_or_offset,
                    block_count=block_count,
                    file_offset=file_offset,
                    file_size=file_size,
                    partition_name=_cstr(data, base + 36, PIT_NAME_LEN),
                    flash_filename=_cstr(data, base + 36 + PIT_NAME_LEN, PIT_NAME_LEN),
                    fota_filename=_cstr(data, base + 36 + 2 * PIT_NAME_LEN, PIT_NAME_LEN),
                )
            )
        return pit

    @property
    def data_size(self) -> int:
        return PIT_HEADER_SIZE + self.entry_count * PIT_ENTRY_SIZE

    @property
    def padded_size(self) -> int:
        size = self.data_size
        remainder = size % PIT_PADDING_MULTIPLE
        if remainder:
            size += PIT_PADDING_MULTIPLE - remainder
        return size

    def pack(self) -> bytes:
        buf = bytearray(self.padded_size)
        struct.pack_into("<I", buf, 0, PIT_MAGIC)
        struct.pack_into("<I", buf, 4, self.entry_count)
        struct.pack_into("<I", buf, 8, self.unknown1)
        struct.pack_into("<I", buf, 12, self.unknown2)
        struct.pack_into("<6H", buf, 16, self.unknown3, self.unknown4, self.unknown5,
                         self.unknown6, self.unknown7, self.unknown8)
        for i, e in enumerate(self.entries):
            base = PIT_HEADER_SIZE + i * PIT_ENTRY_SIZE
            struct.pack_into("<9I", buf, base, e.binary_type, e.device_type, e.identifier,
                             e.attributes, e.update_attributes, e.block_size_or_offset,
                             e.block_count, e.file_offset, e.file_size)
            buf[base + 36 : base + 36 + PIT_NAME_LEN] = e.partition_name.encode("latin-1")[
                : PIT_NAME_LEN - 1
            ].ljust(PIT_NAME_LEN, b"\x00")
            buf[base + 36 + PIT_NAME_LEN : base + 36 + 2 * PIT_NAME_LEN] = (
                e.flash_filename.encode("latin-1")[: PIT_NAME_LEN - 1].ljust(PIT_NAME_LEN, b"\x00")
            )
            buf[base + 36 + 2 * PIT_NAME_LEN : base + 36 + 3 * PIT_NAME_LEN] = (
                e.fota_filename.encode("latin-1")[: PIT_NAME_LEN - 1].ljust(PIT_NAME_LEN, b"\x00")
            )
        return bytes(buf)

    def find_entry(self, key) -> "PitEntry | None":
        """Find an entry by partition name (str) or numeric identifier (int)."""
        if isinstance(key, int):
            for e in self.entries:
                if e.identifier == key:
                    return e
            return None
        key_norm = str(key).strip()
        for e in self.entries:
            if e.partition_name == key_norm:
                return e
        # Case-insensitive fallback.
        for e in self.entries:
            if e.partition_name.lower() == key_norm.lower():
                return e
        return None

    def describe(self) -> str:
        lines = [f"PIT: {self.entry_count} entries"]
        for e in self.entries:
            if not e.is_flashable:
                continue
            kind = "CP" if e.is_modem else "AP"
            lines.append(
                f"  id={e.identifier:<3} [{kind}] {e.partition_name:<20} "
                f"flash='{e.flash_filename}'"
            )
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# USB device discovery
# --------------------------------------------------------------------------- #

@dataclass
class DeviceInfo:
    vid: int
    pid: int
    bus: int
    address: int
    product: str = ""

    @property
    def label(self) -> str:
        name = self.product or "Samsung device"
        return f"{name} ({self.vid:04X}:{self.pid:04X}) bus {self.bus} addr {self.address}"


def _interface_is_odin(intf) -> bool:
    ins = [ep for ep in intf if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN]
    outs = [ep for ep in intf if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_OUT]
    return len(list(intf)) == 2 and len(ins) == 1 and len(outs) == 1


def find_devices() -> "list[DeviceInfo]":
    """Return every attached Samsung device that looks like it is in download mode."""
    if not HAVE_PYUSB:
        raise OdinError("pyusb is not installed. Install it with 'pip install pyusb'.")
    found = []
    try:
        devices = usb.core.find(find_all=True, idVendor=SAMSUNG_VID)
    except usb.core.NoBackendError as exc:
        raise OdinError(
            "No USB backend available. Install libusb (e.g. 'apt install libusb-1.0-0' "
            "or 'brew install libusb')."
        ) from exc
    for dev in devices:
        try:
            product = usb.util.get_string(dev, dev.iProduct) or ""
        except Exception:
            product = ""
        found.append(
            DeviceInfo(
                vid=dev.idVendor,
                pid=dev.idProduct,
                bus=getattr(dev, "bus", 0) or 0,
                address=getattr(dev, "address", 0) or 0,
                product=product,
            )
        )
    return found


# --------------------------------------------------------------------------- #
# The bridge
# --------------------------------------------------------------------------- #

class OdinDevice:
    """A connection to a single Samsung device in Download / Odin mode."""

    def __init__(self, log=None):
        self._log_cb = log or (lambda _msg: None)
        self._dev = None
        self._intf = None
        self._in_ep = None
        self._out_ep = None
        self._detached = False
        self.file_part_size = DEFAULT_FILE_PART_SIZE
        self.sequence_max_length = DEFAULT_SEQUENCE_MAX_LENGTH
        self.sequence_timeout = DEFAULT_SEQUENCE_TIMEOUT

    # ---- logging -------------------------------------------------------- #

    def _log(self, message: str):
        self._log_cb(message)

    # ---- connection ----------------------------------------------------- #

    def open(self, info: "DeviceInfo | None" = None):
        if not HAVE_PYUSB:
            raise OdinError("pyusb is not installed. Install it with 'pip install pyusb'.")

        try:
            self._locate_and_open(info)
        except usb.core.NoBackendError as exc:
            raise OdinError(
                "No USB backend available. Install libusb (e.g. 'apt install libusb-1.0-0' "
                "or 'brew install libusb')."
            ) from exc

    def _locate_and_open(self, info):
        if info is not None:
            dev = usb.core.find(
                idVendor=info.vid, idProduct=info.pid,
                custom_match=lambda d: (getattr(d, "bus", 0) or 0) == info.bus
                and (getattr(d, "address", 0) or 0) == info.address,
            )
        else:
            dev = None
            for pid in DOWNLOAD_MODE_PIDS:
                dev = usb.core.find(idVendor=SAMSUNG_VID, idProduct=pid)
                if dev is not None:
                    break
            if dev is None:
                dev = usb.core.find(idVendor=SAMSUNG_VID)

        if dev is None:
            raise OdinError("No Samsung device found. Boot the phone into Download mode and connect USB.")

        self._dev = dev
        self._log(f"Opening device {dev.idVendor:04X}:{dev.idProduct:04X}...")

        try:
            dev.set_configuration()
        except usb.core.USBError as exc:
            # A configuration is often already active, in which case this is benign.
            self._log(f"set_configuration warning (continuing): {exc}")

        cfg = dev.get_active_configuration()
        chosen = None
        for intf in cfg:
            if intf.bInterfaceClass == USB_CLASS_CDC_DATA and _interface_is_odin(intf):
                chosen = intf
                break
        if chosen is None:
            # Fallback: any interface with exactly one bulk IN and one bulk OUT.
            for intf in cfg:
                if _interface_is_odin(intf):
                    chosen = intf
                    break
        if chosen is None:
            raise OdinError("Could not find the download-mode interface on the device.")

        self._intf = chosen
        self._detach_kernel_driver(chosen.bInterfaceNumber)
        usb.util.claim_interface(dev, chosen.bInterfaceNumber)

        self._in_ep = usb.util.find_descriptor(
            chosen,
            custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN,
        )
        self._out_ep = usb.util.find_descriptor(
            chosen,
            custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT,
        )
        if self._in_ep is None or self._out_ep is None:
            raise OdinError("Failed to locate bulk endpoints on the download-mode interface.")
        self._log("Interface claimed.")

    def _detach_kernel_driver(self, intf_number: int):
        try:
            if self._dev.is_kernel_driver_active(intf_number):
                self._dev.detach_kernel_driver(intf_number)
                self._detached = True
                self._log("Detached kernel driver.")
        except (NotImplementedError, usb.core.USBError):
            pass

    def close(self):
        if self._dev is None:
            return
        try:
            if self._intf is not None:
                usb.util.release_interface(self._dev, self._intf.bInterfaceNumber)
                if self._detached:
                    try:
                        self._dev.attach_kernel_driver(self._intf.bInterfaceNumber)
                    except (NotImplementedError, usb.core.USBError):
                        pass
        finally:
            usb.util.dispose_resources(self._dev)
            self._dev = None
            self._intf = None
            self._in_ep = None
            self._out_ep = None
            self._detached = False

    # ---- low level bulk transfers -------------------------------------- #

    def _send_bulk(self, data: bytes, timeout: int) -> None:
        written = self._out_ep.write(data, timeout)
        if written != len(data):
            raise OdinError(f"Short bulk write: sent {written} of {len(data)} bytes.")

    def _recv_bulk(self, length: int, timeout: int) -> bytes:
        # Reading zero bytes is unreliable across backends; read one dummy byte.
        read_len = length if length > 0 else 1
        data = self._dev.read(self._in_ep.bEndpointAddress, read_len, timeout)
        return bytes(data)

    def _send_empty(self):
        try:
            self._out_ep.write(b"", DEFAULT_TIMEOUT_EMPTY)
        except usb.core.USBError:
            pass  # Best effort, matches reference behaviour.

    def _recv_empty(self):
        try:
            self._dev.read(self._in_ep.bEndpointAddress, 1, DEFAULT_TIMEOUT_EMPTY)
        except usb.core.USBError:
            pass  # Best effort.

    # ---- packet layer --------------------------------------------------- #

    def _send_packet(self, data: bytes, timeout: int = DEFAULT_TIMEOUT_SEND,
                     empty_before: bool = False, empty_after: bool = True) -> None:
        if empty_before:
            self._send_empty()
        self._send_bulk(data, timeout)
        if empty_after:
            self._send_empty()

    def _recv_packet(self, size: int = RESPONSE_SIZE, timeout: int = DEFAULT_TIMEOUT_RECEIVE,
                     empty_before: bool = False, empty_after: bool = False) -> bytes:
        if empty_before:
            self._recv_empty()
        data = self._recv_bulk(size, timeout)
        if empty_after:
            self._recv_empty()
        return data

    @staticmethod
    def _control(control_type: int, *words: int) -> bytes:
        buf = bytearray(CONTROL_PACKET_SIZE)
        struct.pack_into("<I", buf, 0, control_type)
        for i, word in enumerate(words):
            struct.pack_into("<I", buf, 4 + i * 4, word)
        return bytes(buf)

    def _control_response(self, expected_type: int, timeout: int = DEFAULT_TIMEOUT_RECEIVE,
                          empty_after: bool = False) -> int:
        data = self._recv_packet(RESPONSE_SIZE, timeout, empty_after=empty_after)
        if len(data) < RESPONSE_SIZE:
            raise OdinError(f"Response too short ({len(data)} bytes).")
        rsp_type, result = struct.unpack_from("<II", data, 0)
        if rsp_type != expected_type:
            raise OdinError(
                f"Unexpected response type 0x{rsp_type:02X} (expected 0x{expected_type:02X})."
            )
        return result

    # ---- handshake / session ------------------------------------------- #

    def handshake(self):
        self._log("Handshaking (ODIN -> LOKE)...")
        self._send_bulk(b"ODIN", 1000)
        data = self._recv_bulk(7, 1000)
        if data[:4] != b"LOKE":
            raise OdinError(f"Handshake failed. Expected 'LOKE', received {bytes(data[:4])!r}.")
        self._log("Handshake OK.")

    def begin_session(self):
        self._log("Beginning session...")
        self._send_packet(self._control(CTL_SESSION, SESSION_BEGIN))
        default_packet_size = self._control_response(RSP_SESSION_SETUP)
        # Give slow bootloaders a moment, as Odin/Heimdall do.
        time.sleep(1.0)

        if default_packet_size != 0:
            # The device supports a negotiated (larger) packet size.
            self.file_part_size = 1048576  # 1 MiB
            self.sequence_max_length = 30   # 30 MiB per sequence
            self.sequence_timeout = 120000  # 2 minutes
            self._send_packet(self._control(CTL_SESSION, SESSION_FILE_PART_SIZE, self.file_part_size))
            result = self._control_response(RSP_SESSION_SETUP)
            if result != 0:
                raise OdinError(f"Unexpected file-part-size response: {result}.")
        self._log("Session begun.")

    def enable_tflash(self):
        self._log("Enabling T-Flash (flash to SD card)...")
        self._send_packet(self._control(CTL_SESSION, SESSION_ENABLE_TFLASH))
        result = self._control_response(RSP_SESSION_SETUP, timeout=5000)
        if result != 0:
            raise OdinError(f"Unexpected T-Flash response: {result}.")

    def send_total_bytes(self, total_bytes: int):
        _check_u32(total_bytes, "Total transfer size")
        self._send_packet(self._control(CTL_SESSION, SESSION_TOTAL_BYTES, total_bytes))
        result = self._control_response(RSP_SESSION_SETUP)
        if result != 0:
            raise OdinError(f"Unexpected total-bytes response: {result}.")

    def end_session(self, reboot: bool = False):
        self._log("Ending session...")
        self._send_packet(self._control(CTL_END_SESSION, END_SESSION))
        self._control_response(RSP_END_SESSION)
        if reboot:
            self._log("Rebooting device...")
            self._send_packet(self._control(CTL_END_SESSION, REBOOT_DEVICE))
            self._control_response(RSP_END_SESSION)

    # ---- PIT ------------------------------------------------------------ #

    def download_pit(self) -> bytes:
        self._log("Downloading PIT from device...")
        self._send_packet(self._control(CTL_PIT_FILE, PIT_DUMP))
        file_size = self._control_response(RSP_PIT_FILE)

        transfer_count = (file_size + PIT_FILE_PART_SIZE - 1) // PIT_FILE_PART_SIZE
        buf = bytearray()
        for i in range(transfer_count):
            self._send_packet(self._control(CTL_PIT_FILE, PIT_PART, i))
            empty_after = i == transfer_count - 1
            chunk = self._recv_packet(PIT_FILE_PART_SIZE, DEFAULT_TIMEOUT_RECEIVE,
                                      empty_after=empty_after)
            buf.extend(chunk)

        self._send_packet(self._control(CTL_PIT_FILE, PIT_END_TRANSFER))
        self._control_response(RSP_PIT_FILE)
        self._log(f"PIT downloaded ({file_size} bytes).")
        return bytes(buf[:file_size])

    def flash_pit(self, pit_bytes: bytes):
        self._log("Flashing PIT (repartition)...")
        pit = PitData.unpack(pit_bytes)
        buf = pit.pack()
        size = len(buf)

        self._send_packet(self._control(CTL_PIT_FILE, PIT_FLASH))
        self._control_response(RSP_PIT_FILE)

        self._send_packet(self._control(CTL_PIT_FILE, PIT_PART, size))
        self._control_response(RSP_PIT_FILE)

        # The PIT body is sent as a raw outbound packet exactly `size` bytes long.
        self._send_packet(buf)
        self._control_response(RSP_PIT_FILE)

        self._send_packet(self._control(CTL_PIT_FILE, PIT_END_TRANSFER, size))
        self._control_response(RSP_PIT_FILE)
        self._log("PIT flashed.")

    # ---- file transfer -------------------------------------------------- #

    def send_file(self, file_path: str, destination: int, device_type: int,
                  file_identifier: int = 0xFFFFFFFF, progress_cb=None,
                  should_stop=None) -> None:
        """Flash a single file to the given destination (phone or modem)."""
        if destination not in (DEST_PHONE, DEST_MODEM):
            raise OdinError("Unknown file destination.")

        file_size = os.path.getsize(file_path)
        if file_size == 0:
            raise OdinError(f"Refusing to flash an empty file: {file_path}")
        _check_u32(file_size, f"Size of {os.path.basename(file_path)}")
        packet_size = self.file_part_size
        seq_max = self.sequence_max_length
        bytes_per_sequence = seq_max * packet_size

        sequence_count = file_size // bytes_per_sequence
        last_sequence_size = seq_max
        partial_packet = file_size % packet_size
        if file_size % bytes_per_sequence != 0:
            sequence_count += 1
            last_sequence_bytes = file_size % bytes_per_sequence
            last_sequence_size = last_sequence_bytes // packet_size
            if partial_packet != 0:
                last_sequence_size += 1

        # Begin file transfer.
        self._send_packet(self._control(CTL_FILE_TRANSFER, FT_FLASH))
        self._control_response(RSP_FILE_TRANSFER)

        bytes_transferred = 0
        with open(file_path, "rb") as fh:
            for seq_index in range(sequence_count):
                if should_stop and should_stop():
                    raise OdinError("Flashing cancelled by user.")
                is_last = seq_index == sequence_count - 1
                sequence_size = last_sequence_size if is_last else seq_max
                sequence_total = sequence_size * packet_size

                self._send_packet(self._control(CTL_FILE_TRANSFER, FT_PART, sequence_total))
                self._control_response(RSP_FILE_TRANSFER)

                for part_index in range(sequence_size):
                    if should_stop and should_stop():
                        raise OdinError("Flashing cancelled by user.")
                    chunk = fh.read(packet_size)
                    if len(chunk) < packet_size:
                        chunk = chunk + b"\x00" * (packet_size - len(chunk))
                    empty_before = part_index != 0
                    self._send_packet(chunk, DEFAULT_TIMEOUT_SEND,
                                      empty_before=empty_before, empty_after=False)
                    received_index = self._send_file_part_response()
                    if received_index != part_index:
                        raise OdinError(
                            f"File part index mismatch: expected {part_index}, got {received_index}."
                        )
                    bytes_transferred = min(bytes_transferred + packet_size, file_size)
                    if progress_cb:
                        progress_cb(bytes_transferred, file_size)

                if is_last and partial_packet != 0:
                    effective = packet_size * (last_sequence_size - 1) + partial_packet
                else:
                    effective = sequence_total

                if destination == DEST_PHONE:
                    end_packet = self._control(
                        CTL_FILE_TRANSFER, FT_END, DEST_PHONE, effective, 0,
                        device_type, file_identifier, 1 if is_last else 0,
                    )
                else:
                    end_packet = self._control(
                        CTL_FILE_TRANSFER, FT_END, DEST_MODEM, effective, 0,
                        device_type, 1 if is_last else 0,
                    )
                self._send_packet(end_packet, DEFAULT_TIMEOUT_SEND,
                                  empty_before=True, empty_after=True)
                self._control_response(RSP_FILE_TRANSFER, timeout=self.sequence_timeout)

    def _send_file_part_response(self) -> int:
        data = self._recv_packet(RESPONSE_SIZE)
        if len(data) < RESPONSE_SIZE:
            raise OdinError("File-part response too short.")
        rsp_type, part_index = struct.unpack_from("<II", data, 0)
        if rsp_type != RSP_SEND_FILE_PART:
            raise OdinError(f"Unexpected file-part response type 0x{rsp_type:02X}.")
        return part_index


# --------------------------------------------------------------------------- #
# High-level orchestration
# --------------------------------------------------------------------------- #

@dataclass
class FlashPartition:
    """A file to flash, mapped to a device partition by name or numeric id."""
    partition: str  # partition name (e.g. "BOOT") or numeric identifier as string
    path: str


def flash(partitions, pit_path: "str | None" = None, repartition: bool = False,
          tflash: bool = False, reboot: bool = True, device_info: "DeviceInfo | None" = None,
          log_cb=None, progress_cb=None, should_stop=None) -> None:
    """Flash the given partitions to a connected Samsung device.

    ``partitions`` is a sequence of :class:`FlashPartition`. The target
    partition for each file is resolved against the device's PIT (or the local
    PIT when ``repartition`` is set).
    """
    log_cb = log_cb or (lambda _m: None)
    device = OdinDevice(log=log_cb)
    device.open(device_info)
    try:
        device.handshake()
        device.begin_session()

        if tflash:
            device.enable_tflash()

        total_bytes = sum(os.path.getsize(p.path) for p in partitions)
        local_pit_bytes = None
        if pit_path:
            with open(pit_path, "rb") as fh:
                local_pit_bytes = fh.read()
            if repartition:
                total_bytes += len(local_pit_bytes)
        device.send_total_bytes(total_bytes)

        if repartition:
            if local_pit_bytes is None:
                raise OdinError("Repartition requires a PIT file.")
            pit = PitData.unpack(local_pit_bytes)
            device.flash_pit(local_pit_bytes)
        else:
            device_pit_bytes = device.download_pit()
            pit = PitData.unpack(device_pit_bytes)

        log_cb(pit.describe())

        for part in partitions:
            if should_stop and should_stop():
                raise OdinError("Flashing cancelled by user.")
            key = part.partition.strip()
            entry = pit.find_entry(int(key)) if key.isdigit() else pit.find_entry(key)
            if entry is None:
                raise OdinError(f"Partition '{part.partition}' not found in PIT.")
            log_cb(f"Flashing '{entry.partition_name}' <- {os.path.basename(part.path)}")
            if entry.is_modem:
                device.send_file(part.path, DEST_MODEM, entry.device_type,
                                 progress_cb=progress_cb, should_stop=should_stop)
            else:
                device.send_file(part.path, DEST_PHONE, entry.device_type,
                                 file_identifier=entry.identifier,
                                 progress_cb=progress_cb, should_stop=should_stop)
            log_cb(f"'{entry.partition_name}' done.")

        device.end_session(reboot=reboot)
        log_cb("All operations completed successfully.")
    finally:
        device.close()
