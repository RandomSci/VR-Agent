"""Synthesize the world sounds: spatial one-shots and looping ambience beds.

    uv run --with numpy --with scipy python scripts/adventure/build_audio.py

Everything is generated from code (no samples, no services), deterministic
per seed, and encoded to small MP3 files in frontend/vr-agent/audio/world/.
Ambience beds loop seamlessly (the tail is crossfaded into the head) and are
kept free of salient events: the Adventure Director adds those as one-shots
so the characters can react to them.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
from scipy import signal

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "frontend" / "vr-agent" / "audio" / "world"
SR = 32000
rng = np.random.default_rng(7)


# ---------------------------------------------------------------------------
# building blocks
# ---------------------------------------------------------------------------


def t_axis(seconds: float) -> np.ndarray:
    return np.arange(int(seconds * SR)) / SR


def white(seconds: float) -> np.ndarray:
    return rng.standard_normal(int(seconds * SR))


def pink(seconds: float) -> np.ndarray:
    n = int(seconds * SR)
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.arange(len(spec))
    f[0] = 1
    spec /= np.sqrt(f)
    x = np.fft.irfft(spec, n)
    return x / (np.std(x) + 1e-9)


def brown(seconds: float) -> np.ndarray:
    x = np.cumsum(rng.standard_normal(int(seconds * SR)))
    x = signal.detrend(x)
    x = highpass(x, 20)
    return x / (np.std(x) + 1e-9)


def _sos(kind, freq, order=2):
    nyq = SR / 2
    if isinstance(freq, (tuple, list)):
        wn = [max(1.0, f) / nyq for f in freq]
        wn = [min(0.999, w) for w in wn]
    else:
        wn = min(0.999, max(1.0, freq) / nyq)
    return signal.butter(order, wn, btype=kind, output="sos")


def lowpass(x, f, order=2):
    return signal.sosfilt(_sos("lowpass", f, order), x)


def highpass(x, f, order=2):
    return signal.sosfilt(_sos("highpass", f, order), x)


def bandpass(x, lo, hi, order=2):
    return signal.sosfilt(_sos("bandpass", (lo, hi), order), x)


def env_adsr(n, a, d, s, r, sustain=0.7):
    a, d, r = int(a * SR), int(d * SR), int(r * SR)
    s_len = max(0, n - a - d - r)
    e = np.concatenate(
        [
            np.linspace(0, 1, max(1, a)),
            np.linspace(1, sustain, max(1, d)),
            np.full(s_len, sustain),
            np.linspace(sustain, 0, max(1, r)),
        ]
    )
    return np.pad(e, (0, max(0, n - len(e))))[:n]


def exp_decay(n, tau):
    return np.exp(-np.arange(n) / (tau * SR))


def sweep_sine(f0, f1, seconds, curve="exp"):
    n = int(seconds * SR)
    if curve == "exp":
        f = f0 * (f1 / f0) ** np.linspace(0, 1, n)
    else:
        f = np.linspace(f0, f1, n)
    return np.sin(2 * np.pi * np.cumsum(f) / SR)


def place(buf, x, at):
    i = int(at * SR)
    end = min(len(buf), i + len(x))
    if end > i:
        buf[i:end] += x[: end - i]
    return buf


def reverb(x, room=0.35, decay=1.6, mix=0.25):
    """Cheap diffuse tail: noise impulse response convolution."""
    n = int(decay * SR)
    ir = rng.standard_normal(n) * np.exp(-np.arange(n) / (decay * SR / 5))
    ir = lowpass(ir, 5000)
    ir[: int(0.012 * SR)] = 0
    wet = signal.fftconvolve(x, ir) * room
    wet = np.pad(wet, (0, max(0, len(x) + n - len(wet))))[: len(x) + n]
    out = (
        np.pad(x, (0, n)) * (1 - mix)
        + wet / (np.max(np.abs(wet)) + 1e-9) * np.max(np.abs(x)) * mix * 3
    )
    return out


def normalize(x, peak_db=-2.0, rms_db=None):
    x = np.asarray(x, dtype=np.float64)
    if rms_db is not None:
        rms = np.sqrt(np.mean(x**2)) + 1e-12
        x = x * (10 ** (rms_db / 20) / rms)
    limit = 10 ** (peak_db / 20)
    peak = np.max(np.abs(x)) + 1e-12
    if peak > limit * 0.7:
        # soft knee above 70% of the limit, never above the limit itself
        knee = limit * 0.7
        over = np.abs(x) > knee
        room = limit - knee
        x = x.copy()
        x[over] = np.sign(x[over]) * (
            knee + room * np.tanh((np.abs(x[over]) - knee) / room)
        )
    return x


def fade(x, fin=0.005, fout=0.05):
    n = len(x)
    a, b = int(fin * SR), int(fout * SR)
    e = np.ones(n)
    if a:
        e[:a] = np.linspace(0, 1, a)
    if b:
        e[-b:] = np.linspace(1, 0, b)
    return x * e


def loopable(x, xfade=2.0):
    """Crossfade the tail into the head so the bed loops without a seam."""
    k = int(xfade * SR)
    body, tail = x[:-k].copy(), x[-k:]
    ramp = np.linspace(0, 1, k)
    body[:k] = body[:k] * np.sqrt(ramp) + tail * np.sqrt(1 - ramp)
    return body


def write_mp3(name: str, x: np.ndarray, kbps: int = 64) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(x * 0.7, -1, 1) * 32767).astype(
        np.int16
    )  # 3 dB headroom: MP3 overshoots on noise
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        with wave.open(tmp.name, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm.tobytes())
        path = OUT / f"{name}.mp3"
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-y",
                "-i",
                tmp.name,
                "-codec:a",
                "libmp3lame",
                "-b:a",
                f"{kbps}k",
                str(path),
            ],
            check=True,
        )
    Path(tmp.name).unlink(missing_ok=True)
    return path


# ---------------------------------------------------------------------------
# one-shots
# ---------------------------------------------------------------------------


def hoot(seconds, f, glide=0.9):
    t = t_axis(seconds)
    freq = f * (1 + (glide - 1) * t / seconds)
    ph = 2 * np.pi * np.cumsum(freq) / SR
    tone = np.sin(ph) + 0.25 * np.sin(2 * ph) + 0.08 * np.sin(3 * ph)
    breath = bandpass(white(seconds), f * 0.8, f * 2.2) * 0.12
    return (tone + breath) * env_adsr(len(t), 0.06, 0.1, 0, seconds * 0.45, 0.75)


def owl():
    buf = np.zeros(int(2.6 * SR))
    place(buf, hoot(0.55, 410), 0.0)
    place(buf, hoot(0.28, 430, 0.95), 0.85)
    place(buf, hoot(0.6, 400, 0.88), 1.2)
    return normalize(reverb(lowpass(buf, 2500), 0.4, 1.8, 0.35), -3)


def frog():
    def croak(seconds, pitch):
        t = t_axis(seconds)
        pulses = signal.sawtooth(2 * np.pi * pitch * t) * (
            0.5 + 0.5 * np.sin(2 * np.pi * 28 * t)
        )
        body = bandpass(pulses, 500, 1400) + 0.6 * bandpass(pulses, 1600, 2600)
        return body * env_adsr(len(t), 0.01, 0.05, 0, seconds * 0.5, 0.8)

    buf = np.zeros(int(1.1 * SR))
    place(buf, croak(0.16, 95), 0.0)
    place(buf, croak(0.24, 88), 0.24)
    return normalize(reverb(buf, 0.2, 0.8, 0.15), -3)


def bird():
    buf = np.zeros(int(1.6 * SR))
    at = 0.0
    for i in range(5):
        seconds = rng.uniform(0.05, 0.1)
        f0, f1 = rng.uniform(2800, 3600), rng.uniform(4200, 5600)
        c = sweep_sine(f0, f1, seconds) * env_adsr(
            int(seconds * SR), 0.005, 0.02, 0, seconds * 0.4, 0.7
        )
        place(buf, c, at)
        at += seconds + rng.uniform(0.04, 0.16)
    return normalize(reverb(buf, 0.3, 1.2, 0.25), -4)


def rustle():
    seconds = 1.4
    n = int(seconds * SR)
    x = bandpass(white(seconds), 1800, 7000)
    grains = np.zeros(n)
    for _ in range(40):
        c = int(rng.uniform(0, n - 2000))
        w = int(rng.uniform(400, 2400))
        grains[c : c + w] += np.hanning(w) * rng.uniform(0.3, 1.0)
    shape = env_adsr(n, 0.15, 0.2, 0, 0.5, 0.8)
    return normalize(x * grains * shape, -4)


def twig():
    buf = np.zeros(int(0.8 * SR))
    for at in (0.0, 0.18):
        n = int(0.12 * SR)
        click = white(0.12) * exp_decay(n, 0.008)
        ring = np.sin(2 * np.pi * rng.uniform(1300, 1900) * t_axis(0.12)) * exp_decay(
            n, 0.02
        )
        place(buf, highpass(click, 800) + 0.5 * ring, at)
    return normalize(reverb(buf, 0.2, 0.7, 0.2), -4)


def thunder():
    seconds = 6.0
    n = int(seconds * SR)
    rumble = lowpass(brown(seconds), 180, 4) * exp_decay(n, 1.6)
    mod = 0.6 + 0.4 * np.abs(lowpass(white(seconds), 3))
    rumble *= mod / (np.max(mod) + 1e-9)
    crack = highpass(white(0.4), 400) * exp_decay(int(0.4 * SR), 0.06)
    buf = rumble * 1.0
    place(buf, crack * 0.5, 0.05)
    return normalize(fade(buf, 0.03, 1.0), -2)


def howl():
    seconds = 3.2
    t = t_axis(seconds)
    contour = np.interp(t, [0, 0.5, 1.6, 2.6, 3.2], [330, 560, 610, 500, 420])
    vib = 1 + 0.012 * np.sin(2 * np.pi * 5.5 * t)
    ph = 2 * np.pi * np.cumsum(contour * vib) / SR
    tone = np.sin(ph) + 0.3 * np.sin(2 * ph)
    x = tone * env_adsr(len(t), 0.4, 0.3, 0, 1.2, 0.8)
    return normalize(reverb(lowpass(x, 1800), 0.6, 2.8, 0.55) * 0.7, -6)


def bell_partials(
    freq, seconds, ratios=(1, 2.0, 2.76, 5.4, 8.9), decays=(1, 0.7, 0.5, 0.25, 0.15)
):
    t = t_axis(seconds)
    x = np.zeros(len(t))
    for r, d in zip(ratios, decays):
        x += np.sin(2 * np.pi * freq * r * t) * np.exp(-t / (seconds * d * 0.35)) / r
    return x * env_adsr(len(t), 0.003, 0.01, 0, 0.05, 1.0)


def chime():
    buf = np.zeros(int(2.4 * SR))
    for i, f in enumerate((1318.5, 1760.0, 2093.0)):
        place(buf, bell_partials(f, 1.8, (1, 2.4, 3.9), (1, 0.5, 0.3)), i * 0.12)
    return normalize(reverb(buf, 0.3, 1.6, 0.3), -5)


def magic():
    buf = np.zeros(int(2.0 * SR))
    notes = (880, 1108.7, 1318.5, 1760, 2217.5)
    for i, f in enumerate(notes):
        tone = bell_partials(f, 0.9, (1, 3.01, 5.2), (1, 0.4, 0.2)) * 0.6
        place(buf, tone, i * 0.07)
    swish = (
        bandpass(white(1.2), 3000, 9000)
        * env_adsr(int(1.2 * SR), 0.3, 0.2, 0, 0.6, 0.6)
        * 0.25
    )
    place(buf, swish, 0.0)
    return normalize(reverb(buf, 0.35, 1.5, 0.35), -4)


def fizzle():
    seconds = 1.2
    n = int(seconds * SR)
    hiss = white(seconds)
    center = np.linspace(6000, 900, n)
    out = np.zeros(n)
    block = 800
    for i in range(0, n, block):
        c = center[i]
        out[i : i + block] = bandpass(hiss[i : i + block + 400], c * 0.6, c * 1.4)[
            :block
        ][: len(out[i : i + block])]
    pops = np.zeros(n)
    for _ in range(14):
        c = int(rng.uniform(0, n - 300))
        pops[c : c + 200] += white(200 / SR) * np.hanning(200) * rng.uniform(0.4, 1)
    x = (out * 0.8 + highpass(pops, 1500)) * env_adsr(n, 0.02, 0.2, 0, 0.7, 0.7)
    return normalize(x, -4)


def splash():
    seconds = 1.4
    n = int(seconds * SR)
    burst = white(seconds) * exp_decay(n, 0.18)
    x = lowpass(burst, 4000) * 0.8
    for _ in range(10):
        s = rng.uniform(0.03, 0.08)
        bub = sweep_sine(rng.uniform(400, 700), rng.uniform(900, 1600), s) * np.hanning(
            int(s * SR)
        )
        place(x, bub * 0.35, rng.uniform(0.1, 1.0))
    return normalize(reverb(x, 0.25, 1.0, 0.2), -3)


def rocks():
    buf = np.zeros(int(2.4 * SR))
    for i in range(6):
        n = int(0.35 * SR)
        thud = lowpass(white(0.35), 260) * exp_decay(n, 0.06) + np.sin(
            2 * np.pi * rng.uniform(60, 110) * t_axis(0.35)
        ) * exp_decay(n, 0.08)
        place(buf, thud * rng.uniform(0.5, 1.0), 0.1 + i * rng.uniform(0.15, 0.3))
    gravel = (
        bandpass(white(2.0), 1500, 5000)
        * env_adsr(int(2.0 * SR), 0.1, 0.3, 0, 1.2, 0.4)
        * 0.3
    )
    place(buf, gravel, 0.2)
    return normalize(reverb(buf, 0.3, 1.2, 0.25), -3)


def wind_gust():
    seconds = 3.4
    n = int(seconds * SR)
    x = pink(seconds)
    t = np.linspace(0, 1, n)
    out = np.zeros(n)
    block = 1024
    for i in range(0, n, block):
        c = 500 + 1400 * np.sin(np.pi * t[i]) ** 2
        out[i : i + block] = bandpass(x[i : i + block + 512], c * 0.5, c * 1.6)[:block][
            : len(out[i : i + block])
        ]
    return normalize(out * np.sin(np.pi * t) ** 1.5, -4)


def discover():
    buf = np.zeros(int(2.6 * SR))
    for i, f in enumerate((783.99, 987.77, 1174.66, 1567.98)):
        place(buf, bell_partials(f, 1.6, (1, 2.0, 3.0), (1, 0.5, 0.3)) * 0.7, i * 0.14)
    shimmer = (
        bandpass(white(1.8), 5000, 10000)
        * env_adsr(int(1.8 * SR), 0.4, 0.4, 0, 0.8, 0.3)
        * 0.12
    )
    place(buf, shimmer, 0.3)
    return normalize(reverb(buf, 0.35, 1.8, 0.35), -4)


def footsteps():
    buf = np.zeros(int(2.2 * SR))
    for i in range(4):
        n = int(0.18 * SR)
        step = lowpass(white(0.18), rng.uniform(500, 800)) * exp_decay(n, 0.03)
        grit = bandpass(white(0.18), 2000, 5000) * exp_decay(n, 0.015) * 0.3
        place(buf, (step + grit) * rng.uniform(0.7, 1), 0.1 + i * 0.48)
    return normalize(buf, -8)


def creak():
    seconds = 1.0
    t = t_axis(seconds)
    f = (
        150
        + 40 * np.sin(2 * np.pi * 1.3 * t)
        + rng.standard_normal(len(t)).cumsum() * 0.02
    )
    saw = signal.sawtooth(2 * np.pi * np.cumsum(f) / SR)
    x = bandpass(saw, 400, 1800) * env_adsr(len(t), 0.1, 0.2, 0, 0.4, 0.7)
    return normalize(reverb(x, 0.2, 0.8, 0.2), -6)


def crackle_burst(seconds, density=30):
    n = int(seconds * SR)
    x = np.zeros(n)
    for _ in range(int(density * seconds)):
        c = int(rng.uniform(0, n - 400))
        w = int(rng.uniform(40, 300))
        x[c : c + w] += white(w / SR) * np.hanning(w) * rng.uniform(0.2, 1.0)
    return highpass(x, 900)


def crackle():
    return normalize(crackle_burst(1.2, 24), -5)


def bell():
    x = bell_partials(196.0, 5.0, (1, 2.01, 2.76, 4.1, 5.43), (1, 0.8, 0.6, 0.35, 0.2))
    return normalize(reverb(x, 0.4, 3.0, 0.35), -4)


def whoosh():
    seconds = 1.3
    n = int(seconds * SR)
    x = pink(seconds)
    t = np.linspace(0, 1, n)
    out = np.zeros(n)
    block = 512
    for i in range(0, n, block):
        c = 400 + 3600 * np.sin(np.pi * t[i])
        out[i : i + block] = bandpass(x[i : i + block + 256], c * 0.6, c * 1.5)[:block][
            : len(out[i : i + block])
        ]
    return normalize(out * np.sin(np.pi * t) ** 2, -5)


ONE_SHOTS = {
    "owl": owl,
    "frog": frog,
    "bird": bird,
    "rustle": rustle,
    "twig": twig,
    "thunder": thunder,
    "howl": howl,
    "chime": chime,
    "magic": magic,
    "fizzle": fizzle,
    "splash": splash,
    "rocks": rocks,
    "wind_gust": wind_gust,
    "discover": discover,
    "footsteps": footsteps,
    "creak": creak,
    "crackle": crackle,
    "bell": bell,
    "whoosh": whoosh,
}


# ---------------------------------------------------------------------------
# ambience beds (40 s loops)
# ---------------------------------------------------------------------------
BED = 42.0


def crickets(seconds, voices=4):
    n = int(seconds * SR)
    t = np.arange(n) / SR
    out = np.zeros(n)
    for v in range(voices):
        f = rng.uniform(3800, 5200)
        rate = rng.uniform(12, 20)  # pulses per second inside a chirp
        chirp_rate = rng.uniform(0.6, 1.4)
        pulse = (np.sin(2 * np.pi * rate * t) > 0.3).astype(float)
        chirp = (np.sin(2 * np.pi * chirp_rate * t + rng.uniform(0, 6)) > 0.2).astype(
            float
        )
        drift = 1 + 0.5 * lowpass(rng.standard_normal(n), 0.2) / 3
        tone = np.sin(2 * np.pi * f * t)
        out += (
            tone
            * lowpass(pulse * chirp, 60)
            * np.clip(drift, 0.2, 1.5)
            * rng.uniform(0.3, 0.8)
        )
    return out


def forest_night():
    x = crickets(BED, 5) * 0.25
    x += lowpass(pink(BED), 700) * 0.22
    return normalize(loopable(x), -6, rms_db=-26)


def forest_day():
    x = lowpass(pink(BED), 1600) * 0.25
    leaves = (
        bandpass(white(BED), 2000, 6000)
        * (0.4 + 0.6 * np.abs(lowpass(white(BED), 0.3) * 3))
        * 0.05
    )
    x += leaves
    for _ in range(12):
        place(x, bird() * rng.uniform(0.08, 0.2), rng.uniform(1, BED - 4))
    return normalize(loopable(x), -6, rms_db=-26)


def city_night():
    x = lowpass(brown(BED), 260, 3) * 0.6
    x += np.sin(2 * np.pi * 58 * t_axis(BED)) * 0.02
    for _ in range(5):
        at = rng.uniform(0, BED - 8)
        car = bandpass(pink(6), 200, 1400) * np.hanning(int(6 * SR)) * 0.25
        place(x, car, at)
    return normalize(loopable(x), -6, rms_db=-27)


def river():
    base = bandpass(pink(BED), 300, 2400) * 0.6
    base += highpass(white(BED), 3000) * 0.05
    for _ in range(260):
        s = rng.uniform(0.02, 0.06)
        blip = sweep_sine(
            rng.uniform(500, 900), rng.uniform(900, 1700), s
        ) * np.hanning(int(s * SR))
        place(base, blip * rng.uniform(0.05, 0.18), rng.uniform(0, BED - 0.1))
    return normalize(loopable(base), -6, rms_db=-24)


def wind():
    x = pink(BED)
    slow = 0.5 + 0.5 * np.sin(2 * np.pi * t_axis(BED) / 11) * np.sin(
        2 * np.pi * t_axis(BED) / 17.3
    )
    out = bandpass(x, 250, 1400) * (0.35 + 0.65 * slow)
    whistle = (
        np.sin(
            2 * np.pi * (620 + 60 * np.sin(2 * np.pi * t_axis(BED) / 7)) * t_axis(BED)
        )
        * slow**3
        * 0.03
    )
    return normalize(loopable(out + whistle), -6, rms_db=-25)


def rain():
    body = lowpass(pink(BED), 2500) * 0.4 + highpass(white(BED), 1500) * 0.25
    drops = np.zeros(int(BED * SR))
    for _ in range(1400):
        c = int(rng.uniform(0, len(drops) - 200))
        drops[c : c + 120] += np.hanning(120) * rng.uniform(0.1, 0.5)
    body += highpass(drops * rng.standard_normal(len(drops)), 2000) * 0.5
    return normalize(loopable(body), -6, rms_db=-23)


def ruins():
    t = t_axis(BED)
    pad = np.zeros(len(t))
    for f in (110, 164.8, 220, 277.2, 329.6):
        detune = 1 + 0.002 * np.sin(2 * np.pi * t / rng.uniform(7, 13))
        pad += np.sin(2 * np.pi * f * detune * t) / (f / 110)
    pad *= 0.5 + 0.5 * np.sin(2 * np.pi * t / 21) ** 2
    pad = lowpass(pad, 1800) * 0.25
    pad += bandpass(pink(BED), 300, 1200) * 0.12
    for _ in range(9):
        place(pad, chime() * 0.06, rng.uniform(1, BED - 3))
    return normalize(loopable(pad), -6, rms_db=-26)


def campfire_bed():
    x = lowpass(brown(BED), 400) * 0.5 + crackle_burst(BED, 9) * 0.6
    return normalize(loopable(x), -6, rms_db=-25)


BEDS = {
    "amb_forest_night": forest_night,
    "amb_forest_day": forest_day,
    "amb_city_night": city_night,
    "amb_river": river,
    "amb_wind": wind,
    "amb_rain": rain,
    "amb_ruins": ruins,
    "amb_campfire": campfire_bed,
}


def main(only: list[str] | None = None) -> None:
    for name, build in {**ONE_SHOTS, **BEDS}.items():
        if only and name not in only:
            continue
        x = build()
        x = fade(x, 0.003, 0.02) if name.startswith("amb_") else fade(x)
        path = write_mp3(name, x, 64 if name.startswith("amb_") else 80)
        print(
            f"{path.relative_to(ROOT)} {len(x) / SR:.1f}s {path.stat().st_size // 1024} KB"
        )


if __name__ == "__main__":
    main(sys.argv[1:] or None)
