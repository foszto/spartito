"""The agent view's header and its settings and usage section, run under node with the fake
DOM of the provider tests: the prompt cache hint behind an info button (hover, focus, click,
Escape), the capability lines in the section, tokens and cost under Usage instead of the
header, and what is open surviving a poll and a rebuilt view."""

import json
import shutil
import subprocess

import pytest
from test_dashboard_capabilities import NAMES as CAPABILITY_NAMES
from test_dashboard_capabilities import PRELUDE as CAPABILITY_PRELUDE
from test_dashboard_providers import HARNESS, page_functions

NAMES = (
    *CAPABILITY_NAMES, "buildView", "buildDetails", "showDetails", "buildCacheInfo", "renderHeader",
    "renderWelcome", "renderQueue", "shownProvider", "contextText", "tokenText", "DETAILS_NAME", "DETAIL_GROUPS", "SUGGESTIONS",
    "detailNames", "detailShown", "activeDetailTab", "detailTabKey", "selectDetailTab", "syncDetailTabs",
    "tabKeyMove", "detailAvailable",
)  # fmt: skip

PRELUDE = r"""
let lastEvent = 0, toolGroup = null, codeShown = null, detailsOpen = false, detailTab = 'capabilities';
function stopAgent() {}
const saved = [];
store = (key, value) => saved.push([key, JSON.parse(JSON.stringify(value))]);
const docListeners = new Set();
document.addEventListener = (type, f) => docListeners.add(f);
document.removeEventListener = (type, f) => docListeners.delete(f);
// Sends a key to the document's listeners: whether one of them prevented its default.
const press = key => {
  const event = { key, prevented: false, preventDefault() { this.prevented = true; } };
  for (const f of [...docListeners]) f(event);
  return event.prevented;
};
node('div', { id: 'view' });
// The text a user sees: hidden nodes left out.
const shownText = n => (n.hidden ? '' : n.text + n.children.map(shownText).join(''));
const isShown = n => { for (; n; n = n.parentNode) if (n.hidden) return false; return true; };
const HINT = 'Switching the model starts a fresh prompt cache; switching the effort may start a fresh prompt cache.';
const agent = (fields = {}) => ({ id: 'fix-login-3f2a', provider: 'claude', status: 'done', role: 'worker',
  session_id: 'session-1', cache_hint: HINT, effective: {}, inherited: {}, model_override: null, effort_override: null,
  last_run: null, capabilities: { on: [], items: [item()], error: null }, context: null, branch: 'orch/fix-login-3f2a',
  tokens: { in: 1000, cache_read: 300000, cache_write: 24500, out: 9300 }, cost_usd: 0.93, project: 'my-app',
  parent_id: null, cwd: '~/src/my-app', runs: 2, queued_runs: 0, children_running: 0, queue: [], changes: '',
  recent_runs: [{ id: 7, kind: 'message', status: 'done', ended_at: '2026-11-19T08:41:00', model: 'opus',
    tokens: { in: 500, cache_read: 150000, cache_write: 12000, out: 4600 }, cost_usd: 0.4612 }], ...fields });
const top = () => $('view').querySelector('.top-bar');
const chromeSelect = () => $('setupCaps').querySelector('[data-setup=cap:chrome]');
selected = 'fix-login-3f2a';
"""


def run_page(code: str) -> object:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    page = page_functions(*NAMES)
    script = (
        f"{HARNESS}\n{CAPABILITY_PRELUDE}\n{PRELUDE}\n{page}\n"
        f"(async () => {{\n{code}\n}})().catch(e => {{ console.error(e); process.exit(1); }});"
    )
    done = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_cache_hint_is_behind_an_info_button() -> None:
    result = run_page(
        """
buildView(); renderHeader(agent());
const open = $('cacheInfoButton'), tip = $('cacheTip');
const view = () => ({ tip: !tip.hidden, expanded: open.getAttribute('aria-expanded') });
const header = { text: shownText(top()).includes('prompt cache'), note: $('setupNote').hidden,
  offered: isShown(open), tag: open.tag, label: open.getAttribute('aria-label'),
  describedBy: open.getAttribute('aria-describedby'), controls: open.getAttribute('aria-controls'),
  role: tip.getAttribute('role'), hint: tip.textContent, closed: view() };
open.onfocus(); const focused = view();
press('Escape'); const escaped = { ...view(), listening: docListeners.size };
open.onclick(); const clicked = view();
open.onblur(); const blurred = view();
$('cacheInfo').onmouseenter(); const hovered = view();
press('Escape'); const hoverEscaped = view();
$('cacheInfo').onmouseenter(); $('cacheInfo').onmouseleave(); const left = view();
renderHeader(agent({ session_id: null })); const noSession = isShown(open);
renderHeader(agent({ cache_hint: null })); const noHint = isShown(open);
renderHeader(agent({ status: 'running' }));
out({ header, focused, escaped, clicked, blurred, hovered, hoverEscaped, left, noSession, noHint,
  running: shownText($('setupNote')), back: isShown(open) });
"""
    )
    assert result["header"] == {
        "text": False,
        "note": True,
        "offered": True,
        "tag": "button",
        "label": "About switching model or effort",
        "describedBy": "cacheTip",
        "controls": "cacheTip",
        "role": "tooltip",
        "hint": "Switching the model starts a fresh prompt cache; switching the effort may start a fresh prompt cache.",
        "closed": {"tip": False, "expanded": "false"},
    }
    shown, hidden = {"tip": True, "expanded": "true"}, {"tip": False, "expanded": "false"}
    assert result["focused"] == shown
    assert result["escaped"] == {**hidden, "listening": 0}
    assert result["clicked"] == shown
    assert result["blurred"] == hidden
    assert result["hovered"] == shown
    assert result["hoverEscaped"] == hidden
    assert result["left"] == hidden
    assert result["noSession"] is False
    assert result["noHint"] is False
    # the other notes stay visible under the controls
    assert (
        result["running"] == "A change applies from the next turn; the running one is not changed."
    )
    assert result["back"] is True


def test_the_capability_lines_are_in_the_section_and_survive_a_poll_while_focused() -> None:
    result = run_page(
        """
buildView(); renderHeader(agent());
const select = chromeSelect(), group = $('details-capabilities');
const placed = { inSection: $('details').contains(select), inGroup: group.contains($('setupCaps')),
  inHeader: top().querySelectorAll('[data-setup=cap:chrome]').length, label: $('details-tab-capabilities').textContent,
  lines: $('setupCaps').children.length };
document.activeElement = select;
const seen = watch(select.parentNode);
renderHeader(agent({ capabilities: { on: ['chrome'], items: [item({ state: 'on', source: 'global' })], error: null } }));
const polled = { same: chromeSelect() === select, detached: seen.detached };
document.activeElement = null;
renderHeader(agent({ capabilities: { on: ['chrome'], items: [item({ state: 'on', source: 'global' })], error: null } }));
out({ placed, polled, redrawn: chromeSelect() !== select, text: shownText($('setupCaps')) });
"""
    )
    assert result["placed"] == {
        "inSection": True,
        "inGroup": True,
        "inHeader": 0,
        "label": "Capabilities",
        "lines": 1,
    }
    assert result["polled"] == {"same": True, "detached": 0}
    assert result["redrawn"] is True
    assert result["text"] == "Claude in Chromeinherit (on)onoffonfrom global"


def test_tokens_and_cost_are_under_usage_not_in_the_header() -> None:
    result = run_page(
        """
buildView(); renderHeader(agent());
const chips = $('view').querySelectorAll('.chip').map(texts);
const usage = $('usage').children.map(texts), runs = $('runs').querySelectorAll('tr').map(r => r.children.map(texts));
renderHeader(agent({ tokens: null, cost_usd: 0, recent_runs: [] }));
out({ chips, header: shownText(top()), usage, runs, group: $('details-tab-usage').textContent,
  empty: shownText($('usage')) });
"""
    )
    assert result["chips"] == ["worker", "context ?", "orch/fix-login-3f2a"]
    assert "$0.93" not in result["header"] and "in ·" not in result["header"]
    assert result["usage"] == ["Tokens", "325.5k in · 9.3k out", "Cost", "$0.93"]
    assert result["runs"] == [
        ["Run", "Status", "Ended", "Tokens", "Cost", "Model"],
        ["#7", "done", "08:41:00", "162.5k in · 4.6k out", "$0.4612", "opus"],
    ]
    assert result["group"] == "Usage"
    assert result["empty"] == "Nothing used yet."


def test_the_open_section_and_its_tabs_survive_a_poll_and_a_rebuilt_view() -> None:
    result = run_page(
        """
buildView(); renderHeader(agent());
const state0 = () => ({ hidden: $('details').hidden, expanded: $('detailsButton').getAttribute('aria-expanded'),
  about: $('details-tab-about').getAttribute('aria-selected'), usage: $('details-tab-usage').getAttribute('aria-selected'),
  active: detailTab, shown: detailNames().filter(n => !$(`details-${n}`).hidden) });
const before = { ...state0(), controls: $('detailsButton').getAttribute('aria-controls'),
  name: $('detailsButton').textContent, groups: $('details-tabs').children.map(t => t.getAttribute('aria-controls')),
  tablist: [$('details-tabs').getAttribute('role'), $('details').children.slice(1).map(g => g.getAttribute('role'))],
  panels: $('details').children.slice(1).map(g => [g.dataset.group, g.getAttribute('aria-labelledby')]) };
$('detailsButton').onclick();
const opened = state0();
$('details-tab-about').onclick();
renderHeader(agent()); renderHeader(agent({ status: 'running' }));
const polled = state0();
buildView(); renderHeader(agent());
const rebuilt = state0();
$('detailsButton').onclick();
out({ before, opened, polled, rebuilt, closed: state0(), saved: saved.at(-1),
  changes: [$('details-changes').hidden, $('details-tab-changes').hidden,
    (renderHeader(agent({ changes: ' M login.py' })), [$('details-changes').hidden, $('details-tab-changes').hidden])] });
"""
    )
    assert result["before"] == {
        "hidden": True,
        "expanded": "false",
        "about": "false",
        "usage": "false",
        "active": "capabilities",
        "shown": ["capabilities"],
        "controls": "details",
        "name": "Settings & usage",
        "groups": ["details-capabilities", "details-usage", "details-about", "details-changes"],
        "tablist": ["tablist", ["tabpanel", "tabpanel", "tabpanel", "tabpanel"]],
        "panels": [
            ["capabilities", "details-tab-capabilities"],
            ["usage", "details-tab-usage"],
            ["about", "details-tab-about"],
            ["changes", "details-tab-changes"],
        ],
    }
    assert result["opened"] == {
        "hidden": False,
        "expanded": "true",
        "about": "false",
        "usage": "false",
        "active": "capabilities",
        "shown": ["capabilities"],
    }
    polled = {
        "hidden": False,
        "expanded": "true",
        "about": "true",
        "usage": "false",
        "active": "about",
        "shown": ["about"],
    }
    assert result["polled"] == polled
    assert result["rebuilt"] == polled
    assert result["closed"] == {**polled, "hidden": True, "expanded": "false"}
    assert result["saved"] == ["orch-detail-tab", "about"]
    # changes cannot be shown: its tab and panel are hidden, and the shown group stays shown
    assert result["changes"] == [True, True, [True, False]]


def test_a_click_or_a_key_shows_exactly_one_panel_and_a_poll_keeps_it() -> None:
    result = run_page(
        """
buildView(); renderHeader(agent()); $('detailsButton').onclick();
const shown = () => detailNames().filter(n => !$(`details-${n}`).hidden);
const tab = (name, key) => { if (key) $('details-tabs').onkeydown({ key, preventDefault() {} }); else $(`details-tab-${name}`).onclick();
  return { tab: detailTab, shown: shown() }; };
const clicked = tab('usage'), back = tab('capabilities'), about = tab('about');
const right = tab(null, 'ArrowRight'), left = tab(null, 'ArrowLeft'), end = tab(null, 'End'), home = tab(null, 'Home');
renderHeader(agent()); renderHeader(agent({ status: 'running' }));
const polled = { tab: detailTab, shown: shown() };
// a group that cannot be shown is skipped, and its panel stays hidden
renderHeader(agent({ changes: '' }));
const withoutChanges = { shown: shown(), changesTab: $('details-tab-changes').hidden, changesPanel: $('details-changes').hidden };
out({ clicked, back, about, right, left, end, home, polled, withoutChanges });
"""
    )
    assert result["clicked"] == {"tab": "usage", "shown": ["usage"]}
    assert result["back"] == {"tab": "capabilities", "shown": ["capabilities"]}
    assert result["about"] == {"tab": "about", "shown": ["about"]}
    assert result["right"] == {
        "tab": "capabilities",
        "shown": ["capabilities"],
    }  # wraps from the last shown
    assert result["left"] == {"tab": "about", "shown": ["about"]}  # wraps from the first
    assert result["end"] == {"tab": "about", "shown": ["about"]}
    assert result["home"] == {"tab": "capabilities", "shown": ["capabilities"]}
    assert result["polled"] == {"tab": "capabilities", "shown": ["capabilities"]}
    assert result["withoutChanges"] == {
        "shown": ["capabilities"],
        "changesTab": True,
        "changesPanel": True,
    }


def test_the_tabs_are_one_tab_stop_and_move_with_the_arrow_keys() -> None:
    result = run_page(
        """
buildView(); renderHeader(agent());
const stops = () => $('details-tabs').children.map(t => [t.getAttribute('aria-selected'), t.tabIndex]);
const key = k => { const event = { key: k, prevented: false, preventDefault() { this.prevented = true; } };
  $('details-tabs').onkeydown(event); return [detailTab, event.prevented]; };
const start = stops();
const right = key('ArrowRight'), left = key('ArrowLeft'), wrapLeft = key('ArrowLeft');
const home = key('Home'), end = key('End'), wrapRight = key('ArrowRight');
const other = key('Enter');
out({ start, right, left, wrapLeft, home, end, wrapRight, other, stops: stops() });
"""
    )
    assert result["start"] == [["true", 0], ["false", -1], ["false", -1], ["false", -1]]
    assert result["right"] == ["usage", True]
    assert result["left"] == ["capabilities", True]
    assert result["wrapLeft"] == ["about", True]
    assert result["home"] == ["capabilities", True]
    assert result["end"] == ["about", True]
    assert result["wrapRight"] == ["capabilities", True]
    assert result["other"] == ["capabilities", False]
    assert result["stops"] == [["true", 0], ["false", -1], ["false", -1], ["false", -1]]


def test_the_maestro_before_its_first_message_offers_only_its_capabilities() -> None:
    result = run_page(
        """
selected = ORCH;
state.orchestrator_setup = { provider: 'claude', effective: {}, inherited: {}, model_override: null,
  effort_override: null, cache_hint: HINT, capabilities: { on: [], items: [item()], error: null } };
buildView(); renderHeader(null);
out({ button: $('detailsButton').hidden, caps: !$('setupCaps').hidden && $('details').contains($('setupCaps')),
  usage: $('details-usage').hidden, about: $('details-about').hidden, changes: $('details-changes').hidden,
  cache: $('cacheInfo').hidden });
"""
    )
    assert result == {
        "button": False,
        "caps": True,
        "usage": True,
        "about": True,
        "changes": True,
        "cache": True,
    }


def test_a_view_rebuilt_under_an_open_hint_leaves_escape_alone() -> None:
    result = run_page(
        """
buildView(); renderHeader(agent());
$('cacheInfo').onmouseenter();
const open = { tip: !$('cacheTip').hidden, listening: docListeners.size };
buildView(); renderHeader(agent());  // a poll rebuilds the view: no mouseleave, no blur
const later = press('Escape');
$('cacheInfo').onmouseenter();
const again = { tip: !$('cacheTip').hidden, taken: press('Escape'), tip2: !$('cacheTip').hidden };
out({ open, later, left: docListeners.size, again, after: docListeners.size, other: press('Escape') });
"""
    )
    assert result == {
        "open": {"tip": True, "listening": 1},
        "later": False,
        "left": 0,
        "again": {"tip": True, "taken": True, "tip2": False},
        "after": 0,
        "other": False,
    }
