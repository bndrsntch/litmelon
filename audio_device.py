from dataclasses import dataclass
from functools import cache

@dataclass
class AudioDevice:
    """
    Representation of a single channel of an eligible system audio output device. Helps us treat
    individual channels of a stereo output device as individual speakers.
    """
    device_index: int
    channel: int
    name: str

    @cache
    def __hash__(self):
        return self.device_index, self.channel
