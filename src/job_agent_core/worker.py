# [역할] 브라우저를 건드리지 않고 '무엇을 어디에 넣을지' 계획만 세우는 작업자입니다.
# 매핑 → 입력 계획 → 위젯 동작 순서로 계산만 하므로 테스트하기 쉽습니다.

from __future__ import annotations

import asyncio

from pydantic import BaseModel, ConfigDict

from .korean_scope import KoreanMvpMapper
from .mapping import FormField, MappingProvider, MappingRecord
from .models import ResumeProfile
from .planning import FillAction, build_fill_plan
from .widgets import WidgetAction, plan_widget_action


# 계획 결과 묶음: 칸 연결 결과, 입력 계획, 위젯 동작, 연결 못 한 칸 수.
class WorkerPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mappings: list[MappingRecord]
    fill_actions: list[FillAction]
    widget_actions: list[WidgetAction]
    skipped_field_count: int = 0


# 실제 입력 전에 계획만 세우는 작업자. 매퍼를 주입받을 수 있어 테스트에서 가짜로 바꿔 끼웁니다.
class KoreanPlanningWorker:
    """Side-effect-free worker boundary; Playwright execution is added in Phase 5."""

    def __init__(self, mapper: MappingProvider | None = None):
        self.mapper = mapper or KoreanMvpMapper()

    # 매퍼가 남긴 한 줄 안내(예: AI 칸 연결을 쓰지 못해 규칙만 사용). 규칙 매퍼는 없음.
    @property
    def notice(self) -> str:
        return getattr(self.mapper, "notice", "")

    # 흐름: ① 칸을 이력서 항목에 연결 → ② 입력할 값 계획 → ③ 칸 종류에 맞는 위젯 동작으로 변환.
    async def plan(self, profile: ResumeProfile, fields: list[FormField]) -> WorkerPlan:
        mapping_result = await self.mapper.map_fields(fields)
        mappings = mapping_result.mappings
        actions = build_fill_plan(profile, fields, mappings)
        by_field = {field.key: field for field in fields}
        widget_actions = [
            plan_widget_action(by_field[action.form_field], action.resume_path, action.value, action.part)
            for action in actions
            if action.status == "planned" and action.value is not None
        ]
        return WorkerPlan(
            mappings=mappings,
            fill_actions=actions,
            widget_actions=widget_actions,
            skipped_field_count=sum(item.resume_path == "unmapped" for item in mappings),
        )

    # 비동기 plan()을 일반 함수처럼 부를 수 있게 감싼 것(스크립트·테스트용).
    def plan_sync(self, profile: ResumeProfile, fields: list[FormField]) -> WorkerPlan:
        return asyncio.run(self.plan(profile, fields))
