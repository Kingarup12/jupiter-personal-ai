(function (root) {
  'use strict';
  // Pure intent/routing helpers for computer use (typed chat, Live voice and Live video share ONE classifier).
  // No DOM, no network: app.js decides what to run; these only decide WHAT the user explicitly asked for.

  const norm = (text) => String(text || '').replace(/\s+/g, ' ').trim();
  const lower = (text) => norm(text).toLowerCase();

  // Explicit computer-use requests only. Ordinary chat ("open a discussion about…") must never start control.
  const DEVICE = '(?:laptop|computer|pc|desktop)';
  const ACTIONS = '(?:type|write|enter|click|press|select|scroll|fill|search|find|open|close|save|paste|change|set)';
  const CONTROL_PATTERNS = [
    new RegExp(`\\b(?:control|use|operate)\\s+(?:my|the)\\s+${DEVICE}\\b`, 'i'),
    new RegExp(`\\bon\\s+(?:my|the)\\s+${DEVICE}\\b`, 'i'),
    /\b(?:start|turn on|enable)\s+computer[- ]use(?:\s+mode)?\b/i,
    new RegExp(`\\b(?:open|switch to|go to|bring up)\\s+(?:the\\s+)?([\\w .+-]{2,40}?)\\s+(?:app\\s+)?and\\s+(?:(?:teach me how to|show me how to)\\s+)?${ACTIONS}\\b`, 'i'),
    new RegExp(`\\bin\\s+(?:the\\s+)?([\\w .+-]{2,40}?)(?:\\s+app|\\s+window)?\\s*,?\\s+${ACTIONS}\\b`, 'i')
  ];
  const STOP_PATTERN = /\b(?:stop|end|exit|cancel|turn off|disable)\s+(?:the\s+)?(?:computer[- ]use(?:\s+mode)?|laptop control|controlling(?:\s+my\s+(?:laptop|computer|pc))?)\b/i;
  const CURRENT_INFO = /\b(?:latest|newest|today'?s?|current(?:ly)?|right now|recent|breaking|news|update|updates|score|scores|price|prices|release|released|live|trending|this week)\b/i;
  const SHOW_VERBS = /\b(?:show me|open|pull up|look up|search(?: for)?|find|check|what(?:'s| is| are)|who(?:'s| is)|when (?:is|does|did)|how much)\b/i;
  const ANSWER_ONLY = /\b(?:just tell me|tell me directly|don'?t open|do not open|no (?:new )?tabs?|without opening|no browser|don'?t (?:show|launch)|only (?:tell|answer))\b/i;

  // WhatsApp (Android): opening/composing is NOT sending. The classifier never says "send" happened.
  const WHATSAPP = /\bwhats\s?app\b/i;
  const RECIPIENT_MESSAGE = [
    /\b(?:message|text|send|write|draft|prepare)(?:\s+(?:a|this|the))?(?:\s+message)?\s+to\s+(.{1,60}?)\s*(?:saying|that says|that|:|-|with)\s+(.{1,1500})$/i,
    /\b(?:message|text|tell)\s+(.{1,60}?)\s*(?:on whatsapp\s*)?(?:saying|that says|that|:)\s+(.{1,1500})$/i,
    /\bwhats\s?app\s+(.{1,60}?)\s*:\s*(.{1,1500})$/i
  ];

  const POLITE = /^(?:(?:hey\s+)?jupiter[,\s]+)?(?:(?:please|can you|could you|would you|will you|kindly)\s+)*/i;
  const QUESTION = /^(?:how|why|what|when|where|which|who|is|are|do|does|did|should|shall|explain|tell me how)\b/i;
  const IMPERATIVE = new RegExp(`^(?:${ACTIONS}|switch to|go to|bring up|in)\\b`, 'i');

  /** Explicit requests only: "control/use my laptop", "start computer use", or an imperative action on a named
   *  app/device. Questions ("how do I open settings on my laptop?") never start control. */
  function isControlRequest(said) {
    if (CONTROL_PATTERNS[0].test(said) || CONTROL_PATTERNS[2].test(said)) return true;
    const body = said.replace(POLITE, '');
    if (QUESTION.test(body) || !IMPERATIVE.test(body)) return false;
    return CONTROL_PATTERNS[1].test(body) || CONTROL_PATTERNS[3].test(body) || CONTROL_PATTERNS[4].test(body);
  }

  function appFromText(text) {
    for (const pattern of CONTROL_PATTERNS.slice(3)) {
      const match = pattern.exec(text);
      if (match && match[1]) {
        const app = norm(match[1]).replace(/^(?:my|the)\s+/i, '');
        if (app && !/^(?:it|this|that|there|here|a|an)$/i.test(app)) return app;
      }
    }
    return '';
  }

  function whatsappRequest(text) {
    const cleaned = norm(text).replace(/\b(?:on|in|via|using)\s+whats\s?app\b/ig, '').replace(/^(?:jupiter[,\s]+)?(?:please\s+)?(?:open\s+whats\s?app\s+and\s+)?/i, '');
    for (const pattern of RECIPIENT_MESSAGE) {
      const match = pattern.exec(cleaned) || pattern.exec(norm(text));
      if (match) {
        const recipient = norm(match[1]).replace(/^(?:to\s+)/i, '').replace(/[.,;]+$/, '');
        const message = norm(match[2]).replace(/^["“']|["”']$/g, '');
        if (recipient && message) return { recipient, message };
      }
    }
    return { recipient: '', message: '' };
  }

  /**
   * Classify ONE user utterance (typed or spoken). context: { modeActive, platform: 'windows'|'android'|'web' }.
   * Returns null for ordinary chat (existing behaviour unchanged).
   */
  function classify(text, context = {}) {
    const said = norm(text);
    if (!said) return null;
    const platform = context.platform || 'web';
    if (STOP_PATTERN.test(said)) return { kind: 'stop' };
    // On Windows, WhatsApp Desktop is just another window for (consent-gated) laptop control.
    if (platform !== 'windows' && WHATSAPP.test(said) && /\b(?:message|text|send|write|draft|prepare|tell|open)\b/i.test(said)) {
      const { recipient, message } = whatsappRequest(said);
      return { kind: 'whatsapp', platform, recipient, message,
        wantsSend: /\bsend\b/i.test(said), openOnly: !message };
    }
    const control = isControlRequest(said);
    if (control && platform === 'windows') return { kind: 'control', app: appFromText(said), goal: said.slice(0, 300),
      presentation: !/\b(?:background|without (?:showing|opening|bringing)|don'?t (?:bring|show)|do not (?:bring|show))\b/i.test(said)
        && /\b(?:in front of me|show (?:me )?(?:the|your) steps|teach me|walk me through|show me how)\b/i.test(said)
        && !/\b(?:not|don'?t|do not)\s+(?:in front|show|teach)/i.test(said) ? 'foreground' : 'background' };
    if (context.modeActive && CURRENT_INFO.test(said) && SHOW_VERBS.test(said)) {
      if (ANSWER_ONLY.test(said)) return { kind: 'answer_only' };
      return { kind: 'browser', query: said.slice(0, 200), url: searchUrl(said) };
    }
    if (control) return { kind: 'control_unavailable', app: appFromText(said) };
    return null;
  }

  function searchUrl(query) {
    const q = norm(query).replace(/^(?:jupiter[,\s]+)?(?:please\s+)?(?:show me|open|pull up|look up|search(?: for)?|find|check)\s+/i, '');
    return `https://www.bing.com/search?q=${encodeURIComponent(q.slice(0, 200))}`;
  }

  /** Windows whose title or process mentions the app the user named (case-insensitive word match). */
  function matchWindows(windows, app) {
    const wanted = lower(app).replace(/\.exe$/, '');
    if (!wanted) return [];
    const words = wanted.split(' ').filter((word) => word.length > 1);
    return (windows || []).filter((w) => {
      const hay = `${lower(w.title)} ${lower(w.process).replace(/\.exe$/, '')}`;
      return words.every((word) => hay.includes(word));
    });
  }

  /** The ONLY wording Jupiter uses for a WhatsApp result. Nothing here ever claims a message was sent. */
  function whatsappStatus(result = {}) {
    const who = norm(result.recipient_label || '') || 'the contact you choose';
    switch (result.status) {
      case 'prepared':
        return `WhatsApp is open with your message to ${who}. Tap Send in WhatsApp to send it. Jupiter can't press Send for you and can't confirm whether it was sent.`;
      case 'picker_opened':
        return 'WhatsApp is open with your message. Choose the chat in WhatsApp, then tap Send. Jupiter can\'t confirm whether it was sent.';
      case 'opened':
        return 'WhatsApp is open. Nothing was written or sent.';
      case 'cancelled':
        return 'Cancelled. Nothing was opened or sent.';
      case 'not_installed':
        return 'WhatsApp isn\'t installed on this phone, so nothing was sent.';
      case 'no_number':
        return `I couldn't find a phone number for ${who}, so nothing was sent.`;
      case 'unsupported':
        return 'WhatsApp actions work in the Jupiter Android app. Nothing was sent.';
      default:
        return 'WhatsApp could not be opened, so nothing was sent.';
    }
  }

  const api = { classify, matchWindows, searchUrl, whatsappStatus, whatsappRequest };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.JupiterComputerUseIntents = api;
})(typeof window !== 'undefined' ? window : null);
