#!/usr/bin/env python
import os
import shutil
from pathlib import Path
from typing import List, Tuple

import librosa
import numpy
import pygame
import soundfile

# ONLY SET TO 500ms for BOWLS
SOUND_FADE_MILLISECONDS = 500

AUDIO_ASSET_PREFIX = "instrumentation/"
CURRENT_WORKING_DIR = Path(__file__).parent.absolute()


# C D E G A C D E G A C
def get_or_create_key_sounds(
        wav_path: str,
        sample_rate_hz: int,
        channels: int,
        tones: List[int],
        clear_cache: bool = False,
) -> list[pygame.mixer.Sound]:
    sounds = []
    y, sr = librosa.load(wav_path, sr=sample_rate_hz, mono=channels == 1)
    file_name = os.path.splitext(os.path.basename(wav_path))[0]
    folder_containing_wav = Path(wav_path).parent.absolute()
    cache_folder_path = Path(folder_containing_wav, file_name)
    if clear_cache and cache_folder_path.exists():
        shutil.rmtree(cache_folder_path)
    if not cache_folder_path.exists():
        print("Generating samples for each key")
        os.mkdir(cache_folder_path)
    for i, tone in enumerate(tones):
        cached_path = Path(cache_folder_path, "{}.wav".format(tone))
        if Path(cached_path).exists():
            # print("Loading note {} out of {} for {} for {}".format(i + 1, len(tones), keys[i], tones[i]))
            sound, sr = librosa.load(cached_path, sr=sample_rate_hz, mono=channels == 1)
            if channels > 1:
                # the shape must be [length, 2]
                sound = numpy.transpose(sound)
        else:
            # print(
            #     "Transposing note {} out of {} for {}".format(
            #         i + 1, len(tones), keys[i]
            #     )
            # )
            if channels == 1:
                sound = librosa.effects.pitch_shift(y, sr=sr, n_steps=tone)
            else:
                new_channels = [
                    librosa.effects.pitch_shift(y[i], sr=sr, n_steps=tone)
                    for i in range(channels)
                ]
                sound = numpy.ascntiguousarray(numpy.vstack(new_channels).T)
            soundfile.write(cached_path, sound, sample_rate_hz, 32)

        sounds.append(sound)

    return [pygame.sndarray.make_sound(sound) for sound in sounds]


def get_audio_data(wav_path: str) -> Tuple:
    audio_data, framerate_hz = soundfile.read(wav_path)
    array_shape = audio_data.shape
    if len(array_shape) == 1:
        channels = 1
    else:
        channels = array_shape[1]
    return audio_data, framerate_hz, channels


class GodModeHandler:
    def __init__(self):
        wav_path = os.path.join(CURRENT_WORKING_DIR, "instrumentation/bowl_c6.wav")
        audio_data, framerate_hz, channels = get_audio_data(wav_path)
        tones = [-22, -20, -17, -15, -12, -10, -8, -5, -3, 0, 2, 4, 7, 9, 12, 14, 16, 19, 21, 24, 26, 28, 31, 33]
        tones = [t - 12 for t in tones]
        self._key_sounds = get_or_create_key_sounds(
            wav_path, framerate_hz, channels, tones,
        )

    def on_press(self, coords: tuple[int, int]) -> None:
        sound = self._key_sounds[coords[0] * 8 + coords[1]]
        sound.stop()
        sound.play(fade_ms=SOUND_FADE_MILLISECONDS)

    def on_release(self, coords: tuple[int, int]) -> None:
        sound = self._key_sounds[coords[0] * 8 + coords[1]]
        sound.fadeout(SOUND_FADE_MILLISECONDS)
