"""manim-voiceover SpeechService backed by Microsoft Edge's neural TTS.

Edge TTS streams word-boundary events alongside the audio, so bookmarks
(<bookmark mark="x"/>) work without running Whisper transcription.
"""

import asyncio
from pathlib import Path

import edge_tts
from manim_voiceover.helper import remove_bookmarks
from manim_voiceover.services.base import SpeechService, initialize_speech_service, path_to_string


class EdgeTTSService(SpeechService):
    def __init__(self, voice: str = "en-US-AndrewNeural", rate: str = "+0%", **kwargs):
        initialize_speech_service(self, kwargs)
        self.voice = voice
        self.rate = rate

    async def _synthesize(self, text: str, out_path: Path) -> list[dict]:
        communicate = edge_tts.Communicate(text, self.voice, rate=self.rate, boundary="WordBoundary")
        boundaries = []
        cursor = 0
        with open(out_path, "wb") as f:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    word = chunk["text"]
                    # Edge reports offsets in 100 ns ticks, which is the same
                    # resolution manim-voiceover uses (10_000_000 per second).
                    idx = text.find(word, cursor)
                    if idx == -1:
                        idx = cursor
                    cursor = idx + len(word)
                    boundaries.append({
                        "audio_offset": int(chunk["offset"]),
                        "duration_milliseconds": int(chunk["duration"] / 10_000),
                        "text_offset": idx,
                        "word_length": len(word),
                        "text": word,
                        "boundary_type": "Word",
                    })
        return boundaries

    def generate_from_text(self, text, cache_dir=None, path=None, **kwargs):
        if cache_dir is None:
            cache_dir = self.cache_dir

        input_text = remove_bookmarks(text)
        input_data = {"input_text": input_text, "service": "edge_tts", "voice": self.voice, "rate": self.rate}

        cached = self.get_cached_result(input_data, cache_dir)
        if cached is not None:
            return cached

        audio_path = self.get_audio_basename(input_data) + ".mp3" if path is None else path_to_string(path)
        boundaries = asyncio.run(self._synthesize(input_text, Path(cache_dir) / audio_path))

        return {
            "input_text": text,
            "input_data": input_data,
            "original_audio": audio_path,
            "word_boundaries": boundaries,
        }
