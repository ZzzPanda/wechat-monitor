"use strict";

const byId = (id) => document.getElementById(id);
const state = { authors: [], profiles: [], videos: [], author: null, loaded: false };
const collator = new Intl.Collator("zh-CN", { numeric: true });
const player = byId("player");
let pendingLoad = 0;
let noticeTimer;
let addingAuthor = false;
let searchController = null;
let selectedAccount = null;

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
  const profile = state.profiles.find(item => item?.name === state.author);
  const profileLink = byId("saved-profile");
  const href = accountLink(profile?.url);
  profileLink.hidden = !href;
  byId("link-profile").hidden = Boolean(href) || !activeAuthors.has(state.author);
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
    state.profiles = Array.isArray(data.profiles) ? data.profiles : [];
    state.videos = data.videos;
    state.loaded = true;
    render();
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
  dialog.addEventListener("click", event => {
    if (event.target !== dialog || (dialog.id === "author-dialog" && addingAuthor)) return;
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
const addButton = byId("submit-author");

function resetAccountSearch() {
  if (searchController) searchController.abort();
  searchController = null;
  selectedAccount = null;
  accountList.replaceChildren();
  accountOptions.hidden = true;
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

function openAuthorSearch(query) {
  authorQuery.value = query;
  resetAccountSearch();
  authorDialog.showModal();
  authorQuery.focus();
}
byId("add-author").addEventListener("click", () => openAuthorSearch(""));
byId("link-profile").addEventListener("click", () => openAuthorSearch(state.author));
authorDialog.addEventListener("close", resetAccountSearch);
authorDialog.addEventListener("cancel", event => { if (addingAuthor) event.preventDefault(); });
authorQuery.addEventListener("input", resetAccountSearch);

byId("author-search-form").addEventListener("submit", async event => {
  event.preventDefault();
  const query = authorQuery.value.trim();
  if (!query || addingAuthor || searchController) return;
  resetAccountSearch();
  const controller = new AbortController();
  searchController = controller;
  searchButton.disabled = true;
  const status = byId("author-search-status");
  status.textContent = "正在微信桌面搜索并核对主页…";
  try {
    const response = await fetch("/api/author-search?" + new URLSearchParams({ q: query }), {
      cache: "no-store", signal: controller.signal
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.error === "string" ? data.error : "搜索暂不可用，请稍后重试。");
    if (!Array.isArray(data.results)) throw new Error("未能读取搜索结果，请重新搜索。");
    if (controller.signal.aborted) return;
    for (const candidate of data.results) showCandidate(candidate);
    const count = accountList.childElementCount;
    accountOptions.hidden = count === 0;
    status.textContent = count ? "找到 " + count + " 个账号，请核对后选择。" :
      (data.results.length ? "这些结果没有可用的微信账号链接。" : "当前结果中没有匹配的视频号，试试完整名称。");
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
  addingAuthor = true;
  addButton.disabled = searchButton.disabled = authorQuery.disabled = accountOptions.disabled = true;
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
    loadDashboard();
  } catch (error) {
    const message = error instanceof TypeError ? "连接失败，请检查服务后重试。" : error.message;
    if (authorDialog.open) setError("author-error", message);
    else notice(message);
  } finally {
    addingAuthor = false;
    authorQuery.disabled = accountOptions.disabled = false;
    searchButton.disabled = !authorQuery.value.trim() || Boolean(searchController);
    addButton.disabled = !selectedAccount;
    addButton.textContent = "加入监控名单";
    closeButtons.forEach(button => { button.disabled = false; });
  }
});

loadDashboard();
