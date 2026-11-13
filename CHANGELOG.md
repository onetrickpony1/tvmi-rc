# Changelog

## Unreleased

### Features
- **Schedules for every channel.** TVM, TVM+, TVMsport+, Magic, Radju Malta and Radju Malta 2 all have schedules now, so
  shows on any of them can be followed.

### Fixes
- **The app file is now called `tvmi-rc.py`, the same as the command.** It used to be `tvmi-recorder.py`. Running the
  installer again replaces the old file and updates "start at login" and the Start menu shortcut. Settings, followed
  shows and the log stay where they are.
- **One name everywhere: TVMi Recorder.** The window and messages used to say "TVMI Recorder" in some places and
  "TVMi Recorder" in others.

## 1.0.0 - 2026-11-12

First public version.

- Record TVM, TVM+ and TVMsport+ live streams by hand, or follow shows from the schedule (or record all shows).
- Built-in recorder with backfill of missed pieces, plus an optional ffmpeg engine; `- report.txt` when anything went wrong.
- Disk-space check, optional `.mkv` conversion, start at login.
- Web control page, Discord notifications, debug log, settings export / import.
- One-line installers (install.sh, install.ps1) that ask before installing Python tkinter, ffmpeg (optional) or tzdata.
- Linux and Windows. Licensed under the GNU GPL v3.0 or later.
