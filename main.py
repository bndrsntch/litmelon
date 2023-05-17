from dataclasses import dataclass
import logging
from pathlib import Path
import time
import threading
import random
import random

import typer
import soundfile as sf
import sounddevice as sd
import numpy as np

from gpiozero import Button, LED

from interactivity_config import button_pins_by_language, light_pins_by_language

@dataclass
class AudioDevice:
    device_index: int
    channel: int

    def __str__(self) -> str:
        device_dict = sd.query_devices(device=self.device_index)
        return f"Name: {device_dict['name']} channel: {self.channel}"

    def __hash__(self):
        return (self.__str__().__hash__())

class Language:
    def __init__(
            self,
            name: str,
            clip_path: Path,
            button: Button | None,
            light: LED | None,
            ) -> None:
        self.name = name
        self.clip_path = clip_path
        self.button = button
        self.light = light
        self.last_played = .0
        if self.button:
            self.button.when_pressed = lambda: self.play

    def play(self):
        global DEVICES
        global PLAYING_LOCK
        global FALLBACK_PLAY_TIMER
        global PLAYBACK_THREAD
        global LAST_LANGUAGE
        global LAST_SPEAKER
        with PLAYING_LOCK:
            if PLAYBACK_THREAD and PLAYBACK_THREAD.is_alive():
                logging.info(f"Tried to play {self.name} but skipping because a clip is already playing")
                return
            if FALLBACK_PLAY_TIMER:
                logging.debug("Cancel the fallback timer")
                FALLBACK_PLAY_TIMER.cancel()
            if PLAYBACK_THREAD:
                PLAYBACK_THREAD.join()
        if len(DEVICES) > 1:
            speaker_idx = random.choice(list(set(range(len(DEVICES))) - set([LAST_SPEAKER])))
        else:
            speaker_idx = 0
        LAST_SPEAKER = speaker_idx
        speaker = DEVICES[speaker_idx]
        logging.info(f"Playing {self.name} on {speaker}")
        clip, samplerate = sf.read(self.clip_path)
        def _play():
            global FALLBACK_PLAY_TIMER
            if self.light:
                self.light.on()
            sd.play(clip, samplerate=samplerate, mapping=[speaker.channel], device=speaker.device_index, blocking=True)
            if self.light:
                self.light.off()
            with PLAYING_LOCK:
                LAST_LANGUAGE = self.name
                logging.debug("Setting fallback play timer")
                FALLBACK_PLAY_TIMER = threading.Timer(TIMEOUT_FALLBACK, trigger_random_language)
                FALLBACK_PLAY_TIMER.start()
        PLAYBACK_THREAD = threading.Thread(target=_play)
        PLAYBACK_THREAD.start()

def get_devices(name_filter: str) -> list[AudioDevice]:
    devices = sd.query_devices()
    devices = [AudioDevice(device['index'], channel) for device in devices for channel in range(1,3) if name_filter in device["name"]]
    (logging.info("Found speaker: {speaker}") for speaker in devices)
    return devices

def get_languages(clips_dir: Path, clip_extension: str) -> list[Language]:
    languages = []
    for clip_path in clips_dir.glob(f"**/*.{clip_extension}"):
        language = clip_path.stem
        if language not in button_pins_by_language:
            logging.warning(f"Found clip for {language} but it is not configured with a button GPIO pin.")
            button = None
        else:
            button = Button(button_pins_by_language[language])
        if language in light_pins_by_language:
            light = LED(ligt_pins_by_language[language])
        else:
            light = None
        languages.append(Language(language, clip_path, button, light))
        logging.info(f"Loaded {language}.")
    logging.info(f"Loaded {len(languages)} clips.")
    return languages

def trigger_random_language():
    global PLAYING_LOCK
    with PLAYING_LOCK:
        logging.debug("Hit fallback, time to play a random language")
        if PLAYBACK_THREAD and PLAYBACK_THREAD.is_alive():
            logging.debug("Snap! Someone started playing a language already, giving up.")
            return
        if len(LANGUAGES) > 1:
            next_language = random.choice((set(LANGUAGES) - set([LAST_LANGUAGE])))
        else:
            next_language = LANGUAGES[0]
        logging.debug(f"Randomly picked {next_language.name} to play.")
        next_language.play()



PLAYING_LOCK = threading.RLock()
PLAYBACK_THREAD: threading.Thread|None = None
FALLBACK_PLAY_TIMER: threading.Thread|None = None

LAST_SPEAKER: int|None = None
LAST_LANGUAGE: str|None = None

DEVICES: list[AudioDevice] = []
LANGUAGES: list[Language] = []

def main(
        clips_dir: Path = "clips",
        clip_extension: str = "wav",
        sound_device_type: str = "USB Audio Device",
        play_timeout: int = 5 * 60,
        ):
    global LANGUAGES
    global DEVICES
    global TIMEOUT_FALLBACK
    global FALLBACK_PLAY_TIMER
    logging.basicConfig(level=logging.DEBUG)
    LANGUAGES = get_languages(clips_dir, clip_extension)
    DEVICES = get_devices(sound_device_type)
    TIMEOUT_FALLBACK = play_timeout
    FALLBACK_PLAY_TIMER = threading.Timer(TIMEOUT_FALLBACK, trigger_random_language)
    FALLBACK_PLAY_TIMER.start()

if __name__ == "__main__":
    typer.run(main)
