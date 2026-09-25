import { api } from './net.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const when = (t) => (t ? new Date(t * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : '—');

let me = null;

function toast(msg, err = false) {
  const t = $('toast');
  t.textContent = msg;
  t.className = err ? 'err' : '';
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add('hidden'), 3500);
}

async function call(method, path, body) {
  try {
    return await api(path, body, method);
  } catch (e) {
    toast(e.message, true);
    return null;
  }
}

async function load() {
  const users = await call('GET', '/api/admin/users');
  if (!users) return;
  $('count').textContent = `${users.length} account${users.length === 1 ? '' : 's'}`;
  $('users').innerHTML = users.map((u) => {
    const self = u.id === me.id;
    return `<tr data-id="${u.id}" class="${u.disabled ? 'off' : ''}">
      <td><b>${esc(u.username)}</b>${self ? ' <span class="pill">you</span>' : ''}${u.must_change ? ' <span class="pill warn">must change password</span>' : ''}</td>
      <td><select data-act="role" ${self ? 'disabled title="You can\'t change your own role"' : ''}>
            <option value="controller" ${u.role === 'controller' ? 'selected' : ''}>Controller</option>
            <option value="admin" ${u.role === 'admin' ? 'selected' : ''}>Admin</option>
          </select></td>
      <td><button data-act="toggle" class="ghost-btn small" ${self ? 'disabled' : ''}>${u.disabled ? 'Disabled — enable' : 'Active — disable'}</button></td>
      <td>${when(u.last_login)}</td>
      <td>${when(u.created)}</td>
      <td class="actions">
        <button data-act="password" class="ghost-btn small">Reset password</button>
        <button data-act="delete" class="danger small" ${self ? 'disabled' : ''}>Delete</button>
      </td>
    </tr>`;
  }).join('');
  const byId = Object.fromEntries(users.map((u) => [u.id, u]));
  for (const tr of $('users').querySelectorAll('tr')) {
    const u = byId[tr.dataset.id];
    tr.querySelector('[data-act=role]').onchange = async (e) => {
      if (await call('PATCH', `/api/admin/users/${u.id}`, { role: e.target.value })) toast(`${u.username} is now ${e.target.value}`);
      load();
    };
    tr.querySelector('[data-act=toggle]').onclick = async () => {
      if (await call('PATCH', `/api/admin/users/${u.id}`, { disabled: !u.disabled })) toast(`${u.username} ${u.disabled ? 'enabled' : 'disabled'}`);
      load();
    };
    tr.querySelector('[data-act=password]').onclick = () => openPasswordDialog(u);
    tr.querySelector('[data-act=delete]').onclick = async () => {
      if (!confirm(`Delete ${u.username}? This can't be undone.`)) return;
      if (await call('DELETE', `/api/admin/users/${u.id}`)) toast(`${u.username} deleted`);
      load();
    };
  }
}

function openPasswordDialog(u) {
  const dlg = $('pw-dialog');
  $('pw-user').textContent = u.username;
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
      toast(`Password for ${u.username} updated`);
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
  toast(`${username} added`);
  e.target.reset();
  $('add-must-change').checked = true;
  load();
};

$('logout').onclick = async () => {
  await call('POST', '/api/auth/logout', {});
  location.href = '/login';
};

(async () => {
  me = await call('GET', '/api/auth/me');
  if (!me) return;
  $('me').textContent = `Signed in as ${me.username}`;
  load();
})();
