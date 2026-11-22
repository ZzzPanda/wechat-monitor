"use strict";

(() => {
  const dialog = byId("player-dialog");
  const status = byId("video-analysis-status");
  const action = byId("video-analysis-action");
  const progress = byId("video-analysis-progress");
  const result = byId("video-analysis-result");
  const activeStatuses = new Set(["queued", "running", "retry_wait"]);
  const stageLabels = {
    preparing: "正在提取画面与语音",
    analyzing: "正在解析片段",
    summarizing: "正在生成摘要",
  };
  const observationLabels = new Map([
    ["frame", "画面"], ["visual", "画面"], ["speech", "口播"], ["audio", "声音"],
    ["ocr", "屏幕文字"], ["text", "屏幕文字"],
  ]);
  let session;

  function timelineLabel(milliseconds) {
    const seconds = Math.floor(milliseconds / 1000);
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor(seconds / 60) % 60;
    const tail = String(seconds % 60).padStart(2, "0");
    return hours ? hours + ":" + String(minutes).padStart(2, "0") + ":" + tail : minutes + ":" + tail;
  }

  function timeLink(start, end) {
    if (!Number.isFinite(start) || start < 0) return null;
    const range = Number.isFinite(end) && end > start && timelineLabel(start) !== timelineLabel(end);
    const label = timelineLabel(start) + (range ? "–" + timelineLabel(end) : "");
    const seek = player.hasAttribute("src");
    const node = element(seek ? "button" : "span", "video-analysis-time", label);
    if (seek) {
      node.type = "button";
      node.setAttribute("aria-label", "跳到 " + timelineLabel(start));
      node.addEventListener("click", () => {
        const seconds = start / 1000;
        showPlayerView("media");
        player.currentTime = Math.min(seconds, Number.isFinite(player.duration) ? player.duration : seconds);
        player.scrollIntoView({ block: "nearest" });
        player.focus({ preventScroll: true });
      });
    }
    return node;
  }

  function observationList(items) {
    if (!Array.isArray(items)) return null;
    const list = element("ul", "video-analysis-observations");
    for (const item of items) {
      if (typeof item?.detail !== "string" || !item.detail.trim()) continue;
      const row = element("li");
      const time = timeLink(item.timestamp_ms ?? item.start_ms, item.end_ms);
      const kind = observationLabels.get(item.kind);
      if (time) row.append(time);
      row.append(element("p", "", (kind ? kind + " · " : "") + item.detail));
      list.append(row);
    }
    return list.childElementCount ? list : null;
  }

  function renderResult(analysis) {
    result.replaceChildren();
    if (!analysis) {
      result.hidden = true;
      return;
    }
    if (typeof analysis.summary === "string" && analysis.summary.trim()) {
      const summary = element("section");
      summary.append(element("h4", "", "摘要"), element("p", "", analysis.summary));
      result.append(summary);
    }
    const topics = Array.isArray(analysis.topics) ? analysis.topics.filter(topic => typeof topic === "string" && topic.trim()) : [];
    if (topics.length) {
      const list = element("ul", "video-analysis-topics");
      list.setAttribute("aria-label", "主题");
      list.append(...topics.map(topic => element("li", "", topic)));
      result.append(list);
    }
    const observations = observationList(analysis.observations);
    if (observations) {
      const evidence = element("section");
      evidence.append(element("h4", "", "关键内容"), observations);
      result.append(evidence);
    }
    if (Array.isArray(analysis.windows)) {
      const windows = element("details", "video-analysis-windows");
      windows.append(element("summary", "", "分段解析"));
      for (const window of analysis.windows) {
        const time = timeLink(window?.start_ms, window?.end_ms);
        if (!time || !window.analysis) continue;
        const segment = element("article");
        segment.append(time);
        if (typeof window.analysis.summary === "string" && window.analysis.summary.trim()) {
          segment.append(element("p", "", window.analysis.summary));
        }
        const evidence = observationList(window.analysis.observations);
        if (evidence) segment.append(evidence);
        windows.append(segment);
      }
      if (windows.childElementCount > 1) result.append(windows);
    }
    result.hidden = !result.childElementCount;
  }

  function render(current) {
    const { job, online, pending, error } = current;
    const state = job?.status;
    const active = activeStatuses.has(state);
    const total = Math.max(0, Number(job?.total) || 0);
    const completed = Math.max(0, Math.min(total, Number(job?.completed) || 0));
    let message = "尚未提交解析";
    if (state === "queued") message = "已入队，等待处理";
    if (state === "running") message = Object.hasOwn(stageLabels, job.stage) ? stageLabels[job.stage] : "正在解析";
    if (state === "retry_wait") message = "等待自动重试";
    if (state === "succeeded") message = "解析完成";
    if (state === "failed") message = "解析失败";
    if (active && total) message += " · 已完成 " + completed + " / " + total + " 段";
    if (!active && pending) message = pending === "POST" ? "正在提交解析任务…" : "正在读取解析状态…";
    if (!active && !job && error && !pending) message = "解析状态暂不可用";
    if (state !== "succeeded" && online === false) message += " · 后台解析进程离线";
    if (status.textContent !== message) status.textContent = message;
    const refresh = Boolean(error) && !active;
    action.hidden = active || state === "succeeded" || Boolean(pending);
    if (!action.hidden) {
      action.textContent = refresh ? "刷新状态" : state === "failed" ? "重试解析" : "提交解析";
    }
    progress.hidden = !active || !total;
    if (!progress.hidden) {
      progress.max = total;
      progress.value = completed;
    }
    setError("video-analysis-error", error || (state === "failed" && typeof job.error === "string" ? job.error : ""));
    if (current.displayedResult !== job?.result) {
      current.displayedResult = job?.result;
      renderResult(state === "succeeded" ? job.result : null);
    }
  }

  async function update(current, method = "GET") {
    if (current.pending) return;
    current.pending = method;
    current.error = "";
    render(current);
    try {
      const options = { method, cache: "no-store", signal: current.controller.signal };
      if (method === "POST") {
        options.headers = { "Content-Type": "application/json" };
        options.body = "{}";
      }
      const response = await fetch("/api/videos/" + encodeURIComponent(current.key) + "/analysis", options);
      const data = await response.json().catch(() => null);
      const fallback = method === "POST" ? "暂时无法提交解析，请稍后重试。" : "暂时无法读取解析状态，请稍后重试。";
      if (!response.ok || typeof data?.worker_online !== "boolean" || !Object.hasOwn(data, "job")) {
        throw new Error(typeof data?.error === "string" ? data.error : fallback);
      }
      current.job = data.job;
      current.online = data.worker_online;
    } catch (error) {
      current.error = error instanceof TypeError ? "连接失败，请稍后重试。" : error.message;
    } finally {
      current.pending = null;
      if (current === session) {
        render(current);
        if (activeStatuses.has(current.job?.status)) {
          current.timer = setTimeout(() => update(current), 2500);
        }
      }
    }
  }

  function stop() {
    if (!session) return;
    clearTimeout(session.timer);
    session.controller.abort();
    session = null;
  }

  function start(key) {
    stop();
    renderResult(null);
    if (typeof key !== "string" || !key) return;
    session = { key, controller: new AbortController() };
    update(session);
  }

  dialog.addEventListener("video-open", event => start(event.detail));
  if (dialog.open) start(dialog.dataset.videoKey);
  dialog.addEventListener("close", () => { if (!dialog.open) stop(); });
  action.addEventListener("click", () => {
    if (session && !action.hidden) update(session, session.error ? "GET" : "POST");
  });
})();
