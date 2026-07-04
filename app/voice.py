"""Voice I/O for the push-to-talk interface.

The agent is interface-agnostic — it takes text and returns text. Voice is just
a thin wrapper: transcribe audio in (Whisper), synthesize audio out (OpenAI TTS).
The whole agentic pipeline in between is unchanged.

Both calls go to OpenAI (keys already configured for the rest of the system).
For production you'd swap STT->Deepgram/AssemblyAI and TTS->ElevenLabs/Cartesia
for lower latency and better voice quality; the seam here makes that a one-file
change.
"""
from __future__ import annotations
import io
from openai import OpenAI

# Reuse the OpenAI key already in the environment (loaded from .env by config).
_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI()
    return _client


# ---- Speech -> text ----------------------------------------------------------
def transcribe(audio_bytes: bytes, filename: str = "speech.webm") -> str:
    """Transcribe recorded audio to text with Whisper.

    audio_bytes: raw bytes of the uploaded audio (browser MediaRecorder gives
    webm/opus; Whisper accepts webm/mp3/mp4/wav/m4a/ogg directly).
    Returns the transcript string (may be empty if nothing was said).
    """
    client = _get_client()
    buf = io.BytesIO(audio_bytes)
    buf.name = filename  # Whisper infers format from the filename extension
    resp = client.audio.transcriptions.create(
        model="whisper-1",
        file=buf,
    )
    return (resp.text or "").strip()


# ---- Text -> speech ----------------------------------------------------------
# OpenAI voices: alloy, echo, fable, onyx, nova, shimmer. "nova" is warm/clear.
_VOICE = "nova"


def synthesize(text: str, voice: str = _VOICE) -> bytes:
    """Synthesize speech audio (mp3) from answer text via OpenAI TTS.

    Returns mp3 bytes. Truncates very long answers so the spoken reply stays
    reasonable (the full text is still shown on screen).
    """
    client = _get_client()
    spoken = text.strip()
    if len(spoken) > 4000:          # keep TTS latency/cost bounded for long answers
        spoken = spoken[:4000].rsplit(".", 1)[0] + "."
    resp = client.audio.speech.create(
        model="tts-1",              # tts-1 = lower latency; tts-1-hd = higher quality
        voice=voice,
        input=spoken or "Here is what I found.",
    )
    return resp.content


def synthesize_stream(text: str, voice: str = _VOICE):
    """Yield mp3 audio in chunks AS OpenAI generates it (streaming TTS).

    Same cost as synthesize() — billed per character of input text — but the
    client can start playing after the first chunk instead of waiting for the
    whole file, cutting perceived latency from several seconds to ~1s. The
    browser <audio> element plays this progressively as bytes arrive.
    """
    client = _get_client()
    spoken = text.strip()
    if len(spoken) > 4000:
        spoken = spoken[:4000].rsplit(".", 1)[0] + "."
    with client.audio.speech.with_streaming_response.create(
        model="tts-1",
        voice=voice,
        input=spoken or "Here is what I found.",
        response_format="mp3",
    ) as resp:
        for chunk in resp.iter_bytes(chunk_size=4096):
            yield chunk
