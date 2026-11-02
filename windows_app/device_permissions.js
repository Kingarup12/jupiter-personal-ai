(function (root) {
  'use strict';
  const descriptions = Object.freeze({
    microphone: {title:'Use your microphone', icon:'ph-microphone', detail:'Speak to Jupiter using dictation or Live Conversation. You can mute or stop whenever you like.', action:'Continue to microphone'},
    camera: {title:'Turn on Live camera', icon:'ph-camera', detail:'Jupiter will receive camera frames while Live video is on. Turn the camera off at any time; voice can continue without video.', action:'Continue to camera'},
    screen: {title:'Share your screen', icon:'ph-monitor', detail:'Choose the tab, window or screen you want Jupiter to see. Notifications and private information in that area may be visible.', action:'Choose what to share'},
    notifications: {title:'Enable notifications', icon:'ph-bell', detail:'Receive reminder notifications outside Jupiter. You can keep using in-app reminders without them.', action:'Continue to notifications'}
  });
  function createPermissions(win) {
    const storageKey = 'jupiter.device-permissions.v1';
    const rememberedKinds = Object.keys(descriptions).filter(kind => kind !== 'screen');
    const seen = new Set();
    try {
      const stored = JSON.parse(win.localStorage?.getItem(storageKey) || 'null');
      if (stored && typeof stored === 'object' && !Array.isArray(stored)) {
        rememberedKinds.forEach(kind => { if (stored[kind] === true) seen.add(kind); });
      }
    } catch (_) { /* storage may be disabled; keep this permission session-only */ }
    const pending = new Map();
    let active = null;
    function cancel() { active?.finish(false); }
    function run(kind, action, {force = false} = {}) {
      if (!descriptions[kind]) return Promise.resolve(false);
      // The Windows host supplies the REAL consent panel; avoid double prompts.
      const hostOwned = kind !== 'screen' && typeof win.pywebview?.api?.device_permission_ui === 'function';
      if (!force && (hostOwned || (kind !== 'screen' && seen.has(kind)))) {
        try { return Promise.resolve(action()); } catch (error) { return Promise.reject(error); }
      }
      if (pending.has(kind)) return pending.get(kind);
      if (active) return Promise.resolve(false); // never overlap consent panels
      const previousFocus = win.document.activeElement;
      const content = descriptions[kind];
      const panel = win.document.createElement('div');
      panel.className = 'jupiter-device-permission';
      panel.innerHTML = '<section class="jupiter-device-permission-card" role="dialog" aria-modal="true" aria-labelledby="device-permission-title" aria-describedby="device-permission-detail">'
        + '<button class="jupiter-device-permission-close" type="button" aria-label="Cancel permission request"><i class="ph ph-x" aria-hidden="true"></i></button>'
        + '<div class="jupiter-device-permission-eyebrow">JUPITER · DEVICE ACCESS</div><div class="jupiter-device-permission-symbol" aria-hidden="true"><i></i></div>'
        + '<h2 id="device-permission-title"></h2><p id="device-permission-detail"></p>'
        + '<p class="jupiter-device-permission-note">Your browser or device will ask for the final permission. Continue does not grant access.</p>'
        + '<div class="jupiter-device-permission-buttons"><button type="button" class="secondary-btn" data-cancel>Not now</button><button type="button" class="primary-btn" data-continue></button></div></section>';
      panel.querySelector('h2').textContent = content.title;
      panel.querySelector('#device-permission-detail').textContent = content.detail;
      panel.querySelector('.jupiter-device-permission-symbol i').className = 'ph ' + content.icon;
      const proceed = panel.querySelector('[data-continue]');
      proceed.textContent = content.action;
      const close = panel.querySelector('.jupiter-device-permission-close');
      const deny = panel.querySelector('[data-cancel]');
      let resolve, reject;
      const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
      const siblings = Array.from(win.document.body.children).filter(el => !el.inert);
      const finish = (continued) => {
        if (active?.panel !== panel) return;
        active = null; pending.delete(kind);
        win.document.removeEventListener('keydown', keyboard, true);
        win.removeEventListener('pagehide', cancel);
        panel.remove(); siblings.forEach(el => {el.inert = false;});
        previousFocus?.isConnected && previousFocus.focus?.({preventScroll:true});
        if (!continued) return resolve(false);
        if (kind !== 'screen') {
          seen.add(kind);
          try {
            const stored = JSON.parse(win.localStorage?.getItem(storageKey) || 'null');
            const choices = stored && typeof stored === 'object' && !Array.isArray(stored) ? stored : {};
            choices[kind] = true;
            win.localStorage?.setItem(storageKey, JSON.stringify(choices));
          } catch (_) { /* the browser/OS remains authoritative for granting device access */ }
        }
        // Call directly INSIDE the Continue click, preserving transient user
        // activation required by getDisplayMedia and notification permission.
        try { resolve(action()); } catch (error) { reject(error); }
      };
      const keyboard = event => {
        if (event.key === 'Escape') {event.preventDefault(); event.stopImmediatePropagation(); finish(false);}
        if (event.key === 'Tab') {
          event.preventDefault();
          const controls = [close, deny, proceed];
          const current = controls.indexOf(win.document.activeElement);
          controls[(current + (event.shiftKey ? 2 : 1)) % 3].focus();
        }
      };
      active = {panel, finish}; pending.set(kind, promise);
      close.addEventListener('click', () => finish(false));
      deny.addEventListener('click', () => finish(false));
      proceed.addEventListener('click', () => finish(true));
      panel.addEventListener('click', event => {if (event.target === panel) finish(false);});
      siblings.forEach(el => {el.inert = true;});
      win.document.body.appendChild(panel);
      win.document.addEventListener('keydown', keyboard, true);
      win.addEventListener('pagehide', cancel);
      deny.focus({preventScroll:true});
      return promise;
    }
    async function explain(kind) { return Boolean(await run(kind, () => true)); }
    async function media(devices, constraints) {
      const kind = constraints.video ? 'camera' : 'microphone';
      // Already-authorized web capture requires no repeated explanation.
      let granted = false;
      try { granted = (await win.navigator.permissions?.query({name:kind}))?.state === 'granted'; } catch (_) {}
      const result = granted ? await devices.getUserMedia(constraints)
        : await run(kind, () => devices.getUserMedia(constraints));
      if (result === false) throw new win.DOMException('Device access cancelled.', 'NotAllowedError');
      return result;
    }
    return {run, explain, media, cancel};
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {descriptions, createPermissions};
  if (root?.document) root.JupiterDevicePermissions = createPermissions(root);
})(typeof window !== 'undefined' ? window : null);
