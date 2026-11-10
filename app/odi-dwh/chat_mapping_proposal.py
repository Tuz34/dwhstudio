"""Deterministic, reviewable proposal fields for AI Studio mapping cards.

The LLM may suggest validated source and target identifiers when the user has
not specified them. Load strategy, keys, and layer come only from user text.
"""

from __future__ import annotations

import re
import unicodedata

# Same Oracle/ODI identifier contract as MappingGenerator._validate_identifier.
_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_DM_WARNING = "Datamart targets are supported only for Type 2 history."
_TYPE2_NAME_WARNING = "Mapping name is longer than 31 characters; Type 2 requires a shorter name."
_LAYERS = {"ODS": "ODS", "EDW": "EDW", "DWH": "DWH", "DM": "DM", "DATAMART": "DM", "DATA MART": "DM"}
_LAYER_WORD = re.compile(r"(?<![A-Z0-9_.])(?:DATA\s+MART|DATAMART|EDW|DWH|ODS|DM)(?![A-Z0-9_.])")
_KEY_MARKER = re.compile(
    r"\b(?:PRIMARY\s+KEY|NATURAL\s+KEY|KEY\s+COLUMNS?|KEYS?|PK|"
    r"ANAHTAR(?:\s+KOLON(?:U|LARI)?)?)\b"
)
_TARGET_MARKER = re.compile(
    r"\b(?:TARGET|HEDEF)\s+(?:(?:TABLE|TABLO(?:SU|SUNA|SUNU)?)(?:\s+(?:NAME|ADI))?\s+)?"
    r"(?:(?:AS|NAMED|ADI)\s+|[:=]\s*)?(\S+)"
    r"|\b(?:TO|INTO)\s+(?:(?:THE|A)\s+)?(?:TABLE\s+)?(\S+)"
)
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
_LOAD_PATTERNS = {
    "type2": re.compile(r"\b(?:SCD\s*[-_]?\s*2|TYPE\s*[-_]?\s*2|TIP\s*[-_]?\s*2|TARIHCELI|HISTORY|HISTORICAL)\b"),
    "type1": re.compile(r"\b(?:SCD\s*[-_]?\s*1|TYPE\s*[-_]?\s*1|TIP\s*[-_]?\s*1)\b"),
    "truncate_load": re.compile(r"\b(?:TRUNCATE|FULL\s+RELOAD|FULL\s+LOAD|TAM\s+YUKLEME|TAM\s+YENILE(?:ME)?)\b"),
    "insert_update": re.compile(r"\b(?:INSERT\s*[/+&-]\s*UPDATE|INSERT\s+UPDATE|UPSERT|MERGE|EKLE\s*[/+&-]\s*GUNCELLE|EKLE\s+VE\s+GUNCELLE|VARSA\s+GUNCELLE\s+YOKSA\s+EKLE)\b"),
}


def _plain(message: str) -> str:
    text = message.replace("ı", "i").replace("İ", "I")
    return "".join(
        char for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    ).upper()


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


def _token_ref(token: str) -> tuple[str | None, str | None]:
    # Only ordinary prose punctuation is removed. SQL punctuation remains
    # attached and fails the full identifier check.
    return _table_ref(token.strip('"\'(),.'))


def _layer(text: str) -> str | None:
    ranked = []
    for match in _LAYER_WORD.finditer(text):
        before = text[max(0, match.start() - 20):match.start()]
        after = text[match.end():match.end() + 22]
        if re.match(r"\s+STUDIO\b", after):
            continue
        score = 0
        if re.search(r"\b(?:TO|INTO|TARGET|HEDEF)\s+$", before):
            score += 3
        if re.match(r"\s+(?:LAYER|KATMAN(?:I|INA|INDA)?)\b", after):
            score += 2
        if re.search(r"\bFROM\s+$", before) or re.match(r"(?:DEN|DAN)\b", after):
            score -= 2
        ranked.append((score, match.start(), _LAYERS[" ".join(match.group().split())]))
    best = max(ranked) if ranked else None
    return best[2] if best and best[0] >= 0 else None


def _negated(text: str, match: re.Match[str]) -> bool:
    before = text[max(0, match.start() - 90):match.start()]
    before = re.split(r"[,;.!?]|\b(?:BUT|AMA|FAKAT)\b", before)[-1]
    if any(word in {"NOT", "DON'T", "DONT", "NEVER", "WITHOUT", "NO", "ASLA"}
           for word in before.split()[-6:]):
        return True
    after = text[match.end():match.end() + 50]
    after = re.split(r"[,;.!?]|\b(?:BUT|AMA|FAKAT)\b", after)[0]
    return any(
        word in {"DEGIL", "OLMASIN", "OLMADAN", "HARIC", "ISTEMIYORUM"}
        or re.fullmatch(r"[A-Z]+(?:MA|ME|SIZ|SUZ)", word)
        for word in after.split()[:3]
    )


def _positive_loads(text: str) -> list[tuple[str, re.Match[str]]]:
    return [
        (kind, match)
        for kind, pattern in _LOAD_PATTERNS.items()
        for match in pattern.finditer(text)
        if not _negated(text, match)
    ]


def _load_type(text: str) -> str | None:
    found = {kind for kind, _match in _positive_loads(text)}
    return next(iter(found)) if len(found) == 1 else None


def _key_tail(tail: str) -> list[str]:
    tokens = tail.lstrip(" :=").split()[:20]
    if tokens and tokens[0] in {"IS", "ARE", "OLARAK", "KOLONU", "COLUMN", "COLUMNS"}:
        tokens = tokens[1:]
    if tokens and tokens[0] in {"THE", "AN", "BIR"}:
        tokens = tokens[1:]
    keys: list[str] = []
    expect_name = True
    for token in tokens:
        if not expect_name and token in {"AND", "VE"}:
            expect_name = True
            continue
        if not expect_name and token in _KEY_BLOCKED:
            break
        if not expect_name:
            return [] if _identifier(token) else keys
        parts = token.rstrip(",").split(",")
        names = [_identifier(part) for part in parts]
        if not parts or not all(names) or any(name in _KEY_BLOCKED for name in names):
            return []
        keys.extend(name for name in names if name not in keys)
        expect_name = token.endswith(",")
    return keys if keys and not expect_name and len(keys) <= 8 else []


def _keys(text: str, load_type: str | None) -> list[str]:
    for marker in _KEY_MARKER.finditer(text):
        before = text[max(0, marker.start() - 28):marker.start()]
        if re.search(r"\b(?:COLUMN|COLUMNS|KOLON|KOLONU|KOLONLARI)(?:\s+NAMED)?\s+$", before):
            continue
        return _key_tail(text[marker.end():])
    match = re.search(r"\b([A-Z][A-Z0-9_]*)\s+ANAHTARI\b", text)
    key = _identifier(match.group(1)) if match else None
    if key and key not in _KEY_BLOCKED:
        return [key]
    if load_type in {"insert_update", "type1", "type2"}:
        for kind, match in _positive_loads(text):
            if kind != load_type:
                continue
            after = text[match.end():]
            on = re.match(r"\s+ON\s+(?:(?:THE|A|AN)\s+)?", after)
            if on:
                return _key_tail(after[on.end():])
    return []


def _explicit_target(text: str) -> tuple[str | None, str | None]:
    for match in _TARGET_MARKER.finditer(text):
        token = match.group(1) or match.group(2)
        if match.group(2) and re.match(r"\s+(?:LAYER|KATMAN(?:I|INA|INDA)?)\b", text[match.end():]):
            continue
        owner, table = _token_ref(token)
        if table and table not in _FUNCTION_WORDS | {"ODS", "EDW", "DWH", "DM", "DATA", "DATAMART", "MART"}:
            return owner, table
    return None, None


def _explicit_source(text: str) -> tuple[str | None, str | None]:
    owner_match = re.search(r"\b(?:SOURCE|KAYNAK)\s+SCHEMA\s+(\S+)", text)
    stated_owner = _identifier(owner_match.group(1)) if owner_match else None
    # Qualified references win over nearby prose. Never mistake a target ref
    # introduced by TARGET/TO/INTO for the source.
    for match in re.finditer(r"\S+", text):
        owner, table = _token_ref(match.group())
        before = text[max(0, match.start() - 40):match.start()]
        is_target = re.search(
            r"\b(?:TARGET|HEDEF|TO|INTO)\s+(?:(?:THE|A|AN)\s+)?(?:TABLE\s+)?$",
            before,
        )
        if owner and table and owner not in _FUNCTION_WORDS and table not in _FUNCTION_WORDS and not is_target:
            return owner, table
    for match in re.finditer(r"\b([A-Z][A-Z0-9_]*)\s+(?:TABLE|TABLO[A-Z]*)\b", text):
        before = text[max(0, match.start() - 18):match.start()]
        if re.search(r"\b(?:SCHEMA|OWNER)\s+$", before):
            continue
        table = _identifier(match.group(1))
        if table and table not in _FUNCTION_WORDS | {"ODS", "DWH", "EDW", "DM"}:
            return stated_owner, table
    for match in re.finditer(
        r"\b(?:SOURCE|FROM|KAYNAK|TABLE|TABLO[A-Z]*)\s+"
        r"(?:(?:THE|A|AN|BIR|BU|SU)\s+)?(\S+)", text,
    ):
        before = text[max(0, match.start() - 18):match.start()]
        if re.search(r"\b(?:TARGET|HEDEF|TO|INTO)\s+$", before):
            continue
        owner, table = _token_ref(match.group(1))
        if table and table not in _FUNCTION_WORDS | {"ODS", "DWH", "EDW", "DM"}:
            return owner or stated_owner, table
    return stated_owner, None


def _hint_source(raw: dict) -> tuple[str | None, str | None]:
    owner, table = _table_ref(raw.get("source_table") or raw.get("src_table"))
    return owner or _identifier(raw.get("source_schema") or raw.get("src_schema")), table


def _hint_target(raw: dict) -> tuple[str | None, str | None]:
    owner, table = _table_ref(raw.get("target_table") or raw.get("tgt_table"))
    return owner or _identifier(raw.get("target_schema") or raw.get("tgt_owner")), table


def _scrub_identifiers(text: str, source_table: str | None, target_table: str | None) -> str:
    tokens = list(re.finditer(r"\S+", text))
    names = {name for name in (source_table, target_table) if name}
    markers = {"TABLE", "TABLO", "TABLOSU", "TABLOSUNU", "TABLOSUNDAN", "TABLOSUNA",
               "TARGET", "HEDEF", "SOURCE", "KAYNAK", "FROM"}
    articles = {"THE", "A", "AN", "BIR", "BU", "SU"}
    clean = [match.group().strip('"\'(),.') for match in tokens]
    masked = set()
    for index, token in enumerate(clean):
        if re.search(r"[A-Z][A-Z0-9_]*\.[A-Z][A-Z0-9_]*", token) or token in names:
            masked.add(index)
        if token in markers:
            next_index = index + 1
            while next_index < len(clean) and clean[next_index] in articles | markers:
                masked.add(next_index)
                next_index += 1
            if next_index < len(clean):
                masked.add(next_index)
    chars = list(text)
    for index in masked:
        match = tokens[index]
        chars[match.start():match.end()] = " " * (match.end() - match.start())
    return "".join(chars)


def _explicit_mapping_name(text: str) -> str | None:
    match = re.search(r"\b(?:MAPPING|MAP)\s+(?:NAME|NAMED|ADI|ADINDA)\s+(\S+)", text)
    return _identifier(match.group(1).strip('"\'(),.')) if match else None


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

    text = _plain(message)
    layer = _layer(text)
    stated: set[str] = set()
    user_source_owner, user_source_table = _explicit_source(text)
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
    explicit_owner, explicit_table = _explicit_target(text)
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

    intent = _scrub_identifiers(text, source_table, target_table)
    load_type = _load_type(intent)
    keys = _keys(intent, load_type)
    explicit_name = _explicit_mapping_name(text)

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
