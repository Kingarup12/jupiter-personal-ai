/* Camera Live orb transition: the SAME orb (one element, one renderer) travels between its speech layout, a large
 * centred "camera starting" presence and its compact Camera Live spot beside the state pill.
 *
 * Camera ON:  speech/top -> centre of the visible Live stage (grows continuously; colour cross-fades into the camera
 *             RGB spectrum, fully RGB on arrival) -> stays large and alive while the camera initialises -> first REAL
 *             presented frame -> back up (shrinks) -> compact spot beside the state pill.
 * Camera OFF: compact -> briefly toward the centre (grows modestly, RGB resolves back to speech blue) -> speech layout.
 * Failure:    a short recoverable-failure pulse, then back to a valid layout; Live audio is never involved.
 *
 * Geometry: the orb box is pinned at the viewport origin (position: fixed; left/top 0) with the LARGEST size of the
 * movement, and every keyframe is an ABSOLUTE pose `translate(x, y) scale(s)` (transform-origin: centre). Web
 * Animations transform keyframes REPLACE the element's CSS transform, so poses must never be relative to the pinned
 * base (marker 152 bug: relative keyframes made the orb fly to the top-left corner). Scale is always <= 1, so the
 * canvas never upscales (stays sharp). Transform/opacity only; prefers-reduced-motion uses a short fade, no travel.
 * Every animation/timer is owned here and cancelled on reverse, failure, Live end or resize.
 */
((root, factory) => {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.JupiterLiveOrbTransition = api;
})(typeof window !== 'undefined' ? window : globalThis, () => {
  const TIMING = Object.freeze({
    toCenterMs: 560,          // top -> centre (grows continuously)
    centerHoldMs: 160,        // minimum presence at the centre before returning
    toCameraMs: 520,          // centre -> compact spot (shrinks); ~1.2 s total visual movement when the camera is fast
    maxCenterWaitMs: 8000,    // a slow camera never freezes the orb in the centre forever
    exitMs: 600,              // compact -> speech (with a modest centre bulge)
    failPulseMs: 260,
    spectrumBlendMs: 560,     // == toCenterMs: fully RGB when the orb arrives at the centre
    reducedFadeMs: 160,
  });
  const PHASES = Object.freeze(['speech', 'entering', 'centered', 'returning', 'camera', 'exiting', 'failing']);

  function createOrbTransition({ wrap, measureSpeech, measureCamera, viewport, setSpectrum = () => {},
    animate, reducedMotion = () => false, setTimer = (fn, ms) => setTimeout(fn, ms),
    clearTimer = (id) => clearTimeout(id), onPhase = () => {}, onSettled = () => {} } = {}) {
    let phase = 'speech';
    let running = [];                 // live Animation-like handles
    let timers = [];
    let cameraReady = false;
    let boxSize = 0;                  // layout size of the pinned box (the largest size of the current movement)
    let restPose = null;              // where the orb rests between animations (absolute pose)
    let startSize = 0;
    let generation = 0;               // every new request invalidates older callbacks

    const setPhase = (next, detail = '') => { phase = next; onPhase(next, detail); };
    const stopAll = () => {
      running.splice(0).forEach((handle) => { try { handle.cancel(); } catch (_) { /* already finished */ } });
      timers.splice(0).forEach((id) => clearTimer(id));
    };
    const later = (ms, fn) => { const id = setTimer(fn, ms); timers.push(id); return id; };
    const centerOf = (rect) => ({ x: rect.x + rect.size / 2, y: rect.y + rect.size / 2 });
    // Absolute pose that shows the orb centred on `rect` at rect.size (box pinned at the viewport origin).
    const poseFor = (rect, extraScale = 1, opacity = 1) => {
      const c = centerOf(rect);
      const scale = (rect.size / Math.max(1, boxSize)) * extraScale;
      return { transform: `translate(${Math.round(c.x - boxSize / 2)}px, ${Math.round(c.y - boxSize / 2)}px) scale(${scale.toFixed(4)})`,
        opacity };
    };
    const pin = (size, rest) => {
      boxSize = Math.max(1, Math.round(size));
      restPose = rest;
      wrap.classList.add('orb-transit');
      wrap.style.setProperty('--orb-transit-size', `${boxSize}px`);
      wrap.style.setProperty('--orb-transit-transform', poseFor(rest).transform);
    };
    // Hands the orb back to its CSS layout WITHOUT a CSS transition (the last animation frame already shows the final
    // geometry; a transition from the pinned box would visibly jump first).
    const unpin = () => {
      wrap.style.transition = 'none';
      wrap.classList.remove('orb-transit', 'orb-transit-failing');
      for (const name of ['--orb-transit-size', '--orb-transit-transform']) wrap.style.removeProperty(name);
      wrap.style.removeProperty('opacity');
      void wrap.offsetWidth;                                  // commit the untransitioned style now
      setTimer(() => { if (!wrap.classList.contains('orb-transit')) wrap.style.removeProperty('transition'); }, 60);
    };
    const run = (keyframes, duration, easing = 'cubic-bezier(0.22, 1, 0.36, 1)') => {
      const handle = animate(wrap, keyframes, { duration, easing, fill: 'forwards' });
      running.push(handle);
      return handle.finished.then(() => true, () => false);
    };
    // Centre of the VISIBLE Live stage (host excludes title/safe areas and the control dock). The centre presence is
    // always larger than where the orb came from, bounded so it never overflows the stage.
    const centerTarget = () => {
      const stage = viewport();
      const short = Math.min(stage.width, stage.height);
      const size = Math.round(Math.min(short * 0.82, 560, Math.max(startSize * 1.15, short * 0.5)));
      return { size, x: stage.left + stage.width / 2 - size / 2, y: stage.top + stage.height / 2 - size / 2 };
    };

    // The host lays out the final geometry first (onSettled), then the pin is released in the same task: no gap frame.
    const settleInCamera = (gen, reason = '') => {
      if (gen !== generation) return;
      onSettled('camera');
      stopAll();
      unpin();
      setPhase('camera', reason);
    };
    const settleInSpeech = (gen, reason = '') => {
      if (gen !== generation) return;
      onSettled('speech');
      stopAll();
      unpin();
      setPhase('speech', reason);
    };

    const returnToCamera = async (gen, reason = '') => {
      if (gen !== generation || !['centered', 'entering', 'failing'].includes(phase)) return;
      stopAll();
      setPhase('returning', reason);
      const target = measureCamera();
      const from = restPose;
      if (reducedMotion()) {
        await run([poseFor(from, 1, 1), poseFor(from, 0.94, 0)], TIMING.reducedFadeMs, 'ease-out');
      } else if (target) {
        await run([poseFor(from), poseFor(target)], TIMING.toCameraMs, 'cubic-bezier(0.4, 0, 0.2, 1)');
      }
      settleInCamera(gen, reason);
    };

    const api = {
      TIMING,
      get phase() { return phase; },
      // Camera requested by the user from speech Live.
      enter() {
        if (!['speech', 'exiting'].includes(phase)) return false;
        const gen = ++generation;
        stopAll();
        cameraReady = false;
        const from = phase === 'exiting' ? restPose : measureSpeech();
        startSize = from?.size || startSize;
        const center = centerTarget();
        pin(Math.max(center.size, from?.size || 0), center);
        setPhase('entering');
        setSpectrum(true, { blendMs: TIMING.spectrumBlendMs });
        const toCenter = reducedMotion()
          ? run([from ? poseFor(from, 1, 1) : poseFor(center, 1, 1), from ? poseFor(from, 1, 0) : poseFor(center, 1, 0.3),
            poseFor(center, 1, 0), poseFor(center, 1, 1)].map((k, i) => ({ ...k, offset: [0, 0.45, 0.55, 1][i] })),
          TIMING.reducedFadeMs * 2, 'ease-in-out')
          : run([from ? poseFor(from) : poseFor(center, 0.9), poseFor(center)], TIMING.toCenterMs);
        toCenter.then(() => {
          if (gen !== generation || phase !== 'entering') return;
          setPhase('centered');
          if (cameraReady) later(TIMING.centerHoldMs, () => { void returnToCamera(gen, 'camera_ready'); });
          else later(TIMING.maxCenterWaitMs, () => { void returnToCamera(gen, 'camera_slow'); });
        });
        return true;
      },
      // The camera produced its first REAL presented frame (never faked: the host checks frame progression).
      cameraReady() {
        cameraReady = true;
        const gen = generation;
        if (phase === 'centered') {
          timers.splice(0).forEach((id) => clearTimer(id));
          later(TIMING.centerHoldMs, () => { void returnToCamera(gen, 'camera_ready'); });
        }
      },
      // terminal=true: permission denied / unsupported -> back to speech. Otherwise the camera UI (unavailable panel,
      // bounded recovery) stays, so the orb settles in its compact spot.
      async cameraFailed({ terminal = false } = {}) {
        if (!['entering', 'centered'].includes(phase)) return false;
        const gen = generation;
        stopAll();
        const at = restPose;
        setPhase('failing', terminal ? 'terminal' : 'retryable');
        wrap.classList.add('orb-transit-failing');
        await run([poseFor(at, 1, 1), poseFor(at, 0.92, 0.55), poseFor(at, 1, 1)], TIMING.failPulseMs, 'ease-in-out');
        if (gen !== generation) return false;
        if (!terminal) { void returnToCamera(gen, 'camera_retryable'); return true; }
        setSpectrum(false, { blendMs: TIMING.spectrumBlendMs });
        wrap.classList.remove('orb-transit-failing');
        const target = measureSpeechAfterUnpin();
        if (target && !reducedMotion()) {
          await run([poseFor(at), poseFor(target)], TIMING.exitMs);
        }
        settleInSpeech(gen, 'camera_failed');
        return true;
      },
      // Camera turned OFF by the user (from Camera Live or mid-transition): reverse to the speech layout.
      exit({ compactRect = null } = {}) {
        if (phase === 'speech') return false;
        const gen = ++generation;
        const fromCompact = phase === 'camera';
        stopAll();
        setPhase('exiting');
        setSpectrum(false, { blendMs: TIMING.spectrumBlendMs });
        let start = restPose;
        if (fromCompact) {
          start = compactRect || measureCamera();
          if (!start) { settleInSpeech(gen, 'no_geometry'); return true; }
          pin(start.size, start);                              // hold the orb exactly where it is
        }
        // The host removes the camera layout right after this call; measure the speech target once it has.
        later(0, async () => {
          if (gen !== generation) return;
          const target = measureSpeechAfterUnpin();
          if (!target || reducedMotion()) {
            if (reducedMotion()) await run([poseFor(start, 1, 1), poseFor(start, 1, 0)], TIMING.reducedFadeMs, 'ease-out');
            settleInSpeech(gen, 'camera_off');
            return;
          }
          const stage = viewport();
          const toward = { x: stage.left + stage.width / 2, y: stage.top + stage.height / 2 };
          const midSize = Math.max(target.size, start.size) * 1.08;
          const startCenter = centerOf(start);
          const midCenter = { x: startCenter.x + (toward.x - startCenter.x) * 0.35, y: startCenter.y + (toward.y - startCenter.y) * 0.35 };
          const mid = { size: midSize, x: midCenter.x - midSize / 2, y: midCenter.y - midSize / 2 };
          pin(midSize, start);                                 // largest size of this movement: scale <= 1 throughout
          await run([{ ...poseFor(start), offset: 0 }, { ...poseFor(mid), offset: 0.42 }, { ...poseFor(target), offset: 1 }],
            TIMING.exitMs, 'cubic-bezier(0.4, 0, 0.2, 1)');
          settleInSpeech(gen, 'camera_off');
        });
        return true;
      },
      // Live ended / page hidden: stop immediately, no animation, no orphan transform.
      cancel() {
        generation += 1;
        stopAll();
        unpin();
        setSpectrum(false);
        if (phase !== 'speech') setPhase('speech', 'cancelled');
      },
      // Keep a centred orb centred on resize; a moving orb re-measures its target when it settles.
      resize() {
        if (phase !== 'centered') return;
        const center = centerTarget();
        pin(Math.max(center.size, boxSize), center);
      },
      snapshot: () => ({ phase, pinned: wrap.classList.contains('orb-transit'), animations: running.length,
        timers: timers.length, boxSize, rest: restPose ? { ...restPose } : null, cameraReady }),
    };
    // Measures the natural speech layout box (the pin is lifted for one synchronous layout read, then restored).
    function measureSpeechAfterUnpin() {
      const saved = ['--orb-transit-size', '--orb-transit-transform'].map((name) => [name, wrap.style.getPropertyValue(name)]);
      wrap.classList.remove('orb-transit');
      const rect = measureSpeech();
      wrap.classList.add('orb-transit');
      saved.forEach(([name, value]) => { if (value) wrap.style.setProperty(name, value); });
      return rect;
    }
    return api;
  }

  // Pure helper for tests/telemetry: where a pose puts the VISIBLE orb box (centre + size) for a pinned box size.
  function poseRect(transform, boxSize) {
    const m = /translate\((-?[\d.]+)px, (-?[\d.]+)px\) scale\(([\d.]+)\)/.exec(String(transform || ''));
    if (!m) return null;
    const x = Number(m[1]); const y = Number(m[2]); const s = Number(m[3]);
    return { cx: x + boxSize / 2, cy: y + boxSize / 2, size: boxSize * s };
  }

  return { createOrbTransition, poseRect, TIMING, PHASES };
});
