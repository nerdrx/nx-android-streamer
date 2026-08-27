#!/usr/bin/env python3
# nx-android-streamer — tray control panel
# The easy, non-CLI front door: sit in the system tray, start/stop the stream,
# show live status and the pairing QR, and close-to-tray instead of quitting.
#
# Needs PySide6 (and, optionally, python-qrcode; otherwise it shells out to the
# `qrencode` binary). `./start.sh setup` installs both. Everything it does is a
# thin driver over ./start.sh — no privileged work happens here.
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

try:
    from PySide6.QtCore import Qt, QProcess, QTimer, QPoint
    from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QRadialGradient, QAction, QFont
    from PySide6.QtWidgets import (
        QApplication, QSystemTrayIcon, QMenu, QWidget, QVBoxLayout, QHBoxLayout,
        QPushButton, QLabel, QLineEdit, QGridLayout, QFrame, QSizePolicy, QPlainTextEdit,
    )
except ImportError:
    sys.stderr.write(
        "nx-tray needs PySide6.\n"
        "  Arch:   sudo pacman -S pyside6 python-qrcode\n"
        "  pip:    pip install PySide6 qrcode\n"
        "Then: ./start.sh gui\n"
    )
    sys.exit(1)

ROOT = Path(__file__).resolve().parent.parent
START = ROOT / "start.sh"
CFG_DIR = Path(os.path.expanduser("~/.config/nx-android-streamer"))
CFG = CFG_DIR / "gui.json"

VOID = "#0a0a12"
ACCENT = "#7700FF"
TEXT = "#e8e8f0"
DIM = "#8a8a9a"

DEFAULTS = {"width": 1080, "height": 2400, "hz": 90, "fps": 60, "bitrate": 8000, "port": 8765}


def load_cfg():
    try:
        return {**DEFAULTS, **json.loads(CFG.read_text())}
    except Exception:
        return dict(DEFAULTS)


def save_cfg(cfg):
    try:
        CFG_DIR.mkdir(parents=True, exist_ok=True)
        CFG.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


def best_addr():
    """Same preference order start.sh uses: tailscale, then first global IPv4."""
    ts = shutil.which("tailscale")
    if ts:
        try:
            out = subprocess.run([ts, "ip", "-4"], capture_output=True, text=True, timeout=3)
            ip = out.stdout.strip().splitlines()
            if ip:
                return ip[0].strip()
        except Exception:
            pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))  # no packets sent; just picks the egress iface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def qr_pixmap(text, px=240):
    """QR as a QPixmap: python-qrcode if importable, else the qrencode binary."""
    try:
        import qrcode
        img = qrcode.make(text)
        img = img.convert("RGB").resize((px, px))
        data = img.tobytes("raw", "RGB")
        from PySide6.QtGui import QImage
        qi = QImage(data, img.width, img.height, 3 * img.width, QImage.Format_RGB888)
        return QPixmap.fromImage(qi)
    except Exception:
        pass
    qe = shutil.which("qrencode")
    if qe:
        try:
            out = subprocess.run([qe, "-o", "-", "-t", "PNG", "-s", "6", text],
                                 capture_output=True, timeout=3)
            pm = QPixmap()
            if out.returncode == 0 and pm.loadFromData(out.stdout):
                return pm.scaled(px, px, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        except Exception:
            pass
    return None


def orb_icon(state):
    """Violet orb; brightness/dot reflect state: idle | streaming | client."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    glow = {"idle": 90, "streaming": 200, "client": 255}.get(state, 90)
    g = QRadialGradient(32, 32, 26)
    g.setColorAt(0.0, QColor(0x77, 0x00, 0xFF, glow))
    g.setColorAt(1.0, QColor(0x77, 0x00, 0xFF, 20))
    p.setBrush(g)
    p.setPen(Qt.NoPen)
    p.drawEllipse(6, 6, 52, 52)
    p.setBrush(QColor(0xE8, 0xE8, 0xF0, 255 if state != "idle" else 120))
    p.drawEllipse(26, 26, 12, 12)
    if state == "client":  # a phone is linked — little confirming dot
        p.setBrush(QColor(0x3d, 0xff, 0x9e))
        p.drawEllipse(44, 10, 10, 10)
    p.end()
    return QIcon(pm)


class Panel(QWidget):
    def __init__(self, app):
        # A normal decorated window: on Wayland a frameless tool window can end
        # up unmovable or simply never mapped, and this is the panel the user
        # actually looks at.
        super().__init__(None, Qt.Window)
        self.app = app
        self.setWindowTitle("NX Android Streamer")
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setFixedWidth(340)
        self.setStyleSheet(f"""
            QWidget {{ background:{VOID}; color:{TEXT}; font-size:13px; }}
            QLabel#h {{ font-size:15px; font-weight:600; }}
            QLabel#dim {{ color:{DIM}; }}
            QPushButton {{ background:#17172a; color:{TEXT}; border:1px solid #2a2a44;
                           border-radius:9px; padding:9px 12px; }}
            QPushButton:hover {{ border-color:{ACCENT}; }}
            QPushButton#go {{ background:{ACCENT}; border:none; font-weight:600; }}
            QPushButton#go:hover {{ background:#8f2bff; }}
            QLineEdit {{ background:#101020; color:{TEXT}; border:1px solid #2a2a44;
                         border-radius:7px; padding:5px; }}
            QFrame#card {{ background:#101020; border:1px solid #20203a; border-radius:12px; }}
        """)
        self._build()

    def _card(self):
        f = QFrame()
        f.setObjectName("card")
        return f

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)

        title = QLabel("NX Android Streamer")
        title.setObjectName("h")
        outer.addWidget(title)

        self.status = QLabel("idle")
        self.status.setObjectName("dim")
        outer.addWidget(self.status)

        self.go = QPushButton("▶  Start streaming")
        self.go.setObjectName("go")
        self.go.clicked.connect(self.toggle)
        outer.addWidget(self.go)

        # live status grid
        card = self._card()
        g = QGridLayout(card)
        g.setContentsMargins(12, 10, 12, 10)
        g.setVerticalSpacing(4)
        self.lbls = {}
        for row, (k, label) in enumerate([("state", "State"), ("res", "Resolution"),
                                          ("bitrate", "Bitrate"), ("client", "Phone linked")]):
            a = QLabel(label); a.setObjectName("dim")
            b = QLabel("—")
            g.addWidget(a, row, 0, Qt.AlignLeft)
            g.addWidget(b, row, 1, Qt.AlignRight)
            self.lbls[k] = b
        outer.addWidget(card)

        # QR + address
        self.qr_card = self._card()
        qv = QVBoxLayout(self.qr_card)
        qv.setContentsMargins(12, 12, 12, 12)
        self.qr_label = QLabel(); self.qr_label.setAlignment(Qt.AlignCenter)
        self.addr_label = QLabel(); self.addr_label.setObjectName("dim")
        self.addr_label.setAlignment(Qt.AlignCenter)
        self.addr_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        qv.addWidget(self.qr_label)
        qv.addWidget(self.addr_label)
        outer.addWidget(self.qr_card)

        # settings row (port + bitrate; the common knobs)
        srow = QHBoxLayout()
        self.port_in = QLineEdit(str(self.app.cfg["port"]))
        self.br_in = QLineEdit(str(self.app.cfg["bitrate"]))
        for w, cap in ((self.port_in, "port"), (self.br_in, "kbps")):
            col = QVBoxLayout()
            lab = QLabel(cap); lab.setObjectName("dim")
            col.addWidget(lab); col.addWidget(w)
            srow.addLayout(col)
        outer.addLayout(srow)

        brow = QHBoxLayout()
        doctor = QPushButton("Repair (doctor)")
        doctor.clicked.connect(self.app.run_doctor)
        browser = QPushButton("Open in browser")
        browser.clicked.connect(self.app.open_browser)
        brow.addWidget(doctor); brow.addWidget(browser)
        outer.addLayout(brow)

        # Live log. Without this the panel is a black box: "nothing happened"
        # and "it died three seconds ago" look identical.
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(400)
        self.log_view.setFixedHeight(120)
        self.log_view.setStyleSheet(
            f"background:#08080f; color:{DIM}; border:1px solid #20203a;"
            "border-radius:8px; font-family:monospace; font-size:11px;"
        )
        self.log_view.setPlaceholderText("log output appears here once you press Start…")
        outer.addWidget(self.log_view)

        hide = QPushButton("Close to tray")
        hide.clicked.connect(self.hide)
        outer.addWidget(hide)

        self.refresh_qr()

    def log(self, line):
        self.log_view.appendPlainText(line.rstrip())

    def refresh_qr(self):
        addr = self.app.addr
        port = self.app.cfg["port"]
        uri = f"nxas://{addr}:{port}"
        pm = qr_pixmap(uri, 220)
        if pm:
            self.qr_label.setPixmap(pm)
        else:
            self.qr_label.setText("(install python-qrcode or qrencode\nto show the pairing code)")
        self.addr_label.setText(f"{uri}\nbrowser: http://{addr}:{port}")

    def toggle(self):
        self.app.toggle_stream()

    def closeEvent(self, e):  # window close = hide to tray, never quit
        e.ignore()
        self.hide()


class TrayApp:
    def __init__(self):
        self.qt = QApplication(sys.argv)
        self.qt.setQuitOnLastWindowClosed(False)  # closing the panel must not quit
        self.cfg = load_cfg()
        self.addr = best_addr()
        self.state = "idle"
        self.proc = None

        self.tray = QSystemTrayIcon(orb_icon("idle"))
        self.tray.setToolTip("NX Android Streamer — idle")
        self.panel = Panel(self)
        self._menu()
        self.tray.activated.connect(self._activated)
        self.tray.show()

    # ---- menu ---------------------------------------------------------
    def _menu(self):
        m = QMenu()
        self.act_open = QAction("Open", triggered=self.show_panel)
        self.act_toggle = QAction("Start streaming", triggered=self.toggle_stream)
        self.act_qr = QAction("Show pairing QR", triggered=self.show_panel)
        self.act_doctor = QAction("Repair (doctor)", triggered=self.run_doctor)
        self.act_browser = QAction("Open in browser", triggered=self.open_browser)
        self.act_quit = QAction("Quit", triggered=self.quit)
        for a in (self.act_open, self.act_toggle, self.act_qr, self.act_doctor, self.act_browser):
            m.addAction(a)
        m.addSeparator()
        m.addAction(self.act_quit)
        self.tray.setContextMenu(m)

    def _activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.show_panel()

    def show_panel(self):
        self.panel.refresh_qr()
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()

    # ---- stream lifecycle --------------------------------------------
    def _apply_settings(self):
        try:
            self.cfg["port"] = int(self.panel.port_in.text() or self.cfg["port"])
            self.cfg["bitrate"] = int(self.panel.br_in.text() or self.cfg["bitrate"])
        except ValueError:
            pass
        save_cfg(self.cfg)

    def toggle_stream(self):
        if self.proc and self.proc.state() != QProcess.NotRunning:
            self.stop_stream()
        else:
            self.start_stream()

    def start_stream(self):
        self._apply_settings()
        self.proc = QProcess()
        from PySide6.QtCore import QProcessEnvironment
        env = QProcessEnvironment.systemEnvironment()
        for k, ek in [("width", "NXAS_WIDTH"), ("height", "NXAS_HEIGHT"), ("hz", "NXAS_HZ"),
                      ("fps", "NXAS_FPS"), ("bitrate", "NXAS_BITRATE"), ("port", "NXAS_PORT")]:
            env.insert(ek, str(self.cfg[k]))
        env.insert("NXAS_ADDR", self.addr)
        self.proc.setProcessEnvironment(env)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._on_output)
        self.proc.finished.connect(self._on_finished)
        self.proc.start("bash", [str(START), "serve"])
        self.set_state("streaming")

    def stop_stream(self):
        if self.proc and self.proc.state() != QProcess.NotRunning:
            self.proc.terminate()  # SIGTERM -> daemon cleans up (fifo, scrcpy, avahi)
            if not self.proc.waitForFinished(4000):
                self.proc.kill()
        self.set_state("idle")

    def _on_output(self):
        if not self.proc:
            return
        chunk = bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        for line in chunk.splitlines():
            self.panel.log(line)
            low = line.lower()
            if "client" in low and "connected" in low and "gone" not in low:
                self.set_state("client")
            elif "gone" in low or "tearing down" in low:
                self.set_state("streaming")
            elif "encoder:" in low and "kbps" in low:
                # e.g. "vah264enc @ 12000 kbps"
                self.panel.lbls["bitrate"].setText(low.split("@")[-1].split(",")[0].strip())

    def _on_finished(self, *_):
        self.set_state("idle")

    # ---- misc actions -------------------------------------------------
    def run_doctor(self):
        term = next((t for t in ("konsole", "alacritty", "kitty", "xterm")
                     if shutil.which(t)), None)
        cmd = f'{START} doctor; echo; read -p "[enter] to close"'
        if term == "konsole":
            subprocess.Popen([term, "-e", "bash", "-lc", cmd])
        elif term:
            subprocess.Popen([term, "-e", "bash", "-lc", cmd])
        else:
            self.tray.showMessage("NX", "No terminal found — run ./start.sh doctor manually.")

    def open_browser(self):
        import webbrowser
        webbrowser.open(f"http://{self.addr}:{self.cfg['port']}")

    def set_state(self, state):
        self.state = state
        self.tray.setIcon(orb_icon(state))
        self.tray.setToolTip(f"NX Android Streamer — {state}")
        running = state != "idle"
        self.act_toggle.setText("Stop streaming" if running else "Start streaming")
        self.panel.go.setText("■  Stop streaming" if running else "▶  Start streaming")
        self.panel.status.setText({"idle": "idle — not streaming",
                                   "streaming": "streaming — waiting for a phone",
                                   "client": "streaming — phone linked"}[state])
        self.panel.lbls["state"].setText(state)
        self.panel.lbls["res"].setText(f"{self.cfg['width']}×{self.cfg['height']}")
        self.panel.lbls["client"].setText("yes" if state == "client" else "no")
        if not running:
            self.panel.lbls["bitrate"].setText("—")

    def quit(self):
        self.stop_stream()
        self.tray.hide()
        self.qt.quit()

    def run(self):
        self.set_state("idle")
        # Show the panel on launch. Tray-only start hides the app behind
        # Plasma's "expand" arrow and reads as "nothing happened".
        if not os.environ.get("NXAS_TRAY_ONLY"):
            self.show_panel()
        if not QSystemTrayIcon.isSystemTrayAvailable():
            print("note: no system tray host — running as a plain window", file=sys.stderr)
        return self.qt.exec()


if __name__ == "__main__":
    if not START.exists():
        sys.stderr.write(f"can't find start.sh at {START}\n")
        sys.exit(1)
    sys.exit(TrayApp().run())
