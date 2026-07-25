# ESP32 Flash Tool

A simple cross-platform desktop GUI wrapper around [esptool](https://github.com/espressif/esptool) for flashing ESP32/ESP8266/etc. firmware.

## Features

- Detect available serial ports
- Select chip, baud rate, and firmware binary
- Flash firmware with optional full erase
- Erase flash
- Read/dump flash to a file
- Get chip info / test connection
- Live output log and progress bar

## Requirements

- Python 3.10+
- `esptool` and `pyserial` (see `requirements.txt`)
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
4. For flashing, browse for a `.bin` firmware file and set the flash address (default `0x1000`).
5. Click **Connect / Info** to verify communication, or **Flash** to program the device.
