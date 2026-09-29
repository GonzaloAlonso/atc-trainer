/**
 * Interface languages: English, German, Spanish.
 *
 * Strings live in /locales/<lang>.json, nested by area ("top.pause"). Static markup is
 * translated through data-i18n* attributes; code calls t(key, params). Coach messages from the
 * server ({key, p, text}) are rendered with tm(): the key is looked up in the "coach" section and
 * falls back to the server's English text. Radio phraseology stays ICAO English everywhere.
 */

export const LANGS = { en: 'English', de: 'Deutsch', es: 'Español' };
const STORE_KEY = 'visor.lang';

let lang = 'en';
let dict = {};
let base = {};

function pick(pref) {
  if (pref && LANGS[pref]) return pref;
  try { const saved = localStorage.getItem(STORE_KEY); if (saved && LANGS[saved]) return saved; } catch { /* ignore */ }
  const nav = (navigator.languages || [navigator.language || 'en']).map((l) => l.slice(0, 2).toLowerCase());
  return nav.find((l) => LANGS[l]) || 'en';
}

async function load(l) {
  const res = await fetch(`/locales/${l}.json`);
  if (!res.ok) throw new Error(`locale ${l}: HTTP ${res.status}`);
  return res.json();
}

function lookup(d, key) {
  let v = d;
  for (const part of key.split('.')) {
    if (v == null || typeof v !== 'object') return undefined;
    v = v[part];
  }
  return typeof v === 'string' ? v : undefined;
}

function format(s, params) {
  if (!params) return s;
  return s.replace(/\{(\w+)\}/g, (m, k) => (params[k] !== undefined && params[k] !== null ? String(params[k]) : m));
}

/** Load the language (user preference, then saved choice, then the browser) and translate the page. */
export async function initI18n(pref) {
  lang = pick(pref);
  try {
    base = await load('en');
    dict = lang === 'en' ? base : await load(lang);
  } catch (e) {
    console.warn('translations unavailable', e);
    lang = 'en';
  }
  document.documentElement.lang = lang;
  applyDom(document);
  document.documentElement.classList.remove('i18n-pending');
  return lang;
}

export function getLang() { return lang; }

/** Remember the choice (and tell the server when signed in), then reload in that language. */
export async function setLang(l, saveToServer = true) {
  if (!LANGS[l]) return;
  try { localStorage.setItem(STORE_KEY, l); } catch { /* ignore */ }
  if (saveToServer) {
    try {
      await fetch('/api/auth/prefs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ lang: l }) });
    } catch { /* offline: the local choice still applies */ }
  }
  location.reload();
}

/** Translated string; missing keys fall back to English, then to the key itself. */
export function t(key, params) {
  const s = lookup(dict, key) ?? lookup(base, key);
  return s === undefined ? key : format(s, params);
}

/** A server coach message {key, p, text} in the current language (parameters may be messages). */
export function tm(m) {
  if (!m) return '';
  if (typeof m === 'string') return m;
  const p = {};
  for (const [k, v] of Object.entries(m.p || {})) p[k] = v && typeof v === 'object' && v.key ? tm(v) : v;
  const s = dict.coach?.[m.key] ?? base.coach?.[m.key];
  return s !== undefined ? format(s, p) : (m.text ?? m.key);
}

/** data-i18n (text), data-i18n-html (our own markup), data-i18n-title, -placeholder, -aria-label. */
export function applyDom(root) {
  root.querySelectorAll('[data-i18n]').forEach((el) => { el.textContent = t(el.dataset.i18n); });
  root.querySelectorAll('[data-i18n-html]').forEach((el) => { el.innerHTML = t(el.dataset.i18nHtml); });
  for (const attr of ['title', 'placeholder', 'aria-label']) {
    const data = 'i18n' + attr.split('-').map((w) => w[0].toUpperCase() + w.slice(1)).join('');
    root.querySelectorAll(`[data-i18n-${attr}]`).forEach((el) => { el.setAttribute(attr, t(el.dataset[data])); });
  }
}

/** A language picker (<select>) that switches and reloads. */
export function languageSelect(el, saveToServer = true) {
  el.innerHTML = Object.entries(LANGS).map(([k, name]) => `<option value="${k}">${name}</option>`).join('');
  el.value = lang;
  el.onchange = () => setLang(el.value, saveToServer);
}

export const fmt = {
  num: (n, digits = 0) => (n == null ? '—' : Number(n).toLocaleString(lang, { maximumFractionDigits: digits, minimumFractionDigits: digits })),
  date: (t) => new Date(t * 1000).toLocaleString(lang, { dateStyle: 'medium', timeStyle: 'short' }),
};
