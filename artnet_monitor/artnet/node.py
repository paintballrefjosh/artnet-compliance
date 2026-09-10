"""
ArtNetNode: makes this machine discoverable by Vista (or any Art-Net controller)
as a unicast-assignable node, and captures incoming ArtDmx frames.

Universe numbering note: Art-Net's wire protocol is 0-based, but Vista's UI
(like most consoles) displays universes starting at 1. Everywhere outside
this module — config file, callback, logs, dashboard — universes are the
Vista-style number you'd actually see/type in Vista. The 0-based wire value
is only ever used internally when building/parsing Art-Net packets.

Usage is simple: give it a list of universe numbers (Vista-style, 1-based),
a callback to receive decoded DMX frames, and call .run() or .start().
"""
import logging
import socket
import threading
import time
import uuid

from .protocol import (
    ARTNET_PORT,
    ArtDmxPacket,
    ArtPollPacket,
    MAX_PORTS_PER_REPLY,
    build_poll_reply,
)

log = logging.getLogger("artnet.node")


def _local_mac_bytes() -> bytes:
    """Locally-administered MAC so we never collide with real hardware vendors."""
    node = uuid.getnode()
    mac = node.to_bytes(6, "big")
    return bytes([mac[0] | 0x02]) + mac[1:]  # set the locally-administered bit


class ArtNetNode:
    def __init__(self, listen_ip: str, universes: list[int], on_dmx_frame):
        """
        listen_ip: the IP address of the NIC to bind/advertise (must be the real,
                   LAN-reachable address of this machine — not 0.0.0.0/127.0.0.1 —
                   since this is the address Vista will unicast ArtDmx back to.)
        universes: universe numbers as shown in Vista's UI (1-based, e.g. 1-8).
        on_dmx_frame: callback(universe: int, data: bytes) called on every ArtDmx
                      frame, `universe` given as the same Vista-style number.
        """
        self.listen_ip = listen_ip
        self.universes = universes  # Vista-style (1-based), used for the public API
        self.on_dmx_frame = on_dmx_frame
        self.mac = _local_mac_bytes()
        self._sock = None
        self._stop = threading.Event()
        self._thread = None

        # Wire-level (0-based) universes, only used for Art-Net packet building/parsing
        wire_universes = [u - 1 for u in universes]
        self._wire_to_display = dict(zip(wire_universes, universes))

        # Split into groups of up to 4 for ArtPollReply "bind index" ports
        self._port_groups = [
            wire_universes[i:i + MAX_PORTS_PER_REPLY]
            for i in range(0, len(wire_universes), MAX_PORTS_PER_REPLY)
        ] or [[]]

    def _open_socket(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass  # not fatal, but ArtPoll from Vista running on same host may not arrive
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", ARTNET_PORT))
        return sock

    def _send_poll_replies(self, sock, dest_addr):
        for bind_index, wire_group in enumerate(self._port_groups, start=1):
            reply = build_poll_reply(
                ip=self.listen_ip,
                mac=self.mac,
                bind_ip=self.listen_ip,
                bind_index=bind_index,
                short_name="ComplianceMon",
                long_name="Art-Net Compliance Monitor",
                universes=wire_group,
            )
            sock.sendto(reply, dest_addr)
        log.info("Sent %d ArtPollReply packet(s) covering universe(s) %s to %s",
                  len(self._port_groups), self.universes, dest_addr)

    def run(self):
        """Blocking receive loop. Call from a dedicated thread if not the main one."""
        self._sock = self._open_socket()
        log.info("Art-Net node listening on UDP %s (universes: %s)",
                  ARTNET_PORT, self.universes)

        # Proactively broadcast on startup so Vista discovers us immediately,
        # rather than waiting for its next periodic ArtPoll cycle.
        try:
            self._send_poll_replies(self._sock, ("255.255.255.255", ARTNET_PORT))
        except OSError as e:
            log.warning("Startup broadcast announcement failed (will still respond "
                        "to incoming ArtPoll): %s", e)

        while not self._stop.is_set():
            try:
                self._sock.settimeout(1.0)
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break

            if ArtPollPacket.matches(data):
                log.debug("ArtPoll received from %s", addr)
                self._send_poll_replies(self._sock, addr)
                continue

            frame = ArtDmxPacket.parse(data)
            if frame is None:
                log.debug("Non-DMX/unrecognized packet from %s (%d bytes, header: %s)",
                          addr, len(data), data[:10])
                continue

            if frame.universe not in self._wire_to_display:
                log.debug(
                    "Dropped ArtDmx from %s: wire universe %d (Vista-style %d) not "
                    "in claimed universes %s — check config universes vs. what Vista "
                    "actually assigned",
                    addr, frame.universe, frame.universe + 1, self.universes,
                )
                continue

            display_universe = self._wire_to_display[frame.universe]
            log.debug("ArtDmx received: universe %d from %s, first 4 channels: %s",
                      display_universe, addr, list(frame.data[:4]))
            self.on_dmx_frame(display_universe, frame.data)

    def start(self):
        self._thread = threading.Thread(target=self.run, daemon=True, name="artnet-node")
        self._thread.start()
        return self._thread

    def stop(self):
        self._stop.set()
        if self._sock:
            self._sock.close()

