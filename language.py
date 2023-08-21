from dataclasses import dataclass, field, InitVar
from pathlib import Path
import threading

from gpiozero import LED
import numpy as np
import soundfile as sf

@dataclass
class Language:
    """
    Representation of a language that consists of:
        - name of the language
        - a cached portion of a clip (loaded into memory)
        - path to the clip file that stores the rest of the clip
        - optional GPIO PIN that controls a relay that controls a light that should light up when the clip
            for this language is playing. The light mappings are read from the interactivity_config.py file.
    """
    name: str
    clip_path: Path
    frames_to_preload: InitVar[int]
    light: LED|None = None
    clip_samplerate: int|None = None
    preloaded_frames: np.ndarray = field(default_factory=lambda: np.array([]))
    preloaded_section_end: int|None = None

    def __post_init__(self, frames_to_preload: int):
        with sf.SoundFile(self.clip_path, 'r') as clip_file:
            assert clip_file.channels == 1, f"TRYING TO LOAD CLIP WITH MULTIPLE CHANNELS FOR: {self.name} -> {self.clip_path}"
            needed_frames = min(clip_file.frames, frames_to_preload)
            self.preloaded_frames = clip_file.read(needed_frames)
            self.preloaded_section_end = clip_file.tell()
            self.clip_samplerate = clip_file.samplerate

    def clip(self):
        file = sf.SoundFile(self.clip_path, 'r')
        file.seek(self.preloaded_section_end)
        return file


class LanguageThread(threading.Thread):

    def __init__(self, language: Language, *args, **kwargs):
        self.language = language
        super().__init__(*args, **kwargs)