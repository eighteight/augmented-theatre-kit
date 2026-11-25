#!/usr/bin/env python3
"""Deliver a show to the phones and run it — without the Dashboard.

This is a minimal Augmented Theatre control app in one file: it serves the
show's media over HTTP, tells the handsets to fetch it, then fires the cues in
order and prints what the room is doing. Nothing here is a mock. It speaks the
same OSC and the same HTTP the Dashboard speaks, so the audience app cannot
tell the difference.

    python3 atc-drive.py ../demo-show/show --deliver      put it on the phones
    python3 atc-drive.py ../demo-show/show --run         then run the show
    python3 atc-drive.py ../demo-show/show               both, one after the other
    python3 atc-drive.py ../demo-show/show --listen      just watch the telemetry
    python3 atc-drive.py ../demo-show/show --wait=60     less patience with the room
    python3 atc-drive.py ../demo-show/show --raise       the AR set, and nothing else
    python3 atc-drive.py --ensemble                      the phones as an instrument
    python3 atc-drive.py ../demo-show/show --debug       every cue shown on the phones

Delivery and running are separate because they are separate in a theatre: the
show goes onto the phones during walk-in, over minutes, and the cues are fired
at curtain. --deliver is walk-in, --run is curtain, and either can be repeated
on its own. Running both in one go is for trying it at a desk.

When a set is raised the drawing it stands on is opened here, so a phone can
find it on this screen with nothing printed. The set is scaled against the
width of that drawing, so measure the picture on the screen and say what it
is: --marker-cm=14.5. --no-marker leaves the screen alone.

Give it any number of folders. A folder holding an ar-set.json is an AR set:
its pieces are delivered and the little .atcset document that ties them
together is written here, exactly as the Dashboard writes it, so /atc/raise
has something to raise. Anything else is a folder of media.

The ensemble needs no show at all: nothing is delivered for it, so
--ensemble may be given without a folder.

It needs no libraries: OSC is a simple enough format to write by hand, and the
standard library has an HTTP server.

Ports (the app's defaults): cues go out on 3000, handsets report on 3001, and
the media is served on 8090.
"""
import hashlib, http.server, json, mimetypes, os, socket, socketserver, struct
import re, shutil, subprocess, sys, tempfile, threading, time

CUE_PORT, TELEMETRY_PORT, HTTP_PORT = 3000, 3001, 8090
# What the handsets file this show under. One name, used by the manifest
# cue and by every inventory question, or a phone reports on a show it was
# never sent and the counts stay at zero.
SHOW_ID = "demo"

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
    """Every broadcast address this machine can reach a venue on.

    Not only the default route's. A laptop with a VPN up has its default route
    inside the tunnel, so "what is my address?" answers with the tunnel's and
    255.255.255.255 goes down it -- and every cue vanishes while the Wi-Fi the
    audience is on sits there untouched. That is a silent failure of exactly
    the kind this whole file exists to avoid, so each interface's own broadcast
    address is asked for by name and all of them are used.
    """
    out = []
    try:
        # ifconfig on macOS and BSD, ip on Linux: both are present wherever
        # they are the right answer, and neither is a dependency to install.
        for command in (["ifconfig"], ["ip", "-4", "-o", "addr"]):
            try:
                text = subprocess.run(command, capture_output=True, text=True, timeout=4).stdout
            except (OSError, subprocess.SubprocessError):
                continue
            if not text:
                continue
            # "broadcast 192.168.31.255" (ifconfig) or "brd 192.168.31.255" (ip)
            words = text.replace("\n", " ").split()
            for i, word in enumerate(words[:-1]):
                if word in ("broadcast", "brd"):
                    candidate = words[i + 1].strip()
                    if candidate.count(".") == 3 and candidate not in out:
                        out.append(candidate)
            if out:
                break
    except Exception:
        pass
    try:                                     # the portable way: ask for our own IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 1)); ip = s.getsockname()[0]; s.close()
        mine = ".".join(ip.split(".")[:3] + ["255"])
        if mine not in out: out.append(mine)
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
# The app only presents what it has a type for, so the extension decides the
# MIME exactly as the Dashboard's own map does — Python's mimetypes has never
# heard of .glb or .atcset.
MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "gif": "image/gif", "webp": "image/webp", "heic": "image/heic",
        "mp4": "video/mp4", "mov": "video/quicktime", "m4v": "video/x-m4v",
        "mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav",
        "aac": "audio/aac", "caf": "audio/x-caf", "ogg": "audio/ogg",
        "txt": "text/plain", "json": "application/json", "pdf": "application/pdf",
        "zip": "application/zip", "usdz": "model/vnd.usdz+zip",
        "glb": "model/gltf-binary", "gltf": "model/gltf+json",
        "atcset": "application/vnd.atc.arset+json",
        "frag": "x-shader/x-fragment"}
SET_MIME = MIME["atcset"]

def ext_of(name):  return name.rsplit(".", 1)[-1].lower() if "." in name else ""
def mime_of(name):
    return MIME.get(ext_of(name)) or mimetypes.guess_type(name)[0] or "application/octet-stream"

def shader_contract(source):
    """What an audio-reactive shader asks of the phone, read from its source.

    The Dashboard's importer derives the same two facts and puts them in the
    manifest: `uniforms` lists the analysis values it reads (only "level"
    exists), and `wantsCamera` says whether it draws over the live camera.
    The phone binds the camera only when told to, so a shader that samples
    uCamera without the flag draws over black.

    It is a scan, not a compiler. The shader has to be GLSL ES 1.00 and may
    declare only uTime, uLevel, uResolution and uCamera; the Dashboard
    refuses anything else at import, and nothing here would catch it.
    """
    text = re.sub(r"/\*.*?\*/|//[^\n]*", "", source.decode("utf-8", "replace"), flags=re.S)
    def declares(u):
        return re.search(r"\buniform\s+\w+\s+(?:\w+\s+)?" + u + r"\s*;", text) is not None
    return {"uniforms": ["level"] if declares("uLevel") else [],
            "wantsCamera": declares("uCamera")}

def glb_bindings(path):
    """{node name: video file} as the model itself declares it.

    A .glb is a 12-byte header then chunks; the first is the glTF JSON. A
    surface says which film belongs on it in its node `extras`, which is a
    Blender custom property — so the binding travels inside the model and
    nobody has to remember it.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(20)
            if head[:4] != b"glTF": return {}
            length, kind = struct.unpack("<II", head[12:20])
            if kind != 0x4E4F534A: return {}          # 'JSON'
            gltf = json.loads(f.read(length))
    except Exception:
        return {}
    bound = {}
    for node in gltf.get("nodes", []):
        name, extras = node.get("name"), node.get("extras")
        if not name or not isinstance(extras, dict): continue
        for key in ("video", "projection", "atc:video"):
            if isinstance(extras.get(key), str) and extras[key]:
                bound[name] = extras[key]; break
    return bound

def is_ar_set(folder):
    """A folder that holds a set, whoever made it.

    Two shapes exist. One is a set as it is authored: pieces beside an
    ar-set.json saying how they fit together. The other is a set as the
    Dashboard keeps it in a show folder -- the same pieces beside the .atcset
    document it generated, which says the same thing in digests. Both are
    sets, so a show folder written by the Dashboard can be driven from here
    without being rearranged first.
    """
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    return any(n in ("ar-set.json", "set.json") or n.endswith(".atcset") for n in names)

def atcset_document(folder):
    """The Dashboard's own set document in this folder, if that is what it is."""
    for name in sorted(os.listdir(folder)):
        if name.endswith(".atcset"):
            return os.path.join(folder, name)
    return None

class ArSet:
    """A folder the phone can stand up on a printed picture.

    The folder is read the way the Dashboard's importer reads it: ar-set.json
    names the pieces, and whatever it does not name is taken by extension.
    """
    IMAGES = ("jpg", "jpeg", "png")
    MODELS = ("glb", "gltf")
    SOUNDS = ("m4a", "mp3", "wav", "aac", "ogg", "caf")
    FILMS  = ("mp4", "mov", "m4v")

    def __init__(self, folder, width_cm=None):
        self.folder = os.path.abspath(folder)
        self.title  = os.path.basename(self.folder)
        self.id     = self.title.lower()
        side = {}
        for name in ("ar-set.json", "set.json"):
            path = os.path.join(self.folder, name)
            if os.path.isfile(path):
                try: side = json.load(open(path))
                except ValueError as e: print(f"  ! {name} is not valid JSON ({e})")
                break
        files = [n for n in sorted(os.listdir(self.folder))
                 if os.path.isfile(os.path.join(self.folder, n)) and not n.startswith(".")]

        def pick(named, exts):
            same = [n for n in files if ext_of(n) in exts]
            for n in same:
                if named and n.lower() == str(named).lower(): return n
            return same[0] if same else None

        self.marker = pick(side.get("marker"), self.IMAGES)
        self.model  = pick(side.get("model"),  self.MODELS)
        self.audio  = pick(side.get("audio"),  self.SOUNDS)
        self.films  = [n for n in files if ext_of(n) in self.FILMS]
        self.width  = float(side.get("widthMeters") or 0.21)
        self.scale  = float(side.get("scale") or 1.0)
        # The set is scaled against the width of the drawing *as the audience
        # holds it*. A picture shown on a screen instead of printed is whatever
        # size that screen makes it, so the measured width replaces the
        # authored one — otherwise the figure stands at the wrong size.
        self.measured = width_cm is not None
        if self.measured: self.width = float(width_cm) / 100.0

        # Which film goes on which surface: the model's own word first, then
        # ar-set.json, then a film named after the surface.
        authored = glb_bindings(os.path.join(self.folder, self.model)) if self.model else {}
        named = side.get("projections") if isinstance(side.get("projections"), dict) else {}
        self.node_of = {}
        for film in self.films:
            node = next((n for n, f in authored.items() if str(f).lower() == film.lower()), None)
            node = node or next((n for n, f in named.items() if str(f).lower() == film.lower()), None)
            self.node_of[film] = node or film.rsplit(".", 1)[0]
        self.authored = bool(authored)

    def pieces(self):
        return [n for n in [self.marker, self.model, self.audio] + self.films if n]

    def problems(self):
        out = []
        if not self.marker: out.append("no drawing (.jpg/.png) — the phone would have nothing to recognise")
        if not self.model:  out.append("no model (.glb)")
        if not 0.01 <= self.width <= 3.0:
            out.append(f"widthMeters is {self.width} — measure the paper being handed out")
        if self.scale <= 0: out.append("scale must be a positive number")
        return out

    def document(self, sha_of):
        """The .atcset the Dashboard would stage — what the phone actually reads."""
        one = {"id": self.id, "title": self.title,
               "marker": {"sha256": sha_of(self.marker), "widthMeters": self.width},
               "model":  {"sha256": sha_of(self.model),  "scale": self.scale},
               "projections": [{"node": self.node_of[f], "sha256": sha_of(f)}
                               for f in self.films]}
        if self.audio: one["audio"] = {"sha256": sha_of(self.audio)}
        return json.dumps({"version": 1, "sets": [one]}).encode()

def open_on_this_computer(path):
    """Hand a file to whatever this desktop opens it with."""
    try:
        if sys.platform == "darwin":   subprocess.run(["open", path], check=True)
        elif os.name == "nt":          os.startfile(path)              # noqa: S606
        else:                          subprocess.run(["xdg-open", path], check=True)
        return True
    except Exception as e:
        print(f"      (could not open it here: {e})")
        return False

def show_marker(arset):
    """Put the drawing on this screen, for a phone with nothing printed to find.

    The page asks the browser for the drawing at its declared printed width.
    CSS centimetres are a fixed 96 px to the inch, which is not any particular
    monitor, so the picture arrives near the right size rather than at it — and
    the caption says to measure it, because that number is what the set is
    scaled against.
    """
    cm = round(arset.width * 100, 2)   # 0.145 m is 14.5 cm, not 14.499999
    # The picture is copied beside the page and asked for by name: a browser
    # showing a local file is not always allowed to reach back out of its own
    # folder for one.
    here = tempfile.mkdtemp(prefix="atc-marker-")
    marker = os.path.join(here, arset.marker)
    shutil.copyfile(os.path.join(arset.folder, arset.marker), marker)
    page = os.path.join(here, "marker.html")
    with open(page, "w") as f:
        f.write(f"""<!doctype html><meta charset="utf-8">
<title>{arset.id} — point the phone at this</title>
<style>
 html,body{{margin:0;height:100%;background:#111;color:#bbb;
   font:13px/1.5 ui-sans-serif,system-ui,sans-serif;
   display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px}}
 img{{width:{cm:g}cm;max-width:92vw;max-height:72vh;image-rendering:crisp-edges;
   box-shadow:0 0 0 1px #333}}
 p{{margin:0;text-align:center;max-width:36em}}
 b{{color:#eee}}
</style>
<img src="{arset.marker}" alt="the anchor for {arset.id}">
<p><b>{arset.id}</b> — hold the phone up to this picture.</p>
<p>The set is scaled against a drawing <b>{cm:g} cm</b> wide. Measure the
picture on this screen; if it is not that, re-run with
<b>--marker-cm=&lt;what you measured&gt;</b>, or print it instead.</p>
""")
    print(f"      showing {arset.marker} on this computer "
          f"({cm:g} cm wide as declared)")
    if not open_on_this_computer(page):
        open_on_this_computer(marker)
    return page

class DeliveredSet:
    """A set the Dashboard already resolved: its document is the truth.

    It answers the same few questions ArSet answers -- which file is the
    drawing, which the model, which films on which surfaces -- so the printing
    and the cueing below do not care which shape a set arrived in.
    """
    def __init__(self, folder, set_id, entry):
        self.folder = os.path.abspath(folder)
        self.id = set_id
        self.title = entry.get("title", set_id)
        self.width = float((entry.get("marker") or {}).get("widthMeters") or 0.21)
        self.scale = float((entry.get("model") or {}).get("scale") or 1.0)
        self.measured = False
        self.authored = True
        by_sha = {}
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if os.path.isfile(path) and not name.startswith("."):
                by_sha[hashlib.sha256(open(path, "rb").read()).hexdigest()] = name
        self.marker = by_sha.get((entry.get("marker") or {}).get("sha256"), "")
        self.model  = by_sha.get((entry.get("model") or {}).get("sha256"), "")
        self.audio  = by_sha.get((entry.get("audio") or {}).get("sha256"), "")
        self.films, self.node_of = [], {}
        for projection in entry.get("projections", []):
            film = by_sha.get(projection.get("sha256"))
            if film:
                self.films.append(film)
                self.node_of[film] = projection.get("node", "")

class Show:
    """The media on disk, hashed the way the handsets identify it."""
    def __init__(self, paths, width_cm=None):
        self.assets, self.sets, self.width_cm = [], [], width_cm
        for path in paths:
            path = os.path.abspath(path)
            if not os.path.isdir(path):
                print(f"  ! {path} is not a folder — skipped"); continue
            if is_ar_set(path):
                self.add_set(path); continue
            for name in sorted(os.listdir(path)):
                full = os.path.join(path, name)
                if name.startswith("."): continue
                # a set may simply live inside the show folder
                if os.path.isdir(full):
                    if is_ar_set(full): self.add_set(full)
                    continue
                self.add_file(full)

    def add(self, name, data, path=None, set_id=None):
        sha = hashlib.sha256(data).hexdigest()
        known = next((a for a in self.assets if a["sha256"] == sha), None)
        if known: return known                 # the same bytes are one asset
        asset = {"sha256": sha, "name": name, "size": len(data), "mime": mime_of(name),
                 "path": path, "data": None if path else data, "set": set_id,
                 # an equirectangular clip is flagged, or the phone shows it flat
                 "layout": "360" if "360" in name and not set_id else "",
                 # a spatial voice is flagged by name, as the 360 clip is, or
                 # the phone plays it as an ordinary sound
                 "hrtf": "hrtf" in name.lower() and mime_of(name).startswith("audio/")}
        if asset["mime"] == MIME["frag"]:
            asset.update(shader_contract(data))
        self.assets.append(asset)
        return asset

    def add_file(self, path, set_id=None):
        return self.add(os.path.basename(path), open(path, "rb").read(), path, set_id)

    def add_set(self, folder):
        # A set the Dashboard wrote already carries its own .atcset document,
        # with every digest resolved. Nothing here can improve on it, and
        # rewriting it would change its hash for no reason, so the pieces and
        # the document are served exactly as they lie.
        document = atcset_document(folder)
        if document:
            self.add_dashboard_set(folder, document)
            return
        s = ArSet(folder, self.width_cm)
        trouble = s.problems()
        if trouble:
            for t in trouble: print(f"  ! {s.id}: {t}")
            print(f"  ! {s.id} will not be delivered")
            return
        sha = {}
        for name in s.pieces():
            sha[name] = self.add_file(os.path.join(folder, name), set_id=s.id)["sha256"]
        doc = s.document(lambda n: sha.get(n, ""))
        self.add(s.id + ".atcset", doc, None, s.id)
        self.sets.append(s)

    def add_dashboard_set(self, folder, document):
        try:
            entry = json.load(open(document))["sets"][0]
            set_id = entry["id"]
        except Exception as e:
            print(f"  ! {os.path.basename(document)} is not a set document ({e})")
            return
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if os.path.isfile(path) and not name.startswith("."):
                self.add_file(path, set_id=set_id)
        self.sets.append(DeliveredSet(folder, set_id, entry))

    def by_mime(self, prefix):
        return [a for a in self.assets if a["mime"].startswith(prefix)]
    def find(self, fragment):
        return next((a for a in self.assets
                     if fragment in a["name"] and not a["set"]), None)
    def manifest(self):
        def entry(a):
            e = {k: a[k] for k in ("sha256", "name", "size", "mime")}
            if a["layout"]: e["layout"] = a["layout"]
            if a["hrtf"]:   e["hrtf"] = True
            for k in ("uniforms", "wantsCamera"):
                if k in a: e[k] = a[k]
            return e
        return {"version": 1, "assets": [entry(a) for a in self.assets]}

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
            data = a["data"] if a["path"] is None else open(a["path"], "rb").read()
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
def deliver(send, show, house, seconds=240):
    """Put the show on the handsets and wait until they say they hold it.

    Worth knowing before watching this: pre-staging is deliberately unhurried.
    A handset sleeps a random moment of up to 30 seconds before it so much as
    fetches the manifest, and another up to 2 seconds before each asset, so a
    full house does not arrive on the server at once. A show of a dozen assets
    is therefore a minute or so away even on an empty network, and raising a
    set before it has landed does nothing at all — which is what this wait is
    for.
    """
    print(f"\n[deliver] serving {len(show.assets)} asset(s) on "
          f"http://{my_ip()}:{HTTP_PORT}")
    for a in show.assets:
        owner = f"  [{a['set']}]" if a["set"] else ""
        print(f"    {a['name']:<20} {a['size']:>9,} b  {a['mime']:<28} "
              f"{a['sha256'][:12]}…{owner}")
    for s in show.sets:
        how = "measured on screen" if s.measured else "printed"
        print(f"\n[set] {s.id} — anchor {s.marker} {how} {s.width * 100:g} cm wide, "
              f"model {s.model} at scale {s.scale:g}")
        for film in s.films:
            where = "from the model" if s.authored else "from ar-set.json"
            print(f"      {film} plays on the surface named {s.node_of[film]!r} ({where})")
        if s.audio: print(f"      score: {s.audio}")
        print(f"      raise it with:  /atc/raise {s.id}")

    # "the manifest changed — sync when convenient"; this is the pre-staging path
    send.send("/atc/manifest", HTTP_PORT, SHOW_ID, TELEMETRY_PORT)
    print(f"\n[deliver] /atc/manifest -> {', '.join(send.targets)}:{CUE_PORT}")
    print(f"[deliver] waiting up to {seconds}s for the room; a handset sleeps up "
          f"to 30s before\n          it even asks for the manifest. Ctrl-C to go "
          f"ahead anyway.")

    wanted = {a["sha256"] for a in show.assets}
    start, last_ask, last_announce, complained = time.time(), 0.0, time.time(), False
    while time.time() - start < seconds:
        now = time.time()
        # Ask what each handset holds. A phone only reports when asked, so
        # without this the counts below would never move off zero.
        if now - last_ask > 4:
            send.send("/atc/inventory", SHOW_ID, TELEMETRY_PORT)
            last_ask = now
        # And say the show is there again, for a handset that was asleep,
        # out of range or only now launched.
        if now - last_announce > 30:
            send.send("/atc/manifest", HTTP_PORT, SHOW_ID, TELEMETRY_PORT)
            last_announce = now
        time.sleep(5)

        print(f"\n[deliver] {int(time.time() - start)}s gone")
        devices = house.report(show)
        if devices and all(wanted <= set(d["holding"]) for d in devices.values()):
            print(f"\n[deliver] every handset that answered holds the whole show")
            return devices
        if not devices and not complained and time.time() - start > 45:
            complained = True
            print(f"""
  Nothing has answered in 45 seconds. Worth checking, in this order:
    · the audience app has been opened since the phone last restarted. An
      Android phone keeps listening in the background and says so in a
      notification; an iPhone stops hearing cues when the app is put away
      and asks its owner to come back; an app the user swiped away hears
      nothing on either
    · the phone and this computer are on the same Wi-Fi, and that the network
      does not isolate clients from each other (most guest and hotel ones do)
    · nothing else holds UDP {TELEMETRY_PORT}: if the Dashboard is running, the replies
      are going to it rather than here
    · this machine is really {my_ip()} — if it has two interfaces, the cue may be
      going out of the wrong one""")
    print(f"\n[deliver] gave up waiting after {seconds}s")
    return house.devices

def cue(send, label, address, *args, hold=4.0, title=None, announce=True):
    """Fire one cue — and say on the phones what is about to happen.

    The description goes out first as /title/1, which writes a line across the
    top of the handset over whatever is already there, on iPhone and Android
    alike. It makes the demo
    readable from the phone rather than from this terminal, and it is the
    quickest way to tell a cue that did not arrive from one that arrived and
    found nothing to do: the words appear either way.
    """
    shown = " ".join(f"{a:.2f}" if isinstance(a, float) else str(a) for a in args)
    print(f"\n[cue] {label}")
    if announce:
        words = title if title is not None else label
        print(f"      /title/1 {words!r}")
        send.send("/title/1", words)
        time.sleep(1.6)
    print(f"      {address} {shown}")
    send.send(address, *args)
    time.sleep(hold)

def warn_if_not_held(house, show, arset):
    """Say so before raising a set the phones have not finished fetching.

    /atc/raise on a set whose pieces are not all in the cache does nothing at
    all — the handset logs a line and the screen stays as it was. That is
    indistinguishable from a broken cue unless someone says it here.
    """
    if not house: return
    pieces = {a["sha256"]: a["name"] for a in show.assets if a["set"] == arset.id}
    with house.lock:
        devices = {k: dict(v) for k, v in house.devices.items()}
    if not devices:
        print("      no handset has said what it holds, so whether this will "
              "raise is unknown")
        return
    for did, d in sorted(devices.items(), key=lambda kv: kv[1]["ip"]):
        short = [n for sha, n in pieces.items() if sha not in d["holding"]]
        if short:
            print(f"      {d['ip']} is still short of {len(short)} piece(s): "
                  f"{', '.join(short[:4])} — it will not raise there")
        else:
            print(f"      {d['ip']} holds the whole set")

def raise_only(send, show, house, marker=True):
    """Deliver, then raise the set and nothing else."""
    if not show.sets:
        print("\n[cue] there is no AR set in what was given"); return
    cue(send, "stop whatever is on screen", "/atc/stop", "", hold=2)
    for s in show.sets:
        print(f"\n[set] {s.id} is about to be raised")
        warn_if_not_held(house, show, s)
        if marker: show_marker(s)
        cue(send, f"an AR set stands up on its anchor — {s.id}",
            "/atc/raise", s.id, 1.0, hold=45,
            title=f"point the phone at the picture — {s.id}")
        cue(send, "and is struck", "/atc/strike", hold=2)
    send.send("/title/1", "")
    print("\n[done]")

# ------------------------------------------------------------- the ensemble
# name, attack ms, partials as (ratio x100, level, decay ms). These are the
# console's own instruments, with the contact times it sends for each.
INSTRUMENTS = {
    "built-in":   (2,    []),
    "marimba":    (9,    [(100, 127, 900),  (400, 70, 450),  (1000, 40, 220)]),
    "xylophone":  (1,    [(100, 127, 300),  (300, 90, 180),  (600, 50, 120)]),
    "vibraphone": (6,    [(100, 127, 6000), (400, 55, 3000), (1000, 25, 1500)]),
    "glass":      (1,    [(100, 127, 4000), (283, 80, 2500), (541, 50, 1200)]),
    "breathed":   (1200, [(100, 127, 8000), (300, 45, 8000)]),
    "piano":      (3,    [(100, 127, 1800), (201, 60, 1200), (304, 35, 700)]),
}

class Ensemble:
    """The phones as one instrument, spoken to the way the console speaks.

    There are no note-on and note-off messages, and no arm or stop cue.
    /midi/chord is the chord being held now, sent again about three times a
    second for as long as it should sound; each phone takes one note of it,
    chosen from its own install id. The first chord a phone hears arms it --
    starts its audio and takes it out of the foyer. A phone that stops hearing
    the chord lets it expire after the ttl, so a lost packet can make a chord
    late but never leave a note sounding. To stop at once, send one empty
    chord with the shortest ttl, 250 ms: when it expires, the phones let
    everything go. /midi/instrument is the sound.

    Every number is an int, on purpose: a float strictly between 0 and 1
    anywhere in a /midi/ cue is read as the share, so a gain sent as 0.8
    silences most of the room. The only floats are the share and the phase.
    """
    TTL, GAIN, RATE = 4000, 100, 0.33

    def __init__(self, send):
        # The revision counts from the clock, as the consoles do -- tenths of
        # a second since 2026 -- so a run started after another sender is
        # above anything the phones still hold.
        self.send = send
        self.rev = int((time.time() - 1767225600) * 10) & 0x7FFFFFFF

    def stop(self):
        """One empty chord for the whole house, ttl 250 ms, three times.

        With no share it reaches every phone whatever part it joined, and
        when it expires a quarter of a second later they let everything go.
        Three copies because the repeats that would correct a lost one have
        just stopped. Half a minute later the phones stand their audio down
        by themselves."""
        self.rev += 1
        print(f"      /midi/chord rev {self.rev} rest, ttl 250  (x3)")
        for _ in range(3):
            self.send.send("/midi/chord", self.rev, 250, self.GAIN, 0); time.sleep(0.05)

    @staticmethod
    def voice_args(name, share=None, phase=None):
        attack, partials = INSTRUMENTS[name]
        args = [0, attack] + [n for p in partials for n in p]
        if share is not None: args.append(float(share))
        if phase:             args.append(float(phase))
        return args

    def state_args(self, chord, density, share=None, phase=None):
        args = [self.rev, self.TTL, self.GAIN, density] + list(chord)
        # Only a share below 1: a trailing 1.0 is not "everyone" but one more
        # pitch, the lowest MIDI has. Everyone is the absence of the argument.
        if share is not None and share < 1: args.append(float(share))
        if phase:                           args.append(float(phase))
        return args

    def hold(self, parts, seconds):
        """Keep sending, three times a second, every part's instrument and chord.

        `parts` is [(chord, instrument, density, share, phase)]. The revision
        moves once, here, because the chords just changed; every repeat after
        that carries the same one, which a phone reads as "still sounding"
        and not as a new attack.
        """
        self.rev += 1
        for chord, inst, density, share, phase in parts:
            where = "" if share is None else f"  share {share:.3f} phase {phase or 0:.3f}"
            print(f"      /midi/instrument {inst:<10} /midi/chord rev {self.rev} "
                  f"{chord or 'rest'} density {density}{where}")
        end = time.time() + seconds
        while time.time() < end:
            for chord, inst, density, share, phase in parts:
                self.send.send("/midi/instrument", *self.voice_args(inst, share, phase))
                self.send.send("/midi/chord", *self.state_args(chord, density, share, phase))
            time.sleep(self.RATE)

def phases(n):
    """The phase of each of n equal parts: 0, then (n-k)/n.

    Written as (n-k)/n, never 1 - k/n: the two differ by a rounding error,
    and a phase a hair under its share puts the phone at position 0 in two
    parts at once.
    """
    return [0.0] + [(n - k) / n for k in range(1, n)]

def run_ensemble(send):
    """A short passage: one chord, a progression, then the house in parts."""
    e = Ensemble(send)
    print("\n[ensemble] the phones as one instrument — turn the volume up")
    send.send("/title/1", "the ensemble: each phone plays one note of the chord")
    try:
        print("\n[ensemble] a chord, struck once, on the built-in voice -- it also arms the phones")
        e.hold([([60, 64, 67], "built-in", 0, None, None)], 5)
        print("\n[ensemble] a progression on marimba, re-struck 40 times a minute")
        for chord in ([57, 60, 64], [53, 57, 60, 65], [55, 59, 62, 67], [60, 64, 67, 72]):
            e.hold([(chord, "marimba", 40, None, None)], 3)
        print("\n[ensemble] a rest — an empty chord, still repeated, silences without stopping")
        e.hold([([], "marimba", 0, None, None)], 2)
        print("\n[ensemble] the house in three parts, each its own chord and instrument")
        send.send("/title/1", "the house in three parts")
        three = phases(3)
        e.hold([([60, 64, 67], "glass",     90, 1 / 3, three[0]),
                ([55, 62],     "breathed",   0, 1 / 3, three[1]),
                ([72, 76, 79], "xylophone", 120, 1 / 3, three[2])], 14)
    finally:
        e.stop()
        send.send("/title/1", "")

def run_show(send, show, house, marker=True):
    """One pass through everything the app can be asked to do."""
    poster = show.find("poster"); clip = show.find("clip.mp4")
    sphere = show.find("360");    gif = show.find("loop.gif")
    chime  = show.find("chime");  voice = show.find("voice")
    shader = next((a for a in show.by_mime(MIME["frag"]) if not a["set"]), None)

    # /start/1 is what the Dashboard's House row sends: the phone files cues
    # under this show, takes down the foyer's own controls and counts itself
    # as in the show — which is what makes a pocketed phone tell its owner the
    # performance is going on without them. /stop/1 at the end undoes it.
    cue(send, "the show begins", "/start/1", SHOW_ID,
        "Augmented Theatre — the demo show", hold=3, announce=False)
    cue(send, "a surtitle, drawn over whatever is on screen", "/text/1",
        "Tomorrow, and tomorrow, and tomorrow", 40, hold=4)
    send.send("/text/1", "")
    if poster: cue(send, f"a still on every screen — {poster['name']}",
                   "/atc/show", poster["name"], hold=4)
    if gif:    cue(send, f"an animated image — {gif['name']}",
                   "/atc/play", gif["name"], 1, hold=5)
    if clip:
        cue(send, f"a film, looping — {clip['name']}",
            "/atc/play/flat", clip["name"], 1, hold=6)
        cue(send, "the same film, quieter, without restarting it",
            "/volume/1", 0.3, hold=3)
        send.send("/volume/1", 1.0)
        cue(send, "half the house only — each phone rolls its own dice",
            "/atc/play/flat", clip["name"], 1, 0.5, hold=5)
        cue(send, "the film as a distortion of the camera's own picture",
            "/atc/play/displace", clip["name"], 1, hold=8)
        cue(send, "and stopped", "/atc/stop", clip["name"], hold=1)
    cue(send, "colour: the whole house goes red", "/color/1", 199, 31, 23, hold=3)
    cue(send, "and back", "/color/0", hold=1)
    # The four numbers are length, start, repeat and strength, in that order.
    # Both times are milliseconds; the strength is a percentage. So: a
    # two-second buzz, starting 250 ms after the cue, at 40 per cent.
    cue(send, "a buzz with a shape — 2s, a beat late, at 40%",
        "/vibrate/1", 2000, 250, 0, 40, hold=4)
    cue(send, "a buzz that keeps going until it is stopped",
        "/vibrate/1", 300, 0, 1, 100, hold=4)
    cue(send, "and stopped", "/vibrate/0", hold=2)
    cue(send, "torch on (a white screen on Android)", "/torch/1", hold=2)
    cue(send, "torch off", "/torch/0", hold=1)
    if chime:  cue(send, f"a sound — {chime['name']}",
                   "/atc/play", chime["name"], 0, hold=4)
    if voice and voice["hrtf"]:
        # A spatial voice loops until it is stopped, and is placed in metres:
        # x right, y up, z forward. It is a headphone effect.
        cue(send, f"a voice placed in the room, on headphones — {voice['name']}",
            "/atc/play", voice["name"], hold=2)
        for x, z, where in ((2.0, 0.0, "to the right"), (0.0, -2.0, "behind"),
                            (-2.0, 0.0, "to the left"), (0.0, 2.0, "in front")):
            cue(send, f"and moved {where}", "/atc/hrtfpos", voice["name"],
                x, 0.0, z, hold=2, announce=False)
        cue(send, "and stopped", "/atc/stop", voice["name"], hold=1)
    elif voice:
        cue(send, f"a voice — {voice['name']}", "/atc/play", voice["name"], 0, hold=4)
    if sphere: cue(send, f"a 360 scene, look around — {sphere['name']}",
                   "/atc/play/360", sphere["name"], 1, hold=10)
    for look in ("rain", "heat", "dziga", "rain3d", "camera", "mirror"):
        cue(send, f"a look over the device's own camera — {look}",
            "/atc/effect", look, 1, hold=5)
        cue(send, f"{look} off", "/atc/effect", look, 0, hold=1)
    if shader:
        # Driven by each phone's own microphone, so every phone draws it
        # differently. The phone asks for the microphone the first time.
        cue(send, f"a shader that listens to the room — {shader['name']} "
                  f"(make some noise)",
            "/atc/audioreact", 1, shader["name"], hold=12,
            title="a shader that listens — make some noise")
        cue(send, "and off", "/atc/audioreact", 0, shader["name"], hold=1)
    for s in show.sets:
        print(f"\n[set] {s.id} is about to be raised")
        warn_if_not_held(house, show, s)
        if marker: show_marker(s)
        cue(send, f"an AR set stands up on its anchor — {s.id} "
                  f"(point the phone at {s.marker})",
            "/atc/raise", s.id, hold=20,
            # the phone's title line is one line; the long version above is for
            # whoever is reading this terminal. The hold is long enough to put
            # the phone down, pick it up again and find the picture.
            title=f"point the phone at the picture — {s.id}")
        cue(send, "and is struck", "/atc/strike", hold=2)
    if not show.sets:
        print("\n[cue] no AR set was delivered, so nothing is raised — pass a "
              "folder\n      holding an ar-set.json to deliver one.")
    run_ensemble(send)
    cue(send, "back to the foyer", "/stop/1", hold=1, announce=False)
    print("\n[done] the show has run")

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if "--ensemble" in flags:
        # Nothing to deliver, so no server and no folder: the chord is
        # generated on the phones from the numbers in the cue.
        send = Sender()
        print(f"Augmented Theatre, the ensemble alone")
        print(f"  cues     -> {', '.join(send.targets)}:{CUE_PORT}")
        try:
            if "--debug" in flags: send.send("/debug/1")
            run_ensemble(send)
        except KeyboardInterrupt:
            print("\nstopped")
        finally:
            if "--debug" in flags: send.send("/debug/0")
        return 0
    if not args:
        print(__doc__); return 2

    width_cm = next((f.split("=", 1)[1] for f in flags if f.startswith("--marker-cm=")), None)
    if width_cm is not None:
        try: width_cm = float(width_cm)
        except ValueError:
            print(f"  ! --marker-cm wants a number of centimetres"); return 2

    wait = next((f.split("=", 1)[1] for f in flags if f.startswith("--wait=")), None)
    try: wait = float(wait) if wait is not None else None
    except ValueError:
        print("  ! --wait wants a number of seconds"); return 2

    show = Show(args, width_cm)
    if not show.assets:
        print(f"nothing to deliver in {', '.join(args)} — "
              f"run ../demo-show/make-demo-show.py first")
        return 1

    house = House()
    Handler.show = show
    try:
        server = Server(("", HTTP_PORT), Handler)
    except OSError as e:
        # The Dashboard serves its own assets on this port, so the commonest
        # reason for this is that it is open on the same machine.
        print(f"  ! port {HTTP_PORT} is taken ({e.strerror}) — quit the Dashboard, "
              f"or run this\n    on another machine. Nothing can be delivered until "
              f"the port is free.")
        return 1
    threading.Thread(target=server.serve_forever, daemon=True).start()
    send = Sender()

    print(f"Augmented Theatre, driven from Python")
    print(f"  this machine : {my_ip()}")
    print(f"  cues     -> {', '.join(send.targets)}:{CUE_PORT}")
    print(f"  media    -> http://{my_ip()}:{HTTP_PORT}")
    print(f"  show     : {len(show.assets)} asset(s)" +
          (f", {len(show.sets)} AR set(s): " + ", ".join(s.id for s in show.sets)
           if show.sets else ", no AR set"))
    print(f"  replies  <- {TELEMETRY_PORT}" + ("" if house.bound else "  (unavailable)"))

    debug = "--debug" in flags
    if debug:
        # Every cue the phone accepts goes on its own screen, with its own
        # address on the first line, until /debug/0. It changes nothing else
        # about what the phone does. For a desk, not for an audience.
        send.send("/debug/1")
        print("  debug    : on — each phone lists the cues it receives")
    try:
        if "--listen" in flags:
            print("\n[listen] watching the telemetry; ctrl-C to stop")
            while True:
                time.sleep(5); print(); house.report(show)
        if "--run" in flags:
            # Delivery already happened, in its own run. Ask what the house is
            # holding so the report below is true, and give the answers a
            # moment to arrive, but announce nothing and wait for nothing:
            # this is curtain, not walk-in.
            print("\n[run] not delivering (--run); asking what the house holds")
            send.send("/atc/inventory", SHOW_ID, TELEMETRY_PORT)
            time.sleep(2)
            house.report(show)
        else:
            devices = deliver(send, show, house, *( (wait,) if wait is not None else () ))
        if "--deliver" in flags:
            # Said as it is: "delivered" is a claim about phones, and a phone
            # that never answered has not made it.
            wanted = {a["sha256"] for a in show.assets}
            whole = [d for d in devices.values() if wanted <= set(d["holding"])]
            if not devices:
                print("\n[deliver] no handset answered — nothing is confirmed on any "
                      "phone.\n          The app has to be open and in front to hear "
                      "anything.")
            elif len(whole) == len(devices):
                print(f"\n[deliver] {len(whole)} handset(s) hold the whole show")
            else:
                print(f"\n[deliver] {len(whole)} of {len(devices)} handset(s) hold the "
                      f"whole show; the rest are still fetching")
            print("\n  run it with:")
            print(f"    python3 {os.path.relpath(sys.argv[0])} "
                  f"{' '.join(os.path.relpath(a) for a in args)} --run")
            return 0 if devices else 1
        if "--raise" in flags:
            # Nothing but the set. The looks that come before it in the full
            # run each hold the device's camera, and whether one of them has
            # really let go of it by the time ARKit asks is the first thing to
            # rule out when a set comes up black.
            raise_only(send, show, house, marker="--no-marker" not in flags)
        else:
            run_show(send, show, house, marker="--no-marker" not in flags)
        print()
        house.report(show)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        if debug: send.send("/debug/0")
    return 0

if __name__ == "__main__":
    sys.exit(main())
