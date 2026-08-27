const storeSearch = document.querySelector("#storeSearch");
const storeResults = document.querySelector("#storeResults");
const selectedStoreEl = document.querySelector("#selectedStore");
const toastEl = document.querySelector("#toast");

let stores = [];
let selectedStore = null;
let debounceTimer = null;

function absoluteFeedUrl(path) {
  return new URL(path, window.location.href).href;
}

function webcalUrl(path) {
  const url = new URL(path, window.location.href);
  return `webcal://${url.host}${url.pathname}${url.search}`;
}

function googleCalendarUrl(path) {
  const feed = absoluteFeedUrl(path);
  return `https://calendar.google.com/calendar/r?cid=${encodeURIComponent(feed)}`;
}

function formatDate(value) {
  if (!value) return "";
  const d = new Date(`${value}T12:00:00`);
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric"
  }).format(d);
}

function toast(message) {
  toastEl.textContent = message;
  toastEl.classList.remove("hidden");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => toastEl.classList.add("hidden"), 2200);
}

async function copyUrl(path) {
  const value = absoluteFeedUrl(path);
  try {
    await navigator.clipboard.writeText(value);
    toast("Subscription URL copied.");
  } catch {
    window.prompt("Copy this subscription URL:", value);
  }
}

function scoreStore(store, query) {
  const q = query.toLowerCase().trim();
  const name = store.name.toLowerCase();
  const location = (store.location || "").toLowerCase();

  if (name === q) return 1000;
  if (name.startsWith(q)) return 800;
  if (name.includes(q)) return 600;
  if (location.startsWith(q)) return 450;
  if (location.includes(q)) return 350;

  const terms = q.split(/\s+/).filter(Boolean);
  if (terms.length && terms.every(t => name.includes(t) || location.includes(t))) {
    return 250;
  }
  return 0;
}

function renderResults(query) {
  const q = query.trim();
  if (q.length < 2) {
    storeResults.innerHTML = "";
    return;
  }

  const matches = stores
    .map(store => ({ store, score: scoreStore(store, q) }))
    .filter(x => x.score > 0)
    .sort((a, b) => b.score - a.score || a.store.name.localeCompare(b.store.name))
    .slice(0, 16);

  if (!matches.length) {
    storeResults.innerHTML =
      `<div class="empty-results">No current FAB store listing matched “${escapeHtml(q)}”. Try the store name, city, state/province, or postal code.</div>`;
    return;
  }

  storeResults.innerHTML = matches.map(({ store }) => `
    <button class="store-result" type="button" role="option" data-store-id="${escapeHtml(store.id)}">
      <span>
        <strong>${escapeHtml(store.name)}</strong>
        <small>${escapeHtml(store.location || "Location not shown by FAB")}</small>
      </span>
      <span class="result-count">${store.event_count} event${store.event_count === 1 ? "" : "s"}</span>
    </button>
  `).join("");

  storeResults.querySelectorAll("[data-store-id]").forEach(button => {
    button.addEventListener("click", () => selectStore(button.dataset.storeId));
  });
}

function selectStore(id) {
  selectedStore = stores.find(store => store.id === id);
  if (!selectedStore) return;

  localStorage.setItem("fabCalendarStoreId", id);
  storeResults.innerHTML = "";
  storeSearch.value = selectedStore.name;

  document.querySelector("#selectedStoreName").textContent = selectedStore.name;
  document.querySelector("#selectedStoreLocation").textContent =
    selectedStore.location || "Location not shown by FAB";

  const officialLink = document.querySelector("#officialStoreLink");
  if (selectedStore.official_url) {
    officialLink.href = selectedStore.official_url;
    officialLink.classList.remove("hidden");
  } else {
    officialLink.classList.add("hidden");
  }

  document.querySelector("#combinedDescription").textContent =
    selectedStore.event_count > 0
      ? `${selectedStore.event_count} currently listed local event${selectedStore.event_count === 1 ? "" : "s"}; next local listing ${formatDate(selectedStore.next_event_date)}.`
      : `No upcoming local events are currently listed, but this permanent feed will remain active and automatically fill when FAB lists new events for this store.`;

  wireFeedSet("Combined", selectedStore.combined_feed);
  wireFeedSet("StoreOnly", selectedStore.store_only_feed);

  selectedStoreEl.classList.remove("hidden");
  selectedStoreEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function wireFeedSet(suffix, path) {
  document.querySelector(`#google${suffix}`).href = googleCalendarUrl(path);
  document.querySelector(`#webcal${suffix}`).href = webcalUrl(path);
  const copyButton = document.querySelector(`#copy${suffix}`);
  copyButton.onclick = () => copyUrl(path);
}

function escapeHtml(text) {
  return String(text ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function setupGlobalFeeds() {
  document.querySelectorAll(".mini-actions[data-feed]").forEach(container => {
    const path = container.dataset.feed;
    container.innerHTML = `
      <a class="button" target="_blank" rel="noopener" href="${googleCalendarUrl(path)}">Google</a>
      <a class="button" href="${webcalUrl(path)}">Apple / Outlook</a>
      <button class="button secondary" type="button">Copy URL</button>
    `;
    container.querySelector("button").addEventListener("click", () => copyUrl(path));
  });
}

async function loadData() {
  setupGlobalFeeds();

  try {
    const [storeResponse, statusResponse] = await Promise.all([
      fetch(`stores.json?v=${Date.now()}`, { cache: "no-store" }),
      fetch(`status.json?v=${Date.now()}`, { cache: "no-store" })
    ]);

    if (!storeResponse.ok) throw new Error("Unable to load store directory");
    const storePayload = await storeResponse.json();
    stores = storePayload.stores || [];

    if (statusResponse.ok) {
      const status = await statusResponse.json();
      const statusPill = document.querySelector("#statusPill");
      statusPill.textContent =
        `${status.store_count ?? stores.length} stores • ${status.event_count ?? "—"} upcoming events`;

      if (status.generated_at) {
        const updated = new Date(status.generated_at);
        document.querySelector("#lastUpdated").textContent =
          `Feed data last generated ${updated.toLocaleString()}.`;
      }
    }

    const saved = localStorage.getItem("fabCalendarStoreId");
    if (saved && stores.some(store => store.id === saved)) {
      selectStore(saved);
    }
  } catch (error) {
    console.error(error);
    document.querySelector("#statusPill").textContent = "Calendar data unavailable";
    storeResults.innerHTML =
      `<div class="empty-results">The store directory is not generated yet. Run the GitHub Actions update workflow once after deploying the repository.</div>`;
  }
}

storeSearch.addEventListener("input", () => {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => renderResults(storeSearch.value), 80);
});

storeSearch.addEventListener("focus", () => {
  if (storeSearch.value.trim().length >= 2) renderResults(storeSearch.value);
});

document.querySelector("#clearSearch").addEventListener("click", () => {
  storeSearch.value = "";
  storeResults.innerHTML = "";
  selectedStoreEl.classList.add("hidden");
  selectedStore = null;
  localStorage.removeItem("fabCalendarStoreId");
  storeSearch.focus();
});

document.querySelector("#changeStore").addEventListener("click", () => {
  selectedStoreEl.classList.add("hidden");
  storeSearch.focus();
  storeSearch.select();
  renderResults(storeSearch.value);
});

loadData();
