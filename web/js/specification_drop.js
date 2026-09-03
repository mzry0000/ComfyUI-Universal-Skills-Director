import { app } from "../../scripts/app.js";

const EXTENSION_NAME = "UniversalSkills.SpecificationDrop";
const TARGET_NODE = "USH_LoadSpecification";
const MAX_SPECIFICATION_BYTES = 256 * 1024;
const UI_BY_NODE = new WeakMap();
const SOURCE_WIDGET_NAMES = [
  "spec_file",
  "uploaded_filename",
  "uploaded_content",
];

function element(tag, options = {}) {
  const value = document.createElement(tag);
  if (options.text !== undefined) value.textContent = options.text;
  if (options.className) value.className = options.className;
  for (const [name, attribute] of Object.entries(options.attributes || {})) {
    value.setAttribute(name, attribute);
  }
  return value;
}

function findWidget(node, name) {
  return node.widgets?.find((widget) => widget.name === name) || null;
}

function hideBackingWidget(widget) {
  // Keep the widget in node.widgets so ComfyUI continues to serialize its
  // value, but remove its duplicate editor from both canvas and DOM renderers.
  widget.hidden = true;
  widget.options = widget.options || {};
  widget.options.hidden = true;
  widget.computeSize = () => [0, -4];
  for (const key of ["element", "inputEl"]) {
    const widgetElement = widget[key];
    if (widgetElement?.style) widgetElement.style.display = "none";
  }
}

function hasLinkedSourceInput(node) {
  return Boolean(
    node.inputs?.some(
      (input) =>
        (SOURCE_WIDGET_NAMES.includes(input.name) ||
          SOURCE_WIDGET_NAMES.includes(input.widget?.name)) &&
        input.link !== undefined &&
        input.link !== null,
    ),
  );
}

function setWidgetValue(node, widget, value) {
  if (widget.element || widget.inputEl) {
    const previous = widget.value;
    // Current DOM widgets invoke their callback from the value setter. Calling
    // inherited setValue() would invoke that callback a second time.
    widget.value = value;
    node.onWidgetChanged?.(widget.name, value, previous, widget);
    node.graph?.incrementVersion?.();
  } else if (typeof widget.setValue === "function") {
    widget.setValue(value, { node, canvas: app.canvas });
  } else {
    const previous = widget.value;
    widget.value = value;
    widget.callback?.(value, app.canvas, node);
    node.onWidgetChanged?.(widget.name, value, previous, widget);
    node.graph?.incrementVersion?.();
  }
  node.graph?.setDirtyCanvas?.(true, true);
}

function setSourceValues(node, widgets, values) {
  const previous = widgets.map((widget) => widget.value);
  try {
    widgets.forEach((widget, index) => {
      setWidgetValue(node, widget, values[index]);
    });
  } catch (error) {
    widgets.forEach((widget, index) => {
      try {
        setWidgetValue(node, widget, previous[index]);
      } catch (_rollbackError) {
        // Preserve the original error. Python revalidates all three values.
      }
    });
    throw error;
  }
}

function safeSourceName(name) {
  if (
    typeof name === "string" &&
    name.length > 0 &&
    name.length <= 255 &&
    name === name.trim() &&
    !/[\\/\u0000-\u001f\u007f]/u.test(name) &&
    (name.toLowerCase().endsWith(".md") ||
      name.toLowerCase().endsWith(".json"))
  ) {
    const folded = name.toLowerCase();
    return (
      folded !== "ush_config.json" &&
      folded !== ".env" &&
      !folded.startsWith(".env.")
    );
  }
  return false;
}

function byteLabel(size) {
  if (size < 1024) return `${size} bytes`;
  return `${(size / 1024).toFixed(1)} KiB`;
}

function installDropWidget(node) {
  if (typeof node.addDOMWidget !== "function") return;

  const pathWidget = findWidget(node, "spec_file");
  const nameWidget = findWidget(node, "uploaded_filename");
  const contentWidget = findWidget(node, "uploaded_content");
  if (!pathWidget || !nameWidget || !contentWidget) return;

  const root = element("div", {
    attributes: {
      role: "group",
      "aria-label": "Specification file loader",
    },
  });
  Object.assign(root.style, {
    boxSizing: "border-box",
    display: "flex",
    alignItems: "center",
    gap: "6px",
    width: "100%",
    minHeight: "58px",
    padding: "7px 8px",
    border: "1px dashed var(--border-color, #666)",
    borderRadius: "6px",
    background: "var(--comfy-input-bg, rgba(20, 20, 20, 0.35))",
  });

  const picker = element("div", {
    attributes: {
      role: "button",
      tabindex: "0",
      "aria-label": "Click or drop a Markdown or JSON specification",
    },
  });
  Object.assign(picker.style, {
    display: "flex",
    flex: "1 1 auto",
    flexDirection: "column",
    gap: "2px",
    minWidth: "0",
    cursor: "pointer",
    outline: "none",
  });

  const instruction = element("div", {
    text: "Click or drop .md / .json",
  });
  Object.assign(instruction.style, {
    fontSize: "12px",
    fontWeight: "500",
    lineHeight: "1.25",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  });
  const status = element("div", {
    text: "UTF-8 · max 256 KiB",
    attributes: { role: "status", "aria-live": "polite" },
  });
  Object.assign(status.style, {
    fontSize: "10.5px",
    lineHeight: "1.25",
    overflowWrap: "anywhere",
    opacity: "0.72",
  });

  const clearButton = element("button", {
    text: "Clear",
    attributes: {
      type: "button",
      title: "Clear the current specification",
      "aria-label": "Clear the current specification",
    },
  });
  Object.assign(clearButton.style, {
    flex: "0 0 auto",
    minHeight: "22px",
    padding: "1px 6px",
    fontSize: "10px",
  });
  const fileInput = element("input", {
    attributes: {
      type: "file",
      accept: ".md,.json,application/json,text/markdown",
      "aria-label": "Choose a Markdown or JSON specification",
    },
  });
  fileInput.style.display = "none";
  picker.append(instruction, status);
  root.append(picker, clearButton, fileInput);

  const renderSourceState = () => {
    const filename = String(nameWidget.value || "");
    const content = String(contentWidget.value || "");
    status.style.color = "";
    if (hasLinkedSourceInput(node)) {
      instruction.textContent = "Source input connected";
      instruction.title = "";
      status.textContent = "Disconnect it to use click/drop";
      clearButton.hidden = true;
      return;
    }
    if (filename && content) {
      const size = new TextEncoder().encode(content).byteLength;
      instruction.textContent = filename;
      instruction.title = filename;
      status.textContent = `Embedded in workflow · ${byteLabel(size)}`;
      clearButton.hidden = false;
      return;
    }
    if (String(pathWidget.value || "")) {
      instruction.textContent = "Trusted path configured";
      instruction.title = "";
      status.textContent = "Fallback source · click/drop to replace";
      clearButton.hidden = false;
      return;
    }
    instruction.textContent = "Click or drop .md / .json";
    instruction.title = "";
    status.textContent = "UTF-8 · max 256 KiB";
    clearButton.hidden = true;
  };

  const setStatus = (message, isError = false) => {
    status.textContent = message;
    status.style.color = isError ? "var(--error-text, #ff8c8c)" : "";
  };

  renderSourceState();

  const openFilePicker = () => {
    if (hasLinkedSourceInput(node)) {
      setStatus("Disconnect linked source inputs before choosing a file.", true);
      return;
    }
    fileInput.click();
  };

  const readFile = async (file) => {
    if (!file) return;
    if (hasLinkedSourceInput(node)) {
      setStatus("Disconnect linked source inputs before using D&D.", true);
      return;
    }
    if (!safeSourceName(file.name)) {
      setStatus("Choose one safely named .md or .json file.", true);
      return;
    }
    if (file.size === 0 || file.size > MAX_SPECIFICATION_BYTES) {
      setStatus("The file must contain 1 byte to 256 KiB of UTF-8 text.", true);
      return;
    }

    try {
      const bytes = await file.arrayBuffer();
      if (bytes.byteLength === 0 || bytes.byteLength > MAX_SPECIFICATION_BYTES) {
        throw new Error("size");
      }
      const content = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
      if (!content.trim()) throw new Error("empty");
      setSourceValues(
        node,
        [pathWidget, nameWidget, contentWidget],
        ["", file.name, content],
      );
      renderSourceState();
    } catch (_error) {
      setStatus("The file could not be read as UTF-8 text within 256 KiB.", true);
    } finally {
      fileInput.value = "";
    }
  };

  picker.addEventListener("click", openFilePicker);
  picker.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    openFilePicker();
  });
  fileInput.addEventListener("change", () => {
    const files = Array.from(fileInput.files || []);
    fileInput.value = "";
    if (files.length !== 1) {
      setStatus("Choose exactly one specification file.", true);
      return;
    }
    void readFile(files[0]);
  });
  clearButton.addEventListener("click", (event) => {
    event.stopPropagation();
    if (hasLinkedSourceInput(node)) {
      setStatus("Disconnect linked source inputs before clearing embedded values.", true);
      return;
    }
    setSourceValues(
      node,
      [pathWidget, nameWidget, contentWidget],
      ["", "", ""],
    );
    renderSourceState();
  });

  root.addEventListener("pointerdown", (event) => event.stopPropagation());

  for (const eventName of ["dragenter", "dragover"]) {
    root.addEventListener(eventName, (event) => {
      event.preventDefault();
      event.stopPropagation();
      root.style.borderColor = "var(--accent-color, #63a7ff)";
    });
  }
  root.addEventListener("dragleave", (event) => {
    event.preventDefault();
    event.stopPropagation();
    root.style.borderColor = "var(--border-color, #666)";
  });
  root.addEventListener("drop", (event) => {
    event.preventDefault();
    event.stopPropagation();
    root.style.borderColor = "var(--border-color, #666)";
    const files = Array.from(event.dataTransfer?.files || []);
    if (files.length !== 1) {
      setStatus("Drop exactly one specification file.", true);
      return;
    }
    void readFile(files[0]);
  });

  const dropWidget = node.addDOMWidget(
    "ush_specification_drop",
    "USH_SPECIFICATION_DROP",
    root,
    {
      serialize: false,
      getMinHeight: () => 58,
    },
  );
  if (!dropWidget) return;
  dropWidget.serialize = false;
  for (const widget of [nameWidget, contentWidget]) {
    hideBackingWidget(widget);
  }

  const fitVisibleWidgets = () => {
    renderSourceState();
    const computed = node.computeSize?.();
    if (!Array.isArray(computed) || computed.length < 2) return;
    const currentWidth = Array.isArray(node.size) ? node.size[0] : computed[0];
    node.setSize?.([Math.max(currentWidth, 260), computed[1]]);
    node.graph?.setDirtyCanvas?.(true, true);
  };
  if (typeof requestAnimationFrame === "function") {
    requestAnimationFrame(fitVisibleWidgets);
  } else {
    fitVisibleWidgets();
  }
  return { renderSourceState };
}

function isTargetNode(node) {
  const nodeType = node.comfyClass || node.constructor?.comfyClass || node.type;
  return nodeType === TARGET_NODE;
}

app.registerExtension({
  name: EXTENSION_NAME,
  async nodeCreated(node) {
    if (!isTargetNode(node)) return;
    const ui = installDropWidget(node);
    if (ui) UI_BY_NODE.set(node, ui);
  },
  loadedGraphNode(node) {
    if (!isTargetNode(node)) return;
    UI_BY_NODE.get(node)?.renderSourceState();
  },
});
