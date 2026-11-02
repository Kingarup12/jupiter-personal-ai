/* Profile-only account records. Uses existing authenticated stores and download/viewer components. */
(function (global) {
  'use strict';
  const labels = {ringing: 'Ringing', accepted: 'Answered', ended: 'Completed',
    declined: 'Declined', missed: 'Missed', suppressed: 'Missed (quiet)', cancelled: 'Cancelled'};
  function date(value) {
    const parsed = new Date(value || '');
    return Number.isNaN(parsed.getTime()) ? 'Time unavailable' : parsed.toLocaleString();
  }
  function create({request, scope, download, viewImage, document: doc = global.document}) {
    let revision = 0;
    const requests = {calls: 0, files: 0};
    const byId = id => doc.getElementById(id);
    function node(tag, text, className = '') {
      const element = doc.createElement(tag);
      element.textContent = text;
      if (className) element.className = className;
      return element;
    }
    function message(id, text) { byId(id)?.replaceChildren(node('p', text, 'memory-review-empty')); }
    function clear() {
      revision++;
      message('call-history-list', 'Open Call Log to load your calls.');
      message('generated-files-list', 'Open Created Files to load your files.');
    }
    async function load(kind) {
      const call = kind === 'calls';
      const target = call ? 'call-history-list' : 'generated-files-list';
      const list = byId(target);
      const startedScope = scope();
      if (!list || !startedScope) return clear();
      const startedRevision = revision;
      const requestId = ++requests[kind];
      const owns = () => revision === startedRevision && scope() === startedScope && requests[kind] === requestId;
      message(target, call ? 'Loading calls…' : 'Loading created files…');
      try {
        const response = await request(call ? '/api/calls/history' : '/api/profile/generated-files');
        if (!response.ok) throw new Error('unavailable');
        const payload = await response.json();
        if (!owns()) return;
        const items = (call ? payload.calls : payload.items) || [];
        if (!Array.isArray(items)) throw new Error('invalid');
        list.replaceChildren();
        if (!items.length) message(target, call ? 'No Jupiter calls yet.' : 'No saved created files yet.');
        for (const item of items.slice(0, 100)) {
          const row = node('article', '', 'profile-record-row');
          const info = node('div', '', 'profile-record-copy');
          if (call) {
            info.append(node('strong', labels[item.status] || 'Call'),
              node('span', String(item.reason || 'Jupiter call')),
              node('small', date(item.created_at)));
            const seconds = Number(item.duration_seconds);
            if (Number.isFinite(seconds) && seconds > 0)
              info.append(node('small', Math.round(seconds) + ' seconds'));
          } else {
            if (!/^[a-f0-9]{32}$/.test(String(item.id))) continue;
            info.append(node('strong', String(item.name || 'Created file')),
              node('small', String(item.mime_type || 'File') + ' · ' + date(item.created_at)));
          }
          row.append(info);
          if (!call) {
            const image = String(item.mime_type || '').startsWith('image/');
            const action = node('button', image ? 'View / Download' : 'Download', 'secondary-btn');
            action.type = 'button';
            action.setAttribute('aria-label', (image ? 'View or download ' : 'Download ') + String(item.name || 'file'));
            const status = node('small', '', 'profile-record-status');
            status.setAttribute('role', 'status');
            action.addEventListener('click', async () => {
              if (!owns() || action.disabled) return;
              action.disabled = true;
              status.textContent = image ? 'Opening…' : 'Preparing download…';
              try {
                const result = await (image ? viewImage(item, owns) : download(item, owns));
                if (owns()) status.textContent = result === 'cancelled' ? 'Cancelled.' :
                  result === 'saved' ? 'Saved.' : image ? '' : 'Download started.';
              } catch (_) {
                if (owns()) status.textContent = 'This file is unavailable. Please try again.';
              } finally { if (owns()) action.disabled = false; }
            });
            const actions = node('div', '', 'profile-record-actions');
            actions.append(action, status);
            row.append(actions);
          }
          list.append(row);
        }
        if (!call && payload.has_more) list.append(node('p', 'Showing the latest 100 files.', 'tab-desc'));
        if (call && owns()) {
          // Reading the log acknowledges unread missed calls; the history itself is retained.
          const seen = await request('/api/calls/missed/clear', {method: 'POST'}).catch(() => null);
          if (seen?.ok && owns()) {
            const badge = byId('call-missed-badge');
            if (badge) { badge.hidden = true; badge.textContent = '0'; }
            byId('call-log-tab')?.setAttribute('aria-label', 'Call Log');
          }
        }
      } catch (_) { if (owns()) message(target, call ? 'Call Log is temporarily unavailable.' : 'Created Files are temporarily unavailable.'); }
    }
    byId('refresh-call-history')?.addEventListener('click', () => load('calls'));
    byId('refresh-generated-files')?.addEventListener('click', () => load('files'));
    return {load, clear};
  }
  global.JupiterProfileRecords = {create};
})(typeof window !== 'undefined' ? window : globalThis);
