"""
Jarvis UI — the floating pill (PyQt6).

Idle: a 28x28 dark pill with a slow amber glow dot and orbiting arc, anchored
right of the camera notch, floating above the menu bar (NSStatusWindowLevel).

Active: animates open (150ms) to a 440x108 pill showing the state indicator,
waveform bars, descriptive tool activity ("Searching Spotify..."), a memory-hit
pulse, and up to 3 transcript lines that fade out gracefully on overflow.
Errors flash the pill red with the failure message. Clicking the pill toggles
a conversation-history panel with the last few exchanges.

The palette follows macOS appearance (dark / light) and re-checks periodically.

Connects to the backend's UI bridge (ui_bridge.py) and reconnects forever, so
it can be started before or after the backend (launchd starts both).
"""

import math
import subprocess
import sys
import time
from collections import deque

from PyQt6.QtCore import (QEasingCurve, QObject, QPropertyAnimation, QRect,
                          Qt, QTimer, pyqtSignal)
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QWidget

from ui_bridge import BridgeClient

# ---------------------------------------------------------------- geometry
IDLE_W, IDLE_H = 28, 28
ACTIVE_W, ACTIVE_H = 440, 108
HISTORY_W, HISTORY_H = 440, 332
TOP_MARGIN = 4
NUM_BARS = 26

NS_STATUS_WINDOW_LEVEL = 25


class Palette:
    def __init__(self, dark: bool):
        self.dark = dark
        if dark:
            self.bg = QColor(10, 10, 12, 232)
            self.bg_edge = QColor(60, 45, 10, 120)
            self.text = QColor(245, 234, 217)        # warm white
            self.dim = QColor(245, 234, 217, 110)
        else:
            self.bg = QColor(244, 239, 230, 240)
            self.bg_edge = QColor(160, 120, 30, 130)
            self.text = QColor(45, 35, 20)
            self.dim = QColor(45, 35, 20, 130)
        self.amber = QColor(255, 170, 0) if dark else QColor(190, 120, 0)
        self.orange = QColor(255, 102, 0)
        self.red = QColor(255, 51, 68)


def macos_is_dark() -> bool:
    try:
        out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                             capture_output=True, text=True, timeout=2)
        return out.stdout.strip() == "Dark"
    except Exception:  # noqa: BLE001
        return True


STATE_LABEL = {
    "idle": "", "listening": "Listening", "thinking": "Thinking",
    "tool": "Working", "speaking": "Speaking", "error": "Error",
}


class BridgeSignals(QObject):
    event = pyqtSignal(dict)


class JarvisPill(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self.palette_ = Palette(macos_is_dark())
        self.state = "idle"
        self.phase = 0.0                       # drives orbit / pulse animations
        self.levels = [0.05] * NUM_BARS        # waveform bar heights
        self.tool_text = ""
        self.error_text = ""
        self.error_until = 0.0
        self.memory_count = 0
        self.memory_until = 0.0
        self.transcript: deque = deque(maxlen=12)   # (role, text) history
        self.expanded_history = False
        self._collapse_timer = QTimer(self)
        self._collapse_timer.setSingleShot(True)
        self._collapse_timer.timeout.connect(self._collapse)

        self._anim = QPropertyAnimation(self, b"geometry")
        self._anim.setDuration(150)            # the wake-up animation
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._tick = QTimer(self)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start(33)

        self._appearance_timer = QTimer(self)
        self._appearance_timer.timeout.connect(self._refresh_appearance)
        self._appearance_timer.start(10_000)

        self.setGeometry(self._target_rect())
        self.show()
        self._raise_above_menubar()

        self.signals = BridgeSignals()
        self.signals.event.connect(self.on_event)
        self.client = BridgeClient(self.signals.event.emit)
        self.client.start()

    # ------------------------------------------------------------ macOS layering
    def _raise_above_menubar(self):
        """NSStatusWindowLevel so the pill floats above every app and the menu bar."""
        try:
            import objc
            view = objc.objc_object(c_void_p=int(self.winId()))
            window = view.window()
            window.setLevel_(NS_STATUS_WINDOW_LEVEL)
            # all spaces + don't activate
            window.setCollectionBehavior_((1 << 0) | (1 << 8))
        except Exception as e:  # noqa: BLE001
            print(f"could not raise window level: {e}", file=sys.stderr)

    def _anchor_x(self) -> int:
        """Anchor right of the camera notch when there is one, else centred."""
        screen = QApplication.primaryScreen().geometry()
        try:
            from AppKit import NSScreen
            ns_screen = NSScreen.mainScreen()
            if hasattr(ns_screen, "auxiliaryTopRightArea"):
                area = ns_screen.auxiliaryTopRightArea()
                if area is not None:
                    return int(area.origin.x) + 12   # just right of the notch
        except Exception:  # noqa: BLE001
            pass
        return screen.center().x() - self._current_width() // 2

    def _current_width(self) -> int:
        if self.expanded_history:
            return HISTORY_W
        return IDLE_W if self.state == "idle" else ACTIVE_W

    def _target_rect(self) -> QRect:
        if self.expanded_history:
            w, h = HISTORY_W, HISTORY_H
        elif self.state == "idle":
            w, h = IDLE_W, IDLE_H
        else:
            w, h = ACTIVE_W, ACTIVE_H
        return QRect(self._anchor_x(), TOP_MARGIN, w, h)

    def _animate_to_target(self):
        target = self._target_rect()
        if target == self.geometry():
            return
        self._anim.stop()
        self._anim.setStartValue(self.geometry())
        self._anim.setEndValue(target)
        self._anim.start()

    # ------------------------------------------------------------ events
    def on_event(self, event: dict):
        kind = event.get("type")
        if kind == "state":
            value = event.get("value", "idle")
            if value == "error":
                self.error_until = time.time() + 3.0
                value = "idle" if self.state == "idle" else self.state
            self.state = value
            if value == "idle":
                # linger briefly so the last words stay readable, then shrink
                self._collapse_timer.start(1500)
            else:
                self._collapse_timer.stop()
                self._animate_to_target()
        elif kind == "transcript":
            self.transcript.append((event.get("role", ""), event.get("text", "")))
        elif kind == "tool":
            status = event.get("status")
            self.tool_text = event.get("label", "") if status == "start" else ""
            if status == "error":
                self.error_until = time.time() + 2.0
        elif kind == "memory":
            self.memory_count = int(event.get("count", 0))
            self.memory_until = time.time() + 2.5
        elif kind == "error":
            self.error_text = event.get("message", "")[:80]
            self.error_until = time.time() + 3.0
        self.update()

    def _collapse(self):
        if self.state == "idle" and not self.expanded_history:
            self._animate_to_target()

    def mousePressEvent(self, event):  # noqa: N802
        self.expanded_history = not self.expanded_history
        self._animate_to_target()

    def _refresh_appearance(self):
        dark = macos_is_dark()
        if dark != self.palette_.dark:
            self.palette_ = Palette(dark)
            self.update()

    # ------------------------------------------------------------ animation tick
    def _on_tick(self):
        self.phase += 0.033
        import random
        for i in range(NUM_BARS):
            if self.state == "speaking":
                target = 0.25 + 0.75 * abs(math.sin(self.phase * 5 + i * 0.7)) * random.random()
            elif self.state == "listening":
                target = 0.15 + 0.35 * random.random()
            elif self.state in ("thinking", "tool"):
                target = 0.12 + 0.10 * abs(math.sin(self.phase * 2 + i * 0.3))
            else:
                target = 0.05
            self.levels[i] += (target - self.levels[i]) * 0.25
        if self.state != "idle" or time.time() < self.error_until:
            self.update()
        elif int(self.phase * 30) % 2 == 0:   # idle glow only needs ~15fps
            self.update()

    # ------------------------------------------------------------ painting
    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette_
        rect = self.rect().adjusted(1, 1, -1, -1)
        radius = rect.height() / 2 if not self.expanded_history and self.state == "idle" else 16.0

        flashing = time.time() < self.error_until
        edge = QColor(pal.red) if flashing else QColor(pal.bg_edge)
        if flashing:  # error flash: pulsing red border
            edge.setAlpha(120 + int(100 * abs(math.sin(self.phase * 8))))

        p.setPen(QPen(edge, 1.5))
        p.setBrush(pal.bg)
        p.drawRoundedRect(rect, radius, radius)

        if self.expanded_history:
            self._paint_history(p)
        elif self.state == "idle":
            self._paint_idle(p)
        else:
            self._paint_active(p)
        p.end()

    def _paint_idle(self, p: QPainter):
        pal = self.palette_
        cx, cy = self.width() / 2, self.height() / 2
        pulse = 0.5 + 0.5 * math.sin(self.phase * 1.2)   # slow breathing glow
        glow = QColor(pal.amber)
        glow.setAlpha(int(40 + 60 * pulse))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(int(cx - 7), int(cy - 7), 14, 14)
        core = QColor(pal.amber)
        core.setAlpha(int(170 + 85 * pulse))
        p.setBrush(core)
        p.drawEllipse(int(cx - 3), int(cy - 3), 6, 6)
        # orbiting arc
        pen = QPen(QColor(pal.orange), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        start_angle = int((-self.phase * 60) % 360 * 16)
        p.drawArc(int(cx - 10), int(cy - 10), 20, 20, start_angle, 70 * 16)

    def _paint_state_row(self, p: QPainter, y: int):
        """State dot + label + memory pulse, shared by active and history views."""
        pal = self.palette_
        color = {"listening": pal.amber, "thinking": pal.orange, "tool": pal.orange,
                 "speaking": pal.amber, "error": pal.red}.get(self.state, pal.amber)
        pulse = 0.6 + 0.4 * math.sin(self.phase * 4)
        dot = QColor(color)
        dot.setAlpha(int(140 + 115 * pulse))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(dot)
        p.drawEllipse(16, y, 10, 10)

        p.setPen(QPen(pal.text))
        p.setFont(QFont("Helvetica Neue", 12, QFont.Weight.DemiBold))
        p.drawText(34, y + 10, STATE_LABEL.get(self.state, ""))

        # memory hit indicator: faint pulse with the count of memories used
        if time.time() < self.memory_until and self.memory_count:
            mem = QColor(pal.amber)
            mem.setAlpha(int(90 + 90 * abs(math.sin(self.phase * 3))))
            p.setPen(QPen(mem))
            p.setFont(QFont("Helvetica Neue", 11))
            p.drawText(self.width() - 64, y + 10, f"✦ {self.memory_count}")

    def _paint_active(self, p: QPainter):
        pal = self.palette_
        self._paint_state_row(p, 12)

        # waveform bars
        bar_area_w = self.width() - 32
        bar_w = bar_area_w / NUM_BARS
        base_y, max_h = 52, 22
        for i, level in enumerate(self.levels):
            h = max(2.0, level * max_h)
            color = QColor(pal.amber if i % 3 else pal.orange)
            color.setAlpha(200)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawRoundedRect(int(16 + i * bar_w), int(base_y - h),
                              max(2, int(bar_w * 0.55)), int(h * 2), 1.5, 1.5)

        # tool activity / error line
        info = self.error_text if time.time() < self.error_until and self.error_text else self.tool_text
        if info:
            p.setPen(QPen(pal.red if info == self.error_text and time.time() < self.error_until
                          else pal.orange))
            p.setFont(QFont("Helvetica Neue", 10))
            p.drawText(16, 72, info)

        # transcript: latest line wrapped to 3 lines, fading on overflow
        if self.transcript:
            role, text = self.transcript[-1]
            prefix = "You: " if role == "user" else ""
            lines, truncated = self._wrap(prefix + text, self.width() - 32,
                                          QFont("Helvetica Neue", 10), 3)
            p.setFont(QFont("Helvetica Neue", 10))
            y = 86 if info else 76
            for idx, line in enumerate(lines):
                color = QColor(pal.dim)
                if truncated and idx == len(lines) - 1:
                    color.setAlpha(60)   # graceful fade instead of a hard cut
                p.setPen(QPen(color))
                p.drawText(16, y + idx * 13, line)

    def _paint_history(self, p: QPainter):
        pal = self.palette_
        self._paint_state_row(p, 12)
        p.setFont(QFont("Helvetica Neue", 10))
        y = 40
        entries = list(self.transcript)[-6:]
        if not entries:
            p.setPen(QPen(pal.dim))
            p.drawText(16, y + 12, "No conversation yet.")
            return
        for role, text in entries:
            speaker = "You" if role == "user" else "Jarvis"
            p.setPen(QPen(pal.amber if role != "user" else pal.text))
            p.setFont(QFont("Helvetica Neue", 10, QFont.Weight.DemiBold))
            p.drawText(16, y + 12, speaker)
            p.setFont(QFont("Helvetica Neue", 10))
            p.setPen(QPen(pal.dim))
            lines, _ = self._wrap(text, self.width() - 90, QFont("Helvetica Neue", 10), 2)
            for idx, line in enumerate(lines):
                p.drawText(72, y + 12 + idx * 13, line)
            y += 14 + len(lines) * 13
            if y > self.height() - 20:
                break

    @staticmethod
    def _wrap(text: str, width: int, font: QFont, max_lines: int) -> tuple[list[str], bool]:
        metrics = QFontMetrics(font)
        words, lines, current = text.split(), [], ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if metrics.horizontalAdvance(candidate) <= width:
                current = candidate
            else:
                lines.append(current)
                current = word
                if len(lines) == max_lines:
                    return lines, True
        if current:
            lines.append(current)
        return lines[:max_lines], len(lines) > max_lines


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    pill = JarvisPill()  # noqa: F841 — kept alive by reference
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
