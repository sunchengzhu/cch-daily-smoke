"""Bounded peer recovery from local gossip, preserving the pinned identity."""

import ipaddress
import time


def public_tcp_address(value):
    """Only dial literal public IPs advertised by the pinned Lightning node."""
    try:
        host, port = value.rsplit(":", 1)
        ip = ipaddress.ip_address(host.strip("[]"))
        if not ip.is_global or ip.is_multicast or not 0 < int(port) < 65536:
            return None
        host = f"[{ip}]" if ip.version == 6 else str(ip)
        return f"{host}:{int(port)}"
    except (ValueError, AttributeError):
        return None


def recover_relay_peer(pubkey, sources, query, connect, *, budget=45):
    """Query bounded sources and synchronously dial at most four unique IPs.

    query(source, args, timeout) and connect(address, timeout) are supplied by
    the caller. A successful CLI exit alone is never treated as recovery.
    The caller must still verify the existing channel and run all smoke checks.
    """
    deadline = time.monotonic() + budget
    notes = []

    def remaining(limit):
        seconds = min(limit, deadline - time.monotonic())
        if seconds < 1:
            raise TimeoutError("relay reconnect budget exhausted")
        return seconds

    def connected():
        result = query(sources[0], ["listpeers"], remaining(5))
        return any(p.get("pub_key") == pubkey for p in result.get("peers", []))

    try:
        if connected():
            return ["Relay peer already connected; no reconnect attempted."]
    except Exception as error:
        # Do not mutate connectivity when we cannot establish whether it exists.
        return [f"Relay reconnect skipped: listpeers unavailable: {error}"]

    candidates = []
    for source in sources[:2]:
        try:
            node = query(source, ["getnodeinfo", "--pub_key", pubkey], remaining(5))["node"]
            if node.get("pub_key") != pubkey:
                raise ValueError("announcement identity does not match pinned relay")
            updated = int(node.get("last_update", 0))
            addresses = [a for item in node.get("addresses", [])
                         if item.get("network") == "tcp"
                         and (a := public_tcp_address(item.get("addr")))]
            notes.append(f"Relay gossip source={source}, last_update={updated}, public_addresses={addresses}")
            candidates.extend((updated, source, address) for address in addresses)
        except Exception as error:
            notes.append(f"Relay gossip source={source} unavailable: {error}")

    seen = set()
    for updated, source, address in sorted(candidates, key=lambda item: item[0], reverse=True):
        if address in seen:
            continue
        if len(seen) == 4:
            break
        seen.add(address)
        try:
            timeout = remaining(12)
            notes.append(f"Relay reconnect attempt: {address}, source={source}, last_update={updated}")
            connect(address, timeout)
        except Exception as error:
            notes.append(f"Relay connect {address}: {error}")
        try:
            if connected():
                notes.append("Pinned relay peer connected; original channel activation still required.")
                return notes
        except Exception as error:
            notes.append(f"Relay peer verification unavailable: {error}")
            break
    notes.append("Relay peer recovery unsuccessful; check current endpoint/listener with the operator.")
    return notes
