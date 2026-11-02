(function (root) {
  'use strict';
  // Offline presentation only; authenticated backend definitions control saves.
  const pendingDefinitions = [
    ['groq', 'Groq', ['api_key']], ['openrouter', 'OpenRouter', ['api_key']],
    ['cloudflare', 'Cloudflare Workers AI', ['api_key', 'account_id']],
    ['nvidia', 'NVIDIA NIM', ['api_key']], ['opencode', 'OpenCode Zen', ['api_key']],
    ['bytez', 'Bytez', ['api_key']], ['openweather', 'OpenWeather', ['api_key']]
  ].map(([id, name, fields]) => ({id, name, fields, status: 'unavailable', required: false}));
  // Official key-page destinations only; never accept URLs from provider metadata.
  const providerKeyPages = Object.freeze({
    groq: 'https://console.groq.com/keys',
    openrouter: 'https://openrouter.ai/settings/keys',
    cloudflare: 'https://dash.cloudflare.com/?to=/:account/ai/workers-ai',
    nvidia: 'https://build.nvidia.com/settings/api-keys',
    opencode: 'https://opencode.ai/auth',
    bytez: 'https://bytez.com/api',
    openweather: 'https://home.openweathermap.org/api_keys'
  });
  const statusLabel = status => ({missing: 'Not provided', saved: 'Saved · not yet verified', connected: 'Used successfully', needs_reverify: 'Needs replacement', unavailable: 'Waiting for backend update'})[status] || 'Status unavailable';
  if (typeof module !== 'undefined' && module.exports) module.exports = {pendingDefinitions, statusLabel, providerKeyPages};
  if (!root || !root.document) return;
  const doc = root.document;
  let generation = 0;
  function clearInputs() {doc.querySelectorAll('#profile-optional-providers input').forEach(input => {input.value = '';});}
  function reset() {
    generation++;
    clearInputs();
    doc.getElementById('profile-optional-providers')?.replaceChildren();
  }
  function element(tag, className, text) {
    const el = doc.createElement(tag);
    if (className) el.className = className;
    if (text) el.textContent = text;
    return el;
  }
  function render(providers, transport) {
    reset();
    const supported = Array.isArray(providers);
    const host = doc.getElementById('profile-optional-providers');
    if (!host) return;
    if (!supported) host.appendChild(element('p', 'optional-provider-note', 'Provider saving requires the updated backend. Entries are disabled until it is available.'));
    const currentGeneration = generation;
    for (const provider of supported ? providers : pendingDefinitions) {
      const row = element('details', 'optional-provider-row');
      const summary = element('summary');
      const heading = element('span', 'optional-provider-heading');
      heading.appendChild(element('strong', '', provider.name));
      heading.appendChild(element('small', '', statusLabel(provider.status)));
      summary.appendChild(heading);
      row.appendChild(summary);
      const form = element('form', 'optional-provider-form');
      form.appendChild(element('p', 'optional-provider-note', provider.usage_note || 'Existing eligibility and free-only rules still apply.'));
      if (providerKeyPages[provider.id]) {
        const link = element('a', 'provider-api-link', provider.id === 'cloudflare' ? 'Get API token' : 'Get API key');
        link.href = providerKeyPages[provider.id];
        link.target = '_blank'; link.rel = 'noopener noreferrer';
        link.setAttribute('aria-label', 'Open ' + provider.name + ' API credentials page in your browser');
        const icon = element('i', 'ph ph-arrow-square-out'); icon.setAttribute('aria-hidden', 'true');
        link.prepend(icon); form.appendChild(link);
        form.appendChild(element('small', 'optional-provider-note', 'Sign in on the provider’s site to create or copy your key.'));
      }
      const inputs = {};
      for (const field of provider.fields || []) {
        if (!['api_key', 'account_id'].includes(field)) continue;
        const label = element('label', '', field === 'account_id' ? 'Cloudflare Account ID' : provider.name + ' API key');
        const input = element('input');
        input.type = 'password'; input.autocomplete = 'new-password'; input.spellcheck = false;
        input.maxLength = field === 'account_id' ? 32 : 512;
        input.required = true; // Only this optional save form, never registration.
        input.disabled = !supported;
        input.placeholder = field === 'account_id' ? 'Enter your Cloudflare account ID' : 'Paste a new key to add or replace';
        label.appendChild(input); form.appendChild(label); inputs[field] = input;
      }
      const status = element('p', 'optional-provider-status');
      status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
      const actions = element('div', 'optional-provider-actions');
      const save = element('button', 'secondary-btn', 'Save key');
      save.type = 'submit'; save.disabled = !supported;
      const remove = element('button', 'optional-provider-remove', 'Remove saved key');
      remove.type = 'button'; remove.disabled = !supported || provider.status === 'missing';
      actions.append(save, remove); form.append(status, actions); row.appendChild(form); host.appendChild(row);
      async function submit(method, fields) {
        if (!supported || !transport || currentGeneration !== generation) return;
        save.disabled = true; remove.disabled = true;
        status.textContent = method === 'PUT' ? 'Saving securely…' : 'Removing this key…';
        Object.values(inputs).forEach(input => {input.value = '';});
        try {
          const result = await transport('/api/profile/provider-credentials/' + encodeURIComponent(provider.id), {
            method, ...(method === 'PUT' ? {body: JSON.stringify({credentials: fields})} : {})
          });
          if (currentGeneration !== generation) return;
          if (!result.ok) {status.textContent = result.status === 401 ? 'Please sign in again.' : 'Could not save this change. Check the fields and try again.'; return;}
          const updated = (result.data.optional_providers || []).find(item => item.id === provider.id);
          provider.status = updated?.status || (method === 'DELETE' ? 'missing' : 'saved');
          heading.querySelector('small').textContent = statusLabel(provider.status);
          status.textContent = method === 'DELETE' ? 'Key removed. Other providers are unchanged.' : 'Saved encrypted for your account. No provider call was made; eligible routes can use it.';
        } catch (_) {
          if (currentGeneration === generation) status.textContent = 'Connection unavailable. Please try again later.';
        } finally {
          if (fields) Object.keys(fields).forEach(key => {fields[key] = '';});
          if (currentGeneration === generation) {save.disabled = false; remove.disabled = provider.status === 'missing';}
        }
      }
      form.addEventListener('submit', event => {
        event.preventDefault();
        const fields = Object.fromEntries(Object.entries(inputs).map(([key, input]) => [key, input.value.trim()]));
        if (Object.values(fields).some(value => !value)) {status.textContent = 'Enter this provider’s fields before saving.'; return;}
        void submit('PUT', fields);
      });
      remove.addEventListener('click', () => {void submit('DELETE');});
    }
  }
  root.JupiterOptionalProviders = {render, reset};
  doc.addEventListener('DOMContentLoaded', () => {
    const modal = doc.getElementById('profile-modal');
    if (modal) new root.MutationObserver(() => {if (modal.classList.contains('hidden')) clearInputs();}).observe(modal, {attributes: true, attributeFilter: ['class']});
  }, {once: true});
})(typeof window !== 'undefined' ? window : null);
