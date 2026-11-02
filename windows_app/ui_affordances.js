(function (root) {
  'use strict';
  function tooltipLabel(attrs, text = '') {
    const explicit = attrs['aria-label'] || attrs['data-tooltip'] || attrs.title;
    const label = String(explicit || text).replace(/\s+/g, ' ').trim();
    return explicit || label.length <= 72 ? label : '';
  }
  // 17 Oct: a hover box that repeats what the control already shows ("Let's go" over a "Let's go" button, "Get OTP"
  // over "Get OTP") is noise. Show a tooltip only when it adds something: an icon-only control, a label that says
  // more than the visible words, or visible text that is cut off.
  function tooltipRedundant(label, visibleText, truncated = false) {
    const norm = value => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase().replace(/[.!?…]+$/, '');
    const tip = norm(label);
    const shown = norm(visibleText);
    if (!tip) return true;
    if (!shown || truncated) return false;
    return tip === shown || tip.includes(shown) || shown.includes(tip);
  }
  function tooltipPosition(rect, size, viewport) {
    const clamp = (n, limit) => Math.max(8, Math.min(n, Math.max(8, limit)));
    const above = rect.top - size.height - 10;
    return {
      left: clamp(rect.left + rect.width / 2 - size.width / 2, viewport.width - size.width - 8),
      top: clamp(above >= 8 ? above : rect.top + rect.height + 10, viewport.height - size.height - 8)
    };
  }
  function permissionHelp(type) {
    const common = 'This guide does not grant or change permission. Only your browser or device can approve access.';
    const help = {
      microphone: {title: 'Microphone permission', icon: 'ph-microphone',
        intro: 'Jupiter needs microphone access only when you choose speech input or Live Voice.',
        steps: ['Start the microphone, then choose Allow in the browser or device prompt.',
          'If access was blocked: on Android, open Settings → Apps → Jupiter → Permissions → Microphone. On Windows or the web, check microphone access in system settings and the browser’s site permissions.',
          'Return to Jupiter and try the microphone again. You can keep using text chat without microphone access.']},
      camera: {title: 'Camera permission', icon: 'ph-camera',
        intro: 'Camera access is used when you turn on video in Live Conversation.',
        steps: ['Turn on the camera, then choose Allow in the browser or device prompt.',
          'If access was blocked: on Android, open Settings → Apps → Jupiter → Permissions → Camera. On Windows or the web, check camera access in system settings and the browser’s site permissions.',
          'Return to Jupiter and turn the camera on again. Live voice can continue without video.']},
      screen: {title: 'Screen-sharing permission', icon: 'ph-monitor',
        intro: 'Share only the tab, window or screen you want Jupiter to see. Private information in that area can be visible.',
        steps: ['Choose Share screen in Live Conversation, review the Jupiter explanation, then select an area in your browser or device picker.',
          'The capture picker belongs to your browser or device. Jupiter cannot approve or choose a screen for you.',
          'Use Stop sharing in Jupiter or the system sharing control to end capture.']},
      notifications: {title: 'Notification permission', icon: 'ph-bell',
        intro: 'Device notifications are optional and depend on support in your browser or installed app.',
        steps: ['Use Jupiter’s existing notification option if available, then choose Allow in the device or browser prompt.',
          'If blocked, check notification permissions for Jupiter or this website in your device or browser settings.',
          'Without device notification access, you can still read notices inside Jupiter.']}
    };
    return help[type] ? Object.assign({}, help[type], {note: common}) : null;
  }
  const api = {tooltipLabel, tooltipRedundant, tooltipPosition, permissionHelp};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (!root || !root.document) return;
  const doc = root.document;
  function init() {
    const tip = doc.createElement('div');
    tip.id = 'jupiter-control-tooltip';
    tip.className = 'jupiter-control-tooltip';
    tip.setAttribute('role', 'tooltip');
    tip.hidden = true;
    doc.body.appendChild(tip);
    let active = null;
    let oldTitle = null;
    let oldDescription = null;
    let labelObserver = null;
    function hide() {
      if (labelObserver) labelObserver.disconnect();
      labelObserver = null;
      if (active) {
        active.classList.remove('jupiter-tooltip-managed');
        if (oldTitle !== null && !active.hasAttribute('title')) active.setAttribute('title', oldTitle);
        if (oldDescription === null) active.removeAttribute('aria-describedby');
        else active.setAttribute('aria-describedby', oldDescription);
      }
      active = null;
      tip.hidden = true;
    }
    function update() {
      if (!active || !active.isConnected || active.closest('.tooltip-suppressed, [hidden], .hidden')) return hide();
      if (active.hasAttribute('title')) {
        oldTitle = active.getAttribute('title');
        active.removeAttribute('title');
      }
      const label = tooltipLabel({
        'aria-label': active.getAttribute('aria-label'),
        'data-tooltip': active.getAttribute('data-tooltip'), title: oldTitle
      }, active.textContent);
      const visible = typeof active.innerText === 'string' ? active.innerText : active.textContent;
      const truncated = active.scrollWidth > active.clientWidth + 1;
      if (!label || tooltipRedundant(label, visible, truncated)) {
        tip.hidden = true;
        return;
      }
      tip.textContent = label;
      tip.hidden = false;
      const pos = tooltipPosition(active.getBoundingClientRect(), tip.getBoundingClientRect(),
        {width: root.innerWidth, height: root.innerHeight});
      tip.style.left = pos.left + 'px';
      tip.style.top = pos.top + 'px';
    }
    function show(target) {
      const control = target.closest && target.closest('button, [role="button"], [data-tooltip]');
      // Preserve the authorised model-diagnostic tooltip and conversation hover cards.
      if (!control || control.closest('[data-model-diagnostic-control], .regen-btn-wrapper, .tooltip-suppressed, .chat-history-item') || control.disabled) return;
      if (control === active) return;
      hide();
      active = control;
      oldTitle = control.getAttribute('title');
      oldDescription = control.getAttribute('aria-describedby');
      control.removeAttribute('title');
      control.classList.add('jupiter-tooltip-managed');
      control.setAttribute('aria-describedby', [oldDescription, tip.id].filter(Boolean).join(' '));
      update();
      if (active) {
        labelObserver = new root.MutationObserver(update);
        labelObserver.observe(active, {attributes: true, attributeFilter: ['aria-label', 'data-tooltip', 'title', 'class', 'disabled'], childList: true, subtree: true, characterData: true});
      }
    }
    doc.addEventListener('pointerover', event => {
      if (event.pointerType === 'touch') return;
      show(event.target);
    });
    doc.addEventListener('pointerout', event => {
      if (active && active.contains(event.target) && !active.contains(event.relatedTarget)) hide();
    });
    doc.addEventListener('focusin', event => {
      if (event.target.matches(':focus-visible')) show(event.target);
    });
    doc.addEventListener('focusout', hide);
    doc.addEventListener('pointerdown', hide);
    doc.addEventListener('scroll', hide, true);
    root.addEventListener('resize', hide);

    const overlay = doc.createElement('div');
    overlay.className = 'modal-overlay jupiter-permission-help hidden';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-labelledby', 'jupiter-permission-title');
    overlay.setAttribute('aria-describedby', 'jupiter-permission-intro');
    overlay.innerHTML = '<div class="modal-content glass-modal jupiter-permission-card"><button type="button" class="jupiter-permission-close" aria-label="Close permission help"><i class="ph ph-x" aria-hidden="true"></i></button><div class="jupiter-permission-symbol" aria-hidden="true"><i></i></div><h2 id="jupiter-permission-title"></h2><p id="jupiter-permission-intro"></p><ol></ol><p class="jupiter-permission-note"></p><button type="button" class="primary-btn jupiter-permission-done">Got it</button></div>';
    doc.body.appendChild(overlay);
    let returnFocus = null;
    const close = overlay.querySelector('.jupiter-permission-close');
    const done = overlay.querySelector('.jupiter-permission-done');
    function dismiss() {
      hide();
      overlay.classList.add('hidden');
      if (returnFocus && returnFocus.isConnected) returnFocus.focus();
    }
    close.addEventListener('click', dismiss);
    done.addEventListener('click', dismiss);
    doc.addEventListener('click', event => {
      const trigger = event.target.closest('[data-permission-help]');
      if (!trigger) return;
      const help = permissionHelp(trigger.getAttribute('data-permission-help'));
      if (!help) return;
      hide();
      returnFocus = trigger;
      overlay.querySelector('h2').textContent = help.title;
      overlay.querySelector('.jupiter-permission-symbol i').className = 'ph ' + help.icon;
      overlay.querySelector('#jupiter-permission-intro').textContent = help.intro;
      overlay.querySelector('.jupiter-permission-note').textContent = help.note;
      const list = overlay.querySelector('ol');
      list.replaceChildren();
      help.steps.forEach(step => {const item = doc.createElement('li'); item.textContent = step; list.appendChild(item);});
      overlay.classList.remove('hidden');
      close.focus();
    });
    doc.addEventListener('keydown', event => {
      if (event.key === 'Escape') hide();
      if (overlay.classList.contains('hidden')) return;
      if (event.key === 'Escape') {event.preventDefault(); event.stopImmediatePropagation(); dismiss();}
      if (event.key === 'Tab') {
        event.preventDefault();
        (doc.activeElement === close ? done : close).focus();
      }
    }, true);
    // Add help beside existing denial notices only; never change capture, permission,
    // error recovery, or Live state. Observe notices, not chat/transcript contents.
    const watchNotice = notice => {
      const attach = () => {
        const text = notice.textContent || '';
        const type = /microphone (?:access is disabled|permission.*denied)/i.test(text) ? 'microphone'
          : /camera permission was denied/i.test(text) ? 'camera' : '';
        if (!type || notice.querySelector('[data-permission-help]')) return;
        const button = doc.createElement('button');
        button.type = 'button'; button.className = 'jupiter-permission-link';
        button.setAttribute('data-permission-help', type);
        button.textContent = 'Permission help';
        notice.appendChild(button);
      };
      new root.MutationObserver(attach).observe(notice, {childList: true});
      attach();
    };
    const cameraNotice = doc.getElementById('live-camera-notice');
    if (cameraNotice) watchNotice(cameraNotice);
    const toast = doc.getElementById('test-role-toast');
    if (toast) watchNotice(toast);
    else {
      const toastArrival = new root.MutationObserver(() => {
        const element = doc.getElementById('test-role-toast');
        if (element) {watchNotice(element); toastArrival.disconnect();}
      });
      toastArrival.observe(doc.body, {childList: true});
    }
  }
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', init, {once: true});
  else init();
})(typeof window !== 'undefined' ? window : null);
