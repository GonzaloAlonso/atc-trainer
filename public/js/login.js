const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);

/** Only same-site relative paths are allowed as redirect targets. */
function nextUrl() {
  const n = params.get('next') || '/';
  return n.startsWith('/') && !n.startsWith('//') ? n : '/';
}

async function post(path, body) {
  const res = await fetch(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Error ${res.status}`);
  return data;
}

function showChange(currentKnown) {
  $('login-form').classList.add('hidden');
  $('change-form').classList.remove('hidden');
  $('current-wrap').classList.toggle('hidden', !!currentKnown);
  $('current').required = !currentKnown;
  if (currentKnown) $('current').value = currentKnown;
  $('new-password').focus();
}

$('login-form').onsubmit = async (e) => {
  e.preventDefault();
  $('login-error').textContent = '';
  const btn = e.submitter;
  btn.disabled = true;
  try {
    const { user } = await post('/api/auth/login', { username: $('username').value.trim(), password: $('password').value });
    if (user.must_change) showChange($('password').value);
    else location.replace(nextUrl());
  } catch (err) {
    $('login-error').textContent = err.message;
    $('password').select();
  } finally {
    btn.disabled = false;
  }
};

$('change-form').onsubmit = async (e) => {
  e.preventDefault();
  $('change-error').textContent = '';
  if ($('new-password').value !== $('new-password-2').value) {
    $('change-error').textContent = 'The new passwords do not match.';
    return;
  }
  try {
    await post('/api/auth/password', { current_password: $('current').value, new_password: $('new-password').value });
    location.replace(nextUrl());
  } catch (err) {
    $('change-error').textContent = err.message;
  }
};

// Arrived here because a password change is pending on an existing session?
(async () => {
  if (params.get('change') !== '1') return;
  const res = await fetch('/api/auth/me');
  if (res.ok && (await res.json()).must_change) showChange(null);
})();

// Footer: version and copyright from the public health endpoint.
fetch('/api/health').then((r) => r.json()).then((h) => {
  $('footer-version').textContent = `Visor ATC ${h.version}`;
  if (h.copyright) $('footer-copyright').textContent = h.copyright;
}).catch(() => {});
