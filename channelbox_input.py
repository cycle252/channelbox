"""Platform input adapters; importing the controller needs neither pynput nor evdev."""
from __future__ import annotations

import logging
import select
import sys
import threading
import time
from pathlib import Path

LOG = logging.getLogger("channelbox.input")
LINUX_KEYS = {
    "KEY_UP": "up", "KEY_CHANNELUP": "up", "KEY_PAGEUP": "up",
    "KEY_DOWN": "down", "KEY_CHANNELDOWN": "down", "KEY_PAGEDOWN": "down",
    "KEY_LEFT": "left", "KEY_RIGHT": "right",
    "KEY_ENTER": "enter", "KEY_KPENTER": "enter", "KEY_OK": "enter",
    "KEY_ESC": "esc", "KEY_EXIT": "esc", "KEY_F5": "f5",
    "KEY_VOLUMEUP": "v", "KEY_VOLUMEDOWN": "x", "KEY_MUTE": "m",
    "KEY_PLAYPAUSE": "p", "KEY_INFO": "i", "KEY_EPG": "b",
    **{f"KEY_{char.upper()}": char for char in "vxcmpigfabdh0123456789"},
    **{f"KEY_KP{digit}": digit for digit in "0123456789"},
}
REPEAT_KEYS = {"up", "down", "left", "right", "v", "x"}


def linux_key(name: str, value: int) -> str | None:
    """Ignore releases and held toggle/digit keys, preserving real repeated presses."""
    key = LINUX_KEYS.get(name)
    if value == 1 or (value == 2 and key in REPEAT_KEYS):
        return key
    return None


def macos_key(key, keyboard) -> str | None:
    for name in ("up", "down", "left", "right", "enter", "esc", "f5"):
        if key == getattr(keyboard.Key, name, None):
            return name
    char = getattr(key, "char", None)
    return char.lower() if char else None


class LinuxListener(threading.Thread):
    def __init__(self, callback, stop_event, selector="flirc", grab=False):
        super().__init__(name="channelbox-input", daemon=True)
        if grab and selector == "auto":
            raise ValueError("Select the FLIRC device explicitly before using --grab-input")
        try:
            import evdev
        except ImportError as exc:
            raise RuntimeError("Install Linux input support: sudo apt install python3-evdev") from exc
        self.evdev = evdev
        self.callback = callback
        self.stop_event = stop_event
        self.selector = selector
        self.grab = grab
        self.devices = {}
        self.last_repeat = {}
        self.warned_missing = False

    def stop(self):
        self.stop_event.set()

    def _matches(self, path, name):
        if self.selector == "auto":
            return True
        if self.selector.startswith("/"):
            return Path(self.selector).resolve() == Path(path).resolve()
        return self.selector.lower() in name.lower()

    def _close(self, path):
        device = self.devices.pop(path)
        device.close()  # Closing also releases an exclusive grab.

    def rescan(self):
        available = set(self.evdev.list_devices())
        for path in list(self.devices):
            if path not in available:
                self._close(path)
        interesting = {getattr(self.evdev.ecodes, name) for name in LINUX_KEYS
                       if hasattr(self.evdev.ecodes, name)}
        for path in sorted(available):
            if path in self.devices:
                continue
            device = None
            try:
                device = self.evdev.InputDevice(path)
                keys = set(device.capabilities().get(self.evdev.ecodes.EV_KEY, []))
                if not self._matches(path, device.name) or not keys.intersection(interesting):
                    device.close()
                    continue
                if self.grab:
                    device.grab()
                self.devices[path] = device
                LOG.info("Listening to %s (%s)", path, device.name)
            except OSError as exc:
                if device:
                    device.close()
                LOG.debug("Cannot use %s: %s", path, exc)
        if not self.devices and not self.warned_missing:
            LOG.warning("No accessible remote matches %r. Check evtest, input permissions, and --input-device; waiting for hotplug.", self.selector)
            self.warned_missing = True
        elif self.devices:
            self.warned_missing = False

    def handle_event(self, event):
        if event.type != self.evdev.ecodes.EV_KEY:
            return
        names = self.evdev.ecodes.KEY.get(event.code, [])
        names = [names] if isinstance(names, str) else names
        key = next((value for name in names if (value := linux_key(name, event.value))), None)
        if key is None:
            return
        now = time.monotonic()
        if event.value == 2 and now - self.last_repeat.get(key, 0) < 0.25:
            return
        self.last_repeat[key] = now
        self.callback(key)

    def run(self):
        next_scan = 0.0
        try:
            while not self.stop_event.is_set():
                if time.monotonic() >= next_scan:
                    self.rescan()
                    next_scan = time.monotonic() + 5
                if not self.devices:
                    self.stop_event.wait(1)
                    continue
                try:
                    readable, _, _ = select.select(list(self.devices.values()), [], [], 0.5)
                except (OSError, ValueError):
                    for path in list(self.devices):
                        self._close(path)
                    continue
                for device in readable:
                    try:
                        events = list(device.read())
                    except BlockingIOError:
                        continue
                    except OSError:
                        self._close(device.path)
                        continue
                    for event in events:
                        self.handle_event(event)
        finally:
            for path in list(self.devices):
                self._close(path)


def create_listener(backend, callback, stop_event, *, selector="flirc", grab=False):
    if backend == "auto":
        backend = "macos" if sys.platform == "darwin" else "linux" if sys.platform.startswith("linux") else "unsupported"
    if backend == "linux":
        return LinuxListener(callback, stop_event, selector, grab)
    if backend == "macos":
        try:
            from pynput import keyboard
        except ImportError as exc:
            raise RuntimeError("Install macOS input support: python3 -m pip install pynput") from exc

        def on_press(key):
            value = macos_key(key, keyboard)
            if value:
                callback(value)

        return keyboard.Listener(on_press=on_press)
    raise RuntimeError(f"Unsupported input backend: {backend}")
