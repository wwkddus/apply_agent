from __future__ import annotations

import asyncio

from pydantic import BaseModel, ConfigDict

from .korean_scope import KoreanMvpMapper
from .mapping import FormField, MappingRecord
from .models import ResumeProfile
from .planning import FillAction, build_fill_plan
from .widgets import WidgetAction, plan_widget_action


class WorkerPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mappings: list[MappingRecord]
    fill_actions: list[FillAction]
    widget_actions: list[WidgetAction]
    skipped_field_count: int = 0


class KoreanPlanningWorker:
    """Side-effect-free worker boundary; Playwright execution is added in Phase 5."""

    def __init__(self, mapper: KoreanMvpMapper | None = None):
        self.mapper = mapper or KoreanMvpMapper()

    async def plan(self, profile: ResumeProfile, fields: list[FormField]) -> WorkerPlan:
        mapping_result = await self.mapper.map_fields(fields)
        mappings = mapping_result.mappings
        actions = build_fill_plan(profile, fields, mappings)
        by_field = {field.key: field for field in fields}
        widget_actions = [
            plan_widget_action(by_field[action.form_field], action.resume_path, action.value)
            for action in actions
            if action.status == "planned" and action.value is not None
        ]
        return WorkerPlan(
            mappings=mappings,
            fill_actions=actions,
            widget_actions=widget_actions,
            skipped_field_count=sum(item.resume_path == "unmapped" for item in mappings),
        )

    def plan_sync(self, profile: ResumeProfile, fields: list[FormField]) -> WorkerPlan:
        return asyncio.run(self.plan(profile, fields))
