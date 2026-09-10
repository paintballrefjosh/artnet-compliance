"""
Low-level Art-Net packet parsing and construction.

Covers just what this tool needs:
  - ArtPoll      (0x2000) - inbound, sent by controllers (e.g. Vista) to discover nodes
  - ArtPollReply (0x2100) - outbound, our response declaring ourselves as a node
  - ArtDmx       (0x5000) - inbound, the actual DMX-over-Art-Net payload we want to capture

Reference: Art-Net 4 protocol specification (Artistic Licence).
"""
import struct
import socket

ARTNET_PORT = 6454
ARTNET_HEADER = b"Art-Net\x00"

OP_POLL = 0x2000
OP_POLL_REPLY = 0x2100
OP_DMX = 0x5000

MAX_PORTS_PER_REPLY = 4  # Art-Net hard limit per ArtPollReply packet


class ArtPollPacket:
    """Minimal parse of an incoming ArtPoll — we only need to know it happened."""

    @staticmethod
    def matches(data: bytes) -> bool:
        if len(data) < 10 or data[:8] != ARTNET_HEADER:
            return False
        opcode = struct.unpack_from("<H", data, 8)[0]
        return opcode == OP_POLL


class ArtDmxPacket:
    """Parsed ArtDmx packet: universe number + DMX channel buffer."""

    __slots__ = ("universe", "sequence", "length", "data")

    def __init__(self, universe: int, sequence: int, length: int, data: bytes):
        self.universe = universe
        self.sequence = sequence
        self.length = length
        self.data = data  # raw DMX buffer, `length` bytes, channel 1 = data[0]

    @classmethod
    def parse(cls, raw: bytes):
        if len(raw) < 18 or raw[:8] != ARTNET_HEADER:
            return None
        opcode = struct.unpack_from("<H", raw, 8)[0]
        if opcode != OP_DMX:
            return None
        # ProtVerHi/Lo(2), Sequence(1), Physical(1), SubUni(1), Net(1), LengthHi/Lo(2), data...
        sequence = raw[12]
        sub_uni = raw[14]
        net = raw[15]
        universe = ((net & 0x7F) << 8) | sub_uni
        length = struct.unpack_from(">H", raw, 16)[0]
        data = raw[18:18 + length]
        return cls(universe, sequence, length, data)


def build_poll_reply(*, ip: str, mac: bytes, bind_ip: str, bind_index: int,
                      short_name: str, long_name: str, universes: list,
                      net: int = 0) -> bytes:
    """
    Build one ArtPollReply packet advertising up to 4 universes (ports).

    `universes` — list of up to 4 Art-Net universe numbers (0-255, combined
    sub-net+universe) that this "bind index" group of ports represents.
    Callers with >4 universes send multiple replies with incrementing
    bind_index, all sharing the same root `ip`/`mac` identity, so Vista
    treats them as additional ports on one logical node.
    """
    assert len(universes) <= MAX_PORTS_PER_REPLY

    ip_bytes = socket.inet_aton(ip)
    bind_ip_bytes = socket.inet_aton(bind_ip)
    padded_universes = universes + [0] * (4 - len(universes))

    packet = bytearray()
    packet += ARTNET_HEADER
    packet += struct.pack("<H", OP_POLL_REPLY)
    packet += ip_bytes
    packet += struct.pack("<H", ARTNET_PORT)
    packet += struct.pack(">H", 0)              # VersInfo
    packet += bytes([net & 0x7F, 0])             # NetSwitch, SubSwitch
    packet += struct.pack(">H", 0x0000)          # Oem
    packet += bytes([0])                          # UbeaVersion
    packet += bytes([0xD2])                       # Status1
    packet += struct.pack(">H", 0x0000)          # EstaMan
    packet += short_name.encode("ascii", "ignore")[:17].ljust(18, b"\x00")
    packet += long_name.encode("ascii", "ignore")[:63].ljust(64, b"\x00")
    node_report = f"#0001 [{bind_index:04d}] compliance-monitor OK".encode("ascii")
    packet += node_report[:63].ljust(64, b"\x00")
    packet += struct.pack(">H", len(universes))        # NumPorts
    port_types = [0x80 if i < len(universes) else 0x00 for i in range(4)]
    packet += bytes(port_types)                        # PortTypes (0x80 = can output)
    packet += bytes([0, 0, 0, 0])                       # GoodInput (n/a, we don't read DMX in)
    good_output = [0x80 if i < len(universes) else 0x00 for i in range(4)]
    packet += bytes(good_output)                        # GoodOutput
    sw_in = [0, 0, 0, 0]
    sw_out = [(u & 0x0F) if i < len(universes) else 0
              for i, u in enumerate(padded_universes)]
    packet += bytes(sw_in)
    packet += bytes(sw_out)
    packet += bytes([0])   # SwVideo
    packet += bytes([0])   # SwMacro
    packet += bytes([0])   # SwRemote
    packet += bytes([0, 0, 0])  # spare
    packet += bytes([0])   # Style (StNode)
    packet += mac[:6].ljust(6, b"\x00")
    packet += bind_ip_bytes
    packet += bytes([bind_index & 0xFF])
    packet += bytes([0])   # Status2
    packet += bytes([0] * 26)  # filler out to the expected ~239-byte total length
    return bytes(packet)
