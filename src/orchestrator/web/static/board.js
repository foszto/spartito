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

  async function moved(item, request) {
    ui.focus = item.id;
    try { await request(); }
    catch (error) { ui.focus = null; toast(error.message, true); return; }
    ui.pending = true;
    await load();
    announce(placeText(item.id));
  }
  const place = (item, state, before) => moved(item, () => call(`/api/board/${item.id}/place`, { state, before }));
  const shift = (item, direction) => moved(item, () => call(`/api/board/${item.id}/move`, { direction }));
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
    if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
      const row = siblings(item.project, item.state), at = row.findIndex(other => other.id === item.id);
      if (row[at + step]) shift(item, step < 0 ? 'up' : 'down');
      else announce(`#${item.id} is already ${step < 0 ? 'first' : 'last'} in ${LABEL[item.state]}`);
      return;
    }
    const state = ORDER[ORDER.indexOf(item.state) + step];
    if (state) place(item, state, null);
    else announce(`#${item.id} is already in ${LABEL[item.state]}`);
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
    // Closing the detail goes back to the board (the route without the id).
    node.addEventListener('close', () => {
      const shown = ui.detail;
      ui.detail = null; ui.submit = null;
      if (shown && current() === route(shown.project, shown.id)) select(route(shown.project));
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

  // The detail named by the route: opened once, kept while it stays named, closed when not.
  async function syncDetail(id) {
    const node = ui.dialog;
    if (!id) {
      if (node && node.open && ui.detail) { ui.detail = null; node.close(); }
      return;
    }
    if (ui.detail && ui.detail.id === id && node.open) {
      const now = find(id);
      if (now && now.updated_at !== ui.detail.updated_at) { ui.detail.updated_at = now.updated_at; extra(now); }
      return;
    }
    let item = find(id);
    if (!item && ui.project) item = (await call('/api/board/view').catch(() => ({ items: [] }))).items.find(i => i.id === id);
    if (!item) {
      toast(`There is no board item #${id}${ui.project ? ` in ${ui.project}` : ''}`, true);
      select(route(ui.project));
      return;
    }
    if (current() !== route(ui.project, id)) return;  // gone elsewhere meanwhile
    showDetail(item);
  }

  function showDetail(item) {
    const node = dialog(), form = fill(node, `#${item.id} · ${item.project}`, item);
    ui.detail = { id: item.id, project: ui.project, updated_at: item.updated_at };
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
    extra(item);
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

  function extra(item) {
    const box = ui.dialog.querySelector('.bv-extra');
    box.replaceChildren();
    for (const fillIn of hooks.detail) fillIn(box, item);
  }

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

  // From a conversation's message: added by a later part; until then it adds nothing.
  function messageAction() {}

  window.BoardView = { ROUTE, owns, render, navEntry, messageAction };
})();
