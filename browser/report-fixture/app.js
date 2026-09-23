(() => {
  const button = document.querySelector("[data-action=report]");
  const confirmation = document.querySelector("#confirmation");
  const instance = document.querySelector("[data-instance]");
  const fields = {
    instanceId: document.querySelector("[data-field=instance-id]"),
    label: document.querySelector("[data-field=label]"),
    nonce: document.querySelector("[data-field=nonce]"),
  };
  const state = {
    instanceId: instance.dataset.instanceId,
    label: instance.dataset.label,
    nonce: instance.dataset.nonce,
    providerRequestCount: 0,
    submitted: false,
  };

  const render = () => {
    instance.dataset.instanceId = state.instanceId;
    instance.dataset.label = state.label;
    instance.dataset.nonce = state.nonce;
    fields.instanceId.textContent = state.instanceId;
    fields.label.textContent = state.label;
    fields.nonce.textContent = state.nonce;
  };
  const fixture = {
    get instanceId() { return state.instanceId; },
    set instanceId(value) { state.instanceId = String(value); render(); },
    get label() { return state.label; },
    set label(value) { state.label = String(value); render(); },
    get nonce() { return state.nonce; },
    set nonce(value) { state.nonce = String(value); render(); },
    get providerRequestCount() { return state.providerRequestCount; },
    get submitted() { return state.submitted; },
  };

  button.addEventListener("click", () => {
    state.submitted = true;
    confirmation.hidden = false;
    confirmation.dataset.reportSubmitted = "true";
  });

  window.fixture = fixture;
})();
