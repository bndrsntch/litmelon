import random
import time
from pathlib import Path
import typing as t

import librosa
import pygame.sndarray
from pygame.mixer import Sound


class PygameClipPlayer:
    def __init__(self, clips_dir: Path, clip_extension: str = "mp3", fadeout_length_ms: int = 6000):
        self._fadeout_length_ms = fadeout_length_ms

        self.language_sounds_by_name: dict[str, str] = {}
        for clip_path in clips_dir.glob(f"**/*.{clip_extension}"):
            self.language_sounds_by_name[clip_path.name.removesuffix(f".{clip_extension}")] = clip_path

        self._current_sound: t.Optional[Sound] = None
        self._current_clip_started_at: t.Optional[float] = None
        self._started_last_transition: t.Optional[float] = None

    @property
    def is_idle(self):
        if (
            self._started_last_transition is not None
            and time.time() - self._started_last_transition > (self._fadeout_length_ms / 1000)
        ):
            print(time.time() - self._started_last_transition)
            print("transition ended")
            self._started_last_transition = None

        if (
            self._current_clip_started_at is not None
            and time.time() - self._current_clip_started_at > self._current_sound.get_length()
        ):
            print("current clip ended")
            self._current_sound = None
            self._current_clip_started_at = None

        return self._current_sound is None

    def play_language(self, language_name: str) -> None:
        if (
            self._started_last_transition is not None
            and time.time() - self._started_last_transition < (self._fadeout_length_ms / 1000)
        ):
            print(time.time() - self._started_last_transition)
            print("waiting for transition to end")
            return

        self._started_last_transition = time.time()
        if self._current_sound is not None:
            print("fading out")
            self._current_sound.fadeout(self._fadeout_length_ms)

        clip_path = self.language_sounds_by_name[language_name]
        sound = pygame.mixer.Sound(clip_path)
        sound.play(fade_ms=self._fadeout_length_ms)
        self._current_sound = sound

    def stop_playing(self) -> None:
        self._current_sound.stop()
        self._current_sound = None
        self._started_last_transition = None
        self._current_clip_started_at = None

    def play_random_language(self) -> None:
        return self.play_language(random.choice(list(self.language_sounds_by_name.keys())))
