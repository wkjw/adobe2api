document.addEventListener("DOMContentLoaded", () => {
  // Tabs
  const tabBtns = document.querySelectorAll(".tab-btn");
  const tabPanes = document.querySelectorAll(".tab-pane");

  tabBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      tabBtns.forEach(b => b.classList.remove("active"));
      tabPanes.forEach(p => p.classList.remove("active"));
      btn.classList.add("active");
      document.getElementById(btn.dataset.target).classList.add("active");
      if (btn.dataset.target === "logs") {
        loadLogs();
      }
    });
  });

  // Token Management
  const tokenInput = document.getElementById("tokenInput");
  const addBtn = document.getElementById("addBtn");
  const addMsg = document.getElementById("addMsg");
  const openAddTokenModalBtn = document.getElementById("openAddTokenModalBtn");
  const tokenModal = document.getElementById("tokenModal");
  const tokenModalCloseBtn = document.getElementById("tokenModalCloseBtn");
  const openRefreshModalBtn = document.getElementById("openRefreshModalBtn");
  const refreshModal = document.getElementById("refreshModal");
  const refreshModalCloseBtn = document.getElementById("refreshModalCloseBtn");
  const refreshBtn = document.getElementById("refreshBtn");
  const tbody = document.querySelector("#tokenTable tbody");

  const STATUS_MAP = {
    "active": "生效中",
    "exhausted": "额度耗尽",
    "invalid": "已失效",
    "error": "请求异常",
    "disabled": "已禁用"
  };

  async function loadTokens() {
    try {
      const res = await fetch("/api/v1/tokens");
      const data = await res.json();
      renderTable(data.tokens || []);
    } catch (err) {
      console.error(err);
      tbody.innerHTML = `<tr><td colspan="7" class="empty-state" style="color: #ffb4bc;">加载失败</td></tr>`;
    }
  }

  function openDialog(modalEl) {
    if (!modalEl) return;
    modalEl.classList.add("open");
    modalEl.setAttribute("aria-hidden", "false");
  }

  function closeDialog(modalEl) {
    if (!modalEl) return;
    modalEl.classList.remove("open");
    modalEl.setAttribute("aria-hidden", "true");
  }

  function formatExpiry(token) {
    if (!token || token.expires_at == null) {
      return '<span style="color:#7f96ad;">未知</span>';
    }
    const remain = Number(token.remaining_seconds || 0);
    const abs = Math.abs(remain);
    const days = Math.floor(abs / 86400);
    const hours = Math.floor((abs % 86400) / 3600);
    const mins = Math.floor((abs % 3600) / 60);
    const rel = days > 0 ? `${days}天${hours}小时` : `${hours}小时${mins}分`;
    if (remain <= 0 || token.is_expired) {
      return `<span style="color:#ffb4bc;">已过期 (${token.expires_at_text || '-'})</span>`;
    }
    if (remain < 3600 * 6) {
      return `<span style="color:#ffca58;">剩余 ${rel}<br><span style="color:#7f96ad;">${token.expires_at_text || '-'}</span></span>`;
    }
    return `<span style="color:#a8bfd8;">剩余 ${rel}<br><span style="color:#7f96ad;">${token.expires_at_text || '-'}</span></span>`;
  }

  function renderTable(tokens) {
    if (!tokens.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty-state">当前没有可用的 Token，请在上方添加。</td></tr>`;
      return;
    }

    tbody.innerHTML = "";
    tokens.forEach(t => {
      const tr = document.createElement("tr");

      const statusClass = `status-${t.status.toLowerCase()}`;
      const isStatusActive = t.status === "active";
      const isFrozen = t.status === "exhausted" || t.status === "invalid";
      const displayStatus = STATUS_MAP[t.status.toLowerCase()] || t.status;
      
      const d = new Date(t.added_at * 1000);
      const dateStr = d.toLocaleString();

      const toggleBtn = isFrozen
        ? `<button class="small" disabled title="额度耗尽或已失效 token 不可启用">不可启用</button>`
        : `<button class="small" onclick="toggleToken('${t.id}', '${isStatusActive ? 'disabled' : 'active'}')">${isStatusActive ? '禁用' : '启用'}</button>`;

      tr.innerHTML = `
        <td style="color: #a8bfd8; font-size: 12px;" title="添加时间: ${dateStr}">${t.id}</td>
        <td class="token-val">${t.value}</td>
        <td><span class="status-badge ${statusClass}">${displayStatus}</span></td>
        <td><span class="status-badge ${t.auto_refresh ? "status-active" : "status-disabled"}">${t.auto_refresh ? "是" : "否"}</span></td>
        <td style="color: ${t.fails > 0 ? '#ffb4bc' : '#a8bfd8'};">${t.fails}</td>
        <td style="font-size:12px; line-height:1.35;">${formatExpiry(t)}</td>
        <td class="action-btns">
          ${toggleBtn}
          <button class="danger" onclick="deleteToken('${t.id}')">删除</button>
        </td>
      `;
      tbody.appendChild(tr);
    });
  }

  addBtn.addEventListener("click", async () => {
    const val = tokenInput.value.trim();
    if (!val) {
      showMsg(addMsg, "请先输入 Token 内容", true);
      return;
    }

    addBtn.disabled = true;
    try {
      const res = await fetch("/api/v1/tokens", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: val })
      });
      if (res.ok) {
        tokenInput.value = "";
        showMsg(addMsg, "添加成功", false);
        loadTokens();
        closeDialog(tokenModal);
      } else {
        showMsg(addMsg, "添加失败，请重试", true);
      }
    } catch (err) {
      showMsg(addMsg, err.message, true);
    }
    addBtn.disabled = false;
  });

  refreshBtn.addEventListener("click", loadTokens);

  if (openAddTokenModalBtn) {
    openAddTokenModalBtn.addEventListener("click", () => openDialog(tokenModal));
  }
  if (tokenModalCloseBtn) {
    tokenModalCloseBtn.addEventListener("click", () => closeDialog(tokenModal));
  }
  if (tokenModal) {
    tokenModal.addEventListener("click", (event) => {
      if (event.target === tokenModal) closeDialog(tokenModal);
    });
  }

  if (openRefreshModalBtn) {
    openRefreshModalBtn.addEventListener("click", async () => {
      await loadRefreshStatus();
      openDialog(refreshModal);
    });
  }
  if (refreshModalCloseBtn) {
    refreshModalCloseBtn.addEventListener("click", () => closeDialog(refreshModal));
  }
  if (refreshModal) {
    refreshModal.addEventListener("click", (event) => {
      if (event.target === refreshModal) closeDialog(refreshModal);
    });
  }

  window.deleteToken = async (id) => {
    if (!confirm("确定要删除这个 Token 吗？")) return;
    try {
      await fetch(`/api/v1/tokens/${id}`, { method: "DELETE" });
      loadTokens();
    } catch (err) {
      alert("删除失败");
    }
  };

  window.toggleToken = async (id, newStatus) => {
    try {
      const res = await fetch(`/api/v1/tokens/${id}/status?status=${newStatus}`, { method: "PUT" });
      if (!res.ok) {
        const text = await res.text();
        alert(`状态更新失败: ${text}`);
        return;
      }
      loadTokens();
    } catch (err) {
      alert("状态更新失败");
    }
  };

  // Config Management
  const confApiKey = document.getElementById("confApiKey");
  const confUseProxy = document.getElementById("confUseProxy");
  const confProxy = document.getElementById("confProxy");
  const confGenerateTimeout = document.getElementById("confGenerateTimeout");
  const saveConfigBtn = document.getElementById("saveConfigBtn");
  const configMsg = document.getElementById("configMsg");
  const refreshBundleInput = document.getElementById("refreshBundleInput");
  const refreshBundleFile = document.getElementById("refreshBundleFile");
  const importRefreshBtn = document.getElementById("importRefreshBtn");
  const refreshNowBtn = document.getElementById("refreshNowBtn");
  const clearRefreshBtn = document.getElementById("clearRefreshBtn");
  const refreshStatus = document.getElementById("refreshStatus");
  const refreshMsg = document.getElementById("refreshMsg");
  let latestRefreshStatus = null;

  // Logs
  const logsTbody = document.querySelector("#logsTable tbody");
  const refreshLogsBtn = document.getElementById("refreshLogsBtn");
  const clearLogsBtn = document.getElementById("clearLogsBtn");
  const previewModal = document.getElementById("previewModal");
  const previewContent = document.getElementById("previewContent");
  const previewCloseBtn = document.getElementById("previewCloseBtn");
  const previewDownloadBtn = document.getElementById("previewDownloadBtn");

  async function loadConfig() {
    try {
      const res = await fetch("/api/v1/config");
      if (res.ok) {
        const data = await res.json();
        confApiKey.value = data.api_key || "";
        confUseProxy.checked = data.use_proxy || false;
        confProxy.value = data.proxy || "";
        confGenerateTimeout.value = Number(data.generate_timeout || 300);
      }
    } catch (err) {
      console.error("加载配置失败", err);
    }
  }

  saveConfigBtn.addEventListener("click", async () => {
    saveConfigBtn.disabled = true;
    try {
      // 保留未在此页面显示的配置项
      const currentRes = await fetch("/api/v1/config");
      const currentData = await currentRes.json();
      
      const payload = {
        ...currentData,
        api_key: confApiKey.value.trim(),
        use_proxy: confUseProxy.checked,
        proxy: confProxy.value.trim(),
        generate_timeout: Math.max(1, Number(confGenerateTimeout.value || 300)),
      };

      const res = await fetch("/api/v1/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      if (res.ok) {
        showMsg(configMsg, "配置已保存", false);
      } else {
        showMsg(configMsg, "保存失败，请检查服务状态", true);
      }
    } catch (err) {
      showMsg(configMsg, err.message, true);
    }
    saveConfigBtn.disabled = false;
  });

  function formatTs(ts) {
    if (!ts) return "-";
    const d = new Date(Number(ts) * 1000);
    if (Number.isNaN(d.getTime())) return "-";
    return d.toLocaleString();
  }

  function renderRefreshStatus(st) {
    if (!refreshStatus) return;
    if (!st || !st.has_profile) {
      refreshStatus.textContent = "自动刷新未启用";
      return;
    }
    const lines = [
      `状态: ${st.enabled ? "已启用" : "已禁用"}`,
      `目标: ${st.endpoint?.url || "-"}`,
      `Client ID: ${st.endpoint?.client_id || "-"}`,
      `最近成功: ${formatTs(st.last_success_at)}`,
      `最近尝试: ${formatTs(st.last_attempt_at)}`,
      `最近错误: ${st.last_error || "-"}`
    ];
    refreshStatus.textContent = lines.join("\n");
  }

  async function loadRefreshStatus() {
    try {
      const res = await fetch("/api/v1/refresh-profile/status");
      if (!res.ok) throw new Error("状态加载失败");
      const data = await res.json();
      latestRefreshStatus = data;
      renderRefreshStatus(data);
    } catch (err) {
      latestRefreshStatus = null;
      renderRefreshStatus(null);
    }
  }

  async function importRefreshBundle() {
    const text = String(refreshBundleInput?.value || "").trim();
    if (!text) {
      showMsg(refreshMsg, "请先粘贴或上传 JSON", true);
      return;
    }
    let parsed;
    try {
      parsed = JSON.parse(text);
    } catch (err) {
      showMsg(refreshMsg, "JSON 格式错误", true);
      return;
    }
    try {
      const res = await fetch("/api/v1/refresh-profile/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ bundle: parsed })
      });
      if (!res.ok) {
        const txt = await res.text();
        throw new Error(txt || "导入失败");
      }
      showMsg(refreshMsg, "导入成功", false);
      await loadRefreshStatus();
    } catch (err) {
      showMsg(refreshMsg, err.message || "导入失败", true);
    }
  }

  async function refreshNow() {
    try {
      // If profile is not imported but JSON exists in the textbox,
      // auto-import first to reduce user steps.
      if (!latestRefreshStatus?.has_profile) {
        const raw = String(refreshBundleInput?.value || "").trim();
        if (raw) {
          let parsed = null;
          try {
            parsed = JSON.parse(raw);
          } catch (err) {
            throw new Error("导入区 JSON 格式错误，请先修正后重试");
          }
          const importRes = await fetch("/api/v1/refresh-profile/import", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ bundle: parsed })
          });
          if (!importRes.ok) {
            const txt = await importRes.text();
            throw new Error(txt || "自动导入失败，请先点导入按钮");
          }
          await loadRefreshStatus();
        }
      }

      const res = await fetch("/api/v1/refresh-profile/refresh-now", { method: "POST" });
      if (!res.ok) {
        let detail = "刷新失败";
        try {
          const body = await res.json();
          detail = body.detail || JSON.stringify(body);
        } catch (_) {
          detail = await res.text();
        }
        throw new Error(detail || "刷新失败");
      }
      showMsg(refreshMsg, "刷新成功，已更新自动刷新 token", false);
      await loadRefreshStatus();
      await loadTokens();
    } catch (err) {
      showMsg(refreshMsg, err.message || "刷新失败", true);
      await loadRefreshStatus();
    }
  }

  async function clearRefreshProfile() {
    if (!confirm("确定清除自动刷新导入数据吗？")) return;
    try {
      const res = await fetch("/api/v1/refresh-profile", { method: "DELETE" });
      if (!res.ok) throw new Error("清除失败");
      if (refreshBundleInput) refreshBundleInput.value = "";
      if (refreshBundleFile) refreshBundleFile.value = "";
      showMsg(refreshMsg, "已清除导入数据", false);
      await loadRefreshStatus();
    } catch (err) {
      showMsg(refreshMsg, err.message || "清除失败", true);
    }
  }

  if (refreshBundleFile) {
    refreshBundleFile.addEventListener("change", async () => {
      const file = refreshBundleFile.files && refreshBundleFile.files[0];
      if (!file) return;
      try {
        const text = await file.text();
        if (refreshBundleInput) refreshBundleInput.value = text;
      } catch (err) {
        showMsg(refreshMsg, "读取文件失败", true);
      }
    });
  }

  if (importRefreshBtn) importRefreshBtn.addEventListener("click", importRefreshBundle);
  if (refreshNowBtn) refreshNowBtn.addEventListener("click", refreshNow);
  if (clearRefreshBtn) clearRefreshBtn.addEventListener("click", clearRefreshProfile);

  async function loadLogs() {
    if (!logsTbody) return;
    try {
      const res = await fetch("/api/v1/logs?limit=200");
      if (!res.ok) throw new Error("加载日志失败");
      const data = await res.json();
      renderLogs(data.logs || []);
    } catch (err) {
      logsTbody.innerHTML = `<tr><td colspan="7" class="empty-state" style="color: #ffb4bc;">${err.message || "日志加载失败"}</td></tr>`;
    }
  }

  function renderLogs(logs) {
    if (!logs.length) {
      logsTbody.innerHTML = `<tr><td colspan="7" class="empty-state">暂无请求日志</td></tr>`;
      return;
    }

    logsTbody.innerHTML = "";
    logs.forEach(item => {
      const tr = document.createElement("tr");
      const dt = new Date((item.ts || 0) * 1000);
      const t = Number(item.duration_sec || 0);
      const status = Number(item.status_code || 0);
      const statusClass = status >= 500 ? "log-status-5xx" : (status >= 400 ? "log-status-4xx" : "log-status-2xx");
      const previewUrl = String(item.preview_url || "").trim();
      const previewKind = String(item.preview_kind || "").trim();
      const previewCell = previewUrl
        ? `<button class="small preview-btn" data-url="${encodeURIComponent(previewUrl)}" data-kind="${previewKind || ""}">查看</button>`
        : `<span style="color:#7f96ad;">-</span>`;
      tr.innerHTML = `
        <td style="white-space: nowrap; color: #a8bfd8;">${dt.toLocaleString()}</td>
        <td><span class="status-badge ${statusClass}">${status || "-"}</span></td>
        <td style="color:#a8bfd8;">${t}</td>
        <td class="token-val">${item.model || "-"}</td>
        <td title="${(item.prompt_preview || "").replace(/"/g, "&quot;")}" style="max-width: 280px; color: #a8bfd8;">${item.prompt_preview || "-"}</td>
        <td style="font-family: 'IBM Plex Mono', monospace; color:#a8bfd8;">${typeof item.proxy_used === "boolean" ? (item.proxy_used ? "是" : "否") : "-"}</td>
        <td>${previewCell}</td>
      `;
      logsTbody.appendChild(tr);
    });
  }

  function inferPreviewKind(url) {
    const lowered = String(url || "").toLowerCase();
    if (/(\.mp4|\.webm|\.ogg)(\?|$)/.test(lowered)) return "video";
    return "image";
  }

  function closePreview() {
    if (!previewModal || !previewContent) return;
    previewModal.classList.remove("open");
    previewModal.setAttribute("aria-hidden", "true");
    previewContent.innerHTML = "";
    if (previewDownloadBtn) {
      previewDownloadBtn.setAttribute("href", "#");
      previewDownloadBtn.setAttribute("download", "");
    }
  }

  function buildDownloadFilename(url, kind) {
    try {
      const u = new URL(url, window.location.origin);
      const fromPath = (u.pathname.split("/").pop() || "").trim();
      if (fromPath) return fromPath;
    } catch (err) {
      // ignore parse errors and fallback
    }
    const ext = kind === "video" ? "mp4" : "png";
    return `asset-${Date.now()}.${ext}`;
  }

  function openPreview(url, kind) {
    if (!previewModal || !previewContent || !url) return;
    const mediaKind = kind || inferPreviewKind(url);
    if (mediaKind === "video") {
      previewContent.innerHTML = `<video controls autoplay playsinline src="${url}"></video>`;
    } else {
      previewContent.innerHTML = `<img src="${url}" alt="预览图" />`;
    }
    if (previewDownloadBtn) {
      previewDownloadBtn.setAttribute("href", url);
      previewDownloadBtn.setAttribute("download", buildDownloadFilename(url, mediaKind));
    }
    previewModal.classList.add("open");
    previewModal.setAttribute("aria-hidden", "false");
  }

  if (logsTbody) {
    logsTbody.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement) || !target.classList.contains("preview-btn")) return;
      const encodedUrl = target.getAttribute("data-url") || "";
      const kind = (target.getAttribute("data-kind") || "").trim();
      if (!encodedUrl) return;
      openPreview(decodeURIComponent(encodedUrl), kind);
    });
  }

  if (previewCloseBtn) {
    previewCloseBtn.addEventListener("click", closePreview);
  }

  if (previewModal) {
    previewModal.addEventListener("click", (event) => {
      if (event.target === previewModal) closePreview();
    });
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closePreview();
      closeDialog(tokenModal);
      closeDialog(refreshModal);
    }
  });

  if (refreshLogsBtn) {
    refreshLogsBtn.addEventListener("click", loadLogs);
  }

  if (clearLogsBtn) {
    clearLogsBtn.addEventListener("click", async () => {
      if (!confirm("确定清空请求日志吗？")) return;
      try {
        const res = await fetch("/api/v1/logs", { method: "DELETE" });
        if (!res.ok) throw new Error("清空失败");
        loadLogs();
      } catch (err) {
        alert(err.message || "清空失败");
      }
    });
  }


  function showMsg(el, text, isError) {
    el.textContent = text;
    el.style.color = isError ? "#ffb4bc" : "#4de2c4";
    setTimeout(() => { el.textContent = ""; }, 3000);
  }

  // Init
  loadTokens();
  loadConfig();
  loadLogs();
  loadRefreshStatus();
});
