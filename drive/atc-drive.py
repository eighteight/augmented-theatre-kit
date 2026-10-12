#!/usr/bin/env python3
"""Deliver a show to the phones and run it — without the Dashboard.

This is a minimal Augmented Theatre control app in one file: it serves the
show's media over HTTP, tells the handsets to fetch it, then fires the cues in
order and prints what the room is doing. Nothing here is a mock. It speaks the
same OSC and the same HTTP the Dashboard speaks, so the audience app cannot
tell the difference.

    python3 atc-drive.py ../demo-show/show            deliver, then run the show
    python3 atc-drive.py ../demo-show/show --deliver  deliver and wait, no cues
    python3 atc-drive.py ../demo-show/show --listen   just watch the telemetry

It needs no libraries: OSC is a simple enough format to write by hand, and the
standard library has an HTTP server.

Ports (the app's defaults): cues go out on 3000, handsets report on 3001, and
the media is served on 8090.
"""
import hashlib, http.server, json, mimetypes, os, socket, socketserver, struct
import sys, threading, time

CUE_PORT, TELEMETRY_PORT, HTTP_PORT = 3000, 3001, 8090

# --------------------------------------------------------------------- OSC 1.0
def _pad(b):
    """Pad to a multiple of four — and not at all when it already is.

    OSC strings carry their own null terminator and are then padded out. When
    the terminated string is already a multiple of four, nothing more is added:
    padding it anyway shifts every argument after it, which is silent and only
    shows up as the wrong value arriving at the far end.
    """
    r = len(b) % 4
    return b if r == 0 else b + b"\0" * (4 - r)
def _s(v):             return _pad(v.encode("utf-8") + b"\0")
def _i(v):             return struct.pack(">i", int(v))
def _f(v):             return struct.pack(">f", float(v))

def osc(address, *args):
    """One OSC message. Types are inferred: str -> s, int -> i, float -> f."""
    tags, body = ",", b""
    for a in args:
        if   isinstance(a, bool):  tags += "i"; body += _i(1 if a else 0)
        elif isinstance(a, int):   tags += "i"; body += _i(a)
        elif isinstance(a, float): tags += "f"; body += _f(a)
        else:                      tags += "s"; body += _s(str(a))
    return _s(address) + _s(tags) + body

def parse_osc(data):
    """Enough of a reader for the handsets' replies."""
    def take_str(b, i):
        e = b.index(b"\0", i); s = b[i:e].decode("utf-8", "replace")
        return s, (e + 4) & ~3
    try:
        addr, i = take_str(data, 0)
        tags, i = take_str(data, i)
        args = []
        for t in tags[1:]:
            if t == "s":   v, i = take_str(data, i); args.append(v)
            elif t == "i": args.append(struct.unpack_from(">i", data, i)[0]); i += 4
            elif t == "f": args.append(struct.unpack_from(">f", data, i)[0]); i += 4
            else: break
        return addr, args
    except Exception:
        return None, []

# ------------------------------------------------------------------ the house
def broadcast_addresses():
    """Every directed broadcast address this machine can see.

    255.255.255.255 follows the default route, which on a laptop is often not
    the venue Wi-Fi, so cues sent there can vanish. The interface's own
    broadcast address is carried by the subnet route instead. We send to each.
    """
    out = []
    try:
        import fcntl, array
        names = array.array("B", b"\0" * 4096)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        n = struct.unpack("iL", fcntl.ioctl(s.fileno(), 0xc00c6914,
            struct.pack("iL", 4096, names.buffer_info()[0])))[0]
        del s
    except Exception:
        pass
    try:                                     # the portable way: ask for our own IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 1)); ip = s.getsockname()[0]; s.close()
        out.append(".".join(ip.split(".")[:3] + ["255"]))
    except Exception:
        pass
    out.append("255.255.255.255")
    return list(dict.fromkeys(out))

def my_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 1)); ip = s.getsockname()[0]; s.close(); return ip
    except Exception:
        return "127.0.0.1"

class Sender:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.targets = broadcast_addresses()
    def send(self, address, *args):
        packet = osc(address, *args)
        for host in self.targets:
            try: self.sock.sendto(packet, (host, CUE_PORT))
            except OSError as e: print(f"    (send to {host} failed: {e})")

# ------------------------------------------------------------------- the show
class Show:
    """The media on disk, hashed the way the handsets identify it."""
    def __init__(self, folder):
        self.folder, self.assets = os.path.abspath(folder), []
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if not os.path.isfile(path) or name.startswith("."): continue
            data = open(path, "rb").read()
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if name.endswith(".m4a"): mime = "audio/mp4"
            if name.endswith(".ogg"): mime = "audio/ogg"
            self.assets.append({"sha256": hashlib.sha256(data).hexdigest(),
                                "name": name, "size": len(data), "mime": mime,
                                "path": path,
                                # an equirectangular clip is flagged, or the
                                # phone would show it flat
                                "layout": "360" if "360" in name else ""})
    def by_mime(self, prefix):
        return [a for a in self.assets if a["mime"].startswith(prefix)]
    def find(self, fragment):
        return next((a for a in self.assets if fragment in a["name"]), None)
    def manifest(self):
        return {"version": 1, "assets": [{k: a[k] for k in
                ("sha256", "name", "size", "mime") if True} | 
                ({"layout": a["layout"]} if a["layout"] else {}) for a in self.assets]}

# --------------------------------------------------------------- serving them
class Handler(http.server.BaseHTTPRequestHandler):
    show = None
    def log_message(self, *a): pass          # the status lines below say more
    def do_GET(self):
        if self.path == "/manifest.json":
            body = json.dumps(Handler.show.manifest()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
            print(f"    -> {self.client_address[0]} fetched the manifest")
            return
        if self.path.startswith("/asset/"):
            sha = self.path.split("/asset/", 1)[1]
            a = next((x for x in Handler.show.assets if x["sha256"] == sha), None)
            if not a: self.send_error(404); return
            data = open(a["path"], "rb").read()
            start, end = 0, len(data) - 1
            rng = self.headers.get("Range", "")
            partial = rng.startswith("bytes=")
            if partial:                        # resume support, as the app expects
                s_, _, e_ = rng[6:].partition("-")
                start = int(s_ or 0); end = int(e_) if e_ else len(data) - 1
            chunk = data[start:end + 1]
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", a["mime"])
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers(); self.wfile.write(chunk)
            print(f"    -> {self.client_address[0]} took {a['name']} "
                  f"({len(chunk):,} bytes{' partial' if partial else ''})")
            return
        self.send_error(404)
    def do_HEAD(self): self.do_GET()

class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True; allow_reuse_address = True

# ------------------------------------------------------- what the house says
class House:
    """What the handsets report back, on the telemetry port."""
    def __init__(self):
        self.devices = {}          # id -> {"ip", "last", "holding": set()}
        self.progress = {}         # (id, sha) -> (received, total)
        self.lock = threading.Lock()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try: self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError: pass
        self.bound = True
        try:
            self.sock.bind(("", TELEMETRY_PORT))
        except OSError as e:
            self.bound = False
            print(f"  ! port {TELEMETRY_PORT} is taken ({e.strerror}) — is the "
                  f"Dashboard running? Delivery still works; the counts below "
                  f"will stay at zero because the replies go to it, not here.")
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self):
        while self.bound:
            try: data, addr = self.sock.recvfrom(65535)
            except OSError: return
            address, args = parse_osc(data)
            if not address: continue
            with self.lock:
                if address == "/atc/holding" and len(args) >= 4:
                    did = str(args[0])
                    d = self.devices.setdefault(did, {"ip": addr[0], "last": 0,
                                                      "holding": set()})
                    d["ip"], d["last"] = addr[0], time.time()
                    d["holding"].update(str(a) for a in args[4:])
                elif address == "/atc/progress" and len(args) >= 4:
                    did, sha, got, tot = str(args[0]), str(args[1]), args[2], args[3]
                    self.progress[(did, sha)] = (got, tot)
                    d = self.devices.setdefault(did, {"ip": addr[0], "last": 0,
                                                      "holding": set()})
                    d["ip"], d["last"] = addr[0], time.time()
                    if tot and got >= tot: d["holding"].add(sha)
                else:
                    did = str(args[0]) if args else addr[0]
                    d = self.devices.setdefault(did, {"ip": addr[0], "last": 0,
                                                      "holding": set()})
                    d["ip"], d["last"] = addr[0], time.time()

    def report(self, show):
        with self.lock:
            devices = dict(self.devices)
            inflight = dict(self.progress)
        if not devices:
            print("  no handset has answered yet")
            return devices
        total = len(show.assets)
        print(f"  {len(devices)} handset(s):")
        for did, d in sorted(devices.items(), key=lambda kv: kv[1]["ip"]):
            have = sum(1 for a in show.assets if a["sha256"] in d["holding"])
            moving = [f"{a['name']} {100*g//t if t else 0}%"
                      for a in show.assets
                      for (i, s), (g, t) in inflight.items()
                      if i == did and s == a["sha256"] and t and g < t]
            age = time.time() - d["last"]
            line = f"    {d['ip']:<15} {have}/{total} held"
            if moving: line += "  fetching: " + ", ".join(moving[:3])
            line += f"   (heard {age:.0f}s ago)"
            print(line)
        return devices

# ------------------------------------------------------------------ the cues
def deliver(send, show, house, seconds=25):
    """Put the show on the handsets and wait for them to say they have it."""
    print(f"\n[deliver] serving {len(show.assets)} asset(s) on "
          f"http://{my_ip()}:{HTTP_PORT}")
    for a in show.assets:
        print(f"    {a['name']:<18} {a['size']:>9,} b  {a['mime']:<16} {a['sha256'][:12]}…")

    # "the manifest changed — sync when convenient"; this is the pre-staging path
    send.send("/atc/manifest", HTTP_PORT, "demo", TELEMETRY_PORT)
    print(f"\n[deliver] /atc/manifest -> {', '.join(send.targets)}:{CUE_PORT}")
    # and ask what each handset already holds, so the counts start truthful
    send.send("/atc/inventory", "demo", TELEMETRY_PORT)

    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(5)
        print(f"\n[deliver] {int(deadline - time.time())}s left")
        house.report(show)
    return house.devices

def cue(send, label, address, *args, hold=4.0):
    shown = " ".join(f"{a:.2f}" if isinstance(a, float) else str(a) for a in args)
    print(f"\n[cue] {label}")
    print(f"      {address} {shown}")
    send.send(address, *args)
    time.sleep(hold)

def run_show(send, show, house):
    """One pass through everything the app can be asked to do."""
    poster = show.find("poster"); clip = show.find("clip.mp4")
    sphere = show.find("360");    gif = show.find("loop.gif")
    chime  = show.find("chime");  voice = show.find("voice")

    cue(send, "house lights: bring every phone into the show", "/atc/show", "", hold=2)
    if poster: cue(send, f"a still on every screen — {poster['name']}",
                   "/atc/play", poster["name"], 1, 1.0, hold=5)
    if gif:    cue(send, f"an animated image — {gif['name']}",
                   "/atc/play", gif["name"], 1, 1.0, hold=5)
    if clip:   cue(send, f"a film, looping — {clip['name']}",
                   "/atc/play/flat", clip["name"], 1, 1.0, hold=8)
    cue(send, "colour: the whole house goes red", "/color/1", 199, 31, 23, hold=3)
    cue(send, "vibrate", "/vibrate/1", hold=2)
    cue(send, "torch on", "/torch/1", hold=2)
    cue(send, "torch off", "/torch/0", hold=1)
    if chime:  cue(send, f"a sound — {chime['name']}",
                   "/atc/play", chime["name"], 0, 1.0, hold=4)
    if voice:  cue(send, f"a voice placed in the room — {voice['name']}",
                   "/atc/play", voice["name"], 0, 1.0, hold=4)
    if sphere: cue(send, f"a 360 scene, look around — {sphere['name']}",
                   "/atc/play/360", sphere["name"], 1, 1.0, hold=10)
    for look in ("rain", "heat", "dziga", "camera", "mirror"):
        cue(send, f"a look over the device's own camera — {look}",
            "/atc/effect", look, 1, 1.0, hold=5)
        cue(send, f"{look} off", "/atc/effect", look, 0, 1.0, hold=1)
    cue(send, "an AR set stands up on its anchor — demo-triadic-ballet",
        "/atc/raise", "demo-triadic-ballet", 1.0, hold=12)
    cue(send, "and is struck", "/atc/strike", hold=2)
    cue(send, "back to the foyer", "/atc/stop", "", hold=1)
    print("\n[done] the show has run")

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args:
        print(__doc__); return 2
    show = Show(args[0])
    if not show.assets:
        print(f"no media in {args[0]} — run ../demo-show/make-demo-show.py first")
        return 1

    house = House()
    Handler.show = show
    server = Server(("", HTTP_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    send = Sender()

    print(f"Augmented Theatre, driven from Python")
    print(f"  this machine : {my_ip()}")
    print(f"  cues     -> {', '.join(send.targets)}:{CUE_PORT}")
    print(f"  media    -> http://{my_ip()}:{HTTP_PORT}")
    print(f"  replies  <- {TELEMETRY_PORT}" + ("" if house.bound else "  (unavailable)"))

    try:
        if "--listen" in flags:
            print("\n[listen] watching the telemetry; ctrl-C to stop")
            while True:
                time.sleep(5); print(); house.report(show)
        deliver(send, show, house)
        if "--deliver" in flags:
            print("\n[deliver] done; not running the cues (--deliver)")
            return 0
        run_show(send, show, house)
        print()
        house.report(show)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0

if __name__ == "__main__":
    sys.exit(main())
