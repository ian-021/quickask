#!/usr/bin/env python3
"""llm-popup: a Spotlight-style popup for trivial questions, answered by Cerebras.

Press the hotkey, type, press Enter, read the streamed answer, press Esc.
Everything lives in this one file. Read it top to bottom.

Threads in play:
  main thread    Qt event loop, all widgets, and the hotkey callback (macOS
                 delivers hotkey events through the app's own event loop).
  worker thread  one per question, does the HTTP streaming and emits tokens.
Qt delivers signals emitted from other threads as queued events on the main
thread, so the widgets are only ever touched from the main thread.
"""

import ctypes
import html
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import yaml
from quickask import config, desktop, router, routing_log
from quickask.scopes import load_scopes
from openai import (
    OpenAI,
    APIConnectionError,
    APIError,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)
from PySide6.QtCore import QObject, QPointF, QSize, Qt, QThread, QTimer, QUrl, Signal, QEvent
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QFrame,
    QLabel,
    QLineEdit,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QSystemTrayIcon,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# ---------------------------------------------------------------- settings --
# <alt> is Option, <cmd> is Command, <ctrl>, <shift>, plain letters and digits,
# <space>, <f1>... Examples: "<cmd>+<shift>+a", "<ctrl>+<alt>+q".
HOTKEY = "<cmd>+<space>"

APP_NAME = "llm-popup"
BASE_URL = "https://api.cerebras.ai/v1"
MODEL = "gpt-oss-120b"
SYSTEM_PROMPT = (
    "You answer quick questions in one or two short sentences. No preamble, "
    "no follow-up offers. For math or special symbols, output the actual "
    "Unicode character (λ, ∑, ≤), never LaTeX. Put every code snippet, shell "
    "command, or formula in a fenced code block: a line with three backticks "
    "before and after it. The reader sees fenced blocks as code boxes with a "
    "copy button, so give just the snippet inside, no commentary. Mention "
    "short identifiers inline between single backticks, which are shown as "
    "inline code. Use no other Markdown."
)



def scope_system_prompt(scope):
    """A routed question gets the scope's body as its system prompt. The
    formatting rules from SYSTEM_PROMPT are appended, because the transcript
    relies on them (fenced code, Unicode symbols, no other Markdown)."""
    return f"{scope.body}\n\n{SYSTEM_PROMPT}"


CONFIG_DIR = Path.home() / ".config" / "llm-popup"
KEY_FILE = CONFIG_DIR / "key"
HISTORY_FILE = CONFIG_DIR / "chat_history.yaml"
# The conversation kept by the hold button, so it survives quitting the app.
HELD_FILE = CONFIG_DIR / "held_conversation.yaml"
# Written once the app has registered itself as a login item, so the user's
# later choice in the menu (or in System Settings) is never overridden.
LOGIN_ITEM_MARKER = CONFIG_DIR / ".login_item_registered"
LOG_FILE = Path.home() / "Library" / "Logs" / "llm-popup.log"

WINDOW_WIDTH = 600
MAX_ANSWER_HEIGHT = 420  # answer area scrolls beyond this
MARGIN = 14              # inner padding of the popup
BACKGROUND = "#121212"


# ----------------------------------------------------------------- helpers --
def is_bundled():
    """True when running from the .app that build.sh produces (PyInstaller
    sets sys.frozen). Login-item registration only makes sense then: from a
    bare interpreter it would register Python itself."""
    return bool(getattr(sys, "frozen", False))


def bundle_path():
    from Foundation import NSBundle
    return NSBundle.mainBundle().bundlePath()


def load_api_key():
    """Environment variable first, then the key file. None if neither."""
    key = os.environ.get("CEREBRAS_API_KEY", "").strip()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text().strip()
    return key or None


def save_api_key(key):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(key + "\n")
    KEY_FILE.chmod(0o600)


def looks_like_api_key(text):
    return text.startswith("csk-") and " " not in text and len(text) > 10


def _str_presenter(dumper, text):
    # Multi-line and long strings are written in YAML block style ("|") so
    # the history file stays readable by a human: no quoting, no escaping,
    # each line of the answer on its own line.
    style = "|" if ("\n" in text or len(text) > 72) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", text, style=style)


yaml.add_representer(str, _str_presenter, Dumper=yaml.SafeDumper)


def _dump_yaml(data, f):
    # width=inf: never fold long lines, the block style above handles them.
    yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, width=float("inf"))


def append_history(question, answer, follow_up=False):
    """Append one entry to chat_history.yaml. Each append is a one-item list,
    so the file as a whole is one long YAML list. A follow-up in a thread is
    marked as such; it always directly follows the entry it continues."""
    entry = {"time": datetime.now().isoformat(timespec="seconds")}
    if follow_up:
        entry["follow_up"] = True
    entry["question"] = question
    entry["answer"] = answer
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY_FILE.open("a", encoding="utf-8") as f:
        _dump_yaml([entry], f)


def save_held(exchanges, threaded=False, scope=None):
    """Persist the kept conversation: its (question, answer) pairs, whether
    it was a thread or a one-off, and the scope whose context it uses, if
    any. An empty list removes the file."""
    if not exchanges:
        HELD_FILE.unlink(missing_ok=True)
        return
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = {"threaded": threaded}
    if scope:
        data["scope"] = scope
    data["exchanges"] = [{"question": q, "answer": a} for q, a in exchanges]
    with HELD_FILE.open("w", encoding="utf-8") as f:
        _dump_yaml(data, f)


def load_held():
    """-> (exchanges, threaded, scope name or None); ([], False, None) when
    nothing is kept."""
    if not HELD_FILE.exists():
        return [], False, None
    try:
        data = yaml.safe_load(HELD_FILE.read_text(encoding="utf-8")) or {}
        exchanges = [(e["question"], e["answer"]) for e in data.get("exchanges", [])]
        scope = data.get("scope")
        return exchanges, bool(data.get("threaded")), (str(scope) if scope else None)
    except Exception as e:
        print(f"could not read {HELD_FILE}: {e}", file=sys.stderr)
        return [], False, None


def _api_error_text(e):
    """The server's own message if the error body has one, else the client's."""
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict) and err.get("message"):
            return err["message"]
    return getattr(e, "message", None) or str(e)


# ---------------------------------------------------------------- commands --
# Typed into the box instead of a question. "/" alone lists them.
def clear_history(popup):
    if not HISTORY_FILE.exists():
        return "Chat history is already empty."
    entries = sum(1 for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines()
                  if line.startswith("- time:"))
    HISTORY_FILE.unlink()
    return f"Chat history cleared ({entries} {'entry' if entries == 1 else 'entries'} removed)."


def new_conversation(popup):
    popup.hold_btn.setChecked(False)
    popup.clear_conversation()
    return "Started a new conversation."


# name -> (description, function taking the popup and returning a message)
COMMANDS = {
    "/new": ("Start over: drop the current thread and release hold", new_conversation),
    "/clearhistory": ("Delete every entry in the chat history", clear_history),
}


def matching_commands(prefix):
    """Commands that start with the typed prefix, exact match first."""
    prefix = prefix.strip().lower()
    if prefix in COMMANDS:
        return [prefix]
    return [c for c in COMMANDS if c.startswith(prefix)]


# ------------------------------------------------------------ API streaming --
class AnswerWorker(QThread):
    """Streams one answer on a background thread so the UI never blocks.

    Tokens and errors are handed to the main thread through signals. The
    inherited `finished` signal fires when run() returns, for any reason.
    """

    token = Signal(str)
    failed = Signal(str)

    # Flipped once if the API rejects reasoning_effort, so later questions
    # do not pay for a failed request each time.
    drop_reasoning_effort = False

    def __init__(self, api_key, question, context=(), system_prompt=None, parent=None):
        super().__init__(parent)
        self.api_key = api_key
        self.question = question
        self.context = list(context)  # earlier (question, answer) pairs of the thread
        self.system_prompt = system_prompt or SYSTEM_PROMPT  # a scope's prompt, or the default
        self._cancelled = threading.Event()

    def cancel(self):
        # The stream loop checks this between chunks and bails out. The HTTP
        # connection is closed when the `with stream` block exits.
        self._cancelled.set()

    def _open_stream(self, client):
        messages = [{"role": "system", "content": self.system_prompt}]
        for q, a in self.context:
            messages.append({"role": "user", "content": q})
            messages.append({"role": "assistant", "content": a})
        messages.append({"role": "user", "content": self.question})
        kwargs = dict(model=MODEL, stream=True, messages=messages)
        if not AnswerWorker.drop_reasoning_effort:
            try:
                return client.chat.completions.create(reasoning_effort="low", **kwargs)
            except BadRequestError:
                AnswerWorker.drop_reasoning_effort = True
        return client.chat.completions.create(**kwargs)

    def run(self):
        # max_retries=0: a failure should show up as a message right away, not
        # after the client's silent exponential backoff.
        client = OpenAI(api_key=self.api_key, base_url=BASE_URL, timeout=30, max_retries=0)
        try:
            with self._open_stream(client) as stream:
                for chunk in stream:
                    if self._cancelled.is_set():
                        break
                    # The last chunk of a stream may carry usage info and no choices.
                    if chunk.choices and chunk.choices[0].delta.content:
                        self.token.emit(chunk.choices[0].delta.content)
        except AuthenticationError:
            self.failed.emit("API key rejected. Paste a new csk-... key here to replace it.")
        except RateLimitError:
            self.failed.emit("Rate limited by Cerebras. Try again in a moment.")
        except APIConnectionError:
            self.failed.emit("Could not reach the Cerebras API. Are you online?")
        except APIError as e:
            self.failed.emit(f"API error: {_api_error_text(e)}")
        except Exception as e:  # never let a worker exception kill the app
            self.failed.emit(f"Error: {e}")


# ------------------------------------------------------------ desktop state --
class DesktopWorker(QThread):
    """Fills in the front window title of the app the user came from. Runs
    osascript, which can take a few hundred ms and may need a permission we
    do not have, so it happens off the main thread and its result is
    optional: whatever is known when the question is sent gets used."""

    done = Signal(object)  # DesktopState

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state

    def run(self):
        try:
            self.done.emit(desktop.with_window_title(self.state))
        except Exception as e:  # never let a worker exception kill the app
            self.done.emit(desktop.DesktopState(app_name=self.state.app_name, pid=self.state.pid,
                                                error=f"{e}"))


# ------------------------------------------------------------------ router --
class RouterWorker(QThread):
    """One Jev call on a background thread. route() never raises, so `done`
    always fires with a RoutingResult; on any failure it carries `error`."""

    done = Signal(object)  # quickask.router.RoutingResult

    def __init__(self, state, scopes, parent=None):
        super().__init__(parent)
        self.state = state
        self.scopes = list(scopes) if scopes else []

    def run(self):
        try:
            self.done.emit(router.route(self.state, self.scopes))
        except Exception as e:  # belt and braces: never let a worker exception kill the app
            self.done.emit(router.RoutingResult(error=f"router crashed: {e}"))


# ------------------------------------------------------------ global hotkey --
# Carbon's RegisterEventHotKey is the API Spotlight-style launchers use. Unlike
# a keyboard event tap it needs no Input Monitoring or Accessibility
# permission, and the system swallows the keystroke so the frontmost app never
# sees it (Option+Space would otherwise type a non-breaking space there). The
# handler is invoked on the main thread from the app's own event loop, which
# Qt runs on top of the Cocoa one.

_MODIFIER_BITS = {"cmd": 1 << 8, "shift": 1 << 9, "alt": 1 << 11, "ctrl": 1 << 12}
_MODIFIER_GLYPHS = {"ctrl": "⌃", "alt": "⌥", "shift": "⇧", "cmd": "⌘"}
_MODIFIER_ORDER = ["ctrl", "alt", "shift", "cmd"]  # Apple's display order

# Virtual key codes of the US layout (Carbon's kVK_* constants).
_KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33,
    "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42,
    ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50,
    "return": 36, "enter": 36, "tab": 48, "space": 49, "backspace": 51,
    "delete": 51, "esc": 53, "escape": 53,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
    "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "page_up": 116, "page_down": 121,
}
_KEY_GLYPHS = {"space": "Space", "return": "↩", "enter": "↩", "tab": "⇥",
               "backspace": "⌫", "delete": "⌫", "esc": "⎋", "escape": "⎋",
               "left": "←", "right": "→", "up": "↑", "down": "↓"}


def parse_hotkey(spec):
    """'<alt>+<space>' -> (modifier bits, key code, ['alt'], 'space')."""
    mods, key = [], None
    for part in spec.lower().split("+"):
        part = part.strip().strip("<>")
        if part in _MODIFIER_BITS:
            mods.append(part)
        elif part in _KEYCODES and key is None:
            key = part
        else:
            raise ValueError(f"unknown token {part!r} in HOTKEY {spec!r}")
    if key is None:
        raise ValueError(f"HOTKEY {spec!r} has no key")
    bits = sum(_MODIFIER_BITS[m] for m in mods)
    return bits, _KEYCODES[key], mods, key


def hotkey_label(spec=HOTKEY):
    """'<alt>+<space>' -> '⌥Space', for menus and hints."""
    _, _, mods, key = parse_hotkey(spec)
    glyphs = "".join(_MODIFIER_GLYPHS[m] for m in _MODIFIER_ORDER if m in mods)
    return glyphs + _KEY_GLYPHS.get(key, key.upper())


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


# OSStatus handler(EventHandlerCallRef, EventRef, void *userData)
_EventHandlerUPP = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)


def _fourcc(s):
    return int.from_bytes(s.encode("ascii"), "big")


class Hotkey(QObject):
    """One global hotkey. `error` is set (and `pressed` never fires) if
    registration failed, typically because another app owns the combination."""

    pressed = Signal()

    _EVENT_HOT_KEY_EXISTS = -9878

    def __init__(self, spec=HOTKEY):
        super().__init__()
        self.spec = spec
        self.error = None
        self._ref = ctypes.c_void_p()
        # The C callback must outlive the registration, hence an attribute.
        self._callback = _EventHandlerUPP(self._on_event)

        c = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Carbon.framework/Carbon")
        c.GetApplicationEventTarget.restype = ctypes.c_void_p
        c.InstallEventHandler.restype = ctypes.c_int32
        c.InstallEventHandler.argtypes = [
            ctypes.c_void_p, _EventHandlerUPP, ctypes.c_ulong,
            ctypes.POINTER(_EventTypeSpec), ctypes.c_void_p, ctypes.c_void_p,
        ]
        c.RegisterEventHotKey.restype = ctypes.c_int32
        c.RegisterEventHotKey.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, _EventHotKeyID,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p),
        ]
        c.UnregisterEventHotKey.restype = ctypes.c_int32
        c.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
        self._carbon = c

    def start(self):
        modifiers, keycode, _, _ = parse_hotkey(self.spec)
        c = self._carbon
        target = c.GetApplicationEventTarget()
        spec = _EventTypeSpec(_fourcc("keyb"), 5)  # kEventClassKeyboard, kEventHotKeyPressed
        status = c.InstallEventHandler(target, self._callback, 1, ctypes.byref(spec), None, None)
        if status != 0:
            self.error = f"Could not install the hotkey handler (error {status})."
            return
        hk_id = _EventHotKeyID(_fourcc("AMhk"), 1)
        status = c.RegisterEventHotKey(keycode, modifiers, hk_id, target, 0, ctypes.byref(self._ref))
        if status == self._EVENT_HOT_KEY_EXISTS:
            self.error = (f"{hotkey_label(self.spec)} is already taken by another app. "
                          f"Change HOTKEY in llm_popup.py or free it in that app.")
        elif status != 0:
            self.error = f"Could not register the hotkey (error {status})."

    def stop(self):
        if self._ref:
            self._carbon.UnregisterEventHotKey(self._ref)
            self._ref = ctypes.c_void_p()

    def _on_event(self, call_ref, event, user_data):
        # Only one hotkey is registered, so any event here is ours.
        self.pressed.emit()
        return 0  # noErr: the event is handled, nobody else sees it


# ------------------------------------------------------------- login item --
class LoginItem:
    """Launch at login through SMAppService (macOS 13+). The app appears in
    System Settings > General > Login Items, where it can be switched off too."""

    ENABLED = 1  # SMAppServiceStatusEnabled

    @staticmethod
    def available():
        if not is_bundled():
            return False
        try:
            from ServiceManagement import SMAppService  # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def _service():
        from ServiceManagement import SMAppService
        return SMAppService.mainAppService()

    @classmethod
    def enabled(cls):
        return cls._service().status() == cls.ENABLED

    @classmethod
    def set_enabled(cls, on):
        svc = cls._service()
        ok, err = svc.registerAndReturnError_(None) if on else svc.unregisterAndReturnError_(None)
        if not ok:
            print(f"login item: {err}", file=sys.stderr)
        return ok

    @classmethod
    def register_once(cls):
        """First launch of an installed copy: turn launch-at-login on, like a
        launcher app would, and leave a marker so we never do it again."""
        if not cls.available() or LOGIN_ITEM_MARKER.exists():
            return
        installed = bundle_path().startswith(("/Applications/", str(Path.home() / "Applications")))
        if not installed:
            return  # e.g. running from dist/: registering that path would break on move
        if cls.set_enabled(True):
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            LOGIN_ITEM_MARKER.touch()


# -------------------------------------------------------------------- popup --
class AskInput(QLineEdit):
    """QLineEdit where Cmd+Backspace wipes the whole query, wherever the caret
    is (Option+Backspace still deletes one word, as everywhere on macOS), and
    Cmd+C copies text selected in the transcript. The input is the only
    widget that ever has keyboard focus, so it has to relay that shortcut:
    the transcript's labels never take focus, or typing would leave the box."""

    on_copy = None  # callable returning True if it copied something

    def keyPressEvent(self, event):
        # Qt reports the Command key as ControlModifier on macOS.
        if event.key() == Qt.Key_Backspace and event.modifiers() & Qt.ControlModifier:
            self.clear()
            self.textEdited.emit("")  # clear() alone does not emit it
            return
        if event.matches(QKeySequence.Copy) and self.on_copy and self.on_copy():
            return
        super().keyPressEvent(event)


def _draw_glyph(p, kind, color):
    pen = QPen(QColor(color), 2.4)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    if kind == "clipboard":
        p.drawRoundedRect(7, 7, 18, 21, 3, 3)      # the board
        p.setBrush(QColor(color))
        p.drawRoundedRect(11.5, 4, 9, 6, 2, 2)     # the clip on top
    elif kind == "check":
        p.drawPolyline(QPolygonF([QPointF(7, 17), QPointF(13, 23), QPointF(25, 9)]))
    elif kind == "plus":
        p.drawLine(QPointF(16, 7), QPointF(16, 25))
        p.drawLine(QPointF(7, 16), QPointF(25, 16))
    elif kind == "reply":                          # a curved "reply" arrow
        path = QPainterPath(QPointF(8, 13))
        path.lineTo(QPointF(19, 13))
        path.cubicTo(QPointF(26, 13), QPointF(26, 25), QPointF(19, 25))
        path.lineTo(QPointF(15, 25))
        p.drawPath(path)
        p.drawPolyline(QPolygonF([QPointF(13, 8), QPointF(8, 13), QPointF(13, 18)]))
    elif kind == "bookmark":                       # a ribbon with a notched bottom
        p.drawPolygon(QPolygonF([QPointF(9, 6), QPointF(23, 6), QPointF(23, 26),
                                 QPointF(16, 20), QPointF(9, 26)]))


def _button_icon(kind):
    """A footer-button icon drawn in code at 2x for Retina. Grey normally and
    white while hovered (QIcon's Active mode); a checked toggle (QIcon's On
    state) is blue."""
    icon = QIcon()
    for mode, state, color in (
        (QIcon.Normal, QIcon.Off, "#9a9a9a"), (QIcon.Active, QIcon.Off, "#ffffff"),
        (QIcon.Normal, QIcon.On, "#6f95ff"), (QIcon.Active, QIcon.On, "#9db5ff"),
    ):
        pm = QPixmap(32, 32)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        _draw_glyph(p, kind, color)
        p.end()
        pm.setDevicePixelRatio(2)
        icon.addPixmap(pm, mode, state)
    return icon


_INLINE_CODE = re.compile(r"`([^`\n]+)`")


def split_segments(text):
    """Split an answer on ``` fences into [("text", ...), ("code", ...)]. An
    unterminated fence (still streaming) counts as an open code block."""
    segments = []
    for i, part in enumerate(text.split("```")):
        if i % 2 == 0:
            part = part.strip("\n")
            if part.strip():
                segments.append(("text", part))
        else:
            first, nl, rest = part.partition("\n")
            tag = first.strip()
            if tag and " " not in tag and len(tag) <= 20:  # language tag, e.g. ```python
                part = rest if nl else ""
            segments.append(("code", part.strip("\n")))
    return segments


def inline_html(text, mono_family):
    """Escape plain text for a rich-text label, styling `inline code`."""
    escaped = html.escape(text)
    escaped = _INLINE_CODE.sub(
        # No padding characters: they would end up in text copied with Cmd+C.
        lambda m: (f'<span style="font-family: \'{mono_family}\'; background-color: #2a2a2f; '
                   f'color: #f2f2f2">{m.group(1)}</span>'),
        escaped)
    return escaped.replace("\n", "<br>")


def _selectable(label):
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    label.setFocusPolicy(Qt.NoFocus)  # keyboard focus stays in the input
    return label


class CodeBlock(QFrame):
    """A fenced code block: monospace text in a darker box, with a copy
    button at its end."""

    def __init__(self, code, fonts, parent=None):
        super().__init__(parent)
        self.setObjectName("code")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.code = ""
        self.label = _selectable(QLabel())
        self.label.setObjectName("codetext")
        self.label.setFont(fonts[1])
        self.label.setTextFormat(Qt.PlainText)

        self._icon_copy = _button_icon("clipboard")
        self._icon_copied = _button_icon("check")
        self.copy_btn = QToolButton()
        self.copy_btn.setIcon(self._icon_copy)
        self.copy_btn.setIconSize(QSize(14, 14))
        self.copy_btn.setToolTip("Copy code")
        self.copy_btn.setCursor(Qt.PointingHandCursor)
        self.copy_btn.setFocusPolicy(Qt.NoFocus)
        self.copy_btn.clicked.connect(self.copy)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.addStretch()
        footer.addWidget(self.copy_btn)
        box = QVBoxLayout(self)
        box.setContentsMargins(10, 8, 10, 4)
        box.setSpacing(2)
        box.addWidget(self.label)
        box.addLayout(footer)
        self.set_code(code)

    def set_code(self, code):
        self.code = code
        self.label.setText(code if code else "...")

    def copy(self):
        QApplication.clipboard().setText(self.code)
        self.copy_btn.setIcon(self._icon_copied)
        QTimer.singleShot(1200, lambda: self.copy_btn.setIcon(self._icon_copy))


class ResponseRow(QWidget):
    """One answer (or message), left-aligned: paragraphs with inline code,
    and a CodeBlock for each fenced block."""

    def __init__(self, fonts, parent=None):
        super().__init__(parent)
        self._fonts = fonts
        self._kinds = None
        self._widgets = []
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(6)

    def set_text(self, text, rich=False):
        if rich:
            segments = [("rich", text)]
        else:
            segments = split_segments(text) or [("empty", "")]
        kinds = [k for k, _ in segments]
        if kinds == self._kinds:
            # Streaming appends to the last segment; the others are unchanged.
            self._fill(self._widgets[-1], *segments[-1])
            return
        for w in self._widgets:
            w.hide()
            w.deleteLater()
        self._widgets = []
        self._kinds = kinds
        for kind, body in segments:
            w = CodeBlock("", self._fonts) if kind == "code" else _selectable(QLabel())
            if kind != "code":
                w.setObjectName("response")
                w.setFont(self._fonts[0])
            self._fill(w, kind, body)
            self.box.addWidget(w)
            w.show()  # a widget added to a visible parent is otherwise shown later
            self._widgets.append(w)

    def _fill(self, w, kind, body):
        if kind == "code":
            w.set_code(body)
            return
        w.setProperty("dim", kind == "empty")
        w.style().unpolish(w)
        w.style().polish(w)
        if kind == "empty":
            w.setTextFormat(Qt.PlainText)
            w.setText("...")
        else:
            w.setTextFormat(Qt.RichText)
            w.setText(body if kind == "rich" else inline_html(body, self._fonts[1].family()))


_BADGE_TIPS = {
    "scope": "The answer was written with this project's context (its scopes/*.md body) as the system prompt.",
    "none": "Answered the plain way, with no project context.",
    "frontier": "Jev thinks a fast small model may get this wrong. Nothing else was called; this is only a hint.",
    "template": "This scope file still has TODO placeholders. Fill it in so the context means something.",
    "error": "The router failed or was too slow, so the answer used the plain path.",
    "pending": "Asking Jev which project this question is about.",
}


class Transcript(QScrollArea):
    """The conversation: questions on the right, answers on the left, a line
    between rounds, newest at the bottom. Grows with its content up to
    MAX_ANSWER_HEIGHT, then scrolls."""

    def __init__(self, fonts, parent=None):
        super().__init__(parent)
        self._fonts = fonts
        # The body is sized by hand in fit(): with widgetResizable the scroll
        # area would use the labels' minimum size hint, which for wrapped
        # text is taller than the text actually needs.
        self.setWidgetResizable(False)
        self.setFrameShape(QScrollArea.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.NoFocus)
        self.viewport().setAutoFillBackground(False)
        self.body = QWidget()
        self.body.setAutoFillBackground(False)
        self.vbox = QVBoxLayout(self.body)
        self.vbox.setContentsMargins(0, 0, 0, 0)
        self.vbox.setSpacing(10)
        self.setWidget(self.body)
        # Clicking into one label drops the selection in the others, so
        # Cmd+C has exactly one selection to copy.
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.MouseButtonPress and isinstance(obj, QLabel) and obj.window() is self.window():
            for label in self.body.findChildren(QLabel):
                if label is not obj and label.hasSelectedText():
                    label.setSelection(0, 0)
        return False

    def selected_text(self):
        for label in self.body.findChildren(QLabel):
            if label.isVisible() and label.hasSelectedText():
                return label.selectedText()
        return ""

    def clear(self):
        def drop(w):
            w.hide()          # gone from the screen now, not after the event loop turns
            w.deleteLater()
        while self.vbox.count():
            item = self.vbox.takeAt(0)
            if item.widget():
                drop(item.widget())

    def _add(self, w):
        self.vbox.addWidget(w)
        # A widget added to an already visible parent is shown through a
        # queued event; show it now so the height computed next is right.
        w.show()
        return w

    def add_question(self, text):
        label = _selectable(QLabel(text))
        label.setObjectName("question")
        label.setFont(self._fonts[0])
        label.setTextFormat(Qt.PlainText)
        label.setAlignment(Qt.AlignRight | Qt.AlignTop)
        return self._add(label)

    def add_response(self, text="", rich=False):
        r = ResponseRow(self._fonts)
        r.set_text(text, rich=rich)
        return self._add(r)

    def add_badges(self, badges):
        """A row of small colored pills under an answer saying which context
        the answer used and what the router thought. `badges` is a list of
        (kind, text); `kind` picks the color in the stylesheet: scope, none,
        frontier, template, error, pending."""
        row = QWidget()
        box = QHBoxLayout(row)
        box.setContentsMargins(0, 2, 0, 0)
        box.setSpacing(6)
        for kind, text in badges:
            label = QLabel(text)
            label.setObjectName("badge")
            label.setProperty("kind", kind)
            label.setTextFormat(Qt.PlainText)
            label.setFocusPolicy(Qt.NoFocus)
            label.setToolTip(_BADGE_TIPS.get(kind, ""))
            box.addWidget(label)
        box.addStretch()
        return self._add(row)

    def add_separator(self):
        line = QFrame()
        line.setObjectName("separator")
        line.setFixedHeight(1)
        line.setAttribute(Qt.WA_StyledBackground, True)
        return self._add(line)

    def _height_for(self, width):
        if self.vbox.hasHeightForWidth():
            return max(0, self.vbox.heightForWidth(width))
        return self.vbox.sizeHint().height()

    def fit(self):
        """Size the body to its content and return the height this scroll
        area should take: the content's, capped at MAX_ANSWER_HEIGHT."""
        self.vbox.activate()
        width = WINDOW_WIDTH - 2 * MARGIN
        height = self._height_for(width)
        if height > MAX_ANSWER_HEIGHT and not self.style().styleHint(QStyle.SH_ScrollBar_Transient):
            # A classic scrollbar takes room from the text; macOS overlays it.
            width -= self.verticalScrollBar().sizeHint().width()
            height = self._height_for(width)
        self.body.setFixedSize(width, height)
        return min(height, MAX_ANSWER_HEIGHT)

    def scroll_to_bottom(self):
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())


class Popup(QWidget):
    """The window, and the conversation it shows.

    Conversation state:
      thread     completed (question, answer) pairs sent as context with the
                 next question. Empty for a one-off question.
      threaded   True once the user chose "Continue as thread" or pressed
                 hold: the input moves below the transcript, questions
                 continue the thread, and typing no longer clears the answer.
      _question  the question currently being answered (or last answered),
      _streamed  and its answer text so far. Pinned into `thread` when the
                 next question is sent.
      hold       keep the conversation when the popup closes (and across app
                 restarts, through HELD_FILE). Without it, closing clears all.
    """

    def __init__(self):
        super().__init__()
        # FramelessWindowHint: no title bar, we paint the rounded panel ourselves.
        # Tool: a floating panel (NSPanel on macOS), not listed in Cmd+Tab.
        # WindowStaysOnTopHint: above every other window, like Spotlight.
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        # Lets the area outside the rounded rectangle be see-through.
        self.setAttribute(Qt.WA_TranslucentBackground)
        # macOS hides tool windows on its own when the app deactivates. We hide
        # explicitly instead so Qt's idea of visibility always matches reality.
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow)
        self.setFixedWidth(WINDOW_WIDTH)

        self.worker = None
        self.scopes = None       # LoadedScopes, set by main() before the first summon
        self.thread = []
        self.threaded = False
        self._question = None
        self._streamed = ""
        self.hold = False
        self._transient = False   # transcript shows a message or the command list
        self._live_row = None     # the ResponseRow receiving streamed tokens
        self._shown_at = 0.0
        self._previous_app = None
        self._desktop = desktop.DesktopState()  # where the user was at summon time
        self._desktop_worker = None
        self._router = None        # RouterWorker whose result this question is waiting for
        self._router_timer = QTimer(self)  # hard ceiling on that wait
        self._router_timer.setSingleShot(True)
        self._router_timer.timeout.connect(self._on_route_timeout)
        self._pending = None       # (key, question) the router is holding up
        self._route = None         # RoutingResult for the current question, once known
        self._route_badges = None  # [(kind, text)] shown under the answer when "use jev" is on
        # Name of the scope this conversation runs in, once an answer has been
        # routed to one. Follow-ups reuse it instead of asking Jev again, so
        # "and why?" cannot lose the project context. Cleared with the thread.
        self._thread_scope = None

        base = QFont()
        self._font_normal = QFont(base)
        self._font_normal.setPointSize(15)
        self._font_mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        self._font_mono.setPointSize(14)
        fonts = (self._font_normal, self._font_mono)

        self.input = AskInput()
        self.input.setPlaceholderText("Ask anything")
        self.input.setFont(QFont(base.family(), 22))
        self.input.returnPressed.connect(self.submit)
        # textEdited fires for typing, not for our own setText/clear calls.
        self.input.textEdited.connect(self._on_text_edited)

        # "use jev": route the question through Jev before answering. Off by
        # default, and off means the exact pre-router behavior.
        self.use_jev = QCheckBox("use jev")
        self.use_jev.setToolTip("Ask Jev which project this is about and answer with that "
                                "project's context. Off: answer directly, as before.")
        self.use_jev.setCursor(Qt.PointingHandCursor)
        self.use_jev.setFocusPolicy(Qt.NoFocus)  # the caret stays in the input
        self.input_row = QWidget()
        row = QHBoxLayout(self.input_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(self.input, 1)
        row.addWidget(self.use_jev)

        self.transcript = Transcript(fonts)
        self.transcript.hide()
        self.input.on_copy = self._copy_selection

        self.continue_btn = self._tool_button("reply", "Keep this answer as context and ask a follow-up",
                                              self.follow_up)
        self.continue_btn.setText("Continue as thread")
        self.continue_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.hold_btn = self._tool_button("bookmark", "Keep this conversation when the popup closes, "
                                                      "and bring it back next time")
        self.hold_btn.setText("Keep")
        self.hold_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.hold_btn.setCheckable(True)
        self.hold_btn.toggled.connect(self._on_hold_toggled)

        self.footer = QHBoxLayout()
        self.footer.setSpacing(2)
        self.footer.addWidget(self.continue_btn)
        self.footer.addStretch()
        self.footer.addWidget(self.hold_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        layout.setSpacing(8)
        layout.addWidget(self.input_row)
        layout.addWidget(self.transcript)
        layout.addLayout(self.footer)

        self.setStyleSheet(f"""
            QLineEdit {{ background: transparent; border: none; color: #f2f2f2;
                         selection-background-color: #3d6be0; }}
            QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
            QLabel#response {{ color: #e2e2e2; selection-background-color: #3d6be0; }}
            QLabel#response[dim="true"] {{ color: #8a8a8a; }}
            QLabel#question {{ color: #a3a3a3; selection-background-color: #3d6be0; }}
            QLabel#badge {{ border-radius: 9px; padding: 2px 9px; font-size: 11px; }}
            QLabel#badge[kind="scope"] {{ background: rgba(111, 149, 255, 0.20); color: #a9c0ff; }}
            QLabel#badge[kind="none"] {{ background: rgba(255, 255, 255, 0.07); color: #9a9a9f; }}
            QLabel#badge[kind="pending"] {{ background: rgba(255, 255, 255, 0.07); color: #9a9a9f; }}
            QLabel#badge[kind="frontier"] {{ background: rgba(255, 184, 77, 0.18); color: #ffc46b; }}
            QLabel#badge[kind="template"] {{ background: rgba(255, 184, 77, 0.18); color: #ffc46b; }}
            QLabel#badge[kind="error"] {{ background: rgba(255, 99, 99, 0.16); color: #ff8a8a; }}
            QCheckBox {{ color: #9a9a9a; font-size: 12px; spacing: 5px; }}
            QCheckBox:hover {{ color: #ffffff; }}
            QCheckBox:checked {{ color: #9db5ff; }}
            QCheckBox::indicator {{ width: 13px; height: 13px; border-radius: 3px;
                                    border: 1px solid #5a5a60; background: #1d1d20; }}
            QCheckBox::indicator:checked {{ background: #6f95ff; border-color: #6f95ff; }}
            QFrame#code {{ background: #1d1d20; border-radius: 8px; }}
            QLabel#codetext {{ color: #e6e6e6; selection-background-color: #3d6be0; }}
            QFrame#separator {{ background: #2e2e32; }}
            QToolButton {{ background: transparent; border: none; border-radius: 5px;
                           padding: 3px 5px; color: #9a9a9a; font-size: 12px; }}
            QToolButton:hover {{ background: rgba(255, 255, 255, 0.10); color: #ffffff; }}
            QToolButton:checked {{ background: rgba(111, 149, 255, 0.18); color: #9db5ff; }}
        """)

        # Esc anywhere in the window. The line edit does not consume Esc, so a
        # window-level shortcut sees it whether the input or the answer has focus.
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self.dismiss)

        # Clicking another app, the desktop, or Cmd+Tab: the app goes inactive.
        # (A window-level ActivationChange never arrives for this tool window.)
        QGuiApplication.instance().applicationStateChanged.connect(self._on_app_state)

        self._restore_held()

    def _tool_button(self, kind, tip, slot=None):
        b = QToolButton()
        b.setIcon(_button_icon(kind))
        b.setIconSize(QSize(16, 16))
        b.setToolTip(tip)
        b.setCursor(Qt.PointingHandCursor)
        b.setFocusPolicy(Qt.NoFocus)  # keep the caret in the input
        if slot:
            b.clicked.connect(slot)
        b.hide()
        return b

    # ---- painting ----
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QColor(255, 255, 255, 28))     # faint 1px edge
        bg = QColor(BACKGROUND)
        bg.setAlpha(246)
        p.setBrush(bg)
        p.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 14, 14)

    # ---- show / hide ----
    def toggle(self):
        if self.isVisible():
            self.dismiss()
        else:
            self.summon()

    def summon(self):
        if self._transient:
            self._render()   # drop a stale message, show the conversation (if any)
        self._fit()

        # Center horizontally, one fifth down, on the screen the mouse is on.
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        self.move(geo.center().x() - self.width() // 2, geo.top() + geo.height() // 5)

        self._shown_at = time.monotonic()
        self._previous_app = _frontmost_app()  # where focus goes back on dismiss
        self._capture_desktop(self._previous_app)
        self.show()
        self.raise_()
        # We are an accessory app (no Dock icon) and another app is frontmost.
        # Qt's activateWindow() alone does not always steal focus on macOS, so
        # ask AppKit to activate us first.
        _activate_app()
        self.activateWindow()
        self.input.setFocus()
        # Keep the last question but select it all, so typing replaces it and
        # Enter would re-ask it. Change to self.input.clear() if you prefer empty.
        self.input.selectAll()

        if not load_api_key():
            self.show_message("No API key yet. Get one at cloud.cerebras.ai, then paste "
                              "it here (it starts with csk-) and press Enter.")

    def dismiss(self):
        if self.worker is not None or self._router is not None:
            # Cancelled mid-answer: drop the incomplete exchange.
            self._cancel_worker()
            self._question = None
            self._streamed = ""
        if self.hold:
            save_held(self._exchanges(), self.threaded, self._thread_scope)
            self._render()
        else:
            self.clear_conversation()
        self.hide()
        # Summoning made us the active app, which took keyboard focus away
        # from whatever was in front. Give it back, so the user lands where
        # they were, as with Spotlight or Raycast, instead of having to click.
        _hide_app(self._previous_app)
        self._previous_app = None

    def _on_app_state(self, state):
        # Ignore the flicker right after summon, while focus is still moving to us.
        if (state == Qt.ApplicationInactive and self.isVisible()
                and time.monotonic() - self._shown_at > 0.3):
            self.dismiss()

    # ---- desktop state ----
    def _capture_desktop(self, app):
        """Remember the app the user came from, then fetch its window title in
        the background. The name is available at once; the title arrives
        later, or never, and that is fine."""
        self._desktop = desktop.from_running_app(app)
        if self._desktop.pid is None:
            return
        w = DesktopWorker(self._desktop, parent=self)
        # `w is self._desktop_worker`: a slow osascript from an earlier summon
        # must not overwrite the state of this one.
        w.done.connect(lambda state, w=w: self._on_desktop(w, state))
        w.finished.connect(w.deleteLater)
        self._desktop_worker = w
        w.start()

    def _on_desktop(self, w, state):
        if w is self._desktop_worker:
            self._desktop_worker = None
            self._desktop = state
            if state.error:
                print(f"desktop: {state.error}", file=sys.stderr)

    def jev_state(self, question):
        """The state object for the router: the question plus whatever is
        known about the desktop right now."""
        return desktop.build_state(question, self._desktop)

    # ---- conversation state ----
    def _exchanges(self):
        """Every completed exchange, the pinned thread plus the latest answer."""
        done = list(self.thread)
        if self._question is not None and self._streamed and self.worker is None:
            done.append((self._question, self._streamed))
        return done

    def clear_conversation(self):
        """Back to an empty box: no thread, no answer, hold released on disk."""
        self._cancel_worker()
        self.thread = []
        self._question = None
        self._streamed = ""
        self._thread_scope = None
        self._set_threaded(False)
        self._render()
        if not self.hold:
            save_held([])

    def _set_threaded(self, on):
        """Thread mode puts the input under the transcript, so a new question
        is added at the bottom of the conversation."""
        self.threaded = on
        self._update_placeholder()
        layout = self.layout()
        layout.removeWidget(self.input_row)
        layout.removeWidget(self.transcript)
        layout.removeItem(self.footer)
        order = [self.transcript, self.input_row] if on else [self.input_row, self.transcript]
        for w in order:
            layout.addWidget(w)
        layout.addLayout(self.footer)

    def _update_placeholder(self):
        """The empty input names the context a follow-up will use, since the
        badge under the last answer scrolls away in a long thread."""
        if not self.threaded:
            text = "Ask anything"
        elif self._thread_scope and self.scopes and self.scopes.get(self._thread_scope):
            text = f"Follow up in {self._thread_scope}"
        else:
            text = "Follow up"
        self.input.setPlaceholderText(text)

    def follow_up(self):
        """Continue as thread: pin the current answer and ask the next
        question with it as context."""
        if self.worker is not None or not self._streamed:
            self.input.setFocus()
            return
        self.thread.append((self._question, self._streamed))
        self._question = None
        self._streamed = ""
        self._set_threaded(True)
        self.input.clear()
        self._render()
        self.input.setFocus()

    def _on_hold_toggled(self, on):
        """Keep: only about what happens when the popup closes. It does not
        change how the conversation works."""
        self.hold = on
        if on:
            save_held(self._exchanges(), self.threaded, self._thread_scope)
        else:
            save_held([])          # it stays on screen until the popup closes

    def _restore_held(self):
        exchanges, threaded, scope = load_held()
        if not exchanges:
            return
        self._thread_scope = scope  # resolved against self.scopes when next used
        self.hold_btn.blockSignals(True)
        self.hold_btn.setChecked(True)
        self.hold_btn.blockSignals(False)
        self.hold = True
        if threaded:
            self.thread = exchanges
            self._set_threaded(True)
        else:
            # A kept one-off: the question goes back in the box, its answer below.
            self.thread = exchanges[:-1]
            self._question, self._streamed = exchanges[-1]
            self.input.setText(self._question)
        self._render()

    # ---- asking ----
    def _on_text_edited(self, text):
        """A leading "/" shows the commands that match what has been typed.
        Otherwise, for a one-off question, typing means a new question: drop
        the old answer so the box shrinks back to just the input. In a thread
        the transcript stays."""
        if text.startswith("/"):
            self._show_commands(text)
        elif self._transient:
            self._render()   # a message or the command list was showing
        elif not self.threaded and self.transcript.isVisible():
            self.clear_conversation()

    def _show_commands(self, prefix):
        matches = matching_commands(prefix)
        if matches:
            rows = "".join(
                f'<tr><td style="color:#f2f2f2; padding-right:20px">{c}</td>'
                f'<td style="color:#9a9a9a">{COMMANDS[c][0]}</td></tr>'
                for c in matches)
            html_ = f'<table cellspacing="0" cellpadding="1">{rows}</table>'
        else:
            html_ = '<span style="color:#9a9a9a">No such command. Type / to see them all.</span>'
        self.show_message(html_, html=True)

    def _copy_selection(self):
        """Cmd+C: copy whatever is selected in the transcript, if anything."""
        text = self.transcript.selected_text()
        if not text:
            return False
        QApplication.clipboard().setText(text)
        return True

    def _run_command(self, text):
        matches = matching_commands(text.split()[0])
        if len(matches) != 1:     # unknown, or an ambiguous prefix
            self._show_commands(text)
            return
        _, run = COMMANDS[matches[0]]
        self.input.clear()
        self.show_message(run(self))

    def submit(self):
        question = self.input.text().strip()
        if not question:
            return

        if question.startswith("/"):
            self._run_command(question)
            return

        # Pasting a key into the box stores it. This is how the key gets set
        # up on first use, and how a rejected key gets replaced.
        if looks_like_api_key(question):
            save_api_key(question)
            self.input.clear()
            self.show_message(f"API key saved to {KEY_FILE}. Ask away.")
            return

        key = load_api_key()
        if not key:
            self.show_message("No API key yet. Get one at cloud.cerebras.ai, then paste "
                              "it here (it starts with csk-) and press Enter.")
            return

        self._cancel_worker()
        if self.threaded:
            # The previous exchange becomes context for this question.
            if self._question is not None and self._streamed:
                self.thread.append((self._question, self._streamed))
            self.input.clear()
        self._question = question
        self._streamed = ""
        self._route = None
        self._route_badges = None
        self._render()  # reveals the answer area right away

        thread_scope = self._current_thread_scope()
        if thread_scope is not None:
            # A follow-up in a conversation that already has a project's
            # context: keep it, no routing. Jev still runs in the shadow.
            self._route_badges = self._thread_badges(thread_scope)
            self._render()
            self._answer(key, question, thread_scope)
            self._shadow_route(question, f"scope:{thread_scope.name}")
        elif self.use_jev.isChecked():
            self._route_then_answer(key, question)
        else:
            self._answer(key, question)       # the pre-router path, started first
            self._shadow_route(question)      # then Jev, in the background, only logged

    def _current_thread_scope(self):
        """The Scope a follow-up should reuse, or None. Only in thread mode:
        a one-off re-asked from the box is routed afresh."""
        if not self.threaded or not self._thread_scope or not self.scopes:
            return None
        scope = self.scopes.get(self._thread_scope)
        if scope is None or scope.is_template:
            print(f"thread scope {self._thread_scope!r} missing or still a template; routing again",
                  file=sys.stderr)
            self._thread_scope = None
            return None
        return scope

    def _thread_badges(self, scope):
        badges = [("scope", f"Using context: {scope.name} · thread")]
        if scope.is_template:
            badges.append(("template", f"{scope.name}.md is still a template"))
        return badges

    def _answer(self, key, question, scope=None):
        """Start streaming the answer. With a scope, its body is the system
        prompt; without, this is the pre-router path unchanged."""
        system_prompt = scope_system_prompt(scope) if scope else None
        # parent=self keeps the QThread alive (Qt owns it); deleteLater once it
        # finishes so cancelled workers do not pile up.
        w = AnswerWorker(key, question, context=self.thread, system_prompt=system_prompt, parent=self)
        # Each slot checks `w is self.worker` because a cancelled worker can
        # still have a token or two queued when the next question starts.
        w.token.connect(lambda text, w=w: self._on_token(w, text))
        w.failed.connect(lambda msg, w=w: self._on_failed(w, msg))
        w.finished.connect(lambda w=w: self._on_finished(w))
        w.finished.connect(w.deleteLater)
        self.worker = w
        w.start()

    # ---- shadow routing ("use jev" unchecked) ----
    def _shadow_route(self, question, path="default"):
        """Run the same Jev call the checked path would, but only to log it.
        Nothing here touches the conversation or the widgets: the answer is
        already streaming and is not waiting on this. `path` is what actually
        ran: "default", or "scope:<name>" for a follow-up in a scoped thread."""
        snapshot = self._desktop
        follow_up = bool(self.thread)
        use_jev = self.use_jev.isChecked()
        w = RouterWorker(self.jev_state(question), self.scopes, parent=self)
        w.done.connect(lambda result: self._log_route(question, snapshot, use_jev, result,
                                                      path, follow_up))
        w.finished.connect(w.deleteLater)
        w.start()

    def _log_route(self, question, snapshot, use_jev, result, path, follow_up):
        entry = routing_log.make_entry(question, snapshot, use_jev, result, path, follow_up)
        routing_log.append(entry)

    # ---- routing ("use jev" checked) ----
    def _route_then_answer(self, key, question):
        """Ask Jev first, then answer with the chosen scope's context. The
        wait is bounded twice: the SDK's own timeout, and a UI timer a little
        above it, so a stuck call can never hold the answer hostage."""
        self._route_badges = [("pending", "Asking Jev which project this is about…")]
        self._render()
        w = RouterWorker(self.jev_state(question), self.scopes, parent=self)
        w.done.connect(lambda result, w=w: self._on_routed(w, result, key, question))
        w.finished.connect(w.deleteLater)
        self._router = w
        self._pending = (key, question)
        self._router_timer.start(int((config.ROUTER_TIMEOUT + 0.25) * 1000))
        w.start()

    def _on_routed(self, w, result, key, question):
        if w is not self._router:
            return  # superseded by a newer question, cancelled, or timed out
        self._router = None
        self._pending = None
        self._router_timer.stop()
        self._route = result
        scope = None
        if result.routed and result.scope_confidence >= config.SCOPE_THRESHOLD:
            scope = self.scopes.get(result.scope)
        if scope is not None and scope.is_template:
            # Matched, but the file is still placeholder text: sending that as
            # the system prompt would only invite made-up answers. Say what
            # matched and answer the plain way.
            self._route_badges = self._route_badges_for(result, None, template=scope)
            scope = None
        else:
            self._route_badges = self._route_badges_for(result, scope)
        if scope is not None:
            self._thread_scope = scope.name   # follow-ups keep this context
            self._update_placeholder()
        self._log_route(question, self._desktop, True, result,
                        f"scope:{scope.name}" if scope else "default", bool(self.thread))
        self._render()
        self._answer(key, question, scope)

    def _on_route_timeout(self):
        if self._router is None or self._pending is None:
            return
        key, question = self._pending
        self._router = None
        self._pending = None
        result = router.RoutingResult(error=f"jev did not answer within {config.ROUTER_TIMEOUT}s",
                                      latency_ms=int((config.ROUTER_TIMEOUT + 0.25) * 1000))
        self._route = result
        self._route_badges = self._route_badges_for(result, None)
        self._log_route(question, self._desktop, True, result, "default", bool(self.thread))
        self._render()
        self._answer(key, question)

    def _route_badges_for(self, result, scope, template=None):
        """The pills under the answer: which context was used, and why.
        `template` is a matched scope that was not used because its file is
        still a template."""
        badges = []
        if result.error:
            badges.append(("error", f"Router off: {result.error} · plain answer"))
            return badges
        if template is not None:
            badges.append(("template", f"Matched {template.name} · {result.scope_confidence:.2f} "
                                       f"· {template.name}.md is still a template · plain answer"))
        elif scope is not None:
            badges.append(("scope", f"Using context: {scope.name} · {result.scope_confidence:.2f} "
                                    f"· {result.latency_ms} ms"))
            if scope.is_template:
                badges.append(("template", f"{scope.name}.md is still a template"))
        elif result.scope != router.NONE:
            badges.append(("none", f"No project context · {result.scope} {result.scope_confidence:.2f} "
                                   f"is under {config.SCOPE_THRESHOLD:g} · {result.latency_ms} ms"))
        else:
            badges.append(("none", f"No project context · none {result.scope_confidence:.2f} "
                                   f"· {result.latency_ms} ms"))
        if result.needs_frontier_prob >= config.FRONTIER_THRESHOLD:
            badges.append(("frontier", f"This might need Claude · {result.needs_frontier_prob:.2f}"))
        return badges

    def _cancel_worker(self):
        if self.worker is not None:
            self.worker.cancel()
            self.worker = None
        # A pending routing call is dropped the same way: its result is
        # ignored when it arrives (see the `w is self._router` checks).
        self._router = None
        self._pending = None
        self._router_timer.stop()

    def _on_token(self, w, text):
        if w is self.worker:
            self._streamed += text
            if self._live_row is not None and not self._transient:
                self._live_row.set_text(self._streamed)
                self._fit()
                self.transcript.scroll_to_bottom()
            else:
                self._render()

    def _on_failed(self, w, msg):
        if w is self.worker:
            self.show_message(msg)

    def _on_finished(self, w):
        if w is self.worker:
            self.worker = None
            if self._streamed:
                append_history(w.question, self._streamed, follow_up=bool(self.thread))
                if self.hold:
                    save_held(self._exchanges(), self.threaded, self._thread_scope)
                self._render()  # "Continue as thread" appears now that the answer is complete

    # ---- transcript ----
    def show_message(self, text, html=False):
        """Show text in the transcript (errors, hints, command output) in place
        of the conversation, which comes back on the next render. Not logged."""
        self._transient = True
        self._live_row = None
        self.transcript.clear()
        self.transcript.add_response(text, rich=html)
        self.transcript.show()
        self.continue_btn.hide()
        self.hold_btn.hide()
        self._fit()

    def _render(self):
        """Rebuild the transcript from the conversation state."""
        self._transient = False
        self._live_row = None
        self.transcript.clear()
        show_questions = bool(self.thread) or self.threaded
        for i, (q, a) in enumerate(self.thread):
            if i:
                self.transcript.add_separator()
            self.transcript.add_question(q)
            self.transcript.add_response(a)
        if self._question is not None:
            if show_questions:
                if self.thread:
                    self.transcript.add_separator()
                self.transcript.add_question(self._question)
            self._live_row = self.transcript.add_response(self._streamed)
            if self._route_badges:
                self.transcript.add_badges(self._route_badges)
        has_content = bool(self.thread) or self._question is not None
        self.transcript.setVisible(has_content)
        # "Continue as thread" only for a finished one-off answer; hold
        # whenever there is something to keep.
        self.continue_btn.setVisible(has_content and not self.threaded
                                     and self.worker is None and bool(self._streamed))
        self.hold_btn.setVisible(has_content)
        self._fit()
        self.transcript.scroll_to_bottom()

    def _fit(self):
        """Grow the transcript to its content (capped) and the window to fit."""
        if self.transcript.isVisible():
            self.transcript.setFixedHeight(self.transcript.fit())
        self.layout().activate()
        self.adjustSize()


# ------------------------------------------------------------- menu bar --
def _menu_bar_icon():
    """A '?' in a rounded square, drawn as a template image so macOS recolors
    it for light and dark menu bars."""
    size = 44  # points * 2, so it is crisp on Retina at 22pt
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(Qt.black, 3.5))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(4, 4, size - 8, size - 8, 10, 10)
    font = QFont()
    font.setPixelSize(28)
    font.setBold(True)
    p.setFont(font)
    p.drawText(pm.rect(), Qt.AlignCenter, "?")
    p.end()
    icon = QIcon(pm)
    icon.setIsMask(True)
    return icon


class MenuBar(QObject):
    """The status-bar item: the only way to quit a Dock-less app, plus the
    launch-at-login switch."""

    def __init__(self, app, popup):
        super().__init__()
        self.menu = QMenu()

        ask = QAction(f"Ask\t{hotkey_label()}", self.menu)
        ask.triggered.connect(popup.summon)
        self.menu.addAction(ask)
        self.menu.addSeparator()

        self.login = QAction("Launch at login", self.menu)
        self.login.setCheckable(True)
        if LoginItem.available():
            self.login.triggered.connect(lambda on: LoginItem.set_enabled(on))
        else:
            self.login.setEnabled(False)
            self.login.setToolTip("Only available when running the built app")
        self.menu.addAction(self.login)

        folder = QAction("Open config folder", self.menu)
        folder.triggered.connect(self._open_config)
        self.menu.addAction(folder)
        self.menu.addSeparator()

        quit_ = QAction(f"Quit {APP_NAME}", self.menu)
        quit_.triggered.connect(app.quit)
        self.menu.addAction(quit_)

        self.menu.aboutToShow.connect(self._refresh)

        self.tray = QSystemTrayIcon(_menu_bar_icon(), self)
        self.tray.setToolTip(f"{APP_NAME}: {hotkey_label()}")
        self.tray.setContextMenu(self.menu)
        self.tray.show()

    def _refresh(self):
        if LoginItem.available():
            self.login.setChecked(LoginItem.enabled())

    @staticmethod
    def _open_config():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(CONFIG_DIR)))


# --------------------------------------------------------------------- main --
def _hide_dock_icon():
    """Become an 'accessory' app: no Dock icon or menu bar, but still allowed
    to take keyboard focus (a 'prohibited' policy could not). The built app
    also sets LSUIElement in its Info.plist, so it never flashes a Dock icon
    while Python starts up."""
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)


def _activate_app():
    from AppKit import NSApplication
    NSApplication.sharedApplication().activateIgnoringOtherApps_(True)


def _frontmost_app():
    from AppKit import NSWorkspace
    return NSWorkspace.sharedWorkspace().frontmostApplication()


def _hide_app(previous=None):
    """Step out of the way: hide our windows and activate `previous` (the app
    that was in front before summon) or, failing that, whatever macOS picks.
    Since macOS 14 an app may only activate another one after yielding its
    own activation to it, hence the yield first."""
    from AppKit import NSApplication, NSApplicationActivateIgnoringOtherApps
    app = NSApplication.sharedApplication()
    if previous is not None and not previous.isTerminated() and previous.processIdentifier() != os.getpid():
        if hasattr(app, "yieldActivationToApplication_"):
            app.yieldActivationToApplication_(previous)
        previous.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    app.hide_(None)


def _log_to_file():
    """The built app has no terminal; keep stderr somewhere findable."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    f = open(LOG_FILE, "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = f


def main():
    if is_bundled():
        _log_to_file()
    # .env (repo root, or ~/.config/llm-popup/.env for the built app) into the
    # environment, so CEREBRAS_API_KEY and TYPESAFE_API_KEY kept there are
    # found by the same env-var-first lookups as before.
    config.setup()
    # Qt would otherwise turn this bare Python process into a regular app with
    # a Dock icon at startup. Skip that; we set the accessory policy ourselves.
    os.environ.setdefault("QT_MAC_DISABLE_FOREGROUND_APPLICATION_TRANSFORM", "1")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)  # hiding the popup must not quit
    _hide_dock_icon()

    popup = Popup()  # created once, kept hidden, so summoning is instant
    # Scopes are read once here. A bad file is reported (log and, below, the
    # popup) but never stops the app: the default path needs no scopes.
    popup.scopes = load_scopes()
    for err in popup.scopes.errors:
        print(f"scopes: {err}", file=sys.stderr)
    popup._update_placeholder()  # a restored thread may name a scope
    menu_bar = MenuBar(app, popup)  # noqa: F841  (kept alive for the app's lifetime)

    hotkey = Hotkey()
    hotkey.pressed.connect(popup.toggle)
    hotkey.start()
    app.aboutToQuit.connect(hotkey.stop)

    if hotkey.error:
        print(hotkey.error, file=sys.stderr)
        popup.summon()
        popup.show_message(hotkey.error)
    elif popup.scopes.errors:
        popup.summon()
        popup.show_message("Some scope files were skipped:\n" + "\n".join(popup.scopes.errors))

    LoginItem.register_once()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
