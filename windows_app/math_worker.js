/* Local bounded symbolic work; never run model-supplied executable code. */
importScripts('assets/nerdamer/all.min.js?v=1.1.13');
const allowed = new Set(['x', 'pi', 'e', 'sin', 'cos', 'tan', 'asin', 'acos', 'atan', 'sinh', 'cosh', 'tanh', 'sqrt', 'abs', 'log', 'exp']);
function expression(value) {
  if (typeof value !== 'string' || !value.trim() || value.length > 160
      || /[^a-z0-9.+\-*/^()\s]/.test(value)) throw Error('Use a mathematical expression in x.');
  if ((value.match(/[a-z]+/g) || []).some(name => !allowed.has(name))) throw Error('Unsupported function or variable.');
  let depth = 0;
  for (const char of value) {
    if (char === '(' && ++depth > 12) throw Error('Expression is too deeply nested.');
    if (char === ')' && --depth < 0) throw Error('Check parentheses.');
  }
  if (depth || (value.match(/\^/g) || []).length > 8) throw Error('Simplify the expression.');
  return value;
}
self.onmessage = event => {
  try {
    const { component, props } = event.data;
    const input = expression(props.expression);
    const parsed = nerdamer(input);
    if (component === 'function_graph') {
      const min = props.xMin, max = props.xMax;
      if (!Number.isFinite(min) || !Number.isFinite(max) || min >= max
          || Math.abs(min) > 10000 || Math.abs(max) > 10000) throw Error('Choose a finite increasing x range within ±10000.');
      const points = [];
      for (let i = 0; i <= 240; i++) {
        const x = min + (max - min) * i / 240;
        let y = NaN;
        try { y = Number(parsed.sub('x', nerdamer(String(x))).evaluate().text('decimals')); } catch (_) {}
        points.push([x, Number.isFinite(y) && Math.abs(y) <= 1e8 ? y : null]);
      }
      self.postMessage({ points }); return;
    }
    if (!['differentiation_calculator', 'integration_calculator'].includes(component)) throw Error('Unknown component.');
    const result = component === 'differentiation_calculator' ? nerdamer.diff(parsed, 'x') : nerdamer.integrate(parsed, 'x');
    if (component === 'integration_calculator' && result.hasIntegral()) {
      self.postMessage({ unavailable: true }); return;
    }
    const tex = result.toTeX(), text = result.toString();
    if (tex.length > 6000 || text.length > 4000) throw Error('Result is too large; simplify the expression.');
    self.postMessage({ tex, text, constant: component === 'integration_calculator' });
  } catch (_) { self.postMessage({ error: 'Unable to calculate this expression. Check the syntax or simplify it.' }); }
};
