"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const templateDefinitions = [
    ["begin_run.png", "开始乐跑"],
    ["free_run.png", "自由跑确认"],
    ["pause_run.png", "暂停"],
    ["end_run.png", "结束"],
    ["confirm_end.png", "确认结束"],
  ];
  const state = {
    token: null, connected: false, loaded: false, config: null, configLoaded: false,
    fileRevision: null, serverConfig: null, serverConfigChanged: false,
    pendingConfigLoad: null,
    configDirty: false, emulatorDirty: false, busy: false, configRevision: 0, emulatorRevision: 0,
    status: { status: "idle", mode: null, progress: {} },
    cursor: 0, polling: false, templates: new Map(), previewUrls: new Map(),
  };
  const active = () => ["running", "stopping"].includes(state.status.status);
  const finite = (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback;

  function errorText(value) {
    if (typeof value === "string") return value;
    if (Array.isArray(value)) return value.map(errorText).join("\n");
    if (value && typeof value === "object") {
      if (value.msg) return `${Array.isArray(value.loc) ? value.loc.join(".") + ": " : ""}${value.msg}`;
      if (value.message) return errorText(value.message);
      return JSON.stringify(value);
    }
    return String(value || "请求失败");
  }

  async function api(path, { method = "GET", body, bootstrap = false } = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    const headers = {};
    if (!bootstrap && state.token) headers["X-Lepao-Token"] = state.token;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    try {
      const response = await fetch(path, {
        method, headers, body: body === undefined ? undefined : JSON.stringify(body),
        cache: "no-store", signal: controller.signal, credentials: "same-origin",
      });
      const raw = await response.text();
      let result;
      try { result = raw ? JSON.parse(raw) : {}; }
      catch { result = { detail: raw || response.statusText }; }
      if (!response.ok) {
        const error = new Error(errorText(result.detail || result.error || result.message || `HTTP ${response.status}`));
        error.status = response.status;
        throw error;
      }
      return result;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("请求超时，请检查本地服务是否仍在运行。");
      throw error;
    } finally { clearTimeout(timeout); }
  }

  function feedback(message, success = false) {
    const element = $("feedback");
    element.textContent = message;
    element.classList.toggle("success", success);
    element.hidden = !message;
  }

  function setConnected(connected) {
    state.connected = connected;
    $("connection").classList.toggle("connected", connected);
    $("connection").classList.toggle("disconnected", !connected);
    $("sideDot").classList.toggle("connected", connected);
    $("sideDot").classList.toggle("disconnected", !connected);
    $("connectionText").textContent = connected ? "本地服务已连接" : "连接已断开";
    $("sideConnection").textContent = connected ? "本地服务在线" : "正在尝试重新连接";
    updateButtons();
  }

  function updateButtons() {
    const unavailable = !state.connected || !state.loaded || state.busy;
    for (const id of ["previewButton", "startButton", "saveConfigButton"]) {
      $(id).disabled = unavailable || !state.configLoaded || active();
    }
    $("saveEmulatorButton").disabled = unavailable || active();
    $("reloadConfigButton").disabled = unavailable;
    $("exampleConfigButton").disabled = unavailable || active();
    $("configConfirmAccept").disabled = unavailable;
    $("stopButton").disabled = !state.connected || state.busy || state.status.status !== "running";
    $("checkButton").disabled = unavailable;
    document.querySelectorAll(".template-upload").forEach((input) => {
      input.disabled = unavailable || active();
    });
    document.body.classList.toggle("busy", state.busy);
  }

  async function action(callback) {
    if (state.busy) return;
    state.busy = true;
    updateButtons();
    feedback("");
    try { await callback(); }
    catch (error) { feedback(error.message || String(error)); }
    finally { state.busy = false; updateButtons(); }
  }

  function setConfigDirty(dirty) {
    state.configDirty = dirty;
    $("draftWarning").hidden = !dirty;
    $("saveState").textContent = dirty ? (state.serverConfigChanged ? "服务端已更新 · 草稿保留" : "有未保存的修改") : "参数已保存";
    $("saveState").classList.toggle("dirty", dirty);
  }

  function populateConfig(data, { draft = false } = {}) {
    const config = data.config || {};
    state.config = { ...config };
    if (!draft) state.serverConfig = { ...config };
    state.fileRevision = data.revision;
    state.configLoaded = true;
    state.serverConfigChanged = false;
    state.configRevision += 1;
    const base = finite(config.base_speed_mps, 4);
    const amplitude = finite(config.speed_amp, 0);
    $("routeInput").value = (config.walk_path || []).map((point) => point.join(",")).join("\n");
    $("distanceInput").value = finite(config.dist_limit_m, 3000);
    $("instanceInput").value = finite(config.instance_index, 0);
    $("minSpeedInput").value = config.min_speed_mps == null ? base - amplitude : config.min_speed_mps;
    $("maxSpeedInput").value = config.max_speed_mps == null ? base + amplitude : config.max_speed_mps;
    $("autoStartInput").checked = config.auto_start_run !== false;
    $("fallbackInput").checked = config.allow_coordinate_fallback === true;
    setConfigDirty(draft);
    $("configSource").textContent = draft ? "示例草稿已载入，尚未写入本机；检查后点击保存。" : data.source === "example" ? "当前读取示例参数，请按需要修改后保存。" : "已与服务端配置同步，正在编辑的草稿不会被轮询覆盖。";
    if (data.source === "example" && !draft) $("saveState").textContent = "示例参数";
    refreshRoute();
    renderStatus(state.status);
  }

  function parseRoute() {
    const lines = $("routeInput").value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    const points = lines.map((line, index) => {
      const parts = line.split(/[,，]/).map((value) => value.trim());
      if (parts.length !== 2 || parts.some((part) => part === "")) throw new Error(`第 ${index + 1} 行需填写「经度,纬度」。`);
      const point = parts.map(Number);
      if (!point.every(Number.isFinite)) throw new Error(`第 ${index + 1} 行包含无效数值。`);
      if (point[0] < -180 || point[0] > 180 || point[1] <= -90 || point[1] >= 90) {
        throw new Error(`第 ${index + 1} 行超出坐标范围，经度应在 -180 至 180，纬度应在 -90 至 90 之间。`);
      }
      return point;
    });
    if (points.length < 3 || new Set(points.map((point) => point.join(","))).size < 3) {
      throw new Error("请填写至少 3 个不同的路线点。");
    }
    points.forEach((point, index) => {
      const next = points[(index + 1) % points.length];
      if (point[0] === next[0] && point[1] === next[1]) throw new Error("相邻路线点不能重复，末行也不需要重复首点。");
      if (Math.abs(point[0] - next[0]) > 180) throw new Error("暂不支持跨越反经线的路线。");
    });
    return points;
  }

  function collectConfig() {
    if (!$("configForm").reportValidity()) throw new Error("请检查表单中标记的参数。");
    const route = parseRoute();
    const min = Number($("minSpeedInput").value);
    const max = Number($("maxSpeedInput").value);
    const distance = Number($("distanceInput").value);
    const instance = Number($("instanceInput").value);
    if (![min, max, distance].every((value) => Number.isFinite(value) && value > 0)) throw new Error("速度和里程必须是大于 0 的有限数值。");
    if (min > max) throw new Error("最低速度不能大于最高速度。");
    if (!Number.isSafeInteger(instance) || instance < 0) throw new Error("模拟器实例编号必须是非负整数。");
    return {
      ...state.config, walk_path: route, dist_limit_m: distance,
      min_speed_mps: min, max_speed_mps: max, base_speed_mps: min / 2 + max / 2,
      instance_index: instance, auto_start_run: $("autoStartInput").checked,
      allow_coordinate_fallback: $("fallbackInput").checked,
    };
  }

  async function saveConfig() {
    const revision = state.configRevision;
    const config = collectConfig();
    let data;
    try {
      data = await api("/api/config", { method: "PUT", body: { config, expected_revision: state.fileRevision } });
    } catch (error) {
      if (error.status === 409) {
        state.serverConfigChanged = true;
        setConfigDirty(true);
        $("configSource").textContent = "服务端配置已被其他客户端修改。草稿已保留，请重新读取配置后再编辑，或显式载入示例草稿。";
        throw new Error(`保存冲突：你的草稿已保留，未覆盖其他客户端的修改。\n${error.message}\n请重新读取已保存配置后再编辑，或显式载入示例草稿。`);
      }
      throw error;
    }
    // Keep exactly what is in the editor. Only startup populates form controls.
    state.config = { ...(data.config || config) };
    state.serverConfig = { ...state.config };
    state.fileRevision = data.revision;
    state.serverConfigChanged = false;
    const unchanged = state.configRevision === revision;
    state.configRevision += 1;
    if (unchanged) setConfigDirty(false);
    $("configSource").textContent = "参数已保存到本机。";
    renderStatus(state.status);
  }

  async function saveEmulator() {
    const revision = state.emulatorRevision;
    const emu_dir = $("emuDirInput").value.trim();
    const player_path = $("playerPathInput").value.trim();
    if (!emu_dir || !player_path) throw new Error("请填写 MuMu 工具目录和模拟器程序的完整路径。");
    await api("/api/emulator", { method: "POST", body: { emu_dir, player_path } });
    if (state.emulatorRevision === revision) {
      state.emulatorDirty = false;
      $("emulatorSaveState").textContent = "路径已保存";
    }
  }

  function renderPreflight(data) {
    $("preflightSummary").textContent = data.ready ? "运行准备已通过，可启动模拟器任务。" : "还有准备项需要处理；本地预演仍可用于检查路线。";
    $("preflightSummary").classList.toggle("ready", Boolean(data.ready));
    const list = $("checkList");
    list.replaceChildren();
    for (const check of data.checks || []) {
      const item = document.createElement("li");
      const icon = document.createElement("span");
      icon.className = `check-icon${check.ok ? " ok" : ""}`;
      icon.textContent = check.ok ? "✓" : "○";
      icon.setAttribute("aria-label", check.ok ? "通过" : "待处理");
      const content = document.createElement("div");
      const label = document.createElement("strong");
      label.textContent = check.label || check.id;
      const detail = document.createElement("small");
      detail.textContent = check.detail || "";
      content.append(label, detail);
      item.append(icon, content);
      list.append(item);
    }
  }

  async function refreshPreflight() {
    const data = await api("/api/preflight");
    renderPreflight(data);
    return data;
  }

  function formatTime(seconds) {
    const total = Math.max(0, Math.floor(finite(seconds)));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const rest = total % 60;
    return `${hours ? String(hours).padStart(2, "0") + ":" : ""}${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
  }

  function renderStatus(data) {
    state.status = data || { status: "idle", progress: {} };
    const progress = state.status.progress || {};
    const names = { idle: "待开始", running: "进行中", stopping: "停止中", stopped: "已停止", completed: "本地任务结束", failed: "任务异常" };
    const mode = state.status.mode === "preview" ? "本地预演 · 不操作模拟器" : state.status.mode === "emulator" ? "模拟器任务 · 平台结果待验证" : "还没有运行任务";
    const distance = finite(progress.distance_m);
    const speed = finite(progress.speed_mps);
    const targetConfig = state.serverConfig || state.config;
    const configuredTarget = finite(targetConfig && targetConfig.dist_limit_m);
    const target = state.status.target_distance_m == null ? configuredTarget : finite(state.status.target_distance_m, configuredTarget);
    const percent = target > 0 ? Math.max(0, Math.min(100, distance / target * 100)) : 0;
    $("statStatus").textContent = names[state.status.status] || state.status.status || "待开始";
    $("statMode").textContent = mode;
    $("statDistance").textContent = (distance / 1000).toFixed(2);
    $("statTarget").textContent = target > 0 ? `目标 ${(target / 1000).toFixed(2)} km` : "目标 —";
    $("statSpeed").textContent = speed.toFixed(2);
    $("statPace").textContent = speed > 0 ? `配速 ${formatTime(1000 / speed)} / km` : "配速 —";
    $("statElapsed").textContent = formatTime(progress.elapsed_s);
    $("statSteps").textContent = `模拟步数 ${Math.max(0, Math.floor(finite(progress.steps))).toLocaleString("zh-CN")}`;
    $("progressPercent").textContent = `${Math.round(percent)}%`;
    $("progressBar").value = percent;
    $("taskMessage").textContent = state.status.message || (state.status.status === "completed" ? "本地任务已结束，平台记录尚未验证。" : "先试一次本地预演，检查路线和参数。");
    updateButtons();
    drawRoute();
  }

  function appendLogs(data) {
    const list = $("logList");
    const nextCursor = finite(data.cursor, state.cursor);
    if (nextCursor < state.cursor) {
      // A restarted service has a new sequence. Request from zero next time;
      // advancing straight to its cursor would skip the new startup entries.
      list.replaceChildren();
      state.cursor = 0;
      if (!(data.entries || []).length) return;
    }
    for (const entry of data.entries || []) {
      if (finite(entry.seq, -1) <= state.cursor) continue;
      const empty = $("logEmpty");
      if (empty) empty.remove();
      const item = document.createElement("li");
      const level = String(entry.level || "info").toLowerCase();
      item.className = level.includes("error") ? "level-error" : level.includes("warn") ? "level-warning" : "level-info";
      const time = document.createElement("time");
      let timestamp = entry.time;
      if (typeof timestamp === "number") timestamp *= timestamp < 1e12 ? 1000 : 1;
      const date = new Date(timestamp);
      time.textContent = Number.isNaN(date.getTime()) ? String(entry.time || "—") : date.toLocaleTimeString("zh-CN", { hour12: false });
      if (!Number.isNaN(date.getTime())) time.dateTime = date.toISOString();
      const levelElement = document.createElement("span");
      levelElement.className = "log-level";
      levelElement.textContent = level.toUpperCase();
      const message = document.createElement("span");
      message.textContent = String(entry.message || "");
      item.append(time, levelElement, message);
      list.append(item);
    }
    while (list.children.length > 1000) list.firstElementChild.remove();
    state.cursor = nextCursor;
    if ($("followLogsInput").checked) list.scrollTop = list.scrollHeight;
  }

  function makeTemplateCards() {
    for (const [name, title] of templateDefinitions) {
      const card = document.createElement("article");
      card.className = "template-item";
      const heading = document.createElement("h3");
      heading.textContent = title;
      const filename = document.createElement("span");
      filename.className = "template-name";
      filename.textContent = name;
      const preview = document.createElement("div");
      preview.className = "template-preview";
      const placeholder = document.createElement("span");
      placeholder.textContent = "未上传模板";
      preview.append(placeholder);
      const status = document.createElement("div");
      status.className = "template-state";
      status.textContent = "等待读取";
      const label = document.createElement("label");
      label.className = "upload-label";
      const input = document.createElement("input");
      input.type = "file";
      input.accept = "image/png,.png";
      input.className = "template-upload";
      input.setAttribute("aria-label", `上传${title} PNG 模板`);
      input.addEventListener("change", () => {
        const file = input.files && input.files[0];
        if (file) action(() => uploadTemplate(name, title, file)).finally(() => { input.value = ""; });
      });
      label.append(input, document.createTextNode("选择 PNG"));
      card.append(heading, filename, preview, status, label);
      $("templateGrid").append(card);
      state.templates.set(name, { preview, placeholder, status, input });
    }
  }

  function renderTemplates(data) {
    let ready = 0;
    for (const [name] of templateDefinitions) {
      const item = (data.items || []).find((candidate) => candidate.name === name);
      const view = state.templates.get(name);
      const valid = Boolean(item && item.exists && item.readable);
      if (valid) ready += 1;
      view.status.textContent = valid ? "已准备 · 可读取" : item && item.exists ? "图片不可读取，请重新上传" : "尚未配置";
      view.status.classList.toggle("ready", valid);
      if (!state.previewUrls.has(name)) view.placeholder.textContent = valid ? "已有本地模板" : "未上传模板";
      if (valid && !state.previewUrls.has(name) && !view.loading) loadExistingTemplate(name);
    }
    $("templateCount").textContent = `${ready} / 5 已准备`;
  }

  async function loadExistingTemplate(name) {
    const view = state.templates.get(name);
    view.loading = true;
    try {
      const response = await fetch(`/api/templates/${encodeURIComponent(name)}`, {
        headers: { "X-Lepao-Token": state.token }, cache: "no-store",
      });
      if (!response.ok) return;
      const blob = await response.blob();
      if (state.previewUrls.has(name)) return; // A newer upload already supplied a preview.
      const url = URL.createObjectURL(blob);
      const image = document.createElement("img");
      image.src = url;
      image.alt = `${templateDefinitions.find(([file]) => file === name)[1]}已保存模板`;
      state.previewUrls.set(name, url);
      view.preview.replaceChildren(image);
    } catch { /* The readable flag still describes the saved file if preview fails. */ }
    finally { view.loading = false; }
  }

  async function uploadTemplate(name, title, file) {
    if (file.size > 2 * 1024 * 1024) throw new Error(`${title}图片超过 2 MiB，请先裁剪或缩小。`);
    const bytes = new Uint8Array(await file.arrayBuffer());
    const signature = [137, 80, 78, 71, 13, 10, 26, 10];
    if (!signature.every((value, index) => bytes[index] === value)) throw new Error("请选择真实的 PNG 图片，修改扩展名不能转换格式。");
    let binary = "";
    for (let offset = 0; offset < bytes.length; offset += 16384) binary += String.fromCharCode(...bytes.subarray(offset, offset + 16384));
    await api(`/api/templates/${encodeURIComponent(name)}`, { method: "PUT", body: { content_base64: btoa(binary) } });
    if (state.previewUrls.has(name)) URL.revokeObjectURL(state.previewUrls.get(name));
    const url = URL.createObjectURL(file);
    state.previewUrls.set(name, url);
    const image = document.createElement("img");
    image.src = url;
    image.alt = `${title}按钮模板`;
    state.templates.get(name).preview.replaceChildren(image);
    renderTemplates(await api("/api/templates"));
    await refreshPreflight();
    feedback(`${title}模板已保存。`, true);
  }

  function refreshRoute() {
    try {
      const points = parseRoute();
      $("routeError").textContent = "";
      $("routeInput").removeAttribute("aria-invalid");
      $("routePointCount").textContent = `${points.length} 个路线点`;
    } catch (error) {
      $("routeError").textContent = error.message;
      $("routeInput").setAttribute("aria-invalid", "true");
      $("routePointCount").textContent = "路线待检查";
    }
    drawRoute();
  }

  function drawRoute() {
    const canvas = $("routeCanvas");
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (!width || !height) return;
    const ratio = Math.max(1, window.devicePixelRatio || 1);
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.scale(ratio, ratio);
    ctx.clearRect(0, 0, width, height);
    ctx.strokeStyle = "#e4e9dc";
    ctx.lineWidth = 1;
    for (let x = 16; x < width; x += 32) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, height); ctx.stroke(); }
    for (let y = 16; y < height; y += 32) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke(); }
    let points;
    try {
      points = active() && state.serverConfig && Array.isArray(state.serverConfig.walk_path) ? state.serverConfig.walk_path : parseRoute();
      if (points.length < 3) throw new Error("Route unavailable");
    }
    catch { $("routeEmpty").hidden = false; return; }
    $("routePointCount").textContent = `${active() ? "运行路线 · " : state.configDirty ? "草稿 · " : ""}${points.length} 个路线点`;
    $("routeEmpty").hidden = true;
    const middleLat = points.reduce((sum, point) => sum + point[1], 0) / points.length;
    const cos = Math.cos(middleLat * Math.PI / 180);
    const xs = points.map((point) => point[0] * cos);
    const ys = points.map((point) => point[1]);
    const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
    const padding = 43;
    const scale = Math.min((width - padding * 2) / Math.max(maxX - minX, 0.000001), (height - padding * 2) / Math.max(maxY - minY, 0.000001));
    const project = (point) => [width / 2 + (point[0] * cos - (minX + maxX) / 2) * scale, height / 2 - (point[1] - (minY + maxY) / 2) * scale];
    const pixels = points.map(project);
    ctx.beginPath();
    pixels.forEach(([x, y], index) => index ? ctx.lineTo(x, y) : ctx.moveTo(x, y));
    ctx.closePath();
    ctx.fillStyle = "#bfd6bc35";
    ctx.fill();
    ctx.strokeStyle = "#5c8d76";
    ctx.lineWidth = 3;
    ctx.lineJoin = "round";
    ctx.stroke();
    pixels.forEach(([x, y], index) => {
      ctx.beginPath(); ctx.arc(x, y, index === 0 ? 5 : 3.5, 0, Math.PI * 2);
      ctx.fillStyle = index === 0 ? "#233f3c" : "#fffefa"; ctx.fill();
      ctx.strokeStyle = "#5c8d76"; ctx.lineWidth = 1.5; ctx.stroke();
      ctx.font = "9px monospace"; ctx.fillStyle = "#67816d"; ctx.fillText(String(index + 1).padStart(2, "0"), x + 8, y - 8);
    });
    const progress = state.status.progress || {};
    if (state.status.mode && progress.lon != null && progress.lat != null && Number.isFinite(Number(progress.lon)) && Number.isFinite(Number(progress.lat))) {
      const [x, y] = project([Number(progress.lon), Number(progress.lat)]);
      if (x >= 0 && x <= width && y >= 0 && y <= height) {
        ctx.beginPath(); ctx.arc(x, y, 11, 0, Math.PI * 2); ctx.fillStyle = "#d2845030"; ctx.fill();
        ctx.beginPath(); ctx.arc(x, y, 5.5, 0, Math.PI * 2); ctx.fillStyle = "#d28450"; ctx.fill();
        ctx.strokeStyle = "#fffefa"; ctx.lineWidth = 2; ctx.stroke();
      }
    }
  }

  async function start(mode) {
    if (active()) throw new Error("已有任务运行中，请先停止当前任务。");
    if (state.configDirty) {
      await saveConfig();
    } else {
      // Do not write a stale browser snapshot over changes made through MCP.
      const revision = state.configRevision;
      const latest = await api("/api/config");
      if (state.configDirty || state.configRevision !== revision) throw new Error("读取期间参数又有修改，请检查后重新开始。");
      populateConfig(latest);
    }
    if (state.configDirty) throw new Error("保存期间路线或参数又有修改，请检查后重新开始。");
    const startRevision = state.configRevision;
    if (mode === "emulator") {
      if (state.emulatorDirty) await saveEmulator();
      if (state.emulatorDirty) throw new Error("保存期间路径又有修改，请检查后重新开始。");
      const readiness = await refreshPreflight();
      if (!readiness.ready) {
        const missing = (readiness.checks || []).filter((check) => !check.ok).map((check) => `• ${check.label || check.id}：${check.detail || "需要处理"}`).join("\n");
        throw new Error(`模拟器任务尚未准备好。\n${missing}`);
      }
    }
    if (state.configDirty || state.configRevision !== startRevision) throw new Error("运行准备期间配置发生了变化，请检查后重新开始。");
    renderStatus(await api("/api/start", { method: "POST", body: { mode } }));
  }

  async function syncConfig() {
    if (state.busy) return;
    const revision = state.configRevision;
    const wasDirty = state.configDirty;
    try {
      const data = await api("/api/config");
      if (state.busy || state.configRevision !== revision) return;
      state.serverConfig = { ...(data.config || {}) };
      if (data.revision !== state.fileRevision || !state.configLoaded) {
        if (!state.configDirty && !wasDirty && state.configRevision === revision && !state.busy) {
          populateConfig(data);
        } else {
          state.serverConfigChanged = true;
          $("configSource").textContent = "服务端配置已改变；当前草稿已保留，保存时会检查版本冲突。";
          if (state.configDirty) setConfigDirty(true);
        }
      }
      renderStatus(state.status);
    } catch (error) {
      if (state.busy || state.configRevision !== revision) return;
      $("configSource").textContent = `服务端配置暂时无法读取：${error.message}。可使用“载入示例草稿”显式修复。`;
    }
  }

  async function poll() {
    if (state.polling) return;
    state.polling = true;
    try {
      if (!state.token || !state.connected) {
        const session = await api("/api/bootstrap", { bootstrap: true });
        state.token = session.token;
      }
      const [status, logs] = await Promise.all([api("/api/status"), api(`/api/logs?after=${encodeURIComponent(state.cursor)}`)]);
      setConnected(true);
      renderStatus(status);
      appendLogs(logs);
      await syncConfig();
    } catch (error) {
      setConnected(false);
      if (error.status === 401 || error.status === 403) state.token = null;
    } finally { state.polling = false; }
  }

  async function initialize() {
    try {
      const session = await api("/api/bootstrap", { bootstrap: true });
      if (!session.token) throw new Error("本地服务未返回连接凭证，请重启服务后刷新页面。");
      state.token = session.token;
      state.loaded = true;
      setConnected(true);
      const modules = [
        ["配置", "/api/config", populateConfig],
        ["模拟器路径", "/api/emulator", (emulator) => {
          $("emuDirInput").value = emulator.emu_dir || "";
          $("playerPathInput").value = emulator.player_path || "";
          $("emulatorSaveState").textContent = emulator.emu_dir ? "已读取本机路径" : "填写路径后保存";
        }],
        ["模板", "/api/templates", renderTemplates],
        ["准备检查", "/api/preflight", renderPreflight],
        ["任务状态", "/api/status", renderStatus],
        ["日志", "/api/logs?after=0", appendLogs],
      ];
      const results = await Promise.allSettled(modules.map(async ([, path, render]) => render(await api(path))));
      const failures = [];
      results.forEach((result, index) => {
        if (result.status !== "rejected") return;
        const message = result.reason.message || String(result.reason);
        failures.push(`${modules[index][0]}：${message}`);
        if (index === 0) {
          $("saveState").textContent = "配置需要修复";
          $("configSource").textContent = "无法读取已保存配置。可重新读取，或点击“载入示例草稿”，检查后显式保存修复。";
        }
      });
      feedback(failures.length ? `部分数据无法读取，其他控制仍可使用：\n${failures.join("\n")}` : "");
      updateButtons();
    } catch (error) {
      setConnected(false);
      feedback(`无法读取控制台数据：${error.message}。确认本地服务仍在运行后刷新页面。`);
    }
  }

  $("configForm").addEventListener("input", () => { state.configRevision += 1; setConfigDirty(true); refreshRoute(); });
  $("configForm").addEventListener("submit", (event) => {
    event.preventDefault();
    action(async () => { await saveConfig(); await refreshPreflight(); feedback("路线与参数已保存。", true); });
  });
  $("emulatorForm").addEventListener("input", () => { state.emulatorRevision += 1; state.emulatorDirty = true; $("emulatorSaveState").textContent = "路径未保存，离开页面将丢失修改"; });
  $("emulatorForm").addEventListener("submit", (event) => {
    event.preventDefault();
    action(async () => { await saveEmulator(); await refreshPreflight(); feedback("MuMu 路径已保存。", true); });
  });
  $("previewButton").addEventListener("click", () => action(() => start("preview")));
  $("startButton").addEventListener("click", () => action(() => start("emulator")));
  $("stopButton").addEventListener("click", () => action(async () => {
    renderStatus(await api("/api/stop", { method: "POST", body: {} }));
  }));
  $("checkButton").addEventListener("click", () => action(async () => {
    await refreshPreflight();
    renderTemplates(await api("/api/templates"));
    if (state.configDirty || state.emulatorDirty) feedback("检查使用已保存的配置；当前表单还有未保存的修改。");
  }));
  function loadConfigFromServer(kind) {
    return action(async () => {
      if (kind === "example") {
        populateConfig(await api("/api/config/example"), { draft: true });
        feedback("示例已载入表单，尚未写入配置文件。请检查后点击保存。", true);
      } else {
        populateConfig(await api("/api/config"));
        feedback("已读取服务端最新配置。", true);
      }
    });
  }

  function closeConfigConfirmation(restoreFocus = true) {
    const pending = state.pendingConfigLoad;
    state.pendingConfigLoad = null;
    $("configConfirm").hidden = true;
    for (const id of ["reloadConfigButton", "exampleConfigButton"]) $(id).setAttribute("aria-expanded", "false");
    if (restoreFocus && pending) $(pending.trigger).focus();
    return pending;
  }

  function requestConfigLoad(kind, trigger) {
    if (state.busy) return;
    if (!state.configDirty) { loadConfigFromServer(kind); return; }
    state.pendingConfigLoad = { kind, trigger };
    $("configConfirmText").textContent = kind === "example"
      ? "示例参数会替换当前表单中的未保存修改；配置文件仍不会改变，直到你点击保存。"
      : "服务端已保存的配置会替换当前表单，未保存修改将被丢弃。";
    $("configConfirm").hidden = false;
    $(trigger).setAttribute("aria-expanded", "true");
    $("configConfirmCancel").focus();
  }

  $("reloadConfigButton").addEventListener("click", () => requestConfigLoad("saved", "reloadConfigButton"));
  $("exampleConfigButton").addEventListener("click", () => requestConfigLoad("example", "exampleConfigButton"));
  $("configConfirmCancel").addEventListener("click", () => closeConfigConfirmation());
  $("configConfirmAccept").addEventListener("click", () => {
    const pending = closeConfigConfirmation();
    if (pending) loadConfigFromServer(pending.kind);
  });
  $("configConfirm").addEventListener("keydown", (event) => {
    if (event.key === "Escape") { event.preventDefault(); closeConfigConfirmation(); }
  });
  window.addEventListener("resize", drawRoute);
  makeTemplateCards();
  updateButtons();
  initialize();
  setInterval(() => { if (state.loaded) poll(); }, 1200);
})();
