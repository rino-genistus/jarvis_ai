"""
Jarvis floating UI — Ironman amber/gold, notch-style.

Sits at the top centre of the screen as a permanent notch replacement.
Always visible above every app, including the macOS menu bar.

  Idle   → small dark pill (28 × 28 px) with a slow amber glow dot + orbiting arc
  Active → expands downward (440 × 108 px):
             state indicator · waveform · tool status  (top row)
             transcript text up to 3 lines             (bottom block)

Usage:
    python jarvis_ui.py
"""
import json
import math
import random
import socket
import sys
import time

from PyQt6.QtCore import QPointF, QRect, QRectF, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QLinearGradient,
    QPainter, QPainterPath, QPen, QRadialGradient,
)
from PyQt6.QtWidgets import QApplication, QWidget

# ── colour palette ─────────────────────────────────────────────────────────────
BG        = QColor(8,   6,   0,  242)   # near-black, faint transparency
AMBER     = QColor(255, 170,  0)         # #ffaa00 — Ironman gold
ORANGE    = QColor(255, 102,  0)         # #ff6600 — Ironman red-orange
TEXT_WARM = QColor(255, 243, 204)        # #fff3cc — warm white
TEXT_DIM  = QColor(180, 140,  60)        # muted amber

HOST, PORT = "127.0.0.1", 9_999

# ── notch geometry ─────────────────────────────────────────────────────────────
IDLE_W    = 28
IDLE_H    = 28
IDLE_Y    = 2

PILL_W    = 440
PILL_H    = 108    # increased from 82 — room for 3-line transcript
PILL_Y    = 50

BAR_COUNT = 24

_NS_STATUS_LEVEL = 25
NOTCH_GAP = 8


# ── notch position detection ───────────────────────────────────────────────────
def _get_anchor_x() -> int:
    """
    Return the x coordinate where the pill's left edge should sit (right of the
    camera notch).  Uses NSScreen.auxiliaryTopRightArea on macOS 12+ to detect
    the exact notch width.  Falls back to right-of-centre on non-notch screens.
    """
    try:
        import ctypes, ctypes.util
        from ctypes import c_double, c_void_p, c_char_p, Structure

        class CGRect(Structure):
            _fields_ = [
                ("x", c_double), ("y", c_double),
                ("w", c_double), ("h", c_double),
            ]

        lib = ctypes.CDLL(ctypes.util.find_library("objc"))
        lib.sel_registerName.restype  = c_void_p
        lib.sel_registerName.argtypes = [c_char_p]
        lib.objc_getClass.restype     = c_void_p
        lib.objc_getClass.argtypes    = [c_char_p]

        lib.objc_msgSend.restype  = c_void_p
        lib.objc_msgSend.argtypes = [c_void_p, c_void_p]
        main_screen = lib.objc_msgSend(
            lib.objc_getClass(b"NSScreen"),
            lib.sel_registerName(b"mainScreen"),
        )

        if main_screen:
            lib.objc_msgSend.restype  = CGRect
            lib.objc_msgSend.argtypes = [c_void_p, c_void_p]
            rect = lib.objc_msgSend(
                main_screen,
                lib.sel_registerName(b"auxiliaryTopRightArea"),
            )
            if rect.w > 0:
                return int(rect.x) + NOTCH_GAP
    except Exception:
        pass

    return QApplication.primaryScreen().geometry().width() // 2 + NOTCH_GAP


# ── native window lift ─────────────────────────────────────────────────────────
def _raise_above_menu_bar(widget: QWidget) -> None:
    """Lift the window above the menu bar and keep it on every Space."""
    try:
        import ctypes, ctypes.util
        lib = ctypes.CDLL(ctypes.util.find_library("objc"))

        lib.sel_registerName.restype  = ctypes.c_void_p
        lib.sel_registerName.argtypes = [ctypes.c_char_p]
        lib.objc_getClass.restype     = ctypes.c_void_p
        lib.objc_getClass.argtypes    = [ctypes.c_char_p]

        lib.objc_msgSend.restype  = ctypes.c_void_p
        lib.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        ns_win = lib.objc_msgSend(
            int(widget.winId()),
            lib.sel_registerName(b"window"),
        )

        lib.objc_msgSend.restype  = ctypes.c_void_p
        lib.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        ns_app = lib.objc_msgSend(
            lib.objc_getClass(b"NSApplication"),
            lib.sel_registerName(b"sharedApplication"),
        )
        if ns_app:
            lib.objc_msgSend.restype  = None
            lib.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
            lib.objc_msgSend(
                ns_app,
                lib.sel_registerName(b"setActivationPolicy:"),
                ctypes.c_long(1),
            )

        if ns_win:
            lib.objc_msgSend.restype  = None
            lib.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
            lib.objc_msgSend(
                ns_win,
                lib.sel_registerName(b"setLevel:"),
                ctypes.c_long(_NS_STATUS_LEVEL),
            )
            lib.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
            lib.objc_msgSend(
                ns_win,
                lib.sel_registerName(b"setCollectionBehavior:"),
                ctypes.c_ulong(1 | 16 | 256),
            )
    except Exception:
        pass


# ── socket reader thread ───────────────────────────────────────────────────────
class SocketReader(QThread):
    event_received = pyqtSignal(dict)

    def run(self):
        while True:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.connect((HOST, PORT))
                buf = b""
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        try:
                            self.event_received.emit(json.loads(line))
                        except json.JSONDecodeError:
                            pass
                sock.close()
                time.sleep(1)
            except (ConnectionRefusedError, OSError):
                time.sleep(1)


# ── main widget ────────────────────────────────────────────────────────────────
class JarvisBar(QWidget):
    def __init__(self):
        super().__init__()

        self._state       = "idle"
        self._transcript  = ""
        self._tool_text   = ""
        self._memory_text = ""
        self._waveform    = [0.0] * BAR_COUNT

        self._tick      = 0
        self._pulse     = 0.0
        self._pulse_dir = 1
        self._anim_t    = 0.0
        self._target_t  = 0.0

        # ── animation state ────────────────────────────────────────────────
        self._ring_phase = 0.0   # 0..360 — drives idle orbiting arc
        self._ripple_r   = 0.0   # 0..1   — listening ripple expansion

        self._anchor_x = _get_anchor_x()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self._apply_geometry(0.0)
        self.show()
        _raise_above_menu_bar(self)
        self._apply_geometry(self._anim_t)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick_frame)
        self._timer.start(16)

        self._reader = SocketReader(self)
        self._reader.event_received.connect(self._on_event)
        self._reader.start()

    # ── geometry ───────────────────────────────────────────────────────────────
    def _apply_geometry(self, t: float) -> None:
        ease = t * t * (3.0 - 2.0 * t)
        w = int(IDLE_W + (PILL_W - IDLE_W) * ease)
        h = int(IDLE_H + (PILL_H - IDLE_H) * ease)
        y = int(IDLE_Y + (PILL_Y - IDLE_Y) * ease)
        self.setGeometry(self._anchor_x, y, w, h)

    # ── socket events ──────────────────────────────────────────────────────────
    def _on_event(self, ev: dict):
        kind = ev.get("event")

        if kind == "state":
            self._state    = ev.get("value", "idle")
            self._target_t = 0.0 if self._state == "idle" else 1.0
            if self._state == "idle":
                self._tool_text = self._memory_text = ""

        elif kind == "transcript":
            role   = ev.get("role", "")
            text   = ev.get("text", "")
            prefix = "You: " if role == "user" else "Jarvis: "
            self._transcript = prefix + text   # full text — multiline draw handles clipping

        elif kind == "tool":
            name   = ev.get("name", "")
            status = ev.get("status", "running")
            self._tool_text = (name.replace("_", " ").title() + "…") if status == "running" else ""

        elif kind == "memory":
            n = ev.get("count", 0)
            self._memory_text = f"{n} memor{'y' if n == 1 else 'ies'}" if n else ""

        elif kind == "waveform":
            data = ev.get("data", [])
            if data:
                self._waveform = (data + [0.0] * BAR_COUNT)[:BAR_COUNT]

    # ── animation frame ────────────────────────────────────────────────────────
    def _tick_frame(self):
        self._tick = (self._tick + 1) % 360

        speed = {
            "idle": 0.007, "listening": 0.025,
            "thinking": 0.035, "speaking": 0.02,
        }.get(self._state, 0.01)
        self._pulse += speed * self._pulse_dir
        if self._pulse >= 1.0:
            self._pulse, self._pulse_dir = 1.0, -1
        elif self._pulse <= 0.0:
            self._pulse, self._pulse_dir = 0.0, 1

        # idle orbiting arc — one full orbit every ~10 s at 60 fps
        self._ring_phase = (self._ring_phase + 0.6) % 360

        # listening ripple — cycles 0→1 continuously while listening
        if self._state == "listening":
            self._ripple_r = (self._ripple_r + 0.02) % 1.0
        else:
            self._ripple_r = 0.0

        # smooth expand / collapse
        if abs(self._anim_t - self._target_t) > 0.003:
            self._anim_t += (self._target_t - self._anim_t) * 0.12
            self._apply_geometry(self._anim_t)
        elif self._anim_t != self._target_t:
            self._anim_t = self._target_t
            self._apply_geometry(self._anim_t)

        # waveform simulation
        if self._state == "listening":
            for i in range(BAR_COUNT):
                self._waveform[i] += (random.uniform(0.1, 0.9) - self._waveform[i]) * 0.3
        elif self._state == "speaking":
            # bell-curve envelope so edges are quieter, centre is loudest
            for i in range(BAR_COUNT):
                env    = math.sin(math.pi * i / (BAR_COUNT - 1))
                target = random.uniform(0.08, 0.88) * (0.35 + 0.65 * env)
                self._waveform[i] += (target - self._waveform[i]) * 0.18
        else:
            for i in range(BAR_COUNT):
                self._waveform[i] *= 0.82

        self.update()

    # ── helpers ────────────────────────────────────────────────────────────────
    def _gc(self, base: QColor, alpha: int) -> QColor:
        c = QColor(base)
        c.setAlpha(max(0, min(255, alpha)))
        return c

    def _draw_multiline(self, p: QPainter, text: str, font: QFont,
                        x: int, y: int, w: int, max_lines: int,
                        color: QColor) -> None:
        """Word-wrap text into at most max_lines; appends "…" when truncated."""
        fm = QFontMetrics(font)
        lh = fm.lineSpacing()

        words = text.split()
        lines: list[str] = []
        cur = ""
        for word in words:
            candidate = f"{cur} {word}".strip()
            if fm.horizontalAdvance(candidate) <= w:
                cur = candidate
            else:
                if cur:
                    lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)

        if len(lines) > max_lines:
            lines = lines[:max_lines]
            last = lines[-1]
            while fm.horizontalAdvance(last + "…") > w:
                if " " in last:
                    last = last.rsplit(" ", 1)[0]
                elif last:
                    last = last[:-1]
                else:
                    break
            lines[-1] = last + "…"

        p.setFont(font)
        p.setPen(color)
        for i, line in enumerate(lines):
            p.drawText(
                QRect(x, y + i * lh, w, lh),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                line,
            )

    # ── painting ───────────────────────────────────────────────────────────────
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._paint(p)

    def _paint(self, p: QPainter):
        w, h = self.width(), self.height()
        t    = self._anim_t

        # pill background
        corner = min(h // 2, 20)
        pill   = QPainterPath()
        pill.addRoundedRect(QRectF(0, 0, w, h), corner, corner)
        p.fillPath(pill, BG)

        # amber border — brightens when expanded
        b_alpha = int(45 + self._pulse * 40 + t * 100)
        pen = QPen(self._gc(AMBER, b_alpha))
        pen.setWidthF(0.8 + t * 0.8)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(pill)

        if t < 0.6:
            self._paint_idle_dot(p, w, h, fade=1.0 - t / 0.6)

        if t > 0.15:
            self._paint_expanded(p, w, fade=min(1.0, (t - 0.15) / 0.55))

    # ── idle: glow dot + orbiting arc ─────────────────────────────────────────
    def _paint_idle_dot(self, p: QPainter, w: int, h: int, fade: float):
        cx, cy, r = w // 2, h // 2, 5

        # layered glow halo
        ga = int(fade * (30 + self._pulse * 60))
        for extra in (8, 5, 2):
            g = QRadialGradient(cx, cy, r + extra)
            g.setColorAt(0, self._gc(AMBER, ga))
            g.setColorAt(1, self._gc(AMBER, 0))
            p.setBrush(QBrush(g))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QRectF(cx - r - extra, cy - r - extra,
                                  (r + extra) * 2, (r + extra) * 2))

        # core dot: gold centre → amber → orange edge
        ca = int(fade * (150 + self._pulse * 105))
        g  = QRadialGradient(cx - 1, cy - 1, r)
        g.setColorAt(0.0, self._gc(QColor(255, 225, 130), ca))
        g.setColorAt(0.6, self._gc(AMBER, ca))
        g.setColorAt(1.0, self._gc(ORANGE, max(0, ca - 55)))
        p.setBrush(QBrush(g))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

        # orbiting arc — 80° segment that slowly rotates
        arc_r = r + 7
        arc_a = int(fade * (40 + self._pulse * 70))
        pen_arc = QPen(self._gc(AMBER, arc_a))
        pen_arc.setWidthF(1.0)
        pen_arc.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen_arc)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(
            QRectF(cx - arc_r, cy - arc_r, arc_r * 2, arc_r * 2),
            int(self._ring_phase * 16),
            80 * 16,
        )

    # ── expanded: indicator · waveform · status · transcript ──────────────────
    def _paint_expanded(self, p: QPainter, w: int, fade: float):
        # ── layout ────────────────────────────────────────────────────────────
        ROW_WAVE     = 28      # y-centre of indicator / waveform / status row
        TRANSCRIPT_Y = 50      # y-top of transcript text block

        STATUS_X = w - 152
        STATUS_W = 144
        BAR_L    = 54
        BAR_R    = STATUS_X - 6
        TEXT_X   = 12
        TEXT_W   = w - 24

        a = fade

        # ── state indicator (left) ─────────────────────────────────────────
        sx, sy, dr = 26, ROW_WAVE, 5
        da = int(a * (140 + self._pulse * 115))

        if self._state == "thinking":
            # outer arc clockwise
            pen = QPen(self._gc(AMBER, da))
            pen.setWidthF(2.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawArc(
                QRectF(sx - dr - 2, sy - dr - 2, (dr + 2) * 2, (dr + 2) * 2),
                (-self._tick * 4) * 16,
                270 * 16,
            )
            # inner arc counter-clockwise, orange
            ia = int(a * (70 + self._pulse * 80))
            pen2 = QPen(self._gc(ORANGE, ia))
            pen2.setWidthF(1.3)
            pen2.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen2)
            p.drawArc(
                QRectF(sx - dr + 1, sy - dr + 1, (dr - 1) * 2, (dr - 1) * 2),
                (self._tick * 3 * 16) % (360 * 16),
                180 * 16,
            )
        else:
            # glow dot for listening / speaking
            g = QRadialGradient(sx - 1, sy - 1, dr)
            g.setColorAt(0.0, self._gc(QColor(255, 225, 130), da))
            g.setColorAt(0.6, self._gc(AMBER, da))
            g.setColorAt(1.0, self._gc(ORANGE, max(0, da - 55)))
            p.setBrush(QBrush(g))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QRectF(sx - dr, sy - dr, dr * 2, dr * 2))

            # listening: two staggered ripple rings expanding outward
            if self._state == "listening":
                for phase_offset in (0.0, 0.5):
                    rr = (self._ripple_r + phase_offset) % 1.0
                    ra = int((1.0 - rr) * a * 130)
                    if ra > 5:
                        pen_rip = QPen(self._gc(AMBER, ra))
                        pen_rip.setWidthF(0.9)
                        p.setPen(pen_rip)
                        p.setBrush(Qt.BrushStyle.NoBrush)
                        p.drawEllipse(QPointF(sx, sy), dr + rr * 11, dr + rr * 11)

        # state label
        label = {
            "listening": "LISTENING",
            "thinking":  "THINKING",
            "speaking":  "SPEAKING",
        }.get(self._state, "")
        if label:
            p.setFont(QFont("Helvetica Neue", 7))
            p.setPen(self._gc(TEXT_DIM, int(a * 165)))
            p.drawText(
                QRect(sx - 26, sy + dr + 3, 52, 11),
                Qt.AlignmentFlag.AlignHCenter,
                label,
            )

        # ── waveform bars ──────────────────────────────────────────────────
        area_w  = BAR_R - BAR_L
        bar_gap = area_w / BAR_COUNT
        bar_w   = bar_gap * 0.52
        max_bh  = 18

        # speaking: soft warm glow behind bars
        if self._state == "speaking":
            glow_a = int(a * self._pulse * 24)
            if glow_a > 2:
                glow = QLinearGradient(float(BAR_L), 0.0, float(BAR_R), 0.0)
                glow.setColorAt(0.0, self._gc(AMBER, 0))
                glow.setColorAt(0.5, self._gc(AMBER, glow_a))
                glow.setColorAt(1.0, self._gc(AMBER, 0))
                p.fillRect(QRectF(BAR_L, ROW_WAVE - 14, area_w, 28), QBrush(glow))

        for i, amp in enumerate(self._waveform):
            bh   = max(2.0, amp * max_bh)
            bx   = BAR_L + i * bar_gap
            by   = ROW_WAVE - bh / 2
            ba   = int(a * (60 + amp * 195))
            grad = QLinearGradient(bx, by, bx, by + bh)
            grad.setColorAt(0, self._gc(AMBER,  ba))
            grad.setColorAt(1, self._gc(ORANGE, max(0, ba // 2)))
            p.setBrush(QBrush(grad))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(QRectF(bx, by, bar_w, bh), bar_w / 2, bar_w / 2)

        # ── tool / memory status (right of waveform) ───────────────────────
        sub = "  ·  ".join(x for x in (self._tool_text, self._memory_text) if x)
        if sub:
            p.setFont(QFont("Helvetica Neue", 8))
            p.setPen(self._gc(TEXT_DIM, int(a * 155)))
            p.drawText(
                QRect(STATUS_X, ROW_WAVE - 8, STATUS_W, 16),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                sub,
            )

        # ── transcript — full width, up to 3 lines with trailing "…" ──────
        if self._transcript:
            self._draw_multiline(
                p, self._transcript,
                QFont("Helvetica Neue", 9),
                TEXT_X, TRANSCRIPT_Y, TEXT_W, 3,
                self._gc(TEXT_WARM, int(a * 215)),
            )


# ── entry ──────────────────────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Jarvis")
    bar = JarvisBar()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
