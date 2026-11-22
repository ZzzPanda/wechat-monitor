"use strict";

(() => {
  const pageSize = 30;
  const articleQuery = byId("public-query");
  const articleSort = byId("public-sort");
  const tasks = byId("public-tasks");
  const historyDialog = byId("public-history-dialog");
  const reader = byId("public-reader");
  const schedule = byId("public-schedule");
  let accounts = [];
  let pastAccounts = [];
  let overviewLoaded = false;
  let jobs = [];
  let settings;
  let accountId = "";
  let offset = 0;
  let overviewRequest;
  let pageRequest;
  let queryTimer;
  let readerRequest;
  let refreshTimer;
  let dirtySchedule = false;
  let savingSchedule = false;
  let historyAccount;
  const stopping = new Set();

  async function api(path, { method = "GET", data, signal } = {}) {
    const options = { method, cache: "no-store", signal };
    if (data !== undefined) {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify(data);
    }
    const response = await fetch("/api/public" + path, options);
    const result = await response.json().catch(() => null);
    if (!response.ok || !result) throw new Error(result?.error || "公众号服务暂时不可用，请稍后重试。");
    return result;
  }

  function errorText(error) {
    return error instanceof TypeError ? "连接失败，请稍后重试。" : error.message;
  }

  function accountRoute(id) {
    return id ? "#public/" + encodeURIComponent(id) : "#public";
  }

  function goToAccount(id) {
    const route = accountRoute(id);
    if (window.location.hash !== route) window.location.hash = route;
    setNavOpen(false);
    if (window.matchMedia("(max-width: 720px)").matches) byId("nav-toggle").focus();
  }

  function currentAccount() {
    return accounts.find(account => account.id === accountId);
  }

  function pastAccount() {
    return pastAccounts.find(account => account.id === accountId);
  }

  function publicAccountRow(entry) {
    return {
      id: entry.id, name: entry.name, count: entry.article_count, note: entry.note || "",
      description: entry.description || "", entry
    };
  }

  let navSignature;
  function renderPublicNav() {
    setCurrentNavItem(byId("open-public"), activeView === "public" && !accountId);
    setCurrentNavItem(byId("open-official-accounts"), activeView === "official-accounts");
    const selected = activeView === "public" ? accountId : null;
    const signature = JSON.stringify([selected, accounts, pastAccounts]);
    if (signature === navSignature) return;
    navSignature = signature;
    const entries = [...accounts, ...pastAccounts];
    const names = new Map();
    for (const entry of entries) names.set(entry.name, (names.get(entry.name) || 0) + 1);
    function button(account) {
      const node = navAccountButton(account.name, account.article_count, selected === account.id, () => goToAccount(account.id));
      if (names.get(account.name) > 1) {
        const detail = account.note || account.description || account.biz;
        const label = node.querySelector(".nav-name");
        const group = element("span", "public-nav-label");
        label.replaceWith(group);
        group.append(label, element("small", "public-nav-detail", detail));
        node.title = [account.name, detail, account.biz === detail ? "" : account.biz].filter(Boolean).join(" · ");
        node.setAttribute("aria-label", node.title);
      }
      return node;
    }
    byId("public-all-count").textContent = String(entries.reduce((sum, entry) => sum + entry.article_count, 0));
    const shortcuts = accountShortcuts(accounts.map(publicAccountRow));
    byId("public-accounts").replaceChildren(...shortcuts.map(row => button(row.entry)));
    if (!accounts.length) byId("public-accounts").append(element("p", "rail-empty", "名单为空"));
  }

  function renderHeader() {
    const account = currentAccount();
    const archived = pastAccount();
    const entry = account || archived;
    byId("public-title").textContent = entry?.name || "文章资料库";
    byId("public-note").hidden = !entry?.note;
    byId("public-note").textContent = entry?.note || "";
    byId("public-edit").hidden = byId("public-remove").hidden = byId("public-history").hidden = !account;
    byId("public-remonitor").hidden = !archived;
    const pending = account && jobs.some(job => job.account_id === account.id && job.mode === "history" && ongoing(job));
    byId("public-history").disabled = Boolean(pending);
    byId("public-history").textContent = pending ? "历史任务进行中" : "拉取历史文章";
  }

  function publicTime(value) {
    return typeof value === "number" ? crawlTime(value) : (value || "");
  }

  async function readArticle(item) {
    readerRequest?.abort();
    const request = readerRequest = new AbortController();
    byId("public-reader-title").textContent = item.title;
    byId("public-reader-meta").textContent = [item.author, publicTime(item.published)].filter(Boolean).join(" · ");
    const source = byId("public-source");
    const url = accountLink(item.url);
    source.hidden = !url;
    if (url) source.href = url;
    else source.removeAttribute("href");
    const body = byId("public-body");
    body.textContent = "正在读取正文…";
    setError("public-reader-error", "");
    reader.showModal();
    reader.scrollTop = 0;
    try {
      const article = await api("/articles/" + encodeURIComponent(item.id), { signal: request.signal });
      if (request.signal.aborted) return;
      if (typeof article.body !== "string" || !article.body.trim()) throw new Error("这篇文章缺少正文，请重新拉取。");
      body.textContent = article.body;
    } catch (error) {
      if (request.signal.aborted) return;
      body.textContent = "";
      setError("public-reader-error", errorText(error));
    }
  }
  reader.addEventListener("close", () => readerRequest?.abort());

  function articleRow(item) {
    const row = element("li");
    const button = element("button", "article-row");
    button.type = "button";
    button.append(element("strong", "article-title", item.title),
      element("span", "article-meta", [item.author, publicTime(item.published)].filter(Boolean).join(" · ")));
    button.addEventListener("click", () => readArticle(item));
    row.append(button);
    return row;
  }

  let previousArticles = "";
  function renderArticles(data) {
    const signature = JSON.stringify(data.articles);
    if (signature !== previousArticles) {
      byId("public-articles").replaceChildren(...data.articles.map(articleRow));
      previousArticles = signature;
    }
    const searching = Boolean(articleQuery.value.trim());
    byId("public-summary").textContent = searching ? "找到 " + data.total + " 篇文章" :
      "已保存 " + data.total + " 篇文章" + (accountId ? "" : " · 监控 " + accounts.length + " 个公众号");
    byId("public-articles").hidden = !data.articles.length;
    byId("public-empty").hidden = Boolean(data.articles.length);
    byId("public-empty-title").textContent = searching ? "没有符合条件的文章" : "暂无已保存文章";
    byId("public-empty-hint").textContent = searching ? "试试其他文章标题或公众号名称。" :
      (accountId ? "此公众号的文章保存后会显示在这里。" : "公众号文章保存后会显示在这里。");
    byId("public-clear-search").hidden = !searching;
    byId("public-pages").hidden = data.total <= pageSize;
    byId("public-prev").disabled = offset === 0;
    byId("public-next-page").disabled = offset + pageSize >= data.total;
    byId("public-page").textContent = "第 " + (1 + Math.floor(offset / pageSize)) + " / " + Math.ceil(data.total / pageSize) + " 页";
  }

  function jobRow(job) {
    const row = element("li", "crawl-job");
    row.append(element("strong", "", job.author), element("span", "", job.mode === "history" ? "历史文章" : "定时增量"),
      element("span", "", jobLabels[job.status] || job.status),
      element("span", "", "发现 " + job.discovered + " · 保存 " + job.saved + " · 跳过 " + job.skipped + " · 失败 " + job.failed));
    if (ongoing(job)) {
      const stop = element("button", "cancel-crawl", "停止");
      stop.type = "button";
      stop.setAttribute("aria-label", "停止「" + job.author + "」的文章采集任务");
      stop.disabled = stopping.has(job.id);
      stop.addEventListener("click", async () => {
        stopping.add(job.id);
        stop.disabled = true;
        try {
          await api("/jobs/" + encodeURIComponent(job.id) + "/cancel", { method: "POST", data: {} });
          notice("已请求停止文章采集");
        } catch (error) {
          notice(errorText(error));
        } finally {
          stopping.delete(job.id);
          stop.disabled = false;
          await refresh();
        }
      });
      row.append(stop);
    }
    if (job.error) row.append(element("span", ["failed", "partial"].includes(job.status) ? "job-error" : "job-info", job.error));
    if (job.item_error && !job.error?.includes(job.item_error)) row.append(element("span", "job-error", job.item_error));
    return row;
  }

  function renderTasks(data) {
    const changed = !settings || JSON.stringify(data.jobs) !== JSON.stringify(jobs);
    jobs = data.jobs;
    settings = data.settings;
    const active = jobs.filter(ongoing);
    const summary = active.length ? active.length + " 个任务进行中" :
      (settings.enabled ? "每 " + settings.interval_minutes + " 分钟采集新增" : "定时采集已关闭");
    byId("public-task-summary").textContent = (data.worker_online === false ? "采集进程离线 · " : "") + summary;
    if (changed) byId("public-jobs").replaceChildren(...(jobs.length ?
      jobs.map(jobRow) : [element("li", "crawl-hint", "暂无采集任务")]));
    if (!dirtySchedule && !savingSchedule) {
      byId("public-enabled").checked = settings.enabled;
      byId("public-start").value = scheduleDateValue(settings.start_at);
      byId("public-interval").value = settings.interval_minutes;
    }
    byId("public-next").textContent = settings.enabled && settings.next_run ?
      "下次运行（北京时间）" + crawlTime(settings.next_run) : "";
    byId("public-save").disabled = !dirtySchedule || savingSchedule || !schedule.checkValidity();
  }

  async function loadPage() {
    clearTimeout(queryTimer);
    pageRequest?.abort();
    if (activeView !== "public" || document.hidden) return;
    const request = pageRequest = new AbortController();
    const params = new URLSearchParams({ limit: pageSize, offset, sort: articleSort.value });
    if (accountId) params.set("account_id", accountId);
    if (articleQuery.value.trim()) params.set("q", articleQuery.value.trim());
    const list = byId("public-articles");
    list.setAttribute("aria-busy", "true");
    try {
      const page = await api("/articles?" + params, { signal: request.signal });
      if (request.signal.aborted) return;
      if (offset && offset >= page.total) {
        offset = Math.max(0, Math.floor((page.total - 1) / pageSize) * pageSize);
        return loadPage();
      }
      renderArticles(page);
      setError("public-error", "");
    } catch (error) {
      if (!request.signal.aborted) setError("public-error", errorText(error));
    } finally {
      if (!request.signal.aborted) list.setAttribute("aria-busy", "false");
    }
  }

  async function refresh() {
    clearTimeout(refreshTimer);
    overviewRequest?.abort();
    if (document.hidden) return false;
    const request = overviewRequest = new AbortController();
    try {
      const data = await api("", { signal: request.signal });
      if (request.signal.aborted) return false;
      accounts = data.accounts;
      pastAccounts = data.past_accounts;
      overviewLoaded = true;
      renderPublicNav();
      if (activeView === "official-accounts") renderPublicDirectory();
      if (activeView === "public") {
        if (accountId && !currentAccount() && !pastAccount()) {
          window.location.replace("#public");
          return true;
        }
        renderTasks(data);
        renderHeader();
        await loadPage();
      }
      return !request.signal.aborted;
    } catch (error) {
      if (!request.signal.aborted && navSignature === undefined) {
        byId("public-accounts").replaceChildren(element("p", "rail-empty", "暂时无法读取名单"));
      }
      if (!request.signal.aborted && activeView === "public") {
        byId("public-summary").textContent = "文章库读取失败";
        setError("public-error", errorText(error));
      }
      if (!request.signal.aborted && activeView === "official-accounts") {
        renderPublicDirectory({ error: errorText(error) });
      }
      return false;
    } finally {
      if (!request.signal.aborted && !document.hidden) {
        refreshTimer = setTimeout(refresh, activeView === "public" && jobs.some(ongoing) ? 4000 : 30000);
      }
    }
  }

  function renderPublicDirectory(status) {
    window.accountDirectory.render("public", accounts.map(publicAccountRow), pastAccounts.map(publicAccountRow), status);
  }

  function changeView() {
    pageRequest?.abort();
    clearTimeout(queryTimer);
    const next = activeView === "public" ? window.location.hash.slice(1).split("/")[1] || "" : "";
    if (next !== accountId) {
      accountId = next;
      offset = 0;
      byId("public-articles").hidden = true;
      byId("public-pages").hidden = true;
      byId("public-empty").hidden = true;
      byId("public-summary").textContent = "正在读取文章库…";
    }
    if (navSignature !== undefined) renderPublicNav();
    if (activeView === "public") renderHeader();
    if (activeView === "official-accounts") renderPublicDirectory({ loading: !overviewLoaded });
    refresh();
  }

  function searchArticles() {
    offset = 0;
    clearTimeout(queryTimer);
    pageRequest?.abort();
    queryTimer = setTimeout(loadPage, 150);
  }

  articleQuery.addEventListener("input", searchArticles);
  byId("public-clear-search").addEventListener("click", () => {
    articleQuery.value = "";
    searchArticles();
    articleQuery.focus();
  });
  articleSort.addEventListener("change", () => { offset = 0; loadPage(); });
  byId("public-refresh").addEventListener("click", refresh);
  byId("public-prev").addEventListener("click", () => { offset = Math.max(0, offset - pageSize); loadPage(); });
  byId("public-next-page").addEventListener("click", () => { offset += pageSize; loadPage(); });
  schedule.addEventListener("input", () => {
    dirtySchedule = true;
    byId("public-save").disabled = !settings || savingSchedule || !schedule.checkValidity();
  });
  schedule.addEventListener("submit", async event => {
    event.preventDefault();
    if (!settings || !dirtySchedule || savingSchedule) return;
    savingSchedule = true;
    const start = byId("public-start").value;
    const data = { enabled: byId("public-enabled").checked, interval_minutes: byId("public-interval").valueAsNumber,
      start_at: start === scheduleDateValue(settings.start_at) ? settings.start_at : Date.parse(start + "+08:00") / 1000 };
    for (const node of schedule.elements) node.disabled = true;
    try {
      await api("/settings", { method: "PATCH", data });
      dirtySchedule = false;
      notice("已保存公众号采集计划");
    } catch (error) {
      notice(errorText(error));
    } finally {
      for (const node of schedule.elements) node.disabled = false;
      savingSchedule = false;
      await refresh();
    }
  });

  accountManagers.public = {
    refresh,
    hasVersion: entry => accounts.some(account => account.id === entry.id && account.version === entry.version),
    async onChanged(method, data) {
      if (method !== "DELETE") {
        offset = 0;
        articleQuery.value = "";
        if (window.location.hash !== accountRoute(data.account.id)) {
          const changed = new Promise(resolve => window.addEventListener("hashchange", resolve, { once: true }));
          goToAccount(data.account.id);
          await changed;
        }
      }
      await refresh();
    }
  };

  byId("public-add").addEventListener("click", () => openAccountSearch("public", { query: "" }));
  byId("public-edit").addEventListener("click", () => {
    const entry = currentAccount();
    if (entry) openAccountEditor("public", entry);
  });
  byId("public-remove").addEventListener("click", () => {
    const entry = currentAccount();
    if (entry) openAccountRemoval("public", entry);
  });
  byId("public-remonitor").addEventListener("click", () => {
    const entry = pastAccount();
    if (entry) openAccountSearch("public", { query: entry.name, expectedBiz: entry.biz });
  });

  function lockDialog(dialog, busy) {
    dialog.setAttribute("aria-busy", String(busy));
    for (const node of dialog.querySelectorAll("button,input,select")) node.disabled = busy;
  }
  byId("public-history").addEventListener("click", () => {
    historyAccount = currentAccount();
    if (!historyAccount) return;
    byId("public-history-account").textContent = historyAccount.name;
    byId("public-limit").value = "";
    setError("public-history-error", "");
    historyDialog.showModal();
    byId("public-limit").focus();
  });
  byId("public-history-form").addEventListener("submit", async event => {
    event.preventDefault();
    const account = historyAccount;
    if (!account) return;
    const data = { account_id: account.id };
    if (byId("public-limit").value) data.max_pages = byId("public-limit").valueAsNumber;
    lockDialog(historyDialog, true);
    try {
      await api("/history", { method: "POST", data });
      offset = 0;
      historyDialog.close();
      tasks.open = true;
      notice("已提交「" + account.name + "」的历史采集任务");
      await refresh();
    } catch (error) {
      setError("public-history-error", errorText(error));
    } finally {
      lockDialog(historyDialog, false);
    }
  });

  document.addEventListener("dashboard-view", changeView);
  document.addEventListener("visibilitychange", () => { if (document.hidden) pageRequest?.abort(); refresh(); });
  changeView();
})();
