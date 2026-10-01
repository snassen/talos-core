"""The design system holds together (docs/design.md): every view is reachable from a place, the first-paint
script knows every Appearance setting, style.css names tokens instead of raw values, and every theme stays
readable. These read the static files; nothing here needs a browser."""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "talos" / "web" / "static"
APP = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")


def block(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i:src.index(end, i)]


def views() -> set[str]:
    return set(re.findall(r"'(\w+)'", block(APP, "const VIEWS = [", "];")))


def tab_views() -> set[str]:
    return {v for v in re.findall(r"\['(\w+)(?::\w+)?', '", block(APP, "const HUBS = [", "HUBS.forEach"))}


def luminance(hex_: str) -> float:
    c = [int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a: str, b: str) -> float:
    x, y = sorted([luminance(a), luminance(b)], reverse=True)
    return (x + 0.05) / (y + 0.05)


def test_every_tab_names_a_view_that_renders():
    render = block(APP, "const RENDER = {", "};")
    for v in tab_views():
        assert v in views(), f"the tab {v} is not in VIEWS"
        assert re.search(rf"\b{v}: view\w+", render), f"the tab {v} has no view in RENDER"


def test_every_view_is_reachable_from_a_place():
    # the check of the answer key opens from the answer key; the release notes from the version in the rail
    assert views() - tab_views() == {"goldcheck", "releases"}


def test_the_first_paint_script_applies_every_appearance_setting():
    keys = re.findall(r"(\w+): '", block(APP, "const LOOK_DEFAULT = {", "};"))
    painted = re.findall(r"'(\w+)'", re.search(r"\[([^\]]*)\]\.forEach", INDEX).group(1))
    assert keys[0] == "mode" and painted == keys[1:], "index.html and LOOK_DEFAULT name different settings"


def test_the_stylesheet_uses_the_type_scale_and_names_no_raw_white():
    rules = CSS[CSS.index("*{box-sizing:border-box}"):]
    sizes = re.findall(r"([^{}]*)\{[^{}]*font-size:[1-9]", rules)
    # only the two SVG axis labels keep a pixel size (they scale with their drawing)
    assert all(re.search(r"text", s.split("\n")[-1]) for s in sizes), sizes
    whites = [ln for ln in rules.splitlines() if re.search(r"#fff\b|#ffffff\b", ln)]
    assert all(".mframe" in ln for ln in whites), whites  # mail is written for a white page


def test_every_theme_has_a_tile_a_css_block_and_readable_text():
    presets = re.findall(r"\['(\w+)', '[A-Z]", block(APP, "const PRESETS = [", "];"))
    assert presets[0] == "talos" and len(presets) == 6
    base = block(CSS, ":root{", "\n}")
    pair = lambda src, tok: re.search(rf"--{tok}:light-dark\((#[0-9a-f]{{6}}),(#[0-9a-f]{{6}})\)", src).groups()
    for p in presets:
        src = base if p == "talos" else block(CSS, f':root[data-preset="{p}"]{{', "}")
        for light, dark in [(pair(src, "text-muted"), pair(src, "plane")), (pair(src, "text-primary"), pair(src, "plane"))]:
            assert contrast(light[0], dark[0]) >= 4.5, f"{p}, light: {light[0]} on {dark[0]}"
            assert contrast(light[1], dark[1]) >= 4.5, f"{p}, dark: {light[1]} on {dark[1]}"
    for a in re.findall(r"\['(\w+)', '[A-Z]\w+', '#", block(APP, "const ACCENTS = [", "];")):
        assert f':root[data-accent="{a}"]' in CSS, a


def test_every_account_has_a_letter():
    # The page names no account: it learns each one's name, letter and colour from /api/owner.
    from talos import accounts
    assert "const ACCOUNT_COLOR = {}, ACCOUNT_NAME = {}, ACCOUNT_LETTER = {}" in APP
    assert "api('/api/owner')" in block(APP, "async function loadOwner(", "\n}")
    shown = accounts.ui()
    assert [a["id"] for a in shown] == [a["id"] for a in accounts.ACCOUNTS]
    for a in shown:
        assert len(a["letter"]) == 1 and a["letter"].isupper() and a["name"], a
        assert f"--series-{a['color']}:" in CSS, a      # a colour the themes define
    assert {a["id"]: (a["letter"], a["color"]) for a in shown}["local"] == ("I", "other")  # its ui settings


def test_the_beacon_is_red_for_overdue_work_or_a_service_down_amber_for_important_mail_and_calm_otherwise(tmp_path):
    import json, shutil, subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    fn = block(APP, "function beaconLevel(", "\nconst BEACON = ")
    run = fn + """
const svc = s => ({services: [{status: 'up'}, {status: s}]});
console.log(JSON.stringify([
  beaconLevel([], [], svc('up')),
  beaconLevel([{reasons: ['inbox']}, {reasons: ['blocked']}], [{level: 'medium'}], svc('up')),
  beaconLevel([], [{level: 'high'}, {level: 'high'}], svc('up')),
  beaconLevel([{reasons: ['review']}], [], svc('late')),
  beaconLevel([{reasons: ['overdue', 'focus']}], [{level: 'high'}], svc('up')),
  beaconLevel([], [], svc('down')),
  beaconLevel(null, null, null),
]));"""
    (tmp_path / "b.js").write_text(run, encoding="utf-8")
    calm, quiet, mail, soft, late, down, nothing = json.loads(subprocess.run([node, str(tmp_path / "b.js")], capture_output=True, text=True, check=True).stdout)
    assert calm == {"level": "calm", "why": []} and quiet["level"] == "calm" and nothing["level"] == "calm"
    assert mail == {"level": "attn", "why": ["2 important mails today"]}
    assert soft["level"] == "attn" and soft["why"] == ["1 review due", "1 service late"]
    assert late["level"] == "alarm" and late["why"][0] == "1 work item overdue"
    assert down == {"level": "alarm", "why": ["1 service down"]}


def test_every_message_shortcut_is_one_letter_used_once_and_shown_on_its_button():
    keys = dict(re.findall(r"(\w+): '(\w)'", block(APP, "const MESSAGE_KEYS = {", "};")))
    assert len(set(keys.values())) == len(keys), "two actions share a shortcut letter"
    for action in keys:
        assert f"MESSAGE_KEYS.{action}" in APP, f"the {action} shortcut is not on any button"
    # the handler never fires while something waits for a confirmation to send or post (promise 1)
    handler = block(APP, "const MESSAGE_KEYS = {", "\nfunction header(")
    assert ".tconfirm" in handler and "dialog[open]" in handler and "'INPUT', 'TEXTAREA', 'SELECT'" in handler


def test_the_places_have_number_keys_that_stay_out_of_fields_and_the_views_with_keys_of_their_own():
    hubs = re.findall(r"\{id: '(\w+)', label:", block(APP, "const HUBS = [", "HUBS.forEach"))
    assert len(hubs) <= 9, "more places than number keys"
    handler = block(APP, "const placeKey = hb =>", "\nfunction renderRail")
    assert "['gold', 'goldcheck', 'discovery', 'studio']" in handler and "'INPUT', 'TEXTAREA', 'SELECT'" in handler
    assert "dialog[open]" in handler and ".tconfirm" in handler and "menuEl" in handler


def test_fetching_ahead_never_asks_for_a_message_a_conversation_an_attachment_a_search_or_a_sync(tmp_path):
    import json, shutil, subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    rx = re.search(r"^const NO_PREFETCH = (/.*/);$", APP, re.M).group(1)
    never = ["/api/messages/12", "/api/threads/7?around=3", "/api/attachments/5", "/api/messages?q=faktura&limit=100",
             "/api/teams/refresh", "/api/calendar/sync", "/api/discover?refresh=1", "/api/drafts"]
    fine = ["/api/messages?direction=in&medium=email&offset=0&limit=100", "/api/facets?medium=teams", "/api/work?status=inbox",
            "/api/calendar?start=2026-09-28&end=2026-10-02", "/api/space"]
    (tmp_path / "p.js").write_text(f"const R = {rx}; console.log(JSON.stringify({json.dumps(never + fine)}.map(p => R.test(p))));")
    got = json.loads(subprocess.run([node, str(tmp_path / "p.js")], capture_output=True, text=True, check=True).stdout)
    assert got == [True] * len(never) + [False] * len(fine)
