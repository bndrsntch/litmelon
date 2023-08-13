#!/usr/bin/env python3

# I have a new idea:
#
# * Create a pseudo-button class that inherits from `HoldMixin` and
#   `InternalDevice`.
# * Make `MatrixKeypad` be a composite device of all these fake buttons.
# * Store the row/col pins outside the `CompositeDevice` stuff.
# * Override `close()` and `closed`. They should act on the row/col pins.
# * Override `__repr__`, if I can think of a better representation.
# * Let the buttons be accessible in multiple ways:
#     * By single index (`CompositeDevice` already provides that).
#     * By (rowno, colno).
#     * By label (`CompositeDevice` provides a limited version of that,
#       because only valid identifiers are accessible), either by
#       attribute or in a dict-like interface.
# * Provide some custom callback code that passes the exact buttons as
#   parameters. This involves passing and/or comparing `_previous_value`
#   with `_last_value`.
#     * Parameters
#         * Current key state
#         * Previous key state
#         * Newly-pressed keys (redundant?)
#         * Newly-released keys (redundant?)
#         * Is it ambiguous now? Was it ambiguous before?
#     * Callbacks
#         * Key press (which ones are now pressed?)
#         * Key release (which ones are now released?)
#         * Key hold (do I need it?)
#         * KeypadChange (one callback to rule them all)
#
# Advantages:
#
# * `HoldMixin` will work out-of-the-box, because it will handle a single
#   button.
# * No one will accidentally touch the hardware pins by accidentally using
#   the `CompositeDevice` interface.
# * Easy-to-use for people who want to make the keypad behave like a
#   keyboard (i.e. one callback for all buttons).
# * Easy-to-use for people who want finer access to individual buttons.
#
# For this new implementation idea, it might be worth reverting to the
# previous commit (the one without HoldMixin).
#
# I'll still need to implement an optional background polling thread.


from collections import defaultdict
from threading import Lock

from gpiozero.devices import CompositeDevice, GPIODevice
from gpiozero.mixins import HoldMixin

class MatrixKeypad(HoldMixin, CompositeDevice):
    # TODO: Convert this into a nicely formatted docstring.
    # TODO: Check if all the docstrings are properly formatted.
    #
    # rows = list of pins (usually 4 pins)
    # cols = list of pins (usually 3 or 4 pins)
    # labels = One of:   list of list  |  list of strings
    #          In fact, it must be a sequence of sequences.
    #          Making it a tuple is good because then it's immutable. But it doesn't really matter.
    #          The dimensions must match rows x cols.
    #          Examples:
    #            ["123A", "456B", "789C", "*0#D"]
    #            [ [1, 2, 3], [4, 5, 6], [7, 8, 9]]
    # output_format = Formats the :attr:`value` in different ways. Check :meth:`_format_value` for the available formats.
    # hold_time = See HoldMixin. Or just copy-paste the description from Button and form ButtonBoard
    # hold_repeat = See HoldMixin. Or just copy-paste the description from Button and form ButtonBoard
    #
    # Debouncing is not implemented here, because we are probing the values at a certain (low enough) frequency.
    #
    # Rant: HoldMixin and EventsMixin have a weird and poorly documented magic method `_wrap_callback`.
    #       Also, those two mixins should call the callbacks passing the new
    #       device state, or let the subclasses provide such feature. Why? Want
    #       to to pass which keys have changed. I'll have to implement it myself.
    def __init__(
            self, rows, cols, labels, *, output_format="labels", hold_time=1,
            hold_repeat=False, pin_factory=None):
        if len(labels) == 0:
            raise ValueError("labels must not be empty")
        if len(labels) != len(rows):
            raise ValueError("labels must have as many elements as rows (pins)")
        if any(len(labelrow) != len(cols) for labelrow in labels):
            raise ValueError("Each element from labels must have the same length as cols (pins)")

        # Should we make a copy of the labels? Or should we just keep a reference?
        # Since we don't use the labels internally for anything, we can just keep a reference.
        self.labels = labels

        self.output_format = output_format
        self.row_pins = [ GPIODevice(pin, pin_factory=pin_factory) for pin in rows ]
        self.col_pins = [ GPIODevice(pin, pin_factory=pin_factory) for pin in cols ]
        super(MatrixKeypad, self).__init__(*self.row_pins, *self.col_pins, pin_factory=pin_factory)

        self._last_read_was_ambiguous = False
        self._last_value = None # Set of (rowno, colno)
        self._previous_value = None # Set of (rowno, colno)

        self._probe_lock = Lock()
        self._reset_pins()

        # Call _fire_events once to set initial state of events
        self._fire_events(self.pin_factory.ticks(), self.is_active)
        self.hold_time = hold_time
        self.hold_repeat = hold_repeat

    @property
    def value(self):
        """
        Queries the keypad to read a new value.

        When reacting from a callback, please consider :attr:`last_value` instead.
        """
        return self._format_value(self._read())

    @value.setter
    def value(self, value):
        pass

    @property
    def last_value(self):
        """
        Returns the last known value from the keypad.

        This gets updated automatically each time :attr:`value` is accessed.
        (Which also happens each time :attr:`values` is consumed.)
        """
        return self._format_value(self._last_value)

    @property
    def is_active(self):
        return self._last_value and len(self._last_value) > 0

    def _format_value(self, set_of_tuples):
        """
        Formats the internal value format to an easier format to the end-user.

        Possible formats:

        * labels: Returns a frozenset of the labels of the pressed buttons.
                  e.g. { "A", "B" }  # If the labels are strings
                  e.g. { 4, 7 }      # If the labels are numbers
        * coords: Returns a frozenset of the 0-based coords (row, col) of the pressed buttons.
                  e.g. { (0, 0), (2, 3) }
        * rowfirstsequence:
        * colfirstsequence:
                  Return a tuple of the state of each button.
                  Useful for setting as a source of a LEDBoard.
                  e.g. (False, False, True, False, False, False, False, False, False)
        """
        if self.output_format in "coords":
            return frozenset(set_of_tuples)
        elif self.output_format in "labels":
            return frozenset(self.labels[rowno][colno] for (rowno, colno) in set_of_tuples)
        elif self.output_format in "colfirstsequence":
            return tuple(
                (rowno, colno) in set_of_tuples
                for colno in range(len(self.col_pins))
                for rowno in range(len(self.row_pins))
            )
        elif self.output_format in "rowfirstsequence":
            return tuple(
                (rowno, colno) in set_of_tuples
                for rowno in range(len(self.row_pins))
                for colno in range(len(self.col_pins))
            )
        else:
            raise ValueError("Unsupported value for 'output_format': {}".format(self.output_format))

    def _reset_pins(self):
        """
        Resets the pins to be all INPUT, avoiding any short-circuit risk.
        """
        for p in self.row_pins + self.col_pins:
            p.pin.when_changed = None

        for p in self.row_pins + self.col_pins:
            p.pin.input_with_pull('up')
            p.pin.bounce = None

    def _read(self):
        """
        Reads the actual state of the keypad.

        It doesn't matter if it queries rows or columns, as the behavior and
        the number of steps would be the same. This implementation just picked
        one of two approaches.

        Technically, if a matrix keypad includes a direction-enforcing
        component such as a diode on each line, then the choice of rows first
        or cols first (as well as pulling high or low) would lead to different
        results. However, almost all matrix keypads are very simple, containing
        nothing other than the buttons themselves. Such keypads assume the user
        will not press multiple keys at once.

        If you have a "smarter" keypad, feel free to subclass this component to
        adapt to your needs. (And share your changes with the rest of the
        world.)
        """

        with self._probe_lock:
            self._reset_pins()

            pressed = set()
            self._last_read_was_ambiguous = False
            potentially_ambiguous = False
            for rowno, row in enumerate(self.row_pins):
                # Set this row as active, pulling it low.
                row.pin.output_with_state(0)

                # If a button is pressed in this row, we expect to read it from the column.
                for colno, col in enumerate(self.col_pins):
                    if col.pin.state == 0:
                        pressed.add((rowno, colno))

                for rowno2, row2 in enumerate(self.row_pins):
                    if rowno2 != rowno:
                        if row2.pin.state == 0:
                            potentially_ambiguous = True

                row.pin.input_with_pull('up')

            if potentially_ambiguous:
                self._last_read_was_ambiguous = self.is_it_ambiguous(pressed)

            self._previous_value = self._last_value
            self._last_value = pressed
            self._fire_events(self.pin_factory.ticks(), self.is_active)
            return pressed

    @property
    def last_read_was_ambiguous(self):
        """
        Boolean value, returns True if the last known state was ambiguous.

        When there are three or more buttons pressed at the same time, it is
        possible the keypad will return ghost button presses (i.e. it thinks a
        button is pressed while it isn't). If you don't need to deal with this
        large amount of simultaneous button presses, you don't need to worry
        about ambiguity.

        This property is updated any time :meth:`_read` gets called, which
        happens any time :attr:`value` is read or :attr:`values` is consumed.
        Thus, if you're dealing with multiple threads or complex code, this
        property might be outdated by the time you read it. However, it's a
        simple solution for most common use-cases.

        A "better" solution would be to include this information into the
        output value itself, but that makes the output value more complicated
        than what most people need. (Most people just need a single button
        press.)

        If you need to check if a certain value is ambiguous, without any
        racing conditions, just call :meth:`is_it_ambiguous`.
        """
        return self._last_read_was_ambiguous

    def is_it_ambiguous(self, set_of_tuples):
        """
        Given a value in the format of set of tuples of row/col coords, returns
        True if this value is ambiguous.

        Matrix keypads are extremely simple devices: one wire for each row, one
        wire for each column, and a button connecting each row/column pair.
        Thus, if three buttons are pressed in a certain way (sharing both a
        row wire and a column wire), then a fourth button press will be read by
        the circuit, even if that fourth button is not pressed. In such case,
        the value is ambiguous, because the code reads four buttons, but it's
        impossible to know if all four are pressed, or which one of those four
        buttons is not pressed.

        As another way to understand it, any read that contains four buttons as
        corners of a rectangle is ambiguous.

        If you are confused, just remember:

        * Zero buttons pressed are never ambiguous.
        * One button pressed is never ambiguous.
        * Two buttons pressed is never ambiguous.
        * Three buttons read as pressed is never ambiguous.
        * Four or more buttons read as pressed may be ambiguous (i.e. may
        include ghost presses).
        """

        items_per_row = defaultdict(list)
        items_per_col = defaultdict(list)

        for (rowno, colno) in set_of_tuples:
            items_per_row[rowno].append(colno)
            items_per_col[colno].append(rowno)

        for row in items_per_row.values():
            if len(row) > 1:
                for colno in row:
                    if len(items_per_col[colno]) > 1:
                        return True

        return False


if __name__ == "__main__":
    kp = MatrixKeypad(
        # pin 21 is not used in this 3x4 matrix
        rows=[2, 3],
        cols=[17, 27],
        labels=["12", "45"],
    )

    import time
    kp.output_format = "coords"
    while True:
        for value in kp.values:
             print(value)
        time.sleep(.1)

    input('Press Enter to quit.')
