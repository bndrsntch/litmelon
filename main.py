import abc
import logging
import re
import sys
from enum import Enum
from multiprocessing import Pool
from pathlib import Path
import random
import time

import pygame
import typer
import sounddevice as sd

from pynput import keyboard

from god_mode import GodModeHandler
from interactivity_config import keys_by_language, \
    button_by_language, buttons_to_letters
from keypad_gpiozero import MatrixKeypad
from pygame_clip_player import PygameClipPlayer

logging.basicConfig(level=logging.DEBUG)

numba_logger = logging.getLogger("numba")
numba_logger.setLevel(level=logging.WARNING)


class InputReceiver(abc.ABC):
    def __init__(self, clip_player: PygameClipPlayer, god_mode_handler: GodModeHandler):
        self._clip_player = clip_player
        self._god_mode = True
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

    def __init__(
        self,
        clip_player: PygameClipPlayer,
        god_mode_handler: GodModeHandler,
        rowpins: list[int],
        colpins: list[int],
    ):
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
            # MatrixKeypad expects these labels, we could use them, but we don't currently
            labels=[self.LABEL_SYMBOLS[i:i + self._ncols] for i in range(0, self._nbuttons, self._ncols)]
        )
        self._kp.output_format = "coords"
        self._button_to_language = {
            button_coord: language_name
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
        if not self._god_mode and self._clip_player.is_idle:
            self._clip_player.play_random_language()
            return

        for pressed in self._kp.values:
            if not self._god_mode and self._easter_egg_condition(pressed):
                self._god_mode = True
                self._last_god_mode_start = time.time()
                self._clip_player.stop_playing()
                logging.info("easter egg mode")

            if self._god_mode and time.time() - self._last_god_mode_start > 300:
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
                    self._clip_player.play_language(language)
                else:
                    for coords in key_down:
                        self._god_mode_handler.on_press(coords)

                    for coords in key_up:
                        self._god_mode_handler.on_release(coords)


class PynputKeyboardInputReceiver(InputReceiver):
    def __init__(self, clip_player: PygameClipPlayer, god_mode_handler: GodModeHandler):
        super().__init__(clip_player, god_mode_handler)
        self._key_to_language = {
            key_name: language_name
            for language_name, key_name in keys_by_language.items()
        }
        listener = keyboard.Listener(on_release=lambda key: self._on_key_press(key),
                                     on_press=lambda key: self._on_key_release(key))
        listener.start()

    def _check_for_god_mode(self):
        if self._god_mode and time.time() - self._last_god_mode_start > 300:
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
                    self._clip_player.play_language(language)
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
        if not self._god_mode and self._clip_player.is_idle:
            self._clip_player.play_random_language()


class InputReceiverType(str, Enum):
    KEYBOARD = "KEYBOARD"
    BUTTON = "BUTTON"
    BUTTON_MATRIX = "BUTTON_MATRIX"


def _run(
    clips_dir: Path,
    clip_extension: str,
    input_receiver_type: InputReceiverType,
    device_name: str,
    fadeout_length_ms: int,
):
    try:
        pygame.mixer.init(
            # Verified that current clip mp3 and instrumentation wav are 48_000
            # Seems better to just centralize rather than reinitialize the sample
            # rate of the mixer every time we change a track
            48_000,
            size=32,
            channels=1,
            devicename=device_name,
            allowedchanges=0,
        )
        clip_player = PygameClipPlayer(
            clips_dir,
            clip_extension,
            fadeout_length_ms=fadeout_length_ms,
        )
        god_mode_handler = GodModeHandler()
        if input_receiver_type == InputReceiverType.KEYBOARD:
            input_receiver = PynputKeyboardInputReceiver(
                clip_player,
                god_mode_handler,
            )
        elif input_receiver_type == InputReceiverType.BUTTON_MATRIX:
            input_receiver = ButtonMatrixInputReceiver(
                clip_player,
                god_mode_handler,
                [2, 3], [17, 27],
            )
        else:
            raise NotImplementedError

        while True:
            input_receiver.look_for_input()
    except Exception as e:
        print(device_name)
        print(e, file=sys.stderr)


def main(
    clips_dir: Path = "clips",
    clip_extension: str = "mp3",
    sound_device_type_pattern: str = r"bcm2835",
    input_receiver_type: InputReceiverType = InputReceiverType.BUTTON_MATRIX,
    fadeout_length_ms: int = 6000,
):
    logging.basicConfig(level=logging.DEBUG)
    device_names = [
        d["name"] for d in sd.query_devices()
        if re.match(sound_device_type_pattern, d["name"]) is not None
    ]

    with Pool(len(device_names)) as p:
        p.starmap(
            _run,
            [(
                clips_dir,
                clip_extension,
                input_receiver_type,
                device_name,
                fadeout_length_ms
            ) for device_name in device_names]
        )


if __name__ == "__main__":
    typer.run(main)
