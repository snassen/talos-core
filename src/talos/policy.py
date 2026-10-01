"""The routing policy: how Jev's probabilities become decisions. Talos's, not the adapter's.

docs/enrichment-plan.md §1 and §4: Jev does not pick labels itself; it returns a probability for
every choice of a `choice` question and a score from 0 to 1 for every `noul` statement.
Deterministic code turns those into decisions, and that code is versioned policy that lives
here, apart from the client (talos.jev), so the evaluation can sweep it without calling Jev
again.

- **One-value field** (origin, type, topic, value): decided when the top probability is at
  least `one_min` (0.70) and it leads the runner-up by at least `one_margin` (0.15), the
  thresholds of the Codex classifier. Otherwise it is unresolved; the top value is still kept
  as the candidate.
- **Many-value field as statements** (ask and route, question templates v1): every statement
  scored at least `many_min` (0.5) is selected. The field is always decided (an empty
  selection is the answer "none"). Its confidence is the least certain statement's certainty,
  max(s, 1 − s): the set is only as sure as its closest call.
- **Many-value field as one choice** (ask and route, question templates v2): one `choice`
  with a `none` option, plus side statements (ask: `deadline`). The choice is routed like a
  one-value field; selected = [top] when top is not `none` and it is decided, otherwise []
  (none). A side statement is added when it scores at least `many_min` *and* the choice
  selected something: a deadline without an ask is no ask. Confidence and margin are the
  choice's. The first evaluation showed why: asked one statement per value, Jev said yes to
  almost every value (ask about 25% precise, route about 35%).
- **Cross-field gates** (templates v2, after routing; see GATES): the ask is forced to none
  unless Jev's origin (its top value, decided or not) is a person, a person via a system or a
  list; the route is forced to none when the origin is an automatic reply, or when Jev's topic
  is not a Work/ topic. A gate only counts when it changed something (the field had a
  selection); which gates fired is returned beside the decisions.

Change a threshold's default, the rule itself, or a gate → bump VERSION. A run records the
policy it was routed with; the report can re-route stored probabilities under any other Policy.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace

VERSION = 2

NONE = "none"
# Question templates v2 (talos.jev): the many-value fields asked as one choice with `none`, and
# the statements asked beside the choice.
CHOICE_MANY = {"ask": ("deadline",), "route": ()}
# The gates, by name: (field forced to none, why).
ASK_ORIGINS = frozenset({"person", "person_via_system", "list"})
WORK_TOPIC = "Work/"
GATES = {
    "ask:origin": "ask forced to none: origin is not person, person_via_system or list",
    "route:auto_reply": "route forced to none: origin is auto_reply",
    "route:topic": "route forced to none: topic is not a Work/ topic",
}


@dataclass(frozen=True)
class Policy:
    one_min: float = 0.70
    one_margin: float = 0.15
    many_min: float = 0.5
    version: int = VERSION

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def name(self) -> str:
        return f"p≥{self.one_min:.2f} m≥{self.one_margin:.2f} s≥{self.many_min:.2f} (v{self.version})"


DEFAULT = Policy()


@dataclass(frozen=True)
class Decision:
    """One field of one case after routing.

    top: the best value (one-value, or a choice's top, `none` included) or the best-scored
    statement (many-value as statements), None if none.
    selected: the decided values: [top] for a decided one-value field, [] when unresolved; the
    statements at or over the threshold for a many-value field; [top] (+ side statements) for a
    many-value choice whose top is decided and not none.
    confidence: the top probability (one-value, choice) or the least certain statement's certainty.
    margin: top minus runner-up (one-value, choice)."""
    field: str
    top: str | None
    selected: tuple[str, ...]
    scores: dict
    confidence: float | None
    margin: float | None
    decided: bool


def route_one(field: str, probabilities: dict[str, float], policy: Policy = DEFAULT) -> Decision:
    ranked = sorted(((float(p), v) for v, p in probabilities.items()), key=lambda x: (-x[0], x[1]))
    if not ranked:
        return Decision(field, None, (), {}, None, None, False)
    top_p, top = ranked[0]
    runner = ranked[1][0] if len(ranked) > 1 else 0.0
    margin = round(top_p - runner, 6)
    decided = top_p >= policy.one_min - 1e-9 and margin >= policy.one_margin - 1e-9
    return Decision(field, top, (top,) if decided else (), dict(probabilities), top_p, margin, decided)


def route_many(field: str, scores: dict[str, float], policy: Policy = DEFAULT) -> Decision:
    if not scores:
        return Decision(field, None, (), {}, None, None, True)
    items = [(v, float(s)) for v, s in scores.items()]
    selected = tuple(v for v, s in items if s >= policy.many_min - 1e-9)
    top = max(items, key=lambda x: (x[1], x[0]))[0] if items else None
    confidence = round(min(max(s, 1 - s) for _, s in items), 6)
    return Decision(field, top, selected, dict(scores), confidence, None, True)


def route_choice_many(field: str, scores: dict[str, float], policy: Policy = DEFAULT,
                      statements: tuple[str, ...] = ()) -> Decision:
    """A many-value field asked as one choice with `none` (templates v2). scores holds the
    choice's probabilities and, under their own names, the side statements' scores."""
    choice = {v: p for v, p in scores.items() if v not in statements}
    one = route_one(field, choice, policy)
    selected = (one.top,) if one.decided and one.top != NONE else ()
    if selected:
        selected += tuple(s for s in statements if float(scores.get(s, 0.0)) >= policy.many_min - 1e-9)
    return Decision(field, one.top, selected, dict(scores), one.confidence, one.margin, one.decided)


def route(field: str, many: bool, scores: dict[str, float], policy: Policy = DEFAULT) -> Decision:
    return route_many(field, scores, policy) if many else route_one(field, scores, policy)


def gate(decisions: dict[str, Decision]) -> tuple[dict[str, Decision], dict[str, list[str]]]:
    """The cross-field gates (templates v2), applied to routed decisions: the gated decisions,
    and per field the gates that changed it (only fields that had a selection are changed)."""
    origin = decisions.get("origin")
    topic = decisions.get("topic")
    o = origin.top if origin else None
    t = topic.top if topic else None
    out = dict(decisions)
    fired: dict[str, list[str]] = {}
    ask = decisions.get("ask")
    if ask is not None and ask.selected and o not in ASK_ORIGINS:
        fired["ask"] = ["ask:origin"]
    r = decisions.get("route")
    if r is not None and r.selected:
        why = []
        if o == "auto_reply":
            why.append("route:auto_reply")
        if not (t or "").startswith(WORK_TOPIC):
            why.append("route:topic")
        if why:
            fired["route"] = why
    for f in fired:
        out[f] = replace(out[f], selected=())
    return out, fired


def route_case(scores: dict[str, dict[str, float]], policy: Policy = DEFAULT, *,
               template: int) -> tuple[dict[str, Decision], dict[str, list[str]]]:
    """Every field of one case: {field: scores} → ({field: Decision}, {field: gates fired}).

    template 1 (ask and route as statements): each field on its own, no gates.
    template 2 (ask and route as one choice with none): routed, then gated."""
    out: dict[str, Decision] = {}
    for f, s in scores.items():
        if f in CHOICE_MANY and template >= 2:
            out[f] = route_choice_many(f, s, policy, CHOICE_MANY[f])
        else:
            out[f] = route(f, f in CHOICE_MANY, s, policy)
    if template < 2:
        return out, {}
    return gate(out)
