/* Piano Tasks - front-end. Vanilla JS, no build step, no dependencies.
   Talks to the server's JSON API with a session cookie (and a Bearer token
   kept in localStorage, which is how the future phone/desktop apps work). */
'use strict';

var TOKEN_KEY = 'piano_tasks_token';
var HISTORY_DAYS = 14;

var state = {
  user: null,
  viewDay: null,      // 'YYYY-MM-DD' being viewed
  serverToday: null,
  tasks: [],
  summary: null,
  showDone: false,
  recurrence: 'daily',
  editors: {}         // task id -> true when its inline editor is open
};

// ---------------------------------------------------------------- helpers

function $(id) { return document.getElementById(id); }

function el(tag, props, children) {
  var node = document.createElement(tag);
  if (props) {
    Object.keys(props).forEach(function (k) {
      var v = props[k];
      if (v === null || v === undefined || v === false) return;
      if (k === 'text') node.textContent = v;
      else if (k === 'dataset') Object.keys(v).forEach(function (d) { node.dataset[d] = v[d]; });
      else if (k.slice(0, 2) === 'on') node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? '' : v);
    });
  }
  (children || []).forEach(function (c) {
    if (c === null || c === undefined) return;
    node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  });
  return node;
}

function parseDay(s) { var p = s.split('-'); return new Date(+p[0], +p[1] - 1, +p[2]); }
function dayStr(d) {
  var m = d.getMonth() + 1, day = d.getDate();
  return d.getFullYear() + '-' + (m < 10 ? '0' : '') + m + '-' + (day < 10 ? '0' : '') + day;
}
function shiftDay(s, n) { var d = parseDay(s); d.setDate(d.getDate() + n); return dayStr(d); }
function prettyDay(s) {
  return parseDay(s).toLocaleDateString(undefined,
    { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' });
}
function headerDay(s) {
  // the top bar gets crowded on a phone; drop the year rather than wrap
  var narrow = window.innerWidth < 560;
  return parseDay(s).toLocaleDateString(undefined, narrow
    ? { weekday: 'short', month: 'short', day: 'numeric' }
    : { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' });
}
function shortDay(s) {
  return parseDay(s).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
}
function relDay(s, todayStr) {
  var diff = Math.round((parseDay(s) - parseDay(todayStr)) / 86400000);
  if (diff === 0) return 'today';
  if (diff === 1) return 'tomorrow';
  if (diff === -1) return 'yesterday';
  if (diff < 0) return Math.abs(diff) + ' days ago';
  return 'in ' + diff + ' days';
}
function dueText(s, todayStr) {
  var diff = Math.round((parseDay(s) - parseDay(todayStr)) / 86400000);
  if (diff < 0) return (diff === -1 ? '1 day overdue' : Math.abs(diff) + ' days overdue');
  if (diff === 0) return 'due today';
  if (diff === 1) return 'due tomorrow';
  return 'due ' + shortDay(s);
}

function toast(message, ok) {
  var box = $('toast');
  box.textContent = message;
  box.className = 'toast' + (ok ? ' ok' : '');
  box.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(function () { box.hidden = true; }, 4200);
}

function getToken() { try { return localStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; } }
function setToken(t) {
  try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch (e) {}
}
/* The web UI authenticates with the HttpOnly session cookie only - it never
   keeps a token in localStorage. The Bearer path exists for the native apps,
   which is why api() still attaches one when present. */

// ------------------------------------------------------------------- API

function api(method, path, body) {
  var headers = { 'X-Client': 'web' };
  var token = getToken();
  if (token) headers['Authorization'] = 'Bearer ' + token;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  return fetch(path, {
    method: method,
    headers: headers,
    credentials: 'same-origin',
    body: body === undefined ? undefined : JSON.stringify(body)
  }).then(function (res) {
    return res.json().catch(function () { return null; }).then(function (data) {
      if (!res.ok) {
        var err = new Error((data && data.error) || ('HTTP ' + res.status));
        err.status = res.status;
        throw err;
      }
      return data;
    });
  });
}

// ------------------------------------------------------------------ views

function showAuth(message) {
  $('app').hidden = true;
  $('auth').hidden = false;
  setToken('');
  state.user = null;
  if (message) {
    var box = $('auth-error');
    box.textContent = message;
    box.hidden = false;
  }
}

function showApp() {
  $('auth').hidden = true;
  $('app').hidden = false;
  $('auth-error').hidden = true;
  $('user-btn').textContent = state.user.display_name || state.user.username;
  $('foot-info').textContent = 'piano-tasks · signed in as ' + state.user.username;
}

function switchAuthTab(which) {
  Array.prototype.forEach.call(document.querySelectorAll('[data-auth-tab]'), function (b) {
    b.classList.toggle('is-active', b.dataset.authTab === which);
  });
  $('login-form').hidden = which !== 'login';
  $('register-form').hidden = which !== 'register';
  $('auth-error').hidden = true;
}

function renderHeader() {
  var today = state.serverToday;
  $('day-label').textContent = headerDay(state.viewDay);
  var bits = [];
  if (state.viewDay === today) bits.push('today');
  else bits.push(relDay(state.viewDay, today));
  $('day-sub').textContent = bits.join(' · ');
  $('today-btn').hidden = state.viewDay === today;
}

function renderStats() {
  var s = state.summary || {};
  var wrap = $('stats');
  wrap.textContent = '';

  function pill(label, value, cls) {
    wrap.appendChild(el('span', { class: 'pill ' + (cls || '') }, [
      el('b', { text: String(value) }), label
    ]));
  }
  pill('daily done', (s.daily_done || 0) + '/' + (s.daily_total || 0),
    s.daily_total && s.daily_done === s.daily_total ? 'good' : '');
  if (s.best_streak) pill('best streak', s.best_streak, '');
  pill('open', s.longterm_open || 0, '');
  if (s.overdue) pill('overdue', s.overdue, 'warn');
  if (!s.daily_total && !s.longterm_open && !s.longterm_done) {
    wrap.appendChild(el('span', { class: 'muted small', text: 'Nothing here yet.' }));
  }
}

function strip(task) {
  var bar = el('div', { class: 'strip' });
  (task.history || []).forEach(function (on, i) {
    var daysAgo = HISTORY_DAYS - 1 - i;
    var day = shiftDay(state.viewDay, -daysAgo);
    bar.appendChild(el('i', {
      class: on ? 'on' : '',
      title: shortDay(day) + (on ? ' — done' : '')
    }));
  });
  return bar;
}

function taskRow(task, kind) {
  var isDaily = task.recurrence === 'daily';
  var done = !!task.done;

  var meta = [];
  if (isDaily) {
    if (task.streak) meta.push(el('span', { class: 'streak', text: '🔥 ' + task.streak + ' day streak' }));
    meta.push(el('span', { text: (task.total_done || 0) + ' total' }));
    if (done && state.viewDay !== state.serverToday) meta.push(el('span', { text: 'done that day' }));
  } else {
    if (task.completed_at) {
      meta.push(el('span', { text: 'done ' + relDay(task.completed_at.slice(0, 10), state.serverToday) }));
    } else if (task.due_date) {
      meta.push(el('span', {
        class: task.overdue ? 'overdue' : '',
        text: dueText(task.due_date, state.serverToday)
      }));
    }
  }
  if (task.notes) meta.push(el('span', { text: '📝' }));
  if (task.archived) meta.push(el('span', { text: 'archived' }));

  var row = el('li', {
    class: 'task' + (done ? ' is-done' : '') + (task.overdue ? ' is-overdue' : ''),
    dataset: { id: String(task.id) }
  }, [
    el('button', {
      class: 'check', type: 'button', title: done ? 'Mark not done' : 'Mark done',
      'aria-label': done ? 'Mark not done' : 'Mark done',
      dataset: { act: 'toggle' }, text: done ? '✓' : ''
    }),
    el('div', { class: 'task-main' }, [
      el('div', { class: 'task-title', text: task.title }),
      meta.length ? el('div', { class: 'task-meta' }, meta) : null,
      task.notes ? el('div', { class: 'task-notes', text: task.notes }) : null
    ]),
    el('div', { class: 'task-actions' }, [
      el('button', {
        class: 'icon-btn', type: 'button', title: 'Edit', 'aria-label': 'Edit',
        dataset: { act: 'edit' }, text: '⋯'
      }),
      el('button', {
        class: 'icon-btn danger', type: 'button', title: 'Delete', 'aria-label': 'Delete',
        dataset: { act: 'delete' }, text: '×'
      })
    ]),
    isDaily ? strip(task) : null,
    state.editors[task.id] ? editor(task) : null
  ]);
  return row;
}

function editor(task) {
  var isDaily = task.recurrence === 'daily';
  var title = el('input', { type: 'text', maxlength: '500', value: task.title, dataset: { field: 'title' } });
  var notes = el('textarea', { rows: '3', maxlength: '20000', dataset: { field: 'notes' } });
  notes.value = task.notes || '';
  var kind = el('select', { dataset: { field: 'recurrence' } }, [
    el('option', { value: 'daily', text: 'Daily (repeats every day)', selected: isDaily }),
    el('option', { value: 'none', text: 'Long-term (one-off)', selected: !isDaily })
  ]);
  var due = el('input', { type: 'date', value: task.due_date || '', dataset: { field: 'due_date' } });

  var dueLabel = el('label', { hidden: isDaily }, ['Due date', due]);

  return el('div', { class: 'editor' }, [
    el('label', null, ['Title', title]),
    el('div', { class: 'editor-row' }, [
      el('label', { class: 'grow' }, ['Type', kind]),
      dueLabel
    ]),
    el('label', null, ['Notes', notes]),
    el('div', { class: 'editor-row' }, [
      el('button', { class: 'primary', type: 'button', dataset: { act: 'save' }, text: 'Save' }),
      el('button', { class: 'ghost', type: 'button', dataset: { act: 'cancel' }, text: 'Cancel' }),
      el('span', { class: 'grow' }),
      el('button', {
        class: 'ghost danger', type: 'button', dataset: { act: 'archive' },
        text: task.archived ? 'Unarchive' : 'Archive'
      })
    ])
  ]);
}

function renderLists() {
  var daily = state.tasks.filter(function (t) { return t.recurrence === 'daily'; });
  var longterm = state.tasks.filter(function (t) { return t.recurrence === 'none'; });

  longterm.sort(function (a, b) {
    if (a.archived !== b.archived) return a.archived ? 1 : -1;
    if (a.done !== b.done) return a.done ? 1 : -1;
    if (a.overdue !== b.overdue) return a.overdue ? -1 : 1;
    var ad = a.due_date || '9999-99-99', bd = b.due_date || '9999-99-99';
    if (ad !== bd) return ad < bd ? -1 : 1;
    return a.id - b.id;
  });

  var visibleLong = state.showDone ? longterm : longterm.filter(function (t) { return !t.done; });

  var dailyList = $('daily-list');
  var longList = $('long-list');
  dailyList.textContent = '';
  longList.textContent = '';

  daily.forEach(function (t) { dailyList.appendChild(taskRow(t, 'daily')); });
  visibleLong.forEach(function (t) { longList.appendChild(taskRow(t, 'longterm')); });

  $('daily-empty').hidden = daily.length > 0;
  $('long-empty').hidden = visibleLong.length > 0;
  $('daily-count').textContent = daily.length
    ? (state.summary.daily_done || 0) + ' of ' + daily.length + ' done' : '';
  $('long-count').textContent = longterm.length
    ? (state.summary.longterm_open || 0) + ' open' +
      (longterm.length - state.summary.longterm_open ? ' · ' + (state.summary.longterm_done) + ' done' : '')
    : '';
}

function render() {
  if (!state.user) return;
  renderHeader();
  renderStats();
  renderLists();
}

// ------------------------------------------------------------------ data

function load(day) {
  var path = '/api/tasks?date=' + (day || state.viewDay || '') +
             (state.showDone ? '&archived=1' : '');
  return api('GET', path).then(function (data) {
    state.viewDay = data.date;
    state.serverToday = data.server_today;
    state.tasks = data.tasks || [];
    state.summary = data.summary || {};
    render();
  });
}

function fail(err) {
  if (err && err.status === 401) {
    showAuth('Your session expired — please sign in again.');
    return;
  }
  toast((err && err.message) || 'Something went wrong');
}

// ---------------------------------------------------------------- actions

function toggle(id) {
  var task = state.tasks.filter(function (t) { return t.id === id; })[0];
  var body = task && task.recurrence === 'daily' ? { date: state.viewDay } : {};
  return api('POST', '/api/tasks/' + id + '/toggle', body).then(function (updated) {
    return patchLocal(updated).then(render);
  }).catch(fail);
}

function patchLocal(updated) {
  state.tasks = state.tasks.map(function (t) { return t.id === updated.id ? updated : t; });
  return api('GET', '/api/summary' + (state.viewDay ? '?date=' + state.viewDay : ''))
    .then(function (s) { state.summary = s; })
    .catch(function () {});
}

function saveEditor(row) {
  var id = +row.dataset.id;
  var get = function (f) { var n = row.querySelector('[data-field="' + f + '"]'); return n ? n.value : ''; };
  var body = {
    title: get('title'),
    notes: get('notes'),
    recurrence: get('recurrence')
  };
  if (body.recurrence === 'none') body.due_date = get('due_date') || null;
  return api('PATCH', '/api/tasks/' + id, body).then(function (updated) {
    delete state.editors[id];
    if (updated.archived) {
      state.tasks = state.tasks.filter(function (t) { return t.id !== id; });
    }
    // render only after the refreshed summary arrives, otherwise the stat
    // pills keep showing the counts from before the save
    return patchLocal(updated).then(function () {
      render();
      toast('Saved', true);
    });
  }).catch(fail);
}

function archive(id, row) {
  var currently = state.tasks.filter(function (t) { return t.id === id; })[0];
  var next = !(currently && currently.archived);
  return api('PATCH', '/api/tasks/' + id, { archived: next }).then(function () {
    delete state.editors[id];
    return load(state.viewDay).then(function () {
      toast(next ? 'Archived — tick “show done & archived” to see it again.' : 'Unarchived', true);
    });
  }).catch(fail);
}

function remove(id) {
  var task = state.tasks.filter(function (t) { return t.id === id; })[0];
  if (!confirm('Delete “' + (task ? task.title : '') + '” and its whole history?\nThis cannot be undone.')) return;
  return api('DELETE', '/api/tasks/' + id).then(function () {
    state.tasks = state.tasks.filter(function (t) { return t.id !== id; });
    delete state.editors[id];
    return api('GET', '/api/summary' + (state.viewDay ? '?date=' + state.viewDay : '')).then(function (s) {
      state.summary = s;
      render();
      toast('Deleted', true);
    });
  }).catch(fail);
}

// ----------------------------------------------------------------- wiring

$('login-form').addEventListener('submit', function (e) {
  e.preventDefault();
  var f = e.target;
  var btn = f.querySelector('button[type="submit"]');
  btn.disabled = true;
  api('POST', '/api/login', {
    username: f.username.value.trim().toLowerCase(),
    password: f.password.value
  }).then(function (data) {
    state.user = data.user;
    $('auth-error').hidden = true;
    showApp();
    return load(data.server_today);
  }).catch(function (err) {
    var box = $('auth-error');
    box.textContent = err.message;
    box.hidden = false;
  }).finally(function () { btn.disabled = false; });
});

$('register-form').addEventListener('submit', function (e) {
  e.preventDefault();
  var f = e.target;
  var btn = f.querySelector('button[type="submit"]');
  btn.disabled = true;
  api('POST', '/api/register', {
    username: f.username.value.trim().toLowerCase(),
    display_name: f.display_name.value.trim(),
    password: f.password.value
  }).then(function (data) {
    state.user = data.user;
    showApp();
    f.reset();
    toast('Welcome, ' + (data.user.display_name || data.user.username) + '!', true);
    return load(data.server_today);
  }).catch(function (err) {
    var box = $('auth-error');
    box.textContent = err.message;
    box.hidden = false;
  }).finally(function () { btn.disabled = false; });
});

Array.prototype.forEach.call(document.querySelectorAll('[data-auth-tab]'), function (b) {
  b.addEventListener('click', function () { switchAuthTab(b.dataset.authTab); });
});

$('add-form').addEventListener('submit', function (e) {
  e.preventDefault();
  var title = $('new-title').value.trim();
  if (!title) return;
  var body = {
    title: title,
    notes: $('new-notes').value,
    recurrence: state.recurrence
  };
  if (state.recurrence === 'none') body.due_date = $('new-due').value || null;
  var btn = e.target.querySelector('button[type="submit"]');
  btn.disabled = true;
  api('POST', '/api/tasks', body).then(function () {
    $('new-title').value = '';
    $('new-notes').value = '';
    $('new-due').value = '';
    $('new-notes').hidden = true;
    return load(state.viewDay);
  }).catch(fail).finally(function () {
    btn.disabled = false;
    $('new-title').focus();
  });
});

Array.prototype.forEach.call(document.querySelectorAll('[data-recurrence]'), function (b) {
  b.addEventListener('click', function () {
    state.recurrence = b.dataset.recurrence;
    Array.prototype.forEach.call(document.querySelectorAll('[data-recurrence]'), function (o) {
      o.classList.toggle('is-active', o === b);
    });
    $('due-wrap').hidden = state.recurrence !== 'none';
  });
});

$('notes-toggle').addEventListener('click', function () {
  var box = $('new-notes');
  box.hidden = !box.hidden;
  if (!box.hidden) box.focus();
});

$('show-done').addEventListener('change', function (e) {
  state.showDone = e.target.checked;
  load(state.viewDay).catch(fail);
});

$('prev-day').addEventListener('click', function () { load(shiftDay(state.viewDay, -1)).catch(fail); });
$('next-day').addEventListener('click', function () { load(shiftDay(state.viewDay, 1)).catch(fail); });
$('today-btn').addEventListener('click', function () { load(state.serverToday).catch(fail); });
$('refresh-btn').addEventListener('click', function () { load(state.viewDay).then(render).catch(fail); });
$('logout-btn').addEventListener('click', function () {
  api('POST', '/api/logout', {}).catch(function () {}).finally(function () {
    showAuth('Signed out.');
  });
});
$('user-btn').addEventListener('click', function () {
  var u = state.user;
  alert('Signed in as ' + u.username +
        (u.display_name ? ' (' + u.display_name + ')' : '') +
        '\nAccount created ' + String(u.created_at).slice(0, 10) +
        '\n\nServer day: ' + state.serverToday);
});
$('export-link').addEventListener('click', function () {
  var t = getToken();   // only set when a native app (not this page) is driving
  if (!t) return;       // otherwise the HttpOnly cookie handles it
  var link = this, done = false;
  fetch('/api/export', { headers: { 'Authorization': 'Bearer ' + t }, credentials: 'same-origin' })
    .then(function (r) { return r.blob(); })
    .then(function (b) {
      done = true;
      var a = document.createElement('a');
      a.href = URL.createObjectURL(b);
      a.download = 'piano-tasks-' + state.serverToday + '.json';
      a.click();
      URL.revokeObjectURL(a.href);
    }).catch(fail);
  if (!done) return;    // fetch is async: let the cookie path run as fallback
});

document.addEventListener('click', function (e) {
  var btn = e.target.closest && e.target.closest('[data-act]');
  if (!btn) return;
  var row = btn.closest('.task');
  if (!row) return;
  var id = +row.dataset.id;
  var act = btn.dataset.act;
  if (act === 'toggle') toggle(id);
  else if (act === 'delete') remove(id);
  else if (act === 'edit') {
    state.editors[id] = !state.editors[id];
    renderLists();
  } else if (act === 'cancel') { delete state.editors[id]; renderLists(); }
  else if (act === 'save') saveEditor(row);
  else if (act === 'archive') archive(id, row);
});

// ------------------------------------------------------------------- boot

window.addEventListener('resize', function () { if (state.user) renderHeader(); });

api('GET', '/health').then(function (info) {
  $('auth-foot').textContent = 'piano-tasks ' + info.version + ' · server day ' + info.server_today;
}).catch(function () {});

api('GET', '/api/me').then(function (me) {
  if (!me.authenticated) { showAuth(); return; }
  state.user = me.user;
  showApp();
  return load(me.server_today);
}).catch(function () { showAuth(); });