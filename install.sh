#!/bin/sh
# tvmi-rc installer for Linux and macOS.
#
#   curl -LsSf https://raw.githubusercontent.com/onetrickpony1/tvmi-rc/main/install.sh | sh
#
# It checks what is missing and asks before installing anything:
#   1. Python 3 with tkinter (needed)       2. ffmpeg (optional)
# then saves tvmi-recorder.py to ~/.local/share/tvmi-rc, adds a `tvmi-rc` command and offers a menu/desktop icon.
# Run it again to update. Uninstall: delete ~/.local/share/tvmi-rc and ~/.local/bin/tvmi-rc.
set -eu

URL="${TVMI_RC_URL:-https://raw.githubusercontent.com/onetrickpony1/tvmi-rc/main/tvmi-recorder.py}"
DIR="$HOME/.local/share/tvmi-rc"
BIN="$HOME/.local/bin"

say() { printf '\n==> %s\n' "$*"; }
fail() { printf '\ntvmi-rc installer: %s\n' "$*" >&2; exit 1; }
ask() {   # ask "question" default(y/n) -> 0 for yes; no terminal -> default
    if ( : < /dev/tty ) 2>/dev/null; then
        printf '%s ' "$1" > /dev/tty
        read -r ans < /dev/tty || ans=""
    else
        ans=""
    fi
    [ -z "$ans" ] && ans="$2"
    case "$ans" in [Yy]*) return 0 ;; *) return 1 ;; esac
}
have_tk() { python3 -c "import tkinter" >/dev/null 2>&1; }

# Run a package-manager command, with sudo if we are not root.
pm() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }

# install_pkg "what" apt-name dnf-name pacman-name brew-name
install_pkg() {
    if command -v apt-get >/dev/null 2>&1; then pm apt-get install -y "$2"
    elif command -v dnf >/dev/null 2>&1; then pm dnf install -y "$3"
    elif command -v pacman >/dev/null 2>&1; then pm pacman -S --needed --noconfirm "$4"
    elif command -v brew >/dev/null 2>&1; then brew install "$5"
    else printf "Don't know your package manager; please install %s yourself.\n" "$1"; return 1
    fi
}

say "Installing tvmi-rc"

# 1. Python 3 + tkinter (needed)
if ! command -v python3 >/dev/null 2>&1; then
    ask "Python 3 is missing and tvmi-rc needs it. Install it now (asks for your password)? [Y/n]" y \
        || fail "Python 3 is required"
    install_pkg "Python 3" python3 python3 python python@3.13 || fail "could not install Python 3"
fi
if ! have_tk; then
    ask "Python's tkinter (the window toolkit) is missing and tvmi-rc needs it. Install it now? [Y/n]" y \
        || fail "tkinter is required"
    install_pkg "tkinter for Python" python3-tk python3-tkinter tk python-tk || fail "could not install tkinter"
    have_tk || fail "tkinter still can't be imported; please install it for your Python and run this again"
fi

# 2. ffmpeg (optional)
if ! command -v ffmpeg >/dev/null 2>&1; then
    if ask "ffmpeg is not installed. It is optional (only for .mkv conversion and the ffmpeg engine). Install it? [y/N]" n; then
        install_pkg "ffmpeg" ffmpeg ffmpeg ffmpeg ffmpeg || printf 'Skipped ffmpeg; tvmi-rc works without it.\n'
    fi
fi

# 3. the app
say "Downloading tvmi-recorder.py"
mkdir -p "$DIR" "$BIN"
if [ -f "${TVMI_RC_SOURCE:-}" ]; then cp "$TVMI_RC_SOURCE" "$DIR/tvmi-recorder.py"
elif command -v curl >/dev/null 2>&1; then curl -LsSf "$URL" -o "$DIR/tvmi-recorder.py"
elif command -v wget >/dev/null 2>&1; then wget -qO "$DIR/tvmi-recorder.py" "$URL"
else fail "needs curl or wget to download the app"
fi
python3 -m py_compile "$DIR/tvmi-recorder.py" || fail "the downloaded file is not valid Python"
printf '#!/bin/sh\nexec python3 "%s/tvmi-recorder.py" "$@"\n' "$DIR" > "$BIN/tvmi-rc"
chmod +x "$BIN/tvmi-rc"

# 4. menu icon
if [ "$(uname)" = "Linux" ] && [ -z "${CI:-}" ] && ask "Add 'TVMi Recorder' to your applications menu? [Y/n]" y; then
    mkdir -p "$HOME/.local/share/applications"
    printf '[Desktop Entry]\nType=Application\nName=TVMi Recorder\nComment=Record the live channels of tvmi.mt\nExec=%s/tvmi-rc\nIcon=media-record\nTerminal=false\nCategories=AudioVideo;\n' \
        "$BIN" > "$HOME/.local/share/applications/tvmi-rc.desktop"
fi

say "Done."
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) printf 'Open a NEW terminal so the command is found (it is in %s).\n' "$BIN" ;;
esac
printf '\nStart with:   tvmi-rc\n'
