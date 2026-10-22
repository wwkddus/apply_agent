# 데스크톱 화면(PySide6). 
# 사용자가 보는 창으로, 
# 이력서·URL 준비 → 브라우저 열기 → 자동 입력 → 확인 필요 항목 처리 순서를 안내합니다.

# 실제 작업은 로컬 API에 요청하고, 
# 오래 걸리는 요청은 백그라운드 스레드에서 실행해 화면이 멈추지 않게 합니다. 
# 최종 제출은 사용자가 브라우저에서 직접 합니다.

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

import httpx
from PySide6.QtCore import QElapsedTimer, QObject, QRunnable, QThreadPool, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QInputDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)



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
    "not_in_resume": "이력서에 없음",
}

# Results the user can resolve from the review dialog.
# 사용자가 확인 창에서 해결할 수 있는 상태들.
REVIEWABLE = {
    "needs_human", "failed_validation", "missing_resume_value", "field_not_found", "filled_llm_unconfirmed", "filled_review",
}
# Rows that also get a '상세' button so the user can type a value the resume did not have.
# '상세' 버튼을 보여줄 행(이력서에 없는 값도 사용자가 직접 입력 가능).
DETAIL = REVIEWABLE | {"not_in_resume"}
# Rows sent to the local-LLM second check after the rule fill.
# 규칙 입력 후 AI 2차 확인으로 보낼 행, 그리고 결과 표의 정렬 순서.
SECOND_CHECK = {"pending_lookup", "missing_resume_value", "needs_human"}
ROW_ORDER = {"not_in_resume": 1, "pending_lookup": 1, "user_only": 2, "disabled_on_page": 3, "kept_existing": 4, "filled_verified": 5}

# 화면 위쪽에 표시하는 5단계 진행 안내.
STEPS = (
    "① 이력서·URL 준비",
    "② 브라우저에서 로그인·지원서 열기",
    "③ 이 페이지 자동 입력",
    "④ 확인 필요 항목 처리",
    "⑤ 직접 검토 후 제출",
)


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

    # 작업 생성 + 이력서 업로드. 업로드가 실패하면 만든 작업을 취소해 민감 데이터를 남기지 않습니다.
    def prepare(self, target_url: str, resume_path: Path) -> dict[str, Any]:
        task = self._request("POST", "/api/tasks", json={"target_url": target_url})
        task_id = task["task_id"]
        try:
            with resume_path.open("rb") as handle:
                preview = self._request(
                    "POST",
                    f"/api/tasks/{task_id}/resume",
                    files={"file": (resume_path.name, handle, "application/pdf")},
                    timeout=240,  # A local LLM may be used for free-form resumes.
                )
        except Exception:
            try:
                self.cancel(task_id)
            except DesktopApiError:
                pass
            raise
        return {"task": task, "preview": preview}

    # 아래 메서드들은 API 엔드포인트를 하나씩 감싼 것입니다. 단계별로 시간 제한이 다릅니다(LLM 확인은 길게).
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

    def inspect_browser(self, task_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/tasks/{task_id}/browser/inspect", timeout=45)

    def fill_browser(self, task_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/tasks/{task_id}/browser/fill", timeout=120)

    def lookup_browser(self, task_id: str, rows: list[dict[str, str]]) -> dict[str, Any]:
        # The local-LLM second check can take a few minutes on a laptop.
        return self._request("POST", f"/api/tasks/{task_id}/browser/lookup", json={"rows": rows}, timeout=300)

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


# '항목 확인' 창: 판단 근거를 보여 주고, 이력서 값·페이지 후보 중에서 고르거나 직접 고쳐 입력하게 합니다.
class ReviewDialog(QDialog):
    """Lets the user confirm or correct one field the agent could not finish on its own."""

    def __init__(self, parent: QWidget, item: dict[str, Any], resume_value: str):
        super().__init__(parent)
        self.setWindowTitle("항목 확인")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow("항목", QLabel(item.get("label") or "라벨 없음"))
        form.addRow("이력서 항목", QLabel(item.get("resume_path") or "연결 없음"))
        status = QLabel(STATUS_LABELS.get(item.get("status", ""), item.get("status", "")))
        form.addRow("상태", status)
        reason = QLabel(item.get("reason", ""))
        reason.setWordWrap(True)
        form.addRow("판단 근거", reason)
        self.value_box = QComboBox()
        self.value_box.setEditable(True)
        self.page_options = [value for value in item.get("candidates", []) if value]
        choices = [value for value in [resume_value, *self.page_options] if value]
        self.value_box.addItems(list(dict.fromkeys(choices)))
        form.addRow("입력할 값", self.value_box)
        layout.addLayout(form)
        hint = QLabel("페이지의 후보 중에서 고르거나 직접 수정한 뒤 입력하세요. 입력 후 브라우저에서 다시 확인합니다.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox()
        self.fill_button = buttons.addButton("이 값으로 입력", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("건너뛰기", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # 사용자가 고른/입력한 값.
    def value(self) -> str:
        return self.value_box.currentText().strip()

    # 값이 페이지 후보 중 하나와 정확히 같은지(같으면 그 선택지를 그대로 클릭).
    def chosen_from_page(self) -> bool:
        """True when the value is exactly one of the option texts read from the page."""
        return self.value() in self.page_options


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


# 메인 창. 현재 작업 상태(이력서 미리보기, 브라우저 상태, 입력 결과)를 들고 화면을 갱신합니다.
class ApplyLocalWindow(QMainWindow):
    def __init__(self, client: DesktopApiClient):
        super().__init__()
        self.client = client
        self.thread_pool = QThreadPool.globalInstance()
        self._operations: set[ApiOperation] = set()
        self.task_id: str | None = None
        self.resume_path: Path | None = None
        self.preview: dict[str, Any] | None = None
        self.browser: dict[str, Any] | None = None
        self.inspection: dict[str, Any] | None = None
        self.fill_report: dict[str, Any] | None = None
        self.started_at = QElapsedTimer()
        self.pending_domain: str | None = None
        self.submit_notice_shown = False
        self.table_mode = "preview"
        self.table_items: list[dict[str, Any]] = []
        self.setWindowTitle("Apply Local")
        self.setMinimumSize(720, 720)
        self.resize(760, 820)
        self._build_ui()
        self._apply_style()
        self._render_initial()

    # 화면 구성: 입력 영역(이력서·URL), 진행 단계, 요약 숫자, 버튼, 결과 표.
    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Apply Local")
        title.setObjectName("title")
        subtitle = QLabel("한국 채용 지원서 로컬 입력 Agent")
        subtitle.setObjectName("muted")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.pin_window = QCheckBox("항상 위에 표시")
        self.pin_window.toggled.connect(self._toggle_always_on_top)
        header.addWidget(self.pin_window)
        layout.addLayout(header)

        steps = QHBoxLayout()
        steps.setSpacing(6)
        self.step_labels: list[QLabel] = []
        for text in STEPS:
            label = QLabel(text)
            label.setObjectName("step")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setWordWrap(True)
            steps.addWidget(label, 1)
            self.step_labels.append(label)
        layout.addLayout(steps)

        input_card = QFrame()
        input_card.setObjectName("card")
        form = QGridLayout(input_card)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(11)
        form.addWidget(QLabel("이력서"), 0, 0)
        self.resume_edit = QLineEdit()
        self.resume_edit.setReadOnly(True)
        self.resume_edit.setPlaceholderText("PDF 이력서를 선택하세요")
        form.addWidget(self.resume_edit, 0, 1)
        self.choose_resume_button = QPushButton("선택")
        self.choose_resume_button.clicked.connect(self._choose_resume)
        form.addWidget(self.choose_resume_button, 0, 2)
        self.keep_resume = QCheckBox("다시 시작해도 이력서 선택 유지")
        self.keep_resume.setChecked(True)
        form.addWidget(self.keep_resume, 1, 1, 1, 2)
        form.addWidget(QLabel("URL"), 2, 0)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://company.example/apply")
        form.addWidget(self.url_edit, 2, 1, 1, 2)
        layout.addWidget(input_card)

        self.status_label = QLabel("이력서와 채용 페이지 URL을 준비해 주세요.")
        self.status_label.setObjectName("status")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.metrics = QFrame()
        self.metrics.setObjectName("metrics")
        metrics_layout = QHBoxLayout(self.metrics)
        self.detected_value = self._metric(metrics_layout, "0", "검출")
        self.actionable_value = self._metric(metrics_layout, "0", "입력 가능")
        self.warning_value = self._metric(metrics_layout, "0", "확인 필요")
        self.elapsed_value = self._metric(metrics_layout, "-", "소요 시간")
        layout.addWidget(self.metrics)

        self.name_frame = QFrame()
        self.name_frame.setObjectName("card")
        name_layout = QHBoxLayout(self.name_frame)
        name_layout.addWidget(QLabel("성·이름 확인"))
        self.family_edit = QLineEdit()
        self.family_edit.setPlaceholderText("성")
        self.given_edit = QLineEdit()
        self.given_edit.setPlaceholderText("이름")
        self.confirm_name_button = QPushButton("확인")
        self.confirm_name_button.clicked.connect(self._confirm_name)
        name_layout.addWidget(self.family_edit)
        name_layout.addWidget(self.given_edit)
        name_layout.addWidget(self.confirm_name_button)
        layout.addWidget(self.name_frame)

        actions = QHBoxLayout()
        self.prepare_button = QPushButton("이력서 읽고 준비")
        self.prepare_button.setObjectName("primary")
        self.prepare_button.clicked.connect(self._prepare)
        self.open_button = QPushButton("브라우저 열기")
        self.open_button.setObjectName("primary")
        self.open_button.clicked.connect(self._open_browser)
        self.refresh_button = QPushButton("상태 새로고침")
        self.refresh_button.clicked.connect(self._refresh_browser)
        self.fill_button = QPushButton("이 페이지 자동 입력")
        self.fill_button.setObjectName("primary")
        self.fill_button.setToolTip("현재 브라우저에 열린 지원서 페이지를 검사하고, 확실한 항목만 입력한 뒤 다시 확인합니다.")
        self.fill_button.clicked.connect(self._fill)
        actions.addWidget(self.prepare_button)
        actions.addWidget(self.open_button)
        actions.addWidget(self.refresh_button)
        actions.addWidget(self.fill_button)
        layout.addLayout(actions)

        secondary_actions = QHBoxLayout()
        self.approve_button = QPushButton("도메인 허용")
        self.approve_button.clicked.connect(self._approve_domain)
        self.next_button = QPushButton("다음 페이지")
        self.next_button.clicked.connect(self._next_page)
        secondary_actions.addWidget(self.approve_button)
        secondary_actions.addWidget(self.next_button)
        layout.addLayout(secondary_actions)

        self.table_hint = QLabel("")
        self.table_hint.setObjectName("muted")
        layout.addWidget(self.table_hint)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["항목", "판단 근거", "상태", ""])
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(0, self.table.horizontalHeader().ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, self.table.horizontalHeader().ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, self.table.horizontalHeader().ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, self.table.horizontalHeader().ResizeMode.ResizeToContents)
        self.table.cellDoubleClicked.connect(self._row_activated)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(self.table.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(self.table.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)

        footer = QHBoxLayout()
        self.reset_button = QPushButton("다시")
        self.reset_button.clicked.connect(self._reset)
        self.exit_button = QPushButton("종료")
        self.exit_button.clicked.connect(self.close)
        footer.addWidget(self.reset_button)
        footer.addStretch()
        footer.addWidget(QLabel("최종 제출은 사용자만 할 수 있습니다."))
        footer.addStretch()
        footer.addWidget(self.exit_button)
        layout.addLayout(footer)
        self.setCentralWidget(root)

    # 화면 스타일(색상·여백) 지정.
    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #ffffff; color: #111111; font-size: 14px; }
            QLabel#title { color: #000000; font-size: 28px; font-weight: 700; }
            QLabel#muted { color: #666666; }
            QLabel#status { background: #f7f7f7; border: 1px solid #d8d8d8; padding: 12px; }
            QFrame#card { background: #ffffff; border: 1px solid #d8d8d8; border-radius: 8px; }
            QFrame#metrics { background: #f7f7f7; border: 1px solid #d8d8d8; border-radius: 8px; }
            QFrame#metrics QLabel { color: #111111; }
            QLineEdit { background: #ffffff; color: #111111; border: 1px solid #b8b8b8; border-radius: 6px; padding: 8px; }
            QLineEdit:focus { border: 1px solid #111111; }
            QPushButton { background: #ffffff; color: #111111; border: 1px solid #777777; border-radius: 6px; padding: 8px 12px; }
            QPushButton:hover { background: #f2f2f2; border-color: #111111; }
            QPushButton:pressed { background: #e7e7e7; }
            QPushButton:disabled { color: #a0a0a0; background: #f5f5f5; border-color: #d0d0d0; }
            QPushButton#primary { background: #111111; color: #ffffff; border-color: #111111; font-weight: 600; }
            QPushButton#primary:hover { background: #333333; }
            QTableWidget { background: #ffffff; alternate-background-color: #fafafa; border: 1px solid #d8d8d8; border-radius: 6px; gridline-color: #e8e8e8; }
            QLabel#step { background: #f3f3f3; color: #777777; border: 1px solid #e0e0e0; border-radius: 6px; padding: 6px 4px; font-size: 12px; }
            QLabel#stepActive { background: #111111; color: #ffffff; border: 1px solid #111111; border-radius: 6px; padding: 6px 4px; font-size: 12px; font-weight: 700; }
            QLabel#stepDone { background: #ffffff; color: #111111; border: 1px solid #b8b8b8; border-radius: 6px; padding: 6px 4px; font-size: 12px; }
            QHeaderView::section { background: #f3f3f3; color: #111111; padding: 8px; border: 0; border-bottom: 1px solid #d8d8d8; font-weight: 600; }
            """
        )

    # 요약 숫자 카드 하나를 만듭니다(찾은 칸, 입력 완료, 확인 필요, 소요 시간).
    @staticmethod
    def _metric(layout: QHBoxLayout, initial: str, caption: str) -> QLabel:
        box = QVBoxLayout()
        value = QLabel(initial)
        value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        value.setStyleSheet("font-size: 23px; font-weight: 700;")
        label = QLabel(caption)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(value)
        box.addWidget(label)
        layout.addLayout(box, 1)
        value.setProperty("caption", label)
        return value

    # 창을 항상 위에 둘지 전환(브라우저와 나란히 보기 위함).
    def _toggle_always_on_top(self, checked: bool) -> None:
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, checked)
        self.show()

    # PDF 이력서 파일 선택.
    def _choose_resume(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "PDF 이력서 선택", "", "PDF (*.pdf)")
        if selected:
            self.resume_path = Path(selected)
            self.resume_edit.setText(self.resume_path.name)

    # 작업 중에는 버튼을 잠가 중복 실행을 막습니다.
    def _set_busy(self, busy: bool) -> None:
        for widget in (
            self.choose_resume_button,
            self.prepare_button,
            self.open_button,
            self.refresh_button,
            self.approve_button,
            self.fill_button,
            self.next_button,
            self.confirm_name_button,
            self.reset_button,
        ):
            widget.setEnabled(not busy)
        if not busy:
            self._update_buttons()

    # 공통 실행 도우미: 상태 문구 표시 → 버튼 잠금 → 백그라운드 실행 → 성공/실패 처리 → 잠금 해제.
    def _run(self, message: str, function: Callable[[], object], success: Callable[[Any], None]) -> None:
        self.status_label.setText(message)
        self._set_busy(True)
        operation = ApiOperation(function)
        self._operations.add(operation)

        def succeeded(result: object) -> None:
            success(result)

        def failed(message_text: str) -> None:
            self.status_label.setText(message_text)
            QMessageBox.warning(self, "Apply Local", message_text)

        def finished() -> None:
            self._operations.discard(operation)
            self._set_busy(False)

        operation.signals.succeeded.connect(succeeded)
        operation.signals.failed.connect(failed)
        operation.signals.finished.connect(finished)
        self.thread_pool.start(operation)

    # ① 단계: 입력값 확인 후 이력서 분석 요청. 소요 시간 측정도 여기서 시작합니다.
    def _prepare(self) -> None:
        url = self.url_edit.text().strip()
        if self.resume_path is None or not self.resume_path.is_file():
            QMessageBox.information(self, "Apply Local", "PDF 이력서를 선택해 주세요.")
            return
        if self.resume_path.suffix.casefold() != ".pdf":
            QMessageBox.information(self, "Apply Local", "PDF 이력서만 사용할 수 있습니다.")
            return
        if not url:
            QMessageBox.information(self, "Apply Local", "채용 페이지 URL을 입력해 주세요.")
            return
        self.started_at.start()
        self._run(
            "이력서를 로컬에서 읽고 있습니다…",
            lambda: self.client.prepare(url, self.resume_path),
            self._prepared,
        )

    # 이력서 분석 결과를 표에 보여 주고, 이름 확인이 필요하면 성/이름 입력칸을 채워 둡니다.
    def _prepared(self, payload: dict[str, Any]) -> None:
        self.task_id = payload["task"]["task_id"]
        self.preview = payload["preview"]
        self.status_label.setText("이력서 인식이 완료되었습니다. 결과를 확인해 주세요.")
        values = self.preview["values"]
        self.detected_value.setText(str(sum(bool(value) for value in values.values())))
        self.actionable_value.setText("-")
        self.warning_value.setText(str(len(self.preview["warnings"])))
        self._show_preview(values)
        if self.preview["name_confirmation_required"]:
            self.family_edit.setText(values.get("person.name.family", ""))
            self.given_edit.setText(values.get("person.name.given", ""))
        self._update_buttons()

    # 이력서 미리보기 표. 값마다 출처(인식 완료/로컬 LLM·확인 필요/사용자 수정)를 표시합니다.
    def _show_preview(self, values: dict[str, str]) -> None:
        self.table_mode = "preview"
        self.table_items = [{"path": path, "value": value} for path, value in values.items()]
        self.table_hint.setText("이력서에서 읽은 값입니다. 값이 틀리거나 비어 있으면 행을 더블클릭해 고치세요.")
        sources = (self.preview or {}).get("sources", {})

        def state(path: str, value: str) -> str:
            if not value:
                return "값 없음"
            return {"llm": "로컬 LLM · 확인 필요", "user": "사용자 수정"}.get(sources.get(path, ""), "인식 완료")

        self._set_rows(
            [(path, value or "—", state(path, value)) for path, value in values.items()],
            actions=["수정"] * len(values),
        )

    # 사용자가 확인한 성/이름을 서버에 보냅니다.
    def _confirm_name(self) -> None:
        if not self.task_id:
            return
        family, given = self.family_edit.text().strip(), self.given_edit.text().strip()
        if not family or not given:
            QMessageBox.information(self, "Apply Local", "성과 이름을 확인해 주세요.")
            return
        self._run(
            "성과 이름을 확인하고 있습니다…",
            lambda: self.client.confirm_name(self.task_id or "", family, given),
            self._name_confirmed,
        )

    # 이름 확인 완료 후 미리보기 갱신.
    def _name_confirmed(self, preview: dict[str, Any]) -> None:
        self.preview = preview
        self.status_label.setText("이름 확인이 완료되었습니다.")
        self._show_preview(preview["values"])
        self._update_buttons()

    # ② 단계: 작업용 브라우저 열기 / 상태 새로 고침.
    def _open_browser(self) -> None:
        if not self.task_id:
            return
        self._run(
            "브라우저를 열고 있습니다…",
            lambda: self.client.open_browser(self.task_id or ""),
            self._browser_updated,
        )

    def _refresh_browser(self) -> None:
        if self.task_id:
            self._run(
                "브라우저 상태를 확인하고 있습니다…",
                lambda: self.client.browser_status(self.task_id or ""),
                self._browser_updated,
            )

    # 브라우저 상태 반영. 승인이 필요한 도메인이 있으면 안내하고, 없으면 로그인 후 자동 입력하라고 안내합니다.
    def _browser_updated(self, browser: dict[str, Any]) -> None:
        self.browser = browser
        self.pending_domain = browser.get("blocked_hostname") or next(
            iter(browser.get("discovered_frame_hosts") or []), None
        )
        if self.pending_domain:
            self.status_label.setText(f"이동 또는 폼 도메인을 확인해 주세요: {self.pending_domain}")
        else:
            title = browser.get("page_title") or browser.get("current_hostname") or "브라우저"
            self.status_label.setText(
                f"{title} · 로그인이 필요하면 브라우저에서 직접 완료하고, 지원서 화면에서 '이 페이지 자동 입력'을 누르세요."
            )
        self._update_buttons()

    # 도메인 허용은 반드시 사용자에게 한 번 물어본 뒤 요청합니다.
    def _approve_domain(self) -> None:
        if not self.task_id or not self.pending_domain:
            return
        hostname = self.pending_domain
        answer = QMessageBox.question(self, "도메인 허용", f"{hostname}\n\n이 도메인을 채용 페이지의 일부로 허용할까요?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._run(
            "도메인을 허용하고 있습니다…",
            lambda: self.client.approve_domain(self.task_id or "", hostname),
            self._browser_updated,
        )

    # ③ 단계: 이 페이지 자동 입력(규칙 입력 + 다시 읽기 확인).
    def _fill(self) -> None:
        if self.task_id:
            self._run(
                "확실한 항목을 입력하고 브라우저에서 다시 검증하고 있습니다…",
                lambda: self.client.fill_browser(self.task_id or ""),
                self._filled,
            )

    # 규칙 입력 결과를 먼저 보여 준 뒤, 이어서 AI 2차 확인(_lookup)을 시작합니다(사용자가 기다리지 않게).
    def _filled(self, report: dict[str, Any]) -> None:
        self.fill_report = report
        self.inspection = None
        self._browser_updated(report["browser"])
        needs_attention = report["needs_human_count"] + report["failed_count"]
        self.detected_value.setText(str(report.get("field_count", len(report["results"]))))
        self.actionable_value.setText(str(report["filled_count"]))
        self.actionable_value.property("caption").setText("입력 완료")
        self.warning_value.setText(str(needs_attention))
        self._update_elapsed()
        disabled = report.get("disabled_count", 0)
        self.status_label.setText(
            f"입력·검증 {report['filled_count']}개, 기존 값 유지 {report['kept_existing_count']}개, "
            f"직접 확인 {needs_attention}개입니다."
            + (f" 페이지에서 비활성화된 {disabled}개는 입력하지 않았습니다." if disabled else "")
            + (f" {report['notice']}" if report.get("notice") else "")
        )
        self._show_results(report["results"])
        self._update_buttons()
        # Started after this operation finishes so the rule results stay visible meanwhile.
        QTimer.singleShot(0, self._lookup)

    # 모든 입력이 끝나면 작업당 한 번 '제출은 직접 하세요' 안내를 띄웁니다.
    def _finish_fill(self) -> None:
        """After the rule fill and the second check: tell once per task that submitting is the user's."""
        if self.fill_report and self.fill_report.get("submit_control_count") and not self.submit_notice_shown:
            self.submit_notice_shown = True
            QMessageBox.information(
                self,
                "최종 제출은 직접",
                "자동 입력이 끝났습니다. Agent는 제출 버튼을 누르지 않습니다. 모든 내용을 확인한 뒤 브라우저에서 직접 제출하세요.",
            )

    # AI 2차 확인 요청: 확인이 필요한 행만 모아 로컬 LLM에 대조를 요청합니다(1~3분).
    def _lookup(self) -> None:
        if not self.task_id or self.fill_report is None:
            return
        rows = [
            {"fingerprint": item["fingerprint"], "status": item["status"]}
            for item in self.fill_report["results"]
            if item.get("status") in SECOND_CHECK and item.get("fingerprint")
        ]
        if not rows:
            self._finish_fill()
            return
        self.fill_summary = self.status_label.text()
        self._run(
            f"규칙 입력은 끝났습니다. 남은 {len(rows)}개 항목을 로컬 LLM으로 이력서와 다시 대조하고 있습니다… (1~3분)",
            lambda: self.client.lookup_browser(self.task_id or "", rows),
            self._looked_up,
        )
        # Buttons are disabled while the local model works; say so instead of looking frozen.
        self.fill_button.setText("이력서 대조 중…")

    # 2차 확인 결과를 기존 결과 표에 fingerprint 기준으로 합칩니다(같은 칸을 정확히 교체).
    def _looked_up(self, report: dict[str, Any]) -> None:
        if self.fill_report is None:
            return
        # Positional keys may differ between requests; the fingerprint names the same field.
        found = {item.get("fingerprint") or item["form_field"]: item for item in report["results"]}
        results = [found.pop(item.get("fingerprint") or item["form_field"], item) for item in self.fill_report["results"]]
        results.extend(found.values())
        self.fill_report = {**self.fill_report, "results": results}
        self._show_results(results)
        pending = sum(item.get("status") in REVIEWABLE for item in results)
        self.warning_value.setText(str(pending))
        added = report.get("lookup_filled_count", 0)
        message = getattr(self, "fill_summary", "")
        if added:
            message += f" 이력서에서 추가로 찾아 입력한 {added}개는 '입력됨 · 확인 필요'로 표시했습니다."
        if report.get("notice"):
            message += f" {report['notice']}"
        self.status_label.setText(message.strip())
        self._update_buttons()
        self._finish_fill()

    # 안전한 '다음' 버튼으로 다음 페이지 이동 요청 / 이동 후 상태 초기화.
    def _next_page(self) -> None:
        if self.task_id:
            self._run(
                "다음 페이지로 이동하고 있습니다…",
                lambda: self.client.next_page(self.task_id or ""),
                self._next_completed,
            )

    def _next_completed(self, browser: dict[str, Any]) -> None:
        self.inspection = None
        self.fill_report = None
        self._browser_updated(browser)
        self.status_label.setText("다음 페이지로 이동했습니다. '이 페이지 자동 입력'을 다시 누르세요.")

    # 입력 결과 표. 사용자가 처리해야 할 항목을 맨 위에 보여 줍니다.
    def _show_results(self, results: list[dict[str, Any]]) -> None:
        # Items needing the user come first so they are visible without scrolling.
        ordered = sorted(results, key=lambda item: 0 if item.get("status") in REVIEWABLE else ROW_ORDER.get(item.get("status"), 5))
        self.table_mode = "results"
        self.table_items = ordered
        pending = sum(item.get("status") in REVIEWABLE for item in ordered)
        absent = sum(item.get("status") == "not_in_resume" for item in ordered)
        user_only = sum(item.get("status") == "user_only" for item in ordered)
        hint = f"확인이 필요한 {pending}개 항목은 '상세'를 눌러 값을 고르거나 고친 뒤 입력하세요." if pending else "확인이 필요한 항목은 없습니다."
        if absent or user_only:
            hint += f" 이력서에 없는 항목 {absent}개, 직접 처리할 항목 {user_only}개도 브라우저에서 확인하세요."
        self.table_hint.setText(hint)
        self._set_rows(
            [
                (
                    f"{item.get('label') or '라벨 없음'}  ·  {item.get('resume_path') or ''}",
                    item.get("reason", ""),
                    STATUS_LABELS.get(item.get("status", ""), item.get("status", "")),
                )
                for item in ordered
            ],
            actions=["상세" if item.get("status") in DETAIL else "" for item in ordered],
        )

    # 표의 행과 '상세/수정' 버튼을 채웁니다.
    def _set_rows(self, rows: list[tuple[str, str, str]], actions: list[str] | None = None) -> None:
        self.table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                self.table.setItem(row, column, item)
            label = (actions or [""] * len(rows))[row]
            if label:
                button = QPushButton(label)
                button.clicked.connect(lambda _checked=False, index=row: self._row_activated(index, 0))
                self.table.setCellWidget(row, 3, button)
            else:
                self.table.removeCellWidget(row, 3)
                self.table.setItem(row, 3, QTableWidgetItem(""))

    # 행 클릭: 미리보기 모드면 이력서 값 수정, 결과 모드면 확인 창 열기.
    def _row_activated(self, row: int, _column: int) -> None:
        if not self.task_id or not 0 <= row < len(self.table_items):
            return
        item = self.table_items[row]
        if self.table_mode == "preview":
            self._edit_resume_value(item["path"], item["value"])
        elif item.get("status") in DETAIL:
            self._review(item)

    # 이력서 값 수정 요청(이번 실행 동안만, 원본 파일은 바뀌지 않음).
    def _edit_resume_value(self, path: str, current: str) -> None:
        value, accepted = QInputDialog.getText(self, "이력서 값 수정", path, text=current)
        if not accepted or value.strip() == current:
            return
        self._run(
            "이력서 값을 수정하고 있습니다…",
            lambda: self.client.edit_resume(self.task_id or "", path, value.strip()),
            lambda preview: self._resume_edited(preview, path),
        )

    # 수정 결과를 반영하고 출처를 '사용자 수정'으로 표시합니다.
    def _resume_edited(self, preview: dict[str, Any], path: str = "") -> None:
        sources = dict((self.preview or {}).get("sources", {}))
        if path:
            sources[path] = "user"
        self.preview = {**(self.preview or {}), **preview, "sources": sources}
        self.status_label.setText("이력서 값을 수정했습니다. 이 실행 동안만 사용되며 파일은 바뀌지 않습니다.")
        self._show_preview(preview["values"])
        self._update_buttons()

    # ④ 단계: 확인 창을 열고, 사용자가 고른 값으로 칸 하나를 입력 요청합니다.
    def _review(self, item: dict[str, Any]) -> None:
        values = (self.preview or {}).get("values", {})
        dialog = ReviewDialog(self, item, values.get(item.get("resume_path") or "", ""))
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.value():
            return
        value, chosen = dialog.value(), dialog.chosen_from_page()
        self._run(
            "선택한 값을 입력하고 브라우저에서 다시 확인하고 있습니다…",
            lambda: self.client.fill_field(self.task_id or "", item, value, chosen=chosen),
            self._field_filled,
        )

    # 칸 하나 입력 결과를 표에 반영. 실패하면 이유를 팝업으로 알려 줍니다(클릭이 무시된 것처럼 보이지 않게).
    def _field_filled(self, result: dict[str, Any]) -> None:
        def same(item: dict[str, Any]) -> bool:
            if item.get("fingerprint") and result.get("fingerprint"):
                return item["fingerprint"] == result["fingerprint"]
            return item.get("form_field") == result.get("form_field")

        results = [result if same(item) else item for item in self.table_items]
        if self.fill_report is not None:
            self.fill_report = {**self.fill_report, "results": results}
        self._show_results(results)
        self.warning_value.setText(str(sum(item.get("status") in REVIEWABLE for item in results)))
        self.actionable_value.setText(str(sum(item.get("status") == "filled_verified" for item in results)))
        filled = result.get("status") == "filled_verified"
        self.status_label.setText("입력하고 브라우저에서 확인했습니다." if filled else result.get("reason", ""))
        self._update_buttons()
        if not filled:
            # Without this the row can look unchanged and the click seems to do nothing.
            QMessageBox.information(self, "입력하지 못했습니다", result.get("reason", "") or "브라우저에서 직접 확인해 주세요.")

    # 현재 상태로 진행 단계(①~⑤)를 계산하고 화면에 표시합니다.
    def _current_step(self) -> int:
        if self.preview is None or self.preview.get("name_confirmation_required"):
            return 0
        if self.browser is None:
            return 1
        if self.fill_report is None:
            return 2 if self.browser.get("can_inspect") else 1
        if any(item.get("status") in REVIEWABLE for item in self.fill_report.get("results", [])):
            return 3
        return 4

    def _render_steps(self) -> None:
        current = self._current_step()
        for index, label in enumerate(self.step_labels):
            label.setObjectName("stepActive" if index == current else "stepDone" if index < current else "step")
            label.style().unpolish(label)
            label.style().polish(label)

    # 소요 시간 표시(이력서 준비 시작부터 규칙 입력 완료까지).
    def _update_elapsed(self) -> None:
        if self.started_at.isValid():
            elapsed = self.started_at.elapsed() // 1000
            self.elapsed_value.setText(f"{elapsed // 60:02d}:{elapsed % 60:02d}")

    # 상태에 따라 버튼 표시/활성화를 결정합니다(지금 할 수 있는 버튼만 보이게).
    def _update_buttons(self) -> None:
        has_preview = self.preview is not None
        needs_name = bool(has_preview and self.preview and self.preview["name_confirmation_required"])
        has_browser = self.browser is not None
        can_inspect = bool(has_browser and self.browser and self.browser.get("can_inspect"))
        has_fill_report = self.fill_report is not None
        self.prepare_button.setVisible(not has_preview)
        self.open_button.setVisible(has_preview and not has_browser)
        self.open_button.setEnabled(has_preview and not needs_name)
        self.name_frame.setVisible(needs_name)
        self.refresh_button.setVisible(has_browser)
        self.approve_button.setVisible(bool(self.pending_domain))
        self.fill_button.setVisible(has_browser)
        self.fill_button.setEnabled(can_inspect)
        self.fill_button.setText("이 페이지 다시 자동 입력" if has_fill_report else "이 페이지 자동 입력")
        self.next_button.setVisible(
            bool(has_fill_report and self.fill_report and self.fill_report.get("safe_next_available"))
        )
        self.choose_resume_button.setEnabled(not has_preview)
        self.url_edit.setReadOnly(has_preview)
        self._render_steps()

    # 화면을 처음 상태로 초기화.
    def _render_initial(self) -> None:
        self.preview = None
        self.browser = None
        self.inspection = None
        self.fill_report = None
        self.pending_domain = None
        self.submit_notice_shown = False
        self.started_at.invalidate()
        self.detected_value.setText("0")
        self.actionable_value.setText("0")
        self.actionable_value.property("caption").setText("입력 가능")
        self.warning_value.setText("0")
        self.elapsed_value.setText("-")
        self.table.setRowCount(0)
        self.table_items = []
        self.table_mode = "preview"
        self.table_hint.setText("")
        self.status_label.setText("이력서와 채용 페이지 URL을 준비해 주세요.")
        self.url_edit.setReadOnly(False)
        self._update_buttons()

    # 새 작업 시작: 서버의 현재 작업을 취소(민감 데이터 삭제)한 뒤 화면을 비웁니다.
    def _reset(self) -> None:
        task_id = self.task_id

        def cleared(_result: object | None = None) -> None:
            self.task_id = None
            if not self.keep_resume.isChecked():
                self.resume_path = None
                self.resume_edit.clear()
            self.url_edit.clear()
            self._render_initial()

        if task_id:
            self._run("현재 작업을 정리하고 있습니다…", lambda: self.client.cancel(task_id), cleared)
        else:
            cleared()

    # 창을 닫을 때도 작업을 취소해 브라우저와 민감 데이터를 정리합니다.
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
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
    window = ApplyLocalWindow(DesktopApiClient(base_url, token))
    window.show()
    return app.exec()

