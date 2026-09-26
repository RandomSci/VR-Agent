"""A TTS engine for tests and the harness: writes a short tone WAV, no network.

It goes through the real TTSTaskManager, so every call is still counted by
the usage meter exactly like a paid engine.
"""

from __future__ import annotations

import math
import os
import struct
import tempfile
import wave


class FakeTTS:
    def __init__(self, seconds_per_char: float = 0.02, max_seconds: float = 1.2):
        self.seconds_per_char = seconds_per_char
        self.max_seconds = max_seconds
        self.calls: list[str] = []
        self.fail = False

    async def async_generate_audio(self, text: str, file_name_no_ext=None) -> str:
        return self.generate_audio(text, file_name_no_ext)

    def generate_audio(self, text: str, file_name_no_ext=None) -> str:
        self.calls.append(text)
        if self.fail:
            raise RuntimeError("fake tts failure")
        seconds = min(self.max_seconds, 0.25 + len(text) * self.seconds_per_char)
        rate = 16000
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="vr-fake-tts-")
        os.close(fd)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            frames = bytearray()
            for i in range(int(rate * seconds)):
                amp = 0.25 * (1 + math.sin(2 * math.pi * 3 * i / rate)) / 2
                frames += struct.pack(
                    "<h", int(32767 * amp * math.sin(2 * math.pi * 220 * i / rate))
                )
            w.writeframes(bytes(frames))
        return path

    def remove_file(self, filepath: str, verbose: bool = True) -> None:
        try:
            os.remove(filepath)
        except OSError:
            pass
