# PyNuaire

Reverse-engineered UART protocol for the **Nuaire Drimaster ECO-HC** whole-house mechanical extract ventilation (dMEV) fan, enabling programmatic speed control via serial.

This repo is not associated or endorsed by Nuaire in any way.

Developed and tested on the **Drimaster ECO-HC**. Other Nuaire units with an internal UART bus may use a similar or identical protocol — PRs and captures from other models are very welcome.

The Drimaster's internal bus uses an inverted UART link (57600 8N1) between the motor unit and controller board. This project decodes that protocol and provides a Python controller that can set fan speed levels 1-6.

## Quick Start

```bash
python controller.py /dev/ttyUSB0 --level 3
```

Type `1`-`6` to change speed, `q` to quit. The controller syncs with the motor's current level before stepping toward the target.

## Hardware

You need a serial adapter capable of **inverted UART** at 57600 baud connected to the motor-controller bus inside the fan unit. An ESP32 works well as an inverting bridge (the built-in UART peripheral supports signal inversion).

## Protocol Summary

Wire format: `1B 1B <nibble-encoded 30 bytes> 0D`

Each decoded byte is split into two wire bytes (one nibble each). The motor sends packets every ~100ms; the controller replies after each one.

Key bytes in the 30-byte decoded packet:

| Byte | Purpose |
|------|---------|
| B00 | Direction: `0x81` = motor, `0x82` = controller |
| B05 | Fan level: `0x01`-`0x06` (L1-L6) |
| B16 | Shared counter: reply with `received - 1` |
| B18 | Level change flag: `0x02` on first packet with new level |
| B1D | Checksum: `(-sum(B00..B1C)) & 0xFF` |

To change speed: set B05 to the new level, set B18=`0x02` on that first packet, maintain the B16 counter, and recalculate the checksum. The motor acknowledges within one packet cycle (~100ms).

See [PROTOCOL.md](PROTOCOL.md) for full protocol documentation.

## Files

- **`controller.py`** - Fan speed controller (the main thing)
- **`PROTOCOL.md`** - Complete protocol reverse engineering notes
- **`main.py`** - Original Raspberry Pi Pico UART decoder (MicroPython)
- **`captures/`** - Saleae logic analyser exports at each speed level and during transitions

## Why "PyNuaire"?

Python + Nuaire. Any device with an inverted UART will work as the serial adapter.

## Contributing

If you have a different Nuaire unit and can capture its UART traffic (e.g. with a Saleae or similar logic analyser), please open a PR or issue with your captures. Even raw `.sal` files or CSV exports are useful.

## License

MIT
