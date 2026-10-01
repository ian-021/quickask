"""Desktop state: which app the user was in, and what its front window is called.

The popup steals focus the moment it appears, so "the frontmost app" has to
be read when the hotkey fires, not when the question is sent. llm_popup.py
already records that app (an NSRunningApplication) in summon(); this module
takes its name and PID from that object and asks System Events, through
osascript, for the title of that process's front window.

Permissions: the app name needs none. The window title needs Accessibility
(System Settings > Privacy & Security > Accessibility) granted to the process
that runs the app: the terminal when running from source, llm-popup.app when
running the bundle. Without it osascript fails with error -1719 and we carry
on with just the app name.
"""

import os
import subprocess
import sys
import time
from dataclasses import dataclass, replace

ACCESSIBILITY_HINT = (
    "Window titles need the Accessibility permission. Open System Settings > "
    "Privacy & Security > Accessibility and enable the app that runs llm-popup "
    "(your terminal when running from source, llm-popup.app for the built app)."
)

# System Events errors we tell apart, by message: the numeric code (-1719) is
# shared between "not allowed assistive access" and a plain "invalid index".
_ERR_NOT_ALLOWED = "assistive access"      # the Accessibility permission is missing
_ERR_NO_WINDOW = "get window"              # Can't get window 1: the app has no windows
_ERR_NOT_GUI_APP = "get application process"  # the PID is not a GUI app (e.g. a shell)

OSASCRIPT_TIMEOUT = 2.0        # seconds; System Events can be slow to wake


@dataclass(frozen=True)
class DesktopState:
    """What we know about where the user was. Every field may be None."""

    app_name: str = None
    pid: int = None
    window_title: str = None
    error: str = None          # why window_title is missing, if we know

    @property
    def permission_missing(self):
        return bool(self.error) and _ERR_NOT_ALLOWED in self.error.lower()


def from_running_app(app):
    """DesktopState with app name and PID from an NSRunningApplication, or an
    empty state if it is None or is this process itself."""
    if app is None:
        return DesktopState()
    try:
        pid = int(app.processIdentifier())
        if pid == os.getpid() or app.isTerminated():
            return DesktopState()
        return DesktopState(app_name=str(app.localizedName() or "") or None, pid=pid)
    except Exception as e:  # a stale proxy object, for instance
        return DesktopState(error=f"could not read frontmost app: {e}")


def front_window_title(pid, timeout=OSASCRIPT_TIMEOUT, retries=1):
    """-> (title or None, error or None). Asks System Events for the front
    window of the process with this PID. Never raises. A window whose name is
    momentarily blank (a browser tab mid-navigation) is asked once more."""
    for attempt in range(retries + 1):
        title, err = _front_window_title_once(pid, timeout)
        if title or err or attempt == retries:
            return title, err
        time.sleep(0.15)
    return None, None


def _front_window_title_once(pid, timeout):
    script = (
        'tell application "System Events"\n'
        f'  set p to first application process whose unix id is {int(pid)}\n'
        "  return name of front window of p\n"
        "end tell"
    )
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"osascript timed out after {timeout}s"
    except OSError as e:
        return None, f"could not run osascript: {e}"
    if r.returncode != 0:
        err = " ".join(r.stderr.split()) or f"osascript exited {r.returncode}"
        low = err.lower()
        if _ERR_NOT_ALLOWED in low:
            err = f"{err} — {ACCESSIBILITY_HINT}"
        elif _ERR_NO_WINDOW in low or _ERR_NOT_GUI_APP in low:
            return None, None  # no window, or not a windowed app; nothing to report
        return None, err
    title = r.stdout.strip()
    return (title or None), None


def with_window_title(state):
    """A copy of `state` with window_title (or error) filled in. Blocking; the
    popup runs this on a worker thread."""
    if state.pid is None:
        return state
    title, err = front_window_title(state.pid)
    return replace(state, window_title=title, error=err)


def build_state(question, desktop=None):
    """The state object sent to Jev. A JSON object with named fields, as the
    TypeSafe docs recommend; fields we do not know are left out rather than
    sent as null so they cannot be mistaken for information."""
    state = {"question": question}
    if desktop is not None:
        if desktop.app_name:
            state["frontmost_app"] = desktop.app_name
        if desktop.window_title:
            state["window_title"] = desktop.window_title
    return state


def current_desktop_state():
    """Frontmost app right now, with its window title. For the command line;
    the popup uses from_running_app() on the app it saw at summon time."""
    from AppKit import NSWorkspace
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return with_window_title(from_running_app(app))


def main(argv=None):
    """python -m quickask.desktop: print what the capture sees. Run it from a
    terminal and it reports the terminal itself, which is enough to check
    that the Accessibility permission is in place."""
    import json
    state = current_desktop_state()
    print(json.dumps(build_state("<question>", state), indent=2, ensure_ascii=False))
    if state.error:
        print(f"\nerror: {state.error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
