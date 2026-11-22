"use strict";

(() => {
  const panel = byId("video-analyses");
  const dialog = byId("player-dialog");
  const list = byId("analysis-list-jobs");
  const summary = byId("analysis-list-summary");
  const query = byId("analysis-list-query");
  const filter = byId("analysis-list-status");
  const empty = byId("analysis-list-empty");
  const statuses = new Map([
    ["queued", "排队中"], ["retry_wait", "等待重试"], ["running", "正在解析"],
    ["succeeded", "已完成"], ["failed", "失败"],
  ]);
  const stages = new Map([
    ["preparing", "提取画面与语音"], ["analyzing", "解析片段"], ["summarizing", "生成摘要"],
  ]);
  let jobs;
  let workerOnline;
  let request;
  let timer;
  let signature;

  function jobRow(job) {
    const item = element("li");
    const link = element("a", "analysis-job-link");
    link.href = "#video-analyses/" + job.work_key;
    link.dataset.workKey = job.work_key;
    const action = job.status === "succeeded" ? "查看结果" : job.status === "failed" ? "查看详情" : "查看进度";
    const title = job.title || "未命名视频";
    const stage = job.status === "running" ? stages.get(job.stage) : "";
    const status = statuses.get(job.status);
    const progressText = job.total > 0 ? job.completed + " / " + job.total + " 段" : "";
    link.setAttribute("aria-label", [title, status, stage, progressText, action].filter(Boolean).join("，"));

    const thumb = element("span", "analysis-job-thumbnail");
    thumb.setAttribute("aria-hidden", "true");
    thumb.append(element("span", "analysis-icon"));
    const poster = sameSiteURL(job.poster);
    if (poster) {
      const image = element("img");
      image.alt = "";
      image.loading = "lazy";
      image.decoding = "async";
      image.addEventListener("error", () => image.remove(), { once: true });
      image.src = poster;
      thumb.append(image);
    }

    const identity = element("span", "analysis-job-name");
    const meta = [job.author, crawlTime(job.updated_at)].filter(Boolean).join(" · ");
    identity.append(element("strong", "", title), element("span", "analysis-job-meta", meta));

    const state = element("span", "analysis-job-state");
    const label = element("span", "analysis-job-status", status);
    label.dataset.state = job.status;
    state.append(label);
    const info = [stage, progressText].filter(Boolean).join(" · ");
    if (info) state.append(element("span", "analysis-job-info", info));
    if (job.status === "running" && job.total > 0) {
      const progress = element("progress");
      progress.max = job.total;
      progress.value = job.completed;
      progress.setAttribute("aria-label", "片段解析进度");
      state.append(progress);
    }
    link.append(thumb, identity, state, element("span", "analysis-job-action", action));
    item.append(link);
    return item;
  }

  function render() {
    if (!jobs) return;
    const search = query.value.trim().toLocaleLowerCase();
    const selectedStatus = filter.value;
    const matches = jobs.filter(job => {
      const group = job.status === "retry_wait" ? "queued" : job.status;
      return (!selectedStatus || selectedStatus === group) &&
        (!search || [job.title, job.author].some(value => value?.toLocaleLowerCase().includes(search)));
    });
    const count = search || selectedStatus ? "显示 " + matches.length + " / " + jobs.length : jobs.length;
    const message = count + " 个任务 · 解析进程" + (workerOnline ? "在线" : "离线");
    if (summary.textContent !== message) summary.textContent = message;
    empty.hidden = matches.length > 0;
    byId("analysis-list-empty-title").textContent = jobs.length ? "没有符合条件的任务" : "尚无解析任务";
    byId("analysis-list-open-library").hidden = jobs.length > 0;
    byId("analysis-list-clear").hidden = !jobs.length;
    list.hidden = !matches.length;

    const next = JSON.stringify(matches.map(job => [job.work_key, job.title, job.author, job.poster,
      job.status, job.stage, job.completed, job.total, job.updated_at]));
    if (next === signature) return;
    signature = next;
    const focused = list.contains(document.activeElement) ? document.activeElement.dataset.workKey : "";
    list.replaceChildren(...matches.map(jobRow));
    if (focused) list.querySelector('[data-work-key="' + focused + '"]')?.focus({ preventScroll: true });
  }

  function visible() {
    return !panel.hidden && !document.hidden && !dialog.open;
  }

  async function refresh() {
    if (!visible() || request) return;
    clearTimeout(timer);
    const controller = new AbortController();
    request = controller;
    if (!jobs) summary.textContent = "正在读取解析任务…";
    try {
      const response = await fetch("/api/video-analyses", { cache: "no-store", signal: controller.signal });
      if (!response.ok) throw new Error();
      const data = await response.json();
      if (!Array.isArray(data?.jobs) || typeof data.worker_online !== "boolean") throw new Error();
      if (request !== controller) return;
      jobs = data.jobs.filter(job => /^[0-9a-f]{64}$/.test(job?.work_key) && statuses.has(job.status))
        .sort((a, b) => b.updated_at - a.updated_at || a.work_key.localeCompare(b.work_key));
      workerOnline = data.worker_online;
      setError("analysis-list-error", "");
      render();
    } catch {
      if (request !== controller) return;
      if (!jobs) summary.textContent = "解析任务暂不可用";
      setError("analysis-list-error", "无法读取解析任务，请稍后刷新。");
    } finally {
      if (request === controller) {
        request = null;
        if (visible()) timer = setTimeout(refresh, 5000);
      }
    }
  }

  function syncVisibility() {
    if (visible()) {
      refresh();
    } else {
      clearTimeout(timer);
      request?.abort();
      request = null;
    }
  }

  query.addEventListener("input", render);
  filter.addEventListener("change", render);
  byId("analysis-list-refresh").addEventListener("click", refresh);
  byId("analysis-list-clear").addEventListener("click", () => {
    query.value = "";
    filter.value = "";
    render();
    query.focus();
  });
  document.addEventListener("dashboard-view", syncVisibility);
  document.addEventListener("visibilitychange", syncVisibility);
  dialog.addEventListener("video-open", syncVisibility);
  dialog.addEventListener("close", syncVisibility);
  syncVisibility();
})();
