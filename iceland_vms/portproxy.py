"""Port forwards as a userspace proxy in the VM supervisor (0.1-4).

A7 on the tablet (23 Oct): firewalld forward-ports were put into the default zone instead of the Wi-Fi's zone,
and even in the right zone the forwarded traffic towards the NAT tap (zone nm-shared) needs a firewalld
policy. A proxy avoids forwarding altogether: the host listens on the port (INPUT, opened with a runtime
--add-port in the uplink zones) and connects to the guest over the tap (OUTPUT). It also makes
localhost:<port> work on the tablet itself.
"""
import select
import socket
import threading
import time


def _pipe(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    for s in (a, b):
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class TcpForward:
    def __init__(self, port, target, log=print, bind="::"):
        self.target, self.log, self.stop = target, log, False
        fam = socket.AF_INET6 if ":" in bind else socket.AF_INET
        self.ls = socket.socket(fam, socket.SOCK_STREAM)
        self.ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if fam == socket.AF_INET6:
            self.ls.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)   # v4 and v6 clients
        self.ls.bind((bind, port))
        self.ls.listen(16)
        self.ls.settimeout(0.5)
        self.t = threading.Thread(target=self.loop, daemon=True)
        self.t.start()

    def loop(self):
        while not self.stop:
            try:
                c, peer = self.ls.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                up = socket.create_connection(self.target, timeout=10)
                up.settimeout(None)
                c.settimeout(None)
            except OSError as e:
                self.log("forward %s -> %s:%d: %s" % (peer[0], self.target[0], self.target[1], e))
                c.close()
                continue
            threading.Thread(target=_pipe, args=(c, up), daemon=True).start()
            threading.Thread(target=_pipe, args=(up, c), daemon=True).start()

    def close(self):
        self.stop = True
        self.ls.close()


class UdpForward:
    IDLE = 60

    def __init__(self, port, target, log=print, bind="::"):
        self.target, self.log, self.stop = target, log, False
        fam = socket.AF_INET6 if ":" in bind else socket.AF_INET
        self.ls = socket.socket(fam, socket.SOCK_DGRAM)
        if fam == socket.AF_INET6:
            self.ls.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        self.ls.bind((bind, port))
        self.peers = {}          # client address -> [upstream socket, last use]
        self.t = threading.Thread(target=self.loop, daemon=True)
        self.t.start()

    def loop(self):
        while not self.stop:
            ups = {v[0]: k for k, v in self.peers.items()}
            try:
                r, _, _ = select.select([self.ls] + list(ups), [], [], 0.5)
            except (OSError, ValueError):
                break
            now = time.time()
            for s in r:
                if s is self.ls:
                    d, peer = self.ls.recvfrom(65536)
                    if peer not in self.peers:
                        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        u.connect(self.target)
                        self.peers[peer] = [u, now]
                    self.peers[peer][1] = now
                    try:
                        self.peers[peer][0].send(d)
                    except OSError:
                        pass
                else:
                    peer = ups[s]
                    try:
                        d = s.recv(65536)
                        self.ls.sendto(d, peer)
                        self.peers[peer][1] = now
                    except OSError:
                        pass
            for peer, (u, last) in list(self.peers.items()):
                if now - last > self.IDLE:
                    u.close()
                    del self.peers[peer]

    def close(self):
        self.stop = True
        self.ls.close()
        for u, _ in self.peers.values():
            u.close()


def start(forwards, guest_ip, log=print):
    """Start one forward per entry; returns the list of running forwards (failed ones are logged)."""
    out = []
    for f in forwards:
        cls = TcpForward if f["proto"] == "tcp" else UdpForward
        try:
            out.append(cls(f["host"], (guest_ip, f["guest"]), log))
            log("forward %s %d -> %s:%d" % (f["proto"], f["host"], guest_ip, f["guest"]))
        except OSError as e:
            log("forward %s %d: %s" % (f["proto"], f["host"], e))
    return out


def stop(running):
    for p in running:
        p.close()
