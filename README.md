# Loss In Translation

This repo contains the sound and light control code for the Loss In Translation art installation.

## Installation

In your favorite Python3.11 environment:
```
pip install -r requirements.txt
```

## Audio Clips

The repo comes with a clip in turkish in the `clips` directory for easy testing.
Copy more `.wav` files into the `clips` directory to test with more languages. Make sure the clips are MONO.

## Running

The sound aspect can be tested on any computer.
Invoke with

```
python main.py --sound_device_type "default"
```

Available CLI options are:

- `clips-dir (clips/)`: where to look for audio clips
- `clip-extension (wav)`: what file extension to load
- `sound-device-type (USB Audio Device)`: which audio devices should be loaded, only those with this argument as part of their name are loaded. See Audio Devices below
- `fallback-time (300)`: Number of seconds of inactivity before the system starts playing a random clip by itself. Lower it to test things.
- `fadeout-length (20)`: Number of seconds of fadeout to use when switching to a new clip. Only used if clip overlap strategy is fadeout.
- `clip-overlap-strategy (*fadeout*|abort)`: What to do if a new clip is requested (via button press) while a clip is already playing. Fadeout fades the current clip out and starts playing the new clip, abort does nothing, not allowing new clips to be triggered until the current clip ends.

## Audio Devices

This uses your system audio devices, but you need to configure which device(s) to use.
You can view the available devices by runnning the following:

```
python -c "import sounddevice; print(sounddevice.query_devices())"
```
You can pass the name of any of these devices (or a substring of one, or common to multiple) to use that/those devices.
`default` is a safe choice on many systems, as well as `Speakers` on a MacBook or `Headphones` on a Raspberry Pi.

For final art installation where custom USB audio cards are used, find the common substring of these devices' names and pass that to load all of them, but only them.
