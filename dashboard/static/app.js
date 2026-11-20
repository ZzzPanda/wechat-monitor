"use strict";

const byId = (id) => document.getElementById(id);
const state = { authors: [], entries: [], profiles: [], videos: [], author: null, loaded: false };
const collator = new Intl.Collator("zh-CN", { numeric: true });
const player = byId("player");
let pendingLoad = 0;
let noticeTimer;
let addingAuthor = false;
let searchController = null;
let selectedAccount = null;
let editingEntry = null;
let replacement = null;
let removingEntry = null;
let choosingReplacement = false;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function avatar(name) {
  const badge = element("span", "avatar", Array.from(name.trim())[0] || "·");
  badge.setAttribute("aria-hidden", "true");
  return badge;
}

function sameSiteURL(value) {
  if (typeof value !== "string" || !value) return "";
  try {
    const url = new URL(value, window.location.origin);
    return url.origin === window.location.origin && url.protocol === window.location.protocol ? url.href : "";
  } catch {
    return "";
  }
}

function bytesLabel(size) {
  if (!Number.isFinite(size) || size < 0) return "";
  if (size < 1000) return size + " B";
  const units = ["KB", "MB", "GB", "TB"];
  let unit = 0;
  let amount = size / 1000;
  while (amount >= 1000 && unit < units.length - 1) {
    amount /= 1000;
    unit++;
  }
  return Number(amount.toFixed(1)) + " " + units[unit];
}

function durationLabel(value) {
  const seconds = Math.round(Number(value));
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds / 60) % 60;
  const tail = String(seconds % 60).padStart(2, "0");
  return hours ? hours + ":" + String(minutes).padStart(2, "0") + ":" + tail : minutes + ":" + tail;
}

function notice(message) {
  clearTimeout(noticeTimer);
  const toast = byId("toast");
  toast.textContent = message;
  toast.hidden = false;
  noticeTimer = setTimeout(() => { toast.hidden = true; }, 3000);
}

function setError(id, message) {
  const node = byId(id);
  node.textContent = message;
  node.hidden = !message;
}

function setNavOpen(open) {
  document.querySelector(".rail").classList.toggle("open", open);
  byId("nav-toggle").setAttribute("aria-expanded", String(open));
}

function selectAuthor(author) {
  state.author = author;
  setNavOpen(false);
  render();
  if (window.matchMedia("(max-width: 720px)").matches) byId("nav-toggle").focus();
}

function authorButton(name, count) {
  const node = element("button", "nav-item");
  node.type = "button";
  node.title = name;
  if (name === state.author) {
    node.classList.add("current");
    node.setAttribute("aria-current", "page");
  }
  node.append(avatar(name), element("span", "nav-name", name), element("span", "nav-count", count));
  node.addEventListener("click", () => selectAuthor(name));
  return node;
}

function renderNav() {
  const counts = new Map();
  for (const video of state.videos) counts.set(video.author, (counts.get(video.author) || 0) + 1);
  const activeAuthors = new Set(state.authors);
  const pastAuthors = Array.from(counts.keys()).filter(name => !activeAuthors.has(name)).sort(collator.compare);
  if (state.author !== null && !activeAuthors.has(state.author) && !counts.has(state.author)) state.author = null;
  const entry = currentEntry();
  const profile = entry || state.profiles.find(item => item?.name === state.author);
  const profileLink = byId("saved-profile");
  const href = accountLink(profile?.url);
  profileLink.hidden = !href;
  byId("link-profile").hidden = Boolean(href) || !activeAuthors.has(state.author);
  byId("edit-author").hidden = !entry;
  byId("remove-author").hidden = !entry;
  byId("remonitor-author").hidden = !pastAuthors.includes(state.author);
  byId("saved-note").hidden = !entry?.note;
  byId("saved-note").textContent = entry?.note || "";
  renderCrawlAction();
  if (href) profileLink.href = href;
  else profileLink.removeAttribute("href");
  const all = byId("all-videos");
  all.classList.toggle("current", state.author === null);
  if (state.author === null) all.setAttribute("aria-current", "page");
  else all.removeAttribute("aria-current");
  byId("all-count").textContent = String(state.videos.length);
  const authors = byId("authors");
  authors.replaceChildren(...state.authors.map(name => authorButton(name, counts.get(name) || 0)));
  if (state.authors.length === 0) authors.append(element("p", "rail-empty", "名单为空"));
  byId("past-group").hidden = pastAuthors.length === 0;
  byId("past-authors").replaceChildren(...pastAuthors.map(name => authorButton(name, counts.get(name))));
  byId("storage").hidden = false;
  byId("storage-size").textContent = bytesLabel(state.videos.reduce((sum, item) => sum + (Number(item.size) || 0), 0));
}

function openVideo(video) {
  const dialog = byId("player-dialog");
  const title = video.title || "未命名视频";
  const url = sameSiteURL(video.url);
  byId("playing-title").textContent = title;
  byId("playing-meta").textContent = [video.author, video.published, bytesLabel(Number(video.size))].filter(Boolean).join(" · ");
  byId("player-error").hidden = Boolean(url);
  const poster = sameSiteURL(video.poster);
  if (poster) player.poster = poster;
  else player.removeAttribute("poster");
  dialog.showModal();
  if (!url) return;
  player.src = url;
  player.play().catch(() => { /* Native controls allow manual playback. */ });
}

function videoCard(video) {
  const title = video.title || "未命名视频";
  const card = element("button", "video-card");
  card.type = "button";
  card.setAttribute("aria-label", "播放：" + title + "，" + video.author);
  card.title = title;
  const thumb = element("span", "thumbnail");
  thumb.setAttribute("aria-hidden", "true");
  thumb.append(element("span", "poster-placeholder"));
  const poster = sameSiteURL(video.poster);
  if (poster) {
    const image = element("img");
    image.alt = "";
    image.loading = "lazy";
    image.decoding = "async";
    image.addEventListener("error", () => image.remove(), { once: true });
    image.src = poster;
    thumb.append(image);
  }
  thumb.append(element("span", "play-icon"));
  const duration = durationLabel(video.duration);
  if (duration) thumb.append(element("span", "duration", duration));
  const body = element("span", "card-body");
  const meta = element("span", "metadata");
  const byline = element("span", "byline");
  byline.append(avatar(video.author), element("span", "author-name", video.author));
  meta.append(byline);
  if (video.published) {
    const date = element("time", "", video.published.replaceAll("-", "."));
    date.dateTime = video.published;
    meta.append(date);
  }
  body.append(element("span", "video-title", title), meta);
  card.append(thumb, body);
  card.addEventListener("click", () => openVideo(video));
  return card;
}

function renderVideos() {
  const query = byId("query").value.trim().toLocaleLowerCase();
  const items = state.videos.filter(video =>
    (state.author === null || video.author === state.author) &&
    (!query || (video.title + " " + video.author).toLocaleLowerCase().includes(query)));
  const sort = byId("sort").value;
  items.sort((a, b) => {
    if (sort === "title") return collator.compare(a.title, b.title);
    const timeOrder = (a.published || "").localeCompare(b.published || "");
    return sort === "oldest" ? timeOrder : -timeOrder;
  });
  byId("page-title").textContent = state.author || "视频资料库";
  byId("summary").textContent = query ? "找到 " + items.length + " 个视频" :
    state.author ? "已保存 " + items.length + " 个视频" :
      "已保存 " + items.length + " 个视频 · 监控 " + state.authors.length + " 个视频号";
  byId("videos").replaceChildren(...items.map(videoCard));
  byId("empty").hidden = items.length !== 0;
  byId("clear-search").hidden = !query;
  if (!items.length) {
    byId("empty-title").textContent = query ? "没有符合条件的视频" : "暂无历史视频";
    byId("empty-text").textContent = query ? "试试其他标题或作者名称。" :
      state.author ? "此作者的作品保存后会显示在这里。" : "视频号作品保存后会显示在这里。";
  }
}

function render() {
  renderNav();
  renderVideos();
}

function currentEntry() {
  return state.entries.find(entry => entry.name === state.author);
}

async function loadDashboard() {
  const version = ++pendingLoad;
  byId("refresh").disabled = true;
  byId("videos").setAttribute("aria-busy", "true");
  setError("fetch-error", "");
  try {
    const response = await fetch("/api/dashboard", { cache: "no-store" });
    if (!response.ok) throw new Error();
    const data = await response.json();
    if (!Array.isArray(data.authors) || !Array.isArray(data.videos)) throw new Error();
    if (version !== pendingLoad) return;
    state.authors = data.authors;
    state.entries = Array.isArray(data.entries) ? data.entries : [];
    state.profiles = Array.isArray(data.profiles) ? data.profiles : [];
    state.videos = data.videos;
    state.loaded = true;
    render();
    return true;
  } catch {
    if (version !== pendingLoad) return;
    setError("fetch-error", "无法读取资料库，请确认服务和外置盘可用，再点刷新。");
    if (!state.loaded) byId("summary").textContent = "资料库暂不可用";
  } finally {
    if (version === pendingLoad) {
      byId("refresh").disabled = false;
      byId("videos").setAttribute("aria-busy", "false");
    }
  }
}

byId("all-videos").addEventListener("click", () => selectAuthor(null));
byId("nav-toggle").addEventListener("click", () => setNavOpen(byId("nav-toggle").getAttribute("aria-expanded") !== "true"));
byId("query").addEventListener("input", () => { if (state.loaded) renderVideos(); });
byId("sort").addEventListener("change", () => { if (state.loaded) renderVideos(); });
byId("refresh").addEventListener("click", loadDashboard);
byId("clear-search").addEventListener("click", () => {
  byId("query").value = "";
  renderVideos();
  byId("query").focus();
});

document.querySelectorAll("[data-close]").forEach(button => {
  button.addEventListener("click", () => byId(button.dataset.close).close());
});
for (const dialog of document.querySelectorAll("dialog")) {
  dialog.addEventListener("cancel", event => {
    if (dialog.getAttribute("aria-busy") === "true") event.preventDefault();
  });
  dialog.addEventListener("click", event => {
    if (event.target !== dialog || dialog.getAttribute("aria-busy") === "true") return;
    const box = dialog.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close();
  });
}

player.addEventListener("error", () => { if (byId("player-dialog").open) byId("player-error").hidden = false; });
byId("player-dialog").addEventListener("close", () => {
  player.pause();
  player.removeAttribute("src");
  player.removeAttribute("poster");
  player.load();
});
const authorDialog = byId("author-dialog");
const authorQuery = byId("author-name");
const accountOptions = byId("account-options");
const accountList = byId("account-list");
const searchButton = byId("search-author");
const freshSearchButton = byId("fresh-author-search");
const addButton = byId("submit-author");

function resetAccountSearch() {
  if (searchController) searchController.abort();
  searchController = null;
  selectedAccount = null;
  accountList.replaceChildren();
  accountOptions.hidden = true;
  freshSearchButton.hidden = true;
  addButton.disabled = true;
  searchButton.disabled = !authorQuery.value.trim() || addingAuthor;
  byId("author-search-status").textContent = "";
  setError("author-error", "");
}

function accountLink(value) {
  if (typeof value !== "string" || !value) return "";
  try {
    const url = new URL(value);
    const official = /(^|\.)(weixin\.qq\.com|weixin\.com)$/.test(url.hostname);
    return official && url.protocol === "https:" && !url.username && !url.password && !url.port ? url.href : "";
  } catch {
    return "";
  }
}

function showCandidate(candidate) {
  if (!candidate || typeof candidate.name !== "string" || !candidate.name.trim() ||
      typeof candidate.token !== "string" || !candidate.token) return;
  const href = accountLink(candidate.url);
  if (!href) return;
  const card = element("div", "account-row");
  const label = element("label", "account-choice");
  const radio = element("input");
  radio.type = "radio";
  radio.name = "candidate";
  radio.addEventListener("change", () => {
    selectedAccount = candidate;
    addButton.disabled = false;
    setError("author-error", "");
  });
  const details = element("span", "account-details");
  details.append(element("strong", "", candidate.name));
  if (typeof candidate.description === "string" && candidate.description.trim()) {
    details.append(element("span", "account-description", candidate.description));
  }
  label.append(radio, details);
  const link = element("a", "account-link");
  link.append(element("span", "", "视频号主页"), element("span", "account-url", href));
  link.href = href;
  link.title = href;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.setAttribute("aria-label", "查看「" + candidate.name + "」的视频号主页");
  card.append(label, link);
  accountList.append(card);
}

function openAuthorSearch(query, forEdit = false) {
  choosingReplacement = forEdit;
  byId("author-dialog-title").textContent = forEdit ? "选择监控视频号" : "添加监控视频号";
  addButton.textContent = forEdit ? "使用此视频号" : "加入监控名单";
  authorQuery.value = query;
  resetAccountSearch();
  authorDialog.showModal();
  authorQuery.focus();
}
byId("add-author").addEventListener("click", () => openAuthorSearch(""));
byId("link-profile").addEventListener("click", () => currentEntry() ? openEditor(true) : openAuthorSearch(state.author));
byId("remonitor-author").addEventListener("click", () => openAuthorSearch(state.author));
authorDialog.addEventListener("close", resetAccountSearch);
authorDialog.addEventListener("cancel", event => { if (addingAuthor) event.preventDefault(); });
authorQuery.addEventListener("input", resetAccountSearch);

byId("author-search-form").addEventListener("submit", async event => {
  event.preventDefault();
  const query = authorQuery.value.trim();
  if (!query || addingAuthor || searchController) return;
  const fresh = event.submitter === freshSearchButton;
  resetAccountSearch();
  if (fresh) authorQuery.focus();
  const controller = new AbortController();
  searchController = controller;
  searchButton.disabled = true;
  const status = byId("author-search-status");
  status.textContent = "正在微信桌面搜索并核对主页…";
  try {
    const params = new URLSearchParams({ q: query });
    if (fresh) params.set("fresh", "1");
    const response = await fetch("/api/author-search?" + params, {
      cache: "no-store", signal: controller.signal
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.error === "string" ? data.error : "搜索暂不可用，请稍后重试。");
    if (!Array.isArray(data.results)) throw new Error("未能读取搜索结果，请重新搜索。");
    if (controller.signal.aborted) return;
    for (const candidate of data.results) showCandidate(candidate);
    const count = accountList.childElementCount;
    accountOptions.hidden = count === 0;
    const summary = count ? "找到 " + count + " 个账号，请核对后选择。" :
      (data.results.length ? "这些结果没有可用的微信账号链接。" : "当前结果中没有匹配的视频号，试试完整名称。");
    freshSearchButton.hidden = data.cached !== true;
    status.textContent = data.cached === true ? "最近 10 分钟的微信搜索结果。" + summary : summary;
  } catch (error) {
    if (controller.signal.aborted) return;
    status.textContent = "";
    setError("author-error", error instanceof TypeError ? "连接失败，请检查服务后重试。" : error.message);
  } finally {
    if (searchController === controller) {
      searchController = null;
      searchButton.disabled = false;
    }
  }
});

addButton.addEventListener("click", async () => {
  if (addingAuthor || !selectedAccount || searchController) return;
  const account = selectedAccount;
  if (choosingReplacement) {
    replacement = account;
    renderEditor();
    authorDialog.close();
    byId("save-author").focus();
    return;
  }
  addingAuthor = true;
  authorDialog.setAttribute("aria-busy", "true");
  addButton.disabled = searchButton.disabled = freshSearchButton.disabled = authorQuery.disabled = accountOptions.disabled = true;
  addButton.textContent = "正在添加…";
  const closeButtons = authorDialog.querySelectorAll("[data-close]");
  closeButtons.forEach(button => { button.disabled = true; });
  setError("author-error", "");
  try {
    const response = await fetch("/api/authors", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: account.token })
    });
    const data = await response.json();
    if (!response.ok) {
      if ([400, 404, 409, 410].includes(response.status)) resetAccountSearch();
      throw new Error(typeof data.error === "string" ? data.error : "添加失败，请重试。");
    }
    authorDialog.close();
    if (data.linked) notice("已为「" + account.name + "」关联视频号主页");
    else notice(data.added === false ? "该视频号已在监控名单中" : "已添加「" + account.name + "」");
    state.author = account.name;
    loadDashboard();
  } catch (error) {
    const message = error instanceof TypeError ? "连接失败，请检查服务后重试。" : error.message;
    if (authorDialog.open) setError("author-error", message);
    else notice(message);
  } finally {
    addingAuthor = false;
    authorDialog.removeAttribute("aria-busy");
    freshSearchButton.disabled = authorQuery.disabled = accountOptions.disabled = false;
    searchButton.disabled = !authorQuery.value.trim() || Boolean(searchController);
    addButton.disabled = !selectedAccount;
    addButton.textContent = "加入监控名单";
    closeButtons.forEach(button => { button.disabled = false; });
  }
});

const editDialog = byId("edit-dialog");
const removeDialog = byId("remove-dialog");
const noteInput = byId("author-note");

function renderEditor() {
  const shown = replacement || editingEntry;
  const url = accountLink(shown.url);
  byId("edit-account-name").textContent = shown.name;
  const link = byId("edit-profile-url");
  link.hidden = !url;
  if (url) link.href = url;
  else link.removeAttribute("href");
  link.textContent = url;
  link.title = url;
  byId("choose-replacement").textContent = url ? "更换账号" : "关联主页";
  byId("save-author").disabled = !replacement && noteInput.value === editingEntry.note;
}

function openEditor(choose = false) {
  editingEntry = currentEntry();
  if (!editingEntry) return;
  replacement = null;
  noteInput.value = editingEntry.note;
  setError("edit-error", "");
  renderEditor();
  editDialog.showModal();
  if (choose) openAuthorSearch(editingEntry.name, true);
  else noteInput.focus();
}
byId("edit-author").addEventListener("click", () => openEditor());
byId("choose-replacement").addEventListener("click", () => openAuthorSearch((replacement || editingEntry).name, true));
editDialog.addEventListener("close", () => { editingEntry = replacement = null; });
noteInput.addEventListener("input", () => {
  setError("edit-error", "");
  renderEditor();
});
byId("remove-author").addEventListener("click", () => {
  removingEntry = currentEntry();
  if (!removingEntry) return;
  byId("remove-description").textContent = "将停止监控「" + removingEntry.name + "」。已保存的视频会保留在资料库中。";
  setError("remove-error", "");
  removeDialog.showModal();
});
removeDialog.addEventListener("close", () => { removingEntry = null; });

async function changeEntry(method, entry, changes) {
  const editing = method === "PATCH";
  const dialog = editing ? editDialog : removeDialog;
  if (!entry || dialog.getAttribute("aria-busy") === "true") return;
  const errorId = editing ? "edit-error" : "remove-error";
  const submit = byId(editing ? "save-author" : "confirm-remove-author");
  dialog.setAttribute("aria-busy", "true");
  dialog.querySelectorAll("button, input").forEach(node => { node.disabled = true; });
  submit.textContent = editing ? "正在保存…" : "正在移除…";
  setError(errorId, "");
  try {
    // Browsers normalize dot-only path segments, including percent-encoded dots.
    const url = entry.name === "." || entry.name === ".."
      ? "/api/authors?" + new URLSearchParams({ name: entry.name }) : "/api/authors/" + encodeURIComponent(entry.name);
    const response = await fetch(url, {
      method, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ version: entry.version, ...changes })
    });
    const data = await response.json();
    if (!response.ok) {
      const message = data.error || "操作失败，请重试。";
      if ([404, 409].includes(response.status) && await loadDashboard() &&
          !state.entries.some(item => item.name === entry.name && item.version === entry.version)) {
        dialog.close();
        notice(message + " 已刷新监控名单。");
        return;
      }
      throw new Error(message);
    }
    if (editing) {
      state.entries = state.entries.map(item => item.name === entry.name ? data.entry : item);
      state.authors = state.authors.map(name => name === entry.name ? data.entry.name : name);
      state.author = data.entry.name;
    } else {
      state.entries = state.entries.filter(item => item.name !== entry.name);
      state.authors = state.authors.filter(name => name !== entry.name);
    }
    state.profiles = state.profiles.filter(item => item.name !== entry.name);
    dialog.close();
    render();
    notice(editing ? "已保存「" + data.entry.name + "」的更改" : "已从监控名单移除「" + entry.name + "」");
    loadDashboard();
  } catch (error) {
    setError(errorId, error instanceof TypeError ? "连接失败，请检查服务后重试。" : error.message);
  } finally {
    dialog.removeAttribute("aria-busy");
    dialog.querySelectorAll("button, input").forEach(node => { node.disabled = false; });
    submit.textContent = editing ? "保存更改" : "确认移除";
    if (editing && editDialog.open && editingEntry) renderEditor();
  }
}
byId("edit-form").addEventListener("submit", event => {
  event.preventDefault();
  const changes = {};
  if (!editingEntry) return;
  if (noteInput.value !== editingEntry.note) changes.note = noteInput.value;
  if (replacement) changes.token = replacement.token;
  if (Object.keys(changes).length) changeEntry("PATCH", editingEntry, changes);
});
byId("confirm-remove-author").addEventListener("click", () => changeEntry("DELETE", removingEntry, {}));

const crawlPanel = byId("crawl-panel");
const scheduleEnabled = byId("schedule-enabled");
const scheduleInterval = byId("schedule-interval");
const scheduleStart = byId("schedule-start");
const scheduleSave = byId("save-schedule");
const jobLabels = { queued: "排队中", running: "采集中", done: "已完成", partial: "部分完成", failed: "失败", cancelled: "已取消" };
let crawlJobs = null;
let crawlSettings = null;
let workerOnline;
const stoppingJobs = new Set();
let crawlRequest = 0;
let crawlTimer;
let startingHistory = false;
let savingSchedule = false;
let scheduleEdited = false;

function ongoing(job) {
  return job.status === "queued" || job.status === "running";
}
const beijingClock = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23"
});
function crawlTime(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  return beijingClock.format(new Date(seconds * 1000));
}
function scheduleDateValue(seconds) {
  if (!Number.isFinite(seconds)) return "";
  const parts = Object.fromEntries(beijingClock.formatToParts(new Date(seconds * 1000)).map(p => [p.type, p.value]));
  return parts.year + "-" + parts.month + "-" + parts.day + "T" + parts.hour + ":" + parts.minute;
}
function scheduleStartSeconds() {
  // Preserve seconds when the operator only changes the interval or switch.
  if (scheduleStart.value === scheduleDateValue(crawlSettings?.start_at)) return crawlSettings?.start_at;
  return Date.parse(scheduleStart.value + "+08:00") / 1000;
}
function renderCrawlAction() {
  const entry = currentEntry();
  const button = byId("start-history");
  const pending = crawlJobs?.some(job => job.author === entry?.name && job.mode === "history" && ongoing(job));
  button.hidden = !entry;
  button.disabled = Boolean(pending || startingHistory);
  button.textContent = pending ? "历史任务进行中" : "拉取历史作品";
}
function renderCrawls() {
  const jobs = crawlJobs || [];
  const running = jobs.filter(ongoing);
  const cadence = crawlSettings?.enabled ? "每 " + crawlSettings.interval_minutes + " 分钟拉取新增" : "定时采集已关闭";
  const latest = jobs[0];
  let summary = cadence;
  if (running.length) summary = running.length + " 个任务进行中";
  else if (latest && ["failed", "partial"].includes(latest.status)) summary = "上次采集" + jobLabels[latest.status];
  if (workerOnline === false) summary = "采集进程离线 · " + summary;
  byId("crawl-summary").textContent = summary;
  byId("worker-offline").hidden = workerOnline !== false;
  const next = crawlTime(crawlSettings?.next_run);
  byId("next-crawl").textContent = crawlSettings?.enabled ?
    (next ? "定时已开启 · 下次运行（北京时间）" + next : "定时已开启") : "定时已关闭";
  if (!scheduleEdited && crawlSettings) {
    scheduleEnabled.checked = crawlSettings.enabled;
    scheduleInterval.value = crawlSettings.interval_minutes;
    scheduleStart.value = scheduleDateValue(crawlSettings.start_at);
  }
  scheduleEnabled.disabled = scheduleInterval.disabled = scheduleStart.disabled = savingSchedule || !crawlSettings;
  scheduleSave.disabled = !scheduleEdited || savingSchedule || !scheduleInterval.validity.valid || !scheduleStart.validity.valid;
  byId("crawl-jobs").replaceChildren(...jobs.map(job => {
    const row = element("li", "crawl-job");
    row.append(element("strong", "", job.author), element("span", "", job.mode === "history" ? "历史作品" : "增量作品"),
      element("span", "", jobLabels[job.status] || job.status), element("span", "", crawlTime(job.created_at)),
      element("span", "", "发现 " + job.discovered + " · 已保存 " + job.saved + " · 已跳过 " + job.skipped + " · 失败 " + job.failed));
    if (ongoing(job)) {
      const stopping = stoppingJobs.has(job.id) || job.cancel_requested;
      const button = element("button", "cancel-crawl", stopping ? "正在停止…" : "停止");
      button.type = "button";
      button.disabled = Boolean(stopping);
      button.title = "保留已保存的视频，正在处理的作品可能先完成";
      button.setAttribute("aria-label", "停止「" + job.author + "」的采集任务");
      button.addEventListener("click", () => stopJob(job));
      row.append(button);
    }
    if (job.error) row.append(element("span", ["failed", "partial"].includes(job.status) ? "job-error" : "job-info", job.error));
    if (job.item_error && !job.error?.includes(job.item_error)) {
      row.append(element("span", "job-error", "最近一次作品处理失败：" + job.item_error));
    }
    return row;
  }));
  byId("no-crawl-jobs").hidden = jobs.length > 0;
  renderCrawlAction();
}
async function loadCrawls() {
  clearTimeout(crawlTimer);
  const request = ++crawlRequest;
  try {
    const response = await fetch("/api/crawls", { cache: "no-store" });
    if (!response.ok) throw new Error();
    const data = await response.json();
    if (request !== crawlRequest) return;
    if (!Array.isArray(data.jobs) || !data.settings) throw new Error();
    const finished = new Set(crawlJobs?.filter(job => !ongoing(job)).map(job => job.id));
    if (crawlJobs && data.jobs.some(job => !ongoing(job) && !finished.has(job.id))) loadDashboard();
    crawlJobs = data.jobs;
    crawlSettings = data.settings;
    workerOnline = data.worker_online;
    renderCrawls();
  } catch {
    if (request === crawlRequest) byId("crawl-summary").textContent = "暂时无法读取采集任务";
  } finally {
    if (request === crawlRequest && !document.hidden) {
      crawlTimer = setTimeout(loadCrawls, crawlJobs?.some(ongoing) ? 4000 : 30000);
    }
  }
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearTimeout(crawlTimer);
  else loadCrawls();
});
crawlPanel.addEventListener("toggle", () => { if (crawlPanel.open) loadCrawls(); });
async function stopJob(job) {
  if (stoppingJobs.has(job.id)) return;
  stoppingJobs.add(job.id);
  setError("crawl-error", "");
  renderCrawls();
  try {
    const response = await fetch("/api/crawls/" + encodeURIComponent(job.id) + "/cancel", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}"
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "停止任务失败。");
    notice(job.status === "queued" ? "已请求停止排队任务" : "已请求停止。当前作品可能先完成，已保存的视频会保留。");
  } catch (error) {
    setError("crawl-error", error instanceof TypeError ? "连接失败，请稍后重试。" : error.message);
  } finally {
    await loadCrawls();
    stoppingJobs.delete(job.id);
    renderCrawls();
  }
}
byId("start-history").addEventListener("click", async () => {
  const entry = currentEntry();
  if (!entry || startingHistory) return;
  if (!entry.url) {
    openEditor(true);
    byId("author-search-status").textContent = "请先关联主页并保存，再拉取历史作品。";
    return;
  }
  startingHistory = true;
  renderCrawlAction();
  crawlPanel.open = true;
  setError("crawl-error", "");
  try {
    const response = await fetch("/api/crawls", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: entry.name, version: entry.version })
    });
    const data = await response.json();
    if (!response.ok) {
      if ([404, 409].includes(response.status)) loadDashboard();
      throw new Error(data.error || "未能启动历史采集任务。");
    }
    notice("已提交「" + entry.name + "」的历史采集任务");
  } catch (error) {
    setError("crawl-error", error instanceof TypeError ? "连接失败，请稍后重试。" : error.message);
  } finally {
    await loadCrawls();
    startingHistory = false;
    renderCrawlAction();
  }
});
byId("schedule-form").addEventListener("input", () => {
  scheduleEdited = Boolean(crawlSettings && (scheduleEnabled.checked !== crawlSettings.enabled ||
    scheduleInterval.valueAsNumber !== crawlSettings.interval_minutes || scheduleStartSeconds() !== crawlSettings.start_at));
  scheduleSave.disabled = !scheduleEdited || !scheduleInterval.validity.valid || !scheduleStart.validity.valid;
});
byId("schedule-form").addEventListener("submit", async event => {
  event.preventDefault();
  const startAt = scheduleStartSeconds();
  if (!scheduleEdited || savingSchedule || !scheduleInterval.validity.valid || !scheduleStart.validity.valid || !Number.isFinite(startAt)) return;
  const body = { enabled: scheduleEnabled.checked, interval_minutes: scheduleInterval.valueAsNumber, start_at: startAt };
  savingSchedule = true;
  renderCrawls();
  setError("crawl-error", "");
  try {
    const response = await fetch("/api/crawl-settings", {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "保存定时设置失败。");
    crawlSettings = data.settings;
    scheduleEdited = false;
    notice("已保存定时采集设置");
  } catch (error) {
    setError("crawl-error", error instanceof TypeError ? "连接失败，请稍后重试。" : error.message);
  } finally {
    savingSchedule = false;
    renderCrawls();
    loadCrawls();
  }
});

scheduleEnabled.disabled = scheduleInterval.disabled = scheduleStart.disabled = true;
loadDashboard();
loadCrawls();
