(function (global) {
  'use strict';
  // Jupiter Live media: edge aura, original procedural cues, camera (visual input) and the Jupiter-only camera
  // transcript overlay. Everything here is isolated from microphone capture and turn ownership: the camera
  // requests VIDEO ONLY, frames travel as a separate stream, and no function here touches Live speech state.

  // ── Edge aura ────────────────────────────────────────────────────────────────────────────────────────────
  const AURA_STATES = new Set(['idle', 'listening', 'thinking', 'speaking']);
  function auraStateFor(liveState) {
    const s = String(liveState || '').toLowerCase();
    if (['assistant_speaking', 'speaking', 'preparing_speech'].includes(s)) return 'speaking';
    if (['transcribing', 'processing', 'thinking', 'generating_text'].includes(s)) return 'thinking';
    if (['listening', 'user_speaking', 'endpoint_confirmation'].includes(s)) return 'listening';
    if (s === 'waiting_for_user') return 'waiting';
    return 'idle';
  }
  // Live ambience ownership (authoritative):
  //   speech Live  = orb halo + living bottom dot field          (NO screen-edge aura)
  //   camera Live  = camera + screen-edge aura + bottom dot field (edge layers exist ONLY while camera is on)
  // The edge layers are created by setCamera(true) and removed by setCamera(false)/detach(), so no CSS rule can
  // leave a perimeter glow on a speech-only session.
  const DOT_FIELD_MARKUP = '<span class="live-dot-base"><i class="live-spectrum"></i></span>'
    + '<span class="live-dot-wave"><b class="live-wave live-wave-a"></b><b class="live-wave live-wave-b"></b>'
    + '<b class="live-wave live-wave-c"></b><b class="live-wave live-wave-voice"></b>'
    + '<b class="live-packet live-packet-a"></b><b class="live-packet live-packet-b"></b><b class="live-shimmer"></b></span>';
  const EDGE_MARKUP = '<span class="live-aura-edge"></span><span class="live-aura-sheen"></span>';
  function createAura({ doc = global.document, raf = global.requestAnimationFrame?.bind(global),
    caf = global.cancelAnimationFrame?.bind(global) } = {}) {
    let el = null, edge = null, frame = null, levelSource = null, smoothed = 0, state = 'idle', camera = false;
    const tick = () => {
      frame = null;
      if (!el || state !== 'speaking' || !levelSource) return;
      const level = Math.max(0, Math.min(1, Number(levelSource()) * 6 || 0));
      smoothed += (level - smoothed) * (level > smoothed ? 0.22 : 0.08);   // quick rise, graceful decay
      el.style.setProperty('--aura-level', smoothed.toFixed(3));
      frame = raf ? raf(tick) : null;
    };
    const removeEdge = () => { edge?.remove?.(); edge = null; };
    return {
      attach(host) {
        if (el || !host || !doc) return el;
        el = doc.createElement('div');
        el.className = 'live-edge-aura';
        el.setAttribute('aria-hidden', 'true');
        el.dataset.state = state;
        el.dataset.edge = 'off';
        // Orb halo (behind the orb) + the living dotted-light field under the control dock.
        el.innerHTML = `<span class="live-aura-top"></span><span class="live-aura-dots">${DOT_FIELD_MARKUP}</span>`;
        host.prepend(el);
        camera = false;
        return el;
      },
      setState(next) {
        state = AURA_STATES.has(next) ? next : 'idle';
        if (!el) return state;
        el.dataset.state = state;
        if (state === 'speaking' && !frame && raf) frame = raf(tick);
        if (state !== 'speaking') { smoothed = 0; el.style.setProperty('--aura-level', '0'); }
        return state;
      },
      setLevelSource(fn) { levelSource = typeof fn === 'function' ? fn : null; },
      // Camera ON creates the screen-edge aura; camera OFF / failure removes it immediately. Live audio is untouched.
      setCamera(on) {
        camera = Boolean(on);
        if (!el) return camera;
        el.classList.toggle('with-camera', camera);
        el.dataset.edge = camera ? 'on' : 'off';
        if (camera && !edge && typeof doc?.createElement === 'function') {
          edge = doc.createElement('span');
          edge.className = 'live-aura-edge-layer';
          edge.innerHTML = EDGE_MARKUP;
          if (typeof el.append === 'function') el.append(edge);
        } else if (!camera) {
          removeEdge();
        }
        return camera;
      },
      detach() {
        if (frame && caf) caf(frame);
        frame = null; levelSource = null;
        removeEdge();
        el?.remove?.(); el = null; state = 'idle'; camera = false;
      },
      get element() { return el; },
      get state() { return state; },
      get edgeActive() { return Boolean(el && camera && edge); }
    };
  }

  // ── Original procedural Live cues (no samples, no copied sounds) ──────────────────────────────────────────
  // Each cue is a short, soft two-partial "glass" chime built from sine/triangle oscillators with a slow attack,
  // quiet peak and exponential decay. Intervals are Jupiter's own: rising fifths to enter, falling to leave.
  const CUES = Object.freeze({
    enter: [[392.0, 0], [587.3, 0.09], [784.0, 0.18]],
    end: [[659.3, 0], [493.9, 0.1], [329.6, 0.2]],
    camera_on: [[523.3, 0], [880.0, 0.07]],
    camera_off: [[880.0, 0], [523.3, 0.07]],
    mute: [[440.0, 0], [349.2, 0.06]],
    unmute: [[349.2, 0], [440.0, 0.06]]
  });
  function createSounds({ AudioContextCtor = global.AudioContext || global.webkitAudioContext,
    isSpeaking = () => false, baseGain = 0.035 } = {}) {
    let ctx = null;
    const played = new Set();
    const ensure = () => {
      if (!AudioContextCtor) return null;
      if (!ctx || ctx.state === 'closed') ctx = new AudioContextCtor({ latencyHint: 'interactive' });
      if (ctx.state === 'suspended') ctx.resume?.().catch?.(() => {});
      return ctx;
    };
    return {
      // token = unique event identity (e.g. session id + cue); re-renders/replays with the same token are ignored.
      play(cue, token = '') {
        const notes = CUES[cue];
        if (!notes) return false;
        const key = `${cue}:${token}`;
        if (token && played.has(key)) return false;
        if (token) { played.add(key); if (played.size > 64) played.delete(played.values().next().value); }
        const audio = ensure();
        if (!audio) return false;
        const gainTarget = baseGain * (isSpeaking() ? 0.35 : 1);  // duck under Jupiter's own speech
        const start = audio.currentTime + 0.01;
        for (const [freq, offset] of notes) {
          for (const [type, mult, level] of [['sine', 1, 1], ['triangle', 2, 0.18]]) {
            const osc = audio.createOscillator(), gain = audio.createGain();
            osc.type = type; osc.frequency.value = freq * mult;
            const t0 = start + offset;
            gain.gain.setValueAtTime(0.0001, t0);
            gain.gain.linearRampToValueAtTime(gainTarget * level, t0 + 0.025);
            gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.32);
            osc.connect(gain); gain.connect(audio.destination);
            osc.start(t0); osc.stop(t0 + 0.34);
          }
        }
        return true;
      },
      async close() {
        const audio = ctx; ctx = null; played.clear();
        if (audio && audio.state !== 'closed') { try { await audio.close(); } catch (_) {} }
      },
      get context() { return ctx; }
    };
  }

  // ── Camera controller ────────────────────────────────────────────────────────────────────────────────────
  const FRAME_INTERVAL_MS = 1000;        // official Live API: video frames at most 1 per second
  const KEYFRAME_MS = 4000;              // resend an unchanged scene at most every 4 s
  const MAX_EDGE = 768;                  // longest side sent to the model (local preview stays full quality)
  const JPEG_QUALITY = 0.6;
  function cameraErrorKind(error) {
    const name = String(error?.name || '');
    if (name === 'NotAllowedError' || name === 'PermissionDeniedError') return 'permission_denied';
    if (name === 'NotFoundError' || name === 'DevicesNotFoundError' || name === 'OverconstrainedError') return 'no_camera';
    if (name === 'NotReadableError' || name === 'TrackStartError' || name === 'AbortError') return 'camera_busy';
    if (name === 'SecurityError' || name === 'TypeError' || name === 'NotSupportedError') return 'unsupported';
    return 'camera_error';
  }
  const CAMERA_MESSAGES = Object.freeze({
    permission_denied: 'Camera permission was denied. Live voice continues without video.',
    no_camera: 'No camera was found. Live voice continues without video.',
    camera_busy: 'The camera is in use by another app. Live voice continues without video.',
    unsupported: 'This app or browser does not allow camera access here. Live voice continues without video.',
    disconnected: 'The camera disconnected. Live voice continues without video.',
    switch_failed: 'Could not switch cameras. The previous camera stays off; Live voice continues.',
    camera_error: 'The camera could not start. Live voice continues without video.',
    no_frames: 'Camera unavailable. Open your camera shutter or check permission.'
  });
  // Camera intent ("wanted") is separate from camera HEALTH. Only permission/unsupported failures are terminal;
  // a closed privacy shutter, a muted/ended track, a busy or briefly missing device is recoverable while wanted.
  const CAMERA_HEALTH = Object.freeze({ STARTING: 'starting', ACTIVE: 'active', NO_FRAMES: 'no_frames',
    RECOVERING: 'recovering', FAILED_RETRYABLE: 'failed_retryable', FAILED_TERMINAL: 'failed_terminal', OFF: 'off' });
  const TERMINAL_CAMERA_KINDS = new Set(['permission_denied', 'unsupported']);
  const RECOVERY_BACKOFF_MS = Object.freeze([2000, 4000, 8000, 15000, 30000]);   // then stays at 30 s while wanted
  const NO_FRAME_GRACE_MS = 3000;          // no NEW frame for 3 s ⇒ unavailable (never judged from black pixels)
  const REBIND_AFTER_MS = 6000;            // then re-attach the same stream to the preview
  const REACQUIRE_AFTER_MS = 12000;        // then re-open getUserMedia
  function createCamera({ mediaDevices = global.navigator?.mediaDevices, video = null, onFrame = () => {},
    onState = () => {}, createCanvas = () => global.document?.createElement('canvas'),
    setIntervalFn = global.setInterval?.bind(global), clearIntervalFn = global.clearInterval?.bind(global),
    setTimeoutFn = global.setTimeout?.bind(global), clearTimeoutFn = global.clearTimeout?.bind(global),
    now = () => Date.now(), isHidden = () => global.document?.visibilityState === 'hidden',
    isMobile = () => /Android|iPhone|iPad|Mobile/i.test(global.navigator?.userAgent || '') } = {}) {
    let stream = null, timer = null, facing = 'user', deviceIds = [], deviceIndex = 0;
    let lastSignature = null, lastSentAt = 0, torchOn = false, generation = 0, active = false;   // active = WANTED
    let health = CAMERA_HEALTH.OFF, lastFrameAt = 0, unhealthySince = 0, rebound = false, recoveryTimer = null;
    let recoveryAttempt = 0, lastQualityFrames = -1, lastMediaTime = -1, frameCallbackId = null, lastKind = '';
    let deviceListener = null, videoListener = null;
    const canvas = createCanvas(), small = createCanvas();
    const track = () => stream?.getVideoTracks?.()[0] || null;
    const capabilities = () => {
      const caps = track()?.getCapabilities?.() || {};
      let trackFacing = '';
      try { trackFacing = String(track()?.getSettings?.().facingMode || ''); } catch (_) { trackFacing = ''; }
      return { canFlip: isMobile() ? true : deviceIds.length > 1, torchSupported: Boolean(caps.torch), torchOn,
               facing, trackFacing, mobile: isMobile(), active, health, wanted: active };
    };
    const emit = (status, extra = {}) => onState({ status, ...extra, ...capabilities() });
    // Frame-progress evidence: requestVideoFrameCallback, else playback-quality / currentTime. With neither
    // (legacy engines, tests) a non-zero video size is accepted as the only available signal.
    const progressObservable = () => Boolean(video && (typeof video.requestVideoFrameCallback === 'function'
      || typeof video.getVideoPlaybackQuality === 'function' || typeof video.currentTime === 'number'));
    const noteFrame = () => { lastFrameAt = now(); };
    const watchFrames = (gen) => {
      if (!video || typeof video.requestVideoFrameCallback !== 'function') return;
      const next = () => {
        frameCallbackId = video.requestVideoFrameCallback(() => {
          if (gen !== generation || !active) return;
          noteFrame();
          next();
        });
      };
      next();
    };
    const pollFallbackProgress = () => {
      if (!video || typeof video.requestVideoFrameCallback === 'function') return;
      const quality = video.getVideoPlaybackQuality?.();
      const frames = Number(quality?.totalVideoFrames);
      if (Number.isFinite(frames) && frames > lastQualityFrames) { lastQualityFrames = frames; noteFrame(); return; }
      const mediaTime = Number(video.currentTime);
      if (Number.isFinite(mediaTime) && mediaTime > lastMediaTime) { lastMediaTime = mediaTime; noteFrame(); }
    };
    const clearRecovery = () => { if (recoveryTimer && clearTimeoutFn) clearTimeoutFn(recoveryTimer); recoveryTimer = null; };
    const detachListeners = () => {
      if (deviceListener) { try { mediaDevices?.removeEventListener?.('devicechange', deviceListener); } catch (_) {} }
      deviceListener = null;
      if (videoListener && video?.removeEventListener) {
        for (const type of ['playing', 'loadeddata', 'canplay']) { try { video.removeEventListener(type, videoListener); } catch (_) {} }
      }
      videoListener = null;
      if (frameCallbackId != null && video?.cancelVideoFrameCallback) { try { video.cancelVideoFrameCallback(frameCallbackId); } catch (_) {} }
      frameCallbackId = null;
    };
    const stopTracks = () => {
      if (timer && clearIntervalFn) clearIntervalFn(timer);
      timer = null;
      stream?.getTracks?.().forEach((t) => { try { t.stop(); } catch (_) {} });
      stream = null; torchOn = false; lastSignature = null;
      if (frameCallbackId != null && video?.cancelVideoFrameCallback) { try { video.cancelVideoFrameCallback(frameCallbackId); } catch (_) {} }
      frameCallbackId = null;
      if (video) { try { video.srcObject = null; } catch (_) {} }
    };
    const setHealthy = (reason) => {
      const recovered = health !== CAMERA_HEALTH.ACTIVE;
      health = CAMERA_HEALTH.ACTIVE; unhealthySince = 0; rebound = false; recoveryAttempt = 0; lastKind = '';
      clearRecovery();
      if (recovered) emit('on', reason ? { recovered: true, reason } : {});
    };
    const setUnavailable = (kind, nextHealth = CAMERA_HEALTH.NO_FRAMES) => {
      const changed = health !== nextHealth || lastKind !== kind;
      health = nextHealth; lastKind = kind;
      if (!unhealthySince) unhealthySince = now();
      lastSignature = null;                                           // never resume with a stale dedupe baseline
      if (changed) emit('unavailable', { kind, retryable: true, message: CAMERA_MESSAGES[kind] || CAMERA_MESSAGES.no_frames });
    };
    const signatureOf = (source, w, h) => {
      if (!small?.getContext) return null;
      small.width = 16; small.height = 16;
      const g = small.getContext('2d', { willReadFrequently: true });
      g.drawImage(source, 0, 0, w, h, 0, 0, 16, 16);
      const px = g.getImageData(0, 0, 16, 16).data, sig = new Uint8Array(256);
      for (let i = 0; i < 256; i++) sig[i] = (px[i * 4] * 0.299 + px[i * 4 + 1] * 0.587 + px[i * 4 + 2] * 0.114) | 0;
      return sig;
    };
    const changed = (a, b) => {
      if (!a || !b) return true;
      let diff = 0;
      for (let i = 0; i < a.length; i++) diff += Math.abs(a[i] - b[i]);
      return diff / a.length > 3;                                   // mean luma change > ~1%
    };
    // Visual frames go to Gemini Live ONLY while the pipeline is healthy (never placeholder/unavailable frames).
    const sample = () => {
      if (!active || health !== CAMERA_HEALTH.ACTIVE || !video || isHidden() || !canvas?.getContext) return;
      const w = video.videoWidth || 0, h = video.videoHeight || 0;
      if (!w || !h) return;
      const scale = Math.min(1, MAX_EDGE / Math.max(w, h));
      const sig = signatureOf(video, w, h);
      const t = now();
      if (!changed(sig, lastSignature) && t - lastSentAt < KEYFRAME_MS) return;   // duplicate suppression
      canvas.width = Math.round(w * scale); canvas.height = Math.round(h * scale);
      canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
      const url = canvas.toDataURL('image/jpeg', JPEG_QUALITY);
      const data = url.slice(url.indexOf(',') + 1);
      if (!data) return;
      lastSignature = sig; lastSentAt = t;
      onFrame({ mime_type: 'image/jpeg', data, width: canvas.width, height: canvas.height });
    };
    const rebind = () => {
      if (!video || !stream) return;
      rebound = true;
      try { video.srcObject = null; video.srcObject = stream; } catch (_) {}
      try { const played = video.play?.(); played?.catch?.(() => {}); } catch (_) {}
    };
    const scheduleRecovery = (delay) => {
      if (!active || recoveryTimer || !setTimeoutFn) return;
      recoveryTimer = setTimeoutFn(() => { recoveryTimer = null; void reacquire(); }, Math.max(0, delay));
    };
    // One health evaluation per 1-second sampling tick (no extra polling loop).
    const tick = () => {
      if (!active) return;
      pollFallbackProgress();
      const t = track();
      if (!t || t.readyState === 'ended') { if (health !== CAMERA_HEALTH.RECOVERING) scheduleRecovery(0); return; }
      const staleFrames = progressObservable() && now() - lastFrameAt > NO_FRAME_GRACE_MS;
      if (t.muted || staleFrames || !(video?.videoWidth > 0)) {
        if (health === CAMERA_HEALTH.ACTIVE || health === CAMERA_HEALTH.STARTING) setUnavailable('no_frames');
        const stuck = now() - (unhealthySince || now());
        if (!rebound && stuck >= REBIND_AFTER_MS) rebind();
        if (stuck >= REACQUIRE_AFTER_MS) scheduleRecovery(0);
        return;
      }
      if (health !== CAMERA_HEALTH.ACTIVE) setHealthy('frames_resumed');
      sample();
    };
    const attachStreamListeners = (gen) => {
      const t = track();
      t?.addEventListener?.('ended', () => {
        if (gen !== generation || !active) return;
        stopTracks();
        setUnavailable('disconnected', CAMERA_HEALTH.FAILED_RETRYABLE);
        scheduleRecovery(RECOVERY_BACKOFF_MS[0]);
      });
      t?.addEventListener?.('mute', () => {
        if (gen !== generation || !active) return;
        setUnavailable('no_frames');                                  // e.g. privacy shutter closed
      });
      t?.addEventListener?.('unmute', () => {
        if (gen !== generation || !active) return;
        noteFrame(); rebind();                                        // shutter opened: resume the preview now
        setHealthy('unmuted');
      });
    };
    const open = async (constraints) => {
      const gen = ++generation;
      const request = { video: constraints, audio: false }; // NEVER the microphone
      const next = global.JupiterDevicePermissions
        ? await global.JupiterDevicePermissions.media(mediaDevices, request)
        : await mediaDevices.getUserMedia({ video: constraints, audio: false });
      if (gen !== generation || !active) { next.getTracks().forEach((t) => t.stop()); return false; }
      stopTracks();
      stream = next;
      lastQualityFrames = -1; lastMediaTime = -1; rebound = false;
      attachStreamListeners(gen);
      if (video) { video.srcObject = stream; video.muted = true; video.playsInline = true; try { await video.play?.(); } catch (_) {} }
      noteFrame();                                                    // grace window starts now
      watchFrames(gen);
      try {
        const devices = await mediaDevices.enumerateDevices?.() || [];
        deviceIds = devices.filter((d) => d.kind === 'videoinput').map((d) => d.deviceId).filter(Boolean);
      } catch (_) { deviceIds = []; }
      if (setIntervalFn) timer = setIntervalFn(tick, FRAME_INTERVAL_MS);
      if (track()?.muted) { health = CAMERA_HEALTH.STARTING; setUnavailable('no_frames'); }
      else { health = CAMERA_HEALTH.STARTING; setHealthy(); }
      return true;
    };
    const constraintsNow = () => (!isMobile() && deviceIds.length > 1 && deviceIds[deviceIndex]
      ? { deviceId: { exact: deviceIds[deviceIndex] }, width: { ideal: 1280 }, height: { ideal: 720 } }
      : { facingMode: { ideal: facing }, width: { ideal: 1280 }, height: { ideal: 720 } });
    const failTerminal = (kind) => {
      active = false; health = CAMERA_HEALTH.FAILED_TERMINAL; clearRecovery(); detachListeners(); stopTracks();
      emit('error', { kind, message: CAMERA_MESSAGES[kind] });
    };
    const reacquire = async () => {
      if (!active) return false;
      health = CAMERA_HEALTH.RECOVERING;
      try {
        return await open(constraintsNow());
      } catch (error) {
        if (!active) return false;
        const kind = cameraErrorKind(error);
        if (TERMINAL_CAMERA_KINDS.has(kind)) { failTerminal(kind); return false; }
        setUnavailable(kind, CAMERA_HEALTH.FAILED_RETRYABLE);
        scheduleRecovery(RECOVERY_BACKOFF_MS[Math.min(recoveryAttempt++, RECOVERY_BACKOFF_MS.length - 1)]);
        return false;
      }
    };
    const attachEnvironmentListeners = () => {
      if (!deviceListener && mediaDevices?.addEventListener) {
        deviceListener = () => {                                     // a camera appeared/changed: re-evaluate now
          if (!active || health === CAMERA_HEALTH.ACTIVE) return;
          clearRecovery(); scheduleRecovery(250);
        };
        try { mediaDevices.addEventListener('devicechange', deviceListener); } catch (_) { deviceListener = null; }
      }
      if (!videoListener && video?.addEventListener) {
        videoListener = () => { if (active) noteFrame(); };
        for (const type of ['playing', 'loadeddata', 'canplay']) { try { video.addEventListener(type, videoListener); } catch (_) {} }
      }
    };
    return {
      async start() {
        if (active) return health === CAMERA_HEALTH.ACTIVE;
        if (!mediaDevices?.getUserMedia) {
          health = CAMERA_HEALTH.FAILED_TERMINAL;
          emit('error', { kind: 'unsupported', message: CAMERA_MESSAGES.unsupported });
          return false;
        }
        active = true; health = CAMERA_HEALTH.STARTING; recoveryAttempt = 0; unhealthySince = 0; lastKind = '';
        attachEnvironmentListeners();
        try {
          return await open({ facingMode: { ideal: facing }, width: { ideal: 1280 }, height: { ideal: 720 } });
        } catch (error) {
          if (!active) return false;
          const kind = cameraErrorKind(error);
          if (TERMINAL_CAMERA_KINDS.has(kind)) { failTerminal(kind); return false; }
          // Shutter closed / device busy / briefly missing: stay WANTED and recover with bounded backoff.
          stopTracks();
          setUnavailable(kind, CAMERA_HEALTH.FAILED_RETRYABLE);
          scheduleRecovery(RECOVERY_BACKOFF_MS[Math.min(recoveryAttempt++, RECOVERY_BACKOFF_MS.length - 1)]);
          return false;
        }
      },
      // Manual Retry from the unavailable panel: reacquire immediately (still bounded by the same generation guards).
      async retry() {
        if (!active) return false;
        clearRecovery(); recoveryAttempt = 0;
        return reacquire();
      },
      async flip() {
        if (!active) return false;
        const previous = { facing, deviceIndex };
        const constraintsFor = (useDevice) => (useDevice
          ? { deviceId: { exact: deviceIds[deviceIndex] }, width: { ideal: 1280 }, height: { ideal: 720 } }
          : { facingMode: { exact: facing }, width: { ideal: 1280 }, height: { ideal: 720 } });
        const useDevice = !isMobile() && deviceIds.length > 1;
        if (useDevice) deviceIndex = (deviceIndex + 1) % deviceIds.length;
        else facing = facing === 'user' ? 'environment' : 'user';
        stopTracks();                               // many phones cannot open a second camera while one is live
        try {
          return await open(constraintsFor(useDevice));
        } catch (_) {
          facing = previous.facing; deviceIndex = previous.deviceIndex;
          try {
            await open(constraintsFor(useDevice));  // restore the previous camera
            emit('notice', { kind: 'switch_failed', message: CAMERA_MESSAGES.switch_failed.replace('stays off', 'is back on') });
          } catch (error) {
            active = false; health = CAMERA_HEALTH.OFF; clearRecovery(); detachListeners(); stopTracks();
            emit('error', { kind: 'switch_failed', message: CAMERA_MESSAGES.switch_failed });
          }
          return false;
        }
      },
      async setTorch(on) {
        const t = track();
        if (!t?.getCapabilities?.().torch) { torchOn = false; emit('on'); return false; }  // never fake a flashlight
        try { await t.applyConstraints({ advanced: [{ torch: Boolean(on) }] }); torchOn = Boolean(on); }
        catch (_) { torchOn = false; }
        emit('on');
        return torchOn;
      },
      // Manual OFF, Live end, page unload: ALL recovery stops (timers, listeners, frame callbacks, tracks).
      stop(reason = 'off') {
        const wasActive = active || Boolean(stream);
        active = false; generation += 1; health = CAMERA_HEALTH.OFF;
        clearRecovery(); detachListeners(); stopTracks();
        if (wasActive) emit('off', { reason });
      },
      sampleNow: sample,
      tickNow: tick,
      get active() { return active; },
      get wanted() { return active; },
      get health() { return health; },
      get stream() { return stream; },
      get recoveryPending() { return Boolean(recoveryTimer); },
      capabilities
    };
  }

  // ── Screen share (visual input) ────────────────────────────────────────────────────────────────────────────
  // Same contract as the camera: explicit user action only, VIDEO ONLY (never system audio), frames travel as a
  // separate stream, memory only, never persisted. Screens carry small text, so the profile keeps more pixels than
  // the camera but sends far fewer frames: a frame goes out only when the screen visibly changed (fine 32×18 luma
  // signature) or as a 5 s keyframe, never faster than 1 fps, and always under the server's 256 KB frame cap.
  const SCREEN_PROFILE = Object.freeze({ INTERVAL_MS: 1000, KEYFRAME_MS: 5000, MAX_EDGE: 1280, JPEG_QUALITY: 0.72,
    MIN_JPEG_QUALITY: 0.45, MAX_FRAME_BYTES: 240 * 1024, SIG_W: 32, SIG_H: 18, CHANGE_THRESHOLD: 1.2, BLANK_FRAMES: 3 });
  const SCREEN_MESSAGES = Object.freeze({
    permission_denied: 'Screen sharing was cancelled. Live continues without it.',
    unsupported: 'Screen sharing is not available in this app or browser.',
    busy: 'Screen sharing could not start right now. Please try again.',
    ended: 'Screen sharing stopped.',
    protected: "This screen looks blank or protected, so Jupiter can't see it. Protected apps stay private.",
    screen_error: 'Screen sharing could not start. Live continues without it.'
  });
  function screenErrorKind(error) {
    const name = String(error?.name || error?.kind || '');
    if (name === 'NotAllowedError' || name === 'PermissionDeniedError' || name === 'permission_denied') return 'permission_denied';
    if (name === 'NotSupportedError' || name === 'TypeError' || name === 'SecurityError' || name === 'unsupported') return 'unsupported';
    if (name === 'NotReadableError' || name === 'AbortError' || name === 'InvalidStateError' || name === 'busy') return 'busy';
    return 'screen_error';
  }
  const base64Bytes = (data) => {
    const s = String(data || '');
    const pad = s.endsWith('==') ? 2 : s.endsWith('=') ? 1 : 0;
    return Math.max(0, Math.floor(s.length * 3 / 4) - pad);
  };
  const signatureDelta = (a, b) => {
    if (!a || !b || a.length !== b.length) return Infinity;
    let diff = 0;
    for (let i = 0; i < a.length; i++) diff += Math.abs(a[i] - b[i]);
    return diff / a.length;
  };
  // A secure/DRM window is captured as a flat dark rectangle; a white document is NOT blank.
  const isBlankSignature = (sig) => {
    if (!sig || !sig.length) return false;
    let min = 255, max = 0, sum = 0;
    for (let i = 0; i < sig.length; i++) { const v = sig[i]; if (v < min) min = v; if (v > max) max = v; sum += v; }
    return max - min <= 6 && sum / sig.length < 24;
  };
  // Encode within the byte budget: step quality down first (keeps text geometry), then the size.
  function encodeWithinBudget(canvas, source, w, h, profile = SCREEN_PROFILE) {
    if (!canvas?.getContext || !w || !h) return null;
    let scale = Math.min(1, profile.MAX_EDGE / Math.max(w, h)), quality = profile.JPEG_QUALITY;
    for (let attempt = 0; attempt < 6; attempt++) {
      canvas.width = Math.max(1, Math.round(w * scale)); canvas.height = Math.max(1, Math.round(h * scale));
      canvas.getContext('2d').drawImage(source, 0, 0, canvas.width, canvas.height);
      const url = String(canvas.toDataURL('image/jpeg', quality) || '');
      const data = url.slice(url.indexOf(',') + 1);
      if (!data) return null;
      if (base64Bytes(data) <= profile.MAX_FRAME_BYTES) return { data, width: canvas.width, height: canvas.height, quality };
      if (quality - 0.1 >= profile.MIN_JPEG_QUALITY - 1e-9) quality = Math.round((quality - 0.1) * 100) / 100;
      else scale *= 0.8;
    }
    return null;                                                      // never send an over-budget frame
  }
  // Sampling must not be throttled while Jupiter's own window is occluded by the window being shared, so the
  // ticker runs in a tiny dedicated worker when one can be created, else on a normal interval.
  function createTicker(ms, fn, { WorkerCtor = global.Worker, BlobCtor = global.Blob, URLApi = global.URL,
    setIntervalFn = global.setInterval?.bind(global), clearIntervalFn = global.clearInterval?.bind(global) } = {}) {
    let worker = null, timer = null, url = '';
    try {
      if (WorkerCtor && BlobCtor && URLApi?.createObjectURL) {
        url = URLApi.createObjectURL(new BlobCtor([`setInterval(function(){postMessage(0)},${Math.max(250, ms | 0)})`],
          { type: 'text/javascript' }));
        worker = new WorkerCtor(url);
        worker.onmessage = () => fn();
      }
    } catch (_) { worker = null; }
    if (!worker && setIntervalFn) timer = setIntervalFn(fn, ms);
    return {
      kind: worker ? 'worker' : 'interval',
      stop() {
        try { worker?.terminate?.(); } catch (_) {}
        if (url) { try { URLApi.revokeObjectURL(url); } catch (_) {} }
        if (timer && clearIntervalFn) clearIntervalFn(timer);
        worker = null; timer = null; url = '';
      }
    };
  }
  // Two backends behind one interface:
  //  • web (Windows WebView2 / browsers): getDisplayMedia → system picker (entire screen, a window or a tab).
  //  • android: the native JupiterScreenShare bridge (MediaProjection with the system consent dialog). Native code
  //    samples, change-gates and compresses; this side only forwards frames while sharing is wanted.
  function createScreenShare({ mediaDevices = global.navigator?.mediaDevices, nativeBridge = global.JupiterScreenShare,
    onFrame = () => {}, onState = () => {}, createCanvas = () => global.document?.createElement('canvas'),
    createVideo = () => global.document?.createElement('video'), ImageCaptureCtor = global.ImageCapture,
    tickerFactory = createTicker, now = () => Date.now(), profile = SCREEN_PROFILE, host = global } = {}) {
    const native = Boolean(nativeBridge && typeof nativeBridge.start === 'function');
    let stream = null, ticker = null, video = null, capture = null, active = false, generation = 0;
    let lastSignature = null, lastSentAt = 0, blankRun = 0, protectedShown = false, busy = false, surface = '';
    const canvas = createCanvas(), small = createCanvas();
    const emit = (status, extra = {}) => onState({ status, backend: native ? 'android' : 'web', surface, active, ...extra });
    const supported = () => {
      if (native) { try { return nativeBridge.isAvailable ? Boolean(nativeBridge.isAvailable()) : true; } catch (_) { return false; } }
      return typeof mediaDevices?.getDisplayMedia === 'function';
    };
    const signatureOf = (source, w, h) => {
      if (!small?.getContext) return null;
      const sw = profile.SIG_W, sh = profile.SIG_H;
      small.width = sw; small.height = sh;
      const g = small.getContext('2d', { willReadFrequently: true });
      g.drawImage(source, 0, 0, w, h, 0, 0, sw, sh);
      const px = g.getImageData(0, 0, sw, sh).data, sig = new Uint8Array(sw * sh);
      for (let i = 0; i < sig.length; i++) sig[i] = (px[i * 4] * 0.299 + px[i * 4 + 1] * 0.587 + px[i * 4 + 2] * 0.114) | 0;
      return sig;
    };
    const noteBlank = (blank) => {
      if (blank) {
        blankRun += 1;
        if (blankRun >= profile.BLANK_FRAMES && !protectedShown) {
          protectedShown = true;
          emit('notice', { kind: 'protected', message: SCREEN_MESSAGES.protected });
        }
        return;
      }
      if (protectedShown) emit('notice', { kind: 'visible' });
      blankRun = 0; protectedShown = false;
    };
    // One decision per tick: skip unchanged screens, skip blank/protected screens, keep the byte budget.
    const consider = (source, w, h) => {
      if (!active || !w || !h) return false;
      const sig = signatureOf(source, w, h);
      const blank = isBlankSignature(sig);
      noteBlank(blank);
      if (blank) { lastSignature = null; return false; }              // never send a black placeholder as "the screen"
      const t = now();
      if (signatureDelta(sig, lastSignature) <= profile.CHANGE_THRESHOLD && t - lastSentAt < profile.KEYFRAME_MS) return false;
      const frame = encodeWithinBudget(canvas, source, w, h, profile);
      if (!frame) return false;
      lastSignature = sig; lastSentAt = t;
      onFrame({ mime_type: 'image/jpeg', data: frame.data, width: frame.width, height: frame.height, source: 'screen' });
      return true;
    };
    const track = () => stream?.getVideoTracks?.()[0] || null;
    const tick = async () => {
      if (!active || busy || native) return;
      const gen = generation;
      busy = true;
      try {
        if (capture?.grabFrame) {
          const bitmap = await capture.grabFrame();
          try { if (gen === generation) consider(bitmap, bitmap.width, bitmap.height); }
          finally { try { bitmap.close?.(); } catch (_) {} }
        } else if (video) {
          consider(video, video.videoWidth || 0, video.videoHeight || 0);
        }
      } catch (_) {
        // A frame grab can fail transiently (window minimised, resize); the next tick tries again.
      } finally { busy = false; }
    };
    const release = () => {
      ticker?.stop?.(); ticker = null;
      stream?.getTracks?.().forEach((t) => { try { t.stop(); } catch (_) {} });
      stream = null; capture = null;
      if (video) { try { video.srcObject = null; } catch (_) {} }
      video = null; lastSignature = null; blankRun = 0; protectedShown = false; busy = false;
    };
    const clearNativeCallbacks = () => {
      if (host.onJupiterScreenShareFrame === nativeFrame) host.onJupiterScreenShareFrame = undefined;
      if (host.onJupiterScreenShareState === nativeState) host.onJupiterScreenShareState = undefined;
    };
    const parse = (payload) => {
      if (payload && typeof payload === 'object') return payload;
      try { return JSON.parse(String(payload || '{}')); } catch (_) { return {}; }
    };
    function nativeFrame(payload) {
      if (!active) return;
      const frame = parse(payload);
      const data = String(frame.data || '');
      if (!data || base64Bytes(data) > profile.MAX_FRAME_BYTES) return;
      onFrame({ mime_type: 'image/jpeg', data, width: Number(frame.width) || 0, height: Number(frame.height) || 0, source: 'screen' });
    }
    function nativeState(payload) {
      const state = parse(payload);
      const status = String(state.status || '');
      if (status === 'on') { if (!active) return; surface = 'monitor'; emit('on'); return; }
      if (status === 'notice') { if (active) emit('notice', { kind: String(state.kind || ''), message: SCREEN_MESSAGES[state.kind] || '' }); return; }
      if (status === 'off' || status === 'error') {
        const wasActive = active;
        active = false; generation += 1; clearNativeCallbacks();
        if (status === 'error') {
          const kind = screenErrorKind({ kind: state.kind });
          emit('error', { kind, message: SCREEN_MESSAGES[kind] });
        } else if (wasActive) emit('off', { reason: String(state.reason || 'system') });
      }
    }
    const api = {
      supported,
      async start() {
        if (active) return true;
        if (!supported()) { emit('error', { kind: 'unsupported', message: SCREEN_MESSAGES.unsupported }); return false; }
        active = true; const gen = ++generation; surface = '';
        emit('starting');
        if (native) {
          host.onJupiterScreenShareFrame = nativeFrame;
          host.onJupiterScreenShareState = nativeState;
          try { nativeBridge.start(); } catch (error) {
            active = false; clearNativeCallbacks();
            emit('error', { kind: 'screen_error', message: SCREEN_MESSAGES.screen_error });
            return false;
          }
          return true;                                                // 'on' arrives after the system consent dialog
        }
        try {
          const next = await mediaDevices.getDisplayMedia({ video: { frameRate: { ideal: 5, max: 5 } }, audio: false });
          if (gen !== generation || !active) { next.getTracks().forEach((t) => t.stop()); return false; }
          stream = next;
          const t = track();
          try { surface = String(t?.getSettings?.().displaySurface || ''); } catch (_) { surface = ''; }
          // The browser/OS "Stop sharing" control ends the track: treat it exactly like our Stop button.
          t?.addEventListener?.('ended', () => { if (gen === generation && active) api.stop('ended'); });
          if (ImageCaptureCtor && t) { try { capture = new ImageCaptureCtor(t); } catch (_) { capture = null; } }
          if (!capture?.grabFrame) {
            video = createVideo();
            if (video) { video.muted = true; video.playsInline = true; video.srcObject = stream; try { await video.play?.(); } catch (_) {} }
          }
          ticker = tickerFactory(profile.INTERVAL_MS, () => { void tick(); });
          emit('on');
          void tick();                                                // first frame right away
          return true;
        } catch (error) {
          if (gen !== generation) return false;
          active = false; release();
          const kind = screenErrorKind(error);
          emit('error', { kind, message: SCREEN_MESSAGES[kind] });
          return false;
        }
      },
      // Manual Stop, the system Stop control, switching to camera, Live end, page unload: everything is released.
      stop(reason = 'off') {
        const wasActive = active || Boolean(stream);
        active = false; generation += 1;
        if (native) { try { nativeBridge.stop?.(); } catch (_) {} clearNativeCallbacks(); }
        release();
        if (wasActive) emit('off', { reason });
      },
      tickNow: tick,
      considerNow: consider,
      get active() { return active; },
      get backend() { return native ? 'android' : 'web'; },
      get surface() { return surface; },
      get stream() { return stream; },
      get tickerKind() { return ticker?.kind || ''; }
    };
    return api;
  }

  // ── Camera transcript overlay: exactly one CURRENT Jupiter response, never user speech ──────────────────────
  function createTranscriptOverlay({ doc = global.document } = {}) {
    let el = null, textEl = null, turnId = '', visible = true, size = 'medium';
    return {
      attach(host) {
        if (el || !host || !doc) return el;
        el = doc.createElement('section');
        el.className = 'live-camera-transcript';
        el.setAttribute('aria-live', 'polite');
        el.setAttribute('aria-label', "Jupiter's current response");
        textEl = doc.createElement('p');
        el.append(textEl);
        host.append(el);
        this.setVisible(visible); this.setSize(size);
        return el;
      },
      // Only assistant text is ever passed here. A new turn REPLACES the previous response.
      showResponse(id, text) {
        const next = String(id || '');
        if (next !== turnId) turnId = next;
        if (textEl) textEl.textContent = String(text || '');
        el?.classList.toggle('empty', !String(text || '').trim());
        if (el && typeof el.scrollHeight === 'number') el.scrollTop = el.scrollHeight;   // compact strip: newest line visible
      },
      clear() { turnId = ''; if (textEl) textEl.textContent = ''; el?.classList.add('empty'); },
      setVisible(on) { visible = Boolean(on); if (el) { el.hidden = !visible; } return visible; },
      setSize(next) { size = ['small', 'medium', 'large'].includes(next) ? next : 'medium'; if (el) el.dataset.size = size; },
      detach() { el?.remove?.(); el = null; textEl = null; turnId = ''; },
      get text() { return textEl?.textContent || ''; },
      get turnId() { return turnId; },
      get visible() { return visible; }
    };
  }

  // LOCAL PREVIEW orientation (never model input: frames are drawn from the raw track above, and CSS on the
  // preview <video> does not affect canvas drawImage). Front/user-facing previews are ALWAYS selfie-mirrored (raise
  // your right hand, see it on the right); rear/environment previews are canonical. No user toggle. Only real
  // facingMode evidence (or the platform's requested facing on mobile) is used; a desktop webcam without facingMode
  // is treated as user-facing, never guessed from its name.
  const previewSideOf = ({ trackFacing = '', requestedFacing = 'user', mobile = false } = {}) => {
    const reported = String(trackFacing || '').toLowerCase();
    if (reported === 'user' || reported === 'environment') return reported;
    if (mobile) return requestedFacing === 'environment' ? 'environment' : 'user';
    return 'unknown';
  };
  const previewOrientation = ({ trackFacing = '', requestedFacing = 'user', mobile = false } = {}) => {
    const side = previewSideOf({ trackFacing, requestedFacing, mobile });
    return { side, flipPreview: side !== 'environment' };
  };

  global.JupiterLiveMedia = Object.freeze({
    previewOrientation, previewSideOf,
    createAura, auraStateFor, createSounds, CUES, createCamera, cameraErrorKind, CAMERA_MESSAGES, CAMERA_HEALTH, RECOVERY_BACKOFF_MS,
    createTranscriptOverlay, FRAME_INTERVAL_MS, KEYFRAME_MS, MAX_EDGE, JPEG_QUALITY,
    createScreenShare, screenErrorKind, SCREEN_PROFILE, SCREEN_MESSAGES, encodeWithinBudget, isBlankSignature,
    signatureDelta, base64Bytes, createTicker
  });
})(typeof window !== 'undefined' ? window : globalThis);
