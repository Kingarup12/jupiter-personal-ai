(function (global) {
  'use strict';
  const registry = Object.freeze(Object.fromEntries([
    ['calculator', 'Calculator'], ['matrix_determinant', 'Matrix determinant'],
    ['differentiation_calculator', 'Differentiation calculator'],
    ['integration_calculator', 'Integration calculator'], ['function_graph', 'Function graph']
  ].map(([component_id, title]) => [component_id, Object.freeze({ component_id, title, version: 1, validate: normalize, render: mount })])));
  const mathNames = new Set(['x','pi','e','sin','cos','tan','asin','acos','atan','sinh','cosh','tanh','sqrt','abs','log','exp']);
  function validExpression(value) {
    if (typeof value !== 'string' || !value.trim() || value.length > 160 || /[^a-z0-9.+\-*/^()\s]/.test(value)
      || (value.match(/[a-z]+/g) || []).some(name => !mathNames.has(name))) return false;
    let depth = 0;
    for (const char of value) {
      if (char === '(') depth++;
      if (char === ')') depth--;
      if (depth < 0 || depth > 12) return false;
    }
    return depth === 0 && (value.match(/\^/g) || []).length <= 8;
  }
  function normalize(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)
      || Object.keys(value).some(k => !['type', 'component', 'version', 'props'].includes(k))
      || value.type !== 'interactive_response' || value.version !== 1) return null;
    const props = Object.hasOwn(value, 'props') ? value.props : {};
    if (!props || typeof props !== 'object' || Array.isArray(props)) return null;
    const size = Object.hasOwn(props, 'size') ? props.size : 3;
    if (value.component === 'calculator' && !Object.keys(props).length)
      return { type: value.type, version: 1, component: value.component, props: {} };
    if (value.component === 'matrix_determinant' && Object.keys(props).every(k => k === 'size')
      && Number.isInteger(size) && size >= 2 && size <= 5)
      return { type: value.type, version: 1, component: value.component, props: { size } };
    if (['differentiation_calculator','integration_calculator','function_graph'].includes(value.component)) {
      const graph = value.component === 'function_graph';
      const expression = Object.hasOwn(props, 'expression') ? props.expression : 'x^2';
      if (Object.keys(props).some(k => !(graph ? ['expression','xMin','xMax'] : ['expression']).includes(k))
          || !validExpression(expression)) return null;
      const normalized = { expression };
      if (graph) {
        const min = Object.hasOwn(props,'xMin') ? props.xMin : -10, max = Object.hasOwn(props,'xMax') ? props.xMax : 10;
        if (!Number.isFinite(min) || !Number.isFinite(max) || Math.abs(min)>10000 || Math.abs(max)>10000 || min>=max) return null;
        Object.assign(normalized, { xMin: min, xMax: max });
      }
      return {type:value.type, version:1, component:value.component, props:normalized};
    }
    return null;
  }
  // Recursive descent over a bounded numeric grammar; never execute input.
  // Exponent notation is accepted only as part of a number (e.g. a previous large result "1e+21").
  const NUMBER = /^(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/;
  function calculate(source) {
    if (!source || source.length > 200 || /[^\d.()+\-*/\se]/.test(source)) throw Error('Enter a valid calculation.');
    const tokens = source.match(/(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?|[()+\-*/]|e/g) || [];
    let index = 0, depth = 0;
    const primary = () => {
      if (++depth > 32) throw Error('Calculation is too deeply nested.');
      let value;
      const token = tokens[index++];
      if (token === '+' || token === '-') value = (token === '-' ? -1 : 1) * primary();
      else if (token === '(') { value = expression(); if (tokens[index++] !== ')') throw Error('Check parentheses.'); }
      else { if (!token || !NUMBER.test(token)) throw Error('Check the calculation.'); value = Number(token); }
      depth--; return value;
    };
    const product = () => { let value = primary(); while (['*', '/'].includes(tokens[index])) { const op = tokens[index++], right = primary(); if (op === '/' && right === 0) throw Error('Cannot divide by zero.'); value = op === '*' ? value * right : value / right; } return value; };
    const expression = () => { let value = product(); while (['+', '-'].includes(tokens[index])) { const op = tokens[index++], right = product(); value = op === '+' ? value + right : value - right; } return value; };
    const result = expression();
    if (index !== tokens.length || !Number.isFinite(result)) throw Error('Check the calculation or use smaller numbers.');
    return Number(result.toPrecision(12)).toString();
  }
  // ── Calculator state machine (pure; the UI only renders its state) ─────────────────────────────
  // expr uses ASCII operators; justEvaluated marks a committed result so the next key is interpreted
  // conventionally: operator → continue from the result, digit/decimal/"(" → new calculation.
  const OPS = new Set(['+', '-', '*', '/']);
  const calculatorInitial = () => ({ expr: '', history: '', result: null, justEvaluated: false, error: '' });
  const unaryMinusAt = (expr, i) => expr[i] === '-' && (i === 0 || OPS.has(expr[i - 1]) || expr[i - 1] === '(');
  function lastNumber(expr) {
    const match = /(\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$|\d*\.$/.exec(expr);
    return match ? { start: match.index, text: match[0] } : null;
  }
  function calculatorStep(state, key) {
    const s = { ...(state || calculatorInitial()) };
    const digit = /^\d$/.test(key), op = OPS.has(key);
    if (key === 'clear') return calculatorInitial();
    if (s.error) {
      if (digit || key === '.' || key === '(' || key === 'backspace') {
        const fresh = { ...calculatorInitial(), history: s.history };
        return key === 'backspace' ? fresh : calculatorStep(fresh, key);
      }
      return s;                                  // operators/equals after an error are ignored
    }
    if (s.justEvaluated) {
      if (op) return { ...s, expr: s.result + key, justEvaluated: false };
      if (digit || key === '.' || key === '(') return calculatorStep({ ...s, expr: '', justEvaluated: false }, key);
      if (key === 'negate') {
        const negated = s.result.startsWith('-') ? s.result.slice(1) : (s.result === '0' ? '0' : '-' + s.result);
        return { ...s, result: negated, expr: negated };
      }
      if (key === 'backspace') return { ...s, history: '' };   // results are not edited digit-by-digit
      return s;                                               // ')' and repeated '=' leave the result
    }
    const expr = s.expr, last = expr.slice(-1);
    const open = (expr.match(/\(/g) || []).length - (expr.match(/\)/g) || []).length;
    const limit = value => ({ ...s, expr: value.slice(0, 200) });
    if (digit) {
      const num = lastNumber(expr);
      if (last === ')') return limit(expr + '*' + key);
      if (num && num.text === '0') return limit(expr.slice(0, -1) + key);
      return limit(expr + key);
    }
    if (key === '.') {
      const num = lastNumber(expr);
      if (num && num.text.includes('.')) return s;
      if (last === ')') return limit(expr + '*0.');
      return limit(expr + (num ? '.' : '0.'));
    }
    if (op) {
      if (!expr) return key === '-' ? limit('-') : s;
      if (last === '(') return key === '-' ? limit(expr + '-') : s;
      if (OPS.has(last)) {
        if (key === '-' && (last === '*' || last === '/') ) return limit(expr + '-');
        if (unaryMinusAt(expr, expr.length - 1)) return expr.length === 1 ? s : limit(expr.slice(0, -2) + key);
        return limit(expr.slice(0, -1) + key);
      }
      return limit((last === '.' ? expr.slice(0, -1) : expr) + key);
    }
    if (key === '(') return limit(expr + (/[\d.)]$/.test(expr) ? '*(' : '('));
    if (key === ')') return open > 0 && /[\d.)]$/.test(expr) ? limit(expr + ')') : s;
    if (key === 'backspace') return { ...s, expr: expr.slice(0, -1) };
    if (key === 'negate') {
      const num = lastNumber(expr);
      if (!num) return s;
      const i = num.start - 1;
      if (i >= 0 && unaryMinusAt(expr, i)) return limit(expr.slice(0, i) + expr.slice(num.start));
      return limit(expr.slice(0, num.start) + '-' + expr.slice(num.start));
    }
    if (key === '=') {
      if (!expr || expr === '-') return s;
      const complete = (OPS.has(last) ? expr.slice(0, -1) : expr) + ')'.repeat(Math.max(0, open));
      const shown = calculatorDisplay(complete) + ' =';
      try {
        const result = calculate(complete);
        return { expr: result, history: shown, result, justEvaluated: true, error: '' };
      } catch (error) {
        return { ...s, history: shown, error: error.message, justEvaluated: false };
      }
    }
    return s;
  }
  function calculatorDisplay(expr) {
    let out = '';
    for (let i = 0; i < expr.length; i++) {
      const ch = expr[i];
      if (ch === '-' && unaryMinusAt(expr, i)) out += '−';
      else if (OPS.has(ch)) out += ` ${({ '*': '×', '/': '÷', '-': '−', '+': '+' })[ch]} `;
      else out += ch;
    }
    return out.replace(/\s+/g, ' ').trim();
  }
  function determinant(matrix) {
    if (matrix.length === 1) return matrix[0][0];
    return matrix[0].reduce((sum, value, col) => sum + (col % 2 ? -1 : 1) * value
      * determinant(matrix.slice(1).map(row => row.filter((_, index) => index !== col))), 0);
  }
  function mount(host, payload, ownsMessage) {
    const schema = normalize(payload);
    if (!schema || !host) return;
    const panel = document.createElement('section'); panel.className = 'interactive-response';
    panel.dataset.componentId = schema.component; panel.dataset.componentVersion = String(schema.version);
    const title = document.createElement('strong'); title.textContent = registry[schema.component].title; panel.append(title);
    const output = document.createElement('output'); output.setAttribute('aria-live', 'polite');
    const owned = () => panel.isConnected && ownsMessage();
    const action = (parent, label, callback) => {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = label;
      button.addEventListener('click', () => { if (!owned()) return; try { callback(); } catch (error) { output.textContent = error.message; } }); parent.append(button); return button;
    };
    if (schema.component === 'calculator') {
      mountCalculator(panel, output, owned, action);
    } else if (schema.component === 'matrix_determinant') {
      const select = document.createElement('select'); select.setAttribute('aria-label', 'Matrix size');
      for (let n = 2; n <= 5; n++) { const option = document.createElement('option'); option.value = String(n); option.textContent = `${n} × ${n}`; select.append(option); }
      select.value = String(schema.props.size); panel.append(select);
      const grid = document.createElement('div'); grid.className = 'interactive-matrix-cells'; panel.append(grid);
      const reset = () => { grid.replaceChildren(); const n = Number(select.value); grid.style.gridTemplateColumns = `repeat(${n}, minmax(0, 1fr))`; for (let i = 0; i < n*n; i++) { const input = document.createElement('input'); input.type = 'number'; input.step = 'any'; input.value = '0'; input.setAttribute('aria-label', `Row ${Math.floor(i/n)+1}, column ${i%n+1}`); grid.append(input); } output.textContent = ''; };
      select.addEventListener('change', () => { if (owned()) reset(); }); reset();
      const controls = document.createElement('div'); controls.className = 'interactive-response-controls'; panel.append(controls);
      action(controls, 'Determinant', () => { const n = Number(select.value); const values = [...grid.children].map(input => input.value.trim() ? Number(input.value) : NaN); if (values.some(v => !Number.isFinite(v) || Math.abs(v) > 1e12)) throw Error('Enter finite numbers between −1e12 and 1e12.'); const result = determinant(Array.from({length:n}, (_, r) => values.slice(r*n, (r+1)*n))); if (!Number.isFinite(result)) throw Error('Use smaller matrix values.'); output.textContent = `Determinant: ${Number(result.toPrecision(12))}`; });
      action(controls, 'Clear', reset);
    } else {
      mountMath(panel, schema, output, owned, action);
    }
    if (!output.isConnected && !panel.contains(output)) panel.append(output);
    host.append(panel);
  }
  const CALC_KEYBOARD = Object.freeze({
    '+': '+', '-': '-', '*': '*', 'x': '*', 'X': '*', '/': '/', '(': '(', ')': ')', '.': '.', ',': '.',
    'Enter': '=', '=': '=', 'Backspace': 'backspace', 'Escape': 'clear', 'Delete': 'clear', 'F9': 'negate'
  });
  function calculatorKeyFromEvent(event) {
    if (event.ctrlKey || event.metaKey || event.altKey) return null;
    if (/^\d$/.test(event.key)) return event.key;
    return CALC_KEYBOARD[event.key] || null;
  }
  function mountCalculator(panel, output, owned, action) {
    const prefs = (global.JupiterUxPreferences?.current?.() || {});
    if (prefs.calculator_density === 'compact') panel.classList.add('interactive-calculator-compact');
    if (prefs.calculator_density === 'expanded') panel.classList.add('interactive-calculator-expanded');
    if (prefs.calculator_large_result) panel.classList.add('interactive-calculator-large-result');
    let state = calculatorInitial();
    const display = document.createElement('div'); display.className = 'interactive-calculator-display';
    const history = document.createElement('div'); history.className = 'calc-history'; history.setAttribute('aria-hidden', 'true');
    output.className = 'calc-current'; output.setAttribute('aria-label', 'Calculator display');
    display.append(history, output); panel.append(display);
    const render = () => {
      history.textContent = state.history;
      output.textContent = state.error || (state.justEvaluated ? state.result : calculatorDisplay(state.expr)) || '0';
      output.classList.toggle('calc-error', Boolean(state.error));
      output.classList.toggle('calc-result', state.justEvaluated);
    };
    const press = key => { state = calculatorStep(state, key); render(); };
    const grid = document.createElement('div'); grid.className = 'interactive-calculator-keys'; panel.append(grid);
    const layout = [['C', 'clear', 'Clear'], ['⌫', 'backspace', 'Backspace'], ['(', '(', 'Open parenthesis'], [')', ')', 'Close parenthesis'],
      ['7', '7'], ['8', '8'], ['9', '9'], ['÷', '/', 'Divide'],
      ['4', '4'], ['5', '5'], ['6', '6'], ['×', '*', 'Multiply'],
      ['1', '1'], ['2', '2'], ['3', '3'], ['−', '-', 'Subtract'],
      ['±', 'negate', 'Change sign'], ['0', '0'], ['.', '.', 'Decimal point'], ['+', '+', 'Add'],
      ['=', '=', 'Calculate']];
    for (const [label, key, aria] of layout) {
      const button = action(grid, label, () => press(key));
      if (aria) button.setAttribute('aria-label', aria);
      if (key === '=') button.classList.add('calc-equals');
      if (OPS.has(key)) button.classList.add('calc-operator');
    }
    panel.tabIndex = 0;
    panel.setAttribute('aria-label', 'Calculator. Type numbers and operators; Enter calculates, Escape clears.');
    panel.addEventListener('keydown', event => {
      if (!owned() || event.target?.tagName === 'INPUT' || event.target?.tagName === 'SELECT') return;
      const key = calculatorKeyFromEvent(event);
      if (!key) return;
      event.preventDefault();
      press(key);
    });
    render();
  }
  function mountMath(panel, schema, output, owned, action) {
    const field = (label, value) => {
      const wrap = document.createElement('label'); wrap.textContent = label;
      const input = document.createElement('input'); input.value = String(value); input.maxLength = 160;
      input.setAttribute('aria-label', label); wrap.append(input); panel.append(wrap); return input;
    };
    const input = field('f(x)', schema.props.expression);
    const graph = schema.component === 'function_graph';
    const min = graph ? field('x minimum', schema.props.xMin) : null;
    const max = graph ? field('x maximum', schema.props.xMax) : null;
    const picture = document.createElement('div'); panel.append(picture);
    const controls = document.createElement('div'); controls.className = 'interactive-response-controls'; panel.append(controls);
    let worker = null, timeout = null, ticket = 0;
    const cancel = () => { ticket++; worker?.terminate(); worker = null; global.clearTimeout(timeout); };
    action(controls, graph ? 'Plot' : 'Calculate', () => {
      cancel(); picture.replaceChildren(); output.textContent = '';
      const props = {expression:input.value, ...(graph ? {xMin:Number(min.value),xMax:Number(max.value)} : {})};
      if (!normalize({...schema,props}) || (graph && (!min.value.trim() || !max.value.trim()))) throw Error('Check the expression and range.');
      if (!global.Worker) throw Error('Local calculation workers are unavailable in this browser.');
      const current = ticket;
      worker = new global.Worker(`math_worker.js?v=${encodeURIComponent(global.JUPITER_FRONTEND_VERSION || '1')}`);
      output.textContent = 'Calculating…';
      const fail = message => { if (current !== ticket) return; cancel(); if (owned()) output.textContent = message; };
      worker.onerror = () => fail('Local calculation could not start. Please reopen the tool.');
      timeout = global.setTimeout(() => fail('Calculation limit reached. Try a simpler expression.'), 3500);
      worker.onmessage = ({data}) => {
        if (current !== ticket) return;
        cancel(); if (!owned()) return;
        if (data.error || data.unavailable) { output.textContent = data.error || 'No closed-form antiderivative was found for this expression.'; return; }
        if (graph) {
          try { drawGraph(picture, data.points); output.textContent = 'Sampled graph; discontinuities may create gaps.'; }
          catch (_) { output.textContent = 'No real finite values in this range.'; }
        }
        else {
          // Text is safe even if math assets are unavailable. No model HTML.
          output.textContent = `${data.text}${data.constant ? ' + C' : ''}`;
        }
      };
      worker.postMessage({component:schema.component,props});
    });
    action(controls, 'Cancel', () => { cancel(); output.textContent = 'Cancelled.'; });
  }
  function drawGraph(host, points) {
    const values = points.map(p => p[1]).filter(y => y !== null && Number.isFinite(y)).sort((a,b)=>a-b);
    if (!values.length) throw Error('No real finite values in this range.');
    let low = values[Math.floor(values.length*.02)], high = values[Math.ceil(values.length*.98)-1];
    if (high === low) { low--; high++; }
    const pad = (high-low)*.1; low-=pad; high+=pad;
    const x0=points[0][0], x1=points[points.length-1][0];
    const X=x=>30+(x-x0)/(x1-x0)*300, Y=y=>180-(y-low)/(high-low)*160;
    const svg = document.createElementNS('http://www.w3.org/2000/svg','svg');
    svg.setAttribute('viewBox','0 0 360 210'); svg.setAttribute('role','img');
    svg.setAttribute('aria-label',`Function graph, x from ${x0} to ${x1}, y from ${low.toPrecision(3)} to ${high.toPrecision(3)}`);
    const path = (d,kind) => { const node=document.createElementNS(svg.namespaceURI,'path'); node.setAttribute('d',d); node.setAttribute('class',kind); svg.append(node); };
    path(`M30 20V180H330`, 'graph-axis');
    if (low<=0 && high>=0) path(`M30 ${Y(0)}H330`,'graph-axis');
    if (x0<=0 && x1>=0) path(`M${X(0)} 20V180`,'graph-axis');
    let d='', previous=null;
    for (const [x,y] of points) {
      if (y===null || y<low || y>high || !Number.isFinite(y)) { previous=null; continue; }
      const continuous=previous!==null && Math.abs(Y(y)-previous)<80;
      d+=`${continuous?'L':'M'}${X(x).toFixed(2)} ${Y(y).toFixed(2)} `; previous=Y(y);
    }
    path(d,'graph-curve');
    for (const [text,x,y] of [[String(x0),30,202],[String(x1),300,202],[high.toPrecision(3),30,14],[low.toPrecision(3),30,176]]) {
      const label=document.createElementNS(svg.namespaceURI,'text'); label.textContent=text;
      label.setAttribute('x',x); label.setAttribute('y',y); svg.append(label);
    }
    host.replaceChildren(svg);
  }
  // ── Bounded per-user UX preferences (same allowlist as services/ux_preferences.py; never code/CSS) ──
  const UX_ALLOWED = Object.freeze({
    calculator_density: ['standard', 'compact', 'expanded'], calculator_large_result: [false, true],
    disable_auto_components: [false, true], live_transcript_visible: [true, false],
    live_transcript_size: ['medium', 'small', 'large']
  });
  const uxDefaults = () => Object.fromEntries(Object.entries(UX_ALLOWED).map(([k, v]) => [k, v[0]]));
  let uxCurrent = uxDefaults();
  const uxSanitize = values => Object.fromEntries(Object.entries(values && typeof values === 'object' ? values : {})
    .filter(([k, v]) => Object.hasOwn(UX_ALLOWED, k) && UX_ALLOWED[k].includes(v)));
  global.JupiterUxPreferences = Object.freeze({
    current: () => ({ ...uxCurrent }),
    apply(values) {
      uxCurrent = { ...uxCurrent, ...uxSanitize(values) };
      try { global.dispatchEvent?.(new global.CustomEvent('jupiter-ux-preferences', { detail: { ...uxCurrent } })); } catch (_) {}
      return { ...uxCurrent };
    },
    reset() { uxCurrent = uxDefaults(); return { ...uxCurrent }; },
    sanitize: uxSanitize,
  });
  global.JupiterInteractive = Object.freeze({ normalize, mount, registry, calculate, determinant, validExpression, drawGraph,
    calculatorStep, calculatorInitial, calculatorDisplay, calculatorKeyFromEvent });
})(window);
