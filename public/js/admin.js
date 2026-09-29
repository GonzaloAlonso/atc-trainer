import { api } from './net.js';
import { initI18n, t } from './i18n.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const when = (x) => (x ? new Date(x * 1000).toLocaleString(document.documentElement.lang, { dateStyle: 'medium', timeStyle: 'short' }) : '—');

let me = null;

function tutorialStatus(u) {
  if (u.tutorial_state === 'completed') return `<span class="pill">${esc(t('admin.tutCompleted'))}</span>`;
  if (u.tutorial_state === 'dismissed') return `<span class="pill warn">${esc(t('admin.tutDeclined'))}</span>`;
  if (u.tutorial_step > 0) return `<span class="pill warn">${esc(t('admin.tutLesson', { n: u.tutorial_step + 1 }))}</span>`;
  return `<span class="dim">${esc(t('admin.tutNotStarted'))}</span>`;
}

function toast(msg, err = false) {
  const el = $('toast');
  el.textContent = msg;
  el.className = err ? 'err' : '';
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add('hidden'), 3500);
}

async function call(method, path, body) {
  try {
    return await api(path, body, method);
  } catch (e) {
    toast(e.message, true);
    return null;
  }
}

const roleName = (r) => t(r === 'admin' ? 'admin.roleAdmin' : 'admin.roleController');

async function load() {
  const users = await call('GET', '/api/admin/users');
  if (!users) return;
  $('count').textContent = t('admin.count', { n: users.length });
  $('users').innerHTML = users.map((u) => {
    const self = u.id === me.id;
    return `<tr data-id="${u.id}" class="${u.disabled ? 'off' : ''}">
      <td><b>${esc(u.username)}</b>${self ? ` <span class="pill">${esc(t('admin.you'))}</span>` : ''}${u.must_change ? ` <span class="pill warn">${esc(t('admin.mustChangePill'))}</span>` : ''}</td>
      <td><select data-act="role" ${self ? `disabled title="${esc(t('admin.ownRole'))}"` : ''}>
            <option value="controller" ${u.role === 'controller' ? 'selected' : ''}>${esc(t('admin.roleController'))}</option>
            <option value="admin" ${u.role === 'admin' ? 'selected' : ''}>${esc(t('admin.roleAdmin'))}</option>
          </select></td>
      <td><button data-act="toggle" class="ghost-btn small" ${self ? 'disabled' : ''}>${esc(t(u.disabled ? 'admin.disabled' : 'admin.active'))}</button></td>
      <td>${tutorialStatus(u)}</td>
      <td>${esc(t(`levels.${u.coach_level}`))}</td>
      <td>${when(u.last_login)}</td>
      <td>${when(u.created)}</td>
      <td class="actions">
        <button data-act="tutorial" class="ghost-btn small" ${u.tutorial_state == null && !u.tutorial_step ? 'disabled' : ''}>${esc(t('admin.resetTutorial'))}</button>
        <button data-act="password" class="ghost-btn small">${esc(t('admin.resetPassword'))}</button>
        <button data-act="delete" class="danger small" ${self ? 'disabled' : ''}>${esc(t('admin.delete'))}</button>
      </td>
    </tr>`;
  }).join('');
  const byId = Object.fromEntries(users.map((u) => [u.id, u]));
  for (const tr of $('users').querySelectorAll('tr')) {
    const u = byId[tr.dataset.id];
    tr.querySelector('[data-act=role]').onchange = async (e) => {
      if (await call('PATCH', `/api/admin/users/${u.id}`, { role: e.target.value })) toast(t('admin.nowRole', { user: u.username, role: roleName(e.target.value) }));
      load();
    };
    tr.querySelector('[data-act=toggle]').onclick = async () => {
      if (await call('PATCH', `/api/admin/users/${u.id}`, { disabled: !u.disabled })) toast(t(u.disabled ? 'admin.enabled' : 'admin.disabledToast', { user: u.username }));
      load();
    };
    tr.querySelector('[data-act=password]').onclick = () => openPasswordDialog(u);
    tr.querySelector('[data-act=tutorial]').onclick = async () => {
      if (await call('PATCH', `/api/admin/users/${u.id}`, { tutorial_reset: true })) toast(t('admin.tutorialAgain', { user: u.username }));
      load();
    };
    tr.querySelector('[data-act=delete]').onclick = async () => {
      if (!confirm(t('admin.confirmDelete', { user: u.username }))) return;
      if (await call('DELETE', `/api/admin/users/${u.id}`)) toast(t('admin.deleted', { user: u.username }));
      load();
    };
  }
}

function openPasswordDialog(u) {
  const dlg = $('pw-dialog');
  $('pw-title').textContent = t('admin.pwFor', { user: u.username });
  $('pw-new').value = '';
  $('pw-error').textContent = '';
  $('pw-must-change').checked = u.id !== me.id;
  dlg.showModal();
  $('pw-cancel').onclick = () => dlg.close();
  $('pw-form').onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api(`/api/admin/users/${u.id}`, { password: $('pw-new').value, must_change: $('pw-must-change').checked }, 'PATCH');
      dlg.close();
      if (u.id === me.id) { location.href = '/login'; return; }   // own sessions were revoked
      toast(t('admin.pwUpdated', { user: u.username }));
      load();
    } catch (err) {
      $('pw-error').textContent = err.message;
    }
  };
}

$('add-form').onsubmit = async (e) => {
  e.preventDefault();
  const username = $('add-username').value.trim();
  const created = await call('POST', '/api/admin/users', {
    username, password: $('add-password').value, role: $('add-role').value, must_change: $('add-must-change').checked,
  });
  if (!created) return;
  toast(t('admin.added', { user: username }));
  e.target.reset();
  $('add-must-change').checked = true;
  load();
};

$('logout').onclick = async () => {
  await call('POST', '/api/auth/logout', {});
  location.href = '/login';
};

me = await call('GET', '/api/auth/me');
await initI18n(me?.lang);
document.title = t('admin.title');
if (me) {
  $('me').textContent = t('admin.signedInAs', { user: me.username });
  load();
}
