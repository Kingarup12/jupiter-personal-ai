(function (root) {
  'use strict';
  // Jupiter in-app calls (marker 172): the DOM-free call controller shared by Windows WT and the Android WebView.
  // A Jupiter call is NOT a phone call: answering joins the existing canonical Live Conversation (one audio
  // pipeline, one microphone owner). Nothing here answers, opens the microphone, camera or screen capture by itself.
  //
  // Ringing ownership (never both):
  //   Android app  -> the native incoming-call notification is the ONLY ringer (follows the phone's silent/vibrate/DND).
  //   Windows / web -> this page plays the ringer, silently when Windows Do Not Disturb / Focus is on.

  const STATES = ['idle', 'incoming', 'connecting', 'connected', 'reconnecting', 'ended'];
  const LIVE_CONNECTED = new Set(['listening', 'user_speaking', 'endpoint_confirmation', 'waiting_for_user',
    'transcribing', 'processing', 'thinking', 'generating_text', 'preparing_speech', 'assistant_speaking', 'speaking']);
  const END_MESSAGES = {
    declined: 'Call declined', missed: 'Missed call', answered_elsewhere: 'Answered on another device',
    cancelled: 'Call cancelled', expired: 'This call has ended', hung_up: 'Call ended', failed: 'Call could not connect',
    signed_out: 'Call ended', not_found: 'This call is no longer available', declined_elsewhere: 'Declined on another device'
  };

  function formatDuration(ms) {
    const total = Math.max(0, Math.floor(Number(ms || 0) / 1000));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = String(total % 60).padStart(2, '0');
    return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${seconds}` : `${minutes}:${seconds}`;
  }

  function endMessage(reason) {
    return END_MESSAGES[reason] || 'Call ended';
  }

  // ── controller ─────────────────────────────────────────────────────────────────────────────────────────────
  function createCallController(options = {}) {
    const api = options.api;                     // { accept(id), decline(id), end(id, seconds, reason) } -> Promise
    const ringer = options.ringer || { start() {}, stop() {} };
    const clock = options.clock || (() => Date.now());
    const setTimer = options.setTimer || ((fn, ms) => setTimeout(fn, ms));
    const clearTimer = options.clearTimer || ((id) => clearTimeout(id));
    const onChange = options.onChange || (() => {});
    const joinLive = options.joinLive || (() => {});          // ({ callId }) -> opens the canonical Live session
    const leaveLive = options.leaveLive || (() => {});
    const handled = new Map();                                // call ids that were answered/declined/missed here
    let state = fresh();
    let ringTimer = null;
    let endTimer = null;
    let answering = '';

    function fresh() {
      return { status: 'idle', direction: '', call: null, reason: '', connectedAt: 0, endedAt: 0, endReason: '',
        error: '', everConnected: false };
    }
    function emit() { onChange(snapshot()); }
    function remember(callId) {
      if (!callId) return;
      handled.set(callId, clock());
      for (const [id, at] of handled) if (clock() - at > 15 * 60_000) handled.delete(id);
    }
    function stopRinging() {
      if (ringTimer !== null) { clearTimer(ringTimer); ringTimer = null; }
      try { ringer.stop(state.call?.call_id || ''); } catch (_) { /* a failing ringer never blocks the call */ }
    }
    function finish(reason, { keep = 2600 } = {}) {
      stopRinging();
      remember(state.call?.call_id);
      answering = '';
      state = { ...state, status: 'ended', endReason: reason, endedAt: clock() };
      emit();
      if (endTimer !== null) clearTimer(endTimer);
      endTimer = setTimer(() => { endTimer = null; if (state.status === 'ended') { state = fresh(); emit(); } }, keep);
    }

    function incoming(call) {
      const id = String(call?.call_id || '');
      if (!/^[a-f0-9]{32}$/.test(id) || handled.has(id)) return false;
      if (state.call?.call_id === id && state.status === 'incoming') return false;      // duplicate push/poll
      if (!['idle', 'ended'].includes(state.status)) return false;                    // already on a call
      const left = Math.max(0, Number(call.ring_seconds_left || 0));
      if (left <= 0) return false;
      if (endTimer !== null) { clearTimer(endTimer); endTimer = null; }
      state = { ...fresh(), status: 'incoming', direction: 'incoming', call: { ...call, call_id: id },
        reason: String(call.reason || 'Jupiter is calling').slice(0, 120) };
      try { ringer.start(state.call); } catch (_) { /* silent ringing is still a visible call */ }
      ringTimer = setTimer(() => { ringTimer = null; if (state.status === 'incoming' && state.call?.call_id === id) finish('missed'); },
        left * 1000);
      emit();
      return true;
    }

    async function answer(callId) {
      const id = String(callId || state.call?.call_id || '');
      if (!id || answering === id || handled.has(id)) return false;                    // double-click / replay guard
      if (state.status !== 'incoming' || state.call?.call_id !== id) {
        if (!['idle', 'ended'].includes(state.status)) return false;
        state = { ...fresh(), status: 'incoming', direction: 'incoming', call: { call_id: id }, reason: 'Jupiter is calling' };
      }
      answering = id;
      stopRinging();                                                                    // stop the sound first
      state = { ...state, status: 'connecting' };
      emit();
      try {
        await api.accept(id);
      } catch (error) {
        answering = '';
        finish(error?.code || 'failed');
        return false;
      }
      remember(id);
      try { joinLive({ callId: id }); } catch (_) { finish('failed'); return false; }
      return true;
    }

    async function decline(callId) {
      const id = String(callId || state.call?.call_id || '');
      if (!id || state.call?.call_id !== id || state.status !== 'incoming') return false;
      finish('declined');
      try { await api.decline(id); } catch (_) { /* the server times it out anyway */ }
      return true;
    }

    function remoteEnded(callId, reason = 'expired') {
      const id = String(callId || '');
      if (!id) return;
      remember(id);
      if (state.call?.call_id === id && ['incoming', 'connecting'].includes(state.status) && answering !== id) finish(reason);
    }

    function startOutgoing() {
      if (!['idle', 'ended'].includes(state.status)) return false;
      if (endTimer !== null) { clearTimer(endTimer); endTimer = null; }
      state = { ...fresh(), status: 'connecting', direction: 'outgoing', reason: 'Calling Jupiter' };
      emit();
      try { joinLive({ callId: '' }); } catch (_) { finish('failed'); return false; }
      return true;
    }

    // Live Conversation states drive connected / reconnecting; the duration counts only while connected once.
    function liveState(liveStateName) {
      if (!['connecting', 'connected', 'reconnecting'].includes(state.status)) return;
      const name = String(liveStateName || '').toLowerCase();
      if (LIVE_CONNECTED.has(name)) {
        if (state.status !== 'connected') {
          state = { ...state, status: 'connected', everConnected: true,
            connectedAt: state.connectedAt || clock() };
          emit();
        }
      } else if (name === 'connecting' && state.everConnected && state.status !== 'reconnecting') {
        state = { ...state, status: 'reconnecting' };
        emit();
      } else if (name === 'error' && state.status === 'connecting' && !state.everConnected) {
        finish('failed');
      }
    }

    function liveClosed() {
      if (!['connecting', 'connected', 'reconnecting'].includes(state.status)) return;
      const callId = state.direction === 'incoming' ? state.call?.call_id : '';
      const seconds = state.connectedAt ? Math.round((clock() - state.connectedAt) / 1000) : 0;
      finish(state.everConnected ? 'hung_up' : 'failed');
      if (callId) Promise.resolve(api.end(callId, seconds, 'hung_up')).catch(() => {});
    }

    function hangUp() {
      if (!['connecting', 'connected', 'reconnecting'].includes(state.status)) return false;
      try { leaveLive(); } catch (_) { /* teardown continues below */ }
      liveClosed();
      return true;
    }

    // Sign-out, account switch, page teardown: stop every sound and timer immediately; nothing survives.
    function teardown() {
      stopRinging();
      if (endTimer !== null) { clearTimer(endTimer); endTimer = null; }
      answering = '';
      handled.clear();
      state = fresh();
      emit();
    }

    function snapshot() {
      const now = clock();
      return {
        status: state.status, direction: state.direction, callId: state.call?.call_id || '', reason: state.reason,
        endReason: state.endReason, endMessage: state.status === 'ended' ? endMessage(state.endReason) : '',
        durationMs: state.connectedAt ? ((state.status === 'ended' ? state.endedAt : now) - state.connectedAt) : 0,
        ringing: state.status === 'incoming'
      };
    }

    return { incoming, answer, decline, remoteEnded, startOutgoing, liveState, liveClosed, hangUp, teardown, snapshot,
      isHandled: (id) => handled.has(String(id || '')) };
  }

  // ── page ringer (Windows / web only) ──────────────────────────────────────────────────────────────────────
  // Default: a soft synthesized two-tone ring (no bundled media). Custom: a user-chosen audio file (object URL).
  // `isQuiet()` reflects Windows Do Not Disturb / Focus: then the call is shown but makes no sound.
  function createPageRinger(options = {}) {
    const AudioContextCtor = options.AudioContextCtor || root?.AudioContext || root?.webkitAudioContext;
    const AudioCtor = options.AudioCtor || root?.Audio;
    const setTimer = options.setTimer || ((fn, ms) => setTimeout(fn, ms));
    const clearTimer = options.clearTimer || ((id) => clearTimeout(id));
    const isQuiet = options.isQuiet || (() => false);
    const customUrl = options.customUrl || (() => '');
    const volume = () => Math.max(0, Math.min(1, Number(options.volume?.() ?? 0.7)));
    let context = null;
    let audio = null;
    let loop = null;
    let playing = false;

    function burst() {
      if (!context) return;
      const now = context.currentTime;
      const gain = context.createGain();
      gain.gain.setValueAtTime(0, now);
      gain.connect(context.destination);
      [[659.25, 0], [880, 0.18], [659.25, 0.42], [880, 0.6]].forEach(([frequency, offset]) => {
        const osc = context.createOscillator();
        osc.type = 'sine';
        osc.frequency.setValueAtTime(frequency, now + offset);
        osc.connect(gain);
        osc.start(now + offset);
        osc.stop(now + offset + 0.16);
      });
      const level = 0.18 * volume();
      [0, 0.18, 0.42, 0.6].forEach((offset) => {
        gain.gain.linearRampToValueAtTime(level, now + offset + 0.02);
        gain.gain.linearRampToValueAtTime(0, now + offset + 0.16);
      });
    }

    function start() {
      if (playing) return 'ringing';
      if (isQuiet()) return 'quiet';
      playing = true;
      const url = customUrl();
      if (url && AudioCtor) {
        try {
          audio = new AudioCtor(url);
          audio.loop = true;
          audio.volume = volume();
          const played = audio.play?.();
          if (played && typeof played.catch === 'function') played.catch(() => { if (playing) startTone(); });
          return 'ringing';
        } catch (_) { audio = null; }
      }
      startTone();
      return 'ringing';
    }

    function startTone() {
      if (!AudioContextCtor) return;
      try {
        context = context || new AudioContextCtor();
        context.resume?.();
        const tick = () => { if (!playing) return; burst(); loop = setTimer(tick, 2400); };
        tick();
      } catch (_) { context = null; }
    }

    function stop() {
      playing = false;
      if (loop !== null) { clearTimer(loop); loop = null; }
      if (audio) { try { audio.pause(); audio.currentTime = 0; } catch (_) {} audio = null; }
      if (context) { const closing = context; context = null; try { closing.close?.(); } catch (_) {} }
    }

    return { start, stop, get playing() { return playing; } };
  }

  // Android: the native notification rings (the phone's ringer switch applies); the page never plays audio.
  function createNativeRinger(bridge) {
    return {
      start(call) {
        const expires = Date.now() + Math.max(1, Number(call?.ring_seconds_left || 0)) * 1000;
        try { return bridge.ring(String(call.call_id), String(call.reason || ''), String(expires)); } catch (_) { return 'unavailable'; }
      },
      stop(callId) { try { if (callId) bridge.stop(String(callId)); } catch (_) {} }
    };
  }

  function ringerOwner({ nativeBridge } = {}) {
    return nativeBridge ? 'native' : 'page';
  }

  const api = { STATES, formatDuration, endMessage, createCallController, createPageRinger, createNativeRinger, ringerOwner };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.JupiterCallKit = api;
})(typeof window !== 'undefined' ? window : null);
