(function (global) {
  'use strict';
  // Lifecycle only: voice identity is resolved by the server, not this controller.
  const create = ({ identity, onCancel }) => {
    let active = null;
    return {
      begin(id, owner = identity()) { active = { id, owner }; },
      owns(id) { return Boolean(active && active.id === id && active.owner === identity()); },
      take(id) {
        if (!this.owns(id)) return false;
        active = null; // consume BEFORE triggering any synchronous click callback
        return true;
      },
      cancel() {
        const previous = active;
        active = null;
        if (previous) onCancel(previous.id);
      }
    };
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = { create };
  else global.JupiterTtsSelection = Object.freeze({ create });
})(typeof window !== 'undefined' ? window : globalThis);
