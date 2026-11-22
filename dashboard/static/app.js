"use strict";

const byId = (id) => document.getElementById(id);
const state = { authors: [], entries: [], profiles: [], videos: [], author: null, loaded: false };
const collator = new Intl.Collator("zh-CN", { numeric: true });
const player = byId("player");
const playerDialog = byId("player-dialog");
const analysisHash = (key) => "#analysis/" + key;
let routedVideo = null;
let pendingLoad = 0;
let noticeTimer;
let addingAuthor = false;
let searchController = null;
let selectedAccount = null;
let editingEntry = null;
let replacement = null;
let removingEntry = null;
let choosingReplacement = false;
let searchKind = "video";
let editKind = "video";
let removeKind = "video";
let expectedSearchBiz = null;
let activeView = "main";

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

function videoAccountHash(author) {
  return author === null ? "#main" : "#main/" + encodeURIComponent(author);
}

function selectAuthor(author) {
  window.location.hash = videoAccountHash(author);
  setNavOpen(false);
  if (window.matchMedia("(max-width: 720px)").matches) byId("nav-toggle").focus();
}

function setCurrentNavItem(node, active) {
  node.classList.toggle("current", active);
  if (active) node.setAttribute("aria-current", "page");
  else node.removeAttribute("aria-current");
}

function navAccountButton(name, count, selected, select) {
  const node = element("button", "nav-item");
  node.type = "button";
  node.title = name;
  setCurrentNavItem(node, selected);
  node.append(avatar(name), element("span", "nav-name", name), element("span", "nav-count", count));
  node.addEventListener("click", select);
  return node;
}

function accountShortcuts(rows) {
  return rows.slice().sort(window.accountDirectory.compare).slice(0, 5);
}

function getVideoAccountRows() {
  const counts = new Map();
  for (const video of state.videos) counts.set(video.author, (counts.get(video.author) || 0) + 1);
  const activeAuthors = new Set(state.authors);
  const entries = new Map(state.entries.map(entry => [entry.name, entry]));
  const row = name => {
    const entry = entries.get(name);
    return { id: name, name, count: counts.get(name) || 0, note: entry?.note || "", description: "", entry };
  };
  return {
    monitored: state.authors.map(row),
    archived: Array.from(counts.keys()).filter(name => !activeAuthors.has(name)).map(row)
  };
}

function renderNav() {
  const { monitored, archived } = getVideoAccountRows();
  const isMonitored = monitored.some(row => row.id === state.author);
  const isArchived = archived.some(row => row.id === state.author);
  if (state.loaded && state.author !== null && !isMonitored && !isArchived) {
    state.author = null;
    if (window.location.hash.startsWith("#main/")) history.replaceState(null, "", "#main");
  }
  const entry = currentEntry();
  const profile = entry || state.profiles.find(item => item?.name === state.author);
  const profileLink = byId("saved-profile");
  const href = accountLink(profile?.url);
  profileLink.hidden = !href;
  byId("link-profile").hidden = Boolean(href) || !isMonitored;
  byId("edit-author").hidden = !entry;
  byId("remove-author").hidden = !entry;
  byId("remonitor-author").hidden = !isArchived;
  byId("saved-note").hidden = !entry?.note;
  byId("saved-note").textContent = entry?.note || "";
  renderCrawlAction();
  if (href) profileLink.href = href;
  else profileLink.removeAttribute("href");
  setCurrentNavItem(byId("all-videos"), activeView === "main" && state.author === null);
  setCurrentNavItem(byId("open-video-accounts"), activeView === "video-accounts");
  byId("all-count").textContent = String(state.videos.length);
  const authors = byId("authors");
  const accountRow = ({name, count}) => navAccountButton(name, count,
    activeView === "main" && name === state.author, () => selectAuthor(name));
  authors.replaceChildren(...accountShortcuts(monitored).map(accountRow));
  if (!monitored.length) authors.append(element("p", "rail-empty", "名单为空"));
  if (activeView === "video-accounts") window.accountDirectory.render("video", monitored, archived);
  byId("storage").hidden = false;
  byId("storage-size").textContent = bytesLabel(state.videos.reduce((sum, item) => sum + (Number(item.size) || 0), 0));
}

function showPlayerView(view) {
  const analysis = view === "analysis";
  if (analysis) player.pause();
  byId("player-media").hidden = analysis;
  byId("video-analysis").hidden = !analysis;
  byId("player-view-media").setAttribute("aria-pressed", String(!analysis));
  byId("player-view-analysis").setAttribute("aria-pressed", String(analysis));
  playerDialog.scrollTop = 0;
}

function openVideo(video, analysis = false) {
  const title = video.title || "未命名视频";
  const url = sameSiteURL(video.url);
  byId("playing-title").textContent = title;
  byId("playing-meta").textContent = [video.author, video.published, bytesLabel(Number(video.size))].filter(Boolean).join(" · ");
  byId("player-error").hidden = Boolean(url);
  const poster = sameSiteURL(video.poster);
  if (poster) player.poster = poster;
  else player.removeAttribute("poster");
  player.pause();
  if (url) player.src = url;
  else {
    player.removeAttribute("src");
    player.load();
  }
  showPlayerView(analysis ? "analysis" : "media");
  if (!playerDialog.open) playerDialog.showModal();
  playerDialog.dataset.videoKey = video.id;
  playerDialog.dispatchEvent(new CustomEvent("video-open", { detail: video.id }));
  if (analysis) byId("player-view-analysis").focus();
  else if (url) player.play().catch(() => { /* Native controls allow manual playback. */ });
}

function openRoutedAnalysis() {
  const key = /^#analysis\/([0-9a-f]{64})$/.exec(window.location.hash)?.[1];
  if (!key) {
    if (routedVideo) {
      routedVideo = null;
      playerDialog.close();
    }
    return;
  }
  if (!state.loaded || (routedVideo === key && playerDialog.open)) return;
  const video = state.videos.find(item => item.id === key);
  if (!video) {
    if (routedVideo) playerDialog.close();
    removeAnalysisHash();
    notice("资料库中找不到这条视频。");
    return;
  }
  routedVideo = key;
  openVideo(video, true);
}

function removeAnalysisHash() {
  history.replaceState(history.state, "", window.location.pathname + window.location.search);
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
  const item = element("article", "video-item");
  const analysisLink = element("a", "video-card-analysis", "视频解析");
  analysisLink.href = analysisHash(video.id);
  analysisLink.setAttribute("aria-label", "视频解析：" + title);
  item.append(card, analysisLink);
  return item;
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
  if (activeView === "main") renderVideos();
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
    openRoutedAnalysis();
    return true;
  } catch {
    if (version !== pendingLoad) return;
    const message = "无法读取资料库，请确认服务和外置盘可用。";
    setError("fetch-error", message);
    if (activeView === "video-accounts") {
      const { monitored, archived } = getVideoAccountRows();
      window.accountDirectory.render("video", monitored, archived, { error: message });
    }
    if (!state.loaded) byId("summary").textContent = "资料库暂不可用";
  } finally {
    if (version === pendingLoad) {
      byId("refresh").disabled = false;
      byId("videos").setAttribute("aria-busy", "false");
    }
  }
}

byId("all-videos").addEventListener("click", () => setNavOpen(false));
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

byId("player-view-media").addEventListener("click", () => showPlayerView("media"));
byId("player-view-analysis").addEventListener("click", () => showPlayerView("analysis"));
player.addEventListener("error", () => { if (playerDialog.open) byId("player-error").hidden = false; });
playerDialog.addEventListener("close", () => {
  if (playerDialog.open) return;
  if (routedVideo && window.location.hash === analysisHash(routedVideo)) removeAnalysisHash();
  routedVideo = null;
  delete playerDialog.dataset.videoKey;
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

const accountTypes = {
  video: { label: "视频号", content: "视频", search: "/api/author-search", accounts: "/api/authors" },
  public: { label: "公众号", content: "文章", search: "/api/public/search", accounts: "/api/public/accounts" }
};

const accountManagers = {
  video: {
    refresh: loadDashboard,
    hasVersion: entry => state.entries.some(item => item.name === entry.name && item.version === entry.version),
    async onChanged(method, data, entry) {
      await loadDashboard();
      const name = data.entry?.name || entry.name;
      const showArchive = state.videos.some(video => video.author === name);
      selectAuthor(method === "DELETE" && !showArchive ? null : name);
    }
  }
};

window.accountDirectoryActions = {
  add: openAccountSearch,
  edit: openAccountEditor,
  remove: openAccountRemoval,
  restore(kind, row) {
    openAccountSearch(kind, { query: row.name, expectedBiz: kind === "public" ? row.entry.biz : null });
  },
  refresh: kind => accountManagers[kind].refresh()
};

function accountChanged(kind, method, data, entry) {
  const directory = kind === "video" ? "video-accounts" : "official-accounts";
  return activeView === directory
    ? accountManagers[kind].refresh()
    : accountManagers[kind].onChanged(method, data, entry);
}

function showCandidate(candidate) {
  if (!candidate || typeof candidate.name !== "string" || !candidate.name.trim() ||
      typeof candidate.token !== "string" || !candidate.token) return;
  const href = accountLink(candidate.url);
  if (searchKind === "video" && !href) return;
  if (searchKind === "public" && (!candidate.biz || (expectedSearchBiz && candidate.biz !== expectedSearchBiz))) return;
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
  card.append(label);
  if (href) {
    const link = element("a", "account-link");
    link.append(element("span", "", "视频号主页"), element("span", "account-url", href));
    link.href = href;
    link.title = href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.setAttribute("aria-label", "查看「" + candidate.name + "」的视频号主页");
    card.append(link);
  }
  accountList.append(card);
}

function openAccountSearch(kind, { query = "", forEdit = false, expectedBiz = null } = {}) {
  const type = accountTypes[kind];
  searchKind = kind;
  expectedSearchBiz = expectedBiz;
  choosingReplacement = forEdit;
  byId("author-dialog-title").textContent = (forEdit ? "选择监控" : "添加监控") + type.label;
  byId("author-search-help").textContent = kind === "public" ? "输入公众号全称，在微信搜索结果中核对名称和主体后选择。" :
    "用微信搜索账号，选择要监控的视频号。主页链接可在微信内打开。";
  authorDialog.querySelector('label[for="author-name"]').textContent = type.label + "名称";
  accountOptions.querySelector("legend").textContent = "选择" + type.label;
  addButton.textContent = forEdit ? "使用此" + type.label : "加入监控名单";
  authorQuery.placeholder = kind === "public" ? "例如：三联生活周刊" : "例如：数字生命卡兹克";
  authorQuery.value = query;
  resetAccountSearch();
  authorDialog.showModal();
  authorQuery.focus();
}
byId("add-author").addEventListener("click", () => openAccountSearch("video"));
byId("link-profile").addEventListener("click", () => {
  const entry = currentEntry();
  if (entry) openAccountEditor("video", entry, true);
  else openAccountSearch("video", { query: state.author });
});
byId("remonitor-author").addEventListener("click", () => openAccountSearch("video", { query: state.author }));
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
    const response = await fetch(accountTypes[searchKind].search + "?" + params, {
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
      (expectedSearchBiz ? "未找到与历史记录身份一致的公众号。" : "未找到可核实的" + accountTypes[searchKind].label + "，请检查完整名称。");
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
    const response = await fetch(accountTypes[searchKind].accounts, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: account.token })
    });
    const data = await response.json();
    if (!response.ok) {
      if ([400, 404, 409, 410].includes(response.status)) resetAccountSearch();
      throw new Error(typeof data.error === "string" ? data.error : "添加失败，请重试。");
    }
    authorDialog.close();
    if (data.linked) notice("已为「" + account.name + "」关联视频号主页");
    else notice(data.added === false ? "该" + accountTypes[searchKind].label + "已在监控名单中" : "已添加「" + account.name + "」");
    await accountChanged(searchKind, "POST", data, account);
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
  const subject = byId("edit-account-subject");
  subject.textContent = editKind === "public" ? (shown.description || "") : "";
  subject.hidden = !subject.textContent;
  byId("choose-replacement").textContent = editKind === "video" && !url ? "关联主页" : "更换账号";
  byId("save-author").disabled = !replacement && noteInput.value === editingEntry.note;
}

function openAccountEditor(kind, entry, choose = false) {
  if (!entry) return;
  editKind = kind;
  editingEntry = entry;
  byId("edit-title").textContent = "编辑监控" + accountTypes[kind].label;
  replacement = null;
  noteInput.value = editingEntry.note;
  setError("edit-error", "");
  renderEditor();
  editDialog.showModal();
  if (choose) openAccountSearch(kind, { query: entry.name, forEdit: true });
  else noteInput.focus();
}
byId("edit-author").addEventListener("click", () => openAccountEditor("video", currentEntry()));
byId("choose-replacement").addEventListener("click", () =>
  openAccountSearch(editKind, { query: (replacement || editingEntry).name, forEdit: true }));
editDialog.addEventListener("close", () => { editingEntry = replacement = null; });
noteInput.addEventListener("input", () => {
  setError("edit-error", "");
  renderEditor();
});
function openAccountRemoval(kind, entry) {
  if (!entry) return;
  removeKind = kind;
  removingEntry = entry;
  byId("remove-description").textContent = "将停止监控「" + entry.name + "」。已保存的" + accountTypes[kind].content + "会保留在资料库中。";
  setError("remove-error", "");
  removeDialog.showModal();
}
byId("remove-author").addEventListener("click", () => openAccountRemoval("video", currentEntry()));
removeDialog.addEventListener("close", () => { removingEntry = null; });

async function changeEntry(method, entry, changes) {
  const editing = method === "PATCH";
  const kind = editing ? editKind : removeKind;
  const manager = accountManagers[kind];
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
    const base = accountTypes[kind].accounts;
    const key = kind === "public" ? entry.id : entry.name;
    const url = key === "." || key === ".."
      ? base + "?" + new URLSearchParams({ name: key }) : base + "/" + encodeURIComponent(key);
    const response = await fetch(url, {
      method, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ version: entry.version, ...changes })
    });
    const data = await response.json();
    if (!response.ok) {
      const message = data.error || "操作失败，请重试。";
      if ([404, 409].includes(response.status) && await manager.refresh() && !manager.hasVersion(entry)) {
        dialog.close();
        notice(message + " 已刷新监控名单。");
        return;
      }
      throw new Error(message);
    }
    dialog.close();
    const saved = data.entry || data.account;
    notice(editing ? "已保存「" + saved.name + "」的更改" : "已从监控名单移除「" + entry.name + "」");
    await accountChanged(kind, method, data, entry);
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

const settingsForm = byId("settings-form");
const settingsFields = byId("settings-fields");
const storagePath = byId("storage-path");
const settingsTextFields = [storagePath, byId("model-api-url"), byId("model-name")];
const modelKey = byId("model-api-key");
const clearModelKey = byId("clear-model-key");
const saveSettings = byId("save-settings");
let savedSettings = null;
let settingsOperation = null;

function settingsChanges() {
  const changes = {};
  for (const field of settingsTextFields) {
    const value = field === storagePath ? field.value : field.value.trim();
    if (value !== savedSettings[field.name]) changes[field.name] = value;
  }
  if (clearModelKey.checked) changes.model_api_key = "";
  else if (modelKey.value.trim()) changes.model_api_key = modelKey.value.trim();
  return changes;
}

function renderSettingsControls() {
  const busy = settingsOperation !== null;
  settingsFields.disabled = busy || !savedSettings;
  settingsForm.setAttribute("aria-busy", String(busy));
  modelKey.disabled = clearModelKey.checked;
  saveSettings.disabled = busy || !savedSettings || Object.keys(settingsChanges()).length === 0;
  saveSettings.textContent = settingsOperation === "saving" ? "正在保存…" : "保存设置";
}

function fillSettings(data) {
  savedSettings = data;
  for (const field of settingsTextFields) field.value = data[field.name];
  byId("settings-root-value").textContent = data.storage_root;
  byId("settings-space-value").textContent = bytesLabel(data.storage_available_bytes) || "暂时无法读取";
  modelKey.value = "";
  clearModelKey.checked = false;
  byId("clear-model-key-option").hidden = !data.model_api_key_configured;
  byId("model-key-hint").textContent = data.model_api_key_configured
    ? "已配置。留空可保留已保存的 Key。"
    : "尚未配置。";
}

async function settingsResponse(response, fallback) {
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(data?.error || fallback);
  if (!data || settingsTextFields.some(field => typeof data[field.name] !== "string") ||
      typeof data.storage_root !== "string" || typeof data.model_api_key_configured !== "boolean") {
    throw new Error(fallback);
  }
  return data;
}

async function loadSettings() {
  if (settingsOperation) return;
  settingsOperation = "loading";
  settingsForm.hidden = true;
  byId("settings-loading").hidden = false;
  byId("retry-settings").hidden = true;
  byId("settings-status").textContent = "";
  setError("settings-load-error", "");
  setError("settings-error", "");
  renderSettingsControls();
  try {
    const response = await fetch("/api/settings", { cache: "no-store" });
    fillSettings(await settingsResponse(response, "暂时无法读取设置。"));
    settingsForm.hidden = false;
  } catch (error) {
    setError("settings-load-error", error instanceof TypeError ? "连接失败，请稍后重试。" : error.message);
    byId("retry-settings").hidden = false;
  } finally {
    settingsOperation = null;
    byId("settings-loading").hidden = true;
    renderSettingsControls();
  }
}

settingsForm.addEventListener("input", () => {
  byId("settings-status").textContent = "";
  setError("settings-error", "");
  renderSettingsControls();
});
settingsForm.addEventListener("submit", async event => {
  event.preventDefault();
  if (saveSettings.disabled) return;
  const changes = settingsChanges();
  settingsOperation = "saving";
  byId("settings-status").textContent = "";
  setError("settings-error", "");
  renderSettingsControls();
  try {
    const response = await fetch("/api/settings", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(changes)
    });
    fillSettings(await settingsResponse(response, "保存设置失败。"));
    byId("settings-status").textContent = "已保存设置";
  } catch (error) {
    setError("settings-error", error instanceof TypeError ? "连接失败，请稍后重试。" : error.message);
  } finally {
    settingsOperation = null;
    renderSettingsControls();
  }
});
byId("retry-settings").addEventListener("click", loadSettings);

const directoryDialog = byId("directory-dialog");
const directoryLocation = byId("directory-location");
const directoryBreadcrumbs = byId("directory-breadcrumbs");
const directoryFilter = byId("directory-filter");
const directoryItems = byId("directory-items");
const directoryStatus = byId("directory-status");
const directoryParent = byId("directory-parent");
const directoryRetry = byId("retry-directory");
const selectDirectory = byId("select-directory");
let currentDirectory;
let directoryRequest;
let directoryQuery = "";

function renderDirectoryRows() {
  const query = directoryFilter.value.trim().toLocaleLowerCase();
  const names = currentDirectory?.directories.filter(name => name.toLocaleLowerCase().includes(query)) || [];
  const rows = document.createDocumentFragment();
  for (const name of names) {
    const item = element("li");
    const button = element("button", "directory-row");
    button.type = "button";
    const icon = element("span", "folder-icon");
    const chevron = element("span", "chevron");
    icon.setAttribute("aria-hidden", "true");
    chevron.setAttribute("aria-hidden", "true");
    button.append(icon, element("span", "", name), chevron);
    button.addEventListener("click", () => browseDirectory(currentDirectory.path + "/" + name));
    item.append(button);
    rows.append(item);
  }
  directoryItems.replaceChildren(rows);
  directoryItems.hidden = names.length === 0;
  directoryStatus.textContent = names.length ? names.length + " 个子目录" :
    (query ? "没有匹配的子目录。" : "此目录没有子目录。");
}

function renderDirectoryLocation(data) {
  const parts = data.path === data.root ? [] : data.path.slice(data.root.length + 1).split("/");
  let path = data.root;
  const crumbs = ["存储根目录", ...parts].map((name, index) => {
    const item = element("li");
    const destination = path;
    if (index === parts.length) {
      const current = element("span", "", name);
      current.setAttribute("aria-current", "location");
      item.append(current);
    } else {
      const button = element("button", "", name);
      button.type = "button";
      button.addEventListener("click", () => browseDirectory(destination));
      item.append(button);
      path += "/" + parts[index];
    }
    return item;
  });
  directoryBreadcrumbs.replaceChildren(...crumbs);
  directoryBreadcrumbs.hidden = false;
  directoryLocation.textContent = data.path;
  directoryLocation.hidden = false;
}

function setDirectoryBusy(busy) {
  directoryItems.setAttribute("aria-busy", String(busy));
  directoryParent.disabled = busy || !currentDirectory?.parent;
  directoryFilter.disabled = busy || !currentDirectory;
  selectDirectory.disabled = busy || !currentDirectory?.can_select;
}

async function browseDirectory(path, useRootIfMissing = false) {
  directoryRequest?.abort();
  const request = new AbortController();
  directoryRequest = request;
  directoryQuery = path;
  const focusLocation = currentDirectory || document.activeElement === directoryRetry;
  setDirectoryBusy(true);
  setError("directory-error", "");
  const note = byId("directory-note");
  note.hidden = true;
  directoryRetry.hidden = true;
  directoryStatus.textContent = "正在读取目录…";
  const read = candidate => {
    const query = candidate ? "?" + new URLSearchParams({ path: candidate }) : "";
    return fetch("/api/settings/directories" + query, { signal: request.signal, cache: "no-store" });
  };
  try {
    let response = await read(path);
    const fellBack = path && useRootIfMissing && (response.status === 400 || response.status === 404);
    if (fellBack) {
      directoryQuery = "";
      response = await read("");
    }
    const data = await response.json().catch(() => null);
    if (request.signal.aborted || !directoryDialog.open) return;
    if (!response.ok) throw new Error(data?.error || "暂时无法读取目录。");
    if (typeof data?.root !== "string" || typeof data.path !== "string" ||
        (data.path !== data.root && !data.path.startsWith(data.root + "/")) ||
        (data.parent !== null && typeof data.parent !== "string") || typeof data.can_select !== "boolean" ||
        !Array.isArray(data.directories) || data.directories.some(name => typeof name !== "string" || !name || name.includes("/"))) {
      throw new Error("返回的目录信息有误，请重新读取。");
    }
    data.directories.sort(collator.compare);
    currentDirectory = data;
    directoryFilter.value = "";
    renderDirectoryLocation(data);
    renderDirectoryRows();
    directoryItems.scrollTop = 0;
    if (fellBack) {
      note.textContent = "原目录暂不可浏览，已打开存储根目录。";
      note.hidden = false;
    }
    if (focusLocation) directoryLocation.focus({ preventScroll: true });
  } catch (error) {
    if (request.signal.aborted || !directoryDialog.open) return;
    if (currentDirectory) renderDirectoryRows();
    else directoryStatus.textContent = "";
    setError("directory-error", error instanceof TypeError ? "连接失败，请重新读取。" : error.message);
    directoryRetry.hidden = false;
  } finally {
    if (!request.signal.aborted && directoryDialog.open) setDirectoryBusy(false);
  }
}

byId("browse-storage").addEventListener("click", () => {
  currentDirectory = null;
  directoryBreadcrumbs.hidden = directoryLocation.hidden = directoryItems.hidden = true;
  directoryFilter.value = "";
  directoryDialog.showModal();
  browseDirectory(storagePath.value, true);
});
directoryDialog.addEventListener("close", () => {
  if (!directoryDialog.open) directoryRequest?.abort();
});
directoryParent.addEventListener("click", () => browseDirectory(currentDirectory.parent));
directoryRetry.addEventListener("click", () => browseDirectory(directoryQuery, !currentDirectory));
directoryFilter.addEventListener("input", renderDirectoryRows);
directoryItems.addEventListener("keydown", event => {
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
  const row = event.target.closest("li");
  const next = event.key === "ArrowDown" ? row?.nextElementSibling : row?.previousElementSibling;
  if (next) {
    event.preventDefault();
    next.querySelector("button").focus();
  }
});
selectDirectory.addEventListener("click", () => {
  storagePath.value = currentDirectory.path;
  storagePath.dispatchEvent(new Event("input", { bubbles: true }));
  directoryDialog.close();
});

const logForm = byId("log-filters");
const logRange = byId("log-range");
const logStart = byId("log-start");
const logEnd = byId("log-end");
const logStatus = byId("log-status");
const logEntries = byId("log-entries");
const logCategories = { system: "系统与部署", crawl: "视频采集", search: "账号搜索" };
const logLevels = { info: "信息", warning: "警告", error: "错误" };
const logClock = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"
});
let logRequest;
let lastLogWindow;

function logWindow() {
  if (logRange.value === "custom") {
    const start = Date.parse(logStart.value + "+08:00") / 1000;
    const end = Date.parse(logEnd.value + "+08:00") / 1000 + 59;
    if (!logStart.validity.valid || !logEnd.validity.valid || !Number.isFinite(start) || !Number.isFinite(end)) {
      throw new Error("请填写有效的开始时间和结束时间。");
    }
    if (start > end) throw new Error("开始时间应早于或等于结束时间。");
    return { start, end };
  }
  const end = Math.floor(Date.now() / 1000);
  return { start: end - Number(logRange.value), end };
}

function logEntry(record) {
  const date = new Date(record.time * 1000);
  const time = element("time", "", logClock.format(date));
  time.dateTime = date.toISOString();
  const meta = element("div", "log-entry-meta");
  meta.append(time, element("span", "", logCategories[record.category]),
    element("span", "log-level " + record.level, logLevels[record.level]));
  const row = element("li", "log-entry");
  row.append(meta, element("p", "log-message", record.message));
  return row;
}

async function loadLogs() {
  logRequest?.abort();
  const request = new AbortController();
  logRequest = request;
  logEntries.hidden = true;
  logEntries.replaceChildren();
  setError("log-error", "");
  logStatus.textContent = "正在读取日志…";
  try {
    const bounds = logWindow();
    const category = logForm.elements["log-category"].value;
    const query = new URLSearchParams({ category, ...bounds });
    const response = await fetch("/api/logs?" + query, { cache: "no-store", signal: request.signal });
    const data = await response.json().catch(() => null);
    if (request.signal.aborted) return;
    if (!response.ok) throw new Error(data?.error || "无法读取日志，请再次查询。");
    if (!Array.isArray(data?.entries) || typeof data.truncated !== "boolean" || data.entries.some(record => !record ||
        !Number.isFinite(record.time) || !Object.hasOwn(logCategories, record.category) ||
        !Object.hasOwn(logLevels, record.level) || typeof record.message !== "string")) {
      throw new Error("返回的日志格式有误，请再次查询。");
    }
    lastLogWindow = bounds;
    logEntries.replaceChildren(...data.entries.map(logEntry));
    logEntries.hidden = data.entries.length === 0;
    logStatus.textContent = data.truncated ? "显示最新 " + data.entries.length + " 条。缩小时间范围可查看更早的记录。" :
      (data.entries.length ? data.entries.length + " 条日志" : "所选时段没有日志。");
  } catch (error) {
    if (request.signal.aborted) return;
    logStatus.textContent = "";
    setError("log-error", error instanceof TypeError ? "连接失败，请再次查询。" : error.message);
  }
}

logForm.addEventListener("submit", event => {
  event.preventDefault();
  loadLogs();
});
logForm.addEventListener("change", event => {
  if (event.target === logRange) {
    const custom = logRange.value === "custom";
    byId("log-custom").hidden = byId("log-custom").disabled = !custom;
    if (custom) {
      const now = Math.floor(Date.now() / 1000);
      const { start, end } = lastLogWindow || { start: now - 86400, end: now };
      if (!logStart.value) logStart.value = scheduleDateValue(start);
      if (!logEnd.value) logEnd.value = scheduleDateValue(end);
      return;
    }
  } else if (event.target.name !== "log-category") {
    return;
  }
  loadLogs();
});

const viewTitles = {
  main: "视频资料库", public: "公众号文章", settings: "设置", logs: "日志",
  "video-accounts": "监控视频号", "official-accounts": "监控公众号"
};

function viewPanel(view) {
  return byId(view === "video-accounts" || view === "official-accounts" ? "account-directory" : view);
}

function focusView(view) {
  const panel = viewPanel(view);
  (panel.id === "account-directory" ? byId("account-directory-title") : panel).focus();
}

function showCurrentView(focus = false) {
  const hash = window.location.hash;
  const target = hash.slice(1).split("/", 1)[0];
  const view = Object.hasOwn(viewTitles, target) ? target : "main";
  if (view === "main" && (target === "main" || !target)) {
    try {
      state.author = hash.startsWith("#main/") ? decodeURIComponent(hash.slice(6)) || null : null;
    } catch {
      state.author = null;
      history.replaceState(null, "", "#main");
    }
  }
  if (view !== activeView) {
    if (activeView === "settings") {
      if (directoryDialog.open) directoryDialog.close();
      modelKey.value = "";
      clearModelKey.checked = false;
    }
    if (activeView === "logs") logRequest?.abort();
    activeView = view;
    const panel = viewPanel(view);
    for (const page of document.querySelectorAll(".workspace > .main")) page.hidden = page !== panel;
    document.querySelector(".skip-link").href = "#" + panel.id;
    document.title = viewTitles[view] + " · 微信监控台";
    if (view === "settings") loadSettings();
    if (view === "logs") loadLogs();
    if (view === "video-accounts") {
      if (!state.loaded) window.accountDirectory.render("video", [], [], { loading: true });
      if (focus) loadDashboard();
    }
  }
  setNavOpen(false);
  for (const name of ["public", "settings", "logs", "video-accounts", "official-accounts"]) {
    setCurrentNavItem(byId("open-" + name), hash === "#" + name);
  }
  if (state.loaded) {
    renderNav();
    if (view === "main") renderVideos();
  } else setCurrentNavItem(byId("all-videos"), view === "main" && state.author === null);
  document.dispatchEvent(new CustomEvent("dashboard-view", { detail: view }));
  if (focus && hash !== "#crawl-panel") focusView(view);
  if (hash === "#crawl-panel") {
    crawlPanel.open = true;
    if (focus) crawlPanel.querySelector("summary").focus();
  }
  openRoutedAnalysis();
}

document.querySelector(".skip-link").addEventListener("click", event => {
  event.preventDefault();
  focusView(activeView);
});
for (const view of ["public", "settings", "logs", "video-accounts", "official-accounts"]) byId("open-" + view).addEventListener("click", () => {
  if (activeView === view) {
    setNavOpen(false);
    focusView(view);
  }
});
window.addEventListener("hashchange", () => showCurrentView(true));
showCurrentView();
loadDashboard();
loadCrawls();
