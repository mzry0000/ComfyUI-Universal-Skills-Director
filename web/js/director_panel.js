import { app } from "../../scripts/app.js";

const EXTENSION_NAME = "UniversalSkills.DirectorQueuePreview";
const PANEL_ID = "ush-director-queue-preview";

function element(tag, options = {}) {
  const node = document.createElement(tag);
  if (options.text !== undefined) node.textContent = options.text;
  if (options.className) node.className = options.className;
  for (const [name, value] of Object.entries(options.attributes || {})) {
    node.setAttribute(name, value);
  }
  return node;
}

function safeSelector(value) {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= 240 &&
    /^[A-Za-z0-9][A-Za-z0-9_.:/-]*$/.test(value)
  );
}

function parsePreview(text) {
  let value;
  try {
    value = JSON.parse(text);
  } catch (_error) {
    throw new Error("Manifest must be valid JSON.");
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Manifest root must be an object.");
  }
  if (value.schema_version !== "1.0") {
    throw new Error("Unsupported schema_version; expected 1.0.");
  }
  if (!Array.isArray(value.allowed_selectors) || value.allowed_selectors.length === 0) {
    throw new Error("allowed_selectors must be a non-empty array.");
  }
  if (!Array.isArray(value.items) || value.items.length === 0) {
    throw new Error("items must be a non-empty array.");
  }

  const items = new Map();
  for (const item of value.items) {
    if (!item || typeof item !== "object" || !safeSelector(item.selector)) {
      throw new Error("Every item needs a safe exact selector.");
    }
    if (items.has(item.selector)) throw new Error("Item selectors must be unique.");
    if (!item.prompt || typeof item.prompt !== "object" || Array.isArray(item.prompt)) {
      throw new Error("Every item needs a ComfyUI API prompt object.");
    }
    items.set(item.selector, item);
  }

  const selectors = [];
  const seen = new Set();
  for (const selector of value.allowed_selectors) {
    if (!safeSelector(selector) || seen.has(selector) || !items.has(selector)) {
      throw new Error("allowed_selectors must contain unique item selectors.");
    }
    seen.add(selector);
    selectors.push(selector);
  }
  return {
    queueId: String(value.queue_id || ""),
    planId: String(value.plan_id || ""),
    planRevision: value.plan_revision,
    selectors,
    items,
  };
}

function shellQuote(value) {
  // The packaged CLI documentation targets PowerShell. Doubling an apostrophe
  // inside a single-quoted token prevents a pasted file name from becoming a
  // second command.
  return `'${String(value).replaceAll("'", "''")}'`;
}

async function copyText(value, status) {
  try {
    await navigator.clipboard.writeText(value);
    status.textContent = "Command copied.";
  } catch (_error) {
    status.textContent = "Clipboard access was unavailable; select the command manually.";
  }
}

function renderDirectorPanel(root) {
  root.replaceChildren();
  const container = element("div");
  Object.assign(container.style, {
    display: "flex",
    flexDirection: "column",
    gap: "10px",
    padding: "12px",
    height: "100%",
    overflow: "auto",
  });

  container.appendChild(element("h3", { text: "Director Queue Preview" }));
  container.appendChild(
    element("p", {
      text: "Paste or open a queue manifest to inspect its allowlisted selectors. This panel never sends /prompt requests; the CLI performs the authoritative strict validation.",
    }),
  );

  const fileInput = element("input", {
    attributes: { type: "file", accept: ".json,application/json" },
  });
  const manifestText = element("textarea", {
    attributes: {
      rows: "12",
      spellcheck: "false",
      placeholder: "Paste Director queue manifest JSON here",
    },
  });
  Object.assign(manifestText.style, { width: "100%", fontFamily: "monospace" });

  const manifestPath = element("input", {
    attributes: {
      type: "text",
      value: "director-queue.json",
      "aria-label": "Manifest path used in copied CLI commands",
    },
  });
  manifestPath.value = "director-queue.json";

  const selectorSelect = element("select", {
    attributes: { "aria-label": "Allowed queue selector" },
  });
  selectorSelect.disabled = true;

  const previewButton = element("button", { text: "Preview manifest" });
  const copyPreviewButton = element("button", { text: "Copy preview CLI" });
  const copyRunButton = element("button", { text: "Copy confirmed run CLI" });
  const copyWaitButton = element("button", { text: "Copy confirmed run + wait CLI" });
  copyPreviewButton.disabled = true;
  copyRunButton.disabled = true;
  copyWaitButton.disabled = true;

  const status = element("p", { text: "No manifest loaded." });
  const details = element("pre");
  Object.assign(details.style, {
    whiteSpace: "pre-wrap",
    overflowWrap: "anywhere",
    padding: "8px",
    border: "1px solid var(--border-color, #555)",
  });

  let preview = null;
  const renderSelectedDetails = () => {
    if (!preview) return;
    const selected = preview.items.get(selectorSelect.value);
    if (!selected) return;
    const classTypes = new Set();
    for (const node of Object.values(selected.prompt)) {
      if (node && typeof node.class_type === "string") classTypes.add(node.class_type);
    }
    details.textContent = JSON.stringify(
      {
        queue_id: preview.queueId,
        plan_id: preview.planId,
        plan_revision: preview.planRevision,
        allowed_selector_count: preview.selectors.length,
        selected: selectorSelect.value,
        node_count: Object.keys(selected.prompt).length,
        class_types: Array.from(classTypes).sort(),
      },
      null,
      2,
    );
  };
  const updatePreview = () => {
    try {
      const priorSelection = selectorSelect.value;
      preview = parsePreview(manifestText.value);
      selectorSelect.replaceChildren();
      for (const selector of preview.selectors) {
        selectorSelect.appendChild(element("option", { text: selector, attributes: { value: selector } }));
      }
      if (preview.selectors.includes(priorSelection)) selectorSelect.value = priorSelection;
      selectorSelect.disabled = false;
      copyPreviewButton.disabled = false;
      copyRunButton.disabled = false;
      copyWaitButton.disabled = false;
      status.textContent = "Preview parsed locally. No HTTP request was sent.";
      renderSelectedDetails();
    } catch (error) {
      preview = null;
      selectorSelect.replaceChildren();
      selectorSelect.disabled = true;
      copyPreviewButton.disabled = true;
      copyRunButton.disabled = true;
      copyWaitButton.disabled = true;
      status.textContent = error instanceof Error ? error.message : "Manifest could not be previewed.";
      details.textContent = "";
    }
  };

  selectorSelect.addEventListener("change", renderSelectedDetails);
  previewButton.addEventListener("click", updatePreview);
  fileInput.addEventListener("change", async () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    try {
      manifestText.value = await file.text();
      manifestPath.value = file.name;
      updatePreview();
    } catch (_error) {
      status.textContent = "The selected file could not be read.";
    }
  });
  copyPreviewButton.addEventListener("click", () => {
    if (!preview) return;
    const command = [
      "python tools/director_queue.py",
      shellQuote(manifestPath.value || "director-queue.json"),
      "--selector",
      shellQuote(selectorSelect.value),
    ].join(" ");
    copyText(command, status);
  });
  copyRunButton.addEventListener("click", () => {
    if (!preview) return;
    const command = [
      "python tools/director_queue.py",
      shellQuote(manifestPath.value || "director-queue.json"),
      "--selector",
      shellQuote(selectorSelect.value),
      "--run --confirm",
    ].join(" ");
    copyText(command, status);
  });
  copyWaitButton.addEventListener("click", () => {
    if (!preview) return;
    const command = [
      "python tools/director_queue.py",
      shellQuote(manifestPath.value || "director-queue.json"),
      "--selector",
      shellQuote(selectorSelect.value),
      "--run --confirm --wait",
    ].join(" ");
    copyText(command, status);
  });

  container.append(
    fileInput,
    manifestText,
    element("label", { text: "CLI manifest path" }),
    manifestPath,
    element("label", { text: "Allowed selector" }),
    selectorSelect,
    previewButton,
    copyPreviewButton,
    copyRunButton,
    copyWaitButton,
    status,
    details,
  );
  root.appendChild(container);
}

app.registerExtension({
  name: EXTENSION_NAME,
  async setup() {
    if (!app.extensionManager?.registerSidebarTab) return;
    app.extensionManager.registerSidebarTab({
      id: PANEL_ID,
      icon: "pi pi-list-check",
      title: "Director",
      tooltip: "Preview Director queue manifests and copy guarded CLI commands",
      type: "custom",
      render: renderDirectorPanel,
    });
  },
});
