(function (global) {
  'use strict';
  const PACKAGE = 'com.arupbairagya.jupiter';
  const ORIGIN = 'https://jupiter.example.invalid';
  const versionNumber = (value) => {
    const match = /^(\d+)(?:[.-]|$)/.exec(String(value || ''));
    return match ? Number(match[1]) : null;
  };
  function safeRelease(release) {
    if (!release?.available || release.package !== PACKAGE || !/^\d+\.\d+\.\d+$/.test(release.version_name)
        || !Number.isSafeInteger(release.version_code) || release.version_code <= 0
        || !/^[a-f0-9]{64}$/.test(release.sha256 || '')) return null;
    const expected = `${ORIGIN}/downloads/Jupiter-${release.version_name}.apk`;
    return release.url === expected ? release : null;
  }
  function decide(manifest, installed, currentFrontend) {
    const android = safeRelease(manifest?.android);
    if (installed?.package === PACKAGE && Number.isSafeInteger(installed.version_code) && android
        && android.version_code > installed.version_code) return {kind: 'android', release: android};
    const current = versionNumber(currentFrontend), next = versionNumber(manifest?.frontend_version);
    if (current !== null && next !== null && next > current) return {kind: 'frontend', version: manifest.frontend_version};
    return {kind: 'none', release: android};
  }
  // JupiterUpdatesNative ships from versionCode 71 (1.0.70). A PRODUCTION Android shell (it has the native audio bridge
  // and loads the trusted HTTPS origin; debug builds load localhost) without it is therefore 1.0.69 or older. Such a
  // shell cannot open downloads itself (no DownloadListener; navigating the WebView to the APK would just replace
  // Jupiter), so it is offered the exact link to open in the phone's browser instead.
  const LEGACY_ANDROID_MAX_VERSION_CODE = 70;
  function legacyAndroidShell() {
    try {
      return !global.JupiterUpdatesNative && Boolean(global.JupiterNativeAudio) && global.location?.origin === ORIGIN;
    } catch (_) { return false; }
  }
  let started = false, pending = null, action = {kind: 'none'}, resolveOrigin, nativeInfo = null;
  const element = (id) => global.document.getElementById(id);
  async function installedVersion() {
    try {
      const raw = global.JupiterUpdatesNative?.installedVersion?.();
      if (raw) nativeInfo = JSON.parse(raw);
    } catch (_) { nativeInfo = null; }
    if (!nativeInfo && legacyAndroidShell()) {
      nativeInfo = {package: PACKAGE, version_code: LEGACY_ANDROID_MAX_VERSION_CODE, version_name: '1.0.69 or older', legacy: true};
    }
    renderVersionInfo();
    return nativeInfo;
  }
  // Always-visible "About Jupiter" version lines: which app shell this is, and the shared interface build.
  // The Jupiter AT test build (debug, ".test.debug") is labelled but never offered production APK updates (decide()).
  const TEST_PACKAGE = `${PACKAGE}.test.debug`;
  function appVersionLabel(info, desktop) {
    if (info?.package === PACKAGE && info.legacy) return 'Android 1.0.69 or older';
    if ((info?.package === PACKAGE || info?.package === TEST_PACKAGE) && info.version_name) {
      const name = info.package === TEST_PACKAGE ? 'Android test (Jupiter AT)' : 'Android';
      return Number.isSafeInteger(info.version_code) ? `${name} ${info.version_name} (build ${info.version_code})` : `${name} ${info.version_name}`;
    }
    return desktop ? 'Windows desktop app' : 'Web browser';
  }
  function renderVersionInfo() {
    const app = element('about-app-version'), ui = element('about-interface-version');
    if (app) app.textContent = appVersionLabel(nativeInfo, Boolean(global.pywebview));
    if (ui) ui.textContent = String(global.JUPITER_FRONTEND_VERSION || 'unversioned');
  }
  function render(manifest, error) {
    renderVersionInfo();
    const badge = element('header-update-badge'), status = element('update-status-text');
    action = error ? {kind: 'none'} : decide(manifest, nativeInfo, global.JUPITER_FRONTEND_VERSION);
    if (badge) {
      badge.classList.toggle('hidden', action.kind === 'none');
      badge.title = action.kind === 'android' ? `New Jupiter ${action.release.version_name} available. Download update.` : 'New Jupiter interface available. Reload to update.';
      badge.setAttribute('aria-label', badge.title);
    }
    if (!status) return;
    status.replaceChildren();
    if (error) { status.textContent = 'Update check unavailable. Try again.'; return; }
    if (action.kind === 'android') status.textContent = nativeInfo?.legacy
      ? `New Jupiter ${action.release.version_name} available. This app version can't download it by itself: open ${action.release.url} in Chrome to install it.`
      : `New Jupiter ${action.release.version_name} available.`;
    else if (action.kind === 'frontend') status.textContent = 'A new Jupiter interface is available. Reload when ready.';
    else if (nativeInfo?.package === TEST_PACKAGE) status.textContent = `Jupiter AT test build ${nativeInfo.version_name || ''}. Test builds are updated from the project, not from published releases.`.replace('  ', ' ');
    else if (nativeInfo?.package === PACKAGE && action.release) status.textContent = `Installed ${nativeInfo.version_name}. No newer published Android update.`;
    else status.textContent = 'Shared interface is current. Native app version cannot be verified here.';
    if (action.kind !== 'none') {
      const button = global.document.createElement('button');
      button.type = 'button'; button.className = 'secondary-btn release-update-action';
      button.textContent = action.kind === 'android' ? (nativeInfo?.legacy ? 'Copy download link' : 'Download update') : 'Reload to update';
      button.addEventListener('click', activate); status.appendChild(button);
    } else if (action.release && !nativeInfo) {
      const link = global.document.createElement('a');
      link.href = action.release.url; link.target = '_blank'; link.rel = 'noopener noreferrer';
      link.textContent = `Download latest Android ${action.release.version_name}`;
      link.className = 'release-update-action'; status.appendChild(link);
    }
  }
  function activate() {
    if (action.kind === 'frontend') { global.location.reload(); return; }
    if (action.kind !== 'android') return;
    const release = safeRelease(action.release);
    if (!release) return;
    if (nativeInfo?.legacy) { showLegacyPanel(release); return; }
    try {
      if (global.JupiterUpdatesNative?.openUpdate) { global.JupiterUpdatesNative.openUpdate(release.url); return; }
      global.open(release.url, '_blank', 'noopener,noreferrer');
    } catch (_) { render(null, true); }
  }
  function showLegacyPanel(release) {
    const doc = global.document;
    doc.getElementById('release-legacy-panel')?.remove();
    const panel = doc.createElement('section');
    panel.id = 'release-legacy-panel';
    panel.className = 'release-legacy-panel';
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-modal', 'false');
    panel.setAttribute('aria-labelledby', 'release-legacy-title');
    const title = doc.createElement('h2');
    title.id = 'release-legacy-title';
    title.textContent = `Update to Jupiter ${release.version_name}`;
    const text = doc.createElement('p');
    text.textContent = 'This app version can\u2019t download updates by itself. Copy the link, open it in Chrome, and install the update. Your account and chats stay as they are.';
    const link = doc.createElement('code');
    link.className = 'release-legacy-url';
    link.textContent = release.url;
    const status = doc.createElement('p');
    status.className = 'release-legacy-status';
    status.setAttribute('role', 'status');
    const copy = doc.createElement('button');
    copy.type = 'button';
    copy.className = 'primary-btn';
    copy.textContent = 'Copy link';
    copy.addEventListener('click', async () => {
      let copied = false;
      try { await global.navigator.clipboard.writeText(release.url); copied = true; } catch (_) { copied = false; }
      if (!copied) {
        const range = doc.createRange();
        range.selectNodeContents(link);
        const selection = global.getSelection?.();
        selection?.removeAllRanges();
        selection?.addRange(range);
      }
      status.textContent = copied ? 'Link copied. Paste it into Chrome.' : 'Link selected: copy it, then paste it into Chrome.';
    });
    const close = doc.createElement('button');
    close.type = 'button';
    close.className = 'secondary-btn';
    close.textContent = 'Close';
    close.addEventListener('click', () => panel.remove());
    const actions = doc.createElement('div');
    actions.className = 'release-legacy-actions';
    actions.append(copy, close);
    panel.append(title, text, link, actions, status);
    doc.body.appendChild(panel);
    copy.focus?.({preventScroll: true});
  }
  function check() {
    if (pending) return pending;
    pending = (async () => {
      const controller = new AbortController();
      const timer = global.setTimeout(() => controller.abort(), 8000);
      try {
        await installedVersion();
        const origin = await resolveOrigin();
        if (!origin) throw new Error('Backend unavailable');
        const response = await global.fetch(`${origin}/api/releases/latest`, {cache: 'no-store', signal: controller.signal});
        if (!response.ok) throw new Error('Update metadata unavailable');
        render(await response.json(), false);
      } catch (_) { render(null, true); }
      finally { global.clearTimeout(timer); pending = null; }
    })();
    return pending;
  }
  function start(options) {
    if (started) return;
    started = true; resolveOrigin = options.resolveBackendOrigin;
    element('header-update-badge')?.addEventListener('click', activate);
    element('check-software-update')?.addEventListener('click', check);
    global.document.addEventListener('visibilitychange', () => { if (!global.document.hidden) void check(); });
    global.addEventListener('online', check);
    // pywebview injects window.pywebview after startup, often while the first check is still in flight (and a
    // check() call during that is deduplicated), so the App line is relabelled the moment it arrives.
    global.addEventListener('pywebviewready', () => { renderVersionInfo(); void check(); });
    global.setInterval(() => { if (!global.document.hidden) void check(); }, 5 * 60 * 1000);
    void check();
  }
  global.JupiterReleaseUpdates = Object.freeze({start, check, decide, safeRelease, installedVersion, activate, appVersionLabel});
})(typeof window !== 'undefined' ? window : globalThis);
