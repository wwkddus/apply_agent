from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

import httpx
from PySide6.QtCore import QElapsedTimer, QObject, QRunnable, QThreadPool, Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
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
}


class DesktopApiError(RuntimeError):
    pass


class DesktopApiClient:
    """Synchronous client used only inside background Qt workers."""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-Job-Agent-Token": token, "Accept": "application/json"}

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

    def prepare(self, target_url: str, resume_path: Path) -> dict[str, Any]:
        task = self._request("POST", "/api/tasks", json={"target_url": target_url})
        task_id = task["task_id"]
        try:
            with resume_path.open("rb") as handle:
                preview = self._request(
                    "POST",
                    f"/api/tasks/{task_id}/resume",
                    files={"file": (resume_path.name, handle, "application/pdf")},
                    timeout=60,
                )
        except Exception:
            try:
                self.cancel(task_id)
            except DesktopApiError:
                pass
            raise
        return {"task": task, "preview": preview}

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
        return self._request("POST", f"/api/tasks/{task_id}/browser/fill", timeout=60)

    def next_page(self, task_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/tasks/{task_id}/browser/next", timeout=30)


class OperationSignals(QObject):
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()


class ApiOperation(QRunnable):
    def __init__(self, function: Callable[[], object]):
        super().__init__()
        self.function = function
        self.signals = OperationSignals()

    def run(self) -> None:
        try:
            result = self.function()
        except Exception as error:  # The UI receives a safe, local error string only.
            self.signals.failed.emit(str(error) or type(error).__name__)
        else:
            self.signals.succeeded.emit(result)
        finally:
            self.signals.finished.emit()


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
        self.setWindowTitle("Apply Local")
        self.setMinimumSize(720, 720)
        self.resize(760, 820)
        self._build_ui()
        self._apply_style()
        self._render_initial()

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
        self.inspect_button = QPushButton("현재 페이지 확인")
        self.inspect_button.setObjectName("primary")
        self.inspect_button.clicked.connect(self._inspect)
        actions.addWidget(self.prepare_button)
        actions.addWidget(self.open_button)
        actions.addWidget(self.refresh_button)
        actions.addWidget(self.inspect_button)
        layout.addLayout(actions)

        secondary_actions = QHBoxLayout()
        self.approve_button = QPushButton("도메인 허용")
        self.approve_button.clicked.connect(self._approve_domain)
        self.fill_button = QPushButton("확인된 항목만 입력")
        self.fill_button.setObjectName("primary")
        self.fill_button.clicked.connect(self._fill)
        self.next_button = QPushButton("다음 페이지")
        self.next_button.clicked.connect(self._next_page)
        secondary_actions.addWidget(self.approve_button)
        secondary_actions.addWidget(self.fill_button)
        secondary_actions.addWidget(self.next_button)
        layout.addLayout(secondary_actions)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["항목", "판단 근거", "상태"])
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(0, self.table.horizontalHeader().ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, self.table.horizontalHeader().ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, self.table.horizontalHeader().ResizeMode.ResizeToContents)
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
            QHeaderView::section { background: #f3f3f3; color: #111111; padding: 8px; border: 0; border-bottom: 1px solid #d8d8d8; font-weight: 600; }
            """
        )

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
        return value

    def _toggle_always_on_top(self, checked: bool) -> None:
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, checked)
        self.show()

    def _choose_resume(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "PDF 이력서 선택", "", "PDF (*.pdf)")
        if selected:
            self.resume_path = Path(selected)
            self.resume_edit.setText(self.resume_path.name)

    def _set_busy(self, busy: bool) -> None:
        for widget in (
            self.choose_resume_button,
            self.prepare_button,
            self.open_button,
            self.refresh_button,
            self.inspect_button,
            self.approve_button,
            self.fill_button,
            self.next_button,
            self.confirm_name_button,
            self.reset_button,
        ):
            widget.setEnabled(not busy)
        if not busy:
            self._update_buttons()

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

    def _show_preview(self, values: dict[str, str]) -> None:
        rows = []
        for path, value in values.items():
            if value:
                rows.append((path, value, "인식 완료"))
        self._set_rows(rows)

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

    def _name_confirmed(self, preview: dict[str, Any]) -> None:
        self.preview = preview
        self.status_label.setText("이름 확인이 완료되었습니다.")
        self._show_preview(preview["values"])
        self._update_buttons()

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

    def _browser_updated(self, browser: dict[str, Any]) -> None:
        self.browser = browser
        self.pending_domain = browser.get("blocked_hostname") or next(
            iter(browser.get("discovered_frame_hosts") or []), None
        )
        if self.pending_domain:
            self.status_label.setText(f"이동 또는 폼 도메인을 확인해 주세요: {self.pending_domain}")
        else:
            title = browser.get("page_title") or browser.get("current_hostname") or "브라우저"
            self.status_label.setText(f"{title} · 로그인이 필요하면 직접 완료한 뒤 현재 페이지를 확인하세요.")
        self._update_buttons()

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

    def _inspect(self) -> None:
        if self.task_id:
            self._run(
                "현재 페이지의 필드를 검출하고 있습니다…",
                lambda: self.client.inspect_browser(self.task_id or ""),
                self._inspected,
            )

    def _inspected(self, report: dict[str, Any]) -> None:
        self.inspection = report
        self.fill_report = None
        self.browser = report["browser"]
        decisions = report["decisions"]
        actionable = sum(item["status"] == "planned" for item in decisions)
        warnings = sum(item["status"] not in {"planned", "kept_existing"} for item in decisions)
        self.detected_value.setText(str(report["field_count"]))
        self.actionable_value.setText(str(actionable))
        self.warning_value.setText(str(warnings))
        self._update_elapsed()
        self.status_label.setText(f"필드 {report['field_count']}개를 검출했고 {actionable}개를 입력할 수 있습니다.")
        self._set_rows([
            (
                f"{item.get('label') or '라벨 없음'}  ·  {item.get('widget_kind', '')}",
                item.get("reason", ""),
                STATUS_LABELS.get(item.get("status", ""), item.get("status", "")),
            )
            for item in decisions
        ])
        domains = report.get("unapproved_frame_hosts") or []
        self.pending_domain = next(iter(domains), None)
        self._update_buttons()

    def _fill(self) -> None:
        if self.task_id:
            self._run(
                "확실한 항목을 입력하고 브라우저에서 다시 검증하고 있습니다…",
                lambda: self.client.fill_browser(self.task_id or ""),
                self._filled,
            )

    def _filled(self, report: dict[str, Any]) -> None:
        self.fill_report = report
        self.browser = report["browser"]
        needs_attention = report["needs_human_count"] + report["failed_count"]
        self.actionable_value.setText(str(report["filled_count"]))
        self.warning_value.setText(str(needs_attention))
        self._update_elapsed()
        self.status_label.setText(
            f"입력·검증 {report['filled_count']}개, 기존 값 유지 {report['kept_existing_count']}개, "
            f"직접 확인 {needs_attention}개입니다."
        )
        self._set_rows([
            (
                f"{item.get('label') or '라벨 없음'}  ·  {item.get('resume_path') or ''}",
                item.get("reason", ""),
                STATUS_LABELS.get(item.get("status", ""), item.get("status", "")),
            )
            for item in report["results"]
        ])
        if report.get("submit_control_count"):
            QMessageBox.information(
                self,
                "최종 제출 버튼 감지",
                "Agent는 최종 제출 버튼을 누르지 않습니다. 모든 내용을 확인한 뒤 브라우저에서 직접 제출하세요.",
            )
        self._update_buttons()

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
        self.status_label.setText("다음 페이지로 이동했습니다. 현재 페이지를 다시 확인하세요.")

    def _set_rows(self, rows: list[tuple[str, str, str]]) -> None:
        self.table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                self.table.setItem(row, column, item)

    def _update_elapsed(self) -> None:
        if self.started_at.isValid():
            elapsed = self.started_at.elapsed() // 1000
            self.elapsed_value.setText(f"{elapsed // 60:02d}:{elapsed % 60:02d}")

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
        self.inspect_button.setVisible(has_browser)
        self.inspect_button.setEnabled(can_inspect)
        self.approve_button.setVisible(bool(self.pending_domain))
        self.fill_button.setVisible(self.inspection is not None and not has_fill_report)
        self.next_button.setVisible(
            bool(has_fill_report and self.fill_report and self.fill_report.get("safe_next_available"))
        )
        self.choose_resume_button.setEnabled(not has_preview)
        self.url_edit.setReadOnly(has_preview)

    def _render_initial(self) -> None:
        self.preview = None
        self.browser = None
        self.inspection = None
        self.fill_report = None
        self.pending_domain = None
        self.started_at.invalidate()
        self.detected_value.setText("0")
        self.actionable_value.setText("0")
        self.warning_value.setText("0")
        self.elapsed_value.setText("-")
        self.table.setRowCount(0)
        self.status_label.setText("이력서와 채용 페이지 URL을 준비해 주세요.")
        self.url_edit.setReadOnly(False)
        self._update_buttons()

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

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        if self.task_id:
            try:
                self.client.cancel(self.task_id)
            except DesktopApiError:
                pass
        event.accept()


def run_desktop(base_url: str, token: str) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Apply Local")
    window = ApplyLocalWindow(DesktopApiClient(base_url, token))
    window.show()
    return app.exec()

