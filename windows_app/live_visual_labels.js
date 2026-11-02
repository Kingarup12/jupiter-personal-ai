/* Camera Live ON-DEMAND visual grounding overlay.
 *
 * There is no Visual Labels mode, no toggle, no preference and no polling. The backend grounds ONE canonical frame
 * only when the user explicitly asks Jupiter to mark / box / label / highlight / point out / locate something and sends
 * one `visual_grounding` event for that Live turn. This module draws that single temporary box on the preview <video>
 * (object-fit: cover), mirroring X for the selfie-style front preview, VERIFIES that it is really on screen, and reports
 * that back (render handshake) so Jupiter never claims a box the user cannot see. The box is removed after ~6 s, when it
 * is replaced, when the camera turns off/switches, or when Live ends. It never makes a network request itself.
 *
 * Grounding state (one owner, this module): idle -> requested -> rendering -> displayed | failed -> idle.
 */
((root, factory) => {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.JupiterVisualLabels = api;
})(typeof window !== 'undefined' ? window : globalThis, () => {
  const BOX_TTL_MS = 6000;           // temporary overlay lifetime (5-8 s product range)

  // Canonical normalized box -> CSS pixels on the preview element (object-fit: cover, optional selfie mirror).
  function mapBoxToPreview(box, { videoWidth, videoHeight, viewWidth, viewHeight, mirrored = false }) {
    if (!box || !videoWidth || !videoHeight || !viewWidth || !viewHeight) return null;
    const scale = Math.max(viewWidth / videoWidth, viewHeight / videoHeight);
    const shownWidth = videoWidth * scale;
    const shownHeight = videoHeight * scale;
    const offsetX = (viewWidth - shownWidth) / 2;
    const offsetY = (viewHeight - shownHeight) / 2;
    let x = offsetX + box.x * shownWidth;
    const y = offsetY + box.y * shownHeight;
    const w = box.w * shownWidth;
    const h = box.h * shownHeight;
    if (mirrored) x = viewWidth - (x + w);               // raw x1..x2 -> width-x2 .. width-x1
    return { x, y, w, h };
  }

  // Which side of the USER'S screen a canonical box is on (selfie preview: raw right <-> displayed left).
  function displaySide(box, mirrored = false) {
    if (!box) return '';
    const cx = box.x + box.w / 2;
    const x = mirrored ? 1 - cx : cx;
    return x < 0.4 ? 'left' : x > 0.6 ? 'right' : 'center';
  }

  function validBox(box) {
    return Boolean(box && [box.x, box.y, box.w, box.h].every((v) => Number.isFinite(v) && v >= 0 && v <= 1)
      && box.w > 0 && box.h > 0 && box.x + box.w <= 1.0001 && box.y + box.h <= 1.0001);
  }

  function createGroundingOverlay({ overlay, getGeometry, now = () => Date.now(), ttlMs = BOX_TTL_MS,
    setTimer = (fn, ms) => setTimeout(fn, ms), clearTimer = (id) => clearTimeout(id), onTelemetry = () => {},
    isVisible = (element) => {
      const rect = element?.getBoundingClientRect?.();
      return rect ? rect.width > 0 && rect.height > 0 : true;
    } } = {}) {
    let current = null;                                   // { label, box, kind, until, turn, requestId }
    let element = null;
    let timer = null;
    let state = 'idle';

    const remove = (next = 'idle') => {
      if (timer !== null) { clearTimer(timer); timer = null; }
      current = null;
      if (element) { element.remove(); element = null; }
      if (overlay) overlay.hidden = true;
      state = next;
    };

    // Places the box; true only when it is really laid out on the visible preview.
    const render = () => {
      if (!overlay || !current) return false;
      if (now() >= current.until) { remove(); return false; }
      const geometry = getGeometry?.();
      const rect = geometry ? mapBoxToPreview(current.box, geometry) : null;
      if (!rect || rect.w < 1 || rect.h < 1 || rect.x + rect.w <= 0 || rect.y + rect.h <= 0
        || rect.x >= geometry.viewWidth || rect.y >= geometry.viewHeight) {
        if (element) element.hidden = true;
        return false;
      }
      if (!element) {
        element = overlay.ownerDocument.createElement('div');
        element.className = 'vl-box';
        const chip = overlay.ownerDocument.createElement('span');
        chip.className = 'vl-chip';
        element.appendChild(chip);
        overlay.appendChild(element);
      }
      element.hidden = false;
      element.dataset.kind = current.kind === 'person' ? 'person' : 'object';
      element.dataset.locate = 'true';
      element.style.transform = `translate(${Math.round(rect.x)}px, ${Math.round(rect.y)}px)`;
      element.style.width = `${Math.max(8, Math.round(rect.w))}px`;
      element.style.height = `${Math.max(8, Math.round(rect.h))}px`;
      element.firstChild.textContent = current.label || '';
      element.firstChild.hidden = !current.label;
      overlay.hidden = false;
      return isVisible(element);
    };

    return {
      requested() { if (state !== 'displayed') state = 'requested'; },
      // One server result for one explicit request -> { rendered, reason }. A failure never draws anything.
      show(event = {}) {
        onTelemetry({ visualGroundingAction: String(event.action || ''), visualGroundingProvider: String(event.provider || ''),
          visualGroundingSuccess: Boolean(event.success && event.box), visualGroundingFailure: event.success ? '' : String(event.reason || ''),
          visualGroundingLatencyMs: Number(event.latency_ms || 0) });
        if (event.action === 'clear') { remove(); return { rendered: false, reason: 'cleared' }; }
        if (!event.success) { state = 'failed'; return { rendered: false, reason: String(event.reason || 'not_found') }; }
        if (!validBox(event.box)) { state = 'failed'; return { rendered: false, reason: 'invalid_box' }; }   // no fake boxes
        remove('rendering');                               // a new mark replaces the previous one
        current = { label: String(event.label || '').slice(0, 40), box: { ...event.box }, kind: event.kind || 'object',
          until: now() + ttlMs, turn: event.logical_turn_id || '', requestId: String(event.request_id || '') };
        const rendered = render();
        if (!rendered) {
          remove('failed');
          onTelemetry({ overlayRendered: 0, overlayErrorReason: 'not_visible' });
          return { rendered: false, reason: 'not_visible' };
        }
        state = 'displayed';
        timer = setTimer(() => { timer = null; remove(); }, ttlMs);
        onTelemetry({ overlayRendered: 1 });
        return { rendered: true, reason: '' };
      },
      clear: () => remove(),
      render,
      get state() { return state; },
      snapshot: () => ({ state, visible: Boolean(current && element && !element.hidden), label: current?.label || '',
        box: current ? { ...current.box } : null, until: current?.until || 0 })
    };
  }

  return { mapBoxToPreview, displaySide, createGroundingOverlay, validBox, BOX_TTL_MS };
});
