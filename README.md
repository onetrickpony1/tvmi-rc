# tvmi-rc

A small desktop app that records the live TV and radio channels of [tvmi.mt](https://tvmi.mt): TVM, TVM+, TVMsport+, Magic,
Radju Malta and Radju Malta 2. Record by hand, or follow shows: it reads each channel's schedule, waits for each airing (a
little early, a little late) and records it.

> **Legal notice.** tvmi-rc is for personal, private use, such as time-shifting a free broadcast you could watch live. Respect
> the broadcaster's terms and copyright law; you alone are responsible for what you record and what you do with it. No
> warranty. Not affiliated with TVM or Public Broadcasting Services. See [LEGAL.md](LEGAL.md).

## Features

- **One file, standard-library Python** with tkinter. `ffmpeg` is optional. A guided installer is included.
- **Schedules for every channel:** TVM, TVM+, TVMsport+, Magic, Radju Malta and Radju Malta 2. **Follow shows** by name, on one
  channel or any, first airing of the day or every airing. Or **record all shows** on TVM and TVM+ (news bulletins can be skipped).
- **Built-in recorder** that downloads the stream itself and backfills missed pieces, so a slow spell or short outage
  leaves no gap. If something is lost for good, a `- report.txt` next to the recording says what and when.
- **Disk check** before every recording; optional `.mkv` conversion (needs ffmpeg).
- **Web page** for your phone or another PC on your network or Tailscale (default port 9001, optional password).
- **Discord notifications** (started, finished, dropped, failed) through a webhook you provide.
- **Debug log**, settings export / import, start at login.
- **Linux and Windows** (macOS is untested).

## Install

One line, no admin rights needed. It checks for Python with tkinter (required), ffmpeg (optional) and, on Windows, the
time-zone package, and **asks before installing anything that is missing**. It then saves the app, adds a `tvmi-rc` command
and offers a menu icon.

**Windows** (PowerShell):
```
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/onetrickpony1/tvmi-rc/main/install.ps1 | iex"
```

**Linux and macOS** (Terminal):
```
curl -LsSf https://raw.githubusercontent.com/onetrickpony1/tvmi-rc/main/install.sh | sh
```

Run the same line again to update. Or skip the installer: it is one file, so `python3 tvmi-rc.py` works wherever
Python 3.10+ with tkinter is installed.

## Use

Start it from the menu, or run `tvmi-rc`. Settings, followed shows and the log live in `~/.config/tvmi-recorder/` (Linux)
or `%APPDATA%\TVMi Recorder\` (Windows). Recordings go to the folder set in the Settings tab (default `~/Videos`):

```
<folder>/<Show>/<Show> - 2026-11-12 2030.ts
```

## Notes

- Schedule times are Maltese local time. With time-zone data (`pip install tzdata` on Windows) daylight saving is handled;
  without it the app assumes the PC clock is on Malta time.
- The web page is reachable from your local network by default and only accepts local, private and Tailscale addresses.
  Set a password in Settings, or turn "reachable from the network" off, if others share your network.
- "Record all shows" covers TVM and TVM+ only; follow radio, Magic or TVMsport+ shows by name. Magic's schedule page is usually empty.
- The site is not an official API. It can change and break the app.

## License

GNU General Public License v3.0 or later. See [LICENSE](LICENSE).
