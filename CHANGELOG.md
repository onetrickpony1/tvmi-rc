# Changelog

## Unreleased

### Features
- **Schedules for every channel.** TVM, TVM+, TVMsport+, Magic, Radju Malta and Radju Malta 2 all have schedules now, so
  shows on any of them can be followed.

### Fixes
- **The web page is for this computer only until you let other devices in, and then it needs a password.** It used
  to accept anyone on the local network with no password. If other devices were already allowed and no password was
  set, one is made for you and shown in Settings: enter it on your phone. After 5 wrong passwords a device has to wait
  before trying again.
- **Importing settings with the same show twice no longer stops the app from starting.** A file with "Xarabank" and
  "xarabank " used to break the import and then every launch. Duplicates are now dropped when importing and loading.
- **Only one copy of the app runs.** Opening it from the menu while it was already running (for example, started at
  login) recorded every followed show twice. The second copy now brings the first one's window to the front.
- **One schedule page that fails to load no longer throws the whole update away.** The other pages are used, and the
  previous listings are kept for the page that failed. The update only counts as failed if nothing loaded for TVM and TVM+.
- **A show that the schedule moves while it records is followed to its new end.** Before, the recording stopped at the
  old time, and with "Every airing" the moved show could be recorded a second time.
- **Upcoming no longer says "recorded" for an airing that failed or stopped early.** It shows "skipped (error)",
  "skipped (low disk)" or "stopped early", and with "First airing each day" a failed first airing no longer blocks the repeat.
- **Recordings of radio and low-bitrate channels are converted to .mkv properly.** The check compared file sizes, which
  could reject a good .mkv. It now compares the length of the .mkv with the .ts.
- **Quitting no longer freezes the window while recordings are converted.** It says what it is finishing and closes by
  itself, and "Quit now" skips the remaining conversions (the .ts files are kept).
- **Logging out, shutting down or killing the app no longer leaves ffmpeg recording on its own.** The app closes cleanly
  on SIGTERM and at logout without asking, and on Linux and Windows ffmpeg now stops if the app is killed.
- **A slow or disconnected network drive no longer holds up the schedule.** Disk checks and ffmpeg probes run in the
  background, so recordings still start and stop on time. A save folder that doesn't answer within 8 seconds is reported.
- **A web action that timed out no longer runs later.** When the page says the recorder didn't answer, nothing was done,
  so pressing Record again can't record twice.
- **The built-in recorder handles a fast pause and resume.** The old part could carry on splitting files under the new
  one, and a burst of failures could turn into a run of false errors and server switches.
- **No more repeated video after the stream's chunk numbers jump.** The recorder now only goes back as far as the last
  chunk it saved, instead of up to 18 minutes.
- **Long runs stay light.** The log in the window keeps the latest 1,000 lines, and a recording on a very flaky
  connection lists its first 100 problems in the report instead of every one.
- **Smaller fixes.** The Upcoming list no longer jumps to the top every 20 seconds. The web page's "Record now" offers
  every channel. Typed margins and save folders are saved straight away. Show names such as "Aux" or "Con" work as
  folder names on Windows. The Test result is checked after the last chunks are in. Discord messages can't arrive out
  of order. Error messages from the built-in recorder no longer say "ffmpeg".
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
