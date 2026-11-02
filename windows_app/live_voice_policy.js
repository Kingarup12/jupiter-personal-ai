((root, factory) => {
  const policy = factory();
  if (typeof module === 'object' && module.exports) module.exports = policy;
  if (root) root.JupiterLiveVoicePolicy = policy;
})(typeof window !== 'undefined' ? window : globalThis, () => {
  const LIVE_TURN_CONFIG = Object.freeze({
    // Marker 214 (real WT 24 Oct: with Spotify playing, the song's vocals never left 2 s of quiet, so Live stayed in
    // LISTENING for good): one turn is never longer than this; what was heard so far is sent.
    maxTurnMs: 25000,
    minimumSilenceMs: 2000,
    quietEndpointMs: 2200,
    stableRecoveryMs: 6000,
    foregroundResumeWindowMs: 1150,
    foregroundHangoverMs: 560,
    candidateResetMs: 180,
    foregroundCandidateMs: 220,
    contextualCandidateMs: 100,
    foregroundProminenceRatio: 2.65,
    contextualProminenceRatio: 1.55,
    continuationProminenceRatio: 1.8,
    foregroundOwnershipFloorRatio: 0.68,
    // A contiguous trail-off of the SAME speaker (quieter end of a sentence, a soft "…like whatever it is") must
    // stay foreground, otherwise the >=2 s silence clock runs while the user is still talking (real WT turn 3).
    // Within the resume window only 40 % of the running speech level is required; after a longer gap the stricter
    // ownership floor applies again, so a quieter background talker cannot re-open a finished turn.
    foregroundContinuationFloorRatio: 0.4,
    foregroundResumeCandidateMs: 140,
    // After a real gap (> hangover) a resume must be SUSTAINED speech, not one strong frame: short speech-like noises
    // (cough, chair, click, a word across the room) otherwise restarted the 2 s floor again and again (real WT: 8-15 s
    // of LISTENING after a finished sentence). Audio keeps streaming meanwhile; post-endpoint continuation remains.
    foregroundSustainedResumeMs: 300,
    // Complete-looking utterances decide within a bounded window after the hard 2000 ms floor. Only explicit
    // incomplete/addressee holds (bounded by stableRecoveryMs) may wait longer.
    // NORMAL state: an ordinary finished utterance ends within 2.0-2.5 s of the last real speech frame. Nothing in
    // the normal path (transcript finality, stability, learned patience) may push it past normalEndpointCapMs.
    completeEndpointCapMs: 2500,
    patienceCapMs: 2500,
    transcriptStabilityCapMs: 2500,
    normalEndpointCapMs: 2500,
    // HESITATION_HOLD (fillers, trailing conjunctions, unfinished clauses): bounded by stableRecoveryMs.
    // EXPLICIT_WAIT_HOLD ("wait", "hold on", "wait one minute", "ek minute ruko"): its own scoped duration.
    explicitWaitDefaultMs: 12000,
    explicitWaitMinMs: 3000,
    explicitWaitMaxMs: 180000,
    transcriptConfidence: 0.64,
    quietTranscriptConfidence: 0.48,
    baselineQuietRate: 0.045,
    baselineActiveRate: 0.014,
  });
  // ── Acoustic speech evidence ───────────────────────────────────────────────────────────────────────────────
  // Provider transcription is never speech evidence, and neither is loudness alone. A user turn needs VOICED audio:
  // pitch-periodic (70-400 Hz) signal, which vowels and voiced consonants carry and taps, clicks, rustle, breath and
  // most room noise do not. Real WT 01:11 (marker 151): an 85 ms energy blip opened a provider turn that Gemini
  // transcribed as "¿Qué?"/"encino." and answered at length. Short real words ("hi", "yes", "no", "why") carry
  // roughly 150-300 ms of voicing, so they still pass.
  const LIVE_EVIDENCE = Object.freeze({
    voicedTurnMs: 120,          // provider activity, visible user transcript, turn submission
    voicedBargeInMs: 160,       // interrupting a reply the user is hearing
    voicedContinuationMs: 160,  // re-opening a submitted turn
    voicedResumeMs: 64,         // extending an open turn after a real pause
    windowMs: 32,               // analysis window
    periodicityThreshold: 0.3,  // YIN cumulative-mean-normalized difference (lower = more periodic)
  });
  // Streaming voicing meter (YIN-style periodicity on a ~8 kHz decimation). Works with any capture frame size: samples
  // carry over between frames, and only completed windows are judged. Pure CPU (~1 M multiply-adds per second).
  const createVoicingMeter = ({ threshold = LIVE_EVIDENCE.periodicityThreshold, windowMs = LIVE_EVIDENCE.windowMs } = {}) => {
    const TARGET_RATE = 8000;
    let pending = new Float32Array(0);
    let rateKey = 0, step = 1, effRate = TARGET_RATE, win = 256, minLag = 20, maxLag = 115;
    const configure = (sampleRate) => {
      const rate = Number(sampleRate) || 16000;
      if (rate === rateKey) return;
      rateKey = rate;
      step = Math.max(1, Math.round(rate / TARGET_RATE));
      effRate = rate / step;
      win = Math.max(64, Math.round(effRate * windowMs / 1000));
      minLag = Math.max(2, Math.floor(effRate / 400));
      maxLag = Math.max(minLag + 2, Math.ceil(effRate / 70));
      pending = new Float32Array(0);
    };
    const periodic = (x, offset, minRms) => {
      const span = win + maxLag;
      let mean = 0;
      for (let i = 0; i < span; i += 1) mean += x[offset + i];
      mean /= span;
      let energy = 0;
      for (let i = 0; i < win; i += 1) { const v = x[offset + i] - mean; energy += v * v; }
      if (Math.sqrt(energy / win) < minRms || energy <= 1e-12) return false;
      let running = 0;
      let best = Infinity;
      for (let tau = 1; tau <= maxLag; tau += 1) {
        let d = 0;
        for (let i = 0; i < win; i += 1) { const v = x[offset + i] - x[offset + i + tau]; d += v * v; }
        running += d;
        if (tau >= minLag && running > 0) best = Math.min(best, (d * tau) / running);
      }
      return best < threshold;
    };
    return {
      // Voiced milliseconds among the analysis windows completed by this frame (0 for unvoiced/quiet audio).
      push(frame, sampleRate, minRms = 0) {
        configure(sampleRate);
        const n = Math.floor((frame?.length || 0) / step);
        if (!n) return 0;
        const next = new Float32Array(pending.length + n);
        next.set(pending, 0);
        for (let i = 0; i < n; i += 1) {
          let sum = 0;
          for (let k = 0; k < step; k += 1) sum += frame[i * step + k] || 0;
          next[pending.length + i] = sum / step;
        }
        let offset = 0;
        let voiced = 0;
        while (offset + win + maxLag <= next.length) {
          if (periodic(next, offset, minRms)) voiced += (win / effRate) * 1000;
          offset += win;
        }
        pending = next.slice(offset);
        return voiced;
      },
      reset() { pending = new Float32Array(0); },
    };
  };
  const adaptiveEndpointMs = (text = '', interim = false, base = LIVE_TURN_CONFIG.quietEndpointMs) => {
    const value = String(text || '').trim();
    // Text continuation cues supplement VAD/finality; they are not measured prosody.
    if (/[,…:;\-–—]$|\.{2,}$|[([{]$/u.test(value)) return 4200;
    const ending = value.replace(/[.!?。！？।]+$/u, '').toLocaleLowerCase();
    if (/(\p{L})\1{2,}\p{L}*$/u.test(ending)
        || /^(?:um+|uh+|hmm+|erm+|উম|হুম|उम्|हम्म)$/iu.test(ending)) return 4200;
    if (/(?:\b(?:if we|the thing is|what I wanted to say was|I think the problem is|wait,? I mean)|যদি আমরা|अगर हम)$/iu.test(ending)) return 4200;
    if (/(?:^|\s)(?:and|or|but|because|so|then|if|when|while|with|to|এবং|কিন্তু|কারণ|যদি|और|लेकिन|क्योंकि|अगर|y|pero|porque|et|mais|parce que)$/iu.test(ending)) return 3500;
    if (/[.!?。！？।]$/u.test(value) || (value && !interim)) return 2000;
    // No continuation cue: a normal sentence whose transcript is merely not marked final yet. Bounded (<= 2.8 s).
    return Math.min(LIVE_TURN_CONFIG.completeEndpointCapMs,
      Math.max(2000, Number(base) || 2200) + (interim ? 400 : 0) + Math.min(200, value.length * 2));
  };
  const BARGE_SUSTAIN_MS = 780;
  // AEC residual + foreground VAD is stronger evidence than transcript text.
  // This is a start-of-speech threshold, never an end-of-turn silence rule.
  const BARGE_NEAR_END_MS = 220;
  const BARGE_EXPLICIT_MS = 220;
  const normalizeBargeText = (value) => String(value || '').toLocaleLowerCase()
    .normalize('NFKC')
    .replace(/[^\p{L}\p{N}\s']/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  const explicitPattern = /^(?:(?:please\s+)?(?:stop|wait|pause|hold\s+on|listen)|no|nope|jupiter|wait\s+jupiter|stop\s+jupiter|ruk(?:o|iye)?|ruko|thehro|thahro|bas|nahi|nahin|suno|ek\s+minute|থামো|থামুন|দাঁড়াও|দাঁড়াও|না|শোনো|এক\s+মিনিট|thamo|thamun|darao|shono)(?:\b|$)/iu;
  const directedCorrectionPattern = /^(?:actually\b|no[,.]?\s+i\s+meant\b|wait[,.]?\s+explain\b|you\s+(?:got|said|heard)\b|nahi[,.]?\s+(?:mera|maine|main)\b|nahin[,.]?\s+(?:mera|maine|main)\b|আসলে\b|না[,.]?\s+আমি\b|শোনো\b)/iu;
  const socialContextPattern = /\b(?:talk|speak|chat|say\s+(?:hi|hello)|go\s+ahead|introduce|meet)\b[^.!?]{0,50}\b(?:friend|bro|sister|brother|she|he|they|her|him|them)\b|\b(?:friend|bro|sister|brother|she|he|they|her|him|them)\b[^.!?]{0,50}\b(?:talk|speak|chat|shy|say\s+(?:hi|hello)|go\s+ahead)\b/iu;
  const socialTurnPattern = /^(?:ya+h?|yeah|yep|no|nope|wait|hello|hi|hey|okay|ok|speak|speak\s+na|come\s+on|say\s+(?:hi|hello|something)|go\s+ahead|tell\s+(?:her|him|them)|why\s+(?:are|is)\s+(?:you|she|he|they)\s+(?:so\s+)?shy)(?:\b|$)/iu;
  const directedTurnPattern = /\b(?:jupiter)\b|[?]\s*$|^(?:tell|explain|show|give|help|open|find|what|why|when|where|who|how|can|could|would|will|do|is|are|please)\b/iu;
  const backgroundLabelPattern = /^(?:\[?(?:background\s+)?(?:noise|fan|room\s+hum|keyboard|mouse\s+click|chair|breathing|speaker\s+echo|distant\s+speech|traffic|horn|wind|music|tv)\]?)[.!?…\s-]*$/iu;
  const recognizerGarbagePattern = /^(?:[\W_]*|(?:uh|um|hmm|mm|ah|er|the|a|i)(?:\s+(?:uh|um|hmm|mm|ah|er|the|a|i)){2,})$/iu;

  const normalizeTurnText = (value) => String(value || '')
    .normalize('NFKC')
    .replace(/\s+/g, ' ')
    .trim();

  const hasRecentSocialContext = (messages = []) => (Array.isArray(messages) ? messages : [])
    .slice(-8)
    .some((message) => socialContextPattern.test(normalizeTurnText(message?.text ?? message?.content ?? message)));

  // Session-local aggregate only: no voice identity, text, or persistent profile.
  const observeThinkingPause = (profile, silenceMs) => {
    if (!profile || silenceMs < 2000 || silenceMs > 5000) return;
    profile.samples = Math.min(8, Number(profile.samples || 0) + 1);
    profile.pauseMs = Math.min(4000, profile.pauseMs
      ? profile.pauseMs * 0.7 + silenceMs * 0.3 : silenceMs);
  };
  const decayThinkingPause = (profile) => {
    if (!profile) return;
    profile.samples = Math.max(0, Number(profile.samples || 0) - 0.25);
    profile.pauseMs = Math.max(0, Number(profile.pauseMs || 0) * 0.9);
  };
  const liveTurnOwnsEvent = (turn, event, sessionId, requireLogicalId = false) => Boolean(
    turn && !turn.closed && !turn.discardedTemporaryInput
    && turn.sessionGeneration === sessionId
    && (!requireLogicalId || event.logical_turn_id === turn.id)
    && (!event.logical_turn_id || event.logical_turn_id === turn.id)
    && (!turn.providerTurnId || !event.provider_turn_id
      || Number(turn.providerTurnId) === Number(event.provider_turn_id))
  );
  const canReconcileLiveFinal = (turn, event, sessionId) => Boolean(
    turn && turn.userCommittedToHistory && !turn.liveSessionEnded && !turn.failureReported
    && !turn.discardedTemporaryInput && !turn.relevantInputOnly && !turn.sideSpeechHeld
    && turn.sessionGeneration === sessionId && event.final && event.logical_turn_id === turn.id
    && event.provider_turn_id && Number(event.provider_turn_id) === Number(turn.providerTurnId)
    && event.attempt_id && event.attempt_id === turn.inputTranscriptAttemptId
    && (!turn.closed || turn.responseLifecycle?.terminal === 'completed')
    && turn.localSpeech?.source === 'local_pcm_vad'
    && turn.localSpeech.sessionId === sessionId && turn.localSpeech.turnId === turn.id
    && turn.localSpeech.captureGeneration === turn.captureGeneration
    && turn.localSpeech.voicedMs >= LIVE_EVIDENCE.voicedTurnMs && turn.localSpeech.frames >= 2
    && (!turn.localSpeech.playbackActive || turn.localSpeech.bargeQualified)
  );
  const liveSpeechEvidenceValid = (turn, sessionId, now, maxAgeMs = 120000) => {
    const evidence = turn?.localSpeech;
    return Boolean(turn && !turn.closed && !turn.discardedTemporaryInput
      && turn.sessionGeneration === sessionId && evidence
      && evidence.sessionId === sessionId && evidence.turnId === turn.id
      && evidence.captureGeneration === turn.captureGeneration
      && evidence.source === 'local_pcm_vad' && evidence.voicedMs >= LIVE_EVIDENCE.voicedTurnMs
      && evidence.frames >= 2 && evidence.lastVoicedAt > 0
      && now >= evidence.lastVoicedAt && now - evidence.lastVoicedAt <= maxAgeMs
      && (!evidence.playbackActive || evidence.bargeQualified));
  };
  const liveResponseInputValid = (turn, sessionId, now, maxAgeMs) => Boolean(
    liveSpeechEvidenceValid(turn, sessionId, now, maxAgeMs)
    || (turn?.userCommittedToHistory && !turn.failureReported && turn.userText
      && liveSpeechEvidenceValid(turn, sessionId, Number(turn.localSpeech?.lastVoicedAt), maxAgeMs))
  );
  const liveAddressee = (text, { held = false, likelySide = false,
    previousAssistantText = '', socialContext = false } = {}) => {
    const value = normalizeTurnText(text);
    const context = normalizeTurnText(previousAssistantText);
    // Reported/quoted wording is discussion, not an instruction to a nearby human.
    const reported = /["“«「].+["”»」]/u.test(value)
      || /^(?:translate|quote|rewrite|what does|how (?:do|would) I say|অনুবাদ|अनुवाद)\b/iu.test(value);
    const side = !reported && /\b(?:not (?:talking|speaking) to you|talking to (?:my|the) (?:friend|brother|sister|mom|dad))\b|(?:তোমাকে বলছি না|আপনাকে বলছি না|তোমার সাথে কথা বলছি না|तुमसे नहीं|आपसे नहीं)/iu.exec(value);
    const addresses = [...value.matchAll(/(?:\bjupiter\b|জুপিটার|जुपिटर)/giu)];
    const address = addresses[addresses.length - 1];
    const clearReturn = address && side && address.index > side.index
      && /(?:[.!?।。]\s*|\b(?:anyway|now|okay|ok|back to you)[,\s]*)$/iu.test(value.slice(0, address.index));
    if (address && (!side || clearReturn)) {
      return { kind: 'direct', text: (held || side) ? value.slice(address.index) : value,
        trimmed: Boolean((held || side) && address.index > 0), reason: 'explicit-address' };
    }
    if (side) return { kind: 'side', text: value, reason: 'explicit-other-addressee' };
    const humanVocative = /^(?:(?:hey|listen)[,\s]+)?(?:mom|dad|bro|sis|মা|বাবা|ভাই|माँ|पापा|भाई)(?:[,،]|\s+(?=(?:can|could|where|give|bring|how|please)\b))/iu.test(value);
    // Physical-world deixis + an action is supporting evidence, not a blacklist.
    const physicalRequest = /^(?:(?:please|can you|could you|did you|don't|do not)\s+)?(?:give|bring|put|touch|take|pass|hold|fetch)\b[^.!?]{0,60}\b(?:that|this|charger|bag)\b|^tell (?:him|her) I(?:'ll| will)\b|^(?:ওটা|চার্জার|ব্যাগ).*(?:দাও|আনো|ধরো)|^(?:चार्जर|बैग).*(?:दो|लाओ|पकड़ो)/iu.test(value);
    const taskContext = /\b(?:translate|translation|dialogue|quote|roleplay|sentence|wording|story)\b|অনুবাদ|अनुवाद/iu.test(context);
    if (!reported && !taskContext && humanVocative) {
      return { kind: 'side', text: value, reason: 'human-vocative-request' };
    }
    // A fresh semantic request can restore interaction without a wake word.
    const semanticRequest = /^(?:(?:anyway|now|actually)[,\s]+)?(?:(?:can|could|would) you\s+)?(?:explain|summarize|continue|help|what (?:about|do you think)|tell me about)\b|^(?:ব্যাখ্যা|বুঝিয়ে|समझाओ|बताइए)/iu.test(value);
    if (held && likelySide && !semanticRequest && !reported && !taskContext) {
      return { kind: 'side', text: value, reason: 'held-likely-side' };
    }
    if (!reported && !taskContext && !semanticRequest && physicalRequest) {
      return { kind: 'ambiguous', text: value, reason: 'physical-request-uncertain' };
    }
    const direct = directedTurnPattern.test(value) || /[?？]\s*$/u.test(value) || semanticRequest;
    const shortReply = /^(?:yes|no|okay|ok|exactly|why|go|sure|right|হ্যাঁ|না|ঠিক|हाँ|नहीं|はい|いいえ|sí|oui)[.!?。！？।]*$/iu.test(value);
    const contextReply = Boolean(context && (shortReply || /[?？]/u.test(context)));
    if (direct || contextReply || reported || taskContext || socialContext) {
      return { kind: 'direct', text: value, reason: contextReply ? 'reply-to-jupiter' : 'conversation-cue' };
    }
    if (shortReply || /^(?:yeah|yes|okay|ok|হ্যাঁ|हाँ)[,\s]+(?:okay|ok|ঠিক|ठीक)[.…\s]*$/iu.test(value)) {
      return { kind: 'ambiguous', text: value, reason: 'unanchored-acknowledgement' };
    }
    // Ordinary statements remain valid: absence of a question/wake word or a
    // subject change is never sufficient evidence to suppress a user's input.
    return { kind: 'conversation', text: value, reason: 'open-conversation' };
  };

  const createLiveTurnState = (now = 0, { socialContext = false } = {}) => ({
    lastAnyAudioAt: 0,
    lastAnySpeechAt: 0,
    lastForegroundConversationAt: 0,
    lastBackgroundSpeechAt: 0,
    lastEnvironmentalNoiseAt: 0,
    candidateStartedAt: 0,
    candidateLastAt: 0,
    candidateSpeechMs: 0,
    candidateFrames: 0,
    candidateVoicedMs: 0,
    foregroundSpeechMs: 0,
    foregroundEstablished: false,
    foregroundActive: false,
    foregroundReferenceRms: 0,
    stableTranscript: '',
    stableTranscriptAt: 0,
    transcriptFinal: false,
    sideSpeechHeld: false,
    sideSpeechLikely: false,
    socialContext: Boolean(socialContext),
    createdAt: Number(now || 0),
    lastClassification: 'silence',
  });

  const observeLiveAudio = (state, sample = {}, config = LIVE_TURN_CONFIG) => {
    const now = Number(sample.now || 0);
    const frameDurationMs = Math.max(0, Number(sample.frameDurationMs || 0));
    const rms = Math.max(0, Number(sample.rms || 0));
    const noiseFloor = Math.max(0.00001, Number(sample.noiseFloor || 0.00001));
    const prominence = Number.isFinite(Number(sample.prominence))
      ? Number(sample.prominence)
      : rms / noiseFloor;
    const audible = Boolean(sample.audible ?? prominence >= 1.18);
    const speechLike = Boolean(sample.speechLike);
    const dynamic = Boolean(sample.dynamic);
    const transient = Boolean(sample.transient);
    const selfEcho = Boolean(sample.selfEcho);
    const systemAudio = Boolean(sample.systemAudio);
    const contextual = Boolean(sample.contextual || state.socialContext);
    // Voicing is optional input (legacy callers/tests omit it); when measured it is the authority for speech.
    const voicingMeasured = Number.isFinite(Number(sample.voicedMs));
    const frameVoicedMs = voicingMeasured ? Math.max(0, Number(sample.voicedMs)) : 0;
    if (voicingMeasured) state.voicingMeasured = true;
    if (audible) state.lastAnyAudioAt = now;
    if (selfEcho) {
      state.foregroundActive = false;
      state.candidateStartedAt = state.candidateSpeechMs = state.candidateFrames = state.candidateVoicedMs = 0;
      state.lastClassification = 'self_echo';
      return { classification: 'self_echo', foreground: false, keepFrame: false, startTurn: false };
    }
    if (!speechLike) {
      if (audible) state.lastEnvironmentalNoiseAt = now;
      if (state.candidateLastAt && now - state.candidateLastAt >= config.candidateResetMs) {
        state.candidateStartedAt = 0;
        state.candidateSpeechMs = 0;
        state.candidateFrames = 0;
        state.candidateVoicedMs = 0;
      }
      state.foregroundActive = false;
      state.lastClassification = audible ? 'environmental_noise' : 'silence';
      return {
        classification: state.lastClassification,
        foreground: false,
        keepFrame: state.foregroundEstablished
          && now - state.lastForegroundConversationAt <= config.foregroundHangoverMs,
        startTurn: false,
      };
    }
    state.lastAnySpeechAt = now;
    if (systemAudio) {
      state.lastBackgroundSpeechAt = now;
      state.foregroundActive = false;
      state.lastClassification = 'background_speech';
      return { classification: 'background_speech', foreground: false, keepFrame: false, startTurn: false };
    }
    const activeForegroundTurn = Boolean(state.foregroundEstablished && state.lastForegroundConversationAt);
    const sinceForeground = activeForegroundTurn ? now - state.lastForegroundConversationAt : Infinity;
    const continuousForeground = activeForegroundTurn && sinceForeground <= config.foregroundHangoverMs;
    const referenceRms = Math.max(0, Number(state.foregroundReferenceRms || 0));
    const floorRatio = activeForegroundTurn && sinceForeground <= Number(config.foregroundResumeWindowMs || 1150)
      ? Number(config.foregroundContinuationFloorRatio || 0.4)
      : Number(config.foregroundOwnershipFloorRatio || 0.68);
    const ownershipLevelMatched = !activeForegroundTurn || !referenceRms || rms >= referenceRms * floorRatio;
    const candidateInProgress = Boolean(
      state.candidateStartedAt
      && state.candidateLastAt
      && now - state.candidateLastAt <= config.candidateResetMs
    );
    // Social/situation context may help a new person begin a later turn, but it
    // must never weaken ownership of the currently open foreground turn. Once
    // a turn exists, only acoustically continuous or reference-matched speech
    // may become a continuation; generic VAD/background speech remains on the
    // separate background clock.
    const prominenceRequired = activeForegroundTurn
      ? config.continuationProminenceRatio
      : contextual ? config.contextualProminenceRatio : config.foregroundProminenceRatio;
    const candidateEvidence = !transient && prominence >= prominenceRequired
      && ownershipLevelMatched
      && (
        dynamic
        || continuousForeground
        || state.foregroundActive
        || candidateInProgress
        || (!activeForegroundTurn && state.candidateSpeechMs >= config.foregroundCandidateMs * 0.45)
      );
    if (candidateEvidence) {
      if (!state.candidateStartedAt || now - state.candidateLastAt > config.candidateResetMs) {
        state.candidateStartedAt = now;
        state.candidateSpeechMs = 0;
        state.candidateFrames = 0;
        state.candidateVoicedMs = 0;
      }
      state.candidateLastAt = now;
      state.candidateSpeechMs += frameDurationMs;
      state.candidateFrames = Number(state.candidateFrames || 0) + 1;
      state.candidateVoicedMs = Number(state.candidateVoicedMs || 0) + frameVoicedMs;
    } else if (!state.candidateLastAt || now - state.candidateLastAt >= config.candidateResetMs) {
      state.candidateStartedAt = 0;
      state.candidateSpeechMs = 0;
      state.candidateFrames = 0;
      state.candidateVoicedMs = 0;
    }
    const resumingAfterGap = activeForegroundTurn && !continuousForeground;
    const requiredMs = resumingAfterGap
      ? Number(config.foregroundSustainedResumeMs || 300)
      : activeForegroundTurn
        ? Number(config.foregroundResumeCandidateMs || 140)
        : Math.max(120, contextual ? config.contextualCandidateMs : config.foregroundCandidateMs);
    // With voicing measured, an unvoiced candidate can neither START a turn nor RESUME one after a real pause
    // (a resume that extends LISTENING must be real speech). Continuous speech inside the hangover is unaffected.
    const voicedEnough = !voicingMeasured || Number(state.candidateVoicedMs || 0)
      >= (resumingAfterGap ? LIVE_EVIDENCE.voicedResumeMs : activeForegroundTurn ? 0 : 1);
    const foreground = candidateEvidence && (
      continuousForeground
      || (state.candidateSpeechMs >= requiredMs && state.candidateFrames >= 2 && voicedEnough)
    );
    if (foreground) {
      const startTurn = !state.foregroundEstablished;
      if (!startTurn && !state.firstBurst && sinceForeground >= 1000) {
        state.firstBurst = { voicedMs: Number(state.foregroundSpeechMs || 0) + Number(state.firstCandidateMs || 0),
          gapMs: sinceForeground };
      }
      if (startTurn) state.firstCandidateMs = Number(state.candidateSpeechMs || 0);
      // The pause lasted until the resumed speech STARTED (candidate onset), not until it was confirmed.
      const pauseMs = Number(state.candidateStartedAt || now) - Number(state.lastForegroundConversationAt || 0);
      if (!startTurn && !state.foregroundActive && pauseMs >= 2000) {
        observeThinkingPause(state.pauseProfile, pauseMs);
      }
      if (startTurn) state.turnStartedAt = now;
      state.foregroundEstablished = true;
      state.foregroundActive = true;
      state.lastForegroundConversationAt = now;
      state.foregroundSpeechMs += frameDurationMs;
      state.foregroundReferenceRms = state.foregroundReferenceRms
        ? state.foregroundReferenceRms * 0.86 + rms * 0.14
        : rms;
      state.lastClassification = 'foreground_speech';
      return { classification: 'foreground_speech', foreground: true, keepFrame: true, startTurn };
    }
    state.lastBackgroundSpeechAt = now;
    state.foregroundActive = false;
    state.lastClassification = 'background_speech';
    return {
      classification: 'background_speech',
      foreground: false,
      keepFrame: state.foregroundEstablished
        && now - state.lastForegroundConversationAt <= config.foregroundHangoverMs,
      startTurn: false,
    };
  };

  const classifyLiveTranscript = ({
    text = '', confidence = null, final = false, acousticProminent = false,
    socialContext = false, transcriptOnly = false, selfEcho = false,
  } = {}, config = LIVE_TURN_CONFIG) => {
    const normalized = normalizeTurnText(text);
    if (!normalized) return { classification: 'empty', foreground: false, text: '' };
    if (selfEcho) return { classification: 'self_echo', foreground: false, text: normalized };
    if (backgroundLabelPattern.test(normalized)) {
      return { classification: 'environmental_noise', foreground: false, text: normalized };
    }
    if (recognizerGarbagePattern.test(normalized)) {
      return { classification: 'recognizer_garbage', foreground: false, text: normalized };
    }
    const numericConfidence = Number(confidence);
    const hasConfidence = confidence != null && Number.isFinite(numericConfidence);
    const reliable = hasConfidence ? numericConfidence >= config.transcriptConfidence : Boolean(final);
    const quietReliable = hasConfidence ? numericConfidence >= config.quietTranscriptConfidence : Boolean(final);
    const socialTurn = Boolean(socialContext && socialTurnPattern.test(normalized));
    const directedTurn = directedTurnPattern.test(normalized);
    const words = normalized.match(/[\p{L}\p{N}][\p{L}\p{N}'’-]*/gu) || [];
    const coherent = words.length >= 2 || socialTurn || directedTurn || /^(?:hi|hello|yeah|yes|no|wait|why|okay|ok)$/iu.test(normalized);
    const transcriptOnlyEvidence = transcriptOnly && coherent && quietReliable && (
      socialTurn
      || directedTurn
      || (final && reliable)
      || (hasConfidence && numericConfidence >= 0.82)
    );
    // With an acoustic stream available, text content cannot promote itself to
    // foreground. Social/directed wording is only a fallback when there is no
    // acoustic classifier at all; delayed partial/final packets never own the
    // active foreground clock.
    const foreground = coherent && quietReliable && (
      acousticProminent || transcriptOnlyEvidence
    );
    return {
      classification: foreground ? 'foreground_speech' : 'background_speech',
      foreground,
      socialTurn,
      directedTurn,
      text: normalized,
    };
  };

  const observeLiveTranscript = (state, evidence = {}, config = LIVE_TURN_CONFIG) => {
    const decision = classifyLiveTranscript({ ...evidence, socialContext: evidence.socialContext ?? state.socialContext }, config);
    const now = Number(evidence.now || 0);
    if (decision.foreground) {
      // Transcript delivery is not acoustic speech evidence.  In particular,
      // delayed/cumulative recognizer and provider results must never move the
      // foreground-silence clock or keep a quiet turn open.  The audio VAD is
      // the sole writer of foregroundEstablished/lastForegroundConversationAt.
      if (state.stableTranscript !== decision.text) state.stableTranscriptAt = now;
      state.stableTranscript = decision.text;
      state.transcriptFinal = Boolean(evidence.final);
      state.lastClassification = 'transcript_foreground';
    } else if (decision.classification === 'background_speech') {
      state.lastBackgroundSpeechAt = now;
      state.lastClassification = 'background_speech';
    } else if (decision.classification === 'environmental_noise') {
      state.lastEnvironmentalNoiseAt = now;
      state.lastClassification = 'environmental_noise';
    }
    return decision;
  };

  // ── Endpoint intent: deterministic, local, no model call. Structure (clause position, imperative form, question
  // shape, tense) decides; a substring alone never does ("I waited one minute" / "How long should I wait?" are not
  // commands). Multilingual forms cover the languages Jupiter's Live session already handles (en, Hinglish, hi, bn).
  const NUMBER_WORDS = Object.freeze({ a: 1, an: 1, one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7,
    eight: 8, nine: 9, ten: 10, fifteen: 15, twenty: 20, thirty: 30, forty: 40, fifty: 50, sixty: 60, ninety: 90,
    couple: 2, few: 3, ek: 1, do: 2, teen: 3, char: 4, chaar: 4, paanch: 5, panch: 5, das: 10, bees: 20, tees: 30,
    'एक': 1, 'दो': 2, 'तीन': 3, 'पांच': 5, 'पाँच': 5, 'दस': 10, 'এক': 1, 'দুই': 2, 'তিন': 3, 'পাঁচ': 5, 'দশ': 10 });
  const NUM = String.raw`(?:\d{1,3}|a|an|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|forty|fifty|sixty|ninety|a couple of|couple of|a few|few|half a|ek|do|teen|chaa?r|paa?nch|das|bees|tees|एक|दो|तीन|पा(?:ं|ँ)च|दस|এক|দুই|তিন|পাঁচ|দশ)`;
  const UNIT = String.raw`(?:sec(?:ond)?s?|min(?:ute)?s?|moment|pal|सेकंड|मिनट|पल|সেকেন্ড|মিনিট)`;
  const LEAD = String.raw`(?:(?:um+|uh+|h+m+|er+m*|okay|ok|so|jupiter|please|haan|acha+|accha+|arre|oh|and|but|actually|just|hey|aur|जुपिटर|জুপিটার)[\s,]+)*`;
  const WAIT_COMMAND = new RegExp(String.raw`^${LEAD}(?:`
    + String.raw`(?:please\s+)?(?:wait|hold on|hang on|hold up)(?:\s+a\s+(?:little|bit))?(?:\s+for\s+me)?(?:\s+(?:for\s+)?(${NUM})\s*(${UNIT}))?(?:\s+(?:please|na))?`
    + String.raw`|(?:give me|gimme)\s+(?:(${NUM})\s*)?(${UNIT})`
    + String.raw`|(?:just\s+)?(a|one)\s+(sec(?:ond)?|moment|minute)(?:\s+please)?`
    + String.raw`|(?:(${NUM})\s*(${UNIT})\s+)?(?:ruko|ruk\s+jao|rukiye|rukna|ruk|wait\s+kar(?:o|na|iye)?|wait\s+karo)`
    + String.raw`|(?:thoda|zara)\s+(?:wait|ruk(?:o|na)?)(?:\s+kar(?:o|na|iye)?)?`
    + String.raw`|(ek)\s+(second|sec|minute|min|pal)`
    + String.raw`|(?:(${NUM})\s*(${UNIT})\s+)?(?:रुको|रुकिए|रुक\s+जाओ)|(?:थोड़ा|ज़रा)\s+रुको|(एक)\s+(मिनट|सेकंड|पल)`
    + String.raw`|(?:(${NUM})\s*(${UNIT})\s+)?(?:দাঁড়াও|দাঁড়ান|অপেক্ষা\s+করো)|একটু\s+দাঁড়াও|(এক)\s+(মিনিট|সেকেন্ড)`
    + String.raw`)$`, 'iu');
  const QUESTION_START = /^(?:how|what|why|when|where|who|which|should|would|could|can|do|does|did|is|are|was|were|will|shall|kya|kitna|kitni|kab|kyun|কত|কখন|কেন|क्या|कितना|कब|क्यों)\b/iu;
  const JUSTIFICATION = /^(?:i\s+(?:need|have|want|got|will|'ll|am|'m)|i'll|i'm|let me|i\s+need|mujhe|main|mai|मुझे|मैं|আমি|আমার)\b/iu;
  const HESITATION_SOUND = /^(?:u+[hm]+|a+h*|a{2,}|e+r+m*|h+m+|m{2,}|um+|uh+|haa+n|উম+|হুম+|উ+|आ+|उम्+|हम्म+)$/iu;
  const HESITATION_TAIL = /(?:^|\s)(?:like|so|and|aur|matlab|toh|phir|yaani|ki|मतलब|और|तो|फिर|মানে|আর|তো)$/iu;
  const HESITATION_PHRASE = /(?:let me (?:think|see|check)|how do i (?:say|put) (?:it|this)|what'?s the word|soch(?:ne)? do|sochta hoon|सोचने दो|ভাবতে দাও)$/iu;
  const clausesOf = (text) => String(text || '').split(/(?:[.!?।。！？;]+|,|\u2026|\.{2,})/u).map((part) => part.trim()).filter(Boolean);
  const meaningfulWords = (text) => (String(text || '').match(/[\p{L}\p{N}][\p{L}\p{N}'’-]*/gu) || [])
    .filter((word) => !HESITATION_SOUND.test(word) && !/^(?:okay|ok|so|jupiter|please|haan|acha|accha|hey|oh|just|and|aur)$/iu.test(word));
  const numberValue = (raw = '') => {
    const value = String(raw || '').trim().toLocaleLowerCase();
    if (!value) return null;
    if (/^\d+$/.test(value)) return Number(value);
    if (/couple/.test(value)) return 2;
    if (/few/.test(value)) return 3;
    if (/half/.test(value)) return 0.5;
    return NUMBER_WORDS[value] ?? null;
  };
  const waitDurationMs = (match, config = LIVE_TURN_CONFIG) => {
    const groups = match.slice(1);
    let amount = null;
    let unit = '';
    for (let index = 0; index < groups.length; index += 1) {
      if (groups[index] && /^(?:sec|min|moment|pal|second|सेकंड|मिनट|पल|সেকেন্ড|মিনিট)/iu.test(groups[index])) {
        unit = groups[index].toLocaleLowerCase();
        amount = numberValue(groups[index - 1]);
        break;
      }
    }
    let ms = config.explicitWaitDefaultMs;
    if (/^(?:min|मिनट|মিনিট)/u.test(unit)) ms = Math.round((amount ?? 1) * 60000);
    else if (/^(?:sec|सेकंड|সেকেন্ড)/u.test(unit) && amount !== null && amount >= 5) ms = Math.round(amount * 1000);
    // "one second" / "a moment" / "ek pal" are figurative: they mean "a moment", not literally one second.
    return Math.min(config.explicitWaitMaxMs, Math.max(config.explicitWaitMinMs, ms));
  };
  // explicit_wait (with duration + whether a real request came before it), hesitation, or normal.
  const endpointIntent = (text = '', config = LIVE_TURN_CONFIG) => {
    const value = normalizeTurnText(text);
    if (!value) return { kind: 'normal' };
    const clauses = clausesOf(value);
    for (let index = clauses.length - 1; index >= 0 && index >= clauses.length - 2; index -= 1) {
      const clause = clauses[index].replace(/[.!?।…]+$/u, '');
      const match = WAIT_COMMAND.exec(clause);
      if (!match) continue;
      const after = clauses.slice(index + 1).join(' ');
      // A question or new request after "wait" means the user kept going; only a short justification may follow.
      if (after && (/[?？]/u.test(value.slice(value.indexOf(after))) || QUESTION_START.test(after)
        || !JUSTIFICATION.test(after) || meaningfulWords(after).length > 8)) break;
      const tail = value.slice(value.lastIndexOf(clauses[index]) + clauses[index].length);
      if (/^\s*[?？]/u.test(tail) || QUESTION_START.test(clause)) break;
      const prior = clauses.slice(0, index).join(' ');
      return { kind: 'explicit_wait', durationMs: waitDurationMs(match, config),
        hasPrior: meaningfulWords(prior).length >= 3, key: `${index}:${clause.toLocaleLowerCase()}` };
    }
    const ending = value.replace(/[.!?。！？।]+$/u, '').trim();
    const lastWord = (ending.match(/[\p{L}\p{N}'’-]+$/u) || [''])[0];
    const terminal = /[.!?。！？।]$/u.test(value) && !/(?:\.{2,}|…)$/u.test(value);
    // Marker 217 (real WT 25 Oct: "Jupiter, can you open." was cut off; the user was still saying "the settings"):
    // a command verb with nothing after it is waiting for its object; held, and answered (never dropped) if no more comes.
    if (AWAITS_OBJECT.test(ending)) return { kind: 'hesitation', meaningful: true, awaitsObject: true };
    if (liveTurnIncomplete(value) || HESITATION_SOUND.test(lastWord) || HESITATION_PHRASE.test(ending)
      || (!terminal && HESITATION_TAIL.test(ending))) {
      return { kind: 'hesitation', meaningful: meaningfulWords(value).length >= 3 };
    }
    return { kind: 'normal' };
  };
  // ── Transcript language stability ────────────────────────────────────────────────────────────────────────────
  // A specific-language hint from script or unambiguous function words. 'latin' = cannot tell (English, Hinglish,
  // romanised speech, names): such text is NEVER stripped. Words shared across languages ("no", "si") are excluded.
  const LANGUAGE_MARKERS = [
    ['es', /[¿¡ñ]|(?:^|[^\p{L}])(?:qué|pasó|hola|gracias|cómo|dónde|está|también|señor|vale|claro|bueno|perdón)(?:$|[^\p{L}])/iu],
    ['fr', /[ç]|(?:^|[^\p{L}])(?:bonjour|merci|oui|salut|pourquoi|comment|très|c'est|ça)(?:$|[^\p{L}])/iu],
    ['de', /[ß]|(?:^|[^\p{L}])(?:hallo|danke|bitte|nein|warum|nicht|genau)(?:$|[^\p{L}])/iu],
    ['pt', /[ãõ]|(?:^|[^\p{L}])(?:obrigad[oa]|olá|você|não)(?:$|[^\p{L}])/iu],
    ['hi', /[\u0900-\u097F]/u], ['bn', /[\u0980-\u09FF]/u], ['ja', /[\u3040-\u30FF]/u],
    ['zh', /[\u4E00-\u9FFF]/u], ['ar', /[\u0600-\u06FF]/u], ['ru', /[\u0400-\u04FF]/u],
  ];
  const languageHintOf = (text = '') => {
    const value = String(text || '');
    for (const [lang, pattern] of LANGUAGE_MARKERS) if (pattern.test(value)) return lang;
    return /\p{L}/u.test(value) ? 'latin' : '';
  };
  // A LEADING fragment the provider emitted without real acoustic support (a blip/noise burst, e.g. the real WT
  // "¿Qué?" before "Hello hello.") that is short AND in a specific language different from the rest of the turn AND
  // never used by this user earlier in the session is a recognition hypothesis, not speech. Genuine code-switching
  // ("Bonjour, how are you?", "Hello, tum kaisa ho?", "¿Qué pasó? I don't understand.") has acoustic support or is
  // not short, so it is kept. Nothing is stripped on text alone (weak = acoustic evidence decided by the caller).
  const unstableLeadingFragment = (fragment = '', rest = '', { weak = false, sessionLanguages = [] } = {}) => {
    if (!weak) return false;
    const words = String(fragment).match(/[\p{L}\p{N}]+/gu) || [];
    const restWords = String(rest).match(/[\p{L}\p{N}]+/gu) || [];
    if (!words.length || words.length > 2 || restWords.length < 2) return false;
    const hint = languageHintOf(fragment);
    if (!hint || hint === 'latin' || hint === languageHintOf(rest)) return false;
    return !(sessionLanguages || []).includes(hint);
  };
  const splitLeadingClause = (text = '') => {
    const match = /^\s*([¿¡]?[^.!?,;…¿¡]{1,32}[.!?,;…]+)\s*([\s\S]*)$/u.exec(String(text || ''));
    return match ? [match[1].trim(), match[2].trim()] : ['', String(text || '').trim()];
  };
  // Marker 215 (real WT 24-25 Oct: background sound came out as "¿Qué?" turns again and again in the transcript and the
  // saved chat): the Live transcriber guesses a language for every sound (a language list is rejected by the Gemini
  // API), so short Spanish / Portuguese artefacts in an English / Hindi / Bengali household are noise.
  // Noise = a short line made (mostly) of such words; real code-switching ("¿Qué pasó? I don't understand.") stays.
  const ASR_NOISE_WORD = /^(?:qu[eé]|c[oó]mo|hoy|d[ií]a|es|pode|ir|banheiro|obrigad[oa]|gracias|hola|d[oó]nde|est[aá]s?|voc[eê]|isso|rinc[oó]n|vamos|bueno|mira|pasa)$/iu;
  const isAsrNoise = (text = '') => {
    const words = String(text || '').toLocaleLowerCase().split(/\s+/u)
      .map((word) => word.replace(/^[¿¡"'“(]+|[?!.,"'”)…]+$/gu, '')).filter(Boolean);
    if (!words.length || words.length > 6) return false;
    const noisy = words.filter((word) => ASR_NOISE_WORD.test(word)).length;
    return noisy / words.length >= 0.6;
  };
  const AWAITS_OBJECT = /(?:^|\s)(?:open|play|close|launch|start|search|search\s+for|look\s+up|find|show\s+me|tell\s+me\s+about|type|write|call|message|send|maximi[sz]e|minimi[sz]e|go\s+to|switch\s+to|turn\s+(?:on|off)|set|increase|decrease)(?:\s+(?:the|a|an|my))?$/iu;
  const liveTurnIncomplete = (text = '') => {
    const value = normalizeTurnText(text);
    if (!value) return false; // PCM-only input still needs transcription first.
    if (/(?:\.{2,}|…|[,;:\-–—]|[([{])$/u.test(value)) return true;
    const ending = value.replace(/[.!?。！？।]+$/u, '').toLocaleLowerCase();
    return /(?:^|\s)(?:a|an|the|if|because|and|or|but|then|to|with|of|suppose|যদি|কিন্তু|কারণ|अगर|लेकिन|क्योंकि)$/iu.test(ending)
      || /^(?:if i|wait[, ]+i|let me|the reason is|so if|i was going to|u+m+|u+h+|h+m+)$/iu.test(ending);
  };
  const liveTurnEndpointDecision = (state, now, config = LIVE_TURN_CONFIG) => {
    const current = Number(now || 0);
    if (!state?.foregroundEstablished || !state.lastForegroundConversationAt) {
      return { finalize: false, reason: 'no-foreground-turn' };
    }
    const sinceForeground = current - state.lastForegroundConversationAt;
    const text = String(state.stableTranscript || '').trim();
    // Marker 214: a turn that never pauses (music with vocals, a TV) is cut at maxTurnMs; the server keeps overheard
    // speech and lyrics silent, so this never makes Jupiter answer a song.
    // (only while the sound is still continuous: a deliberate "wait one minute" hold is quiet and keeps its time)
    if (state.turnStartedAt && current - state.turnStartedAt >= Number(config.maxTurnMs || 25000)
      && sinceForeground < Number(config.minimumSilenceMs || 2000)) {
      return text ? { finalize: true, reason: 'max-turn-length' }
        : { finalize: false, discard: true, reason: 'max-turn-length-empty' };
    }
    const stableFor = current - Number(state.stableTranscriptAt || state.lastForegroundConversationAt);
    const frameGap = state.lastFrameAt ? current - state.lastFrameAt : 0;
    const recoveryMs = Math.max(2000, config.stableRecoveryMs || 6000);
    // DECISION PRECEDENCE: real speech (active / resume building) > EXPLICIT_WAIT_HOLD > HESITATION_HOLD >
    // addressee hold > NORMAL endpoint (2.0-2.5 s) > abandoned-turn cleanup. Every path obeys the hard floor.
    // Real speech ends an explicit hold immediately (checked before the floor, which restarts with the speech).
    const priorWait = state.explicitWait;
    if (priorWait && state.lastForegroundConversationAt > priorWait.startedAt) {
      // Real speech resumed during the hold: the wait is over, the SAME turn simply continues.
      state.explicitWait = null;
      state.explicitWaitCancelledKey = priorWait.key;
      state.explicitWaitEvent = { type: 'cancelled_by_speech', durationMs: priorWait.durationMs };
    }
    if (sinceForeground < 2000) return { finalize: false, reason: 'silence-floor', waitMs: 2000 - sinceForeground };
    if (state.candidateStartedAt && state.candidateSpeechMs > 0 && state.candidateLastAt
      && current - state.candidateLastAt <= config.candidateResetMs
      && current - state.candidateStartedAt <= Number(config.foregroundSustainedResumeMs || 300) + 200
      // Only a VOICED candidate may hold the decision (noise held real WT turn 2 in LISTENING for 3.4 s), and even a
      // voiced non-speech sound can extend the normal cap by at most ~600 ms before the endpoint is taken.
      && (!state.voicingMeasured || Number(state.candidateVoicedMs || 0) > 0)
      && sinceForeground < Number(config.normalEndpointCapMs || 2500) + 600) {
      return { finalize: false, reason: 'resume-candidate', waitMs: 120 };
    }
    const intent = endpointIntent(text, config);
    if (intent.kind === 'explicit_wait' && intent.key !== state.explicitWaitCancelledKey) {
      if (!state.explicitWait || state.explicitWait.key !== intent.key) {
        state.explicitWait = { key: intent.key, durationMs: intent.durationMs, hasPrior: intent.hasPrior,
          startedAt: state.lastForegroundConversationAt };
        state.explicitWaitEvent = { type: 'started', durationMs: intent.durationMs };
      }
      const wait = state.explicitWait;
      const remaining = wait.startedAt + wait.durationMs - current;
      if (remaining > 0) {
        return { finalize: false, reason: 'explicit-wait', waitMs: Math.min(remaining, 1000), holdState: 'explicit_wait',
          remainingMs: remaining, durationMs: wait.durationMs };
      }
      state.explicitWaitEvent = { type: 'expired', durationMs: wait.durationMs };
      // Expired with a real request before it: answer that request. "Wait" alone: no fake query, just stop.
      return wait.hasPrior ? { finalize: true, reason: 'explicit-wait-expired' }
        : { finalize: false, discard: true, reason: 'explicit-wait-expired-empty' };
    }
    if (intent.kind === 'hesitation' && intent.awaitsObject) {
      const holdMs = Number(config.awaitsObjectHoldMs || 4000);
      if (sinceForeground < holdMs) {
        return { finalize: false, reason: 'awaits-object-hold', holdState: 'hesitation', waitMs: holdMs - sinceForeground };
      }
      return { finalize: true, reason: 'awaits-object-expired' };       // Jupiter asks what to open; nothing is lost
    }
    if (intent.kind === 'hesitation') {
      if (sinceForeground < recoveryMs) {
        return { finalize: false, reason: 'hesitation-hold', holdState: 'hesitation', waitMs: recoveryMs - sinceForeground };
      }
      // Bounded: an unfinished clause or a bare filler is abandoned (existing ~6 s rule); a real request that merely
      // trailed off ("...can you tell me the weather, umm") is answered.
      return !intent.meaningful || liveTurnIncomplete(text)
        ? { finalize: false, discard: true, reason: 'abandoned-incomplete-input' }
        : { finalize: true, reason: 'hesitation-expired' };
    }
    const addressee = liveAddressee(text, { held: state.sideSpeechHeld,
      likelySide: state.sideSpeechLikely, previousAssistantText: state.previousAssistantText,
      socialContext: state.socialContext });
    if (['side', 'ambiguous'].includes(addressee.kind)) {
      // Uncertainty alone must not delete normal input. At the bounded quiet
      // limit use the existing stable-transcript path, reserving discard for
      // sufficiently strong side-speech evidence only.
      if (sinceForeground >= recoveryMs && addressee.kind === 'side') {
        return { finalize: false, discard: true, reason: 'abandoned-side-input' };
      }
      if (sinceForeground < recoveryMs) return { finalize: false, reason: 'addressee-hold', waitMs: recoveryMs - sinceForeground };
    }
    if (frameGap > 900) {
      // Missing capture is a fault, never normal silence. Only an already
      // stable transcript can survive it; the host separately reacquires input.
      return { finalize: Boolean(text && stableFor >= 2000 && sinceForeground >= recoveryMs),
        reason: text && stableFor >= 2000 && sinceForeground >= recoveryMs
          ? 'fallback_stable_transcript' : 'capture-gap', recoverCapture: true };
    }
    if (state.foregroundActive && sinceForeground < 300) {
      return { finalize: false, reason: 'foreground-active', waitMs: 300 };
    }
    if (text && stableFor >= 1200 && sinceForeground >= recoveryMs) {
      return { finalize: true, reason: 'fallback_stable_transcript' };
    }
    // PCM-only STT cannot expose text before transcription. Its quiet interval
    // adapts to voiced duration, and still requires an established foreground
    // turn plus inactive VAD. It does not manufacture a transcript.
    // NORMAL: hesitation/incomplete cues were handled above, so this is a finished-looking utterance.
    let requiredQuiet = Math.min(Number(config.normalEndpointCapMs || 2500), text
      ? adaptiveEndpointMs(text, !state.transcriptFinal, config.quietEndpointMs)
      : 2300 + Math.min(200, Number(state.foregroundSpeechMs || 0) * 0.12));
    // Repeated confirmed resumes increase patience only for uncertain endings;
    // a clear final/short answer still commits at the hard floor.
    if (requiredQuiet > 2000 && Number(state.pauseProfile?.samples || 0) >= 2) {
      requiredQuiet = Math.min(Math.min(recoveryMs, Number(config.patienceCapMs || 2500)), Math.max(requiredQuiet,
        Number(state.pauseProfile.pauseMs || 0) + 650));
    }
    // Late provider transcript deltas may briefly defer a decision, never beyond the bounded stability cap: the
    // transcript is not speech evidence and must not keep a finished turn in LISTENING.
    const stableRequired = state.transcriptFinal || sinceForeground >= Number(config.transcriptStabilityCapMs || 2500) ? 0 : 500;
    const waitMs = Math.max(requiredQuiet - sinceForeground, text ? stableRequired - stableFor : 0);
    if (waitMs <= 0) return { finalize: true,
      reason: text && /[.!?。！？।]$/u.test(text) && !/[.…]{2,}$/u.test(text)
        ? 'high_confidence_completion' : state.transcriptFinal ? 'provider_final' : 'adaptive_silence' };
    return { finalize: false, reason: 'endpoint-candidate', waitMs: Math.max(1, waitMs) };
  };

  // One logical response, replaceable attempts. IDs are ownership, never text.
  const beginResponseAttempt = (turn, type, now, providerId = '') => {
    if (!turn || turn.closed || turn.failureReported) return null;
    const response = turn.responseLifecycle ||= { serial: 0, current: null, terminal: '' };
    if (response.terminal) return null;
    if (providerId && response.current?.providerId === providerId) return response.current;
    if (response.current) response.current.cancelled = true;
    const attempt = { id: `${turn.sessionGeneration}:${turn.id}:${++response.serial}`,
      type, providerId, startedAt: now, lastProgressAt: now, firstOutput: false };
    response.current = attempt;
    return attempt;
  };
  const ownsResponseAttempt = (turn, attempt) => Boolean(attempt && !attempt.cancelled
    && !turn?.closed && !turn?.responseLifecycle?.terminal && turn.responseLifecycle.current === attempt);
  const finishResponseAttempt = (turn, attempt, reason) => {
    if (!ownsResponseAttempt(turn, attempt)) return false;
    attempt.finished = true;
    turn.responseLifecycle.terminal = reason;
    turn.responseLifecycle.current = null;
    return true;
  };
  const generationProgressDecision = (request, now) => {
    if (!request || request.finished || request.cancelled) return { stale: false };
    const progress = Number(request.lastProgressAt ?? request.startedAt);
    const limit = request.firstOutput ? (request.progressTimeoutMs || 45000) : (request.firstOutputTimeoutMs || 60000);
    return { stale: Number(now) - progress >= limit,
      reason: request.firstOutput ? 'progress_timeout' : 'first_output_timeout' };
  };

  const backchannelEligible = (state, now, lastAt = 0) => Boolean(
    state?.foregroundActive && state.foregroundSpeechMs >= 18000
    && now - state.lastForegroundConversationAt < 300
    && (!lastAt || now - lastAt >= 45000) && !state.backchannelUsed
  );

  const evaluateBargeInCandidate = ({
    text = '', durationMs = 0, meaningfulSpeechMs = durationMs, isFinal = false,
    confidence = null, selfEcho = false, foregroundEvidence = false,
    backgroundSpeech = false, environmental = false, systemAudio = false,
    acousticAvailable = false, nearEndConfirmed = false, voicedMs = null
  } = {}) => {
    const normalized = normalizeBargeText(text);
    if (selfEcho) return { decision: 'reject', reason: 'self-echo' };
    if (acousticAvailable && voicedMs != null && Number(voicedMs) < LIVE_EVIDENCE.voicedBargeInMs) {
      return { decision: 'pending', reason: 'awaiting-voiced-speech' };
    }
    if (systemAudio) return { decision: 'reject', reason: 'system-audio' };
    if (environmental) return { decision: 'reject', reason: 'environmental-noise' };
    if (backgroundSpeech) return { decision: 'reject', reason: 'background-speech' };
    const explicit = explicitPattern.test(normalized);
    const directed = directedCorrectionPattern.test(normalized);
    const words = normalized.split(/\s+/).filter(Boolean);
    const reliable = confidence == null || !Number.isFinite(Number(confidence)) || Number(confidence) >= 0.68 || isFinal;
    if (explicit && reliable && meaningfulSpeechMs >= BARGE_EXPLICIT_MS
      && (!acousticAvailable || foregroundEvidence)) {
      return { decision: 'accept', reason: 'explicit-interruption', explicit: true };
    }
    if (directed && reliable && meaningfulSpeechMs >= 420
      && (!acousticAvailable || foregroundEvidence)) {
      return { decision: 'accept', reason: 'directed-correction' };
    }
    if (!normalized) {
      return foregroundEvidence && meaningfulSpeechMs >= (nearEndConfirmed ? BARGE_NEAR_END_MS : BARGE_SUSTAIN_MS)
        ? { decision: 'accept', reason: 'sustained-voiced-audio' }
        : { decision: 'pending', reason: 'awaiting-sustained-speech' };
    }
    if (words.length === 1) {
      return isFinal ? { decision: 'reject', reason: 'unrelated-short-fragment' } : { decision: 'pending', reason: 'short-fragment' };
    }
    if (meaningfulSpeechMs >= BARGE_SUSTAIN_MS && reliable && (!acousticAvailable || foregroundEvidence)) {
      return { decision: 'accept', reason: 'sustained-recognized-utterance' };
    }
    if (isFinal && meaningfulSpeechMs < BARGE_SUSTAIN_MS) {
      return { decision: 'reject', reason: 'too-short' };
    }
    return { decision: 'pending', reason: 'collecting-evidence' };
  };

  // Compare shape, not digital output loudness: speaker gain and microphone
  // gain are unrelated. Bounded sparse correlation covers acoustic delay.
  const playbackEchoCorrelation = (frame, rate, chunks, endTime) => {
    if (!frame?.length || !chunks?.length || !rate) return 0;
    chunks = chunks.filter(c => c.startsAt < endTime && c.endsAt > endTime - frame.length / rate - 0.24);
    const stride = Math.max(1, Math.floor(frame.length / 64));
    let best = 0;
    for (let delay = 0; delay <= 0.24; delay += 0.002) {
      let dot = 0, inputEnergy = 0, outputEnergy = 0, count = 0;
      for (let i = 0; i < frame.length; i += stride) {
        const time = endTime - (frame.length - i) / rate - delay;
        const chunk = chunks.find(c => time >= c.startsAt && time < c.endsAt);
        if (!chunk) continue;
        const x = frame[i], y = chunk.samples[Math.round((time - chunk.startsAt) * chunk.rate)] || 0;
        dot += x * y; inputEnergy += x * x; outputEnergy += y * y; count++;
      }
      if (count >= 32 && inputEnergy > 1e-9 && outputEnergy > 1e-9)
        best = Math.max(best, Math.abs(dot) / Math.sqrt(inputEnergy * outputEnergy));
    }
    return best;
  };

  const isLiveStopControl = (text) => /^(?:(?:please\s+)?(?:stop(?:\s+it)?|wait|hold\s+on|enough|pause)|থামো|থামুন|দাঁড়াও|দাঁড়াও|बस|रुको|रुकिए|থাম|बस\s+करो|basta|para|arrête|stoppe)(?:[\s,.!।?—-]+(?:(?:please\s+)?(?:stop(?:\s+it)?|wait|hold\s+on|enough|pause)|থামো|থামুন|रुको|बस))*[\s.!।?]*$/iu.test(String(text || '').trim().replace(/^(?:(?:yeah|yes|okay|ok)[\s,.!—-]+)+/iu, ''));

  return Object.freeze({
    LIVE_TURN_CONFIG,
    LIVE_EVIDENCE,
    createVoicingMeter,
    adaptiveEndpointMs,
    liveAddressee,
    observeThinkingPause,
    decayThinkingPause,
    liveTurnOwnsEvent,
    canReconcileLiveFinal,
    playbackEchoCorrelation,
    isLiveStopControl,
    liveSpeechEvidenceValid,
    liveResponseInputValid,
    generationProgressDecision,
    beginResponseAttempt,
    ownsResponseAttempt,
    finishResponseAttempt,
    backchannelEligible,
    BARGE_SUSTAIN_MS,
    BARGE_EXPLICIT_MS,
    normalizeBargeText,
    normalizeTurnText,
    hasRecentSocialContext,
    createLiveTurnState,
    observeLiveAudio,
    classifyLiveTranscript,
    observeLiveTranscript,
    liveTurnEndpointDecision,
    endpointIntent,
    isAsrNoise,
    languageHintOf,
    unstableLeadingFragment,
    splitLeadingClause,
    liveTurnIncomplete,
    AWAITS_OBJECT,
    evaluateBargeInCandidate
  });
});
