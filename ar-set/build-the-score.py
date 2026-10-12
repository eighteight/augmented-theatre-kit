# A short 8-bit loop for the AR set: square-wave lead, pulse bass, noise
# percussion. Eight seconds at 120 BPM, looping exactly so it can sit under a
# looping projection without a seam.
import math, struct, wave, random

SR    = 44100
BPM   = 120.0
BEAT  = 60.0 / BPM          # 0.5 s
BARS  = 4
BEATS = BARS * 4            # 16 beats = 8 s
TOTAL = int(SR * BEATS * BEAT)
buf   = [0.0] * TOTAL
random.seed(7)

def note_hz(semitones_from_a4):
    return 440.0 * (2.0 ** (semitones_from_a4 / 12.0))

# Semitone offsets from A4 for the notes used.
N = {'C4':3,'D4':5,'E4':7,'F4':8,'G4':10,'A4':12,'B4':14,
     'C5':15,'D5':17,'E5':19,'G5':22,'A5':24,
     'C3':-9,'F3':-4,'G3':-2,'A3':0}

def square(t, hz, duty=0.5):
    return 1.0 if (t * hz) % 1.0 < duty else -1.0

def env(i, n, attack=0.005, release=0.06):
    a, r = int(SR*attack), int(SR*release)
    if i < a: return i / max(1, a)
    if i > n - r: return max(0.0, (n - i) / max(1, r))
    return 1.0

def play(name, start_beat, beats, gain=0.22, duty=0.5, detune=0.0):
    hz = note_hz(N[name]) * (2 ** (detune/1200.0))
    s0 = int(start_beat * BEAT * SR); n = int(beats * BEAT * SR)
    for i in range(n):
        if s0+i >= TOTAL: break
        t = i / SR
        buf[s0+i] += square(t, hz, duty) * env(i, n) * gain

def noise(start_beat, beats, gain=0.08, hp=False):
    s0 = int(start_beat*BEAT*SR); n = int(beats*BEAT*SR)
    last = 0.0
    for i in range(n):
        if s0+i >= TOTAL: break
        v = random.uniform(-1, 1)
        if hp: v, last = v - last, v        # crude high-pass -> hat
        buf[s0+i] += v * env(i, n, 0.001, 0.02) * gain

# --- lead: arpeggios over C - Am - F - G, eighth notes ---
chords = [['C4','E4','G4','C5'], ['A4','C5','E5','A5'],
          ['F4','A4','C5','E5'], ['G4','B4','D5','G5']]
for bar, ch in enumerate(chords):
    for e in range(8):                       # eight eighth-notes per bar
        pitch = ch[[0,1,2,3,2,1,2,3][e]]
        play(pitch, bar*4 + e*0.5, 0.45, gain=0.20, duty=0.25)

# --- bass: root on every beat, a twelfth below ---
roots = ['C3','A3','F3','G3']
for bar, r in enumerate(roots):
    for b in range(4):
        play(r, bar*4 + b, 0.9, gain=0.26, duty=0.5)

# --- percussion: hat on every eighth, a thicker hit on 1 and 3 ---
for b in range(BEATS*2):
    noise(b*0.5, 0.12, gain=0.05, hp=True)
for b in range(BEATS):
    if b % 2 == 0: noise(b, 0.18, gain=0.10)

peak = max(abs(v) for v in buf) or 1.0
with wave.open('chiptune.wav','wb') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
    w.writeframes(b''.join(struct.pack('<h', int(max(-1,min(1,v/peak*0.89))*32767)) for v in buf))
print(f"wrote chiptune.wav: {BEATS*BEAT:.1f}s, peak normalised")
