#!/usr/bin/env python3
"""
pinuaire controller3 — Nuaire Drimaster fan controller, new protocol.

New protocol (observed from inverted-signal Saleae captures, no framing errors):
  Wire format:  1B 1B <nibble-encoded 30 bytes> 0D
  Each decoded byte is split into two wire bytes; lower 4 bits of each wire byte
  is one nibble.  E.g. 0x81 → 0x38 0x31 ("8" "1" in ASCII).

Decoded packet layout (30 bytes):
  B00  type    0x81 = motor→controller (RX)   0x82 = controller→motor (TX)
  B01  0xAB    constant
  B02  0x0C    constant
  B03  0x00    constant
  B04  0x01    constant
  B05  level   fan speed level: 0x01=L1, 0x02=L2, 0x03=L3, 0x04=L4, 0x05=L5, 0x06=L6
               Linear mapping confirmed from transition capture (L1 up L2 down L1).
               Earlier static "L1" captures were mislabelled — fan was actually at L3.
  B06  0x00
  B07  0x00
  B08  RPM band  Motor only: 0x65=L1, 0xA2=L2-L4, 0xA1=L5-L6. Controller always 0x00.
  B09  0x01 (motor) / 0x00 (ctrl)
  B0A–B11      Mostly 0x00; motor has 0x4B at B0C, 0x08 at B10
  B12  seq     Motor only: 3-bit up-counter (0x40–0x47, lower bits cycle 0–7)
  B13  0x9C (motor) / 0x00 (ctrl)
  B14  0x00
  B15  0x00    occasionally 0x03 (unknown)
  B16  ctr16   Shared counter — each side replies with received_B16 - 1.
               Both sides always differ by exactly 1. No independent tracking needed.
  B17  0x81    constant both directions
  B18  chgflag Controller only: 0x02 on first packet with a new level (level change request).
               Without this flag, motor ignores B05 changes. Returns to 0x00 after first packet.
  B19–B1A      0x00
  B1B  0x03 (motor) / 0x02 (ctrl)
  B1C  0x00
  B1D  chk     Checksum: (-sum(B00..B1C)) & 0xFF  (sum of all 30 bytes == 0x00)

Controller→motor TX cycle:
  - Send a TX packet immediately after receiving each motor RX packet (~100ms intervals)
  - Increment B12 seq (motor ignores it but mirror it anyway)
  - Decrement B16 by 2 each packet
  - Motor responds with B16 = our B16 + 1

Usage:
    python controller3.py /dev/tty.usbserial-XXXX
    python controller3.py /dev/tty.usbserial-XXXX --level 3

Type 1-6 to change fan level, q to quit.
"""

import argparse
import select
import sys
import time
from typing import Optional

import serial

BAUD = 57600

# B05 level bytes — linear, confirmed from transition capture (L1→L2→L1).
# Static "L1" captures were mislabelled (fan was at L3). L1=0x01 confirmed.
LEVEL_BYTES = {1: 0x01, 2: 0x02, 3: 0x03, 4: 0x04, 5: 0x05, 6: 0x06}
BYTE_TO_LEVEL = {v: k for k, v in LEVEL_BYTES.items()}

RESPONSE_DELAY = 0.017   # 17ms — matches real controller timing
KEEPALIVE_INTERVAL = 0.5
READ_CHUNK = 256
RX_BUF_MAX = 2048


def checksum(payload: bytearray) -> int:
    """Return checksum byte such that sum(payload + [cs]) & 0xFF == 0."""
    return (-sum(payload)) & 0xFF


def encode_packet(decoded: bytearray) -> bytes:
    """Nibble-encode a 30-byte decoded packet into wire format with 1B 1B header and 0D trailer."""
    wire = bytearray([0x1B, 0x1B])
    for b in decoded:
        wire.append(0x30 | ((b >> 4) & 0x0F))
        wire.append(0x30 | (b & 0x0F))
    wire.append(0x0D)
    return bytes(wire)


def decode_packet(raw: bytes) -> Optional[bytearray]:
    """
    Decode a wire packet (after stripping 1B 1B header and 0D terminator).
    Returns 30-byte bytearray or None if length is wrong.
    """
    if len(raw) % 2 != 0:
        raw = raw[:-1]
    nibbles = [b & 0x0F for b in raw]
    if len(nibbles) < 60:
        return None
    decoded = bytearray((nibbles[i] << 4) | nibbles[i + 1] for i in range(0, 60, 2))
    return decoded


def verify_checksum(pkt: bytearray) -> bool:
    return len(pkt) == 30 and sum(pkt) & 0xFF == 0


class DrimasterController:
    def __init__(self, port: str, level: int = 3):
        self.ser = serial.Serial(
            port=port,
            baudrate=BAUD,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.001,
        )
        self.target_level = level   # what the user wants
        self.current_level = None   # what we're actually sending (synced from motor first)
        self.synced = False         # have we echoed the motor's level at least once?
        self.sync_count = 0         # how many packets we've sent at the synced level
        self.motor_ready = False    # B08 has left 0x9F settling mode
        self.last_b08 = None
        self.ctr16 = 0x80          # B16 starting value; synced from motor on first RX
        self.seq = 0               # B12 lower 3 bits (0–7)
        self.tx_count = 0
        self.rx_count = 0
        self.running = True
        self.rx_buf = bytearray()
        self.last_tx_time: Optional[float] = None
        self.pending_tx_at: Optional[float] = None
        self.have_seen_rx = False
        self.last_motor_pkt: Optional[bytearray] = None
        self.last_sent_level: Optional[int] = None  # track for B18 change flag

    def effective_level(self) -> int:
        """Determine what level to actually send in the next packet."""
        if self.current_level is None:
            return 3  # fallback until synced
        if not self.synced or self.sync_count < 3:
            return self.current_level  # echo motor's level first
        if not self.motor_ready:
            return self.current_level  # wait for B08 to leave settling mode (0x9F)
        if self.current_level == self.target_level:
            return self.current_level
        # Step one level at a time toward target
        if self.current_level < self.target_level:
            return self.current_level + 1
        return self.current_level - 1

    def build_tx_packet(self) -> tuple[bytes, bytearray]:
        """Build TX packet. Returns (wire_bytes, decoded_packet).

        Matches real controller format exactly (from Saleae captures):
          82 AB 0C 00 01 <level> 00*16 <B16> 81 <B18> 00 00 02 00 <chk>
        B18 = 0x02 on first packet with a new level (level change flag), else 0x00.
        """
        send_level = self.effective_level()

        # Detect level change — B18=0x02 signals "I want to change level"
        level_changing = (self.last_sent_level is not None
                         and send_level != self.last_sent_level)

        pkt = bytearray(30)
        pkt[0x00] = 0x82
        pkt[0x01] = 0xAB
        pkt[0x02] = 0x0C
        pkt[0x04] = 0x01
        pkt[0x05] = LEVEL_BYTES[send_level]
        pkt[0x16] = self.ctr16 & 0xFF
        pkt[0x17] = 0x81
        pkt[0x18] = 0x02 if level_changing else 0x00  # level change flag!
        pkt[0x1B] = 0x02
        pkt[0x1D] = checksum(pkt[:0x1D])

        self.last_sent_level = send_level

        self.ctr16 = (self.ctr16 - 1) & 0xFF
        self.seq = (self.seq + 1) & 0x7

        return encode_packet(pkt), pkt

    @staticmethod
    def hex_dump(pkt: bytearray) -> str:
        return " ".join(f"{b:02X}" for b in pkt)

    def send_packet(self, reason: str = "reply") -> None:
        wire, pkt = self.build_tx_packet()
        self.ser.write(wire)
        self.tx_count += 1
        self.last_tx_time = time.monotonic()
        self.pending_tx_at = None
        self.sync_count += 1
        # Show what was actually sent (B05 from the packet, not a second effective_level call)
        sent_level = BYTE_TO_LEVEL.get(pkt[0x05], f"?{pkt[0x05]:02X}")
        target_str = f" (target=L{self.target_level})" if pkt[0x05] != LEVEL_BYTES.get(self.target_level) else ""
        print(f"  TX #{self.tx_count} ({reason}) level=L{sent_level}{target_str}")
        print(f"    {self.hex_dump(pkt)}")

    def find_next_rx_packet(self) -> Optional[bytearray]:
        """
        Scan rx_buf for 1B 1B ... 0D framing.
        Returns decoded 30-byte packet or None.
        """
        while True:
            start = self.rx_buf.find(b"\x1B\x1B")
            if start < 0:
                self.rx_buf = self.rx_buf[-1:]
                return None
            if start > 0:
                del self.rx_buf[:start]

            end = self.rx_buf.find(b"\x0D", 2)
            if end < 0:
                if len(self.rx_buf) > 200:
                    del self.rx_buf[:2]   # header is stale, skip
                return None

            raw_body = bytes(self.rx_buf[2:end])
            del self.rx_buf[:end + 1]

            pkt = decode_packet(raw_body)
            if pkt is None or not verify_checksum(pkt):
                continue   # bad frame, keep scanning

            if pkt[0x00] != 0x81:
                continue   # not a motor RX packet

            return pkt

    def handle_rx_packet(self, pkt: bytearray) -> None:
        self.rx_count += 1
        self.have_seen_rx = True
        level_byte = pkt[0x05]
        motor_level = BYTE_TO_LEVEL.get(level_byte, None)
        level_str = motor_level if motor_level is not None else f"?{level_byte:02X}"
        b08 = pkt[0x08]
        b16 = pkt[0x16]
        cs_ok = verify_checksum(pkt)
        ready_str = " READY" if self.motor_ready else " settling"
        print(f"RX #{self.rx_count} level={level_str} B08=0x{b08:02X}{ready_str} B16=0x{b16:02X} cs={'OK' if cs_ok else 'BAD'}")
        print(f"    {self.hex_dump(pkt)}")

        # Track B08 — 0x9F means motor is settling from uncontrolled state
        self.last_b08 = b08
        if not self.motor_ready and b08 != 0x9F:
            self.motor_ready = True
            print(f"  MOTOR READY (B08=0x{b08:02X})")

        # Sync: adopt the motor's reported level on first contact
        if not self.synced and motor_level is not None:
            self.current_level = motor_level
            self.synced = True
            self.sync_count = 0
            print(f"  SYNCED to motor level L{motor_level}")

        # Track when motor acknowledges a level change
        if motor_level is not None and motor_level != self.current_level:
            old = self.current_level
            self.current_level = motor_level
            self.sync_count = 0
            print(f"  Motor changed: L{old} -> L{motor_level}")

        self.last_motor_pkt = bytearray(pkt)

        # Motor sent B16=N; we reply with N-1, then each subsequent TX decrements by 1 more.
        # build_tx_packet will use self.ctr16 then decrement, so set it to the reply value now.
        self.ctr16 = (b16 - 1) & 0xFF

        self.pending_tx_at = time.monotonic() + RESPONSE_DELAY

    def poll_serial(self) -> None:
        data = self.ser.read(READ_CHUNK)
        if data:
            self.rx_buf.extend(data)
            if len(self.rx_buf) > RX_BUF_MAX:
                self.rx_buf = self.rx_buf[-RX_BUF_MAX:]
        while True:
            pkt = self.find_next_rx_packet()
            if pkt is None:
                break
            self.handle_rx_packet(pkt)

    def maybe_send_scheduled_tx(self) -> None:
        if self.pending_tx_at and time.monotonic() >= self.pending_tx_at:
            self.send_packet(reason="reply")

    def maybe_send_keepalive(self) -> None:
        if not self.have_seen_rx:
            return
        if self.pending_tx_at:
            return
        now = time.monotonic()
        if self.last_tx_time and (now - self.last_tx_time) > KEEPALIVE_INTERVAL:
            self.send_packet(reason="keepalive")

    def handle_keyboard(self) -> None:
        if not sys.stdin.isatty():
            return
        if select.select([sys.stdin], [], [], 0)[0]:
            ch = sys.stdin.read(1)
            if not ch or ch == 'q':
                print("Quitting.")
                self.running = False
            elif ch in "123456":
                new_level = int(ch)
                if new_level != self.target_level:
                    print(f"TARGET L{self.target_level} -> L{new_level}")
                    self.target_level = new_level
                    self.sync_count = 3  # allow stepping immediately

    def run(self) -> None:
        print(f"pinuaire controller3 — {self.ser.port} @ {BAUD} baud")
        print(f"Target level: L{self.target_level}")
        print("Will sync to motor's current level first, then step toward target")
        print("Type 1-6 to change fan level, q to quit")
        print("Waiting for motor packets...")
        print()
        try:
            while self.running:
                self.poll_serial()
                self.maybe_send_scheduled_tx()
                self.maybe_send_keepalive()
                self.handle_keyboard()
                time.sleep(0.001)
        except KeyboardInterrupt:
            print("\nInterrupted.")
        finally:
            self.running = False
            self.ser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Nuaire Drimaster fan controller (new protocol)")
    parser.add_argument("port", help="Serial port (e.g. /dev/tty.usbserial-1234)")
    parser.add_argument("--level", type=int, default=3, choices=range(1, 7),
                        help="Initial fan level (1-6, default: 3)")
    args = parser.parse_args()

    ctrl = DrimasterController(args.port, args.level)
    ctrl.run()


if __name__ == "__main__":
    main()
