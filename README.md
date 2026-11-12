# tvmi-rc

A small desktop app that records the live channels of [tvmi.mt](https://tvmi.mt) (TVM, TVM+ and TVMsport+). Record by hand, or
**follow shows**: it reads the TVM / TVM+ schedule, waits for each airing (a little early, a little late) and records it.

> **Legal notice.** tvmi-rc is for personal, private use, such as time-shifting a free broadcast you could watch live. Respect
> the broadcaster's terms and copyright law; you alone are responsible for what you record and what you do with it. No
> warranty. Not affiliated with TVM or Public Broadcasting Services. See [LEGAL.md](LEGAL.md).

## Features

- **One file, no installs.** Standard-library Python with tkinter. `ffmpeg` is optional.
- **Follow shows** by name, on TVM, TVM+ or either, first airing of the day or every airing. Or **record everything**
  (news bulletins can be skipped).
- **Built-in recorder** that downloads the stream itself and backfills missed pieces, so a slow spell or short outage
  leaves no gap. If something is lost for good, a `- report.txt` next to the recording says what and when.
- **Disk check** before every recording; optional `.mkv` conversion (needs ffmpeg).
- **Web page** for your phone or another PC on your network or Tailscale (default port 9001, optional password).
- **Discord notifications** (started, finished, dropped, failed) through a webhook you provide.
- **Debug log**, settings export / import, start at login.
- **Linux and Windows** (macOS is untested).

## Install and run

Needs Python 3.10 or newer with tkinter (included with Python on Windows; on Linux, e.g. `python3-tk`).

```
python3 tvmi-recorder.py
```

On Windows, double-click it, or rename it to `.pyw` for no console window. To use the optional ffmpeg engine or the `.mkv`
option, install ffmpeg or put `ffmpeg` next to the script.

Settings, followed shows and the log live in `~/.config/tvmi-recorder/` (Linux) or `%APPDATA%\TVMi Recorder\` (Windows).
Recordings go to the folder set in the Settings tab (default `~/Videos`):

```
<folder>/<Show>/<Show> - 2026-11-12 2030.ts
```

## Notes

- Schedule times are Maltese local time. With time-zone data (`pip install tzdata` on Windows) daylight saving is handled;
  without it the app assumes the PC clock is on Malta time.
- The web page is reachable from your local network by default and only accepts local, private and Tailscale addresses.
  Set a password in Settings, or turn "reachable from the network" off, if others share your network.
- The site is not an official API. It can change and break the app.

## Licence

GNU General Public License v3.0 or later. See [LICENSE](LICENSE).
