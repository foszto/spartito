'use strict';
// The task board across projects, a view in the page's main area: #board: (every project),
// #board:<project> (one project), #board:<project>/<id> (that item's detail open; the project
// may be empty: #board:/<id>). app.js reaches it only through window.BoardView; it uses only
// these helpers of app.js, at call time: el, icon, button, call, toast, select, confirmDialog,
// plural, ago. Optional keys of an item (live, blockers, source) render nothing when absent.
(() => {
  const ROUTE = 'board:';
  const STATES = [['idea', 'Idea'], ['ready', 'Ready'], ['in_progress', 'In progress'], ['done', 'Done']];
  const LABEL = Object.fromEntries(STATES);
  const ORDER = STATES.map(([value]) => value);
  const SHOW_DONE = 'orch-board-done';  // localStorage: the Done column's cards are shown

  // Each later part plugs in here, so it can be taken out on its own:
  //   card(node, item): adds to a card; menu(item): [[label, action, disabled?], …] for its "⋯";
  //   head(root): adds to the header; paint(): after the columns were rebuilt;
  //   detail(box, item): fills the detail's extra part (called again when the item changed).
  const hooks = { card: [], menu: [], head: [], paint: [], detail: [] };
  const ui = {
    root: null, project: null, data: null, key: '', ticket: 0, holds: new Set(), pending: false,
    focus: null, showDone: false, dialog: null, detail: null,
  };

  const owns = target => target.startsWith(ROUTE);
  function parse(target) {
    const rest = target.slice(ROUTE.length), match = rest.match(/^(.*)\/(\d+)$/);
    return match ? { project: match[1], id: Number(match[2]) } : { project: rest, id: null };
  }
  const route = (project, id) => `${ROUTE}${project || ''}${id ? `/${id}` : ''}`;
  const current = () => decodeURIComponent(location.hash.slice(1));
  const remembered = () => { try { return JSON.parse(localStorage.getItem(SHOW_DONE)) === true; } catch (error) { return false; } };
  const remember = value => { try { localStorage.setItem(SHOW_DONE, JSON.stringify(value)); } catch (error) { /* optional */ } };
  const items = () => (ui.data ? ui.data.items : []);
  const find = id => items().find(item => item.id === id);
  // The items of one project in one state, in board order.
  const siblings = (project, state) => items().filter(item => item.project === project && item.state === state);

  // -- the view -------------------------------------------------------------------------

  async function render(view, target) {
    const { project, id } = parse(target);
    if (!ui.root || !view.contains(ui.root) || ui.project !== project) build(view, project);
    await load();
    syncDetail(id);
    return target;
  }

  function build(view, project) {
    ui.project = project; ui.data = null; ui.key = ''; ui.showDone = remembered();
    const root = el('div', 'bv'), head = el('div', 'top-bar bv-head'), who = el('div', 'who');
    const face = el('span', 'avatar bv-avatar'); face.append(icon('board'));
    const sub = el('div', 'bv-sub'), count = el('span', 'bv-count'), said = el('span', 'bv-said');
    said.setAttribute('aria-live', 'polite');
    sub.append(count, said);
    who.append(el('h2', '', 'Board'), sub);
    const filter = el('select', 'input bv-filter');
    filter.setAttribute('aria-label', 'Project');
    filter.onchange = () => select(route(filter.value));
    const toggle = el('label', 'bv-toggle'), done = el('input');
    done.type = 'checkbox'; done.checked = ui.showDone;
    done.onchange = () => { ui.showDone = done.checked; remember(done.checked); paint(); };
    toggle.append(done, 'Show done');
    const add = button('primary', 'plus', 'Add item', addItem);
    add.dataset.add = '';
    const tools = el('div', 'bv-tools');
    tools.append(filter, toggle, add);
    head.append(face, who, tools);
    const columns = el('div', 'bv-cols');
    for (const [state, label] of STATES) {
      const column = el('section', 'bv-col'), title = el('h3');
      column.dataset.state = state;
      title.append(label, el('span', 'count'));
      const list = el('div', 'bv-list');
      list.setAttribute('role', 'list'); list.setAttribute('aria-label', label);
      column.append(title, list);
      columns.append(column);
    }
    const failure = el('div', 'welcome bv-failed');
    failure.hidden = true;
    root.append(head, columns, failure);
    ui.root = root;
    for (const extend of hooks.head) extend(root);
    view.replaceChildren(root);
  }

  // The board as the server has it now; the columns are rebuilt only when it changed.
  async function load() {
    const ticket = ++ui.ticket, project = ui.project;
    let data;
    try {
      data = await call(`/api/board/view${project ? `?project=${encodeURIComponent(project)}` : ''}`);
    } catch (error) {
      if (ticket === ui.ticket && project === ui.project) failed(error.message);
      return;
    }
    if (ticket !== ui.ticket || project !== ui.project) return;  // a newer answer is on its way
    const key = JSON.stringify(data);
    if (key === ui.key && !ui.pending) return;
    ui.data = data; ui.key = key;
    paint();
  }

  // An answer the board could not be built from (an unknown project): said instead of the columns.
  function failed(message) {
    ui.data = null; ui.key = '';
    const box = ui.root.querySelector('.bv-failed');
    box.replaceChildren(el('h3', '', 'The board could not be loaded'), el('p', '', message));
    if (ui.project) box.append(button('', '', 'Show every project', () => select(ROUTE)));
    box.hidden = false;
    ui.root.querySelector('.bv-cols').hidden = true;
  }

  // Nothing is rebuilt while a hold is on (a drag, an open menu): it is done when it ends.
  function hold(name) { ui.holds.add(name); }
  function release(name) {
    ui.holds.delete(name);
    if (!ui.holds.size && ui.pending) paint();
  }

  function paint() {
    if (!ui.root || !ui.data) return;
    if (ui.holds.size) { ui.pending = true; return; }
    ui.pending = false;
    const data = ui.data, all = !ui.project;
    ui.root.querySelector('.bv-failed').hidden = true;
    ui.root.querySelector('.bv-cols').hidden = false;
    const filter = ui.root.querySelector('.bv-filter');
    if (filter.dataset.built !== JSON.stringify(data.projects)) {
      filter.dataset.built = JSON.stringify(data.projects);
      filter.replaceChildren(new Option('All projects', ''), ...data.projects.map(name => new Option(name, name)));
    }
    filter.value = ui.project;
    const add = ui.root.querySelector('[data-add]');
    add.disabled = !data.projects.length;
    add.title = data.projects.length ? '' : 'Register a project first';
    const shown = data.items.filter(item => item.state !== 'done' || ui.showDone).length;
    ui.root.querySelector('.bv-count').textContent = `${all ? 'All projects' : ui.project} · ${plural(data.items.length, 'item')}`
      + (shown < data.items.length ? ` (${data.items.length - shown} done hidden)` : '');

    // Focus stays on the item (and the control in it) it was on, wherever the item is now.
    const active = document.activeElement, holder = active && active.closest ? active.closest('.bv-card') : null;
    const focusId = ui.focus !== null ? ui.focus : holder && ui.root.contains(holder) ? Number(holder.dataset.id) : null;
    const part = ui.focus === null && active !== holder && active.dataset ? active.dataset.part : null;
    const moved = ui.focus !== null;
    ui.focus = null;

    for (const column of ui.root.querySelectorAll('.bv-col')) {
      const state = column.dataset.state, list = column.querySelector('.bv-list');
      const mine = data.items.filter(item => item.state === state);
      column.querySelector('.count').textContent = mine.length ? String(mine.length) : '';
      const nodes = [];
      if (state === 'done' && !ui.showDone && mine.length) {
        const more = button('ghost bv-more', '', `Show ${plural(mine.length, 'done item')}`, () => {
          ui.showDone = true; remember(true); ui.root.querySelector('.bv-toggle input').checked = true; paint();
        });
        more.dataset.more = '';
        nodes.push(more);
      } else {
        const projects = all ? [...new Set(mine.map(item => item.project))] : mine.length ? [ui.project] : [];
        for (const name of projects) {
          const group = el('div', 'bv-group');
          group.dataset.project = name;
          if (all) group.append(el('h4', '', name));
          group.append(...mine.filter(item => item.project === name).map(card));
          nodes.push(group);
        }
        if (!mine.length) nodes.push(el('div', 'bv-empty', 'Nothing here.'));
      }
      list.replaceChildren(...nodes);
    }
    if (!data.projects.length) {
      ui.root.querySelector('.bv-list').replaceChildren(el('div', 'bv-empty', 'No projects are registered yet.'));
    }
    for (const after of hooks.paint) after();

    if (focusId !== null) {
      const node = ui.root.querySelector(`.bv-card[data-id="${focusId}"]`);
      const target = node ? (part && node.querySelector(`[data-part="${part}"]`)) || node
        : moved ? ui.root.querySelector(`.bv-col[data-state="done"] [data-more]`) : null;
      if (target) target.focus({ preventScroll: !moved });
    }
  }

  function card(item) {
    const node = el('div', 'bv-card'), head = el('div', 'bv-card-head');
    node.dataset.id = String(item.id); node.dataset.project = item.project; node.dataset.state = item.state;
    node.tabIndex = 0;
    node.setAttribute('role', 'listitem');
    node.setAttribute('aria-label', `#${item.id} ${item.title}, ${LABEL[item.state]}, ${item.project}`);
    node.title = `Updated ${ago(item.updated_at)} · Enter or a click opens it`;
    head.append(el('span', 'bv-id mono', `#${item.id}`));
    node.append(head, el('div', 'bv-title', item.title));
    if (item.description) node.append(el('div', 'bv-desc', item.description));
    const meta = el('div', 'bv-meta');
    if (item.agent_id) {
      const agent = el('button', 'chip bv-agent');
      agent.type = 'button'; agent.dataset.part = 'agent';
      agent.title = `Open the conversation of ${item.agent_id}`;
      agent.append(icon('chat'), item.agent_id);
      agent.onclick = event => { event.stopPropagation(); select(item.agent_id); };
      meta.append(agent);
    }
    if (item.branch) {
      const branch = el('span', 'chip mono bv-branch');
      branch.append(icon('branch'), item.branch); branch.title = `Branch ${item.branch}`;
      meta.append(branch);
    }
    if (meta.childElementCount) node.append(meta);
    node.onclick = event => { if (!event.target.closest('button, input, a')) open(item); };
    node.addEventListener('keydown', event => keys(event, node, item));
    for (const extend of hooks.card) extend(node, item);
    return node;
  }

  // Enter opens an item; the arrows (no modifier) go to the card above or below.
  function keys(event, node, item) {
    if (event.target !== node || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (event.key === 'Enter') { event.preventDefault(); open(item); return; }
    if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
    const cards = [...node.closest('.bv-list').querySelectorAll('.bv-card')], at = cards.indexOf(node);
    const next = cards[at + (event.key === 'ArrowUp' ? -1 : 1)];
    if (next) { event.preventDefault(); next.focus(); }
  }

  // -- live state: the linked agent's status, the branch's git state ----------------------
  // From each item's `live` (the server's cached git pass; the view's `synced_at` says when it
  // last ran). `git` null (no branch, or not checked yet) shows nothing.

  const AGENT_WORDS = { unknown: 'gone' };
  const agentWord = agent => AGENT_WORDS[agent.status] || agent.status;
  function dot(status) {
    const node = el('span', 'bv-dot');
    node.dataset.status = status;
    return node;
  }
  // The branch's state as {text, cls, title}: commits ahead/behind its base, uncommitted work
  // (`dirty`: the number of changed files), merged, no such branch (`ahead` null, as
  // `orch board --live` says), or the error of the last check (`error` keeps the values seen
  // before it).
  function gitState(git) {
    const checked = `checked ${ago(git.checked_at)}`;
    if (git.error) return { text: 'git error', cls: 'error', title: `${git.branch}: ${git.error} · ${checked}` };
    if (git.merged) return { text: 'merged ✓', cls: 'merged', title: `${git.branch} is merged into its base · ${checked}` };
    if (git.ahead == null) return { text: 'no branch', cls: '', title: `There is no branch ${git.branch} in the project · ${checked}` };
    const short = [], long = [];
    if (git.ahead) { short.push(`↑${git.ahead}`); long.push(`${plural(git.ahead, 'commit')} ahead of its base`); }
    if (git.behind) { short.push(`↓${git.behind}`); long.push(`${git.behind} behind`); }
    if (git.dirty) { short.push('dirty'); long.push(`${plural(+git.dirty, 'uncommitted change')} in its worktree`); }
    if (!short.length) { short.push('no new commits'); long.push('nothing ahead of its base'); }
    return { text: short.join(' · '), cls: git.dirty ? 'dirty' : '', title: `${git.branch}: ${long.join(', ')} · ${checked}` };
  }
  function gitChip(git) {
    const state = gitState(git), node = el('span', `chip bv-git ${state.cls}`, state.text);
    node.title = state.title;
    return node;
  }
  // live.pr, the pull request of the item's branch, is reserved for a later batch: it renders
  // here, as a chip for the card and the detail. Null today, so nothing is shown.
  function prChip(pr) {
    return null;
  }

  hooks.card.push((node, item) => {
    const live = item.live;
    if (!live) return;
    const agent = node.querySelector('[data-part=agent]');
    if (agent && live.agent && live.agent.id === item.agent_id) {
      const word = agentWord(live.agent);
      agent.querySelector('svg').replaceWith(dot(live.agent.status));
      agent.append(el('span', 'bv-status', word));
      agent.title = `${item.agent_id}: ${word} · open its conversation`;
    }
    const branch = node.querySelector('.bv-branch');
    if (branch && live.git) branch.after(gitChip(live.git));
    const pr = prChip(live.pr);
    if (pr) node.querySelector('.bv-meta').append(pr);
  });
  hooks.head.push(root => {
    const synced = el('span', 'bv-synced');
    root.querySelector('.bv-said').before(synced);
  });
  hooks.paint.push(() => {
    const synced = ui.root.querySelector('.bv-synced'), at = ui.data.synced_at;
    synced.textContent = at ? ` · git state checked ${ago(at)}` : '';
    synced.title = at ? new Date(at).toLocaleString() : '';
  });
  hooks.detail.push((box, item) => {
    const live = item.live;
    if (!live || (!live.agent && !item.branch)) return;
    const section = el('section', 'bv-live');
    section.append(el('h4', '', 'Live state'));
    if (live.agent) {
      const row = el('p', 'bv-live-row'), agent = el('button', 'link bv-live-agent');
      agent.type = 'button'; agent.title = 'Open its conversation'; agent.dataset.key = 'live-agent';
      agent.append(dot(live.agent.status), live.agent.id);
      agent.onclick = () => { select(live.agent.id); if (ui.dialog.open) ui.dialog.close(); };
      row.append(el('span', 'bv-live-key', 'Agent'), agent, el('span', 'bv-status', agentWord(live.agent)));
      section.append(row);
    }
    if (item.branch) {
      const row = el('p', 'bv-live-row');
      row.append(el('span', 'bv-live-key', 'Branch'), el('span', 'mono', item.branch));
      if (live.git) {
        const state = gitState(live.git);
        row.append(gitChip(live.git), el('span', 'bv-when', state.title.slice(state.title.lastIndexOf('checked'))));
      } else {
        row.append(el('span', 'bv-when', 'not checked yet'));
      }
      section.append(row);
    }
    const pr = prChip(live.pr);
    if (pr) section.append(pr);
    box.append(section);
  });

  // The result of a move, read out by screen readers: "#12 → Ready, 2 of 5".
  function announce(text) { if (ui.root) ui.root.querySelector('.bv-said').textContent = text ? ` · ${text}` : ''; }
  function placeText(id) {
    const item = find(id);
    if (!item) return `#${id} moved`;
    const row = siblings(item.project, item.state);
    return `#${id} → ${LABEL[item.state]}, ${row.findIndex(other => other.id === id) + 1} of ${row.length}`;
  }

  // -- moving items: drag and drop, Alt+arrows, the "⋯" menu ------------------------------
  // A drop sends `place {state, before}`; Alt+Up/Down the existing `move`; Alt+Left/Right a
  // place at the end of the previous or next state. Reordering is within the item's project:
  // in the all-projects view another project's group is no target.

  // Moves run one after another, each worked out from the item as the board knows it after the
  // move before (two quick Alt+Right presses move two states). `plan(now)`: the request for the
  // item as it is now, or a text to announce when there is nothing to do.
  let moving = Promise.resolve();
  function moved(item, plan) {
    moving = moving.then(() => step(item.id, plan)).catch(error => toast(error.message, true));
    return moving;
  }
  async function step(id, plan) {
    const now = find(id);
    if (!now) return;
    const request = plan(now);
    if (typeof request === 'string') { announce(request); return; }
    ui.focus = id;
    let changed;
    try { changed = await request; }
    catch (error) { ui.focus = null; toast(error.message, true); return; }
    // The answer is the item as it is now: known at once, whatever the reload below brings.
    if (ui.data) ui.data.items = ui.data.items.map(other => (other.id === id ? { ...other, ...changed } : other));
    ui.pending = true;
    await load();
    announce(placeText(id));
  }
  const place = (item, state, before) => moved(item, () => call(`/api/board/${item.id}/place`, { state, before }));
  const shift = (item, direction) => moved(item, now => {
    const row = siblings(now.project, now.state), at = row.findIndex(other => other.id === now.id);
    if (!row[at + (direction === 'up' ? -1 : 1)]) return `#${now.id} is already ${direction === 'up' ? 'first' : 'last'} in ${LABEL[now.state]}`;
    return call(`/api/board/${now.id}/move`, { direction });
  });
  const stepState = (item, step) => moved(item, now => {
    const state = ORDER[ORDER.indexOf(now.state) + step];
    return state ? call(`/api/board/${now.id}/place`, { state, before: null }) : `#${now.id} is already in ${LABEL[now.state]}`;
  });
  // The moves an item has now: [label, action] with disabled ones for the ends.
  function moves(item) {
    const row = siblings(item.project, item.state), at = row.findIndex(other => other.id === item.id);
    const list = [['Move up', () => shift(item, 'up'), at <= 0], ['Move down', () => shift(item, 'down'), at === row.length - 1]];
    for (const [state, label] of STATES) if (state !== item.state) list.push([`Move to ${label}`, () => place(item, state, null)]);
    return list;
  }

  function stepKeys(event, node, item) {
    if (event.target !== node || !event.altKey || event.ctrlKey || event.metaKey) return;
    const step = { ArrowUp: -1, ArrowDown: 1, ArrowLeft: -1, ArrowRight: 1 }[event.key];
    if (!step) return;
    event.preventDefault();
    if (event.key === 'ArrowUp' || event.key === 'ArrowDown') shift(item, step < 0 ? 'up' : 'down');
    else stepState(item, step);
  }

  function menu(anchor, item) {
    closeMenu();
    const box = el('div', 'bv-menu');
    box.setAttribute('role', 'menu'); box.setAttribute('aria-label', `#${item.id}`);
    const entries = [...moves(item), ...hooks.menu.flatMap(extend => extend(item)), ['Open details', () => open(item)]];
    for (const [label, action, disabled] of entries) {
      const entry = el('button', 'bv-menu-item', label);
      entry.type = 'button'; entry.setAttribute('role', 'menuitem'); entry.disabled = Boolean(disabled);
      entry.onclick = () => { closeMenu(false); action(); };
      box.append(entry);
    }
    box.onkeydown = event => {
      const enabled = [...box.querySelectorAll('button:not(:disabled)')], at = enabled.indexOf(document.activeElement);
      if (event.key === 'Escape') { event.preventDefault(); closeMenu(); }
      else if (event.key === 'Tab') closeMenu(false);
      else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        enabled[(at + (event.key === 'ArrowDown' ? 1 : enabled.length - 1)) % enabled.length].focus();
      }
    };
    ui.menu = { box, anchor };
    hold('menu');
    document.body.append(box);
    const spot = anchor.getBoundingClientRect(), size = box.getBoundingClientRect();
    const below = spot.bottom + 4 + size.height <= innerHeight;
    box.style.top = `${Math.max(4, below ? spot.bottom + 4 : spot.top - 4 - size.height)}px`;
    box.style.left = `${Math.max(4, Math.min(spot.right - size.width, innerWidth - size.width - 4))}px`;
    anchor.setAttribute('aria-expanded', 'true');
    box.querySelector('button:not(:disabled)').focus();
  }
  // `refocus`: back to the "⋯" it came from (Escape); not when an entry acts or focus moved on.
  function closeMenu(refocus = true) {
    const shown = ui.menu;
    if (!shown) return;
    ui.menu = null;
    shown.box.remove();
    shown.anchor.setAttribute('aria-expanded', 'false');
    if (refocus && shown.anchor.isConnected) shown.anchor.focus();
    release('menu');
  }
  document.addEventListener('mousedown', event => {
    if (ui.menu && !ui.menu.box.contains(event.target) && event.target !== ui.menu.anchor) closeMenu(false);
  }, true);
  window.addEventListener('resize', () => closeMenu(false));
  window.addEventListener('hashchange', () => closeMenu(false));

  // Where a drag over `column` would drop: {state, before, own: the group it goes into,
  // refused: another project's group}. Before the first card of its group whose middle is
  // below the pointer; at the end of the group without one (or outside every group).
  function dropAt(event, column) {
    const drag = ui.drag, state = column.dataset.state, over = event.target.closest('.bv-group');
    if (over && over.dataset.project !== drag.project) return { state, refused: over };
    const own = over || [...column.querySelectorAll('.bv-group')].find(group => group.dataset.project === drag.project);
    let before = null;
    if (over) {
      for (const node of over.querySelectorAll('.bv-card')) {
        if (Number(node.dataset.id) === drag.id) continue;
        const box = node.getBoundingClientRect();
        if (event.clientY < box.top + box.height / 2) { before = Number(node.dataset.id); break; }
      }
    }
    return { state, before, own: own || null };
  }

  function mark(column, target) {
    unmark();
    if (target.refused) { target.refused.classList.add('bv-refused'); return; }
    column.classList.add('bv-over');
    const line = el('div', 'bv-marker');
    const at = target.before === null ? null : column.querySelector(`.bv-card[data-id="${target.before}"]`);
    if (at) at.before(line);
    else if (target.own) target.own.append(line);
    else column.querySelector('.bv-list').append(line);
  }
  function unmark() {
    if (!ui.root) return;
    for (const node of ui.root.querySelectorAll('.bv-marker')) node.remove();
    for (const node of ui.root.querySelectorAll('.bv-over, .bv-refused')) node.classList.remove('bv-over', 'bv-refused');
  }

  function draggable(node, item) {
    node.draggable = true;
    node.addEventListener('dragstart', event => {
      closeMenu(false);
      ui.drag = { id: item.id, project: item.project };
      hold('drag');
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', `#${item.id} ${item.title}`);
      node.classList.add('bv-dragged');
    });
    node.addEventListener('dragend', () => {
      node.classList.remove('bv-dragged');
      unmark();
      ui.drag = null;
      release('drag');
    });
  }

  function droppable(column) {
    column.addEventListener('dragover', event => {
      if (!ui.drag) return;
      const target = dropAt(event, column);
      mark(column, target);
      if (target.refused) { event.dataTransfer.dropEffect = 'none'; return; }
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
    });
    column.addEventListener('dragleave', event => { if (!column.contains(event.relatedTarget)) unmark(); });
    column.addEventListener('drop', event => {
      if (!ui.drag) return;
      event.preventDefault();
      const target = dropAt(event, column), item = find(ui.drag.id);
      unmark();
      if (!target.refused && item) place(item, target.state, target.before);
    });
  }

  hooks.head.push(root => { for (const column of root.querySelectorAll('.bv-col')) droppable(column); });
  hooks.card.push((node, item) => {
    draggable(node, item);
    const more = button('ghost icon bv-more-button', '', '⋯', event => { event.stopPropagation(); menu(more, item); }, `Move #${item.id}…`);
    more.dataset.part = 'menu';
    more.setAttribute('aria-haspopup', 'menu'); more.setAttribute('aria-expanded', 'false');
    node.querySelector('.bv-card-head').append(more);
    node.addEventListener('keydown', event => stepKeys(event, node, item));
  });

  // -- the item dialog: a new item, or one item's detail ---------------------------------

  const open = item => select(route(ui.project, item.id));

  function dialog() {
    if (ui.dialog) return ui.dialog;
    const node = el('dialog', 'bv-dialog'), form = el('form');
    form.method = 'dialog';
    const head = el('div', 'dialog-head'), close = button('ghost icon', 'x', '', null, 'Close');
    close.value = 'cancel'; close.type = 'submit'; close.formNoValidate = true;
    head.append(el('h3', 'bv-dialog-title'), close);
    const body = el('div', 'dialog-body');
    const field = (label, control, cls) => {
      const box = el('label', `field${cls ? ` ${cls}` : ''}`); box.append(el('span', 'label', label), control); return box;
    };
    const input = (name, cls, placeholder) => {
      const node = el('input', `input${cls ? ` ${cls}` : ''}`); node.name = name; node.autocomplete = 'off';
      if (placeholder) node.placeholder = placeholder;
      return node;
    };
    const project = el('select', 'input'), title = input('title'), description = el('textarea', 'input');
    project.name = 'project';
    title.required = true; title.maxLength = 200;
    description.name = 'description'; description.rows = 5; description.maxLength = 4000;
    const state = el('select', 'input');
    state.name = 'state';
    state.replaceChildren(...STATES.map(([value, label]) => new Option(label, value)));
    const pair = el('div', 'grid-2');
    pair.append(field('State', state), field('Agent', input('agent', 'mono', 'optional agent id')));
    body.append(field('Project', project, 'bv-project'), field('Title', title), field('Description', description), pair,
      field('Branch', input('branch', 'mono', 'optional')), el('p', 'bv-stamp'), el('div', 'bv-extra'));
    const foot = el('div', 'dialog-foot'), cancel = el('button', 'btn', 'Cancel'), save = el('button', 'btn primary', 'Save');
    cancel.value = 'cancel'; cancel.formNoValidate = true; save.value = 'ok';
    foot.append(el('span', 'error'), cancel, save);
    form.append(head, body, foot);
    node.append(form);
    form.onsubmit = async event => {
      if (!event.submitter || event.submitter.value !== 'ok') return;  // cancel: let it close
      event.preventDefault();
      save.disabled = true;
      // The item saved gets the focus once the dialog is closed (a modal keeps it until then).
      try { const id = await ui.submit(new FormData(form)); node.close(); focusCard(id); }
      catch (error) { foot.querySelector('.error').textContent = error.message; }
      finally { save.disabled = false; }
    };
    // Closing the detail goes back to the board (the route without the id), and the focus to
    // the item's card as it is now (the one it came from may have been redrawn meanwhile).
    node.addEventListener('close', () => {
      const shown = ui.detail;
      ui.detail = null; ui.submit = null;
      if (shown && current() === route(shown.project, shown.id)) select(route(shown.project));
      if (shown && owns(current())) focusCard(shown.id);
    });
    document.body.append(node);
    ui.dialog = node;
    return node;
  }

  function fill(node, title, item) {
    const form = node.querySelector('form');
    node.querySelector('.bv-dialog-title').textContent = title;
    node.querySelector('.error').textContent = '';
    form.elements.title.value = item.title || '';
    form.elements.description.value = item.description || '';
    form.elements.state.value = item.state || 'idea';
    form.elements.agent.value = item.agent_id || '';
    form.elements.branch.value = item.branch || '';
    return form;
  }
  const fields = data => ({ title: data.get('title'), description: data.get('description'), state: data.get('state'),
    agent: data.get('agent').trim(), branch: data.get('branch').trim() });

  function addItem() {
    if (!ui.data || !ui.data.projects.length) return;
    const node = dialog(), form = fill(node, 'New item', {});
    ui.detail = null;
    const project = form.elements.project;
    project.replaceChildren(...ui.data.projects.map(name => new Option(name, name)));
    project.value = ui.project || ui.data.projects[0];
    node.querySelector('.bv-project').hidden = false;
    node.querySelector('.bv-stamp').textContent = '';
    node.querySelector('.bv-extra').replaceChildren();
    ui.submit = async data => {
      const item = await call('/api/board/add', { project: data.get('project'), ...fields(data) });
      toast(`#${item.id} added to ${item.project}`);
      await load();
      return item.id;
    };
    node.showModal();
    form.elements.title.focus();
  }

  // What an open detail shows of an item besides its form: refilled when this changes (an
  // edit, the agent's status, the branch's state, a decision answered or withdrawn). Not when
  // only the time of the git check moves on.
  function shownKey(item) {
    const live = item.live && item.live.git ? { ...item.live, git: { ...item.live.git, checked_at: null } } : item.live;
    return JSON.stringify([item.updated_at, live, item.blockers]);
  }

  // The detail named by the route: opened once, kept while it stays named, closed when not.
  async function syncDetail(id) {
    const node = ui.dialog;
    if (!id) {
      if (node && node.open && ui.detail) { ui.detail = null; node.close(); }
      return;
    }
    if (ui.detail && ui.detail.id === id && node.open) {
      const now = find(id);
      if (now && shownKey(now) !== ui.detail.key) { ui.detail.key = shownKey(now); extra(now); }
      return;
    }
    // Only an item the route's board shows: #board:alpha/9 with an item of beta is refused.
    const item = find(id);
    if (!item) {
      toast(ui.project ? `#${id} is not an item of ${ui.project}` : `There is no board item #${id}`, true);
      select(route(ui.project));
      return;
    }
    if (current() !== route(ui.project, id)) return;  // gone elsewhere meanwhile
    showDetail(item);
  }

  function showDetail(item) {
    const node = dialog(), form = fill(node, `#${item.id} · ${item.project}`, item);
    ui.detail = { id: item.id, project: ui.project, key: shownKey(item) };
    node.querySelector('.bv-project').hidden = true;
    const stamp = node.querySelector('.bv-stamp');
    stamp.textContent = `Added ${ago(item.created_at)} · changed ${ago(item.updated_at)}`;
    stamp.title = `Added ${new Date(item.created_at).toLocaleString()}\nChanged ${new Date(item.updated_at).toLocaleString()}`;
    ui.submit = async data => {
      // Only what was changed here: a state or link set meanwhile by an agent stays.
      const was = { title: item.title, description: item.description, state: item.state,
        agent: item.agent_id || '', branch: item.branch || '' };
      const changed = Object.fromEntries(Object.entries(fields(data)).filter(([key, value]) => value !== was[key]));
      if (!Object.keys(changed).length) return item.id;
      await call(`/api/board/${item.id}`, changed);
      toast(`#${item.id} saved`);
      await load();
      return item.id;
    };
    extra(item, true);
    if (!node.open) node.showModal();
    form.elements.title.focus();
  }

  function focusCard(id) {
    const node = id && ui.root && ui.root.querySelector(`.bv-card[data-id="${id}"]`);
    if (node) node.focus();
  }

  // Leaving the board (Back, a link) closes the detail it had open.
  window.addEventListener('hashchange', () => {
    if (ui.detail && ui.dialog.open && !owns(current())) { ui.detail = null; ui.dialog.close(); }
  });

  // -- an item's history and runs, in its detail -------------------------------------------
  // Who changed it (the owner, an agent and its run, an automatic rule), what changed, and
  // the runs the history names; a change made by an older orch process shows as not recorded.

  const ACTIONS = { add: 'added', edit: 'edited', state: 'moved', place: 'moved', move: 'reordered', dispatch: 'dispatched',
    restore: 'put back: the dispatch could not be sent', unrecorded: 'changed by an older orch process',
    link: 'linked', unlink: 'unlinked' };
  // Fields shown by name; any other is shown as it comes: [old, new] as a change, a text as it is
  // (the merge's `commit`, an item's `source`).
  const FIELDS = { title: 'title', agent_id: 'agent', branch: 'branch', decision: 'decision' };
  const clip = text => (text.length > 60 ? `${text.slice(0, 59)}…` : text);

  function actor(entry) {
    if (entry.actor_kind === 'owner') return 'owner';
    if (entry.actor_kind === 'auto') return `auto: ${entry.actor || 'rule'}`;
    if (entry.actor_kind === 'agent') {
      if (!entry.actor) return 'orch chat session';
      return `agent ${entry.actor}${entry.run_id && entry.action !== 'dispatch' ? ` (run ${entry.run_id})` : ''}`;
    }
    return 'not recorded';
  }

  function change(entry) {
    const parts = [], changes = entry.changes || {};
    const value = (field, v) => (v === null || v === undefined || v === '' ? '—'
      : field === 'state' ? LABEL[v] || v : field === 'title' ? `“${clip(String(v))}”`
        : field === 'decision' ? `#${v}` : String(v));
    for (const [field, pair] of Object.entries(changes)) {
      if (field === 'description') parts.push('description changed');
      else if (field === 'state') parts.push(entry.action === 'add' ? `in ${value('state', pair[1])}` : `${value('state', pair[0])} → ${value('state', pair[1])}`);
      else if (field === 'position' && !changes.state) parts.push(pair[1] < pair[0] ? 'up' : 'down');
      else if (Array.isArray(pair) && field !== 'position') parts.push(`${FIELDS[field] || field} ${value(field, pair[0])} → ${value(field, pair[1])}`);
      else if (!Array.isArray(pair) && pair !== null && typeof pair !== 'object') parts.push(`${field} ${clip(String(pair))}`);
    }
    if (entry.action === 'dispatch' && entry.run_id) parts.push(`run ${entry.run_id}`);
    return [ACTIONS[entry.action] || entry.action, parts.join(', ')].filter(Boolean).join(' · ');
  }

  hooks.detail.push(async (box, item) => {
    const history = el('section', 'bv-history'), runs = el('section', 'bv-runs');
    history.append(el('h4', '', 'History'), el('p', 'bv-note', 'Loading…'));
    box.append(history, runs);
    let detail;
    try { detail = await call(`/api/board/${item.id}/detail`); }
    catch (error) { history.querySelector('.bv-note').textContent = `Not available: ${error.message}`; return; }
    const list = el('ol', 'bv-entries');
    for (const entry of [...detail.history].reverse()) {
      const row = el('li', entry.actor_kind ? '' : 'bv-gap'), when = el('time', 'bv-when', ago(entry.ts));
      when.dateTime = entry.ts; when.title = new Date(entry.ts).toLocaleString();
      row.append(when, el('span', 'bv-actor', actor(entry)), el('span', 'bv-what', change(entry)));
      list.append(row);
    }
    const notes = [];
    if (detail.unrecorded) notes.push(el('p', 'bv-note bv-gap', 'Its latest change was not recorded: an older orch process made it.'));
    if (!detail.history.length) notes.push(el('p', 'bv-note', 'Nothing recorded yet.'));
    history.replaceChildren(el('h4', '', 'History'), ...notes, list);
    if (!detail.runs.length) return;
    const buttons = el('div', 'bv-run-list');
    for (const run of detail.runs) {
      const entry = el('button', 'bv-run');
      entry.type = 'button'; entry.dataset.key = `run:${run.id}`;
      entry.title = `Open the conversation of ${run.agent_id}`;
      entry.append(el('span', 'mono', `#${run.id}`), el('span', 'bv-run-agent', run.agent_id),
        el('span', `pill ${run.status}`, run.status), el('span', 'bv-when', ago(run.ended_at || run.created_at)));
      entry.onclick = () => { select(run.agent_id); if (ui.dialog.open) ui.dialog.close(); };
      buttons.append(entry);
    }
    runs.append(el('h4', '', 'Runs'), buttons);
  });

  // The part under the form. Opening a detail (`fresh`) fills it in place. A refill of the open
  // detail is built aside and swapped in once complete (its history and Link list are loaded),
  // keeping the focus on the same control (else a neighbour in the dialog), the chosen Link
  // option while it is still offered, and the scroll position. The form is not touched.
  let fills = 0;
  const FOCUSABLE = 'button:not(:disabled), a[href], select:not(:disabled), input:not(:disabled), [tabindex]';

  async function extra(item, fresh) {
    const box = ui.dialog.querySelector('.bv-extra'), turn = ++fills;
    if (fresh || !box.childElementCount) {
      box.replaceChildren();
      for (const fillIn of hooks.detail) fillIn(box, item);
      return;
    }
    const next = el('div', 'bv-extra');
    await Promise.allSettled(hooks.detail.map(fillIn => fillIn(next, item)));
    if (turn !== fills || !ui.dialog.open) return;  // a later refill, or closed meanwhile
    const kept = heldState(box);
    box.replaceChildren(...next.childNodes);
    putBack(box, kept);
  }

  const focusables = root => [...root.querySelectorAll(FOCUSABLE)].filter(node => !node.closest('[hidden]'));

  function heldState(box) {
    const scrollers = [ui.dialog, ...ui.dialog.querySelectorAll('.dialog-body')];
    const pick = box.querySelector('.bv-link-select'), active = document.activeElement;
    const kept = { scroll: scrollers.map(node => [node, node.scrollTop]), choice: pick && !pick.disabled ? pick.value : '' };
    if (!active || !box.contains(active)) return kept;
    const section = active.closest('section'), own = section ? focusables(section) : [];
    kept.focus = { key: active.dataset.key || null, part: section ? section.className : null,
      inPart: own.indexOf(active), inBox: focusables(box).indexOf(active) };
    return kept;
  }

  function putBack(box, kept) {
    const pick = box.querySelector('.bv-link-select');
    if (pick && kept.choice && [...pick.options].some(option => option.value === kept.choice)) pick.value = kept.choice;
    const want = kept.focus;
    if (want) {
      const near = (list, at) => (list.length ? list[Math.min(Math.max(at, 0), list.length - 1)] : null);
      const section = want.part && [...box.querySelectorAll('section')].find(node => node.className === want.part);
      const same = want.key && focusables(box).find(node => node.dataset.key === want.key);
      const target = same || (section && near(focusables(section), want.inPart)) || near(focusables(box), want.inBox)
        || ui.dialog.querySelector('form').elements.title;
      target.focus({ preventScroll: true });
    }
    for (const [node, top] of kept.scroll) node.scrollTop = top;
  }

  // -- selecting items and dispatching them ------------------------------------------------
  // Idea and ready cards have a checkbox (Space on the card toggles it). Dispatch sends exactly
  // the selection, one message per project to its lead, after a confirmation that says which
  // lead gets which items; an item no longer idea or ready is skipped and said so.

  const picked = new Set();
  const pickable = item => item.state === 'idea' || item.state === 'ready';

  function pick(item, on = !picked.has(item.id)) {
    if (on) picked.add(item.id); else picked.delete(item.id);
    const node = ui.root.querySelector(`.bv-card[data-id="${item.id}"]`);
    if (node) { node.classList.toggle('bv-picked', on); node.querySelector('[data-part=pick]').checked = on; }
    selectionBar();
    announce(`${on ? 'Selected' : 'Unselected'} #${item.id} · ${picked.size} selected`);
  }

  function selectionBar() {
    const box = ui.root && ui.root.querySelector('.bv-selbar');
    if (!box) return;
    box.hidden = !picked.size;
    box.querySelector('.bv-selcount').textContent = `${picked.size} selected`;
    box.querySelector('.bv-selids').textContent = [...picked].sort((a, b) => a - b).map(id => `#${id}`).join(' ');
  }

  const short = text => (text.length > 48 ? `${text.slice(0, 47)}…` : text);
  async function dispatch(ids) {
    const chosen = ids.map(find).filter(Boolean);
    if (!chosen.length) return;
    // The lead each project's items go to: the one it has, or one started for them.
    const known = await call('/api/state').then(data => data.projects, () => []);
    const leadOf = name => {
      const project = known.find(p => p.name === name);
      const lead = project && project.agents.find(a => a.role === 'lead' && (!a.parent_id || a.parent_id === 'orchestrator'));
      return lead ? lead.id : `a new lead of ${name}`;
    };
    const projects = [...new Set(chosen.map(item => item.project))].sort();
    const lines = projects.map(name => `${name} → ${leadOf(name)}: `
      + chosen.filter(item => item.project === name).map(item => `#${item.id} ${short(item.title)}`).join(', '));
    confirmDialog({
      title: `Dispatch ${plural(chosen.length, 'item')}?`,
      text: `${lines.join(' · ')}. Each lead gets its items as one message from you; they become in progress, linked to it.`,
      ok: 'Dispatch',
      action: async () => {
        const result = await call('/api/board/dispatch-items', { ids: chosen.map(item => item.id) });
        for (const item of chosen) picked.delete(item.id);
        const sent = result.results.map(group => `${group.items.map(item => `#${item.id}`).join(', ')} to ${group.agent_id}`);
        const skipped = result.skipped.map(entry => `#${entry.id} not sent: ${entry.reason}`);
        const said = [sent.length ? `Sent ${sent.join('; ')}` : 'Nothing sent', ...skipped].join(' · ');
        toast(said, !sent.length, skipped.length ? 9000 : 0);
        announce(said);
        ui.pending = true;
        await load();
        selectionBar();
      },
    });
  }

  hooks.head.push(root => {
    const box = el('div', 'bv-selbar');
    box.hidden = true;
    box.setAttribute('role', 'region'); box.setAttribute('aria-label', 'Selected items');
    box.append(el('span', 'bv-selcount'), el('span', 'bv-selids mono'),
      button('primary', 'send', 'Dispatch', () => dispatch([...picked])),
      button('ghost', 'x', 'Clear', () => { picked.clear(); paint(); selectionBar(); announce('Selection cleared'); }));
    root.querySelector('.bv-head').after(box);
  });
  hooks.card.push((node, item) => {
    node.addEventListener('keydown', event => {
      if (event.target !== node || event.key !== ' ' || event.altKey || event.ctrlKey || event.metaKey) return;
      event.preventDefault();
      if (pickable(item)) pick(item);
      else announce(`Only idea and ready items can be dispatched; #${item.id} is ${LABEL[item.state]}`);
    });
    if (!pickable(item)) return;
    const box = el('input', 'bv-pick');
    box.type = 'checkbox'; box.dataset.part = 'pick'; box.checked = picked.has(item.id);
    box.setAttribute('aria-label', `Select #${item.id} for dispatch`);
    box.onchange = () => pick(item, box.checked);
    node.classList.toggle('bv-picked', box.checked);
    node.querySelector('.bv-card-head').prepend(box);
  });
  hooks.menu.push(item => (pickable(item) ? [
    [picked.has(item.id) ? 'Unselect' : 'Select for dispatch', () => pick(item)],
    [`Dispatch #${item.id}…`, () => dispatch([item.id])],
  ] : []));
  // An item gone, or no longer idea or ready, leaves the selection.
  hooks.paint.push(() => {
    for (const id of [...picked]) { const item = find(id); if (!item || !pickable(item)) picked.delete(id); }
    selectionBar();
  });

  // -- the entry in the left column --------------------------------------------------------

  function navEntry(selected) {
    const row = el('button', `nav-item bv-entry${owns(selected) ? ' selected' : ''}`);
    row.type = 'button';
    const face = el('span', 'avatar bv-avatar'), label = el('span', 'label');
    face.append(icon('board'));
    label.append(el('span', 'name', 'Board'), el('span', 'sub', 'Tasks of every project'));
    row.append(face, label);
    row.title = 'The task board of every project';
    row.onclick = () => select(ROUTE);
    return row;
  }

  // -- items from a conversation's message; decisions that block items ---------------------
  // A message gets "Add to board" (app.js calls messageAction for each one it shows); the item
  // keeps its `source`. An item's `blockers` are the open decisions that block it: one linked to
  // it, or one of its agent linked to no item. The detail links and unlinks the project's open
  // decisions.

  const ORCHESTRATOR = 'orchestrator';  // its conversation needs a project for the item
  const cut = (text, limit) => (text.length > limit ? `${text.slice(0, limit - 1)}…` : text);

  function messageAction(message, event, agentId) {
    if (!event || !event.id || !event.text || !agentId || owns(agentId) || agentId.startsWith('lead:')) return;
    const when = message.querySelector('.when');
    if (!when) return;
    const add = el('button', 'bv-msg-add');
    add.type = 'button'; add.title = 'Add this message to the task board as an item';
    add.append(icon('board'), 'Add to board');
    add.onclick = () => fromMessage(add, event, agentId);
    when.append(add);
  }

  let fromDialog = null;
  function fromForm() {
    if (fromDialog) return fromDialog;
    const node = el('dialog', 'bv-dialog bv-from'), form = el('form');
    form.method = 'dialog';
    const field = (label, control, cls) => {
      const box = el('label', `field${cls ? ` ${cls}` : ''}`); box.append(el('span', 'label', label), control); return box;
    };
    const title = el('input', 'input'), description = el('textarea', 'input'), state = el('select', 'input'), project = el('select', 'input');
    title.name = 'title'; title.required = true; title.maxLength = 200; title.autocomplete = 'off';
    description.name = 'description'; description.rows = 6; description.maxLength = 4000;
    state.name = 'state'; state.replaceChildren(...STATES.map(([value, label]) => new Option(label, value)));
    project.name = 'project';
    const pair = el('div', 'grid-2');
    pair.append(field('Project', project, 'bv-from-project'), field('State', state));
    const head = el('div', 'dialog-head'), body = el('div', 'dialog-body'), foot = el('div', 'dialog-foot');
    head.append(el('h3', 'bv-dialog-title', 'Add to the board'));
    body.append(el('p', 'bv-stamp bv-from-note'), field('Title', title), field('Description', description), pair);
    const cancel = el('button', 'btn', 'Cancel'), save = el('button', 'btn primary', 'Add');
    cancel.value = 'cancel'; cancel.formNoValidate = true; save.value = 'ok';
    foot.append(el('span', 'error'), cancel, save);
    form.append(head, body, foot);
    node.append(form);
    form.onsubmit = async event => {
      if (!event.submitter || event.submitter.value !== 'ok') return;
      event.preventDefault();
      save.disabled = true;
      try { const link = await node.submit(new FormData(form)); node.close(); link.focus(); }
      catch (error) { foot.querySelector('.error').textContent = error.message; }
      finally { save.disabled = false; }
    };
    document.body.append(node);
    fromDialog = node;
    return node;
  }

  // The message's button becomes a link to its item; the link, to focus.
  function onBoard(button, item) {
    const link = el('a', 'bv-msg-add bv-msg-added', `On the board: #${item.id}`);
    link.href = `#${encodeURIComponent(route(item.project, item.id))}`;
    link.title = 'Open the item on the board';
    button.replaceWith(link);
    return link;
  }

  // A message already made into an item (its conversation was redrawn since) gets the link to
  // that item instead of a second one.
  async function fromMessage(button, event, agentId) {
    const board = await call('/api/board/view').catch(() => ({ projects: [], items: [] }));
    const made = board.items.find(item => item.source && item.source.agent_id === agentId
      && String(item.source.event_id) === String(event.id));
    if (made) { toast(`Already on the board: #${made.id} in ${made.project}`); onBoard(button, made).focus(); return; }
    const node = fromForm(), form = node.querySelector('form'), needsProject = agentId === ORCHESTRATOR;
    const firstLine = event.text.split('\n').map(line => line.replace(/^[#>*\-\s]+/, '').trim()).find(Boolean) || '';
    form.elements.title.value = cut(firstLine, 200);
    form.elements.description.value = cut(event.text, 4000);
    form.elements.state.value = 'idea';
    node.querySelector('.error').textContent = '';
    node.querySelector('.bv-from-note').textContent = `From a message of ${agentId}; the item keeps a link to it.`;
    node.querySelector('.bv-from-project').hidden = !needsProject;
    if (needsProject) form.elements.project.replaceChildren(...board.projects.map(name => new Option(name, name)));
    node.submit = async data => {
      const body = { agent_id: agentId, event_id: event.id, title: data.get('title'),
        description: data.get('description'), state: data.get('state') };
      if (needsProject) body.project = data.get('project');
      const item = await call('/api/board/from-message', body);
      toast(`#${item.id} added to the board of ${item.project}`);
      return onBoard(button, item);
    };
    node.showModal();
    form.elements.title.focus();
  }

  // A decision opened from the board: the detail closes (back to its board), the decisions
  // dialog opens on it (app.js follows #decision:<id>).
  function openDecision(id) {
    const shown = ui.detail;
    if (shown && ui.dialog.open) { ui.detail = null; ui.dialog.close(); select(route(shown.project)); }
    location.hash = `#decision:${id}`;
  }
  const blockText = blocker => `#${blocker.id} ${blocker.question}`;

  hooks.card.push((node, item) => {
    const blockers = item.blockers || [];
    let meta = node.querySelector('.bv-meta');
    if (!meta && (item.source || blockers.length)) { meta = el('div', 'bv-meta'); node.append(meta); }
    if (blockers.length) {
      const badge = el('button', 'chip bv-blocked-badge', `⚑ ${plural(blockers.length, 'decision')}`);
      badge.type = 'button'; badge.dataset.part = 'blockers';
      badge.title = `Blocked by ${blockers.map(blockText).join('; ')}${blockers.length === 1 ? ' · open it' : ' · open the item'}`;
      badge.onclick = event => { event.stopPropagation(); if (blockers.length === 1) openDecision(blockers[0].id); else open(item); };
      meta.prepend(badge);
      node.classList.add('bv-blocked');
    }
    if (item.source) {
      const from = el('button', 'chip bv-source', `from ${item.source.agent_id}`);
      from.type = 'button'; from.dataset.part = 'source';
      from.title = `Made from a message of ${item.source.agent_id}; open its conversation`;
      from.onclick = event => { event.stopPropagation(); select(item.source.agent_id); };
      meta.append(from);
    }
  });

  hooks.detail.push((box, item) => {
    if (item.source) {
      const section = el('section', 'bv-source-box'), row = el('p', 'bv-live-row'), agent = el('button', 'link bv-live-agent', item.source.agent_id);
      agent.type = 'button'; agent.title = 'Open its conversation'; agent.dataset.key = 'source-agent';
      agent.onclick = () => { select(item.source.agent_id); if (ui.dialog.open) ui.dialog.close(); };
      row.append(el('span', '', 'Made from a message of'), agent,
        el('span', 'bv-when', `message ${item.source.event_id}${item.source.run_id ? ` · run ${item.source.run_id}` : ''}`));
      section.append(el('h4', '', 'Source'), row);
      box.append(section);
    }
    const section = blockerSection(item);
    box.append(section);
    return section.ready;  // its Link list, loaded
  });

  function blockerSection(item) {
    const section = el('section', 'bv-blockers'), blockers = item.blockers || [], heading = el('h4', '', 'Blocked by');
    heading.tabIndex = -1; heading.dataset.key = 'blockers';  // the focus goes here after a link or unlink
    section.append(heading);
    if (!blockers.length) section.append(el('p', 'bv-note', 'No open decision blocks it.'));
    const list = el('ul', 'bv-blocker-list');
    for (const blocker of blockers) {
      const row = el('li'), link = el('a', 'bv-decision', blockText(blocker));
      link.href = `#decision:${blocker.id}`; link.dataset.key = `decision:${blocker.id}`;
      link.title = 'Open the decision';
      link.onclick = event => { event.preventDefault(); openDecision(blocker.id); };
      row.append(link, el('span', 'bv-when', blocker.linked ? `linked · asked by ${blocker.agent_id}`
        : `not linked: blocks every open item of ${blocker.agent_id}`));
      if (blocker.linked) {
        const unlink = button('ghost bv-unlink', '', 'Unlink', () => linkDecision(item, blocker.id, false), `Unlink decision #${blocker.id}`);
        unlink.dataset.key = `unlink:${blocker.id}`;
        row.append(unlink);
      }
      list.append(row);
    }
    if (blockers.length) section.append(list);
    // Link an open decision not linked yet (from the page's state).
    const pick = el('select', 'input bv-link-select'), add = button('', '', 'Link', () => pick.value && linkDecision(item, Number(pick.value), true));
    pick.setAttribute('aria-label', 'An open decision of the project to link');
    pick.dataset.key = 'link-select'; add.dataset.key = 'link-add';
    const row = el('div', 'bv-link-row');
    row.append(pick, add);
    section.append(row);
    section.ready = call('/api/state').then(state => {
      const linked = new Set(blockers.filter(b => b.linked).map(b => b.id));
      // The project's and the orchestrator's (no project), as the server accepts both.
      const open = (state.decisions || []).filter(d => (d.project == null || d.project === item.project) && !linked.has(d.id));
      pick.replaceChildren(...open.map(d => new Option(`#${d.id} ${cut(d.question, 70)} (${d.agent_name})`, d.id)));
      if (!open.length) { pick.append(new Option(`No other open decision in ${item.project}`, '')); pick.disabled = true; add.disabled = true; }
    }, () => { row.hidden = true; });
    return section;
  }

  async function linkDecision(item, decisionId, linked) {
    try { await call(`/api/board/${item.id}/blockers`, { decision_id: decisionId, linked }); }
    catch (error) { toast(error.message, true); return; }
    announce(`Decision #${decisionId} ${linked ? 'linked to' : 'unlinked from'} #${item.id}`);
    ui.pending = true;
    await load();
    if (ui.detail && ui.detail.id === item.id) {
      await extra(find(item.id) || item);
      if (ui.detail && ui.detail.id === item.id) ui.dialog.querySelector('.bv-blockers h4').focus();
    }
  }

  window.BoardView = { ROUTE, owns, render, navEntry, messageAction };
})();
