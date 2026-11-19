"""The dashboard's capabilities, run under node with the fake DOM of the provider tests: the
settings box of each scope (rows, the request each choice sends, the role settings read-only),
the lines of the agent setup (the disabled "on" of a final role restriction, the next-turn hint,
a refusal), the new-agent dialog (only changed capabilities are sent), the badges of the agent
rows, and a page talking to a server without any of it."""

import json
import shutil
import subprocess

import pytest
from test_dashboard_providers import HARNESS, STATIC, page_functions

NAMES = (
    "TIER_INFO", "el", "button", "avatar", "choices", "options", "provider", "providerLabel", "canRun",
    "isUsable", "pickProvider", "providerTitle", "unavailableText", "modelLabel", "modelOptions",
    "providerChoices", "providerViews", "providersChanged", "providerHint", "inherited", "openAgentDialog", "renderSettings",
    "defaultProviderBox", "setupSlot", "setupBusy", "bumpSetup", "inherits", "settingLabel",
    "renderSetup", "buildProviderControl", "setupRequest", "describe", "markOwn", "markExtras",
    "buildControl", "triValue", "triState", "onOff", "capabilityLabel", "capabilityItems",
    "renderCapabilitySetup", "capabilityLine", "saveCapability", "capabilitiesBox", "capabilityRow",
    "isActive", "plural", "isIdle", "stateOf", "count", "tokens", "short",
    "contextWarn", "compactAtTitle", "contextTitle", "renderNav", "navItem", "setupTicket",
    "setupFresh", "placeChildren", "CAPABILITY_SETTINGS", "renderCacheInfo", "showCacheTip", "cacheTipKey",
    "renderSettingsTabs", "applySettingsTab", "selectSettingsTab", "tabKeyMove",
)  # fmt: skip

# What the code under test needs beyond the provider tests' harness, and invented data.
PRELUDE = r"""
let capabilityNote = null, navKey = '', stateTicket = null;
const ARCHIVE = 'archive:', COST = 'cost:', CONTEXT_WARN = 80;
const window = {};
function icon() { return new Node('svg'); }
Object.defineProperty(Node.prototype, 'style', { get() { return this._style ??= {}; } });
function store() {}
function openBoard() {} function archiveSafe() {} function removeProject() {} function openProjectDialog() {}
const setupCaps = node('div', { id: 'setupCaps' });
node('div', { id: 'nav' }); node('div', { id: 'summary' });
const capsField = data('div', 'capabilitiesField');
capsField.hidden = true;
capsField.append(data('div', 'capabilities'));
agentDialog.children[1].append(capsField);

const CHROME = { name: 'chrome', label: 'Claude in Chrome', description: 'Browser tools',
  reaches: 'your real browser, signed in as you', requires: [], providers: ['claude'], status: 'unknown',
  reason: 'Spartito cannot see the extension', default: false, global: null, roles: { reviewer: false },
  plugin: false, docs: 'docs/capabilities.md#chrome' };
state.capabilities = [CHROME];
state.spent = { tokens: null, cost_usd: 0 };
state.archived = [];
const settingsBox = () => $('settingsRows').children.find(b => b.children[0]?.textContent === 'Capabilities');
const radios = box => box.querySelectorAll('[name=capability-chrome]');
const radioView = box => radios(box).map(r => [r.parentNode.textContent, r.checked, r.disabled]);
const item = (fields = {}) => ({ name: 'chrome', label: 'Claude in Chrome', state: 'off', source: 'default',
  override: null, final: false, note: null, ...fields });
const agentSetup = (items, fields = {}) => ({ id: 'fix-login-3f2a', provider: 'claude', effective: {}, inherited: {},
  own: { model: null, effort: null }, session: true, running: false, cacheHint: null, resolved: null,
  role: 'worker', capabilities: { on: [], items, error: null }, ...fields });
const lines = () => setupCaps.children.map(n => n.textContent);
const capSelect = () => setupCaps.querySelector('[data-setup=cap:chrome]');
const FAKE = { ...CHROME, name: 'fake-tool', label: 'Fake tool', reaches: '', roles: {}, status: 'unknown',
  reason: 'not checked' };
// Counts the times a node is taken out of its parent (a focused one would lose the focus).
const watch = n => {
  let parent = n.parentNode; const seen = { detached: 0 };
  Object.defineProperty(n, 'parentNode', { get: () => parent, set: v => { if (!v) seen.detached++; parent = v; } });
  return seen;
};
// A later /api/state with `fields` for chrome, as refresh hands it on (stateTicket: when it started).
const pollState = (fields, ticket = setupTicket()) => {
  state.capabilities = [{ ...CHROME, ...fields }]; stateTicket = ticket; providersChanged();
};
"""


def run_page(code: str) -> object:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    page = page_functions(*NAMES)
    script = (
        f"{HARNESS}\n{PRELUDE}\n{page}\n"
        f"(async () => {{\n{code}\n}})().catch(e => {{ console.error(e); process.exit(1); }});"
    )
    done = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_global_box_lists_each_capability_and_sets_it_at_once() -> None:
    result = run_page(
        """
renderSettings();
const box = settingsBox(), row = box.querySelector('.cap-row');
const before = { rows: box.querySelectorAll('.cap-row').length, head: row.querySelector('.cap-head').textContent,
  reaches: row.querySelector('.cap-reaches').textContent, status: row.querySelector('.cap-status-line').textContent,
  statusTitle: row.querySelector('.cap-status-line').title, radios: radioView(box),
  inheritTitle: radios(box)[0].parentNode.title, roles: row.querySelectorAll('.cap-role').map(texts) };
replies.push({ capabilities: [{ ...CHROME, global: true }] });
radios(box)[1].onchange({ target: radios(box)[1] });
await settle();
const on = { request: posted.at(-1), radios: radioView(box), status: box.querySelector('.cap-status').textContent };
replies.push(new Error("unknown capability 'jira' (known: chrome)"));
radios(box)[0].onchange({ target: radios(box)[0] });
await settle();
out({ before, on, inherit: posted.at(-1), refused: box.querySelector('.cap-status').textContent,
  kept: radioView(box) });
"""
    )
    assert result["before"] == {
        "rows": 1,
        "head": "Claude in ChromeClaude",
        "reaches": "Reaches your real browser, signed in as you",
        "status": "Status: unknown (Spartito cannot see the extension)",
        "statusTitle": "Spartito cannot see the extension",
        "radios": [["inherit (off)", True, False], ["on", False, False], ["off", False, False]],
        "inheritTitle": "Takes the default: off",
        "roles": ["role reviewer: off (final)"],
    }
    assert result["on"] == {
        "request": {"path": "/api/capabilities", "body": {"name": "chrome", "state": True}},
        "radios": [["inherit (off)", False, False], ["on", True, False], ["off", False, False]],
        "status": "Claude in Chrome: on globally, from each agent’s next turn.",
    }
    assert result["inherit"] == {
        "path": "/api/capabilities",
        "body": {"name": "chrome", "state": None},
    }
    assert result["refused"] == "unknown capability 'jira' (known: chrome)"
    assert result["kept"] == [
        ["inherit (off)", False, False],
        ["on", True, False],
        ["off", False, False],
    ]


def test_a_projects_box_loads_its_listing_and_sets_it_for_the_project() -> None:
    result = run_page(
        """
state.settings.projects['my-app'] = {};
settingsDialog.querySelector('[name=scope]').value = 'my-app';
const listing = later();
replies.push(listing.promise);
renderSettings();
const box = settingsBox(), loading = box.textContent.includes('Loading…'), asked = posted.at(-1);
listing.answer({ capabilities: [{ ...CHROME, global: true, project: null, project_roles: { tester: true } }] });
await settle();
const view = { radios: radioView(box), title: radios(box)[0].parentNode.title,
  roles: box.querySelectorAll('.cap-role').map(texts),
  label: box.querySelector('.segmented').getAttribute('aria-label') };
replies.push({ capabilities: [{ ...CHROME, global: true, project: false, project_roles: { tester: true } }] });
radios(box)[2].onchange({ target: radios(box)[2] });
await settle();
out({ loading, asked, view, request: posted.at(-1), radios: radioView(box),
  status: box.querySelector('.cap-status').textContent });
"""
    )
    assert result["loading"] is True
    assert result["asked"] == {"path": "/api/capabilities?project=my-app"}
    assert result["view"] == {
        "radios": [["inherit (on)", True, False], ["on", False, False], ["off", False, False]],
        "title": "Takes the global setting: on",
        "roles": ["role reviewer: off (final)", "role tester (my-app): on"],
        "label": "Claude in Chrome for my-app",
    }
    assert result["request"] == {
        "path": "/api/capabilities",
        "body": {"name": "chrome", "state": False, "project": "my-app"},
    }
    assert result["radios"] == [
        ["inherit (on)", False, False],
        ["on", False, False],
        ["off", True, False],
    ]
    assert result["status"] == "Claude in Chrome: off for my-app, from each agent’s next turn."


def test_the_setup_shows_each_capability_with_its_own_choice() -> None:
    result = run_page(
        """
renderSetup(agentSetup([item(), item({ name: 'fake-tool', label: 'Fake tool', state: 'skipped', source: 'agent',
  override: true, note: 'codex does not support it' })]));
const plain = { hidden: setupCaps.hidden, lines: lines(),
  values: setupCaps.querySelectorAll('select').map(s => [s.value, s.options.map(o => [o.textContent, o.disabled])]) };
renderSetup(agentSetup([item({ source: 'role', final: true })]));
const final = { lines: lines(), on: capSelect().options[1].disabled, title: capSelect().title };
renderSetup(agentSetup([item({ state: 'skipped', source: 'agent', override: true, final: true,
  note: "off for role reviewer: the role's setting wins over the agent's" })], { role: 'reviewer' }));
out({ plain, final, own: lines() });
"""
    )
    assert result["plain"] == {
        "hidden": False,
        "lines": [
            "Claude in Chromeinherit (off)onoffofffrom default",
            "Fake toolinheritonoffskippedfrom its own setting· codex does not support it",
        ],
        "values": [
            ["", [["inherit (off)", False], ["on", False], ["off", False]]],
            ["on", [["inherit", False], ["on", False], ["off", False]]],
        ],
    }
    assert result["final"] == {
        "lines": [
            "Claude in Chromeinherit (off)onoffofffrom role"
            "· off for role worker: its own setting cannot turn it on"
        ],
        "on": True,
        "title": "off for role worker: its own setting cannot turn it on",
    }
    assert result["own"] == [
        "Claude in Chromeinheritonoffskippedfrom its own setting"
        "· off for role reviewer: the role's setting wins over the agent's"
    ]


def test_a_change_in_the_setup_is_sent_and_a_running_turn_hinted() -> None:
    result = run_page(
        """
renderSetup(agentSetup([item()]));
replies.push({ capabilities: { on: ['chrome'], items: [item({ state: 'on', source: 'agent', override: true })],
  error: null }, running: true });
capSelect().value = 'on';
capSelect().onchange();
await settle();
const running = { request: posted.at(-1), toast: toasts.at(-1), lines: lines(), value: capSelect().value };
// once the turn has ended, the hint goes
renderSetup({ ...setupData, running: false });
out({ running, after: lines() });
"""
    )
    assert result["running"] == {
        "request": {"path": "/api/agents/fix-login-3f2a/capabilities", "body": {"chrome": True}},
        "toast": "Claude in Chrome of fix-login-3f2a: on, from its next turn",
        "lines": [
            "Claude in Chromeinheritonoffonfrom its own setting",
            "A turn is running: the change applies from the next one.",
        ],
        "value": "on",
    }
    assert result["after"] == ["Claude in Chromeinheritonoffonfrom its own setting"]


def test_a_refused_change_is_shown_and_the_choice_put_back() -> None:
    result = run_page(
        """
renderSetup(agentSetup([item({ source: 'role', final: true })], { role: 'reviewer' }));
replies.push(new Error("chrome is off for role reviewer, and an agent's own setting cannot turn it on; "
  + "change the role's: `spartito capabilities on chrome --role reviewer`"));
capSelect().value = 'on';
capSelect().onchange();
await settle();
const note = setupCaps.querySelector('.cap-note.error');
out({ request: posted.at(-1), note: note && note.textContent, value: capSelect().value });
"""
    )
    assert result["request"] == {
        "path": "/api/agents/fix-login-3f2a/capabilities",
        "body": {"chrome": True},
    }
    assert result["note"] == (
        "chrome is off for role reviewer, and an agent's own setting cannot turn it on; "
        "change the role's: `spartito capabilities on chrome --role reviewer`"
    )
    assert result["value"] == ""


def test_the_new_agent_dialog_sends_only_changed_capabilities() -> None:
    result = run_page(
        """
const project = { name: 'my-app', default_provider: 'claude', agents: [] };
openAgentDialog(project);
const select = agentDialog.querySelector('[data-capability=chrome]'), hint = select.parentNode.querySelector('.help');
const shown = { field: capsField.hidden, options: select.options.map(o => o.textContent), value: select.value,
  hint: hint.hidden };
replies.push({ agent_id: 'fix-login-3f2a', run_id: 1 });
await opened.at(-1).submit(formData(agentDialog));
const untouched = posted.at(-1).body;
openAgentDialog(project);
const again = agentDialog.querySelector('[data-capability=chrome]');
again.value = 'on'; again.onchange();
// another provider: the hint says it would be skipped there
const codex = agentDialog.querySelectorAll('[name=provider]')[1];
codex.checked = true; agentDialog.querySelectorAll('[name=provider]')[0].checked = false;
codex.onchange({ target: codex });
const skipped = again.parentNode.querySelector('.help');
replies.push({ agent_id: 'fix-login-3f2a', run_id: 2, note: 'codex does not support chrome: skipped' });
await opened.at(-1).submit(formData(agentDialog));
out({ shown, untouched: 'capabilities' in untouched, sent: posted.at(-1).body.capabilities,
  provider: posted.at(-1).body.provider, skipped: [skipped.hidden, skipped.textContent], toast: toasts.at(-1) });
"""
    )
    assert result["shown"] == {
        "field": False,
        "options": ["inherit", "on", "off"],
        "value": "",
        "hint": True,
    }
    assert result["untouched"] is False
    assert result["sent"] == {"chrome": True}
    assert result["provider"] == "codex"
    assert result["skipped"] == [False, "not supported on Codex: skipped there if on"]
    assert result["toast"] == "Agent started. codex does not support chrome: skipped"


def test_agent_rows_show_a_badge_per_capability() -> None:
    result = run_page(
        """
const agent = (id, capabilities) => ({ id, project: 'my-app', role: 'worker', provider: 'claude', status: 'done',
  queued_runs: 0, parent_id: null, capabilities });
state.projects = [{ name: 'my-app', path: '~/src/my-app', base_branch: 'main', default_provider: 'claude',
  agents: [agent('fix-login-3f2a', ['chrome', 'fake-tool']), agent('docs-7c1d', [])] }];
state.orchestrator = { ...agent(ORCH, ['chrome']), project: null };
renderNav();
const rows = $('nav').querySelectorAll('.nav-item');
out(rows.map(r => [r.querySelector('.name').textContent, r.querySelectorAll('.cap-badge').map(texts),
  r.title.split('\\n').at(-1)]));
"""
    )
    assert result == [
        ["Maestro", ["Claude in Chrome"], "with Claude in Chrome"],
        [
            "fix-login-3f2a",
            ["Claude in Chrome", "fake-tool"],
            "with Claude in Chrome, fake-tool",
        ],
        ["docs-7c1d", [], "worker · done"],
        ["Cost", [], "spend and budgets"],
    ]


def test_a_page_of_an_older_server_shows_no_capabilities() -> None:
    result = run_page(
        """
delete state.capabilities;
state.projects = [{ name: 'my-app', path: '~/src/my-app', base_branch: 'main', default_provider: 'claude',
  agents: [{ id: 'fix-login-3f2a', project: 'my-app', role: 'worker', provider: 'claude', status: 'done',
    queued_runs: 0, parent_id: null }] }];
renderSettings();
const global = Boolean(settingsBox());
state.settings.projects['my-app'] = {};
settingsDialog.querySelector('[name=scope]').value = 'my-app';
renderSettings();
const project = Boolean(settingsBox());
const { capabilities, role, ...old } = agentSetup([]);
renderSetup(old);
const setup = { hidden: setupCaps.hidden, children: setupCaps.children.length };
renderSetup({ ...old, capabilities: ['chrome'] });  // a list, not a breakdown
const listed = setupCaps.hidden;
capsField.hidden = false;
openAgentDialog(state.projects[0]);
const field = capsField.hidden;
replies.push({ agent_id: 'docs-7c1d', run_id: 1 });
await opened.at(-1).submit(formData(agentDialog));
renderNav();
out({ global, project, setup, listed, field, asked: posted.map(p => p.path), sent: 'capabilities' in posted.at(-1).body,
  badges: $('nav').querySelectorAll('.cap-badge').length });
"""
    )
    assert result == {
        "global": False,
        "project": False,
        "setup": {"hidden": True, "children": 0},
        "listed": True,
        "field": True,
        "asked": ["/api/agents"],
        "sent": False,
        "badges": 0,
    }


def test_a_save_answers_a_focused_choice_at_once() -> None:
    result = run_page(
        """
// a <select> keeps the focus after a pick: the answer shows anyway
renderSetup(agentSetup([item({ source: 'role', final: true })], { role: 'reviewer' }));
document.activeElement = capSelect();
replies.push(new Error('chrome is off for role reviewer'));
capSelect().value = 'on';
capSelect().onchange();
await settle();
const refused = { value: capSelect().value, notes: setupCaps.querySelectorAll('.cap-note.error').map(texts) };
renderSetup(agentSetup([item()]));
document.activeElement = capSelect();
replies.push({ capabilities: { on: ['chrome'], items: [item({ state: 'on', source: 'agent', override: true })],
  error: null }, running: true });
capSelect().value = 'on';
capSelect().onchange();
await settle();
out({ refused, saved: lines(), value: capSelect().value });
"""
    )
    assert result["refused"] == {"value": "", "notes": ["chrome is off for role reviewer"]}
    assert result["saved"] == [
        "Claude in Chromeinheritonoffonfrom its own setting",
        "A turn is running: the change applies from the next one.",
    ]
    assert result["value"] == "on"


def test_a_refusal_note_goes_with_its_cause() -> None:
    result = run_page(
        """
const final = [item({ source: 'role', final: true })];
renderSetup(agentSetup(final, { role: 'reviewer' }));
replies.push(new Error('chrome is off for role reviewer'));
capSelect().value = 'on';
capSelect().onchange();
await settle();
const errors = () => setupCaps.querySelectorAll('.cap-note.error').length;
renderSetup(agentSetup(final, { role: 'reviewer' }));  // a poll: the same breakdown
const polled = errors();
renderSetup(agentSetup([item()], { role: 'reviewer' }));  // the role's setting was changed
const changed = errors();
replies.push(new Error('chrome is off for role reviewer'));
renderSetup(agentSetup(final, { role: 'reviewer' }));
capSelect().value = 'on';
capSelect().onchange();
await settle();
renderSetup({ ...agentSetup(final), id: 'docs-7c1d' });  // another agent
const other = errors();
renderSetup(agentSetup(final, { role: 'reviewer' }));  // and back
const back = errors();
out({ polled, changed, other, back });
"""
    )
    assert result == {"polled": 1, "changed": 0, "other": 0, "back": 0}


def test_a_poll_leaves_a_setup_choice_alone_while_it_is_used() -> None:
    result = run_page(
        """
const poll = () => renderSetup(agentSetup([item({ state: 'on', source: 'global' })]));
renderSetup(agentSetup([item()]));
const chosen = capSelect(), answer = later();
replies.push(answer.promise);
chosen.value = 'on';
chosen.onchange();
poll();  // the pick still settles
const settling = [capSelect() === chosen, capSelect().value];
await settle();
poll();  // the pick is on its way
const saving = [capSelect() === chosen, capSelect().value];
answer.answer({ capabilities: { on: ['chrome'], items: [item({ state: 'on', source: 'agent', override: true })],
  error: null }, running: false });
await settle();
const answered = capSelect().value;
const focused = capSelect();
document.activeElement = focused;
renderSetup(agentSetup([item({ state: 'off', source: 'agent', override: false })]));
const kept = [capSelect() === focused, capSelect().value];
document.activeElement = null;
renderSetup(agentSetup([item({ state: 'off', source: 'agent', override: false })]));
out({ settling, saving, answered, kept, left: [capSelect() === focused, capSelect().value] });
"""
    )
    assert result == {
        "settling": [True, "on"],
        "saving": [True, "on"],
        "answered": "on",
        "kept": [True, "on"],
        "left": [False, "off"],
    }


def test_a_poll_redraws_the_settings_box_but_not_a_row_in_use() -> None:
    result = run_page(
        """
settingsDialog.open = true;
renderSettings();
const box = settingsBox(), statusOf = () => box.querySelector('.cap-status-line').textContent;
const poll = fields => { state.capabilities = [{ ...CHROME, ...fields }]; providersChanged(); };
poll({ status: 'available', reason: 'found' });
const later_status = statusOf();
document.activeElement = radios(box)[0];
poll({ status: 'unavailable', reason: 'needs Claude' });
const focused = statusOf();
document.activeElement = null;
const answer = later();
replies.push(answer.promise);
radios(box)[0].checked = false; radios(box)[1].checked = true;  // as the browser does on a click
radios(box)[1].onchange({ target: radios(box)[1] });
poll({ status: 'unknown', reason: 'not checked' });  // the pick still settles
const settling = radioView(box);
await settle();
const saving = radioView(box);
poll({ status: 'available', reason: 'found again' });  // the pick is on its way
const polled = radioView(box);
answer.answer(Promise.reject(new Error('refused by server')));
await settle();
out({ later_status, focused, settling, saving, polled, after: radioView(box), status: statusOf(),
  error: box.querySelector('.cap-status').textContent });
"""
    )
    assert result["later_status"] == "Status: available (found)"
    assert result["focused"] == "Status: available (found)"
    assert result["settling"] == [
        ["inherit (off)", False, False],
        ["on", True, False],
        ["off", False, False],
    ]
    assert result["saving"] == [
        ["inherit (off)", False, True],
        ["on", True, True],
        ["off", False, True],
    ]
    assert result["polled"] == result["saving"]
    assert result["after"] == [
        ["inherit (off)", True, False],
        ["on", False, False],
        ["off", False, False],
    ]
    assert result["status"] == "Status: available (found again)"
    assert result["error"] == "refused by server"


def test_a_poll_never_takes_a_focused_setup_line_out() -> None:
    result = run_page(
        """
const fake = item({ name: 'fake-tool', label: 'Fake tool' });
capabilityNote = { id: 'fix-login-3f2a', text: 'A turn is running: the change applies from the next one.' };
renderSetup(agentSetup([item({ state: 'on', source: 'agent', override: true }), fake], { running: true }));
const line = capSelect().parentNode, seen = watch(line);
document.activeElement = capSelect();
// the turn ends and the other capability changes: the hint goes, the other line is redrawn
renderSetup(agentSetup([item({ state: 'on', source: 'agent', override: true }),
  { ...fake, state: 'on', source: 'global' }], { running: false }));
out({ detached: seen.detached, same: capSelect().parentNode === line, inside: line.parentNode === setupCaps,
  lines: lines() });
"""
    )
    assert result == {
        "detached": 0,
        "same": True,
        "inside": True,
        "lines": [
            "Claude in Chromeinheritonoffonfrom its own setting",
            "Fake toolinherit (on)onoffonfrom global",
        ],
    }


def test_a_poll_never_takes_a_focused_settings_row_out() -> None:
    result = run_page(
        """
settingsDialog.open = true;
state.capabilities = [CHROME, FAKE];
renderSettings();
const box = settingsBox(), row = radios(box)[0].parentNode.parentNode.parentNode, seen = watch(row);
const statuses = () => box.querySelectorAll('.cap-status-line').map(texts);
document.activeElement = radios(box)[0];
state.capabilities = [{ ...CHROME, status: 'available', reason: 'found' }, { ...FAKE, status: 'available', reason: 'found' }];
providersChanged();
out({ detached: seen.detached, same: radios(box)[0].parentNode.parentNode.parentNode === row,
  inside: row.parentNode === box.querySelector('.cap-rows'), statuses: statuses() });
"""
    )
    assert result == {
        "detached": 0,
        "same": True,
        "inside": True,
        "statuses": [
            "Status: unknown (Spartito cannot see the extension)",
            "Status: available (found)",
        ],
    }


def test_an_mcp_servers_name_with_a_colon_is_saved_and_its_choice_refocused() -> None:
    """MCP servers are capabilities named mcp:<server>; the colon goes into no selector (a
    real page refuses `[name=capability-mcp:docs-db]`), and the saved choice gets the focus."""
    result = run_page(
        """
const DOCS = { ...FAKE, name: 'mcp:docs-db', label: 'docs-db', kind: 'mcp', providers: ['claude'] };
state.capabilities = [CHROME, DOCS];
renderSettings();
const box = settingsBox(), mine = box.querySelectorAll('.cap-row')[1].querySelectorAll('input');
const focused = [];
Node.prototype.focus = function () { focused.push([this.name, this.value]); };
document.activeElement = mine[1];
replies.push({ capabilities: [CHROME, { ...DOCS, global: true }] });
mine[1].onchange({ target: mine[1] });
await settle();
out({ names: mine.map(r => r.name), request: posted.at(-1), focused,
  status: box.querySelector('.cap-status').textContent });
"""
    )
    assert result == {
        "names": ["capability-mcp:docs-db"] * 3,
        "request": {"path": "/api/capabilities", "body": {"name": "mcp:docs-db", "state": True}},
        "focused": [["capability-mcp:docs-db", "on"]],
        "status": "docs-db: on globally, from each agent’s next turn.",
    }


def test_a_poll_from_before_a_save_leaves_the_saved_value() -> None:
    result = run_page(
        """
settingsDialog.open = true;
renderSettings();
const box = settingsBox(), statusOf = () => box.querySelector('.cap-status-line').textContent;
const before = setupTicket(), answer = later();
replies.push(answer.promise);
radios(box)[0].checked = false; radios(box)[1].checked = true;
radios(box)[1].onchange({ target: radios(box)[1] });
await settle();
const during = setupTicket();  // a poll that starts while the save is on its way
answer.answer({ capabilities: [{ ...CHROME, global: true }] });
await settle();
pollState({ global: null, status: 'available', reason: 'found' }, before);
const early = [radioView(box), statusOf()];
pollState({ global: null, status: 'available', reason: 'found' }, during);
const meanwhile = [radioView(box), statusOf()];
pollState({ global: true, status: 'available', reason: 'found' });
out({ early, meanwhile, after: [radioView(box), statusOf()] });
"""
    )
    on = [["inherit (off)", False, False], ["on", True, False], ["off", False, False]]
    assert result["early"] == [on, "Status: unknown (Spartito cannot see the extension)"]
    assert result["meanwhile"] == result["early"]
    assert result["after"] == [on, "Status: available (found)"]


def test_a_poll_from_before_a_project_save_is_left_out_too() -> None:
    result = run_page(
        """
settingsDialog.open = true;
state.settings.projects['my-app'] = {};
settingsDialog.querySelector('[name=scope]').value = 'my-app';
replies.push({ capabilities: [{ ...CHROME, project: null, project_roles: {} }] });
renderSettings();
await settle();
const box = settingsBox(), statusOf = () => box.querySelector('.cap-status-line').textContent;
const before = setupTicket();
replies.push({ capabilities: [{ ...CHROME, project: true, project_roles: {} }] });
radios(box)[0].checked = false; radios(box)[1].checked = true;
radios(box)[1].onchange({ target: radios(box)[1] });
await settle();
pollState({ global: false, status: 'available', reason: 'found' }, before);
const early = [radioView(box), statusOf()];
pollState({ global: false, status: 'available', reason: 'found' });
out({ request: posted.at(-1), early, after: [radioView(box), statusOf()] });
"""
    )
    assert result["request"] == {
        "path": "/api/capabilities",
        "body": {"name": "chrome", "state": True, "project": "my-app"},
    }
    assert result["early"] == [
        [["inherit (off)", False, False], ["on", True, False], ["off", False, False]],
        "Status: unknown (Spartito cannot see the extension)",
    ]
    assert result["after"] == [
        [["inherit (off)", False, False], ["on", True, False], ["off", False, False]],
        "Status: available (found)",
    ]


def test_the_page_scripts_parse() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    for script in sorted(STATIC.glob("*.js")):
        done = subprocess.run([node, "--check", str(script)], capture_output=True, text=True)
        assert done.returncode == 0, f"{script.name}: {done.stderr}"
