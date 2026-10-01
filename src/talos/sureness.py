"""How sure a value is, in words: seven named spans of confidence (docs/studio.md).

A value's confidence is a number between 0 and 1 (Jev's probability, or the calibrated one a
Studio lift gives). Named spans make it readable and filterable:

| Level     | From  | What it means                                                     |
|-----------|-------|-------------------------------------------------------------------|
| Fact      | 1.00  | You decided it.                                                   |
| Certain   | 0.95  | A rule set it, or Jev (or a lift) is at least 95% sure.           |
| Confident | 0.85  | Right almost every time.                                          |
| Likely    | 0.70  | Right most of the time: Talos accepts exact values from here.     |
| Maybe     | 0.50  | As often right as not.                                            |
| Doubtful  | 0.30  | More often wrong than right.                                      |
| Guess     | 0.00  | A shot in the dark.                                               |

The source decides where a value starts: the owner's own (human) is always Fact; a rule's (and the
pre-pass's) is Certain; an import is Likely; a model value is placed by its confidence (a model
value with none, as the answer-key propagation writes them, counts as Likely).

The same spans filter Mail (`sure=` beside the value filters, search.messages_sql) and head the
Studio's calibration. level_sql() gives the level in SQL, so both sides of the wire agree.
"""

from __future__ import annotations

# (key, label, lower bound, meaning), surest first.
LEVELS = (
    ("fact", "Fact", 1.00, "You decided it."),
    ("certain", "Certain", 0.95, "A rule set it, or Jev is at least 95% sure."),
    ("confident", "Confident", 0.85, "Right almost every time."),
    ("likely", "Likely", 0.70, "Right most of the time: Talos accepts exact values from here."),
    ("maybe", "Maybe", 0.50, "As often right as not."),
    ("doubtful", "Doubtful", 0.30, "More often wrong than right."),
    ("guess", "Guess", 0.00, "A shot in the dark."),
)
KEYS = tuple(k for k, *_ in LEVELS)
FLOOR = {k: lo for k, _, lo, _ in LEVELS}
# What a source counts as, when its own confidence does not say.
SOURCE_CONFIDENCE = {"human": 1.0, "rule": 0.97, "import": 0.75}
MODEL_WITHOUT_CONFIDENCE = 0.75


class SurenessError(ValueError):
    pass


def check(level: str | None) -> str | None:
    if level in (None, ""):
        return None
    if level not in FLOOR:
        raise SurenessError(f"a level is one of {', '.join(KEYS)}")
    return level


def confidence_of(source_kind: str | None, confidence: float | None) -> float:
    """The number a value is placed by: the owner's are 1.0, a rule's 0.97, a model's its own."""
    if source_kind in SOURCE_CONFIDENCE:
        return SOURCE_CONFIDENCE[source_kind]
    return MODEL_WITHOUT_CONFIDENCE if confidence is None else float(confidence)


def level_of(source_kind: str | None, confidence: float | None) -> str:
    """The level of a value: Fact only for the owner's own, then by its confidence."""
    if source_kind == "human":
        return "fact"
    c = confidence_of(source_kind, confidence)
    for k, _, lo, _ in LEVELS[1:]:
        if c >= lo - 1e-9:
            return k
    return "guess"


def at_least(level: str, floor: str) -> bool:
    """Is level as sure as floor, or surer?"""
    return KEYS.index(level) <= KEYS.index(floor)


def confidence_sql(source: str, conf: str) -> str:
    """SQL for confidence_of over a source kind and a confidence column."""
    return (f"(case {source} when 'human' then 1.0 when 'rule' then {SOURCE_CONFIDENCE['rule']}"
            f" when 'import' then {SOURCE_CONFIDENCE['import']}"
            f" else coalesce({conf}, {MODEL_WITHOUT_CONFIDENCE}) end)")


def level_sql(source: str, conf: str) -> str:
    """SQL for level_of: the level key of a value, from its source kind and confidence columns."""
    c = confidence_sql(source, conf)
    parts = " ".join(f"when {c} >= {lo - 1e-9} then '{k}'" for k, _, lo, _ in LEVELS[1:])
    return f"(case when {source} = 'human' then 'fact' {parts} else 'guess' end)"


def floor_sql(floor: str, source: str, conf: str) -> str:
    """SQL, true when a value (source kind, confidence) is at least as sure as floor."""
    check(floor)
    if floor == "fact":
        return f"({source} = 'human')"
    return f"({confidence_sql(source, conf)} >= {FLOOR[floor] - 1e-9})"


def levels() -> list[dict]:
    """The levels for the UI: key, label, lower bound, meaning."""
    return [{"key": k, "label": label, "from": lo, "meaning": text} for k, label, lo, text in LEVELS]
