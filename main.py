import logging
from pathlib import Path
import time
import threading

import typer
import sounddevice as sd

from gpiozero import LED

from language import Language
from audio_device import AudioDevice
from clip_player import ClipOverlapStrategy, ClipPlayer
from interactivity_config import keys_by_language, light_pins_by_language

logging.basicConfig(level=logging.DEBUG)

def get_devices(name_filter: str) -> list[AudioDevice]:
    """
    Use the `sounddevice` library to construct a list of all audio devices whose names contain the `name_filter` as a substring.
    """
    devices = sd.query_devices()
    devices = [AudioDevice(device['index'], channel) for device in devices for channel in range(0,2) if name_filter in device["name"]]
    logging.info(f"Initialized with devices: {devices}")
    return devices

def get_languages(clips_dir: Path, clip_extension: str, clip_preload_frames: int) -> list[Language]:
    """
    Initialize all languages by loading audio clips from `clips_dir` who have the `clip_extension` file extension.
    Also load any light mappings from `interactivity_config.py`
    """
    languages = []
    for clip_path in clips_dir.glob(f"**/*.{clip_extension}"):
        language = clip_path.stem
        if language in light_pins_by_language:
            light = LED(light_pins_by_language[language])
        else:
            light = None
        languages.append(Language(language, clip_path, clip_preload_frames, light))
        logging.info(f"Loaded {language}.")
    logging.info(f"Loaded {len(languages)} clips.")
    return languages

def main(
        clips_dir: Path = "clips",
        clip_extension: str = "mp3",
        sound_device_type: str = "MOTU Phones",
        fallback_time: int = 30,
        fadeout_length: int = 10,
        clip_overlap_strategy: ClipOverlapStrategy = ClipOverlapStrategy.fadeout,
        clip_preload_blocks: int = 100,
        blocksize: int = 4096,
        buffersize: int = 24,
        ):
    logging.basicConfig(level=logging.DEBUG)
    languages = get_languages(clips_dir, clip_extension, clip_preload_blocks * blocksize)
    devices =  get_devices(sound_device_type)
    key_to_language = {key_name: language_name for language_name, key_name in keys_by_language.items()}
    clip_player = ClipPlayer(
            languages,
            devices,
            key_to_language,
            fallback_time=fallback_time,
            fadeout_length=fadeout_length,
            clip_overlap_strategy=clip_overlap_strategy,
            blocksize=blocksize,
            buffersize=buffersize,
        )
    # TESTING code:
    clip_player.play_random_language()
    def loop():
        while True:
            time.sleep(120)
    threading.Thread(target=loop).start()


if __name__ == "__main__":
    typer.run(main)
