# Flash Tools

Cross-platform desktop flashing GUIs:

- **ESP32 Flash Tool** (`src/esp32_flashtool.py`) — flashes ESP32/ESP8266 family devices.
- **Samsung Flash Tool** (`src/samsung_flashtool.py`) — flashes Samsung devices in Download/Odin mode.

---

# ESP32 Flash Tool

A cross-platform desktop GUI wrapper around [esptool](https://github.com/espressif/esptool) for flashing ESP32/ESP8266 family devices.

## Features

- Detect available serial ports
- Select chip, baud rate, firmware binary, and flash address
- **Multiple binary slots** — queue several `(address, file)` pairs for a single `write_flash` command
- **Drag-and-drop** firmware files onto the firmware field (requires `tkinterdnd2`)
- **OTA / network flash** — download a `.bin` from a URL and add it to the flash queue
- **Dark theme** — toggle between dark and light UI modes
- **Profiles** — save and load settings (port, baud, chip, binaries, theme) as JSON
- Erase, read/dump flash, and chip-info operations
- Save the output log to a file
- Live output log and progress bar

## Requirements

- Python 3.10+
- `esptool`, `pyserial`, and `tkinterdnd2` (see `requirements.txt`)
- On Linux, the system `python3-tk` package for tkinter

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
python3 src/esp32_flashtool.py
```

## Usage

1. Connect your ESP32 to a USB/UART bridge.
2. Select the serial port from the dropdown (or click Refresh).
3. Choose the target chip and baud rate.
4. Build the flash queue:
   - Browse for a `.bin` firmware file, set its flash address, and click **Add**.
   - Drag-and-drop a `.bin` file onto the firmware field.
   - Enter a firmware URL and click **Fetch & Add**.
5. Click **Connect / Info** to verify communication, **Flash All** to program, or **Erase** to wipe the device.

---

# Samsung Flash Tool

A general-purpose Samsung firmware flasher — an open alternative to Odin. The
Odin/LOKE download-mode protocol is implemented natively in Python
(`src/odin_protocol.py`) on top of `pyusb`/libusb, so no external flashing
binary (Odin, Heimdall) is required.

## Features

- Detect Samsung devices in Download/Odin mode over USB
- Download and pretty-print the device's **PIT** (partition table)
- Flash any number of partition images, mapped to partitions by **name or numeric id**
- **Repartition** by flashing a local `.pit` file
- **T-Flash** mode (flash to an SD card instead of internal storage)
- **Add firmware TAR** — enumerates a `.tar`/`.tar.md5` and queues its images with guessed partition names
- Modem (CP) partitions are automatically flashed via the modem transfer path
- Auto-reboot after flashing, or send a reboot on its own
- Drag-and-drop, dark theme, JSON profiles, live log with progress, save log

## Requirements

- Python 3.10+
- `pyusb` (see `requirements.txt`) and a libusb backend:
  - Debian/Ubuntu: `sudo apt install libusb-1.0-0`
  - macOS: `brew install libusb`
  - Windows: install a WinUSB driver for the device (e.g. via [Zadig](https://zadig.akeo.ie/))
- On Linux, raw USB access. Either run with `sudo`, or install a udev rule:

  ```bash
  echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="04e8", MODE="0666"' \
    | sudo tee /etc/udev/rules.d/51-samsung-download.rules
  sudo udevadm control --reload-rules
  ```

## Run

```bash
python3 src/samsung_flashtool.py
```

## Usage

1. Boot the phone into Download mode (typically **Volume Down + Bixby/Home + Power**,
   then Volume Up to confirm) and connect it over USB.
2. Click **Refresh**, then pick the device from the dropdown.
3. Click **Detect** to handshake and dump the device's PIT — this lists the exact
   partition names you can flash to.
4. Build the flash queue:
   - Choose a partition name (or type a numeric PIT id), browse for the image, click **Add**.
   - Drag-and-drop an image onto the image field (the partition name is guessed).
   - Or use **File → Add firmware TAR...** to queue every image in an Odin `.tar.md5`.
5. Optionally set a `.pit` file and tick **Repartition**, or tick **T-Flash**.
6. Click **Flash**. Progress is reported per file; **Stop** halts after the current transfer.

### Notes and limitations

- LZ4-compressed members inside modern firmware TARs (`*.img.lz4`) are skipped when
  importing an archive — decompress them first (`lz4 -d boot.img.lz4`).
- Interrupting a flash mid-transfer can leave the device unbootable. Only flash
  firmware built for your exact model, and keep the device connected until it reboots.
- The protocol implementation follows the publicly documented LOKE bootloader
  behaviour; it does not bypass bootloader locks, carrier locks, or FRP.
