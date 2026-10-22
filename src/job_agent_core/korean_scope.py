# 실제로 쓰는 한국 채용 사이트용 칸 연결(매핑) 규칙. 

# 속성(name/id) → 라벨 규칙 순으로 연결한 뒤,
# 이름(성+이름 칸), 휴대폰 3칸(010/1234/5678), 
# 타인 정보(비상연락처 등) 같은 '칸 사이의 구조'를 보고 결과를 보정합니다.

from __future__ import annotations

import re

from .attribute_semantics import UNMAPPED, infer_from_attributes
from .constants import ALLOWED_RESUME_PATHS, EDUCATION_FIELDS, EDUCATION_LEVELS
from .mapping import FormField, INSTRUCTION_RE, MappingRecord, MappingResult
from .models import PersonalInfo, ResumeProfile


# 지원하는 이력서 경로 전체와, 화면에 보여줄 한국어 항목 이름.
KOREAN_MVP_PATHS = ALLOWED_RESUME_PATHS

KOREAN_MVP_FIELD_LABELS = {
    "person.name.full": "성명",
    "person.name.family": "성",
    "person.name.given": "이름",
    "person.name_en.full": "영문 성명",
    "person.name_en.family": "영문 성",
    "person.name_en.given": "영문 이름",
    "person.birth_date": "생년월일",
    "person.phone": "휴대폰번호",
    "person.email": "이메일",
    "person.email_secondary": "보조 이메일",
    "person.postal_code": "우편번호",
    "person.address": "주소",
    "person.address_detail": "상세주소",
    "education.highest_level": "최종 학력",
    "person.military_status": "병역",
    "person.disability_status": "장애 여부",
    "person.veteran_status": "보훈 여부",
    "skills": "보유기술",
}


# 학력 라벨 규칙(위에서부터 먼저 맞는 것을 사용). '학교'만 있는 넓은 규칙은 맨 끝에 둡니다.
_SITE_EDU_RULES = (
    ("double_major", r"복수\s*전공|부\s*전공|double\s*major|minor"),
    ("admission_type", r"입학\s*구분|admission\s*type"),
    ("graduation_status", r"졸업\s*(구분|여부|상태)|graduation\s*status|enrollment\s*status|학적\s*(상태|구분)|재학\s*(상태|여부)"),
    ("gpa_scale", r"만점|gpa\s*scale"),
    (
        "school_name",
        r"학교\s*명|대학교\s*명|대학원\s*명|school\s*name|university\s*name|college\s*name|institution",
    ),
    ("degree", r"학위|degree"),
    ("major", r"전공|학과|major|field\s*of\s*study|department"),
    ("admission_date", r"입학|admission|enrol"),
    ("graduation_date", r"졸업|graduat"),
    ("gpa", r"평점|학점|gpa"),
    ("campus_type", r"본교|분교|campus"),
    ("region", r"지역|소재지|region|location"),
    ("school_name", r"학교|school|university|college"),
)

# 개인정보 라벨 규칙. 보조 이메일을 기본 이메일보다 먼저 검사해 기본 이메일이 복사되지 않게 합니다.
_SITE_PERSONAL_RULES = (
    # Keyword only, so '장애', '장애여부', '장애정보' and 'Disability information' all match.
    ("person.military_status", r"병역|군\s*필|military"),
    ("person.disability_status", r"장애|disabilit"),
    ("person.veteran_status", r"보훈|veteran"),
    ("person.birth_date", r"생년월일|출생|birth"),
    ("person.address_detail", r"상세\s*주소|address\s*(line\s*2|detail)"),
    ("person.postal_code", r"우편\s*번호|postal|zip"),
    # A second address is a separate item; the primary email must not be copied into it.
    ("person.email_secondary", r"(보조|2차|추가|두\s*번째|예비|secondary|alternate|alternative|second)\s*(이메일|e-?mail)"),
    ("person.email", r"이메일|e-?mail"),
    ("person.address", r"주소|address"),
    ("person.phone", r"휴대|핸드폰|전화|연락처|phone|mobile|\btel\b"),
    ("skills", r"보유\s*기술|기술\s*스택|skills?"),
    ("person.name.full", r"성명|이름|full\s*name|applicant\s*name|^name$"),
)

# 학력 수준 판단 패턴: '지역/캠퍼스'는 학력 문맥일 때만 학력 항목으로 보고,
# '졸업일(graduate date)'을 대학원으로 착각하지 않도록 대학원은 명확한 표현만 인정합니다.
_GENERIC_EDU_FIELDS = {"region", "campus_type"}
# 'Graduate date' (졸업일) is not a graduate school; only explicit wordings name the level.
_GRAD_RE = re.compile(r"대학원|graduate\s*(school|level|program)|postgraduate|master|석사|박사|doctor", re.I)
_HIGHEST_RE = re.compile(r"최종\s*학력|highest\s*(level\s*of\s*)?education|last(est)?\s*education|final\s*education", re.I)
_UNIV_RE = re.compile(r"대학교|대학|학부|undergraduate|bachelor|학사|university|college", re.I)


# 신뢰도 0.8 이상이면 자동 수락, 아니면 사람 확인.
def _route(record: MappingRecord, field: FormField) -> MappingRecord:
    if record.resume_path == "unmapped":
        status = "unmapped_required" if field.required else "unmapped_optional"
    elif record.confidence >= 0.8:
        status = "auto_accepted"
    else:
        status = "needs_human_review"
    return record.model_copy(update={"status": status})


# Contact details of someone other than the applicant must never receive the applicant's values.
# 본인이 아닌 사람(비상연락처·보호자·추천인 등)의 칸. 지원자 정보가 들어가면 안 됩니다.
_THIRD_PARTY_RE = re.compile(r"비상|긴급|보호자|추천인|가족|부모|배우자|emergency|guardian|referee|reference|parent|spouse", re.I)
_LABEL_NOISE_RE = re.compile(r"\(?\s*필수\s*\)?|\*|[:：()\[\]]")
_SCRIPT_QUALIFIER_RE = re.compile(r"영문|한자|한글|국문|english|korean|chinese")
_ENGLISH_RE = re.compile(r"영문|english")
_OTHER_SCRIPT_RE = re.compile(r"한자|chinese")
# 이름 칸 라벨 → 역할(성/이름/전체 이름).
_NAME_ROLES = {
    "성": "family",
    "姓": "family",
    "lastname": "family",
    "familyname": "family",
    "surname": "family",
    "firstname": "given",
    "givenname": "given",
    "名": "given",
    "이름": "name",
    "name": "name",
    "성명": "full",
    "성함": "full",
    "fullname": "full",
    "applicantname": "full",
    "지원자명": "full",
    "지원자이름": "full",
    "지원자성명": "full",
}


# 속성(name/id)으로 정해진 결과인지 확인. 속성 결과는 구조 보정에서 덮어쓰지 않습니다.
def _from_attribute(record: MappingRecord) -> bool:
    return record.reason.startswith("attribute:")


# 라벨에서 '(필수)', '*', 괄호 등을 지운 비교용 문자열.
def _bare(value: str | None) -> str:
    return re.sub(r"\s+", "", _LABEL_NOISE_RE.sub("", value or "")).casefold()


# 칸 하나가 성/이름/전체 이름/영문 이름/한자 이름 중 무엇인지 판단합니다.
def _name_role(field: FormField) -> str | None:
    """Classify a name field from its own label, using the placeholder only to refine it."""
    label = _bare(field.label)
    core = _SCRIPT_QUALIFIER_RE.sub("", label)
    if re.search(r"[가-힣]", core):
        # '(영문 이름) Gildong': a romanized example next to a Hangul label is not part of the label.
        core = re.sub(r"[a-z]+", "", core)
    role = _NAME_ROLES.get(core)
    if role is None:
        return None
    if _OTHER_SCRIPT_RE.search(label):
        return "other_script"
    hint = _NAME_ROLES.get(_bare(field.placeholder))
    if role in {"full", "name"} and hint in {"family", "given"}:
        role = hint
    return f"en_{role}" if _ENGLISH_RE.search(label) else role


# 칸 키('f0-12')에서 순서 번호를 꺼냅니다. 칸이 서로 붙어 있는지 판단할 때 사용.
def _position(key: str) -> tuple[str, int | None]:
    prefix, _, number = key.rpartition("-")
    return (prefix, int(number)) if number.isdigit() else (key, None)


# 두 칸이 같은 폼에서 가까이(limit 이내) 붙어 있는지.
def _adjacent(first: FormField, second: FormField, index_gap: int, limit: int) -> bool:
    (prefix_a, number_a), (prefix_b, number_b) = _position(first.key), _position(second.key)
    if number_a is None or number_b is None:
        return abs(index_gap) <= limit
    return prefix_a == prefix_b and abs(number_a - number_b) <= limit


# 휴대폰 3칸 입력의 첫 칸(010 선택지가 있거나 3자리 칸)인지.
def _phone_head(field: FormField) -> bool:
    if field.widget_kind == "select":
        return any(re.sub(r"\D", "", option.value or option.text) == "010" for option in field.options)
    return field.widget_kind == "text" and field.maxlength == 3


# 휴대폰 3칸 입력의 둘째·셋째 칸(4자리 칸)인지.
def _phone_tail(field: FormField, record: MappingRecord, head: FormField) -> bool:
    if field.widget_kind != "text" or field.maxlength != 4:
        return False
    if record.resume_path == "person.phone":
        return True
    label = _bare(field.label)
    return record.resume_path == "unmapped" and (label in {"", _bare(head.label)} or re.fullmatch(r"[-~\d]*", label) is not None)


# 한국 사이트용 규칙 매퍼(Before 버전의 노트북 규칙을 옮겨 와 개선). LLM 없이 항상 같은 결과.
class KoreanMvpMapper:
    """Deterministic mapper ported from G001_장연_AIAgent.ipynb fallback rules."""

    # 흐름: 칸별 1차 연결 → 이름 구조 보정 → 휴대폰 3칸 보정 → 타인 정보 차단 → 신뢰도로 상태 결정.
    # 마지막으로 같은 이력서 값이 여러 칸에 자동 연결되면 뒤의 것은 사람 확인으로 돌립니다.
    async def map_fields(self, fields: list[FormField]) -> MappingResult:
        records = [self._map_one(field) for field in fields]
        records = self._apply_name_structure(fields, records)
        records = self._apply_phone_segments(fields, records)
        records = self._apply_third_party_guard(fields, records)
        records = [_route(record, field) for record, field in zip(records, fields)]
        whole: set[str] = set()
        parts: set[tuple[str, str]] = set()
        result: list[MappingRecord] = []
        for record in records:
            path, part = record.resume_path, record.part
            # A value used whole conflicts with any segmented use of the same value and vice versa.
            duplicate = path in whole or (part is None and any(item[0] == path for item in parts)) or (path, part) in parts
            if record.status == "auto_accepted" and duplicate:
                record = record.model_copy(
                    update={"status": "needs_human_review", "reason": "동일한 이력서 항목에 중복 매핑됨"}
                )
            elif record.status == "auto_accepted":
                if part is None:
                    whole.add(path)
                else:
                    parts.add((path, part))
            result.append(record)
        return MappingResult(mappings=result)

    # 비상연락처·보호자 영역의 개인정보 칸은 연결을 해제합니다(내 정보가 남의 칸에 들어가지 않게).
    def _apply_third_party_guard(self, fields: list[FormField], records: list[MappingRecord]) -> list[MappingRecord]:
        return [
            self._unmapped(field, "structure: 지원자 본인이 아닌 사람(비상연락처·보호자·추천인 등)의 정보")
            if record.resume_path.startswith("person.")
            and not _from_attribute(record)
            and _THIRD_PARTY_RE.search(f"{field.section} {field.label}")
            else record
            for field, record in zip(fields, records)
        ]

    # 이름 칸 보정: '성' 칸 옆의 '이름'은 이름(given), 혼자 있는 '이름'은 전체 이름.
    # 영문 라벨은 영문 이름을 쓰고, 한자 이름은 이력서에 없으므로 입력하지 않습니다.
    def _apply_name_structure(self, fields: list[FormField], records: list[MappingRecord]) -> list[MappingRecord]:
        """'성' + '이름' next to each other are family/given; a lone '이름' is the full name."""
        roles = [_name_role(field) for field in fields]
        updated: list[MappingRecord] = []
        for index, (field, record, role) in enumerate(zip(fields, records, roles)):
            blocked = record.resume_path == "unmapped" and record.reason != "fallback: no rule matched"
            if role is None or blocked or _from_attribute(record):
                updated.append(record)
                continue
            if role == "other_script":
                updated.append(self._unmapped(field, "structure: 한자 이름은 이력서에 없는 표기라 입력하지 않음"))
                continue
            # English-qualified labels ('영문 성', 'English name') use the romanized name.
            english = role.startswith("en_")
            base, prefix = ("person.name_en", "영문 ") if english else ("person.name", "")
            role = role.removeprefix("en_")
            families = [other for other, item in enumerate(roles) if item == ("en_family" if english else "family")]
            if role == "family":
                updated.append(self._mapped(field, f"{base}.family", 0.95, f"structure: {prefix}성 필드"))
            elif role == "given":
                updated.append(self._mapped(field, f"{base}.given", 0.95, f"structure: {prefix}이름(given) 필드"))
            elif role == "name" and any(_adjacent(fields[other], field, other - index, 2) for other in families):
                updated.append(self._mapped(field, f"{base}.given", 0.95, f"structure: 인접한 {prefix}성 필드와 짝을 이루는 이름"))
            else:
                updated.append(self._mapped(field, f"{base}.full", 0.9, f"structure: 단일 {prefix}이름 필드는 전체 이름"))
        return updated

    # 010 / 1234 / 5678처럼 3칸으로 나뉜 휴대폰 입력을 찾아 각 칸에 1/3, 2/3, 3/3 부분을 지정합니다.
    def _apply_phone_segments(self, fields: list[FormField], records: list[MappingRecord]) -> list[MappingRecord]:
        """Recognise the common Korean 010 / 1234 / 5678 three-box phone input."""
        updated = list(records)
        index = 0
        while index + 2 < len(fields):
            head, *tail = fields[index : index + 3]
            if (
                records[index].resume_path == "person.phone"
                and _phone_head(head)
                and all(_adjacent(head, item, offset + 1, 2) for offset, item in enumerate(tail))
                and all(_phone_tail(item, records[index + 1 + offset], head) for offset, item in enumerate(tail))
            ):
                for offset in range(3):
                    updated[index + offset] = self._mapped(
                        fields[index + offset],
                        "person.phone",
                        0.92,
                        f"structure: 휴대폰 번호 {offset + 1}/3 칸",
                    ).model_copy(update={"part": f"{offset + 1}/3"})
                index += 3
                continue
            index += 1
        return updated

    # 칸 하나 1차 연결. 순서: 지시문 의심 → 제외 / 체크박스 → 제외(동의는 사람이) / 최종 학력
    # → 속성(name/id) 판단 → 학력 라벨 규칙 → 개인정보 라벨 규칙 → 없으면 미연결.
    # 라벨이 너무 길면(질문 문장 등) 신뢰도를 낮춰 사람 확인으로 보냅니다.
    def _map_one(self, field: FormField) -> MappingRecord:
        raw = " ".join(
            value
            for value in [field.label, field.placeholder or "", field.name or "", field.element_id or ""]
            if value
        )
        if INSTRUCTION_RE.search(raw):
            return self._unmapped(field, "웹페이지의 지시문처럼 보이는 문구는 신뢰하지 않음")
        if field.widget_kind == "checkbox":
            return self._unmapped(field, "fallback: checkbox (consent/decision) is never mapped to resume data")
        if _HIGHEST_RE.search(raw):
            return self._mapped(field, "education.highest_level", 0.9, "fallback: 최종 학력 (이력서 학력에서 계산)")
        hint = infer_from_attributes(field.name, field.element_id)
        if hint is not None:
            # A structured name/id path outranks a visual label that may belong to a neighbouring column.
            if hint.resume_path == UNMAPPED:
                return self._unmapped(field, hint.reason)
            return self._mapped(field, hint.resume_path, 0.95, hint.reason)

        lowered = raw.casefold()
        context = f"{field.section} {field.label}"
        long_label = len(field.label.split()) > 8
        education = next((suffix for suffix, pattern in _SITE_EDU_RULES if re.search(pattern, lowered, re.I)), None)
        personal = next((path for path, pattern in _SITE_PERSONAL_RULES if re.search(pattern, lowered, re.I)), None)
        school_word_present = re.search(r"학교|school|university|college|대학", lowered, re.I)

        education_context = re.search(r"학력|학교|대학|전공|school|univ|college|educat|academic", f"{lowered} {field.section}", re.I)
        if education in _GENERIC_EDU_FIELDS and not education_context:
            # '지역' or 'campus' alone (e.g. 희망 근무 지역) is not an education item.
            education = None
        if education and not (personal and education == "school_name" and not school_word_present):
            level = (
                "graduate_school"
                if _GRAD_RE.search(context)
                else "university"
                if _UNIV_RE.search(context)
                else None
            )
            # The notebook defaults an unknown education level to university. Keep that
            # mapping, but let an explicit short education label enter the deterministic
            # baseline instead of making every sectionless real-site field non-actionable.
            confidence = 0.6 if long_label else 0.9
            return self._mapped(
                field,
                f"education.{level or 'university'}.{education}",
                confidence,
                f"fallback: education rule '{education}', level={'from section/label' if level else 'unknown → university (baseline correction)'}",
            )
        if personal:
            return self._mapped(
                field,
                personal,
                0.6 if long_label else 0.9,
                f"fallback: personal rule '{personal.rsplit('.', 1)[-1]}'"
                + (" (long label)" if long_label else ""),
            )
        return self._unmapped(field, "fallback: no rule matched")

    # 연결 결과 / 미연결 결과를 만드는 도우미.
    @staticmethod
    def _mapped(field: FormField, path: str, confidence: float, reason: str) -> MappingRecord:
        return MappingRecord(
            form_field=field.key,
            resume_path=path,
            confidence=confidence,
            reason=reason,
            source="notebook_fallback_rule",
        )

    @staticmethod
    def _unmapped(field: FormField, reason: str) -> MappingRecord:
        return MappingRecord(
            form_field=field.key,
            resume_path="unmapped",
            confidence=0,
            reason=reason,
            source="notebook_fallback_rule",
        )


# 한국 형식 검사: 우편번호 5자리, 휴대폰 010 형식이 아니면 자동 입력에서 빼고 경고합니다.
def enforce_korean_mvp_profile(profile: ResumeProfile) -> tuple[ResumeProfile, list[str]]:
    """Validate Korean contact formats while preserving notebook-supported education."""
    warnings: list[str] = []
    personal_data = profile.personal.model_dump()
    postal_code = profile.personal.postal_code
    if postal_code and not re.fullmatch(r"\d{5}", postal_code):
        personal_data["postal_code"] = ""
        warnings.append("postal_code: 한국 우편번호 5자리 형식이 아니어서 자동 입력에서 제외됨")
    phone = profile.personal.phone
    digits = re.sub(r"\D", "", phone)
    if phone and not (re.fullmatch(r"010\d{8}", digits) or re.fullmatch(r"8210\d{8}", digits)):
        personal_data["phone"] = ""
        warnings.append("phone: 한국 휴대폰 번호 형식이 아니어서 자동 입력에서 제외됨")
    personal = PersonalInfo.model_validate(personal_data)
    return profile.model_copy(update={"personal": personal}), warnings


# 이력서를 '경로: 값' 딕셔너리로 펼칩니다(입력·화면 표시용).
def korean_mvp_values(profile: ResumeProfile) -> dict[str, str]:
    name = profile.personal.name
    values = {
        "person.name.full": name.full,
        "person.name.family": name.family,
        "person.name.given": name.given,
        "person.name_en.full": profile.personal.name_en.full,
        "person.name_en.family": profile.personal.name_en.family,
        "person.name_en.given": profile.personal.name_en.given,
        "person.birth_date": profile.personal.birth_date,
        "person.phone": profile.personal.phone,
        "person.email": profile.personal.email,
        "person.email_secondary": profile.personal.email_secondary,
        "person.postal_code": profile.personal.postal_code,
        "person.address": profile.personal.address,
        "person.address_detail": profile.personal.address_detail,
        "person.military_status": profile.personal.military_status,
        "person.disability_status": profile.personal.disability_status,
        "person.veteran_status": profile.personal.veteran_status,
        "skills": profile.skills,
    }
    for level in EDUCATION_LEVELS:
        record = next((item for item in profile.education if item.education_level == level), None)
        for field in EDUCATION_FIELDS:
            values[f"education.{level}.{field}"] = getattr(record, field) if record else ""
    return values
