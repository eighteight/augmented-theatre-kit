#!/usr/bin/env python3
"""Build a complete demo show from nothing.

Every file this writes is synthesised here — there is no sampled footage, no
stock photography and no third-party audio anywhere in this repository, so the
whole show can be published, forked and rebuilt without asking anyone.

    python3 make-demo-show.py [outdir]        default: demo-show/show

Needs ffmpeg on PATH for the moving pictures, and Blender for the AR set (see
../ar-set). Everything else is plain Python.
"""
import json, math, os, struct, subprocess, sys, wave, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
# Beside the script, not beside the shell, so the command in the README
# writes where the README says it does.
OUT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1
                      else os.path.join(HERE, "show"))
os.makedirs(OUT, exist_ok=True)
def p(*a): return os.path.join(OUT, *a)
def say(m): print(f"  {m}", flush=True)

def ffmpeg(args, label):
    """Run ffmpeg, and take the output away again if it did not work. ffmpeg
    creates the file before it discovers it has no encoder for it, so a failed
    run otherwise leaves an empty file that looks like a real asset."""
    out = args[-1]
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args],
                       capture_output=True, text=True)
    ok = r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0
    if not ok:
        if os.path.exists(out):
            os.remove(out)
        say(f"could not write {label}: {(r.stderr.strip() or 'empty output')[:120]}")
        return False
    say(f"wrote {label}"); return True

# ---------------------------------------------------------------- still image
def png(path, w, h, pixel):
    """A PNG written by hand: no Pillow, no dependencies."""
    raw = b"".join(b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(w))
                   for y in range(h))
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))
    open(path, "wb").write(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b""))

def poster(path):
    """A Bauhaus-ish title card: flat fields, hard edges, no gradients."""
    W, H = 600, 800     # written a pixel at a time in pure Python; keep it small
    def px(x, y):
        u, v = x / W, y / H
        if 0.08 < u < 0.92 and 0.08 < v < 0.30: return (199, 31, 23)      # red band
        if (u - 0.5) ** 2 + (v - 0.52) ** 2 < 0.045: return (23, 61, 148)  # blue disc
        if 0.30 < u < 0.70 and 0.74 < v < 0.80: return (242, 191, 20)     # yellow bar
        if 0.06 < u < 0.94 and 0.06 < v < 0.94: return (237, 235, 224)    # paper
        return (18, 18, 20)
    png(path, W, H, px); say(f"wrote {os.path.basename(path)}")

# ---------------------------------------------------------------------- audio
def chime(path, seconds=2.0, sr=44100):
    n = int(sr * seconds); buf = []
    for i in range(n):
        t = i / sr
        env = math.exp(-3.2 * t)
        v = sum(math.sin(2 * math.pi * f * t) * a
                for f, a in ((880, .6), (1320, .3), (1760, .15))) * env
        buf.append(v)
    peak = max(abs(v) for v in buf) or 1.0
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(b"".join(struct.pack("<h", int(v / peak * 0.89 * 32767)) for v in buf))
    say(f"wrote {os.path.basename(path)}")

def voice(path, seconds=3.0, sr=44100):
    """A spoken-word stand-in: a formant-ish warble, so the 3D audio cue has
    something with a clear direction to it without recording anybody."""
    n = int(sr * seconds); buf = []
    for i in range(n):
        t = i / sr
        f0 = 130 + 18 * math.sin(2 * math.pi * 1.7 * t)
        sig = sum(math.sin(2 * math.pi * f0 * k * t) / k for k in (1, 2, 3, 4, 5))
        env = min(1.0, 8 * t, 8 * (seconds - t)) * (0.6 + 0.4 * math.sin(2 * math.pi * 2.3 * t))
        buf.append(sig * env)
    peak = max(abs(v) for v in buf) or 1.0
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(b"".join(struct.pack("<h", int(v / peak * 0.89 * 32767)) for v in buf))
    say(f"wrote {os.path.basename(path)}")

# ----------------------------------------------------------------------- main
print(f"building a demo show in {OUT}")
poster(p("poster.png"))
chime(p("chime.wav")); voice(p("voice.wav"))

ffmpeg(["-i", p("chime.wav"), "-c:a", "aac", "-b:a", "128k", p("chime.m4a")], "chime.m4a")
# Ogg Vorbis is what the handsets want for the spatial voice, but not every
# ffmpeg build ships an encoder for it: libvorbis is the usual one, the native
# "vorbis" encoder is marked experimental and needs -strict -2, and some builds
# have neither. Try them in turn and fall back to AAC, which every build has.
for enc, extra, ext in (("libvorbis", ["-q:a", "4"], "ogg"),
                        ("vorbis",    ["-q:a", "4", "-strict", "-2"], "ogg"),
                        ("aac",       ["-b:a", "128k"], "m4a")):
    if ffmpeg(["-i", p("voice.wav"), "-c:a", enc, *extra, p(f"voice.{ext}")],
              f"voice.{ext} ({enc})"):
        break
else:
    say("no usable audio encoder for the voice — skipping it")

# a flat clip: drifting colour fields with a moving rule, 10 s
ffmpeg(["-f", "lavfi", "-i", "color=c=0x12121a:s=1280x720:d=10:r=25",
        "-f", "lavfi", "-i", "life=s=160x90:mold=10:r=25:ratio=0.1:"
                             "death_color=0x1a3d94:life_color=0xf2bf14,scale=1280:720:flags=neighbor",
        "-filter_complex", "[0:v][1:v]blend=all_mode=screen",
        "-t", "10", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "28",
        p("clip.mp4")], "clip.mp4")

# an equirectangular clip for the 360 cue: 2:1, a horizon and moving bands
ffmpeg(["-f", "lavfi", "-i", "color=c=0x0b0b12:s=2048x1024:d=12:r=25",
        "-vf", "drawbox=y=512:w=2048:h=3:color=0xc71f17@1:t=fill,"
               "drawgrid=w=128:h=128:t=1:c=0x2a2a3a@0.8,"
               "noise=alls=8:allf=t",
        "-t", "12", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "30",
        p("sphere-360.mp4")], "sphere-360.mp4")

# an animated GIF, so the image path gets exercised too
ffmpeg(["-f", "lavfi", "-i", "color=c=0xedebe0:s=480x480:d=4:r=12",
        "-vf", "drawbox=x='240+160*sin(2*PI*t/4)-30':y=210:w=60:h=60:color=0x173d94@1:t=fill",
        "-t", "4", "-loop", "0", p("loop.gif")], "loop.gif")

for tmp in ("chime.wav", "voice.wav"):
    os.path.exists(p(tmp)) and os.remove(p(tmp))

print("\n  the show's media:")
for f in sorted(os.listdir(OUT)):
    print(f"    {f:<18} {os.path.getsize(p(f)):>9,} bytes")
print("\n  next: build the AR set with ../ar-set, then drive it with ../drive/atc-drive.py")
