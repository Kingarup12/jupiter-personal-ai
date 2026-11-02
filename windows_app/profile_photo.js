(function (global) {
  'use strict';

  const MAX_BYTES = 5 * 1024 * 1024;
  const MAX_OUTPUT = 1024;
  const MAX_ZOOM = 4;
  const TYPES = Object.freeze(['image/jpeg', 'image/png', 'image/webp']);
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
  const sameAccount = (a, b) => Boolean(a?.email && a?.token && b
    && a.email === b.email && a.token === b.token && a.generation === b.generation);
  const validateFile = (file) => {
    if (!file || !TYPES.includes(file.type)) throw new Error('Choose a JPEG, PNG, or WebP image.');
    if (!file.size || file.size > MAX_BYTES) throw new Error('Choose an image up to 5 MB.');
  };
  const validateImageFile = async (file) => {
    validateFile(file);
    const bytes = new Uint8Array(await file.slice(0, 12).arrayBuffer());
    const text = (start, end) => String.fromCharCode(...bytes.slice(start, end));
    const actual = bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff ? 'image/jpeg'
      : bytes[0] === 0x89 && text(1, 4) === 'PNG' && bytes[4] === 13 && bytes[5] === 10
        && bytes[6] === 26 && bytes[7] === 10 ? 'image/png'
        : text(0, 4) === 'RIFF' && text(8, 12) === 'WEBP' ? 'image/webp' : '';
    if (actual !== file.type) throw new Error('Choose a valid JPEG, PNG, or WebP image.');
    // The decoder still must read the whole image; a valid signature alone is insufficient.
  };
  const crop = (width, height, zoom = 1, centerX = width / 2, centerY = height / 2) => {
    if (![width, height, zoom, centerX, centerY].every(Number.isFinite)
        || width < 1 || height < 1 || width > 8000 || height > 8000) {
      throw new Error('Choose a valid image no larger than 8000 pixels on either side.');
    }
    zoom = clamp(zoom, 1, Math.min(MAX_ZOOM, width, height));
    const side = Math.min(width, height) / zoom;
    centerX = clamp(centerX, side / 2, width - side / 2);
    centerY = clamp(centerY, side / 2, height - side / 2);
    return { width, height, zoom, centerX, centerY, x: centerX - side / 2,
      y: centerY - side / 2, side, outputSize: Math.min(MAX_OUTPUT, Math.floor(side)) };
  };
  const pan = (state, dx, dy, viewportSize) => crop(state.width, state.height, state.zoom,
    state.centerX - dx * state.side / viewportSize,
    state.centerY - dy * state.side / viewportSize);

  // One operation owns the selected file, account/session and upload. Cancelling
  // invalidates all late decode/render/upload completions, including A -> B -> A.
  const createUploadSession = ({ getIdentity, upload, onSaved, timeoutMs = 30000 }) => {
    let current = null;
    const isCurrent = (operation) => Boolean(operation && current === operation
      && sameAccount(operation.identity, getIdentity()));
    const cancel = () => { current?.controller?.abort(); current = null; };
    return {
      get current() { return current; },
      isCurrent,
      cancel,
      begin() {
        cancel();
        const identity = getIdentity();
        if (!identity?.email || !identity?.token) throw new Error('Sign in again to change your profile photo.');
        current = { identity: { ...identity }, busy: false, controller: null };
        return current;
      },
      async save(render) {
        const operation = current;
        if (!isCurrent(operation) || operation.busy) return false;
        operation.busy = true;
        const controller = new AbortController();
        operation.controller = controller;
        let timer;
        const expired = new Promise((_, reject) => {
          timer = setTimeout(() => {
            controller.abort();
            reject(new Error('Saving took too long. Please try again.'));
          }, timeoutMs);
        });
        try {
          const work = async () => {
            const blob = await render();
            if (!isCurrent(operation) || controller.signal.aborted) return false;
            validateFile(blob);
            const result = await upload(blob, operation.identity, controller.signal);
            if (!isCurrent(operation) || controller.signal.aborted) return false;
            onSaved(blob, result, operation.identity);
            current = null;
            return true;
          };
          return await Promise.race([work(), expired]);
        } catch (error) {
          if (isCurrent(operation)) throw error;
          return false;
        } finally {
          clearTimeout(timer);
          operation.busy = false;
        }
      }
    };
  };

  const draw = (canvas, source, state, size) => {
    canvas.width = size;
    canvas.height = size;
    const context = canvas.getContext('2d');
    if (!context) throw new Error('Jupiter could not prepare this crop.');
    context.imageSmoothingEnabled = true;
    context.imageSmoothingQuality = 'high';
    context.drawImage(source, state.x, state.y, state.side, state.side, 0, 0, size, size);
  };
  const encodeCrop = (source, state, createCanvas = () => document.createElement('canvas')) => {
    // Lossless, alpha-preserving output. The server validates and stores these
    // exact bytes; the low-resolution circular preview is NEVER an input here.
    return new Promise((resolve, reject) => {
      const failed = () => reject(new Error('Jupiter could not prepare this photo. Choose another image.'));
      try {
        const canvas = createCanvas();
        draw(canvas, source, state, state.outputSize);
        canvas.toBlob(blob => {
          if (blob && blob.size <= MAX_BYTES) resolve(blob);
          else failed();
        }, 'image/png');
      } catch (_) { failed(); }
    });
  };
  const decode = async (file) => {
    await validateImageFile(file);
    if (typeof createImageBitmap === 'function') {
      try { return await createImageBitmap(file, { imageOrientation: 'from-image' }); }
      catch (_) { /* Browser-decoded Image fallback also honors EXIF orientation. */ }
    }
    const url = URL.createObjectURL(file);
    try {
      const image = new Image();
      image.src = url;
      await image.decode();
      return image;
    } finally { URL.revokeObjectURL(url); }
  };

  const mount = ({ dialog, input, trigger, getIdentity, upload, onSaved, onAccountChange, onError }) => {
    const find = (name) => dialog.querySelector(`[data-photo-${name}]`);
    const viewport = find('viewport');
    const preview = find('preview');
    const zoom = find('zoom');
    const save = find('save');
    const status = find('status');
    const quality = find('quality');
    let source = null;
    let state = null;
    let previousFocus = null;
    let frame = 0;
    let pointer = null;
    let account = getIdentity();
    let accountWatch = 0;
    const session = createUploadSession({ getIdentity, upload, onSaved });
    const releaseSource = () => { source?.close?.(); source = null; state = null; };
    const close = () => {
      session.cancel();
      releaseSource();
      viewport.getContext('2d')?.clearRect(0, 0, viewport.width, viewport.height);
      preview.getContext('2d')?.clearRect(0, 0, preview.width, preview.height);
      cancelAnimationFrame(frame);
      frame = 0;
      pointer = null;
      clearInterval(accountWatch);
      dialog.close();
      if (previousFocus?.isConnected) previousFocus.focus();
      input.value = '';
    };
    const syncAccount = () => {
      const next = getIdentity();
      if (sameAccount(account, next) || (!account && !next)) return;
      account = next;
      close();
      onAccountChange();
    };
    const render = () => {
      frame = 0;
      if (!source || !state || !dialog.open) return;
      const size = Math.max(1, Math.round(viewport.getBoundingClientRect().width * Math.min(devicePixelRatio || 1, 3)));
      try {
        draw(viewport, source, state, size);
        draw(preview, source, state, 240);
      } catch (_) {
        close(); onError('Jupiter could not display this photo. Choose another image.'); return;
      }
      zoom.value = state.zoom;
      zoom.max = Math.min(MAX_ZOOM, state.width, state.height);
      zoom.setAttribute('aria-valuetext', `${state.zoom.toFixed(2)} times zoom`);
      quality.textContent = `${state.outputSize} × ${state.outputSize} pixels`
        + (state.outputSize < 512 ? ' · This crop has limited detail. Try less zoom or a larger original.' : ' · High-quality square photo');
    };
    const requestRender = () => { if (!frame) frame = requestAnimationFrame(render); };
    const setBusy = (busy) => {
      save.disabled = busy || !source;
      zoom.disabled = busy || !source;
      find('reset').disabled = busy || !source;
      save.textContent = busy ? 'Saving…' : 'Save photo';
      dialog.setAttribute('aria-busy', String(busy));
    };
    const beginPicker = () => {
      syncAccount();
      try { session.begin(); input.click(); }
      catch (error) { onError(error.message); }
    };
    trigger.addEventListener('click', beginPicker);
    input.addEventListener('cancel', () => session.cancel());
    input.addEventListener('change', async () => {
      const file = input.files?.[0];
      input.value = '';
      const operation = session.current;
      if (!file || !session.isCurrent(operation)) { session.cancel(); return; }
      try { validateFile(file); }
      catch (error) { session.cancel(); onError(error.message); return; }
      previousFocus = trigger;
      status.textContent = 'Opening photo…';
      quality.textContent = '';
      setBusy(false);
      dialog.showModal();
      find('cancel').focus();
      accountWatch = setInterval(syncAccount, 250);
      let decoded = null;
      const timeout = setTimeout(() => {
        if (!source && session.isCurrent(operation)) {
          close(); onError('This image could not be opened. Choose another photo.');
        }
      }, 15000);
      try {
        decoded = await decode(file);
        if (!session.isCurrent(operation)) { decoded.close?.(); return; }
        state = crop(decoded.naturalWidth || decoded.width, decoded.naturalHeight || decoded.height);
        source = decoded;
        status.textContent = 'Drag the photo or use the arrow keys to position it.';
        setBusy(false);
        requestRender();
      } catch (_) {
        decoded?.close?.();
        if (session.isCurrent(operation)) {
          close(); onError('This image is invalid or could not be decoded. Choose another photo.');
        }
      } finally { clearTimeout(timeout); }
    });
    const reposition = (dx, dy) => {
      if (!state || session.current?.busy) return;
      state = pan(state, dx, dy, Math.max(1, viewport.getBoundingClientRect().width));
      requestRender();
    };
    viewport.addEventListener('pointerdown', event => {
      if (!state || session.current?.busy || !event.isPrimary || event.button !== 0) return;
      pointer = { id: event.pointerId, x: event.clientX, y: event.clientY };
      viewport.setPointerCapture(event.pointerId);
      viewport.focus();
      event.preventDefault();
    });
    viewport.addEventListener('pointermove', event => {
      if (!pointer || pointer.id !== event.pointerId) return;
      reposition(event.clientX - pointer.x, event.clientY - pointer.y);
      pointer.x = event.clientX; pointer.y = event.clientY;
    });
    const endPointer = () => { pointer = null; };
    viewport.addEventListener('pointerup', endPointer);
    viewport.addEventListener('pointercancel', endPointer);
    viewport.addEventListener('lostpointercapture', endPointer);
    viewport.addEventListener('keydown', event => {
      const movement = { ArrowLeft: [-10, 0], ArrowRight: [10, 0], ArrowUp: [0, -10], ArrowDown: [0, 10] }[event.key];
      if (movement) { event.preventDefault(); reposition(...movement); }
    });
    zoom.addEventListener('input', () => {
      if (!state || session.current?.busy) return;
      state = crop(state.width, state.height, Number(zoom.value), state.centerX, state.centerY);
      requestRender();
    });
    find('reset').addEventListener('click', () => {
      if (state && !session.current?.busy) { state = crop(state.width, state.height); requestRender(); }
    });
    find('cancel').addEventListener('click', close);
    find('close').addEventListener('click', close);
    dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
    dialog.addEventListener('keydown', event => {
      event.stopPropagation();
      if (event.key !== 'Tab') return;
      const controls = [...dialog.querySelectorAll('button:not(:disabled), input:not(:disabled), [tabindex="0"]')];
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
    save.addEventListener('click', async () => {
      if (!source || !session.isCurrent(session.current) || session.current.busy) return;
      const operation = session.current;
      setBusy(true);
      status.textContent = 'Saving your profile photo…';
      try {
        if (await session.save(() => encodeCrop(source, state))) close();
      } catch (error) {
        if (session.isCurrent(operation)) status.textContent = error.message || 'Jupiter could not save this photo. Please retry.';
      } finally {
        if (dialog.open && session.isCurrent(operation)) setBusy(false);
      }
    });
    new ResizeObserver(requestRender).observe(viewport);
    window.addEventListener('storage', syncAccount);
    return { syncAccount, close };
  };

  const api = { MAX_BYTES, MAX_OUTPUT, MAX_ZOOM, TYPES, sameAccount, validateFile, validateImageFile, crop, pan, draw, encodeCrop, createUploadSession, mount };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else global.JupiterProfilePhoto = Object.freeze(api);
})(typeof window !== 'undefined' ? window : globalThis);
