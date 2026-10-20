from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Frame, Page

from .mapping import FormField


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
  const textFromIds = (ids) => clean(String(ids || "").split(/\s+/).map((id) => {
    const element = document.getElementById(id);
    return element ? element.innerText || element.textContent || "" : "";
  }).join(" "));
  const labelFor = (element) => {
    const aria = clean(element.getAttribute("aria-label"));
    if (aria) return aria;
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
    const row = element.closest("fieldset, [role=group], tr, .row, .field, .form-group");
    if (row) {
      const heading = row.querySelector("legend, label, [data-label], .label, th");
      if (heading && heading !== element) return clean(heading.innerText || heading.textContent || "");
    }
    return clean(element.getAttribute("placeholder") || element.getAttribute("name") || element.id);
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
  const selectedValuePresent = (element, kind) => {
    if (kind === "radio_group") return Boolean(element.checked);
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
    if (tag === "input" && (role === "combobox" || element.hasAttribute("aria-autocomplete") || element.hasAttribute("list"))) return "custom_combobox";
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
    "input, textarea, select, [role=combobox], button[aria-haspopup=listbox]"
  )).filter(visible);

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
    if (type === "hidden" || type === "password" || ["submit", "reset", "button", "image"].includes(type)) continue;
    if (tag === "button" && !(element.getAttribute("role") === "combobox" || element.getAttribute("aria-haspopup") === "listbox")) continue;
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
      placeholder: clean(element.getAttribute("placeholder")) || null,
      section: sectionFor(element),
      group_labels: groupLabelsFor(element),
      name: clean(element.getAttribute("name")) || null,
      element_id: clean(element.id) || null,
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
    const label = clean((legend && (legend.innerText || legend.textContent)) || labelFor(first));
    if (isSensitive(first, label)) continue;
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
      options: radios.slice(0, 100).map((radio) => ({ value: clean(radio.value), text: labelFor(radio) })),
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


@dataclass(slots=True)
class FieldTarget:
    frame: Frame
    key: str
    widget_kind: str
    part_keys: dict[str, str] = field(default_factory=dict)

    @property
    def selector(self) -> str:
        return f'[data-apply-local-field="{self.key}"]'

    def part_selector(self, part: str) -> str:
        return f'[data-apply-local-field="{self.part_keys[part]}"]'


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


@dataclass(slots=True)
class PageInspection:
    fields: list[FormField]
    targets: dict[str, FieldTarget]
    controls: list[ControlTarget]
    login_controls_detected: bool
    unapproved_frame_hosts: set[str] = field(default_factory=set)


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
    return PageInspection(
        fields=fields,
        targets=targets,
        controls=controls,
        login_controls_detected=login_controls,
        unapproved_frame_hosts=unapproved_frame_hosts,
    )
