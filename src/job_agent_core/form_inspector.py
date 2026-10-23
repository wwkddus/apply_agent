# [역할] 화면 분석(칸 찾기). 브라우저 안에서 스크립트를 실행해 입력 칸·버튼 목록을 읽어 FormField로 만듭니다.
# 값은 읽지 않고 '값이 있는지'만 확인하며, 각 칸에 표시용 속성(data-apply-local-field)을 붙여 이후 입력 단계가 같은 칸을 정확히 찾게 합니다.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Frame, Page

from .address_search import is_postcode_host
from .mapping import FormField


# 브라우저(각 frame)에서 실행되는 분석 스크립트. 하는 일:
# ① 보이는 입력 칸 수집(숨겨진 라디오는 감싼 label이 보이면 포함)
# ② 라벨 찾기: aria → <label> → 행 제목 → 주변 제목 → 질문 문장 순서
# ③ 칸 종류 판단: 텍스트/선택/라디오/날짜/검색 콤보/커스텀 달력 등
# ④ 연·월·일 선택 3개는 날짜 칸 하나로, 같은 name의 라디오는 한 그룹으로 묶기
# ⑤ 비밀번호·인증번호·주민번호 같은 민감 칸은 자동 입력 대상에서 제외
# ⑥ 버튼 목록과 로그인 칸 존재 여부를 함께 반환.
INSPECT_SCRIPT = r"""
({ prefix, limit }) => {
  const FIELD_ATTR = "data-apply-local-field";
  const NEXT_ATTR = "data-apply-local-next";
  const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const compact = (value) => clean(value).replace(/[\s_\-/]+/g, "").toLowerCase();
  const visible = (element) => {
    if (!(element instanceof Element)) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
  };
  // Custom-styled radios/checkboxes are often 0x0; the visible label is what the user clicks.
  const shown = (element) => visible(element) || (
    element instanceof HTMLInputElement && ["radio", "checkbox"].includes(element.type)
    && [...Array.from(element.labels || []), element.closest("label")].some((label) => label && visible(label))
  );
  const ownText = (node) => clean(Array.from(node.childNodes)
    .filter((child) => child.nodeType === Node.TEXT_NODE).map((child) => child.textContent).join(" "));
  const textFromIds = (ids) => clean(String(ids || "").split(/\s+/).map((id) => {
    const element = document.getElementById(id);
    return element ? element.innerText || element.textContent || "" : "";
  }).join(" "));
  // Icon-button captions such as "Open calendar" describe the control, not the field.
  const genericAria = /^(open calendar|toggle suggestions|clear value|날짜 선택|select|선택|open|열기)$/i;
  // Separate form fields; native radios sharing a name count as one field.
  const FIELD_SELECTOR = "input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]), textarea, select, "
    + "[role=combobox], [role=radiogroup], button[aria-haspopup=listbox], button[aria-haspopup=grid], button[aria-haspopup=dialog]";
  const otherFieldCount = (container, element) => {
    const units = new Set();
    for (const item of container.querySelectorAll(FIELD_SELECTOR)) {
      if (item === element || element.contains(item) || item.contains(element) || !shown(item)) continue;
      units.add(item.type === "radio" && item.name ? `radio:${item.name}` : item);
    }
    return units.size;
  };
  // Pages sometimes point aria-labelledby at a missing id; a person still reads the label just above.
  // Use it only when the nearest container holding a label wraps this one field and nothing else.
  const nearbyLabel = (element) => {
    let node = element.parentElement;
    for (let depth = 0; node && depth < 5; depth += 1, node = node.parentElement) {
      // Sites often style the caption as <div class="form-label"> / <div class="select-label">.
      const heading = Array.from(node.querySelectorAll("label, legend, [class*='label']"))
        .find((item) => !item.contains(element) && visible(item) && clean(item.innerText || item.textContent || ""));
      if (!heading) continue;
      return otherFieldCount(node, element) ? "" : clean(heading.innerText || heading.textContent || "");
    }
    return "";
  };
  // A question written as the text of a wrapping block (e.g. <div class="form-label">질문<input></div>).
  const ancestorText = (element) => {
    let node = element.parentElement;
    for (let depth = 0; node && depth < 4; depth += 1, node = node.parentElement) {
      if (otherFieldCount(node, element)) return "";
      const text = ownText(node);
      if (text) return text.slice(0, 200);
    }
    return "";
  };
  // The heading of a radio group: the first text block next to the options that holds no radio.
  const radioGroupHeading = (radios) => {
    let node = radios[0].parentElement;
    while (node && !radios.every((radio) => node.contains(radio))) node = node.parentElement;
    for (let depth = 0; node && depth < 3; depth += 1, node = node.parentElement) {
      const heading = Array.from(node.children).find((child) => !child.querySelector("input, select, textarea")
        && visible(child) && clean(child.innerText || child.textContent || "").length > 0);
      if (heading) return clean(heading.innerText || heading.textContent || "").slice(0, 120);
      if (node.querySelectorAll("input[type=radio]").length > radios.length) break;
    }
    return "";
  };
  // Korean forms often lay out '<dt>성명</dt><dd><input></dd>' or '<th>성명</th><td>..</td><th>생년월일</th><td>..</td>'.
  // The caption is the dt/th right before the cell holding the field, not the first one in the list or row.
  const cellCaption = (element) => {
    const cell = element.closest("dd, td");
    let node = cell ? cell.previousElementSibling : null;
    while (node && !["DT", "TH"].includes(node.tagName)) node = node.previousElementSibling;
    return node ? clean(node.innerText || node.textContent || "") : "";
  };
  const labelFor = (element) => {
    const aria = clean(element.getAttribute("aria-label"));
    if (aria && !genericAria.test(aria)) return aria;
    const labelled = textFromIds(element.getAttribute("aria-labelledby"));
    if (labelled) return labelled;
    if (element.labels && element.labels.length) {
      const value = clean(Array.from(element.labels).map((item) => item.innerText || item.textContent || "").join(" "));
      if (value) return value;
    }
    if (element.id) {
      const explicit = document.querySelector(`label[for="${CSS.escape(element.id)}"]`);
      if (explicit) return clean(explicit.innerText || explicit.textContent || "");
    }
    const wrapping = element.closest("label");
    if (wrapping) return clean(wrapping.innerText || wrapping.textContent || "");
    const caption = cellCaption(element);
    if (caption) return caption;
    const row = element.closest("fieldset, [role=group], tr, .row, .field, .form-group");
    if (row) {
      const heading = row.querySelector("legend, label, [data-label], .label, th");
      if (heading && heading !== element) return clean(heading.innerText || heading.textContent || "");
    }
    const nearby = nearbyLabel(element);
    if (nearby) return nearby;
    const question = ancestorText(element);
    if (question) return question;
    return clean(element.getAttribute("title") || element.getAttribute("placeholder") || element.getAttribute("name") || element.id);
  };
  // A field without a name (e.g. a button group) reports the data-model names of the other fields in the
  // nearest block that has any, so '입학구분' in a graduate-school block is not read as the university one.
  // The mapper uses them only when they all point at the same education level.
  const contextNames = (element) => {
    if (clean(element.getAttribute("name"))) return [];
    let node = element.parentElement;
    for (let depth = 0; node && depth < 4; depth += 1, node = node.parentElement) {
      const names = Array.from(node.querySelectorAll("[name]"))
        .filter((item) => item !== element && !element.contains(item))
        .map((item) => clean(item.getAttribute("name")))
        .filter((name) => /[._\[]|[a-z][A-Z]/.test(name));
      if (names.length) return Array.from(new Set(names)).slice(0, 12);
    }
    return [];
  };
  const sectionFor = (element) => {
    const fieldset = element.closest("fieldset");
    if (fieldset) {
      const legend = fieldset.querySelector(":scope > legend");
      if (legend) return clean(legend.innerText || legend.textContent || "");
    }
    const section = element.closest("section, [role=region]");
    if (section) {
      const heading = section.querySelector("h1, h2, h3, [role=heading]");
      if (heading) return clean(heading.innerText || heading.textContent || "");
    }
    // Otherwise the nearest heading written before the field's block ('<h4>고등학교</h4><dl>..</dl>').
    let node = element;
    for (let depth = 0; node && node !== document.body && depth < 8; depth += 1, node = node.parentElement) {
      for (let sibling = node.previousElementSibling; sibling; sibling = sibling.previousElementSibling) {
        if (/^H[1-6]$/.test(sibling.tagName) || sibling.getAttribute("role") === "heading") {
          return clean(sibling.innerText || sibling.textContent || "").slice(0, 80);
        }
      }
    }
    return "";
  };
  const groupLabelsFor = (element) => {
    const group = element.closest("fieldset, [role=group], tr, .row, .form-row") || element.form;
    if (!group) return [];
    const values = Array.from(group.querySelectorAll("label, legend, [aria-label]"))
      .filter(visible)
      .map((item) => clean(item.getAttribute("aria-label") || item.innerText || item.textContent || ""))
      .filter(Boolean);
    return Array.from(new Set(values)).slice(0, 12);
  };
  const isSensitive = (element, label) => {
    const haystack = compact([
      label,
      element.getAttribute("name"),
      element.id,
      element.getAttribute("placeholder"),
      element.getAttribute("autocomplete"),
    ].join(" "));
    return /비밀번호|패스워드|captcha|보안문자|자동입력방지|otp|일회용|인증번호|verificationcode|주민등록|외국인등록|여권번호|계좌번호|카드번호|current-password|new-password|one-time-code/.test(haystack);
  };
  const optionData = (select) => Array.from(select.options || []).slice(0, 100).map((option) => ({
    value: clean(option.value),
    text: clean(option.textContent),
  }));
  const SELECT_PROMPT = /^[-=\s]*([^-=]{0,20}\s)?(선택(하세요|해\s*주세요|하십시오)?|미선택|choose|select( one)?|please select)[-=\s.]*$/i;
  const selectedValuePresent = (element, kind) => {
    if (kind === "radio_group") return Boolean(element.checked);
    if (element.hasAttribute("data-placeholder-shown") || element.getAttribute("data-placeholder") === "true") return false;
    // A calendar trigger shows its date as text; a label such as "날짜 선택" has no digits.
    if (kind === "custom_date_picker" && element.tagName === "BUTTON") return /\d/.test(element.innerText || element.textContent || "");
    if (kind === "select") {
      const option = element.selectedIndex >= 0 ? element.options[element.selectedIndex] : null;
      if (!option || !clean(option.value)) return false;
      // '<option>선택</option>' without a value still reads as a prompt, not an answer.
      return !(element.selectedIndex === 0 && SELECT_PROMPT.test(clean(option.textContent)));
    }
    if (kind === "custom_select") {
      const text = clean(element.innerText || element.textContent || element.getAttribute("data-value"));
      return Boolean(text && !/^(선택|선택하세요|미선택|choose|select)$/i.test(text));
    }
    return Boolean(clean(element.value || element.getAttribute("data-value")));
  };
  const kindFor = (element) => {
    const tag = element.tagName.toLowerCase();
    const type = clean(element.getAttribute("type")).toLowerCase();
    const role = clean(element.getAttribute("role")).toLowerCase();
    if (tag === "textarea") return "textarea";
    if (tag === "select") return "select";
    if (type === "date") return "native_date";
    if (type === "month") return "native_month";
    if (type === "radio") return "radio_group";
    if (type === "checkbox") return "checkbox";
    if (type === "file") return "file";
    if (role === "combobox" && tag !== "input") return "custom_select";
    if (tag === "button" && element.getAttribute("aria-haspopup") === "listbox") return "custom_select";
    if (tag === "button" && ["grid", "dialog"].includes(element.getAttribute("aria-haspopup"))) return "custom_date_picker";
    if (tag === "input" && (role === "combobox" || element.hasAttribute("aria-autocomplete") || element.hasAttribute("list"))) return "custom_combobox";
    // jQuery UI autocomplete (e.g. SK school/major search) adds no ARIA role, only this class.
    if (tag === "input" && element.classList.contains("ui-autocomplete-input")) return "custom_combobox";
    if (tag === "input" && element.readOnly && element.getAttribute("aria-haspopup") === "dialog") return "custom_date_picker";
    if (tag === "input") return "text";
    return "unsupported";
  };

  document.querySelectorAll(`[${FIELD_ATTR}], [${NEXT_ATTR}]`).forEach((element) => {
    element.removeAttribute(FIELD_ATTR);
    element.removeAttribute(NEXT_ATTR);
  });

  const fields = [];
  const used = new Set();
  let sequence = 0;
  const standard = Array.from(document.querySelectorAll(
    "input, textarea, select, [role=combobox], button[aria-haspopup=listbox], button[aria-haspopup=grid], button[aria-haspopup=dialog][name]"
  )).filter(shown);

  // Treat a clearly labelled year/month/day select group as one date field.
  for (const select of standard.filter((element) => element.tagName === "SELECT")) {
    if (used.has(select)) continue;
    const container = select.closest("fieldset, [role=group], .row, .form-row, tr") || select.parentElement;
    if (!container) continue;
    const selects = Array.from(container.querySelectorAll("select")).filter(visible);
    if (selects.length < 2 || selects.length > 3) continue;
    const parts = {};
    for (const item of selects) {
      const identity = compact([labelFor(item), item.name, item.id].join(" "));
      const part = /(?:년|year)/i.test(identity) ? "year" : /(?:월|month)/i.test(identity) ? "month" : /(?:일|day)/i.test(identity) ? "day" : null;
      if (!part || parts[part]) continue;
      parts[part] = item;
    }
    if (!parts.year || !parts.month || Object.keys(parts).length !== selects.length) continue;
    const ownText = Array.from(container.children)
      .filter((child) => child.tagName !== "SELECT")
      .map((child) => clean(child.getAttribute("aria-label") || child.innerText || child.textContent || ""))
      .find((value) => /생년월일|출생일/.test(compact(value))) || "";
    const label = ownText || clean(container.getAttribute("aria-label"));
    if (!/생년월일|출생일/.test(compact(label))) continue;
    const key = `${prefix}-${sequence++}`;
    const partKeys = {};
    const partOptions = {};
    for (const [part, item] of Object.entries(parts)) {
      const partKey = `${key}-${part}`;
      item.setAttribute(FIELD_ATTR, partKey);
      partKeys[part] = partKey;
      partOptions[part] = optionData(item);
      used.add(item);
    }
    fields.push({
      key,
      label,
      type: "select",
      widget_kind: "split_date_select",
      required: selects.some((item) => item.required || item.getAttribute("aria-required") === "true"),
      placeholder: null,
      section: sectionFor(select),
      group_labels: groupLabelsFor(select),
      name: null,
      element_id: null,
      current_value_present: selects.every((item) => Boolean(clean(item.value))),
      readonly: false,
      options: [],
      pattern: null,
      maxlength: null,
      date_format: null,
      parts: partKeys,
      part_options: partOptions,
    });
  }

  const radioGroups = new Map();
  for (const element of standard) {
    if (fields.length >= limit || used.has(element)) continue;
    const tag = element.tagName.toLowerCase();
    const type = clean(element.getAttribute("type")).toLowerCase();
    if (type === "hidden" || type === "password" || (tag === "input" && ["submit", "reset", "button", "image"].includes(type))) continue;
    if (tag === "button" && !(element.getAttribute("role") === "combobox" || ["listbox", "grid", "dialog"].includes(element.getAttribute("aria-haspopup")))) continue;
    const label = labelFor(element);
    if (isSensitive(element, label)) continue;
    const kind = kindFor(element);
    if (kind === "radio_group") {
      const name = clean(element.getAttribute("name"));
      if (!name) continue;
      if (!radioGroups.has(name)) radioGroups.set(name, []);
      radioGroups.get(name).push(element);
      continue;
    }
    const key = `${prefix}-${sequence++}`;
    element.setAttribute(FIELD_ATTR, key);
    fields.push({
      key,
      label,
      type: type || tag,
      widget_kind: kind,
      required: Boolean(element.required || element.getAttribute("aria-required") === "true"),
      placeholder: clean(element.getAttribute("placeholder"))
        || (tag === "button" && element.getAttribute("data-placeholder") === "true" ? clean(element.innerText) : "")
        || null,
      section: sectionFor(element),
      group_labels: groupLabelsFor(element),
      name: clean(element.getAttribute("name")) || null,
      element_id: clean(element.id) || null,
      context_names: contextNames(element),
      current_value_present: selectedValuePresent(element, kind),
      readonly: Boolean(element.readOnly || element.getAttribute("aria-readonly") === "true"),
      options: kind === "select" ? optionData(element) : [],
      pattern: clean(element.getAttribute("pattern")) || null,
      maxlength: element.maxLength > 0 ? element.maxLength : null,
      date_format: clean(element.getAttribute("data-date-format")) || null,
      parts: {},
      part_options: {},
    });
  }

  for (const [name, radios] of radioGroups.entries()) {
    if (fields.length >= limit) break;
    const first = radios[0];
    const key = `${prefix}-${sequence++}`;
    radios.forEach((radio) => radio.setAttribute(FIELD_ATTR, key));
    const fieldset = first.closest("fieldset, [role=radiogroup]");
    const legend = fieldset ? fieldset.querySelector("legend, [role=heading]") : null;
    const label = clean((legend && (legend.innerText || legend.textContent)) || cellCaption(first) || radioGroupHeading(radios) || labelFor(first));
    if (isSensitive(first, label)) continue;
    // Radios without distinct values (all "on") are chosen by position.
    const distinct = new Set(radios.map((radio) => radio.value)).size === radios.length;
    fields.push({
      key,
      label,
      type: "radio",
      widget_kind: "radio_group",
      required: radios.some((radio) => radio.required || radio.getAttribute("aria-required") === "true"),
      placeholder: null,
      section: sectionFor(first),
      group_labels: groupLabelsFor(first),
      name,
      element_id: null,
      current_value_present: radios.some((radio) => radio.checked),
      readonly: false,
      options: radios.slice(0, 100).map((radio, index) => ({ value: distinct ? clean(radio.value) : `#${index}`, text: labelFor(radio) })),
      pattern: null,
      maxlength: null,
      date_format: null,
      parts: {},
      part_options: {},
    });
  }

  // ARIA button groups (role=radiogroup > role=radio) that are not backed by native radios.
  for (const group of Array.from(document.querySelectorAll("[role=radiogroup]")).filter(visible)) {
    if (fields.length >= limit) break;
    if (group.querySelector("input[type=radio]")) continue;
    const radios = Array.from(group.querySelectorAll("[role=radio]")).filter(visible);
    if (radios.length < 2) continue;
    const label = labelFor(group);
    if (isSensitive(group, label)) continue;
    const key = `${prefix}-${sequence++}`;
    group.setAttribute(FIELD_ATTR, key);
    fields.push({
      key,
      label,
      type: "radio",
      widget_kind: "aria_radio_group",
      required: group.getAttribute("aria-required") === "true",
      placeholder: null,
      section: sectionFor(group),
      group_labels: groupLabelsFor(group),
      name: clean(group.getAttribute("name") || group.getAttribute("data-name")) || null,
      element_id: clean(group.id) || null,
      context_names: group.getAttribute("name") || group.getAttribute("data-name") ? [] : contextNames(group),
      current_value_present: radios.some((radio) => radio.getAttribute("aria-checked") === "true"),
      readonly: group.getAttribute("aria-readonly") === "true" || group.getAttribute("aria-disabled") === "true",
      options: radios.slice(0, 50).map((radio) => ({ value: clean(radio.innerText || radio.getAttribute("aria-label")), text: clean(radio.innerText || radio.getAttribute("aria-label")) })),
      pattern: null,
      maxlength: null,
      date_format: null,
      parts: {},
      part_options: {},
    });
  }

  const controls = [];
  for (const element of Array.from(document.querySelectorAll("button, input[type=submit], input[type=button], a[role=button]"))) {
    if (!visible(element)) continue;
    const text = clean(element.innerText || element.value || element.getAttribute("aria-label") || element.textContent || "");
    const attrType = clean(element.getAttribute("type") || element.type).toLowerCase();
    const formType = element.form ? clean(element.form.getAttribute("method")).toLowerCase() : "";
    const tag = element.tagName.toLowerCase();
    const key = `${prefix}-control-${controls.length}`;
    element.setAttribute(NEXT_ATTR, key);
    controls.push({ key, tag, attr_type: attrType, form_type: formType, text: text.slice(0, 120) });
  }

  const loginControlsDetected = standard.some((element) => {
    if (!visible(element)) return false;
    const type = clean(element.getAttribute("type")).toLowerCase();
    const autocomplete = clean(element.getAttribute("autocomplete")).toLowerCase();
    return type === "password" || ["username", "current-password", "new-password", "one-time-code"].includes(autocomplete);
  });

  return { fields, controls, login_controls_detected: loginControlsDetected };
}
"""


# 칸 하나를 다시 찾기 위한 정보: 어느 frame에 있는지 + 붙여 둔 표시 속성 키.
@dataclass(slots=True)
class FieldTarget:
    frame: Frame
    key: str
    widget_kind: str
    part_keys: dict[str, str] = field(default_factory=dict)

    # 입력 단계에서 이 칸을 찾는 CSS 선택자.
    @property
    def selector(self) -> str:
        return f'[data-apply-local-field="{self.key}"]'

    # 연/월/일처럼 여러 부분으로 된 칸의 각 부분 선택자.
    def part_selector(self, part: str) -> str:
        return f'[data-apply-local-field="{self.part_keys[part]}"]'


# 버튼 하나(문구·type 포함). 제출 버튼인지 판단해 누르지 않기 위해 수집합니다.
@dataclass(slots=True)
class ControlTarget:
    frame: Frame
    key: str
    tag: str
    attr_type: str
    form_type: str
    text: str

    @property
    def selector(self) -> str:
        return f'[data-apply-local-next="{self.key}"]'


# 화면 분석 결과 묶음: 칸 목록, 칸 위치, 버튼, 로그인 칸 여부, 승인 안 된 iframe 도메인.
@dataclass(slots=True)
class PageInspection:
    fields: list[FormField]
    targets: dict[str, FieldTarget]
    controls: list[ControlTarget]
    login_controls_detected: bool
    unapproved_frame_hosts: set[str] = field(default_factory=set)


# 칸마다 고정 이름(fingerprint)을 붙입니다. 페이지에 칸이 추가되면 순서 번호가 밀리기 때문에,
# name 속성이 유일하면 그것을, 아니면 '영역 + 라벨 + 몇 번째'를 사용합니다.
def assign_fingerprints(fields: list[FormField]) -> None:
    """Name the field by its data-model name when unique, else by section + label + occurrence."""
    # 칸 키에서 frame 번호를 꺼냅니다.
    def frame_of(field: FormField) -> str:
        return field.key.split("-", 2)[1] if field.key.count("-") >= 2 else ""

    names: dict[tuple[str, str], int] = {}
    for field in fields:
        if field.name:
            names[(frame_of(field), field.name)] = names.get((frame_of(field), field.name), 0) + 1
    seen: dict[tuple[str, str, str], int] = {}
    for field in fields:
        frame = frame_of(field)
        if field.name and names[(frame, field.name)] == 1:
            field.fingerprint = f"{frame}|name:{field.name}"
            continue
        label_key = (frame, field.section, field.label)
        occurrence = seen.get(label_key, 0)
        seen[label_key] = occurrence + 1
        field.fingerprint = f"{frame}|label:{field.section}|{field.label}|{occurrence}"


# 페이지 전체 분석. frame(iframe 포함)마다 스크립트를 실행하되,
# 주소 검색 창은 건너뛰고, 승인되지 않은 도메인의 iframe은 읽지 않고 승인 후보로만 기록합니다.
async def inspect_page(
    page: Page,
    *,
    approved_hosts: set[str],
    host_allowed: Any,
    max_fields: int = 300,
) -> PageInspection:
    fields: list[FormField] = []
    targets: dict[str, FieldTarget] = {}
    controls: list[ControlTarget] = []
    login_controls = False
    unapproved_frame_hosts: set[str] = set()
    for frame_index, frame in enumerate(page.frames):
        if frame.is_detached():
            continue
        frame_host = ""
        if frame.url not in {"", "about:blank"}:
            from urllib.parse import urlsplit

            frame_host = (urlsplit(frame.url).hostname or "").casefold()
            # The postcode search window is driven by the address adapter, not read as form fields.
            if is_postcode_host(frame_host):
                continue
            if frame_host and not any(host_allowed(frame_host, approved) for approved in approved_hosts):
                unapproved_frame_hosts.add(frame_host)
                continue
        try:
            raw = await frame.evaluate(
                INSPECT_SCRIPT,
                {"prefix": f"al-f{frame_index}", "limit": max_fields - len(fields)},
            )
        except Exception:
            continue
        login_controls = login_controls or bool(raw.get("login_controls_detected"))
        for item in raw.get("fields", []):
            try:
                parsed = FormField.model_validate(item)
            except Exception:
                continue
            if parsed.key in targets:
                continue
            fields.append(parsed)
            targets[parsed.key] = FieldTarget(
                frame=frame,
                key=parsed.key,
                widget_kind=parsed.widget_kind,
                part_keys=dict(parsed.parts),
            )
            if len(fields) >= max_fields:
                break
        for item in raw.get("controls", []):
            controls.append(
                ControlTarget(
                    frame=frame,
                    key=str(item.get("key", "")),
                    tag=str(item.get("tag", "")),
                    attr_type=str(item.get("attr_type", "")),
                    form_type=str(item.get("form_type", "")),
                    text=str(item.get("text", "")),
                )
            )
        if len(fields) >= max_fields:
            break
    assign_fingerprints(fields)
    return PageInspection(
        fields=fields,
        targets=targets,
        controls=controls,
        login_controls_detected=login_controls,
        unapproved_frame_hosts=unapproved_frame_hosts,
    )
