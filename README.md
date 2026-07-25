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
