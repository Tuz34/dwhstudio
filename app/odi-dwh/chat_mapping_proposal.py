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
_LAYER_TOKEN = r"(?:DATA\s+MART|DATAMART|EDW|DWH|ODS|DM)"
_LAYER_PATTERNS = (
    re.compile(rf"\b(?:INTO|TO)(?:\s+THE)?\s+(?P<layer>{_LAYER_TOKEN})\b"),
    re.compile(rf"\bIN\s+THE\s+(?P<layer>{_LAYER_TOKEN})\b"),
    re.compile(rf"\b(?P<layer>{_LAYER_TOKEN})\s+(?:LAYER|KATMAN(?:I|INA|INDA)?)\b"),
    re.compile(rf"\b(?:LAYER|KATMAN)\s+(?P<layer>{_LAYER_TOKEN})\b"),
)
_KEY_MARKER = re.compile(
    r"\b(?:PRIMARY\s+KEY|NATURAL\s+KEY|KEY\s+COLUMNS?|KEYS?|PK|"
    r"ANAHTAR(?:\s+KOLON(?:U|LARI)?)?)\b"
)
_TARGET_MARKER = re.compile(
    r"\b(?:TARGET(?:\s+TABLE)?|HEDEF(?:\s+TABLO[A-Z]*)?|"
    r"INTO\s+(?:THE\s+)?TABLE)\s*(?::|=)?\s+(?P<token>\S+)"
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
    "type2": re.compile(r"\b(?:SCD\s*[-_]?\s*2|TYPE\s*[-_]?\s*2|TIP\s*[-_]?\s*2)(?:\s+HISTORY)?\b|\b(?:TARIHCELI|HISTORY|HISTORICAL)\b"),
    "type1": re.compile(r"\b(?:SCD\s*[-_]?\s*1|TYPE\s*[-_]?\s*1|TIP\s*[-_]?\s*1)\b"),
    "truncate_load": re.compile(r"\b(?:TRUNCATE(?:\s+INSERT)?|FULL\s+RELOAD|FULL\s+LOAD|TAM\s+YUKLEME|TAM\s+YENILE(?:ME)?)\b"),
    "insert_update": re.compile(r"\b(?:INSERT\s*[/+&-]\s*UPDATE|INSERT\s+UPDATE|UPSERT|MERGE|EKLE\s*[/+&-]\s*GUNCELLE|EKLE\s+VE\s+GUNCELLE|VARSA\s+GUNCELLE\s+YOKSA\s+EKLE)\b"),
}
_LOAD_PREFIX = re.compile(
    r"\b(?:(?:AS|USING|WITH)\s+(?:(?:A|AN|THE)\s+)?(?:(?:LOAD|MODE)\s+)?"
    r"|LOAD\s+TYPE\s+|YUKLEME\s+TIPI\s+)$"
)
_LOAD_SUFFIX = re.compile(r"\s+(?:OLARAK|ILE|SEKLINDE)\b")
_HEDGE = re.compile(
    r"\?|\b(?:NOT|NO|NEVER|DON'?T|WITHOUT|AVOID|RATHER\s+THAN|INSTEAD\s+OF|"
    r"EXCEPT|UNLESS|ONLY\s+IF|IF|SHOULD|DEGIL|OLMASIN|OLMADAN|HARIC|"
    r"YERINE|EGER|ISTEMIYORUM|ASLA|TARIHCESIZ|TARIHSIZ)\b|"
    r"\b(?:YAP|ET|TUT|KULLAN)(?:MA|ME|MADAN|MEDEN|MAYIN|MEYIN|MAZ|MEZ)\b"
)


def _plain(message: str) -> str:
    text = message.replace("ı", "i").replace("İ", "I").replace("’", "'")
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


def _masked(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        for index in range(start, end):
            if chars[index] != "?":
                chars[index] = " "
    return "".join(chars)


def _layer(text: str) -> str | None:
    found = {
        _LAYERS[" ".join(match.group("layer").split())]
        for pattern in _LAYER_PATTERNS for match in pattern.finditer(text)
    }
    return next(iter(found)) if len(found) == 1 else None


def _load_constructions(text: str) -> list[tuple[str, int, int]]:
    found = []
    for kind, pattern in _LOAD_PATTERNS.items():
        for match in pattern.finditer(text):
            prefix = _LOAD_PREFIX.search(text[:match.start()])
            suffix = _LOAD_SUFFIX.match(text, match.end())
            if prefix or suffix:
                found.append((kind, prefix.start() if prefix else match.start(),
                              suffix.end() if suffix else match.end()))
    return found


def _load_type(text: str) -> str | None:
    if _HEDGE.search(text):
        return None
    found = _load_constructions(text)
    return found[0][0] if len(found) == 1 else None


def _key_boundary(text: str) -> bool:
    if re.match(r"(?:TARGET|HEDEF|INTO|TO|IN|LAYER|KATMAN|AS|USING|WITH|"
                r"LOAD|YUKLEME|MODEL|MODELLE|AKTAR|CREATE|TRANSFER)\b", text):
        return True
    if any(pattern.match(text) for pattern in _LAYER_PATTERNS):
        return True
    return any(start == 0 for _kind, start, _end in _load_constructions(text))


def _key_tail(tail: str, offset: int, *, on_clause: bool = False) -> tuple[list[str], list[tuple[int, int]]]:
    tokens = list(re.finditer(r"[^\s,():=]+|[,():=]", tail))
    index = 0
    if tokens and tokens[0].group() in {":", "="}:
        index += 1
    if on_clause and index < len(tokens) and tokens[index].group() in {"THE", "AN"}:
        index += 1
    if (on_clause and index + 1 < len(tokens) and tokens[index].group() == "A"
            and tokens[index + 1].group() not in {",", "AND", "VE"}):
        index += 1
    keys: list[str] = []
    spans: list[tuple[int, int]] = []
    expect_name = True
    parenthesized = False
    for token_match in tokens[index:]:
        piece = token_match.group()
        if keys and _key_boundary(tail[token_match.start():]):
            return ([], spans) if parenthesized else (keys, spans)
        if piece == "(" and not keys and not parenthesized:
            parenthesized = True
            continue
        if piece == ")" and parenthesized and not expect_name:
            return keys, spans
        if piece in {",", "AND", "VE"}:
            if expect_name:
                return [], spans
            expect_name = True
            continue
        if not expect_name:
            if _identifier(piece) and not _HEDGE.fullmatch(piece):
                spans.append((offset + token_match.start(), offset + token_match.end()))
            return [], spans
        sentence_end = piece.endswith(".") and piece.count(".") == 1
        name = _identifier(piece[:-1] if sentence_end else piece)
        if not name or name in _KEY_BLOCKED:
            if name and not _HEDGE.fullmatch(name):
                spans.append((offset + token_match.start(), offset + token_match.end()))
            return [], spans
        spans.append((offset + token_match.start(), offset + token_match.end()))
        if name not in keys:
            keys.append(name)
        if len(keys) > 8:
            return [], spans
        expect_name = False
        if sentence_end:
            return keys, spans
    return (keys if keys and not expect_name and not parenthesized else []), spans


def _keys(text: str) -> tuple[list[str], list[tuple[int, int]]]:
    for marker in _KEY_MARKER.finditer(text):
        before = text[max(0, marker.start() - 28):marker.start()]
        if re.search(r"\b(?:COLUMN|COLUMNS|KOLON|KOLONU|KOLONLARI)(?:\s+NAMED)?\s+$", before):
            continue
        return _key_tail(text[marker.end():], marker.end())
    if not _HEDGE.search(text):
        constructions = _load_constructions(text)
        if len(constructions) == 1 and constructions[0][0] in {"insert_update", "type1", "type2"}:
            end = constructions[0][2]
            on = re.match(r"\s+ON\b", text[end:])
            if on:
                return _key_tail(text[end + on.end():], end + on.end(), on_clause=True)
    return [], []


def _explicit_target(text: str) -> tuple[str | None, str | None, list[tuple[int, int]]]:
    references = []
    spans = []
    for match in _TARGET_MARKER.finditer(text):
        token = match.group("token")
        if token in {"LAYER", "KATMAN"}:
            continue
        owner, table = _token_ref(token)
        if table:
            spans.append(match.span("token"))
            references.append((owner, table))
    distinct = set(references)
    return (*references[0], spans) if len(distinct) == 1 else (None, None, spans)


def _explicit_source(
    text: str, target_spans: list[tuple[int, int]],
) -> tuple[str | None, str | None, list[tuple[int, int]]]:
    owner_match = re.search(r"\b(?:SOURCE|KAYNAK)\s+SCHEMA\s+(\S+)", text)
    stated_owner = _identifier(owner_match.group(1)) if owner_match else None
    owner_spans = [owner_match.span(1)] if owner_match and stated_owner else []

    def available(span: tuple[int, int]) -> bool:
        return not any(span[0] < end and start < span[1]
                       for start, end in [*target_spans, *owner_spans])

    for match in re.finditer(r"\S+", text):
        span = match.span()
        owner, table = _token_ref(match.group())
        if (available(span) and owner and table and owner not in _FUNCTION_WORDS
                and table not in _FUNCTION_WORDS):
            return owner, table, [span, *owner_spans]

    candidates = []
    for pattern in (
        re.compile(r"\b(?P<token>[A-Z][A-Z0-9_]*)\s+(?:TABLE|TABLO[A-Z]*)\b"),
        re.compile(r"\b(?:TABLE|TABLO[A-Z]*)\s+(?:(?:THE|A|AN|BIR|BU|SU)\s+)?(?P<token>\S+)"),
    ):
        for match in pattern.finditer(text):
            span = match.span("token")
            owner, table = _token_ref(match.group("token"))
            if available(span) and table and table not in _FUNCTION_WORDS:
                candidates.append((span[0], owner or stated_owner, table, span))
    if candidates:
        _start, owner, table, span = min(candidates, key=lambda candidate: candidate[0])
        return owner, table, [span, *owner_spans]
    return stated_owner, None, owner_spans


def _hint_source(raw: dict) -> tuple[str | None, str | None]:
    owner, table = _table_ref(raw.get("source_table") or raw.get("src_table"))
    return owner or _identifier(raw.get("source_schema") or raw.get("src_schema")), table


def _hint_target(raw: dict) -> tuple[str | None, str | None]:
    owner, table = _table_ref(raw.get("target_table") or raw.get("tgt_table"))
    return owner or _identifier(raw.get("target_schema") or raw.get("tgt_owner")), table


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
    stated: set[str] = set()
    explicit_owner, explicit_table, target_spans = _explicit_target(text)
    user_source_owner, user_source_table, source_spans = _explicit_source(text, target_spans)
    identifier_free = _masked(text, [*target_spans, *source_spans])
    keys, key_spans = _keys(identifier_free)
    intent = _masked(identifier_free, key_spans)
    layer = _layer(intent)
    load_type = _load_type(intent)
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
