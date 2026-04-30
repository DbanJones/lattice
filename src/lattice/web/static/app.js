// ============================================================
// Lattice frontend — vanilla JS, single-page, hash routing.
// Routes:
//   #/                     → projects list
//   #/p/<name>             → project detail (overview tab)
//   #/p/<name>/<section>   → project detail with given subnav
// ============================================================

// ─── markdown helper ─────────────────────────────────

function renderMarkdown(text) {
  if (window.marked) {
    return marked.parse(text);
  }
  return escapeHtml(text).replace(/\n\n/g, "<br><br>");
}

// ─── state ───────────────────────────────────────────

const state = {
  projects: [],
  current: null,        // selected project name
  detail: null,         // project detail JSON
  hierarchy: null,
  drafts: [],
  selectedDraft: null,
  ws: null,
  passes: new Map(),
  currentPass: 1,
  elapsedTimer: null,
};

const PHASE_LABELS = {
  structure_outline: "Structuring raw prose into outline",
  normalise_outline: "Tagging claims for renderability",
  add_conclusion: "Adding conclusion section",
  ingest: "Parsing outline",
  plan: "Building cluster plan",
  render: "Rendering clusters",
  auto_recovery: "Auto-recovery (re-rendering failed clusters)",
  extract_references: "Extracting references from raw paper",
  relationship_inference: "Inferring claim relationships",
  redraft: "Redrafting clusters with new relationships",
  audit: "Auditing prose",
  convergence_loop: "Convergence loop (autofix → re-render → retry)",
  autofix: "Autofix (flags → proposals → apply)",
  rerender: "Re-rendering dirty clusters",
  finalise: "Finalising document",
  finalise_retry: "Retrying delivery after recovery",
  voice_review: "Voice compliance review",
  source_gap_review: "Source-gap review",
};

// ─── boot ────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  window.addEventListener("hashchange", router);
  document
    .querySelector(".brand-link")
    .addEventListener("click", e => {
      e.preventDefault();
      navigate("/");
    });
  loadVersion();
  router();
});

async function loadVersion() {
  const slot = document.getElementById("server-version");
  if (!slot) return;
  try {
    const data = await fetchJSON("/api/version");
    slot.textContent = `v${data.version}`;
  } catch (err) {
    slot.textContent = "";
  }
}

// ─── routing ─────────────────────────────────────────

function navigate(path) {
  location.hash = "#" + path;
}

function router() {
  const hash = location.hash.replace(/^#/, "") || "/";
  if (hash === "/") return renderProjectsList();
  const m = hash.match(/^\/p\/([^\/]+)(?:\/([^\/]+))?(?:\/([^\/]+))?$/);
  if (m) return renderProjectDetail(m[1], m[2] || "dashboard", m[3] || null);
  navigate("/");
}

// ─── breadcrumb ──────────────────────────────────────

function setBreadcrumb(items) {
  const el = document.getElementById("breadcrumb");
  el.innerHTML = items
    .map((it, i) => {
      if (it.href) return `<a href="#${escapeAttr(it.href)}">${escapeHtml(it.label)}</a>`;
      return `<span class="current">${escapeHtml(it.label)}</span>`;
    })
    .join(" / ");
}

// ─── projects list ───────────────────────────────────

async function renderProjectsList() {
  setBreadcrumb([]);
  const main = document.getElementById("app");
  main.innerHTML = "";
  main.appendChild(cloneTemplate("tpl-projects-list"));

  main.querySelector('[data-action="new-project"]')
    .addEventListener("click", openNewProjectModal);

  // Insert a "+ New category" button next to "+ New project".
  const actions = main.querySelector('.page-actions');
  if (actions && !actions.querySelector('[data-action="new-category"]')) {
    const btn = document.createElement('button');
    btn.className = 'btn';
    btn.dataset.action = 'new-category';
    btn.textContent = '+ New category';
    btn.addEventListener('click', () => addEmptyCategory(main));
    actions.insertBefore(btn, actions.firstChild);
  }

  const grid = main.querySelector('[data-bind="grid"]');
  grid.innerHTML = `<div class="empty-state">Loading…</div>`;

  try {
    const projects = await fetchJSON("/api/projects");
    state.projects = projects;
    if (!projects.length) {
      grid.innerHTML = `
        <div class="empty-state">
          <h3>No projects yet</h3>
          <p>Click <strong>+ New project</strong> above, or run <code>lattice init &lt;name&gt;</code> in your projects directory.</p>
        </div>`;
      return;
    }
    renderCategorisedGrid(grid, projects);
  } catch (err) {
    grid.innerHTML = `<div class="empty-state">Failed to load: ${escapeHtml(err.message)}</div>`;
  }
}

// ─── categorised projects grid ──────────────────────

function renderCategorisedGrid(grid, projects) {
  grid.innerHTML = "";
  grid.classList.add('grid-by-category');

  // Group by category, preserving the API's sorted order.
  const buckets = new Map();
  projects.forEach(p => {
    const cat = p.category || 'Uncategorised';
    if (!buckets.has(cat)) buckets.set(cat, []);
    buckets.get(cat).push(p);
  });
  // Preserve any extra empty categories the user just created in this session.
  (state.extraCategories || []).forEach(cat => {
    if (!buckets.has(cat)) buckets.set(cat, []);
  });

  for (const [category, items] of buckets) {
    grid.appendChild(renderCategorySection(category, items));
  }
}

function renderCategorySection(category, items) {
  const section = document.createElement('section');
  section.className = 'category-section';
  section.dataset.category = category;

  const header = document.createElement('div');
  header.className = 'category-head';
  header.innerHTML = `
    <h2 class="category-title" contenteditable="false" spellcheck="false"></h2>
    <span class="category-count muted small">${items.length} project${items.length === 1 ? '' : 's'}</span>
    <div class="category-actions">
      <button class="btn-ghost sm" data-action="rename-category" title="Rename category">Rename</button>
    </div>
  `;
  header.querySelector('.category-title').textContent = category;
  header.querySelector('[data-action="rename-category"]')
    .addEventListener('click', () => renameCategoryPrompt(category));
  section.appendChild(header);

  const list = document.createElement('div');
  list.className = 'category-cards';
  list.dataset.category = category;
  items.forEach(p => list.appendChild(renderProjectCard(p)));

  // Drop target for drag-and-drop. Empty categories still need to
  // accept drops so they don't disappear.
  list.addEventListener('dragover', e => {
    if (!state.dragging) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    list.classList.add('drag-over');
    // Find the card the cursor is closest to and place a drop indicator.
    const after = cardAfter(list, e.clientY);
    state.dragging.classList.add('dragging');
    if (after == null) {
      list.appendChild(state.dragging);
    } else {
      list.insertBefore(state.dragging, after);
    }
  });
  list.addEventListener('dragleave', e => {
    if (!list.contains(e.relatedTarget)) list.classList.remove('drag-over');
  });
  list.addEventListener('drop', e => {
    e.preventDefault();
    list.classList.remove('drag-over');
  });

  section.appendChild(list);
  return section;
}

function cardAfter(list, y) {
  const cards = [...list.querySelectorAll('.project-card:not(.dragging)')];
  return cards.find(card => {
    const box = card.getBoundingClientRect();
    return y < box.top + box.height / 2;
  }) || null;
}

// Slugify a human-readable name into a folder-safe form. Mirrors the
// server-side `_slugify_project_name` so we can suggest a default
// folder name without a round-trip.
function slugifyProjectName(name) {
  const cleaned = String(name || "")
    .replace(/[^A-Za-z0-9_\-]+/g, " ")
    .trim()
    .toLowerCase();
  return cleaned.replace(/\s+/g, "_").replace(/^[_\-]+|[_\-]+$/g, "").slice(0, 80);
}

async function renameProjectPrompt(p) {
  const currentDisplay = p.display_name || p.name;
  const currentFolder = p.name;
  const newDisplay = window.prompt(
    `Rename project\n\nDisplay name (shown in the UI):`,
    currentDisplay,
  );
  if (newDisplay == null) return;
  const cleanedDisplay = newDisplay.trim();
  if (!cleanedDisplay) {
    alert("Display name cannot be empty.");
    return;
  }

  const suggestedFolder = slugifyProjectName(cleanedDisplay) || currentFolder;
  const folderInput = window.prompt(
    `Folder name on disk\n\n` +
    `Leave unchanged to keep the URL stable, or enter a new slug. ` +
    `Allowed: lowercase letters, digits, underscore, hyphen.\n\n` +
    `Renaming the folder will change the project's URL.`,
    suggestedFolder !== currentFolder ? suggestedFolder : currentFolder,
  );
  if (folderInput == null) return;
  const cleanedFolder = folderInput.trim();
  const folderChanged = cleanedFolder && cleanedFolder !== currentFolder;
  if (cleanedFolder && cleanedFolder !== slugifyProjectName(cleanedFolder)) {
    alert(
      `Folder name must be a slug (lowercase letters, digits, _, -). ` +
      `Try: ${slugifyProjectName(cleanedFolder)}`,
    );
    return;
  }

  const body = {};
  if (cleanedDisplay !== currentDisplay) body.display_name = cleanedDisplay;
  if (folderChanged) body.folder_name = cleanedFolder;
  if (Object.keys(body).length === 0) return;

  try {
    const resp = await fetch(
      `/api/projects/${encodeURIComponent(currentFolder)}`,
      {
        method: "PATCH",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
      },
    );
    if (!resp.ok) {
      const detail = await resp.json().catch(() => ({}));
      alert("Rename failed: " + (detail.detail || `HTTP ${resp.status}`));
      return;
    }
    const updated = await resp.json();
    // If we're inside the project detail view for this project and the
    // folder changed, navigate to the new URL so subsequent API calls
    // resolve. Otherwise just refresh the list.
    if (folderChanged && state.current === currentFolder) {
      navigate(`/p/${encodeURIComponent(updated.name)}`);
    } else if (location.hash === "" || location.hash === "#/" || location.hash === "#") {
      await renderProjectsList();
    } else {
      // Re-render whatever view we're on.
      router();
    }
  } catch (err) {
    alert("Network error: " + err.message);
  }
}

async function renameCategoryPrompt(category) {
  const fresh = window.prompt(`Rename category "${category}" to:`, category);
  if (fresh == null) return;
  const cleaned = fresh.trim();
  if (!cleaned || cleaned === category) return;
  // Update every project currently in this category.
  const targets = (state.projects || []).filter(p => (p.category || 'Uncategorised') === category);
  await Promise.all(targets.map(p => fetch(
    `/api/projects/${encodeURIComponent(p.name)}`,
    {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({category: cleaned}),
    }
  )));
  // Migrate extra-categories list too.
  if (state.extraCategories) {
    state.extraCategories = state.extraCategories.map(c => c === category ? cleaned : c);
  }
  await renderProjectsList();
}

function addEmptyCategory(main) {
  const name = window.prompt('New category name:');
  if (!name) return;
  const cleaned = name.trim();
  if (!cleaned) return;
  state.extraCategories = state.extraCategories || [];
  if (!state.extraCategories.includes(cleaned)) state.extraCategories.push(cleaned);
  renderProjectsList();
}

async function persistOrderFromDom() {
  const main = document.getElementById('app');
  const sections = main.querySelectorAll('.category-section');
  const order = [];
  sections.forEach(section => {
    const category = section.dataset.category;
    const cards = section.querySelectorAll('.project-card');
    cards.forEach((card, idx) => {
      order.push({
        name: card.dataset.projectName,
        category,
        position: idx,
      });
    });
  });
  if (!order.length) return;
  await fetch('/api/projects/_reorder', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({order}),
  });
  // Refresh state.projects so subsequent operations see the new layout.
  state.projects = await fetchJSON('/api/projects');
}

async function deleteProject(p) {
  const confirmed = window.confirm(
    `Delete "${p.display_name || p.name}"?\n\n` +
    `The folder will be moved to .trash/ inside your projects root, ` +
    `so you can recover it manually if needed.`
  );
  if (!confirmed) return;
  await fetch(`/api/projects/${encodeURIComponent(p.name)}`, {method: 'DELETE'});
  await renderProjectsList();
}

// ─── new project modal ──────────────────────────────

function openNewProjectModal() {
  const modal = cloneTemplate("tpl-new-project-modal");
  document.body.appendChild(modal);

  const close = () => modal.remove();
  modal.querySelectorAll('[data-action="close"]').forEach(b =>
    b.addEventListener("click", close));
  modal.addEventListener("click", e => {
    if (e.target === modal) close();
  });

  const form = modal.querySelector('[data-bind="form"]');
  const errorEl = modal.querySelector('[data-bind="error"]');

  // Live preview of the slugified folder name as the user types.
  const nameInput = modal.querySelector('[data-bind="name-input"]');
  const folderPreview = modal.querySelector('[data-bind="folder-preview"]');
  function updateFolderPreview() {
    const slug = slugifyName(nameInput.value);
    folderPreview.textContent = slug || "(empty)";
  }
  nameInput.addEventListener("input", updateFolderPreview);
  updateFolderPreview();

  // ─── outline file picker (drop + browse) ───
  const outlineDrop = modal.querySelector('[data-bind="outline-drop"]');
  const outlineInput = modal.querySelector('[data-bind="outline-file-input"]');
  const outlineLabel = modal.querySelector('[data-bind="outline-file-label"]');
  const outlinePrompt = outlineDrop.querySelector('.dropzone-prompt');
  const outlineSelected = outlineDrop.querySelector('.dropzone-selected');
  const outlineClear = modal.querySelector('[data-action="clear-outline-file"]');
  const outlineTextarea = form.querySelector('textarea[name="outline"]');
  // Stash an uploaded file separately (DOCX) since we can't preview it.
  const outlineState = {file: null};

  bindDropzone(outlineDrop, async files => {
    if (!files.length) return;
    await handleOutlineFile(files[0]);
  });

  outlineInput.addEventListener("change", async () => {
    const file = outlineInput.files?.[0];
    if (!file) return;
    await handleOutlineFile(file);
  });

  async function handleOutlineFile(file) {
    const ext = file.name.split(".").pop().toLowerCase();
    if (!["md", "markdown", "txt", "docx", "pdf"].includes(ext)) {
      alert(`Unsupported outline format: ${ext}. Use .md, .txt, .docx, or .pdf.`);
      return;
    }
    outlineState.file = file;
    outlineLabel.textContent = `${file.name} (${formatBytes(file.size)})`;
    outlinePrompt.classList.add("hidden");
    outlineSelected.classList.remove("hidden");

    if (ext === "md" || ext === "markdown" || ext === "txt") {
      const text = await file.text();
      outlineTextarea.value = text;
      outlineTextarea.disabled = false;
    } else if (ext === "pdf") {
      // Server extracts text via pypdf; we drop result in the textarea.
      outlineTextarea.value = "[Extracting text from PDF…]";
      outlineTextarea.disabled = true;
      try {
        const fd = new FormData();
        fd.append("file", file);
        const resp = await fetch("/api/extract-text", {method: "POST", body: fd});
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${resp.status}`);
        }
        const data = await resp.json();
        outlineTextarea.value = data.text;
        outlineTextarea.disabled = false;
        // Once text is extracted we no longer need to upload the PDF as
        // outline file — the textarea has the content. Clear the file
        // so the create flow uses the text path, not the file path.
        outlineState.file = null;
        outlineLabel.textContent =
          `${file.name} (${data.page_count || "?"} pages, ${data.char_count.toLocaleString()} chars extracted)`;
      } catch (err) {
        outlineTextarea.value =
          `[Failed to extract text: ${err.message}. Edit manually below.]`;
        outlineTextarea.disabled = false;
      }
    } else if (ext === "docx") {
      // Server-side: DOCX uploaded as-is; ingester parses it. Try
      // extract-text too so the user has an editable preview.
      outlineTextarea.value = "[Extracting text from DOCX preview…]";
      outlineTextarea.disabled = true;
      try {
        const fd = new FormData();
        fd.append("file", file);
        const resp = await fetch("/api/extract-text", {method: "POST", body: fd});
        if (resp.ok) {
          const data = await resp.json();
          outlineTextarea.value = data.text;
        } else {
          outlineTextarea.value =
            `[${file.name} will be uploaded as DOCX and parsed at ingest time.]`;
        }
      } catch (e) {
        outlineTextarea.value =
          `[${file.name} will be uploaded as DOCX and parsed at ingest time.]`;
      }
      outlineTextarea.disabled = false;
    }
  }

  outlineClear.addEventListener("click", () => {
    outlineState.file = null;
    outlineInput.value = "";
    outlineLabel.textContent = "";
    outlinePrompt.classList.remove("hidden");
    outlineSelected.classList.add("hidden");
    outlineTextarea.value = "";
    outlineTextarea.disabled = false;
  });

  // ─── source files picker (drop + browse) ───
  const sourceDrop = modal.querySelector('[data-bind="source-drop"]');
  const sourceInput = modal.querySelector('[data-bind="source-files-input"]');
  const sourceList = modal.querySelector('[data-bind="source-list"]');
  const sourceState = {files: []};

  bindDropzone(sourceDrop, files => {
    files.forEach(f => sourceState.files.push(f));
    refreshSourceList();
  });

  sourceInput.addEventListener("change", () => {
    Array.from(sourceInput.files || []).forEach(f => sourceState.files.push(f));
    sourceInput.value = "";
    refreshSourceList();
  });

  function refreshSourceList() {
    sourceList.innerHTML = "";
    sourceState.files.forEach((f, i) => {
      const li = document.createElement("li");
      li.innerHTML = `
        <span>${escapeHtml(f.name)}</span>
        <span class="file-meta">${formatBytes(f.size)}</span>
        <button type="button" class="remove" data-index="${i}" title="Remove">✕</button>`;
      li.querySelector(".remove").addEventListener("click", () => {
        sourceState.files.splice(i, 1);
        refreshSourceList();
      });
      sourceList.appendChild(li);
    });
  }

  // ─── form submission ───
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    errorEl.textContent = "";
    const submitBtn = form.querySelector('button[type="submit"]');
    submitBtn.disabled = true;
    submitBtn.textContent = "Creating…";

    const fd = new FormData(form);
    const usingOutlineFile = outlineState.file && /\.docx$/i.test(outlineState.file.name);

    const body = {
      name: fd.get("name").trim(),
      voice: fd.get("voice"),
      // For .docx, we send no inline outline — the file is uploaded separately
      // and ingest is delegated to the outline-upload endpoint.
      outline: usingOutlineFile
        ? null
        : (outlineTextarea.value || "").trim() || null,
      // Defer ingest until after the docx + sources upload.
      ingest_now: !usingOutlineFile && fd.get("ingest_now") === "on",
    };

    try {
      // Step 1: create the project (folder structure + voice + initial outline if text).
      const created = await postJSON("/api/projects", body);

      // Step 2: if a DOCX outline was selected, upload it and trigger ingest.
      if (usingOutlineFile) {
        submitBtn.textContent = "Uploading outline…";
        const outlineFD = new FormData();
        outlineFD.append("file", outlineState.file);
        outlineFD.append("ingest", fd.get("ingest_now") === "on" ? "true" : "false");
        const outlineResp = await fetch(
          `/api/projects/${encodeURIComponent(created.name)}/outline`,
          {method: "POST", body: outlineFD},
        );
        if (!outlineResp.ok) {
          const detail = await outlineResp.json().catch(() => ({}));
          errorEl.textContent = `Outline upload failed: ${detail.detail || outlineResp.status}`;
          submitBtn.disabled = false;
          submitBtn.textContent = "Create project";
          return;
        }
      }

      // Step 3: if sources were added, upload them and run the indexer.
      if (sourceState.files.length) {
        submitBtn.textContent = `Uploading ${sourceState.files.length} source(s)…`;
        const bucket = fd.get("source_bucket") || "papers";
        const srcFD = new FormData();
        srcFD.append("bucket", bucket);
        sourceState.files.forEach(f => srcFD.append("files", f));
        const srcResp = await fetch(
          `/api/projects/${encodeURIComponent(created.name)}/sources`,
          {method: "POST", body: srcFD},
        );
        if (srcResp.ok) {
          submitBtn.textContent = "Indexing sources…";
          await fetch(
            `/api/projects/${encodeURIComponent(created.name)}/sources/index`,
            {method: "POST"},
          );
        } else {
          const detail = await srcResp.json().catch(() => ({}));
          errorEl.textContent = `Sources upload had issues: ${detail.detail || srcResp.status}`;
        }
      }

      close();
      if (created.notes && created.notes.length) {
        alert("Project created with notes:\n\n" + created.notes.join("\n"));
      }
      navigate(`/p/${encodeURIComponent(created.name)}`);
    } catch (err) {
      errorEl.textContent = `Network error: ${err.message}`;
      submitBtn.disabled = false;
      submitBtn.textContent = "Create project";
    }
  });

  setTimeout(() => modal.querySelector('input[name="name"]')?.focus(), 0);
}

/**
 * Make `el` accept drag-and-drop file uploads. When files are dropped,
 * `onFiles(files)` is called with an Array of File objects. Adds visual
 * feedback (".dragover" class) on dragover/dragleave. Click-through to
 * the inner <input type="file"> still works for keyboard/screen-reader
 * users — that input lives inside .dropzone-link.
 */
function bindDropzone(el, onFiles) {
  let depth = 0;
  el.addEventListener("dragenter", e => {
    e.preventDefault();
    depth++;
    el.classList.add("dragover");
  });
  el.addEventListener("dragover", e => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  });
  el.addEventListener("dragleave", e => {
    depth = Math.max(0, depth - 1);
    if (depth === 0) el.classList.remove("dragover");
  });
  el.addEventListener("drop", e => {
    e.preventDefault();
    depth = 0;
    el.classList.remove("dragover");
    const files = Array.from(e.dataTransfer?.files || []);
    if (files.length) onFiles(files);
  });
}

async function postJSON(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `HTTP ${resp.status}`);
  }
  return resp.json();
}

function renderProjectCard(p) {
  const card = cloneTemplate("tpl-project-card");
  card.dataset.projectName = p.name;
  card.draggable = true;

  card.querySelector('[data-bind="name"]').textContent = p.display_name || p.name;
  const pathLine = (p.display_name && p.display_name !== p.name)
    ? `${p.path} · folder: ${p.name}`
    : p.path;
  card.querySelector('[data-bind="path"]').textContent = pathLine;
  card.querySelector('[data-bind="words"]').textContent =
    p.paper_words ? p.paper_words.toLocaleString() : "—";
  card.querySelector('[data-bind="last"]').textContent = formatTimestamp(p.last_render);

  const status = card.querySelector('[data-bind="status"]');
  if (p.paper_words > 0) {
    status.textContent = "Delivered";
    status.classList.add("ok");
  } else {
    status.textContent = "Not rendered";
  }

  // Rename + delete buttons (top-right corner).
  const ren = document.createElement('button');
  ren.className = 'card-rename';
  ren.title = 'Rename project';
  ren.textContent = '✎';
  ren.addEventListener('click', e => {
    e.stopPropagation();
    renameProjectPrompt(p);
  });
  card.appendChild(ren);

  const del = document.createElement('button');
  del.className = 'card-delete';
  del.title = 'Delete project';
  del.textContent = '×';
  del.addEventListener('click', e => {
    e.stopPropagation();
    deleteProject(p);
  });
  card.appendChild(del);

  // Drag handle (⋮⋮ icon at top-left). The whole card is draggable;
  // this just gives the user a clear affordance.
  const grip = document.createElement('span');
  grip.className = 'card-grip';
  grip.title = 'Drag to reorder or recategorise';
  grip.textContent = '⋮⋮';
  card.appendChild(grip);

  // Drag-and-drop wiring.
  card.addEventListener('dragstart', e => {
    state.dragging = card;
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', p.name);
    setTimeout(() => card.classList.add('dragging'), 0);
  });
  card.addEventListener('dragend', async () => {
    card.classList.remove('dragging');
    state.dragging = null;
    document.querySelectorAll('.category-cards.drag-over')
      .forEach(el => el.classList.remove('drag-over'));
    await persistOrderFromDom();
  });

  // Click navigates to detail (but ignore clicks on grip/rename/delete).
  card.addEventListener("click", e => {
    if (e.target.closest('.card-delete')
        || e.target.closest('.card-rename')
        || e.target.closest('.card-grip')) return;
    navigate(`/p/${encodeURIComponent(p.name)}`);
  });
  return card;
}

// ─── project detail ──────────────────────────────────

async function renderProjectDetail(name, sectionId, subSectionId) {
  state.current = name;
  state.currentSubSection = subSectionId || null;
  setBreadcrumb([
    {label: "Projects", href: "/"},
    {label: name},  // updated to display_name once detail loads
  ]);

  const main = document.getElementById("app");
  main.innerHTML = "";
  main.appendChild(cloneTemplate("tpl-project-detail"));

  // Load detail in parallel.
  let detail, hierarchy, drafts, outlineStatus, runHistory;
  try {
    [detail, hierarchy, drafts, outlineStatus, runHistory] = await Promise.all([
      fetchJSON(`/api/projects/${encodeURIComponent(name)}`),
      fetchJSON(`/api/projects/${encodeURIComponent(name)}/hierarchy`),
      fetchJSON(`/api/projects/${encodeURIComponent(name)}/drafts`),
      fetchJSON(`/api/projects/${encodeURIComponent(name)}/outline-status`)
        .catch(() => null),
      fetchJSON(`/api/projects/${encodeURIComponent(name)}/run-history`)
        .catch(() => ({history: [], latest_by_level: {}, summary: {}})),
    ]);
  } catch (err) {
    main.querySelector('[data-bind="dashboard"]').innerHTML =
      `<div class="empty-state"><h3>Failed to load project</h3><p>${escapeHtml(err.message)}</p></div>`;
    return;
  }
  state.detail = detail;
  state.hierarchy = hierarchy;
  state.drafts = drafts;
  state.outlineStatus = outlineStatus;
  state.runHistory = runHistory;

  // Header — prefer the human-readable display name, fall back to slug.
  const headerName = detail.display_name || name;
  main.querySelector('[data-bind="name"]').textContent = headerName;
  // Update breadcrumb with the display name now that we've loaded it.
  setBreadcrumb([
    {label: "Projects", href: "/"},
    {label: headerName},
  ]);
  main.querySelector('[data-bind="thesis"]').textContent =
    detail.thesis_statement ? truncate(detail.thesis_statement, 220) : "No thesis statement.";

  // Header actions: rename + the primary CTA that routes to Review.
  const headerActions = main.querySelector('[data-bind="header-actions"]');
  headerActions.innerHTML = `
    <button class="btn" data-action="rename-project" title="Rename project or folder">Rename</button>
    <button class="btn primary" data-action="goto-review">Start review →</button>
  `;
  headerActions.querySelector('[data-action="goto-review"]').addEventListener("click", () => {
    navigate(`/p/${encodeURIComponent(name)}/review`);
  });
  headerActions.querySelector('[data-action="rename-project"]').addEventListener("click", () => {
    renameProjectPrompt({name, display_name: detail.display_name || name});
  });

  // Status strip: at-a-glance pipeline progression so the header
  // always shows where the project is. Each pip is now a clickable
  // shortcut that jumps to the relevant tab.
  renderStatusStrip(main, detail, outlineStatus, drafts);

  // Subnav binding.
  const tabs = main.querySelectorAll(".subnav-tab");
  const panels = main.querySelectorAll(".subnav-panel");
  tabs.forEach(tab => {
    tab.addEventListener("click", () => {
      const target = tab.dataset.section;
      navigate(`/p/${encodeURIComponent(name)}/${target}`);
    });
    tab.classList.toggle("active", tab.dataset.section === sectionId);
  });
  panels.forEach(p => {
    p.classList.toggle("visible", p.dataset.panel === sectionId);
  });

  // Render the active section's body. Tab IDs were consolidated:
  //   - "hierarchy" → "outline"  (more user-friendly term)
  //   - "audit" + "reviews" → "quality"  (one place for QA artefacts)
  //   - "run" → "review"  (the action surface)
  switch (sectionId) {
    case "dashboard":  renderDashboard(main); break;
    case "outline":    renderHierarchy(main); break;
    case "sources":    renderSources(main); break;
    case "references": renderReferences(main); break;
    case "review":     renderRunPanel(main, subSectionId); break;
    // Legacy URLs gracefully redirect to the new structure.
    case "overview":  navigate(`/p/${encodeURIComponent(state.current)}/dashboard`); break;
    case "drafts":    navigate(`/p/${encodeURIComponent(state.current)}/dashboard`); break;
    case "quality":   navigate(`/p/${encodeURIComponent(state.current)}/review`); break;
    case "hierarchy": navigate(`/p/${encodeURIComponent(state.current)}/outline`); break;
    case "audit":
    case "reviews":   navigate(`/p/${encodeURIComponent(state.current)}/review`); break;
    case "run":       navigate(`/p/${encodeURIComponent(state.current)}/review`); break;
  }
}

// ─── sources tab ─────────────────────────────────────

async function renderSources(main) {
  const panel = main.querySelector('[data-bind="sources"]');
  panel.innerHTML = `<div class="muted small">Loading…</div>`;
  const data = await fetchJSON(`/api/projects/${encodeURIComponent(state.current)}/sources`);
  state.sourcesData = data;

  const indexedHtml = data.indexed.length
    ? `<div class="indexed-summary">
         ${data.indexed.map(s => `<span class="indexed-pill">${escapeHtml(s.source_id)} · ${s.passage_count} passages</span>`).join("")}
       </div>`
    : `<p class="muted small">Nothing indexed yet. Upload sources below and click <strong>Index sources</strong>.</p>`;

  const buckets = ["papers", "prior_writing", "notes", "data", "web"];
  const projectSlug = encodeURIComponent(state.current);
  const bucketsHtml = buckets.map(b => {
    const files = data.buckets[b] || [];
    const filesHtml = files.length
      ? files.map(f => {
          const fileUrl = `/api/projects/${projectSlug}/sources/${encodeURIComponent(b)}/${encodeURIComponent(f.filename)}`;
          const actions = [];
          if (f.text_sidecar) {
            const txtUrl = `/api/projects/${projectSlug}/sources/${encodeURIComponent(b)}/${encodeURIComponent(f.text_sidecar)}`;
            actions.push(
              `<a class="bucket-action" href="${txtUrl}" target="_blank" rel="noopener">View text</a>`
            );
          }
          if (f.filename.toLowerCase().endsWith(".pdf")) {
            actions.push(
              `<a class="bucket-action" href="${fileUrl}" target="_blank" rel="noopener">PDF</a>`
            );
          } else {
            actions.push(
              `<a class="bucket-action" href="${fileUrl}" target="_blank" rel="noopener">Open</a>`
            );
          }
          return `
            <div class="bucket-file">
              <span class="name">${escapeHtml(f.filename)}</span>
              <span class="meta">${formatBytes(f.size_bytes)}</span>
              <span class="meta">${formatTimestamp(f.mtime)}</span>
              <span class="bucket-actions">${actions.join("")}</span>
            </div>`;
        }).join("")
      : `<div class="bucket-empty">No files in this bucket.</div>`;
    return `
      <div class="bucket-section">
        <div class="bucket-head">
          <h4>refs/${escapeHtml(b)}/</h4>
          <span class="muted small">${files.length} file${files.length === 1 ? "" : "s"}</span>
        </div>
        <div class="bucket-files">${filesHtml}</div>
      </div>`;
  }).join("");

  panel.innerHTML = `
    <div class="card">
      <h3 class="subhead">Indexed sources</h3>
      ${indexedHtml}
    </div>

    <div class="card">
      <div class="page-head" style="margin-bottom: 12px; padding-bottom: 12px; border-bottom: 1px solid var(--border);">
        <div>
          <h3 class="subhead" style="margin: 0 0 4px;">Add sources</h3>
          <span class="muted small">PDF · DOCX · Markdown · TXT · HTML · XLSX, up to 200 MB each.</span>
        </div>
        <div class="page-actions" style="display: flex; gap: 8px; align-items: center;">
          <select data-bind="upload-bucket" style="padding: 6px 8px; border: 1px solid var(--border); border-radius: 4px;">
            <option value="papers">papers</option>
            <option value="prior_writing">prior_writing</option>
            <option value="notes">notes</option>
            <option value="data">data</option>
            <option value="web">web</option>
          </select>
          <button class="btn" data-action="reindex">Re-index</button>
        </div>
      </div>
      <div class="dropzone" data-bind="sources-drop" style="margin-bottom: 18px;">
        <div class="dropzone-prompt">
          <svg class="dropzone-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/>
          </svg>
          <span><strong>Drop files here</strong> or
            <label class="dropzone-link">
              browse
              <input type="file" multiple accept=".pdf,.docx,.md,.markdown,.txt,.html,.htm,.xlsx" hidden data-bind="upload-input" />
            </label>
          </span>
          <span class="muted small dropzone-hint">Files land in the bucket selected above and auto-index.</span>
        </div>
      </div>
      ${bucketsHtml}
    </div>`;

  // Wire dropzone + browse + reindex.
  const sourcesDrop = panel.querySelector('[data-bind="sources-drop"]');
  if (sourcesDrop) {
    bindDropzone(sourcesDrop, files => uploadSourcesFiles(files, panel, main));
  }
  panel.querySelector('[data-bind="upload-input"]').addEventListener("change", async ev => {
    const files = Array.from(ev.target.files || []);
    if (!files.length) return;
    await uploadSourcesFiles(files, panel, main);
  });

  panel.querySelector('[data-action="reindex"]').addEventListener("click", async () => {
    const overlay = showUploadOverlay(panel, "Indexing sources…");
    try {
      const resp = await fetch(
        `/api/projects/${encodeURIComponent(state.current)}/sources/index`,
        {method: "POST"},
      );
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const result = await resp.json();
      overlay.update(`Indexed ${result.indexed_count} source(s) · ${result.skipped_count} unchanged`);
      setTimeout(() => { overlay.close(); renderSources(main); }, 700);
    } catch (err) {
      overlay.close();
      alert(`Index failed: ${err.message}`);
      renderSources(main);
    }
  });
}

async function uploadSourcesFiles(files, panel, main) {
  const bucket = panel.querySelector('[data-bind="upload-bucket"]').value;
  const totalBytes = files.reduce((n, f) => n + f.size, 0);
  const overlay = showUploadOverlay(
    panel,
    `Uploading ${files.length} file${files.length === 1 ? "" : "s"} (${formatBytes(totalBytes)}) to refs/${bucket}/…`,
  );
  try {
    const fd = new FormData();
    fd.append("bucket", bucket);
    files.forEach(f => fd.append("files", f));
    const resp = await fetch(
      `/api/projects/${encodeURIComponent(state.current)}/sources`,
      {method: "POST", body: fd},
    );
    if (!resp.ok) {
      const txt = await resp.text();
      throw new Error(txt || `HTTP ${resp.status}`);
    }
    const result = await resp.json();

    overlay.update("Indexing…");
    await fetch(
      `/api/projects/${encodeURIComponent(state.current)}/sources/index`,
      {method: "POST"},
    );

    const skipped = result.skipped || [];
    overlay.update(
      `Done — ${result.saved.length} saved` +
        (skipped.length ? `, ${skipped.length} skipped` : ""),
    );
    setTimeout(() => {
      overlay.close();
      if (skipped.length) {
        alert(
          "Some files were skipped:\n\n" +
            skipped.map(s => `${s.filename}: ${s.reason}`).join("\n"),
        );
      }
      renderSources(main);
    }, 600);
  } catch (err) {
    overlay.close();
    alert(`Upload failed: ${err.message}`);
    renderSources(main);
  }
}

function showUploadOverlay(panel, message) {
  const overlay = document.createElement("div");
  overlay.className = "upload-overlay";
  overlay.innerHTML = `
    <div class="upload-overlay-card">
      <div class="upload-spinner"></div>
      <div class="upload-overlay-msg">${escapeHtml(message)}</div>
    </div>`;
  panel.style.position = "relative";
  panel.appendChild(overlay);
  return {
    update: msg => {
      const el = overlay.querySelector(".upload-overlay-msg");
      if (el) el.textContent = msg;
    },
    close: () => overlay.remove(),
  };
}

// ─── overview ─────────────────────────────────────────

async function renderDashboard(main) {
  const panel = main.querySelector('[data-bind="dashboard"]');
  panel.innerHTML = `<div class="muted small" style="padding: 12px;">Loading dashboard…</div>`;
  const d = state.detail;
  const h = state.hierarchy;
  const outlineStatus = state.outlineStatus;
  const projSlug = encodeURIComponent(state.current);

  // Fetch the bits the dashboard wants that aren't in the detail load:
  // originals (paper PDF + raw text), changelogs index, and recent
  // audit summary. All optional — render gracefully if any fail.
  const [originalsRes, changelogsRes, auditRes] = await Promise.allSettled([
    fetchJSON(`/api/projects/${projSlug}/originals`),
    fetchJSON(`/api/projects/${projSlug}/changelogs`),
    fetchJSON(`/api/projects/${projSlug}/audit`),
  ]);
  const originals = (originalsRes.status === "fulfilled" ? originalsRes.value.originals : []) || [];
  const changelogs = (changelogsRes.status === "fulfilled" ? changelogsRes.value.changelogs : []) || [];
  // Defensive: the audit endpoint historically returned a list,
  // briefly returned a per-voice dict, and now returns a flat list
  // again. Coerce so the dashboard never crashes on shape drift.
  let auditFlags = [];
  if (auditRes.status === "fulfilled" && auditRes.value) {
    const raw = auditRes.value.flags;
    if (Array.isArray(raw)) {
      auditFlags = raw;
    } else if (raw && typeof raw === "object") {
      auditFlags = Object.values(raw).flat();
    }
  }

  const drafts = state.drafts || [];
  const runHistory = state.runHistory || {history: [], summary: {}};
  const lastRun = runHistory.history?.slice(-1)[0];
  const completedLevels = runHistory.summary?.levels_completed_successfully || [];

  // ── 1. Action items panel — what the user should do next ──
  const actionItems = computeActionItems({
    outlineStatus, drafts, completedLevels, auditFlags, sourcesIndexed: 0,
  });

  // ── 2. Paper preview source: default to the current outline (the
  //   structured argument the user is working on), with the rendered
  //   drafts and other originals available via the dropdown. The
  //   outline is the live source of truth — drafts are derivatives.
  const wordCountLabel = wc => wc ? ` (${wc.toLocaleString()} words)` : "";
  const paperOptions = [];
  const currentOutline = originals.find(o => o.role === "current_outline");
  const rawTextOriginal = originals.find(o => o.role === "raw_text");
  const currentOutlineWords = currentOutline?.word_count || 0;
  const rawWords = rawTextOriginal?.word_count || 0;
  if (currentOutline) {
    paperOptions.push({
      value: `original:${currentOutline.filename}:${currentOutline.kind}`,
      label: `Current outline${wordCountLabel(currentOutlineWords)}`,
    });
  }
  const currentDraft = drafts.find(dr => dr.is_current);
  if (currentDraft) {
    paperOptions.push({
      value: `draft:${currentDraft.filename}`,
      label: `Current draft${wordCountLabel(currentDraft.word_count)}`,
    });
  }
  drafts.filter(dr => !dr.is_current).slice(0, 5).forEach(dr => {
    paperOptions.push({
      value: `draft:${dr.filename}`,
      label: `${dr.filename}${wordCountLabel(dr.word_count)}`,
    });
  });
  originals
    .filter(o => o.role !== "current_outline")
    .forEach(o => {
      const suffix = o.kind === "markdown" ? wordCountLabel(o.word_count) : "";
      paperOptions.push({
        value: `original:${o.filename}:${o.kind}`,
        label: `${o.label}${suffix}`,
      });
    });
  const defaultPaperKey = paperOptions[0]?.value || "";

  // Word-count strip — shows raw input → outline → draft pipeline at
  // a glance so the user can see how the structuring + render shaped
  // the document.
  const wcSegments = [];
  if (rawTextOriginal) {
    wcSegments.push(`<span><strong>${rawWords.toLocaleString()}</strong> raw words</span>`);
  }
  if (currentOutlineWords) {
    wcSegments.push(`<span><strong>${currentOutlineWords.toLocaleString()}</strong> outline words</span>`);
  }
  if (currentDraft) {
    wcSegments.push(`<span><strong>${currentDraft.word_count.toLocaleString()}</strong> rendered words</span>`);
  }
  const wcStripHtml = wcSegments.length
    ? `<div class="paper-wordcounts">${wcSegments.join('<span class="wc-sep">→</span>')}</div>`
    : "";

  // ── Build the dashboard layout ──
  panel.innerHTML = `
    <div class="dashboard-grid">
      <div class="dashboard-col-main">
        <div class="card paper-preview-card">
          <div class="paper-preview-head">
            <div class="paper-preview-title">
              <h3 class="subhead" style="margin: 0 0 2px;">The paper</h3>
              <div class="paper-preview-meta-row">
                <span class="muted small" data-bind="paper-source-meta"></span>
                <a class="btn sm hidden" data-bind="paper-source-download" download>Download</a>
              </div>
            </div>
            ${paperOptions.length
              ? `<label class="paper-source-label">
                  <span class="muted small">Showing:</span>
                  <select data-bind="paper-source" class="paper-source-select">
                    ${paperOptions.map(o =>
                      `<option value="${escapeAttr(o.value)}">${escapeHtml(o.label)}</option>`
                    ).join("")}
                  </select>
                </label>`
              : ""}
          </div>
          ${wcStripHtml}
          <div class="paper-preview-body" data-bind="paper-preview">
            ${paperOptions.length
              ? `<div class="muted small">Loading…</div>`
              : `<div class="empty-state" style="padding: 24px 0;">
                  <p>No paper produced yet. Set up an outline, then run a review.</p>
                  <div style="display: flex; gap: 8px; margin-top: 12px; justify-content: center;">
                    <button class="btn primary" data-action="goto-outline">Go to Outline</button>
                    <button class="btn" data-action="goto-review">Run a review</button>
                  </div>
                </div>`}
          </div>
          <div class="paper-preview-footer" data-bind="paper-source-footer"></div>
        </div>

        ${actionItems.length
          ? `<div class="card action-items-card">
              <h3 class="subhead">Action items</h3>
              <ul class="action-items">
                ${actionItems.map(item => `
                  <li class="action-item ${item.severity}">
                    <span class="dot ${item.severity}"></span>
                    <div>
                      <strong>${escapeHtml(item.title)}</strong>
                      <span class="muted small">${item.body}</span>
                    </div>
                    ${item.cta ? `<button class="btn sm" data-action="goto-${item.cta.tab}"${item.cta.subtab ? ` data-subtab="${escapeAttr(item.cta.subtab)}"` : ""}>${escapeHtml(item.cta.label)} →</button>` : ""}
                  </li>`).join("")}
              </ul>
            </div>`
          : ""}
      </div>

      <div class="dashboard-col-side">
        ${renderDashboardLatestRunCard(lastRun)}
        ${renderDashboardRecentActivityCard(runHistory.history?.slice(-5).reverse() || [], changelogs)}
        ${renderDashboardThesisCard(h)}
      </div>
    </div>`;

  // Wire up paper source dropdown.
  const sourceSelect = panel.querySelector('[data-bind="paper-source"]');
  const previewBody = panel.querySelector('[data-bind="paper-preview"]');
  const previewMeta = panel.querySelector('[data-bind="paper-source-meta"]');
  const previewDownload = panel.querySelector('[data-bind="paper-source-download"]');
  const previewFooter = panel.querySelector('[data-bind="paper-source-footer"]');

  function updatePaperHeader(key) {
    if (!previewMeta || !previewFooter) return;
    const [kind, filename] = (key || "").split(":");
    let metaText = "";
    let footerHtml = "";
    if (previewDownload) {
      previewDownload.classList.add("hidden");
      previewDownload.removeAttribute("href");
    }
    if (kind === "draft" && filename) {
      const draftRecord = drafts.find(dr => dr.filename === filename);
      const fullPath = draftRecord
        ? draftRecord.path
        : `outputs/${filename}`;
      metaText = `outputs/${filename}`;
      footerHtml = `
        <span class="muted small">📄 The rewritten paper · saved at <code class="mono">${escapeHtml(fullPath)}</code></span>`;
      if (previewDownload) {
        previewDownload.href =
          `/api/projects/${projSlug}/drafts/${encodeURIComponent(filename)}`;
        previewDownload.setAttribute("download", filename);
        previewDownload.classList.remove("hidden");
      }
    } else if (kind === "original" && filename) {
      const o = originals.find(or => or.filename === filename);
      metaText = `structure/${filename}`;
      const url = `/api/projects/${projSlug}/originals/${encodeURIComponent(filename)}`;
      footerHtml = `
        <span class="muted small">${escapeHtml(o ? o.label : filename)} · the input source</span>
        <a class="btn sm" href="${url}" target="_blank" rel="noopener">Open</a>`;
    }
    previewMeta.textContent = metaText;
    previewFooter.innerHTML = footerHtml;
  }

  async function loadPaperPreview(key) {
    if (!key) return;
    updatePaperHeader(key);
    previewBody.innerHTML = `<div class="muted small">Loading…</div>`;
    const [kind, filename, fileKind] = key.split(":");
    try {
      if (kind === "draft") {
        const text = await fetchText(`/api/projects/${projSlug}/drafts/${encodeURIComponent(filename)}`);
        previewBody.innerHTML = `<article class="prose">${renderMarkdown(text)}</article>`;
      } else if (kind === "original") {
        const url = `/api/projects/${projSlug}/originals/${encodeURIComponent(filename)}`;
        if (fileKind === "pdf") {
          previewBody.innerHTML = `
            <p class="muted small">PDF · <a href="${url}" target="_blank" rel="noopener">open in new tab</a></p>
            <iframe src="${url}" class="pdf-frame" title="${escapeHtml(filename)}"></iframe>`;
        } else if (fileKind === "docx") {
          previewBody.innerHTML = `
            <p class="muted small">Word document — <a href="${url}" target="_blank" rel="noopener">download <code>${escapeHtml(filename)}</code></a> to view.</p>`;
        } else {
          const text = await fetchText(url);
          previewBody.innerHTML = `<article class="prose">${renderMarkdown(text)}</article>`;
        }
      }
    } catch (err) {
      previewBody.innerHTML = `<div class="muted small">Failed to load: ${escapeHtml(err.message)}</div>`;
    }
  }
  if (sourceSelect) {
    sourceSelect.addEventListener("change", e => loadPaperPreview(e.target.value));
    loadPaperPreview(defaultPaperKey);
  }

  // Wire up tab-jump buttons across the dashboard.
  panel.querySelectorAll("[data-action^='goto-']").forEach(btn => {
    btn.addEventListener("click", () => {
      const tab = btn.dataset.action.replace(/^goto-/, "");
      const subtab = btn.dataset.subtab;
      navigate(subtab
        ? `/p/${projSlug}/${tab}/${encodeURIComponent(subtab)}`
        : `/p/${projSlug}/${tab}`);
    });
  });

  // "View the rewrite" → switch paper dropdown to that draft + scroll.
  const viewRewriteBtn = panel.querySelector('[data-action="view-rewrite"]');
  if (viewRewriteBtn) {
    viewRewriteBtn.addEventListener("click", () => {
      const filename = viewRewriteBtn.dataset.filename;
      if (sourceSelect && filename) {
        const value = `draft:${filename}`;
        // If the option exists, select it and trigger load.
        const opt = Array.from(sourceSelect.options).find(o => o.value === value);
        if (opt) {
          sourceSelect.value = value;
          loadPaperPreview(value);
          // Scroll the preview into view so the user actually sees it.
          const card = panel.querySelector(".paper-preview-card");
          if (card) card.scrollIntoView({behavior: "smooth", block: "start"});
        }
      }
    });
  }

  // Wire up "view changelog" links inside the recent activity card.
  panel.querySelectorAll("[data-changelog]").forEach(btn => {
    btn.addEventListener("click", async (ev) => {
      ev.preventDefault();
      const filename = btn.dataset.changelog;
      const target = btn.parentElement;
      try {
        const text = await fetchText(
          `/api/projects/${projSlug}/changelogs/${encodeURIComponent(filename)}`);
        // Inline expansion below the row.
        const existing = target.parentElement.querySelector(".changelog-inline");
        if (existing) existing.remove();
        const block = document.createElement("div");
        block.className = "changelog-inline";
        block.innerHTML = `<article class="prose">${renderMarkdown(text)}</article>`;
        target.parentElement.appendChild(block);
      } catch (err) {
        alert("Failed to load changelog: " + err.message);
      }
    });
  });
}

function computeActionItems({outlineStatus, drafts, completedLevels, auditFlags}) {
  const items = [];
  if (!outlineStatus || !outlineStatus.outline.exists) {
    items.push({
      severity: "bad",
      title: "No outline yet",
      body: "Add a `# THESIS` and `# A. Section` outline before running a review.",
      cta: {tab: "outline", label: "Go to Outline"},
    });
  } else if (!outlineStatus.outline.is_structured) {
    items.push({
      severity: "warn",
      title: "Outline is raw prose",
      body: "Lattice will auto-structure on next review, or you can edit it now.",
      cta: {tab: "review", label: "Run review"},
    });
  }
  if (drafts.length === 0 && outlineStatus?.outline.is_structured) {
    items.push({
      severity: "warn",
      title: "Outline is ready, no draft yet",
      body: "Run a Quick review to render the prose for the first time.",
      cta: {tab: "review", label: "Run review"},
    });
  }
  const criticalFlags = auditFlags.filter(f => f.severity === "critical").length;
  if (criticalFlags > 0) {
    items.push({
      severity: "bad",
      title: `${criticalFlags} critical audit flag${criticalFlags === 1 ? "" : "s"}`,
      body: "Critical issues block delivery. Re-run with autocorrect or address by hand.",
      cta: {tab: "review", subtab: "audit", label: "View flags"},
    });
  }
  if (drafts.length > 0 && !completedLevels.includes("standard") && !completedLevels.includes("deep")) {
    items.push({
      severity: "info",
      title: "Draft exists but no audit run",
      body: "Standard review adds audit + voice review on top of the existing draft.",
      cta: {tab: "review", label: "Run Standard"},
    });
  }
  return items;
}

function renderDashboardLatestRunCard(lastRun) {
  if (!lastRun) {
    return `
      <div class="card">
        <h3 class="subhead">Latest review</h3>
        <p class="muted small">No reviews yet.</p>
        <button class="btn primary sm" data-action="goto-review">Run your first review →</button>
      </div>`;
  }
  const ok = lastRun.finalise_succeeded;
  const finished = formatTimestamp(new Date(lastRun.finished_at).getTime() / 1000);
  const finalPath = lastRun.final_path || "";
  const finalFilename = finalPath ? finalPath.split(/[\\/]/).pop() : "";
  return `
    <div class="card">
      <h3 class="subhead">Latest review</h3>
      <div class="kv-list">
        <div class="kv"><span class="k">Level</span><span class="v"><span class="pill">${escapeHtml(lastRun.level)}</span></span></div>
        <div class="kv"><span class="k">Outcome</span><span class="v">${ok ? `<span class="pill ok">delivered</span>` : `<span class="pill bad">blocked</span>`}</span></div>
        <div class="kv"><span class="k">Clusters</span><span class="v">${lastRun.rendered_clusters} of ${lastRun.total_clusters}</span></div>
        <div class="kv"><span class="k">Audit flags</span><span class="v">${lastRun.audit_flags || 0}</span></div>
        <div class="kv"><span class="k">Duration</span><span class="v">${formatDuration(lastRun.elapsed_seconds)}</span></div>
        <div class="kv"><span class="k">Finished</span><span class="v">${finished}</span></div>
      </div>
      ${ok && finalFilename
        ? `<button class="btn primary sm" style="margin-top: 12px; width: 100%;" data-action="view-rewrite" data-filename="${escapeAttr(finalFilename)}">View the rewrite →</button>`
        : ""}
    </div>`;
}

function renderDashboardRecentActivityCard(history, changelogs) {
  if (!history.length && !changelogs.length) return "";
  const rows = history.map(r => {
    const finished = formatTimestamp(new Date(r.finished_at).getTime() / 1000);
    const matchingLog = changelogs.find(cl => cl.filename.startsWith(
      r.finished_at.slice(0, 4) + r.finished_at.slice(5, 7) + r.finished_at.slice(8, 10)
    ) && cl.filename.includes(r.level));
    const okPill = r.finalise_succeeded ? `<span class="pill ok">✓</span>` : `<span class="pill bad">✗</span>`;
    return `
      <li class="activity-row">
        ${okPill}
        <div class="activity-meta">
          <strong>${escapeHtml(r.level)}</strong>
          <span class="muted small">${finished} · ${formatDuration(r.elapsed_seconds)}</span>
        </div>
        <div class="activity-stats muted small">
          ${r.rendered_clusters}/${r.total_clusters} clusters · ${r.audit_flags || 0} flags
        </div>
        <div>${matchingLog
          ? `<button class="btn-link sm" data-changelog="${escapeAttr(matchingLog.filename)}">view changes</button>`
          : ""}</div>
      </li>`;
  }).join("");
  return `
    <div class="card">
      <h3 class="subhead">Recent activity</h3>
      <ul class="activity-list">${rows}</ul>
      <button class="btn-link sm" data-action="goto-review">Full history in Review tab →</button>
    </div>`;
}

function renderDashboardThesisCard(h) {
  if (!h) return "";
  if (h.thesis_statement) {
    return `
      <div class="card">
        <h3 class="subhead">Thesis</h3>
        <p style="margin: 0; line-height: 1.5;">${escapeHtml(h.thesis_statement)}</p>
        ${h.thesis_argued && h.thesis_argued !== h.thesis_statement
          ? `<p class="muted small" style="margin: 8px 0 0;"><strong>What body argues:</strong> ${escapeHtml(h.thesis_argued)}</p>`
          : ""}
      </div>`;
  }
  return `
    <div class="card">
      <h3 class="subhead">Thesis</h3>
      <p class="muted small">No thesis statement set. Add one at the top of <code>structure/outline.md</code>.</p>
    </div>`;
}

function renderOutlineStatusHtml(status) {
  if (!status) {
    return `<p class="muted small">Could not read outline status.</p>`;
  }
  const o = status.outline;
  const g = status.graph;
  const r = status.raw_archive;

  // Decide the headline state. Three primary states:
  //   • "missing"      — no outline file at all
  //   • "raw"          — outline exists but is raw prose
  //   • "structured"   — outline exists in lattice format
  // …and an orthogonal ingest sub-state telling the user whether the
  // graph reflects the current outline.
  let headline;
  if (!o.exists) {
    headline = `<div class="outline-state bad">
      <span class="dot"></span>
      <strong>No outline uploaded</strong>
      <span class="muted small">Upload one in the Sources area or paste it into structure/outline.md.</span>
    </div>`;
  } else if (!o.is_structured) {
    headline = `<div class="outline-state warn">
      <span class="dot"></span>
      <strong>Raw prose detected — needs structuring</strong>
      <span class="muted small">Click <strong>Start review</strong> and Lattice will use Claude to extract a thesis, sections, and claim bullets before ingesting. The original will be archived to <code>outline.raw.md</code>.</span>
    </div>`;
  } else {
    headline = `<div class="outline-state ok">
      <span class="dot"></span>
      <strong>Outline ready (lattice format)</strong>
      <span class="muted small">Headers parsed. Click Start review to ingest and render.</span>
    </div>`;
  }

  const stats = [];
  if (o.exists) {
    stats.push(`<div class="kv"><span class="k">File</span><span class="v mono">${escapeHtml(o.filename)} · ${formatBytes(o.size_bytes)} · ${formatTimestamp(o.mtime)}</span></div>`);
    stats.push(`<div class="kv"><span class="k">Format</span><span class="v">${escapeHtml(o.format)}${o.is_structured ? " · structured" : " · raw prose"}</span></div>`);
  }
  if (g.exists && !g.corrupt) {
    const fresh = g.section_count > 0 && g.claim_count > 0;
    stats.push(`<div class="kv"><span class="k">Last ingest</span><span class="v">${g.section_count} sections · ${g.claim_count} claims · ${formatTimestamp(g.mtime)}${fresh ? "" : " <span class=\"pill bad\" style=\"margin-left:6px;\">empty</span>"}</span></div>`);
  } else if (g.exists && g.corrupt) {
    stats.push(`<div class="kv"><span class="k">Last ingest</span><span class="v">graph file is corrupt</span></div>`);
  } else {
    stats.push(`<div class="kv"><span class="k">Last ingest</span><span class="v muted">never run</span></div>`);
  }
  if (r.exists) {
    stats.push(`<div class="kv"><span class="k">Original archived</span><span class="v"><code class="path-pill">${escapeHtml(r.filename)}</code> · ${formatBytes(r.size_bytes)} · ${formatTimestamp(r.mtime)}</span></div>`);
  }

  let preview = "";
  if (o.exists && o.preview) {
    preview = `<details class="outline-preview"><summary>Preview first 600 characters</summary><pre>${escapeHtml(o.preview)}</pre></details>`;
  }

  return headline + `<div class="kv-list">${stats.join("")}</div>` + preview;
}

// ─── hierarchy ────────────────────────────────────────

function renderHierarchy(main) {
  const panel = main.querySelector('[data-bind="outline"]');
  const h = state.hierarchy;

  if (!h.sections.length) {
    panel.innerHTML = `<div class="empty-state"><h3>No sections</h3><p>Run <code>lattice ingest</code> to parse an outline.</p></div>`;
    return;
  }

  // Toolbar: switch between tree view and interactive graph view.
  // Also: expand-all / collapse-all + export-to-PPTX.
  const projSlug = encodeURIComponent(state.current);
  const toolbarHtml = `
    <div class="graph-viz-toolbar">
      <div class="muted small">
        <strong>${h.totals.sections}</strong> sections ·
        <strong>${h.totals.clusters}</strong> clusters ·
        <strong>${h.totals.claims}</strong> claims ·
        <strong>${h.totals.relationships}</strong> relationships
      </div>
      <div style="display: flex; gap: 6px; flex-wrap: wrap;">
        <button class="btn sm active" data-view="tree">Tree view</button>
        <button class="btn sm" data-view="graph">Interactive graph</button>
        <button class="btn sm" data-action="expand-all">Expand all</button>
        <button class="btn sm" data-action="collapse-all">Collapse all</button>
        <a class="btn sm" href="/api/projects/${projSlug}/export/teaching-deck" download>Export to PowerPoint</a>
      </div>
    </div>`;

  const sectionsHtml = h.sections.map(s => {
    const clustersHtml = s.clusters.map(c => {
      const claimsHtml = c.claims.map(cl => renderClaimNode(cl)).join("");
      return `
        <div class="tree-cluster">
          <div class="tree-cluster-head" data-toggle="cluster">
            <svg class="chevron-sm" width="10" height="10" viewBox="0 0 12 12"><path d="M3 4 L6 8 L9 4" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>
            <span><code class="mono" style="font-size: 11px;">${escapeHtml(c.cluster_id)}</code> · ${escapeHtml(c.role || "")}</span>
            <span class="muted small">${c.claims.length} claim${c.claims.length === 1 ? "" : "s"} · target ${c.target_words_min}–${c.target_words_max} words</span>
          </div>
          <div class="tree-claims">${claimsHtml}</div>
        </div>`;
    }).join("");

    return `
      <div class="tree-section collapsed">
        <div class="tree-section-head" data-toggle="section">
          <svg class="chevron" width="14" height="14" viewBox="0 0 12 12"><path d="M3 4 L6 8 L9 4" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>
          <span class="tree-section-title">${escapeHtml(s.title || s.section_id)}</span>
          <div class="tree-section-meta">
            <span class="pill">${escapeHtml(s.role || "argumentative")}</span>
            <span class="pill mono">${s.clusters.length} clusters</span>
          </div>
        </div>
        <div class="tree-section-body">${clustersHtml || '<p class="muted small">No clusters in this section yet.</p>'}</div>
      </div>`;
  }).join("");

  panel.innerHTML = `
    ${toolbarHtml}
    <div class="hierarchy-tree-view"><div class="tree">${sectionsHtml}</div></div>
    <div class="hierarchy-graph-view hidden"></div>`;

  // Bind toggles.
  panel.querySelectorAll('[data-toggle="section"]').forEach(el => {
    el.addEventListener("click", () => el.parentElement.classList.toggle("collapsed"));
  });
  panel.querySelectorAll('[data-toggle="cluster"]').forEach(el => {
    el.addEventListener("click", () => el.parentElement.classList.toggle("collapsed"));
  });

  // Expand-all / Collapse-all toolbar actions.
  const expandBtn = panel.querySelector('[data-action="expand-all"]');
  if (expandBtn) {
    expandBtn.addEventListener("click", () => {
      panel.querySelectorAll(".tree-section").forEach(s => s.classList.remove("collapsed"));
    });
  }
  const collapseBtn = panel.querySelector('[data-action="collapse-all"]');
  if (collapseBtn) {
    collapseBtn.addEventListener("click", () => {
      panel.querySelectorAll(".tree-section").forEach(s => s.classList.add("collapsed"));
    });
  }

  // Bind view-switcher buttons.
  const treeView = panel.querySelector(".hierarchy-tree-view");
  const graphView = panel.querySelector(".hierarchy-graph-view");
  panel.querySelectorAll("[data-view]").forEach(btn => {
    btn.addEventListener("click", () => {
      panel.querySelectorAll("[data-view]").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      const target = btn.dataset.view;
      treeView.classList.toggle("hidden", target !== "tree");
      graphView.classList.toggle("hidden", target !== "graph");
      if (target === "graph" && !graphView.dataset.loaded) {
        graphView.innerHTML = `
          <iframe class="graph-viz-frame" src="/api/projects/${encodeURIComponent(state.current)}/graph-viz" loading="lazy"></iframe>
          <p class="muted small" style="text-align: right; margin-top: 6px;">
            Interactive cytoscape.js layout · drag nodes, scroll to zoom, click for details
          </p>`;
        graphView.dataset.loaded = "1";
      }
    });
  });
}

function renderClaimNode(claim) {
  const tags = [];
  if (claim.author_origin) tags.push(`<span class="tag author">author</span>`);
  if (claim.evidence_count > 0) tags.push(`<span class="tag evidence">${claim.evidence_count} bound</span>`);
  else if (!claim.author_origin) tags.push(`<span class="tag no-evidence">no evidence</span>`);
  if (claim.role_in_cluster) tags.push(`<span class="tag">${escapeHtml(claim.role_in_cluster)}</span>`);
  if (claim.importance >= 0.7) tags.push(`<span class="tag" style="background: var(--accent-soft); color: var(--accent); border-color: transparent;">important</span>`);

  const rOut = claim.rels_out || [];
  const rIn = claim.rels_in || [];
  let relsHtml = "";
  if (rOut.length || rIn.length) {
    const outRows = rOut.map(r => `
      <li><span class="rel-arrow">→</span><code class="mono">${escapeHtml(r.type)}</code> <code class="mono">${escapeHtml(r.other_claim)}</code>${r.note ? ` <span class="muted small">— ${escapeHtml(r.note)}</span>` : ""}</li>`).join("");
    const inRows = rIn.map(r => `
      <li><span class="rel-arrow">←</span><code class="mono">${escapeHtml(r.type)}</code> from <code class="mono">${escapeHtml(r.other_claim)}</code>${r.note ? ` <span class="muted small">— ${escapeHtml(r.note)}</span>` : ""}</li>`).join("");
    relsHtml = `
      <details class="tree-claim-rels">
        <summary>${rOut.length + rIn.length} relationship${(rOut.length + rIn.length) === 1 ? "" : "s"} (${rOut.length} out · ${rIn.length} in)</summary>
        <ul>${outRows}${inRows}</ul>
      </details>`;
    tags.push(`<span class="tag rel-tag">${rOut.length + rIn.length} rel</span>`);
  }

  return `
    <div class="tree-claim">
      <div class="tree-claim-head">
        <code class="tree-claim-id">${escapeHtml(claim.claim_id)}</code>
        <div class="tree-claim-tags">${tags.join("")}</div>
      </div>
      <div class="tree-claim-text">${escapeHtml(claim.statement)}</div>
      ${claim.mechanism ? `<div class="tree-claim-mech">${escapeHtml(claim.mechanism)}</div>` : ""}
      ${relsHtml}
    </div>`;
}

// ─── references ──────────────────────────────────────

const REFERENCE_STYLES = [
  ["harvard", "Harvard"],
  ["apa", "APA (7th ed.)"],
  ["chicago_author_date", "Chicago (author-date)"],
  ["mla", "MLA (9th ed.)"],
  ["vancouver", "Vancouver"],
  ["ieee", "IEEE"],
];

async function renderReferences(main) {
  const panel = main.querySelector('[data-bind="references"]');
  panel.innerHTML = `<div class="muted small">Loading references…</div>`;

  // Persist user's chosen style across renders within the session.
  state.referenceStyle = state.referenceStyle || "harvard";
  let manifest;
  try {
    manifest = await fetchJSON(
      `/api/projects/${encodeURIComponent(state.current)}/references` +
      `?style=${encodeURIComponent(state.referenceStyle)}`
    );
  } catch (err) {
    panel.innerHTML = `<div class="empty-state"><h3>Could not load references</h3><p>${escapeHtml(err.message)}</p></div>`;
    return;
  }

  const totals = manifest.totals || {};
  const styleOptions = REFERENCE_STYLES.map(([v, l]) =>
    `<option value="${escapeAttr(v)}"${v === manifest.style ? " selected" : ""}>${escapeHtml(l)}</option>`
  ).join("");

  const projSlug = encodeURIComponent(state.current);

  // Single consolidated toolbar — totals on the left, all actions
  // on the right. The file links + AI refresh + add-manual all live
  // here so the page stops bouncing the user between two stacked
  // action cards.
  const toolbarHtml = `
    <div class="card refs-toolbar">
      <div class="refs-toolbar-stats">
        <div class="refs-stat">
          <strong>${totals.source_count}</strong>
          <span class="muted small">source${totals.source_count === 1 ? "" : "s"}</span>
        </div>
        <div class="refs-stat">
          <strong>${totals.used_count}</strong>
          <span class="muted small">cited</span>
        </div>
        <div class="refs-stat">
          <strong>${totals.total_usages}</strong>
          <span class="muted small">citation${totals.total_usages === 1 ? "" : "s"}</span>
        </div>
      </div>
      <div class="refs-toolbar-actions">
        <label class="refs-style-picker">
          <span class="muted small">Style:</span>
          <select data-bind="style-select">${styleOptions}</select>
        </label>
        <details class="refs-files-menu">
          <summary class="btn sm primary" title="Add or update references">Populate references ▾</summary>
          <div class="refs-files-dropdown">
            <button class="btn-link" data-action="extract-from-outline" title="Pull citations out of the original paper text">Extract from paper</button>
            <button class="btn-link" data-action="add-manual" title="Type a single citation by hand">Populate manually</button>
            <button class="btn-link" data-action="refresh-ai" title="Use Claude to verify and enrich existing references">AI check references ✨</button>
          </div>
        </details>
        <details class="refs-files-menu">
          <summary class="btn sm" title="Persisted references file">Files ▾</summary>
          <div class="refs-files-dropdown">
            <a href="/api/projects/${projSlug}/references-file?fmt=md" target="_blank" rel="noopener">View references.md</a>
            <a href="/api/projects/${projSlug}/references-file?fmt=json" target="_blank" rel="noopener">View references.json</a>
            <button class="btn-link" data-action="resave-references">Regenerate now</button>
          </div>
        </details>
      </div>
    </div>`;

  if (!manifest.references.length) {
    panel.innerHTML = `
      ${toolbarHtml}
      <div class="card refs-empty">
        <h3 class="subhead">No references yet</h3>
        <p class="muted small">Click <strong>Populate references ▾</strong> in the toolbar above, then pick one of:</p>
        <ul class="refs-empty-list">
          <li><strong>Extract from paper</strong> — let Claude pull the bibliography section out of the raw paper text (<code>structure/outline.raw.md</code>).</li>
          <li><strong>Populate manually</strong> — type in a single citation by hand.</li>
          <li><strong>AI check references ✨</strong> — for sources already indexed, ask Claude to add summaries, key findings, citation estimates, and per-claim usage roles.</li>
        </ul>
        <p class="muted small" style="margin-top: 10px;">Or drop files into the <strong>Sources</strong> tab → <em>papers</em> bucket, then return here.</p>
      </div>`;
    wireReferencesFileActions(panel, projSlug);
    return;
  }

  const refsHtml = manifest.references.map(r => renderReferenceCard(r, manifest.style)).join("");
  panel.innerHTML = `
    ${toolbarHtml}
    <div class="references-list">${refsHtml}</div>
    <details class="card refs-bibliography-block">
      <summary>
        <strong>Full bibliography</strong>
        <span class="muted small">${manifest.style.replace("_", " ")} · ${manifest.references.length} entries · click to expand</span>
      </summary>
      <ol class="references-bibliography">
        ${manifest.references.map(r => `
          <li>${renderInlineMarkdown(r.formatted.bibliography)}</li>`).join("")}
      </ol>
    </details>`;

  // Style switcher → re-fetch + re-render in place.
  panel.querySelector('[data-bind="style-select"]').addEventListener("change", e => {
    state.referenceStyle = e.target.value;
    renderReferences(main);
  });

  wireReferencesFileActions(panel, projSlug);

  // Wire 'Save about' inputs.
  panel.querySelectorAll("[data-action='save-about']").forEach(btn => {
    btn.addEventListener("click", async () => {
      const sourceId = btn.dataset.sourceId;
      const textarea = panel.querySelector(`[data-bind="about-input"][data-source-id="${escapeAttr(sourceId)}"]`);
      if (!textarea) return;
      const original = btn.textContent;
      btn.disabled = true;
      btn.textContent = "Saving…";
      try {
        const resp = await fetch(
          `/api/projects/${encodeURIComponent(state.current)}/references/${encodeURIComponent(sourceId)}/about`,
          {
            method: "PUT",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({about: textarea.value}),
          }
        );
        if (!resp.ok) {
          alert("Save failed: " + await resp.text());
        } else {
          btn.textContent = "Saved";
          setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 1200);
          return;
        }
      } catch (err) {
        alert("Network error: " + err.message);
      }
      btn.disabled = false;
      btn.textContent = original;
    });
  });
}

function wireReferencesFileActions(panel, projSlug) {
  // Close the dropdown menu after any of its buttons is clicked.
  // Without this, clicking "Extract from paper" leaves the menu
  // hanging open behind the modal, which feels broken.
  panel.querySelectorAll(".refs-files-dropdown button, .refs-files-dropdown a").forEach(el => {
    el.addEventListener("click", () => {
      const parent = el.closest("details");
      if (parent) parent.open = false;
    });
  });

  // Manual regenerate → POST /references/save with cited_only=true.
  const saveBtn = panel.querySelector('[data-action="resave-references"]');
  if (saveBtn) {
    saveBtn.addEventListener("click", async (ev) => {
      const target = ev.currentTarget;
      const original = target.textContent;
      target.disabled = true;
      target.textContent = "Writing…";
      try {
        const resp = await fetch(`/api/projects/${projSlug}/references/save`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({cited_only: true}),
        });
        if (!resp.ok) {
          alert("Save failed: " + await resp.text());
        } else {
          target.textContent = "Saved ✓";
          setTimeout(() => {
            target.textContent = original;
            target.disabled = false;
          }, 1400);
          return;
        }
      } catch (err) {
        alert("Network error: " + err.message);
      }
      target.textContent = original;
      target.disabled = false;
    });
  }

  // Extract from outline.raw.md → preview citations → confirm → accept.
  const extractBtn = panel.querySelector('[data-action="extract-from-outline"]');
  if (extractBtn) {
    extractBtn.addEventListener("click", async () => {
      extractBtn.disabled = true;
      const original = extractBtn.textContent;
      extractBtn.textContent = "Asking Claude…";
      try {
        const resp = await fetch(
          `/api/projects/${projSlug}/references/extract`,
          {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({source: "outline"}),
          }
        );
        if (!resp.ok) {
          alert("Extraction failed: " + await resp.text());
          return;
        }
        const data = await resp.json();
        if (!data.citations || !data.citations.length) {
          alert(`No references found in ${data.source_label}. The paper may not contain a Bibliography section, or the section text was not preserved.`);
          return;
        }
        openExtractPreviewModal(data, projSlug);
      } catch (err) {
        alert("Network error: " + err.message);
      } finally {
        extractBtn.disabled = false;
        extractBtn.textContent = original;
      }
    });
  }

  // Manual add → form modal.
  const manualBtn = panel.querySelector('[data-action="add-manual"]');
  if (manualBtn) {
    manualBtn.addEventListener("click", () => {
      openManualReferenceModal(projSlug);
    });
  }

  // AI check references → POST /references/refresh-ai → re-render.
  // Because the button lives inside a dropdown that closes on click,
  // we show a sticky status banner at the top of the panel so the
  // user can see something is happening (this can take 10-30s).
  const aiBtn = panel.querySelector('[data-action="refresh-ai"]');
  if (aiBtn) {
    aiBtn.addEventListener("click", async () => {
      const ok = window.confirm(
        "Run AI check over your references?\n\n"
        + "Claude will add a summary, key findings, an estimated "
        + "citation count, the work's standing in its field, and a "
        + "per-claim explanation of how each citation is used.\n\n"
        + "This calls the LLM once per cited reference — typically "
        + "10-30 seconds total."
      );
      if (!ok) return;

      const banner = showRefsBanner(panel, "loading",
        "Asking Claude to check references…",
        "This typically takes 10-30 seconds. The cards will refresh automatically when it's done."
      );

      try {
        // ``cited_only: false`` so we enrich every indexed source,
        // not just those already bound to claims via Evidence — the
        // user wants the AI summary regardless of whether they've
        // wired the citation into a specific claim yet.
        const resp = await fetch(
          `/api/projects/${projSlug}/references/refresh-ai`,
          {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({cited_only: false}),
          }
        );
        if (!resp.ok) {
          const errText = await resp.text();
          showRefsBanner(panel, "error",
            "AI check failed",
            errText.includes("no indexed sources")
              ? "This project has no cited sources yet. Add references via 'Extract from paper' or 'Populate manually' first."
              : errText,
            banner,
          );
          return;
        }
        const data = await resp.json();
        const failures = data.errors || [];
        const failureSample = failures.slice(0, 3).map(e =>
          `<li><code>${escapeHtml(e.source_id)}</code> — ${escapeHtml(e.error)}</li>`
        ).join("");
        const moreFailures = failures.length > 3
          ? `<li class="muted small">…and ${failures.length - 3} more</li>` : "";

        if (data.attempted_count === 0) {
          // Empty pool → not a failure, just nothing to do.
          showRefsBanner(panel, "warn",
            "No references to enrich",
            "This project has no indexed sources. Use 'Extract from paper' or 'Populate manually' first, then try again.",
            banner,
          );
          return;
        }

        if (data.enriched_count === 0) {
          showRefsBanner(panel, "error",
            `Could not enrich any of ${data.attempted_count} reference${data.attempted_count === 1 ? "" : "s"}`,
            failures.length
              ? `First failures:`
              : "Claude returned an unexpected shape. Try again, or run on a single reference first.",
            banner,
            failures.length ? `<ul class="refs-error-list">${failureSample}${moreFailures}</ul>` : null,
          );
          return;
        }
        const partialBody = failures.length
          ? `${data.failed_count} reference${data.failed_count === 1 ? "" : "s"} failed — see details below.`
          : "Cards below now show AI summaries, key findings, and per-claim usage roles.";
        showRefsBanner(panel,
          failures.length ? "warn" : "ok",
          `Enriched ${data.enriched_count} of ${data.attempted_count} references`,
          partialBody,
          banner,
          failures.length ? `<ul class="refs-error-list">${failureSample}${moreFailures}</ul>` : null,
        );
        setTimeout(() => {
          // Re-render so the new fields appear (but keep banner visible).
          const main = document.getElementById("app");
          renderReferences(main);
        }, failures.length ? 4500 : 1200);
      } catch (err) {
        showRefsBanner(panel, "error",
          "Network error",
          err.message || "Could not reach the server.",
          banner,
        );
      }
    });
  }
}

function showRefsBanner(panel, kind, title, body, replace = null, extrasHtml = null) {
  if (replace && replace.parentElement) replace.remove();
  // Remove any other lingering banner so we don't stack them.
  panel.querySelectorAll(".refs-banner").forEach(b => b.remove());

  const el = document.createElement("div");
  el.className = `refs-banner refs-banner-${kind}`;
  el.innerHTML = `
    ${kind === "loading" ? `<span class="refs-banner-spinner"></span>` : ""}
    <div class="refs-banner-body">
      <strong>${escapeHtml(title)}</strong>
      <span class="muted small">${escapeHtml(body)}</span>
      ${extrasHtml || ""}
    </div>
    ${kind !== "loading" ? `<button class="btn-ghost sm" data-banner-dismiss>×</button>` : ""}`;
  // Insert right after the toolbar so it's always near the top.
  const toolbar = panel.querySelector(".refs-toolbar");
  if (toolbar) toolbar.insertAdjacentElement("afterend", el);
  else panel.prepend(el);

  const dismiss = el.querySelector("[data-banner-dismiss]");
  if (dismiss) {
    dismiss.addEventListener("click", () => el.remove());
    // Auto-dismiss success-only banners after 4s; warn/error stay
    // until the user dismisses them so error detail can be read.
    if (kind === "ok") setTimeout(() => el.remove(), 4000);
  }
  return el;
}

function openExtractPreviewModal(data, projSlug) {
  const modal = document.createElement("div");
  modal.className = "modal-backdrop";
  const rows = data.citations.map((c, i) => `
    <label class="extract-row">
      <input type="checkbox" data-idx="${i}" checked />
      <div>
        <div class="extract-title"><strong>${escapeHtml(c.title || "(untitled)")}</strong>${c.year ? ` (${c.year})` : ""}</div>
        <div class="muted small">${escapeHtml((c.authors || []).join(", ") || "no authors")}${c.container ? ` · ${escapeHtml(c.container)}` : ""}${c.doi ? ` · doi:${escapeHtml(c.doi)}` : ""}</div>
      </div>
    </label>`).join("");
  modal.innerHTML = `
    <div class="modal" style="max-width: 760px;">
      <div class="modal-head">
        <h2>Extracted ${data.extracted_count} reference${data.extracted_count === 1 ? "" : "s"} from ${escapeHtml(data.source_label)}</h2>
        <button class="btn-ghost" data-action="close">✕</button>
      </div>
      <div class="modal-body">
        <p class="muted small">Review the extracted citations and uncheck any that look wrong. The selected ones will be saved as Sources in this project.</p>
        <div class="extract-list">${rows}</div>
        <div class="modal-actions" style="margin-top: 16px;">
          <button class="btn" data-action="close">Cancel</button>
          <button class="btn primary" data-action="accept">Save selected</button>
        </div>
      </div>
    </div>`;
  document.body.appendChild(modal);

  const close = () => modal.remove();
  modal.querySelectorAll('[data-action="close"]').forEach(b =>
    b.addEventListener("click", close));
  modal.addEventListener("click", e => {
    if (e.target === modal) close();
  });

  modal.querySelector('[data-action="accept"]').addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    btn.textContent = "Saving…";
    const selected = [];
    modal.querySelectorAll('input[type="checkbox"]:checked').forEach(cb => {
      const idx = Number(cb.dataset.idx);
      selected.push(data.citations[idx]);
    });
    if (!selected.length) {
      alert("Nothing selected.");
      btn.disabled = false;
      btn.textContent = "Save selected";
      return;
    }
    try {
      const resp = await fetch(
        `/api/projects/${projSlug}/references/extract/accept`,
        {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({citations: selected}),
        }
      );
      if (!resp.ok) {
        alert("Save failed: " + await resp.text());
        btn.disabled = false;
        btn.textContent = "Save selected";
        return;
      }
      const result = await resp.json();
      close();
      // Re-render the References tab so new entries appear.
      const main = document.getElementById("app");
      renderReferences(main);
      alert(`Saved ${result.added.length} reference${result.added.length === 1 ? "" : "s"} to this project.`);
    } catch (err) {
      alert("Network error: " + err.message);
      btn.disabled = false;
      btn.textContent = "Save selected";
    }
  });
}

function openManualReferenceModal(projSlug) {
  const modal = document.createElement("div");
  modal.className = "modal-backdrop";
  modal.innerHTML = `
    <div class="modal" style="max-width: 620px;">
      <div class="modal-head">
        <h2>Add a reference</h2>
        <button class="btn-ghost" data-action="close">✕</button>
      </div>
      <form class="modal-body" data-bind="form">
        <label class="field">
          <span class="field-label">Authors <span class="muted small">(comma-separated)</span></span>
          <input type="text" name="authors" placeholder="Shai Danziger, Jonathan Levav, Liora Avnaim-Pesso" required />
        </label>
        <div class="form-grid">
          <label class="field">
            <span class="field-label">Year</span>
            <input type="number" name="year" min="1500" max="2100" placeholder="2011" />
          </label>
          <label class="field">
            <span class="field-label">Type</span>
            <select name="type">
              <option value="primary_paper">primary_paper</option>
              <option value="note">note</option>
              <option value="dataset">dataset</option>
              <option value="web_page">web_page</option>
              <option value="prior_writing">prior_writing</option>
            </select>
          </label>
        </div>
        <label class="field">
          <span class="field-label">Title</span>
          <input type="text" name="title" required placeholder="Extraneous factors in judicial decisions" />
        </label>
        <label class="field">
          <span class="field-label">Container <span class="muted small">(journal / book / website)</span></span>
          <input type="text" name="container" placeholder="Proceedings of the National Academy of Sciences" />
        </label>
        <div class="form-grid">
          <label class="field">
            <span class="field-label">Volume</span>
            <input type="text" name="volume" placeholder="108" />
          </label>
          <label class="field">
            <span class="field-label">Issue</span>
            <input type="text" name="issue" placeholder="17" />
          </label>
          <label class="field">
            <span class="field-label">Pages</span>
            <input type="text" name="pages" placeholder="6889-6892" />
          </label>
        </div>
        <label class="field">
          <span class="field-label">DOI <span class="muted small">(no URL prefix)</span></span>
          <input type="text" name="doi" placeholder="10.1073/pnas.1018033108" />
        </label>
        <label class="field">
          <span class="field-label">URL <span class="muted small">(if no DOI)</span></span>
          <input type="text" name="url" placeholder="https://example.org/..." />
        </label>
        <div class="modal-actions">
          <button type="button" class="btn" data-action="close">Cancel</button>
          <button type="submit" class="btn primary">Save reference</button>
        </div>
      </form>
    </div>`;
  document.body.appendChild(modal);

  const close = () => modal.remove();
  modal.querySelectorAll('[data-action="close"]').forEach(b =>
    b.addEventListener("click", close));
  modal.addEventListener("click", e => {
    if (e.target === modal) close();
  });

  const form = modal.querySelector('[data-bind="form"]');
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    const fd = new FormData(form);
    const authors = (fd.get("authors") || "").toString().split(",")
      .map(a => a.trim()).filter(Boolean);
    const body = {
      authors,
      year: fd.get("year") || null,
      type: fd.get("type") || "primary_paper",
      title: fd.get("title") || "",
      container: (fd.get("container") || "").toString().trim() || null,
      volume: (fd.get("volume") || "").toString().trim() || null,
      issue: (fd.get("issue") || "").toString().trim() || null,
      pages: (fd.get("pages") || "").toString().trim() || null,
      doi: (fd.get("doi") || "").toString().trim() || null,
      url: (fd.get("url") || "").toString().trim() || null,
    };
    const submitBtn = form.querySelector('button[type="submit"]');
    submitBtn.disabled = true;
    submitBtn.textContent = "Saving…";
    try {
      const resp = await fetch(`/api/projects/${projSlug}/references/manual`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        alert("Save failed: " + await resp.text());
        submitBtn.disabled = false;
        submitBtn.textContent = "Save reference";
        return;
      }
      close();
      const main = document.getElementById("app");
      renderReferences(main);
    } catch (err) {
      alert("Network error: " + err.message);
      submitBtn.disabled = false;
      submitBtn.textContent = "Save reference";
    }
  });
}

function renderReferenceCard(r, style) {
  const used = r.used_in_paper || [];
  const c = r.citation || {};
  const meta = r.metadata || {};
  const fmt = r.formatted || {};
  const ai = r.ai;

  // ── Card title: human-readable, e.g. "Danziger et al. (2011)" ──
  let shortTitle = "";
  if (c.authors && c.authors.length) {
    const last = (a) => {
      const t = (a || "").trim();
      return t.includes(",") ? t.split(",", 1)[0].trim() : (t.split(" ").slice(-1)[0] || t);
    };
    const surnames = c.authors.map(last);
    if (surnames.length === 1) shortTitle = surnames[0];
    else if (surnames.length === 2) shortTitle = `${surnames[0]} & ${surnames[1]}`;
    else shortTitle = `${surnames[0]} et al.`;
    if (c.year) shortTitle += ` (${c.year})`;
  } else {
    shortTitle = c.title ? truncate(c.title, 50) : r.source_id;
  }

  // ── Status pills (compact, one row, only the meaningful ones) ──
  const statusPills = [];
  if (used.length) {
    statusPills.push(
      `<span class="pill citation-count">${used.length} citation${used.length === 1 ? "" : "s"}</span>`
    );
  } else {
    statusPills.push(`<span class="pill muted">uncited</span>`);
  }
  if (meta.peer_reviewed) statusPills.push(`<span class="pill ok">peer-reviewed</span>`);
  if (ai) {
    const confLabel = ai.confidence ? ` (${ai.confidence})` : "";
    statusPills.push(`<span class="pill ai-pill" title="AI-enriched${confLabel}">✨</span>`);
  }

  // ── Inline AI summary band (subtle, integrated into the card) ──
  let aiSummaryHtml = "";
  if (ai && ai.summary) {
    const aiMeta = [];
    if (ai.citation_count_estimate !== null && ai.citation_count_estimate !== undefined) {
      aiMeta.push(`<span>~${ai.citation_count_estimate.toLocaleString()} citations</span>`);
    }
    if (ai.confidence && ai.confidence !== "unknown") {
      aiMeta.push(`<span class="ai-conf-${escapeAttr(ai.confidence)}">${escapeHtml(ai.confidence)} confidence</span>`);
    }
    aiSummaryHtml = `
      <div class="ref-ai-band">
        <p class="ref-ai-summary">${escapeHtml(ai.summary)}</p>
        ${aiMeta.length ? `<div class="ref-ai-meta">${aiMeta.join(" · ")}</div>` : ""}
      </div>`;
  }

  // ── Collapsible: AI key findings + field position ──
  let aiDetailsHtml = "";
  if (ai && ((ai.key_findings || []).length || ai.field_position)) {
    aiDetailsHtml = `
      <details class="ref-collapsible">
        <summary>Key findings & field position</summary>
        <div class="ref-collapsible-body">
          ${ai.field_position
            ? `<p class="ref-field-pos"><strong>Field position:</strong> ${escapeHtml(ai.field_position)}</p>`
            : ""}
          ${(ai.key_findings || []).length
            ? `<ul class="ref-findings">
                 ${ai.key_findings.map(f => `<li>${escapeHtml(f)}</li>`).join("")}
               </ul>`
            : ""}
        </div>
      </details>`;
  }

  // ── Collapsible: per-claim usage detail ──
  const usageBody = used.length
    ? used.map(u => `
        <li class="ref-usage">
          <div class="ref-usage-row">
            <code class="mono">${escapeHtml(u.cluster_ids[0] || u.section_id || "—")}</code>
            ${u.ai_role ? `<span class="pill ai-role">${escapeHtml(u.ai_role.replace(/_/g, " "))}</span>` : ""}
            ${u.binding_strength
              ? `<span class="pill binding-${escapeAttr(u.binding_strength)}">${escapeHtml(u.binding_strength)}</span>`
              : ""}
            ${u.page ? `<span class="muted small">p. ${u.page}</span>` : ""}
          </div>
          <div class="ref-usage-claim">${escapeHtml(truncate(u.claim_statement, 200))}</div>
          ${u.ai_explanation ? `<div class="ref-usage-explanation">${escapeHtml(u.ai_explanation)}</div>` : ""}
          ${u.quote_text ? `<blockquote class="ref-quote">${escapeHtml(truncate(u.quote_text, 200))}</blockquote>` : ""}
        </li>`).join("")
    : "";

  const usageHtml = used.length ? `
    <details class="ref-collapsible" ${used.length <= 3 ? "open" : ""}>
      <summary>Used here in ${used.length} place${used.length === 1 ? "" : "s"}</summary>
      <ul class="ref-usage-list">${usageBody}</ul>
    </details>` : "";

  // ── Collapsible: full citation metadata ──
  const metaHtml = `
    <details class="ref-collapsible">
      <summary>Citation metadata · all 6 styles · notes</summary>
      <div class="ref-collapsible-body">
        <div class="ref-styles-grid">
          <div class="kv"><span class="k">In-text</span><span class="v"><code>${escapeHtml(fmt.in_text || "")}</code></span></div>
          <div class="kv"><span class="k">Narrative</span><span class="v"><code>${escapeHtml(fmt.in_text_narrative || "")}</code></span></div>
          ${c.authors && c.authors.length ? `<div class="kv"><span class="k">Authors</span><span class="v">${escapeHtml(c.authors.join(", "))}</span></div>` : ""}
          ${c.container ? `<div class="kv"><span class="k">Container</span><span class="v">${escapeHtml(c.container)}</span></div>` : ""}
          ${c.volume ? `<div class="kv"><span class="k">Volume</span><span class="v">${escapeHtml(c.volume)}${c.issue ? `(${escapeHtml(c.issue)})` : ""}</span></div>` : ""}
          ${c.pages ? `<div class="kv"><span class="k">Pages</span><span class="v">${escapeHtml(c.pages)}</span></div>` : ""}
          ${c.doi ? `<div class="kv"><span class="k">DOI</span><span class="v"><a href="https://doi.org/${escapeAttr(c.doi)}" target="_blank" rel="noopener">${escapeHtml(c.doi)}</a></span></div>` : ""}
          ${c.url ? `<div class="kv"><span class="k">URL</span><span class="v"><a href="${escapeAttr(c.url)}" target="_blank" rel="noopener">${escapeHtml(c.url)}</a></span></div>` : ""}
          ${meta.file_path ? `<div class="kv"><span class="k">File</span><span class="v mono">${escapeHtml(meta.file_path)}</span></div>` : ""}
          <div class="kv"><span class="k">source_id</span><span class="v mono">${escapeHtml(r.source_id)}</span></div>
          <div class="kv"><span class="k">type</span><span class="v">${escapeHtml(r.type)}</span></div>
        </div>
        <div class="ref-notes-block">
          <label class="muted small">Your notes (override AI summary)</label>
          <textarea data-bind="about-input" data-source-id="${escapeAttr(r.source_id)}" rows="3" placeholder="Optional — anything you want to remember about how this source fits your argument.">${escapeHtml(r.about || "")}</textarea>
          <button class="btn sm" data-action="save-about" data-source-id="${escapeAttr(r.source_id)}">Save notes</button>
        </div>
      </div>
    </details>`;

  // Quick-access links row: DOI, URL, local file. Always shown when
  // any are available so the user can open the source paper in one
  // click, without expanding the metadata section.
  const linkButtons = [];
  if (c.doi) {
    linkButtons.push(
      `<a class="ref-link-btn" href="https://doi.org/${escapeAttr(c.doi)}" target="_blank" rel="noopener" title="Open via DOI"><span class="ref-link-icon">🔗</span> DOI</a>`
    );
  }
  if (c.url) {
    linkButtons.push(
      `<a class="ref-link-btn" href="${escapeAttr(c.url)}" target="_blank" rel="noopener" title="Open URL"><span class="ref-link-icon">↗</span> URL</a>`
    );
  }
  // Local file fallback: open the source PDF/sidecar if it lives
  // under refs/ (i.e. an indexed source file).
  if (meta.file_path && meta.file_path.startsWith("refs/")) {
    const rel = meta.file_path.replace(/^refs\//, "");
    const slashIdx = rel.indexOf("/");
    if (slashIdx > 0) {
      const bucket = rel.slice(0, slashIdx);
      const fname = rel.slice(slashIdx + 1);
      const projSlug = encodeURIComponent(state.current);
      linkButtons.push(
        `<a class="ref-link-btn" href="/api/projects/${projSlug}/sources/${encodeURIComponent(bucket)}/${encodeURIComponent(fname)}" target="_blank" rel="noopener" title="Open the indexed file"><span class="ref-link-icon">📄</span> File</a>`
      );
    }
  }
  // Search fallback: if there's no DOI / URL / file but there's a
  // title, offer a Google Scholar search so the user can still find
  // the paper online.
  if (!linkButtons.length && c.title) {
    const q = encodeURIComponent(
      [...(c.authors || []).slice(0, 1), c.title, c.year || ""].filter(Boolean).join(" ")
    );
    linkButtons.push(
      `<a class="ref-link-btn ref-link-search" href="https://scholar.google.com/scholar?q=${q}" target="_blank" rel="noopener" title="Find via Google Scholar (no DOI on file)"><span class="ref-link-icon">🔍</span> Search</a>`
    );
  }
  const linkRowHtml = linkButtons.length
    ? `<div class="ref-link-row">${linkButtons.join("")}</div>`
    : "";

  return `
    <div class="reference-card" data-source-id="${escapeAttr(r.source_id)}">
      <div class="reference-card-head">
        <h3 class="ref-short-title">${escapeHtml(shortTitle)}</h3>
        <div class="ref-pills">${statusPills.join("")}</div>
      </div>
      ${c.title ? `<p class="ref-paper-title">${escapeHtml(c.title)}</p>` : ""}
      <div class="reference-citation">${renderInlineMarkdown(fmt.bibliography || "")}</div>
      ${linkRowHtml}
      ${aiSummaryHtml}
      ${aiDetailsHtml}
      ${usageHtml}
      ${metaHtml}
    </div>`;
}

function renderInlineMarkdown(text) {
  // Lightweight: italics, then auto-link any http(s):// URL or bare
  // doi.org URL in the formatted bibliography. Escape FIRST so URLs
  // can't smuggle HTML.
  let s = escapeHtml(text);
  s = s.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  // Match http(s):// URLs ending at whitespace or '<' (so we don't
  // greedily eat trailing punctuation that's part of the sentence).
  s = s.replace(
    /(https?:\/\/[^\s<]+?)([.,;)]?(?:\s|$|<))/g,
    '<a href="$1" target="_blank" rel="noopener">$1</a>$2'
  );
  return s;
}

// ─── drafts ──────────────────────────────────────────

async function renderDrafts(main) {
  const panel = main.querySelector('[data-bind="drafts"]');
  panel.innerHTML = `<div class="muted small">Loading…</div>`;

  const projSlug = encodeURIComponent(state.current);
  let originalsData = {originals: []};
  try {
    originalsData = await fetchJSON(`/api/projects/${projSlug}/originals`);
  } catch (err) {
    // Don't block draft rendering on the originals fetch — just show
    // an empty originals section.
    originalsData = {originals: []};
  }

  const originals = originalsData.originals || [];
  const drafts = state.drafts || [];
  const hasOriginalPdf = originals.some(o => o.role === "original_pdf");
  const hasOriginalDocx = originals.some(o => o.role === "original_docx");

  // ── Original paper section ──
  const originalsHtml = originals.length
    ? originals.map(o => renderOriginalRow(o, projSlug)).join("")
    : `<div class="muted small" style="padding: 8px 0;">No original artefacts preserved for this project.</div>`;

  // Upload-original CTA (only show if no PDF/DOCX original is saved).
  const uploadCta = (!hasOriginalPdf && !hasOriginalDocx)
    ? `
      <div class="originals-upload">
        <span class="muted small">No original PDF / DOCX attached. Upload it so it appears here:</span>
        <label class="btn sm">
          Upload original
          <input type="file" accept=".pdf,.docx" hidden data-bind="upload-original" />
        </label>
      </div>`
    : "";

  // ── Rendered drafts section ──
  const draftsHtml = drafts.length
    ? drafts.map(d => `
        <div class="draft-row draft-row--with-action" data-filename="${escapeAttr(d.filename)}" data-kind="rendered">
          <div class="draft-name">
            ${escapeHtml(d.filename)}
            ${d.is_current ? `<span class="pill ok">current</span>` : ""}
          </div>
          <div class="draft-meta">${d.word_count.toLocaleString()} words</div>
          <div class="draft-meta">${formatBytes(d.size_bytes)}</div>
          <div class="draft-meta">${formatTimestamp(d.mtime)}</div>
          <a class="bucket-action" data-stop-row-click
             href="/api/projects/${projSlug}/drafts/${encodeURIComponent(d.filename)}"
             download="${escapeAttr(d.filename)}">Download</a>
        </div>`).join("")
    : `<div class="muted small" style="padding: 8px 0;">No drafts yet. Run a review to generate one.</div>`;

  panel.innerHTML = `
    <div class="card">
      <h3 class="subhead">Original paper</h3>
      <p class="muted small" style="margin: 0 0 10px;">The user-uploaded source material this project was built from. Compare against the rendered drafts below.</p>
      <div class="draft-list">${originalsHtml}</div>
      ${uploadCta}
    </div>
    <div class="card">
      <h3 class="subhead">Rendered drafts</h3>
      <p class="muted small" style="margin: 0 0 10px;">Outputs of the review pipeline, newest first.</p>
      <div class="draft-list">${draftsHtml}</div>
    </div>
    <div class="draft-viewer hidden" data-bind="viewer"><div class="muted small">Pick a file above to preview.</div></div>`;

  // Wire row clicks (originals + drafts share the .draft-row class).
  panel.querySelectorAll(".draft-row").forEach(row => {
    row.addEventListener("click", ev => {
      // Per-row action links opt out via [data-stop-row-click] so the
      // download anchor doesn't double-trigger a preview load.
      if (ev.target.closest("[data-stop-row-click]")) return;
      const kind = row.dataset.kind;
      const filename = row.dataset.filename;
      const fileKind = row.dataset.fileKind;
      if (kind === "rendered") {
        loadDraft(filename, panel);
      } else if (kind === "original") {
        loadOriginal(filename, fileKind, panel);
      }
    });
  });

  // Wire upload-original input.
  const uploadInput = panel.querySelector('[data-bind="upload-original"]');
  if (uploadInput) {
    uploadInput.addEventListener("change", async ev => {
      const file = ev.target.files?.[0];
      if (!file) return;
      const fd = new FormData();
      fd.append("file", file);
      try {
        const resp = await fetch(
          `/api/projects/${projSlug}/structure/original`,
          {method: "POST", body: fd}
        );
        if (!resp.ok) {
          alert("Upload failed: " + await resp.text());
          return;
        }
        renderDrafts(main);
      } catch (err) {
        alert("Network error: " + err.message);
      }
    });
  }

  // Auto-load: prefer the current rendered draft, else the raw text.
  const current = drafts.find(d => d.is_current);
  if (current) {
    const row = panel.querySelector(`.draft-row[data-filename="${escapeAttr(current.filename)}"][data-kind="rendered"]`);
    if (row) row.click();
  } else {
    const rawRow = panel.querySelector('.draft-row[data-filename="outline.raw.md"]');
    if (rawRow) rawRow.click();
  }
}

function renderOriginalRow(o, projSlug) {
  const url = `/api/projects/${projSlug}/originals/${encodeURIComponent(o.filename)}`;
  const isBinary = o.kind === "pdf" || o.kind === "docx";
  const meta = isBinary
    ? `${formatBytes(o.size_bytes)} · ${formatTimestamp(o.mtime)}`
    : `${(o.word_count || 0).toLocaleString()} words · ${formatBytes(o.size_bytes)} · ${formatTimestamp(o.mtime)}`;
  const action = isBinary
    ? `<a class="bucket-action" href="${url}" target="_blank" rel="noopener">Open ${o.kind.toUpperCase()}</a>`
    : `<span class="muted small">click to preview</span>`;
  return `
    <div class="draft-row" data-filename="${escapeAttr(o.filename)}" data-kind="original" data-file-kind="${escapeAttr(o.kind)}">
      <div class="draft-name">
        ${escapeHtml(o.label)}
        <span class="pill">${escapeHtml(o.kind)}</span>
      </div>
      <div class="draft-meta">${o.filename}</div>
      <div class="draft-meta">${meta}</div>
      <div class="draft-meta">${action}</div>
    </div>`;
}

async function loadDraft(filename, panel) {
  const viewer = panel.querySelector('[data-bind="viewer"]');
  panel.querySelectorAll(".draft-row").forEach(r =>
    r.classList.toggle("selected",
      r.dataset.filename === filename && r.dataset.kind === "rendered"));
  viewer.classList.remove("hidden");
  viewer.innerHTML = `<div class="muted small">Loading…</div>`;
  try {
    const text = await fetchText(
      `/api/projects/${encodeURIComponent(state.current)}/drafts/${encodeURIComponent(filename)}`);
    viewer.innerHTML = `<article class="prose">${renderMarkdown(text)}</article>`;
  } catch (err) {
    viewer.innerHTML = `<div class="muted small">Failed to load: ${escapeHtml(err.message)}</div>`;
  }
}

async function loadOriginal(filename, kind, panel) {
  const viewer = panel.querySelector('[data-bind="viewer"]');
  panel.querySelectorAll(".draft-row").forEach(r =>
    r.classList.toggle("selected",
      r.dataset.filename === filename && r.dataset.kind === "original"));
  viewer.classList.remove("hidden");
  const projSlug = encodeURIComponent(state.current);
  const url = `/api/projects/${projSlug}/originals/${encodeURIComponent(filename)}`;

  if (kind === "pdf") {
    // Embed the PDF via the browser's native viewer.
    viewer.innerHTML = `
      <div class="prose">
        <p class="muted small">PDF preview · <a href="${url}" target="_blank" rel="noopener">open in new tab</a></p>
        <iframe src="${url}" class="pdf-frame" title="${escapeHtml(filename)}"></iframe>
      </div>`;
    return;
  }
  if (kind === "docx") {
    // No native browser DOCX viewer — offer download.
    viewer.innerHTML = `
      <div class="prose">
        <p>This is a Word document. <a href="${url}" target="_blank" rel="noopener">Click here to download <code>${escapeHtml(filename)}</code></a> and open it locally.</p>
      </div>`;
    return;
  }

  // Markdown / text → render inline.
  viewer.innerHTML = `<div class="muted small">Loading…</div>`;
  try {
    const text = await fetchText(url);
    viewer.innerHTML = `<article class="prose">${renderMarkdown(text)}</article>`;
  } catch (err) {
    viewer.innerHTML = `<div class="muted small">Failed to load: ${escapeHtml(err.message)}</div>`;
  }
}

// ─── quality (audit + reviews combined) ──────────────

async function renderQualityPanel(main) {
  const panel = main.querySelector('[data-bind="quality"]');
  panel.innerHTML = `<div class="muted small">Loading…</div>`;

  const proj = encodeURIComponent(state.current);
  const [auditRes, voiceRes, gapRes, changelogsRes] = await Promise.allSettled([
    fetchJSON(`/api/projects/${proj}/audit`),
    fetchText(`/api/projects/${proj}/voice-review`),
    fetchJSON(`/api/projects/${proj}/source-gap`),
    fetchJSON(`/api/projects/${proj}/changelogs`),
  ]);

  const changelogCount = changelogsRes.status === "fulfilled"
    ? (changelogsRes.value.changelogs || []).length
    : 0;

  panel.innerHTML = `
    <nav class="quality-tabs">
      <button class="quality-tab active" data-q-tab="changelog">Change log${changelogCount ? ` (${changelogCount})` : ""}</button>
      <button class="quality-tab" data-q-tab="audit">Audit flags</button>
      <button class="quality-tab" data-q-tab="voice">Voice review</button>
      <button class="quality-tab" data-q-tab="gap">Source-gap review</button>
    </nav>
    <div class="quality-body">
      <section class="quality-section visible" data-q-panel="changelog">${renderChangelogIndexHtml(changelogsRes)}</section>
      <section class="quality-section" data-q-panel="audit">${renderAuditHtml(auditRes)}</section>
      <section class="quality-section" data-q-panel="voice">${renderVoiceHtml(voiceRes)}</section>
      <section class="quality-section" data-q-panel="gap">${renderGapHtml(gapRes)}</section>
    </div>`;

  panel.querySelectorAll(".quality-tab").forEach(t => {
    t.addEventListener("click", () => {
      panel.querySelectorAll(".quality-tab").forEach(x => x.classList.toggle("active", x === t));
      panel.querySelectorAll(".quality-section").forEach(s =>
        s.classList.toggle("visible", s.dataset.qPanel === t.dataset.qTab));
    });
  });

  // Wire changelog row clicks → load + show body in-place.
  panel.querySelectorAll("[data-changelog]").forEach(row => {
    row.addEventListener("click", async () => {
      const filename = row.dataset.changelog;
      const viewer = panel.querySelector('[data-bind="changelog-viewer"]');
      panel.querySelectorAll("[data-changelog]").forEach(r =>
        r.classList.toggle("selected", r === row));
      viewer.classList.remove("hidden");
      viewer.innerHTML = `<div class="muted small">Loading…</div>`;
      try {
        const text = await fetchText(
          `/api/projects/${proj}/changelogs/${encodeURIComponent(filename)}`);
        viewer.innerHTML = `<article class="prose">${renderMarkdown(text)}</article>`;
      } catch (err) {
        viewer.innerHTML = `<div class="muted small">Failed to load: ${escapeHtml(err.message)}</div>`;
      }
    });
  });
}

function renderChangelogIndexHtml(res) {
  if (res.status !== "fulfilled") {
    return `<div class="card"><p class="muted small">Could not list changelogs.</p></div>`;
  }
  const logs = res.value.changelogs || [];
  if (!logs.length) {
    return `<div class="empty-state"><h3>No reviews yet</h3><p>Each review writes a markdown changelog here so you can see exactly what it modified — clusters re-rendered, audit-flag deltas, outline mutations, paper word-count changes. Run a review to populate this.</p></div>`;
  }
  const rows = logs.map(l => {
    // Filename pattern: YYYYMMDD_HHMMSS_<level>.md
    const m = l.filename.match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})_(\w+)\.md$/);
    let humanWhen = formatTimestamp(l.mtime);
    let level = "—";
    if (m) {
      level = m[7];
    }
    return `
      <div class="draft-row" data-changelog="${escapeAttr(l.filename)}">
        <div>
          <div class="draft-name"><code>${escapeHtml(level)}</code> · ${escapeHtml(l.filename)}</div>
          <div class="muted small">${escapeHtml(humanWhen)} · ${formatBytes(l.size_bytes)}</div>
        </div>
        <span class="muted small">view →</span>
      </div>`;
  }).join("");
  return `
    <div class="card">
      <h3 class="subhead">Change logs (newest first)</h3>
      <p class="muted small">Click a row to inspect what that review changed.</p>
      <div class="draft-list">${rows}</div>
    </div>
    <div class="card hidden" data-bind="changelog-viewer"></div>`;
}

function renderAuditHtml(res) {
  if (res.status !== "fulfilled") {
    return `<div class="empty-state"><h3>No audit data</h3><p>Run a Standard or Deep review to populate the audit.</p></div>`;
  }
  const raw = res.value && res.value.flags;
  let flags = [];
  if (Array.isArray(raw)) flags = raw;
  else if (raw && typeof raw === "object") flags = Object.values(raw).flat();
  if (!flags.length) {
    return `<div class="empty-state"><h3>No audit flags</h3><p>The last Standard or Deep review found nothing to flag — or hasn't been run yet.</p></div>`;
  }
  const byCat = {};
  const bySev = {critical: 0, standard: 0, minor: 0};
  flags.forEach(f => {
    byCat[f.category] = (byCat[f.category] || 0) + 1;
    bySev[f.severity] = (bySev[f.severity] || 0) + 1;
  });
  const sumCards = Object.entries(byCat).sort((a, b) => b[1] - a[1]).map(([cat, n]) => `
    <div class="flag-cat">
      <div class="flag-cat-num">${n}</div>
      <span class="flag-cat-label">${escapeHtml(cat)}</span>
    </div>`).join("");
  const flagRows = flags.slice(0, 200).map(f => `
    <div class="flag-row">
      <span class="flag-severity ${f.severity}">${escapeHtml(f.severity)}</span>
      <div>
        <div class="flag-rule">${escapeHtml(f.rule_id)}</div>
        <div class="flag-text">${escapeHtml(truncate(f.offending_text || "", 160))}</div>
        ${f.suggestion ? `<div class="muted small" style="margin-top: 4px;">${escapeHtml(truncate(f.suggestion, 200))}</div>` : ""}
      </div>
      <code class="path-pill">${escapeHtml(f.cluster_id || "—")}</code>
    </div>`).join("");
  return `
    <div class="card">
      <h3 class="subhead">Severity summary</h3>
      <div style="display: flex; gap: 18px; margin-bottom: 12px;">
        <span class="pill bad">${bySev.critical} critical</span>
        <span class="pill warn">${bySev.standard} standard</span>
        <span class="pill">${bySev.minor} minor</span>
      </div>
      <h3 class="subhead">By category</h3>
      <div class="flag-summary">${sumCards}</div>
    </div>
    <div class="card">
      <h3 class="subhead">Flags (showing first 200 of ${flags.length})</h3>
      <div class="flag-list">${flagRows}</div>
    </div>`;
}

function renderVoiceHtml(res) {
  if (res.status !== "fulfilled") {
    return `<div class="card"><p class="muted small">Voice review not generated yet. Run a Standard or Deep review to produce one.</p></div>`;
  }
  return `
    <div class="card">
      <article class="prose" style="font-size: 14px; line-height: 1.6;">${renderMarkdown(res.value)}</article>
    </div>`;
}

function renderGapHtml(res) {
  if (res.status !== "fulfilled") {
    return `<div class="card"><p class="muted small">Source-gap review not generated yet. Run a Deep review with a reference document path in Advanced options.</p></div>`;
  }
  const cats = {};
  res.value.gaps.forEach(g => (cats[g.category] = cats[g.category] || []).push(g));
  const order = ["analytical_move", "mechanism", "quantitative", "arithmetic", "named_scholar", "named_example", "structural"];
  const titles = {
    analytical_move: "Analytical moves the render flattens",
    mechanism: "Mechanisms / named theories the render lacks",
    quantitative: "Specific numbers the render omits",
    arithmetic: "Step-by-step working the render abstracts",
    named_scholar: "Scholars the render does not engage by name",
    named_example: "Concrete examples the render omits",
    structural: "Structure / scaffolding the render lacks",
  };
  const categoriesHtml = order.filter(o => cats[o] && cats[o].length).map(o => `
    <div class="gap-category">
      <h3>${escapeHtml(titles[o])} <span class="pill">${cats[o].length}</span></h3>
      ${cats[o].slice(0, 30).map(g => `
        <div class="gap-card">
          <div class="gap-summary">${escapeHtml(g.summary)}</div>
          <blockquote class="gap-snippet">${escapeHtml(g.reference_snippet)}</blockquote>
          ${g.suggested_action ? `<div class="gap-action">${escapeHtml(g.suggested_action)}</div>` : ""}
          ${g.target_claim_id ? `<div class="muted small" style="margin-top: 4px;">target claim: <code class="path-pill">${escapeHtml(g.target_claim_id)}</code></div>` : ""}
        </div>`).join("")}
    </div>`).join("");
  return `
    <div class="card">
      <h3 class="subhead">Source-gap review · ${res.value.gaps.length} gaps</h3>
      ${categoriesHtml}
    </div>`;
}

// ─── review panel ────────────────────────────────────

function renderRunPanel(main, initialSubTab) {
  const panel = main.querySelector('[data-bind="review"]');
  panel.innerHTML = "";
  const validSubTabs = new Set(["run", "audit", "voice", "gap", "changelog"]);
  const startTab = validSubTabs.has(initialSubTab) ? initialSubTab : "run";

  // Sub-nav: Run controls vs the artefacts produced by previous runs
  // (audit / voice review / source-gap / change log). Sticks Quality +
  // change log content under Review since they're a natural pair —
  // run produces results, look at results in same place.
  const subnav = document.createElement("nav");
  subnav.className = "review-subnav";
  subnav.innerHTML = `
    <button class="review-tab active" data-r-tab="run">Run a review</button>
    <button class="review-tab" data-r-tab="audit">Audit flags</button>
    <button class="review-tab" data-r-tab="voice">Voice review</button>
    <button class="review-tab" data-r-tab="gap">Source-gap</button>
    <button class="review-tab" data-r-tab="changelog">Change log</button>
  `;
  panel.appendChild(subnav);

  const body = document.createElement("div");
  body.className = "review-body";
  panel.appendChild(body);

  function showSubview(name) {
    panel.querySelectorAll(".review-tab").forEach(t =>
      t.classList.toggle("active", t.dataset.rTab === name));
    body.innerHTML = `<div class="muted small" style="padding: 12px;">Loading…</div>`;
    if (name === "run") renderReviewRunSubview(body);
    else if (name === "audit") renderReviewAuditSubview(body);
    else if (name === "voice") renderReviewVoiceSubview(body);
    else if (name === "gap") renderReviewGapSubview(body);
    else if (name === "changelog") renderReviewChangelogSubview(body);
  }
  panel.querySelectorAll(".review-tab").forEach(t => {
    t.addEventListener("click", () => showSubview(t.dataset.rTab));
  });
  // Default to the Run sub-view, or honour the deep-link sub-tab.
  showSubview(startTab);
}

function renderReviewRunSubview(body) {
  body.innerHTML = "";

  // Show level-progression banner so the user can see what's been
  // done and what each level adds. Builds on top of run-history so
  // upgrading from Quick → Standard is informed by what's cached.
  const progressionBanner = renderLevelProgressionBanner();
  if (progressionBanner) {
    const bannerEl = document.createElement("div");
    bannerEl.innerHTML = progressionBanner;
    body.appendChild(bannerEl.firstElementChild);
  }

  body.appendChild(cloneTemplate("tpl-run-form"));
  const panel = body;

  const voiceSelect = panel.querySelector('[data-bind="voice-select"]');
  voiceSelect.innerHTML = (state.detail.voices || ["academic"])
    .map(v => `<option value="${escapeAttr(v)}">${escapeHtml(v)}</option>`)
    .join("");

  // Default to the first level the user hasn't successfully completed.
  const completed = (state.runHistory?.summary?.levels_completed_successfully) || [];
  const order = ["quick", "standard", "deep"];
  const nextLevel = order.find(l => !completed.includes(l)) || "standard";
  const radio = panel.querySelector(`input[name="level"][value="${nextLevel}"]`);
  if (radio) radio.checked = true;

  // Annotate level cards with a check + "completed at HH:MM" badge so
  // the user knows the work isn't being thrown away.
  const latestByLevel = state.runHistory?.latest_by_level || {};
  panel.querySelectorAll(".level-option").forEach(opt => {
    const input = opt.querySelector('input[name="level"]');
    const lvl = input ? input.value : null;
    if (lvl && latestByLevel[lvl]) {
      const last = latestByLevel[lvl];
      const card = opt.querySelector(".level-card");
      const badge = document.createElement("div");
      badge.className = "level-completed-badge";
      const ok = last.finalise_succeeded;
      badge.innerHTML = `
        <span class="dot ${ok ? "ok" : "bad"}"></span>
        ${ok ? "Last run delivered" : "Last run blocked"} ·
        ${formatTimestamp(new Date(last.finished_at).getTime() / 1000)}`;
      card.appendChild(badge);
    }
  });

  const form = panel.querySelector(".run-form");
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    const fd = new FormData(form);
    const body = {
      voice: fd.get("voice") || "academic",
      level: fd.get("level") || "standard",
      reference_path: (fd.get("reference_path") || "").trim() || null,
      max_passes: Number(fd.get("max_passes") || 3),
      chunk_min: Number(fd.get("chunk_min") || 3),
      chunk_max: Number(fd.get("chunk_max") || 4),
      force: fd.get("force") === "on",
    };
    try {
      const resp = await fetch(`/api/projects/${encodeURIComponent(state.current)}/runs`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const txt = await resp.text();
        alert(`Failed to start run: ${txt}`);
        return;
      }
      const data = await resp.json();
      startTimeline(data, panel);
      openWebSocket(state.current, data.run_id, panel);
    } catch (err) {
      alert(`Network error: ${err.message}`);
    }
  });
}

// ─── review sub-views: audit / voice / gap / changelog ──────

async function renderReviewAuditSubview(body) {
  const proj = encodeURIComponent(state.current);
  let res = await Promise.allSettled([
    fetchJSON(`/api/projects/${proj}/audit`),
  ]);
  body.innerHTML = renderAuditHtml(res[0]);
}

async function renderReviewVoiceSubview(body) {
  const proj = encodeURIComponent(state.current);
  let res = await Promise.allSettled([
    fetchText(`/api/projects/${proj}/voice-review`),
  ]);
  body.innerHTML = renderVoiceHtml(res[0]);
}

async function renderReviewGapSubview(body) {
  const proj = encodeURIComponent(state.current);
  let res = await Promise.allSettled([
    fetchJSON(`/api/projects/${proj}/source-gap`),
  ]);
  body.innerHTML = renderGapHtml(res[0]);
}

async function renderReviewChangelogSubview(body) {
  const proj = encodeURIComponent(state.current);
  const cl = await Promise.allSettled([
    fetchJSON(`/api/projects/${proj}/changelogs`),
  ]);
  body.innerHTML = renderChangelogIndexHtml(cl[0]);

  // Wire row clicks → load + show body in-place (same pattern as the
  // old Quality panel).
  body.querySelectorAll("[data-changelog]").forEach(row => {
    row.addEventListener("click", async () => {
      const filename = row.dataset.changelog;
      const viewer = body.querySelector('[data-bind="changelog-viewer"]');
      body.querySelectorAll("[data-changelog]").forEach(r =>
        r.classList.toggle("selected", r === row));
      viewer.classList.remove("hidden");
      viewer.innerHTML = `<div class="muted small">Loading…</div>`;
      try {
        const text = await fetchText(
          `/api/projects/${proj}/changelogs/${encodeURIComponent(filename)}`);
        viewer.innerHTML = `<article class="prose">${renderMarkdown(text)}</article>`;
      } catch (err) {
        viewer.innerHTML = `<div class="muted small">Failed to load: ${escapeHtml(err.message)}</div>`;
      }
    });
  });
}

function renderLevelProgressionBanner() {
  const completed = state.runHistory?.summary?.levels_completed_successfully || [];
  const latest = state.runHistory?.latest_by_level || {};
  if (!completed.length && !Object.keys(latest).length) {
    // Fresh project — encourage starting at quick.
    return `
      <div class="card progression-banner">
        <div class="progression-head">
          <span class="dot ok"></span>
          <strong>Fresh project</strong>
        </div>
        <p class="muted small" style="margin: 6px 0 0;">
          Start with <strong>Quick</strong> to render the prose and see the document take shape (~5 min).
          Re-run as <strong>Standard</strong> later to add audit checks and a voice review (your rendered prose is reused, so only the new checks run).
          <strong>Deep</strong> adds an autocorrect convergence loop and a source-gap review against an external reference.
        </p>
      </div>`;
  }

  const stages = [
    {key: "quick", label: "Quick", adds: "Rendered prose"},
    {key: "standard", label: "Standard", adds: "+ Audit flags · Voice review"},
    {key: "deep", label: "Deep", adds: "+ Autocorrect convergence · Source-gap review"},
  ];
  const stageHtml = stages.map(s => {
    const done = completed.includes(s.key);
    const last = latest[s.key];
    const subtitle = last
      ? `${last.finalise_succeeded ? "delivered" : "blocked"} · ${formatTimestamp(new Date(last.finished_at).getTime() / 1000)}`
      : s.adds;
    return `
      <div class="progression-stage ${done ? "done" : "pending"}">
        <div class="dot ${done ? "ok" : "muted"}"></div>
        <div class="stage-info">
          <strong>${s.label}</strong>
          <span class="muted small">${escapeHtml(subtitle)}</span>
        </div>
      </div>`;
  }).join("");

  return `
    <div class="card progression-banner">
      <h3 class="subhead">What's been run</h3>
      <div class="progression-row">${stageHtml}</div>
      <p class="muted small" style="margin: 10px 0 0;">
        Each level builds on the previous one — already-rendered clusters are reused, so re-running at a higher level only does the additional steps.
      </p>
    </div>`;
}

// ─── status strip + level progression on Overview ────

function renderStatusStrip(main, detail, outlineStatus, drafts) {
  const slot = main.querySelector('[data-bind="status-strip"]');
  if (!slot) return;
  const completed = state.runHistory?.summary?.levels_completed_successfully || [];
  const outlineReady = outlineStatus && outlineStatus.outline.exists && outlineStatus.outline.is_structured;
  const rendered = detail.paper_words > 0;
  const auditComplete = completed.includes("standard") || completed.includes("deep");
  const sourceCount = (state.detail && state.detail.cluster_count !== undefined)
    ? (state.sourcesData?.indexed?.length || 0) : 0;
  // Each pip routes to the tab where the user can act on this state.
  const items = [
    {label: "Outline", ok: !!outlineReady, hint: outlineReady ? "structured" : "needs setup", tab: "outline"},
    {label: "Render", ok: rendered, hint: rendered ? `${detail.paper_words.toLocaleString()} words` : "not yet rendered", tab: "review"},
    {label: "Audit", ok: auditComplete, hint: auditComplete ? "complete" : "run Standard", tab: "review"},
    {label: "Source-gap", ok: completed.includes("deep"), hint: completed.includes("deep") ? "complete" : "run Deep + ref doc", tab: "review"},
    {label: "References", ok: state.runHistory?.history?.length > 0, hint: state.detail?.cluster_count ? "indexed" : "no sources yet", tab: "references"},
  ];
  slot.innerHTML = items.map(i => `
    <button type="button" class="status-pip ${i.ok ? "ok" : "pending"}" data-tab="${escapeAttr(i.tab)}" title="Jump to ${escapeHtml(i.tab)} tab">
      <span class="dot ${i.ok ? "ok" : "muted"}"></span>
      <strong>${escapeHtml(i.label)}</strong>
      <span class="muted small">${escapeHtml(i.hint)}</span>
    </button>`).join("");

  slot.querySelectorAll(".status-pip").forEach(btn => {
    btn.addEventListener("click", () => {
      navigate(`/p/${encodeURIComponent(state.current)}/${btn.dataset.tab}`);
    });
  });
}

// ─── live status / timeline ──────────────────────────

function startTimeline(runData, panel) {
  state.passes = new Map();
  state.currentPass = 1;

  const status = panel.querySelector('[data-bind="status"]');
  status.classList.remove("hidden");
  panel.querySelector('[data-bind="run-id"]').textContent = runData.run_id;
  panel.querySelector('[data-bind="pass"]').textContent = "Pass 1";
  panel.querySelector('[data-bind="state"]').textContent = "running";
  panel.querySelector('[data-bind="state"]').className = "pill running";
  panel.querySelector('[data-bind="timeline"]').innerHTML = "";
  panel.querySelector('[data-bind="summary"]').classList.add("hidden");
  panel.querySelector('[data-bind="summary"]').innerHTML = "";

  const startedAt = Date.now();
  if (state.elapsedTimer) clearInterval(state.elapsedTimer);
  state.elapsedTimer = setInterval(() => {
    const seconds = Math.round((Date.now() - startedAt) / 1000);
    panel.querySelector('[data-bind="elapsed"]').textContent = formatDuration(seconds);
  }, 1000);
}

function openWebSocket(projectName, runId, panel) {
  if (state.ws) try { state.ws.close(); } catch (e) {}
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/api/projects/${encodeURIComponent(projectName)}/runs/${runId}`;
  const ws = new WebSocket(url);
  state.ws = ws;
  ws.addEventListener("message", e => handleEvent(JSON.parse(e.data), panel));
  ws.addEventListener("close", () => {
    if (state.elapsedTimer) {
      clearInterval(state.elapsedTimer);
      state.elapsedTimer = null;
    }
  });
}

function handleEvent(event, panel) {
  switch (event.type) {
    case "pass_started":
      state.currentPass = event.pass_index;
      ensurePassRow(event.pass_index, event.total_passes, panel);
      panel.querySelector('[data-bind="pass"]').textContent =
        `Pass ${event.pass_index} of ${event.total_passes}`;
      break;
    case "phase_begun":
      upsertPhase(state.currentPass, event.phase, {
        state: "running", total: event.total, done: 0,
        status: event.status,
      }, panel);
      break;
    case "phase_advanced":
      upsertPhase(state.currentPass, event.phase, {
        state: "running", total: event.total, done: event.done,
        status: event.status, elapsed: event.elapsed_seconds,
      }, panel);
      break;
    case "phase_status":
      upsertPhase(state.currentPass, event.phase, {
        state: "running", status: event.status,
        elapsed: event.elapsed_seconds,
      }, panel);
      break;
    case "phase_skipped":
      // Stage didn't run because preconditions weren't met. Render
      // it as a greyed-out row so the user knows it was considered
      // and explicitly bypassed (instead of just missing).
      upsertPhase(state.currentPass, event.phase, {
        state: "skipped",
        status: `skipped — ${event.reason}`,
      }, panel);
      break;
    case "phase_ended":
      // Classify the end status as one of three visual states:
      //   - "complete"     — clean success
      //   - "intermediate" — phase didn't deliver but the pipeline is
      //                      explicitly continuing with more work
      //                      (yellow dot, "in progress" feel)
      //   - "failed"       — terminal failure, nothing else will fix it
      //                      (red dot)
      // Statuses like "not ready yet — pipeline continues with X" or
      // "still refused — convergence loop will retry" are
      // intermediate; "still refused — N cluster(s) could not be
      // recovered" is terminal.
      const status = (event.status || "").toLowerCase();
      const looksIntermediate = (
        status.includes("pipeline continues")
        || status.includes("will retry")
        || status.includes("retry after")
        || status.includes("retry on")
        || status.includes("not ready yet")
      );
      const looksTerminal = !looksIntermediate && (
        status.includes("refused")
        || status.includes("could not be recovered")
        || status.includes("still failed")
        || status.includes("recovery raised")
        || status.includes("0 recovered")
        || status.startsWith("failed")
      );
      let endState = "complete";
      if (looksTerminal) endState = "failed";
      else if (looksIntermediate) endState = "intermediate";
      upsertPhase(state.currentPass, event.phase, {
        state: endState,
        status: event.status,
        elapsed: event.elapsed_seconds,
      }, panel);
      break;
    case "run_finished": finishRun(event, panel); break;
    case "run_failed":   failRun(event, panel); break;
  }
}

function ensurePassRow(passIndex, totalPasses, panel) {
  const tl = panel.querySelector('[data-bind="timeline"]');
  if (tl.querySelector(`.timeline-pass[data-pass-index="${passIndex}"]`)) return;
  const li = document.createElement("li");
  li.className = "timeline-pass";
  li.dataset.passIndex = passIndex;
  li.textContent = `Pass ${passIndex} of ${totalPasses}`;
  tl.appendChild(li);
}

function upsertPhase(passIndex, phase, patch, panel) {
  ensurePassRow(passIndex, document.querySelector('[data-bind="pass"]').textContent
    .replace("Pass ", "").split(" of ")[1] || passIndex, panel);

  let item = panel.querySelector(
    `.timeline-item[data-pass="${passIndex}"][data-phase="${phase}"]`);
  if (!item) {
    item = document.createElement("li");
    item.className = "timeline-item running";
    item.dataset.pass = passIndex;
    item.dataset.phase = phase;
    item.innerHTML = `
      <div class="dot"></div>
      <div class="label">
        <span class="phase-name">${escapeHtml(PHASE_LABELS[phase] || phase)}</span>
        <span class="status"></span>
      </div>
      <div class="progress-text"></div>
      <div class="elapsed">—</div>
      <div class="progress-bar"><div class="fill"></div></div>`;
    panel.querySelector('[data-bind="timeline"]').appendChild(item);
  }

  item.classList.remove("running", "complete", "failed", "intermediate", "skipped");
  item.classList.add(patch.state || "running");
  item.querySelector(".status").textContent = patch.status || "";

  // When a phase ends successfully, snap the progress bar/text to
  // 100%. Some phases (audit, finalise) never call advance(), so
  // their counter would otherwise stay at "0/N" forever even though
  // the work completed.
  const terminalStates = new Set(["complete", "failed", "intermediate", "skipped"]);
  const done = terminalStates.has(patch.state) && patch.state !== "skipped"
    ? (patch.total || patch.done || 0) : (patch.done || 0);
  if (patch.total && patch.state !== "skipped") {
    item.querySelector(".progress-text").textContent =
      `${done}/${patch.total}`;
    const pct = Math.min(100, Math.round((done / patch.total) * 100));
    item.querySelector(".progress-bar .fill").style.width = `${pct}%`;
  } else if (patch.state === "skipped") {
    item.querySelector(".progress-text").textContent = "—";
    item.querySelector(".progress-bar .fill").style.width = "0%";
  } else if (terminalStates.has(patch.state)) {
    item.querySelector(".progress-bar .fill").style.width = "100%";
  }
  if (typeof patch.elapsed === "number") {
    item.querySelector(".elapsed").textContent = formatDuration(patch.elapsed);
  }
}

function finishRun(event, panel) {
  const stateBadge = panel.querySelector('[data-bind="state"]');
  stateBadge.textContent = event.finalise_succeeded ? "delivered" : "blocked";
  stateBadge.className = `pill ${event.finalise_succeeded ? "ok" : "warn"}`;

  if (state.elapsedTimer) { clearInterval(state.elapsedTimer); state.elapsedTimer = null; }
  panel.querySelector('[data-bind="elapsed"]').textContent =
    formatDuration(event.elapsed_seconds);

  const summary = panel.querySelector('[data-bind="summary"]');
  summary.classList.remove("hidden");
  const rows = [
    ["Final document", event.final_path
      ? `<span class="mono">${escapeHtml(event.final_path)}</span>`
      : '<span class="muted">refused — see blocking detail below</span>'],
    ["Clusters rendered", `${event.rendered_clusters} of ${event.total_clusters}`],
    ["Audit flags", String(event.audit_flags || 0)],
    ["Voice review",
      event.voice_review_path ? `<span class="mono">${escapeHtml(event.voice_review_path)}</span>` : "—"],
    ["Source-gap review",
      event.source_gap_path ? `<span class="mono">${escapeHtml(event.source_gap_path)}</span>` : "—"],
    ["Total elapsed", formatDuration(event.elapsed_seconds)],
  ];
  if (event.notes && event.notes.length) {
    rows.push(["Notes", event.notes.map(escapeHtml).join("<br>")]);
  }

  let blockingHtml = "";
  if (event.blocking) {
    blockingHtml = renderBlockingDetail(event.blocking);
  }

  summary.innerHTML = `
    <h4>Result</h4>
    <div class="run-summary-grid">
      ${rows.map(([k, v]) => `<div class="key">${escapeHtml(k)}</div><div class="val">${v}</div>`).join("")}
    </div>
    ${blockingHtml}`;
}

function renderBlockingDetail(blocking) {
  const flags = blocking.readiness_flags || [];
  const failed = blocking.failed_clusters || [];
  const errors = blocking.errors || [];

  if (!flags.length && !failed.length && !errors.length && !blocking.raw_delivery_blocked) {
    return "";
  }

  const flagsHtml = flags.length
    ? `<div class="block-section">
         <h5>Readiness flags</h5>
         <ul class="block-list">
           ${flags.map(f => `
             <li>
               <code class="block-tag">${escapeHtml(f.category)}</code>
               <span class="block-count">×${f.count}</span>
               <div class="block-msg">${escapeHtml(f.message)}</div>
               ${f.fix ? `<div class="block-fix">→ ${escapeHtml(f.fix)}</div>` : ""}
             </li>`).join("")}
         </ul>
       </div>`
    : "";

  const failedHtml = failed.length
    ? `<div class="block-section">
         <h5>Failed clusters</h5>
         <ul class="block-list">
           ${failed.map(c => `
             <li>
               <code class="block-tag">${escapeHtml(c.cluster_id)}</code>
               <div class="block-msg mono">${escapeHtml(c.reason)}</div>
               <div class="block-fix muted small">prose file: <code>${escapeHtml(c.prose_file)}</code></div>
             </li>`).join("")}
         </ul>
       </div>`
    : "";

  const errorsHtml = errors.length
    ? `<div class="block-section">
         <h5>Diagnostic errors</h5>
         <ul class="block-list">
           ${errors.map(e => `<li class="block-msg mono">${escapeHtml(e)}</li>`).join("")}
         </ul>
       </div>`
    : "";

  const rawHtml = blocking.raw_delivery_blocked
    ? `<details class="block-raw">
         <summary>Show raw delivery_blocked.md</summary>
         <pre>${escapeHtml(blocking.raw_delivery_blocked)}</pre>
       </details>`
    : "";

  return `
    <div class="blocking-detail">
      <h4>Why was this refused?</h4>
      ${flagsHtml}
      ${failedHtml}
      ${errorsHtml}
      ${rawHtml}
    </div>`;
}

const FAILURE_HINTS = {
  no_outline: {
    title: "No outline found",
    body: "This project has no <code>structure/outline.md</code> file yet. Open the project's Sources tab to upload one, or paste an outline into the file directly.",
  },
  outline_has_no_structure: {
    title: "Outline isn't in lattice format",
    body: `The outline file was found but didn't contain any <code>#&nbsp;THESIS</code> or <code>#&nbsp;A.&nbsp;Section</code> headers. Lattice expects a structured outline, not raw paper prose. Edit <code class="mono" data-bind="outline-path"></code> to look like:
<pre class="failure-example"># THESIS

Your thesis sentence.

# A. Section heading

  - First claim
  - MY VIEW: your synthesis [user_synthesis]
</pre>`,
  },
  empty_cluster_plan: {
    title: "No clusters built",
    body: "Ingest succeeded but the cluster planner produced nothing. Check that your outline has at least one section with bullet claims under it.",
  },
  ingest_failed: {
    title: "Outline parser crashed",
    body: "The markdown / docx parser hit an exception while reading your outline. Inspect the detail below for the exact error.",
  },
  plan_failed: {
    title: "Cluster plan crashed",
    body: "The planner hit an exception while turning claims into clusters. Inspect the detail below.",
  },
  claude_not_available: {
    title: "Claude CLI not found",
    body: "Lattice runs the pipeline against your local Claude Code subscription. Make sure the <code>claude</code> CLI is installed and on your PATH, then retry.",
  },
  auto_structure_failed: {
    title: "Auto-structuring failed",
    body: "Lattice tried to convert your raw prose into a structured outline using Claude, but the call failed. Inspect the detail below — common causes are network errors or the model returning unexpected output. You can also write the outline by hand using <code>#&nbsp;THESIS</code> / <code>#&nbsp;A.&nbsp;Section</code> headers.",
  },
};

function failRun(event, panel) {
  const stateBadge = panel.querySelector('[data-bind="state"]');
  stateBadge.textContent = "failed";
  stateBadge.className = "pill bad";
  if (state.elapsedTimer) { clearInterval(state.elapsedTimer); state.elapsedTimer = null; }
  const summary = panel.querySelector('[data-bind="summary"]');
  summary.classList.remove("hidden");

  const reason = event.reason || "unknown";
  const hint = FAILURE_HINTS[reason];
  const title = hint ? hint.title : "Run failed";
  const body = hint ? hint.body : "";

  summary.innerHTML = `
    <h4>${escapeHtml(title)}</h4>
    ${body ? `<div class="failure-body">${body}</div>` : ""}
    <div class="run-summary-grid">
      <div class="key">Reason code</div><div class="val mono">${escapeHtml(reason)}</div>
      ${event.detail ? `<div class="key">Detail</div><div class="val mono" style="white-space: pre-wrap;">${escapeHtml(event.detail)}</div>` : ""}
      ${event.outline_path ? `<div class="key">Outline file</div><div class="val mono">${escapeHtml(event.outline_path)}</div>` : ""}
    </div>
    ${event.traceback
      ? `<details class="block-raw" style="margin-top: 12px;">
           <summary>Show Python traceback</summary>
           <pre>${escapeHtml(event.traceback)}</pre>
         </details>`
      : ""}`;

  // Backfill the outline path placeholder in the hint body if present.
  if (event.outline_path) {
    const slot = summary.querySelector('[data-bind="outline-path"]');
    if (slot) slot.textContent = event.outline_path;
  }
}

// ─── helpers ────────────────────────────────────────

function cloneTemplate(id) {
  const tpl = document.getElementById(id);
  if (!tpl) {
    console.error(`Template ${id} not found`);
    const fallback = document.createElement("div");
    fallback.textContent = `Template ${id} missing`;
    return fallback;
  }
  const content = tpl.content.cloneNode(true);
  // If the template has exactly one element child, return it directly
  // so callers can use .querySelector() and .appendChild() naturally.
  // Otherwise return the DocumentFragment, which also supports
  // querySelector — but a DocumentFragment "loses" its children when
  // appended, so callers should not retain a reference to it after
  // appendChild. Templates with multiple roots should wrap in a div.
  const elements = content.children;
  if (elements.length === 1) return elements[0];
  console.warn(`Template ${id} has ${elements.length} root elements; wrap in a single div.`);
  return content;
}

async function fetchJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status} on ${url}`);
  return resp.json();
}

async function fetchText(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status} on ${url}`);
  return resp.text();
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, ch =>
    ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[ch]));
}

function escapeAttr(s) { return escapeHtml(s).replace(/"/g, "&quot;"); }

/** Mirror of the backend _slugify_project_name. Used for the live
 * folder-name preview in the New Project modal so the user can see
 * what their input will become before submitting. */
function slugifyName(s) {
  if (!s) return "";
  // Anything that isn't alphanumeric/underscore/dash → space.
  const cleaned = s.replace(/[^A-Za-z0-9_\-]+/g, " ").trim().toLowerCase();
  // Collapse internal whitespace runs to single underscores.
  return cleaned.replace(/\s+/g, "_").replace(/^[_\-]+|[_\-]+$/g, "").slice(0, 80);
}

function truncate(s, n) {
  s = String(s ?? "");
  return s.length > n ? s.slice(0, n - 1).trim() + "…" : s;
}

function formatTimestamp(ts) {
  if (!ts) return "no render yet";
  const d = new Date(ts * 1000);
  const now = Date.now();
  const diffMin = (now - d.getTime()) / 60000;
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${Math.round(diffMin)}m ago`;
  if (diffMin < 60 * 24) return `${Math.round(diffMin / 60)}h ago`;
  return d.toLocaleDateString();
}

function formatDuration(seconds) {
  if (typeof seconds !== "number" || seconds < 0) return "—";
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  if (m < 60) return `${m}m ${String(r).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${String(m % 60).padStart(2, "0")}m`;
}

function formatBytes(b) {
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${(b / (1024 * 1024)).toFixed(1)} MB`;
}
