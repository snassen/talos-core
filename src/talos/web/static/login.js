// The sign-in page (talos.webauth). Text only: no innerHTML here either.
const form = document.getElementById('login-form');
const msg = document.getElementById('msg');
const go = document.getElementById('go');
const say = (text, bad = true) => { msg.textContent = text; msg.classList.toggle('bad', bad); };
const next = () => {
  const n = new URLSearchParams(location.search).get('next') || '/';
  return n.startsWith('/') && !n.startsWith('//') ? n : '/';  // only back into Talos
};

fetch('/auth/status').then(r => r.json()).then(st => {
  if (st.signed_in) location.replace(next());
  if (!st.set_up) {
    say('Signing in is not set up yet. On this Mac, open Terminal and run:  uv run talos web setup  (in the talos folder).');
    go.disabled = true;
  } else if (st.locked_until) {
    say('Too many wrong attempts. Signing in opens again at ' + new Date(st.locked_until).toLocaleTimeString('sv-SE') + '.');
  } else {
    document.getElementById('password').focus();
  }
}).catch(() => say('Talos is not answering.'));

form.addEventListener('submit', async e => {
  e.preventDefault();
  go.disabled = true;
  say('Checking…', false);
  try {
    const r = await fetch('/auth/login', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Talos': '1'},
      body: JSON.stringify({password: form.password.value, code: form.code.value})});
    const d = await r.json().catch(() => ({}));
    if (r.ok) { say('Signed in.', false); location.replace(next()); return; }
    form.code.value = '';
    say(d.error || 'That did not work.');
  } catch (err) {
    say('Talos is not answering.');
  }
  go.disabled = false;
});
