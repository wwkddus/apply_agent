# 메뉴 막대(macOS) / 알림 영역(Windows) 아이콘. 지금 단계를 네잎클로버로 보여 줍니다.
# 대기: 가끔 좌우로 흔들림 · 분석·AI 확인(진행률 모름): 바람개비처럼 회전
# 입력 중(진행률 앎): 입력한 비율만큼 잎이 시계 방향으로 채워지고, 다음 잎이 깜빡임
# 사용자 확인 필요: 가운데 ! + 확인할 때까지 금색으로 깜빡임
# 완료: 넷째 잎이 채워진 뒤 금색으로 몇 번 반짝이고 원래 색 · 오류: 회색 클로버에 ?
# 그림은 assets/clover/의 SVG(모양만, 디자인 원본: design/clover/make_icons.py)이고, 색은 여기서 칠합니다.
# 색은 메뉴에서 「초록 / 흑백」 중에 고르며, 고른 값만 QSettings에 저장합니다.
# 창이 숨겨졌거나 최소화돼 있을 때는 확인 필요·완료·오류를 시스템 알림으로도 알립니다(이력서 값은 넣지 않음).

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QRectF, QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

ICON_DIR = Path(__file__).parent / "assets" / "clover"
FRAME_MS = 150

# 프레임 = (그림 이름, 색 역할). 색 역할: base(기본색) · gold(완료 반짝임) · grey(오류) · dim(흑백에서 반짝임 꺼짐).
_FLASH_COLOR = [("clover", "gold")] * 2 + [("clover", "base")] * 2
_FLASH_MONO = [("clover", "base")] * 2 + [("clover", "dim")] * 2
# 깜빡임은 모양이 아니라 색으로(! 를 켰다 껐다 하면 작아서 잘 안 보임).
_NOTICE_COLOR = [("notice", "gold")] * 3 + [("notice", "base")] * 3
_NOTICE_MONO = [("notice", "base")] * 3 + [("notice", "dim")] * 3


def frames(state: str, style: str) -> list[tuple[str, str]]:
    """상태별 프레임. 여러 장이면 FRAME_MS 간격으로 바꿔 끼웁니다."""
    if state == "idle":  # 약 3초 쉬고 한 번 좌우로 흔들기
        return [("clover", "base")] * 20 + [(name, "base") for name in ("clover_left", "clover", "clover_right", "clover")]
    if state == "applying":
        return [(f"spin_{index}", "base") for index in range(8)]
    if state.startswith("filling_"):  # 채운 잎 n장 + 다음 잎 깜빡임(약 1초 주기)
        count = int(state.rsplit("_", 1)[1])
        following = f"fill_{count + 1}" if count < 3 else "clover"
        return [(f"fill_{count}", "base")] * 4 + [(following, "base")] * 3
    if state in {"waiting", "attention"}:  # 같은 그림: 사용자가 확인할 때까지 계속 깜빡임
        return _NOTICE_COLOR if style == "green" else _NOTICE_MONO
    if state == "success":  # 넷째 잎이 채워지고 세 번 반짝인 뒤 기본색으로 멈춤
        grow = [("fill_3", "base")] * 2 + [("clover", "base")] * 2
        return grow + (_FLASH_COLOR if style == "green" else _FLASH_MONO) * 3 + [("clover", "base")]
    return [("error", "grey")]


FILL_STATES = tuple(f"filling_{count}" for count in range(4))
STATES = ("idle", "applying", *FILL_STATES, "waiting", "attention", "success", "error")
# 완료만 한 번 재생하고 멈추고, 나머지 여러 장짜리는 계속 반복합니다.
PLAY_ONCE = {"success"}
STYLES = {"green": "초록", "mono": "흑백"}
DEFAULT_STYLE = "green"
# 창이 안 보일 때 띄우는 시스템 알림 문구. 이력서 값·사이트 주소는 넣지 않습니다.
NOTICES = {
    "waiting": "브라우저에서 로그인이나 사이트 허용 같은 확인이 필요합니다.",
    "attention": "자동 입력을 마쳤습니다. 확인이 필요한 항목이 있습니다.",
    "success": "자동 입력을 마쳤습니다. 브라우저에서 확인한 뒤 직접 제출해 주세요.",
    "error": "문제가 생겼습니다. 창을 열어 확인해 주세요.",
}


def should_notify(state: str, window: QWidget) -> bool:
    """창이 숨겨졌거나 최소화돼 있을 때만 알림을 띄웁니다(창이 보이면 창 안의 안내로 충분)."""
    return state in NOTICES and (not window.isVisible() or window.isMinimized())


TOOLTIPS = {
    "idle": "대기 중",
    "applying": "작업 중",
    **{state: "자동 입력 중" for state in FILL_STATES},
    "waiting": "브라우저에서 확인해 주세요",
    "success": "완료",
    "attention": "확인이 필요한 항목이 있습니다",
    "error": "문제가 생겼습니다",
}


# Windows 작업 표시줄이 어두운지(Windows 11 기본값). 흑백일 때 어두우면 흰색으로 그립니다.
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


def palette(style: str, dark_taskbar: bool = False) -> dict[str, tuple[str, float]]:
    """색 역할 → (색, 불투명도). 흑백은 모든 역할이 한 색이고, 반짝임은 흐리게 해서 표현합니다."""
    if style == "green":
        return {"base": ("#4cbb2b", 1.0), "gold": ("#f2b705", 1.0), "grey": ("#9a9a9a", 1.0), "dim": ("#4cbb2b", 0.35)}
    ink = "#ffffff" if dark_taskbar else "#000000"
    return {"base": (ink, 1.0), "gold": (ink, 1.0), "grey": (ink, 1.0), "dim": (ink, 0.35)}


def render_icon(name: str, color: str, *, opacity: float = 1.0, mask: bool = False) -> QIcon:
    """정사각형 SVG 한 장을 여러 크기로 그려 아이콘으로 만듭니다.

    Qt는 macOS에서 「메뉴 막대 높이 - 4pt」 이하인 가장 큰 그림을 고릅니다: 보통 막대(22pt)는 18pt(레티나 36px),
    노치가 있는 맥북(24pt)은 20pt(40px)까지 커집니다. Windows 알림 영역은 배율별 16~48px를 씁니다.
    mask(macOS 흑백)면 템플릿 이미지라 시스템이 다크·라이트 메뉴 막대에 맞춰 칠합니다.
    """
    renderer = QSvgRenderer(str(ICON_DIR / f"{name}.svg"))
    icon = QIcon()
    for side in (16, 18, 20, 22, 24, 32, 36, 40, 44, 48):
        pixmap = QPixmap(side, side)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(opacity)
        renderer.render(painter, QRectF(0, 0, side, side))
        painter.setOpacity(1.0)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), QColor(color))
        painter.end()
        icon.addPixmap(pixmap)
    icon.setIsMask(mask)
    return icon


class MenuBarIcon(QObject):
    def __init__(self, window: QWidget, quit_app: Callable[[], None]):
        super().__init__(window)
        self.window = window
        self.settings = QSettings("Apply Local", "Apply Local")
        style = str(self.settings.value("menubar/style", DEFAULT_STYLE))
        self.style = style if style in STYLES else DEFAULT_STYLE
        self.icons: dict[tuple[str, str], QIcon] = {}
        self.state = ""
        self.frame = 0
        self.timer = QTimer(self)
        self.timer.setInterval(FRAME_MS)
        self.timer.timeout.connect(self._next_frame)
        self._load_icons()

        self.tray = QSystemTrayIcon(self.icons[("clover", "base")], self)
        menu = QMenu()
        self.toggle_action = QAction("Apply Local 열기", menu)
        self.toggle_action.triggered.connect(self.toggle_window)
        menu.addAction(self.toggle_action)
        colors = menu.addMenu("아이콘 색")
        group = QActionGroup(colors)
        for key, label in STYLES.items():
            action = colors.addAction(label)
            action.setCheckable(True)
            action.setChecked(key == self.style)
            action.triggered.connect(lambda _checked=False, value=key: self.set_style(value))
            group.addAction(action)
        open_ai_setup = getattr(window, "open_ai_setup", None)
        if open_ai_setup is not None:
            menu.addAction("AI 기능 설정…", open_ai_setup)
        menu.addSeparator()
        menu.addAction("종료", quit_app)
        menu.aboutToShow.connect(self._update_menu)
        self.menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._activated)
        self.tray.messageClicked.connect(self.show_window)
        self.set_state("idle")
        self.tray.show()

    @staticmethod
    def available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    # 지금 색 설정으로 모든 프레임의 아이콘을 미리 만들어 둡니다.
    def _load_icons(self) -> None:
        mono_mac = self.style == "mono" and sys.platform == "darwin"
        colors = palette(self.style, taskbar_is_dark())
        self.icons = {
            (name, role): render_icon(name, colors[role][0], opacity=colors[role][1], mask=mono_mac)
            for state in STATES for name, role in frames(state, self.style)
        }

    # 메뉴에서 색을 바꾸면 저장하고, 지금 상태를 새 색으로 처음부터 다시 보여 줍니다.
    def set_style(self, style: str) -> None:
        if style not in STYLES or style == self.style:
            return
        self.style = style
        self.settings.setValue("menubar/style", style)
        self._load_icons()
        state, self.state = self.state, ""
        self.set_state(state)

    # 상태가 바뀔 때만 애니메이션을 처음부터 다시 시작합니다.
    def set_state(self, state: str, tooltip: str = "") -> None:
        self.tray.setToolTip(f"Apply Local · {tooltip or TOOLTIPS[state]}")
        if state == self.state:
            return
        self.state = state
        self.frame = 0
        current = frames(state, self.style)
        self.tray.setIcon(self.icons[current[0]])
        if should_notify(state, self.window):
            self.tray.showMessage("Apply Local", NOTICES[state], QSystemTrayIcon.MessageIcon.Information, 8000)
        if len(current) > 1:
            self.timer.start()
        else:
            self.timer.stop()

    def _next_frame(self) -> None:
        current = frames(self.state, self.style)
        self.frame += 1
        if self.frame >= len(current):
            if self.state in PLAY_ONCE:
                self.timer.stop()
                return
            self.frame = 0
        self.tray.setIcon(self.icons[current[self.frame]])

    # 아이콘 클릭(Windows)·메뉴로 창을 보이거나 숨깁니다.
    def toggle_window(self) -> None:
        if self.window.isVisible() and not self.window.isMinimized():
            self.window.hide()
        else:
            self.show_window()

    # 알림을 누르거나 메뉴에서 열면 숨김·최소화를 풀고 앞으로 가져옵니다.
    def show_window(self) -> None:
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger and sys.platform != "darwin":
            self.toggle_window()

    def _update_menu(self) -> None:
        self.toggle_action.setText("Apply Local 숨기기" if self.window.isVisible() else "Apply Local 열기")
