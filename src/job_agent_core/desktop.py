# 데스크톱 화면(PySide6). 화면 위쪽에 떠 있는 캡슐 바 하나로,
# 1. 이력서 업로드 → 2. 채용 URL 업로드 → 3. 자동 입력 → 4. AI 2차 검증 → 5. 완료 순서를 안내합니다.
# 평소에는 한 줄(상태 + 진행 막대)만 보이고, 오른쪽 화살표로 아래로 펼칩니다.

# 실제 작업은 로컬 API에 요청하고,
# 오래 걸리는 요청은 백그라운드 스레드에서 실행해 화면이 멈추지 않게 합니다.
# 최종 제출은 사용자가 브라우저에서 직접 합니다.

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

import httpx
from PySide6.QtCore import QByteArray, QElapsedTimer, QObject, QPoint, QRectF, QRunnable, QSettings, QSize, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QColor, QDesktopServices, QFont, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import ollama_setup
from .constants import EDUCATION_LEVELS
from .tray import MenuBarIcon


# 내부 상태 코드 → 화면에 보여줄 한국어 문구.
STATUS_LABELS = {
    "planned": "입력 가능",
    "kept_existing": "기존 값 유지",
    "mapping_review_required": "확인 필요",
    "name_confirmation_required": "이름 확인 필요",
    "missing_required": "이력서 값 없음",
    "missing_optional": "이력서 값 없음",
    "readonly": "읽기 전용",
    "unsupported": "직접 처리",
    "needs_human": "직접 확인",
    "unmapped_required": "필수 항목 미연결",
    "unmapped_optional": "미연결",
    "skipped": "지원 범위 아님",
    "filled_verified": "입력·검증 완료",
    "missing_resume_value": "이력서 값 없음",
    "failed_validation": "검증 실패",
    "field_not_found": "필드 변경됨",
    "disabled_on_page": "입력 불가(비활성)",
    "filled_llm_unconfirmed": "입력됨 · 확인 필요",
    "filled_review": "입력됨 · 확인 필요",
    "user_only": "직접 처리",
    "pending_lookup": "이력서 확인 중",
    "not_in_resume": "이력서에 근거 없음",
}

# 사용자가 '확인 필요'에서 해결할 수 있는 상태들.
REVIEWABLE = {
    "needs_human", "failed_validation", "missing_resume_value", "field_not_found", "filled_llm_unconfirmed", "filled_review",
}
# '모두 보기'에서 값을 고를 수 있는 행(이력서에 없는 값도 사용자가 직접 입력 가능).
DETAIL = REVIEWABLE | {"not_in_resume"}
# 규칙 입력 후 AI 2차 확인으로 보낼 행, 그리고 결과 목록의 정렬 순서.
SECOND_CHECK = {"pending_lookup", "missing_resume_value", "needs_human"}
ROW_ORDER = {"not_in_resume": 1, "pending_lookup": 1, "user_only": 2, "disabled_on_page": 3, "kept_existing": 4, "filled_verified": 5}

# 진행 단계(캡슐 바를 펼쳤을 때 보이는 5단계).
STEPS = ("1. 이력서 업로드", "2. 채용 URL 업로드", "3. 자동 입력", "4. AI 2차 검증", "5. 완료")
# 화면 상태 → 몇 번째 단계인지.
STAGE_STEP = {"ai": 0, "upload": 0, "parsing": 0, "resume": 0, "url": 1, "filling": 2, "checking": 3, "done": 4, "review": 4}

# 이력서 확인 화면에서 보여 줄 이름(이력서 경로 → 한국어).
RESUME_LABELS = {
    "person.name.full": "이름",
    "person.name.family": "성",
    "person.name.given": "이름(성 제외)",
    "person.name_en.full": "영문 이름",
    "person.name_en.family": "영문 성",
    "person.name_en.given": "영문 이름(성 제외)",
    "person.birth_date": "생년월일",
    "person.phone": "휴대전화",
    "person.email": "이메일",
    "person.email_secondary": "보조 이메일",
    "person.postal_code": "우편번호",
    "person.address": "주소",
    "person.address_detail": "상세 주소",
    "person.military_status": "병역",
    "person.disability_status": "장애 여부",
    "person.veteran_status": "보훈 여부",
    "skills": "보유 기술",
}
EDUCATION_LABELS = {"high_school": "고등학교", "previous_university": "편입 전 대학", "university": "대학교", "graduate_school": "대학원"}
EDUCATION_FIELD_LABELS = {
    "school_name": "학교명", "campus_type": "본교/분교", "degree": "학위", "major": "전공", "double_major": "복수전공",
    "admission_type": "입학구분", "admission_date": "입학일", "graduation_status": "졸업구분",
    "graduation_date": "졸업일", "gpa": "학점", "gpa_scale": "만점", "region": "소재지",
}
# 이력서 확인 화면 기본 행(나머지는 '모두 보기').
RESUME_SUMMARY = ("person.birth_date", "person.phone", "person.email", "person.address")

# 템플릿 이력서(앱에 함께 들어 있음). 브라우저에서 값을 고친 뒤 PDF로 저장합니다.
TEMPLATE_PATH = Path(__file__).parent / "assets" / "resume_template.html"

# 앱 아이콘(디자인 원본: design/logo/make_app_icon.py). 작은 크기는 글자를 뺀 간단한 그림.
APP_ICON_PATHS = (Path(__file__).parent / "assets" / "app_icon.png", Path(__file__).parent / "assets" / "app_icon_small.png")


def app_icon() -> QIcon:
    icon = QIcon()
    for path in APP_ICON_PATHS:
        icon.addFile(str(path))
    return icon

# AI 보조 기능 선택(QSettings): "on" 또는 "off". 값이 없으면 첫 실행이라 설정 화면을 먼저 보여 줍니다.
AI_CHOICE_KEY = "ai/choice"

# 색상(디자인 시안과 같은 흑백 팔레트).
INK = "#111111"
MUTED = "#8a8a8a"
LINE = "#e4e4e4"
SOFT = "#f4f5f7"

CAPSULE_WIDTH = 620

# 아이콘(24x24 선 그림).
CHEVRON_DOWN = '<polyline points="6 9 12 15 18 9"/>'
CHEVRON_UP = '<polyline points="6 15 12 9 18 15"/>'
PIN = '<path d="M9 3h6l-1 7 3 3H7l3-3z"/><line x1="12" y1="13" x2="12" y2="21"/>'
PIN_ON = '<path d="M9 3h6l-1 7 3 3H7l3-3z" fill="#111111"/><line x1="12" y1="13" x2="12" y2="21"/>'
CHECK_DONE = '<circle cx="12" cy="12" r="10" fill="#111111" stroke="none"/><polyline points="7 12.5 10.5 16 17 9" stroke="#ffffff"/>'


def resume_label(path: str) -> str:
    if path in RESUME_LABELS:
        return RESUME_LABELS[path]
    parts = path.split(".")
    if path == "education.high_school.major":
        return "고등학교 계열"
    if len(parts) == 3 and parts[0] == "education":
        return f"{EDUCATION_LABELS.get(parts[1], parts[1])} {EDUCATION_FIELD_LABELS.get(parts[2], parts[2])}"
    return path


def svg_icon(body: str, size: int = 18) -> QIcon:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#111111" '
        f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
    )
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    pixmap = QPixmap(size * 3, size * 3)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(3)
    return QIcon(pixmap)


# 체크박스 체크·드롭다운 화살표 그림. 스타일시트는 파일 경로로만 그림을 받으므로 임시 폴더에 한 번 써 둡니다.
STYLE_IMAGES = {
    "check": '<polyline points="3.5 8.5 6.5 11.5 12.5 5" fill="none" stroke="#ffffff" stroke-width="2" '
             'stroke-linecap="round" stroke-linejoin="round"/>',
    "arrow": '<path d="M4.5 6.5h7L8 10.5z" fill="#111111"/>',
}


def style_image(name: str) -> str:
    path = Path(tempfile.gettempdir()) / f"apply_local_{name}.svg"
    if not path.exists():
        path.write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">{STYLE_IMAGES[name]}</svg>', encoding="utf-8"
        )
    return path.as_posix()


# API 요청 실패 시 사용자에게 보여줄 오류.
class DesktopApiError(RuntimeError):
    pass


# 로컬 API를 부르는 클라이언트. 백그라운드 작업 안에서만 사용합니다(화면 멈춤 방지).
class DesktopApiClient:
    """Synchronous client used only inside background Qt workers."""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-Job-Agent-Token": token, "Accept": "application/json"}

    # 공통 요청 함수: 토큰 헤더 추가, 시간 초과·연결 오류·서버 오류를 사용자용 문구로 바꿉니다.
    def _request(self, method: str, path: str, *, timeout: float = 30, **kwargs: Any) -> dict[str, Any]:
        headers = {**self.headers, **kwargs.pop("headers", {})}
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                timeout=timeout,
                **kwargs,
            )
        except httpx.TimeoutException as error:
            raise DesktopApiError("요청 시간이 초과되었습니다.") from error
        except httpx.HTTPError as error:
            raise DesktopApiError("로컬 서비스에 연결할 수 없습니다.") from error
        if response.is_error:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            raise DesktopApiError(detail or f"요청을 처리하지 못했습니다. ({response.status_code})")
        return response.json()

    # 작업 생성(URL은 나중에) + 이력서 업로드. 앞서 고친 값(edits)이 있으면 다시 적용합니다.
    # 업로드가 실패하면 만든 작업을 취소해 민감 데이터를 남기지 않습니다.
    def prepare(self, resume_path: Path, edits: list[tuple[str, str, str]] | None = None) -> dict[str, Any]:
        task = self._request("POST", "/api/tasks", json={})
        task_id = task["task_id"]
        try:
            with resume_path.open("rb") as handle:
                preview = self._request(
                    "POST",
                    f"/api/tasks/{task_id}/resume",
                    files={"file": (resume_path.name, handle, "application/pdf")},
                    timeout=240,  # A local LLM may be used for free-form resumes.
                )
            sources = dict(preview.get("sources", {}))
            for kind, first, second in edits or []:
                if kind == "name":
                    preview = self.confirm_name(task_id, first, second)
                else:
                    preview = self.edit_resume(task_id, first, second)
                    sources[first] = "user"
            preview["sources"] = sources
        except Exception:
            try:
                self.cancel(task_id)
            except DesktopApiError:
                pass
            raise
        return {"task": task, "preview": preview}

    # 아래 메서드들은 API 엔드포인트를 하나씩 감싼 것입니다. 단계별로 시간 제한이 다릅니다(LLM 확인은 길게).
    def set_target(self, task_id: str, target_url: str) -> dict[str, Any]:
        return self._request("PUT", f"/api/tasks/{task_id}/target", json={"target_url": target_url})

    def confirm_name(self, task_id: str, family: str, given: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/tasks/{task_id}/confirm-name",
            json={"family": family, "given": given},
        )

    def cancel(self, task_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/tasks/{task_id}/cancel", timeout=15)

    def open_browser(self, task_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/tasks/{task_id}/browser/open", timeout=60)

    def browser_status(self, task_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/tasks/{task_id}/browser/status")

    def approve_domain(self, task_id: str, hostname: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/tasks/{task_id}/browser/approve-domain",
            json={"hostname": hostname},
            timeout=45,
        )

    def fill_browser(self, task_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/tasks/{task_id}/browser/fill", timeout=120)

    def fill_progress(self, task_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/tasks/{task_id}/browser/progress", timeout=5)

    def lookup_browser(self, task_id: str, rows: list[dict[str, str]]) -> dict[str, Any]:
        # The local-LLM second check can take a few minutes on a laptop.
        return self._request("POST", f"/api/tasks/{task_id}/browser/lookup", json={"rows": rows}, timeout=300)

    def skip_lookup(self, task_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/tasks/{task_id}/browser/lookup/skip", timeout=10)

    def next_page(self, task_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/tasks/{task_id}/browser/next", timeout=30)

    def fill_field(
        self, task_id: str, item: dict[str, Any], value: str, *, chosen: bool = False
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/tasks/{task_id}/browser/fill-field",
            json={
                "form_field": item["form_field"],
                "label": item.get("label", ""),
                "value": value,
                "fingerprint": item.get("fingerprint", ""),
                "chosen_from_candidates": chosen,
            },
            timeout=45,
        )

    def edit_resume(self, task_id: str, path: str, value: str) -> dict[str, Any]:
        return self._request("PATCH", f"/api/tasks/{task_id}/resume", json={"path": path, "value": value})

    def set_ai(self, enabled: bool) -> dict[str, Any]:
        return self._request("PUT", "/api/settings/ai", json={"enabled": enabled})


# 백그라운드 작업 → 화면으로 결과를 전달하는 신호(성공/실패/종료).
class OperationSignals(QObject):
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()


# API 호출을 백그라운드 스레드에서 실행하는 작업 단위.
class ApiOperation(QRunnable):
    def __init__(self, function: Callable[[], object]):
        super().__init__()
        self.function = function
        self.signals = OperationSignals()
        # 사용자가 기다리지 않기로 한 작업(AI 2차 검증 건너뛰기). 결과가 와도 화면에 반영하지 않습니다.
        self.detached = False

    # 작업 실행. 오류는 안전한 문자열로만 화면에 전달합니다.
    def run(self) -> None:
        try:
            result = self.function()
        except Exception as error:  # The UI receives a safe, local error string only.
            self.signals.failed.emit(str(error) or type(error).__name__)
        else:
            self.signals.succeeded.emit(result)
        finally:
            self.signals.finished.emit()


# AI 기능 설치(Ollama 확인 → 설치 → 실행 → 모델 받기)를 백그라운드에서 실행합니다. 진행 문구와 비율을 보냅니다.
class SetupSignals(QObject):
    progress = Signal(str, object)
    succeeded = Signal(object)
    failed = Signal(str)


class SetupOperation(QRunnable):
    def __init__(self, prepare: Callable[..., object] = ollama_setup.prepare):
        super().__init__()
        self.prepare = prepare
        self.cancel = threading.Event()
        self.signals = SetupSignals()

    def run(self) -> None:
        try:
            result = self.prepare(lambda text, value: self.signals.progress.emit(text, value), self.cancel)
        except ollama_setup.OllamaSetupError as error:
            self.signals.failed.emit(str(error))
        except Exception as error:  # 화면에는 안전한 문구만 보냅니다.
            self.signals.failed.emit(f"AI 기능을 준비하지 못했습니다. ({type(error).__name__})")
        else:
            self.signals.succeeded.emit(result)


# 진행 막대. 값(0~1)을 보여 주거나, 진행률을 모를 때는 짧은 막대가 좌우로 움직입니다.
class ProgressLine(QWidget):
    def __init__(self, height: int = 6):
        super().__init__()
        self.setFixedHeight(height)
        self.busy = False
        self.value = 0.0
        self.offset = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

    def set_value(self, value: float) -> None:
        self.busy = False
        self.value = max(0.0, min(1.0, value))
        self.timer.stop()
        self.update()

    def set_busy(self) -> None:
        self.busy = True
        if not self.timer.isActive():
            self.timer.start(16)
        self.update()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.timer.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self.busy:
            self.timer.start(16)
        super().showEvent(event)

    def _tick(self) -> None:
        self.offset = (self.offset + 0.008) % 1.3
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        rect = QRectF(self.rect())
        radius = rect.height() / 2
        painter.setBrush(QColor(LINE))
        painter.drawRoundedRect(rect, radius, radius)
        painter.setBrush(QColor(INK))
        if self.busy:
            painter.setClipRect(rect)
            width = rect.width() * 0.3
            painter.drawRoundedRect(QRectF((self.offset - 0.3) * rect.width(), 0, width, rect.height()), radius, radius)
        elif self.value > 0:
            painter.drawRoundedRect(QRectF(0, 0, max(rect.width() * self.value, rect.height()), rect.height()), radius, radius)


# 5단계 표시. 끝난 단계는 검은 원+체크, 지금 단계는 테두리+점, 남은 단계는 회색 테두리입니다.
class Stepper(QWidget):
    def __init__(self, labels: tuple[str, ...]):
        super().__init__()
        self.labels = labels
        self.current = 0
        self.setFixedHeight(54)

    def set_current(self, index: int) -> None:
        self.current = index
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        count = len(self.labels)
        margin = 52.0
        step = (self.width() - 2 * margin) / (count - 1)
        xs = [margin + index * step for index in range(count)]
        y, radius = 12.0, 9.0
        for index in range(count - 1):
            painter.setPen(QPen(QColor(INK if index < self.current else LINE), 2))
            painter.drawLine(int(xs[index] + radius), int(y), int(xs[index + 1] - radius), int(y))
        font = QFont(self.font())
        font.setPixelSize(12)
        for index, x in enumerate(xs):
            if index < self.current:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(INK))
                painter.drawEllipse(QRectF(x - radius, y - radius, radius * 2, radius * 2))
                painter.setPen(QPen(QColor("#ffffff"), 1.8))
                painter.drawPolyline([QPoint(int(x - 4), int(y)), QPoint(int(x - 1), int(y + 3)), QPoint(int(x + 4), int(y - 3))])
            elif index == self.current:
                painter.setPen(QPen(QColor(INK), 2))
                painter.setBrush(QColor("#ffffff"))
                painter.drawEllipse(QRectF(x - radius + 1, y - radius + 1, radius * 2 - 2, radius * 2 - 2))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(INK))
                painter.drawEllipse(QRectF(x - 4, y - 4, 8, 8))
            else:
                painter.setPen(QPen(QColor("#c9c9c9"), 1.5))
                painter.setBrush(QColor("#ffffff"))
                painter.drawEllipse(QRectF(x - radius + 1, y - radius + 1, radius * 2 - 2, radius * 2 - 2))
            font.setBold(index == self.current)
            painter.setFont(font)
            painter.setPen(QColor(INK if index <= self.current else MUTED))
            painter.drawText(QRectF(x - 60, y + radius + 6, 120, 20), Qt.AlignmentFlag.AlignCenter, self.labels[index])


# PDF를 끌어 놓거나 '파일 선택'으로 고르는 점선 영역.
class DropZone(QFrame):
    dropped = Signal(object)

    def __init__(self):
        super().__init__()
        self.setObjectName("dropZone")
        self.setAcceptDrops(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 22, 20, 20)
        layout.setSpacing(8)
        arrow = QLabel("↑")
        arrow.setObjectName("arrow")
        arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text = QLabel("PDF 이력서를 여기로 끌어 놓거나")
        self.text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.button = QPushButton("파일 선택")
        self.button.setObjectName("primary")
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(self.button)
        row.addStretch()
        layout.addWidget(arrow)
        layout.addWidget(self.text)
        layout.addLayout(row)

    @staticmethod
    def _pdf(event) -> Path | None:
        for url in event.mimeData().urls():
            if url.isLocalFile() and url.toLocalFile().casefold().endswith(".pdf"):
                return Path(url.toLocalFile())
        return None

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self.isEnabled() and self._pdf(event):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt override
        path = self._pdf(event)
        if path:
            self.dropped.emit(path)


# 캡슐 바 아래에 잠깐 붙는 알림 한 줄. 승인이 필요한 경우에만 버튼이 있습니다.
class Toast(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("toast")
        self.setFixedWidth(CAPSULE_WIDTH)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(22, 10, 10, 10)
        self.label = QLabel()
        self.label.setObjectName("toastText")
        self.label.setWordWrap(True)
        self.action = QPushButton()
        self.action.setObjectName("toastAction")
        layout.addWidget(self.label, 1)
        layout.addWidget(self.action)
        self.callback: Callable[[], None] | None = None
        self.action.clicked.connect(self._act)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)
        self.hide()

    def show_message(self, text: str, action: str = "", callback: Callable[[], None] | None = None) -> None:
        self.label.setText(text)
        self.callback = callback
        self.action.setText(action)
        self.action.setVisible(bool(action))
        self.show()
        if action:
            self.timer.stop()  # 승인이 필요한 알림은 사용자가 누를 때까지 남깁니다.
        else:
            self.timer.start(7000)

    def _act(self) -> None:
        callback = self.callback
        self.hide()
        if callback:
            callback()


# 창 테두리가 없어서 OS 기본 모양을 흉내 낸 닫기·최소화 버튼을 직접 그립니다.
# macOS: 왼쪽 위 신호등(빨강 닫기 · 노랑 최소화 · 회색 확대 비활성), 마우스를 올리면 × − 표시.
# Windows: 오른쪽 위 − ×, 마우스를 올리면 배경(닫기는 빨강).
class WindowButton(QToolButton):
    MAC_COLORS = {"close": "#ff5f57", "minimize": "#febc2e", "zoom": "#d9d9d9"}

    def __init__(self, kind: str, mac: bool):
        super().__init__()
        self.kind = kind
        self.mac = mac
        self.group_hovered = False
        self.setToolTip({"close": "닫기", "minimize": "최소화", "zoom": ""}[kind])
        self.setFixedSize(QSize(14, 14) if mac else QSize(34, 26))
        self.setEnabled(kind != "zoom")
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().enterEvent(event)
        self._hover_group(True)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().leaveEvent(event)
        self._hover_group(False)

    # macOS는 신호등 하나에만 올려도 세 개 모두 기호가 보입니다.
    def _hover_group(self, hovered: bool) -> None:
        buttons = self.parent().findChildren(WindowButton) if self.mac and self.parent() else [self]
        for button in buttons:
            button.group_hovered = hovered
            button.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        if self.mac:
            painter.setPen(QPen(QColor(0, 0, 0, 30), 0.8))
            painter.setBrush(QColor(self.MAC_COLORS[self.kind]))
            painter.drawEllipse(rect.adjusted(1, 1, -1, -1))
            if not (self.group_hovered and self.isEnabled()):
                return
            painter.setPen(QPen(QColor(0, 0, 0, 150), 1.3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            c, r = rect.center(), 2.6
        else:
            hovered = self.underMouse()
            if hovered:
                painter.fillRect(rect, QColor("#c42b1c" if self.kind == "close" else "#e5e5e5"))
            painter.setPen(QPen(QColor("#ffffff" if hovered and self.kind == "close" else INK), 1.0))
            c, r = rect.center(), 4.5
        if self.kind == "close":
            painter.drawLine(c.x() - r, c.y() - r, c.x() + r, c.y() + r)
            painter.drawLine(c.x() - r, c.y() + r, c.x() + r, c.y() - r)
        else:
            painter.drawLine(c.x() - r, c.y(), c.x() + r, c.y())


# 메인 창(캡슐 바). 현재 작업 상태(이력서 미리보기, 브라우저 상태, 입력 결과)를 들고 화면을 갱신합니다.
class ApplyLocalWindow(QWidget):
    def __init__(self, client: DesktopApiClient):
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowMinimizeButtonHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Apply Local")
        self.client = client
        self.thread_pool = QThreadPool.globalInstance()
        self._operations: set[ApiOperation] = set()
        # 화면을 잠그는 작업들(건너뛴 AI 검증은 빠짐).
        self._busy_operations: set[ApiOperation] = set()
        self.busy_text = ""
        self.stage = "upload"
        self.expanded = True
        self.task_id: str | None = None
        self.resume_path: Path | None = None
        self.preview: dict[str, Any] | None = None
        # 이 실행 동안 사용자가 고친 이력서 값. 다음 지원서에서 다시 적용합니다(메모리에만, 앱을 끄면 사라짐).
        self.edits: list[tuple[str, str, str]] = []
        self.browser: dict[str, Any] | None = None
        self.fill_report: dict[str, Any] | None = None
        self.fill_progress: dict[str, Any] | None = None
        self.fill_summary = ""
        self.pending_domain: str | None = None
        self.lookup_operation: ApiOperation | None = None
        self.lookup_count = 0
        self.lookup_clock = QElapsedTimer()
        self.show_all_results = False
        self.show_all_resume = False
        self.review_boxes: list[tuple[dict[str, Any], QComboBox, list[str]]] = []
        self._drag_from: QPoint | None = None
        # AI 보조 기능 설정: 선택 저장소(run_desktop에서 붙임), 설치 작업, 설정 화면에 오기 전 단계, 확인한 상태.
        self.ai_settings: QSettings | None = None
        self.setup_operation: SetupOperation | None = None
        self.stage_before_ai = "upload"
        self.ai_status: ollama_setup.OllamaStatus | None = None
        self.ai_memory = 0
        self.ai_progress: tuple[str, float | None] = ("", None)
        self.ai_error = ""
        # 메뉴 막대 아이콘(run_desktop에서 붙임)과, 마지막 작업이 실패했는지(아이콘을 오류 표정으로).
        self.tray = None
        self.failed_last = False
        self._polling = False
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(300)
        self.poll_timer.timeout.connect(self._poll_progress)
        self.clock = QTimer(self)
        self.clock.timeout.connect(self._tick)
        self.clock.start(1000)
        self._build_ui()
        self._apply_style()
        self._set_pinned(True)
        self._render()

    # ---------- 화면 구성 ----------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 12, 18, 22)
        outer.setSpacing(8)
        # 창 크기를 내용에 맞춥니다(펼치기·접기, 단계 바뀜).
        outer.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)

        self.capsule = QFrame()
        self.capsule.setObjectName("capsule")
        self.capsule.setFixedWidth(CAPSULE_WIDTH)
        shadow = QGraphicsDropShadowEffect(self.capsule)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 38))
        self.capsule.setGraphicsEffect(shadow)
        capsule_layout = QVBoxLayout(self.capsule)
        capsule_layout.setContentsMargins(24, 12, 18, 12)
        capsule_layout.setSpacing(12)

        self.header = QWidget()
        self.header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.header.customContextMenuRequested.connect(self._header_menu)
        header = QHBoxLayout(self.header)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(10)
        mac = sys.platform == "darwin"
        self.close_button = WindowButton("close", mac)
        self.close_button.clicked.connect(self.close)
        self.minimize_button = WindowButton("minimize", mac)
        self.minimize_button.clicked.connect(self.showMinimized)
        brand = QLabel("Apply Local")
        brand.setObjectName("brand")
        self.header_status = QLabel()
        self.header_status.setObjectName("headerStatus")
        self.header_bar = ProgressLine(6)
        self.header_bar.setFixedWidth(150)
        self.header_count = QLabel()
        self.header_count.setObjectName("muted")
        self.pin_button = QToolButton()
        self.pin_button.setCheckable(True)
        self.pin_button.setToolTip("항상 위에 표시")
        self.pin_button.toggled.connect(self._set_pinned)
        self.toggle_button = QToolButton()
        self.toggle_button.setToolTip("펼치기 / 접기")
        self.toggle_button.clicked.connect(self._toggle_expanded)
        if mac:
            lights = QWidget()
            lights_layout = QHBoxLayout(lights)
            lights_layout.setContentsMargins(0, 0, 4, 0)
            lights_layout.setSpacing(8)
            for button in (self.close_button, self.minimize_button, WindowButton("zoom", mac)):
                lights_layout.addWidget(button)
            header.addWidget(lights)
        header.addWidget(brand)
        header.addSpacing(14)
        header.addWidget(self.header_status)
        header.addStretch()
        header.addWidget(self.header_bar)
        header.addWidget(self.header_count)
        header.addWidget(self.pin_button)
        header.addWidget(self.toggle_button)
        if not mac:
            header.addSpacing(4)
            header.addWidget(self.minimize_button)
            header.addWidget(self.close_button)
        capsule_layout.addWidget(self.header)

        self.body = QWidget()
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 6, 6)
        body.setSpacing(14)
        line = QFrame()
        line.setObjectName("line")
        line.setFixedHeight(1)
        body.addWidget(line)
        self.stepper = Stepper(STEPS)
        body.addWidget(self.stepper)
        self.pages = {
            "ai": self._build_ai_page(),
            "upload": self._build_upload_page(),
            "resume": self._build_resume_page(),
            "url": self._build_url_page(),
            "filling": self._build_fill_page(),
            "checking": self._build_check_page(),
            "done": self._build_done_page(),
            "review": self._build_review_page(),
        }
        for page in self.pages.values():
            body.addWidget(page)
        capsule_layout.addWidget(self.body)
        outer.addWidget(self.capsule)

        self.toast = Toast()
        outer.addWidget(self.toast)

        quit_shortcut = QShortcut(QKeySequence("Ctrl+Q"), self)
        quit_shortcut.activated.connect(self.close)

    @staticmethod
    def _link(text: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("link")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    @staticmethod
    def _muted(text: str = "") -> QLabel:
        label = QLabel(text)
        label.setObjectName("muted")
        label.setWordWrap(True)
        return label

    # 첫 실행 · 메뉴의 「AI 기능 설정」: AI 보조 기능(Ollama)을 설명하고, 동의하면 설치합니다.
    def _build_ai_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        title = QLabel("AI 보조 기능")
        title.setObjectName("big")
        layout.addWidget(title)
        intro = QLabel(
            "이 컴퓨터에서만 실행되는 AI(Ollama)가 처음 보는 지원서 칸을 이해하고, 규칙으로 못 채운 항목을 "
            "이력서 원문과 한 번 더 확인합니다. 이력서와 개인정보는 이 컴퓨터 밖으로 보내지 않습니다."
        )
        intro.setObjectName("info")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.ai_detail = self._muted("이 컴퓨터의 Ollama를 확인하고 있습니다…")
        layout.addWidget(self.ai_detail)
        self.ai_consent = self._muted(
            "「설치하고 사용」을 누르면 ollama.com에서 Ollama(오픈소스, MIT 라이선스)와 AI 모델을 받아 설치하는 데 "
            "동의하는 것입니다. 설치 파일은 Ollama 서명을 확인한 뒤에만 설치합니다."
        )
        layout.addWidget(self.ai_consent)
        self.ai_progress_label = QLabel()
        layout.addWidget(self.ai_progress_label)
        self.ai_bar = ProgressLine(8)
        layout.addWidget(self.ai_bar)
        buttons = QHBoxLayout()
        self.ai_later_link = self._link("기다리지 않고 계속(규칙으로)")
        self.ai_later_link.setToolTip("설치는 뒤에서 계속됩니다. 끝나면 알려 드립니다.")
        self.ai_later_link.clicked.connect(self._leave_ai_setup)
        buttons.addWidget(self.ai_later_link)
        buttons.addStretch()
        self.ai_skip_button = QPushButton("AI 없이 사용")
        self.ai_skip_button.setObjectName("secondary")
        self.ai_skip_button.clicked.connect(self._choose_rules_only)
        self.ai_cancel_button = QPushButton("설치 취소")
        self.ai_cancel_button.setObjectName("secondary")
        self.ai_cancel_button.clicked.connect(self._cancel_ai_setup)
        self.ai_install_button = QPushButton("설치하고 사용 (권장)")
        self.ai_install_button.setObjectName("primary")
        self.ai_install_button.setMinimumWidth(170)
        self.ai_install_button.clicked.connect(self._install_ai)
        for button in (self.ai_skip_button, self.ai_cancel_button, self.ai_install_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        layout.addWidget(self._muted("나중에 메뉴(머리 줄 오른쪽 클릭 또는 메뉴 막대 아이콘)의 「AI 기능 설정」에서 바꿀 수 있습니다."))
        return page

    # 1. 이력서 업로드.
    def _build_upload_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.keep_resume = QCheckBox("이력서 기억하기")
        self.keep_resume.setChecked(True)
        self.keep_resume.setToolTip("앱을 끄기 전까지 다음 지원서에 같은 이력서를 씁니다. 파일로 저장하지 않습니다.")
        layout.addWidget(self.keep_resume)
        self.drop_zone = DropZone()
        self.drop_zone.button.clicked.connect(self._choose_resume)
        self.drop_zone.dropped.connect(self._resume_selected)
        layout.addWidget(self.drop_zone)
        footer = QHBoxLayout()
        self.template_hint = self._muted("템플릿 이력서를 그대로 고쳐 쓰면 가장 정확합니다.")
        self.template_link = self._link("템플릿 열기")
        self.template_link.clicked.connect(self._open_template)
        footer.addWidget(self.template_hint, 1)
        footer.addWidget(self.template_link)
        layout.addLayout(footer)
        self.template_hint.setVisible(TEMPLATE_PATH.is_file())
        self.template_link.setVisible(TEMPLATE_PATH.is_file())
        return page

    # 1. 이력서 확인(읽은 직후). 값이 틀리면 '수정'으로 고칩니다.
    def _build_resume_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        top = QHBoxLayout()
        self.resume_name = QLabel()
        self.resume_name.setObjectName("strong")
        self.other_file_link = self._link("다른 파일")
        self.other_file_link.setObjectName("mutedLink")
        self.other_file_link.clicked.connect(self._other_file)
        top.addWidget(self.resume_name, 1)
        top.addWidget(self.other_file_link)
        layout.addLayout(top)

        self.name_frame = QFrame()
        self.name_frame.setObjectName("info")
        name_layout = QHBoxLayout(self.name_frame)
        name_layout.setContentsMargins(14, 10, 10, 10)
        name_layout.addWidget(QLabel("성·이름 확인"))
        self.family_edit = QLineEdit()
        self.family_edit.setPlaceholderText("성")
        self.given_edit = QLineEdit()
        self.given_edit.setPlaceholderText("이름")
        self.confirm_name_button = QPushButton("확인")
        self.confirm_name_button.setObjectName("primary")
        self.confirm_name_button.clicked.connect(self._confirm_name)
        name_layout.addWidget(self.family_edit)
        name_layout.addWidget(self.given_edit)
        name_layout.addWidget(self.confirm_name_button)
        layout.addWidget(self.name_frame)

        self.resume_rows = QWidget()
        self.resume_grid = QGridLayout(self.resume_rows)
        self.resume_grid.setContentsMargins(0, 0, 0, 0)
        self.resume_grid.setHorizontalSpacing(18)
        self.resume_grid.setVerticalSpacing(0)
        self.resume_scroll = QScrollArea()
        self.resume_scroll.setWidgetResizable(True)
        self.resume_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.resume_scroll.setWidget(self.resume_rows)
        self.resume_rows.setObjectName("rows")
        self.resume_scroll.viewport().setAutoFillBackground(False)
        layout.addWidget(self.resume_scroll)

        footer = QHBoxLayout()
        footer.addWidget(self._muted("값이 틀리면 「수정」으로 고치세요. 파일은 바뀌지 않습니다."), 1)
        self.resume_next_button = QPushButton("다음")
        self.resume_next_button.setObjectName("primary")
        self.resume_next_button.setMinimumWidth(110)
        self.resume_next_button.clicked.connect(self._resume_confirmed)
        footer.addWidget(self.resume_next_button)
        layout.addLayout(footer)
        return page

    # 2. 채용 URL 업로드 → 브라우저 열기 → (로그인은 사용자가) → 자동 입력 시작.
    def _build_url_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        row = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://company.example/apply")
        self.url_edit.returnPressed.connect(self._open_browser)
        self.open_button = QPushButton("브라우저 열기")
        self.open_button.setObjectName("primary")
        self.open_button.clicked.connect(self._open_browser)
        row.addWidget(self.url_edit, 1)
        row.addWidget(self.open_button)
        layout.addLayout(row)
        info = QLabel("열린 브라우저에서 로그인·본인 인증을 직접 마치고,\n지원서 입력 화면까지 이동한 뒤 아래 버튼을 누르세요.")
        info.setObjectName("info")
        layout.addWidget(info)
        footer = QHBoxLayout()
        self.browser_info = self._muted()
        self.fill_button = QPushButton("자동 입력 시작")
        self.fill_button.setObjectName("primary")
        self.fill_button.setMinimumWidth(150)
        self.fill_button.setToolTip("현재 브라우저에 열린 지원서 페이지를 검사하고, 확실한 항목만 입력한 뒤 다시 확인합니다.")
        self.fill_button.clicked.connect(self._fill)
        footer.addWidget(self.browser_info, 1)
        footer.addWidget(self.fill_button)
        layout.addLayout(footer)
        return page

    # 3. 자동 입력(칸마다 진행 표시).
    def _build_fill_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self._muted("지금 입력 중"))
        self.fill_current = QLabel()
        self.fill_current.setObjectName("big")
        layout.addWidget(self.fill_current)
        layout.addSpacing(4)
        self.fill_bar = ProgressLine(8)
        layout.addWidget(self.fill_bar)
        self.fill_count = self._muted()
        self.fill_count.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.fill_count)
        self.fill_recent = QWidget()
        self.fill_recent_layout = QVBoxLayout(self.fill_recent)
        self.fill_recent_layout.setContentsMargins(0, 0, 0, 0)
        self.fill_recent_layout.setSpacing(4)
        layout.addWidget(self.fill_recent)
        layout.addSpacing(4)
        layout.addWidget(self._muted("입력한 값은 브라우저에서 다시 읽어 확인합니다."))
        return page

    # 4. AI 2차 검증(진행률을 알 수 없어 움직이는 막대 + 경과 시간).
    def _build_check_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self._muted("로컬 AI가 이력서 원문에서 근거를 찾는 중"))
        self.check_title = QLabel()
        self.check_title.setObjectName("big")
        layout.addWidget(self.check_title)
        layout.addSpacing(4)
        self.check_bar = ProgressLine(8)
        self.check_bar.set_busy()
        layout.addWidget(self.check_bar)
        self.check_time = self._muted()
        self.check_time.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.check_time)
        info = QLabel("원문에 근거 문장이 있는 값만 넣고, 「확인 필요」로 표시합니다.")
        info.setObjectName("info")
        layout.addWidget(info)
        row = QHBoxLayout()
        row.addStretch()
        self.skip_button = QPushButton("건너뛰기")
        self.skip_button.setObjectName("secondary")
        self.skip_button.setMinimumWidth(110)
        self.skip_button.clicked.connect(self._skip_lookup)
        row.addWidget(self.skip_button)
        layout.addLayout(row)
        return page

    # 5. 완료(이 페이지의 입력이 끝났을 때).
    def _build_done_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        self.filled_value = self._tile(tiles, "입력 완료")
        self.review_value = self._tile(tiles, "확인 필요")
        self.absent_value = self._tile(tiles, "이력서에 없음")
        self.manual_value = self._tile(tiles, "직접 처리")
        layout.addLayout(tiles)
        self.review_button = QPushButton()
        self.review_button.setObjectName("primaryWide")
        self.review_button.clicked.connect(lambda: self._show_review(all_rows=False))
        layout.addWidget(self.review_button)
        self.all_results_link = self._link("입력 결과 모두 보기")
        self.all_results_link.clicked.connect(lambda: self._show_review(all_rows=True))
        all_row = QHBoxLayout()
        all_row.addStretch()
        all_row.addWidget(self.all_results_link)
        all_row.addStretch()
        layout.addLayout(all_row)
        notice = QLabel("입력 결과를 확인한 뒤 저장·최종 제출은 직접 해 주세요.")
        notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(notice)
        footer = QHBoxLayout()
        self.next_hint = self._muted("다음 페이지가 있으면")
        self.next_button = self._link("다음 페이지 입력")
        self.next_button.clicked.connect(self._next_page)
        self.next_dot = QLabel("·")
        self.new_button = self._link("새 지원서")
        self.new_button.setToolTip("「이력서 기억하기」가 켜져 있으면 이력서를 다시 올리지 않고 URL부터 시작합니다.")
        self.new_button.clicked.connect(self._new_application)
        footer.addWidget(self.next_hint, 1)
        footer.addWidget(self.next_button)
        footer.addWidget(self.next_dot)
        footer.addWidget(self.new_button)
        layout.addLayout(footer)
        return page

    @staticmethod
    def _tile(layout: QHBoxLayout, caption: str) -> QLabel:
        tile = QFrame()
        tile.setObjectName("tile")
        box = QVBoxLayout(tile)
        box.setContentsMargins(8, 12, 8, 10)
        box.setSpacing(2)
        value = QLabel("0")
        value.setObjectName("tileValue")
        value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label = QLabel(caption)
        label.setObjectName("muted")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(value)
        box.addWidget(label)
        layout.addWidget(tile, 1)
        return value

    # 확인 필요 · 상세. 결과 표 대신 확인이 필요한 항목만 보여 줍니다.
    def _build_review_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        top = QHBoxLayout()
        self.review_title = QLabel()
        self.review_title.setObjectName("strong")
        self.review_toggle = self._link("모두 보기")
        self.review_toggle.setObjectName("mutedLink")
        self.review_toggle.clicked.connect(lambda: self._show_review(all_rows=not self.show_all_results))
        top.addWidget(self.review_title, 1)
        top.addWidget(self.review_toggle)
        layout.addLayout(top)
        self.review_rows = QWidget()
        self.review_grid = QGridLayout(self.review_rows)
        self.review_grid.setContentsMargins(0, 0, 0, 0)
        self.review_grid.setHorizontalSpacing(12)
        self.review_grid.setVerticalSpacing(0)
        self.review_scroll = QScrollArea()
        self.review_scroll.setWidgetResizable(True)
        self.review_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.review_scroll.setWidget(self.review_rows)
        self.review_rows.setObjectName("rows")
        self.review_scroll.viewport().setAutoFillBackground(False)
        layout.addWidget(self.review_scroll)
        self.remember_choice = QCheckBox("이 선택 기억하기")
        # 선택 기억(같은 항목이 2번 나오면 다음부터 자동)은 다음 단계에서 구현합니다.
        self.remember_choice.setEnabled(False)
        self.remember_choice.setToolTip("준비 중입니다.")
        layout.addWidget(self.remember_choice)
        footer = QHBoxLayout()
        self.back_link = self._link("← 결과로")
        self.back_link.setObjectName("mutedLink")
        self.back_link.clicked.connect(lambda: self._set_stage("done"))
        self.apply_button = QPushButton("적용")
        self.apply_button.setObjectName("primary")
        self.apply_button.setMinimumWidth(130)
        self.apply_button.clicked.connect(self._apply_review)
        footer.addWidget(self.back_link)
        footer.addStretch()
        footer.addWidget(self.apply_button)
        layout.addLayout(footer)
        return page

    # 화면 스타일(디자인 시안의 흑백 팔레트).
    def _apply_style(self) -> None:
        font = QFont()
        font.setFamilies(["Pretendard", "Apple SD Gothic Neo", "Malgun Gothic", "Noto Sans KR"])
        font.setPixelSize(14)
        self.setFont(font)
        check_image, arrow_image = style_image("check"), style_image("arrow")
        self.setStyleSheet(
            f"""
            QWidget {{ color: {INK}; font-size: 14px; }}
            QFrame#capsule {{ background: #ffffff; border: 1px solid {LINE}; border-radius: 26px; }}
            QLabel#brand {{ font-size: 16px; font-weight: 700; }}
            QLabel#headerStatus {{ font-size: 14px; }}
            QLabel#muted {{ color: {MUTED}; font-size: 13px; }}
            QLabel#strong {{ font-size: 15px; font-weight: 700; }}
            QLabel#big {{ font-size: 18px; font-weight: 700; }}
            QLabel#arrow {{ font-size: 24px; }}
            QLabel#tileValue {{ font-size: 24px; font-weight: 700; }}
            QLabel#rowLabel {{ color: {MUTED}; }}
            QLabel#rowTitle {{ font-weight: 700; }}
            QFrame#line {{ background: {LINE}; }}
            QFrame#capsule QFrame#rowLine {{ background: {LINE}; }}
            QFrame#capsule QFrame#dropZone {{ background: {SOFT}; border: 1.5px dashed #bdbdbd; border-radius: 14px; }}
            QFrame#capsule QFrame#info, QFrame#capsule QLabel#info {{ background: {SOFT}; border-radius: 12px; padding: 12px 16px; }}
            QFrame#capsule QFrame#tile {{ background: {SOFT}; border-radius: 12px; }}
            QFrame#toast {{ background: {INK}; border-radius: 22px; }}
            QLabel#toastText {{ color: #ffffff; background: transparent; }}
            QPushButton#toastAction {{ background: #ffffff; color: {INK}; border: 1px solid #ffffff; border-radius: 13px; min-height: 26px; padding: 0 18px; font-weight: 700; }}
            QLineEdit {{ background: #ffffff; border: 1px solid #d6d6d6; border-radius: 10px; padding: 9px 12px; }}
            QLineEdit:focus {{ border: 1px solid {INK}; }}
            QComboBox {{ background: #ffffff; border: 1px solid #d6d6d6; border-radius: 10px; padding: 7px 10px; min-width: 150px; }}
            QComboBox::drop-down {{ border: 0; width: 24px; }}
            QComboBox::down-arrow {{ image: url({arrow_image}); width: 12px; height: 12px; }}
            QPushButton {{ background: #ffffff; color: {INK}; border: 1px solid #d6d6d6; border-radius: 17px; min-height: 34px; padding: 0 20px; }}
            QPushButton#primary, QPushButton#primaryWide {{ background: {INK}; color: #ffffff; border: 1px solid {INK}; font-weight: 700; }}
            QPushButton#primaryWide {{ border-radius: 20px; min-height: 40px; }}
            QPushButton#primary:hover, QPushButton#primaryWide:hover {{ background: #333333; }}
            QPushButton#primary:disabled, QPushButton#primaryWide:disabled {{ background: #f0f0f0; color: #a8a8a8; border: 1px solid #e0e0e0; }}
            QPushButton#secondary {{ font-weight: 700; }}
            QPushButton#secondary:hover {{ border-color: {INK}; }}
            QPushButton#link, QPushButton#mutedLink {{ background: transparent; border: 0; min-height: 0; padding: 2px 0; font-weight: 700; }}
            QPushButton#mutedLink {{ color: {MUTED}; font-weight: 400; }}
            QPushButton#link:disabled {{ color: #c0c0c0; }}
            QToolButton {{ border: 0; padding: 2px; }}
            QCheckBox {{ spacing: 8px; }}
            QCheckBox::indicator {{ width: 16px; height: 16px; border: 1.5px solid {INK}; border-radius: 4px; background: #ffffff; }}
            QCheckBox::indicator:checked {{ background: {INK}; image: url({check_image}); }}
            QCheckBox::indicator:disabled {{ border-color: #c9c9c9; }}
            QCheckBox:disabled {{ color: #b0b0b0; }}
            QScrollArea, QWidget#rows {{ background: transparent; }}
            """
        )
        self.pin_button.setIconSize(QSize(18, 18))
        self.toggle_button.setIconSize(QSize(20, 20))

    # ---------- 창 동작 ----------

    # 항상 위에 표시(브라우저와 함께 쓰기 위해 기본으로 켬). 머리 줄의 핀으로 끄고 켭니다.
    def _set_pinned(self, pinned: bool) -> None:
        if self.pin_button.isChecked() != pinned:
            self.pin_button.setChecked(pinned)
            return
        self.pin_button.setIcon(svg_icon(PIN_ON if pinned else PIN))
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, pinned)
        if visible:
            self.show()

    def _toggle_expanded(self) -> None:
        self.expanded = not self.expanded
        self._render()

    # 머리 줄 오른쪽 클릭 메뉴: 항상 위에 표시 / 종료(창 테두리가 없으므로).
    def _header_menu(self, position: QPoint) -> None:
        menu = QMenu(self)
        pin = menu.addAction("항상 위에 표시")
        pin.setCheckable(True)
        pin.setChecked(self.pin_button.isChecked())
        pin.toggled.connect(self.pin_button.setChecked)
        if self.tray is not None:
            menu.addAction("숨기기 (메뉴 막대 아이콘으로 다시 열기)", self.hide)
        menu.addAction("AI 기능 설정…", self.open_ai_setup)
        menu.addAction("이력서 템플릿 열기", self._open_template)
        menu.addSeparator()
        menu.addAction("종료", self.close)
        menu.exec(self.header.mapToGlobal(position))

    # 테두리 없는 창이라 머리 줄을 끌어 옮깁니다.
    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton and self.header.geometry().contains(
            self.capsule.mapFrom(self, event.position().toPoint())
        ):
            self._drag_from = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._drag_from is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_from)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._drag_from = None
        super().mouseReleaseEvent(event)

    # 화면 위쪽 가운데에 띄웁니다.
    def place_on_screen(self) -> None:
        screen = self.screen().availableGeometry()
        self.adjustSize()
        self.move(screen.center().x() - self.width() // 2, screen.top() + 8)

    # ---------- 공통 실행 ----------

    @property
    def busy(self) -> bool:
        return bool(self._busy_operations)

    # 공통 실행 도우미: 상태 문구 표시 → 버튼 잠금 → 백그라운드 실행 → 성공/실패 처리 → 잠금 해제.
    def _run(
        self,
        message: str,
        function: Callable[[], object],
        success: Callable[[Any], None],
        failure: Callable[[str], None] | None = None,
    ) -> ApiOperation:
        operation = ApiOperation(function)
        self._operations.add(operation)
        self._busy_operations.add(operation)
        self.busy_text = message
        self.failed_last = False

        def succeeded(result: object) -> None:
            if not operation.detached:
                success(result)

        def failed(message_text: str) -> None:
            if operation.detached:
                return
            self.failed_last = True
            self.toast.show_message(message_text)
            if failure:
                failure(message_text)

        def finished() -> None:
            self._operations.discard(operation)
            self._busy_operations.discard(operation)
            if not self._busy_operations:
                self.busy_text = ""
            self._render()

        operation.signals.succeeded.connect(succeeded)
        operation.signals.failed.connect(failed)
        operation.signals.finished.connect(finished)
        self.thread_pool.start(operation)
        self._render()
        return operation

    def _set_stage(self, stage: str) -> None:
        self.stage = stage
        self._render()

    # ---------- AI 보조 기능 ----------

    # 앱을 켤 때 한 번: 선택이 없으면(첫 실행) 설정 화면, 켜 두었으면 Ollama를 깨우고 준비 상태를 확인합니다.
    def start_ai(self, settings: QSettings) -> None:
        self.ai_settings = settings
        choice = settings.value(AI_CHOICE_KEY)
        if choice not in {"on", "off"}:
            self.open_ai_setup()
            return
        enabled = choice == "on"
        self._background(lambda: self._wake_ai(enabled), self._ai_woken)

    def _wake_ai(self, enabled: bool) -> ollama_setup.OllamaStatus | None:
        self.client.set_ai(enabled)
        if not enabled:
            return None
        status = ollama_setup.detect()
        if status.installed and not status.running:
            ollama_setup.start(status.location)
            status = ollama_setup.wait_until_running(timeout=30)
        return status

    def _ai_woken(self, status: ollama_setup.OllamaStatus | None) -> None:
        if status is not None and not status.ready:
            self.toast.show_message("AI 기능 준비가 끝나지 않아 규칙만 사용합니다.", "설정", self.open_ai_setup)

    # 화면을 잠그지 않는 백그라운드 확인(Ollama 상태, AI 켜기/끄기).
    def _background(self, function: Callable[[], object], success: Callable[[Any], None]) -> None:
        operation = ApiOperation(function)
        self._operations.add(operation)
        operation.signals.succeeded.connect(success)
        operation.signals.failed.connect(self.toast.show_message)
        operation.signals.finished.connect(lambda: self._operations.discard(operation))
        self.thread_pool.start(operation)

    # 메뉴의 「AI 기능 설정」. 입력 작업 중에는 열지 않습니다.
    def open_ai_setup(self) -> None:
        if self.stage != "ai":
            if self.busy or self.stage in {"parsing", "filling", "checking"}:
                self.toast.show_message("지금 작업이 끝난 뒤 AI 기능 설정을 열 수 있습니다.")
                return
            self.stage_before_ai = self.stage
        self.expanded = True
        if not self.isVisible():
            self.show()
        self._set_stage("ai")
        if self.setup_operation is None:
            self.ai_status = None
            self._background(lambda: (ollama_setup.detect(), ollama_setup.total_memory()), self._ai_detected)

    def _ai_detected(self, result: tuple[ollama_setup.OllamaStatus, int]) -> None:
        self.ai_status, self.ai_memory = result
        self._render()

    # 설치할 것과 이 컴퓨터에 대한 안내.
    def _ai_plan_text(self) -> str:
        status = self.ai_status
        if status is None:
            return "이 컴퓨터의 Ollama를 확인하고 있습니다…"
        if status.ready:
            return "Ollama와 AI 모델이 모두 준비되어 있습니다."
        if not status.installed and ollama_setup.unsupported_reason():
            return ollama_setup.unsupported_reason()
        parts = []
        if not status.installed:
            parts.append(f"Ollama 앱(약 {ollama_setup.gigabytes(ollama_setup.INSTALLER_SIZES.get(ollama_setup.platform_key(), 0))})")
        if status.missing_models:
            size = sum(ollama_setup.MODEL_SIZES.get(name, 5_000_000_000) for name in status.missing_models)
            parts.append(f"AI 모델 {len(status.missing_models)}개(약 {ollama_setup.gigabytes(size)})")
        text = "설치할 것: " + " · ".join(parts) + ". 인터넷 속도에 따라 수십 분 걸릴 수 있습니다." if parts else ""
        if status.installed and not status.running:
            text = (text + " " if text else "") + "설치된 Ollama를 실행합니다."
        if 0 < self.ai_memory < ollama_setup.RECOMMENDED_MEMORY:
            text += f"\n이 컴퓨터 메모리 {round(self.ai_memory / 1024**3)}GB · AI 처리가 느릴 수 있습니다(16GB 이상 권장)."
        return text

    def _install_ai(self) -> None:
        if self.setup_operation is not None:
            return
        operation = SetupOperation()
        self.setup_operation = operation
        self.ai_error = ""
        self.ai_progress = ("준비 중", None)
        operation.signals.progress.connect(self._ai_progress_changed)
        operation.signals.succeeded.connect(self._ai_ready)
        operation.signals.failed.connect(self._ai_failed)
        self.thread_pool.start(operation)
        self._render()

    # 진행 이벤트가 잦아 화면 전체 대신 문구와 막대만 바꿉니다.
    def _ai_progress_changed(self, text: str, value: object) -> None:
        self.ai_progress = (text, value if isinstance(value, float) else None)
        if self.stage == "ai":
            self._render_ai()
            self._render_header()

    def _ai_ready(self, status: ollama_setup.OllamaStatus) -> None:
        self.setup_operation = None
        self.ai_status = status
        if self.ai_settings is not None:
            self.ai_settings.setValue(AI_CHOICE_KEY, "on")
        self._background(lambda: self.client.set_ai(True), lambda _result: None)
        self.toast.show_message("AI 보조 기능이 준비되었습니다.")
        if self.stage == "ai":
            self._leave_ai_setup()
        else:
            self._render()

    def _ai_failed(self, text: str) -> None:
        self.setup_operation = None
        self.ai_error = text
        if self.stage != "ai":
            self.toast.show_message(text, "설정", self.open_ai_setup)
        self._render()

    def _cancel_ai_setup(self) -> None:
        if self.setup_operation is not None:
            self.setup_operation.cancel.set()
            self.ai_progress = ("취소하는 중…", None)
            self._render_ai()

    def _choose_rules_only(self) -> None:
        if self.ai_settings is not None:
            self.ai_settings.setValue(AI_CHOICE_KEY, "off")
        self._background(lambda: self.client.set_ai(False), lambda _result: None)
        self.toast.show_message("AI 없이 규칙만 사용합니다. 메뉴의 「AI 기능 설정」에서 언제든 켤 수 있습니다.")
        self._leave_ai_setup()

    def _leave_ai_setup(self) -> None:
        self._set_stage(self.stage_before_ai if self.stage_before_ai != "ai" else "upload")

    def _render_ai(self) -> None:
        status, running = self.ai_status, self.setup_operation is not None
        self.ai_detail.setText(self._ai_plan_text())
        self.ai_consent.setVisible(not running and status is not None and not status.installed)
        text, value = self.ai_progress
        self.ai_progress_label.setText(text if running else self.ai_error)
        self.ai_progress_label.setVisible(running or bool(self.ai_error))
        self.ai_bar.setVisible(running)
        if running and value is not None:
            self.ai_bar.set_value(value)
        elif running:
            self.ai_bar.set_busy()
        blocked = status is not None and not status.installed and bool(ollama_setup.unsupported_reason())
        self.ai_install_button.setVisible(not running)
        self.ai_install_button.setEnabled(status is not None and not blocked)
        if status is not None and status.ready:
            self.ai_install_button.setText("AI 사용")
        elif status is not None and status.installed:
            self.ai_install_button.setText("모델 받고 사용 (권장)" if status.missing_models else "AI 사용 (권장)")
        else:
            self.ai_install_button.setText("다시 시도" if self.ai_error else "설치하고 사용 (권장)")
        self.ai_skip_button.setVisible(not running)
        self.ai_cancel_button.setVisible(running)
        self.ai_later_link.setVisible(running)

    # ---------- 1. 이력서 ----------

    def _choose_resume(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "PDF 이력서 선택", "", "PDF (*.pdf)")
        if selected:
            self._resume_selected(Path(selected))

    # 파일을 고르면 바로 읽습니다(작업 생성 + 업로드). URL은 다음 단계에서 정합니다.
    def _resume_selected(self, path: Path) -> None:
        if path.suffix.casefold() != ".pdf" or not path.is_file():
            self.toast.show_message("PDF 이력서만 사용할 수 있습니다.")
            return
        self.resume_path = path
        self.edits = []
        self._prepare(next_stage="resume")

    def _prepare(self, next_stage: str) -> None:
        if self.resume_path is None:
            return
        resume_path, edits = self.resume_path, list(self.edits)
        self.stage = "parsing" if next_stage == "resume" else next_stage
        self._run(
            "이력서를 로컬에서 읽고 있습니다…",
            lambda: self.client.prepare(resume_path, edits),
            lambda payload: self._prepared(payload, next_stage),
            failure=lambda _message: self._set_stage("upload"),
        )

    # 이력서 분석 결과. 이름 확인이 필요하면 성/이름 입력칸을 채워 둡니다.
    def _prepared(self, payload: dict[str, Any], next_stage: str = "resume") -> None:
        self.task_id = payload["task"]["task_id"]
        self.preview = payload["preview"]
        values = self.preview.get("values", {})
        if self.preview.get("name_confirmation_required"):
            self.family_edit.setText(values.get("person.name.family", ""))
            self.given_edit.setText(values.get("person.name.given", ""))
            next_stage = "resume"
        self.stage = next_stage
        self._render_resume()
        self._render()

    def _render_resume(self) -> None:
        self.resume_name.setText(self.resume_path.name if self.resume_path else "")
        values = (self.preview or {}).get("values", {})
        sources = (self.preview or {}).get("sources", {})
        self._clear(self.resume_grid)
        rows: list[tuple[str, str, str]] = []
        name = values.get("person.name.full", "")
        family, given = values.get("person.name.family", ""), values.get("person.name.given", "")
        rows.append(("이름", f"{name} (성 {family} · 이름 {given})" if name and family and given else name, "person.name.full"))
        if self.show_all_resume:
            rows += [(resume_label(path), value, path) for path, value in values.items()
                     if path != "person.name.full" and not path.startswith("person.name.")]
        else:
            rows += [(resume_label(path), values.get(path, ""), path) for path in RESUME_SUMMARY]
            schools = [values.get(f"education.{level}.school_name", "") for level in EDUCATION_LEVELS]
            rows.append(("학력", " · ".join(school for school in schools if school), ""))
        for index, (label, value, path) in enumerate(rows):
            note = {"llm": " · 로컬 LLM", "user": " · 사용자 수정"}.get(sources.get(path, ""), "")
            title = QLabel(label + note)
            title.setObjectName("rowLabel")
            text = QLabel(value or "—")
            text.setToolTip(value)
            edit = self._link("수정" if path else "모두 보기")
            edit.setObjectName("mutedLink")
            if path:
                edit.clicked.connect(lambda _checked=False, p=path, v=value: self._edit_resume_value(p, v))
            else:
                edit.clicked.connect(self._toggle_resume_rows)
            row = index * 2
            self.resume_grid.addWidget(title, row, 0)
            self.resume_grid.addWidget(text, row, 1)
            self.resume_grid.addWidget(edit, row, 2, alignment=Qt.AlignmentFlag.AlignRight)
            for widget in (title, text, edit):
                widget.setMinimumHeight(38)
            line = QFrame()
            line.setObjectName("rowLine")
            line.setFixedHeight(1)
            self.resume_grid.addWidget(line, row + 1, 0, 1, 3)
        self.resume_grid.setColumnStretch(1, 1)
        self.resume_grid.setColumnMinimumWidth(0, 90)
        self.resume_scroll.setFixedHeight(min(40 * len(rows) + 4, 330))

    def _toggle_resume_rows(self) -> None:
        self.show_all_resume = not self.show_all_resume
        self._render_resume()
        self._render()

    # 사용자가 확인한 성/이름을 서버에 보냅니다.
    def _confirm_name(self) -> None:
        if not self.task_id:
            return
        family, given = self.family_edit.text().strip(), self.given_edit.text().strip()
        if not family or not given:
            self.toast.show_message("성과 이름을 확인해 주세요.")
            return
        self._run(
            "성과 이름을 확인하고 있습니다…",
            lambda: self.client.confirm_name(self.task_id or "", family, given),
            lambda preview: self._name_confirmed(preview, family, given),
        )

    def _name_confirmed(self, preview: dict[str, Any], family: str = "", given: str = "") -> None:
        self.preview = {**(self.preview or {}), **preview, "sources": (self.preview or {}).get("sources", {})}
        if family and given:
            self.edits.append(("name", family, given))
        self._render_resume()
        self._render()

    # 이력서 값 수정 요청(이번 실행 동안만, 원본 파일은 바뀌지 않음).
    def _edit_resume_value(self, path: str, current: str) -> None:
        value, accepted = QInputDialog.getText(self, "이력서 값 수정", resume_label(path), text=current)
        if not accepted or value.strip() == current:
            return
        self._run(
            "이력서 값을 수정하고 있습니다…",
            lambda: self.client.edit_resume(self.task_id or "", path, value.strip()),
            lambda preview: self._resume_edited(preview, path, value.strip()),
        )

    def _resume_edited(self, preview: dict[str, Any], path: str = "", value: str = "") -> None:
        sources = dict((self.preview or {}).get("sources", {}))
        if path:
            sources[path] = "user"
            self.edits.append(("edit", path, value))
        self.preview = {**(self.preview or {}), **preview, "sources": sources}
        self._render_resume()
        self._render()

    def _resume_confirmed(self) -> None:
        self._set_stage("url")
        self.url_edit.setFocus()

    # 다른 이력서 파일: 지금 작업을 정리하고 1단계로.
    def _other_file(self) -> None:
        self._cancel_task(lambda: self._start_over(keep_resume=False))

    # 템플릿을 브라우저로 엽니다. 값을 고친 뒤 「PDF로 저장」으로 이력서 PDF를 만듭니다.
    def _open_template(self) -> None:
        if TEMPLATE_PATH.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(TEMPLATE_PATH)))

    # ---------- 2. URL · 브라우저 ----------

    # URL을 작업에 정한 뒤 작업용 브라우저를 엽니다.
    def _open_browser(self) -> None:
        url = self.url_edit.text().strip()
        if not self.task_id or self.browser is not None or self.busy:
            return
        if not url:
            self.toast.show_message("채용 페이지 URL을 입력해 주세요.")
            return
        task_id = self.task_id

        def open_with_target() -> dict[str, Any]:
            self.client.set_target(task_id, url)
            return self.client.open_browser(task_id)

        self._run("브라우저를 열고 있습니다…", open_with_target, self._browser_updated)

    # 브라우저 상태 반영. 승인이 필요한 도메인이 있으면 알림에 '허용' 버튼을 붙입니다.
    def _browser_updated(self, browser: dict[str, Any]) -> None:
        self.browser = browser
        self.pending_domain = browser.get("blocked_hostname") or next(
            iter(browser.get("discovered_frame_hosts") or []), None
        )
        if self.pending_domain:
            self.toast.show_message(f"다른 사이트로 이동하려 합니다: {self.pending_domain}", "허용", self._approve_domain)
        self._render()

    # 도메인 허용은 사용자가 알림의 '허용'을 눌렀을 때만 요청합니다.
    def _approve_domain(self) -> None:
        if not self.task_id or not self.pending_domain:
            return
        hostname = self.pending_domain
        self._run(
            "도메인을 허용하고 있습니다…",
            lambda: self.client.approve_domain(self.task_id or "", hostname),
            self._browser_updated,
        )

    # ---------- 3. 자동 입력 ----------

    def _fill(self) -> None:
        if not self.task_id or self.browser is None or self.busy:
            return
        self.fill_progress = None
        self.stage = "filling"
        self.poll_timer.start()
        self._run(
            "확실한 항목을 입력하고 브라우저에서 다시 검증하고 있습니다…",
            lambda: self.client.fill_browser(self.task_id or ""),
            self._filled,
            failure=lambda _message: self._fill_failed(),
        )

    def _fill_failed(self) -> None:
        self.poll_timer.stop()
        self.stage = "url"

    # 입력 중 진행 상황을 짧은 간격으로 묻습니다(겹쳐 묻지 않음, 실패는 조용히 무시).
    def _poll_progress(self) -> None:
        if self._polling or not self.task_id or self.stage != "filling":
            return
        self._polling = True
        operation = ApiOperation(lambda: self.client.fill_progress(self.task_id or ""))
        self._operations.add(operation)

        def done() -> None:
            self._polling = False
            self._operations.discard(operation)

        operation.signals.succeeded.connect(self._progress_updated)
        operation.signals.finished.connect(done)
        self.thread_pool.start(operation)

    def _progress_updated(self, progress: dict[str, Any]) -> None:
        if self.stage == "filling":
            self.fill_progress = progress
            self._render()

    # 규칙 입력 결과를 받은 뒤, 이어서 AI 2차 확인(_lookup)을 시작합니다.
    def _filled(self, report: dict[str, Any]) -> None:
        self.poll_timer.stop()
        self.fill_report = report
        self._browser_updated(report["browser"])
        needs_attention = report["needs_human_count"] + report["failed_count"]
        disabled = report.get("disabled_count", 0)
        self.fill_summary = (
            f"입력·검증 {report['filled_count']}개, 기존 값 유지 {report['kept_existing_count']}개, "
            f"직접 확인 {needs_attention}개입니다."
            + (f" 페이지에서 비활성화된 {disabled}개는 입력하지 않았습니다." if disabled else "")
        )
        if report.get("notice"):
            self.toast.show_message(report["notice"])
        self.stage = "done"
        # Started after this operation finishes so the rule results stay visible meanwhile.
        QTimer.singleShot(0, self._lookup)

    # ---------- 4. AI 2차 검증 ----------

    # 확인이 필요한 행만 모아 로컬 LLM에 대조를 요청합니다(1~3분).
    def _lookup(self) -> None:
        if not self.task_id or self.fill_report is None:
            return
        rows = [
            {"fingerprint": item["fingerprint"], "status": item["status"]}
            for item in self.fill_report["results"]
            if item.get("status") in SECOND_CHECK and item.get("fingerprint")
        ]
        if not rows:
            self._set_stage("done")
            return
        self.lookup_count = len(rows)
        self.lookup_clock.start()
        self.stage = "checking"
        self.lookup_operation = self._run(
            f"남은 {len(rows)}개 항목을 로컬 LLM으로 이력서와 다시 대조하고 있습니다…",
            lambda: self.client.lookup_browser(self.task_id or "", rows),
            self._looked_up,
            failure=lambda _message: self._set_stage("done"),
        )

    # 기다리지 않고 결과로 넘어갑니다. 서버에도 알려, 늦게 온 답은 페이지에 넣지 않게 합니다.
    def _skip_lookup(self) -> None:
        operation = self.lookup_operation
        if operation is None or not self.task_id:
            return
        operation.detached = True
        self._busy_operations.discard(operation)
        if not self._busy_operations:
            self.busy_text = ""
        self.lookup_operation = None
        task_id = self.task_id
        skip = ApiOperation(lambda: self.client.skip_lookup(task_id))
        self._operations.add(skip)
        skip.signals.finished.connect(lambda: self._operations.discard(skip))
        self.thread_pool.start(skip)
        self.toast.show_message("AI 2차 검증을 건너뛰었습니다.")
        self._set_stage("done")

    # 2차 확인 결과를 기존 결과에 fingerprint 기준으로 합칩니다(같은 칸을 정확히 교체).
    def _looked_up(self, report: dict[str, Any]) -> None:
        self.lookup_operation = None
        if self.fill_report is None:
            return
        # Positional keys may differ between requests; the fingerprint names the same field.
        found = {item.get("fingerprint") or item["form_field"]: item for item in report["results"]}
        results = [found.pop(item.get("fingerprint") or item["form_field"], item) for item in self.fill_report["results"]]
        results.extend(found.values())
        self.fill_report = {**self.fill_report, "results": results}
        added = report.get("lookup_filled_count", 0)
        message = ""
        if added:
            message = f"이력서에서 추가로 찾아 입력한 {added}개는 '입력됨 · 확인 필요'로 표시했습니다."
        if report.get("notice"):
            message = f"{message} {report['notice']}".strip()
        if message:
            self.toast.show_message(message)
        self.stage = "done"
        self._render()

    # ---------- 5. 완료 · 확인 필요 ----------

    def _results(self) -> list[dict[str, Any]]:
        return list((self.fill_report or {}).get("results", []))

    def _counts(self) -> dict[str, int]:
        results = self._results()
        return {
            "filled": sum(item.get("status") == "filled_verified" for item in results),
            "kept": sum(item.get("status") == "kept_existing" for item in results),
            "review": sum(item.get("status") in REVIEWABLE for item in results),
            "absent": sum(item.get("status") == "not_in_resume" for item in results),
            "manual": sum(item.get("status") == "user_only" for item in results),
            "total": len(results),
        }

    def _show_review(self, *, all_rows: bool) -> None:
        self.show_all_results = all_rows
        self._render_review()
        self._set_stage("review")

    # 확인 필요 목록. 사용자가 해결할 수 있는 행에는 값 고르기(이력서 값·페이지 후보·직접 입력)를 붙입니다.
    def _render_review(self) -> None:
        results = self._results()
        ordered = sorted(results, key=lambda item: 0 if item.get("status") in REVIEWABLE else ROW_ORDER.get(item.get("status"), 5))
        rows = ordered if self.show_all_results else [item for item in ordered if item.get("status") in REVIEWABLE]
        pending = sum(item.get("status") in REVIEWABLE for item in results)
        self.review_title.setText(f"입력 결과 {len(rows)}" if self.show_all_results else f"확인 필요 {pending}")
        self.review_toggle.setText("확인 필요만" if self.show_all_results else "모두 보기")
        self._clear(self.review_grid)
        self.review_boxes = []
        values = (self.preview or {}).get("values", {})
        for index, item in enumerate(rows):
            title = QLabel(item.get("label") or "라벨 없음")
            title.setObjectName("rowTitle")
            status = item.get("status", "")
            note = "선택지가 여러 개" if status == "needs_human" and len(item.get("candidates") or []) > 1 else STATUS_LABELS.get(status, status)
            sub = self._muted(note)
            sub.setToolTip(item.get("reason", ""))
            text_box = QVBoxLayout()
            text_box.setContentsMargins(0, 10, 0, 10)
            text_box.setSpacing(2)
            text_box.addWidget(title)
            text_box.addWidget(sub)
            row = index * 2
            self.review_grid.addLayout(text_box, row, 0)
            if status in DETAIL:
                options = [value for value in item.get("candidates", []) if value]
                resume_value = values.get(item.get("resume_path") or "", "")
                box = QComboBox()
                box.setEditable(True)
                box.addItems(list(dict.fromkeys([value for value in [resume_value, *options] if value])))
                box.setCurrentText(resume_value)
                box.lineEdit().setPlaceholderText("선택하세요")
                box.setMinimumWidth(180)
                self.review_grid.addWidget(box, row, 1, alignment=Qt.AlignmentFlag.AlignRight)
                self.review_boxes.append((item, box, options))
            line = QFrame()
            line.setObjectName("rowLine")
            line.setFixedHeight(1)
            self.review_grid.addWidget(line, row + 1, 0, 1, 2)
        self.review_grid.setColumnStretch(0, 1)
        self.review_scroll.setFixedHeight(max(min(66 * len(rows) + 4, 360), 40))

    # 고른 값들을 한 칸씩 입력하고 브라우저에서 다시 확인합니다.
    def _apply_review(self) -> None:
        if not self.task_id:
            return
        chosen = [
            (item, box.currentText().strip(), box.currentText().strip() in options)
            for item, box, options in self.review_boxes
            if box.currentText().strip()
        ]
        if not chosen:
            self.toast.show_message("입력할 값을 골라 주세요.")
            return
        task_id = self.task_id

        def fill_all() -> list[dict[str, Any]]:
            return [self.client.fill_field(task_id, item, value, chosen=picked) for item, value, picked in chosen]

        self._run("선택한 값을 입력하고 브라우저에서 다시 확인하고 있습니다…", fill_all, self._review_filled)

    # 입력 결과를 반영. 실패한 칸은 이유를 알림으로 알려 줍니다(클릭이 무시된 것처럼 보이지 않게).
    def _review_filled(self, filled: list[dict[str, Any]]) -> None:
        def key(item: dict[str, Any]) -> str:
            return item.get("fingerprint") or item.get("form_field", "")

        updates = {key(result): result for result in filled}
        results = [updates.get(key(item), item) for item in self._results()]
        if self.fill_report is not None:
            self.fill_report = {**self.fill_report, "results": results}
        failed = [result for result in filled if result.get("status") != "filled_verified"]
        if failed:
            self.toast.show_message(f"{failed[0].get('label') or '항목'}: {failed[0].get('reason', '') or '브라우저에서 직접 확인해 주세요.'}")
        else:
            self.toast.show_message("입력하고 브라우저에서 확인했습니다.")
        self._render_review()
        self._render()

    # 안전한 '다음' 버튼으로 다음 페이지 이동 → 2단계 화면에서 다시 '자동 입력 시작'.
    def _next_page(self) -> None:
        if self.task_id:
            self._run(
                "다음 페이지로 이동하고 있습니다…",
                lambda: self.client.next_page(self.task_id or ""),
                self._next_completed,
            )

    def _next_completed(self, browser: dict[str, Any]) -> None:
        self.fill_report = None
        self._browser_updated(browser)
        self.toast.show_message("다음 페이지로 이동했습니다. 「자동 입력 시작」을 다시 누르세요.")
        self._set_stage("url")

    # 새 지원서: 지금 작업을 정리한 뒤, 이력서를 기억하면 URL 단계부터, 아니면 1단계부터.
    def _new_application(self) -> None:
        self._cancel_task(lambda: self._start_over(keep_resume=self.keep_resume.isChecked()))

    def _cancel_task(self, then: Callable[[], None]) -> None:
        task_id = self.task_id
        self.task_id = None
        if task_id:
            self._run("현재 작업을 정리하고 있습니다…", lambda: self.client.cancel(task_id), lambda _result: then(),
                      failure=lambda _message: then())
        else:
            then()

    def _start_over(self, *, keep_resume: bool) -> None:
        self.preview = None
        self.browser = None
        self.fill_report = None
        self.fill_progress = None
        self.pending_domain = None
        self.show_all_results = False
        self.show_all_resume = False
        self.url_edit.clear()
        self.toast.hide()
        self.failed_last = False
        if keep_resume and self.resume_path is not None and self.resume_path.is_file():
            self._prepare(next_stage="url")
        else:
            self.resume_path = None
            self.edits = []
            self._set_stage("upload")

    # 하위 호환: 예전 '다시' 버튼과 같은 동작.
    def _reset(self) -> None:
        self._new_application()

    # ---------- 화면 갱신 ----------

    @staticmethod
    def _clear(layout: QLayout) -> None:
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
            elif child.layout():
                ApplyLocalWindow._clear(child.layout())

    def _tick(self) -> None:
        if self.stage == "checking":
            self._render_header()
            self._render_check()

    def _elapsed(self) -> str:
        seconds = self.lookup_clock.elapsed() // 1000 if self.lookup_clock.isValid() else 0
        return f"{seconds // 60}:{seconds % 60:02d}"

    # 머리 줄: 단계 문구 + 진행 막대 + 숫자.
    def _render_header(self) -> None:
        stage, bar, count = self.stage, self.header_bar, ""
        text, mode, value = "", "hidden", 0.0
        counts = self._counts()
        if stage == "ai":
            text = "AI 보조 기능 설정"
            if self.setup_operation is not None:
                mode, value = ("value", self.ai_progress[1]) if self.ai_progress[1] is not None else ("busy", 0.0)
        elif stage == "upload":
            text = "1. 이력서를 올려 주세요"
        elif stage == "parsing":
            text, mode = "1. 이력서를 읽는 중", "busy"
        elif stage == "resume":
            text = "1. 이름 확인이 필요합니다" if (self.preview or {}).get("name_confirmation_required") else "1. 이력서 확인"
        elif stage == "url":
            if self.busy:
                text, mode = "2. " + ("이력서 준비 중" if self.task_id is None else "브라우저 여는 중" if self.browser is None else "처리 중"), "busy"
            elif self.browser is None:
                text = "2. 채용 URL을 넣어 주세요"
            else:
                text = "2. 지원서 화면에서 자동 입력 시작"
        elif stage == "filling":
            progress = self.fill_progress or {}
            total, done = progress.get("total", 0), progress.get("done", 0)
            text = "3. 자동 입력 중"
            if total:
                mode, value, count = "value", done / total, f"{done} / {total}"
            else:
                mode = "busy"
        elif stage == "checking":
            text, mode, count = "4. AI 2차 검증 중", "busy", f"{self.lookup_count}개 · {self._elapsed()}"
        else:
            text = f"5. 완료 · 확인 필요 {counts['review']}" if counts["review"] else "5. 완료"
            mode, value = "value", 1.0
            if counts["total"]:
                count = f"{counts['filled'] + counts['kept']} / {counts['total']}"
            if self.busy:
                text, mode = "5. 처리 중", "busy"
        self.header_status.setText(text)
        bar.setVisible(mode != "hidden")
        if mode == "busy":
            bar.set_busy()
        elif mode == "value":
            bar.set_value(value)
        self.header_count.setText(count)
        self.header_count.setVisible(bool(count))

    def _render_check(self) -> None:
        self.check_title.setText(f"규칙으로 못 채운 {self.lookup_count}개 항목 확인 중")
        self.check_time.setText(f"{self._elapsed()} 경과 · 보통 1~3분")

    def _render_fill(self) -> None:
        progress = self.fill_progress or {}
        total, done = progress.get("total", 0), progress.get("done", 0)
        self.fill_current.setText(progress.get("current") or "지원서 화면을 분석하고 있습니다")
        if total:
            self.fill_bar.set_value(done / total)
            self.fill_count.setText(f"{done} / {total}")
        else:
            self.fill_bar.set_busy()
            self.fill_count.setText("")
        self._clear(self.fill_recent_layout)
        for item in reversed(progress.get("recent", [])[-3:]):
            row = QHBoxLayout()
            icon = QLabel()
            icon.setPixmap(svg_icon(CHECK_DONE, 16).pixmap(16, 16))
            label = QLabel(item.get("label", ""))
            status = self._muted(STATUS_LABELS.get(item.get("status", ""), item.get("status", "")))
            row.addWidget(icon)
            row.addWidget(label, 1)
            row.addWidget(status)
            self.fill_recent_layout.addLayout(row)

    def _render_done(self) -> None:
        counts = self._counts()
        self.filled_value.setText(str(counts["filled"]))
        self.review_value.setText(str(counts["review"]))
        self.absent_value.setText(str(counts["absent"]))
        self.manual_value.setText(str(counts["manual"]))
        self.review_button.setText(f"확인 필요 {counts['review']}개 보기")
        self.review_button.setVisible(counts["review"] > 0)
        safe_next = bool((self.fill_report or {}).get("safe_next_available"))
        for widget in (self.next_hint, self.next_button, self.next_dot):
            widget.setVisible(safe_next)

    # 현재 상태로 전체 화면을 맞춥니다(지금 할 수 있는 버튼만 켜짐).
    def _render(self) -> None:
        stage = self.stage
        page_name = "upload" if stage == "parsing" else stage
        for name, page in self.pages.items():
            page.setVisible(name == page_name)
        self.body.setVisible(self.expanded)
        self.toggle_button.setIcon(svg_icon(CHEVRON_UP if self.expanded else CHEVRON_DOWN))
        self.stepper.setVisible(stage != "ai")
        self.stepper.set_current(STAGE_STEP[stage])
        self._render_header()
        if stage == "ai":
            self._render_ai()

        busy = self.busy
        parsing = stage == "parsing"
        self.drop_zone.setEnabled(not busy)
        self.drop_zone.button.setText("이력서를 읽는 중…" if parsing else "파일 선택")
        self.keep_resume.setEnabled(not busy)

        needs_name = bool((self.preview or {}).get("name_confirmation_required"))
        self.name_frame.setVisible(needs_name)
        self.confirm_name_button.setEnabled(not busy)
        self.resume_next_button.setEnabled(not busy and not needs_name and self.task_id is not None)
        self.other_file_link.setEnabled(not busy)

        has_browser = self.browser is not None
        self.url_edit.setReadOnly(has_browser)
        self.open_button.setVisible(not has_browser)
        self.open_button.setEnabled(not busy and self.task_id is not None)
        self.fill_button.setEnabled(not busy and has_browser)
        if has_browser:
            title = (self.browser or {}).get("page_title") or ""
            host = (self.browser or {}).get("current_hostname") or ""
            self.browser_info.setText(" · ".join(part for part in (f"현재 페이지: {host}" if host else "", title) if part))
        else:
            self.browser_info.setText("브라우저를 열면 「자동 입력 시작」이 켜집니다.")

        if stage == "filling":
            self._render_fill()
        if stage == "checking":
            self._render_check()
        self.skip_button.setEnabled(self.lookup_operation is not None)
        if stage in {"done", "review"}:
            self._render_done()
        for widget in (self.review_button, self.all_results_link, self.new_button, self.next_button, self.apply_button):
            widget.setEnabled(not busy)
        if self.tray is not None:
            self.tray.set_state(self.tray_state(), self.header_status.text())
        self.adjustSize()

    # 메뉴 막대 아이콘 표정: 오류 > 입력 진행률/작업 중 > 사용자 조작 대기 > 확인 필요/완료 > 대기.
    def tray_state(self) -> str:
        if self.failed_last:
            return "error"
        # 입력 단계에서 진행률을 알면 채운 잎 수(0~3)로, 모르면(분석·AI 확인·설치) 회전으로 보여 줍니다.
        progress = self.fill_progress or {}
        if self.stage == "filling" and progress.get("total"):
            return f"filling_{min(3, 4 * progress.get('done', 0) // progress['total'])}"
        if self.busy or self.stage in {"parsing", "filling", "checking"} or (self.stage == "ai" and self.setup_operation):
            return "applying"
        if self.pending_domain or (self.stage == "url" and self.browser is not None):
            return "waiting"
        if self.stage in {"done", "review"}:
            return "attention" if self._counts()["review"] else "success"
        return "idle"

    # 창을 닫을 때도 작업을 취소해 브라우저와 민감 데이터를 정리합니다.
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        if self.setup_operation is not None:
            self.setup_operation.cancel.set()
        if self.task_id:
            try:
                self.client.cancel(self.task_id)
            except DesktopApiError:
                pass
        event.accept()


# 데스크톱 앱 실행 진입점.
def run_desktop(base_url: str, token: str) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Apply Local")
    app.setWindowIcon(app_icon())
    window = ApplyLocalWindow(DesktopApiClient(base_url, token))
    if MenuBarIcon.available():

        def quit_app() -> None:
            window.close()
            app.quit()

        window.tray = MenuBarIcon(window, quit_app)
        window._render()
    window.start_ai(QSettings("Apply Local", "Apply Local"))
    window.place_on_screen()
    window.show()
    return app.exec()
