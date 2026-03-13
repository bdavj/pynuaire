from machine import UART, Pin
import utime
import sys

BAUD = 57600

UART_A_ID = 0
UART_A_RX = 1
UART_A_INV = 0

UART_B_ID = 1
UART_B_RX = 9
UART_B_INV = 0

# NOTE: In MicroPython, invert is a bitmask (INV_TX=1, INV_RX=2 on most ports).
# We explicitly set RX inversion mode per line to avoid accidental TX-only inversion.
uart_a = UART(
    UART_A_ID,
    baudrate=BAUD,
    rx=Pin(UART_A_RX, Pin.IN, Pin.PULL_UP),
    invert=UART_A_INV,
    bits=8,
    parity=None,
    stop=1,
)
uart_b = UART(
    UART_B_ID,
    baudrate=BAUD,
    rx=Pin(UART_B_RX, Pin.IN, Pin.PULL_UP),
    invert=UART_B_INV,
    bits=8,
    parity=None,
    stop=1,
)

PACKET_LEN = 63
MAX_BUF = 256
STRICT_TAIL = False
DEFAULT_RUN_SECONDS = 5  # Set to None for endless run.
VALID_B4_VALUES = (0x89, 0x96)
PRINT_FULL = False
PRINT_UNKNOWN = False

streams = {
    "A": {"uart": uart_a, "buf": bytearray()},
    "B": {"uart": uart_b, "buf": bytearray()},
}
last_by_stream_type = {}

def level_from_byte(v):
    return {
        0x9D: "L1",
        0x9B: "L2",
        0x99: "L3",
        0x97: "L4",
        0x95: "L5",
        0x93: "L6",
    }.get(v, "?")

def hexs(buf):
    return " ".join("{:02X}".format(b) for b in buf)

def is_known_packet(pkt):
    basic_ok = (
        len(pkt) == PACKET_LEN and
        pkt[0] == 0x72 and
        pkt[1] == 0x72 and
        pkt[2] in (0x6C, 0xAC) and
        pkt[3] == 0xB6 and
        pkt[4] in VALID_B4_VALUES
    )
    if not basic_ok:
        return False
    if STRICT_TAIL:
        return pkt[61] == 0xE5 and pkt[62] == 0x00
    return True

def print_packet(name, pkt):
    tail_ok = (len(pkt) >= 63 and pkt[61] == 0xE5 and pkt[62] == 0x00)
    key = (name, pkt[2])
    prev = last_by_stream_type.get(key)
    diff_preview = "first"
    diff_count = 0
    if prev is not None:
        diffs = []
        for i in range(PACKET_LEN):
            if prev[i] != pkt[i]:
                diffs.append(i)
        diff_count = len(diffs)
        if diff_count == 0:
            diff_preview = "none"
        else:
            parts = []
            limit = 8
            for i in diffs[:limit]:
                parts.append("{:02d}:{:02X}->{:02X}".format(i, prev[i], pkt[i]))
            if diff_count > limit:
                parts.append("+{} more".format(diff_count - limit))
            diff_preview = " ".join(parts)
    last_by_stream_type[key] = pkt
    print(
        "{} KNOWN type={} level={}({:02X}) b05={:02X} b11={:02X} b12={:02X} "
        "b45={:02X} b46={:02X} tail={:02X} {:02X} {} diff#={} {}".format(
            name,
            "6C" if pkt[2] == 0x6C else "AC",
            level_from_byte(pkt[12]), pkt[12],
            pkt[5], pkt[11], pkt[12], pkt[45], pkt[46],
            pkt[61], pkt[62],
            "OK" if tail_ok else "BAD",
            diff_count, diff_preview
        )
    )
    if PRINT_FULL:
        print("{} FULL {}".format(name, hexs(pkt)))

def print_unknown(name, data):
    if PRINT_UNKNOWN and data:
        print("{} UNKNOWN {}".format(name, hexs(data)))

def set_buf(s, new_bytes):
    s["buf"] = bytearray(new_bytes)

def process_stream(name, s):
    uart = s["uart"]
    buf = s["buf"]

    if uart.any():
        data = uart.read()
        if data:
            buf.extend(data)

    if len(buf) > MAX_BUF:
        set_buf(s, buf[-128:])
        buf = s["buf"]

    while True:
        start = -1
        for i in range(len(buf) - 1):
            if buf[i] == 0x72 and buf[i + 1] == 0x72:
                start = i
                break

        if start < 0:
            if len(buf) > 1:
                set_buf(s, buf[-1:])
            return

        if start > 0:
            junk = bytes(buf[:start])
            print_unknown(name, junk)
            set_buf(s, buf[start:])
            buf = s["buf"]

        if len(buf) < PACKET_LEN:
            return

        pkt = bytes(buf[:PACKET_LEN])

        if is_known_packet(pkt):
            print_packet(name, pkt)
            set_buf(s, buf[PACKET_LEN:])
            buf = s["buf"]
        else:
            print_unknown(name, pkt[:8])
            set_buf(s, buf[1:])
            buf = s["buf"]

print("Header-based Drimaster decoder starting...")
print("A=UART0 RX GP{}, B=UART1 RX GP{}, {} baud".format(UART_A_RX, UART_B_RX, BAUD))
print("UART invert: A={}, B={}".format(UART_A_INV, UART_B_INV))
if STRICT_TAIL:
    print("Strict tail check enabled (E5 00)")
else:
    print("Strict tail check disabled")

run_ms = None
if DEFAULT_RUN_SECONDS is not None:
    run_ms = int(DEFAULT_RUN_SECONDS * 1000)

if len(sys.argv) >= 2:
    try:
        run_ms = int(float(sys.argv[1]) * 1000)
    except:
        print("Ignoring invalid run duration arg: {}".format(sys.argv[1]))

if run_ms is None:
    print("Run duration: endless")
else:
    print("Run duration: {} ms".format(run_ms))

start_ms = utime.ticks_ms()

while True:
    process_stream("A", streams["A"])
    process_stream("B", streams["B"])
    if run_ms is not None and utime.ticks_diff(utime.ticks_ms(), start_ms) >= run_ms:
        print("Run duration reached; stopping.")
        break
    utime.sleep_ms(1)
