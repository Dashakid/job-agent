"""
Bot-challenge detection shared by main.py and batch_runner.py.

Nothing here solves or bypasses a challenge. The runners use it to tell the
two kinds apart:

- A BLOCKING challenge stops the form from being reached or filled: a
  Cloudflare "Just a moment..." interstitial, or a reCAPTCHA/hCaptcha image
  puzzle popped over the page. The runner pauses and waits for the human.
- A PASSIVE one does not: the invisible reCAPTCHA v3 badge Greenhouse puts on
  every application page, or an "I'm not a robot" checkbox / Turnstile widget
  inside the form. Filling carries on, and the review step reminds the human
  to tick it before submitting.

The old check matched any iframe titled "reCAPTCHA", which includes the
invisible badge. Whether it had loaded by the time of the check was a race,
so the same Greenhouse job was "blocked by a challenge" on one run and filled
fine on the next.
"""

# Returns a short reason string when a blocking challenge is showing, else "".
BLOCKING_CHALLENGE_JS = r"""() => {
  const title = (document.title || '').toLowerCase();
  const titleMarkers = ['just a moment', 'attention required', 'access denied', 'are you human'];
  if (titleMarkers.some(m => title.includes(m))) return 'challenge page: ' + document.title.trim();
  if (document.querySelector('#cf-challenge-running, #challenge-form, #challenge-stage')) {
    return 'Cloudflare challenge';
  }
  const shown = e => {
    const r = e.getBoundingClientRect();
    const s = getComputedStyle(e);
    return r.width >= 60 && r.height >= 60 && r.bottom > 0 && r.right > 0 &&
           s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  // reCAPTCHA's image puzzle lives in the "bframe" iframe; it sits hidden
  // off-screen until a challenge is actually issued.
  for (const f of document.querySelectorAll("iframe[src*='recaptcha'][src*='bframe']")) {
    if (shown(f)) return 'reCAPTCHA puzzle';
  }
  for (const f of document.querySelectorAll("iframe[src*='hcaptcha'][src*='frame=challenge']")) {
    if (shown(f)) return 'hCaptcha puzzle';
  }
  return '';
}"""

# Returns a description of a CAPTCHA widget the human must tick at submit
# time (not blocking), else "".
FORM_CAPTCHA_JS = r"""() => {
  const shown = e => {
    const r = e.getBoundingClientRect();
    const s = getComputedStyle(e);
    return r.width >= 60 && r.height >= 40 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  for (const f of document.querySelectorAll("iframe[src*='recaptcha'][src*='anchor']")) {
    if (f.closest('.grecaptcha-badge') || /size=invisible/.test(f.src)) continue;
    if (shown(f)) return "reCAPTCHA \"I'm not a robot\" checkbox";
  }
  for (const f of document.querySelectorAll("iframe[src*='hcaptcha'][src*='frame=checkbox']")) {
    if (/checkbox-invisible/.test(f.src)) continue;
    if (shown(f)) return 'hCaptcha checkbox';
  }
  for (const e of document.querySelectorAll(".cf-turnstile, iframe[src*='challenges.cloudflare.com']")) {
    if (shown(e)) return 'Cloudflare Turnstile check';
  }
  return '';
}"""
