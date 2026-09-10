"""Enumerates real network interfaces and their IPv4 addresses, for the
dashboard's interface-selection dropdown."""
import socket

import psutil


def list_ipv4_interfaces() -> list[dict]:
    """
    Returns [{"name": "Ethernet", "ip": "10.8.8.50"}, ...] for every interface
    with a usable IPv4 address. Loopback and link-local (169.254.x.x, which
    Windows assigns when a NIC has no real address) are excluded since Vista
    could never reach either.
    """
    results = []
    for name, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if addr.family != socket.AF_INET:
                continue
            ip = addr.address
            if ip.startswith("127.") or ip.startswith("169.254."):
                continue
            results.append({"name": name, "ip": ip})
    return results
