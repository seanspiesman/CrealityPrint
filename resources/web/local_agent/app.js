(() => {
  "use strict";

  const state = { jobs: [], printers: [], profiles: [], policy: {}, model: {}, alerts: [], conversations: [], questions: [] };
  const pending = new Map();
  let sequence = 0;
  let activeConversation = "";
  let printerFormOwner = "";
  let printerFormDirty = false;
  let cfsFormDirty = false;
  let toastTimer = 0;

  const byId = (id) => document.getElementById(id);
  const text = (node, value) => { node.textContent = value == null ? "" : String(value); };
  const element = (tag, className, value) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) text(node, value);
    return node;
  };
  const safeError = (value) => String(value || "The local service could not complete that request.").slice(0, 600);

  function request(action, payload = {}) {
    if (!window.wx || typeof window.wx.postMessage !== "function") {
      setConnection(false);
      showToast("The native local service bridge is unavailable.", true);
      return "";
    }
    const requestId = `${Date.now()}-${++sequence}`;
    const key = action === "state" ? "" : makeId();
    pending.set(requestId, { action, payload });
    window.wx.postMessage(JSON.stringify({
      command: "local_agent_request",
      data: { request_id: requestId, action, payload, idempotency_key: key },
    }));
    return requestId;
  }

  function makeId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
    return `local-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function showToast(message, isError = false) {
    const box = byId("toast");
    box.className = isError ? "toast error" : "toast";
    text(box, message);
    box.hidden = false;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => { box.hidden = true; }, 4200);
  }

  function setConnection(online) {
    byId("connection-dot").className = online ? "dot online" : "dot offline";
    text(byId("connection-label"), online ? "Local service connected" : "Local service unavailable");
  }

  function handleResult(message) {
    if (!message || typeof message !== "object" || message.command !== "local_agent_result") return;
    const result = message.data || {};
    const pendingRequest = pending.get(result.request_id) || {};
    const action = pendingRequest.action || "";
    pending.delete(result.request_id);
    if (!result.ok) {
      setConnection(action === "state" ? false : true);
      showToast(safeError(result.error), true);
      return;
    }
    setConnection(true);
    if (action === "state") {
      Object.assign(state, result.result || {});
      const conversations = Array.isArray(state.conversations) ? state.conversations : [];
      if (!activeConversation || !conversations.some((item) => item.id === activeConversation))
        activeConversation = conversations.length ? conversations[0].id : "";
      render();
      if (activeConversation) request("get_conversation", { conversation_id: activeConversation });
      return;
    }
    if (action === "get_conversation") {
      state.activeConversation = result.result || null;
      renderConversation();
      return;
    }
    if (action === "save_cfs_inventory") cfsFormDirty = false;
    if (action === "open_project") {
      showToast(result.result && result.result.inspection_started
        ? "Opening the saved project in an isolated inspection window."
        : "The saved project could not be opened for isolated inspection.",
      !(result.result && result.result.inspection_started));
    } else {
      showToast("Saved locally.");
    }
    if (action === "chat") {
      const output = result.result || {};
      if (output.conversation_id) activeConversation = String(output.conversation_id);
    }
    if (action === "new_conversation") {
      const output = result.result || {};
      if (output.id) activeConversation = String(output.id);
    }
    if (action === "approve_resume") {
      const jobId = pendingRequest.payload && pendingRequest.payload.job_id;
      if (jobId) request("resume_job", { job_id: jobId });
    }
    request("state");
  }

  window.handleSlicerEvent = handleResult;

  function go(view) {
    document.querySelectorAll(".view").forEach((section) => { section.hidden = section.id !== `view-${view}`; });
    document.querySelectorAll(".nav").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  }

  function addButton(parent, label, action, className = "secondary") {
    const button = element("button", className, label);
    button.type = "button";
    button.addEventListener("click", action);
    parent.appendChild(button);
    return button;
  }

  function makeBadge(value) {
    const status = String(value || "unknown").toLowerCase();
    const css = ["printing", "prepared", "completed", "connected", "clear"].includes(status) ? "good"
      : ["held", "paused", "starting", "queued", "unknown"].includes(status) ? "warn"
        : ["failed", "error", "offline", "canceled"].includes(status) ? "bad" : "";
    return element("span", `badge ${css}`, status.replaceAll("_", " "));
  }

  function makeListItem(title, detail, status) {
    const row = element("div", "list-item");
    const body = element("div");
    body.append(element("div", "item-title", title));
    if (detail) body.append(element("div", "item-sub", detail));
    row.append(body, makeBadge(status));
    return row;
  }

  function renderSummary() {
    const jobs = Array.isArray(state.jobs) ? state.jobs : [];
    const printers = Array.isArray(state.printers) ? state.printers : [];
    const active = jobs.filter((job) => ["printing", "starting", "paused", "queued"].includes(job.state)).length;
    const held = jobs.filter((job) => job.state === "held" || job.state === "paused").length;
    const activeAlerts = (Array.isArray(state.alerts) ? state.alerts : []).filter((alert) => !alert.acknowledged).length;
    const cards = [
      ["Registered printers", printers.length, `${printers.filter((p) => p.identity_confirmed).length} identities confirmed`],
      ["Active jobs", active, "Queued, starting or printing"],
      ["Need your attention", held + activeAlerts, "Held jobs and service alerts"],
      ["Print profiles", Array.isArray(state.profiles) ? state.profiles.length : 0, "Configured local presets"],
    ];
    const target = byId("summary-cards");
    target.replaceChildren();
    cards.forEach(([label, value, sub]) => {
      const card = element("article", "summary-card");
      card.append(element("div", "label", label), element("div", "value", value), element("div", "sub", sub));
      target.append(card);
    });
    const recent = byId("recent-jobs");
    recent.replaceChildren();
    if (!jobs.length) text(recent, "No jobs yet.");
    jobs.slice(0, 4).forEach((job) => recent.append(makeListItem(job.request && job.request.request || job.id,
      job.printer_id || "Printer not assigned", job.state)));
    const alerts = byId("alerts-list");
    alerts.replaceChildren();
    const entries = Array.isArray(state.alerts) ? state.alerts : [];
    const attentionJobs = jobs.filter((job) => job.state === "held" || job.state === "paused");
    if (!entries.length && !attentionJobs.length) text(alerts, "No current alerts.");
    entries.filter((alert) => !alert.acknowledged).forEach((alert) => {
      const row = makeListItem(alert.title || alert.message || "Service alert", alert.detail || "", alert.level || "warn");
      if (alert.id) addButton(row, "Acknowledge", () => request("acknowledge_alert", { alert_id: Number(alert.id) }), "text-button");
      alerts.append(row);
    });
    const history = entries.filter((alert) => alert.acknowledged);
    history.slice(0, 2).forEach((alert) => alerts.append(makeListItem(
      alert.title || alert.message || "Acknowledged alert", alert.detail || "", "acknowledged")));
    attentionJobs.forEach((job) => alerts.append(makeListItem(job.request && job.request.request || job.id,
      (job.holds || []).join(" · "), job.state)));
  }

  function renderJobs() {
    const jobs = Array.isArray(state.jobs) ? state.jobs : [];
    const target = byId("jobs-list");
    target.replaceChildren();
    if (!jobs.length) {
      target.append(element("div", "panel empty-state", "No jobs yet. Create a job above to begin."));
      return;
    }
    jobs.forEach((job) => {
      const card = element("article", "job-card");
      const top = element("div", "job-top");
      const summary = element("div");
      summary.append(element("div", "item-title", job.request && job.request.request || job.id));
      summary.append(element("div", "item-sub", `${job.printer_id || "No printer selected"} · ${job.profile_id || "No profile"}`));
      top.append(summary, makeBadge(job.state));
      card.append(top);
      if (job.observation && job.observation.progress !== undefined && job.observation.progress !== null) {
        const value = Number(job.observation.progress);
        const percentage = value <= 1 ? Math.round(value * 100) : Math.round(value);
        card.append(element("div", "item-sub", `Print progress · ${Math.max(0, Math.min(100, percentage))}%`));
      }
      if (job.estimates || job.gcode_sha256) {
        const estimate = job.estimates || {};
        const budget = `Estimate · ${estimate.hours == null ? "unknown time" : `${estimate.hours} h`} · ${estimate.grams == null ? "unknown filament" : `${estimate.grams} g`}`;
        card.append(element("div", "item-sub", `${budget} · Artifact ${String(job.gcode_sha256 || "unavailable").slice(0, 12)}`));
      }
      (job.holds || []).forEach((hold) => card.append(element("div", "item-sub", `Hold: ${hold}`)));
      if (Array.isArray(job.models) && job.models.length) {
        const candidates = element("div", "candidate-list");
        job.models.forEach((model) => {
          const row = element("div", "candidate");
          row.append(element("span", "", model.name || model.artifact_id));
          addButton(row, job.selected_artifact === model.artifact_id ? "Selected" : "Select model",
            () => request("select_model", { job_id: job.id, artifact_id: model.artifact_id }));
          candidates.append(row);
        });
        card.append(candidates);
      }
      const actions = element("div", "job-actions");
      if (["acquired", "held"].includes(job.state)) {
        const profile = element("select", "");
        profile.setAttribute("aria-label", "Preparation profile");
        profilesFor(job).forEach((candidate) => {
          const option = element("option", "", `${candidate.id} · ${candidate.material || "material unspecified"}`);
          option.value = candidate.id;
          profile.append(option);
        });
        if (!profile.options.length) {
          const option = element("option", "", "No profile available"); option.value = ""; profile.append(option);
        }
        actions.append(profile);
        addButton(actions, "Prepare", () => request("prepare_job", { job_id: job.id, profile_id: profile.value }));
      }
      if (["prepared", "held"].includes(job.state)) addButton(actions, "Queue", () => request("queue_job", { job_id: job.id }));
      if (["queued", "prepared"].includes(job.state)) addButton(actions, "Start", () => request("start_job", { job_id: job.id }), "primary");
      if (job.state === "printing") addButton(actions, "Pause", () => request("pause_job", { job_id: job.id }));
      if (job.state === "paused") addButton(actions, "Resume · owner decision", () => {
        if (!window.confirm("Resume this paused print? This records your owner decision.")) return;
        request("approve_resume", { job_id: job.id });
      }, "primary");
      if (["paused", "printing", "starting"].includes(job.state)) addButton(actions, "Cancel", () => {
        if (window.confirm("Cancel this print?")) request("cancel_job", { job_id: job.id });
      }, "secondary");
      if (["held", "prepared", "queued"].includes(job.state) && job.gcode_sha256 && job.estimates) addButton(actions, "Approve budget", () => {
        if (window.confirm("Approve the displayed resource limits for this exact prepared job?"))
          request("approve_budget", { job_id: job.id });
      });
      if (job.project_available || job.project_sha256) addButton(actions, "Inspect project", () => request("open_project", { job_id: job.id }));
      card.append(actions);
      target.append(card);
    });
  }

  function profilesFor(job) {
    return (Array.isArray(state.profiles) ? state.profiles : []).filter((profile) =>
      !job.printer_id || profile.printer_id === job.printer_id);
  }

  function renderPrinters() {
    const priorCfsPrinter = byId("cfs-printer").value;
    const printers = Array.isArray(state.printers) ? state.printers : [];
    const target = byId("printers-list"); target.replaceChildren();
    printers.forEach((printer) => {
      const card = element("article", "printer-card");
      const title = element("div", "job-top");
      title.append(element("div", "item-title", printer.name || printer.id), makeBadge(printer.status || (printer.identity_confirmed ? "configured" : "unknown")));
      card.append(title);
      card.append(element("div", "item-sub", `${printer.model || "Model unspecified"} · ${printer.material || "Material unknown"} · ${printer.color || "Color unknown"}`));
      card.append(element("div", "item-sub", `Identity ${printer.identity_confirmed ? "confirmed" : "unconfirmed"} · Control ${printer.control_qualified ? "qualified" : "not qualified"} · Camera ${printer.camera_association_confirmed ? "associated" : "unconfirmed"}`));
      target.append(card);
    });
    if (!printers.length) target.append(element("div", "panel empty-state", "No printer records returned by the service."));
    const ids = ["printer-id", "job-printer", ...Array.from(document.querySelectorAll(".printer-select")).map((node) => node.id)];
    ids.filter(Boolean).forEach((id) => fillPrinterSelect(byId(id), printers));
    document.querySelectorAll(".printer-select").forEach((select) => fillPrinterSelect(select, printers));
    if (cfsFormDirty && priorCfsPrinter && !printers.some((printer) => printer.id === priorCfsPrinter)) {
      const staleOption = element("option", "", `${priorCfsPrinter} (unavailable; unsaved edits)`);
      staleOption.value = priorCfsPrinter;
      byId("cfs-printer").append(staleOption);
      byId("cfs-printer").value = priorCfsPrinter;
    }
    const printerSelect = byId("printer-id");
    if (!printerFormDirty && printerSelect && printerSelect.value && printerFormOwner !== printerSelect.value) {
      const printer = printers.find((candidate) => candidate.id === printerSelect.value);
      if (printer) populatePrinterForm(printer);
      printerFormOwner = printerSelect.value;
    }
    renderCfsInventory();
  }

  function markCfsDirty() {
    cfsFormDirty = true;
    const selected = byId("cfs-printer").value;
    text(byId("cfs-dirty-label"), selected
      ? `Unsaved edits · will save to ${selected}` : "Unsaved edits · choose a printer before saving");
  }

  function makeCfsSlotRow(slot = {}) {
    const row = element("div", "cfs-slot-row");
    row.dataset.cfsSlotRow = "true";
    const field = (name, label, type = "text", value = "") => {
      const wrapper = element("label");
      wrapper.append(element("span", "", label));
      const input = element("input");
      input.name = name;
      input.type = type;
      input.value = value == null ? "" : String(value);
      if (type === "number") { input.min = "0"; input.step = "1"; }
      input.addEventListener("input", markCfsDirty);
      input.addEventListener("change", markCfsDirty);
      wrapper.append(input);
      row.append(wrapper);
      return input;
    };
    row.slotFields = {
      slot_id: field("slot_id", "Slot ID", "text", slot.slot_id),
      material: field("material", "Material", "text", slot.material),
      color: field("color", "Color", "text", slot.color),
      remaining_grams: field("remaining_grams", "Remaining grams", "number", slot.remaining_grams),
    };
    const verifiedLabel = element("label", "verified-field");
    row.slotFields.verified = element("input");
    row.slotFields.verified.type = "checkbox";
    row.slotFields.verified.checked = Boolean(slot.verified);
    row.slotFields.verified.addEventListener("change", markCfsDirty);
    verifiedLabel.append(row.slotFields.verified, element("span", "", "Verified"));
    row.append(verifiedLabel);
    const remove = element("button", "remove-slot", "Remove");
    remove.type = "button";
    remove.addEventListener("click", () => { row.remove(); markCfsDirty(); });
    row.append(remove);
    return row;
  }

  function renderCfsInventory() {
    const select = byId("cfs-printer");
    const selected = (state.printers || []).find((printer) => printer.id === select.value);
    if (cfsFormDirty) {
      const label = byId("cfs-dirty-label");
      if (!label.textContent) markCfsDirty();
      return;
    }
    const target = byId("cfs-slots");
    target.replaceChildren();
    if (selected) {
      (Array.isArray(selected.cfs_slots) ? selected.cfs_slots : []).forEach((slot) => target.append(makeCfsSlotRow(slot)));
    }
    text(byId("cfs-dirty-label"), "");
    if (!target.childElementCount) target.append(element("div", "empty-state", "No slots saved for this printer. Add a slot to record its inventory."));
  }

  function populatePrinterForm(printer) {
    const form = byId("printer-form");
    ["name", "model", "endpoint", "nozzle_mm", "material", "color", "remaining_grams", "camera_url",
      "frame_sequence_header", "frame_time_header", "bed_detector_url", "failure_detector_url"].forEach((key) => {
      if (form.elements[key]) form.elements[key].value = printer[key] == null ? "" : printer[key];
    });
    if (form.elements.cfs) form.elements.cfs.value = printer.cfs == null ? "" : String(printer.cfs);
    ["camera_association_confirmed", "identity_confirmed", "filament_verified"].forEach((key) => {
      if (form.elements[key]) form.elements[key].checked = Boolean(printer[key]);
    });
  }

  function fillPrinterSelect(select, printers) {
    if (!select) return;
    const current = select.value;
    select.replaceChildren();
    printers.forEach((printer) => {
      const option = element("option", "", `${printer.name || printer.id} (${printer.id})`);
      option.value = printer.id;
      select.append(option);
    });
    if (!printers.length) {
      const option = element("option", "", "Select a printer"); option.value = ""; select.append(option);
    } else if (printers.some((printer) => printer.id === current)) select.value = current;
  }

  function renderProfiles() {
    const target = byId("profiles-list"); target.replaceChildren();
    (Array.isArray(state.profiles) ? state.profiles : []).forEach((profile) => {
      const card = element("article", "profile-card");
      card.append(element("div", "item-title", profile.id), makeBadge(profile.verified ? "verified" : "unverified"));
      card.append(element("div", "item-sub", `${profile.printer_id} · ${profile.material} · ${profile.color || "Color unspecified"} · ${profile.nozzle_mm || "?"} mm`));
      target.append(card);
    });
    if (!target.childElementCount) target.append(element("div", "panel empty-state", "No profiles configured."));
  }

  function renderSettings() {
    const model = state.model || {};
    const modelForm = byId("model-form");
    if (!modelForm.dataset.dirty) {
      modelForm.elements.base_url.value = model.base_url || "";
      modelForm.elements.model.value = model.model || "";
      text(byId("model-key-state"), model.api_key_set ? "API key is saved in the local service." : "No API key is saved.");
    }
    const policy = state.policy || {};
    const policyForm = byId("policy-form");
    if (!policyForm.dataset.dirty) {
      ["max_hours", "max_grams", "monitoring_loss_seconds"].forEach((field) => {
        if (policy[field] !== undefined) policyForm.elements[field].value = policy[field];
      });
      if (policy.pause_on_monitoring_loss !== undefined)
        policyForm.elements.pause_on_monitoring_loss.value = String(policy.pause_on_monitoring_loss);
    }
  }

  function renderConversation() {
    const target = byId("conversation-list"); target.replaceChildren();
    const conversations = Array.isArray(state.conversations) ? state.conversations : [];
    const picker = byId("conversation-history");
    picker.replaceChildren();
    conversations.forEach((conversation) => {
      const option = element("option", "", new Date(Number(conversation.created || 0) * 1000).toLocaleString());
      option.value = conversation.id;
      picker.append(option);
    });
    if (activeConversation) picker.value = activeConversation;
    const selected = state.activeConversation && state.activeConversation.id === activeConversation
      ? state.activeConversation : null;
    if (!selected) { text(target, "No conversations yet. Start a new conversation."); return; }
    const messages = Array.isArray(selected.messages) ? selected.messages : [];
    if (!messages.length) target.append(element("div", "empty-state", "Waiting for the local assistant response…"));
    messages.forEach((message) => {
      const role = String(message.role || message.type || "assistant");
      const body = element("div", `message ${role === "user" ? "user" : ""}`);
      body.append(element("span", "role", role));
      const content = typeof message.content === "string" ? message.content
        : typeof message.text === "string" ? message.text
          : message.tool_name ? `${message.tool_name}: ${JSON.stringify(message.result || message.arguments || {})}`
            : "Message record";
      body.append(element("span", "", content));
      target.append(body);
    });
    target.scrollTop = target.scrollHeight;
  }

  function renderQuestions() {
    const questions = Array.isArray(state.questions) ? state.questions : [];
    const open = questions.filter((question) => String(question.status || "open").toLowerCase() === "open");
    const resolved = questions.filter((question) => !open.includes(question));
    const target = byId("questions-list");
    target.replaceChildren();
    if (!open.length) target.append(element("div", "empty-state", "No open questions."));
    open.forEach((question) => {
      const card = element("article", "question-card");
      const job = question.job_id ? `Related job · ${question.job_id}` : "Asked before a job exists";
      card.append(element("div", "item-sub", job));
      card.append(element("div", "question-text", question.question || "Question text unavailable."));
      const form = element("form", "question-answer");
      const answer = element("textarea");
      answer.name = "answer";
      answer.rows = 2;
      answer.maxLength = 4000;
      answer.required = true;
      answer.setAttribute("aria-label", "Your answer");
      answer.placeholder = "Your answer";
      form.append(answer);
      const submit = element("button", "primary", "Send answer");
      submit.type = "submit";
      form.append(submit);
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        const value = String(answer.value || "").trim();
        if (!value) return;
        request("answer_question", { question_id: question.id, answer: value });
      });
      card.append(form);
      target.append(card);
    });
    const history = byId("question-history");
    history.replaceChildren();
    resolved.forEach((question) => {
      const item = element("article", "question-history-item");
      item.append(element("div", "item-sub", "Answered"));
      item.append(element("div", "question-text", question.question || "Question text unavailable."));
      item.append(element("div", "item-sub", `Your answer · ${question.answer || "No answer recorded"}`));
      history.append(item);
    });
  }

  function render() {
    renderSummary(); renderJobs(); renderPrinters(); renderProfiles(); renderSettings(); renderConversation(); renderQuestions();
  }

  function formValues(form) { return Object.fromEntries(new FormData(form).entries()); }
  function optionalNumber(value) { return value === "" ? null : Number(value); }
  function listFromTextarea(value) { return value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean); }

  document.querySelectorAll(".nav").forEach((button) => button.addEventListener("click", () => go(button.dataset.view)));
  document.querySelectorAll("[data-go]").forEach((button) => button.addEventListener("click", () => go(button.dataset.go)));
  byId("refresh").addEventListener("click", () => request("state"));
  byId("job-form").addEventListener("submit", (event) => {
    event.preventDefault(); const values = formValues(event.currentTarget);
    if (Boolean(values.source_url) === Boolean(values.local_path)) { showToast("Enter exactly one model URL or local path.", true); return; }
    request("create_job", { request: values.request, source_url: values.source_url || null,
      local_path: values.local_path || null, printer_id: values.printer_id || null,
      material: values.material || null, color: values.color || null, settings: {}, copies: Number(values.copies || 1) });
    event.currentTarget.reset();
  });
  byId("printer-form").addEventListener("submit", (event) => {
    event.preventDefault(); const v = formValues(event.currentTarget);
    request("enroll_printer", { printer_id: v.printer_id, name: v.name || null, model: v.model,
      endpoint: v.endpoint || null, api_key_env: v.api_key_env || null, nozzle_mm: optionalNumber(v.nozzle_mm),
      cfs: v.cfs === "" ? null : v.cfs === "true", material: v.material || null, color: v.color || null,
      remaining_grams: optionalNumber(v.remaining_grams), camera_url: v.camera_url || null,
      camera_association_confirmed: event.currentTarget.elements.camera_association_confirmed.checked,
      frame_sequence_header: v.frame_sequence_header || null, frame_time_header: v.frame_time_header || null,
      bed_detector_url: v.bed_detector_url || null, failure_detector_url: v.failure_detector_url || null,
      identity_confirmed: event.currentTarget.elements.identity_confirmed.checked,
      filament_verified: event.currentTarget.elements.filament_verified.checked });
    printerFormDirty = false;
  });
  byId("printer-form").addEventListener("input", () => { printerFormDirty = true; });
  byId("printer-form").addEventListener("change", (event) => {
    if (event.target === byId("printer-id")) {
      printerFormDirty = false;
      printerFormOwner = event.target.value;
      const printer = (state.printers || []).find((candidate) => candidate.id === event.target.value);
      if (printer) populatePrinterForm(printer);
    } else printerFormDirty = true;
  });
  byId("reference-form").addEventListener("submit", (event) => {
    event.preventDefault(); const v = formValues(event.currentTarget);
    request("enroll_reference", { printer_id: v.printer_id, bed_clear_confirmed: event.currentTarget.elements.bed_clear_confirmed.checked,
      roi: [Number(v.left), Number(v.top), Number(v.right), Number(v.bottom)] });
  });
  byId("cfs-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const printerId = byId("cfs-printer").value;
    const slots = Array.from(byId("cfs-slots").children)
      .filter((row) => row.dataset.cfsSlotRow === "true")
      .map((row) => {
        const field = row.slotFields;
        return { slot_id: field.slot_id.value.trim(), material: field.material.value.trim() || null,
          color: field.color.value.trim() || null,
          remaining_grams: field.remaining_grams.value === "" ? null : Number(field.remaining_grams.value),
          verified: Boolean(field.verified.checked) };
      });
    if (!printerId) { showToast("Choose a printer before saving CFS slots.", true); return; }
    if (slots.length > 16 || slots.some((slot) => !/^[A-Za-z0-9_-]{1,40}$/.test(slot.slot_id)) ||
        new Set(slots.map((slot) => slot.slot_id)).size !== slots.length ||
        slots.some((slot) => slot.remaining_grams !== null && (!Number.isFinite(slot.remaining_grams) || slot.remaining_grams < 0))) {
      showToast("Use unique slot IDs and non-negative remaining grams; up to 16 slots are allowed.", true);
      return;
    }
    request("save_cfs_inventory", { printer_id: printerId, slots });
  });
  byId("cfs-form").addEventListener("input", (event) => {
    if (event.target !== byId("cfs-printer")) markCfsDirty();
  });
  byId("cfs-form").addEventListener("change", (event) => {
    if (event.target === byId("cfs-printer")) {
      if (cfsFormDirty) markCfsDirty();
      else renderCfsInventory();
    } else markCfsDirty();
  });
  byId("cfs-add-slot").addEventListener("click", () => {
    const target = byId("cfs-slots");
    if (!byId("cfs-printer").value) { showToast("Choose a printer before adding a slot.", true); return; }
    if (target.children.length === 1 && !target.children[0].dataset.cfsSlotRow) target.replaceChildren();
    target.append(makeCfsSlotRow());
    markCfsDirty();
  });
  byId("cfs-discard").addEventListener("click", () => {
    cfsFormDirty = false;
    renderCfsInventory();
  });
  byId("probe-printer").addEventListener("click", () => request("probe_printer", { printer_id: byId("cfs-form").elements.printer_id.value }));
  byId("profile-form").addEventListener("submit", (event) => {
    event.preventDefault(); const v = formValues(event.currentTarget);
    request("save_profile", { id: v.id, printer_id: v.printer_id, material: v.material, color: v.color || null,
      nozzle_mm: Number(v.nozzle_mm), verified: v.verified === "true",
      settings: listFromTextarea(v.settings), filaments: listFromTextarea(v.filaments) });
  });
  byId("model-form").addEventListener("submit", (event) => {
    event.preventDefault(); const v = formValues(event.currentTarget);
    const payload = { base_url: v.base_url, model: v.model };
    if (v.api_key) payload.api_key = v.api_key;
    request("save_model", payload);
    event.currentTarget.elements.api_key.value = "";
    event.currentTarget.dataset.dirty = "";
  });
  byId("policy-form").addEventListener("submit", (event) => {
    event.preventDefault(); const v = formValues(event.currentTarget);
    if (!window.confirm("Confirm these resource limits and monitoring-loss behavior for automatic starts?")) return;
    request("save_policy", { max_hours: Number(v.max_hours), max_grams: Number(v.max_grams),
      monitoring_loss_seconds: Number(v.monitoring_loss_seconds), pause_on_monitoring_loss: v.pause_on_monitoring_loss === "true" });
    event.currentTarget.dataset.dirty = "";
  });
  ["model-form", "policy-form"].forEach((id) => {
    byId(id).addEventListener("input", (event) => { event.currentTarget.dataset.dirty = "true"; });
    byId(id).addEventListener("change", (event) => { event.currentTarget.dataset.dirty = "true"; });
  });
  byId("new-conversation").addEventListener("click", () => request("new_conversation", {}));
  byId("conversation-history").addEventListener("change", (event) => {
    activeConversation = event.currentTarget.value;
    state.activeConversation = null;
    request("get_conversation", { conversation_id: activeConversation });
  });
  byId("chat-form").addEventListener("submit", (event) => {
    event.preventDefault(); const message = event.currentTarget.elements.message.value.trim();
    if (!message) return;
    const payload = { message };
    if (activeConversation) payload.conversation_id = activeConversation;
    request("chat", payload); event.currentTarget.reset();
  });

  document.addEventListener("click", (event) => {
    const item = event.target.closest("[data-view]");
    if (item && item.classList.contains("nav")) go(item.dataset.view);
  });
  document.addEventListener("click", (event) => {
    const action = event.target.closest("[data-action]");
    if (action) request(action.dataset.action, JSON.parse(action.dataset.payload || "{}"));
  });
  window.setInterval(() => request("state"), 2500);
  request("state");
})();
