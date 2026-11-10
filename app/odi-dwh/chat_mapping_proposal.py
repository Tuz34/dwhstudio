"""Deterministic, reviewable proposal fields for AI Studio mapping cards.

The LLM may identify a mapping request and suggest source identifiers. Load
strategy, keys, and destination layer come only from the user's own message.
"""

from __future__ import annotations

import re
import unicodedata

# Same Oracle/ODI identifier contract as MappingGenerator._validate_identifier.
_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_LAYERS = {"ODS": "ODS", "EDW": "EDW", "DWH": "DWH", "DM": "DM", "DATAMART": "DM", "DATA MART": "DM"}
_LAYER_WORD = re.compile(r"(?<![A-Z0-9_.])(?:DATA\s+MART|DATAMART|EDW|DWH|ODS|DM)(?![A-Z0-9_.])")
_KEY_MARKER = re.compile(
    r"\b(?:PRIMARY\s+KEY|NATURAL\s+KEY|KEY\s+COLUMNS?|KEYS?|PK|"
    r"ANAHTAR(?:\s+KOLON(?:U|LARI)?)?|ON)\b"
)
_TARGET_MARKER = re.compile(
    r"\b(?:TARGET|HEDEF)\s+(?:(?:TABLE|TABLO(?:SU|SUNA|SUNU)?)(?:\s+(?:NAME|ADI))?\s+)?"
    r"(?:(?:AS|NAMED|ADI)\s+|[:=]\s*)?(\S+)"
    r"|\b(?:TO|INTO)\s+(?:(?:THE|A)\s+)?(?:TABLE\s+)?(\S+)"
)
_SOURCE_MARKER = re.compile(r"\b(?:SOURCE|FROM|KAYNAK|TABLE|TABLO(?:SU|SUNU)?)\s+([^\s]+)")


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


def _load_type(text: str) -> str | None:
    patterns = {
        "type2": r"\b(?:SCD\s*[-_]?\s*2|TYPE\s*[-_]?\s*2|TIP\s*[-_]?\s*2|TARIHCELI|HISTORY|HISTORICAL)\b",
        "type1": r"\b(?:SCD\s*[-_]?\s*1|TYPE\s*[-_]?\s*1|TIP\s*[-_]?\s*1|TARIHCESIZ)\b",
        "truncate_load": r"\b(?:TRUNCATE|FULL\s+RELOAD|FULL\s+LOAD|TAM\s+YUKLEME|TAM\s+YENILE(?:ME)?)\b",
        "insert_update": r"\b(?:INSERT\s*[/+&-]\s*UPDATE|INSERT\s+UPDATE|UPSERT|MERGE|EKLE\s*[/+&-]\s*GUNCELLE|EKLE\s+VE\s+GUNCELLE|VARSA\s+GUNCELLE\s+YOKSA\s+EKLE)\b",
    }
    found = {kind for kind, pattern in patterns.items() if re.search(pattern, text)}
    if "type2" in found or "type1" in found:
        return next(iter(found & {"type1", "type2"})) if len(found & {"type1", "type2"}) == 1 else None
    return next(iter(found)) if len(found) == 1 else None


def _key_tail(tail: str) -> list[str]:
    tokens = tail.lstrip(" :=").split()
    if tokens and tokens[0] in {"IS", "ARE", "OLARAK", "KOLONU", "COLUMN", "COLUMNS"}:
        tokens.pop(0)
    keys: list[str] = []
    expect_name = True
    for token in tokens[:16]:
        if not expect_name and token in {"AND", "VE"}:
            expect_name = True
            continue
        if not expect_name:
            break
        parts = token.rstrip(",").split(",")
        names = [_identifier(part) for part in parts]
        if not parts or not all(names) or any(name in {"TO", "INTO", "AS", "FOR", "WITH", "ODS", "EDW", "DWH", "DM", "KATMANINA", "KATMANINDA"} for name in names):
            break
        keys.extend(name for name in names if name not in keys)
        expect_name = token.endswith(",")
    return keys[:8]


def _keys(text: str, load_type: str | None) -> list[str]:
    for marker in _KEY_MARKER.finditer(text):
        keys = _key_tail(text[marker.end():])
        if not keys:
            continue
        if marker.group() == "ON" and not (
            load_type or any(key == "ID" or key.endswith("_ID") for key in keys)
        ):
            continue
        return keys
    match = re.search(r"\b([A-Z][A-Z0-9_]*)\s+ANAHTARI\b", text)
    key = _identifier(match.group(1)) if match else None
    return [key] if key else []


def _explicit_target(text: str) -> tuple[str | None, str | None]:
    for match in _TARGET_MARKER.finditer(text):
        token = match.group(1) or match.group(2)
        if match.group(2) and re.match(r"\s+(?:LAYER|KATMAN(?:I|INA|INDA)?)\b", text[match.end():]):
            continue
        owner, table = _token_ref(token)
        if table and table not in {"ODS", "EDW", "DWH", "DM", "DATA", "DATAMART", "MART", "LAYER", "SCHEMA", "OWNER", "KATMANI", "KATMANINA", "KATMANINDA"}:
            return owner, table
    return None, None


def _explicit_source(text: str) -> tuple[str | None, str | None]:
    owner_match = re.search(r"\b(?:SOURCE|KAYNAK)\s+SCHEMA\s+(\S+)", text)
    stated_owner = _identifier(owner_match.group(1)) if owner_match else None
    for match in _SOURCE_MARKER.finditer(text):
        if re.search(r"\b(?:TARGET|HEDEF|TO|INTO)\s+$", text[max(0, match.start() - 15):match.start()]):
            continue
        owner, table = _token_ref(match.group(1))
        if table and table not in {"ODS", "EDW", "DWH", "DM", "LAYER", "KATMANINA", "SCHEMA", "OWNER", "TABLE", "TABLO"}:
            return owner or stated_owner, table
    # A qualified table in the user's message is sufficient source evidence
    # unless it is explicitly introduced as the target.
    for match in re.finditer(r"\S+", text):
        owner, table = _token_ref(match.group())
        before = text[max(0, match.start() - 24):match.start()]
        if owner and table and not re.search(r"\b(?:TARGET|HEDEF|TO|INTO)\s+(?:TABLE\s+)?$", before):
            return owner, table
    return stated_owner, None


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
    source_owner, source_table = _explicit_source(text)
    hint_owner, hint_table = _table_ref(raw.get("source_table") or raw.get("src_table"))
    source_table = source_table or hint_table
    source_owner = source_owner or _identifier(raw.get("source_schema") or raw.get("src_schema")) or hint_owner

    explicit_owner, explicit_table = _explicit_target(text)
    target_owner = explicit_owner or (
        ("DM" if layer == "DM" else "DWH") if layer
        else _identifier(raw.get("target_schema") or raw.get("tgt_owner"))
    )
    if explicit_table:
        target_table = explicit_table
    elif layer in {"ODS", "EDW", "DWH"} and source_owner and source_table:
        prefix = f"{layer}_" if layer != "DWH" else ""
        target_table = _identifier(f"{prefix}{source_owner}_{source_table}")
    elif layer:
        target_table = None
    else:
        # Without a recognized destination there is no safe naming rule.
        target_table = None

    proposal: dict = {"operation": "CREATE"}
    if source_owner:
        proposal.update(source_schema=source_owner, src_schema=source_owner)
    if source_table:
        proposal["source_table"] = source_table
        proposal["src_table"] = f"{source_owner}.{source_table}" if source_owner else source_table
        proposal["src_model"] = "ORACLE_SOURCE"
    if layer:
        proposal["target_layer"] = layer
        if layer != "DM":
            proposal["tgt_model"] = "ORACLE_ODS" if layer == "ODS" else "ORACLE_DWH"
    if target_owner:
        proposal.update(target_schema=target_owner, tgt_owner=target_owner)
    if target_table:
        proposal.update(target_table=target_table, tgt_table=target_table)
    if layer in {"ODS", "EDW", "DWH"} and source_owner and source_table:
        name = _identifier(f"MAP_{source_owner}_{source_table}_TO_{layer}")
        if name:
            proposal["mapping_name"] = name

    load_type = _load_type(text)
    if load_type:
        proposal["load_type"] = load_type
    keys = _keys(text, load_type)
    if keys:
        proposal["key_columns"] = keys
    return proposal
