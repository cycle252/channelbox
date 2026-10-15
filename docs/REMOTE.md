# Remote controls

Program FLIRC's Full Keyboard profile to emit these keys. FLIRC stores the
mapping on its receiver. The browser guide works without a handheld remote.

| Button/key | Action |
| --- | --- |
| Up / Channel+ | Next channel; previous row in the TV guide |
| Down / Channel− | Previous channel; next row in the TV guide |
| 0–9 | Exact channel number after the short input timeout |
| B | Open/close the TV channel list |
| A | Toggle favorite for the current/highlighted TV channel |
| C | All/Favorites inside the TV list |
| D | Swap current and previous channel |
| Enter / OK | Tune highlighted TV guide row |
| V / media Volume Up | Increase software volume |
| X / media Volume Down | Decrease software volume |
| M | Mute/unmute |
| P | Pause/play |
| I | Channel information |
| Escape | Close guide, or exit player when no guide is open |
| F5 | Reload local playlist |

The TV overlay is a compact channel list. The richer web guide, programme
previews and category browser are separate and are not yet shown as a TV
browser overlay. Volume controls mpv, not the television's hardware volume.
An enabled restart service relaunches the player after Escape; stop the
service for maintenance.

Use `sudo evtest` to inspect FLIRC, then quit evtest before starting playback.
The default selector matches `flirc`; pass `--input-device` to the portable
player launcher for a different name or stable event-device path.
For temporary keyboard testing, use `--input-device auto --no-grab`.

## Exit video playback

Both browser controls have **Exit player**. This cleanly exits the controller
and mpv, even if the TV guide overlay is open. The EPG web server stays running.
Close guide only closes the overlay. With the documented services, a clean
exit stays stopped; restart with `systemctl --user start channelbox.service`
or reboot with desktop autostart configured.
