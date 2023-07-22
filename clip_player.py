from dataclasses import dataclass, field
from enum import Enum
import logging
import queue
import random
import threading
import time

import numpy as np
from pynput import keyboard
import sounddevice as sd


from language import Language, LanguageThread
from audio_device import AudioDevice

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
    current_playback_thread: LanguageThread|None = None
    playback_stream: sd.OutputStream|None = None
    fallback_timer: threading.Timer|None = None
    fallback_lock: threading.RLock = field(default_factory=lambda: threading.RLock())
    fadeout_start_time: int|None = None
    fadeout_thread: threading.Thread|None = None
    preempted_threads: set[int] = field(default_factory=lambda: set())
    fadeout_lock: threading.RLock = field(default_factory=lambda: threading.RLock())
    preemption_lock: threading.RLock = field(default_factory=lambda: threading.RLock())
    fadeout: bool = False
    buffersize: int = 1024
    blocksize: int = 1024

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
        if self.current_playback_thread and self.current_playback_thread.is_alive():
            if self.current_playback_thread.language == language:
                logging.info(f"Already playing {language.name}, skipping this invocation.")
                return
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
                self.fadeout = False
                self.fadeout_start_time = None
                self.fadeout_thread = None
                with self.preemption_lock:
                    if threading.current_thread().native_id in self.preempted_threads:
                        logging.debug(f"Thread {threading.current_thread().native_id} was pre-empted, skipping playback.")
                        # This thread was preempted while waiting to play, just return without playing
                        return
            fadeout_curve = np.linspace(1.0, 0, self.fadeout_length * language.clip_samplerate)
            device = self.get_next_device()
            play_queue = queue.Queue(maxsize=self.buffersize)
            playback_finished = threading.Event()
            def callback(outdata, frames, stream_time, status):
                if status:
                    logging.warn(status)
                try:
                    frames_to_play = play_queue.get_nowait()
                except queue.Empty:
                    logging.error('Buffer is empty: increase buffersize?')
                    raise sd.CallbackAbort
                try:
                    chunksize = len(frames_to_play)
                    buffersize = outdata.shape[0]
                    outdata[:chunksize, 1 - device.channel] = 0      
                    with self.fadeout_lock:
                        if self.fadeout:
                            current_time = time.time()
                            ms_since_fadeout_start = current_time - self.fadeout_start_time
                            frames_since_fadeout_start = max(0, int(ms_since_fadeout_start * language.clip_samplerate))
                            fadeout_amounts = fadeout_curve[frames_since_fadeout_start:frames_since_fadeout_start + chunksize]
                            num_fadeout_frames = fadeout_amounts.shape[0]
                            if  num_fadeout_frames < chunksize:
                                fadeout_amounts = np.pad(fadeout_amounts, (0, chunksize - num_fadeout_frames))
                            frames_to_play *= fadeout_amounts
                    outdata[:chunksize, device.channel] = frames_to_play
                    if chunksize < buffersize:
                        logging.info(f"DEVICE: {device} :: {language.name} end reached, shutting stream down.")
                        outdata[chunksize:,:] = 0
                        raise sd.CallbackStop()
                    with self.fadeout_lock:
                        if self.fadeout and time.time() > self.fadeout_start_time + self.fadeout_length:
                            logging.info(f"DEVICE: {device} :: fadeout time reached, shutting stream down.")
                            self.fadeout = False
                            self.fadeout_start_time = None
                            raise sd.CallbackAbort
                except sd.CallbackStop:
                    raise sd.CallbackStop()
                except sd.CallbackAbort:
                    raise sd.CallbackAbort
                except Exception as e:
                    logging.error(f"Uncaught playback exception: {e.message}")
                    raise sd.CallbackStop()
            with language.clip() as clip_file:
                # First put the pre-loaded frames into the queue
                num_preloaded_blocks = language.preloaded_frames.shape[0] / self.blocksize
                num_preloaded_blocks_to_enqueue = min(num_preloaded_blocks, self.buffersize)
                logging.debug(f"Will enqueue {num_preloaded_blocks_to_enqueue} of the {num_preloaded_blocks} pre-loaded sound blocks to the playback queue.")
                for block_idx in range(num_preloaded_blocks_to_enqueue):
                    play_queue.put_nowait(language.preloaded_frames[block_idx*self.blocksize:(block_idx + 1) * self.blocksize])
                self.playback_stream = sd.OutputStream(samplerate=language.clip_samplerate, device=device.device_index, blocksize=self.blocksize, dtype="float32", channels=2, callback=callback, finished_callback=playback_finished.set)
                if language.light:
                    language.light.on()
                logging.debug("Ready to start playback")
                with self.playback_stream:
                    timeout = self.blocksize * self.buffersize / language.clip_samplerate
                    block_idx = num_preloaded_blocks_to_enqueue
                    if num_preloaded_blocks > block_idx:
                        logging.debug(f"Will load the remaning {num_preloaded_blocks - block_idx} blocks to the queue.")
                    while num_preloaded_blocks > block_idx:
                        data = language.preloaded_frames[block_idx * self.blocksize:(block_idx+1)*self.blocksize]
                        try:
                            play_queue.put(data, timeout=timeout)
                        except queue.Full:
                            logging.warning(f"Queue full for {language.name}")
                            # queue is Full we might've just reached the end of playback
                            break
                        block_idx += 1
                    logging.debug("Start reading from the disk.")
                    while True:
                        data = clip_file.read(self.blocksize, dtype="float32")
                        try:
                            play_queue.put(data, timeout=timeout)
                        except queue.Full:
                            logging.warning(f"Queue full for {language.name}")
                            # queue is Full we might've just reached the end of playback
                            break
                        if data.shape[0] < self.blocksize:
                            logging.warn(f"Last buffer reached for {language.name}")
                            break
                    playback_finished.wait()
            self.playback_stream = None
            if language.light:
                language.light.off()
            with self.fallback_lock:
                logging.debug("Resestting fallback clock")
                self.set_fallback_timer()
        self.current_playback_thread = LanguageThread(language=language, target=_play)
        self.current_playback_thread.start()
        logging.info(f"Started playback thread {self.current_playback_thread.native_id} for {language.name}.")
