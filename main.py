from dataclasses import dataclass, field, InitVar
from enum import Enum
from functools import cache
import logging
from pathlib import Path
import time
import threading
import random

import typer
import soundfile as sf
import sounddevice as sd
import numpy as np

from gpiozero import Button, LED

from interactivity_config import button_pins_by_language, light_pins_by_language

class ClipOverlapStrategy(str, Enum):
    abort = "abort"
    fadeout = "fadeout"

@dataclass
class Language:
    name: str
    clip_path: InitVar[Path]
    light: LED|None = None
    clip: np.ndarray|None = None
    samplerate: int|None = None

    def __post_init__(self, clip_path: Path) -> None:
        self.clip, self.samplerate = sf.read(clip_path)

@dataclass
class AudioDevice:
    device_index: int
    channel: int

    def __post_init__(self):
        print(f"initialized with {self.device_index}:{self.channel}")

    @cache
    def __hash__(self):
        return (self.device_index, self.channel)

@dataclass
class ClipPlayer:
    languages: list[Language]
    devices: list[AudioDevice]
    button_to_language: dict[Button, str] = field(default_factory=lambda: {})
    fallback_time: int = 600 # seconds of silence before random clip is played
    fadeout_length: int = 5 # seconds
    clip_overlap_strategy: ClipOverlapStrategy = ClipOverlapStrategy.fadeout
    last_language: int|None = None
    last_device: int|None = None
    current_playback_thread: threading.Thread|None = None
    playback_stream: sd.OutputStream|None = None
    fallback_timer: threading.Timer|None = None
    fallback_lock: threading.RLock = field(default_factory=lambda: threading.RLock())
    fadeout_start_time: int|None = None
    fadeout_thread: threading.Thread|None = None
    preempted_threads: set[int] = field(default_factory=lambda: set())
    fadeout_lock: threading.RLock = field(default_factory=lambda: threading.RLock())
    fadeout: bool = False

    def __post_init__(self):
        self.set_fallback_timer()
        name_to_language = {language.name: language for language in self.languages}
        for button, language_name in self.button_to_language.items():
            button.when_pressed = lambda: self.play_language(name_to_language[language_name])

    def set_fallback_timer(self):
        with self.fallback_lock:
            if self.fallback_timer:
                self.fallback_timer.cancel()
            logging.info(f"Reset fallback timer to {self.fallback_time}")
            self.fallback_timer = threading.Timer(self.fallback_time, self.play_random_language)
            self.fallback_timer.start()

    def get_next_language(self) -> Language:
        if len(self.languages) > 1:
            language_idx = random.choice(list(set(range(len(self.languages))) - set([self.last_language])))
        else:
            language_idx = 0
        self.last_language = language_idx
        return self.languages[language_idx]

    def get_next_device(self) -> AudioDevice:
        if len(self.devices) > 1:
            device_idx = random.choice(list(set(range(len(self.devices))) - set([self.last_device])))
        else:
            device_idx = 0
        self.last_device = device_idx
        print(f"picked idx {device_idx}")
        print(f"picked device {self.devices[device_idx]}")
        return self.devices[device_idx]

    def play_random_language(self) -> None:
        logging.info("Hit fallback timer, playing a random language")
        self.play_language(self.get_next_language())

    def play_language(self, language: Language) -> None:
        assert language.clip.ndim == 1, "RECEIVED CLIP WITH MULTIPLE CHANNELS"
        if self.current_playback_thread and self.current_playback_thread.is_alive():
            logging.info(f"Already playing a clip")
            match self.clip_overlap_strategy:
                case ClipOverlapStrategy.fadeout:
                    with self.fadeout_lock:
                        logging.info(f"initiating fadeout, to take {self.fadeout_length} seconds.")
                        self.fadeout = True
                        if self.fadeout_thread and self.fadeout_thread.is_alive():
                            # there's already a thread fading out, so throw out the waiting thread.
                            logging.info(f"Pre-empting waiting thread {self.current_playback_thread.native_id}")
                            self.preempted_threads.add(self.current_playback_thread.native_id)
                        else:
                            # nothing is currenty fading out, that means current playback thread is actually playing
                            # start fading it out
                            self.fadeout_start_time = self.playback_stream.time
                            self.fadeout_thread = self.current_playback_thread
                case ClipOverlapStrategy.abort:
                    logging.info(f"Already playing a clip, aborting clip playback.")
                    return
        def _play():
            if self.fadeout_thread:
                logging.info(f"currently fading out, waiting for fadeout")
                self.fadeout_thread.join()
                with self.fadeout_lock:
                    if threading.current_thread().native_id in self.preempted_threads:
                        logging.info(f"Thread {threading.current_thread().native_id} was pre-empted, skipping playback.")
                        # This thread was preempted while waiting to play, just return without playing
                        return
                    self.fadeout = False
                    self.fadeout_start_time = None
                    self.fadeout_threads = []
                logging.info(f"fadeout complete, starting playback")
            global current_frame
            global device
            device = self.get_next_device()
            current_frame = 0
            playback_finished = threading.Event()
            def callback(outdata, frames, time, status):
                global current_frame
                global device
                if status:
                    logging.warn(status)
                chunksize = min(len(language.clip) - current_frame, frames)
                outdata[:chunksize, 1 - device.channel] = 0
                buffer_to_be_played = language.clip[current_frame:current_frame + chunksize]
                with self.fadeout_lock:
                    if self.fadeout:
                        current_frame_length = chunksize / language.samplerate
                        time_since_fadeout_started = time.currentTime - self.fadeout_start_time
                        fadeout_amount_at_frame_start = 1.0 - (time_since_fadeout_started / self.fadeout_length)
                        fadeout_amount_at_frame_end = 1.0 - (time_since_fadeout_started + current_frame_length) / self.fadeout_length
                        fadeout_amounts = np.geomspace(fadeout_amount_at_frame_start, fadeout_amount_at_frame_end, chunksize)
                        buffer_to_be_played *= fadeout_amounts
                outdata[:chunksize, device.channel] = buffer_to_be_played
                if chunksize < frames:
                    logging.info(f"DEVICE: {device} :: {language.name} end reached, shutting stream down.")
                    outdata[chunksize:,:] = 0
                    raise sd.CallbackStop()
                with self.fadeout_lock:
                    if self.fadeout and time.currentTime > self.fadeout_start_time + self.fadeout_length:
                        logging.info(f"DEVICE: {device} :: fadeout time reached, shutting stream down.")
                        self.fadeout = False
                        self.fadeout_start_time = None
                        raise sd.CallbackStop()
                current_frame += chunksize
            self.playback_stream = sd.OutputStream(samplerate=language.samplerate, device=device.device_index, channels=2, callback=callback, finished_callback=playback_finished.set)
            if language.light:
                language.light.on()
            with self.playback_stream:
                playback_finished.wait()
            self.playback_stream = None
            logging.info(f"{language.name} playback complete")
            if language.light:
                language.light.off()
            with self.fallback_lock:
                logging.info("Resestting fallback clock")
                self.set_fallback_timer()
        self.current_playback_thread = threading.Thread(target=_play)
        logging.info(f"Starting playback thread {self.current_playback_thread.native_id} for {language.name}.")
        self.current_playback_thread.start()

def get_devices(name_filter: str) -> list[AudioDevice]:
    devices = sd.query_devices()
    devices = [AudioDevice(device['index'], channel) for device in devices for channel in range(0,2) if name_filter in device["name"]]
    print(f"found devices: {devices}")
    return devices

def get_languages(clips_dir: Path, clip_extension: str) -> list[Language]:
    languages = []
    for clip_path in clips_dir.glob(f"**/*.{clip_extension}"):
        language = clip_path.stem
        if language in light_pins_by_language:
            light = LED(ligt_pins_by_language[language])
        else:
            light = None
        languages.append(Language(language, clip_path, light))
        logging.info(f"Loaded {language}.")
    logging.info(f"Loaded {len(languages)} clips.")
    return languages

def main(
        clips_dir: Path = "clips",
        clip_extension: str = "wav",
        sound_device_type: str = "USB Audio Device",
        fallback_time: int = 5 * 60,
        fadeout_length: int = 5,
        clip_overlap_strategy: ClipOverlapStrategy = ClipOverlapStrategy.fadeout,
        ):
    logging.basicConfig(level=logging.DEBUG)
    languages = get_languages(clips_dir, clip_extension)
    devices =  get_devices(sound_device_type)
    language_name_to_button = {language_name: Button(button_pin) for language_name, button_pin in button_pins_by_language.items()}
    clip_player = ClipPlayer(
            languages,
            devices,
            language_name_to_button,
            fallback_time=fallback_time,
            fadeout_length=fadeout_length,
            clip_overlap_strategy=clip_overlap_strategy
        )
    clip_player.play_random_language()
    time.sleep(1)
    clip_player.play_random_language()
    clip_player.play_random_language()
    clip_player.play_random_language()
    time.sleep(3)
    clip_player.play_random_language()


if __name__ == "__main__":
    typer.run(main)
