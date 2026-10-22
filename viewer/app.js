"use strict";

const palette = ["#6574c7", "#258596", "#d48740", "#b26a99", "#5b936a", "#6685aa"];
const sourceNames = { accessibility: "无障碍控件树", arkui: "ArkUI组件树" };
const state = {
  data: null, windows: [], selected: null, node: null, entries: [], expanded: new Set(), zoom: 1,
  source: "accessibility", rootPointer: "",
};

/** @brief 取得页面中已经声明的元素。 */
function element(id) {
  return document.getElementById(id);
}

/** @brief 创建仅使用文本内容的元素，避免采集内容被解释为 HTML。 */
function create(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = String(text);
  return item;
}

/** @brief 按原始 JSON Pointer 定位窗口子树，不依赖窗口排序。 */
function resolvePointer(root, pointer) {
  return pointer.split("/").slice(1).reduce((node, part) => {
    const key = part.replace(/~1/g, "/").replace(/~0/g, "~");
    if (!node || !Object.hasOwn(node, key)) throw new Error(`Invalid layout pointer: ${pointer}`);
    return node[key];
  }, root);
}

/** @brief 将原始控件层级索引为带路径的节点列表。 */
function indexTree(node, pointer, depth = 0) {
  const entry = { node, pointer, depth };
  state.entries.push(entry);
  node.children.forEach((child, index) => indexTree(child, `${pointer}/children/${index}`, depth + 1));
  return entry;
}

/** @brief 将窗口边界限制在截图范围，并保持原始像素坐标含义。 */
function clippedBox(box) {
  const display = state.data.metadata.display;
  const x = Math.max(0, box.x);
  const y = Math.max(0, box.y);
  const right = Math.min(display.width, box.x + box.width);
  const bottom = Math.min(display.height, box.y + box.height);
  return { x, y, width: Math.max(0, right - x), height: Math.max(0, bottom - y) };
}

/** @brief 以百分比设置矩形，使窗口与截图在任意缩放下准确对齐。 */
function positionBox(item, rawBox) {
  const box = clippedBox(rawBox);
  const display = state.data.metadata.display;
  item.style.left = `${box.x / display.width * 100}%`;
  item.style.top = `${box.y / display.height * 100}%`;
  item.style.width = `${box.width / display.width * 100}%`;
  item.style.height = `${box.height / display.height * 100}%`;
  item.hidden = box.width === 0 || box.height === 0;
}

/** @brief 展示本次采集时间、尺寸和节点总量。 */
function renderHeader() {
  const metadata = state.data.metadata;
  const date = new Date(metadata.started_at);
  element("capture-time").textContent = `${date.toISOString().slice(0, 19).replace("T", " ")} UTC`;
  element("capture-time").title = metadata.capture_id;
  const stats = [
    ["显示器", `${metadata.display.id} · ${metadata.display.width} × ${metadata.display.height}`],
    ["应用窗口", `${state.windows.length} 个`],
    ...treeStatistics(),
  ];
  element("header-stats").replaceChildren();
  stats.forEach(([name, value]) => {
    const item = create("div", "header-stat");
    item.append(create("span", "", name), create("strong", "", value));
    element("header-stats").append(item);
  });
  element("screen-subtitle").textContent = `${metadata.device.model} · ${metadata.device.os_version}`;
}

/** @brief 汇总当前来源的独立窗口树节点数量。 */
function treeStatistics() {
  const trees = state.windows.map((window) => window.trees[state.source]);
  const count = trees.reduce((sum, tree) => sum + tree.node_count, 0);
  return [[`${sourceNames[state.source]}节点`, `${count} 个`]];
}

/** @brief 显示独立窗口树的采集来源及坐标含义。 */
function renderSourceNotice() {
  const arkui = state.source === "arkui";
  const reference = state.selected.trees[state.source];
  const note = element("tree-source-note");
  note.textContent = arkui ? "FrameRect 为局部布局坐标，ArkUI组件不在截图中高亮。"
    : "显示无障碍接口返回的控件与语义属性。";
  const scope = element("tree-source-scope");
  scope.hidden = false;
  scope.textContent = `source: ${reference.source}`;
  if (arkui) scope.textContent += ` · scope: ${reference.scope} · display_id: ${reference.display_id}`;
}

/** @brief 以小状态说明采集时间关系和窗口树覆盖范围。 */
function renderNotes() {
  const metadata = state.data.metadata;
  const sequential = create("span", "", "一张截图 · 顺序采集 · 非原子快照");
  const omitted = metadata.foreground_window_ids_without_tree;
  const coverage = create("span", "", `${omitted.length} 个前景窗口未返回控件树`);
  coverage.title = `窗口 ID：${omitted.join("、")}。可能包含系统窗口或完全被遮挡的应用。`;
  element("capture-notes").append(sequential, coverage);
}

/** @brief 为每个窗口绘制边界和包名、层叠顺序标注。 */
function renderOverlays() {
  const layer = element("window-overlays");
  state.windows.slice().reverse().forEach((window) => {
    const overlay = create("div", "window-overlay");
    overlay.dataset.windowId = String(window.window_id);
    overlay.style.setProperty("--window-color", window.color);
    positionBox(overlay, window.bounding_box);
    const tag = create("div", "window-tag");
    tag.append(create("span", "tag-bundle", window.bundle_name));
    tag.append(create("span", "tag-z", `z_order ${window.z_order}`));
    overlay.append(tag);
    layer.append(overlay);
  });
}

/** @brief 显示始终可以访问的窗口列表，包括被其他窗口遮挡的窗口。 */
function renderWindowList() {
  const list = element("window-list");
  list.replaceChildren();
  state.windows.forEach((window) => {
    const button = create("button", "window-card");
    button.type = "button";
    button.dataset.windowId = String(window.window_id);
    button.style.setProperty("--window-color", window.color);
    button.setAttribute("aria-pressed", String(window === state.selected));
    button.classList.toggle("selected", window === state.selected);
    const heading = create("div", "card-heading");
    heading.append(create("span", "color-dot"), create("strong", "", window.window_name));
    heading.append(create("span", "z-badge", `z_order ${window.z_order}`));
    const box = window.bounding_box;
    const detail = create("div", "card-detail");
    detail.append(create("span", "", `窗口 ${window.window_id}`));
    detail.append(create("span", "", `${box.width} × ${box.height}`));
    const count = create("span", "", `${window.trees[state.source].node_count} 节点`);
    count.title = sourceNames[state.source];
    detail.append(count);
    button.append(heading, create("p", "card-bundle", window.bundle_name), detail);
    button.addEventListener("click", () => selectWindow(window));
    list.append(button);
  });
}

/** @brief 展示窗口标识和原始矩形坐标。 */
function renderWindowFacts() {
  const window = state.selected;
  const box = window.bounding_box;
  element("selected-name").textContent = window.window_name;
  element("selected-bundle").textContent = window.bundle_name;
  element("selected-z").textContent = `z_order ${window.z_order}`;
  element("selected-dot").style.setProperty("--window-color", window.color);
  const facts = [["窗口 / 显示器", `${window.window_id} / ${window.display_id}`],
    ["bounding_box", `(${box.x}, ${box.y}) · ${box.width} × ${box.height}`]];
  const container = element("window-facts");
  container.replaceChildren();
  facts.forEach(([label, value]) => {
    const item = create("div", "window-fact");
    item.append(create("span", "", label), create("strong", "", value));
    container.append(item);
  });
  element("node-count").textContent = String(window.trees[state.source].node_count);
}

/** @brief 使用所选来源的窗口树，保留两个来源各自的原始定位方式。 */
function loadSelectedTree() {
  const reference = state.selected.trees[state.source];
  renderSourceNotice();
  state.entries = [];
  state.rootPointer = state.source === "accessibility" ? reference.json_pointer : "";
  const root = state.source === "accessibility"
    ? resolvePointer(state.data.trees.accessibility, reference.json_pointer)
    : state.data.trees.arkui[String(state.selected.window_id)];
  indexTree(root, state.rootPointer);
  state.expanded = new Set(state.entries.filter((entry) => entry.depth < 4).map((entry) => entry.pointer));
  element("tree-search").value = "";
  element("tree-search").placeholder = state.source === "arkui"
    ? "搜索 tag、ID、AccessibilityId 或原始属性" : "搜索 text、id、type";
  element("tree-container").scrollTop = 0;
  selectNode(state.entries[0]);
}

/** @brief 切换窗口，同时保持当前选择的控件树来源。 */
function selectWindow(window) {
  state.selected = window;
  renderWindowList();
  renderWindowFacts();
  const outline = element("selection-outline");
  outline.style.setProperty("--window-color", window.color);
  positionBox(outline, window.bounding_box);
  loadSelectedTree();
}

/** @brief 切换树来源并更新计数、原始文件说明和高亮能力。 */
function selectSource(source) {
  state.source = source;
  for (const name of Object.keys(sourceNames)) {
    element(`source-${name}`).setAttribute("aria-pressed", String(name === source));
  }
  element("highlight-node").disabled = source === "arkui";
  renderHeader();
  selectWindow(state.selected);
}

/** @brief 获取适合树节点行的控件内容。 */
function nodeDescription(attributes) {
  if (state.source === "arkui") {
    if (attributes.textContent) return `“${attributes.textContent}”`;
    if (attributes.ID !== undefined) return `ID ${attributes.ID}`;
    return attributes.AccessibilityId !== undefined ? `AccessibilityId ${attributes.AccessibilityId}` : "";
  }
  const text = attributes.text || attributes.originalText;
  if (text) return `“${text}”`;
  if (attributes.id) return `#${attributes.id}`;
  return attributes.description || "";
}

/** @brief 创建一个节点行，展开操作和节点选择互不干扰。 */
function renderTreeRow(entry, search) {
  const row = create("div", "tree-row");
  row.dataset.pointer = entry.pointer;
  row.style.paddingLeft = `${search ? 0 : entry.depth * 12}px`;
  row.classList.toggle("selected", entry === state.node);
  const toggle = create("button", "tree-toggle");
  toggle.type = "button";
  toggle.disabled = search || entry.node.children.length === 0;
  toggle.textContent = toggle.disabled ? "·" : state.expanded.has(entry.pointer) ? "▾" : "▸";
  toggle.setAttribute("aria-label", state.expanded.has(entry.pointer) ? "收起子节点" : "展开子节点");
  toggle.addEventListener("click", () => toggleNode(entry));
  const button = create("button", "tree-select");
  button.type = "button";
  button.title = entry.pointer;
  button.append(create("span", "node-kind", entry.node.attributes.type || "(空类型)"));
  button.append(create("span", "node-description", nodeDescription(entry.node.attributes)));
  if (entry.node.children.length) button.append(create("span", "node-children", entry.node.children.length));
  button.addEventListener("click", () => selectNode(entry));
  row.append(toggle, button);
  return row;
}

/** @brief 判断当前节点是否匹配文本、标识或类型搜索。 */
function matchesSearch(entry, query) {
  const attrs = entry.node.attributes;
  const fields = state.source === "arkui"
    ? [attrs.type, attrs.ID, attrs.AccessibilityId, JSON.stringify(attrs), entry.node.raw]
    : [attrs.text, attrs.originalText, attrs.id, attrs.type];
  return fields
    .some((value) => String(value ?? "").toLocaleLowerCase().includes(query));
}

/** @brief 判断树节点的所有祖先是否处于展开状态。 */
function isVisibleEntry(entry) {
  let pointer = entry.pointer;
  while (pointer !== state.rootPointer) {
    pointer = pointer.slice(0, pointer.lastIndexOf("/children/"));
    if (!state.expanded.has(pointer)) return false;
  }
  return true;
}

/** @brief 渲染完整层级或搜索结果，同时保留原始节点路径。 */
function renderTree() {
  const query = element("tree-search").value.trim().toLocaleLowerCase();
  const entries = state.entries.filter((entry) => query ? matchesSearch(entry, query) : isVisibleEntry(entry));
  const fragment = document.createDocumentFragment();
  entries.forEach((entry) => fragment.append(renderTreeRow(entry, Boolean(query))));
  if (!entries.length) fragment.append(create("p", "empty-state", "没有匹配的控件"));
  element("tree-container").replaceChildren(fragment);
  element("search-summary").textContent = query ? `找到 ${entries.length} 个匹配节点` : "选择节点查看原始属性";
}

/** @brief 切换单个节点的展开状态。 */
function toggleNode(entry) {
  if (state.expanded.has(entry.pointer)) state.expanded.delete(entry.pointer);
  else state.expanded.add(entry.pointer);
  renderTree();
}

/** @brief 展示选中节点的全部原始属性，不过滤空值。 */
function selectNode(entry) {
  state.node = entry;
  const fields = Object.entries(entry.node.attributes);
  const preferred = state.source === "arkui"
    ? ["type", "ID", "AccessibilityId", "FrameRect", "textContent"]
    : ["type", "text", "id", "bounds", "accessibilityId", "hostWindowId", "displayId"];
  const priority = (key) => preferred.includes(key) ? preferred.indexOf(key) : preferred.length;
  fields.sort(([left], [right]) => priority(left) - priority(right));
  const fragment = document.createDocumentFragment();
  fields.forEach(([key, value]) => {
    fragment.append(create("dt", "", key));
    const content = typeof value === "object" ? JSON.stringify(value) : String(value);
    fragment.append(create("dd", content === "" ? "empty" : "", content === "" ? "(空)" : content));
  });
  element("node-attributes").replaceChildren(fragment);
  const reference = state.selected.trees[state.source];
  element("node-path").textContent = state.source === "arkui"
    ? `${reference.file}#${entry.pointer}` : `${reference.file}${entry.pointer}`;
  element("node-path").title = element("node-path").textContent;
  if (state.source === "arkui") {
    element("node-path").title += `\n原文：${reference.raw.file}:${entry.node.source_line}`;
  }
  element("attributes-title").textContent = `节点属性 · ${entry.node.attributes.type || "(空类型)"}`;
  element("attributes-title").title = "值来自原始控件树；(空) 表示空字符串。";
  element("raw-node").hidden = state.source !== "arkui";
  element("raw-node").open = false;
  element("raw-node-text").textContent = state.source === "arkui" ? entry.node.raw : "";
  element("node-attributes").parentElement.scrollTop = 0;
  renderTree();
  highlightNode();
}

/** @brief 根据原始 bounds 高亮控件，不改变窗口点击层级。 */
function highlightNode() {
  const outline = element("node-outline");
  outline.hidden = true;
  if (state.source === "arkui") return;
  if (!element("highlight-node").checked || !state.node) return;
  if (state.node.pointer === state.rootPointer) return;
  const bounds = state.node.node.attributes.bounds;
  const match = /^\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]$/.exec(bounds);
  if (!match) {
    console.error("Invalid node bounds:", bounds);
    return;
  }
  const [x, y, right, bottom] = match.slice(1).map(Number);
  positionBox(outline, { x, y, width: right - x, height: bottom - y });
}

/** @brief 用真实窗口层叠顺序处理截图点击，选中状态不影响命中。 */
function selectAtPoint(event) {
  const rect = element("screen").getBoundingClientRect();
  const display = state.data.metadata.display;
  const x = (event.clientX - rect.left) / rect.width * display.width;
  const y = (event.clientY - rect.top) / rect.height * display.height;
  const target = state.windows.find((window) => {
    const box = window.bounding_box;
    return x >= box.x && x < box.x + box.width && y >= box.y && y < box.y + box.height;
  });
  if (target) selectWindow(target);
}

/** @brief 根据可用画布尺寸调整缩放，放大后保留滚动访问完整截图。 */
function fitCanvas() {
  const viewport = element("canvas-viewport");
  const display = state.data.metadata.display;
  const ratio = display.width / display.height;
  const width = Math.min(viewport.clientWidth - 44, (viewport.clientHeight - 44) * ratio) * state.zoom;
  element("screen").style.width = `${width}px`;
  element("screen").style.height = `${width / ratio}px`;
  element("canvas-content").style.width = `${Math.max(viewport.clientWidth, width + 44)}px`;
  const percent = Math.round(width / display.width * 100);
  element("zoom-level").textContent = `${percent}% · 原图 ${display.width} × ${display.height}`;
}

/** @brief 连接窗口选择、树搜索和截图显示控制。 */
function bindControls() {
  for (const name of Object.keys(sourceNames)) {
    element(`source-${name}`).addEventListener("click", () => selectSource(name));
  }
  element("screen").addEventListener("click", selectAtPoint);
  element("tree-search").addEventListener("input", renderTree);
  element("highlight-node").addEventListener("change", highlightNode);
  element("show-labels").addEventListener("change", (event) => {
    element("window-overlays").hidden = !event.target.checked;
  });
  element("expand-all").addEventListener("click", () => {
    state.expanded = new Set(state.entries.map((entry) => entry.pointer));
    renderTree();
  });
  element("collapse-all").addEventListener("click", () => {
    state.expanded.clear();
    renderTree();
  });
  element("fit-view").addEventListener("click", () => {
    state.zoom = 1;
    fitCanvas();
  });
  element("zoom-in").addEventListener("click", () => {
    state.zoom = Math.min(4, state.zoom + .5);
    fitCanvas();
  });
  new ResizeObserver(fitCanvas).observe(element("canvas-viewport"));
}

/** @brief 加载单文件页面中的采集数据并构建检查器。 */
function initialize() {
  try {
    state.data = JSON.parse(element("capture-data").textContent);
    state.windows = state.data.metadata.windows.slice().sort((left, right) => right.z_order - left.z_order);
    state.windows.forEach((window, index) => { window.color = palette[index % palette.length]; });
    const display = state.data.metadata.display;
    element("canvas-viewport").style.aspectRatio = `${display.width} / ${display.height}`;
    element("screenshot").src = state.data.screenshot;
    renderNotes();
    renderOverlays();
    bindControls();
    const application = state.windows.find((window) => window.window_type === 1);
    state.selected = application || state.windows[0];
    selectSource("accessibility");
    fitCanvas();
  } catch (error) {
    console.error("Failed to initialize capture viewer:", error);
    element("error-message").textContent = `无法显示采集结果：${error.message}`;
    element("error-message").hidden = false;
    document.querySelector(".workspace").hidden = true;
  }
}

initialize();
