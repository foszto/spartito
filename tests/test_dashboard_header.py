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
)  # fmt: skip

PRELUDE = r"""
let lastEvent = 0, toolGroup = null, codeShown = null, detailsOpen = false;
function stopAgent() {}
const detailGroups = {}, saved = [];
store = (key, value) => saved.push([key, JSON.parse(JSON.stringify(value))]);
const docListeners = new Set();
document.addEventListener = (type, f) => docListeners.add(f);
document.removeEventListener = (type, f) => docListeners.delete(f);
const press = key => { for (const f of [...docListeners]) f({ key, preventDefault() {} }); };
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
  inHeader: top().querySelectorAll('[data-setup=cap:chrome]').length, label: group.children[0].textContent,
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
out({ chips, header: shownText(top()), usage, runs, group: $('details-usage').children[0].textContent,
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


def test_the_open_section_and_its_groups_survive_a_poll_and_a_rebuilt_view() -> None:
    result = run_page(
        """
buildView(); renderHeader(agent());
const state0 = () => ({ hidden: $('details').hidden, expanded: $('detailsButton').getAttribute('aria-expanded'),
  about: $('details-about').open, usage: $('details-usage').open });
const before = { ...state0(), controls: $('detailsButton').getAttribute('aria-controls'),
  name: $('detailsButton').textContent, groups: $('details').children.map(g => g.dataset.group) };
$('detailsButton').onclick();
const opened = state0();
$('details-about').open = false; $('details-about').ontoggle();
renderHeader(agent()); renderHeader(agent({ status: 'running' }));
const polled = state0();
buildView(); renderHeader(agent());
const rebuilt = state0();
$('detailsButton').onclick();
out({ before, opened, polled, rebuilt, closed: state0(), saved: saved.at(-1),
  changes: [$('details-changes').hidden, (renderHeader(agent({ changes: ' M login.py' })), $('details-changes').hidden)] });
"""
    )
    assert result["before"] == {
        "hidden": True,
        "expanded": "false",
        "about": True,
        "usage": True,
        "controls": "details",
        "name": "Settings & usage",
        "groups": ["capabilities", "usage", "about", "changes"],
    }
    assert result["opened"] == {"hidden": False, "expanded": "true", "about": True, "usage": True}
    assert result["polled"] == {"hidden": False, "expanded": "true", "about": False, "usage": True}
    assert result["rebuilt"] == {"hidden": False, "expanded": "true", "about": False, "usage": True}
    assert result["closed"] == {"hidden": True, "expanded": "false", "about": False, "usage": True}
    assert result["saved"] == ["orch-detail-groups", {"about": False}]
    assert result["changes"] == [True, False]


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
