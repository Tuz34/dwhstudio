"""Deterministic, reviewable proposal fields for AI Studio mapping cards.

The LLM may suggest validated source and target identifiers when the user has
not specified them. Load strategy, keys, and layer come only from user text.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from datamart_contract import DATAMART_PLACEMENT_WARNING as _DM_PLACEMENT_WARNING
from datamart_contract import DATAMART_TYPE2_ONLY_WARNING as _DM_WARNING
from mapping_generator import classic_internal_source_model

# Same Oracle/ODI identifier contract as MappingGenerator._validate_identifier.
_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_TYPE2_NAME_WARNING = "Mapping name is longer than 31 characters; Type 2 requires a shorter name."
_LAYERS = {"ODS": "ODS", "EDW": "EDW", "DWH": "DWH", "DM": "DM", "DATAMART": "DM", "DATA MART": "DM"}
# Classic acceptance uses ORACLE_ODS for ODS targets and ORACLE_DWH for F3.
_TARGET_MODEL_BY_LAYER = {"ODS": "ORACLE_ODS", "DWH": "ORACLE_DWH", "EDW": "ORACLE_DWH"}
_PART = r'''(?:[A-Z][A-Z0-9_]*|"[A-Z][A-Z0-9_]*"|'[A-Z][A-Z0-9_]*'|`[A-Z][A-Z0-9_]*`|“[A-Z][A-Z0-9_]*”|‘[A-Z][A-Z0-9_]*’)'''
_DOTTED = re.compile(rf"{_PART}(?:\.{_PART})+")
_WORD = re.compile(r"[A-Z][A-Z0-9_]*(?:['’][A-Z0-9_]+)*|[0-9]+")
_CASE_SUFFIX = re.compile(r"['’]([A-Z]{1,6})(?![A-Z0-9_])")
_WORD_CASE_SUFFIX = re.compile(r"([A-Z][A-Z0-9_]*)['’]([A-Z]{1,6})\Z")
_QUOTED = {'"': '"', "'": "'", '`': '`', '“': '”', '‘': '’'}
_PUNCTUATION = frozenset(",.():=?!/+&-")
_DATIVE_SUFFIXES = frozenset({"A", "E", "YA", "YE"})
_CASE_LAYER_WORDS = frozenset(_LAYERS) | {"MART"}
_FUNCTION_WORDS = frozenset({
    "THE", "A", "AN", "INTO", "TO", "FROM", "AS", "ON", "IN", "FOR", "WITH",
    "AND", "OR", "OF", "IS", "ARE", "BY", "USE", "USING", "BIR", "BU", "SU",
    "ILE", "VE", "ICIN", "OLARAK", "LAYER", "KATMAN", "KATMANI", "KATMANINA",
    "KATMANINDA", "SOURCE", "TARGET", "HEDEF", "SCHEMA", "OWNER", "TABLE",
    "TABLO", "TABLOSU", "TABLOSUNU", "TABLOSUNDAN", "PLEASE", "NOT", "NO",
    "WITHOUT", "NEED", "THEN", "DEGIL", "HARIC",
    "MODEL", "MODELLE", "LOAD", "CREATE", "TRANSFER", "AKTAR", "MAPPING",
})
_CALENDAR_WORDS = frozenset({
    "TODAY", "TOMORROW", "YESTERDAY", "BUGUN", "YARIN", "DUN", "DAY", "DAYS",
    "WEEK", "WEEKS", "MONTH", "MONTHS", "YEAR", "YEARS", "MONDAY", "TUESDAY",
    "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY", "JANUARY",
    "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST",
    "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER", "PAZARTESI", "SALI",
    "CARSAMBA", "PERSEMBE", "CUMA", "CUMARTESI", "PAZAR",
})
_KEY_BLOCKED = (_FUNCTION_WORDS | _CALENDAR_WORDS | frozenset({
    "ODS", "DWH", "EDW", "DM", "DATAMART", "DATA", "MART", "TRUNCATE",
    "MERGE", "UPSERT", "INSERT", "UPDATE", "RELOAD", "FULL", "TYPE", "TIP",
    "SCD1", "SCD2", "HISTORY", "HISTORICAL", "EKLE", "GUNCELLE", "KEY",
    "KEYS", "PRIMARY", "NATURAL", "ANAHTAR",
})) - {"A"}  # Explicit `key A, B` names columns; `a` is an article only in prose.
_LOAD_PHRASES = {
    "insert_update": (
        ("INSERT", "/", "UPDATE"), ("INSERT", "+", "UPDATE"),
        ("INSERT", "&", "UPDATE"), ("INSERT", "-", "UPDATE"),
        ("INSERT", "UPDATE"), ("UPSERT",), ("MERGE",),
        ("EKLE", "/", "GUNCELLE"), ("EKLE", "+", "GUNCELLE"),
        ("EKLE", "&", "GUNCELLE"), ("EKLE", "-", "GUNCELLE"),
        ("EKLE", "VE", "GUNCELLE"),
        ("EKLEME", "/", "GUNCELLEME"), ("EKLEME", "GUNCELLEME"),
        ("VARSA", "GUNCELLE", "YOKSA", "EKLE"),
    ),
    "truncate_load": (
        ("TRUNCATE", "INSERT"), ("TRUNCATE",), ("FULL", "RELOAD"),
        ("FULL", "LOAD"), ("TAM", "YUKLEME"), ("TAM", "YENILE"),
        ("TAM", "YENILEME"),
    ),
    "type1": (
        ("TYPE", "1"), ("TIP", "1"), ("SCD", "1"), ("SCD", "-", "1"),
        ("SCD", "_", "1"), ("SCD1",), ("SCD_1",),
    ),
    "type2": (
        ("TYPE", "2", "HISTORY"), ("TIP", "2", "HISTORY"),
        ("SCD", "2", "HISTORY"), ("SCD2", "HISTORY"),
        ("TYPE", "2"), ("TIP", "2"), ("SCD", "2"),
        ("SCD", "-", "2"), ("SCD_2",), ("SCD2",),
        ("TARIHCELI",), ("HISTORY",), ("HISTORICAL",),
    ),
}
_HEDGE_EXACT = frozenset({
    "NOT", "NO", "NEVER", "DONT", "WITHOUT", "EXCEPT", "UNLESS", "IF", "CANNOT",
    "OLMASIN", "OLMADAN", "HARIC", "YERINE", "EGER", "ASLA", "HAYIR",
    "GEREKSIZ", "GEREKMEZ", "GEREKMIYOR",
})
_NEGATIVE_ENDING = (
    r"(?:(?:MA|ME)(?:SIN(?:IZ)?|YIN(?:IZ)?|DAN|DEN|Z|DI[A-Z]*|"
    r"YACAK[A-Z]*|YECEK[A-Z]*|MALI[A-Z]*|MELI[A-Z]*)|MIYOR[A-Z]*)"
)
_AUX_NEGATIVE = re.compile(
    rf"(?:YAP(?:IL|TIR|TIRIL)?|ET|EDIL|ETTIR(?:IL)?){_NEGATIVE_ENDING}\Z"
)
_OTHER_NEGATIVE = re.compile(
    rf"(?:YUKLE(?:N|T|TIL)?|SIL(?:IN|DIR|DIRIL)?|AT(?:IL|TIR|TIRIL)?|"
    rf"KULLAN(?:IL|DIR|DIRIL)?|TUT(?:UL|TUR|TURUL)?|OL(?:UN|DUR|DURUL)?|"
    rf"EKLE(?:N|T|TIL)?|GUNCELLE(?:N|T|TIL)?){_NEGATIVE_ENDING}\Z"
)
_ISTEM_NEGATIVE = re.compile(r"ISTEM(?:IYOR[A-Z]*|EM|EZ|EDIM|EYIZ)\Z")
_AVOID_FORMS = frozenset({"AVOID", "AVOIDS", "AVOIDING", "AVOIDED"})
_SHOULD_FORMS = frozenset({"SHOULD", "SHOULDNT", "SHOULDN'T"})
_LOAD_PRIVATIVE = re.compile(r"(?:TARIH|TRUNCATE|INSERT|UPDATE|MERGE|UPSERT|TYPE|TIP|SCD|YUKLE|YENILE|GUNCELLE|EKLE)[A-Z0-9_]*(?:SIZ|SUZ)\Z")


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    case_suffix: str = ""
    identifier_shaped: bool = False


def _plain(message: str, *, uppercase: bool = True) -> str:
    text = message.replace("ı", "i").replace("İ", "I")
    plain = "".join(
        char for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    )
    return plain.upper() if uppercase else plain


def _case_suffix_allowed(base: str, suffix: str, original_base: str) -> bool:
    return suffix != "S" and not (suffix == "T" and base.endswith("N")) and bool(original_base) and (
        "." in base or "_" in base or any(char.isdigit() for char in base) or
        original_base.isupper() or base in _CASE_LAYER_WORDS
    )


def _identifier_shape_as_typed(value: str) -> bool:
    return bool(value) and (value.isupper() or "_" in value or any(char.isdigit() for char in value))


def _lex(message: str) -> list[_Token]:
    """A dotted name or a quoted span is one opaque grammar token."""
    case_text = _plain(message, uppercase=False)
    text = case_text.upper()
    tokens = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        dotted = _DOTTED.match(text, index)
        word = _WORD.match(text, index) if not dotted else None
        match = dotted or word
        if match:
            end = match.end()
            suffix = _CASE_SUFFIX.match(text, end) if dotted else None
            if suffix and suffix.group(1) == "S":
                suffix = None
            if suffix:
                end = suffix.end()
            if end < len(text) and text[end] in ";'\"`“”‘’":
                while end < len(text) and not text[end].isspace() and text[end] not in ",()?":
                    end += 1
                tokens.append(_Token("invalid", text[index:end]))
                index = end
                continue
            if dotted:
                parts = [part.strip('"\'`“”‘’') for part in match.group().split(".")]
                tokens.append(_Token("dotted", ".".join(parts), suffix.group(1) if suffix else ""))
            else:
                value = match.group().replace("’", "'")
                word_suffix = _WORD_CASE_SUFFIX.fullmatch(value)
                if word_suffix and _case_suffix_allowed(
                    word_suffix.group(1), word_suffix.group(2),
                    case_text[index:index + len(word_suffix.group(1))]
                    if len(case_text) == len(text) else "",
                ):
                    original_base = case_text[index:index + len(word_suffix.group(1))]
                    tokens.append(_Token("word", word_suffix.group(1), word_suffix.group(2),
                                         _identifier_shape_as_typed(original_base)))
                else:
                    original_word = case_text[index:end] if len(case_text) == len(text) else ""
                    tokens.append(_Token("word", value,
                                         identifier_shaped=_identifier_shape_as_typed(original_word)))
            index = end
            continue
        if char in _QUOTED:
            close = text.find(_QUOTED[char], index + 1)
            while (close >= 0 and _QUOTED[char] in {"'", "’"} and
                   0 < close < len(text) - 1 and text[close - 1].isalnum() and
                   text[close + 1].isalnum()):
                close = text.find(_QUOTED[char], close + 1)
            if close < 0:
                index += 1  # An unclosed quote does not hide the remaining message.
                continue
            quoted_value = text[index + 1:close]
            index = close + 1
            tokens.append(_Token("quoted", quoted_value))
            continue
        if char in _PUNCTUATION:
            tokens.append(_Token("punct", char))
            index += 1
            continue
        end = index + 1
        while end < len(text) and not text[end].isspace() and text[end] not in ",()?":
            end += 1
        tokens.append(_Token("invalid", text[index:end]))
        index = end
    return tokens


def _identifier(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip().upper()
    return clean if _IDENTIFIER.fullmatch(clean) else None


def _table_ref(value: object) -> tuple[str | None, str | None]:
    if not isinstance(value, str):
        return None, None
    parts = value.strip().split(".")
    if len(parts) == 1:
        return None, _identifier(parts[0])
    if len(parts) == 2:
        owner, table = (_identifier(part) for part in parts)
        return (owner, table) if owner and table else (None, None)
    return None, None


def _at(tokens: list[_Token], index: int, value: str, taken: set[int]) -> bool:
    if index < 0 or index >= len(tokens) or index in taken:
        return False
    expected = "punct" if value in _PUNCTUATION else "word"
    return tokens[index].kind == expected and tokens[index].value == value


def _sequence(tokens: list[_Token], index: int, values: tuple[str, ...], taken: set[int]) -> bool:
    return all(_at(tokens, index + offset, value, taken) for offset, value in enumerate(values))


def _table_marker(tokens: list[_Token], index: int, taken: set[int]) -> bool:
    return (_at(tokens, index, "TABLE", taken) or
            (index < len(tokens) and index not in taken and tokens[index].kind == "word"
             and tokens[index].value.startswith("TABLO")))


def _layer_at(tokens: list[_Token], index: int, taken: set[int]) -> tuple[str, int] | None:
    if _sequence(tokens, index, ("DATA", "MART"), taken):
        return "DM", index + 2
    if index < len(tokens) and index not in taken and tokens[index].kind == "word":
        layer = _LAYERS.get(tokens[index].value)
        if layer:
            return layer, index + 1
    return None


def _layer_constructions(tokens: list[_Token], taken: set[int]) -> list[tuple[str, int, int]]:
    found = []
    for index in range(len(tokens)):
        if _at(tokens, index, "INTO", taken) or _at(tokens, index, "TO", taken):
            start = index + 1
            if _at(tokens, start, "THE", taken):
                start += 1
            layer = _layer_at(tokens, start, taken)
            if layer and not _at(tokens, layer[1], "STUDIO", taken):
                found.append((layer[0], index, layer[1]))
        if _sequence(tokens, index, ("IN", "THE"), taken):
            layer = _layer_at(tokens, index + 2, taken)
            if layer and not _at(tokens, layer[1], "STUDIO", taken):
                found.append((layer[0], index, layer[1]))
        layer = _layer_at(tokens, index, taken)
        if layer and tokens[layer[1] - 1].case_suffix in _DATIVE_SUFFIXES:
            found.append((layer[0], index, layer[1]))
        if layer and any(_at(tokens, layer[1], ending, taken) for ending in
                         ("LAYER", "KATMANI", "KATMANINA", "KATMANINDA")):
            found.append((layer[0], index, layer[1] + 1))
        if _at(tokens, index, "LAYER", taken) or _at(tokens, index, "KATMAN", taken):
            layer = _layer_at(tokens, index + 1, taken)
            if layer:
                found.append((layer[0], index, layer[1]))
    return found


def _layer(tokens: list[_Token], taken: set[int]) -> str | None:
    found = {layer for layer, _start, _end in _layer_constructions(tokens, taken)}
    return next(iter(found)) if len(found) == 1 else None


def _load_phrase_at(tokens: list[_Token], index: int, taken: set[int]) -> tuple[str, int] | None:
    matches = [
        (kind, index + len(phrase))
        for kind, phrases in _LOAD_PHRASES.items() for phrase in phrases
        if _sequence(tokens, index, phrase, taken)
    ]
    return max(matches, key=lambda item: item[1]) if matches else None


def _load_constructions(tokens: list[_Token], taken: set[int]) -> list[tuple[str, int, int]]:
    found = []
    index = 0
    while index < len(tokens):
        phrase = _load_phrase_at(tokens, index, taken)
        if not phrase:
            index += 1
            continue
        kind, end = phrase
        prefix = None
        previous = index - 1
        if any(_at(tokens, previous, word, taken) for word in ("LOAD", "MODE")):
            previous -= 1
        if any(_at(tokens, previous, word, taken) for word in ("A", "AN", "THE")):
            previous -= 1
        if any(_at(tokens, previous, word, taken) for word in ("AS", "USING", "WITH")):
            prefix = previous
        elif (_sequence(tokens, index - 2, ("LOAD", "TYPE"), taken) or
              _sequence(tokens, index - 2, ("YUKLEME", "TIPI"), taken)):
            prefix = index - 2
        suffix = any(_at(tokens, end, word, taken) for word in ("OLARAK", "ILE", "SEKLINDE"))
        if prefix is not None or suffix:
            found.append((kind, prefix if prefix is not None else index, end + int(suffix)))
            index = end
        else:
            index += 1
    return found


def _hedge_word(word: str) -> bool:
    if "_" in word or "." in word or any(char.isdigit() for char in word):
        return False
    return bool(
        word in _HEDGE_EXACT or word in _AVOID_FORMS or word in _SHOULD_FORMS or
        word in {"YAPMA", "ETME"} or
        "N'T" in word or word.startswith("DEGIL") or
        _AUX_NEGATIVE.fullmatch(word) or _OTHER_NEGATIVE.fullmatch(word) or
        _ISTEM_NEGATIVE.fullmatch(word) or _LOAD_PRIVATIVE.fullmatch(word)
    )


def _hedged(tokens: list[_Token], taken: set[int]) -> bool:
    for index, entry in enumerate(tokens):
        if index in taken or entry.kind in {"quoted", "dotted"}:
            continue
        if "?" in entry.value:
            return True
        if entry.kind != "word":
            continue
        word = entry.value
        if _hedge_word(word):
            return True
        if (_sequence(tokens, index, ("RATHER", "THAN"), taken) or
                _sequence(tokens, index, ("INSTEAD", "OF"), taken) or
                _sequence(tokens, index, ("GEREK", "YOK"), taken)):
            return True
    return False


def _load_type(tokens: list[_Token], taken: set[int]) -> str | None:
    if _hedged(tokens, taken):
        return None
    found = _load_constructions(tokens, taken)
    return found[0][0] if len(found) == 1 else None


def _target_value_index(tokens: list[_Token], index: int, taken: set[int]) -> int | None:
    if _at(tokens, index, "TARGET", taken) or _at(tokens, index, "HEDEF", taken):
        next_index = index + 1
        if _table_marker(tokens, next_index, taken):
            next_index += 1
        elif _at(tokens, next_index, "LAYER", taken) or _at(tokens, next_index, "KATMAN", taken):
            return None
    elif _at(tokens, index, "INTO", taken):
        next_index = index + 1
        if _at(tokens, next_index, "THE", taken):
            next_index += 1
        if not _table_marker(tokens, next_index, taken):
            return None
        next_index += 1
    else:
        return None
    if _at(tokens, next_index, ":", taken) or _at(tokens, next_index, "=", taken):
        next_index += 1
    return next_index


def _explicit_dm_target_owner(tokens: list[_Token]) -> tuple[str | None, set[int], set[int]]:
    """Read a separately stated Datamart target owner without treating SCHEMA as a table."""
    owners = []
    markers: set[int] = set()
    taken: set[int] = set()
    invalid = False
    for index in range(len(tokens) - 1):
        if (tokens[index].kind != "word" or tokens[index].value not in {"TARGET", "HEDEF"}
                or tokens[index + 1].kind != "word"
                or tokens[index + 1].value not in {"SCHEMA", "OWNER", "SEMA"}):
            continue
        markers.add(index)
        taken.update({index, index + 1})
        value_index = index + 2
        if _at(tokens, value_index, ":", set()) or _at(tokens, value_index, "=", set()):
            value_index += 1
        if value_index >= len(tokens):
            invalid = True
            continue
        taken.add(value_index)
        owner = (_identifier(tokens[value_index].value)
                 if tokens[value_index].kind in {"word", "quoted"} else None)
        if owner:
            owners.append(owner)
        else:
            invalid = True
    owner = owners[0] if not invalid and len(set(owners)) == 1 else None
    return owner, markers, taken


def _explicit_target(
    tokens: list[_Token], ignored_markers: set[int] | None = None,
) -> tuple[str | None, str | None, set[int], bool]:
    references = []
    taken: set[int] = set()
    marker_seen = False
    invalid_marker = False
    for index in range(len(tokens)):
        if ignored_markers and index in ignored_markers:
            continue
        value_index = _target_value_index(tokens, index, taken)
        if value_index is None:
            continue
        marker_seen = True
        if value_index >= len(tokens):
            invalid_marker = True
            continue
        entry = tokens[value_index]
        if entry.kind not in {"word", "dotted"}:
            invalid_marker = True
            continue
        owner, table = _table_ref(entry.value)
        if table:
            taken.add(value_index)
            references.append((owner, table))
        else:
            invalid_marker = True
    distinct = set(references)
    if not invalid_marker and len(distinct) == 1:
        return (*references[0], taken, marker_seen)
    return None, None, taken, marker_seen


def _explicit_source(
    tokens: list[_Token], target_taken: set[int],
) -> tuple[str | None, str | None, set[int]]:
    stated_owner = None
    owner_taken: set[int] = set()
    for index in range(len(tokens) - 2):
        if ((_sequence(tokens, index, ("SOURCE", "SCHEMA"), target_taken) or
             _sequence(tokens, index, ("KAYNAK", "SCHEMA"), target_taken))
                and tokens[index + 2].kind == "word"):
            stated_owner = _identifier(tokens[index + 2].value)
            if stated_owner:
                owner_taken.add(index + 2)
                break
    unavailable = target_taken | owner_taken
    for index, entry in enumerate(tokens):
        if index not in unavailable and entry.kind == "dotted":
            owner, table = _table_ref(entry.value)
            if owner and table:
                return owner, table, {index} | owner_taken

    candidates = []
    invalid_marker = False
    for marker in range(len(tokens)):
        if not _table_marker(tokens, marker, unavailable):
            continue
        previous = marker - 1
        suffix_only = tokens[marker].value != "TABLE" and tokens[marker].value != "TABLO"
        preceded_by_name = (previous >= 0 and previous not in unavailable and
                            tokens[previous].kind in {"word", "dotted", "invalid", "quoted"} and
                            tokens[previous].value not in _FUNCTION_WORDS)
        if suffix_only or preceded_by_name:
            candidate = previous
        else:
            candidate = marker + 1
            if candidate < len(tokens) and tokens[candidate].value in {"THE", "A", "AN", "BIR", "BU", "SU"}:
                candidate += 1
        if candidate < 0 or candidate >= len(tokens) or candidate in unavailable:
            invalid_marker = True
            continue
        entry = tokens[candidate]
        table = _identifier(entry.value) if entry.kind == "word" else None
        if not table or table in _FUNCTION_WORDS:
            invalid_marker = True
            continue
        candidates.append((candidate, table))
    if invalid_marker:
        return stated_owner, None, owner_taken
    if candidates:
        index, table = min(candidates)
        return stated_owner, table, {index} | owner_taken
    return stated_owner, None, owner_taken


def _key_marker_end(tokens: list[_Token], index: int, taken: set[int]) -> int | None:
    if ((_at(tokens, index, "PRIMARY", taken) or _at(tokens, index, "NATURAL", taken))
            and _at(tokens, index + 1, "KEY", taken)):
        return index + 2
    if _at(tokens, index, "KEY", taken):
        before = tokens[index - 1].value if index > 0 else ""
        two_before = tokens[index - 2].value if index > 1 else ""
        if before in {"COLUMN", "COLUMNS", "KOLON", "KOLONU", "KOLONLARI"} or (
                before == "NAMED" and two_before in {"COLUMN", "COLUMNS", "KOLON"}):
            return None
        if _at(tokens, index + 1, "COLUMN", taken) or _at(tokens, index + 1, "COLUMNS", taken):
            return index + 2
        return index + 1
    if any(_at(tokens, index, word, taken) for word in ("KEYS", "PK", "ANAHTAR")):
        if _at(tokens, index, "ANAHTAR", taken) and any(
                _at(tokens, index + 1, word, taken) for word in ("KOLON", "KOLONU", "KOLONLARI")):
            return index + 2
        return index + 1
    return None


def _key_boundary(
    tokens: list[_Token], index: int, taken: set[int],
    layer_starts: set[int], load_starts: set[int],
) -> bool:
    if index in layer_starts or index in load_starts:
        return True
    return any(_at(tokens, index, word, taken) for word in (
        "TARGET", "HEDEF", "INTO", "TO", "IN", "LAYER", "KATMAN", "AS",
        "USING", "WITH", "LOAD", "YUKLEME", "MODEL", "MODELLE", "AKTAR",
        "CREATE", "TRANSFER",
    ))


def _key_tail(
    tokens: list[_Token], start: int, taken: set[int],
    layer_starts: set[int], load_starts: set[int], *, on_clause: bool = False,
) -> tuple[list[str], set[int]]:
    index = start
    if _at(tokens, index, ":", taken) or _at(tokens, index, "=", taken):
        index += 1
    parenthesized = _at(tokens, index, "(", taken)
    if parenthesized:
        index += 1
    if on_clause and any(_at(tokens, index, article, taken) for article in ("THE", "AN")):
        index += 1
    if (on_clause and _at(tokens, index, "A", taken) and index + 1 < len(tokens)
            and tokens[index + 1].kind == "word" and tokens[index + 1].value not in {"AND", "VE"}):
        index += 1
    keys: list[str] = []
    used: set[int] = set()
    expect_name = True
    first_key_shaped = False
    while index < len(tokens):
        entry = tokens[index]
        if keys and _key_boundary(tokens, index, taken, layer_starts, load_starts):
            return ([], used) if parenthesized else (keys, used)
        if _at(tokens, index, ")", taken) and parenthesized and not expect_name:
            return keys, used
        if _at(tokens, index, ".", taken) or _at(tokens, index, "!", taken) or _at(tokens, index, "?", taken):
            return (keys if not expect_name and not parenthesized else []), used
        if (_at(tokens, index, ",", taken) or _at(tokens, index, "AND", taken)
                or _at(tokens, index, "VE", taken)):
            if expect_name:
                return [], used
            segment_end = index + 1
            while (segment_end < len(tokens) and
                   not _key_boundary(tokens, segment_end, taken, layer_starts, load_starts) and
                   not any(_at(tokens, segment_end, separator, taken) for separator in
                           (",", "AND", "VE", ".", "!", "?", ")"))):
                segment_end += 1
            if (segment_end > index + 2 and all(
                    entry.kind == "word" and _identifier(entry.value)
                    for entry in tokens[index + 1:segment_end])):
                return ([], used) if parenthesized else (keys, used)
            expect_name = True
            index += 1
            continue
        if index in taken or entry.kind != "word":
            return [], used
        name = _identifier(entry.value)
        if not expect_name or not name or name in _KEY_BLOCKED or _hedge_word(entry.value):
            if name and not _hedge_word(entry.value):
                used.add(index)
            return [], used
        if keys and first_key_shaped and not entry.identifier_shaped:
            return ([], used) if parenthesized else (keys, used)
        used.add(index)
        if not keys:
            first_key_shaped = entry.identifier_shaped
        if name not in keys:
            keys.append(name)
        if len(keys) > 8:
            return [], used
        expect_name = False
        index += 1
    return (keys if keys and not expect_name and not parenthesized else []), used


def _keys(tokens: list[_Token], taken: set[int]) -> tuple[list[str], set[int]]:
    loads = _load_constructions(tokens, taken)
    load_starts = {start for _kind, start, _end in loads}
    layer_starts = {start for _layer_name, start, _end in _layer_constructions(tokens, taken)}
    for index in range(len(tokens)):
        marker_end = _key_marker_end(tokens, index, taken)
        if marker_end is not None:
            return _key_tail(tokens, marker_end, taken, layer_starts, load_starts)
    if not _hedged(tokens, taken) and len(loads) == 1 and loads[0][0] in {"insert_update", "type1", "type2"}:
        end = loads[0][2]
        if _at(tokens, end, "ON", taken):
            return _key_tail(tokens, end + 1, taken, layer_starts, load_starts, on_clause=True)
    return [], set()


def _hint_source(raw: dict) -> tuple[str | None, str | None]:
    owner, table = _table_ref(raw.get("source_table") or raw.get("src_table"))
    return owner or _identifier(raw.get("source_schema") or raw.get("src_schema")), table


def _hint_target(raw: dict) -> tuple[str | None, str | None]:
    owner, table = _table_ref(raw.get("target_table") or raw.get("tgt_table"))
    return owner or _identifier(raw.get("target_schema") or raw.get("tgt_owner")), table


_EXPLICIT_MODEL_NAMES = frozenset({
    "SQL_SERVER", "MSSQL", "ADVENTUREWORK_PRODUCTION", "SRC_HR", "SRC_SH", "TGT_DWH", "DWH",
})


def _model_name_at(tokens: list[_Token], index: int) -> str | None:
    if index < 0 or index >= len(tokens) or tokens[index].kind != "word":
        return None
    name = _identifier(tokens[index].value)
    if name and (name.startswith("ORACLE_") or name in _EXPLICIT_MODEL_NAMES):
        return name
    return None


def _explicit_source_model(tokens: list[_Token]) -> tuple[str | None, set[int]]:
    """Return a source model the sentence names, and the tokens that name it."""
    phrases = (
        ("SOURCE", "MODEL", "CODE"),
        ("SRC", "MODEL"),
        ("SOURCE", "MODEL"),
        ("KAYNAK", "MODELI"),
        ("KAYNAK", "MODEL"),
    )
    for index in range(len(tokens)):
        if tokens[index].kind == "word" and tokens[index].value == "SRC_MODEL":
            name = _model_name_at(tokens, index + 1)
            if name:
                return name, {index, index + 1}
        for phrase in phrases:
            if _sequence(tokens, index, phrase, set()) and _model_name_at(tokens, index + len(phrase)):
                name = _model_name_at(tokens, index + len(phrase))
                return name, set(range(index, index + len(phrase) + 1))
    return None, set()


def _explicit_mapping_name(tokens: list[_Token]) -> str | None:
    for index in range(len(tokens) - 2):
        if (tokens[index].kind == "word" and tokens[index].value in {"MAPPING", "MAP"}
                and tokens[index + 1].kind == "word"
                and tokens[index + 1].value in {"NAME", "NAMED", "ADI", "ADINDA"}
                and tokens[index + 2].kind == "word"):
            return _identifier(tokens[index + 2].value)
    return None


def normalize_mapping_request(message: str, raw: object) -> dict | None:
    """Normalize a proposed mapping without trusting LLM load, key, or layer guesses."""
    if not isinstance(raw, dict):
        return None
    operation = str(raw.get("operation") or "CREATE").strip().upper()
    if operation not in {"CREATE", "UPDATE", "DELETE"}:
        return None
    if operation != "CREATE":
        name = _identifier(raw.get("mapping_name"))
        if not name:
            return None
        proposal = {"operation": operation, "mapping_name": name}
        owner = _identifier(raw.get("tgt_owner"))
        if owner:
            proposal["tgt_owner"] = owner
        if operation == "UPDATE" and isinstance(raw.get("changes"), dict):
            changes = raw["changes"]
            safe_changes = {}
            table = _identifier(changes.get("tgt_table"))
            if table:
                safe_changes["tgt_table"] = table
            if isinstance(changes.get("description"), str):
                safe_changes["description"] = changes["description"]
            if safe_changes:
                proposal["changes"] = safe_changes
        return proposal

    tokens = _lex(message)
    stated: set[str] = set()
    explicit_model, model_taken = _explicit_source_model(tokens)
    dm_owner, dm_owner_markers, dm_owner_taken = (
        _explicit_dm_target_owner(tokens) if _layer(tokens, model_taken) == "DM"
        else (None, set(), set())
    )
    explicit_owner, explicit_table, target_taken, target_marker_seen = _explicit_target(
        tokens, dm_owner_markers
    )
    user_source_owner, user_source_table, source_taken = _explicit_source(
        tokens, target_taken | dm_owner_taken | model_taken
    )
    identifier_taken = target_taken | dm_owner_taken | source_taken | model_taken
    keys, key_taken = _keys(tokens, identifier_taken)
    intent_taken = identifier_taken | key_taken
    layer = _layer(tokens, intent_taken)
    load_type = _load_type(tokens, intent_taken)
    hint_source_owner, hint_source_table = _hint_source(raw)
    if user_source_table:
        source_table = user_source_table
        source_owner = user_source_owner or (
            hint_source_owner if hint_source_table == user_source_table else None
        )
        stated.add("source_table")
        if user_source_owner:
            stated.add("source_schema")
    elif user_source_owner:
        source_owner, source_table = user_source_owner, None
        stated.add("source_schema")
    else:
        source_owner, source_table = hint_source_owner, hint_source_table

    if layer:
        stated.add("target_layer")
    if dm_owner:
        stated.add("target_schema")
    hint_target_owner, hint_target_table = _hint_target(raw)
    if explicit_table:
        target_table = explicit_table
        target_owner = explicit_owner or dm_owner or (
            "DWH" if layer
            else hint_target_owner if hint_target_table == explicit_table else None
        )
        stated.add("target_table")
        if explicit_owner:
            stated.add("target_schema")
    elif target_marker_seen or (dm_owner_markers and not dm_owner):
        target_owner = dm_owner or ("DWH" if layer else None)
        target_table = None
    elif layer in {"ODS", "EDW", "DWH", "DM"} and source_owner and source_table:
        target_owner = dm_owner or "DWH"
        prefix = f"{layer}_" if layer != "DWH" else ""
        target_table = _identifier(f"{prefix}{source_owner}_{source_table}")
    elif layer:
        target_owner = dm_owner or "DWH"
        target_table = None
    else:
        target_owner, target_table = hint_target_owner, hint_target_table

    if source_table and target_table and source_table == target_table and (
            source_owner == target_owner or source_owner is None or target_owner is None):
        target_owner, target_table = None, None
        stated.discard("target_schema")
        stated.discard("target_table")
    explicit_name = _explicit_mapping_name(tokens)
    dm_classic = layer == "DM" and load_type != "type2"
    if dm_classic:
        target_owner = explicit_owner or dm_owner or "DWH"
        target_table = None
        stated.discard("target_table")

    proposal: dict = {"operation": "CREATE"}
    if source_owner:
        proposal.update(source_schema=source_owner, src_schema=source_owner)
    if source_table:
        proposal["source_table"] = source_table
        proposal["src_table"] = source_table
        if explicit_model:
            proposal["src_model"] = explicit_model
            stated.add("source_model")
        else:
            proposal["src_model"] = classic_internal_source_model(source_owner) or "ORACLE_SOURCE"
    if layer:
        proposal["target_layer"] = layer
        target_model = _TARGET_MODEL_BY_LAYER.get(layer)
        if target_model:
            proposal["tgt_model"] = target_model
    if target_owner:
        proposal.update(target_schema=target_owner, tgt_owner=target_owner)
    if target_table:
        proposal.update(target_table=target_table, tgt_table=target_table)
    if explicit_name and not dm_classic:
        proposal["mapping_name"] = explicit_name
        stated.add("mapping_name")
    elif not dm_classic and layer in {"ODS", "EDW", "DWH", "DM"} and source_owner and source_table:
        name = _identifier(f"MAP_{source_owner}_{source_table}_TO_{layer}")
        if name and (load_type != "type2" or len(name) <= 31):
            proposal["mapping_name"] = name
    if load_type:
        proposal["load_type"] = load_type
        stated.add("load_type")
    if keys:
        proposal["key_columns"] = keys
        stated.add("key_columns")
    warnings = []
    if layer == "DM" and load_type != "type2":
        warnings.append(_DM_WARNING)
    if layer == "DM" and (
            (dm_owner and dm_owner != "DWH") or
            (explicit_owner and explicit_owner != "DWH") or
            (explicit_table and not explicit_table.startswith("DM_"))):
        warnings.append(_DM_PLACEMENT_WARNING)
    if load_type == "type2" and explicit_name and len(explicit_name) > 31:
        warnings.append(_TYPE2_NAME_WARNING)
    if warnings:
        proposal["warnings"] = warnings
    proposal["stated_fields"] = [
        field for field in ("source_schema", "source_table", "source_model", "target_layer",
                            "target_schema", "target_table", "mapping_name",
                            "load_type", "key_columns") if field in stated
    ]
    return proposal
