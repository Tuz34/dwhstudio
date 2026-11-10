"""Deterministic, reviewable proposal fields for AI Studio mapping cards.

The LLM may suggest validated source and target identifiers when the user has
not specified them. Load strategy, keys, and layer come only from user text.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Same Oracle/ODI identifier contract as MappingGenerator._validate_identifier.
_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_DM_WARNING = "Datamart targets are supported only for Type 2 history."
_TYPE2_NAME_WARNING = "Mapping name is longer than 31 characters; Type 2 requires a shorter name."
_LAYERS = {"ODS": "ODS", "EDW": "EDW", "DWH": "DWH", "DM": "DM", "DATAMART": "DM", "DATA MART": "DM"}
_PART = r'''(?:[A-Z][A-Z0-9_]*|"[A-Z][A-Z0-9_]*"|'[A-Z][A-Z0-9_]*'|`[A-Z][A-Z0-9_]*`|“[A-Z][A-Z0-9_]*”|‘[A-Z][A-Z0-9_]*’)'''
_DOTTED = re.compile(rf"{_PART}(?:\.{_PART})+")
_WORD = re.compile(r"[A-Z][A-Z0-9_]*(?:['’][A-Z0-9_]+)*|[0-9]+")
_QUOTED = {'"': '"', "'": "'", '`': '`', '“': '”', '‘': '’'}
_PUNCTUATION = frozenset(",.():=?!/+&-")
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
})
_NEGATIVE_VERB = re.compile(r"(?:YAP|ET|OL|TUT|KULLAN|SIL|YUKLE|AT)(?:MA|ME)[A-Z0-9_]*\Z")
_LOAD_PRIVATIVE = re.compile(r"(?:TARIH|TRUNCATE|INSERT|UPDATE|MERGE|UPSERT|TYPE|TIP|SCD|YUKLE|YENILE|GUNCELLE|EKLE)[A-Z0-9_]*(?:SIZ|SUZ)\Z")


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str


def _plain(message: str) -> str:
    text = message.replace("ı", "i").replace("İ", "I")
    return "".join(
        char for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    ).upper()


def _lex(message: str) -> list[_Token]:
    """A dotted name or a quoted span is one opaque grammar token."""
    text = _plain(message)
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
            if end < len(text) and text[end] in ";'\"`“”‘’":
                while end < len(text) and not text[end].isspace() and text[end] not in ",()?":
                    end += 1
                tokens.append(_Token("invalid", text[index:end]))
                index = end
                continue
            if dotted:
                parts = [part.strip('"\'`“”‘’') for part in match.group().split(".")]
                tokens.append(_Token("dotted", ".".join(parts)))
            else:
                tokens.append(_Token("word", match.group().replace("’", "'")))
            index = end
            continue
        if char in _QUOTED:
            close = text.find(_QUOTED[char], index + 1)
            while (close >= 0 and _QUOTED[char] in {"'", "’"} and
                   0 < close < len(text) - 1 and text[close - 1].isalnum() and
                   text[close + 1].isalnum()):
                close = text.find(_QUOTED[char], close + 1)
            index = len(text) if close < 0 else close + 1
            tokens.append(_Token("quoted", ""))
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
    return bool(
        word in _HEDGE_EXACT or "N'T" in word or
        word.startswith(("AVOID", "SHOULD", "DEGIL", "ISTEM")) or
        _NEGATIVE_VERB.fullmatch(word) or _LOAD_PRIVATIVE.fullmatch(word)
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
        loading_noun = word == "YUKLEME" and (
            _at(tokens, index - 1, "TAM", taken) or _at(tokens, index + 1, "TIPI", taken))
        if not loading_noun and _hedge_word(word):
            return True
        if (_sequence(tokens, index, ("RATHER", "THAN"), taken) or
                _sequence(tokens, index, ("INSTEAD", "OF"), taken)):
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


def _explicit_target(tokens: list[_Token]) -> tuple[str | None, str | None, set[int]]:
    references = []
    taken: set[int] = set()
    for index in range(len(tokens)):
        value_index = _target_value_index(tokens, index, taken)
        if value_index is None or value_index >= len(tokens):
            continue
        entry = tokens[value_index]
        if entry.kind not in {"word", "dotted"}:
            continue
        owner, table = _table_ref(entry.value)
        if table:
            taken.add(value_index)
            references.append((owner, table))
    distinct = set(references)
    return (*references[0], taken) if len(distinct) == 1 else (None, None, taken)


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
    for index, entry in enumerate(tokens):
        if index in unavailable or entry.kind != "word":
            continue
        table = _identifier(entry.value)
        if not table or table in _FUNCTION_WORDS:
            continue
        if (_table_marker(tokens, index + 1, unavailable) or
                _table_marker(tokens, index - 1, unavailable) or
                (index >= 2 and tokens[index - 1].kind == "word" and
                 tokens[index - 1].value in {"THE", "A", "AN", "BIR", "BU", "SU"} and
                 _table_marker(tokens, index - 2, unavailable))):
            candidates.append((index, table))
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
        used.add(index)
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
    explicit_owner, explicit_table, target_taken = _explicit_target(tokens)
    user_source_owner, user_source_table, source_taken = _explicit_source(tokens, target_taken)
    identifier_taken = target_taken | source_taken
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
    hint_target_owner, hint_target_table = _hint_target(raw)
    if explicit_table:
        target_table = explicit_table
        target_owner = explicit_owner or (
            ("DM" if layer == "DM" else "DWH") if layer
            else hint_target_owner if hint_target_table == explicit_table else None
        )
        stated.add("target_table")
        if explicit_owner:
            stated.add("target_schema")
    elif layer in {"ODS", "EDW", "DWH"} and source_owner and source_table:
        target_owner = "DWH"
        prefix = f"{layer}_" if layer != "DWH" else ""
        target_table = _identifier(f"{prefix}{source_owner}_{source_table}")
    elif layer:
        target_owner = "DM" if layer == "DM" else "DWH"
        target_table = None
    else:
        target_owner, target_table = hint_target_owner, hint_target_table

    if source_table and target_table and source_table == target_table and (
            source_owner == target_owner or source_owner is None or target_owner is None):
        target_owner, target_table = None, None
        stated.discard("target_schema")
        stated.discard("target_table")
    explicit_name = _explicit_mapping_name(tokens)

    proposal: dict = {"operation": "CREATE"}
    if source_owner:
        proposal.update(source_schema=source_owner, src_schema=source_owner)
    if source_table:
        proposal["source_table"] = source_table
        proposal["src_table"] = source_table
        proposal["src_model"] = "ORACLE_SOURCE"
    if layer:
        proposal["target_layer"] = layer
        if layer != "DM":
            proposal["tgt_model"] = "ORACLE_ODS" if layer == "ODS" else "ORACLE_DWH"
    if target_owner:
        proposal.update(target_schema=target_owner, tgt_owner=target_owner)
    if target_table:
        proposal.update(target_table=target_table, tgt_table=target_table)
    if explicit_name:
        proposal["mapping_name"] = explicit_name
        stated.add("mapping_name")
    elif layer in {"ODS", "EDW", "DWH"} and source_owner and source_table:
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
    if load_type == "type2" and explicit_name and len(explicit_name) > 31:
        warnings.append(_TYPE2_NAME_WARNING)
    if warnings:
        proposal["warnings"] = warnings
    proposal["stated_fields"] = [
        field for field in ("source_schema", "source_table", "target_layer",
                            "target_schema", "target_table", "mapping_name",
                            "load_type", "key_columns") if field in stated
    ]
    return proposal
