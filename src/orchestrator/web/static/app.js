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
  home: '<path d="M4 11 12 4l8 7M6 10v9h12v-9"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  branch: '<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="8" r="2"/><path d="M6 7v10M18 10c0 4-6 3-6 7"/>',
  chat: '<path d="M4 5h16v11H9l-5 4z"/>',
  send: '<path d="M12 19V5M5 12l7-7 7 7"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
  sliders: '<path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/>',
  moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5"/>',
};
const TIER_INFO = {
  orchestrator: ['Orchestrator', 'The top-level agent you talk to here.'],
  lead: ['Project lead', 'Plans the work of one project and runs its agents.'],
  worker: ['Workers', 'Every other role: worker, planner, reviewer, tester.'],
};
const SUGGESTIONS = [
  'Which projects and agents exist, and what is each of them doing?',
  'In project <name>: plan the following feature, split it into tasks and implement them with workers: ',
  'Review the unmerged agent branches in project <name> and tell me which are ready to merge.',
];

let state = { orchestrator: null, projects: [], providers: [], roles: [], tiers: [], settings: { global: {}, projects: {} } };
let selected = decodeURIComponent(location.hash.slice(1)) || ORCH;
let shown = null, lastEvent = 0, refreshing = false, again = false, toolGroup = null;

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
  if (provider === ORCH) node.append(icon('logo')); else node.append((provider || '?').slice(0, 2).replace(/^./, c => c.toUpperCase()));
  if (status !== undefined) node.append(el('span', 'dot'));
  return node;
}
const stored = (key, fallback) => { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch (error) { return fallback; } };
const store = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); } catch (error) { /* optional */ } };
const clock = ts => new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
const basename = path => path.replace(/\/+$/, '').split('/').pop();

async function call(path, body) {
  const init = body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
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
const leadOf = project => project.agents.find(a => a.role === 'lead' && (!a.parent_id || a.parent_id === ORCH));
const provider = name => state.providers.find(p => p.name === name) || { name, models: [], efforts: [] };
const isActive = agent => agent && (agent.status === 'running' || agent.status === 'waiting');

function select(id) {
  selected = id; location.hash = encodeURIComponent(id); shown = null; refresh();
}

// -- markdown (builds DOM nodes; never parses agent text as HTML) -----------

function count(n) {
  return n < 1000 ? String(n) : n < 1e6 ? `${(n / 1000).toFixed(1)}k` : `${(n / 1e6).toFixed(1)}M`;
}

// Prompt tokens (fresh + cache reads + cache writes) and output tokens, or null when unknown.
function tokens(usage) {
  if (!usage || usage.input_tokens == null) return null;
  return [usage.input_tokens + (usage.cache_read_tokens || 0) + (usage.cache_creation_tokens || 0), usage.output_tokens || 0];
}

function tokenText(usage) {
  const t = tokens(usage);
  return t ? `${count(t[0])} in · ${count(t[1])} out` : '';
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
  const key = JSON.stringify([state.orchestrator, state.projects, selected, collapsed]);
  if (key === navKey) return;
  navKey = key;
  const nav = $('nav');
  nav.replaceChildren();

  const orch = state.orchestrator;
  const top = navItem(ORCH, ORCH, orch ? orch.status : '', 'Orchestrator',
    orch ? `${orch.status} · talks to every project` : 'Tell it what you want built', 0);
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

  let total = 0, busy = 0, cost = orch ? orch.cost_usd : 0, used = tokens(orch && orch.usage) || [0, 0];
  const addTokens = usage => { const t = tokens(usage); if (t) used = [used[0] + t[0], used[1] + t[1]]; };
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
      button('ghost icon', 'x', '', () => removeProject(project), 'Remove project'));
    head.append(toggle, actions);
    const agents = el('div', 'agents');
    const ids = new Set(project.agents.map(a => a.id));
    const add = (parent, depth) => {
      for (const agent of project.agents.filter(a => (ids.has(a.parent_id) ? a.parent_id : null) === parent)) {
        total++; cost += agent.cost_usd; addTokens(agent.usage);
        if (isActive(agent)) busy++;
        agents.append(navItem(agent.id, agent.provider, agent.status, agent.id, `${agent.role} · ${agent.status}`, depth));
        add(agent.id, depth + 1);
      }
    };
    add(null, 0);
    if (!project.agents.length) agents.append(el('div', 'empty-note', 'No agents yet.'));
    box.append(head, agents);
    nav.append(box);
  }
  $('summary').textContent = `${total} agents · ${busy} active · ${count(used[0] + used[1])} tokens · $${cost.toFixed(2)} spent`;
}

function navItem(id, providerName, status, name, sub, depth) {
  const row = el('button', `nav-item${id === selected ? ' selected' : ''}`);
  row.type = 'button';
  row.style.paddingLeft = `${8 + depth * 16}px`;
  const label = el('span', 'label');
  label.append(el('span', 'name', name), el('span', 'sub', sub));
  row.append(avatar(providerName, status), label);
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
  const chips = el('div', 'chips'); chips.id = 'chips';
  who.append(title, chips);
  const detailsButton = button('ghost', 'info', 'Details', () => { $('details').hidden = !$('details').hidden; });
  const stop = button('danger', 'stop', 'Stop', stopAgent);
  const remove = button('ghost icon', 'trash', '', removeAgent, 'Remove agent');
  detailsButton.id = 'detailsButton'; stop.id = 'stop'; remove.id = 'remove';
  top.append(face, who, detailsButton, stop, remove);

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
  view.append(top, details, log, busy, form);
  shown = selected; lastEvent = 0; toolGroup = null;
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
    el('h3', '', isOrch ? 'Talk to the orchestrator' : leadProject ? `Lead of ${leadProject}` : 'No messages yet'),
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

function renderHeader(agent) {
  const leadProject = selected.startsWith('lead:') ? selected.slice(5) : null;
  const title = $('title'), chips = $('chips');
  const name = agent ? (agent.id === ORCH ? 'Orchestrator' : agent.id) : leadProject ? `${leadProject} lead` : 'Orchestrator';
  const face = avatar(selected === ORCH ? ORCH : agent ? agent.provider : 'claude');
  face.id = 'face'; $('face').replaceWith(face);
  title.replaceChildren(name, el('span', `pill ${agent ? agent.status : ''}`, agent ? agent.status : 'not started'));
  $('stop').hidden = !isActive(agent);
  $('remove').hidden = !agent;
  $('detailsButton').hidden = !agent;
  renderWelcome(agent, leadProject);

  const busy = $('busy');
  busy.replaceChildren();
  if (agent && agent.status === 'running') {
    const dots = el('span', 'typing'); dots.append(el('i'), el('i'), el('i'));
    busy.append(dots, 'working…');
  } else if (agent && agent.status === 'waiting') {
    busy.append('queued — waiting for its turn or for the agents it depends on');
  }

  chips.replaceChildren();
  if (!agent) return;
  const chip = (text, iconName) => {
    if (!text) return;
    const node = el('span', 'chip'); if (iconName) node.append(icon(iconName)); node.append(text); chips.append(node);
  };
  const effective = agent.effective || {};
  chip(agent.role); chip(agent.provider);
  chip(effective.model || 'default model');
  chip(effective.effort && `effort ${effective.effort}`);
  chip(agent.branch, 'branch');
  chip(tokenText(agent.usage));
  chip(agent.cost_usd ? `$${agent.cost_usd.toFixed(2)}` : '');

  const facts = $('facts'); facts.replaceChildren();
  const fact = (key, value) => { if (value) facts.append(el('dt', '', key), el('dd', '', String(value))); };
  fact('Project', agent.project); fact('Started by', agent.parent_id); fact('Workspace', agent.cwd);
  fact('Session', agent.session_id); fact('Turns', `${agent.runs} (${agent.queued_runs} active)`);
  fact('Last run model', agent.last_run && agent.last_run.usage && agent.last_run.usage.model);
  fact('Take over in a terminal', agent.session_id ? `orch attach ${agent.id}` : '');
  const runs = $('runs'); runs.replaceChildren();
  const row = (cells, tag) => { const tr = el('tr'); for (const c of cells) tr.append(el(tag, '', c)); runs.append(tr); };
  if ((agent.recent_runs || []).length) row(['Run', 'Status', 'Ended', 'Tokens', 'Cost', 'Model'], 'th');
  for (const run of agent.recent_runs || []) {
    row([`#${run.id}`, run.status, (run.ended_at || '').slice(11, 19), tokenText(run) || '–',
      run.cost_usd != null ? `$${run.cost_usd.toFixed(4)}` : '–', run.model || ''], 'td');
  }
  $('changes').textContent = agent.changes || '';
  $('changes').hidden = !agent.changes;
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
    const kind = event.kind === 'prompt' ? 'user' : event.kind === 'error' ? 'error' : 'reply';
    const message = el('div', `msg ${kind}`), body = el('div', 'body');
    if (kind === 'reply') body.append(markdown(event.text)); else body.textContent = event.text;
    message.append(el('div', 'when', clock(event.ts)), body);
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

async function stopAgent() {
  try { await call(`/api/agents/${encodeURIComponent(selected)}/stop`, {}); toast('Stopped'); await refresh(); }
  catch (error) { toast(error.message, true); }
}

function removeAgent() {
  const id = selected, agent = findAgent(id);
  confirmDialog({
    title: `Remove ${id}?`,
    text: agent?.branch
      ? 'Its turns are stopped and its worktree is deleted. Uncommitted work in the worktree is lost.'
      : 'Its turns are stopped and its conversation is removed from the orchestrator.',
    option: agent?.branch ? `Also delete branch ${agent.branch}` : null,
    ok: 'Remove',
    action: async option => {
      await call(`/api/agents/${encodeURIComponent(id)}/remove`, { delete_branch: option });
      toast('Agent removed'); select(ORCH);
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
    $('agentModels').replaceChildren(...chosen.models.map(name => { const o = el('option'); o.value = name; return o; }));
    field('model').placeholder = model ? `${model} (from settings)` : 'default';
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
    box.append(el('h4', '', title), el('p', 'about', about));
    for (const p of state.providers) {
      if (tier === 'orchestrator' && p.name !== 'claude') continue;
      const row = el('div', 'tier-row'), who = el('span', 'who');
      who.append(avatar(p.name), p.name);
      const model = el('input', 'input'), effort = el('select', 'input'), list = el('datalist');
      const base = scope ? state.settings.global?.[tier]?.[p.name] || {} : {};
      list.id = `models-${tier}-${p.name}`;
      list.append(...p.models.map(name => { const o = el('option'); o.value = name; return o; }));
      model.name = `${tier}.${p.name}.model`; model.setAttribute('list', list.id); model.autocomplete = 'off';
      model.value = data?.[tier]?.[p.name]?.model || '';
      model.placeholder = base.model ? `${base.model} (global)` : 'default model';
      model.setAttribute('aria-label', `${title} ${p.name} model`);
      effort.name = `${tier}.${p.name}.effort`;
      effort.setAttribute('aria-label', `${title} ${p.name} effort`);
      options(effort, [['', base.effort ? `${base.effort} (global)` : 'default effort'], ...p.efforts.map(e => [e, e])],
        data?.[tier]?.[p.name]?.effort || '');
      row.append(who, model, effort, list);
      box.append(row);
    }
    rows.append(box);
  }
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
    }
    await call('/api/settings', { project: form.get('scope') || null, data });
    toast('Settings saved'); await refresh();
  });
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
  try {
    state = await call('/api/state');
    if (selected.startsWith('lead:')) {
      const project = state.projects.find(p => p.name === selected.slice(5));
      const lead = project && leadOf(project);
      if (lead) { selected = lead.id; location.hash = encodeURIComponent(selected); shown = null; }
    }
    renderNav();
    const target = selected;
    const exists = Boolean(findAgent(target));
    const virtual = !exists && (target === ORCH || target.startsWith('lead:'));
    if (!exists && !virtual) {
      const gone = el('div', 'welcome'); gone.append(el('h3', '', 'This agent no longer exists'));
      $('view').replaceChildren(gone); shown = null; return;
    }
    if (shown !== target) buildView();
    if (virtual) { renderHeader(null); return; }
    const data = await call(`/api/agents/${encodeURIComponent(target)}/events?after=${lastEvent}`);
    if (target !== selected || shown !== target) return;  // the selection changed meanwhile
    renderHeader(data.agent);
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
window.addEventListener('hashchange', () => {
  const id = decodeURIComponent(location.hash.slice(1)) || ORCH;
  if (id !== selected) select(id);
});
refresh();
setInterval(refresh, 2000);
