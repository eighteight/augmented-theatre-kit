#!/usr/bin/env python3
"""Build the whole demo AR set: anchor, model, films, score and ar-set.json.

    python3 make-ar-set.py [outdir]      default: ./demo-triadic-ballet

Needs Blender and ffmpeg on PATH. Everything it writes is generated here, so
the set can be published and rebuilt by anyone.
"""
import json, math, os, shutil, struct, subprocess, sys, zlib

OUT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "demo-triadic-ballet")
HERE = os.path.dirname(os.path.abspath(__file__))
os.makedirs(OUT, exist_ok=True)
def p(*a): return os.path.join(OUT, *a)
def say(m): print(f"  {m}", flush=True)

def run(cmd, label):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        say(f"FAILED {label}: {(r.stderr or r.stdout).strip()[:200]}"); return False
    say(f"built {label}"); return True

def blender():
    for c in (os.environ.get("BLENDER"), "blender",
              "/Applications/Blender.app/Contents/MacOS/Blender",
              r"C:\Program Files\Blender Foundation\Blender\blender.exe"):
        if c and (shutil.which(c) or os.path.exists(c)): return c
    return None

# ------------------------------------------------------------------ the anchor
# A tracker finds a picture by its corners and edges. Three large flat shapes
# give it almost nothing to hold on to, so the primaries are broken up with
# rules and counter-forms until there is detail everywhere, and the whole thing
# is asymmetric — a design with a mirror line can be recognised the wrong way
# round.
def anchor(path, N=1024):
    def px(x, y):
        u, v = x / N, y / N
        if u < .05 or u > .95 or v < .05 or v > .95:      return (20, 20, 22)
        if (u-.33)**2 + (v-.34)**2 < .026:                                 # red circle
            return (237,235,224) if ((x//18 + y//18) % 2 and (u-.33)**2+(v-.34)**2 < .009) else (199,31,23)
        if .55 < u < .88 and .14 < v < .46:                                # blue square
            return (20,20,22) if (y // 14) % 3 == 0 else (23,61,148)
        if v > .60 and abs(u-.5) < (v-.60) * 1.5 and v < .88:              # yellow triangle
            return (20,20,22) if (x // 12) % 4 == 0 else (242,191,20)
        if .50 < v < .545 and .10 < u < .90:              return (20, 20, 22)  # rule
        if .13 < u < .17 and .60 < v < .92:               return (20, 20, 22)  # bar
        if .80 < u < .92 and .62 < v < .70:               return (199, 31, 23) # counter-form
        if .86 < u < .90 and .76 < v < .92:               return (23, 61, 148)
        return (237, 235, 224)
    raw = b"".join(b"\x00" + b"".join(bytes(px(x, y)) for x in range(N)) for y in range(N))
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))
    tmp = path.replace(".jpg", ".png")
    open(tmp, "wb").write(b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", N, N, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    if run(["ffmpeg","-y","-loglevel","error","-i",tmp,"-q:v","3",path], "marker.jpg"):
        os.remove(tmp)

print(f"building the demo AR set in {OUT}")
anchor(p("marker.jpg"))

# ------------------------------------------------------------------ the films
for name, life in (("screen-left", "0x1a3d94"), ("screen-right", "0xc71f17")):
    run(["ffmpeg","-y","-loglevel","error",
         "-f","lavfi","-i","color=c=0x0a0a12:s=640x360:d=6:r=24",
         "-f","lavfi","-i",f"life=s=160x90:mold=10:r=24:ratio=0.12:"
                           f"death_color={life}:life_color=0xf2bf14,"
                           f"scale=640:360:flags=neighbor",
         "-filter_complex","[0:v][1:v]blend=all_mode=screen",
         "-t","6","-c:v","libx264","-pix_fmt","yuv420p","-crf","30",
         p(f"{name}.mp4")], f"{name}.mp4")

# ------------------------------------------------------------------- the score
if run([sys.executable, os.path.join(HERE, "build-the-score.py")], "chiptune.wav"):
    src = os.path.join(os.getcwd(), "chiptune.wav")
    if os.path.exists(src):
        run(["ffmpeg","-y","-loglevel","error","-i",src,"-c:a","aac","-b:a","128k",
             p("music.m4a")], "music.m4a")
        os.remove(src)

# ------------------------------------------------------------------- the model
b = blender()
if b:
    run([b,"-b","--factory-startup","-noaudio","--python",
         os.path.join(HERE,"build-the-figure.py"),"--",p("stage.glb")], "stage.glb")
else:
    say("Blender not found — set BLENDER=/path/to/blender and run again "
        "(everything else is built)")

# ---------------------------------------------------------------- the sidecar
json.dump({"marker":"marker.jpg","model":"stage.glb","audio":"music.m4a",
           "widthMeters":0.21,"scale":1.0,
           "projections":{"screen-left":"screen-left.mp4",
                          "screen-right":"screen-right.mp4"}},
          open(p("ar-set.json"),"w"), indent=2)
say("wrote ar-set.json")

print("\n  the set:")
for f in sorted(os.listdir(OUT)):
    print(f"    {f:<20} {os.path.getsize(p(f)):>9,} bytes")
print(f"\n  print marker.jpg at 21 cm square, drop this folder on the Dashboard,")
print(f"  or serve it with ../drive/atc-drive.py")
