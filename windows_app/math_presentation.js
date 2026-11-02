(function (global) {
  'use strict';
  const escape = text => String(text).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const cache = new Map();
  function render(tex, display = false) {
    if (!global.katex || tex.length > 6000) return escape(tex);
    const key = `${display}:${tex}`;
    if (cache.has(key)) return cache.get(key);
    let html;
    try {
      html = global.katex.renderToString(tex, { displayMode: display, throwOnError: true,
        trust: false, strict: 'error', maxSize: 10, maxExpand: 200, output: 'htmlAndMathml' });
    } catch (_) { html = `<span class="math-fallback">${escape(tex)}</span>`; }
    if (cache.size >= 64) cache.delete(cache.keys().next().value);
    cache.set(key, html);
    return html;
  }
  // Fences are already protected by the Markdown parser. Protect inline code
  // here as well; only the trusted renderer's output is restored after escaping.
  function extract(source, streaming = false) {
    const html = [];
    const text = source.replace(/(`[^`\n]*`)|\$\$([\s\S]*?)\$\$|\\\[([\s\S]*?)\\\]|\\\(([^\n]*?)\\\)|(?<![\\$])\$([^$\n]+)\$(?!\d)/g,
      (match, code, block, bracket, paren, inline) => {
        if (code) return code;
        const display = block !== undefined || bracket !== undefined;
        const tex = (block ?? bracket ?? paren ?? inline).trim();
        // Currency/prose pairs are not equations. Explicit \(...\) remains
        // available for ambiguous numeric-only inline expressions.
        if (inline !== undefined && !(/\\[a-zA-Z]+|[_^=+*/{}]|^[a-zA-Z]$/.test(tex))) return match;
        const rendered = streaming ? escape(match) : render(tex, display);
        html.push(display ? `<span class="math-display">${rendered}</span>` : rendered);
        return `\u0000MATH${html.length - 1}\u0000`;
      });
    return { text, restore: value => value.replace(/\u0000MATH(\d+)\u0000/g, (_, i) => html[Number(i)] || '') };
  }
  // Plain (non-Markdown) text such as USER bubbles: everything is escaped; only spans produced by the trusted
  // KaTeX renderer (or its escaped fallback for malformed TeX) are inserted as HTML.
  function renderPlainWithMath(source) {
    const value = String(source ?? '');
    if (!/\\\(|\\\[|\$/.test(value)) return escape(value);
    const math = extract(value, false);
    return math.restore(escape(math.text));
  }
  global.JupiterMath = Object.freeze({ extract, render, renderPlainWithMath, escape });
})(typeof window !== 'undefined' ? window : globalThis);
