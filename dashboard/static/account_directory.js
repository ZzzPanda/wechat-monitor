"use strict";

(() => {
  const get = id => document.getElementById(id);
  const list = get("directory-accounts");
  const query = get("directory-query");
  const monitored = get("directory-monitored");
  const archived = get("directory-archived");
  const filters = {
    video: { history: false, query: "" },
    public: { history: false, query: "" }
  };
  const names = new Intl.Collator("zh-CN", { numeric: true });
  let kind = "video";
  let activeRows = [];
  let archivedRows = [];
  let visibleAccounts = new Map();
  let status = {};
  let signature = "";

  function compare(a, b) {
    const rank = b.count - a.count || names.compare(a.name, b.name);
    if (rank || a.id === b.id) return rank;
    return a.id < b.id ? -1 : 1;
  }

  function actionButton(action, label, name) {
    const button = element("button", "quiet-button", label);
    button.type = "button";
    button.dataset.action = action;
    button.title = label + "「" + name + "」";
    if (action === "remove") button.classList.add("remove-link");
    return button;
  }

  function accountItem(account, sameName, history) {
    const item = element("li", "directory-account");
    item.dataset.id = account.id;
    const link = element("a", "directory-account-link");
    link.href = (kind === "video" ? "#main/" : "#public/") + encodeURIComponent(account.id);
    const identity = element("span", "directory-account-identity");
    identity.append(element("strong", "directory-account-name", account.name));
    const details = [account.note, account.description];
    if (sameName && kind === "public") details.push(account.entry?.biz || account.id);
    const detail = [...new Set(details.filter(Boolean))].join(" · ");
    if (detail) identity.append(element("span", "directory-account-detail", detail));
    link.append(avatar(account.name), identity);
    const count = element("span", "directory-account-count", account.count + (kind === "video" ? " 条视频" : " 篇文章"));
    const actions = element("div", "directory-account-actions");
    if (history) {
      actions.append(actionButton("restore", "重新监控", account.name));
    } else {
      actions.append(actionButton("edit", "编辑", account.name), actionButton("remove", "移除", account.name));
    }
    item.append(link, count, actions);
    return item;
  }

  function paint() {
    const label = kind === "video" ? "视频号" : "公众号";
    const filter = filters[kind];
    const search = filter.query.trim().toLowerCase();
    const rows = filter.history ? archivedRows : activeRows;
    const matches = rows.filter(row => !search || [row.name, row.note, row.description, row.entry?.biz]
      .some(value => value && value.toLowerCase().includes(search)));
    const pending = Boolean(status.loading);
    const failed = Boolean(status.error);
    const ready = !pending && !failed;
    get("account-directory-title").textContent = label + "管理";
    get("directory-add-label").textContent = "添加" + label;
    monitored.setAttribute("aria-pressed", String(!filter.history));
    archived.setAttribute("aria-pressed", String(filter.history));
    monitored.querySelector(".directory-scope-count").textContent = ready ? activeRows.length : "";
    archived.querySelector(".directory-scope-count").textContent = ready ? archivedRows.length : "";
    get("directory-loading").hidden = !pending || failed;
    get("directory-error-text").textContent = status.error || "";
    get("account-directory-error").hidden = !failed;
    get("directory-empty").hidden = !ready || matches.length > 0;
    get("directory-empty-title").textContent = search ? "没有符合条件的账号"
      : (filter.history ? "暂无历史" : "暂无监控中的") + label;
    get("directory-clear").hidden = !search;
    list.hidden = !ready || !matches.length;
    if (!ready) return;

    // Resolve actions against fresh entries even if the visible row text stays the same.
    visibleAccounts = new Map(matches.map(row => [row.id, row]));
    const counts = new Map();
    for (const row of rows) counts.set(row.name, (counts.get(row.name) || 0) + 1);
    const next = JSON.stringify([kind, filter.history, matches.map(row =>
      [row.id, row.name, row.note, row.description, row.count, row.entry?.biz, counts.get(row.name)])]);
    if (next === signature) return;
    signature = next;
    list.replaceChildren(...matches.map(row => accountItem(row, counts.get(row.name) > 1, filter.history)));
  }

  function render(nextKind, rows, historyRows, nextStatus = {}) {
    kind = nextKind;
    activeRows = [...rows].sort(compare);
    archivedRows = [...historyRows].sort(compare);
    status = nextStatus;
    if (query.value !== filters[kind].query) query.value = filters[kind].query;
    paint();
  }

  function showHistory(history) {
    filters[kind].history = history;
    paint();
  }

  monitored.addEventListener("click", () => showHistory(false));
  archived.addEventListener("click", () => showHistory(true));
  query.addEventListener("input", () => {
    filters[kind].query = query.value;
    paint();
  });
  get("directory-clear").addEventListener("click", () => {
    filters[kind].query = query.value = "";
    paint();
    query.focus();
  });
  get("directory-add-account").addEventListener("click", () => window.accountDirectoryActions.add(kind));
  get("directory-retry").addEventListener("click", () => window.accountDirectoryActions.refresh(kind));
  list.addEventListener("click", event => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const row = visibleAccounts.get(button.closest("li").dataset.id);
    if (!row) return;
    const action = button.dataset.action;
    window.accountDirectoryActions[action](kind, action === "restore" ? row : row.entry);
  });
  window.accountDirectory = { render, compare };
})();
