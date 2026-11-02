(function (global) {
  'use strict';
  // Jupiter copyable content blocks: one reusable card for anything the user is likely to copy — code, prompts,
  // messages/replies to send, emails, templates and other reusable text. Pure string rendering (no DOM needed to
  // build markup) plus ONE delegated click handler, so blocks rendered by innerHTML during streaming, history
  // restore or edits all work without per-render wiring. Content is always escaped by the caller's escape().

  const TEXT_KINDS = Object.freeze({
    prompt: 'prompt', 'system-prompt': 'prompt',
    message: 'message', reply: 'message', sms: 'message', whatsapp: 'message', dm: 'message', chat: 'message',
    email: 'email', mail: 'email',
    template: 'template',
    text: 'text', txt: 'text', plaintext: 'text', plain: 'text', note: 'text', caption: 'text', bio: 'text',
    post: 'text', tweet: 'text', markdown: 'text', md: 'text'
  });
  const LABELS = Object.freeze({ code: 'Code', prompt: 'Prompt', message: 'Message', email: 'Email',
    template: 'Template', text: 'Text' });
  const ICONS = Object.freeze({ code: 'ph-code', prompt: 'ph-sparkle', message: 'ph-chat-circle-text',
    email: 'ph-envelope-simple', template: 'ph-file-text', text: 'ph-text-align-left' });
  const LANGUAGE_NAMES = Object.freeze({
    js: 'JavaScript', javascript: 'JavaScript', jsx: 'JSX', ts: 'TypeScript', typescript: 'TypeScript', tsx: 'TSX',
    py: 'Python', python: 'Python', sh: 'Shell', bash: 'Bash', shell: 'Shell', zsh: 'Shell', console: 'Terminal',
    powershell: 'PowerShell', ps1: 'PowerShell', ps: 'PowerShell', bat: 'Batch', cmd: 'Batch',
    html: 'HTML', css: 'CSS', scss: 'SCSS', json: 'JSON', yaml: 'YAML', yml: 'YAML', xml: 'XML', toml: 'TOML',
    sql: 'SQL', java: 'Java', kotlin: 'Kotlin', kt: 'Kotlin', swift: 'Swift', c: 'C', cpp: 'C++', 'c++': 'C++',
    cs: 'C#', csharp: 'C#', go: 'Go', rust: 'Rust', rs: 'Rust', php: 'PHP', rb: 'Ruby', ruby: 'Ruby', dart: 'Dart',
    r: 'R', lua: 'Lua', diff: 'Diff', dockerfile: 'Dockerfile', ini: 'INI', env: 'ENV', regex: 'Regex'
  });
  // A lead-in sentence right before an UNTAGGED block (or a quote) that says what the block is for.
  const LEAD_IN_RULES = [
    ['prompt', /\bprompts?\b/i],
    ['email', /\b(e-?mail|mail)\b/i],
    ['message', /\b(message|reply|replies|text (?:him|her|them|back)|dm|whatsapp|sms|response you can send)\b/i],
    ['template', /\btemplates?\b/i],
    ['text', /\b(caption|bio|post|tweet|status|note|script|draft|announcement|description)\b/i]
  ];
  const COPY_INTENT = /(:\s*$)|\b(copy|paste|send|forward|use (?:this|it)|here(?:['’]s| is| are)|below)\b/i;

  function leadInKind(leadIn) {
    const text = String(leadIn || '').replace(/\s+/g, ' ').trim().slice(-240);
    if (!text || !COPY_INTENT.test(text)) return null;
    const sentence = text.split(/(?<=[.!?])\s+/).pop() || text;       // the sentence introducing the block
    for (const [kind, pattern] of LEAD_IN_RULES) if (pattern.test(sentence)) return kind;
    return null;
  }

  function classify(language, leadIn = '') {
    const lang = String(language || '').trim().toLowerCase();
    if (lang && TEXT_KINDS[lang]) {
      return { kind: TEXT_KINDS[lang], label: lang === 'markdown' || lang === 'md' ? 'Markdown' : LABELS[TEXT_KINDS[lang]] };
    }
    if (lang) return { kind: 'code', label: LANGUAGE_NAMES[lang] || lang.charAt(0).toUpperCase() + lang.slice(1) };
    const kind = leadInKind(leadIn);
    return kind ? { kind, label: LABELS[kind] } : { kind: 'code', label: LABELS.code };
  }

  // body = raw (unescaped) text; escape = the caller's HTML escaper. Code keeps <pre data-language><code>.
  function render({ body = '', language = '', leadIn = '', escape, streaming = false } = {}) {
    const esc = typeof escape === 'function' ? escape : (value) => String(value || '');
    const { kind, label } = classify(language, leadIn);
    const lang = String(language || '').trim();
    const langAttr = lang ? ` data-language="${esc(lang)}"` : '';
    const proseClass = kind === 'code' ? '' : ' class="copyable-prose"';
    return `<div class="copyable-block" data-copy-kind="${kind}"${streaming ? ' data-streaming="true"' : ''}>`
      + '<div class="copyable-block-header">'
      + `<span class="copyable-block-label"><i class="ph ${ICONS[kind]}" aria-hidden="true"></i><span>${esc(label)}</span></span>`
      + `<button type="button" class="copyable-block-copy" data-copyable-copy aria-label="Copy ${esc(label.toLowerCase())}">`
      + '<i class="ph ph-copy" aria-hidden="true"></i><span class="action-label">Copy</span></button>'
      + '</div>'
      + `<pre${langAttr}${proseClass}><code>${esc(body)}</code></pre>`
      + '</div>';
  }

  // Exact block text: textContent keeps indentation, blank lines and trailing spaces inside the block.
  function textOf(block) {
    return String(block?.querySelector?.('pre > code')?.textContent ?? '');
  }

  let installed = false;
  function install(doc = global.document, copy) {
    if (installed || !doc || typeof copy !== 'function') return false;
    installed = true;
    doc.addEventListener('click', (event) => {
      const button = event.target?.closest?.('[data-copyable-copy]');
      if (!button) return;
      event.preventDefault();
      const block = button.closest('.copyable-block');
      if (block) void copy(button, textOf(block));
    });
    return true;
  }

  global.JupiterCopyable = Object.freeze({ classify, leadInKind, render, textOf, install, TEXT_KINDS, LABELS });
})(typeof window !== 'undefined' ? window : globalThis);
