import base64
import os
from pydub import AudioSegment
from pydub.utils import make_chunks
from loguru import logger
from ..agent.output_types import Actions
from ..agent.output_types import DisplayText


def _get_volume_by_chunks(audio: AudioSegment, chunk_length_ms: int) -> list:
    """
    Calculate the normalized volume (RMS) for each chunk of the audio.

    Parameters:
        audio (AudioSegment): The audio segment to process.
        chunk_length_ms (int): The length of each audio chunk in milliseconds.

    Returns:
        list: Normalized volumes for each chunk.
    """
    chunks = make_chunks(audio, chunk_length_ms)
    volumes = [chunk.rms for chunk in chunks]
    max_volume = max(volumes)
    if max_volume == 0:
        raise ValueError("Audio is empty or all zero.")
    return [volume / max_volume for volume in volumes]


def audio_level(audio: AudioSegment) -> dict[str, float | int | bool]:
    samples = audio.get_array_of_samples()
    if not samples:
        return {"sample_count": 0, "min": 0, "max": 0, "rms": 0.0, "peak": 0.0, "silent": True}
    mn, mx = min(samples), max(samples)
    max_possible = float(1 << (8 * audio.sample_width - 1))
    peak = max(abs(mn), abs(mx)) / max_possible if max_possible else 0.0
    rms = float(audio.rms) / max_possible if max_possible else 0.0
    return {
        "sample_count": len(samples),
        "min": int(mn),
        "max": int(mx),
        "rms": round(rms, 6),
        "peak": round(peak, 6),
        "silent": rms < 0.0005 and peak < 0.001,
    }


def prepare_audio_payload(
    audio_path: str | None,
    chunk_length_ms: int = 20,
    display_text: DisplayText = None,
    actions: Actions = None,
    forwarded: bool = False,
) -> dict[str, any]:
    """
    Prepares the audio payload for sending to a broadcast endpoint.
    If audio_path is None, returns a payload with audio=None for silent display.

    Parameters:
        audio_path (str | None): The path to the audio file to be processed, or None for silent display
        chunk_length_ms (int): The length of each audio chunk in milliseconds
        display_text (DisplayText, optional): Text to be displayed with the audio
        actions (Actions, optional): Actions associated with the audio

    Returns:
        dict: The audio payload to be sent
    """
    if isinstance(display_text, DisplayText):
        display_text = display_text.to_dict()
    debug = os.environ.get("VR_SPEECH_DEBUG", "1").strip().lower() not in ("0", "false", "off", "no")

    if not audio_path:
        if debug:
            logger.warning("SPEECH_DIAG payload silent audio_path=None")
        # Return payload for silent display
        return {
            "type": "audio",
            "mime": None,
            "audio": None,
            "volumes": [],
            "slice_length": chunk_length_ms,
            "display_text": display_text,
            "actions": actions.to_dict() if actions else None,
            "forwarded": forwarded,
        }

    try:
        audio = AudioSegment.from_file(audio_path)
        audio_bytes = audio.export(format="wav").read()
    except Exception as e:
        raise ValueError(
            f"Error loading or converting generated audio file to wav file '{audio_path}': {e}"
        )
    audio_base64 = base64.b64encode(audio_bytes).decode("utf-8")
    volumes = _get_volume_by_chunks(audio, chunk_length_ms)
    levels = audio_level(audio)
    if debug:
        try:
            source_size = os.path.getsize(str(audio_path))
        except Exception:
            source_size = -1
        logger.warning(
            "SPEECH_DIAG payload prepared "
            f"path={audio_path} source_bytes={source_size} wav_bytes={len(audio_bytes)} "
            f"b64_chars={len(audio_base64)} duration_ms={len(audio)} "
            f"frame_rate={audio.frame_rate} channels={audio.channels} sample_width={audio.sample_width} "
            f"samples={levels['sample_count']} min={levels['min']} max={levels['max']} "
            f"rms={levels['rms']} peak={levels['peak']} silent={levels['silent']} "
            f"volumes={len(volumes)} slice_ms={chunk_length_ms} mime=audio/wav"
        )

    payload = {
        "type": "audio",
        "mime": "audio/wav",
        "audio": audio_base64,
        "volumes": volumes,
        "slice_length": chunk_length_ms,
        "display_text": display_text,
        "actions": actions.to_dict() if actions else None,
        "forwarded": forwarded,
        "audio_level": levels,
    }

    return payload


# Example usage:
# payload, duration = prepare_audio_payload("path/to/audio.mp3", display_text="Hello", expression_list=[0,1,2])
