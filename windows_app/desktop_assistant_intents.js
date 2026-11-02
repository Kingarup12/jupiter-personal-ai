(function (root) {
  'use strict';
  // Marker 197: desktop/phone device requests (volume, media, play, notch, screen marks, phone agent), English +
  // Hinglish. Pure: no DOM, no network. The SAME classifier as services/desktop_intents.py (Live voice); both run
  // tests/fixtures/desktop_intents.json so they cannot drift apart.

  const POLITE = /^(?:(?:hey|hi|hii+|hello|helo|ok(?:ay)?|arre|acha|achha|namaste|so|now|and|then)[\s,.!]+)*(?:jupiter[\s,.!]+)?(?:(?:please|pls|plz|can you|could you|would you|will you|kindly|zara|jaldi|just|i want you to|i need you to)\s+)*/i;
  const TRAIL = /(?:[\s,.!?]+(?:please|pls|plz|jupiter|yaar|na|now|abhi|for me))*[\s.!?।]*$/i;
  const QUESTION = /^(?:how|why|what is the volume|what's the volume|when|is it|are you|do you|does|did|should|can i|how do|how to|kaise)\b/i;

  const re = (source) => new RegExp(source, 'i');
  // Volume: a small parser instead of fixed sentences ("decrease my volume to 10%", "Spotify ki awaaz kam karo").
  const VOL = /\b(?:volume|vol|sound|audio|awaaz|aawaz|awaz|awaj|aawaaz|aawaj)\b/i;
  const LOUDNESS = /\b(?:louder|quieter|softer)\b|^(?:turn|crank)\s+(?:it|the\s+(?:music|song|video|tv))\s+(?:up|down)\b/i;
  const UP_WORD = /\b(?:increase|raise|boost|up|louder|higher|badha\w*|tez|tej|zyada|jyada|upar|high)\b/i;
  const DOWN_WORD = /\b(?:decrease|reduce|lower|drop|down|quieter|softer|less|kam|dheere|dhire|dheem\w*|dhim\w*|neeche|niche|ghata\w*|low)\b/i;
  const MUTE_WORD = /\b(?<un>un)?mute\b|\b(?:awaaz|aawaz|awaz|sound|volume)\s+(?<sw>band|chalu|on|off)\b/i;
  const NUMBER = /(?<![\d.])(\d{1,3})\s*(?:%|percent|per\s*cent|pratishat)?(?![\d.])/i;
  const TO_NUMBER = /\b(?:to|at|till|until|upto|up\s+to|around|about|pe|par|tak)\s+(?:about\s+|around\s+)?\d/i;
  const SET_HINDI = /\d{1,3}\s*(?:%|percent|per\s*cent|pratishat)?\s+(?:pe\s+|par\s+)?(?:kar|karo|kardo|kar\s*do|kar\s*de|rakho|rakh\s*do|set)\b/i;
  const BY_NUMBER = /\bby\s+(?:about\s+|around\s+)?\d/i;
  const MAX = /\b(?:full|max|maximum|poora|puri|pura)\b/i;
  const MIN = /\b(?:min|minimum|zero)\b/i;
  const NOT_AUDIO = /\b(?:cylinder|cube|cuboid|sphere|cone|prism|pyramid|tank|container|bottle|cm|mm|litres?|liters?|ml|cubic|m3|formula|calculate|area|density|mass|gas|liquid|book|series|edition|trading)\b/i;
  const STATEMENT = /\b(?:is|was|are|were|seems|shows?|showing|stays?|stuck|still|already)\b/i;
  const IMPERATIVE = /^(?:set|make|put|turn|keep|change|increase|decrease|raise|lower|reduce|boost|drop|bring|crank|mute|unmute|volume|sound|awaaz|aawaz|awaz)\b/i;
  const KNOWN_APPS = ['youtube music', 'apple music', 'google chrome', 'microsoft edge', 'chrome', 'edge', 'firefox', 'brave',
    'opera', 'spotify', 'discord', 'zoom', 'teams', 'vlc', 'whatsapp', 'telegram', 'skype', 'youtube', 'netflix', 'prime video', 'jupiter'];
  const KNOWN_APP = new RegExp(`\\b(${KNOWN_APPS.map((a) => a.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})\\b`, 'i');
  const NOT_APP = new Set(`my the this that it its your our pc computer laptop system speaker speakers headphone headphones
device windows everything all me volume sound audio awaaz master overall main total full max maximum min minimum
a an set make put turn keep change increase decrease raise lower reduce boost drop bring crank mute unmute up down
please thoda thodi aur jara zara`.split(/\s+/));
  const APP_PATTERNS = [
    /^(?:the\s+)?([a-z][\w.+-]{1,20})\s+(?:ki|ka|ke)\s+(?:awaaz|aawaz|awaz|volume|sound)\b/i,
    /^(?:un)?mute\s+(?:the\s+)?([a-z][\w.+-]{1,20})$/i,
    /^make\s+(?:the\s+)?([a-z][\w.+-]{1,20})\s+(?:louder|quieter|softer)$/i,
    /\b(?:of|for|in|on)\s+(?:the\s+)?([a-z][\w.+-]{1,20})\b/i,
    /\b([a-z][\w.+-]{1,20})(?:'s)?\s+(?:volume|sound|awaaz)\b/i
  ];
  const volumeApp = (said) => {
    const known = said.match(KNOWN_APP);
    if (known) return known[1];
    for (const pattern of APP_PATTERNS) {
      const match = said.match(pattern);
      if (match && !NOT_APP.has(match[1].toLowerCase()) && !/^\d+$/.test(match[1])) return match[1];
    }
    return '';
  };
  /** Volume change the user asked for, or null. "to N" sets N, "by N" changes by N, a direction alone moves 10. */
  function volume(said) {
    if (!(VOL.test(said) || LOUDNESS.test(said) || MUTE_WORD.test(said)) || NOT_AUDIO.test(said)) return null;
    if (STATEMENT.test(said) && !IMPERATIVE.test(said)) return null;     // "my volume is at 52" reports, it does not ask
    const app = volumeApp(said);
    const up = said.match(UP_WORD);
    const down = said.match(DOWN_WORD);
    let sign = 0;
    if (up || down) sign = (up && (!down || up.index < down.index)) ? 1 : -1;
    const number = said.match(NUMBER);
    const mute = said.match(MUTE_WORD);
    if (mute && !number && !sign) {
      const muted = !mute.groups?.un && ['band', 'off'].includes(String(mute.groups?.sw || 'band').toLowerCase());
      return { kind: 'volume', mute: muted, app };
    }
    if (number) {
      const value = Math.max(0, Math.min(100, Number(number[1])));
      if (BY_NUMBER.test(said)) return sign ? { kind: 'volume', delta: sign * Math.max(1, value), app } : null;
      if (TO_NUMBER.test(said) || SET_HINDI.test(said) || !sign) return { kind: 'volume', set: value, app };
      return { kind: 'volume', delta: sign * Math.max(1, value), app };
    }
    if (MAX.test(said)) return { kind: 'volume', set: 100, app };
    if (MIN.test(said)) return { kind: 'volume', set: 0, app };
    if (sign) return { kind: 'volume', delta: sign * 10, app };
    return null;
  }

  const MEDIA = [
    [/^(?:pause|resume|unpause|stop|continue)\s+(?:the\s+)?(?:music|song|video|playback|audio|track|it|spotify|youtube)$/i, 'play_pause'],
    [/^(?:play|resume)\s+(?:the\s+)?(?:music|song)(?:\s+again)?$|^play(?: it)?(?: again)?$/i, 'play_pause'],
    [/^(?:(?:play\s+)?(?:the\s+)?next|skip(?:\s+(?:this|it|the))?)(?:\s+(?:song|track|video))?$|^(?:agla|agli|next)\s+(?:gaana|gana|song|video)(?:\s+(?:chalao|lagao|chala\s*do|laga\s*do|bajao))?$/i, 'next'],
    [/^(?:(?:play\s+)?(?:the\s+)?previous|go back to the (?:last|previous))\s+(?:song|track|video)$|^(?:pichla|pichhla|pichli|previous)\s+(?:gaana|gana|song|video)(?:\s+(?:chalao|lagao|chala\s*do|laga\s*do|bajao))?$/i, 'previous'],
    [/^(?:gaana|gana|music|song|video)\s+(?:roko|rok\s*do|band\s+karo|band\s+kar\s*do|pause\s+karo|pause\s+kar\s*do|chalao|chala\s*do|resume\s+karo|phir\s+se\s+chalao)$/i, 'play_pause']
  ];
  const PLAY_APP = '(?<app>youtube music|youtube|yt|spotify)';
  const PLAY = [
    re(`^(?:play|put on)\\s+(?<q>.+?)\\s+(?:on|in|from|using)\\s+${PLAY_APP}$`),
    re(`^(?:open\\s+|play\\s+|start\\s+)?${PLAY_APP}\\s+(?:and\\s+)?(?:then\\s+)?play\\s+(?<q>.+)$`),
    re(`^.*?\\bopen\\s+(?:the\\s+|my\\s+)?${PLAY_APP}(?:\\s+app)?\\s*,?\\s+and\\s+(?:then\\s+)?play\\s+(?<q>.+)$`),   // marker 214
    re(`^(?<q>.+?)\\s+${PLAY_APP}\\s+(?:pe|par|mein|me|par se|pe se)\\s+(?:bajao|baja\\s*do|chalao|chala\\s*do|laga\\s*do|lagao|play\\s+karo|play\\s+kar\\s*do|sunao)$`),
    /^(?:play|put on)\s+(?<q>.+)$/i,
    /^(?<q>.+?)\s+(?:bajao|baja\s*do|play\s+karo|play\s+kar\s*do|sunao|laga\s*do)$/i
  ];
  const NOT_MEDIA = /^(?:a\s+|some\s+)?(?:game|games|chess|ludo|cards|quiz|with me|along|role|pretend|trivia|a role|20 questions|antakshari|the fool|dumb|hide and seek|taali|tali|bell|ghanti|siti|seeti|whistle|dhol)\b/i;
  const GENERIC_MUSIC = /^(?:some\s+)?(?:music|songs?|gaana|gaane|gana)$/i;
  const MEDIA_PAUSE = /\b(?:pause|stop|roko|rok\s*do|band)\b|रोको|बंद/i;
  // Marker 214: "play any song" = Jupiter picks (twin of desktop_intents._ANY_MUSIC).
  const ANY_MUSIC = /^(?:any|some|something|anything|whatever|kuch|kuch\s+bhi|koi\s+bhi|a)(?:\s+(?:good|nice|random|other))?(?:\s+(?:song|songs|music|gaana|gaane|track|tracks))?$/i;
  const ANY_MUSIC_QUERY = "today's top hits";
  // Marker 214: laptop screen brightness (twin of desktop_intents._brightness).
  const BRIGHT = /\b(?:brightness|brighter|screen\s+light|roshni|chamak)\b|\b(?:dim|darken)\s+(?:the\s+|my\s+)?screen\b|ब्राइटनेस|रोशनी/i;
  const BRIGHT_UP = /\b(?:increase|raise|up|more|higher|brighter|max(?:imum)?|full|badha\w*|zyada|tez)\b|बढ़ा/i;
  const BRIGHT_DOWN = /\b(?:decrease|lower|reduce|down|less|dim|darken|min(?:imum)?|kam|ghata\w*)\b|कम|घटा/i;
  const brightness = (said) => {
    if (!BRIGHT.test(said)) return null;
    const up = BRIGHT_UP.exec(said), down = BRIGHT_DOWN.exec(said);
    const sign = (up || down) ? ((up && (!down || up.index < down.index)) ? 1 : -1) : 0;
    const number = /(\d{1,3})\s*(?:%|percent|per\s*cent|pratishat)?/i.exec(said);
    if (number) {
      const value = Math.max(0, Math.min(100, Number(number[1])));
      if (/\bby\s+\d/i.test(said) && sign) return { kind: 'brightness', delta: sign * Math.max(1, value) };
      return { kind: 'brightness', set: value };
    }
    if (/\b(?:max(?:imum)?|full|100)\b/i.test(said)) return { kind: 'brightness', set: 100 };
    if (/\b(?:min(?:imum)?|lowest)\b/i.test(said)) return { kind: 'brightness', set: 5 };
    return sign ? { kind: 'brightness', delta: sign * 20 } : null;
  };

  const UI = [
    [/^(?:maximi[sz]e)\s+(?:yourself|jupiter|the\s+(?:app|window))$|^make\s+yourself\s+full\s*screen$|^(?:go|become)\s+full\s*screen$|^full\s*screen\s+(?:ho\s+jao|karo)$/i, 'maximize'],
    [/^(?:expand|enlarge|grow)\s+(?:yourself|jupiter|the\s+(?:app|window))$|^make\s+yourself\s+(?:bigger|larger|big)$|^(?:go|become)\s+(?:big|bigger)$|^(?:bade|bada|badi)\s+ho\s+jao$/i, 'expand'],
    [/^(?:minimi[sz]e|shrink|hide)\s+(?:yourself|jupiter|the\s+(?:app|window))$|^make\s+yourself\s+(?:smaller|small|tiny)$|^(?:go|switch|move)\s+(?:to\s+)?(?:the\s+)?notch(?:\s+mode)?$|^notch\s+mode$|^go\s+small$|^(?:chhot[ae]|chot[ae])\s+ho\s+jao$|^notch\s+(?:mein|me)\s+(?:jao|chale\s+jao|aa\s+jao)$/i, 'minimize'],
    [/^(?:come\s+back|restore\s+(?:yourself|the\s+(?:app|window))|open\s+(?:the\s+)?full\s+(?:app|window|jupiter)|show\s+(?:the\s+)?full\s+(?:app|window)|wapas\s+aao|vapas\s+aao|full\s+screen\s+(?:mein|me)\s+aao)$/i, 'restore'],
    [/^(?:expand|open|enlarge|grow)\s+(?:the\s+)?notch$|^show\s+(?:the\s+)?transcript\s+in\s+(?:the\s+)?notch$|^notch\s+(?:bada|badi)\s+karo$/i, 'expanded'],
    [/^(?:collapse|shrink|minimi[sz]e)\s+(?:the\s+)?notch$|^notch\s+(?:chhota|chota|choti)\s+karo$/i, 'compact'],
    [/^(?:hide|close|remove)\s+(?:the\s+)?notch$|^notch\s+(?:hatao|band\s+karo|chhupao)$/i, 'hidden']
  ];

  // "Can you control my PC?" -> what Jupiter can really do (instead of "Which window?" plus an invented yes).
  const CAPABILITIES = /^(?:do\s+you\s+have\s+(?:access\s+to|control\s+(?:of|over))\s+(?:my|the|this)\s+(?:pc|computer|laptop|desktop|system|machine|phone|mobile)$)|^(?:control|use|operate|access|handle|run)\s+(?:my|the|this)\s+(?:pc|computer|laptop|desktop|system|machine|phone|mobile)(?:\s+for\s+me)?$|^what\s+(?:all\s+)?can\s+you\s+do\s+(?:on|with|in)\s+(?:my|the|this)\s+(?:pc|computer|laptop|desktop|system)$|^(?:mera|meri|mere)\s+(?:pc|computer|laptop)\s+(?:control|chala|use)\s+(?:kar\s+sakte\s+ho|karo|kar\s*do|sakte\s+ho|kar\s+sakti\s+ho)$/i;
  const CAPABILITIES_REPLY = 'Yes. On this laptop I can: change the volume (“set the volume to 10%”, “mute Chrome”), play music (“play Kesariya on YouTube”, “next song”), mark and explain things on your screen (“circle the error on my screen”), open apps (“open Chrome”), maximize, minimize or close windows (“maximize Edge”, “close the window you opened”), make Juno spin, jump or dance, shrink myself into the notch (“minimise yourself” / “come back”), work inside an open window (“in Notepad, type hello”), and search the web in Live (“search the web for the latest Gemini model”). Tell me what you want done.';
  const CAPABILITIES_REPLY_PHONE = 'Yes. On this phone I can: change the volume (“set the volume to 10%”), play music (“play Kesariya on YouTube”, “next song”) open apps (“open WhatsApp”), and search the web in Live (“search the web for today’s IPL score”). Tell me what you want done.';
  // "open Google Chrome in my laptop", "Notepad kholo". A known app always counts; any other name only with "app" or
  // "on my laptop/phone", so Jupiter's own things ("open a new chat", "open settings") are never hijacked.
  const OPEN_APP_NAMES = new Set(`chrome|google chrome|edge|microsoft edge|firefox|brave|opera|notepad|calculator|calc|paint|
file explorer|explorer|files|my files|windows settings|task manager|terminal|command prompt|powershell|word|microsoft word|
excel|microsoft excel|powerpoint|outlook|spotify|whatsapp|teams|zoom|discord|telegram|vlc|vs code|vscode|
visual studio code|steam|camera app|microsoft store|play store|clock|alarms|photos|gallery|snipping tool|control panel|
youtube|gmail|google maps|maps|instagram|calendar|contacts|messages|phone app|dialer|google`.replace(/\n/g, '').split('|'));
  const DEVICE_NOUN = '(?:laptop|pc|computer|desktop|system|phone|mobile)';
  const OPEN_APP = [
    re(`^(?:open|launch|start|run|bring\\s+up)\\s+(?:up\\s+)?(?:the\\s+|my\\s+)?(?<app>[a-z][\\w .+&'-]{1,40}?)(?<suffix>\\s+app(?:lication)?)?(?:\\s+(?:on|in)\\s+(?:my|the|this)\\s+(?<dev>${DEVICE_NOUN}))?(?:\\s+for\\s+me)?$`),
    re(`^(?:(?:mere|meray|apne)\\s+)?(?:(?<dev>${DEVICE_NOUN})\\s+(?:pe|par|mein|me)\\s+)?(?<app>[a-z][\\w .+&'-]{1,40}?)(?<suffix>\\s+app)?\\s+(?:kholo|khol\\s*do|khol\\s*de|open\\s+karo|open\\s+kar\\s*do|chalu\\s+karo|start\\s+karo)$`)
  ];
  const NOT_AN_APP = /^(?:a\s+|the\s+|new\s+)*(?:chat|new chat|history|memory|memories|settings|profile|camera|mic|microphone|live|transcript|sidebar|menu|notch|door|window|tab|link|url|website|file|folder|pdf|document|image|photo|it|this|that|conversation|account|notifications?)$/i;
  const APP_ALT = [...OPEN_APP_NAMES].sort((a, b) => b.length - a.length).map((n) => n.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
  // Marker 208: String.raw - the old template literal turned \b into a backspace and \s into "s" (it never matched).
  const OPEN_KNOWN = new RegExp(String.raw`\b(?:can you|could you|would you|will you|please|kindly|you|jupiter)\b.{0,30}?\b(?:open|launch|start)\s+(?:the\s+|my\s+)?(${APP_ALT})\b(?!\s+(?:tab|file|folder|website|page|link|chat))`, 'i');
  const OPEN_NEW_WINDOW = new RegExp(String.raw`\b(?:open|launch|start)\s+(?:a\s+|one\s+more\s+)?(?:new|another|second)\s+(${APP_ALT})\s+(?:window|tab)\b`, 'i');
  const openKnownApp = (text) => {
    const said = String(text || '').replace(/\s+/g, ' ').trim();
    const fresh = said.match(OPEN_NEW_WINDOW);
    if (fresh) return { kind: 'open_app', app: fresh[1], new_window: true };
    if (OPEN_DOMAIN.test(said) || OPEN_SITE.test(said)) return null;   // marker 212: "... Edge and open youtube.com"
    if (/\band\s+play\b/i.test(said)) return null;                      // marker 214: "open Spotify and play something"
    const match = said.match(OPEN_KNOWN);
    return match ? { kind: 'open_app', app: match[1] } : null;
  };
  // Marker 208: window actions (twin of desktop_intents._window_steps) and buddy actions (twin of _buddy).
  const WINDOW_APP = new RegExp(String.raw`\b(${APP_ALT})\b`, 'i');
  const NEW_WINDOW = /\b(?:new|another|second|naya|nayi|dusra|doosra)\s+(?:window|tab|one)\b/i;
  const MAXIMIZE_WORD = /\b(?:maximi[sz]e|full\s*screen|bada\s+karo|bada\s+kar\s*do)\b/i;
  const WINDOW_VERBS = [
    ['maximize', /\b(?:maximi[sz]e|make\s+(?:it\s+)?full\s*screen|full\s*screen\s+(?:karo|kar\s*do)|bada\s+(?:karo|kar\s*do))\b/i],
    ['minimize', /\b(?:minimi[sz]e|chhota\s+(?:karo|kar\s*do)|neeche\s+karo)\b/i],
    ['restore', /\b(?:restore|un-?minimi[sz]e|bring\s+back|wapas\s+(?:lao|khol\w*))\b/i],
    ['close', /\b(?:close|shut)\b|\bband\s+(?:karo|kar\s*do|kar\s*de)\b/i]
  ];
  const WINDOW_NOUN = /\b(?:window|windows|app|tab)\b|\b(?:it|that|this|that\s+one|this\s+one)\b/i;
  const WINDOW_LAST = /\b(?:new|that|this|last|other|extra|naya|nayi)\s+(?:window|one|tab)\b|\byou\s+(?:just\s+|currently\s+)?(?:opened|open|launched)\b|\b(?:jo|joh)\s+(?:abhi\s+|tumne\s+)?(?:khola|kholi|khole)\b/i;
  const WINDOW_EXISTING = /\b(?:existing|already\s+open|old|original|previous|purana|purani|pehle\s+wala|pehli)\b/i;
  const USER_DID = /\b(?:let\s+me|i\s+(?:have\s+|had\s+|already\s+)?|i'?ve|i'?ll|maine)\s*(?:just\s+)?(?:minimi[sz]ed?|maximi[sz]ed?|closed?|restored?|opened?)\b/i;
  const WINDOW_CLAUSES = /[.?!\u0964;]+|,\s*(?:and\s+)?(?:then\s+)?|\s+(?:and\s+then|and|then|aur\s+phir|aur|phir)\s+/i;
  function windowSteps(text) {
    const steps = [];
    for (const raw of String(text || '').replace(/\s+/g, ' ').split(WINDOW_CLAUSES)) {
      const clause = String(raw || '').trim().replace(/^[\s,]+|[\s,]+$/g, '');
      if (!clause || USER_DID.test(clause) || QUESTION.test(clause) || /\btabs?\b/i.test(clause)) continue;   // a tab is not a window
      const found = WINDOW_VERBS.find(([, verb]) => verb.test(clause));
      if (!found) continue;
      const action = found[0];
      const appMatch = clause.match(WINDOW_APP);
      const app = appMatch ? appMatch[1] : '';
      if (action === 'close' && WINDOW_LAST.test(clause)) steps.push({ action: 'close', app: '', target: 'last' });
      else if (app || WINDOW_NOUN.test(clause)) {
        const target = WINDOW_EXISTING.test(clause) ? 'existing' : (WINDOW_LAST.test(clause) ? 'last' : '');
        steps.push({ action, app, target });
      }
      if (steps.length === 3) break;
    }
    return steps;
  }
  // Marker 209: browser tasks without keyboard/mouse, and any other concrete computer task -> computer control.
  // Twin of desktop_intents._browser / looks_like_computer_task (keep in step).
  const SITES = ['youtube', 'google', 'gmail', 'wikipedia', 'amazon', 'flipkart', 'facebook', 'instagram', 'twitter', 'linkedin',
    'github', 'netflix', 'chatgpt', 'claude', 'gemini', 'google maps', 'maps', 'google drive', 'drive', 'whatsapp web', 'reddit',
    'stack overflow', 'outlook', 'spotify', 'hotstar', 'prime video', 'canva', 'notion'];
  const SITE_ALT = SITES.slice().sort((a, b) => b.length - a.length).map((s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
  const BROWSER_NAME = String.raw`(?:microsoft\s+edge|google\s+chrome|mozilla\s+firefox|edge|chrome|firefox|brave|opera)`;
  const IN_BROWSER = new RegExp(String.raw`\b(${BROWSER_NAME})\b`, 'i');
  const NEW_TAB = /\b(?:open|start|create|launch|kholo|khol\s*do|khol\s*de)\b.{0,30}\b(?:a\s+)?(?:new|naya|nayi|ek|another|one\s+more|fresh)\s+(?:blank\s+|empty\s+|khali\s+)?tab\b|\b(?:new|naya|nayi|ek)\s+tab\s+(?:kholo|khol\s*do|khol\s*de|open\s+karo|open\s+kar\s*do|chahiye)\b/i;
  const OPEN_DOMAIN = /\b(?:open|go\s+to|visit|take\s+me\s+to|kholo|khol\s*do)\s+(?:the\s+)?(?:website\s+|site\s+)?(?<site>(?:https?:\/\/)?[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:\/\S*)?)/i;
  const OPEN_SITE = new RegExp(String.raw`\b(?:open|go\s+to|visit|take\s+me\s+to)\s+(?:the\s+)?(?<site>${SITE_ALT})\s+(?:website|site|web\s*page|page|in\s+(?:a\s+)?new\s+tab|in\s+(?:the\s+)?browser|in\s+${BROWSER_NAME})\b|\b(?:go\s+to|visit|take\s+me\s+to)\s+(?<site2>${SITE_ALT})\b|\b(?<site3>${SITE_ALT})\s+(?:website|site)\s+(?:kholo|khol\s*do|open\s+karo)\b`, 'i');
  const SEARCH_ON = new RegExp(String.raw`\b(?:search|google|look\s+up|find|dhundo|dhoondo)\s+(?:for\s+|about\s+)?(?<q>.{2,200}?)\s+(?:on|in|using)\s+(?:the\s+)?(?<engine>google|youtube|bing|browser|${BROWSER_NAME})\b`, 'i');
  const SEARCH_PREFIX = /^(?:google|search\s+(?:on\s+)?google\s+for|open\s+google\s+and\s+search(?:\s+for)?|open\s+(?:a\s+)?new\s+tab\s+and\s+search(?:\s+for)?)\s+(?<q>.{2,200})$/i;
  const SEARCH_HINDI = /\b(?<engine>google|youtube|browser)\s+(?:pe|par|mein|me)\s+(?<q>.{2,200}?)\s+(?:search\s+karo|search\s+kar\s*do|dhundo|dhoondo|sarch\s+karo|search\s+karke\s+dikhao)\b/i;
  function browserAction(text) {
    const said = String(text || '').replace(/\s+/g, ' ').trim().replace(/^[\s?.!]+|[\s?.!]+$/g, '');
    if (!said || QUESTION.test(said) || USER_DID.test(said)) return null;
    const named = said.match(IN_BROWSER);
    const browser = named ? named[1] : '';
    for (const pattern of [SEARCH_PREFIX, SEARCH_HINDI, SEARCH_ON]) {
      const match = said.match(pattern);
      if (!match) continue;
      const query = group(match, 'q').replace(/^[\s,.'"]+|[\s,.'"]+$/g, '');
      const engine = (group(match, 'engine') || 'google').toLowerCase();
      if (query && !new RegExp(String.raw`^${BROWSER_NAME}$`, 'i').test(query)) {
        return { kind: 'browser', action: 'search', query: query.slice(0, 200), engine: ['youtube', 'bing'].includes(engine) ? engine : 'google', browser };
      }
    }
    const site = said.match(OPEN_DOMAIN) || said.match(OPEN_SITE);
    if (site) return { kind: 'browser', action: 'open_url', url: group(site, 'site') || group(site, 'site2') || group(site, 'site3'), browser };
    if (NEW_TAB.test(said)) return { kind: 'browser', action: 'new_tab', browser };
    return null;
  }
  const TASK_VERB = /\b(?:open|close|click|press|tap|type|write|enter|search|go\s+to|switch(?:\s+to)?|scroll|select|copy|paste|save|rename|create|make|turn\s+(?:on|off)|enable|disable|change|set|fill|download|upload|bookmark|refresh|reload|zoom\s+(?:in|out)|pin|unpin|move|drag|delete|sort|find|send|reply|attach|print|log\s+in|sign\s+in|check|uncheck|mark|play|pause|start|stop|add|remove|kholo|khol\s*do|band\s+karo|band\s+kar\s*do|likho|dabao|chalao|bhejo|hatao|badlo|karo)\b/i;
  const TASK_OBJECT = /\b(?:tabs?|browser|website|web\s*site|web\s*page|webpage|link|url|address\s+bar|bookmarks?|windows?|desktop|taskbar|start\s+menu|(?:windows|system|laptop|pc|computer|display|sound|audio|bluetooth|wi-?fi|network|privacy|chrome|edge|browser|firefox)\s+settings|settings\s+(?:app|mein|me)|wi-?fi|bluetooth|dark\s+mode|light\s+mode|night\s+light|wallpaper|screenshot|button|menu|drop-?down|checkbox|text\s*box|search\s*(?:box|bar)|form|field|folder|folders|file\s+explorer|downloads?\s+folder|files?|recycle\s+bin|notifications?\s+panel|cursor|scroll\s*bar|sidebar)\b/i;
  const TASK_APP = new RegExp(String.raw`\b(?:in|on|into|inside|using|within)\s+(?:the\s+|my\s+)?(?:${APP_ALT}|${BROWSER_NAME})\b|\b(?:${APP_ALT}|${BROWSER_NAME})\s+(?:mein|me|pe|par)\b`, 'i');
  const EXPLICIT_TASK = /^in\s+(?!(?:my\s+(?:opinion|view)|short|general|fact|brief|detail|summary|total|other\s+words|simple\s+(?:words|terms)|the\s+end|the\s+meantime|any\s+case|that\s+case|this\s+case|case|addition|conclusion|particular|reality|theory|practice|future|return|hindi|english|bengali|bangla|that|this|it)\b)(?:the\s+)?[\w .+'-]{2,40}?(?:\s+app|\s+window)?\s*,?\s+(?:type|write|enter|click|press|select|scroll|fill|search|find|open|close|save|paste|change|set|tick|untick|check|uncheck|choose|pick|toggle|turn\s+(?:on|off)|clear|delete|add|remove|go\s+to|switch|play|pause|calculate|compute|draw|rename|copy|move|mark|sort|submit|create|make|start|stop|refresh|reload)\b|\b(?:open|switch\s+to|go\s+to|bring\s+up)\s+(?:the\s+)?[\w .+'-]{2,40}?\s+(?:app\s+)?and\s+(?:type|write|enter|click|press|select|scroll|fill|search|find|save|paste)\b/i;
  // Marker 211: "open Calculator and calculate 125 times 8" (twin of desktop_intents._OPEN_AND_DO, keep in step).
  const WEB_APP_NAMES = new Set(['youtube', 'gmail', 'google', 'google maps', 'maps', 'instagram', 'chrome', 'google chrome',
    'edge', 'microsoft edge', 'firefox', 'brave', 'opera']);
  const DESK_APP_ALT = [...OPEN_APP_NAMES].filter((n) => !WEB_APP_NAMES.has(n)).sort((a, b) => b.length - a.length)
    .map((n) => n.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
  const OPEN_AND_DO = new RegExp(String.raw`^(?:open|launch|start|bring\s+up)\s+(?:up\s+)?(?:the\s+|my\s+)?(?:${DESK_APP_ALT})(?:\s+app)?\s*,?\s+(?:and(?:\s+then)?|then)\s+(?!play\b)\w.{2,}|^(?:${DESK_APP_ALT})(?:\s+app)?\s+(?:kholo|khol\s*do|khol\s*ke|open\s+karo|open\s+kar\s*ke)\s*,?\s+(?:aur\s+|and\s+|phir\s+|fir\s+)?(?!play\b)\w.{2,}`, 'i');
  const opensAppForTask = (text) => OPEN_AND_DO.test(clean(text));
  function looksLikeComputerTask(text) {
    const said = String(text || '').replace(/\s+/g, ' ').trim().replace(/^[\s?.!]+|[\s?.!]+$/g, '');
    if (!said || said.length > 400 || QUESTION.test(clean(said)) || USER_DID.test(said)) return false;
    if (EXPLICIT_TASK.test(clean(said)) || opensAppForTask(said)) return true;
    return TASK_VERB.test(said) && (TASK_OBJECT.test(said) || TASK_APP.test(said));
  }
  // Marker 210: Live's microphone by voice / text (twin of desktop_intents._MIC_MUTE / _MIC_UNMUTE).
  const MIC_MUTE = /\b(?:mute|turn\s+off|switch\s+off|disable|band\s+karo|band\s+kar\s*do|off\s+karo)\s+(?:the\s+|your\s+|my\s+)?(?:mic|mike|microphone)\b|\b(?:mic|mike|microphone)\s+(?:mute|off|band)(?:\s+(?:karo|kar\s*do))?\b|\bmute\s+(?:yourself|the\s+live|live)\b|\bstop\s+listening\s+to\s+me\b/i;
  const MIC_UNMUTE = /\b(?:unmute|turn\s+on|switch\s+on|enable|on\s+karo|chalu\s+karo)\s+(?:the\s+|your\s+|my\s+)?(?:mic|mike|microphone)\b|\b(?:mic|mike|microphone)\s+(?:unmute|on|chalu)(?:\s+(?:karo|kar\s*do))?\b/i;
  const BUDDY_SUBJECT = /\b(?:juno|pixie|buddy|robot|yourself|your\s+body)\b/i;
  const BUDDY_ACTIONS = [
    ['spin', /\b(?:spin|rotate|rotation|turn\s+around|twirl|360|three\s+sixty|ghoom\w*|ghum\w*|ghuma\w*)\b/i],
    ['jump', /\b(?:jump|hop|kood\w*|kud\w*|uchhal\w*)\b/i],
    ['wave', /\bwave\b|\bhaath\s+hila\w*|\bsay\s+hi\b/i],
    ['dance', /\b(?:dance|naach\w*|nach\w*)\b/i],
    ['celebrate', /\bcelebrat\w*|\bparty\b/i]
  ];
  const BUDDY_BARE = /^(?:do\s+a\s+|make\s+a\s+)?(?:360|three\s+sixty|spin|rotate|turn\s+around|twirl|jump|dance|celebrate)(?:\s+(?:around|once|again|360|three\s+sixty|degrees?|for\s+me|a\s+little))*$/i;
  function buddyAction(said) {
    if (!(BUDDY_SUBJECT.test(said) || BUDDY_BARE.test(said))) return null;
    const found = BUDDY_ACTIONS.find(([, verb]) => verb.test(said));
    return found ? { kind: 'buddy', action: found[0] } : null;
  }
  // "What is my current volume of my speaker?" -> read the real level.
  const VOLUME_STATUS = /\b(?:what(?:'s|\s+is)|how\s+(?:loud|high|low)\s+is|tell\s+me|check)\b.{0,30}\b(?:volume|sound\s+level)\b|\b(?:volume|awaaz|aawaz)\s+(?:abhi\s+)?(?:kitna|kitni|kitne)\b/i;
  function openApp(said) {
    for (const pattern of OPEN_APP) {
      const match = said.match(pattern);
      if (!match) continue;
      const app = group(match, 'app').replace(/\s+/g, ' ').replace(/^[\s.,']+|[\s.,']+$/g, '');
      if (!app || NOT_AN_APP.test(app)) return null;
      if (OPEN_APP_NAMES.has(app.toLowerCase()) || group(match, 'suffix') || group(match, 'dev')) return { kind: 'open_app', app };
      return null;
    }
    return null;
  }
  // "Can you show me the transcription that you are saying?" -> the Live transcript view (every client can do this).
  const TRANSCRIPT = "(?:transcripts?|transcriptions?|captions?|subtitles?|live\\s+text|(?:the\\s+)?text\\s+(?:of\\s+)?(?:what\\s+)?you(?:'re|\\s+are)?\\s+(?:saying|speaking)|what\\s+you(?:'re|\\s+are)\\s+saying)";
  const SHOW_TRANSCRIPT = re(`\\b(?:show|display|turn\\s+on|enable|open|see|give|put\\s+up|bring\\s+up|dikhao|dikha\\s*do|want)\\b.{0,40}\\b${TRANSCRIPT}|\\b${TRANSCRIPT}.{0,30}\\b(?:dikhao|dikha\\s*do|on\\s+karo|chalu\\s+karo|show\\s+karo)\\b`);
  const HIDE_TRANSCRIPT = re(`\\b(?:hide|turn\\s+off|disable|close|remove|stop\\s+showing|band\\s+karo|hatao)\\b.{0,40}\\b${TRANSCRIPT}|\\b${TRANSCRIPT}.{0,30}\\b(?:hatao|band\\s+karo|off\\s+karo|chhupao)\\b`);
  const SCREEN = '(?:screen|display|monitor)';
  const ANNOTATE = [
    re(`\\b(?:circle|mark|highlight|box|underline|point\\s+(?:out|to|at)|show\\s+me\\s+where|draw\\s+(?:on|around|a)|annotate|label|explain|what(?:'s|\\s+is|\\s+are)|which|where(?:'s|\\s+is)|find|teach\\s+me)\\b.{0,200}?\\b(?:on|in|at|from)\\s+(?:my|the|this)\\s+${SCREEN}\\b`),
    re(`^(?:explain|describe|read|look\\s+at|check|see|scan|annotate)\\s+(?:what(?:'s|\\s+is)\\s+on\\s+)?(?:my|the|this)\\s+${SCREEN}\\b`),
    re(`\\b${SCREEN}\\s+(?:pe|par|mein|me)\\b.{0,160}?\\b(?:mark|circle|highlight|dikhao|batao|samjhao|explain|kya\\s+hai|kahan\\s+hai|kaha\\s+hai|draw|gol\\s+karo|underline)\\b`),
    re(`\\b(?:mere|meri|meray)\\s+${SCREEN}\\b.{0,120}?\\b(?:dekho|dekh\\s+ke|samjhao|batao|explain|kya\\s+hai|mark)\\b`)
  ];
  const NOT_ANNOTATE = /\b(?:shar(?:e|ing)|screen\s*shot|screenshot|record(?:ing)?|brightness|turn\s+off|lock|wallpaper|resolution|split\s+screen|full\s*screen)\b/i;
  const CLEAR = [
    /^(?:clear|remove|erase|delete|wipe|hide)\s+(?:the\s+|all\s+(?:the\s+)?|those\s+|these\s+|your\s+)?(?:drawings?|marks?|markings?|annotations?|circles?|scribbles?|highlights?|red\s+marks?|pen\s+marks?)(?:\s+(?:from|on)\s+(?:my|the)\s+screen)?$/i,
    /^clear\s+(?:my|the)\s+screen$/i,
    /^(?:marks?|markings?|drawings?|annotations?|circles?|nishan)\s+(?:hatao|hata\s*do|saaf\s+karo|saaf\s+kar\s*do|mita\s*do|mitao)$|^screen\s+saaf\s+(?:karo|kar\s*do)$/i
  ];

  const PHONE = '(?:phone|mobile|android|smartphone)';
  const PHONE_TASK = [
    re(`^(?:on|using|use|with)\\s+(?:my|the)\\s+${PHONE}\\s*[,:-]?\\s*(?:to\\s+|and\\s+)?(?<task>.{3,})$`),
    re(`^(?<task>.{3,}?)\\s+(?:on|using|with)\\s+(?:my|the)\\s+${PHONE}$`),
    re(`^(?:mere|meray|apne)?\\s*${PHONE}\\s+(?:pe|par|mein|me|se)\\s+(?<task>.{3,})$`),
    re(`^(?<task>.{3,}?)\\s+(?:mere|meray|apne)?\\s*${PHONE}\\s+(?:pe|par|mein|me|se)(?:\\s+(?:karo|kar\\s*do|kar\\s*de))?$`)
  ];
  const PHONE_STOP = re(`^(?:stop|cancel|abort|end)\\s+(?:the\\s+)?${PHONE}\\s+(?:task|work|job|agent|automation)$|^${PHONE}\\s+(?:task|kaam|ka\\s+kaam)\\s+(?:band\\s+karo|band\\s+kar\\s*do|roko|rok\\s*do|cancel\\s+karo)$`);
  const PHONE_NOT_TASK = /^(?:is|are|was|were|it|this|that|my|the|a|an|number|call|ringing|charging|battery)$/i;

  function clean(text) {
    let value = String(text || '').replace(/’/g, "'").replace(/\s+/g, ' ').trim();
    let previous = null;
    while (previous !== value) {
      previous = value;
      value = value.replace(POLITE, '');
    }
    return value.replace(TRAIL, '').replace(/^[\s,.]+|[\s,.]+$/g, '');
  }
  const group = (match, name) => String(match?.groups?.[name] || '').trim();
  const appOf = (match) => {
    const app = group(match, 'app') || group(match, 'app2');
    return DEVICE_WORDS.has(app.toLowerCase()) ? '' : app;
  };
  const numberOf = (match) => (/^\d+$/.test(group(match, 'n')) ? Number(group(match, 'n')) : null);
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));

  /** The device action the user explicitly asked for, or null. `capabilities` (from the host) limits matches. */
  // 17 Oct: "What you can do? Like can you control my laptop?" — each sentence is checked on its own (Python twin).
  const SENTENCE_SPLIT = /[?!.\u0964]+|\s+(?:like|i mean|matlab|that is|basically)\s+/i;
  function asksCapabilities(said) {
    if (CAPABILITIES.test(said)) return true;
    return said.split(SENTENCE_SPLIT).some((part) => part.trim() && CAPABILITIES.test(clean(part)));
  }
  // Marker 217 (twins of desktop_intents._SCREEN_QUESTION / _SELF_WINDOW)
  const SCREEN_QUESTION = new RegExp(
    String.raw`\b(?:how\s+many|what|which|list|name|tell\s+me|kitne|kitni|kaun\s*kaun|kaun\s*se|kaunse|konse|kya\s+kya)\b`
    + String.raw`.{0,60}\b(?:apps?|applications?|windows|programs?|softwares?)\b.{0,80}`
    + String.raw`\b(?:open|opened|running|chal\s+rah\w*|khul\w*|taskbar|screen|laptop|computer|pc|desktop)\b`
    + String.raw`|\bwhat(?:'s|\s+is)\s+(?:open\s+)?on\s+(?:my|the)\s+taskbar\b`
    + String.raw`|\bwhich\s+(?:app|application|window)\s+is\s+(?:in\s+front|open|active|focused|on\s+top)\b`
    + String.raw`|\btaskbar\s+(?:mein|me|pe|par)\s+(?:kya|kitne|kaun)`, 'i');
  const SELF_WINDOW = new RegExp(
    String.raw`\b(?<verb>minimi[sz]e|maximi[sz]e|restore|hide|bring\s+back|make\s+full\s*screen)\s+(?:the\s+|this\s+)?`
    + String.raw`(?:jupiter(?:\s+(?:wt|at|app|application|window))*|your\s+(?:own\s+)?(?:app|window)|this\s+(?:jupiter\s+)?app)\b`
    + String.raw`(?!\s+(?:website|tab|page|folder|file))`, 'i');
  // Marker 218: twin of services/desktop_calculator.expression_of ("calculate 125 times 8" -> "125*8").
  const CALC_WORDS = [[/\b(?:multiplied\s+by|times|into|x|guna|gunaa)\b|[×✕]/g, '*'], [/\b(?:divided\s+by|over|bata|by)\b|÷/g, '/'],
    [/\b(?:plus|add|and|jod|jodo)\b/g, '+'], [/\b(?:minus|less|subtract|ghatao)\b|−/g, '-'], [/\b(?:point|dashamlav)\b/g, '.']];
  const calculatorExpression = (text) => {
    let value = String(text || '').toLowerCase();
    const asked = value.match(/\b(?:calculate|compute|solve|what\s+is|what's|kitna\s+hai|kitne)\b(.+)$/);
    if (asked) value = asked[1];
    value = value.replace(/(\d),(?=\d{3}\b)/g, '$1');
    for (const [pattern, symbol] of CALC_WORDS) value = value.replace(pattern, ` ${symbol} `);
    const expression = (value.match(/\d+(?:\.\d+)?|[+\-*/]/g) || []).join('');
    return /^\d+(?:\.\d+)?(?:[+\-*/]\d+(?:\.\d+)?)*$/.test(expression) && /[+\-*/]/.test(expression) ? expression : '';
  };
  function classify(text, capabilities = null) {
    const allowed = (kind) => !capabilities || Boolean(capabilities[kind]);
    const said = clean(text);
    if (!said || said.length > 600) return null;
    if (allowed('mobile')) {
      if (PHONE_STOP.test(said)) return { kind: 'phone_stop' };
      if (!QUESTION.test(said)) {
        for (const pattern of PHONE_TASK) {
          const match = said.match(pattern);
          if (!match) continue;
          const task = group(match, 'task').replace(/^[\s,.]+|[\s,.]+$/g, '');
          if (task.length >= 3 && !PHONE_NOT_TASK.test(task)) return { kind: 'phone_task', task };
        }
      }
    }
    if (['notch', 'volume', 'media', 'open_app'].some(allowed) && asksCapabilities(said)) return { kind: 'capabilities' };
    if (allowed('volume') && VOLUME_STATUS.test(said) && !NOT_AUDIO.test(said)) return { kind: 'volume_status' };
    if (allowed('window') && SCREEN_QUESTION.test(said)) return { kind: 'screen_info', question: said.slice(0, 200) };   // marker 217
    const self = allowed('notch') ? said.match(SELF_WINDOW) : null;
    if (self) {
      const verb = self.groups.verb.toLowerCase();
      return { kind: 'ui_mode', mode: verb.startsWith('maxim') || verb.includes('full') ? 'maximize'
        : verb.startsWith('restore') || verb.startsWith('bring') ? 'restore' : 'minimize' };
    }
    if (QUESTION.test(said)) return null;
    if (allowed('open_app') && /\b(?:calculator|calc)\b/i.test(said)) {      // marker 218 (twin of desktop_intents)
      const expression = calculatorExpression(said);
      if (expression) return { kind: 'calculator', expression };
    }
    // "In <window>, type auto mute test": the typed words are text, never a command of their own (marker 210).
    if (allowed('computer_use') && (EXPLICIT_TASK.test(said) || opensAppForTask(said))) return { kind: 'computer_task', goal: String(text || '').replace(/\s+/g, ' ').trim().slice(0, 300) };
    if (allowed('app_ui')) {
      if (MIC_UNMUTE.test(said)) return { kind: 'live_mic', muted: false };
      if (MIC_MUTE.test(said)) return { kind: 'live_mic', muted: true };
      if (HIDE_TRANSCRIPT.test(said)) return { kind: 'live_transcript', visible: false };
      if (SHOW_TRANSCRIPT.test(said)) return { kind: 'live_transcript', visible: true };
    }
    if (allowed('annotate')) {
      if (CLEAR.some((p) => p.test(said))) return { kind: 'clear_annotations' };
      if (!NOT_ANNOTATE.test(said) && ANNOTATE.some((p) => p.test(said))) return { kind: 'annotate', request: said.slice(0, 600) };
    }
    if (allowed('open_app')) {
      const opened = openApp(said) || openKnownApp(text);
      if (opened) {
        if (MAXIMIZE_WORD.test(text)) opened.maximize = true;          // "... already open, minimised, so maximize it"
        if (NEW_WINDOW.test(text)) opened.new_window = true;
        return opened;
      }
    }
    if (allowed('browser')) {
      const browsing = browserAction(text);
      if (browsing) return browsing;
    }
    if (allowed('window')) {
      const steps = windowSteps(text);
      if (steps.length) return { kind: 'window', steps };
    }
    if (allowed('buddy') || allowed('app_ui')) {
      const buddy = buddyAction(said);
      if (buddy) return buddy;
    }
    if (allowed('notch')) {
      for (const [pattern, mode] of UI) if (pattern.test(said)) return { kind: 'ui_mode', mode };
    }
    if (allowed('brightness')) {
      const light = brightness(said);
      if (light) return light;
    }
    if (allowed('volume')) {
      const change = volume(said);
      if (change) return change;
    }
    if (allowed('media')) {
      for (const [pattern, action] of MEDIA) {
        if (!pattern.test(said)) continue;
        // marker 215: "pause" pauses, "play" plays (twin of desktop_intents._media_key)
        if (action !== 'play_pause') return { kind: 'media_key', action };
        const mediaIntent = { kind: 'media_key', action, want: MEDIA_PAUSE.test(said) ? 'pause' : 'play' };
        const namedApp = said.match(/\b(spotify|youtube)\b/i);                 // marker 218 (twin of _media_key)
        return namedApp ? { ...mediaIntent, app: namedApp[1].toLowerCase() } : mediaIntent;
      }
      for (const pattern of PLAY) {
        const match = said.match(pattern);
        if (!match) continue;
        let query = group(match, 'q').replace(/^[\s,.'"]+|[\s,.'"?!]+$/g, '').replace(/\s+(?:for\s+me|please|na|yaar)$/i, '')
          .replace(/[\s,.!]*\b(?:i'?m|i\s+am)\s+(?:still\s+)?waiting\b.*$/i, '').replace(/[\s,.]+$/, '')    // marker 217
          .replace(/^(?:the\s+)?(?:song|track|gaana|gana)\s+(?:called\s+|named\s+)?(?=\S)/i, '');
        const app = (group(match, 'app') || 'youtube').toLowerCase().replace('yt', 'youtube');
        if (!query || NOT_MEDIA.test(query)) return null;
        if (/^(?:(?:play|it|now|please|again|na|yaar|jaldi|karo|do|kar|abhi)[\s,.!]*)+$/i.test(query)) {
          return { kind: 'media_key', action: 'play_pause', want: 'play' };                   // marker 217
        }
        if (GENERIC_MUSIC.test(query)) return { kind: 'media_key', action: 'play_pause', want: 'play' };
        if (ANY_MUSIC.test(query)) return { kind: 'play', query: ANY_MUSIC_QUERY, app };      // marker 214
        query = query.replace(/\s+(?:ke|ka|ki)\s+(?:gaane|gane|gaana|gana|songs?)$/i, ' songs').replace(/\s+(?:gaana|gana|song)$/i, '');
        return { kind: 'play', query: query.slice(0, 200), app };
      }
    }
    // Last: every specific action above wins; anything else concrete goes to computer control (marker 209).
    if (allowed('computer_use') && looksLikeComputerTask(text)) return { kind: 'computer_task', goal: String(text || '').replace(/\s+/g, ' ').trim().slice(0, 300) };
    return null;
  }

  // Follow-ups right after a volume change ("Now can you set to 40%?", "a bit more", "thoda kam").
  const FOLLOW_UP_MS = 90000;
  const FOLLOW_SET = /^(?:(?:set|make|put|change|turn|keep|bring)\s+(?:it\s+)?)?(?:to\s+|at\s+)?(\d{1,3})\s*(?:%|percent|per\s*cent|pratishat)?(?:\s+(?:kar\s*do|karo|pe\s+kar\s*do|rakho))?$/i;
  const FOLLOW_MORE = /^(?:(?:a\s+)?(?:little|bit|little\s+bit|tiny\s+bit|lot)\s+)?(?:more|louder|higher|up)$|^(?:thoda\s+|aur\s+|thoda\s+aur\s+)?(?:zyada|jyada|badhao|badha\s*do|tez)(?:\s+karo)?$/i;
  const FOLLOW_LESS = /^(?:(?:a\s+)?(?:little|bit|little\s+bit|tiny\s+bit|lot)\s+)?(?:less|lower|quieter|softer|down)$|^(?:thoda\s+|aur\s+|thoda\s+aur\s+)?(?:kam|dheere|dhire)(?:\s+karo)?$/i;
  /** A volume follow-up, only when the previous device action (within 90 s) was a volume change. */
  function followUp(text, last) {
    if (!last || last.kind !== 'volume') return null;
    const said = clean(text);
    const app = String(last.app || '');
    const set = said.match(FOLLOW_SET);
    if (set) return { kind: 'volume', set: Math.max(0, Math.min(100, Number(set[1]))), app };
    if (FOLLOW_MORE.test(said)) return { kind: 'volume', delta: 10, app };
    if (FOLLOW_LESS.test(said)) return { kind: 'volume', delta: -10, app };
    return null;
  }
  const pct = (value) => `${Math.round(Number(value) || 0)}%`;
  /** What to tell the user after the host did (or could not do) the action. Never claims more than the result. */
  function describe(intent, result) {
    const r = result || {};
    if (!r.ok) {
      const why = {
        untrusted: 'that only works in the Jupiter Windows app', audio_unavailable: "I can't reach the speakers right now",
        app_not_playing: `${r.app || 'that app'} isn't playing any sound right now, so I can't change its volume`,
        declined: 'you chose not to let me look at your screen', private_surface: 'the window in front looks private (passwords, banking or a vault), so I did not look at it',
        human_verification: 'that screen needs you (sign-in, CAPTCHA or a security prompt), so I did not look at it',
        no_vision_route: "my vision model isn't available right now", artemis_not_installed: "the phone agent (ARTEMIS) isn't set up on this laptop yet. Run scripts/setup_artemis.py first",
        no_device: 'no Android phone or emulator is connected', do_not_disturb: "Do Not Disturb is on, so the phone won't let me change the volume", busy: 'a phone task is already running. Say "stop the phone task" first',
        open_failed: "I couldn't open it", unsupported: 'that only works in the Jupiter Windows app',
        not_found: `I couldn't find an app called “${r.app || intent.app || 'that'}” on this device`,
        empty_app: 'I did not catch which app to open',
        no_window: `${r.app || 'that app'} has no open window`, nothing_opened: "I haven't opened a window in this conversation",
        already_closed: `the ${r.app || ''} window I opened is already closed`.replace('  ', ' '),
        several_windows: `${r.app || 'that app'} has ${r.count || 'several'} windows open, so I did not guess which one to close. Say which one, or close it with its X`,
        no_app: 'I did not catch which window you mean', window_failed: "Windows didn't let me change that window",
        // marker 218: the action ran but Windows did not show the result, so it is NOT reported as done
        no_window_appeared: `I started ${r.app || intent.app || 'the app'}, but no window of it appeared on your screen`,
        still_open: `${r.app || 'that app'} is still open (it may be asking you to save something)`,
        browser_no_change: `I asked ${r.browser || 'the browser'} to open it, but nothing changed in its window`,
        no_result: "Calculator didn't show an answer", unsafe_expression: 'I can only do plain arithmetic in Calculator',
        unsafe_url: 'I can only open public web addresses (https)', empty_query: 'I did not catch what to search for',
        browser_not_found: `${r.browser || 'that browser'} isn't installed on this laptop`,
        busy_control: 'I am already working in a window. Say “stop computer use” first, then ask again',
        no_task_window: 'there is no open window I can work in. Open the app first, then ask again',
        app_not_opened: `I couldn't open ${r.detail || 'that app'}, so I didn't start the task`,
        declined: 'you chose not to let me control that window', control_failed: `computer control could not start (${r.detail || 'unavailable'})`,
        buddy_hidden: 'your buddy only appears during a Live conversation. Start Live and ask again', buddy_off: 'your buddy is turned off in Settings › Personalization › Buddy',
        live_not_open: 'that belongs to Live voice. Start Live first',
        no_brightness_control: "this screen's brightness can't be changed from Windows (an external monitor uses its own buttons)",
        brightness_failed: "Windows didn't let me change the brightness"
      }[r.error] || 'it did not work';
      return `Sorry, ${why}. Nothing was changed.`;
    }
    switch (intent.kind) {
      case 'brightness':
        return r.after === r.before ? `Brightness is already ${pct(r.after)}.` : `Brightness set to ${pct(r.after)}.`;
      case 'volume':
        if (r.muted && intent.mute) return `${r.app ? `${r.app} is` : 'Sound is'} muted.`;
        if (intent.mute === false) return `${r.app ? `${r.app} is` : 'Sound is'} unmuted (${pct(r.after)}).`;
      {
        const label = r.app ? `${r.app} volume` : 'Volume';
        if (typeof intent.set === 'number') {
          // Phones have ~15 volume steps: say the real level and that it is the nearest one ("13%, closest to 10%").
          return Math.abs(Number(r.after) - intent.set) > 2
            ? `${label} set to ${pct(r.after)} (the closest step to ${pct(intent.set)}).` : `${label} set to ${pct(r.after)}.`;
        }
        if (r.after === r.before) {
          return `${label} is already at ${pct(r.after)}${r.after >= 100 ? ' (the maximum)' : r.after <= 0 ? ' (the minimum)' : ''}.`;
        }
        return `${label} ${r.after > r.before ? 'up' : 'down'} to ${pct(r.after)}.`;
      }
      case 'media_key':
        if (intent.want && r.method === 'media_session') {           // marker 215: the real state, not "I sent a key"
          if (!r.changed && r.tried) return intent.want === 'pause' ? `I asked ${r.app || 'the player'} to pause, but it kept playing.`   // marker 218
            : `I asked ${r.app || 'the player'} to play, but it didn't start. Press play in ${r.app || 'the app'}.`;
          if (!r.changed) return intent.want === 'pause' ? 'Nothing is playing right now.' : 'It is already playing.';
          return intent.want === 'pause' ? `Paused${r.app ? ` ${r.app}` : ''}.` : `Playing${r.app ? ` ${r.app}` : ''} again.`;
        }
        return { play_pause: 'I sent play/pause to the app that is playing.', next: 'I sent “next track” to the app that is playing.',
          previous: 'I sent “previous track” to the app that is playing.', stop: 'I sent stop to the app that is playing.' }[intent.action] || 'Done.';
      case 'play':
        if (/spotify/i.test(String(r.app || ''))) {
          // Marker 215: only "playing" when Spotify really started (its title became the song)
          if (r.playing && r.ad) return `Spotify is playing an ad first; “${r.now_playing || r.query}” starts right after it.`;   // marker 217
          return r.playing ? `Playing on Spotify now: ${r.now_playing || r.query}.`
            : `I opened “${r.query}” in Spotify but it didn't start playing. Press play on the song you want.`;
        }
        return r.autoplay ? `Playing “${r.query}” on YouTube.` : `Opened YouTube results for “${r.query}”.`;
      case 'capabilities':
        return r.platform === 'android' ? CAPABILITIES_REPLY_PHONE : CAPABILITIES_REPLY;
      case 'ui_mode':
        return { expand: 'The notch is expanded.', maximize: 'Done, Jupiter is full size.', minimize: "I'm in the notch at the top of your screen. Say “come back” or press ↗ to open me again.",
          restore: "I'm back.", expanded: 'The notch is expanded.', compact: 'The notch is small now.', hidden: 'The notch is hidden.' }[intent.mode] || 'Done.';
      case 'open_app':
        if (r.how === 'existing_window') return r.maximized ? `${r.app || intent.app} was already open, so I brought that window back and maximized it.` : `${r.app || intent.app} was already open, so I brought that window to the front.`;
        return `Opened ${r.app || intent.app}${intent.new_window ? ' in a new window' : ''}.`;
      case 'window': {
        const verbs = { maximize: 'maximized', minimize: 'minimized', restore: 'restored', close: 'closed', focus: 'brought to the front' };
        const done = (r.steps || []).map((step) => (step.target === 'last' ? `${verbs[step.action] || 'changed'} the ${step.app || ''} window I opened`.replace('  ', ' ')
          : `${verbs[step.action] || 'changed'} the ${step.app ? `${step.app} ` : ''}window`));
        return done.length ? `Done: I ${done.join(', then ')}.` : 'Done.';
      }
      case 'browser':
        if (intent.action === 'search') return `Searched ${intent.engine === 'youtube' ? 'YouTube' : 'Google'} for “${intent.query}” in a new ${r.browser || 'browser'} tab.`;
        if (intent.action === 'open_url') return `Opened ${r.site || intent.url} in a new ${r.browser || 'browser'} tab.`;
        return /chrome/i.test(String(r.browser || '')) ? 'Opened a new tab in Google Chrome (on Google’s page).' : `Opened a new tab in ${r.browser || 'your browser'}.`;
      case 'computer_task':
        return r.summary || 'Working on it.';
      case 'calculator':                                                         // marker 218
        return r.result ? `${String(intent.expression).replace(/\*/g, ' × ').replace(/\//g, ' ÷ ').replace(/\+/g, ' + ').replace(/-/g, ' − ')} = ${r.result} (shown in Calculator).`
          : "I couldn't get the answer from Calculator.";
      case 'screen_info':                                                        // marker 217
        return r.summary || "I couldn't read the taskbar right now.";
      case 'live_mic':
        return intent.muted ? 'Mic muted: I won’t hear the room now. Type to me, or tap Unmute when you want to talk.'
          : 'Mic is on again: I can hear you.';
      case 'buddy':
        if (r.buddy === 'pixie') return `Pixie has no body to ${intent.action === 'wave' ? 'wave with' : intent.action}, so she did a happy wiggle instead. Choose Juno in Settings › Personalization › Buddy for spins, jumps and dances.`;
        return { spin: 'Juno did a full 360° spin.', jump: 'Juno jumped.', wave: 'Juno waved.', dance: 'Juno is dancing.', celebrate: 'Juno is celebrating.' }[intent.action] || 'Done.';
      case 'volume_status':
        return `Your speaker volume is at ${pct(r.after)}${r.muted ? ' (muted)' : ''}.`;
      case 'live_transcript':
        if (!intent.visible) return 'Transcript hidden.';
        return r.buddy ? 'The transcript is on: you can read it in Live and in your buddy at the top of the screen.'
          : 'The transcript is on your screen now: you will see what you say and what I say.';
      case 'clear_annotations':
        return r.cleared ? 'Cleared the marks from your screen.' : 'There were no marks on your screen.';
      default:
        return r.summary || 'Done.';
    }
  }

  const api = { classify, describe, clean, followUp, FOLLOW_UP_MS };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.JupiterDesktopAssistantIntents = api;
})(typeof window !== 'undefined' ? window : null);
