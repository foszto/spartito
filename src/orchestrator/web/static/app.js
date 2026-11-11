'use strict';

const $ = id => document.getElementById(id);
const ORCH = 'orchestrator';
const ICONS = {
  logo: '<circle cx="12" cy="5" r="2.2"/><circle cx="5" cy="18" r="2.2"/><circle cx="19" cy="18" r="2.2"/><path d="M12 7.2v4.3M12 11.5 6.2 16M12 11.5l5.8 4.5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  chevron: '<path d="m6 9 6 6 6-6"/>',
  right: '<path d="m9 6 6 6-6 6"/>',
  up: '<path d="M12 19V5M5 12l7-7 7 7"/>',
  down: '<path d="M12 5v14M5 12l7 7 7-7"/>',
  board: '<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M8 9h8M8 13h8M8 17h4"/>',
  home: '<path d="M4 11 12 4l8 7M6 10v9h12v-9"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  branch: '<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="8" r="2"/><path d="M6 7v10M18 10c0 4-6 3-6 7"/>',
  chat: '<path d="M4 5h16v11H9l-5 4z"/>',
  send: '<path d="M12 19V5M5 12l7-7 7 7"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
  archive: '<rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v11h14V8M10 12h4"/>',
  restore: '<path d="M4 12a8 8 0 1 0 2.5-5.8M4 4v4h4"/>',
  pencil: '<path d="M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4"/>',
  compress: '<path d="M4 9h16M4 15h16M12 3v4M10 5l2 2 2-2M12 21v-4M10 19l2-2 2 2"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
  sliders: '<path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/>',
  moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
  ask: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.4 2.3c-.6.3-.9.8-.9 1.4v.6M12 16.8v.4"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5"/>',
};
const TIER_INFO = {
  orchestrator: ['Orchestrator', () => `${state.orchestrator_name}, the top-level agent you talk to here.`],
  lead: ['Project lead', 'Plans the work of one project and runs its agents.'],
  worker: ['Workers', 'Every other role: worker, planner, reviewer, tester.'],
};
const ARCHIVE = 'archive:';  // the selection that shows the archive
const DECISION = 'decision:';  // #decision:<id> opens the decisions dialog on that decision
const SUGGESTIONS = [
  'Which projects and agents exist, and what is each of them doing?',
  'In project <name>: plan the following feature, split it into tasks and implement them with workers: ',
  'Review the unmerged agent branches in project <name> and tell me which are ready to merge.',
];

// The names come with the page too, so no label waits for the first /api/state reply.
let state = { orchestrator_name: document.body.dataset.orchestratorName, orchestrator: null, projects: [], providers: [], roles: [], tiers: [], archived: [], settings: { global: {}, projects: {} } };
const hashTarget = () => decodeURIComponent(location.hash.slice(1));
// The agent shown; a decision named by the hash is shown once the state is loaded (refresh).
let selected = hashTarget().startsWith(DECISION) ? ORCH : hashTarget() || ORCH;
let wantedDecision = hashTarget().startsWith(DECISION) ? hashTarget().slice(DECISION.length) : null;
let shown = null, lastEvent = 0, refreshing = false, again = false, toolGroup = null;
let shownAgent = null, archiveList = [], archiveKey = '';
const findArchived = id => archiveList.find(agent => agent.id === id);

// -- small helpers -----------------------------------------------------------

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}
function icon(name, cls) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  if (cls) svg.setAttribute('class', cls);
  svg.innerHTML = ICONS[name];  // static markup from the table above
  return svg;
}
function button(cls, iconName, label, onclick, title) {
  const node = el('button', `btn ${cls}`);
  node.type = 'button';
  if (iconName) node.append(icon(iconName));
  if (label) node.append(label);
  if (title) { node.title = title; node.setAttribute('aria-label', title); }
  node.onclick = onclick;
  return node;
}
function avatar(provider, status) {
  const node = el('span', `avatar ${provider === ORCH ? 'orch' : provider} ${status || ''}`);
  if (provider === ORCH) node.append(icon('logo'));
  else if (provider === 'archive') node.append(icon('archive'));
  else node.append((provider || '?').slice(0, 2).replace(/^./, c => c.toUpperCase()));
  if (status !== undefined) node.append(el('span', 'dot'));
  return node;
}
const stored = (key, fallback) => { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch (error) { return fallback; } };
const store = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); } catch (error) { /* optional */ } };
const clock = ts => new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
const basename = path => path.replace(/\/+$/, '').split('/').pop();

async function call(path, body, keepalive) {
  const init = body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), keepalive: Boolean(keepalive) };
  const response = await fetch(path, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

let toastTimer;
function toast(message, isError, duration) {
  const node = $('toast');
  node.textContent = message; node.className = isError ? 'error' : ''; node.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { node.hidden = true; }, duration || (isError ? 7000 : 2500));
}

const allAgents = () => state.projects.flatMap(p => p.agents).concat(state.orchestrator || []);
const findAgent = id => allAgents().find(agent => agent.id === id);
const isArchived = id => (state.archived || []).includes(id);
const leadOf = project => project.agents.find(a => a.role === 'lead' && (!a.parent_id || a.parent_id === ORCH));
const provider = name => state.providers.find(p => p.name === name) || { name, models: [], efforts: [] };
// A provider's models as {value, label}: the value goes to the CLI, the label (with the version
// where the provider names one) is shown. Any other typed name is still accepted.
const modelLabel = (p, value) => p.models.find(m => m.value === value)?.label || value;
const modelOptions = p => p.models.map(m => {
  const o = el('option'); o.value = m.value; if (m.label !== m.value) o.label = m.label; return o;
});
const isActive = agent => agent && (agent.status === 'running' || agent.status === 'waiting');
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
// No own turn, the last one went fine (or there was none), but its agents still work: it takes a
// message now and is notified when they finish. A failed or stopped last turn keeps its status.
const isIdle = agent => Boolean(agent && ['done', 'new'].includes(agent.status) && agent.children_running > 0);
// CSS class and label of an agent's state, with the agents working below it and the turns
// queued behind the current one ("+N queued").
function stateOf(agent) {
  if (!agent) return { cls: '', label: 'not started' };
  const cls = isIdle(agent) ? 'idle' : agent.status;
  const parts = [cls];
  if (agent.children_running) parts.push(`${plural(agent.children_running, 'agent')} working`);
  const more = isActive(agent) ? agent.queued_runs - 1 : 0;
  if (more > 0) parts.push(`+${more} queued`);
  return { cls, label: parts.join(' · ') };
}
// Who queued a run: an agent id, the user, or the system for a notification.
const senderOf = run => run.sender || (run.kind === 'notification' ? 'system' : 'user');

function select(id) {
  flushSetup();  // a choice still settling belongs to the agent that was shown
  selected = id; location.hash = encodeURIComponent(id); shown = null; refresh();
}

// -- markdown (builds DOM nodes; never parses agent text as HTML) -----------

function count(n) {
  return n < 1000 ? String(n) : n < 1e6 ? `${(n / 1000).toFixed(1)}k` : `${(n / 1e6).toFixed(1)}M`;
}

// Prompt tokens (fresh + cache reads + cache writes) and output tokens, or null when unknown.
function tokens(counts) {
  return counts ? [counts.in + counts.cache_read + counts.cache_write, counts.out] : null;
}

function tokenText(counts) {
  const t = tokens(counts);
  return t ? `${count(t[0])} in · ${count(t[1])} out` : '';
}

// How full an agent's conversation is against its model's window (`context` of the view).
const CONTEXT_WARN = 80;  // percent from which it is shown in a warning colour (without a compaction threshold)
function short(n) {
  return n < 1000 ? String(n) : n < 1e6 ? `${Math.round(n / 1000)}k` : `${+(n / 1e6).toFixed(1)}M`;
}
function contextText(context) {
  if (!context) return '';
  if (context.compacted) return 'compacted';
  if (!context.window) return `${short(context.tokens)} ctx`;
  return `${short(context.tokens)}/${short(context.window)} ${context.percent}%`;
}
// Above the automatic compaction threshold (`compact_at`, when on), else from CONTEXT_WARN.
function contextWarn(context) {
  if (!context) return false;
  if (context.compact_at) return context.tokens > context.compact_at || Boolean(context.compact_paused);
  return context.percent >= CONTEXT_WARN;
}
function compactAtTitle(context) {
  if (!context || context.compact_at === undefined) return '';
  if (!context.compact_at) return '\nAutomatic compaction: off';
  if (context.compact_paused) {
    return `\nAutomatic compaction paused: the last compaction left ${short(context.compact_paused)}, above the `
      + `threshold of ${short(context.compact_at)}; compact it by hand or raise the threshold`;
  }
  return `\nAutomatic compaction after a turn that ends above ${short(context.compact_at)} tokens`;
}
function contextTitle(context) {
  if (!context) return 'Context size not known yet: it is measured after a turn';
  if (context.compacted) return 'The conversation was compacted; its size is known after the next turn' + compactAtTitle(context);
  return `Conversation: ${context.tokens.toLocaleString()} tokens`
    + (context.window ? ` of a ${context.window.toLocaleString()}-token window (${context.percent}%)` : '')
    + compactAtTitle(context);
}

function inline(text, parent) {
  const pattern = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\))/g;
  let last = 0, match;
  while ((match = pattern.exec(text))) {
    if (match.index > last) parent.append(text.slice(last, match.index));
    if (match[1]) parent.append(el('code', '', match[1].slice(1, -1)));
    else if (match[2]) parent.append(el('strong', '', match[2].slice(2, -2)));
    else {
      const link = el('a', '', match[4]);
      link.href = match[5]; link.target = '_blank'; link.rel = 'noopener noreferrer';
      parent.append(link);
    }
    last = pattern.lastIndex;
  }
  if (last < text.length) parent.append(text.slice(last));
}

function markdown(text) {
  const root = el('div', 'md'), lines = text.split('\n');
  const isList = line => /^\s*([-*+]|\d+[.)])\s+/.test(line);
  const isRule = line => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);
  const cells = line => line.trim().replace(/^\||\|$/g, '').split('|').map(cell => cell.trim());
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.trim().startsWith('```')) {
      const code = [];
      for (i++; i < lines.length && !lines[i].trim().startsWith('```'); i++) code.push(lines[i]);
      i++;
      const pre = el('pre'); pre.append(el('code', '', code.join('\n'))); root.append(pre);
    } else if (!line.trim()) {
      i++;
    } else if (/^#{1,6}\s+/.test(line)) {
      const level = line.match(/^#+/)[0].length;
      const heading = el(`h${Math.min(level + 2, 6)}`);
      inline(line.replace(/^#+\s+/, ''), heading); root.append(heading); i++;
    } else if (line.includes('|') && i + 1 < lines.length && isRule(lines[i + 1])) {
      const table = el('table'), head = el('tr');
      for (const cell of cells(line)) { const th = el('th'); inline(cell, th); head.append(th); }
      table.append(head);
      for (i += 2; i < lines.length && lines[i].includes('|') && lines[i].trim(); i++) {
        const row = el('tr');
        for (const cell of cells(lines[i])) { const td = el('td'); inline(cell, td); row.append(td); }
        table.append(row);
      }
      const wrap = el('div', 'table-wrap'); wrap.append(table); root.append(wrap);
    } else if (isList(line)) {
      const list = el(/^\s*\d/.test(line) ? 'ol' : 'ul');
      while (i < lines.length && (isList(lines[i]) || /^\s{2,}\S/.test(lines[i]))) {
        if (isList(lines[i])) {
          const item = el('li'); inline(lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/, ''), item); list.append(item);
        } else if (list.lastChild) {
          list.lastChild.append(' '); inline(lines[i].trim(), list.lastChild);
        }
        i++;
      }
      root.append(list);
    } else {
      const paragraph = [];
      while (i < lines.length && lines[i].trim() && !lines[i].trim().startsWith('```')
        && !/^#{1,6}\s+/.test(lines[i]) && !isList(lines[i])
        && !(lines[i].includes('|') && i + 1 < lines.length && isRule(lines[i + 1]))) paragraph.push(lines[i++]);
      const p = el('p'); inline(paragraph.join('\n'), p); root.append(p);
    }
  }
  return root;
}

// -- sidebar -----------------------------------------------------------------

let navKey = '';
function renderNav() {
  // Rebuild only on change: replacing a button between mousedown and mouseup swallows the click.
  const collapsed = stored('orch-collapsed', []);
  const key = JSON.stringify([state.orchestrator, state.projects, state.archived, selected, collapsed]);
  if (key === navKey) return;
  navKey = key;
  const nav = $('nav');
  nav.replaceChildren();

  const orch = state.orchestrator;
  const top = navItem(ORCH, ORCH, orch ? stateOf(orch).cls : '', state.orchestrator_name,
    orch ? `${stateOf(orch).label} · talks to every project` : 'Tell it what you want built', 0, orch?.context);
  top.classList.add('orchestrator');
  nav.append(top);

  const section = el('div', 'section');
  section.append(el('span', '', 'Projects'), button('ghost icon', 'plus', '', openProjectDialog, 'Add project'));
  nav.append(section);
  if (!state.projects.length) {
    const empty = el('div', 'empty-note', 'No projects yet. ');
    empty.style.paddingLeft = '8px';
    nav.append(empty, button('primary', 'plus', 'Add project', openProjectDialog));
  }

  let total = 0, busy = 0, cost = orch ? orch.cost_usd : 0, used = tokens(orch && orch.tokens) || [0, 0];
  const addTokens = counts => { const t = tokens(counts); if (t) used = [used[0] + t[0], used[1] + t[1]]; };
  for (const project of state.projects) {
    const box = el('div', `project${collapsed.includes(project.name) ? ' collapsed' : ''}`);
    const head = el('div', 'project-head'), toggle = el('button', 'toggle');
    toggle.type = 'button';
    toggle.title = `${project.path} · ${project.base_branch}`;
    toggle.setAttribute('aria-expanded', String(!collapsed.includes(project.name)));
    toggle.append(icon('chevron'), el('span', 'name', project.name), el('span', 'count', String(project.agents.length)));
    toggle.onclick = () => {
      const now = stored('orch-collapsed', []);
      store('orch-collapsed', now.includes(project.name) ? now.filter(n => n !== project.name) : [...now, project.name]);
      renderNav();
    };
    const actions = el('div', 'actions');
    actions.append(
      button('ghost icon', 'chat', '', () => select(leadOf(project)?.id || `lead:${project.name}`), 'Talk to the project lead'),
      button('ghost icon', 'plus', '', () => openAgentDialog(project), 'New agent'),
      button('ghost icon', 'board', '', () => openBoard(project.name), 'Task board'),
      button('ghost icon', 'archive', '', () => archiveSafe(project), 'Archive every finished agent whose work is safe'),
      button('ghost icon', 'x', '', () => removeProject(project), 'Remove project'));
    head.append(toggle, actions);
    const agents = el('div', 'agents');
    const ids = new Set(project.agents.map(a => a.id));
    const add = (parent, depth) => {
      for (const agent of project.agents.filter(a => (ids.has(a.parent_id) ? a.parent_id : null) === parent)) {
        total++; cost += agent.cost_usd; addTokens(agent.tokens);
        if (isActive(agent)) busy++;
        const now = stateOf(agent);
        agents.append(navItem(agent.id, agent.provider, now.cls, agent.id, `${agent.role} · ${now.label}`, depth, agent.context));
        add(agent.id, depth + 1);
      }
    };
    add(null, 0);
    if (!project.agents.length) agents.append(el('div', 'empty-note', 'No agents yet.'));
    box.append(head, agents);
    nav.append(box);
  }
  const archived = (state.archived || []).length;
  if (archived || selected === ARCHIVE) {
    const entry = navItem(ARCHIVE, 'archive', '', 'Archive', `${plural(archived, 'agent')} · restore or delete`, 0);
    entry.classList.add('archive-entry');
    nav.append(entry);
  }
  $('summary').textContent = `${total} agents · ${busy} active · ${count(used[0] + used[1])} tokens · $${cost.toFixed(2)} spent`;
}

function navItem(id, providerName, status, name, sub, depth, context) {
  const row = el('button', `nav-item${id === selected ? ' selected' : ''}`);
  row.type = 'button';
  row.style.paddingLeft = `${8 + depth * 16}px`;
  const label = el('span', 'label'), subLine = el('span', 'sub', sub);
  // One small field: the context's fill (or `compacted`), in a warning colour from CONTEXT_WARN.
  const fill = context && (context.compacted ? 'compacted' : context.percent != null ? `ctx ${context.percent}%` : '');
  if (fill) subLine.append(' · ', el('span', `ctx${contextWarn(context) ? ' warn' : ''}`, fill));
  label.append(el('span', 'name', name), subLine);
  row.append(avatar(providerName, status), label);
  row.title = `${name}\n${sub}${fill ? `\n${contextTitle(context)}` : ''}`;
  row.onclick = () => select(id);
  return row;
}

// -- conversation ------------------------------------------------------------

function buildView() {
  const view = $('view');
  view.replaceChildren();
  const top = el('div', 'top-bar'), who = el('div', 'who');
  const face = el('span'); face.id = 'face';
  const title = el('h2'); title.id = 'title';
  // The model/effort controls live in their own slot, so rebuilding the chips around them
  // never takes away a control the user is using.
  const chips = el('div', 'chips'), lead = el('span', 'chip-group'), tail = el('span', 'chip-group');
  const setup = el('span', 'setup'), note = el('div', 'setup-note');
  lead.id = 'chips'; tail.id = 'chipsTail'; setup.id = 'setup'; note.id = 'setupNote'; note.hidden = true;
  setup.addEventListener('focusout', () => setTimeout(() => renderSetup(setupData)));
  chips.append(lead, setup, tail);
  who.append(title, chips, note);
  const detailsButton = button('ghost', 'info', 'Details', () => { $('details').hidden = !$('details').hidden; });
  const stop = button('danger', 'stop', 'Stop', stopAgent);
  const compact = button('ghost', 'compress', 'Compact', () => compactAgent(selected));
  compact.id = 'compact';
  const archive = button('ghost', 'archive', 'Archive', () => archiveAgent(selected));
  const restore = button('ghost', 'restore', 'Restore', () => restoreAgent(selected));
  const remove = button('ghost icon', 'trash', '', () => removeAgent(selected), 'Delete agent for good');
  detailsButton.id = 'detailsButton'; stop.id = 'stop'; archive.id = 'archive'; restore.id = 'restore'; remove.id = 'remove';
  archive.title = 'Hide it from the lists; everything is kept and it can be restored';
  top.append(face, who, compact, detailsButton, stop, archive, restore, remove);

  const details = el('div'); details.id = 'details'; details.hidden = true;
  const facts = el('dl'); facts.id = 'facts';
  const changes = el('pre'); changes.id = 'changes';
  const runs = el('table'); runs.id = 'runs';
  details.append(facts, runs, changes);

  const log = el('div'); log.id = 'log';
  const thread = el('div', 'thread'); thread.id = 'thread';
  const welcome = el('div', 'welcome'); welcome.id = 'welcome';
  log.append(welcome, thread);
  const busy = el('div'); busy.id = 'busy';
  const queue = el('div', 'queue'); queue.id = 'queue'; queue.hidden = true;

  const form = el('form'); form.id = 'composer';
  const box = el('div', 'compose-box'), input = el('textarea');
  input.rows = 1; input.id = 'message'; input.placeholder = 'Write a message…';
  input.setAttribute('aria-label', 'Message');
  const send = el('button', 'btn primary'); send.append(icon('send'));
  send.title = 'Send'; send.setAttribute('aria-label', 'Send');
  box.append(input, send);
  form.append(box, el('div', 'compose-hint', 'Enter to send · Shift+Enter for a new line'));
  const grow = () => { input.style.height = 'auto'; input.style.height = `${input.scrollHeight}px`; };
  input.oninput = grow;
  input.onkeydown = event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); form.requestSubmit(); }
  };
  form.onsubmit = async event => {
    event.preventDefault();
    const message = input.value.trim();
    if (!message) return;
    send.disabled = true;
    try { await sendMessage(message); input.value = ''; grow(); }
    catch (error) { toast(error.message, true); }
    finally { send.disabled = false; input.focus(); }
  };
  view.append(top, details, log, queue, busy, form);
  shown = selected; lastEvent = 0; toolGroup = null; setupData = null;
  input.focus();
}

function renderWelcome(agent, leadProject) {
  const welcome = $('welcome');
  welcome.hidden = Boolean(agent && agent.runs > 0);
  if (welcome.hidden || welcome.dataset.for === selected) return;
  welcome.dataset.for = selected;
  welcome.replaceChildren();
  const isOrch = selected === ORCH;
  welcome.append(avatar(isOrch ? ORCH : agent ? agent.provider : 'claude'),
    el('h3', '', isOrch ? `Talk to ${state.orchestrator_name}` : leadProject ? `Lead of ${leadProject}` : 'No messages yet'),
    el('p', '', isOrch
      ? 'It registers projects, briefs project leads and starts agents in their own worktrees. You stay in one conversation.'
      : leadProject ? 'The lead knows the codebase, plans the work and runs its own worker agents. Send a goal to start it.'
      : 'Send a message to give this agent its first task.'));
  if (isOrch) {
    const list = el('div', 'suggestions');
    for (const text of SUGGESTIONS) {
      const item = el('button', 'suggestion', text); item.type = 'button';
      item.onclick = () => { const input = $('message'); input.value = text; input.focus(); input.oninput(); };
      list.append(item);
    }
    welcome.append(list);
  }
}

function renderHeader(agent, setupFreshness = true) {
  const leadProject = selected.startsWith('lead:') ? selected.slice(5) : null;
  const title = $('title'), chips = $('chips');
  const name = agent ? (agent.id === ORCH ? state.orchestrator_name : agent.id) : leadProject ? `${leadProject} lead` : state.orchestrator_name;
  const face = avatar(selected === ORCH ? ORCH : agent ? agent.provider : 'claude');
  face.id = 'face'; $('face').replaceWith(face);
  const now = stateOf(agent);
  title.replaceChildren(name, el('span', `pill ${now.cls}`, now.label));
  if (agent?.archived) title.append(el('span', 'pill archived', 'archived'));
  $('stop').hidden = !isActive(agent);
  // Compaction is offered once there is a conversation; a refusal other than that is shown.
  const compactButton = $('compact');
  compactButton.hidden = !agent || Boolean(agent.archived) || !agent.session_id;
  compactButton.disabled = Boolean(agent?.compact_refusal);
  compactButton.title = agent?.compact_refusal
    || 'Replace the conversation with a summary (the CLI\'s own compaction), queued like a message';
  $('archive').hidden = !agent || Boolean(agent.archived) || agent.id === ORCH;
  $('restore').hidden = !agent?.archived;
  $('remove').hidden = !agent?.archived;  // only archived agents are deleted
  $('detailsButton').hidden = !agent;
  renderWelcome(agent, leadProject);

  const busy = $('busy');
  busy.replaceChildren();
  if (agent?.archived) {
    busy.append(`Archived ${new Date(agent.archived.archived_at).toLocaleString()}`
      + `${agent.archived.how === 'auto' ? ' automatically' : ''} — a message or Restore brings it back`);
  } else if (agent && agent.status === 'running') {
    const dots = el('span', 'typing'); dots.append(el('i'), el('i'), el('i'));
    busy.append(dots, 'working…');
  } else if (agent && agent.status === 'waiting') {
    busy.append('queued — waiting for its turn or for the agents it depends on');
  } else if (agent && agent.children_running) {
    const n = agent.children_running, work = n === 1 ? '1 agent works' : `${n} agents work`;
    if (isIdle(agent)) busy.append(el('span', 'idle-mark'));
    busy.append(`${isIdle(agent) ? 'idle' : `last turn ${agent.status}`} while ${work}`
      + ' — it can take a message now and is notified when they finish');
  }
  renderQueue(agent);

  const tail = $('chipsTail');
  chips.replaceChildren(); tail.replaceChildren();
  // Before its first message the orchestrator does not exist yet; its model can be chosen anyway.
  const draft = !agent && selected === ORCH ? state.orchestrator_setup : null;
  renderSetup(agent ? {
    id: agent.id, provider: agent.provider, effective: agent.effective || {}, inherited: agent.inherited || {},
    own: { model: agent.model_override, effort: agent.effort_override },
    session: Boolean(agent.session_id), running: agent.status === 'running', cacheHint: agent.cache_hint,
    resolved: agent.last_run?.usage?.model || null,
  } : draft && {
    id: ORCH, provider: draft.provider, effective: draft.effective, inherited: draft.inherited,
    own: { model: draft.model_override, effort: draft.effort_override }, session: false, running: false,
    cacheHint: draft.cache_hint,
  }, setupFreshness);
  if (!agent) return;
  const chip = (text, iconName, parent) => {
    if (!text) return;
    const node = el('span', 'chip'); if (iconName) node.append(icon(iconName)); node.append(text);
    (parent || chips).append(node);
  };
  chip(agent.role); chip(agent.provider);
  const context = agent.context;
  if (context || agent.session_id) {
    const node = el('span', `chip context${contextWarn(context) ? ' warn' : ''}`);
    if (context && !context.compacted && context.window) {
      const bar = el('span', 'bar'), fill = el('i');
      fill.style.width = `${Math.min(100, context.percent)}%`;
      bar.append(fill); node.append(bar);
    }
    node.append(contextText(context) || 'context ?');
    if (context?.compact_at) node.append(el('span', 'threshold', context.compact_paused ? 'auto-compact paused' : `auto-compact ${short(context.compact_at)}`));
    node.title = contextTitle(context);
    tail.append(node);
  }
  // A compaction is never the agent's status or last run: a failed one is shown here.
  const compaction = agent.last_compaction;
  if (compaction && compaction.status !== 'done') {
    const node = el('span', 'chip compact-failed', `compact ${compaction.status}`);
    node.title = `Compaction #${compaction.id}: ${compaction.error || compaction.status}`;
    tail.append(node);
  }
  chip(agent.branch, 'branch', tail);
  chip(tokenText(agent.tokens), null, tail);
  chip(agent.cost_usd ? `$${agent.cost_usd.toFixed(2)}` : '', null, tail);

  const facts = $('facts'); facts.replaceChildren();
  const fact = (key, value) => { if (value) facts.append(el('dt', '', key), el('dd', '', String(value))); };
  fact('Project', agent.project); fact('Started by', agent.parent_id); fact('Workspace', agent.cwd);
  fact('Session', agent.session_id); fact('Turns', `${agent.runs} (${agent.queued_runs} active)`);
  fact('Agents working', agent.children_running && `${agent.children_running} below this agent`);
  fact('Last run model', agent.last_run && agent.last_run.usage && agent.last_run.usage.model);
  fact('Take over in a terminal', agent.session_id ? `orch attach ${agent.id}` : '');
  const runs = $('runs'); runs.replaceChildren();
  const row = (cells, tag) => { const tr = el('tr'); for (const c of cells) tr.append(el(tag, '', c)); runs.append(tr); };
  if ((agent.recent_runs || []).length) row(['Run', 'Status', 'Ended', 'Tokens', 'Cost', 'Model'], 'th');
  for (const run of agent.recent_runs || []) {
    row([`#${run.id}${run.kind === 'compact' ? ' compact' : ''}`, run.status, (run.ended_at || '').slice(11, 19), tokenText(run.tokens) || '–',
      run.cost_usd != null ? `$${run.cost_usd.toFixed(4)}` : '–', run.model || ''], 'td');
  }
  $('changes').textContent = agent.changes || '';
  $('changes').hidden = !agent.changes;
}

// -- model and effort of the selected agent ---------------------------------

let setupData = null;
const OTHER = '\u0000other';
// Arrow keys on a closed select fire `change` for every option passed (Windows, Linux): the
// choice is saved once it rests for a moment, not each one on the way.
const SETUP_SETTLE_MS = 350;
// Choices waiting to settle, by agent and setting; flushed when the selection changes or the
// page goes away, so none is dropped.
const pendingSetup = new Map();
const savingSetup = new Map();  // saves in flight, by agent and setting: count
// A poll asked for while a save of that agent was in flight, or before one ended, may carry its
// old values: such answers leave its controls alone (`setupFresh` false). Kept per agent, so a
// save for one agent never holds up the controls of another.
const setupEpochs = new Map();  // agent id -> saves started and ended
const setupTicket = () => ({
  epochs: new Map(setupEpochs), saving: new Set([...savingSetup.keys()].map(slot => slot.split('\u0000')[0])) });
const setupFresh = (ticket, id) => !ticket.saving.has(id) && (ticket.epochs.get(id) || 0) === (setupEpochs.get(id) || 0);
const bumpSetup = id => setupEpochs.set(id, (setupEpochs.get(id) || 0) + 1);
const setupSlot = (id, key) => `${id}\u0000${key}`;
const setupBusy = (id, key) => pendingSetup.has(setupSlot(id, key)) || savingSetup.has(setupSlot(id, key));
const inherits = value => value || 'CLI default';
const settingLabel = key => (key === 'model' ? 'Model' : 'Effort');

function flushSetup(keepalive) {
  for (const entry of [...pendingSetup.values()]) { clearTimeout(entry.timer); entry.save(keepalive); }
}

// The header's model and effort controls. A control the user is using (focused: an open list,
// a half-typed name) is never rebuilt; it is updated in place and rebuilt once the focus leaves.
function renderSetup(data, fresh = true) {
  const box = $('setup'), note = $('setupNote');
  // A stale answer does not touch controls that are there; a view without any still gets them.
  if (!box || (!fresh && box.childElementCount)) return;
  setupData = data;
  if (!data) { box.replaceChildren(); note.textContent = ''; note.hidden = true; return; }
  const notes = [];
  if (data.running) notes.push('A change applies from the next turn; the running one is not changed.');
  if (data.session && data.cacheHint) notes.push(data.cacheHint);
  note.textContent = notes.join(' ');
  note.hidden = !notes.length;
  for (const key of ['model', 'effort']) {
    const p = provider(data.provider);
    const items = key === 'model' ? p.models.map(m => [m.value, m.label]) : p.efforts.map(e => [e, e]);
    const extras = key === 'model' ? [data.resolved, p.models_note] : [];
    const built = JSON.stringify([data.id, data.own[key], data.inherited[key], items, extras, notes.length > 0]);
    const current = box.querySelector(`[data-control=${key}]`);
    if (current && current.contains(document.activeElement)) { syncControl(current, data, key); continue; }
    if (current && (current.dataset.built === built || setupBusy(data.id, key))) continue;  // keep a pick on its way
    const control = buildControl(data, key, items, notes.length > 0);
    control.dataset.built = built;
    if (current) current.replaceWith(control); else box.append(control);
  }
}

function describe(select, data, key) {
  const own = data.own[key], label = settingLabel(key), inherit = inherits(data.inherited[key]);
  select.setAttribute('aria-label', `${label} of ${data.id}, ${own ? 'set on the agent' : 'inherited'}`);
  select.title = own ? `${label} set on this agent; “inherit” falls back to ${inherit}`
    : `${label} inherited from the settings; pick one to set it on this agent`;
  select.options[0].textContent = `inherit (${inherit})`;
}

function markOwn(wrap, own) {
  wrap.classList.toggle('own', Boolean(own));
  wrap.querySelector('.mark')?.remove();
  if (own) wrap.insertBefore(el('span', 'mark', 'own'), wrap.querySelector('.extra'));
}

// After the model: the model the last run actually used, and why the list may be incomplete.
function markExtras(wrap, data, key) {
  for (const node of wrap.querySelectorAll('.extra')) node.remove();
  if (key !== 'model') return;
  if (data.resolved) {
    const node = el('span', 'extra resolved mono', `→ ${data.resolved}`);
    node.title = 'Model of the last run, as reported by the CLI';
    wrap.append(node);
  }
  const note = provider(data.provider).models_note;
  if (note) wrap.append(el('span', 'extra models-note', note));
}

function buildControl(data, key, items, described) {
  const own = data.own[key], wrap = el('label', 'setting'), select = el('select', 'input compact');
  wrap.dataset.control = key;
  select.dataset.setup = key;
  if (described) select.setAttribute('aria-describedby', 'setupNote');
  const list = [...items];
  if (own && !list.some(([value]) => value === own)) list.push([own, own]);
  options(select, [['', ''], ...list, ...(key === 'model' ? [[OTHER, 'other…']] : [])], own || '');
  describe(select, data, key);
  select.onchange = () => {
    const id = data.id, slot = setupSlot(id, key);
    clearTimeout(pendingSetup.get(slot)?.timer);
    pendingSetup.delete(slot);
    if (select.value === OTHER) { editModel(select, data); return; }
    const value = select.value || null;
    const save = keepalive => { pendingSetup.delete(slot); saveSetup(id, key, value, keepalive); };
    pendingSetup.set(slot, { timer: setTimeout(save, SETUP_SETTLE_MS), save });
  };
  wrap.append(el('span', 'key', key), select);
  markExtras(wrap, data, key);
  markOwn(wrap, own);
  return wrap;
}

// Brings a focused control up to date without replacing it (that would close an open list).
function syncControl(wrap, data, key) {
  const select = wrap.querySelector('select');
  if (!select || !select.isConnected) return;  // a name is being typed: leave it alone
  const own = data.own[key];
  describe(select, data, key);
  markExtras(wrap, data, key);
  markOwn(wrap, own);
  if (setupBusy(data.id, key)) return;  // the user's own pick is on its way
  if (own && ![...select.options].some(o => o.value === own)) {
    const label = key === 'model' ? modelLabel(provider(data.provider), own) : own;
    select.add(new Option(label, own), key === 'model' ? select.options[select.length - 1] : null);
  }
  select.value = own || '';
}

// "other…": an input for any model name (the providers accept names they do not list). Only
// Enter sets it; Escape or leaving the field cancels, so a half-typed name is never stored.
function editModel(select, data) {
  const id = data.id, own = data.own.model, input = el('input', 'input compact mono'), list = el('datalist');
  list.id = 'setupModels';
  list.append(...modelOptions(provider(data.provider)));
  input.setAttribute('list', list.id); input.autocomplete = 'off'; input.spellcheck = false;
  input.dataset.setup = 'model'; input.value = own || ''; input.placeholder = 'model name';
  input.setAttribute('aria-label', `Model of ${id}: type a name, Enter sets it, Escape cancels`);
  let done = false;
  const finish = (save, refocus) => {
    if (done) return;
    done = true;
    const value = input.value.trim();
    if (save && value && value !== own) {
      if (![...select.options].some(o => o.value === value)) {
        select.add(new Option(modelLabel(provider(data.provider), value), value), select.options[select.length - 1]);
      }
      select.value = value;
      saveSetup(id, 'model', value);
    } else {
      select.value = own || '';
    }
    if (input.isConnected) { input.replaceWith(select); list.remove(); }
    if (refocus) select.focus();
    // a refresh that came in meanwhile may have left the control behind
    else if (setupData?.id === id) setTimeout(() => renderSetup(setupData));
  };
  input.onkeydown = event => {
    if (event.key === 'Enter') { event.preventDefault(); finish(true, true); }
    if (event.key === 'Escape') { event.preventDefault(); finish(false, true); }
  };
  input.onblur = () => finish(false, false);
  select.replaceWith(input, list);
  input.focus(); input.select();
}

async function saveSetup(id, key, value, keepalive) {
  const slot = setupSlot(id, key);
  savingSetup.set(slot, (savingSetup.get(slot) || 0) + 1);
  bumpSetup(id);
  let failed = false;
  try {
    const result = await call(`/api/agents/${encodeURIComponent(id)}/model`, { [key]: value }, keepalive);
    const now = result.effective[key] || 'CLI default';
    toast(value ? `${settingLabel(key)} of ${id}: ${now}, from its next turn` : `${settingLabel(key)} of ${id} inherited again: ${now}`);
    if (setupData?.id === id) {
      setupData = { ...setupData, effective: result.effective, inherited: result.inherited,
        own: { model: result.model_override, effort: result.effort_override } };
    }
  } catch (error) {
    failed = true;
    toast(error.message, true);
  } finally {
    const left = savingSetup.get(slot) - 1;
    if (left > 0) savingSetup.set(slot, left); else savingSetup.delete(slot);
    bumpSetup(id);
  }
  if (setupData?.id !== id) return;
  const control = $('setup')?.querySelector(`[data-control=${key}]`);
  if (failed && control) delete control.dataset.built;  // show the stored value again
  renderSetup(setupData);
  refresh();
}

function renderQueue(agent) {
  const queue = $('queue'), items = (agent && agent.queue) || [];
  // `queue` may be capped; `queued_runs` counts every active turn, the running one too.
  const waiting = items.length && Math.max(agent.queued_runs - (agent.status === 'running' ? 1 : 0), items.length);
  // Rebuilt only when it changed, so a focused Edit/Withdraw button keeps its focus.
  const key = JSON.stringify([waiting, items]);
  if (queue.dataset.key === key) return;
  queue.dataset.key = key;
  queue.hidden = !items.length;
  queue.replaceChildren();
  if (!items.length) return;
  queue.append(el('div', 'queue-head', `Queued (${waiting})`));
  for (const run of items) {
    const row = el('div', `queued ${run.kind}`);
    row.append(el('span', 'kind', run.kind), el('span', 'from', senderOf(run)), el('span', 'when', clock(run.created_at)),
      el('span', 'preview', run.prompt.replace(/\s+/g, ' ')));
    row.title = `#${run.id}: ${run.prompt}`;
    // A notification's text is written when it starts: nothing to edit or withdraw yet.
    // A compaction can be withdrawn, not edited.
    if (run.kind === 'message' || run.kind === 'compact') {
      const actions = el('span', 'actions');
      if (run.kind === 'message') actions.append(button('ghost icon', 'pencil', null, () => editQueued(run), `Edit queued message #${run.id}`));
      actions.append(button('ghost icon', 'x', null, () => withdrawQueued(run), `Withdraw queued ${run.kind === 'compact' ? 'compaction' : 'message'} #${run.id}`));
      row.append(actions);
    }
    queue.append(row);
  }
  if (waiting > items.length) queue.append(el('div', 'queued more', `+${waiting - items.length} more`));
}

function appendEvents(events) {
  const log = $('log'), thread = $('thread');
  const pinned = log.scrollHeight - log.scrollTop - log.clientHeight < 140;
  for (const event of events) {
    lastEvent = Math.max(lastEvent, event.id);
    if (['tool', 'tool_result', 'info'].includes(event.kind)) {
      if (!toolGroup) {
        toolGroup = el('details', 'tools');
        const summary = el('summary');
        summary.append(icon('right'), el('span', 'count'), el('span', 'last'));
        toolGroup.append(summary);
        thread.append(toolGroup);
      }
      toolGroup.append(el('div', `step mono ${event.kind}`, event.text));
      const steps = toolGroup.querySelectorAll('.step').length;
      toolGroup.querySelector('.count').textContent = `${steps} step${steps === 1 ? '' : 's'}`;
      toolGroup.querySelector('.last').textContent = event.text.slice(0, 120);
      continue;
    }
    toolGroup = null;
    const kind = { prompt: 'user', error: 'error', notification: 'system' }[event.kind] || 'reply';
    const message = el('div', `msg ${kind}`), body = el('div', 'body');
    if (kind === 'reply') body.append(markdown(event.text)); else body.textContent = event.text;
    const when = kind === 'system' ? `notification · ${clock(event.ts)}` : clock(event.ts);
    message.append(el('div', 'when', when), body);
    thread.append(message);
  }
  if (events.length && pinned) log.scrollTop = log.scrollHeight;
}

async function sendMessage(message) {
  let result;
  if (selected === ORCH) result = await call('/api/orchestrator', { message });
  else if (selected.startsWith('lead:'))
    result = await call(`/api/projects/${encodeURIComponent(selected.slice(5))}/lead`, { message });
  else result = await call(`/api/agents/${encodeURIComponent(selected)}/messages`, { message });
  if (result.agent_id !== selected) {  // a lead that was just created: keep the view, adopt its id
    selected = result.agent_id; shown = selected; location.hash = encodeURIComponent(selected);
  }
  await refresh();
}

async function editQueued(run) {
  const dialog = $('editDialog'), text = dialog.querySelector('[name=message]');
  let full;
  try { full = await call(`/api/runs/${run.id}`); } catch (error) { toast(error.message, true); return; }
  dialog.querySelector('[data-run]').textContent = `#${run.id}`;
  openDialog(dialog, async data => {
    await call(`/api/runs/${run.id}/edit`, { message: data.get('message') });
    toast('Queued message updated'); await refresh();
  });
  text.value = full.prompt; text.focus();
}

function withdrawQueued(run) {
  const what = run.kind === 'compact' ? 'compaction' : 'message';
  confirmDialog({
    title: `Withdraw queued ${what} #${run.id}?`,
    text: `It never runs; turns that wait for it are skipped. “${run.prompt.replace(/\s+/g, ' ').slice(0, 200)}”`,
    ok: 'Withdraw',
    action: async () => {
      await call(`/api/runs/${run.id}/withdraw`, {});
      toast(`Queued ${what} withdrawn`); await refresh();
    },
  });
}

function compactAgent(id) {
  const dialog = $('compactDialog');
  dialog.querySelector('[data-agent]').textContent = id;
  dialog.querySelector('[data-context]').textContent = shownAgent?.id === id ? contextTitle(shownAgent.context) : '';
  openDialog(dialog, async data => {
    await call(`/api/agents/${encodeURIComponent(id)}/compact`, { instructions: data.get('instructions') });
    toast('Compaction queued'); await refresh();
  });
  dialog.querySelector('[name=instructions]').focus();
}

async function stopAgent() {
  try { await call(`/api/agents/${encodeURIComponent(selected)}/stop`, {}); toast('Stopped'); await refresh(); }
  catch (error) { toast(error.message, true); }
}

function removeAgent(id = selected, branch = undefined) {
  if (branch === undefined) branch = shownAgent?.id === id ? shownAgent.branch : findArchived(id)?.branch;
  confirmDialog({
    title: `Delete ${id} for good?`,
    text: branch
      ? 'Its record, conversation and logs are deleted, and its worktree too: uncommitted work in it is lost. This cannot be undone.'
      : 'Its record, conversation and logs are deleted. This cannot be undone.',
    option: branch ? `Also delete branch ${branch}` : null,
    ok: 'Delete',
    action: async option => {
      await call(`/api/agents/${encodeURIComponent(id)}/remove`, { delete_branch: option });
      toast('Agent deleted');
      if (selected === id) select(ARCHIVE); else await refresh();
    },
  });
}

async function archiveAgent(id) {
  try {
    const result = await call(`/api/agents/${encodeURIComponent(id)}/archive`, {});
    if (result.confirm) {
      confirmDialog({
        title: `Archive ${id} with unmerged work?`,
        text: `${result.confirm}. Archiving keeps all of it (branch, worktree, conversation); it only hides the agent.`,
        ok: 'Archive anyway',
        action: async () => { await call(`/api/agents/${encodeURIComponent(id)}/archive`, { confirm: true }); archived(id); },
      });
      return;
    }
    archived(id);
  } catch (error) { toast(error.message, true); }
}

function archived(id) {
  toast(`${id} archived; restore it from the Archive`);
  refresh();
}

async function restoreAgent(id) {
  try { await call(`/api/agents/${encodeURIComponent(id)}/restore`, {}); toast(`${id} restored`); }
  catch (error) { toast(error.message, true); }
  archiveKey = ''; await refresh();
}

function archiveSafe(project) {
  confirmDialog({
    title: `Archive finished agents of ${project.name}?`,
    text: 'Every agent that is not working and whose work is safe: no worktree of its own, or its branch merged into '
      + `${project.base_branch} and no uncommitted changes. The project lead stays. Archived agents can be restored.`,
    ok: 'Archive',
    action: async () => {
      const result = await call(`/api/projects/${encodeURIComponent(project.name)}/archive`, {});
      const kept = Object.keys(result.kept).length;
      toast(`${plural(result.archived.length, 'agent')} archived${kept ? `; ${kept} kept with unmerged work` : ''}`, false, kept ? 6000 : 0);
      await refresh();
    },
  });
}

function removeProject(project) {
  confirmDialog({
    title: `Remove project ${project.name}?`,
    text: 'Only the registration is removed; the repository is not touched. A project that still has agents cannot be removed.',
    ok: 'Remove',
    action: async () => {
      await call(`/api/projects/${encodeURIComponent(project.name)}/remove`, {});
      toast('Project removed'); await refresh();
    },
  });
}

// -- archive -----------------------------------------------------------------

async function renderArchive() {
  if (shown !== ARCHIVE) {
    const page = el('div', 'archive-view'), head = el('div', 'top-bar'), who = el('div', 'who');
    const face = avatar('archive'); face.id = 'face';
    who.append(el('h2', '', 'Archive'), el('div', 'setup-note',
      'Archived agents are hidden from the lists; their conversation, branch and worktree are kept. '
      + 'A message to one restores it. Only here can an agent be deleted for good.'));
    head.append(face, who);
    const list = el('div', 'archive-list'); list.id = 'archiveList';
    page.append(head, list);
    $('view').replaceChildren(page);
    shown = ARCHIVE; archiveKey = '';
  }
  const data = await call('/api/archive');
  if (selected !== ARCHIVE) return;
  archiveList = data.agents;
  const key = JSON.stringify(archiveList);
  if (key === archiveKey) return;  // unchanged: keep the buttons (and their focus)
  archiveKey = key;
  const list = $('archiveList');
  list.replaceChildren();
  if (!archiveList.length) { list.append(el('div', 'empty-note', 'Nothing is archived.')); return; }
  for (const name of [...new Set(archiveList.map(agent => agent.project))].sort()) {
    list.append(el('h4', 'archive-project', name));
    for (const agent of archiveList.filter(a => a.project === name)) {
      const row = el('div', 'archived-row'), label = el('button', 'label');
      label.type = 'button'; label.onclick = () => select(agent.id);
      label.title = 'Open its conversation';
      const now = stateOf(agent);
      const when = `${new Date(agent.archived_at).toLocaleString()}${agent.archived_how === 'auto' ? ' · automatically' : ''}`;
      label.append(el('span', 'name', agent.id),
        el('span', 'sub', [agent.role, now.label, agent.branch, `archived ${when}`].filter(Boolean).join(' · ')));
      row.append(avatar(agent.provider, now.cls), label,
        button('ghost', 'restore', 'Restore', () => restoreAgent(agent.id)),
        button('ghost icon', 'trash', '', () => removeAgent(agent.id, agent.branch), `Delete ${agent.id} for good`));
      list.append(row);
    }
  }
}

// -- list dialogs ------------------------------------------------------------
// A header button with a count opens a dialog that lists items from `state`; every refresh
// updates the count and, while the dialog is open, the list. One entry per list:
//   button, dialog: element ids (index.html: a .count-button with a .badge; a .list-dialog with
//   [data-list] and a [data-close] button); items(): the entries; key(item): a stable id;
//   build(item): its node, built once (what is typed in it survives refreshes); empty: text.
const LIST_DIALOGS = {
  decisions: { button: 'decisionsButton', dialog: 'decisionsDialog', items: () => state.decisions || [],
    key: d => String(d.id), build: decisionItem, empty: 'No decision is waiting for you.' },
};
function renderListDialogs() { for (const spec of Object.values(LIST_DIALOGS)) renderListDialog(spec); }
function renderListDialog(spec) {
  const items = spec.items(), button = $(spec.button), badge = button.querySelector('.badge');
  badge.textContent = String(items.length); badge.hidden = !items.length;
  button.classList.toggle('has-items', items.length > 0);
  const dialog = $(spec.dialog);
  if (!dialog.open) return;
  syncList(dialog.querySelector('[data-list]'), items, spec.key, spec.build, spec.empty);
  for (const node of dialog.querySelectorAll('[data-since]')) node.textContent = ago(node.dataset.since);
}
function setupListDialog(spec) {
  const dialog = $(spec.dialog);
  $(spec.button).onclick = () => { dialog.showModal(); renderListDialog(spec); };
  dialog.querySelector('[data-close]').onclick = () => dialog.close();
}
// Keeps the nodes of items still listed in place (focus and typed text survive), inserts new
// ones in order and removes the rest.
function syncList(container, items, keyOf, build, empty) {
  const old = new Map();
  for (const node of [...container.children]) {
    if (node.dataset.key) old.set(node.dataset.key, node); else node.remove();
  }
  let at = container.firstChild;
  for (const item of items) {
    const key = keyOf(item);
    let node = old.get(key);
    old.delete(key);
    if (!node) { node = build(item); node.dataset.key = key; }
    if (node === at) at = at.nextSibling; else container.insertBefore(node, at);
  }
  for (const node of old.values()) node.remove();
  if (!items.length) container.append(el('div', 'empty-note', empty));
}
function ago(ts) {
  const minutes = Math.max(0, Math.floor((Date.now() - new Date(ts)) / 60000));
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} min ago`;
  return minutes < 2880 ? `${Math.floor(minutes / 60)} h ago` : `${Math.floor(minutes / 1440)} days ago`;
}

// -- decisions ---------------------------------------------------------------

const AGENT_NOTES = {
  archived: 'The agent is archived; your answer restores it.',
  gone: 'The agent no longer exists; answering closes this decision.',
};
function decisionItem(d) {
  const item = el('div', 'list-item decision'), head = el('div', 'item-head');
  const agent = el('button', 'link', d.agent_name);
  agent.type = 'button'; agent.title = 'Open its conversation';
  agent.onclick = () => { $('decisionsDialog').close(); select(d.agent_id); };
  const since = el('span', 'since', ago(d.created_at)); since.dataset.since = d.created_at;
  since.title = new Date(d.created_at).toLocaleString();
  head.append(el('span', 'chip', d.project || 'no project'), agent, el('span', '', `#${d.id}`), since);
  item.append(head, el('p', 'question', d.question));
  if (AGENT_NOTES[d.agent_state]) item.append(el('p', 'note', AGENT_NOTES[d.agent_state]));
  if (d.recommendation) item.append(el('p', 'recommendation', `Recommends: ${d.recommendation}`));
  const form = el('form', 'answer'), input = el('input', 'input'), send = el('button', 'btn primary', 'Answer');
  const controls = [input, send];
  if (d.options.length) {
    const list = el('div', 'options');
    d.options.forEach((option, index) => {
      const choice = button('', '', `${index + 1}. ${option}`, () => answerDecision(d, option, controls));
      choice.title = 'Answer with this option';
      controls.push(choice); list.append(choice);
    });
    item.append(list);
  }
  input.placeholder = d.options.length ? 'Or answer in your own words…' : 'Your answer…';
  input.setAttribute('aria-label', `Answer to decision ${d.id}`);
  input.autocomplete = 'off';
  form.onsubmit = event => {
    event.preventDefault();
    if (input.value.trim()) answerDecision(d, input.value.trim(), controls);
  };
  form.append(input, send);
  item.append(form);
  return item;
}
// The decision `wantedDecision` (from the hash): the dialog opens on it, marked. One no longer
// open shows the agent that asked instead. The hash goes back to the agent shown, so the same
// decision can be asked for again.
async function showWantedDecision() {
  const id = wantedDecision, dialog = $('decisionsDialog');
  wantedDecision = null;
  if ((state.decisions || []).some(d => String(d.id) === id)) {
    history.replaceState(null, '', `#${encodeURIComponent(selected)}`);
    if (!dialog.open) dialog.showModal();
    renderListDialog(LIST_DIALOGS.decisions);
    for (const node of dialog.querySelectorAll('[data-list] > [data-key]')) {
      node.classList.toggle('marked', node.dataset.key === id);
      if (node.dataset.key !== id) continue;
      node.scrollIntoView({ block: 'center' });
      node.querySelector('input')?.focus({ preventScroll: true });
    }
    return;
  }
  const all = await call('/api/decisions?all=1').catch(() => ({ decisions: [] }));
  const decision = all.decisions.find(d => String(d.id) === id);
  if (!decision) {
    history.replaceState(null, '', `#${encodeURIComponent(selected)}`);
    toast(`There is no decision #${id}`, true); return;
  }
  if (dialog.open) dialog.close();
  toast(`Decision #${id} is no longer open`);
  if (decision.agent_id === selected) history.replaceState(null, '', `#${encodeURIComponent(selected)}`);
  else select(decision.agent_id);
}
async function answerDecision(d, answer, controls) {
  for (const control of controls) control.disabled = true;
  try {
    await call(`/api/decisions/${d.id}/answer`, { answer });
    toast(`Answer sent to ${d.agent_name}`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    for (const control of controls) control.disabled = false;
    await refresh();
  }
}

// -- task board ----------------------------------------------------------------
// One project's board in a list dialog: a list per state (syncList keeps the nodes; an item's
// node is rebuilt when it changed), add/edit in boardItemDialog, dispatch after a confirmation.

const BOARD_STATES = [['idea', 'Idea'], ['ready', 'Ready'], ['in_progress', 'In progress'], ['done', 'Done']];
let boardProject = null, boardTicket = 0;
function openBoard(name) {
  boardProject = name;
  const dialog = $('boardDialog');
  dialog.querySelector('[data-project]').textContent = name;
  for (const list of dialog.querySelectorAll('[data-list]')) list.replaceChildren();
  if (!dialog.open) dialog.showModal();
  renderBoard();
}
async function renderBoard() {
  const dialog = $('boardDialog');
  if (!dialog.open || !boardProject) return;
  const ticket = ++boardTicket, project = boardProject;
  let items;
  try { items = (await call(`/api/board?project=${encodeURIComponent(project)}`)).items; }
  catch (error) { if (ticket === boardTicket) toast(error.message, true); return; }
  if (ticket !== boardTicket || project !== boardProject || !dialog.open) return;  // a newer pass runs
  for (const section of dialog.querySelectorAll('.board-state')) {
    const mine = items.filter(item => item.state === section.dataset.state), list = section.querySelector('[data-list]');
    section.querySelector('.count').textContent = mine.length ? String(mine.length) : '';
    syncList(list, mine, item => `${item.id}:${item.updated_at}`, boardItem, 'Nothing here.');
    const nodes = [...list.querySelectorAll('.board-item')];
    nodes.forEach((node, index) => {
      node.querySelector('[data-up]').disabled = index === 0;
      node.querySelector('[data-down]').disabled = index === nodes.length - 1;
    });
  }
  const ready = items.filter(item => item.state === 'ready').length, dispatch = dialog.querySelector('[data-dispatch]');
  dispatch.textContent = ready ? `Dispatch ${plural(ready, 'ready item')}` : 'Dispatch ready items';
  dispatch.disabled = !ready;
  // The ids shown now: the confirmation names them and only they are dispatched.
  dispatch.dataset.ids = JSON.stringify(items.filter(item => item.state === 'ready').map(item => item.id));
}
function boardItem(item) {
  const node = el('div', 'list-item board-item'), head = el('div', 'item-head'), tools = el('div', 'item-tools');
  const stateSelect = el('select', 'input');
  options(stateSelect, BOARD_STATES, item.state);
  stateSelect.title = 'State'; stateSelect.setAttribute('aria-label', `State of item ${item.id}`);
  stateSelect.onchange = () => boardChange(`/api/board/${item.id}`, { state: stateSelect.value });
  const up = button('ghost icon', 'up', '', () => boardChange(`/api/board/${item.id}/move`, { direction: 'up' }), 'Move up');
  const down = button('ghost icon', 'down', '', () => boardChange(`/api/board/${item.id}/move`, { direction: 'down' }), 'Move down');
  up.dataset.up = ''; down.dataset.down = '';
  tools.append(up, down, stateSelect, button('ghost icon', 'pencil', '', () => editBoardItem(item), 'Edit'));
  head.append(el('span', 'chip', `#${item.id}`), el('span', 'title', item.title), tools);
  node.append(head);
  if (item.description) node.append(el('p', 'description', item.description));
  if (item.agent_id || item.branch) {
    const links = el('div', 'links');
    if (item.agent_id) {
      const agent = el('button', 'link', item.agent_id);
      agent.type = 'button'; agent.title = 'Open its conversation';
      agent.onclick = () => { $('boardDialog').close(); select(item.agent_id); };
      links.append(agent);
    }
    if (item.branch) { const branch = el('span', 'chip mono'); branch.append(icon('branch'), item.branch); links.append(branch); }
    node.append(links);
  }
  return node;
}
async function boardChange(path, body) {
  try { await call(path, body); } catch (error) { toast(error.message, true); }
  await renderBoard();
}
function boardItemForm(title, item) {
  const dialog = $('boardItemDialog'), form = dialog.querySelector('form');
  dialog.querySelector('[data-title]').textContent = title;
  options(form.elements.state, BOARD_STATES);
  const project = state.projects.find(p => p.name === boardProject);
  $('boardAgents').replaceChildren(...(project ? project.agents : []).map(a => { const o = el('option'); o.value = a.id; return o; }));
  form.elements.title.value = item.title || ''; form.elements.description.value = item.description || '';
  form.elements.state.value = item.state || 'idea';
  form.elements.agent.value = item.agent_id || ''; form.elements.branch.value = item.branch || '';
  return dialog;
}
const boardFields = data => ({ title: data.get('title'), description: data.get('description'), state: data.get('state'),
  agent: data.get('agent').trim(), branch: data.get('branch').trim() });
function addBoardItem() {
  const project = boardProject, dialog = boardItemForm(`New item in ${project}`, {});
  openDialog(dialog, async data => {
    const item = await call('/api/board/add', { project, ...boardFields(data) });  // all or nothing
    toast(`#${item.id} added`); await renderBoard();
  });
  dialog.querySelector('[name=title]').focus();
}
function editBoardItem(item) {
  const dialog = boardItemForm(`Edit #${item.id}`, item);
  openDialog(dialog, async data => {
    // Only what the owner changed: a state or link set meanwhile by an agent stays.
    const was = { title: item.title, description: item.description, state: item.state,
      agent: item.agent_id || '', branch: item.branch || '' };
    const changed = Object.fromEntries(Object.entries(boardFields(data)).filter(([key, value]) => value !== was[key]));
    if (!Object.keys(changed).length) return;
    await call(`/api/board/${item.id}`, changed);
    toast(`#${item.id} saved`); await renderBoard();
  });
  dialog.querySelector('[name=title]').focus();
}
function dispatchBoard() {
  const project = boardProject, ids = JSON.parse($('boardDialog').querySelector('[data-dispatch]').dataset.ids || '[]');
  if (!ids.length) return;
  confirmDialog({
    title: `Dispatch ${plural(ids.length, 'ready item')} of ${project}?`,
    text: `${ids.map(id => `#${id}`).join(', ')} go to the project lead as one message from you, and become in progress, `
      + 'linked to the lead. The lead updates their states as the work proceeds.',
    ok: 'Dispatch',
    action: async () => {
      const result = await call('/api/board/dispatch', { project, ids });
      const sent = result.items.length, gone = ids.length - sent;
      toast(sent ? `${plural(sent, 'item')} sent to ${result.agent_id}${gone ? `; ${gone} no longer ready, not sent` : ''}`
        : 'None of them is ready any more; nothing sent', false, gone ? 6000 : 0);
      await refresh(); await renderBoard();
    },
  });
}
function setupBoard() {
  const dialog = $('boardDialog');
  dialog.querySelector('[data-close]').onclick = () => dialog.close();
  dialog.querySelector('[data-add]').onclick = addBoardItem;
  dialog.querySelector('[data-dispatch]').onclick = dispatchBoard;
}

// -- dialogs -----------------------------------------------------------------

function openDialog(dialog, submit) {
  const form = dialog.querySelector('form'), error = dialog.querySelector('.error');
  error.textContent = '';
  form.onsubmit = async event => {
    if (event.submitter?.value !== 'ok') return;  // cancel: let the dialog close
    event.preventDefault();
    event.submitter.disabled = true;
    try { await submit(new FormData(form)); dialog.close(); form.reset(); }
    catch (failure) { error.textContent = failure.message; }
    finally { event.submitter.disabled = false; }
  };
  dialog.showModal();
}

function choices(container, type, name, items, value, onchange) {
  // items: [value, title, description?]; rendered as radios or checkboxes inside labels
  container.replaceChildren(...items.map(([itemValue, title, description]) => {
    const label = el('label'), input = el('input'), face = el('span');
    input.type = type; input.name = name; input.value = itemValue;
    input.checked = type === 'radio' && itemValue === value;
    if (onchange) input.onchange = onchange;
    if (description !== undefined) face.append(el('b', '', title), el('small', '', description)); else face.textContent = title;
    label.append(input, face);
    return label;
  }));
}
function options(select, items, value) {
  select.replaceChildren(...items.map(([itemValue, label]) => {
    const option = el('option', '', label); option.value = itemValue; return option;
  }));
  if (value !== undefined) select.value = value;
}

function confirmDialog({ title, text, option, ok, action }) {
  const dialog = $('confirmDialog'), label = dialog.querySelector('[data-option]');
  dialog.querySelector('[data-title]').textContent = title;
  dialog.querySelector('[data-text]').textContent = text;
  dialog.querySelector('[data-ok]').textContent = ok;
  label.hidden = !option;
  label.querySelector('span').textContent = option || '';
  openDialog(dialog, data => action(data.get('option') === 'on'));
}

// -- add project: folder picker ---------------------------------------------

let folder = null;

async function loadFolder(path) {
  const dialog = $('projectDialog');
  try {
    folder = await call(`/api/browse${path ? `?path=${encodeURIComponent(path)}` : ''}`);
    store('orch-folder', folder.path);
    $('pickerFilter').value = '';
    dialog.querySelector('.error').textContent = '';
    renderFolder();
  } catch (error) {
    if (!folder) { folder = { path: '', parent: null, home: '', entries: [], git: false }; renderFolder(); }
    dialog.querySelector('.error').textContent = error.message;
  }
}

function chooseRepository(path) {
  const dialog = $('projectDialog');
  dialog.querySelector('[name=path]').value = path;
  dialog.querySelector('[name=name]').placeholder = basename(path) || 'defaults to the folder name';
  renderFolder();
}

function renderFolder() {
  const dialog = $('projectDialog'), chosen = dialog.querySelector('[name=path]').value;
  const crumbs = $('pickerCrumbs');
  crumbs.replaceChildren();
  let walked = '';
  for (const part of ['', ...folder.path.split('/').filter(Boolean)]) {
    walked = part ? `${walked}/${part}` : '';
    const target = walked || '/';
    const crumb = el('button', '', part || '/'); crumb.type = 'button';
    crumb.onclick = () => loadFolder(target);
    crumbs.append(crumb, el('i', '', part ? '/' : ''));
  }
  crumbs.lastChild?.remove();
  crumbs.scrollLeft = crumbs.scrollWidth;
  $('pickerUp').disabled = !folder.parent;
  $('pickerHere').hidden = !folder.git || Boolean(folder.project);

  const filter = $('pickerFilter').value.trim().toLowerCase();
  const entries = folder.entries.filter(entry => entry.name.toLowerCase().includes(filter));
  const list = $('pickerList');
  list.replaceChildren();
  if (!entries.length) list.append(el('div', 'picker-note', filter ? 'No folder matches the filter.' : 'No sub-folders here.'));
  for (const entry of entries) {
    const row = el('div', `folder${entry.git ? ' git' : ''}${entry.path === chosen ? ' selected' : ''}`);
    const main = el('button', 'folder'); main.type = 'button';
    main.style.padding = '0'; main.style.flex = '1'; main.style.minWidth = '0';
    main.append(icon(entry.git ? 'branch' : 'folder', 'kind'), el('span', 'fname', entry.name));
    if (entry.project) main.append(el('span', 'tag added', `added as ${entry.project}`));
    else if (entry.git) main.append(el('span', 'tag', 'git repository'));
    main.onclick = () => (entry.git && !entry.project ? chooseRepository(entry.path) : loadFolder(entry.path));
    main.ondblclick = () => loadFolder(entry.path);
    const open = button('ghost icon open', 'right', '', () => loadFolder(entry.path), `Open ${entry.name}`);
    row.append(main, open);
    list.append(row);
  }
}

function openProjectDialog() {
  const dialog = $('projectDialog');
  dialog.querySelector('form').reset();
  choices(dialog.querySelector('[data-segment=provider]'), 'radio', 'provider',
    state.providers.map(p => [p.name, p.name]), 'claude');
  $('pickerUp').onclick = () => folder?.parent && loadFolder(folder.parent);
  $('pickerHome').onclick = () => loadFolder(folder?.home || '');
  $('pickerUseHere').onclick = () => chooseRepository(folder.path);
  $('pickerFilter').oninput = renderFolder;
  $('pickerFilter').onkeydown = event => { if (event.key === 'Enter') event.preventDefault(); };
  folder = null;
  $('pickerList').replaceChildren(el('div', 'picker-note', 'Loading…'));
  loadFolder(stored('orch-folder', ''));
  setTimeout(() => $('pickerFilter').focus());
  openDialog(dialog, async data => {
    const project = await call('/api/projects', Object.fromEntries(data));
    toast(project.note ? `Project ${project.name} added. ${project.note}` : `Project ${project.name} added`, false, project.note ? 12000 : 0);
    await refresh();
  });
}

// -- new agent ---------------------------------------------------------------

function inherited(tier, providerName, key, projectName) {
  const from = scope => scope?.[tier]?.[providerName]?.[key];
  return from(state.settings.projects[projectName]) || from(state.settings.global);
}

function openAgentDialog(project) {
  const dialog = $('agentDialog'), field = name => dialog.querySelector(`[name=${name}]`);
  dialog.querySelector('form').reset();
  dialog.querySelector('[data-project]').textContent = project.name;
  const current = name => dialog.querySelector(`[name=${name}]:checked`)?.value;
  const sync = () => {
    const role = state.roles.find(r => r.name === current('role')), chosen = provider(current('provider'));
    const tier = role?.lead ? 'lead' : 'worker';
    const model = inherited(tier, chosen.name, 'model', project.name);
    const effort = inherited(tier, chosen.name, 'effort', project.name);
    $('agentModels').replaceChildren(...modelOptions(chosen));
    field('model').placeholder = model ? `${model} (from settings)` : 'default';
    field('model').title = chosen.models_note || '';
    options(field('effort'), [['', effort ? `${effort} (from settings)` : 'default'], ...chosen.efforts.map(e => [e, e])], '');
  };
  choices(dialog.querySelector('[data-cards=role]'), 'radio', 'role',
    state.roles.map(r => [r.name, r.name, r.description]), 'worker', sync);
  choices(dialog.querySelector('[data-segment=provider]'), 'radio', 'provider',
    state.providers.map(p => [p.name, p.name]), project.default_provider, sync);
  options(field('workspace'), [['', 'Default for the role'], ['own', 'Own worktree and branch'], ['main', 'Main checkout of the project'],
    ...project.agents.filter(a => a.branch).map(a => [`share:${a.id}`, `Share the worktree of ${a.id}`])]);
  dialog.querySelector('[data-after-field]').hidden = !project.agents.length;
  choices(dialog.querySelector('[data-checks=after]'), 'checkbox', 'after', project.agents.map(a => [a.id, a.id]));
  sync();
  openDialog(dialog, async data => {
    const workspace = data.get('workspace');
    const result = await call('/api/agents', {
      project: project.name, task: data.get('task'), name: data.get('name'), role: data.get('role'),
      provider: data.get('provider'), model: data.get('model'), effort: data.get('effort'),
      worktree: workspace === 'own' ? true : workspace === 'main' ? false : null,
      share_with: workspace.startsWith('share:') ? workspace.slice(6) : null,
      after: data.getAll('after'),
    });
    toast('Agent started'); select(result.agent_id);
  });
}

// -- settings ----------------------------------------------------------------

function renderSettings() {
  const dialog = $('settingsDialog'), scope = dialog.querySelector('[name=scope]').value;
  const data = scope ? state.settings.projects[scope] || {} : state.settings.global;
  $('settingsHelp').textContent = scope
    ? `Overrides for ${scope}. Empty fields inherit the global default.`
    : 'Defaults for every project. An agent started with an explicit model or effort keeps its own.';
  const rows = $('settingsRows');
  rows.replaceChildren();
  for (const tier of state.tiers) {
    if (scope && tier === 'orchestrator') continue;
    const box = el('div', 'tier'), [title, about] = TIER_INFO[tier] || [tier, ''];
    box.append(el('h4', '', title), el('p', 'about', typeof about === 'function' ? about() : about));
    for (const p of state.providers) {
      if (tier === 'orchestrator' && p.name !== 'claude') continue;
      const row = el('div', 'tier-row'), who = el('span', 'who');
      who.append(avatar(p.name), p.name);
      const model = el('input', 'input'), effort = el('select', 'input'), list = el('datalist');
      const base = scope ? state.settings.global?.[tier]?.[p.name] || {} : {};
      list.id = `models-${tier}-${p.name}`;
      list.append(...modelOptions(p));
      model.name = `${tier}.${p.name}.model`; model.setAttribute('list', list.id); model.autocomplete = 'off';
      model.value = data?.[tier]?.[p.name]?.model || '';
      model.placeholder = base.model ? `${base.model} (global)` : 'default model';
      model.setAttribute('aria-label', `${title} ${p.name} model`);
      model.title = p.models_note || '';
      effort.name = `${tier}.${p.name}.effort`;
      effort.setAttribute('aria-label', `${title} ${p.name} effort`);
      options(effort, [['', base.effort ? `${base.effort} (global)` : 'default effort'], ...p.efforts.map(e => [e, e])],
        data?.[tier]?.[p.name]?.effort || '');
      row.append(who, model, effort, list);
      box.append(row);
    }
    rows.append(box);
  }
  const key = state.archive_after_key, box = el('div', 'tier');
  const field = el('input', 'input'), global = state.settings.global?.[key];
  box.append(el('h4', '', 'Archive'), el('p', 'about',
    'Finished agents are archived after this many idle hours, if their work is safe (no worktree of their own, '
    + `or merged and clean). Never project leads or ${state.orchestrator_name}. 0 or “off”: never.`));
  field.name = key; field.autocomplete = 'off'; field.inputMode = 'decimal';
  field.value = data?.[key] ?? '';
  field.placeholder = scope ? `${global ?? state.archive_after_default} (global)` : `${state.archive_after_default} (default)`;
  field.setAttribute('aria-label', 'Archive after idle hours');
  const row = el('div', 'tier-row'); row.append(el('span', 'who', 'hours'), field);
  box.append(row);
  rows.append(box);
  rows.append(compactAtBox(scope, data));
  if (!scope) rows.append(...SWITCHES.map(switchBox));
}

function compactAtBox(scope, data) {
  const key = state.compact_at_key, box = el('div', 'tier');
  const field = el('input', 'input'), global = state.settings.global?.[key];
  box.append(el('h4', '', 'Automatic compaction'), el('p', 'about',
    'When a turn ends with the context above this many tokens, the conversation is compacted before the next turn '
    + '(ahead of a queued message). Leads keep their rules, state and open work. Not again while the last '
    + 'compaction left it above. 0 or “off”: never.'));
  field.name = key; field.autocomplete = 'off';
  field.value = data?.[key] ?? '';
  field.placeholder = scope ? `${global ?? state.compact_at_default} (global)` : `${state.compact_at_default} (default)`;
  field.setAttribute('aria-label', 'Compact above context tokens');
  const row = el('div', 'tier-row'); row.append(el('span', 'who', 'tokens'), field);
  box.append(row);
  return box;
}

// Global-only on/off settings, on unless set to false (settings.SWITCHES).
const SWITCHES = [
  { key: 'keep_awake', title: 'Keep awake', label: 'Keep the machine awake while agents work',
    about: 'While an agent’s turn runs, this machine does not go to sleep on idle (the display may).' },
  { key: 'desktop_notifications', title: 'Notifications', label: 'Show desktop notifications',
    about: 'On this machine: when an agent asks you for a decision, and when an agent you messaged directly has finished. '
      + 'An open dashboard page allowed to show notifications shows them (a click opens the agent or the decision); '
      + 'without one, macOS shows them.' },
];
function switchBox({ key, title, label: text, about }) {
  const box = el('div', 'tier'), label = el('label', 'switch-row'), check = el('input');
  check.type = 'checkbox'; check.name = key; check.checked = state.settings.global?.[key] !== false;
  label.append(check, text);
  box.append(el('h4', '', title), el('p', 'about', about), label);
  if (key === 'desktop_notifications' && PageNotification) box.append(pagePermission());
  return box;
}

function openSettings() {
  const dialog = $('settingsDialog'), scope = dialog.querySelector('[name=scope]');
  options(scope, [['', 'All projects (global defaults)'], ...state.projects.map(p => [p.name, `Project: ${p.name}`])], '');
  scope.onchange = renderSettings;
  renderSettings();
  openDialog(dialog, async form => {
    const data = {};
    for (const [name, value] of form) {
      const [tier, providerName, key] = name.split('.');
      if (key) ((data[tier] ??= {})[providerName] ??= {})[key] = value;
      else if (name === state.archive_after_key || name === state.compact_at_key) data[name] = value;
    }
    if (!form.get('scope')) for (const { key } of SWITCHES) data[key] = form.get(key) === 'on';
    await call('/api/settings', { project: form.get('scope') || null, data });
    toast('Settings saved'); await refresh();
  });
}

// -- notifications from this page ----------------------------------------------
// With the browser's permission this page shows the desktop notifications (desktop.py: notices)
// itself, so that a click opens the agent or the decision. It polls with a POST (the same-origin
// check keeps other sites from faking it); while the polls come, osascript leaves new notices to
// the page. Each notice is claimed first, so two pages, or a page and osascript, never show the
// same one. Without the Notification API (an embedded WebKit window) none of this runs and
// osascript shows them as before.
// Hidden pages: browsers throttle the page's own timers (Chrome: once a minute after five
// minutes hidden), which would let the marker go stale, so the poll is ticked by a dedicated
// worker (static/tick.js), whose timers are not throttled with the page.
// The cursor (the last notice id dealt with) lives in sessionStorage, so a reload of the tab
// carries on where it was: a notice recorded meanwhile was left to this page (the marker was
// still fresh) and is shown after the reload; one older than NOTICE_MAX_AGE_MS is no news and
// skipped. The cursor moves past a notice only once its claim got an answer; a claim that failed
// is tried again on the next tick.
const PageNotification = window.Notification;
const NOTICE_POLL_MS = 4000, NOTICE_CHARS = 180, NOTICE_MAX_AGE_MS = 10 * 60 * 1000;
const NOTICE_CURSOR = 'orch-notice-cursor';  // sessionStorage key
let noticeCursor = savedNoticeCursor(), noticePolling = false;  // null: none yet (replay nothing)

function savedNoticeCursor() {
  try {
    const raw = sessionStorage.getItem(NOTICE_CURSOR), value = Number(raw);
    return raw !== null && Number.isSafeInteger(value) && value >= 0 ? value : null;
  } catch (error) {
    return null;
  }
}
function moveNoticeCursor(id) {
  noticeCursor = id;
  try { sessionStorage.setItem(NOTICE_CURSOR, String(id)); } catch (error) { /* in memory only */ }
}
async function pollNotices() {
  if (!PageNotification || PageNotification.permission !== 'granted' || noticePolling) return;
  noticePolling = true;
  try {
    const data = await call('/api/notices/poll', { after: noticeCursor });
    // None yet, or one above the server's (its table was pruned, or another home serves on this
    // port): start from the server's, replaying nothing.
    if (noticeCursor === null || noticeCursor > data.cursor) { moveNoticeCursor(data.cursor); return; }
    for (const notice of data.notices) {
      if (Date.now() - Date.parse(notice.created_at) > NOTICE_MAX_AGE_MS) { moveNoticeCursor(notice.id); continue; }
      let claimed;
      try {
        claimed = (await call(`/api/notices/${notice.id}/claim`, {})).claimed;
      } catch (error) {
        return;  // no answer: this notice and the ones after it on the next tick
      }
      moveNoticeCursor(notice.id);
      if (!claimed) continue;
      try { showNotice(notice); } catch (error) { /* the browser refused it */ }
    }
    moveNoticeCursor(Math.max(noticeCursor, data.cursor));
  } catch (error) {
    /* the next tick tries again */
  } finally {
    noticePolling = false;
  }
}
function showNotice(notice) {
  const text = notice.text.length > NOTICE_CHARS ? `${notice.text.slice(0, NOTICE_CHARS - 1)}…` : notice.text;
  const bubble = new PageNotification(state.project_name || document.title, { body: text, tag: `notice-${notice.id}` });
  bubble.onclick = () => {
    window.focus();
    bubble.close();
    const decision = notice.kind === 'decision' && notice.decision_id != null;
    const fragment = decision ? `${DECISION}${notice.decision_id}` : encodeURIComponent(notice.agent_id);
    if (location.hash === `#${fragment}`) followHash(); else location.hash = fragment;
  };
}
function startNoticePoll() {
  if (!PageNotification) return;
  try {
    const ticker = new Worker('/static/tick.js');
    ticker.onmessage = pollNotices;
    ticker.postMessage(NOTICE_POLL_MS);
  } catch (error) {
    setInterval(pollNotices, NOTICE_POLL_MS);  // throttled while hidden, but still a poll
  }
  pollNotices();
}
// The permission, in the settings: asked for only from this click.
function pagePermission() {
  const line = el('p', 'about page-notify'), permission = PageNotification.permission;
  if (permission === 'granted') {
    line.append('This page shows them; a click opens the agent or the decision.');
  } else if (permission === 'denied') {
    line.append('This browser blocks notifications from this page. To have a click open the agent or decision, '
      + 'allow them in the site settings (Chrome: the icon left of the address; Safari: Settings › Websites › Notifications).');
  } else {
    line.append('Let this page show them, so that a click opens the agent or the decision: ',
      button('ghost', '', 'Allow in this browser', async () => {
        try { await PageNotification.requestPermission(); } catch (error) { /* stays as it was */ }
        line.replaceWith(pagePermission());
        pollNotices();
      }));
  }
  return line;
}

// -- theme -------------------------------------------------------------------

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $('themeButton').replaceChildren(icon(theme === 'dark' ? 'sun' : 'moon'));
}

// -- refresh loop ------------------------------------------------------------

async function refresh() {
  if (refreshing) { again = true; return; }  // rerun once the current pass is done
  refreshing = true;
  const ticket = setupTicket();  // answers older than a save do not touch the model controls
  try {
    state = await call('/api/state');
    if (selected.startsWith('lead:')) {
      const project = state.projects.find(p => p.name === selected.slice(5));
      const lead = project && leadOf(project);
      if (lead) { selected = lead.id; location.hash = encodeURIComponent(selected); shown = null; }
    }
    renderNav();
    renderListDialogs();
    if (wantedDecision !== null) showWantedDecision();
    renderBoard();
    const target = selected;
    if (target === ARCHIVE) { await renderArchive(); return; }
    const exists = Boolean(findAgent(target)) || isArchived(target);
    const virtual = !exists && (target === ORCH || target.startsWith('lead:'));
    if (!exists && !virtual) {
      const gone = el('div', 'welcome'); gone.append(el('h3', '', 'This agent no longer exists'));
      $('view').replaceChildren(gone); shown = null; return;
    }
    if (shown !== target) buildView();
    if (virtual) { renderHeader(null, setupFresh(ticket, target)); return; }
    const data = await call(`/api/agents/${encodeURIComponent(target)}/events?after=${lastEvent}`);
    if (target !== selected || shown !== target) return;  // the selection changed meanwhile
    shownAgent = data.agent;
    renderHeader(data.agent, setupFresh(ticket, target));
    appendEvents(data.events);
  } catch (error) {
    $('summary').textContent = `Disconnected: ${error.message}`;
  } finally {
    refreshing = false;
    if (again) { again = false; refresh(); }
  }
}

for (const node of document.querySelectorAll('[data-icon]')) node.append(icon(node.dataset.icon));
applyTheme(document.documentElement.dataset.theme);
$('themeButton').onclick = () => {
  const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  try { localStorage.setItem('orch-theme', theme); } catch (error) { /* optional */ }
  applyTheme(theme);
};
$('settingsButton').onclick = openSettings;
for (const spec of Object.values(LIST_DIALOGS)) setupListDialog(spec);
setupBoard();
window.addEventListener('pagehide', () => flushSetup(true));
function followHash() {
  const target = hashTarget();
  if (target.startsWith(DECISION)) { wantedDecision = target.slice(DECISION.length); refresh(); return; }
  const id = target || ORCH;
  if (id !== selected) select(id);
}
window.addEventListener('hashchange', followHash);
refresh();
setInterval(refresh, 2000);
startNoticePoll();
