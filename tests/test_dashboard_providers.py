"""The dashboard's provider choices, run under node with a small fake DOM: every supported
provider is shown, one that cannot run here disabled with its reason; the choices start on
a provider that can run; the requests carry the provider (and the confirmation of a move);
the replies' notes are shown."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src" / "orchestrator" / "web" / "static"

# The page's own code under test; everything else it touches is a stub of the harness below.
NAMES = (
    "TIER_INFO", "el", "button", "avatar", "choices", "options", "confirmDialog",
    "provider", "providerLabel", "canRun", "isUsable", "pickProvider", "providerTitle",
    "unavailableText", "checkProviders", "providerViews", "providersChanged",
    "modelLabel", "modelOptions", "providerChoices", "providerHint", "shownProvider",
    "openProjectDialog", "inherited", "openAgentDialog", "renderSettings", "defaultProviderBox",
    "setupSlot", "setupBusy", "bumpSetup", "inherits", "settingLabel", "renderSetup",
    "buildProviderControl", "moveAgent", "moved", "setupRequest", "describe", "markOwn",
    "markExtras", "buildControl", "sendMessage", "renderCacheInfo", "showCacheTip", "cacheTipKey",
)  # fmt: skip


def page_functions(*names: str) -> str:
    """The named top-level functions and constants of app.js, as source."""
    script = (STATIC / "app.js").read_text()
    parts = []
    for name in names:
        function = re.search(rf"^(?:async )?function {name}\(.*?^\}}\n", script, re.M | re.S)
        # up to the `;` that ends a line followed by a new statement (not a closing bracket)
        constant = re.search(rf"^const {name} = .*?;\n(?=[^\s}})\]]|\n)", script, re.M | re.S)
        found = function or constant
        assert found, name
        parts.append(found.group(0))
    return "\n".join(parts)


# A minimal document: nodes with children, attributes and simple selectors
# (tag, .class, [attr], [attr=value], :checked); markup is refused.
HARNESS = r"""
const ids = {};
class Node {
  constructor(tag) {
    this.tag = tag; this.children = []; this.parentNode = null; this.className = ''; this.text = '';
    this.dataset = {}; this.attrs = {}; this.hidden = false; this.disabled = false; this.checked = false;
    this.title = ''; this.value = ''; this.name = ''; this.type = '';
  }
  set id(value) { this._id = value; ids[value] = this; }
  get id() { return this._id; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(c => c.textContent).join(''); }
  set innerHTML(value) { throw new Error('markup set: ' + value); }
  get classList() {
    const node = this, list = () => node.className.split(' ').filter(Boolean);
    return { contains: c => list().includes(c), add: c => { if (!list().includes(c)) node.className = [...list(), c].join(' '); },
      toggle: (c, on) => { const rest = list().filter(x => x !== c); node.className = (on ? [...rest, c] : rest).join(' '); } };
  }
  get childElementCount() { return this.children.filter(c => c.tag !== '#text').length; }
  get options() { return this.children.filter(c => c.tag === 'option'); }
  adopt(n) {
    if (typeof n === 'string') n = Object.assign(new Node('#text'), { text: n });
    n.remove(); n.parentNode = this; return n;
  }
  append(...nodes) { for (const n of nodes) this.children.push(this.adopt(n)); }
  prepend(...nodes) { this.children.unshift(...nodes.map(n => this.adopt(n))); }
  insertBefore(n, ref) { const i = ref ? this.children.indexOf(ref) : -1; n = this.adopt(n); if (i < 0) this.children.push(n); else this.children.splice(i, 0, n); }
  replaceChildren(...nodes) { for (const c of this.children) c.parentNode = null; this.children = []; this.append(...nodes); }
  remove() { if (this.parentNode) { this.parentNode.children = this.parentNode.children.filter(c => c !== this); this.parentNode = null; } }
  replaceWith(...nodes) {
    const parent = this.parentNode, i = parent.children.indexOf(this);
    this.remove(); parent.children.splice(i, 0, ...nodes.map(n => parent.adopt(n)));
  }
  contains(n) { while (n) { if (n === this) return true; n = n.parentNode; } return false; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  attr(k) {
    if (k.startsWith('data-')) return this.dataset[k.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase())];
    if (k === 'name') return this.name || undefined;
    return k in this.attrs ? this.attrs[k] : undefined;
  }
  matches(selector) {
    const m = selector.match(/^([a-z]*)((?:\.[\w-]+)*)((?:\[[^\]]+\])*)(:checked)?$/);
    if (!m) throw new Error('selector ' + selector);
    if (m[1] && this.tag !== m[1]) return false;
    for (const c of m[2].split('.').filter(Boolean)) if (!this.classList.contains(c)) return false;
    for (const [, k, v] of m[3].matchAll(/\[([\w-]+)(?:=([^\]]+))?\]/g)) {
      const have = this.attr(k);
      if (have === undefined || (v !== undefined && String(have) !== v)) return false;
    }
    return !m[4] || this.checked;
  }
  querySelectorAll(selector) {
    const found = [];
    const walk = n => { for (const c of n.children) { if (c.tag !== '#text' && c.matches(selector)) found.push(c); walk(c); } };
    walk(this); return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  focus() {} addEventListener() {}
}
const document = { createElement: tag => new Node(tag), activeElement: null };
const $ = id => ids[id];
const node = (tag, props = {}, ...children) => { const n = Object.assign(new Node(tag), props); n.append(...children); return n; };
const data = (tag, key, value = '') => { const n = new Node(tag); n.dataset[key] = value; return n; };
// What a submitted form holds: the named controls, radios and checkboxes only when checked.
const formData = root => {
  const pairs = root.querySelectorAll('[name]').filter(n => !['radio', 'checkbox'].includes(n.type) || n.checked)
    .map(n => [n.name, n.value]);
  return { get: k => pairs.find(([n]) => n === k)?.[1] ?? null, getAll: k => pairs.filter(([n]) => n === k).map(([, v]) => v),
    [Symbol.iterator]: () => pairs[Symbol.iterator]() };
};
const texts = n => n.textContent;
const labelled = group => group.children.map(l => ({ text: l.textContent, value: l.children[0].value,
  checked: l.children[0].checked, disabled: l.children[0].disabled, title: l.title }));

// Stubs of the page's other parts.
const ORCH = 'orchestrator', CLI = 'spartito', OTHER = '\u0000other', SETUP_SETTLE_MS = 0;
const pendingSetup = new Map(), savingSetup = new Map(), setupEpochs = new Map();
let setupData = null, selected = ORCH, folder = null, shown = null, providersSeen = '';
const location = { hash: '' };
const SWITCHES = [];
function compactAtBox() { return new Node('div'); }
function versionBox() { return new Node('div'); }
function switchBox() { return new Node('div'); }
const posted = [], replies = [], toasts = [], opened = [];
async function call(path, body) {
  posted.push(body === undefined ? { path } : { path, body });
  const reply = await replies.shift();  // a promise: an answer still on its way
  if (reply instanceof Error) throw reply;
  return reply || {};
}
function toast(message) { toasts.push(message); }
async function refresh() {}
function openDialog(dialog, submit) { dialog.open = true; opened.push({ dialog, submit }); }
function select(id) { selected = id; }
function loadFolder() {} function renderFolder() {} function chooseRepository() {}
const stored = (key, fallback) => fallback;
const tick = () => new Promise(resolve => setTimeout(resolve));
// Lets the settle timers (SETUP_SETTLE_MS is 0 here) and the requests behind them run.
const settle = async () => { for (let i = 0; i < 10; i++) await tick(); };
// An answer that comes when the test says so.
const later = () => { let answer; const promise = new Promise(resolve => { answer = resolve; }); return { promise, answer }; };

const CLAUDE = { name: 'claude', label: 'Claude', description: 'Anthropic coding agent CLI', executable: 'claude',
  efforts: ['low', 'medium', 'high'], models: [{ value: 'opus', label: 'opus' }], models_note: null,
  status: 'available', usable: true, reason: 'found on PATH; login not checked', default: true };
const CODEX = { name: 'codex', label: 'Codex', description: 'OpenAI coding agent CLI', executable: 'codex',
  efforts: ['low', 'medium', 'high', 'xhigh'], models: [], models_note: 'model list loading',
  status: 'unknown', usable: true, reason: 'not checked yet', default: false };
const MISSING = { ...CODEX, status: 'not_installed', usable: false, reason: 'not found on PATH',
  models_note: 'the CLI is not available' };
const SETTINGS = { global: { orchestrator: { codex: { effort: 'high' } } }, projects: {} };
let state = { orchestrator_name: 'Maestro', providers: [CLAUDE, CODEX], tiers: ['orchestrator', 'lead', 'worker'],
  roles: [{ name: 'worker', description: 'Does one task.', lead: false }], settings: SETTINGS,
  projects: [{ name: 'my-app', default_provider: 'claude', agents: [] }], orchestrator_setup: null };

// The page's dialogs, as far as the code under test reads them.
const projectDialog = node('dialog', { id: 'projectDialog' }, node('form', { reset() {} },
  node('input', { name: 'path', value: '~/src/my-app' }), node('input', { name: 'name' }),
  data('div', 'segment', 'provider'), node('span', { id: 'projectProviderHint' })));
for (const id of ['pickerUp', 'pickerHome', 'pickerUseHere', 'pickerFilter', 'pickerList']) node('div', { id });
const agentDialog = node('dialog', { id: 'agentDialog' }, data('span', 'project'), node('form', { reset() {} },
  node('textarea', { name: 'task', value: 'Fix the login form' }), data('div', 'cards', 'role'),
  data('div', 'segment', 'provider'), node('span', { id: 'agentProviderHint' }), node('input', { name: 'name' }),
  node('input', { name: 'model' }), node('datalist', { id: 'agentModels' }), data('span', 'modelsNote'),
  node('select', { name: 'effort' }), node('select', { name: 'workspace', value: '' }), data('div', 'afterField'),
  data('div', 'checks', 'after')));
const settingsDialog = node('dialog', { id: 'settingsDialog' }, node('form', {},
  node('select', { name: 'scope', value: '' }), node('span', { id: 'settingsHelp' }), node('div', { id: 'settingsRows' })));
const confirmBox = node('dialog', { id: 'confirmDialog' }, node('form', {}, data('h3', 'title'), data('p', 'text'),
  (() => { const l = data('label', 'option'); l.append(node('span')); return l; })(), data('button', 'ok')));
const setupBox = node('span', { id: 'setup' }), setupNote = node('div', { id: 'setupNote' });
const out = value => process.stdout.write(JSON.stringify(value));
"""

PAGE = page_functions(*NAMES)


def run_page(code: str) -> object:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = f"{HARNESS}\n{PAGE}\n(async () => {{\n{code}\n}})().catch(e => {{ console.error(e); process.exit(1); }});"
    done = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


AGENT = """
const agentSetup = provider => ({ id: 'fix-login-3f2a', provider, effective: {}, inherited: {},
  own: { model: null, effort: null }, session: true, running: false, cacheHint: null, resolved: null });
"""


def test_an_unusable_provider_is_shown_disabled_with_its_reason() -> None:
    result = run_page(
        AGENT
        + """
state.providers = [CLAUDE, MISSING];
const group = new Node('div');
providerChoices(group, 'provider', 'claude', null);
renderSetup(agentSetup('claude'));
const choice = $('setup').querySelector('[data-setup=provider]');
out({ radios: labelled(group), options: choice.options.map(o => [o.value, o.textContent, o.disabled, o.title]),
  value: choice.value, label: choice.getAttribute('aria-label'), first: $('setup').children[0].dataset.control });
"""
    )
    assert result["radios"] == [
        {
            "text": "Claude",
            "value": "claude",
            "checked": True,
            "disabled": False,
            "title": "Anthropic coding agent CLI",
        },
        {
            "text": "Codex",
            "value": "codex",
            "checked": False,
            "disabled": True,
            "title": "Codex is not available: not found on PATH",
        },
    ]
    assert result["options"] == [
        ["claude", "Claude", False, "Anthropic coding agent CLI"],
        ["codex", "Codex (not available)", True, "Codex is not available: not found on PATH"],
    ]
    assert result["value"] == "claude"
    assert result["label"] == "Provider of fix-login-3f2a"
    assert result["first"] == "provider"


def test_the_choices_start_on_a_provider_that_can_run() -> None:
    result = run_page(
        """
const picks = () => [pickProvider(undefined), pickProvider('codex'), pickProvider('claude')];
const usable = picks();
state.providers = [CLAUDE, MISSING];
const missing = picks();
state.providers = [{ ...CLAUDE, usable: false, status: 'not_logged_in', reason: 'not logged in', default: false },
  { ...CODEX, default: true }];
const fallback = picks();
openProjectDialog();
const project = labelled(projectDialog.querySelector('[data-segment=provider]')).filter(r => r.checked).map(r => r.value);
state.providers = [{ ...CLAUDE, usable: false, default: false }, MISSING];
const none = picks();
out({ usable, missing, fallback, project, none });
"""
    )
    assert result == {
        "usable": ["claude", "codex", "claude"],
        "missing": ["claude", "claude", "claude"],
        "fallback": ["codex", "codex", "codex"],
        "project": ["codex"],
        "none": [None, None, None],
    }


def test_the_new_agent_dialog_prefers_the_projects_provider_and_says_when_it_cannot() -> None:
    result = run_page(
        """
const project = { name: 'my-app', default_provider: 'codex', agents: [] };
const group = agentDialog.querySelector('[data-segment=provider]'), hint = $('agentProviderHint');
openAgentDialog(project);
const usable = { checked: labelled(group).filter(r => r.checked).map(r => r.value), hint: hint.hidden,
  models: $('agentModels').children.length, note: agentDialog.querySelector('[data-models-note]').textContent,
  noteHidden: agentDialog.querySelector('[data-models-note]').hidden,
  efforts: agentDialog.querySelector('[name=effort]').options.map(o => o.value) };
state.providers = [CLAUDE, MISSING];
openAgentDialog(project);
const fallback = { checked: labelled(group).filter(r => r.checked).map(r => r.value), hint: hint.hidden,
  text: hint.textContent, title: hint.title, button: hint.querySelector('button').textContent,
  efforts: agentDialog.querySelector('[name=effort]').options.map(o => o.value) };
// "Check again" asks for a fresh check and redraws the choice with its answer.
replies.push({ providers: [CLAUDE, CODEX] });
await hint.querySelector('button').onclick();
const again = { asked: posted.at(-1), hint: hint.hidden, disabled: labelled(group).map(r => r.disabled) };
// The submitted request names the chosen provider (the project's again); its note is shown.
replies.push({ agent_id: 'fix-login-3f2a', run_id: 1, note: 'codex is not available: started on claude' });
await opened.at(-1).submit(formData(agentDialog));
out({ usable, fallback, again, request: posted.at(-1), toast: toasts.at(-1), selected });
"""
    )
    assert result["usable"] == {
        "checked": ["codex"],
        "hint": True,
        "models": 0,
        "note": "model list loading",
        "noteHidden": False,
        "efforts": ["", "low", "medium", "high", "xhigh"],
    }
    assert result["fallback"] == {
        "checked": ["claude"],
        "hint": False,
        "text": "Codex is not available (not installed). Claude is chosen instead. Check again",
        "title": "Codex is not available: not found on PATH",
        "button": "Check again",
        "efforts": ["", "low", "medium", "high"],
    }
    assert result["again"] == {
        "asked": {"path": "/api/providers?refresh=1"},
        "hint": True,
        "disabled": [False, False],
    }
    assert result["request"]["path"] == "/api/agents"
    # nothing picked by the user: the project's default, which can run again
    assert result["request"]["body"]["provider"] == "codex"
    assert result["toast"] == "Agent started. codex is not available: started on claude"
    assert result["selected"] == "fix-login-3f2a"


def test_the_maestro_tier_lists_every_provider() -> None:
    result = run_page(
        """
renderSettings();
const rows = $('settingsRows');
const tier = rows.children.find(box => box.children[0].textContent === 'Orchestrator');
out({ fields: tier.querySelectorAll('[name]').map(n => n.name), who: tier.querySelectorAll('.who').map(texts),
  effort: tier.querySelector('[name=orchestrator\\.codex\\.effort]')?.value ?? null,
  box: rows.querySelector('[name=default_provider]') !== null });
"""
    )
    assert result["fields"] == [
        "orchestrator.claude.model",
        "orchestrator.claude.effort",
        "orchestrator.codex.model",
        "orchestrator.codex.effort",
    ]
    assert result["who"] == ["ClClaude", "CoCodex"]
    assert result["effort"] == "high"
    assert result["box"] is False  # the default provider is a project's


def test_a_projects_default_provider_is_set_at_once_and_its_note_shown() -> None:
    result = run_page(
        """
state.settings.projects['my-app'] = {};
settingsDialog.querySelector('[name=scope]').value = 'my-app';
renderSettings();
const box = $('settingsRows').children[0];
const radios = box.querySelectorAll('[name=default_provider]');
const before = radios.map(r => [r.value, r.checked]);
replies.push({ project: { name: 'my-app', default_provider: 'codex' },
  note: '1 existing agent(s) keep their provider (fix-login-3f2a); new agents run on codex' });
radios[1].onchange({ target: radios[1] });
await settle();
const after = box.querySelectorAll('[name=default_provider]').map(r => [r.value, r.checked]);
const note = box.querySelector('.provider-status').textContent;
replies.push(new Error('codex is not available: not found on PATH'));
const now = box.querySelectorAll('[name=default_provider]');
now[0].onchange({ target: now[0] });
await settle();
out({ title: box.children[0].textContent, before, request: posted[0], after, note,
  refused: box.querySelector('.provider-status').textContent,
  kept: box.querySelectorAll('[name=default_provider]').map(r => [r.value, r.checked]),
  group: box.querySelector('.segmented').getAttribute('aria-describedby') });
"""
    )
    assert result["title"] == "Default provider"
    assert result["before"] == [["claude", True], ["codex", False]]
    assert result["request"] == {
        "path": "/api/projects/my-app/provider",
        "body": {"provider": "codex"},
    }
    assert result["after"] == [["claude", False], ["codex", True]]
    assert result["note"] == (
        "1 existing agent(s) keep their provider (fix-login-3f2a); new agents run on codex"
    )
    assert result["refused"] == "codex is not available: not found on PATH"
    assert result["kept"] == [["claude", False], ["codex", True]]
    assert result["group"] == "settingsProviderHint"


def test_moving_an_agent_is_confirmed_and_shows_the_new_providers_settings() -> None:
    result = run_page(
        AGENT
        + """
renderSetup(agentSetup('claude'));
const choice = () => $('setup').querySelector('[data-setup=provider]');
const confirm = { confirm: 'fix-login-3f2a has a conversation on claude; on codex it starts a new conversation' };
choice().value = 'codex';
replies.push(confirm);
choice().onchange();
await settle();
const asked = { request: posted.at(-1), value: choice().value, disabled: choice().disabled, provider: setupData.provider,
  title: confirmBox.querySelector('[data-title]').textContent, text: confirmBox.querySelector('[data-text]').textContent,
  ok: confirmBox.querySelector('[data-ok]').textContent };
// Cancel: the dialog closes without its action; nothing more is sent, nothing changes.
opened.pop(); confirmBox.open = false;
await settle();
renderSetup(setupData);
const cancelled = { sent: posted.length, value: choice().value, provider: setupData.provider };
// Again, and this time confirmed.
choice().value = 'codex';
replies.push(confirm);
choice().onchange();
await settle();
const sentBeforeConfirm = posted.length;
replies.push({ agent_id: 'fix-login-3f2a', provider: 'codex', effective: { model: null, effort: null },
  inherited: { model: null, effort: null }, model_override: null, effort_override: null, session_id: null,
  cache_hint: 'Switching the model starts a fresh prompt cache.' });
await opened.at(-1).submit(formData(confirmBox));
out({ asked, cancelled, sentBeforeConfirm, confirmed: posted.at(-1), value: choice().value, provider: setupData.provider,
  session: setupData.session, toast: toasts.at(-1),
  efforts: $('setup').querySelector('[data-control=effort]').querySelector('select').options.map(o => o.value),
  modelsNote: $('setup').querySelector('.models-note')?.textContent ?? null });
"""
    )
    assert result["asked"] == {
        "request": {"path": "/api/agents/fix-login-3f2a/model", "body": {"provider": "codex"}},
        "value": "claude",
        "disabled": False,
        "provider": "claude",
        "title": "Move fix-login-3f2a to Codex?",
        "text": "fix-login-3f2a has a conversation on claude; on codex it starts a new conversation.",
        "ok": "Move",
    }
    assert result["cancelled"] == {"sent": 1, "value": "claude", "provider": "claude"}
    assert result["sentBeforeConfirm"] == 2
    assert result["confirmed"] == {
        "path": "/api/agents/fix-login-3f2a/model",
        "body": {"provider": "codex", "confirm": True},
    }
    assert result["value"] == "codex"
    assert result["provider"] == "codex"
    assert result["session"] is False
    assert result["toast"] == "fix-login-3f2a runs on Codex from its next turn"
    assert result["efforts"] == ["", "low", "medium", "high", "xhigh"]
    assert result["modelsNote"] == "model list loading"


def test_a_move_without_a_conversation_needs_no_confirmation() -> None:
    result = run_page(
        """
state.orchestrator_setup = { provider: 'claude', effective: {}, inherited: {}, model_override: null, effort_override: null };
renderSetup({ id: ORCH, provider: 'claude', effective: {}, inherited: {}, own: { model: null, effort: null },
  session: false, running: false, cacheHint: null });
replies.push({ agent_id: ORCH, provider: 'codex', effective: {}, inherited: {}, model_override: null,
  effort_override: null, session_id: null, cache_hint: null });
const choice = $('setup').querySelector('[data-setup=provider]');
choice.value = 'codex';
choice.onchange();
await settle();
out({ posted, opened: opened.length, provider: setupData.provider });
"""
    )
    assert result == {
        "posted": [{"path": "/api/agents/orchestrator/model", "body": {"provider": "codex"}}],
        "opened": 0,
        "provider": "codex",
    }


def test_the_setup_says_when_the_agents_provider_cannot_run() -> None:
    result = run_page(
        AGENT
        + """
state.providers = [CLAUDE, MISSING];
renderSetup(agentSetup('codex'));
const note = $('setupNote');
out({ hidden: note.hidden, text: note.textContent, title: note.querySelector('.provider-hint').title,
  described: $('setup').querySelector('[data-setup=provider]').getAttribute('aria-describedby'),
  value: $('setup').querySelector('[data-setup=provider]').value });
"""
    )
    assert result == {
        "hidden": False,
        "text": "Codex is not available (not installed). Check again",
        "title": "Codex is not available: not found on PATH",
        "described": "setupNote",
        "value": "codex",
    }


def test_new_projects_and_messages_show_their_notes() -> None:
    result = run_page(
        """
openProjectDialog();
replies.push({ name: 'my-app', note: 'its default provider codex is not available' });
await opened.at(-1).submit(formData(projectDialog));
const project = { request: posted.at(-1), toast: toasts.at(-1) };
replies.push({ agent_id: ORCH, run_id: 1, note: 'claude is not available: Maestro runs on codex' });
await sendMessage('Which projects exist?');
const maestro = toasts.at(-1);
selected = 'lead:my-app';
replies.push({ agent_id: 'my-app-lead', run_id: 2, note: 'the lead keeps its provider claude' });
await sendMessage('Plan the docs');
const lead = { request: posted.at(-1), toast: toasts.at(-1), selected };
replies.push({ agent_id: 'my-app-lead', run_id: 3, note: null });
const before = toasts.length;
await sendMessage('Go on');
out({ project, maestro, lead, quiet: toasts.length === before });
"""
    )
    assert result["project"] == {
        "request": {
            "path": "/api/projects",
            "body": {"path": "~/src/my-app", "name": "", "provider": "claude"},
        },
        "toast": "Project my-app added. its default provider codex is not available",
    }
    assert result["maestro"] == "claude is not available: Maestro runs on codex"
    assert result["lead"] == {
        "request": {"path": "/api/projects/my-app/lead", "body": {"message": "Plan the docs"}},
        "toast": "the lead keeps its provider claude",
        "selected": "my-app-lead",
    }
    assert result["quiet"] is True


def test_an_agent_to_be_shows_the_provider_it_will_get() -> None:
    result = run_page(
        """
state.projects[0].default_provider = 'codex';
state.orchestrator_setup = { provider: 'claude' };
selected = 'lead:my-app'; const lead = shownProvider(null);
selected = ORCH; const maestro = shownProvider(null);
const agent = shownProvider({ provider: 'codex' });
state.orchestrator_setup = { provider: 'codex' }; const moved = shownProvider(null);
out({ lead, maestro, agent, moved });
"""
    )
    assert result == {"lead": "codex", "maestro": "claude", "agent": "codex", "moved": "codex"}


def test_nothing_in_the_page_assumes_a_provider() -> None:
    page = (STATIC / "app.js").read_text() + (STATIC.parent / "index.html").read_text()
    assert "'claude'" not in page
    assert "'codex'" not in page


PICK = """
// What a click on a radio does: it is checked, the others of its group not, and it says so.
const pick = (container, value) => {
  for (const label of container.children) label.children[0].checked = label.children[0].value === value;
  const input = container.children.find(label => label.children[0].value === value).children[0];
  input.onchange({ target: input });
};
const checkedIn = container => labelled(container).filter(r => r.checked).map(r => r.value);
"""

MOVED = """
const movedTo = provider => ({ agent_id: 'fix-login-3f2a', provider, effective: {}, inherited: {},
  model_override: null, effort_override: null, session_id: null, cache_hint: null });
"""


def test_a_provider_choice_is_sent_once_it_rests_and_not_again_while_on_its_way() -> None:
    result = run_page(
        AGENT
        + MOVED
        + """
renderSetup(agentSetup('claude'));
const choice = () => $('setup').querySelector('[data-setup=provider]');
// Arrow keys through a closed list: a change at every step, one request for where it rests.
for (const value of ['codex', 'claude', 'codex']) { choice().value = value; choice().onchange(); }
const settling = posted.length;
const answer = later();
replies.push(answer.promise);
await settle();
const control = choice();
const inFlight = { posted: posted.map(p => p.body), disabled: control.disabled };
// A poll meanwhile leaves the control alone.
renderSetup(agentSetup('claude'));
const kept = { same: choice() === control, disabled: choice().disabled, value: choice().value };
answer.answer(movedTo('codex'));
await settle();
const after = { value: choice().value, disabled: choice().disabled, provider: setupData.provider };
// Back to the provider it is on before the choice rests: nothing is sent.
choice().value = 'claude'; choice().onchange(); choice().value = 'codex'; choice().onchange();
await settle();
out({ settling, inFlight, kept, after, total: posted.length });
"""
    )
    assert result == {
        "settling": 0,
        "inFlight": {"posted": [{"provider": "codex"}], "disabled": True},
        "kept": {"same": True, "disabled": True, "value": "codex"},
        "after": {"value": "codex", "disabled": False, "provider": "codex"},
        "total": 1,
    }


def test_a_failed_move_puts_the_choice_back() -> None:
    result = run_page(
        AGENT
        + """
renderSetup(agentSetup('claude'));
const choice = () => $('setup').querySelector('[data-setup=provider]');
choice().value = 'codex';
replies.push(new Error('fix-login-3f2a has a running or queued turn: stop it or wait, then move it'));
choice().onchange();
await settle();
out({ value: choice().value, disabled: choice().disabled, provider: setupData.provider, toast: toasts.at(-1),
  opened: opened.length });
"""
    )
    assert result == {
        "value": "claude",
        "disabled": False,
        "provider": "claude",
        "toast": "fix-login-3f2a has a running or queued turn: stop it or wait, then move it",
        "opened": 0,
    }


def test_a_refresh_leaves_a_focused_provider_choice_alone() -> None:
    result = run_page(
        AGENT
        + """
renderSetup(agentSetup('claude'));
const choice = () => $('setup').querySelector('[data-setup=provider]');
const focused = choice();
document.activeElement = focused;
state.providers = [CLAUDE, MISSING];
renderSetup(agentSetup('claude'));
const kept = choice() === focused;
document.activeElement = null;
renderSetup(agentSetup('claude'));
out({ kept, rebuilt: choice() !== focused, options: choice().options.map(o => [o.value, o.disabled]) });
"""
    )
    assert result == {
        "kept": True,
        "rebuilt": True,
        "options": [["claude", False], ["codex", True]],
    }


def test_the_dialogs_say_when_no_provider_can_run() -> None:
    result = run_page(
        """
state.providers = [{ ...CLAUDE, usable: false, status: 'not_logged_in', reason: 'not logged in', default: false }, MISSING];
openProjectDialog();
const projectGroup = projectDialog.querySelector('[data-segment=provider]'), projectHint = $('projectProviderHint');
const project = { radios: labelled(projectGroup).map(r => [r.value, r.checked, r.disabled]),
  hint: projectHint.textContent, title: projectHint.title };
openAgentDialog({ name: 'my-app', default_provider: 'claude', agents: [] });
const agentGroup = agentDialog.querySelector('[data-segment=provider]');
const agent = { radios: labelled(agentGroup).map(r => [r.value, r.checked, r.disabled]), hint: $('agentProviderHint').textContent };
await opened.at(-1).submit(formData(agentDialog));
out({ project, agent, sent: posted.at(-1).body.provider });
"""
    )
    none = [["claude", False, True], ["codex", False, True]]
    assert result["project"] == {
        "radios": none,
        "hint": "None of the agent CLIs can run here. Check again",
        "title": "Claude is not available: not logged in\nCodex is not available: not found on PATH",
    }
    assert result["agent"] == {
        "radios": none,
        "hint": "None of the agent CLIs can run here. Check again",
    }
    assert result["sent"] is None


def test_providers_of_an_older_server_count_as_usable() -> None:
    result = run_page(
        AGENT
        + PICK
        + """
state.providers = [{ name: 'claude', models: [], efforts: [] }, { name: 'codex', models: [], efforts: [] }];
const group = new Node('div');
providerChoices(group, 'provider', pickProvider(undefined), null);
openAgentDialog({ name: 'my-app', default_provider: 'codex', agents: [] });
renderSetup(agentSetup('claude'));
out({ radios: labelled(group).map(r => [r.text, r.checked, r.disabled]), usable: isUsable('codex'),
  agent: checkedIn(agentDialog.querySelector('[data-segment=provider]')), hint: $('agentProviderHint').hidden,
  options: $('setup').querySelector('[data-setup=provider]').options.map(o => [o.textContent, o.disabled]),
  note: $('setupNote').hidden });
"""
    )
    assert result == {
        "radios": [["claude", True, False], ["codex", False, False]],
        "usable": True,
        "agent": ["codex"],
        "hint": True,
        "options": [["claude", False], ["codex", False]],
        "note": True,
    }


def test_an_open_dialog_takes_in_later_provider_results() -> None:
    result = run_page(
        PICK
        + """
const CHECKED = { ...CODEX, status: 'available', reason: 'found on PATH; login not checked',
  models: [{ value: 'gpt-x', label: 'gpt-x' }], models_note: null };
openAgentDialog({ name: 'docs-site', default_provider: 'codex', agents: [] });
const group = agentDialog.querySelector('[data-segment=provider]'), field = name => agentDialog.querySelector(`[name=${name}]`);
const note = agentDialog.querySelector('[data-models-note]');
const before = { models: $('agentModels').children.length, note: note.hidden ? null : note.textContent };
field('model').value = 'my-model'; field('name').value = 'write-guide'; field('effort').value = 'xhigh';
state.providers = [CLAUDE, CHECKED];
providersChanged();
const checked = { chosen: checkedIn(group), models: $('agentModels').children.map(o => o.value),
  note: note.hidden ? null : note.textContent, model: field('model').value, name: field('name').value,
  effort: field('effort').value };
// The provider one would get turns out to be missing: the choice moves, the hint says so.
state.providers = [CLAUDE, MISSING];
providersChanged();
const missing = { chosen: checkedIn(group), hint: $('agentProviderHint').textContent, effort: field('effort').value,
  model: field('model').value };
// A closed dialog is left alone.
agentDialog.open = false;
state.providers = [CLAUDE, CHECKED];
providersChanged();
const closed = checkedIn(group);
// The new-project dialog: a picked provider that can no longer run is the one exception.
openProjectDialog();
const projectGroup = projectDialog.querySelector('[data-segment=provider]');
pick(projectGroup, 'codex');
const picked = checkedIn(projectGroup);
state.providers = [CLAUDE, MISSING];
providersChanged();
const project = { chosen: checkedIn(projectGroup), hint: $('projectProviderHint').textContent };
// The settings: the model lists and whether a provider can run.
projectDialog.open = false;
renderSettings();
settingsDialog.open = true;
state.providers = [CLAUDE, CHECKED];
providersChanged();
const settings = { models: $('settingsRows').querySelector('[name=orchestrator\\.codex\\.model]').parentNode
  .querySelector('datalist').children.map(o => o.value),
  unusable: $('settingsRows').querySelectorAll('.who').filter(w => w.classList.contains('unusable')).length };
out({ before, checked, missing, closed, picked, project, settings });
"""
    )
    assert result["before"] == {"models": 0, "note": "model list loading"}
    assert result["checked"] == {
        "chosen": ["codex"],
        "models": ["gpt-x"],
        "note": None,
        "model": "my-model",
        "name": "write-guide",
        "effort": "xhigh",
    }
    assert result["missing"] == {
        "chosen": ["claude"],
        "hint": "Codex is not available (not installed). Claude is chosen instead. Check again",
        "effort": "",  # Claude offers no xhigh
        "model": "my-model",
    }
    assert result["closed"] == ["claude"]
    assert result["picked"] == ["codex"]
    assert result["project"] == {
        "chosen": ["claude"],
        "hint": "Codex is not available (not installed). Claude is chosen instead. Check again",
    }
    assert result["settings"] == {"models": ["gpt-x"], "unusable": 0}


def test_the_new_agent_dialog_follows_the_roles_provider() -> None:
    result = run_page(
        PICK
        + """
const project = { name: 'my-app', default_provider: 'claude', agents: [] };
const roles = agentDialog.querySelector('[data-cards=role]'), group = agentDialog.querySelector('[data-segment=provider]');
const hint = $('agentProviderHint');
const walk = () => {
  openAgentDialog(project);
  const seen = [checkedIn(group)[0]];
  pick(roles, 'reviewer'); seen.push(checkedIn(group)[0]);
  pick(roles, 'worker'); seen.push(checkedIn(group)[0]);
  return seen;
};
state.roles = [{ name: 'worker', description: 'Does one task.', lead: false, provider: null },
  { name: 'reviewer', description: 'Reviews a branch.', lead: false, provider: 'codex' }];
const withField = walk();
// A provider the user picked stays when the role changes.
openAgentDialog(project);
pick(group, 'codex'); pick(roles, 'reviewer'); pick(roles, 'worker');
const userPick = checkedIn(group)[0];
// The role's provider cannot run: the project's default, with the hint.
state.providers = [CLAUDE, MISSING];
openAgentDialog(project);
pick(roles, 'reviewer');
const missing = { chosen: checkedIn(group)[0], hint: hint.textContent };
// A server older than this page: no `provider` on the roles.
state.providers = [CLAUDE, CODEX];
state.roles = state.roles.map(({ provider, ...role }) => role);
const withoutField = walk();
out({ withField, userPick, missing, withoutField });
"""
    )
    assert result == {
        "withField": ["claude", "codex", "claude"],
        "userPick": "codex",
        "missing": {
            "chosen": "claude",
            "hint": "Codex is not available (not installed). Claude is chosen instead. Check again",
        },
        "withoutField": ["claude", "claude", "claude"],
    }


def test_a_projects_default_provider_is_sent_once_it_rests_and_one_at_a_time() -> None:
    result = run_page(
        """
state.settings.projects['my-app'] = {};
settingsDialog.querySelector('[name=scope]').value = 'my-app';
renderSettings();
const box = $('settingsRows').children[0];
const radios = () => box.querySelectorAll('[name=default_provider]');
for (const index of [1, 0, 1]) radios()[index].onchange({ target: radios()[index] });
const answer = later();
replies.push(answer.promise);
await settle();
const inFlight = { posted: posted.map(p => p.body), disabled: radios().map(r => r.disabled) };
answer.answer({ project: { name: 'my-app', default_provider: 'codex' }, note: null });
await settle();
out({ inFlight, after: radios().map(r => [r.value, r.checked, r.disabled]), total: posted.length,
  status: box.querySelector('.provider-status').textContent });
"""
    )
    assert result == {
        "inFlight": {"posted": [{"provider": "codex"}], "disabled": [True, True]},
        "after": [["claude", False, False], ["codex", True, False]],
        "total": 1,
        "status": "New agents of my-app run on Codex.",
    }


def test_a_pick_keeps_the_keyboard_on_the_provider_choice() -> None:
    result = run_page(
        PICK
        + """
let focusedOn = null;
Node.prototype.focus = function () { focusedOn = this; document.activeElement = this; };
// The new-project dialog: a pick does not rebuild the radios the keyboard is on.
openProjectDialog();
const group = projectDialog.querySelector('[data-segment=provider]');
const before = group.children[1].children[0];
pick(group, 'codex');
const same = group.children[1].children[0] === before;
await settle();  // the dialog's own focus on its folder filter comes first
// The project's default provider: after the save the focus is back on the chosen radio.
state.settings.projects['my-app'] = {};
settingsDialog.querySelector('[name=scope]').value = 'my-app';
renderSettings();
const box = $('settingsRows').children[0];
const radio = box.querySelectorAll('[name=default_provider]')[1];
radio.focus();
replies.push({ project: { name: 'my-app', default_provider: 'codex' }, note: null });
radio.onchange({ target: radio });
await settle();
out({ same, focused: focusedOn !== radio && focusedOn.value === 'codex' && focusedOn.checked && box.contains(focusedOn) });
"""
    )
    assert result == {"same": True, "focused": True}


def test_a_focused_provider_choice_moves_back_after_a_move() -> None:
    """The choice compares with the provider the agent is on now, not the one it was built
    with: a focused control is not rebuilt after a move."""
    result = run_page(
        AGENT
        + MOVED
        + """
renderSetup({ ...agentSetup('claude'), session: false });
const control = $('setup').querySelector('[data-setup=provider]');
document.activeElement = control;
control.value = 'codex';
replies.push(movedTo('codex'));
control.onchange();
await settle();
const first = { same: $('setup').querySelector('[data-setup=provider]') === control, provider: setupData.provider };
control.value = 'claude';
replies.push(movedTo('claude'));
control.onchange();
await settle();
out({ first, posted: posted.map(p => p.body), same: $('setup').querySelector('[data-setup=provider]') === control,
  value: control.value, provider: setupData.provider });
"""
    )
    assert result == {
        "first": {"same": True, "provider": "codex"},
        "posted": [{"provider": "codex"}, {"provider": "claude"}],
        "same": True,
        "value": "claude",
        "provider": "claude",
    }


def test_a_recheck_with_the_same_result_leaves_an_open_dialog_alone() -> None:
    result = run_page(
        PICK
        + """
const at = when => state.providers.map(p => ({ ...p, checked_at: when, version: '1.0.0', path: '/opt/cli' }));
state.providers = at('2026-11-13T10:00:00+00:00');
providersChanged();
openAgentDialog({ name: 'docs-site', default_provider: 'codex', agents: [] });
const group = agentDialog.querySelector('[data-segment=provider]'), effort = agentDialog.querySelector('[name=effort]');
const nodes = () => [group.children[1].children[0], effort.options[0], $('agentModels')];
const radio = group.children[1].children[0], before = nodes(), models = $('agentModels').children.slice();
document.activeElement = radio;
// The re-check five minutes later: only checked_at differs.
state.providers = at('2026-11-13T10:05:00+00:00');
providersChanged();
const recheck = { same: nodes().every((node, i) => node === before[i]), datalist: $('agentModels').children.every((o, i) => o === models[i]),
  focus: document.activeElement === radio && group.contains(radio) };
// Codex turns out to be missing: redrawn.
state.providers = [CLAUDE, MISSING];
providersChanged();
out({ recheck, redrawn: group.children[1].children[0] !== radio, chosen: checkedIn(group) });
"""
    )
    assert result == {
        "recheck": {"same": True, "datalist": True, "focus": True},
        "redrawn": True,
        "chosen": ["claude"],
    }
