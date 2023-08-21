import abc
import logging
import typing as t
from enum import Enum
from pathlib import Path
import random
import time

import typer
import sounddevice as sd

from gpiozero import LED

from pynput import keyboard

from god_mode import GodModeHandler
from language import Language
from audio_device import AudioDevice
from clip_player import ClipOverlapStrategy, ClipPlayer
from interactivity_config import button_numbers_by_language, keys_by_language, light_pins_by_language, \
    button_by_language, buttons_to_letters
from keypad_gpiozero import MatrixKeypad

logging.basicConfig(level=logging.DEBUG)


def get_devices(name_filter: str) -> list[AudioDevice]:
    """
    Use the `sounddevice` library to construct a list of all audio devices whose names contain the `name_filter` as a substring.
    """
    all_devices = sd.query_devices()
    devices = [AudioDevice(device['index'], channel, device['name'])
               for device in all_devices for channel in range(0, 2) if name_filter in device["name"]]
    logging.info(f"Initialized with devices: {devices}")
    if len(devices) == 0:
        logging.error(f"Available devices {all_devices}")
        raise Exception(f"No devices found with filter [{name_filter}]")

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
    if len(languages) == 0:
        raise Exception(f"No [{clip_extension}] files found in [{clips_dir}]")
    return languages


class InputReceiver(abc.ABC):
    def __init__(self, clip_player: ClipPlayer, god_mode_handler: GodModeHandler):
        self._clip_player = clip_player
        self._god_mode = False
        self._god_mode_handler = god_mode_handler
        self._last_god_mode_start = None

    def look_for_input(self) -> None:
        """
        This is the main event loop to look for new events for input types that do not
        support callbacks.
        """
        pass


class ButtonMatrixInputReceiver(InputReceiver):
    """
    Receives input from a diode button matrix.

    When multiple keys are pressed, the first key pressed is used, otherwise
    if they were pressed simultaneously, a random key is chosen.
    """
    LABEL_SYMBOLS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def __init__(self, clip_player: ClipPlayer, god_mode_handler: GodModeHandler, rowpins: list[int], colpins: list[int],
                 languages: list[Language]):
        super().__init__(clip_player, god_mode_handler)
        self._rowpins = rowpins
        self._colpins = colpins
        self._nrows = len(rowpins)
        self._ncols = len(colpins)
        self._nbuttons = self._nrows * self._ncols
        self._pressed = None  # State for which button is currently pressed.
        self._kp = MatrixKeypad(
            rows=rowpins,
            cols=colpins,
            # MatrixKeypad expects these labels, we could use them but we don't currently
            labels=[self.LABEL_SYMBOLS[i:i + self._ncols] for i in range(0, self._nbuttons, self._ncols)]
        )
        self._kp.output_format = "coords"
        languages_by_name = {language.name: language for language in languages}
        self._button_to_language = {
            button_coord: languages_by_name[language_name]
            for language_name, button_coord in button_by_language.items()
        }
        self._key_states = {
            (0, 0): False,
            (0, 1): False,
            (0, 2): False,
            (0, 3): False,
            (0, 4): False,
            (0, 5): False,
            (0, 6): False,
            (0, 7): False,
            (1, 0): False,
            (1, 1): False,
            (1, 2): False,
            (1, 3): False,
            (1, 4): False,
            (1, 5): False,
            (1, 6): False,
            (1, 7): False,
            (2, 0): False,
            (2, 1): False,
            (2, 2): False,
            (2, 3): False,
            (2, 4): False,
            (2, 5): False,
            (2, 6): False,
            (2, 7): False,
        }

    def _easter_egg_condition(self, pressed) -> bool:
        """ Easter egg when all buttons are pressed. Reset to bring it back to normal.
        """
        return len(pressed) == self._nbuttons

    def look_for_input(self) -> None:
        for pressed in self._kp.values:
            if not self._god_mode and self._easter_egg_condition(pressed):
                self._god_mode = True
                self._last_god_mode_start = time.time()
                # TODO: GET THE CLIP PLAYER TO STOP
                logging.info("easter egg mode")

            if self._god_mode and time.time() - self._last_god_mode_start > ClipPlayer.fallback_time:
                self._god_mode = False
                self._last_god_mode_start = None
                self._clip_player.play_random_language()
                logging.info("easter egg mode expired")

            if self._pressed not in pressed:
                self._pressed = None

            if self._pressed is None and len(pressed) > 0:
                key_down = []
                key_up = []
                for coords, is_on in self._key_states.items():
                    if is_on:
                        if coords in pressed:
                            continue
                        else:
                            key_up.append(coords)
                    elif coords in pressed:
                        key_down.append(coords)

                    self._key_states[coords] = coords in pressed

                if not self._god_mode:
                    self._pressed = random.choice(list(pressed))
                    language = self._button_to_language[self._pressed]
                    self._clip_player.play_language(language, abort_if_playing=False)
                else:
                    for coords in key_down:
                        self._god_mode_handler.on_press(coords)

                    for coords in key_up:
                        self._god_mode_handler.on_release(coords)


class PynputKeyboardInputReceiver(InputReceiver):
    def __init__(self, clip_player: ClipPlayer, god_mode_handler: GodModeHandler, languages: list[Language]):
        super().__init__(clip_player, god_mode_handler)
        languages_by_name = {language.name: language for language in languages}
        self._key_to_language = {
            key_name: languages_by_name[language_name]
            for language_name, key_name in keys_by_language.items()
        }
        listener = keyboard.Listener(on_release=lambda key: self._on_key_press(key),
                                     on_press=lambda key: self._on_key_release(key))
        listener.start()

    def _check_for_god_mode(self):
        if self._god_mode and time.time() - self._last_god_mode_start > ClipPlayer.fallback_time:
            self._god_mode = False
            self._last_god_mode_start = None
            self._clip_player.play_random_language()
            logging.info("easter egg mode expired")

    def _on_key_press(self, key) -> None:
        char = getattr(key, "char", None)
        if char is not None:
            if not self._god_mode:
                if str(char) in self._key_to_language:
                    language = self._key_to_language[char]
                    self._clip_player.play_language(language, abort_if_playing=False)
            else:
                for k, v in buttons_to_letters.items():
                    if str(char) == v:
                        self._god_mode_handler.on_press(k)

    def _on_key_release(self, key) -> None:
        char = getattr(key, "char", None)
        if char is not None:
            if self._god_mode:
                for k, v in buttons_to_letters.items():
                    if str(char) == v:
                        self._god_mode_handler.on_release(k)

    def look_for_input(self) -> None:
        pass


class InputReceiverType(str, Enum):
    KEYBOARD = "KEYBOARD"
    BUTTON = "BUTTON"
    BUTTON_MATRIX = "BUTTON_MATRIX"


def main(
        clips_dir: Path = "clips",
        clip_extension: str = "wav",
        sound_device_type: str = "bcm2835",
        input_receiver_type: InputReceiverType = InputReceiverType.BUTTON_MATRIX,
        fallback_time: int = 10,
        fadeout_length: int = 6,
        clip_overlap_strategy: ClipOverlapStrategy = ClipOverlapStrategy.fadeout,
        clip_preload_blocks: int = 250,
        blocksize: int = 8192,
        buffersize: int = 12,
):
    logging.basicConfig(level=logging.DEBUG)
    languages = get_languages(clips_dir, clip_extension, clip_preload_blocks * blocksize)
    devices = get_devices(sound_device_type)

    clip_player = ClipPlayer(
        languages,
        devices,
        fallback_time=fallback_time,
        fadeout_length=fadeout_length,
        clip_overlap_strategy=clip_overlap_strategy,
        blocksize=blocksize,
        buffersize=buffersize,
    )

    # TODO: FIGURE OUT HOW TO GET THIS OVER MULTIPLE DEVICES
    god_mode_handler = GodModeHandler(devices[0].name)
    if input_receiver_type == InputReceiverType.KEYBOARD:
        input_receiver = PynputKeyboardInputReceiver(
            clip_player,
            god_mode_handler,
            languages,
        )
    elif input_receiver_type == InputReceiverType.BUTTON_MATRIX:
        input_receiver = ButtonMatrixInputReceiver(
            clip_player,
            god_mode_handler,
            [2, 3], [17, 27],
            languages,
        )
    else:
        raise NotImplementedError

    # TESTING code:
    while True:
        input_receiver.look_for_input()


if __name__ == "__main__":
    typer.run(main)
