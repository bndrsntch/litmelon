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
from pynput import keyboard

from gpiozero import LED

from interactivity_config import keys_by_language, light_pins_by_language

logging.basicConfig(level=logging.INFO)

class ClipOverlapStrategy(str, Enum):
    """
    Strategies for what to do when a new clip is attempted to play while a clip is already playing.
        abort: let the current clip finish, ignore the key press
        fadeout: trigger a fadeout, queue this clip to start playing after the fadeout. If there are
                    other clips queued for the fadeout of the current clip, skip them.
    """
    abort = "abort"
    fadeout = "fadeout"

@dataclass
class Language:
    """
    Representation of a language that consists of an audio clip (loaded into memory from a file) and
    and option GPIO PIN that controls a relay that controls a light that should light up when the clip
    for this language is playing. The light mappings are read from the interactivity_config.py file.
    """
    name: str
    clip_path: InitVar[Path]
    light: LED|None = None
    clip: np.ndarray|None = None
    samplerate: int|None = None

    def __post_init__(self, clip_path: Path) -> None:
        self.clip, self.samplerate = sf.read(clip_path)
        assert self.clip.ndim == 1, f"TRYING TO LOAD CLIP WITH MULTIPLE CHANNELS FOR: {self.name} -> {clip_path}"

@dataclass
class AudioDevice:
    """
    Representation of a single channel of an eligible system audio output device. Helps us treat
    individual channels of a stereo output device as individual speakers.
    """
    device_index: int
    channel: int

    @cache
    def __hash__(self):
        return (self.device_index, self.channel)

@dataclass
class ClipPlayer:
    """
    The main class responsible of orchestrating the following:
        - Setting up the low-level audio playback threads.
        - Playing clips when their corresponding keys are played.
        - Applying the clip overlap strategy:
            - Either fade out the current clip gradually over the set number of seconds
            - Or ignore the request to play a new clip if a different clip is still playing.
        - Running a separate thread based on a timer that triggers a random language to play
            if no interaction happens for a set amount of time.
    """
    languages: list[Language]
    devices: list[AudioDevice]
    key_to_language: dict[str, str] = field(default_factory=lambda: {})
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
    preemption_lock: threading.RLock = field(default_factory=lambda: threading.RLock())
    fadeout: bool = False

    def __post_init__(self):
        """
        Sets up the key interactions (as defined in interactivity_config.py)
        Starts of the timed thread that plays a random clip if nothing happens for a given amount of time.
        """
        self.set_fallback_timer()
        listener = keyboard.Listener(on_release=lambda key:self.on_key_press(key))
        listener.start()
        self.name_to_language = {language.name: language for language in self.languages}

    def on_key_press(self, key):
        try:
            char = str(key.char)
            if char in self.key_to_language:
                language_name = self.key_to_language[char]
                language = self.name_to_language[language_name]
                self.play_language(language, abort_if_playing=False)
        except AttributeError:
            pass

    def set_fallback_timer(self):
        """
        Helper function to reset the fallback timer thread. Is called at initialization time and every time a clip finishes playing.
        """
        with self.fallback_lock:
            if self.fallback_timer:
                self.fallback_timer.cancel()
            self.fallback_timer = threading.Timer(self.fallback_time, self.play_random_language)
            self.fallback_timer.start()
            logging.info(f"Reset fallback timer to {self.fallback_time}")

    def get_next_language(self) -> Language:
        """
        Get the next language that should be played randomly.
        If there is more than one language available, picks a random language that's different from the last one.
        """
        if len(self.languages) > 1:
            language_idx = random.choice(list(set(range(len(self.languages))) - set([self.last_language])))
        else:
            language_idx = 0
        self.last_language = language_idx
        return self.languages[language_idx]

    def get_next_device(self) -> AudioDevice:
        """
        Get the next output device that should be used for audio playback.
        If there is more than one device avaiablel, picks a random device that's different from the last one.
        """
        if len(self.devices) > 1:
            device_idx = random.choice(list(set(range(len(self.devices))) - set([self.last_device])))
        else:
            device_idx = 0
        self.last_device = device_idx
        return self.devices[device_idx]

    def play_random_language(self) -> None:
        """
        Triggers the playback of a random language. Is meant to  be used by the fallback timer thread.
        Note that this always aborts playback if a different clip happens to be playing when this is called.
        """
        logging.info("Hit fallback timer, attempting to play a random language")
        self.play_language(self.get_next_language(), abort_if_playing=True)

    def play_language(self, language: Language, abort_if_playing:bool) -> None:
        """
        Attempts to play the given language in the system. This consists of the following:
            1. Create a fadeout curve that will be used to fade this clip out if needed:
                - The fadeout curve is an array of coefficients in (0,1) that scale down the value of each sample that plays.
                    The number of frames needed is sample rate (samples per s) * fadeout time (s). We use numpy geomspace to
                    exponentially fade out. The nth element of this array corresponds to how much quieter the nth sample that
                    is played after fadeout is initiated should be.
            2. Check if a different clip is currently playing.
                - if yes, apply the clip overlap strategy:
                    - if this function call or the overall strategy is set to abort when overlapping, return immediately
                    - if the overall strategy is fadeout:
                        i. Check if the currently playing clip is already in fadeout mode and a different clip is queued to play afterwards:
                            - if yes, pre-empt(cancel) the queued clip
                            - if no, set the currently playing clip to fade-out mode
            3. Set up the thread to play this clip:
                -  Define the play_ function that will run in its own thread to play this clip. This function:
                    i. Checks if there is a different clip currently fading out:
                        - TODO: probably want to play a chime here to acknowledge a new clip was queued.
                        - if yes, block until the fadeout of the current clip is complete.
                    ii. Checks if it has been pre-empted while waiting for the fadeout:
                        - if yes, return immediately
                    iii. Sets up the callback function to play the clip using the `sounddevice` library:
                    iv. If any lights are tied to this language, turns them on.
                    v. Starts the `sounddevice` playback using a custom callback function:
                        - The fallback function writes audio data from an input buffer(the clip) to an output buffer(the audio device).
                          It gets repeatedly called by the audiodevice library to keep buffering data from the clip to the device.
                          It is given the output buffer, size of the output buffer, time since clip started playing and any underflow/overflow status.
                          Every time it's invoked it does the following:
                            a. Log any under/overflow issues
                            b. Load the amount of samples that need to be written to the output buffer
                            c. If fadeout is currently happening, compute how much each sample needs to be faded out:
                                - Since we have a fadeout curve already, and the callback tells us which samples we're playing, we l
                                - Multiply the audio data with the coefficients to scale down the signal (this works because we have raw audio data in numpy arrays)
                            d. If the remaining length of the clip is shorter than the buffer length, pad the buffer with 0s.
                            e. If the last samples of the clip were written to the buffer in this call, or if the final fadeout time is reached, signal that playback should stop.
                    vi. Waits for the stream to end.
                    vii. Turns off any associated lights.
                    viii. Resets the fallback timer.
            3. Start the play thread.
        """
        logging.info(f"Start attempt to play {language.name}")
        fadeout_curve = np.linspace(1.0, 0, self.fadeout_length * language.samplerate)
        if self.current_playback_thread and self.current_playback_thread.is_alive():
            if abort_if_playing or self.clip_overlap_strategy == ClipOverlapStrategy.abort:
                logging.debug(f"Already playing a clip, aborting clip playback.")
                return
            else:
                logging.debug(f"Already playing a clip, start fadeout if needed and wait.")
                with self.fadeout_lock:
                    self.fadeout = True
                    if self.fadeout_thread and self.fadeout_thread.is_alive():
                        # there's already a thread fading out, so throw out the waiting thread.
                        logging.debug(f"Pre-empting waiting thread {self.current_playback_thread.native_id}")
                        with self.preemption_lock:
                            self.preempted_threads.add(self.current_playback_thread.native_id)
                    else:
                        # nothing is currenty fading out, that means current playback thread is actually playing
                        # start fading it out
                        logging.debug(f"Set fadeout start time for active playback thread {self.current_playback_thread.native_id}")
                        self.fadeout_start_time = time.time()
                        self.fadeout_thread = self.current_playback_thread
        def _play():
            if self.fadeout_thread:
                logging.debug(f"Playback thread {self.fadeout_thread.native_id} is fading out, wait!")
                self.fadeout_thread.join()
                logging.debug(f"Playback thread {self.fadeout_thread.native_id} is done fading out, go ahead!!")
                self.fadeout = False
                self.fadeout_start_time = None
                self.fadeout_thread = None
                with self.preemption_lock:
                    if threading.current_thread().native_id in self.preempted_threads:
                        logging.debug(f"Thread {threading.current_thread().native_id} was pre-empted, skipping playback.")
                        # This thread was preempted while waiting to play, just return without playing
                        return
            global current_frame
            global device
            device = self.get_next_device()
            current_frame = 0
            playback_finished = threading.Event()
            def callback(outdata, buffersize, stream_time, status):
                try:
                    global current_frame
                    global device
                    if status:
                        logging.warn(status)
                    chunksize = min(len(language.clip) - current_frame, buffersize)
                    outdata[:chunksize, 1 - device.channel] = 0
                    buffer_to_be_played = language.clip[current_frame:current_frame + chunksize]
                    with self.fadeout_lock:
                        if self.fadeout:
                            current_time = time.time()
                            ms_since_fadeout_start = current_time - self.fadeout_start_time
                            frames_since_fadeout_start = max(0, int(ms_since_fadeout_start * language.samplerate))
                            fadeout_amounts = fadeout_curve[frames_since_fadeout_start:frames_since_fadeout_start + chunksize]
                            num_fadeout_frames = fadeout_amounts.shape[0]
                            num_buffer_frames = buffer_to_be_played.shape[0]
                            if  num_fadeout_frames < num_buffer_frames:
                                fadeout_amounts = np.pad(fadeout_amounts, (0, num_buffer_frames - num_fadeout_frames))
                            buffer_to_be_played *= fadeout_amounts
                    outdata[:chunksize, device.channel] = buffer_to_be_played
                    if chunksize < buffersize:
                        logging.info(f"DEVICE: {device} :: {language.name} end reached, shutting stream down.")
                        outdata[chunksize:,:] = 0
                        raise sd.CallbackStop()
                    with self.fadeout_lock:
                        if self.fadeout and time.time() > self.fadeout_start_time + self.fadeout_length:
                            logging.info(f"DEVICE: {device} :: {language.name} fadeout time reached, shutting stream down.")
                            self.fadeout = False
                            self.fadeout_start_time = None
                            raise sd.CallbackStop()
                    current_frame += chunksize
                except sd.CallbackStop:
                    raise sd.CallbackStop
                except Exception as e:
                    logging.error(f"DEVICE: {device} :: {language.name} playback error: {e.message}")
                    raise sd.CallbackStop()
            self.playback_stream = sd.OutputStream(samplerate=language.samplerate, device=device.device_index, channels=2, callback=callback, finished_callback=playback_finished.set)
            if language.light:
                language.light.on()
            with self.playback_stream:
                playback_finished.wait()
            self.playback_stream = None
            if language.light:
                language.light.off()
            with self.fallback_lock:
                logging.debug("Resestting fallback clock")
                self.set_fallback_timer()
        self.current_playback_thread = threading.Thread(target=_play)
        self.current_playback_thread.start()
        logging.info(f"Started playback thread {self.current_playback_thread.native_id} for {language.name}.")

def get_devices(name_filter: str) -> list[AudioDevice]:
    """
    Use the `sounddevice` library to construct a list of all audio devices whose names contain the `name_filter` as a substring.
    """
    devices = sd.query_devices()
    devices = [AudioDevice(device['index'], channel) for device in devices for channel in range(0,2) if name_filter in device["name"]]
    logging.info(f"Initialized with devices: {devices}")
    return devices

def get_languages(clips_dir: Path, clip_extension: str) -> list[Language]:
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
        languages.append(Language(language, clip_path, light))
        logging.info(f"Loaded {language}.")
    logging.info(f"Loaded {len(languages)} clips.")
    return languages

def main(
        clips_dir: Path = "clips",
        clip_extension: str = "mp3",
        sound_device_type: str = "default",
        fallback_time: int = 30,
        fadeout_length: int = 10,
        clip_overlap_strategy: ClipOverlapStrategy = ClipOverlapStrategy.fadeout,
        ):
    logging.basicConfig(level=logging.DEBUG)
    languages = get_languages(clips_dir, clip_extension)
    devices =  get_devices(sound_device_type)
    key_to_language = {key_name: language_name for language_name, key_name in keys_by_language.items()}
    clip_player = ClipPlayer(
            languages,
            devices,
            key_to_language,
            fallback_time=fallback_time,
            fadeout_length=fadeout_length,
            clip_overlap_strategy=clip_overlap_strategy
        )
    # TESTING code:
    clip_player.play_random_language()
    def loop():
        while True:
            time.sleep(120)
    threading.Thread(target=loop).start()


if __name__ == "__main__":
    typer.run(main)
