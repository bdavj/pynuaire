# pinuaire — Nuaire Drimaster Protocol Reverse Engineering

> **STATUS**: New protocol fully decoded from clean Saleae captures (inverted signal).
> Use `controller3.py` for fan control. The old 63-byte protocol docs below are retained
> for reference but may reflect a different firmware version or board variant.

---

## New Protocol (current — inverted signal, clean captures)

Discovered when re-capturing with inverted polarity on the Saleae — all framing errors
disappeared. The new captures have a completely different wire format from the old 63-byte packets.

### Wire Format

```
1B 1B  <nibble-encoded body>  0D
```

Each decoded byte is transmitted as **two wire bytes**, each carrying one nibble in its
lower 4 bits. E.g. `0x81` → `0x38 0x31` (ASCII `'8' '1'`).

- **Header**: `0x1B 0x1B` (ESC ESC)
- **Body**: 60 wire bytes → 30 decoded bytes
- **Terminator**: `0x0D` (CR)

### Channels

| Saleae channel | Direction | Decoded B00 |
|----------------|-----------|-------------|
| Async Serial | Motor → Controller | `0x81` |
| Async Serial [1] | Controller → Motor | `0x82` |

### Decoded Packet Structure (30 bytes)

| Offset | Motor→Ctrl (0x81) | Ctrl→Motor (0x82) | Notes |
|--------|-------------------|-------------------|-------|
| B00 | `0x81` | `0x82` | Direction identifier |
| B01 | `0xAB` | `0xAB` | Constant |
| B02 | `0x0C` | `0x0C` | Constant |
| B03 | `0x00` | `0x00` | Constant |
| B04 | `0x01` | `0x01` | Constant |
| **B05** | **level** | **level** | **Fan speed level — literal number (see below)** |
| B06–B07 | `0x00` | `0x00` | |
| B08 | `0x9F`/`0xA1`/`0xA2` | `0x00` | Motor: speed/RPM indicator; doesn't change on level transition |
| B09 | `0x01` | `0x00` | |
| B0A–B0B | `0x00` | `0x00` | |
| B0C | `0x4B` | `0x00` | Motor only |
| B0D–B0F | `0x00` | `0x00` | |
| B10 | `0x08` | `0x00` | Motor only |
| B11 | `0x00` | `0x00` | |
| B12 | seq counter | `0x00` | Motor only: 3-bit up-counter (0x40–0x47), lower bits cycle 0–7 |
| B13 | `0x9C` | `0x00` | Motor only |
| B14–B15 | `0x00` | `0x00` | B15 occasionally `0x03` (unknown) |
| **B16** | **ctr16** | **ctr16** | **Shared down-counter (see below)** |
| B17 | `0x81` | `0x81` | Constant both directions |
| **B18** | `0x00` | `0x00` or `0x02` | **Level change flag**: controller sends `0x02` on first packet with a new B05 level, then `0x00`. Without this flag, motor ignores B05 changes. |
| B19–B1A | `0x00` | `0x00` | |
| B1B | `0x03` | `0x02` | Differs by direction |
| B1C | `0x00` | `0x00` | |
| **B1D** | **checksum** | **checksum** | `(-sum(B00..B1C)) & 0xFF` — sum of all 30 bytes = 0 |

### Fan Speed Level (B05)

B05 carries the fan speed as a **literal integer** — fully confirmed across all levels:

| Level | B05 | B08 | Confirmed |
|-------|-----|-----|-----------|
| L1 | `0x01` | `0x9F`? | ⚠ inferred — "L1" file actually captured at L3 (see below) |
| L2 | `0x02` | `0xA2` | ✓ static capture |
| L3 | `0x03` | `0xA2` | ✓ static + button press |
| L4 | `0x04` | `0xA2` | ✓ static + button press |
| L5 | `0x05` | `0xA1` | ✓ static capture |
| L6 | `0x06` | `0xA1` | ✓ static capture |

**B08** appears to encode a motor speed band: `0xA2` for L2–L4, `0xA1` for L5–L6 (reported by motor only; does not change immediately on level transition).

**"L1" file anomaly**: the file named "L1" shows B05=`0x03` (L3), not `0x01`. It was likely captured while the fan was at L3. B08=`0x9F` (lower than all other captures) suggests the motor may have been mid-acceleration. A genuine L1 capture is still needed.

Both controller and motor carry the same B05 value. The motor echoes the commanded level
within one packet cycle (~100ms).

**Critical: B18 level change flag** — when changing B05 to a new level, the controller must also
set B18=`0x02` on that first packet. The motor ignores B05 changes without this flag. B18 returns
to `0x00` on subsequent packets. Confirmed from Saleae transition captures and live testing.

**Level transition timing** (confirmed from button press captures and live control):
- Controller changes B05 + sets B18=0x02 in a single packet
- Motor echoes the new level in its very next reply (~100ms later)

### Shared Counter (B16)

B16 is a **shared down-counter** — each side simply replies with `received_B16 - 1`:

```
Ctrl sends  B16=N    →  Motor replies  B16=N-1
Motor sends B16=N-1  →  Ctrl replies   B16=N-2
```

Both sides always differ by exactly 1. Either side can lead (no fixed initiator).
To sync: on receiving a packet with B16=N, send reply with B16=N-1.

### Checksum (B1D)

`B1D = (-sum(B00..B1C)) & 0xFF`

Equivalently: `sum(all 30 bytes) == 0x00`. Confirmed on every packet across all captures.

### Commanding Fan Speed

To change fan speed:
1. Set B05 to the desired level byte (0x01–0x06)
2. Set B18 to `0x02` on the **first** packet with the new level (level change flag)
3. Maintain the B16 counter (reply with received_B16 - 1)
4. Recalculate B1D checksum
5. Keep all other bytes at their fixed values; B18 returns to `0x00` after the first change packet

The motor responds to the new level immediately (next reply packet). The B18 flag is required — without it, the motor ignores B05 changes. Confirmed working via `controller3.py` over inverted UART bridge.

---

## Project Goal

**pinuaire** (Pi + Nuaire) aims to reverse engineer the serial communication protocol used internally by a **Nuaire Drimaster** whole-house mechanical extract ventilation (dMEV) fan unit, so that fan speed can be monitored and controlled programmatically — for example, via Home Assistant or similar home automation.

The Drimaster normally exposes only a simple voltage/switch interface for speed selection. The internal UART bus carries richer information (actual speed level, state, possibly sensor readings) and presumably accepts commands. Decoding it would allow:

- Monitoring actual running speed level
- Issuing speed-change commands without physical wiring
- Integrating with humidity/CO2 sensors for automated boost control
- Logging operational data

---

## Hardware Setup

- **Logic analyser**: SIGROK-compatible device capturing at ~5 MHz
- **Decoder on-device**: Raspberry Pi Pico (MicroPython) running `main.py`, listening on two UART RX pins simultaneously
- **Bus**: Two UART lines (labelled A and B) tapped from inside the Drimaster unit
- **Baud rate**: 57,600 baud, 8N1 (no parity, 1 stop bit)

---

## Captures

### SIGROK Captures (original)

Five captures were taken with the fan set to a fixed speed level, exported from SIGROK as raw bit-level UART output:

| File | Fan level | RX pkts decoded | TX pkts decoded | Notes |
|------|-----------|-----------------|-----------------|-------|
| `UART - L1.txt` | L1 (slowest) | 5 (3 valid) | 5 (5 valid) | Good quality |
| `UART L2.txt` | L2 | 7 (6 valid) | 7 (7 valid) | TX heavily corrupted — likely SIGROK config issue |
| `UART L3.txt` | L3 | 16 (10 valid) | 16 (16 valid) | Longest capture, best for counter analysis |
| `UART L4.txt` | L4 | 4 (2 valid) | 4 (4 valid) | Good quality |
| `UART L6.txt` | L6 (fastest) | 2 (0 valid) | 3 (3 valid) | No valid RX packets |

### Saleae Captures

Second round captured with Saleae logic analyser. Two UART channels: "Async Serial" = RX (motor→controller), "Async Serial [1]" = TX (controller→motor).

| File | Fan level | RX valid/total | TX valid/total | Notes |
|------|-----------|----------------|----------------|-------|
| `L1` | L1 | 0/0 | 0/0 | Empty capture |
| `L2` | L2 | 1/4 | 5/5 | Good TX data |
| `L3` | L3 | 1/3 | 2/2 | Valid RX has bit-shift corruption |
| `L4` | L4 | 0/0 | 0/0 | Empty capture |
| `L5` | L5 | 0/0 | 0/0 | Empty capture |
| `L6` | L6 | 2/3 | 3/3 | Valid RX still has corruption in data fields |
| `L1-up-button` | L1→L2 transition | 4/10 | 10/10 | **Button press captured!** Best capture overall |
| `L2-down-button` | L2 (down press) | 1/7 | 7/7 | Down-press effect not visible in data |

**Key finding**: RX corruption persists with Saleae — this is a **hardware signal integrity issue** on the motor→controller line, not a decoder/settings problem. TX is consistently clean.

"Valid" = packet ends with the `E5 00` tail marker. Invalid packets have corrupted tails due to bit-shifting errors (see below).

---

## Packet Types

There are **two packet types**, distinguished by byte 0x02:

| Type | Byte 0x02 | Direction | Role |
|------|-----------|-----------|------|
| **0x6C** | TX | Controller → Motor unit | Command/polling — mostly padding (0x9F), carries fan level + counters |
| **0xAC** | RX | Motor unit → Controller | Status/response — rich data, carries fan level + operational parameters |

Both types share the same 63-byte length, `72 72` magic header, and `E5 00` tail.

---

## Bit-Shifting Corruption

A systematic issue affects the SIGROK captures: occasional 1–3 bit left-shift events corrupt groups of bytes. Symptoms:

- `0x9F` (the idle/padding value) becomes `0x3E` (<<1), `0x7C` (<<2), or `0xF8` (<<3)
- Other values shift similarly: `0x8F` → `0x1E` (<<1) or `0x3C` (<<2)
- The corruption appears at varying positions in each packet (a "wandering" glitch)
- Packets with intact `E5 00` tails are uncorrupted; others should be treated with caution

**Root cause**: The RX line (motor→controller) has a hardware signal integrity issue. The same corruption pattern appears with both SIGROK and Saleae analysers, ruling out decoder configuration. The TX line is consistently clean. This may be caused by the motor's UART transmitter having marginal drive strength, impedance mismatch, or electrical noise from the motor itself.

---

## TX Packet Structure (0x6C — Controller → Motor)

TX packets are very simple. When bit-shift corruption is accounted for, the true structure is:

```
Offset  Value       Notes
------  -----       -----
00-01   72 72       Magic header
02      6C          Packet type (TX)
03      B6          Fixed
04      89          Fixed
05      9F          Fixed (padding)
06      87          Fixed
07-09   9F 9F 9F    Padding
0A      9D          Fixed
0B      9F          Fixed (padding — 3E/7C values are corruption)
0C      LEVEL       Fan speed level (see encoding table)
0D-2C   9F...       32 bytes of padding (all 9F)
2D      counter_A   Slow counter, increments by +2 every ~8 packets
2E      counter_B   Fast counter, 8-value cycle stepping by +4
2F      8F          Fixed
30      9D          Fixed
31-37   9F...       Padding
38      9B          Fixed
39-3A   9F 9F       Padding
3B      counter_C   Slow counter, changes over time
3C      counter_D   Fast counter, 8-value cycle stepping by -4
3D      E5          Tail byte 1
3E      00          Tail byte 2
```

### TX Counter Details

**B2E (counter_B)** cycles through 8 values, incrementing by +4 each packet:
`81 → 85 → 89 → 8D → 91 → 95 → 99 → 9D → 81 → ...`

Confirmed from Saleae L1-up-button TX sequence: 99, 9D, 81, 85, 89, 8D, 91, 95, 99.
Base value is **0x81** (not 0x83 as originally thought). This is a 3-bit counter in bits[4:2] with fixed bits `1_00_xxx_01`.

**B3C (counter_D)** cycles through the same 8 values, decrementing by -4 each packet:
`9F → 9B → 97 → 93 → 8F → 8B → 87 → 83 → 9F → ...`

Confirmed from Saleae: L2 TX shows 97, 93, 8F, 8B, 87; L1-up-button shows 9B, 97, 93, 8F, 8B, 87, 83.

**B2D (counter_A)** increments by +2 every ~8 packets (i.e., once per full B2E cycle). Effectively a "high byte" of a 6-bit counter.

**B3B (counter_C)** — previously thought to be a pure counter. Saleae data shows level-dependent values: L2=0x85, L3=0x83/81, L6=0x93. In the L1-up-button transition it changed from 0x83 to 0x81. May encode operational state rather than being a simple sequence counter. Needs further investigation.

These counters likely serve as packet sequence numbers for detecting lost packets.

---

## RX Packet Structure (0xAC — Motor → Controller)

RX packets carry operational data from the motor unit. The full byte map from valid packets across L1–L4:

```
Offset  L1    L2    L3    L4    Status   Notes
------  ----  ----  ----  ----  ------   -----
00      72    72    72    72    CONST    Magic
01      72    72    72    72    CONST    Magic
02      AC    AC    AC    AC    CONST    Packet type (RX)
03      B6    B6    B6    B6    CONST
04      89    89    89    89    CONST
05      E0    C0    C0    80    VARIES   Speed-related (see below)
06      76    76    76    76    CONST
07-09   9F    9F    9F    9F    CONST
0A      9D    9D    9D    9D    CONST
0B      9F    9F    9F    9F    CONST
0C      9D    9B    99    97    VARIES   Fan level byte
0D-0F   9F    9F    9F    9F    CONST
10      9F    9F    C0    C0    VARIES   Speed-related
11      85    85    56    56    VARIES   Mode flag (85=low, 56=normal)
12      83    83    83    83    CONST
13      9F    9F    9F    9F    CONST
14      9D    9D    9D    9D    CONST
15      9F    9F    9F    9F    CONST
16-19   varies                  VARIES   (see per-level data below)
1A      D6/9D                   VARIES
1B      varies                  VARIES   Speed-related echo of B05
1C-20   F6/9F mix               VARIES   Operational data
21      varies                  VARIES
22      F6    F6    F6    F6    CONST
23-24   9F    9F    9F    9F    CONST
25      97    97    97    97    CONST
26-28   varies                  VARIES   Operational data
29-2A   9F    9F    9F    9F    CONST
2B-2C   varies                  VARIES
2D      varies                  VARIES   Level-related
2E      varies                  VARIES   Level-related
2F      8F    8F    8F    8F    CONST
30      9D    9D    9D    9D    CONST    (L2 anomalous — capture issue)
31-36   varies                  VARIES   Operational data
37      80    --    C0    E0    VARIES   Speed-related (inverted from B05!)
38      96    96    96    96    CONST
39      F6    F6    F6    F6    CONST
3A      F6    --    F6    F6    CONST
3B      36    36    76    B6    VARIES   Counter/status
3C      varies                  VARIES   Counter/status
3D      E5    E5    E5    E5    CONST    Tail
3E      00    00    00    00    CONST    Tail
```

### RX Alternating Sub-Formats (Saleae Discovery)

Saleae captures reveal that RX packets alternate between **two distinct sub-formats** on every other packet. This is NOT corruption — the byte values at key positions are too consistent to be random bit-shift errors.

| Field | Format A | Format B | Distinguisher |
|-------|----------|----------|---------------|
| B06 | 0x76 | 0x87 | Primary ID |
| B11 | 0x36 | 0x83 | Secondary ID |
| B0B-0F | `9D 9F [level] 9F 9F 9F` | Contains F6 values | Data vs padding |
| B16-20 | Mostly 0x9F padding | `[E0/C0] F6 F6 F6 56 F6 F6 F6 F6 F6` | Rich data in B |
| B1A | 0x95 | 0x56 | Consistent per format |
| B31-36 | All 0x9F | Various | More data in B |

Format A appears to carry the "simple" status (level, speed indicators). Format B carries denser operational data (the F6 regions may encode sensor readings, RPM, or counters). The alternation is A-B-A-B with each format appearing every ~200ms.

**Note**: Many RX packets flagged as "invalid tail" may still have correct data — the bit-shift corruption tends to affect the tail more than the body. Format classification by B06/B11 is more reliable than tail validation.

### Key RX Fields

**B05 — Speed indicator**: Encodes fan speed in upper bits. Values by level:

| Level | B05 | B10 | B37 | B11 |
|-------|-----|-----|-----|-----|
| L1 | E0 | 9F | 80 | 85 |
| L2 | C0 | 9F | — | 85 |
| L3 | C0 | C0 | C0 | 56 |
| L4 | 80 | C0 | E0 | 56 |

**Note**: B11 values above are from SIGROK (0x85/0x56). Saleae Format A consistently shows B11=0x36 and Format B shows B11=0x83. The SIGROK values may have been from a different sub-format mix or corrupted. The Format A/B distinction supersedes the simple L2/L3 "mode boundary" theory.

B05 and B37 appear to be **inverted**: when B05 goes down, B37 goes up.

**B2D–B2E — Level echo**: These bytes appear to contain level-related values that differ from B0C. In RX packets, B2D values at different levels include the level encoding values for adjacent speeds (e.g., B2D=0x97 at L4, which is L4's own level code).

**B1B — Speed echo**: Mirrors B05 in some packets (e.g., L4: B1B=0x80 = same as B05).

---

## Fan Speed Level Encoding (byte 0x0C)

Used in **both** TX and RX packets. Decreases by 2 per speed step.

Formula: `level = (0x9F - byte) / 2`

| Fan level | Byte value | Binary |
|-----------|-----------|--------|
| L1 | 9D | 1001 1101 |
| L2 | 9B | 1001 1011 |
| L3 | 99 | 1001 1001 |
| L4 | 97 | 1001 0111 |
| L5 | 95 | 1001 0101 |
| L6 | 93 | 1001 0011 |

Encodes a 3-bit value in bits[4:2], with fixed bits `1_00_xxx_01` (base 0x81, though 0x9F = all-ones state).

---

## Example Packets

### Clean TX packet (L4, pkt 0)
```
72 72 6C B6 89 9F 87 9F 9F 9F 9D 9F 97 9F 9F 9F
                                       ^^ level=L4
9F 9F 9F 9F 9F 9F 9F 9F 9F 9F 9F 9F 9F 9F 9F 9F
9F 9F 9F 9F 9F 9F 9F 9F 9F 9F 9F 9F 9F 97 8B 8F
                                          ^^ ^^ counters
9D 9F 9F 9F 9F 9F 9F 9F 9B 9F 9F 81 95 E5 00
                                    ^^ ^^ counters
```

### Clean RX packet (L4, pkt 0)
```
72 72 AC B6 89 80 76 9F 9F 9F 9D 9F 97 9F 9F 9F
               ^^ speed               ^^ level=L4
C0 56 83 9F 9D 9F 9F 9F 9F 9F 9D 80 F6 F6 F6 F6
^^ ^^ mode        operational data...
F6 F6 F6 9F 9F 97 40 D6 87 9F 9F 9F 9F 97 89 8F
                                       operational
9D 9F 9F 9F 9F 9F 9F E0 96 F6 F6 B6 16 E5 00
                     ^^                ^^ ^^
                  speed(inv)         counters/status
```

---

## Decoder (`main.py`)

The MicroPython script runs on a Pico and monitors both UART lines simultaneously. It:

- Scans the byte stream for the `72 72` header to synchronise to packet boundaries
- Validates each 63-byte candidate packet against the known header/tail structure
- Prints level, packet type, and key byte values for every valid packet
- Tracks diffs between consecutive packets of the same type to highlight changing fields

Key tunable constants:
- `STRICT_TAIL` — require `E5 00` tail to accept a packet (currently off)
- `VALID_B4_VALUES` — set of accepted values for byte 4 (`{0x89, 0x96}`)
- `PRINT_FULL` / `PRINT_UNKNOWN` — verbosity controls

---

## Button Press / Level Transition (Saleae)

The `L1-up-button` capture shows a complete L1→L2 transition:

```
t=0.932  RX  B0C=0x9D (L1)   ← Motor still reporting L1
t=0.963  TX  B0C=0x9B (L2)   ← Controller already commanding L2 (button was pressed)
t=1.033  RX  B0C=0x9B (L2)   ← Motor acknowledges L2 within ~100ms
t=1.133  TX  B0C=0x9B (L2)   ← Continues at L2
...all subsequent packets: L2
```

**Key observations**:
- The controller changes B0C in TX immediately on button press
- The motor responds within one packet cycle (~100ms)
- No special "command" packet is needed — the level change is just a different B0C value in the regular polling TX packet
- Counters (B2E, B3C) continue their normal cycling through the transition — no reset or special sequence
- B3B changed from 0x83 to 0x81 during the transition (may be related to operational state adjustment)

The `L2-down-button` capture shows all TX and RX packets at L2 — the down button press either occurred outside the capture window or the controller hadn't yet updated B0C.

---

## What Is Still Unknown

1. **RX Format B data (bytes 0x16–0x36)** — The F6-heavy "Format B" RX packets contain dense operational data. These could encode RPM feedback, temperature, humidity, error codes, or running hours. The alternating A/B format means each sub-type updates every ~200ms. Need to decode the F6 data regions.

2. **Checksum/CRC** — `E5 00` is likely a fixed tail marker rather than a checksum, since all uncorrupted packets across both SIGROK and Saleae captures share the same value regardless of packet contents. But this hasn't been definitively proven.

3. **Command injection** — the TX packet structure is now well-understood, and the Saleae button-press capture confirms that changing B0C is sufficient to command a level change. To change fan speed:
   - Set B0C to the desired level byte
   - Maintain valid counter values (B2E incrementing by +4, B3C decrementing by -4)
   - Keep the `E5 00` tail
   - Leave all other bytes as their fixed/9F values

   The motor accepted a level change with no special handshake — just the new B0C value in the regular polling packet. Counter sync requirements are unknown but the motor responded immediately.

4. **B3B role** — originally documented as "counter_C" (slow counter), but Saleae shows it varies with fan level (L2=0x85, L3=0x83, L6=0x93) and changed during the L1→L2 transition. May encode operational mode, target speed, or be a counter with level-dependent initial value.

5. **RX signal integrity** — confirmed as a hardware issue (same corruption on SIGROK and Saleae). May need a line driver, pull-up/pull-down resistor adjustment, or shorter probe leads to get clean RX data. The motor's UART TX may have marginal drive strength.

6. **RX B05/B10/B37 encoding** — these speed-related bytes don't follow a simple linear pattern. B05 and B37 are inversely correlated. The previous B11 "mode flag" (0x85 vs 0x56) was likely confusion between Format A (B11=0x36) and Format B (B11=0x83) sub-packets.

---

## Recommended Next Steps

1. **Command injection test** — forge a TX packet with a different level byte and transmit it on the TX line to see if the motor responds. The Saleae button-press capture proves the motor accepts level changes via B0C with no special handshake. Start with the simplest possible change (e.g., L3→L4) keeping counters incrementing normally.

2. **Fix RX signal quality** — try hardware improvements on the motor→controller line:
   - Add a pull-up resistor (4.7kΩ to 3.3V)
   - Use shorter/shielded probe leads
   - Try a line driver/buffer IC
   - Check if the motor TX pin has an open-drain output that needs external pull-up

3. **Decode RX Format B** — once RX signal is cleaner, focus on the F6-heavy Format B packets. These carry the densest operational data and likely include RPM, sensor readings, or error codes.

4. **Longer captures at each level** — the L1-up-button capture (10 packets per channel) was the most valuable so far. Longer captures would help decode the Format B data fields.

5. **Environmental variation** — capture while changing temperature/humidity to identify sensor data in the RX packet.

6. **Capture at L5** — fill in the gap in the level data (L5=0x95 predicted but unconfirmed).
