/* ─── State ─── */
let accounts = [];
let entries = [];
let allResults = [];
let entryIdCounter = 0;
let uploadRunning = false;
let loginLock = false;
let currentView = "dashboard";
let logCollapsed = false;
let authUser = null;
let authResolved = false;
const accountVerifyPromises = new Map();
let pendingLoginAccountName = "";
const PRIMARY_ACCOUNT_NAME = "default";
const BACKGROUND_VERIFY_INTERVAL_MS = 2 * 60 * 1000;
let backgroundVerifyTimer = null;
let currentVersion = "0.0.0";
let forceUpdateInfo = null;
let optionalUpdateInfo = null;
const WINDOW_RECHECK_THROTTLE_MS = 30 * 1000;
let windowCheckInFlight = null;
let lastWindowCheckAt = 0;
let lastOptionalUpdatePromptKey = "";
/** 当前批次开始上传时的条目 id 顺序（与后端 current 下标一致），用于发表后从队列移除 */
let uploadOrderIds = [];

const addEntryBtnDefault = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>添加到队列`;

/* ─── Helpers ─── */
const esc = (s) => {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
};
const $ = (id) => document.getElementById(id);
const toLocalDatetime = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}T${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;

async function api(url, opts = {}) {
  const skipAuthRedirect = Boolean(opts.skipAuthRedirect);
  const headers = new Headers(opts.headers || {});
  if (!(opts.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const fetchOptions = { ...opts, headers };
  delete fetchOptions.skipAuthRedirect;
  const res = await fetch(url, fetchOptions);
  if (res.status === 401) {
    if (!skipAuthRedirect) handleUnauthorized();
    throw new Error("UNAUTHORIZED");
  }
  return res;
}

function setLoginError(message) {
  const el = $("loginError");
  if (!el) return;
  el.textContent = message || "";
  el.style.display = message ? "block" : "none";
}

function setUpdateError(message) {
  const el = $("updateError");
  if (!el) return;
  el.textContent = message || "";
  el.style.display = message ? "block" : "none";
}

function setAuthUser(user) {
  authUser = user || null;
  const name = (authUser && authUser.username) || "";
  $("authUserName").textContent = name || "-";
  $("sidebarUser").style.display = authUser ? "flex" : "none";
}

function showAuthShell() {
  $("updateShell").style.display = "none";
  $("authShell").style.display = "flex";
  $("appShell").style.display = "none";
}

function showAppShell() {
  $("updateShell").style.display = "none";
  $("authShell").style.display = "none";
  $("appShell").style.display = "flex";
}

function showUpdateShell() {
  $("updateShell").style.display = "flex";
  $("authShell").style.display = "none";
  $("appShell").style.display = "none";
}

function disconnectWS() {
  if (!socket) return;
  try {
    socket.disconnect();
  } catch (_) {
    /* ignore */
  }
  socket = null;
}

function handleUnauthorized() {
  if (!authResolved) return;
  setAuthUser(null);
  stopBackgroundAccountVerify();
  disconnectWS();
  showAuthShell();
  setLoginError("登录状态已失效，请重新登录");
}

/* ─── Modal ─── */
let modalCallback = null;
function showModal(title, body, onOk) {
  $("modalTitle").innerHTML = esc(title);
  $("modalBody").innerHTML = body;
  $("modalOverlay").style.display = "flex";
  modalCallback = onOk || null;
}
$("modalOk").addEventListener("click", async () => {
  $("modalOverlay").style.display = "none";
  if (modalCallback) await modalCallback();
  modalCallback = null;
});
$("modalCancel").addEventListener("click", () => {
  $("modalOverlay").style.display = "none";
  modalCallback = null;
});
$("modalOverlay").addEventListener("click", (e) => {
  if (e.target === $("modalOverlay")) {
    $("modalOverlay").style.display = "none";
    modalCallback = null;
  }
});

/* ─── Toast ─── */
function toast(msg, type) {
  type = type || "info";
  const container = $("toastContainer");
  const el = document.createElement("div");
  el.className = "toast " + type;
  el.innerHTML = `<span class="toast-msg">${esc(msg)}</span><button class="toast-close" aria-label="关闭">&times;</button>`;
  el.querySelector(".toast-close").addEventListener("click", () => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 150);
  });
  container.appendChild(el);
  setTimeout(() => {
    if (el.parentNode) {
      el.classList.add("leaving");
      setTimeout(() => el.remove(), 150);
    }
  }, 4000);
}

/* ─── Auth ─── */
async function enterAuthedApp(user) {
  setLoginError("");
  setAuthUser(user);
  showAppShell();
  connectWS();
  await loadAccounts({ silent: true, skipVerify: true });
  startBackgroundAccountVerify({ immediate: true });
  if (currentView === "dashboard") await loadDashboard();
  else if (currentView === "results") await refreshResults();
  else if (currentView === "logs") await refreshLog();
}

async function checkAuth() {
  try {
    const res = await api("/api/auth/status");
    const data = await res.json();
    authResolved = true;
    if (data && data.authenticated) {
      await enterAuthedApp(data.user);
      return;
    }
    if (data && data.expired) {
      setLoginError("登录已过期，请重新登录");
    }
  } catch (e) {
    if (e.message !== "UNAUTHORIZED") {
      console.error("Auth status error:", e);
    }
    authResolved = true;
  }
  setAuthUser(null);
  showAuthShell();
}

function getOptionalUpdatePromptKey(info) {
  if (!info || !info.available || info.required) return "";
  return `${info.latest_version || ""}|${info.current_version || currentVersion || ""}`;
}

async function submitLogin(event) {
  event.preventDefault();
  const username = $("loginUsername").value.trim();
  const password = $("loginPassword").value;
  if (!username || !password) {
    setLoginError("请输入账号和密码");
    return;
  }

  const btn = $("loginSubmitBtn");
  btn.disabled = true;
  setLoginError("");
  btn.innerHTML = "登录中...";
  try {
    const res = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
      skipAuthRedirect: true,
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || "登录失败，请检查账号或密码");
    }
    const data = await res.json();
    authResolved = true;
    $("loginPassword").value = "";
    await enterAuthedApp(data.user);
    toast("登录成功", "success");
  } catch (e) {
    let message = e.message === "UNAUTHORIZED" ? "登录状态已失效，请重新登录" : e.message || "登录失败，请稍后重试";
    setLoginError(message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/></svg>登录进入`;
  }
}

async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST", body: JSON.stringify({}) });
  } catch (_) {
    /* ignore */
  }
  setAuthUser(null);
  stopBackgroundAccountVerify();
  disconnectWS();
  showAuthShell();
  setStatus("idle", "未登录");
  $("liveLog").textContent = "";
  setLoginError("");
  $("loginPassword").value = "";
  $("loginUsername").focus();
}

function applyVersionInfo(info) {
  currentVersion = (info && info.version) || currentVersion || "0.0.0";
  $("sidebarVersion").textContent = "v" + currentVersion;
}

function renderForceUpdate(info) {
  forceUpdateInfo = info || null;
  $("updateCurrentVersion").textContent = "当前版本: v" + ((info && info.current_version) || currentVersion || "-");
  $("updateLatestVersion").textContent = "最新版本: v" + ((info && info.latest_version) || "-");
  $("updateSummary").textContent = info && info.required ? "当前版本已停止支持，必须先更新后才能继续使用。" : "发现新版本，请先更新后再继续使用。";
  $("updateNotes").textContent = (info && info.notes) || "本次更新包含重要修复，请立即升级。";
  setUpdateError("");
  showUpdateShell();
}

function promptOptionalUpdate(info) {
  optionalUpdateInfo = info || null;
  if (!info || !info.available || info.required) return;
  const promptKey = getOptionalUpdatePromptKey(info);
  if (!promptKey || promptKey === lastOptionalUpdatePromptKey) return;
  lastOptionalUpdatePromptKey = promptKey;
  const notes = esc(info.notes || "发现新版本，建议及时更新。").replace(/\n/g, "<br>");
  const body = `检测到新版本 <strong>v${esc(info.latest_version || "-")}</strong>。<br>` + `当前版本: v${esc(info.current_version || currentVersion || "-")}<br><br>` + notes + "<br><br>是否现在更新？";
  showModal("发现新版本", body, async () => {
    await startDirectUpdate();
  });
}

async function checkForceUpdate() {
  try {
    const [versionRes, updateRes] = await Promise.all([
      fetch("/api/version")
        .then((r) => r.json())
        .catch(() => ({ version: "0.0.0" })),
      fetch("/api/update/check")
        .then((r) => r.json())
        .catch(() => ({ enabled: false })),
    ]);
    applyVersionInfo(versionRes || {});
    if (updateRes && updateRes.enabled && updateRes.available) {
      if (updateRes.required) {
        renderForceUpdate(updateRes);
        return { blocked: true, info: updateRes };
      }
      return { blocked: false, info: updateRes };
    }
  } catch (e) {
    console.error("Update check error:", e);
  }
  return { blocked: false, info: null };
}

async function startDirectUpdate() {
  const btn = $("updateNowBtn");
  btn.disabled = true;
  btn.textContent = "更新中…";
  setUpdateError("");
  try {
    const res = await fetch("/api/update/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.error || "更新失败，请稍后重试");
    }
    $("updateNotes").textContent = "更新包已下载并尝试打开，请按系统提示完成安装。安装完成后重新打开软件即可。";
  } catch (e) {
    setUpdateError(e.message || "更新失败，请稍后重试");
  } finally {
    btn.disabled = false;
    btn.textContent = "立即更新";
  }
}

async function runWindowOpenChecks(options = {}) {
  const { force = false } = options;
  const now = Date.now();
  if (!force && windowCheckInFlight) return windowCheckInFlight;
  if (!force && now - lastWindowCheckAt < WINDOW_RECHECK_THROTTLE_MS) return windowCheckInFlight || null;

  lastWindowCheckAt = now;
  windowCheckInFlight = (async () => {
    const updateState = await checkForceUpdate();
    if (updateState && updateState.blocked) return updateState;
    await checkAuth();
    if (updateState && updateState.info && updateState.info.available) {
      setTimeout(() => promptOptionalUpdate(updateState.info), 300);
    }
    return updateState;
  })();

  try {
    return await windowCheckInFlight;
  } finally {
    windowCheckInFlight = null;
  }
}

$("loginForm").addEventListener("submit", submitLogin);
$("logoutBtn").addEventListener("click", logout);
$("updateNowBtn").addEventListener("click", startDirectUpdate);

/* ─── Socket.IO ─── */
let socket;

function connectWS() {
  if (!authUser || socket) return;
  socket = io({ transports: ["websocket"] });
  socket.on("message", (d) => {
    if (d.type === "log") appendLog(d);
    if (d.type === "progress") onProgress(d);
    if (d.type === "upload-end") onUploadEnd(d);
    if (d.type === "login-expired") onLoginExpired(d);
    if (d.type === "account-updated") {
      loginLock = false;
      resetAddForm();
      const targetAccount = d.account || pendingLoginAccountName || getSelectedAccountName();
      loadAccounts({ verifyAll: false, silent: true }).then(async () => {
        if (targetAccount) {
          await verifyAccount(targetAccount, { silent: true, buttonless: true, forceToastOnInvalid: true });
        } else {
          updateAccountStatus();
        }
      });
      pendingLoginAccountName = "";
      toast("登录成功", "success");
    }
    if (d.type === "login-result") {
      loginLock = false;
      resetAddForm();
      if (d.result !== "success") pendingLoginAccountName = "";
      updateAccountPanel();
      if (d.result === "expired") toast("二维码已过期，请重试", "warn");
      else if (d.result === "timeout") toast("登录超时，请重试", "warn");
      else if (d.result === "error") toast("登录出错: " + (d.error || ""), "error");
    }
  });
  socket.on("connect", () => {
    if (!uploadRunning) updateAccountStatus();
  });
  socket.on("disconnect", () => {
    socket = null;
    if (!authUser) return;
    setStatus("error", "连接断开");
    setTimeout(connectWS, 2000);
  });
  socket.on("connect_error", () => {
    if (!authUser) return;
    if (!uploadRunning) setStatus("error", "连接断开");
  });
}

/* ═══════════════════════════════════════════════
   Navigation
   ═══════════════════════════════════════════════ */
document.querySelectorAll(".nav-item").forEach((item) => {
  item.addEventListener("click", async () => {
    if (loginLock) return toast("请先完成扫码登录", "warn");
    const view = item.dataset.view;
    if (view === currentView) return;
    await switchView(view);
  });
});

async function switchView(view) {
  if (loginLock) return;
  activateView(view);

  if (view === "dashboard") loadDashboard();
  if (view === "results") refreshResults();
  if (view === "logs") refreshLog();
}

/* ═══════════════════════════════════════════════
   Status indicator
   ═══════════════════════════════════════════════ */
function setStatus(type, text) {
  const dot = $("statusDot");
  const label = $("statusLabel");
  if (!dot || !label) return;
  dot.className = "status-dot " + type;
  label.textContent = text;
}

function pickPrimaryAccount(list = []) {
  return list.find((a) => a.name === PRIMARY_ACCOUNT_NAME) || list[0] || null;
}

function getPrimaryAccount() {
  return accounts[0] || null;
}

function getStatusText(status) {
  const s = String(status || "").toLowerCase();
  const map = {
    ready: "已登录",
    "needs-login": "未登录",
    idle: "未登录",
    running: "处理中",
    error: "异常",
    published: "已发布",
    failed: "失败",
    uncertain: "待确认",
    skipped: "已跳过",
    success: "成功",
  };
  return map[s] || status || "";
}

function activateView(view) {
  currentView = view;
  document.querySelector(".main").scrollTop = 0;
  document.querySelectorAll(".nav-item").forEach((n) => n.classList.remove("active"));
  document.querySelector(`.nav-item[data-view="${view}"]`).classList.add("active");
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  $(`view-${view}`).classList.add("active");
}

function getSelectedAccountName() {
  return (getPrimaryAccount() && getPrimaryAccount().name) || PRIMARY_ACCOUNT_NAME;
}

function getAccountByName(name) {
  return accounts.find((a) => a.name === name) || null;
}

function updateAccountPanel() {
  const acct = getPrimaryAccount();
  const badge = $("channelStateBadge");
  const loginBtn = $("channelLoginBtn");
  const verifyBtn = $("channelVerifyBtn");
  const closeBtn = $("channelCloseBtn");
  if (!badge || !loginBtn || !verifyBtn || !closeBtn) return;

  let badgeText = "未登录";
  let badgeClass = "idle";

  if (loginLock) {
    badgeText = "扫码中";
    badgeClass = "running";
  } else if (acct && acct.status === "ready") {
    badgeText = "已登录";
    badgeClass = "success";
  }

  badge.className = "sidebar-account-badge " + badgeClass;
  badge.textContent = badgeText;
  loginBtn.disabled = loginLock;
  verifyBtn.disabled = loginLock;
  closeBtn.disabled = false;
  loginBtn.textContent = loginLock ? "等待扫码…" : acct && acct.status === "ready" ? "重新登录" : "扫码登录";
}

function applyVerifyResult(name, vData) {
  const acct = accounts.find((a) => a.name === name || a.name === vData.name) || getPrimaryAccount();
  if (!acct) return null;
  acct.lastLogin = new Date().toISOString();
  if (!vData.error) {
    if (vData.valid === true) acct.status = "ready";
    else if (vData.valid === false) acct.status = "needs-login";
  }
  updateAccountPanel();
  updateAccountStatus();
  return acct;
}

async function runBackgroundAccountVerify() {
  if (!authUser || loginLock || uploadRunning) return false;
  const name = getSelectedAccountName();
  if (!name) return false;
  return verifyAccount(name, { silent: true, buttonless: true });
}

function stopBackgroundAccountVerify() {
  if (!backgroundVerifyTimer) return;
  clearInterval(backgroundVerifyTimer);
  backgroundVerifyTimer = null;
}

function startBackgroundAccountVerify(options = {}) {
  const { immediate = false } = options;
  stopBackgroundAccountVerify();
  if (!authUser) return;
  if (immediate) {
    setTimeout(() => {
      runBackgroundAccountVerify().catch(() => {});
    }, 0);
  }
  backgroundVerifyTimer = setInterval(() => {
    runBackgroundAccountVerify().catch(() => {});
  }, BACKGROUND_VERIFY_INTERVAL_MS);
}

/* ═══════════════════════════════════════════════
   DASHBOARD
   ═══════════════════════════════════════════════ */
async function loadDashboard() {
  try {
    const [resR, resA] = await Promise.all([api("/api/results"), api("/api/accounts")]);
    const results = await resR.json();
    const primary = pickPrimaryAccount(await resA.json());
    accounts = primary ? [primary] : [];

    const total = results.length;
    const published = results.filter((r) => (r.status || "").toLowerCase() === "published").length;
    const failed = results.filter((r) => (r.status || "").toLowerCase() === "failed").length;
    const rate = total > 0 ? Math.round((published / total) * 100) : 0;
    const active = primary && primary.status === "ready" ? 1 : 0;

    $("statTotal").textContent = total;
    $("statRate").textContent = rate + "%";
    $("statRate").className = "stat-value" + (rate >= 80 ? " accent" : rate >= 50 ? "" : "");
    $("statFailed").textContent = failed;
    $("statAccounts").textContent = active;

    // Recent activity table
    const recent = [...results].reverse().slice(0, 10);
    const tb = $("dashTable");
    if (recent.length === 0) {
      tb.innerHTML = '<div class="empty-state">暂无发布记录</div>';
    } else {
      tb.innerHTML = `<table><thead><tr><th>视频</th><th>标题</th><th>状态</th><th>错误</th></tr></thead><tbody>${recent
        .map((r) => {
          const sc = (r.status || "").toLowerCase();
          return `<tr>
            <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(r.video_path || "")}">${esc((r.video_path || "").split("/").pop().split("\\").pop())}</td>
            <td>${esc(r.title || "")}</td>
            <td><span class="status-cell ${sc}"><span class="dot"></span>${esc(getStatusText(r.status))}</span></td>
            <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text-tertiary)" title="${esc(r.error || "")}">${esc(r.error || "")}</td>
          </tr>`;
        })
        .join("")}</tbody></table>`;
    }
  } catch (e) {
    console.error("Dashboard load error:", e);
  }
}

$("dashRefreshBtn").addEventListener("click", loadDashboard);

$("clearEntriesBtn").addEventListener("click", () => {
  if (entries.length === 0) return;
  showModal("清空队列", `确定要清空全部 ${entries.length} 个待上传条目吗？`, () => {
    entries = [];
    renderEntries();
    $("timeline").innerHTML = "";
    $("timeline").classList.remove("visible");
    $("progressWrap").style.display = "none";
    toast("队列已清空", "info");
  });
});

/* ═══════════════════════════════════════════════
   ENTRIES
   ═══════════════════════════════════════════════ */
function addEntry(videoPath, videoName, coverPath, coverName, title, drama, time, desc) {
  if (!videoPath) return toast("请选择视频文件", "error");
  entries.push({
    id: ++entryIdCounter,
    video_path: videoPath,
    videoName,
    cover_path: coverPath || "",
    coverName: coverName || "",
    title: title.trim(),
    short_drama_name: drama || "",
    publish_time: time || "",
    description: desc || "",
    _uploadStatus: "pending",
  });
  renderEntries();
}

function removeEntry(id) {
  entries = entries.filter((e) => e.id !== id);
  renderEntries();
}

function renderEntries() {
  const el = $("entryList");
  $("entryCount").textContent = entries.length;
  $("startBtn").disabled = entries.length === 0 || uploadRunning;
  $("clearEntriesBtn").style.display = entries.length > 0 && !uploadRunning ? "" : "none";

  if (entries.length === 0) {
    el.innerHTML = '<div class="empty-state">暂无视频待上传</div>';
    return;
  }
  el.innerHTML = entries
    .map((e, i) => {
      const statusMap = {
        pending: ["待上传", "pending"],
        done: ["已发布", "done"],
        fail: ["失败", "fail"],
      };
      const [sLabel, sClass] = statusMap[e._uploadStatus] || ["待上传", "pending"];
      const valError = e._validationError || "";
      let displayDesc = e.description || "";
      if (displayDesc.length > 50) displayDesc = displayDesc.slice(0, 50) + "…";
      if (!displayDesc) displayDesc = e.title || "(无描述)";
      return `<div class="entry-item${valError ? " invalid" : ""}" draggable="true" data-id="${e.id}">
      <span class="entry-num">${i + 1}</span>
      <div class="entry-info">
        <div class="entry-title">${esc(displayDesc)}</div>
        <div class="entry-meta">
          <span>${esc(e.videoName || e.video_path.split(/[\\/]/).pop())}</span>
          ${e.title ? `<span>标题: ${esc(e.title)}</span>` : ""}
          ${e.cover_path ? `<span>封面: ${esc(e.coverName || e.cover_path.split(/[\\/]/).pop())}</span>` : ""}
          ${e.short_drama_name ? `<span>剧集: ${esc(e.short_drama_name)}</span>` : ""}
          ${e.publish_time ? `<span>定时: ${esc(e.publish_time.replace("T", " "))}</span>` : ""}
          ${valError ? `<span class="val-error">${esc(valError)}</span>` : ""}
        </div>
      </div>
      <span class="entry-status ${sClass}"><span class="dot"></span>${sLabel}</span>
      <button class="btn-icon" data-remove="${e.id}" title="删除">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>`;
    })
    .join("");

  // Attach remove handlers
  el.querySelectorAll("[data-remove]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      removeEntry(parseInt(btn.dataset.remove));
    });
  });

  // Drag-to-reorder
  setupDragReorder();
}

/* ─── Drag to reorder ─── */
function setupDragReorder() {
  const items = document.querySelectorAll("#entryList .entry-item");
  let dragSrc = null;

  items.forEach((item) => {
    item.addEventListener("dragstart", function (e) {
      dragSrc = this;
      this.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", "");
    });

    item.addEventListener("dragend", function () {
      this.classList.remove("dragging");
      document.querySelectorAll("#entryList .entry-item").forEach((el) => el.classList.remove("drag-over"));
      dragSrc = null;
    });

    item.addEventListener("dragover", function (e) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      if (this !== dragSrc) this.classList.add("drag-over");
    });

    item.addEventListener("dragleave", function () {
      this.classList.remove("drag-over");
    });

    item.addEventListener("drop", function (e) {
      e.preventDefault();
      this.classList.remove("drag-over");
      if (this === dragSrc) return;

      const srcId = parseInt(dragSrc.dataset.id);
      const dstId = parseInt(this.dataset.id);
      const srcIdx = entries.findIndex((e) => e.id === srcId);
      const dstIdx = entries.findIndex((e) => e.id === dstId);
      if (srcIdx < 0 || dstIdx < 0) return;

      const [moved] = entries.splice(srcIdx, 1);
      entries.splice(dstIdx, 0, moved);
      renderEntries();
    });
  });
}

/* ─── Add entry button ─── */
$("addEntryBtn").addEventListener("click", async () => {
  // 批量模式：一次性添加多个视频
  if (batchVideoFiles.length > 0) {
    const title = $("formTitle").value;
    const drama = $("formDrama").value;
    const baseTime = $("formTime").value;
    const interval = parseInt($("formInterval").value) || 0;
    const desc = $("formDesc").value;
    const coverPath = $("coverPreview").dataset.path || "";
    const coverName = $("coverPreview").dataset.name || "";

    batchVideoFiles.forEach(({ path, name }, i) => {
      let t = baseTime;
      if (baseTime && interval > 0) {
        const d = new Date(baseTime);
        const now = new Date();
        if (d <= now) {
          d.setTime(now.getTime());
          d.setMinutes(d.getMinutes() + interval + i * interval);
        } else {
          d.setMinutes(d.getMinutes() + i * interval);
        }
        t = toLocalDatetime(d);
      }
      entries.push({
        id: ++entryIdCounter,
        video_path: path,
        videoName: name,
        cover_path: coverPath,
        coverName,
        title: title.trim(),
        short_drama_name: drama || "",
        publish_time: t,
        description: desc || "",
        _uploadStatus: "pending",
      });
    });
    renderEntries();
    toast(`已添加 ${batchVideoFiles.length} 个视频到队列`, "success");
    batchVideoFiles.forEach((f) => {
      if (f._blobUrl) URL.revokeObjectURL(f._blobUrl);
    });
    batchVideoFiles = [];
    $("batchVideoStrip").style.display = "none";
    $("addEntryBtn").innerHTML = addEntryBtnDefault;
    clearDropZone("video");
    clearDropZone("cover");
    $("formTitle").value = "";
    $("formDrama").value = "";
    $("formDesc").value = "";
    return;
  }

  // 单视频模式
  addEntry($("videoPreview").dataset.path, $("videoPreview").dataset.name, $("coverPreview").dataset.path, $("coverPreview").dataset.name, $("formTitle").value, $("formDrama").value, $("formTime").value, $("formDesc").value);
  const time = $("formTime").value;
  const interval = parseInt($("formInterval").value) || 0;
  if (time && interval > 0) {
    const d = new Date(time);
    const now = new Date();
    if (d <= now) {
      d.setTime(now.getTime());
      d.setMinutes(d.getMinutes() + 1);
    } else {
      d.setMinutes(d.getMinutes() + interval);
    }
    $("formTime").value = toLocalDatetime(d);
  }
  clearDropZone("video");
  clearDropZone("cover");
  $("formTitle").value = "";
  $("formDrama").value = "";
  $("formDesc").value = "";
});

/* ─── Title hint ─── */
$("formTitle").addEventListener("input", function () {
  const hint = $("titleHint");
  const v = this.value;
  if (!v) {
    hint.textContent = "出现在搜索、话题、发现页等场景";
    hint.className = "field-hint";
    return;
  }
  if (v.length < 6) {
    hint.textContent = "还需 " + (6 - v.length) + " 个字符达建议长度";
    hint.className = "field-hint";
    return;
  }
  const allowed = new Set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 《》（）"":+?%℃ ');
  for (const ch of v) {
    if (!allowed.has(ch) && !(ch >= "一" && ch <= "鿿")) {
      hint.textContent = '不支持字符 "' + ch + '"';
      hint.className = "field-hint err";
      return;
    }
  }
  hint.textContent = "OK";
  hint.className = "field-hint ok";
});

/* ═══════════════════════════════════════════════
   DRAG & DROP (file upload)
   ═══════════════════════════════════════════════ */
function setupDropZone(type) {
  const zone = $(`${type}Drop`);
  const input = $(`${type}Input`);

  input.addEventListener("change", () => {
    const files = input.files;
    if (!files || files.length === 0) return;
    if (type === "video" && files.length > 1) {
      handleBatchVideos(files);
    } else {
      handleFile(type, files[0]);
    }
  });
  zone.addEventListener("dragover", (e) => {
    e.preventDefault();
    zone.classList.add("drag-over");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("drag-over"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("drag-over");
    const files = e.dataTransfer.files;
    if (!files || files.length === 0) return;
    // Validate file type
    if (type === "video") {
      const invalid = [...files].filter((f) => !f.type.startsWith("video/"));
      if (invalid.length > 0) {
        toast("不支持的文件类型: " + invalid.map((f) => f.name).join(", ") + "，请拖入 MP4 视频文件", "error");
        return;
      }
    } else if (type === "cover") {
      if (!files[0].type.startsWith("image/")) {
        toast("请拖入 PNG / JPG 图片作为封面", "error");
        return;
      }
    }
    if (type === "video" && files.length > 1) {
      handleBatchVideos(files);
    } else {
      handleFile(type, files[0]);
    }
  });
}

async function handleFile(type, file) {
  // 单文件上传时清除批量暂存
  if (type === "video") {
    batchVideoFiles.forEach((f) => {
      if (f._blobUrl) URL.revokeObjectURL(f._blobUrl);
    });
    batchVideoFiles = [];
    $("batchVideoStrip").style.display = "none";
    $("addEntryBtn").innerHTML = addEntryBtnDefault;
  }
  const preview = $(`${type}Preview`);
  const zone = $(`${type}Drop`);
  const el = preview.querySelector(type === "video" ? "video" : "img");
  const nameEl = preview.querySelector(".drop-filename");

  // Revoke previous blob URL
  if (preview.dataset.blobUrl) URL.revokeObjectURL(preview.dataset.blobUrl);
  const url = URL.createObjectURL(file);
  preview.dataset.blobUrl = url;
  el.src = url;
  nameEl.textContent = file.name;
  preview.style.display = "flex";
  zone.querySelector(".drop-icon").style.display = "none";
  zone.querySelector(".drop-text").style.display = "none";
  zone.querySelector(".drop-hint").textContent = file.name;

  const formData = new FormData();
  formData.append("file", file);
  try {
    const res = await api("/api/upload/file", { method: "POST", body: formData });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || "上传失败");
    }
    const data = await res.json();
    preview.dataset.path = data.path;
    preview.dataset.name = data.name;
  } catch (err) {
    toast("文件上传失败: " + (err.message || "网络错误"), "error");
    return;
  }
}

function uploadVideo(file) {
  return new Promise(async (resolve) => {
    const formData = new FormData();
    formData.append("file", file);
    try {
      const res = await api("/api/upload/file", { method: "POST", body: formData });
      const data = await res.json();
      resolve({ path: data.path, name: data.name });
    } catch {
      resolve({ path: file.name, name: file.name });
    }
  });
}

// 批量拖拽暂存区 — 上传后不立即入队，等用户填完表单
let batchVideoFiles = [];

async function handleBatchVideos(files) {
  const list = [...files];
  const hintEl = $("videoDrop").querySelector(".drop-hint");
  hintEl.textContent = "上传中 0/" + list.length + "…";

  const results = [];
  for (let i = 0; i < list.length; i++) {
    results.push(await uploadVideo(list[i]));
    hintEl.textContent = "上传中 " + (i + 1) + "/" + list.length + "…";
  }

  // 暂存上传结果，填充横排预览条
  batchVideoFiles = results;
  const strip = $("batchVideoStrip");
  strip.innerHTML = list
    .map((file, i) => {
      const blobUrl = URL.createObjectURL(file);
      results[i]._blobUrl = blobUrl; // 暂存以便后续清理
      return `<div class="batch-video-card" data-index="${i}">
      <video src="${blobUrl}" muted preload="metadata" title="${esc(results[i].name)}"></video>
      <div class="batch-video-name">${esc(results[i].name)}</div>
    </div>`;
    })
    .join("");
  strip.style.display = "flex";
  $("videoPreview").style.display = "none";
  // 点击卡片预览
  strip.querySelectorAll(".batch-video-card").forEach((card) => {
    card.addEventListener("click", () => {
      const vid = card.querySelector("video");
      if (vid.paused) {
        vid.play();
      } else {
        vid.pause();
      }
    });
  });
  // 更新按钮文案
  $("addEntryBtn").innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>添加 ${results.length} 个到队列`;

  hintEl.textContent = results.length + " 个视频已就绪";
  toast(`${results.length} 个视频已就绪，请填写信息后点击"添加到队列"`, "info");
}

function clearDropZone(type) {
  const zone = $(`${type}Drop`);
  const preview = $(`${type}Preview`);
  const input = $(`${type}Input`);
  const defaultHints = { video: "MP4 · 可批量选择 · 最大 20GB", cover: "PNG / JPG" };
  if (preview.dataset.blobUrl) {
    URL.revokeObjectURL(preview.dataset.blobUrl);
    delete preview.dataset.blobUrl;
  }
  preview.style.display = "none";
  preview.dataset.path = "";
  preview.dataset.name = "";
  input.value = "";
  zone.querySelector(".drop-icon").style.display = "";
  zone.querySelector(".drop-text").style.display = "";
  zone.querySelector(".drop-hint").textContent = defaultHints[type];
}

setupDropZone("video");
setupDropZone("cover");

/* ═══════════════════════════════════════════════
   UPLOAD
   ═══════════════════════════════════════════════ */
$("startBtn").addEventListener("click", startUpload);
$("stopBtn").addEventListener("click", stopUpload);
$("retryBtn").addEventListener("click", retryFailed);
$("queueClearRefreshBtn").addEventListener("click", clearOrRefreshQueue);

function generateCSV() {
  const header = "video_path,title,description,short_drama_name,publish_time,cover_path";
  const rows = entries.map((e) => {
    const cols = [e.video_path, e.title || "", e.description || "", e.short_drama_name || "", e.publish_time || "", e.cover_path || ""];
    return cols
      .map((v) => {
        const s = String(v || "");
        return s.includes(",") || s.includes('"') ? '"' + s.replace(/"/g, '""') + '"' : s;
      })
      .join(",");
  });
  return header + "\n" + rows.join("\n");
}

async function startUpload() {
  if (loginLock) return toast("请先完成扫码登录", "warn");
  const account = getSelectedAccountName();
  if (entries.length === 0) return toast("请先添加视频", "error");

  const csv = generateCSV();
  setStatus("running", "上传中…");
  $("startBtn").disabled = true;
  $("stopBtn").disabled = false;
  $("liveLog").textContent = "";
  uploadRunning = true;
  uploadOrderIds = entries.map((e) => e.id);
  entries.forEach((e) => {
    e._uploadStatus = "pending";
    delete e._validationError;
  });
  renderEntries();

  // Show timeline
  const tl = $("timeline");
  tl.classList.add("visible");
  tl.innerHTML = entries
    .map(
      (e, i) =>
        `<div class="timeline-node pending" data-tl="${e.id}">
      <div class="timeline-node-title">${i + 1}. ${esc(e.title || e.videoName || "未命名")}</div>
      <div class="timeline-node-meta">等待中</div>
    </div>`
    )
    .join("");

  $("progressWrap").style.display = "block";
  $("retryBtn").style.display = "none";
  updateProgress(0, entries.length);

  const rawIv = parseInt($("formInterval").value, 10);
  const scheduleIntervalMin = Number.isFinite(rawIv) && rawIv > 0 ? Math.min(1440, rawIv) : 1;
  try {
    const res = await api("/api/upload/start", {
      method: "POST",
      body: JSON.stringify({ account, csv, schedule_interval_min: scheduleIntervalMin }),
    });
    if (!res.ok) {
      const d = await res.json();
      toast(d.error || "启动失败", "error");
      uploadRunning = false;
      uploadOrderIds = [];
      $("progressWrap").style.display = "none";
      $("timeline").classList.remove("visible");
      $("timeline").innerHTML = "";
      resetUI();
      renderEntries();
    }
  } catch (e) {
    toast("错误: " + e.message, "error");
    uploadRunning = false;
    uploadOrderIds = [];
    $("progressWrap").style.display = "none";
    $("timeline").classList.remove("visible");
    $("timeline").innerHTML = "";
    resetUI();
    renderEntries();
  }
}

let stopForceTimer = null;
let stopFinalTimer = null;

function clearStopGuards() {
  if (stopForceTimer) {
    clearTimeout(stopForceTimer);
    stopForceTimer = null;
  }
  if (stopFinalTimer) {
    clearTimeout(stopFinalTimer);
    stopFinalTimer = null;
  }
}

function stopUpload() {
  showModal("停止上传", "确定要停止当前上传吗？<br>已完成的视频不会受影响，剩余视频将标记为失败。<br><br>" + "若 8 秒内后台仍未退出，将自动强制关闭浏览器，让你能立刻重新开始。", () => {
    api("/api/upload/stop", { method: "POST", body: JSON.stringify({}) });
    $("stopBtn").disabled = true;
    toast("正在停止…", "info");

    clearStopGuards();
    stopForceTimer = setTimeout(async () => {
      stopForceTimer = null;
      if (!uploadRunning) return;
      toast("后台仍未退出，正在强制关闭浏览器…", "warn");
      try {
        await api("/api/upload/stop", {
          method: "POST",
          body: JSON.stringify({ force: true }),
        });
      } catch (e) {
        /* 忽略，下面还会本地复位 */
      }

      stopFinalTimer = setTimeout(async () => {
        stopFinalTimer = null;
        if (!uploadRunning) return;
        // 与后端同步一次最终状态；若后端也认为没在跑，就强制本地复位
        try {
          const st = await api("/api/upload/state").then((r) => (r.ok ? r.json() : null));
          if (st && st.running === false) {
            uploadRunning = false;
            resetUI();
            renderEntries();
            toast("已强制复位，可重新开始上传", "info");
            return;
          }
        } catch (e) {
          /* fall through */
        }
        uploadRunning = false;
        resetUI();
        renderEntries();
        toast("已本地强制复位（后台稍后才会真正退出）", "warn");
      }, 5000);
    }, 8000);
  });
}

function updateProgress(current, total) {
  const pct = total > 0 ? Math.round((current / total) * 100) : 0;
  $("progressFill").style.width = pct + "%";
  $("progressText").textContent = current + " / " + total + " (" + pct + "%)";
}

function onProgress(data) {
  const k = data.current - 1;
  if (k < 0 || k >= uploadOrderIds.length) {
    updateProgress(data.current, data.total);
    return;
  }
  const rid = uploadOrderIds[k];
  const tlNode = document.querySelector(`.timeline-node[data-tl="${rid}"]`);

  // 已成功发表（或跳过）：从队列移除，时间线节点一并移除
  if (data.status === "published" || data.status === "skipped") {
    const removeIdx = entries.findIndex((e) => e.id === rid);
    if (removeIdx >= 0) entries.splice(removeIdx, 1);
    if (tlNode) tlNode.remove();
    renderEntries();
    updateProgress(data.current, data.total);
    return;
  }

  const ent = entries.find((e) => e.id === rid);
  if (ent) {
    if (data.status === "failed") ent._uploadStatus = "fail";
    else if (data.status === "uncertain") ent._uploadStatus = "fail";
    else ent._uploadStatus = "pending";
  }
  if (tlNode) {
    tlNode.classList.remove("pending", "active", "done", "fail");
    const statusClass = data.status === "failed" || data.status === "uncertain" ? "fail" : "active";
    tlNode.classList.add(statusClass);
    const meta = tlNode.querySelector(".timeline-node-meta");
    if (meta) {
      meta.textContent = data.status === "failed" ? "失败" : data.status === "uncertain" ? "未确认" : "处理中…";
    }
  }
  renderEntries();
  updateProgress(data.current, data.total);
}

// Clean server-renamed filename: "1680000000_a1b2c3_video.mp4" → "video.mp4"
function cleanUploadName(filePath) {
  const name = (filePath || "").split(/[\\/]/).pop();
  return name.replace(/^\d+_[a-z0-9]{6}_/, "");
}

async function retryFailed() {
  const failed = entries.filter((e) => e._uploadStatus === "fail");
  if (failed.length === 0) return toast("没有失败的条目", "info");
  entries = failed;
  entries.forEach((e) => {
    e._uploadStatus = "pending";
    delete e._validationError;
  });
  $("entryCount").textContent = entries.length;
  await startUpload();
}

function onLoginExpired(data) {
  toast(`账号登录已过期！视频「${data.title || "未知"}」上传中断，请重新扫码登录`, "error");
  uploadRunning = false;
  uploadOrderIds = [];
  $("timeline").classList.remove("visible");
  $("timeline").innerHTML = "";
  resetUI();
  loadAccounts(); // refresh account list and select
  setStatus("error", "登录过期");
  updateAccountPanel();
}

function onUploadEnd(data) {
  clearStopGuards();
  uploadRunning = false;
  uploadOrderIds = [];
  resetUI();
  $("progressWrap").style.display = "none";
  const tl = $("timeline");
  tl.classList.remove("visible");
  tl.innerHTML = "";
  if (data.loginExpired) {
    loadAccounts();
    setStatus("error", "登录过期");
  }
  if (data.success) {
    const pct = Math.round((data.results / data.total) * 100);
    toast("完成: " + data.results + "/" + data.total + " (" + pct + "%)", "success");
    // 已发表项已在 onProgress 中移除；未跑到的 pending 记为失败并仅保留失败行便于重试
    entries.forEach((e) => {
      if (e._uploadStatus === "pending") e._uploadStatus = "fail";
    });
    entries = entries.filter((e) => e._uploadStatus === "fail");
    renderEntries();
    refreshResults();
  } else {
    toast("上传失败: " + (data.error || "未知错误"), "error");
    renderEntries();
  }
  const hasFailed = entries.some((e) => e._uploadStatus === "fail");
  $("retryBtn").style.display = hasFailed ? "" : "none";
}

/** 刷新发布记录；未在上传时可清除队列中的失败项 */
async function clearOrRefreshQueue() {
  let cleared = 0;
  try {
    await refreshResults();
  } catch {
    /* ignore */
  }
  if (!uploadRunning) {
    const before = entries.length;
    entries = entries.filter((e) => e._uploadStatus !== "fail");
    cleared = before - entries.length;
  }
  renderEntries();
  if (cleared > 0) toast("已刷新，并清除 " + cleared + " 条失败项", "success");
  else toast("已刷新", "success");
}

function resetUI() {
  $("startBtn").disabled = false;
  $("stopBtn").disabled = true;
  $("progressWrap").style.display = "none";
  if (!uploadRunning) updateAccountStatus();
}

/* ═══════════════════════════════════════════════
   LOGS
   ═══════════════════════════════════════════════ */
function appendLog(data) {
  // Live log (upload view)
  const liveLog = $("liveLog");
  if (liveLog) {
    const level = data.level === "ERROR" ? "err" : data.level === "WARN" ? "warn" : "info";
    liveLog.innerHTML += '<span class="' + level + '">[' + (data.ts ? new Date(data.ts).toLocaleTimeString() : "") + "] " + esc(data.msg) + "</span>\n";
    liveLog.scrollTop = liveLog.scrollHeight;
  }

  // Full log view — if visible and auto-refresh on, refresh
  if (currentView === "logs" && $("logAutoRefresh").checked) {
    refreshLog();
  }
}

$("clearLogBtn").addEventListener("click", () => {
  $("liveLog").textContent = "";
});

/* ═══════════════════════════════════════════════
   ACCOUNTS
   ═══════════════════════════════════════════════ */
async function loadAccounts(options = {}) {
  const { verifyAll = false, silent = false, skipVerify = true } = options;
  const res = await api("/api/accounts");
  const primary = pickPrimaryAccount(await res.json());
  accounts = primary ? [primary] : [];
  updateAccountPanel();
  updateAccountStatus();

  if (skipVerify) return;

  const targets = primary ? (verifyAll ? [primary] : !primary.lastLogin || Number.isNaN(new Date(primary.lastLogin).getTime()) || Date.now() - new Date(primary.lastLogin).getTime() >= 3600000 ? [primary] : []) : [];
  if (targets.length > 0) {
    const results = await Promise.allSettled(targets.map((a) => verifyAccount(a.name, { silent, buttonless: true })));
    if (!silent && results.some((r) => r.status === "fulfilled")) {
      toast("已完成视频号账号登录状态校验", "success");
    }
  }
  updateAccountPanel();
  updateAccountStatus();
}

function resetAddForm() {
  updateAccountPanel();
}

$("channelLoginBtn").addEventListener("click", () => {
  if (loginLock) return toast("请先完成扫码登录", "warn");
  loginAccount();
});

$("channelVerifyBtn").addEventListener("click", () => {
  if (loginLock) return toast("请先完成扫码登录", "warn");
  verifyAccount(getSelectedAccountName());
});

$("channelCloseBtn").addEventListener("click", async () => {
  try {
    const res = await api(`/api/accounts/${getSelectedAccountName()}/close`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      return toast(data.error || "关闭浏览器失败", "error");
    }
    loginLock = false;
    pendingLoginAccountName = "";
    updateAccountPanel();
    toast("已关闭当前登录浏览器窗口", "success");
  } catch (e) {
    toast("关闭浏览器失败: " + e.message, "error");
  }
});

function updateAccountStatus() {
  const acct = getPrimaryAccount();
  if (!acct) {
    setStatus("idle", "未配置");
    return;
  }
  if (acct.status === "ready") setStatus("success", "已登录");
  else if (acct.status === "needs-login") setStatus("idle", "未登录");
  else setStatus("idle", getStatusText(acct.status));
}

async function verifyAccount(name, options = {}) {
  const { silent = false, buttonless = false, forceToastOnInvalid = false } = options;
  if (accountVerifyPromises.has(name)) return accountVerifyPromises.get(name);
  const card = buttonless ? null : document.querySelector(`[data-verify="${esc(name)}"]`);
  if (card) {
    card.disabled = true;
    card.textContent = "验证中…";
  }

  const task = (async () => {
    try {
      const vRes = await api(`/api/accounts/${name}/verify`, { method: "POST" });
      const vData = await vRes.json().catch(() => ({}));
      if (!vRes.ok) {
        if (!silent) toast(vData.error || "验证失败", "error");
        return false;
      }
      if (vData.notice) {
        if (!silent) toast(vData.notice, "info");
        return false;
      }
      applyVerifyResult(name, vData);
      if (vData.error) {
        if (!silent || forceToastOnInvalid) toast(vData.hint || vData.error, "error");
        return false;
      }
      if (!silent) {
        toast(vData.valid ? "登录状态有效" : "登录已过期，请重新扫码", vData.valid ? "success" : "error");
      } else if (!vData.valid && forceToastOnInvalid) {
        toast("当前视频号账号未登录，请先扫码登录", "error");
      }
      return Boolean(vData.valid);
    } catch {
      if (!silent) toast("验证失败", "error");
      return false;
    } finally {
      if (card) {
        card.disabled = false;
        card.textContent = "验证";
      }
      accountVerifyPromises.delete(name);
    }
  })();

  accountVerifyPromises.set(name, task);
  return task;
}

async function loginAccount(name = getSelectedAccountName()) {
  if (!name) {
    toast("未找到可用的视频号账号", "error");
    return;
  }
  pendingLoginAccountName = name;
  loginLock = true;
  updateAccountPanel();
  try {
    const res = await api("/api/accounts/" + name + "/login", { method: "POST" });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      pendingLoginAccountName = "";
      loginLock = false;
      updateAccountPanel();
      toast(data.error || "启动登录失败", "error");
      return;
    }
    toast("登录窗口已打开，请在浏览器窗口中扫码", "info");
  } catch (e) {
    pendingLoginAccountName = "";
    loginLock = false;
    updateAccountPanel();
    toast("启动登录失败: " + e.message, "error");
  }
}

/* ═══════════════════════════════════════════════
   RESULTS
   ═══════════════════════════════════════════════ */
let currentFilter = "all";

document.querySelectorAll("#resultFilters .filter-tab").forEach((tab) => {
  tab.addEventListener("click", function () {
    document.querySelectorAll("#resultFilters .filter-tab").forEach((t) => t.classList.remove("active"));
    this.classList.add("active");
    currentFilter = this.dataset.filter;
    renderResultsTable();
  });
});

async function refreshResults() {
  const res = await api("/api/results");
  allResults = await res.json();
  renderResultsTable();
}

function renderResultsTable() {
  const tb = document.querySelector("#resultsTable tbody");
  const search = ($("resultSearch")?.value || "").toLowerCase();
  let filtered = currentFilter === "all" ? allResults : allResults.filter((r) => (r.status || "").toLowerCase() === currentFilter);
  if (search) {
    filtered = filtered.filter((r) => (r.title || "").toLowerCase().includes(search) || (r.video_path || "").toLowerCase().includes(search) || (r.error || "").toLowerCase().includes(search));
  }

  if (filtered.length === 0) {
    tb.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--text-tertiary);padding:30px;font-size:13px">暂无发布记录</td></tr>';
    return;
  }
  tb.innerHTML = filtered
    .map((r) => {
      const sc = (r.status || "").toLowerCase();
      return "<tr>" + '<td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + esc(r.video_path || "") + '">' + esc((r.video_path || "").split("/").pop().split("\\").pop()) + "</td>" + "<td>" + esc(r.title || "") + "</td>" + '<td><span class="status-cell ' + sc + '"><span class="dot"></span>' + esc(getStatusText(r.status)) + "</span></td>" + '<td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text-tertiary)" title="' + esc(r.error || "") + '">' + esc(r.error || "") + "</td>" + "</tr>";
    })
    .join("");
}
document.addEventListener("DOMContentLoaded", () => {
  const searchEl = $("resultSearch");
  if (searchEl) searchEl.addEventListener("input", renderResultsTable);
  // 默认定时发布时间设为当前时间（本地时区），禁止选择过去时间
  if ($("formTime")) {
    const localStr = toLocalDatetime(new Date());
    $("formTime").min = localStr;
    if (!$("formTime").value) $("formTime").value = localStr;
  }
});

$("refreshResultsBtn").addEventListener("click", refreshResults);
$("exportResultsBtn").addEventListener("click", async () => {
  const res = await api("/api/results");
  const rows = await res.json();
  if (rows.length === 0) return toast("暂无结果", "info");
  const csv = [
    "video_path,title,status,error",
    ...rows.map((r) =>
      [r.video_path, r.title, r.status, r.error]
        .map((v) => {
          const s = String(v || "");
          return s.includes(",") || s.includes('"') ? '"' + s.replace(/"/g, '""') + '"' : s;
        })
        .join(",")
    ),
  ].join("\n");
  const blob = new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "results.csv";
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 100);
});

/* ═══════════════════════════════════════════════
   FULL LOG
   ═══════════════════════════════════════════════ */
async function refreshLog() {
  try {
    const res = await api("/api/log");
    const lines = await res.json();
    const searchTerm = $("logSearch").value.toLowerCase();
    const filtered = searchTerm ? lines.filter((l) => l.toLowerCase().includes(searchTerm)) : lines;
    $("fullLog").textContent = filtered.join("\n");
  } catch (e) {
    console.error("Log refresh error:", e);
  }
}

$("refreshLogBtn").addEventListener("click", refreshLog);
$("logSearch").addEventListener("input", refreshLog);

/* ═══════════════════════════════════════════════
   Log panel collapse
   ═══════════════════════════════════════════════ */
$("logToggle").addEventListener("click", function () {
  logCollapsed = !logCollapsed;
  const viewer = $("liveLog");
  const header = this;
  if (logCollapsed) {
    viewer.style.display = "none";
    header.classList.add("collapsed");
  } else {
    viewer.style.display = "";
    header.classList.remove("collapsed");
  }
});

/* ═══════════════════════════════════════════════
   KEYBOARD SHORTCUTS
   ═══════════════════════════════════════════════ */
document.addEventListener("keydown", function (e) {
  if (!authUser) return;
  // Ctrl+Enter: start upload (from upload view)
  if (e.ctrlKey && e.key === "Enter") {
    if (currentView === "upload" && !uploadRunning && entries.length > 0) {
      e.preventDefault();
      startUpload();
    }
  }
  // Ctrl+1..4: switch views
  const viewMap = { 1: "dashboard", 2: "upload", 3: "results", 4: "logs" };
  if (e.ctrlKey && viewMap[e.key]) {
    e.preventDefault();
    switchView(viewMap[e.key]);
  }
});

/* ═══════════════════════════════════════════════
   THEME — auto-sync system preference
   优先级: localStorage 显式选择 > 系统设置
   ═══════════════════════════════════════════════ */
(function () {
  const KEY = "theme";
  const darkMQL = window.matchMedia("(prefers-color-scheme: dark)");

  function applyDark() {
    document.body.setAttribute("data-theme", "dark");
    $("themeToggle").textContent = "☀️";
  }
  function applyLight() {
    document.body.removeAttribute("data-theme");
    $("themeToggle").textContent = "🌙";
  }

  function applyTheme() {
    const saved = localStorage.getItem(KEY);
    if (saved === "dark") {
      applyDark();
      return;
    }
    if (saved === "light") {
      applyLight();
      return;
    }
    // 默认跟随系统
    darkMQL.matches ? applyDark() : applyLight();
  }

  applyTheme();

  // 系统主题变化时自动跟随（仅当用户未显式选择时）
  darkMQL.addEventListener("change", () => {
    const saved = localStorage.getItem(KEY);
    if (saved !== "dark" && saved !== "light") applyTheme();
  });

  // 手动切换：保存显式选择
  $("themeToggle").addEventListener("click", () => {
    const isDark = document.body.getAttribute("data-theme") === "dark";
    if (isDark) {
      applyLight();
      localStorage.setItem(KEY, "light");
    } else {
      applyDark();
      localStorage.setItem(KEY, "dark");
    }
  });
})();

window.addEventListener("focus", () => {
  runWindowOpenChecks().catch((e) => {
    console.error("Window focus check error:", e);
  });
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  runWindowOpenChecks().catch((e) => {
    console.error("Window visibility check error:", e);
  });
});

/* ═══════════════════════════════════════════════
   INIT
   ═══════════════════════════════════════════════ */
(async function initApp() {
  runWindowOpenChecks({ force: true }).then(() => {
    if (!authUser) $("loginUsername").focus();
  }).catch((e) => {
    console.error("Init check error:", e);
    checkAuth().then(() => {
      if (!authUser) $("loginUsername").focus();
    });
  });
})();
