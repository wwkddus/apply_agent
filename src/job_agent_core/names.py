from __future__ import annotations

import re

from .models import PersonName, ResumeProfile

# Common Korean compound family names. Recognising one is useful, but still
# requires confirmation because some three-character names remain ambiguous.
KOREAN_COMPOUND_FAMILY_NAMES = {
    "남궁",
    "독고",
    "동방",
    "사공",
    "서문",
    "선우",
    "제갈",
    "황보",
}


def _compact(value: str) -> str:
    return re.sub(r"[\s·・]", "", value or "")


def infer_name_parts(full_name: str) -> PersonName:
    """Infer components conservatively; uncertain splits are never confirmed."""
    full = " ".join((full_name or "").split())
    compact = _compact(full)
    if not compact:
        return PersonName()

    if re.fullmatch(r"[가-힣]{2,5}", compact):
        compound = next((surname for surname in KOREAN_COMPOUND_FAMILY_NAMES if compact.startswith(surname)), None)
        if compound and len(compact) > len(compound):
            return PersonName(
                full=compact,
                family=compound,
                given=compact[len(compound) :],
                order="family_given",
                split_confidence=0.78,
                split_method="korean_compound_family_rule",
                confirmed=False,
            )
        if len(compact) >= 2:
            return PersonName(
                full=compact,
                family=compact[0],
                given=compact[1:],
                order="family_given",
                split_confidence=0.92 if len(compact) == 3 else 0.82,
                split_method="korean_single_family_rule",
                confirmed=False,
            )

    if "," in full:
        family, given = [part.strip() for part in full.split(",", 1)]
        if family and given:
            return PersonName(
                full=full,
                family=family,
                given=given,
                order="family_given",
                split_confidence=0.9,
                split_method="comma_order_rule",
                confirmed=False,
            )

    parts = full.split()
    if len(parts) >= 2:
        return PersonName(
            full=full,
            family=parts[-1],
            given=parts[0],
            middle=" ".join(parts[1:-1]),
            order="given_family",
            split_confidence=0.55,
            split_method="latin_space_heuristic",
            confirmed=False,
        )
    return PersonName(full=full)


def confirm_name_parts(profile: ResumeProfile, *, family: str, given: str, middle: str = "") -> ResumeProfile:
    """Return a copy with user-confirmed name components."""
    name = PersonName.model_validate(
        {
            **profile.personal.name.model_dump(),
            "family": family.strip(),
            "given": given.strip(),
            "middle": middle.strip(),
            "split_confidence": 1.0,
            "split_method": "user_confirmed",
            "confirmed": True,
        }
    )
    personal = profile.personal.model_copy(update={"name": name})
    return profile.model_copy(update={"personal": personal})


def normalized_name(value: str) -> str:
    return re.sub(r"[\s\-'·・]", "", (value or "")).casefold()


def name_components_usable(name: PersonName) -> bool:
    """Family/given may be filled without asking only when the split is verifiable."""
    if not (name.family and name.given):
        return False
    if name.order == "given_family":
        combined = f"{name.given}{name.middle}{name.family}"
    else:
        combined = f"{name.family}{name.middle}{name.given}"
    if normalized_name(combined) != normalized_name(name.full):
        return False
    if name.confirmed:
        return True
    # A romanized name whose family token matches the Hangul surname has a known order.
    if name.split_method in {"native_surname_match", "comma_order_rule"}:
        return True
    # A 2-3 syllable Korean name without a known compound surname is split 1 + rest.
    # Compound surnames, longer names and unmatched Latin names stay with the user.
    return name.split_method == "korean_single_family_rule" and len(_compact(name.full)) in {2, 3}


# Common romanizations of Korean family names, including Chinese pinyin/Cantonese forms for
# Chinese names written in Hangul (e.g. 장 -> Zhang/Cheung). Used only to decide which Latin
# token is the family name; anything not covered falls back to Revised Romanization.
SURNAME_ROMANIZATIONS: dict[str, set[str]] = {
    "김": {"kim", "gim", "jin", "kam"},
    "이": {"lee", "yi", "rhee", "i", "rhie", "li", "ri", "lei"},
    "박": {"park", "pak", "bak", "bark", "pai"},
    "최": {"choi", "choe", "chey", "cui", "tsui"},
    "정": {"jung", "jeong", "chung", "joung", "cheong", "zheng", "cheng", "ding"},
    "강": {"kang", "gang", "jiang", "kong"},
    "조": {"cho", "jo", "joe", "zhao", "chiu", "chao"},
    "윤": {"yoon", "yun", "youn", "yin", "wan"},
    "장": {"jang", "chang", "zhang", "cheung", "jiang", "zang"},
    "임": {"lim", "im", "rim", "yim", "lin", "ren"},
    "한": {"han", "hahn"},
    "오": {"oh", "o", "wu", "ng", "woo"},
    "서": {"seo", "suh", "so", "seoh", "xu", "shu", "xue"},
    "신": {"shin", "sin", "shen", "xin"},
    "권": {"kwon", "gwon", "kweon", "quan"},
    "황": {"hwang", "whang", "huang", "wong"},
    "안": {"ahn", "an", "ann"},
    "송": {"song", "soong", "sung"},
    "전": {"jeon", "jun", "chun", "chon", "quan", "tian", "qian"},
    "홍": {"hong", "hung"},
    "유": {"yoo", "yu", "ryu", "you", "liu", "lau", "yau"},
    "류": {"ryu", "yoo", "yu", "liu", "rew"},
    "고": {"ko", "go", "koh", "gao", "goh"},
    "문": {"moon", "mun", "wen", "man"},
    "양": {"yang", "ryang", "yeung"},
    "손": {"son", "sohn", "sun", "suen"},
    "배": {"bae", "bai", "pae", "pei"},
    "백": {"baek", "paek", "paik", "back", "bai", "pak"},
    "허": {"heo", "hur", "huh", "hu", "xu", "hui"},
    "남": {"nam", "nan"},
    "노": {"noh", "no", "roh", "lu", "lo", "nu"},
    "하": {"ha", "hah", "he", "xia"},
    "곽": {"kwak", "gwak", "guo", "kwok"},
    "성": {"sung", "seong", "sheng", "cheng"},
    "차": {"cha", "che"},
    "주": {"joo", "ju", "chu", "zhou", "chow", "zhu"},
    "우": {"woo", "wu", "u"},
    "구": {"koo", "gu", "ku", "goo"},
    "민": {"min"},
    "진": {"jin", "chin", "chen", "chan"},
    "나": {"na", "ra"},
    "심": {"shim", "sim", "shen"},
    "원": {"won", "yuan", "woan"},
    "엄": {"um", "eom", "yan", "yim"},
    "채": {"chae", "chai", "cai"},
    "천": {"cheon", "chun", "qian", "chin"},
    "방": {"bang", "pang", "fang"},
    "공": {"kong", "gong"},
    "현": {"hyun", "hyeon", "xuan"},
    "함": {"ham", "hahm"},
    "변": {"byun", "byeon", "pyun", "bian"},
    "염": {"yeom", "yum", "yan"},
    "여": {"yeo", "yuh", "lu", "yu"},
    "추": {"choo", "chu", "qiu"},
    "도": {"do", "doh", "tao", "tu"},
    "소": {"so", "soh", "su"},
    "석": {"seok", "suk", "shi"},
    "선": {"sun", "seon", "xuan"},
    "설": {"seol", "sul", "xue"},
    "마": {"ma"},
    "길": {"gil", "kil"},
    "연": {"yeon", "yun", "lian", "yan"},
    "위": {"wi", "wee", "wei"},
    "표": {"pyo", "biao"},
    "명": {"myung", "myeong", "ming"},
    "기": {"ki", "gi", "qi"},
    "반": {"ban", "pan"},
    "왕": {"wang"},
    "금": {"keum", "geum", "kum", "jin"},
    "옥": {"ok", "ock", "yu"},
    "육": {"yook", "yuk", "lu"},
    "인": {"in", "yin"},
    "맹": {"maeng", "meng"},
    "제": {"je", "jae", "ji"},
    "모": {"mo", "mao"},
    "탁": {"tak", "zhuo"},
    "국": {"kook", "guk", "kuk", "ju"},
    "어": {"eo", "uh", "yu"},
    "은": {"eun", "yin"},
    "편": {"pyeon", "pyun", "pian"},
    "용": {"yong", "long"},
    "남궁": {"namgoong", "namgung", "nangong"},
    "황보": {"hwangbo", "huangfu"},
    "제갈": {"jegal", "zhuge"},
    "선우": {"sunwoo", "seonu", "xianyu"},
    "독고": {"dokgo", "dugu"},
    "사공": {"sagong", "sikong"},
    "서문": {"seomun", "ximen"},
    "동방": {"dongbang", "dongfang"},
}

_RR_INITIALS = ["g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "", "j", "jj", "ch", "k", "t", "p", "h"]
_RR_VOWELS = ["a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae", "oe", "yo", "u", "wo", "we", "wi", "yu", "eu", "ui", "i"]
_RR_FINALS = ["", "k", "k", "k", "n", "n", "n", "t", "l", "k", "m", "l", "l", "l", "p", "l", "m", "p", "p", "t", "t", "ng", "t", "t", "k", "t", "p", "t"]


def revised_romanization(hangul: str) -> str:
    out = []
    for char in hangul:
        code = ord(char) - 0xAC00
        if not 0 <= code < 11172:
            return ""
        out.append(_RR_INITIALS[code // 588] + _RR_VOWELS[(code % 588) // 28] + _RR_FINALS[code % 28])
    return "".join(out)


def _loose(value: str) -> str:
    """Collapse spelling variants (k/g, p/b, t/d, r/l, ee/i, oo/u, eo/u) for a lenient match."""
    text = re.sub(r"[^a-z]", "", value.casefold())
    for pattern, replacement in (("ee", "i"), ("oo", "u"), ("eo", "u"), ("ou", "u"), ("k", "g"), ("p", "b"),
                                 ("t", "d"), ("r", "l"), ("ch", "j"), ("sh", "s"), ("h$", "")):
        text = re.sub(pattern, replacement, text)
    return text


def _surname_matches(token: str, surname: str) -> bool:
    lowered = re.sub(r"[^a-z]", "", token.casefold())
    if not lowered or not surname:
        return False
    if lowered in SURNAME_ROMANIZATIONS.get(surname, set()):
        return True
    romanized = revised_romanization(surname)
    return bool(romanized) and _loose(lowered) == _loose(romanized)


def infer_latin_name(latin: str, native: PersonName) -> PersonName:
    """Split a romanized name using the native (Hangul) family name to decide the order.

    'Hong Gildong' and 'HONG Gildong' -> family first; 'Gildong Hong' -> family last.
    When the native surname matches neither end (or both), the order is left for the user.
    """
    full = " ".join((latin or "").replace(",", " , ").split()).replace(" , ", ", ")
    if not full:
        return PersonName()
    if "," in full:
        family, given = [part.strip() for part in full.split(",", 1)]
        return PersonName(full=full.replace(",", ""), family=family, given=given, order="family_given",
                          split_confidence=0.9, split_method="comma_order_rule")
    tokens = full.split()
    if len(tokens) < 2:
        return PersonName(full=full)
    surname = native.family or (_compact(native.full)[:1] if re.fullmatch(r"[가-힣]{2,5}", _compact(native.full)) else "")
    first, last = _surname_matches(tokens[0], surname), _surname_matches(tokens[-1], surname)
    if first != last:
        if first:
            return PersonName(full=full, family=tokens[0], given=" ".join(tokens[1:]), order="family_given",
                              split_confidence=0.9, split_method="native_surname_match")
        return PersonName(full=full, family=tokens[-1], given=" ".join(tokens[:-1]), order="given_family",
                          split_confidence=0.9, split_method="native_surname_match")
    upper = [index for index, token in enumerate(tokens) if len(token) > 1 and token.isupper()]
    if len(upper) == 1 and upper[0] in {0, len(tokens) - 1}:
        index = upper[0]
        order = "family_given" if index == 0 else "given_family"
        given = tokens[1:] if index == 0 else tokens[:-1]
        return PersonName(full=full, family=tokens[index], given=" ".join(given), order=order,
                          split_confidence=0.7, split_method="uppercase_surname")
    return PersonName(full=full)
