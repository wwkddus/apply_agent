# 메뉴 막대(macOS) / 알림 영역(Windows) 아이콘. 지금 단계를 하이라이트가 있는 큰 눈 얼굴로 보여 줍니다.
# 대기 · 입력 중(지팡이 흔들기) · 사용자 조작 대기(양쪽 날개) · 완료(엄지, 한 번 튀어 오름) · 확인 필요 ⊙.⊙ ! · 오류 o.O ?
# 그림은 assets/menubar/의 SVG(디자인 원본: design/menubar/make_icons.py)이며, 프레임을 바꿔 끼워 움직입니다.

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

ICON_DIR = Path(__file__).parent / "assets" / "menubar"
# Windows 알림 영역은 정사각형이라 얼굴·기호만 남긴 그림을 따로 씁니다.
WIN_ICON_DIR = ICON_DIR / "win"
# 상태별 프레임. 여러 장이면 FRAME_MS 간격으로 바꿔 끼웁니다.
FRAMES = {
    "idle": ["idle"],
    "applying": [f"applying_{index}" for index in range(8)],
    "waiting": ["waiting"],
    "success": [f"success_{index}" for index in range(4)],
    "attention": ["attention"],
    "error": ["error"],
}
# 입력 중은 계속 반복하고, 완료는 한 번만 튀어 오른 뒤 멈춥니다.
LOOPING = {"applying"}
FRAME_MS = 150
TOOLTIPS = {
    "idle": "대기 중",
    "applying": "작업 중",
    "waiting": "브라우저에서 확인해 주세요",
    "success": "완료",
    "attention": "확인이 필요한 항목이 있습니다",
    "error": "문제가 생겼습니다",
}


# Windows 작업 표시줄이 어두운지(Windows 11 기본값). 어두우면 아이콘을 흰색으로 그립니다.
def taskbar_is_dark() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            return winreg.QueryValueEx(key, "SystemUsesLightTheme")[0] == 0
    except OSError:
        return True


def render_icon(name: str, *, mask: bool, color: str = "#000000", square: bool = False) -> QIcon:
    """SVG 한 장을 여러 크기로 그려 아이콘으로 만듭니다.

    macOS(가로형): 메뉴 막대 높이(18pt)와 레티나(36px). 크기를 직접 넣어 두면 정사각형으로 줄어들지 않습니다.
    Windows(square): 정사각형 그림(assets/menubar/win)을 배율별 크기(16~48px)로.
    macOS는 템플릿(mask)이라 색을 시스템이 정하고, Windows는 color로 직접 칠합니다.
    """
    renderer = QSvgRenderer(str((WIN_ICON_DIR if square else ICON_DIR) / f"{name}.svg"))
    size = renderer.defaultSize()
    icon = QIcon()
    for height in (16, 20, 24, 32, 48) if square else (18, 36):
        width = round(height * size.width() / size.height())
        pixmap = QPixmap(width, height)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter, QRectF(0, 0, width, height))
        if color != "#000000":
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            painter.fillRect(pixmap.rect(), QColor(color))
        painter.end()
        icon.addPixmap(pixmap)
    # macOS 템플릿 이미지: 다크·라이트 메뉴 막대 색에 맞춰 시스템이 칠합니다.
    icon.setIsMask(mask)
    return icon


class MenuBarIcon(QObject):
    def __init__(self, window: QWidget, quit_app: Callable[[], None]):
        super().__init__(window)
        self.window = window
        mask = sys.platform == "darwin"
        color = "#ffffff" if taskbar_is_dark() else "#000000"
        self.icons = {
            name: render_icon(name, mask=mask, color=color, square=not mask)
            for frames in FRAMES.values() for name in frames
        }
        self.state = ""
        self.frame = 0
        self.timer = QTimer(self)
        self.timer.setInterval(FRAME_MS)
        self.timer.timeout.connect(self._next_frame)

        self.tray = QSystemTrayIcon(self.icons["idle"], self)
        menu = QMenu()
        self.toggle_action = QAction("Apply Local 열기", menu)
        self.toggle_action.triggered.connect(self.toggle_window)
        menu.addAction(self.toggle_action)
        menu.addSeparator()
        menu.addAction("종료", quit_app)
        menu.aboutToShow.connect(self._update_menu)
        self.menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._activated)
        self.set_state("idle")
        self.tray.show()

    @staticmethod
    def available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    # 상태가 바뀔 때만 애니메이션을 처음부터 다시 시작합니다.
    def set_state(self, state: str, tooltip: str = "") -> None:
        self.tray.setToolTip(f"Apply Local · {tooltip or TOOLTIPS[state]}")
        if state == self.state:
            return
        self.state = state
        self.frame = 0
        self.tray.setIcon(self.icons[FRAMES[state][0]])
        if len(FRAMES[state]) > 1:
            self.timer.start()
        else:
            self.timer.stop()

    def _next_frame(self) -> None:
        frames = FRAMES[self.state]
        self.frame += 1
        if self.frame >= len(frames):
            if self.state not in LOOPING:
                self.timer.stop()
                return
            self.frame = 0
        self.tray.setIcon(self.icons[frames[self.frame]])

    # 아이콘 클릭(Windows)·메뉴로 창을 보이거나 숨깁니다.
    def toggle_window(self) -> None:
        if self.window.isVisible():
            self.window.hide()
        else:
            self.window.show()
            self.window.raise_()
            self.window.activateWindow()

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger and sys.platform != "darwin":
            self.toggle_window()

    def _update_menu(self) -> None:
        self.toggle_action.setText("Apply Local 숨기기" if self.window.isVisible() else "Apply Local 열기")
