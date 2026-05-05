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
  main.querySelector('[data-action="compare-projects"]')
    .addEventListener("click", () => openCompareModal(state.projects || []));

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
  let detail, hierarchy, drafts, outlineStatus, runHistory, projectStateData;
  try {
    [detail, hierarchy, drafts, outlineStatus, runHistory, projectStateData] = await Promise.all([
      fetchJSON(`/api/projects/${encodeURIComponent(name)}`),
      fetchJSON(`/api/projects/${encodeURIComponent(name)}/hierarchy`),
      fetchJSON(`/api/projects/${encodeURIComponent(name)}/drafts`),
      fetchJSON(`/api/projects/${encodeURIComponent(name)}/outline-status`)
        .catch(() => null),
      fetchJSON(`/api/projects/${encodeURIComponent(name)}/run-history`)
        .catch(() => ({history: [], latest_by_level: {}, summary: {}})),
      fetchJSON(`/api/projects/${encodeURIComponent(name)}/state`)
        .catch(() => null),
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
  state.projectState = projectStateData;

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

  // Header actions: rename + Full Review (split button — primary
  // runs the saved selection; the caret opens a popover where the
  // user toggles which activities to include and picks a mode).
  const headerActions = main.querySelector('[data-bind="header-actions"]');
  headerActions.innerHTML = `
    <button class="btn" data-action="rename-project" title="Rename project or folder">Rename</button>
    ${buildFullReviewControlsHtml()}
  `;
  headerActions.querySelector('[data-action="rename-project"]').addEventListener("click", () => {
    renameProjectPrompt({name, display_name: detail.display_name || name});
  });
  wireFullReviewControls(headerActions, name);

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

  // Subnav: four tabs, each with its own sub-tabs underneath
  // (matching the Output pattern for visual consistency).
  switch (sectionId) {
    case "dashboard":  renderDashboardTab(main, subSectionId); break;
    case "activities": renderActivitiesTab(main, subSectionId); break;
    case "sources":    renderSourcesTab(main, subSectionId); break;
    case "citations":  renderCitationsTab(main, subSectionId); break;
    case "output":     renderOutputTab(main, subSectionId); break;
    // Legacy URLs gracefully redirect.
    case "overview":
    case "drafts":     navigate(`/p/${encodeURIComponent(state.current)}/dashboard`); break;
    case "review":
    case "run":        navigate(`/p/${encodeURIComponent(state.current)}/activities`); break;
    case "outline":
    case "hierarchy":  navigate(`/p/${encodeURIComponent(state.current)}/sources/outline`); break;
    case "references": navigate(`/p/${encodeURIComponent(state.current)}/sources/references`); break;
    case "audit":
    case "voice":
    case "gap":
    case "changelog":
    case "quality":
    case "reviews":    navigate(`/p/${encodeURIComponent(state.current)}/output`); break;
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
  const projectStateData = state.projectState || null;
  const activityHistory = projectStateData?.history || [];
  const lastActivity = activityHistory.slice(-1)[0] || null;

  // ── 1. Action items panel — what the user should do next ──
  const actionItems = computeActionItems({
    outlineStatus, drafts, projectStateData, auditFlags,
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
                  <p>No paper produced yet. Set up an outline, then run Scaffold and Draft.</p>
                  <div style="display: flex; gap: 8px; margin-top: 12px; justify-content: center;">
                    <button class="btn primary" data-action="goto-outline">Go to Outline</button>
                    <button class="btn" data-action="goto-activities">Go to Activities</button>
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
        ${renderDashboardLatestActivityCard(lastActivity, lastRun)}
        ${renderDashboardRecentActivityCard(activityHistory, runHistory.history || [], changelogs)}
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

function computeActionItems({outlineStatus, drafts, projectStateData, auditFlags}) {
  const items = [];
  const markers = projectStateData?.markers || {};
  const next = projectStateData?.next_activity || null;

  if (!outlineStatus || !outlineStatus.outline.exists) {
    items.push({
      severity: "bad",
      title: "No outline yet",
      body: "Add a `# THESIS` and `# A. Section` outline before scaffolding.",
      cta: {tab: "outline", label: "Go to Outline"},
    });
  } else if (!outlineStatus.outline.is_structured) {
    items.push({
      severity: "warn",
      title: "Outline is raw prose",
      body: "Run Scaffold to auto-structure it into thesis, sections, and claim bullets.",
      cta: {tab: "activities", label: "Go to Scaffold"},
    });
  }
  if (drafts.length === 0 && outlineStatus?.outline.is_structured && markers.has_clusters) {
    items.push({
      severity: "warn",
      title: "Scaffold ready, no draft yet",
      body: "Run Draft to render the prose for the first time.",
      cta: {tab: "activities", label: "Go to Draft"},
    });
  }
  const criticalFlags = auditFlags.filter(f => f.severity === "critical").length;
  if (criticalFlags > 0) {
    items.push({
      severity: "bad",
      title: `${criticalFlags} critical audit flag${criticalFlags === 1 ? "" : "s"}`,
      body: "Critical issues block delivery. Re-run Refine with autocorrect or address by hand.",
      cta: {tab: "output", subtab: "audit", label: "View flags"},
    });
  }
  if (markers.has_paper && !markers.has_audit_flags) {
    items.push({
      severity: "info",
      title: "Draft exists but no audit run",
      body: "Refine adds audit + voice review on top of the existing draft.",
      cta: {tab: "activities", label: "Go to Refine"},
    });
  }
  // If nothing else matched but we have a recommendation, surface it
  // as a low-priority "what next?" hint so the user always sees the
  // single best next step.
  if (items.length === 0 && next) {
    items.push({
      severity: "info",
      title: `Recommended next: ${next.label}`,
      body: next.why,
      cta: {tab: "activities", label: `Go to ${next.label}`},
    });
  }
  return items;
}

// Capitalises an activity verb (e.g. "find_gaps" → "Find gaps") for
// display, falling back to the raw label if the verb is unknown.
const ACTIVITY_LABEL_MAP = {
  ingest: "Ingest", scaffold: "Scaffold", draft: "Draft",
  find_gaps: "Find gaps", refine: "Refine",
  restructure: "Restructure", review: "Review",
};
function activityLabel(verb) {
  if (!verb) return "Activity";
  return ACTIVITY_LABEL_MAP[verb] || verb;
}

function renderDashboardLatestActivityCard(lastActivity, lastRun) {
  // Prefer the new activity history; fall back to legacy run-history
  // for projects that only have pre-activity records.
  if (!lastActivity && !lastRun) {
    return `
      <div class="card">
        <h3 class="subhead">Latest activity</h3>
        <p class="muted small">Nothing run yet.</p>
        <button class="btn primary sm" data-action="goto-activities">Start an activity →</button>
      </div>`;
  }
  if (lastActivity) {
    const ok = lastActivity.ok !== false;
    const finished = formatTimestamp(new Date(lastActivity.finished_at).getTime() / 1000);
    return `
      <div class="card">
        <h3 class="subhead">Latest activity</h3>
        <div class="kv-list">
          <div class="kv"><span class="k">Activity</span><span class="v"><span class="pill">${escapeHtml(activityLabel(lastActivity.verb))}</span></span></div>
          <div class="kv"><span class="k">Mode</span><span class="v">${escapeHtml(lastActivity.mode || "thorough")}</span></div>
          <div class="kv"><span class="k">Outcome</span><span class="v">${ok ? `<span class="pill ok">ok</span>` : `<span class="pill bad">failed</span>`}</span></div>
          <div class="kv"><span class="k">Duration</span><span class="v">${formatDuration(lastActivity.elapsed_seconds || 0)}</span></div>
          <div class="kv"><span class="k">Finished</span><span class="v">${finished}</span></div>
        </div>
        <button class="btn primary sm" style="margin-top: 12px; width: 100%;" data-action="goto-activities">Open Activities →</button>
      </div>`;
  }
  // Legacy fallback for projects with no activity_history.json yet.
  const ok = lastRun.finalise_succeeded;
  const finished = formatTimestamp(new Date(lastRun.finished_at).getTime() / 1000);
  const finalPath = lastRun.final_path || "";
  const finalFilename = finalPath ? finalPath.split(/[\\/]/).pop() : "";
  return `
    <div class="card">
      <h3 class="subhead">Latest activity <span class="muted small">(legacy)</span></h3>
      <div class="kv-list">
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

function renderDashboardRecentActivityCard(activityHistory, runHistory, changelogs) {
  // Merge activity history (verb-named) and legacy run history
  // (level-named) into a single chronological feed. Activity entries
  // always render with their verb; legacy entries render with a
  // "legacy" tag and no review-depth wording.
  const merged = [];
  for (const a of activityHistory) {
    merged.push({
      kind: "activity",
      label: activityLabel(a.verb),
      finished_at: a.finished_at,
      ok: a.ok !== false,
      elapsed: a.elapsed_seconds || 0,
      stats: a.stats || null,
    });
  }
  for (const r of runHistory) {
    merged.push({
      kind: "legacy",
      label: "legacy run",
      finished_at: r.finished_at,
      ok: !!r.finalise_succeeded,
      elapsed: r.elapsed_seconds || 0,
      stats: `${r.rendered_clusters}/${r.total_clusters} clusters · ${r.audit_flags || 0} flags`,
      changelogKey: r.level || "",
    });
  }
  if (!merged.length && !changelogs.length) return "";
  merged.sort((a, b) => (a.finished_at < b.finished_at ? 1 : -1));
  const recent = merged.slice(0, 5);
  const rows = recent.map(r => {
    const finished = formatTimestamp(new Date(r.finished_at).getTime() / 1000);
    const matchingLog = changelogs.find(cl => cl.filename.startsWith(
      r.finished_at.slice(0, 4) + r.finished_at.slice(5, 7) + r.finished_at.slice(8, 10)
    ) && (r.changelogKey ? cl.filename.includes(r.changelogKey) : true));
    const okPill = r.ok ? `<span class="pill ok">✓</span>` : `<span class="pill bad">✗</span>`;
    const tag = r.kind === "legacy" ? `<span class="muted small">(legacy)</span>` : "";
    const stats = typeof r.stats === "string" ? r.stats : "";
    return `
      <li class="activity-row">
        ${okPill}
        <div class="activity-meta">
          <strong>${escapeHtml(r.label)}</strong> ${tag}
          <span class="muted small">${finished} · ${formatDuration(r.elapsed)}</span>
        </div>
        <div class="activity-stats muted small">${stats}</div>
        <div>${matchingLog
          ? `<button class="btn-link sm" data-changelog="${escapeAttr(matchingLog.filename)}">view changes</button>`
          : ""}</div>
      </li>`;
  }).join("");
  return `
    <div class="card">
      <h3 class="subhead">Recent activity</h3>
      <ul class="activity-list">${rows}</ul>
      <button class="btn-link sm" data-action="goto-activities">Full history in Activities tab →</button>
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

  // Toolbar: stats + tree-control buttons. The interactive graph
  // lives in its own sub-tab now (Sources → Graph), so the Tree/Graph
  // toggle that used to be here is gone.
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
        <button class="btn sm" data-action="expand-all">Expand all</button>
        <button class="btn sm" data-action="collapse-all">Collapse all</button>
        <a class="btn sm" href="/api/projects/${projSlug}/export/teaching-deck" download>Export to PowerPoint</a>
      </div>
    </div>`;

  // Build a parent -> children map so nested sections render under
  // their parent. Sections without a parent are top-level. The order
  // within siblings respects ``position``.
  const childrenByParent = new Map();
  h.sections.forEach(s => {
    const key = s.parent || "__root__";
    if (!childrenByParent.has(key)) childrenByParent.set(key, []);
    childrenByParent.get(key).push(s);
  });
  for (const list of childrenByParent.values()) {
    list.sort((a, b) => (a.position ?? 0) - (b.position ?? 0));
  }

  function renderSectionNode(s) {
    const depth = s.depth ?? 0;
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

    const childSections = (childrenByParent.get(s.section_id) || []);
    const childSectionsHtml = childSections.map(renderSectionNode).join("");

    return `
      <div class="tree-section depth-${depth} ${depth === 0 ? "" : "collapsed"}" data-section-id="${escapeAttr(s.section_id)}">
        <div class="tree-section-head" data-toggle="section">
          <svg class="chevron" width="14" height="14" viewBox="0 0 12 12"><path d="M3 4 L6 8 L9 4" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>
          <span class="tree-section-title">${escapeHtml(s.title || s.section_id)}</span>
          <div class="tree-section-meta">
            <span class="pill">${escapeHtml(s.role || "argumentative")}</span>
            <span class="pill mono">${s.clusters.length} clusters${childSections.length ? ` · ${childSections.length} subsection${childSections.length === 1 ? "" : "s"}` : ""}</span>
          </div>
        </div>
        <div class="tree-section-body">
          ${clustersHtml || (childSections.length ? "" : '<p class="muted small">No clusters in this section yet.</p>')}
          ${childSectionsHtml}
        </div>
      </div>`;
  }

  const rootSections = childrenByParent.get("__root__") || [];
  const sectionsHtml = rootSections.map(renderSectionNode).join("");

  panel.innerHTML = `
    ${toolbarHtml}
    <div class="hierarchy-tree-view"><div class="tree">${sectionsHtml}</div></div>`;

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

}


// Standalone interactive-graph view, used by the Sources → Graph
// sub-tab. The /graph-viz endpoint serves a self-contained
// cytoscape.js page; we just iframe it.
function renderHierarchyGraph(main) {
  const panel = main.querySelector('[data-bind="graph"]');
  if (!panel) return;
  const projSlug = encodeURIComponent(state.current);
  // Voices drive the unrenderable-marker scan: the visualiser reads
  // ``.lattice/drafts/<voice>/cluster_*.md``. Surface a picker only
  // when the project has rendered more than one voice.
  const voices = state.detail?.voices || [];
  const voiceQs = v => v ? `?voice=${encodeURIComponent(v)}` : "";
  const initialVoice = voices[0] || "";
  const picker = voices.length > 1
    ? `<label class="muted small" style="display:flex; gap:6px; align-items:center;">
         Voice
         <select data-bind="graph-voice">
           ${voices.map(v => `<option value="${escapeAttr(v)}">${escapeHtml(v)}</option>`).join("")}
         </select>
       </label>`
    : "";
  panel.innerHTML = `
    <div class="graph-frame-wrap">
      <div class="graph-controls" style="display:flex; gap:12px; align-items:center; justify-content:flex-end; margin-bottom:6px;">
        ${picker}
      </div>
      <iframe class="graph-viz-frame" data-bind="graph-iframe"
              src="/api/projects/${projSlug}/graph-viz${voiceQs(initialVoice)}"
              loading="lazy"></iframe>
      <p class="muted small" style="text-align: right; margin-top: 6px;">
        Interactive cytoscape.js layout · drag nodes, scroll to zoom, click for details
      </p>
    </div>`;
  const select = panel.querySelector('[data-bind="graph-voice"]');
  const iframe = panel.querySelector('[data-bind="graph-iframe"]');
  if (select && iframe) {
    select.addEventListener("change", () => {
      iframe.src = `/api/projects/${projSlug}/graph-viz${voiceQs(select.value)}`;
    });
  }
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
    : `<div class="muted small" style="padding: 8px 0;">No drafts yet. Run <strong>Draft</strong> to generate one.</div>`;

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
    return `<div class="empty-state"><h3>No change logs yet</h3><p>Each activity writes a markdown changelog here so you can see exactly what it modified — clusters re-rendered, audit-flag deltas, outline mutations, paper word-count changes. Run an activity to populate this.</p></div>`;
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
      <p class="muted small">Click a row to inspect what that activity changed.</p>
      <div class="draft-list">${rows}</div>
    </div>
    <div class="card hidden" data-bind="changelog-viewer"></div>`;
}

function renderAuditHtml(res) {
  if (res.status !== "fulfilled") {
    return `<div class="empty-state"><h3>No audit data</h3><p>Run <strong>Refine</strong> to audit the draft and populate this view.</p></div>`;
  }
  const raw = res.value && res.value.flags;
  let flags = [];
  if (Array.isArray(raw)) flags = raw;
  else if (raw && typeof raw === "object") flags = Object.values(raw).flat();
  if (!flags.length) {
    return `<div class="empty-state"><h3>No audit flags</h3><p>The last Refine run found nothing to flag — or hasn't been run yet.</p></div>`;
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
    return `<div class="card"><p class="muted small">Voice review not generated yet. Run <strong>Refine</strong> to produce one.</p></div>`;
  }
  return `
    <div class="card">
      <article class="prose" style="font-size: 14px; line-height: 1.6;">${renderMarkdown(res.value)}</article>
    </div>`;
}

function renderGapHtml(res) {
  if (res.status !== "fulfilled") {
    return `<div class="card"><p class="muted small">Source-gap review not generated yet. Run <strong>Find gaps</strong> with a reference document path in Advanced options.</p></div>`;
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

// ─── review sub-views: audit / voice / gap / changelog ──────
//
// These are shared by the Output tab subnav (see renderOutputTab).
// The historical "Review" top-level tab and its quick/standard/deep
// run form have been removed — runs now go through the Activities
// tab and call the verb-named endpoints under /api/projects/.../activities/.

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
    fetchJSON(`/api/projects/${proj}/lit-gaps`),
  ]);
  body.innerHTML = renderLitGapsHtml(res[0]);
}


function renderLitGapsHtml(res) {
  if (res.status !== "fulfilled") {
    return `
      <div class="card">
        <p class="muted small">No lit-gaps report yet. Run <strong>Find gaps</strong> from the Activities tab — it analyses the scaffold per section to surface canonical works, counter-arguments, and recent literature the paper isn't engaging with.</p>
      </div>`;
  }
  const report = res.value;
  if (!report.sections || !report.sections.length) {
    return `<div class="card"><p class="muted small">Report has no sections.</p></div>`;
  }
  const verifiedRatio = report.total_suggestions
    ? `${report.verified_count}/${report.total_suggestions}`
    : "0/0";
  const generatedTime = report.generated_at
    ? formatTimestamp(new Date(report.generated_at).getTime() / 1000)
    : "—";

  const sectionsHtml = report.sections.map(sec => {
    if (!sec.suggestions.length) {
      return `
        <div class="lit-section">
          <h4 class="lit-section-head">${escapeHtml(sec.section_title)}
            <span class="muted small">no gaps suggested</span></h4>
        </div>`;
    }
    const byKind = {canonical: [], counter_argument: [], recent: []};
    sec.suggestions.forEach(s => (byKind[s.kind] || byKind.canonical).push(s));
    const renderGroup = (label, items) => items.length ? `
      <div class="lit-group">
        <h5 class="lit-group-head">${escapeHtml(label)} <span class="muted small">${items.length}</span></h5>
        ${items.map(renderLitSuggestion).join("")}
      </div>` : "";
    return `
      <div class="lit-section">
        <h4 class="lit-section-head">
          ${escapeHtml(sec.section_title)}
          <span class="muted small">${sec.suggestions.length} suggestion${sec.suggestions.length === 1 ? "" : "s"}</span>
        </h4>
        ${renderGroup("Canonical works", byKind.canonical)}
        ${renderGroup("Counter-arguments", byKind.counter_argument)}
        ${renderGroup("Recent (last 5y)", byKind.recent)}
      </div>`;
  }).join("");

  return `
    <div class="card">
      <div class="lit-summary">
        <h3 class="subhead">Literature gaps · ${report.total_suggestions} suggestion(s)</h3>
        <div class="lit-summary-meta">
          <span class="pill ${report.mode === "thorough" ? "ok" : ""}">${escapeHtml(report.mode)} mode</span>
          <span class="muted small">Verified on OpenAlex: <strong>${verifiedRatio}</strong></span>
          <span class="muted small">Generated: ${generatedTime}</span>
        </div>
      </div>
      <div class="lit-sections">${sectionsHtml}</div>
    </div>`;
}


function renderLitSuggestion(s) {
  const verifiedBadge = s.verified
    ? `<span class="pill ok">verified</span>`
    : `<span class="pill" title="Not verified on OpenAlex — could be hallucinated">unverified</span>`;
  const confBadge = `<span class="pill conf-${escapeAttr(s.confidence)}">${escapeHtml(s.confidence)}</span>`;
  const claimRefs = (s.claim_ids || []).length
    ? `<span class="muted small">→ ${s.claim_ids.map(c => `<code>${escapeHtml(c)}</code>`).join(" ")}</span>`
    : "";
  const linkBits = [];
  if (s.doi) {
    const cleanDoi = String(s.doi).replace(/^https?:\/\/doi\.org\//, "");
    linkBits.push(
      `<a href="https://doi.org/${encodeURIComponent(cleanDoi)}" target="_blank" rel="noopener">DOI</a>`
    );
  }
  if (s.openalex_id) {
    linkBits.push(`<a href="${escapeAttr(s.openalex_id)}" target="_blank" rel="noopener">OpenAlex</a>`);
  }
  const citationLine = s.canonical_title
    ? `<div class="lit-canonical muted small">
         <strong>${escapeHtml(s.canonical_authors[0] || s.author)}</strong>
         ${s.publication_year ? ` (${s.publication_year})` : ""}
         · ${escapeHtml(s.canonical_title)}
         ${typeof s.cited_by_count === "number" ? ` · cited ${s.cited_by_count.toLocaleString()}×` : ""}
         ${linkBits.length ? ` · ${linkBits.join(" · ")}` : ""}
       </div>`
    : "";
  return `
    <div class="lit-suggestion ${s.verified ? "verified" : "unverified"}">
      <div class="lit-head">
        <span class="lit-author">${escapeHtml(s.author)}${s.year ? ` (${s.year})` : ""}</span>
        <span class="lit-title">${escapeHtml(s.work)}</span>
      </div>
      <div class="lit-meta">
        ${verifiedBadge}
        ${confBadge}
        ${claimRefs}
      </div>
      <p class="lit-why">${escapeHtml(s.why_relevant)}</p>
      ${citationLine}
    </div>`;
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

// ─── status strip on project header ────

function renderStatusStrip(main, detail, outlineStatus, drafts) {
  const slot = main.querySelector('[data-bind="status-strip"]');
  if (!slot) return;
  const markers = state.projectState?.markers || {};
  const next = state.projectState?.next_activity || null;
  const outlineReady = outlineStatus && outlineStatus.outline.exists && outlineStatus.outline.is_structured;
  const rendered = !!markers.has_paper || detail.paper_words > 0;
  const auditComplete = !!markers.has_audit_flags;
  // Each pip routes to the tab where the user can act on this state.
  // Hint copy refers to activities (Scaffold/Draft/Refine/Find gaps),
  // not to the retired quick/standard/deep review levels.
  const items = [
    {
      label: "Outline",
      ok: !!outlineReady,
      hint: outlineReady ? "structured" : "needs setup",
      tab: "sources",
      subtab: "outline",
    },
    {
      label: "Scaffold",
      ok: !!markers.has_clusters,
      hint: markers.has_clusters ? "graph + clusters ready" : "run Scaffold",
      tab: "activities",
    },
    {
      label: "Draft",
      ok: rendered,
      hint: rendered
        ? `${(detail.paper_words || 0).toLocaleString()} words`
        : "run Draft",
      tab: "activities",
    },
    {
      label: "Refine",
      ok: auditComplete,
      hint: auditComplete ? "audit complete" : "run Refine",
      tab: "activities",
    },
    {
      label: "References",
      ok: state.detail?.cluster_count > 0,
      hint: state.detail?.cluster_count ? "indexed" : "no sources yet",
      tab: "sources",
      subtab: "references",
    },
  ];
  slot.innerHTML = items.map(i => `
    <button type="button" class="status-pip ${i.ok ? "ok" : "pending"}" data-tab="${escapeAttr(i.tab)}"${i.subtab ? ` data-subtab="${escapeAttr(i.subtab)}"` : ""} title="Jump to ${escapeHtml(i.tab)} tab">
      <span class="dot ${i.ok ? "ok" : "muted"}"></span>
      <strong>${escapeHtml(i.label)}</strong>
      <span class="muted small">${escapeHtml(i.hint)}</span>
    </button>`).join("")
    + (next
      ? `<div class="status-pip next-activity" title="Recommended next step">
          <span class="dot ok"></span>
          <strong>Next: ${escapeHtml(next.label)}</strong>
          <span class="muted small">${escapeHtml(next.why)}</span>
        </div>`
      : "");

  slot.querySelectorAll(".status-pip[data-tab]").forEach(btn => {
    btn.addEventListener("click", () => {
      const tab = btn.dataset.tab;
      const subtab = btn.dataset.subtab;
      navigate(subtab
        ? `/p/${encodeURIComponent(state.current)}/${tab}/${encodeURIComponent(subtab)}`
        : `/p/${encodeURIComponent(state.current)}/${tab}`);
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


// ════════════════════════════════════════════════════════════
// Activities tab — verb-oriented action surface (replaces the
// review-level radio + "Start review" button). Each card opens a
// focused sheet with only the inputs that activity needs; locked
// cards show the unlock condition instead of being hidden.
// ════════════════════════════════════════════════════════════

const ACTIVITY_DEFS = [
  {
    verb: "ingest",
    name: "Re-ingest",
    icon: "↻",
    summary: "Re-parse the outline → graph + cluster plan. No LLM, deterministic.",
    fastHint: "Same as Thorough — ingest never calls Claude.",
    thoroughHint: "Same as Fast — ingest never calls Claude.",
  },
  {
    verb: "scaffold",
    name: "Scaffold",
    icon: "▤",
    summary: "Outline → graph → cluster plan.",
    fastHint: "Skip relationship inference and reference extraction.",
    thoroughHint: "Infer claim relationships and pull references from raw text.",
  },
  {
    verb: "draft",
    name: "Draft",
    icon: "✎",
    summary: "Render prose from the cluster plan.",
    fastHint: "No autocorrect — render straight from the graph.",
    thoroughHint: "Run autocorrect=safe to fix mechanical voice nits inline.",
  },
  {
    verb: "find_gaps",
    name: "Find gaps",
    icon: "⚠",
    summary: "Per-section: what canonical literature is the paper missing?",
    fastHint: "Claude only — fastest, but suggestions aren't verified.",
    thoroughHint: "Claude + OpenAlex verification of every suggested work.",
  },
  {
    verb: "refine",
    name: "Refine",
    icon: "✓",
    summary: "Audit the draft, then iterate until it converges.",
    fastHint: "Audit only — produce flags for manual review.",
    thoroughHint: "Convergence loop (autocorrect=aggressive) + voice review.",
  },
  {
    verb: "restructure",
    name: "Restructure",
    icon: "⇅",
    summary: "Audit the document order against academic-writing rules.",
    fastHint: "Section-level only — fastest pass.",
    thoroughHint: "Section + per-section cluster ordering.",
  },
  {
    verb: "review",
    name: "Review",
    icon: "§",
    summary: "Supervisor-style critique with marked track changes.",
    fastHint: "Per-cluster revisions only.",
    thoroughHint: "Per-cluster + per-section + overall critique.",
  },
];


// Shared sub-nav scaffolder. Mounts a tab strip + body inside `panel`
// and dispatches to one of the supplied subview renderers based on
// `initialSubTab`. Every primary tab uses this for visual consistency
// with the Output pattern.
function buildTabSubnav(panel, tabs, initialSubTab, renderers) {
  panel.innerHTML = "";
  const validIds = new Set(tabs.map(t => t.id));
  const startTab = validIds.has(initialSubTab) ? initialSubTab : tabs[0].id;

  const subnav = document.createElement("nav");
  subnav.className = "review-subnav";
  subnav.innerHTML = tabs.map(t =>
    `<button class="review-tab" data-r-tab="${escapeAttr(t.id)}">${escapeHtml(t.label)}</button>`
  ).join("");
  panel.appendChild(subnav);

  const body = document.createElement("div");
  body.className = "review-body";
  panel.appendChild(body);

  function showSubview(name) {
    panel.querySelectorAll(".review-tab").forEach(t =>
      t.classList.toggle("active", t.dataset.rTab === name));
    body.innerHTML = `<div class="muted small" style="padding: 12px;">Loading…</div>`;
    const fn = renderers[name];
    if (typeof fn === "function") fn(body);
  }
  panel.querySelectorAll(".review-tab").forEach(t => {
    t.addEventListener("click", () => showSubview(t.dataset.rTab));
  });
  showSubview(startTab);
  return {body, showSubview};
}


// ─── Dashboard tab — at-a-glance project status + change log ──

function renderDashboardTab(main, initialSubTab) {
  const panel = main.querySelector('[data-bind="dashboard-panel"]');
  buildTabSubnav(panel, [
    {id: "summary",    label: "Summary"},
    {id: "changelogs", label: "Change log"},
  ], initialSubTab, {
    summary(body) {
      // Inject the bind target the legacy renderDashboard expects.
      body.innerHTML = "";
      const host = document.createElement("div");
      host.dataset.bind = "dashboard";
      body.appendChild(host);
      renderDashboard(main);
    },
    changelogs(body) {
      renderReviewChangelogSubview(body);
    },
  });
}


async function renderActivitiesTab(main, initialSubTab) {
  const panel = main.querySelector('[data-bind="activities-panel"]');
  buildTabSubnav(panel, [
    {id: "start",   label: "Start"},
    {id: "running", label: "Running"},
    {id: "history", label: "History"},
  ], initialSubTab, {
    start(body)  { renderActivitiesStart(body); },
    running(body) { renderActivitiesRunning(body); },
    history(body) { renderActivitiesHistory(body); },
  });
}


async function renderActivitiesStart(body) {
  body.innerHTML = "";
  body.appendChild(cloneTemplate("tpl-activity-launcher"));

  const wrapper = body.querySelector(".activities-wrapper");
  const banner = wrapper.querySelector('[data-bind="state-banner"]');
  const cardsHost = wrapper.querySelector('[data-bind="cards"]');
  const historyHost = wrapper.querySelector('[data-bind="history"]');
  // Hide the inline history block — there's a dedicated History sub-tab now.
  if (historyHost) historyHost.classList.add("hidden");

  banner.innerHTML = `<div class="muted small">Loading project state…</div>`;
  let projectStateData;
  try {
    projectStateData = await fetchJSON(
      `/api/projects/${encodeURIComponent(state.current)}/state`
    );
  } catch (err) {
    banner.innerHTML = `<div class="empty-state">Failed to load state: ${escapeHtml(err.message)}</div>`;
    return;
  }
  state.projectState = projectStateData;

  const stateLabel = ({
    S0: "Empty — add an outline to get started",
    S1: "Outline added — ready to scaffold",
    S2: "Scaffolded — ready to draft",
    S3: "Drafted — ready to refine or find gaps",
    S4: "Reviewed — flags ready for action",
  })[projectStateData.state] || projectStateData.state;
  banner.innerHTML = `
    <div class="state-pill"><span class="dot"></span><strong>${escapeHtml(projectStateData.state)}</strong> · ${escapeHtml(stateLabel)}</div>
  `;

  // Activity cards.
  cardsHost.innerHTML = "";
  ACTIVITY_DEFS.forEach(def => {
    const blocker = projectStateData.blockers[def.verb];
    // The template's root <button> IS the card — cloneTemplate returns
    // it directly, so we query its descendants for the inner binds.
    const cardBtn = cloneTemplate("tpl-activity-card");
    cardBtn.querySelector('[data-bind="icon"]').textContent = def.icon;
    cardBtn.querySelector('[data-bind="name"]').textContent = def.name;
    cardBtn.querySelector('[data-bind="summary"]').textContent = def.summary;

    const statePill = cardBtn.querySelector('[data-bind="state-pill"]');
    const meta = cardBtn.querySelector('[data-bind="meta"]');

    // Compute "last run" for this verb from history.
    const last = (projectStateData.history || [])
      .slice().reverse().find(h => h.verb === def.verb);
    if (blocker) {
      cardBtn.disabled = true;
      cardBtn.classList.add("locked");
      statePill.textContent = "Locked";
      statePill.className = "activity-state locked";
      meta.textContent = blocker;
    } else {
      statePill.textContent = "Ready";
      statePill.className = "activity-state ready";
      meta.textContent = last
        ? `Last run · ${formatTimestamp(new Date(last.finished_at).getTime() / 1000)} (${last.mode})`
        : "Never run";
      cardBtn.addEventListener("click", () => openActivitySheet(def, projectStateData));
    }
    cardsHost.appendChild(cardBtn);
  });
}


// "Running" sub-tab: shows the live progress card if a run is in
// flight, otherwise an idle prompt. ``state.lastActivityRun`` is set
// by attachActivityProgress when a new activity starts.
function renderActivitiesRunning(body) {
  body.innerHTML = "";
  const runInfo = state.lastActivityRun;
  if (!runInfo) {
    body.innerHTML = `
      <div class="empty-state">
        <h3>No active run</h3>
        <p>Start an activity from the Start tab — its live progress will appear here.</p>
      </div>`;
    return;
  }
  // Re-attach the progress card. If the run already finished, the
  // existing summary block stays visible inside the card.
  const slot = document.createElement("div");
  slot.dataset.bind = "progress";
  body.appendChild(slot);
  slot.appendChild(cloneTemplate("tpl-activity-progress"));
  slot.querySelector('[data-bind="title"]').textContent =
    `${runInfo.verbName} (${runInfo.mode}) · live progress`;
  // If the WebSocket is still open, hand the new container off to it.
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    startTimeline({run_id: runInfo.run_id}, slot);
    state.activityProgressPanel = slot;
  } else {
    // Run already concluded — render a static summary the user can re-read.
    const status = slot.querySelector('[data-bind="status"]');
    status.classList.remove("hidden");
    slot.querySelector('[data-bind="run-id"]').textContent = runInfo.run_id;
    slot.querySelector('[data-bind="state"]').textContent = "ended";
    slot.querySelector('[data-bind="state"]').className = "pill";
  }
}


async function renderActivitiesHistory(body) {
  body.innerHTML = `<div class="muted small" style="padding: 12px;">Loading history…</div>`;
  let stateData;
  try {
    stateData = await fetchJSON(
      `/api/projects/${encodeURIComponent(state.current)}/state`
    );
  } catch (err) {
    body.innerHTML = `<div class="empty-state">Failed to load: ${escapeHtml(err.message)}</div>`;
    return;
  }
  const history = (stateData.history || []).slice().reverse();
  if (!history.length) {
    body.innerHTML = `
      <div class="empty-state">
        <h3>No activities run yet</h3>
        <p>Start one from the <strong>Start</strong> tab.</p>
      </div>`;
    return;
  }
  body.innerHTML = `
    <ul class="history-list">
      ${history.map(h => `
        <li>
          <span class="history-verb">${escapeHtml(h.verb)}</span>
          <span class="muted small">${escapeHtml(h.mode)}</span>
          <span class="muted small">${formatTimestamp(new Date(h.finished_at).getTime() / 1000)}</span>
          <span class="muted small">${formatDuration(h.elapsed_seconds || 0)}</span>
          <span class="pill ${h.finalise_succeeded ? "ok" : "bad"}">
            ${h.finalise_succeeded ? "ok" : "blocked"}
          </span>
        </li>
      `).join("")}
    </ul>`;
}


function openActivitySheet(def, projectStateData) {
  const sheet = cloneTemplate("tpl-activity-sheet");
  document.body.appendChild(sheet);
  sheet.querySelector('[data-bind="title"]').textContent = def.name;
  sheet.querySelector('[data-bind="lede"]').textContent = def.summary;
  sheet.querySelector('[data-bind="submit"]').textContent = `Start ${def.name}`;

  // Per-activity fields.
  const fieldsHost = sheet.querySelector('[data-bind="fields"]');
  const voices = (state.detail.voices && state.detail.voices.length)
    ? state.detail.voices : ["academic"];
  const voiceField = `
    <label class="field">
      <span class="field-label">Voice</span>
      <select name="voice">
        ${voices.map(v => `<option value="${escapeAttr(v)}">${escapeHtml(v)}</option>`).join("")}
      </select>
    </label>`;
  fieldsHost.insertAdjacentHTML("beforeend", voiceField);

  // find_gaps no longer takes per-run inputs — it reads the scaffold
  // and (in thorough mode) verifies via OpenAlex. The mode toggle
  // covers the only meaningful choice.
  if (def.verb === "draft") {
    fieldsHost.insertAdjacentHTML("beforeend", `
      <label class="field-check">
        <input type="checkbox" name="force" />
        <span>Force re-render every cluster (ignore cache)</span>
      </label>`);
  }
  if (def.verb === "scaffold") {
    fieldsHost.insertAdjacentHTML("beforeend", `
      <label class="field">
        <span class="field-label">Section depth</span>
        <select name="nesting_depth">
          <option value="1">1 — flat (top-level sections only)</option>
          <option value="2" selected>2 — sections + subsections (## A.1)</option>
          <option value="3">3 — also sub-subsections (### A.1.1)</option>
        </select>
        <span class="muted small">Caps how deep the auto-outliner can nest. Only applied when the outline is raw prose and Claude has to extract structure.</span>
      </label>`);
  }
  if (def.verb === "refine") {
    fieldsHost.insertAdjacentHTML("beforeend", `
      <label class="field">
        <span class="field-label">Max convergence passes</span>
        <input type="number" name="max_passes" value="3" min="1" max="6" />
        <span class="muted small">Only used in Thorough mode.</span>
      </label>`);
  }
  // (Both modes are valid for find_gaps: fast = Claude only,
  // thorough = Claude + OpenAlex verification.)

  // Mode hint: show the right hint as the user toggles.
  const hintEl = sheet.querySelector('[data-bind="mode-hint"]');
  function setHint() {
    const mode = sheet.querySelector('input[name="mode"]:checked').value;
    hintEl.textContent = mode === "fast" ? def.fastHint : def.thoroughHint;
  }
  setHint();
  sheet.querySelectorAll('input[name="mode"]').forEach(r =>
    r.addEventListener("change", setHint));

  // Wire close + submit.
  sheet.querySelectorAll('[data-action="close"]').forEach(btn =>
    btn.addEventListener("click", () => sheet.remove()));

  const form = sheet.querySelector('[data-bind="form"]');
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    const fd = new FormData(form);
    const body = {
      voice: fd.get("voice") || "academic",
      mode: fd.get("mode") || "thorough",
    };
    if (def.verb === "draft") {
      body.force = fd.get("force") === "on";
    }
    if (def.verb === "refine") {
      body.max_passes = Number(fd.get("max_passes") || 3);
    }
    if (def.verb === "scaffold") {
      body.nesting_depth = Number(fd.get("nesting_depth") || 2);
    }
    const errEl = sheet.querySelector('[data-bind="error"]');
    errEl.textContent = "";
    try {
      const resp = await fetch(
        `/api/projects/${encodeURIComponent(state.current)}/activities/${def.verb}`,
        {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(body),
        }
      );
      if (!resp.ok) {
        errEl.textContent = await resp.text();
        return;
      }
      const data = await resp.json();
      sheet.remove();
      attachActivityProgress(def, data);
    } catch (err) {
      errEl.textContent = `Network error: ${err.message}`;
    }
  });
}


function attachActivityProgress(def, runData) {
  // Cache the run so the Running sub-tab can reattach if the user
  // navigates away and back.
  state.lastActivityRun = {
    run_id: runData.run_id,
    verb: def.verb,
    verbName: def.name,
    mode: runData.mode,
  };
  // Navigate to the Running sub-tab; the renderer there will mount
  // the progress card and start the timeline + WebSocket.
  navigate(`/p/${encodeURIComponent(state.current)}/activities/running`);
  // Wait one tick for the route change to render, then mount.
  setTimeout(() => {
    const main = document.getElementById("app");
    const slot = main.querySelector('.review-body [data-bind="progress"]');
    if (!slot) return;
    slot.innerHTML = "";
    slot.appendChild(cloneTemplate("tpl-activity-progress"));
    slot.querySelector('[data-bind="title"]').textContent =
      `${def.name} (${runData.mode}) · live progress`;
    startTimeline(runData, slot);
    openWebSocket(state.current, runData.run_id, slot);
    state.activityProgressPanel = slot;
  }, 0);
}


// ════════════════════════════════════════════════════════════
// Sources tab — outline graph + source files + references all
// stacked as collapsible sections inside one tab. Each section
// hosts a [data-bind] target so the existing legacy renderers
// can populate it without rewriting their internals.
// ════════════════════════════════════════════════════════════

function renderSourcesTab(main, initialSubTab) {
  const panel = main.querySelector('[data-bind="sources-panel"]');
  buildTabSubnav(panel, [
    {id: "outline",    label: "Outline"},
    {id: "heatmap",    label: "Heatmap"},
    {id: "graph",      label: "Graph"},
    {id: "files",      label: "Source files"},
    {id: "references", label: "References"},
  ], initialSubTab, {
    outline(body) {
      body.innerHTML = "";
      const host = document.createElement("div");
      host.dataset.bind = "outline";
      body.appendChild(host);
      renderHierarchy(main);
    },
    heatmap(body) {
      body.innerHTML = "";
      const host = document.createElement("div");
      host.dataset.bind = "heatmap";
      body.appendChild(host);
      renderSectionHeatmap(main);
    },
    graph(body) {
      body.innerHTML = "";
      const host = document.createElement("div");
      host.dataset.bind = "graph";
      body.appendChild(host);
      renderHierarchyGraph(main);
    },
    files(body) {
      body.innerHTML = "";
      const host = document.createElement("div");
      host.dataset.bind = "sources";
      body.appendChild(host);
      renderSources(main);
    },
    references(body) {
      body.innerHTML = "";
      const host = document.createElement("div");
      host.dataset.bind = "references";
      body.appendChild(host);
      renderReferences(main);
    },
  });
}


// ─── section heat-map (Phase 3A/C) ──────────────────────────
//
// One row per section, coloured by trust score (red = read carefully,
// green = healthy). Click a row to scope the rescaffold planner to
// that section. Reads /api/projects/{name}/section-metrics; the
// endpoint computes everything fresh, so this view always reflects
// the current graph + any audit/readiness files that have run.

async function renderSectionHeatmap(main) {
  const projSlug = state.current;
  const host = main.querySelector('[data-bind="heatmap"]');
  host.innerHTML = `<div class="muted">Loading…</div>`;
  try {
    const data = await fetchJSON(
      `/api/projects/${encodeURIComponent(projSlug)}/section-metrics`,
    );
    if (!data.sections || !data.sections.length) {
      host.innerHTML = `<div class="empty-state">
        <h3>No sections yet</h3>
        <p>Run <code>lattice ingest</code> to parse an outline.</p>
      </div>`;
      return;
    }
    host.innerHTML = `
      <div class="heatmap-wrapper">
        <header class="heatmap-header">
          <h3>Section heat-map</h3>
          <p class="muted small">Trust score per section. Lower scores
            (red) need careful reading or revision; high scores (green)
            are well-developed. Click a section to copy a rescaffold
            command scoped to it.</p>
          <div class="heatmap-doc-score">
            <strong>Document trust:</strong> ${data.document_score.toFixed(2)}
            ${data.untrustworthy_sections.length > 0
              ? `<span class="muted">· ${data.untrustworthy_sections.length} section(s) below 0.5</span>`
              : ""}
          </div>
        </header>
        <table class="heatmap-table">
          <thead>
            <tr>
              <th>section</th>
              <th>trust</th>
              <th>metric</th>
              <th>claims</th>
              <th>flags</th>
              <th>blocks</th>
              <th>evidence</th>
              <th>mechanism</th>
              <th>thesis</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            ${data.sections.map(s => `
              <tr class="heatmap-row" data-section-id="${escapeAttr(s.section_id)}">
                <td><code>${escapeHtml(s.section_id)}</code> <span class="muted small">${escapeHtml((s.section_title || "").slice(0, 36))}</span></td>
                <td><span class="heatmap-score" style="background:${heatColour(s.trust_score)}">${s.trust_score.toFixed(2)}</span></td>
                <td><span class="muted small">${s.metric_score.toFixed(2)}</span></td>
                <td>${s.claim_count}</td>
                <td>${s.audit_flag_count > 0 ? `<span class="badge-warning">${s.audit_flag_count}</span>` : "—"}</td>
                <td>${s.readiness_blocks > 0 ? `<span class="badge-error">${s.readiness_blocks}</span>` : "—"}</td>
                <td><span class="muted small">${s.evidence_backing.toFixed(2)}</span></td>
                <td><span class="muted small">${s.mechanism_coverage.toFixed(2)}</span></td>
                <td><span class="muted small">${s.thesis_connection.toFixed(2)}</span></td>
                <td><button class="btn-link" data-action="copy-cmd">scope rescaffold</button></td>
              </tr>
            `).join("")}
          </tbody>
        </table>
        <div class="heatmap-notes">
          ${data.sections.filter(s => s.notes && s.notes.length > 0).map(s => `
            <div class="heatmap-note-block">
              <strong>${escapeHtml(s.section_id)}</strong>
              <ul>
                ${s.notes.map(n => `<li>${escapeHtml(n)}</li>`).join("")}
              </ul>
            </div>
          `).join("")}
        </div>
      </div>`;
    // Wire the per-row "scope rescaffold" buttons.
    host.querySelectorAll('[data-action="copy-cmd"]').forEach(btn => {
      btn.addEventListener("click", (e) => {
        const tr = e.currentTarget.closest("tr");
        const sid = tr.dataset.sectionId;
        const cmd = `lattice rescaffold ${projSlug} --voice academic --section ${sid}`;
        navigator.clipboard?.writeText?.(cmd);
        btn.textContent = "copied";
        setTimeout(() => { btn.textContent = "scope rescaffold"; }, 1500);
      });
    });
  } catch (err) {
    host.innerHTML = `<div class="empty-state">
      <h3>Could not load section metrics</h3>
      <p>${escapeHtml(err.message)}</p>
    </div>`;
  }
}

// Map a 0..1 trust score to a heatmap colour.
//   0.0–0.3 : red       (urgent attention)
//   0.3–0.5 : orange    (read carefully)
//   0.5–0.7 : yellow    (decent; could be better)
//   0.7–1.0 : green     (healthy)
function heatColour(score) {
  if (score < 0.3) return "#fee2e2";
  if (score < 0.5) return "#fed7aa";
  if (score < 0.7) return "#fef3c7";
  return "#d1fae5";
}


// ════════════════════════════════════════════════════════════
// Output tab — paper, audit flags, voice review, source-gap
// report, and the change log. All read-only artefacts produced
// by activities; consolidating them into one tab avoids the
// previous 5-tab clutter.
// ════════════════════════════════════════════════════════════

function renderOutputTab(main, initialSubTab) {
  const panel = main.querySelector('[data-bind="output-panel"]');
  panel.innerHTML = "";

  // Cockpit is the new default surface. The legacy single-purpose
  // sub-views (audit / voice / gap / restructure / review / changelog)
  // remain reachable via deep links and a "Legacy panels" expander
  // for backwards compatibility, but are no longer the primary path.
  // Phase 7 adds the History view (snapshots + revert).
  const validSubTabs = new Set([
    "cockpit", "history", "audit", "voice", "gap",
    "restructure", "review", "changelog",
  ]);
  const startTab = validSubTabs.has(initialSubTab) ? initialSubTab : "cockpit";

  const subnav = document.createElement("nav");
  subnav.className = "review-subnav";
  subnav.innerHTML = `
    <button class="review-tab" data-r-tab="cockpit">Cockpit</button>
    <button class="review-tab" data-r-tab="history">History</button>
    <button class="review-tab" data-r-tab="audit">Audit flags</button>
    <button class="review-tab" data-r-tab="voice">Voice review</button>
    <button class="review-tab" data-r-tab="gap">Lit gaps</button>
    <button class="review-tab" data-r-tab="restructure">Restructure</button>
    <button class="review-tab" data-r-tab="review">Review</button>
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
    if (name === "cockpit")        renderCockpitSubview(body);
    else if (name === "history")   renderHistorySubview(body);
    else if (name === "audit")     renderReviewAuditSubview(body);
    else if (name === "voice")     renderReviewVoiceSubview(body);
    else if (name === "gap")       renderReviewGapSubview(body);
    else if (name === "restructure") renderRestructureSubview(body);
    else if (name === "review")    renderSupervisorReviewSubview(body);
    else if (name === "changelog") renderReviewChangelogSubview(body);
  }
  panel.querySelectorAll(".review-tab").forEach(t => {
    t.addEventListener("click", () => showSubview(t.dataset.rTab));
  });
  showSubview(startTab);
}


// ════════════════════════════════════════════════════════════
// Revision Cockpit — Phase 3 skeleton.
//
// One four-pane working surface that replaces the scattered
// audit/voice/gap/restructure/review tabs:
//
//   ┌──────────────────────┬────────────────────┐
//   │ Paper preview        │ Argument map       │
//   ├──────────────────────┼────────────────────┤
//   │ Issues queue         │ Evidence + claim   │
//   │  (audit + lit gaps + │  inspector + actions│
//   │   restructure +      │                    │
//   │   review revisions)  │                    │
//   └──────────────────────┴────────────────────┘
//
// One selection model drives all four panes: clicking a queue
// item, paragraph, or claim selects a (clusterId, claimId) pair,
// and every pane re-renders in sync.
//
// Action buttons are wired to /api/projects/.../cockpit/actions/...
// which currently returns 501 — the routes exist so Phase 5 can
// plug in real mutations without frontend changes.
// ════════════════════════════════════════════════════════════

function _cockpitDefaultVoice() {
  // Pick the project's first voice when more than one is available.
  // Fall back to "academic" so single-voice projects keep their
  // existing behaviour.
  const voices = state.detail?.voices || [];
  return voices[0] || "academic";
}

function _cockpitVoice() {
  return state.cockpit?.voice || _cockpitDefaultVoice();
}

function renderCockpitSubview(body) {
  const projSlug = encodeURIComponent(state.current);
  const voices = state.detail?.voices || [];
  const initialVoice = _cockpitDefaultVoice();
  const voicePicker = voices.length > 1
    ? `<select data-bind="cockpit-voice"
              style="padding:3px 6px; font-size:11px; border:1px solid var(--border); border-radius:4px; background:var(--surface);">
        ${voices.map(v => `<option value="${escapeAttr(v)}"${v === initialVoice ? " selected" : ""}>${escapeHtml(v)}</option>`).join("")}
      </select>`
    : `<span class="muted small">${escapeHtml(initialVoice)}</span>`;
  body.innerHTML = `
    <div class="cockpit">
      <section class="cockpit-pane paper">
        <div class="cockpit-pane-head">
          <h4>Paper preview</h4>
          <span class="muted small" data-bind="cockpit-paper-meta"></span>
          ${voicePicker}
        </div>
        <div class="cockpit-pane-body" data-bind="cockpit-paper">
          <div class="muted small">Loading paper…</div>
        </div>
      </section>
      <section class="cockpit-pane map">
        <div class="cockpit-pane-head">
          <h4>Argument map</h4>
          <select data-bind="cockpit-map-mode"
                  style="padding:3px 6px; font-size:11px; border:1px solid var(--border); border-radius:4px; background:var(--surface);">
            <option value="default">Default</option>
            <option value="thesis_support_path">Thesis support path</option>
            <option value="section_proof_chain">Section proof chain</option>
            <option value="weak_evidence_zones">Weak evidence zones</option>
            <option value="counterargument_map">Counterargument map</option>
            <option value="unrenderable_clusters">Unrenderable clusters</option>
          </select>
        </div>
        <div class="cockpit-pane-body no-padding">
          <iframe data-bind="cockpit-map"
                  src="/api/projects/${projSlug}/graph-viz?voice=${encodeURIComponent(initialVoice)}&mode=default"
                  loading="lazy"></iframe>
        </div>
      </section>
      <section class="cockpit-pane queue">
        <div class="cockpit-pane-head">
          <h4>Issues &amp; actions</h4>
          <span class="muted small" data-bind="cockpit-queue-counts"></span>
        </div>
        <div class="cockpit-queue-toolbar" data-bind="cockpit-queue-filters"></div>
        <div class="cockpit-pane-body no-padding" data-bind="cockpit-queue">
          <div class="muted small" style="padding:14px;">Loading queue…</div>
        </div>
      </section>
      <section class="cockpit-pane evidence">
        <div class="cockpit-pane-head">
          <h4>Claim &amp; evidence</h4>
          <span class="muted small" data-bind="cockpit-claim-meta"></span>
        </div>
        <div class="cockpit-pane-body" data-bind="cockpit-claim">
          <div class="cockpit-pane-body empty" style="padding:0;">
            Select a queue item or click a paragraph to inspect a claim.
          </div>
        </div>
      </section>
    </div>`;

  // Bootstrap the cockpit's selection-model state. Stored on the
  // outer `state` so paragraph clicks and queue clicks can find
  // each other across panes.
  state.cockpit = {
    queue: [],
    sources: {},
    selectedItemId: null,
    selectedClaimId: null,
    selectedClusterId: null,
    activeFilter: "all",
    mapMode: "default",
    voice: initialVoice,
    paperMarkdown: "",
    clusterByParagraph: {},
  };

  // Wire the voice picker (only present when there's more than one
  // voice on disk; fall back is a static label).
  const voiceSel = body.querySelector('[data-bind="cockpit-voice"]');
  if (voiceSel) {
    voiceSel.addEventListener("change", () => {
      state.cockpit.voice = voiceSel.value;
      // Re-load every voice-keyed surface in the cockpit.
      loadCockpitPaper(body);
      loadCockpitQueue(body);
      const iframe = body.querySelector('[data-bind="cockpit-map"]');
      if (iframe) {
        const mode = state.cockpit.mapMode || "default";
        iframe.src = `/api/projects/${projSlug}/graph-viz?voice=${encodeURIComponent(state.cockpit.voice)}&mode=${encodeURIComponent(mode)}`;
      }
      // Clear the evidence pane — selection IDs are voice-agnostic
      // but the rendered paragraph and audit flags are not.
      const claimTarget = body.querySelector('[data-bind="cockpit-claim"]');
      if (claimTarget) {
        claimTarget.innerHTML = `<div class="cockpit-pane-body empty" style="padding:0;">Select a queue item or click a paragraph to inspect a claim.</div>`;
      }
    });
  }

  wireCockpitMapBridge(body);
  loadCockpitPaper(body);
  loadCockpitQueue(body);
}

// Phase 6 — bidirectional selection sync between the cockpit and
// the graph-viz iframe. The iframe accepts:
//   {type: 'lattice:set-mode', mode}        — switch the map overlay
//   {type: 'lattice:select-claim', claim_id} — highlight a claim
// The iframe emits:
//   {type: 'lattice:node-tapped', node_id, kind, section_id, cluster_id}
function wireCockpitMapBridge(body) {
  const iframe = body.querySelector('[data-bind="cockpit-map"]');
  const modePicker = body.querySelector('[data-bind="cockpit-map-mode"]');
  if (!iframe || !modePicker) return;

  modePicker.addEventListener("change", () => {
    const mode = modePicker.value;
    state.cockpit.mapMode = mode;
    // Reload the iframe with the new mode in the URL so deep-links
    // also work, *and* postMessage so an already-loaded iframe
    // updates without a full reload roundtrip.
    try {
      iframe.contentWindow?.postMessage(
        {type: "lattice:set-mode", mode}, "*");
    } catch (e) { /* cross-origin guard */ }
  });

  // When the iframe taps a claim node, surface it in the queue +
  // evidence panes. Filters the queue down to items targeting the
  // tapped claim's cluster so the user immediately sees what's
  // actionable on it.
  window.addEventListener("message", evt => {
    const msg = evt.data;
    if (!msg || typeof msg !== "object") return;
    if (msg.type !== "lattice:node-tapped") return;
    if (!msg.node_id) return;
    state.cockpit.selectedClaimId = msg.node_id;
    state.cockpit.selectedClusterId = msg.cluster_id || null;
    // Re-render the queue with the new selection highlight (no
    // filter change — the user just wants to see what touches this
    // claim) and load the claim into the evidence pane.
    const queueTarget = body.querySelector('[data-bind="cockpit-queue"]');
    if (queueTarget) renderCockpitQueueList(queueTarget, body);
    loadCockpitClaim(body, msg.node_id);
  });
}

async function loadCockpitPaper(body) {
  const proj = encodeURIComponent(state.current);
  const target = body.querySelector('[data-bind="cockpit-paper"]');
  const meta = body.querySelector('[data-bind="cockpit-paper-meta"]');
  try {
    const text = await fetchText(
      `/api/projects/${proj}/paper?voice=${encodeURIComponent(_cockpitVoice())}`);
    state.cockpit.paperMarkdown = text;
    // Walk the raw markdown to extract paragraph→cluster bindings
    // (marked.js drops the HTML comments before they reach the DOM).
    state.cockpit.clusterByParagraph = _parseClusterBoundaries(text);
    target.innerHTML = `<article class="cockpit-paper">${renderMarkdown(text)}</article>`;
    if (meta) meta.textContent = `${text.split(/\s+/).length.toLocaleString()} words`;
    annotateCockpitParagraphs(target, body);
  } catch (err) {
    state.cockpit.paperMarkdown = "";
    state.cockpit.clusterByParagraph = {};
    target.innerHTML = `<div class="cockpit-pane-body empty" style="padding:0;">No rendered paper yet — run <strong>Draft</strong> first.</div>`;
  }
}

// Parse the joined paper markdown for cluster boundary markers
// emitted by the finaliser:
//   <!-- lattice:cluster c.x.1 s.x -->
// Returns a map keyed by post-marker paragraph index in the rendered
// DOM (0-based) → {cluster_id, section_id}. The mapping is
// approximate but stable: each marker is followed by exactly one
// cluster's prose, which becomes one or more <p> elements after
// markdown rendering.
function _parseClusterBoundaries(markdown) {
  const out = {};
  if (!markdown) return out;
  // Split the markdown on the marker. Track which paragraphs each
  // marker introduces by counting blank-line-separated blocks in
  // the chunk between consecutive markers.
  const re = /<!--\s*lattice:cluster\s+([^\s]+)\s+([^\s]+)\s*-->/g;
  // Walk markers in order, slicing the markdown into "before first
  // marker | block 0 | block 1 | …".
  const markers = [];
  let m;
  while ((m = re.exec(markdown)) !== null) {
    markers.push({
      cluster_id: m[1], section_id: m[2],
      start: m.index, end: m.index + m[0].length,
    });
  }
  if (!markers.length) return out;
  // Count <p>-equivalent paragraphs ahead of the first marker.
  let paragraphCursor = _countMarkdownParagraphs(markdown.slice(0, markers[0].start));
  for (let i = 0; i < markers.length; i++) {
    const startSlice = markers[i].end;
    const endSlice = i + 1 < markers.length ? markers[i + 1].start : markdown.length;
    const chunk = markdown.slice(startSlice, endSlice);
    const nParas = _countMarkdownParagraphs(chunk);
    for (let j = 0; j < nParas; j++) {
      out[paragraphCursor + j] = {
        cluster_id: markers[i].cluster_id,
        section_id: markers[i].section_id,
      };
    }
    paragraphCursor += nParas;
  }
  return out;
}

// Count rendered <p> elements a markdown chunk would produce.
// Approximation good enough for binding: split on blank lines, drop
// chunks that are only headings, list markers, or whitespace.
function _countMarkdownParagraphs(chunk) {
  if (!chunk) return 0;
  let count = 0;
  for (const block of chunk.split(/\n{2,}/)) {
    const stripped = block.trim();
    if (!stripped) continue;
    // Skip ATX headings — they render as <h1>..<h6>, not <p>.
    if (/^#{1,6}\s/.test(stripped)) continue;
    // Skip HTML comments — they don't render at all.
    if (stripped.startsWith("<!--") && stripped.endsWith("-->")) continue;
    // List blocks render as <ul>/<ol>, not as a single <p>; the
    // current cockpit selector picks up <li>s though, so count each
    // bullet as one paragraph for binding purposes.
    if (/^[\-\*\+]\s/m.test(stripped)) {
      const items = stripped.split(/\n(?=[\-\*\+]\s)/).filter(s => s.trim());
      count += Math.max(1, items.length);
      continue;
    }
    count += 1;
  }
  return count;
}

function annotateCockpitParagraphs(target, body) {
  const paras = target.querySelectorAll(".cockpit-paper p, .cockpit-paper li");
  const bindings = state.cockpit.clusterByParagraph || {};
  paras.forEach((p, i) => {
    p.classList.add("cockpit-paragraph");
    p.dataset.idx = String(i);
    const binding = bindings[i];
    if (binding) {
      p.dataset.clusterId = binding.cluster_id;
      p.dataset.sectionId = binding.section_id;
    }
    p.addEventListener("click", () => {
      target.querySelectorAll(".cockpit-paragraph.selected").forEach(el =>
        el.classList.remove("selected"));
      p.classList.add("selected");
      const b = state.cockpit.clusterByParagraph?.[Number(p.dataset.idx)];
      if (!b) return;
      // Drive cockpit selection. When the cluster has exactly one
      // claim, also load the claim into the evidence pane; otherwise
      // surface the cluster-level selection so the user can pick a
      // specific claim from the queue or the map.
      state.cockpit.selectedClusterId = b.cluster_id;
      const claimId = _firstClaimInCluster(b.cluster_id);
      state.cockpit.selectedClaimId = claimId;
      const queueTarget = body.querySelector('[data-bind="cockpit-queue"]');
      if (queueTarget) renderCockpitQueueList(queueTarget, body);
      if (claimId) {
        loadCockpitClaim(body, claimId);
        // Forward the selection to the graph iframe.
        const iframe = body.querySelector('[data-bind="cockpit-map"]');
        if (iframe) {
          try {
            iframe.contentWindow?.postMessage({
              type: "lattice:select-claim", claim_id: claimId,
            }, "*");
          } catch (e) { /* same-origin guard */ }
        }
      }
    });
  });
}

// Best-effort claim lookup for a cluster: scan the queue's
// affects_claim_ids since that's the data already loaded in the
// cockpit. For richer paragraph→claim resolution, Phase 4 traces are
// the canonical source — wiring those in is a follow-up.
function _firstClaimInCluster(clusterId) {
  for (const item of state.cockpit.queue || []) {
    if (item.target_cluster_id === clusterId && item.target_claim_id) {
      return item.target_claim_id;
    }
    const affects = item.affects_claim_ids || [];
    if (item.target_cluster_id === clusterId && affects.length) {
      return affects[0];
    }
  }
  return null;
}

async function loadCockpitQueue(body) {
  const proj = encodeURIComponent(state.current);
  const target = body.querySelector('[data-bind="cockpit-queue"]');
  const filterBar = body.querySelector('[data-bind="cockpit-queue-filters"]');
  const countsEl = body.querySelector('[data-bind="cockpit-queue-counts"]');
  try {
    const data = await fetchJSON(
      `/api/projects/${proj}/cockpit-queue?voice=${encodeURIComponent(_cockpitVoice())}`);
    state.cockpit.queue = data.items || [];
    state.cockpit.sources = data.sources || {};
    state.cockpit.counts = data.counts || {};
    if (countsEl) {
      const n = data.counts?.total || 0;
      countsEl.textContent = n
        ? `${n} item${n === 1 ? "" : "s"}`
        : "all clear";
    }
    renderCockpitQueueFilters(filterBar, body);
    renderCockpitQueueList(target, body);
  } catch (err) {
    target.innerHTML = `<div class="cockpit-pane-body empty" style="padding:24px;">Failed to load queue: ${escapeHtml(err.message)}</div>`;
  }
}

function renderCockpitQueueFilters(filterBar, body) {
  if (!filterBar) return;
  const counts = state.cockpit.counts?.by_kind || {};
  const filters = [
    {key: "all",            label: "All",         count: state.cockpit.queue.length},
    {key: "audit_flag",     label: "Audit",       count: counts.audit_flag || 0},
    {key: "lit_gap",        label: "Lit gaps",    count: counts.lit_gap || 0},
    {key: "restructure",    label: "Restructure", count: counts.restructure || 0},
    {key: "review_proposal",label: "Review",      count: counts.review_proposal || 0},
  ];
  filterBar.innerHTML = filters
    .map(f => `<button data-filter="${escapeAttr(f.key)}"
                       class="${state.cockpit.activeFilter === f.key ? "active" : ""}"
                       ${f.count === 0 && f.key !== "all" ? "disabled" : ""}>
                 ${escapeHtml(f.label)} <span class="muted">${f.count}</span>
               </button>`)
    .join("");
  filterBar.querySelectorAll("button[data-filter]").forEach(btn => {
    btn.addEventListener("click", () => {
      state.cockpit.activeFilter = btn.dataset.filter;
      renderCockpitQueueFilters(filterBar, body);
      const target = body.querySelector('[data-bind="cockpit-queue"]');
      renderCockpitQueueList(target, body);
    });
  });
}

function renderCockpitQueueList(target, body) {
  if (!target) return;
  const items = state.cockpit.queue.filter(it =>
    state.cockpit.activeFilter === "all" || it.kind === state.cockpit.activeFilter);
  if (!items.length) {
    const sources = state.cockpit.sources || {};
    const anyMissing = Object.values(sources).every(v => v === "missing");
    target.innerHTML = `<div class="cockpit-pane-body empty" style="padding:24px;">
      ${anyMissing
        ? "Nothing run yet. Run <strong>Refine</strong>, <strong>Find gaps</strong>, <strong>Restructure</strong>, or <strong>Review</strong> from the Activities tab to populate this queue."
        : "Nothing in this filter."}
    </div>`;
    return;
  }
  target.innerHTML = `<ul class="cockpit-queue-list">
    ${items.map(it => `
      <li class="cockpit-queue-item severity-${escapeAttr(it.severity)} ${state.cockpit.selectedItemId === it.id ? "selected" : ""}"
          data-item-id="${escapeAttr(it.id)}"
          data-claim-id="${escapeAttr(it.target_claim_id || "")}"
          data-cluster-id="${escapeAttr(it.target_cluster_id || "")}"
          data-section-id="${escapeAttr(it.target_section_id || "")}">
        <span class="severity-stripe"></span>
        <div>
          <div class="item-title">${escapeHtml(it.title || it.kind)}</div>
          <div class="item-body">${escapeHtml(it.body || it.suggestion || "")}</div>
          <div class="item-meta">
            <span class="item-kind">${escapeHtml(it.kind)}</span>
            ${it.target_cluster_id ? `<span class="item-kind">${escapeHtml(it.target_cluster_id)}</span>` : ""}
          </div>
        </div>
        <span class="muted small">→</span>
      </li>`).join("")}
  </ul>`;
  target.querySelectorAll(".cockpit-queue-item").forEach(el => {
    el.addEventListener("click", () => {
      state.cockpit.selectedItemId = el.dataset.itemId;
      state.cockpit.selectedClaimId = el.dataset.claimId || null;
      state.cockpit.selectedClusterId = el.dataset.clusterId || null;
      // Re-render the queue to update selected highlight, then load
      // the claim detail (if a claim is bound) into the evidence pane.
      renderCockpitQueueList(target, body);
      if (state.cockpit.selectedClaimId) {
        loadCockpitClaim(body, state.cockpit.selectedClaimId);
      } else {
        // Items targeted at a cluster but no specific claim: surface
        // the queue item itself in the evidence pane so the user can
        // still act on it.
        renderCockpitItemDetail(body, state.cockpit.queue.find(
          i => i.id === state.cockpit.selectedItemId));
      }
      // Phase 6 — push the selection over to the graph iframe so
      // the corresponding node gets the selected-from-parent ring.
      const iframe = body.querySelector('[data-bind="cockpit-map"]');
      if (iframe && state.cockpit.selectedClaimId) {
        try {
          iframe.contentWindow?.postMessage({
            type: "lattice:select-claim",
            claim_id: state.cockpit.selectedClaimId,
          }, "*");
        } catch (e) { /* same-origin guard */ }
      }
    });
  });
}

async function loadCockpitClaim(body, claimId) {
  const proj = encodeURIComponent(state.current);
  const target = body.querySelector('[data-bind="cockpit-claim"]');
  const meta = body.querySelector('[data-bind="cockpit-claim-meta"]');
  target.innerHTML = `<div class="muted small">Loading claim…</div>`;
  try {
    const data = await fetchJSON(
      `/api/projects/${proj}/cockpit-claim/${encodeURIComponent(claimId)}?voice=${encodeURIComponent(_cockpitVoice())}`);
    if (meta) {
      meta.textContent = data.section
        ? `${data.section.title || data.section.section_id}`
        : "";
    }
    renderCockpitClaimDetail(target, data);
  } catch (err) {
    target.innerHTML = `<div class="cockpit-pane-body empty" style="padding:0;">Could not load claim: ${escapeHtml(err.message)}</div>`;
  }
}

function renderCockpitClaimDetail(target, data) {
  const claim = data.claim || {};
  const section = data.section || {};
  const cluster = data.cluster || {};
  const flags = data.audit_flags || [];
  const evidence = claim.evidence || [];
  const actions = data.available_actions || [];
  target.innerHTML = `
    <div class="cockpit-claim-block">
      <p class="cockpit-claim-statement">${escapeHtml(claim.statement || "")}</p>
      <div class="cockpit-claim-meta">
        <span class="pill">${escapeHtml(claim.type || "")}</span>
        ${claim.author_origin ? `<span class="pill ok">author</span>` : ""}
        <span class="pill">${escapeHtml(section.title || section.section_id || "—")}</span>
        ${cluster.cluster_id ? `<span class="pill">cluster ${escapeHtml(cluster.cluster_id)}</span>` : ""}
        <span class="pill">importance ${(claim.importance || 0).toFixed(2)}</span>
      </div>
      ${claim.mechanism ? `<p class="muted small"><strong>mechanism:</strong> ${escapeHtml(claim.mechanism)}</p>` : ""}
      ${claim.scope_conditions?.length ? `<p class="muted small"><strong>scope:</strong> ${claim.scope_conditions.map(escapeHtml).join("; ")}</p>` : ""}
    </div>
    ${data.rendered_paragraph
      ? `<div class="cockpit-claim-block">
          <h4 class="muted small" style="text-transform:uppercase; margin:0 0 4px; letter-spacing:0.04em;">Rendered paragraph</h4>
          <div class="cockpit-claim-paragraph">${escapeHtml(data.rendered_paragraph.trim())}</div>
        </div>`
      : `<div class="cockpit-claim-block muted small">No rendered paragraph — cluster not yet drafted.</div>`}
    <div class="cockpit-claim-block">
      <h4 class="muted small" style="text-transform:uppercase; margin:0 0 6px; letter-spacing:0.04em;">Evidence (${evidence.length})</h4>
      ${evidence.length
        ? evidence.map(ev => `
            <div class="cockpit-evidence-row">
              <span><strong>${escapeHtml(ev.source || "?")}</strong>
                ${ev.passage ? `<span class="muted small">· ${escapeHtml(ev.passage)}</span>` : ""}
              </span>
              <span class="pill">${escapeHtml(ev.binding_strength || "?")}</span>
            </div>`).join("")
        : `<div class="muted small">No bound evidence on this claim.</div>`}
    </div>
    ${flags.length ? `
      <div class="cockpit-claim-block">
        <h4 class="muted small" style="text-transform:uppercase; margin:0 0 6px; letter-spacing:0.04em;">Audit flags on this cluster (${flags.length})</h4>
        ${flags.slice(0, 6).map(f => `
          <div class="cockpit-evidence-row">
            <span><strong>${escapeHtml(f.rule_id || "")}</strong>
              <span class="muted small">${escapeHtml((f.offending_text || "").slice(0, 80))}</span>
            </span>
            <span class="pill ${f.severity === "critical" ? "bad" : f.severity === "minor" ? "" : "warn"}">${escapeHtml(f.severity || "")}</span>
          </div>`).join("")}
      </div>` : ""}
    <div class="cockpit-action-row" data-bind="cockpit-actions">
      ${actions.map(a => `
        <button data-action="${escapeAttr(a)}"
                data-claim-id="${escapeAttr(claim.claim_id || "")}"
                data-cluster-id="${escapeAttr(cluster.cluster_id || "")}"
                ${a === "redraft-cluster" ? "class=\"primary\"" : ""}>
          ${escapeHtml(actionLabel(a))}
        </button>`).join("")}
    </div>`;
  wireCockpitActions(target);
}

function renderCockpitItemDetail(body, item) {
  const target = body.querySelector('[data-bind="cockpit-claim"]');
  if (!target || !item) return;
  target.innerHTML = `
    <div class="cockpit-claim-block">
      <p class="cockpit-claim-statement">${escapeHtml(item.title || "")}</p>
      <div class="cockpit-claim-meta">
        <span class="pill">${escapeHtml(item.kind)}</span>
        <span class="pill ${item.severity === "critical" ? "bad" : item.severity === "info" ? "" : "warn"}">${escapeHtml(item.severity)}</span>
        ${item.target_section_id ? `<span class="pill">section ${escapeHtml(item.target_section_id)}</span>` : ""}
        ${item.target_cluster_id ? `<span class="pill">cluster ${escapeHtml(item.target_cluster_id)}</span>` : ""}
      </div>
      ${item.body ? `<p>${escapeHtml(item.body)}</p>` : ""}
      ${item.suggestion ? `
        <div class="cockpit-claim-paragraph">${escapeHtml(item.suggestion)}</div>` : ""}
    </div>
    <div class="cockpit-action-row" data-bind="cockpit-actions">
      ${(item.actions || []).map(a => `
        <button data-action="${escapeAttr(a)}"
                data-cluster-id="${escapeAttr(item.target_cluster_id || "")}">
          ${escapeHtml(actionLabel(a))}
        </button>`).join("")}
    </div>`;
  wireCockpitActions(target);
}

function actionLabel(action) {
  return ({
    "add-source": "Add source",
    "edit-claim": "Edit claim",
    "split-claim": "Split claim",
    "merge-claim": "Merge claim",
    "redraft-cluster": "Redraft cluster",
    "mark-intentional": "Mark intentional",
  })[action] || action;
}

function wireCockpitActions(target) {
  target.querySelectorAll("[data-action]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const action = btn.dataset.action;
      const proj = encodeURIComponent(state.current);
      const payload = {
        claim_id: btn.dataset.claimId || null,
        cluster_id: btn.dataset.clusterId || null,
        voice: _cockpitVoice(),
      };
      try {
        const resp = await fetch(
          `/api/projects/${proj}/cockpit/actions/${encodeURIComponent(action)}`,
          {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(payload),
          });
        const data = await resp.json().catch(() => ({}));
        if (resp.status === 501) {
          const detail = data.detail || data;
          showCockpitToast(
            `“${actionLabel(action)}” is a Phase 3 stub — ${detail.next_phase || "wires up in a later phase."}`);
        } else if (!resp.ok) {
          showCockpitToast(`Action failed: ${resp.status} ${resp.statusText}`);
        } else {
          showCockpitToast(`${actionLabel(action)}: ${data.status || "done"}`);
        }
      } catch (err) {
        showCockpitToast(`Network error: ${err.message}`);
      }
    });
  });
}

function showCockpitToast(message) {
  document.querySelectorAll(".cockpit-toast").forEach(t => t.remove());
  const el = document.createElement("div");
  el.className = "cockpit-toast";
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}


// ════════════════════════════════════════════════════════════
// History — Phase 7 snapshot list + revert.
//
// Shows every snapshot taken (one per major activity, plus
// pre-revert snapshots and any manual saves) newest-first. Each
// row offers "Diff vs current" and "Revert". Reverting always
// takes a pre-revert snapshot first so the action is itself
// recoverable.
// ════════════════════════════════════════════════════════════

const SNAPSHOT_KIND_LABELS = {
  manual: "Manual save",
  before_ingest: "Before Ingest",
  before_scaffold: "Before Scaffold",
  before_draft: "Before Draft",
  before_find_gaps: "Before Find gaps",
  before_refine: "Before Refine",
  before_restructure: "Before Restructure",
  before_review: "Before Review",
  before_redraft: "Before Redraft",
  pre_revert: "Pre-revert",
};

async function renderHistorySubview(body) {
  const proj = encodeURIComponent(state.current);
  body.innerHTML = `
    <div class="card">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
        <h3 class="subhead" style="margin:0;">Snapshots</h3>
        <button class="btn sm" data-action="snapshot-now">+ Save snapshot</button>
      </div>
      <div data-bind="snapshots-list" class="muted small">Loading…</div>
    </div>
    <div class="card hidden" data-bind="snapshot-diff" style="margin-top:12px;"></div>`;
  const listEl = body.querySelector('[data-bind="snapshots-list"]');
  const diffEl = body.querySelector('[data-bind="snapshot-diff"]');

  body.querySelector('[data-action="snapshot-now"]').addEventListener("click", async () => {
    const message = window.prompt("Snapshot message (optional):", "");
    if (message === null) return;
    try {
      const resp = await fetch(`/api/projects/${proj}/snapshots`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({message: message || "Manual snapshot"}),
      });
      if (!resp.ok) {
        showCockpitToast(`Snapshot failed: ${resp.status}`);
        return;
      }
      showCockpitToast("Snapshot saved.");
      renderHistorySubview(body); // refresh
    } catch (err) {
      showCockpitToast(`Network error: ${err.message}`);
    }
  });

  let data;
  try {
    data = await fetchJSON(`/api/projects/${proj}/snapshots`);
  } catch (err) {
    listEl.innerHTML = `<p class="muted small">Failed to load snapshots: ${escapeHtml(err.message)}</p>`;
    return;
  }
  if (!data.snapshots.length) {
    listEl.innerHTML = `
      <p class="muted small">No snapshots yet — they're taken automatically before each activity. Run an activity from the Activities tab, or click "+ Save snapshot" above to checkpoint manually.</p>`;
    return;
  }
  listEl.innerHTML = `<ul class="cockpit-queue-list">
    ${data.snapshots.map(s => `
      <li class="cockpit-queue-item severity-info" data-snap-id="${escapeAttr(s.snapshot_id)}">
        <span class="severity-stripe"></span>
        <div>
          <div class="item-title">${escapeHtml(SNAPSHOT_KIND_LABELS[s.kind] || s.kind)}</div>
          <div class="item-body">
            ${escapeHtml(s.message || "(no message)")}
          </div>
          <div class="item-meta">
            <span class="item-kind">${escapeHtml(s.actor)}</span>
            <span class="item-kind">${escapeHtml(formatTimestamp(new Date(s.created_at).getTime() / 1000))}</span>
            <span class="item-kind">${s.cluster_count} clusters · ${s.source_count} sources</span>
          </div>
        </div>
        <div style="display:flex; gap:6px; flex-direction:column;">
          <button class="btn sm" data-action="diff" data-snap-id="${escapeAttr(s.snapshot_id)}">Diff vs current</button>
          <button class="btn sm" data-action="revert" data-snap-id="${escapeAttr(s.snapshot_id)}">Revert</button>
        </div>
      </li>`).join("")}
  </ul>`;

  listEl.querySelectorAll('[data-action="diff"]').forEach(btn => {
    btn.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const snapId = btn.dataset.snapId;
      diffEl.classList.remove("hidden");
      diffEl.innerHTML = `<p class="muted small">Loading diff…</p>`;
      try {
        const resp = await fetchJSON(
          `/api/projects/${proj}/snapshots/${encodeURIComponent(snapId)}/diff`);
        diffEl.innerHTML = renderSnapshotDiff(snapId, resp.diff);
      } catch (err) {
        diffEl.innerHTML = `<p class="muted small">Diff failed: ${escapeHtml(err.message)}</p>`;
      }
    });
  });

  listEl.querySelectorAll('[data-action="revert"]').forEach(btn => {
    btn.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const snapId = btn.dataset.snapId;
      if (!window.confirm(
        `Revert project to snapshot "${snapId}"?\n\n` +
        `A pre-revert snapshot will be taken first so this is recoverable.`)) return;
      try {
        const resp = await fetch(
          `/api/projects/${proj}/snapshots/${encodeURIComponent(snapId)}/revert`,
          {method: "POST"});
        if (!resp.ok) {
          showCockpitToast(`Revert failed: ${resp.status}`);
          return;
        }
        showCockpitToast(`Reverted to ${snapId}. Refreshing…`);
        // Re-render the History view + reload the project so the
        // dashboard / cockpit see the restored state.
        setTimeout(() => location.reload(), 800);
      } catch (err) {
        showCockpitToast(`Network error: ${err.message}`);
      }
    });
  });
}

function renderSnapshotDiff(snapId, diff) {
  const total = diff.total_changes || 0;
  if (!total) {
    return `<p class="muted small">No structural changes between
      <code>${escapeHtml(snapId)}</code> and current state.</p>`;
  }
  const row = (label, items) =>
    items && items.length
      ? `<div class="kv"><span class="k">${escapeHtml(label)}</span>
          <span class="v">${items.length} (${items.slice(0, 6).map(escapeHtml).join(", ")}${items.length > 6 ? "…" : ""})</span></div>`
      : "";
  return `
    <h3 class="subhead">Diff: <code>${escapeHtml(snapId)}</code> → current</h3>
    <p class="muted small">${total} structural change${total === 1 ? "" : "s"}.</p>
    <div class="kv-list">
      ${row("Sections added",        diff.sections_added)}
      ${row("Sections removed",      diff.sections_removed)}
      ${row("Claims added",          diff.claims_added)}
      ${row("Claims removed",        diff.claims_removed)}
      ${diff.claims_modified?.length
        ? `<div class="kv"><span class="k">Claims modified</span><span class="v">${diff.claims_modified.length} (${diff.claims_modified.slice(0, 6).map(c => escapeHtml(c.claim_id)).join(", ")}${diff.claims_modified.length > 6 ? "…" : ""})</span></div>`
        : ""}
      ${row("Relationships added",   diff.relationships_added)}
      ${row("Relationships removed", diff.relationships_removed)}
      ${row("Sources added",         diff.sources_added)}
      ${row("Sources removed",       diff.sources_removed)}
      ${row("Clusters added",        diff.clusters_added)}
      ${row("Clusters removed",      diff.clusters_removed)}
      ${row("Clusters modified",     diff.clusters_modified)}
    </div>`;
}


// ════════════════════════════════════════════════════════════
// Compare — cross-project scaffold comparison. Lives on the
// projects list page; opens a modal to pick two projects, runs
// /api/compare synchronously, then renders a results page in
// place of the projects grid.
// ════════════════════════════════════════════════════════════

function openCompareModal(projects) {
  if (projects.length < 2) {
    alert("Need at least two projects to compare. Create or scaffold another project first.");
    return;
  }
  const modal = cloneTemplate("tpl-compare-modal");
  document.body.appendChild(modal);

  const optsHtml = projects.map(p =>
    `<option value="${escapeAttr(p.name)}">${escapeHtml(p.display_name || p.name)}</option>`
  ).join("");
  const selectA = modal.querySelector('[data-bind="select-a"]');
  const selectB = modal.querySelector('[data-bind="select-b"]');
  selectA.innerHTML = optsHtml;
  selectB.innerHTML = optsHtml;
  if (projects.length >= 2) {
    selectA.value = projects[0].name;
    selectB.value = projects[1].name;
  }

  modal.querySelectorAll('[data-action="close"]').forEach(btn =>
    btn.addEventListener("click", () => modal.remove()));

  const form = modal.querySelector('[data-bind="form"]');
  const errEl = modal.querySelector('[data-bind="error"]');
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    const fd = new FormData(form);
    const body = {
      project_a: fd.get("project_a"),
      project_b: fd.get("project_b"),
      mode: fd.get("mode") || "thorough",
    };
    if (body.project_a === body.project_b) {
      errEl.textContent = "Pick two different projects.";
      return;
    }
    errEl.textContent = "";
    const submitBtn = form.querySelector('button[type="submit"]');
    submitBtn.disabled = true;
    submitBtn.textContent = body.mode === "thorough"
      ? "Comparing… (~30s)"
      : "Comparing…";
    try {
      const resp = await fetch("/api/compare", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        errEl.textContent = await resp.text();
        submitBtn.disabled = false;
        submitBtn.textContent = "Compare →";
        return;
      }
      const report = await resp.json();
      modal.remove();
      renderCompareResults(report);
    } catch (err) {
      errEl.textContent = `Network error: ${err.message}`;
      submitBtn.disabled = false;
      submitBtn.textContent = "Compare →";
    }
  });
}


function renderCompareResults(report) {
  const main = document.getElementById("app");
  main.innerHTML = "";
  main.appendChild(cloneTemplate("tpl-compare-results"));
  setBreadcrumb([{label: "Projects", href: "/"}, {label: "Compare"}]);

  const a = report.project_a;
  const b = report.project_b;
  main.querySelector('[data-bind="title"]').textContent =
    `${a.project_name} ⇄ ${b.project_name}`;
  main.querySelector('[data-bind="subtitle"]').textContent =
    `${report.mode === "thorough" ? "LLM semantic comparison" : "Structural comparison"} · ` +
    `${report.pairs.length} paired claim(s)`;

  main.querySelector('[data-action="back"]')
    .addEventListener("click", () => navigate("/"));

  // Structural summary table.
  const sumEl = main.querySelector('[data-bind="summary"]');
  sumEl.innerHTML = `
    <h3 class="subhead">Structural summary</h3>
    <table class="compare-table">
      <thead>
        <tr><th></th><th>${escapeHtml(a.project_name)}</th><th>${escapeHtml(b.project_name)}</th></tr>
      </thead>
      <tbody>
        <tr><td class="muted">Sections</td><td>${a.section_count}</td><td>${b.section_count}</td></tr>
        <tr><td class="muted">Claims</td><td>${a.claim_count}</td><td>${b.claim_count}</td></tr>
        <tr><td class="muted">Relationships</td><td>${a.relationship_count}</td><td>${b.relationship_count}</td></tr>
        <tr><td class="muted">Thesis</td>
          <td class="compare-thesis-cell">${escapeHtml(a.thesis_statement || "—")}</td>
          <td class="compare-thesis-cell">${escapeHtml(b.thesis_statement || "—")}</td>
        </tr>
      </tbody>
    </table>`;

  // Thesis comparison.
  const thesisEl = main.querySelector('[data-bind="thesis"]');
  if (report.thesis_comparison) {
    const t = report.thesis_comparison;
    thesisEl.innerHTML = `
      <h3 class="subhead">Thesis comparison</h3>
      <span class="pill ${t.agreement === "opposing" ? "bad" : t.agreement === "same" ? "ok" : ""}">${escapeHtml(t.agreement)}</span>
      <p>${escapeHtml(t.summary)}</p>`;
  } else {
    thesisEl.innerHTML = `
      <h3 class="subhead">Thesis comparison</h3>
      <p class="muted small">Not computed (fast mode or one thesis missing).</p>`;
  }

  // Pairs.
  const pairsEl = main.querySelector('[data-bind="pairs"]');
  if (!report.pairs.length) {
    pairsEl.innerHTML = `
      <h3 class="subhead">Paired claims</h3>
      <p class="muted small">No paired claims found. Either the papers cover unrelated material, or fast mode skipped the LLM pairing.</p>`;
  } else {
    pairsEl.innerHTML = `
      <h3 class="subhead">Paired claims (${report.pairs.length})</h3>
      <div class="pair-list">
        ${report.pairs.map(p => `
          <div class="pair">
            <div class="pair-head">
              <span class="pill rel-${escapeAttr(p.relationship)}">${escapeHtml(p.relationship)}</span>
              <span class="muted small">${escapeHtml(p.confidence)} confidence</span>
            </div>
            <div class="pair-body">
              <div class="pair-side"><span class="muted small">${escapeHtml(a.project_name)}</span><p>${escapeHtml(p.claim_a_text)}</p></div>
              <div class="pair-arrow">⇄</div>
              <div class="pair-side"><span class="muted small">${escapeHtml(b.project_name)}</span><p>${escapeHtml(p.claim_b_text)}</p></div>
            </div>
            ${p.rationale ? `<p class="pair-rationale muted small">${escapeHtml(p.rationale)}</p>` : ""}
          </div>
        `).join("")}
      </div>`;
  }

  // Unique-to-each lists.
  const uaEl = main.querySelector('[data-bind="unique-a"]');
  const ubEl = main.querySelector('[data-bind="unique-b"]');
  uaEl.innerHTML = `
    <h3 class="subhead">Only in ${escapeHtml(a.project_name)} (${report.unique_a.length})</h3>
    ${report.unique_a.length ? `<ul class="unique-list">${
      report.unique_a.slice(0, 30).map(c =>
        `<li>${escapeHtml(c.text)}</li>`
      ).join("")
    }</ul>${report.unique_a.length > 30 ? `<p class="muted small">+${report.unique_a.length - 30} more.</p>` : ""}` :
      `<p class="muted small">All claims paired.</p>`}`;
  ubEl.innerHTML = `
    <h3 class="subhead">Only in ${escapeHtml(b.project_name)} (${report.unique_b.length})</h3>
    ${report.unique_b.length ? `<ul class="unique-list">${
      report.unique_b.slice(0, 30).map(c =>
        `<li>${escapeHtml(c.text)}</li>`
      ).join("")
    }</ul>${report.unique_b.length > 30 ? `<p class="muted small">+${report.unique_b.length - 30} more.</p>` : ""}` :
      `<p class="muted small">All claims paired.</p>`}`;
}


// ════════════════════════════════════════════════════════════
// Full Review — split button in the project header that runs
// every selected activity in sequence (scaffold → draft →
// find_gaps → refine, only the ticked ones). The caret popover
// lets the user pick which activities and which mode; the
// selection persists in localStorage so the next project loads
// with the same defaults.
// ════════════════════════════════════════════════════════════

const FULL_REVIEW_ORDER = [
  "scaffold", "restructure", "draft", "find_gaps", "refine", "review",
];

const FULL_REVIEW_LABELS = {
  scaffold:    "Scaffold",
  restructure: "Restructure",
  draft:       "Draft",
  find_gaps:   "Find gaps",
  refine:      "Refine",
  review:      "Review",
};

function getFullReviewConfig() {
  try {
    const raw = JSON.parse(localStorage.getItem("lattice.fullReview") || "null");
    if (raw && typeof raw === "object") {
      return {
        verbs: {
          scaffold:    raw.verbs?.scaffold    ?? true,
          restructure: raw.verbs?.restructure ?? false,
          draft:       raw.verbs?.draft       ?? true,
          find_gaps:   raw.verbs?.find_gaps   ?? true,
          refine:      raw.verbs?.refine      ?? true,
          review:      raw.verbs?.review      ?? false,
        },
        mode: raw.mode === "fast" ? "fast" : "thorough",
      };
    }
  } catch (e) { /* fall through */ }
  return {
    verbs: {
      scaffold: true, restructure: false, draft: true,
      find_gaps: true, refine: true, review: false,
    },
    mode: "thorough",
  };
}

function saveFullReviewConfig(cfg) {
  localStorage.setItem("lattice.fullReview", JSON.stringify(cfg));
}

function buildFullReviewControlsHtml() {
  const cfg = getFullReviewConfig();
  const verbRows = FULL_REVIEW_ORDER.map(v => `
    <label class="popover-check">
      <input type="checkbox" name="verb" value="${v}" ${cfg.verbs[v] ? "checked" : ""}/>
      <span>${escapeHtml(FULL_REVIEW_LABELS[v])}</span>
    </label>
  `).join("");
  return `
    <div class="full-review-group" data-bind="full-review-group">
      <button class="btn primary" data-action="run-full-review">Full Review →</button>
      <button class="btn full-review-caret" data-action="toggle-full-review-popover" aria-label="Pick activities">▾</button>
      <div class="full-review-popover hidden" data-bind="full-review-popover">
        <button type="button" class="popover-quick" data-action="quick-reingest"
          title="Re-parse the outline (deterministic, no LLM)">
          ↻&nbsp;&nbsp;Re-ingest now
          <span class="muted small">no LLM, ~1s</span>
        </button>
        <p class="popover-head">Activities to run</p>
        <div class="popover-checks">${verbRows}</div>
        <div class="popover-modes">
          <span class="muted small">Mode</span>
          <label class="popover-mode">
            <input type="radio" name="mode" value="fast" ${cfg.mode === "fast" ? "checked" : ""}/>
            <span>Fast</span>
          </label>
          <label class="popover-mode">
            <input type="radio" name="mode" value="thorough" ${cfg.mode === "thorough" ? "checked" : ""}/>
            <span>Thorough</span>
          </label>
        </div>
        <p class="muted small popover-foot">Saved automatically. Closes on outside click.</p>
      </div>
    </div>
  `;
}

function wireFullReviewControls(scope, projectName) {
  const group = scope.querySelector('[data-bind="full-review-group"]');
  if (!group) return;
  const popover = group.querySelector('[data-bind="full-review-popover"]');

  group.querySelector('[data-action="toggle-full-review-popover"]')
    .addEventListener("click", ev => {
      ev.stopPropagation();
      popover.classList.toggle("hidden");
    });

  // Outside-click closes the popover.
  document.addEventListener("click", ev => {
    if (!group.contains(ev.target)) popover.classList.add("hidden");
  });

  // Persist on every change.
  popover.addEventListener("change", () => {
    const verbs = {};
    popover.querySelectorAll('input[name="verb"]').forEach(c =>
      verbs[c.value] = c.checked);
    const mode = popover.querySelector('input[name="mode"]:checked')?.value || "thorough";
    saveFullReviewConfig({verbs, mode});
  });

  group.querySelector('[data-action="run-full-review"]')
    .addEventListener("click", () => {
      popover.classList.add("hidden");
      runFullReview(projectName);
    });

  // Quick action: fire a one-off ingest. Doesn't go through the Full
  // Review sequence — ingest is its own thing, fast and deterministic.
  group.querySelector('[data-action="quick-reingest"]')
    .addEventListener("click", () => {
      popover.classList.add("hidden");
      runQuickIngest(projectName);
    });
}

async function runQuickIngest(projectName) {
  const def = ACTIVITY_DEFS.find(d => d.verb === "ingest");
  try {
    const resp = await fetch(
      `/api/projects/${encodeURIComponent(projectName)}/activities/ingest`,
      {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({voice: state.detail?.voices?.[0] || "academic", mode: "fast"}),
      }
    );
    if (!resp.ok) {
      alert(`Ingest failed: ${await resp.text()}`);
      return;
    }
    const data = await resp.json();
    attachActivityProgress(def, data);
  } catch (err) {
    alert(`Network error: ${err.message}`);
  }
}

async function runFullReview(projectName) {
  const cfg = getFullReviewConfig();
  const selected = FULL_REVIEW_ORDER.filter(v => cfg.verbs[v]);
  if (!selected.length) {
    alert("Pick at least one activity in the dropdown.");
    return;
  }
  // Make sure the user sees progress: navigate to Activities → Running.
  if (!location.hash.startsWith(`#/p/${encodeURIComponent(projectName)}/activities`)) {
    navigate(`/p/${encodeURIComponent(projectName)}/activities/running`);
    await new Promise(r => setTimeout(r, 30));
  } else {
    navigate(`/p/${encodeURIComponent(projectName)}/activities/running`);
    await new Promise(r => setTimeout(r, 30));
  }

  const main = document.getElementById("app");
  const body = main.querySelector(".review-body");
  if (!body) return;

  // Banner showing current step within the sequence.
  const stepLabels = selected.map(v => FULL_REVIEW_LABELS[v]).join(" → ");
  body.innerHTML = `
    <div class="full-review-banner card">
      <h3 class="subhead">Full Review running</h3>
      <p class="muted small" data-bind="step">Step 0 of ${selected.length}</p>
      <p class="muted small">Plan: <strong>${escapeHtml(stepLabels)}</strong> (${escapeHtml(cfg.mode)})</p>
    </div>
    <div data-bind="progress-host"></div>
  `;
  const stepEl = body.querySelector('[data-bind="step"]');
  const progressHost = body.querySelector('[data-bind="progress-host"]');

  for (let i = 0; i < selected.length; i++) {
    const verb = selected[i];
    stepEl.textContent = `Step ${i + 1} of ${selected.length}: ${FULL_REVIEW_LABELS[verb]}`;

    // Re-fetch state to confirm preconditions still hold (the previous
    // activity may have produced the artefact this one needs).
    let projState;
    try {
      projState = await fetchJSON(
        `/api/projects/${encodeURIComponent(projectName)}/state`
      );
    } catch (err) {
      progressHost.insertAdjacentHTML("beforeend",
        `<div class="empty-state">State refresh failed: ${escapeHtml(err.message)}. Stopping.</div>`);
      return;
    }
    const blocker = projState.blockers[verb];
    if (blocker) {
      progressHost.insertAdjacentHTML("beforeend",
        `<div class="muted small" style="padding: 12px;">Skipped ${escapeHtml(verb)}: ${escapeHtml(blocker)}</div>`);
      continue;
    }

    // Mount a fresh progress card for this activity.
    const slot = document.createElement("div");
    slot.dataset.fullReviewStep = String(i);
    progressHost.appendChild(slot);
    slot.appendChild(cloneTemplate("tpl-activity-progress"));
    slot.querySelector('[data-bind="title"]').textContent =
      `${FULL_REVIEW_LABELS[verb]} (${cfg.mode}) · live progress`;

    // Build the request body.
    const body_ = {voice: state.detail?.voices?.[0] || "academic", mode: cfg.mode};
    if (verb === "draft")     body_.force = false;
    if (verb === "refine")    body_.max_passes = 3;

    let runData;
    try {
      const resp = await fetch(
        `/api/projects/${encodeURIComponent(projectName)}/activities/${verb}`,
        {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(body_),
        }
      );
      if (!resp.ok) {
        const txt = await resp.text();
        slot.querySelector('[data-bind="status"]').classList.remove("hidden");
        slot.querySelector('[data-bind="state"]').textContent = "failed";
        slot.querySelector('[data-bind="state"]').className = "pill bad";
        progressHost.insertAdjacentHTML("beforeend",
          `<div class="muted small" style="padding: 8px;">Failed to start ${escapeHtml(verb)}: ${escapeHtml(txt)}. Stopping.</div>`);
        return;
      }
      runData = await resp.json();
    } catch (err) {
      progressHost.insertAdjacentHTML("beforeend",
        `<div class="empty-state">Network error starting ${escapeHtml(verb)}: ${escapeHtml(err.message)}. Stopping.</div>`);
      return;
    }

    // Cache the run so the Running sub-tab knows what's in flight.
    state.lastActivityRun = {
      run_id: runData.run_id,
      verb,
      verbName: FULL_REVIEW_LABELS[verb],
      mode: runData.mode,
    };
    startTimeline(runData, slot);
    const ok = await openWebSocketAndWait(projectName, runData.run_id, slot);
    if (!ok) {
      progressHost.insertAdjacentHTML("beforeend",
        `<div class="muted small" style="padding: 8px;">${escapeHtml(verb)} did not finalise. Stopping the sequence.</div>`);
      return;
    }
  }

  stepEl.textContent = `Done · ${selected.length} activit${selected.length === 1 ? "y" : "ies"} ran`;
  progressHost.insertAdjacentHTML("beforeend",
    `<div class="card" style="padding: 16px;">
       <h3 class="subhead">Full Review complete</h3>
       <p class="muted small">All selected activities finished. Switch to the History sub-tab for the run record, or to Output for the artefacts.</p>
     </div>`);
}

// Promise-wrapper around openWebSocket: resolves true on
// run_finished, false on run_failed or socket close without a
// terminal event. Used by Full Review to await each activity.
function openWebSocketAndWait(projectName, runId, panel) {
  return new Promise(resolve => {
    if (state.ws) try { state.ws.close(); } catch (e) {}
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/api/projects/${encodeURIComponent(projectName)}/runs/${runId}`;
    const ws = new WebSocket(url);
    state.ws = ws;
    let settled = false;
    ws.addEventListener("message", e => {
      const event = JSON.parse(e.data);
      handleEvent(event, panel);
      if (event.type === "run_finished" && !settled) {
        settled = true;
        resolve(true);
      }
      if (event.type === "run_failed" && !settled) {
        settled = true;
        resolve(false);
      }
    });
    ws.addEventListener("close", () => {
      if (state.elapsedTimer) {
        clearInterval(state.elapsedTimer);
        state.elapsedTimer = null;
      }
      if (!settled) { settled = true; resolve(false); }
    });
    ws.addEventListener("error", () => {
      if (!settled) { settled = true; resolve(false); }
    });
  });
}


// ════════════════════════════════════════════════════════════
// Output → Restructure sub-tab
// ════════════════════════════════════════════════════════════

async function renderRestructureSubview(body) {
  const proj = encodeURIComponent(state.current);
  let res;
  try {
    res = await fetchJSON(`/api/projects/${proj}/restructure`);
  } catch (err) {
    body.innerHTML = `
      <div class="card">
        <p class="muted small">No restructure report yet. Run <strong>Restructure</strong> from the Activities tab — it audits the document order against academic-writing rules and surfaces dependency violations.</p>
      </div>`;
    return;
  }
  const generated = res.generated_at
    ? formatTimestamp(new Date(res.generated_at).getTime() / 1000)
    : "—";

  const sectionDiff = res.section_reorder ? renderRestructureOrderDiff(
    "Section order",
    res.section_reorder.current_order,
    res.section_reorder.proposed_order,
    res.section_reorder.commentary,
  ) : "";

  const clusterDiffs = (res.cluster_reorders || [])
    .filter(cr => _orderChanged(cr.current_order, cr.proposed_order))
    .map(cr => renderRestructureOrderDiff(
      `Clusters in “${cr.section_title}”`,
      cr.current_order,
      cr.proposed_order,
      cr.commentary,
    )).join("");

  const noChangeNote = !sectionDiff && !clusterDiffs
    ? `<p class="muted small">No reordering recommended at any level.</p>`
    : "";

  const suggestionsList = (res.suggestions || []).length
    ? `
      <h3 class="subhead" style="margin-top: 18px;">Specific operations (${res.suggestions.length})</h3>
      <div class="restructure-suggestions">
        ${res.suggestions.map(renderRestructureSuggestion).join("")}
      </div>`
    : "";

  body.innerHTML = `
    <div class="card">
      <div class="lit-summary">
        <h3 class="subhead">Restructure · ${res.suggestions.length} suggestion(s)</h3>
        <div class="lit-summary-meta">
          <span class="pill ${res.mode === "thorough" ? "ok" : ""}">${escapeHtml(res.mode)} mode</span>
          <span class="muted small">Generated: ${generated}</span>
        </div>
      </div>
      ${noChangeNote}
      ${sectionDiff}
      ${clusterDiffs}
      ${suggestionsList}
    </div>`;
}


function renderRestructureOrderDiff(title, current, proposed, commentary) {
  const changed = _orderChanged(current, proposed);
  const renderList = (ids, highlightAt) => `
    <ol class="order-list">
      ${ids.map((id, i) => {
        const moved = highlightAt && current.indexOf(id) !== i;
        return `<li class="${moved ? "moved" : ""}"><code>${escapeHtml(id)}</code></li>`;
      }).join("")}
    </ol>`;
  return `
    <div class="restructure-block">
      <h4 class="restructure-block-head">
        ${escapeHtml(title)}
        ${changed ? `<span class="pill">change suggested</span>` : `<span class="pill ok">order looks fine</span>`}
      </h4>
      <div class="restructure-diff">
        <div>
          <span class="muted small">Current</span>
          ${renderList(current, false)}
        </div>
        <div>
          <span class="muted small">Proposed</span>
          ${renderList(proposed, changed)}
        </div>
      </div>
      ${commentary ? `<p class="muted small restructure-commentary">${escapeHtml(commentary)}</p>` : ""}
    </div>`;
}


function renderRestructureSuggestion(s) {
  const movePart = s.before_id
    ? `before <code>${escapeHtml(s.before_id)}</code>`
    : s.after_id
      ? `after <code>${escapeHtml(s.after_id)}</code>`
      : s.paired_id
        ? `with <code>${escapeHtml(s.paired_id)}</code>`
        : "";
  return `
    <div class="restructure-suggestion">
      <div class="restructure-suggestion-head">
        <span class="pill kind-${escapeAttr(s.kind)}">${escapeHtml(s.kind.replace("_", " "))}</span>
        <span class="pill conf-${escapeAttr(s.confidence)}">${escapeHtml(s.confidence)}</span>
        <code>${escapeHtml(s.target_id)}</code>
        ${movePart ? `<span class="muted small">→ ${movePart}</span>` : ""}
      </div>
      <p class="restructure-rationale">${escapeHtml(s.rationale)}</p>
      ${s.rule ? `<p class="muted small restructure-rule">Rule: ${escapeHtml(s.rule)}</p>` : ""}
    </div>`;
}


function _orderChanged(a, b) {
  if (!Array.isArray(a) || !Array.isArray(b)) return false;
  if (a.length !== b.length) return true;
  for (let i = 0; i < a.length; i++) {
    if (a[i] !== b[i]) return true;
  }
  return false;
}


// ════════════════════════════════════════════════════════════
// Output → Review sub-tab — supervisor critique + track changes
// ════════════════════════════════════════════════════════════

async function renderSupervisorReviewSubview(body) {
  const proj = encodeURIComponent(state.current);
  body.innerHTML = `<div class="muted small" style="padding: 12px;">Loading review…</div>`;

  let report;
  let trackChanges = "";
  try {
    [report, trackChanges] = await Promise.all([
      fetchJSON(`/api/projects/${proj}/review`),
      fetchText(`/api/projects/${proj}/review-track-changes`).catch(() => ""),
    ]);
  } catch (err) {
    body.innerHTML = `
      <div class="card">
        <p class="muted small">No supervisor review yet. Run <strong>Review</strong> from the Activities tab — it produces a supervisor-style critique of the rendered paper plus a marked track-changes version.</p>
      </div>`;
    return;
  }

  const generated = report.generated_at
    ? formatTimestamp(new Date(report.generated_at).getTime() / 1000)
    : "—";
  const sectionsHtml = (report.section_critiques || []).map(sc => `
    <details class="review-section">
      <summary><strong>${escapeHtml(sc.section_title)}</strong></summary>
      <p>${escapeHtml(sc.critique)}</p>
    </details>`).join("");

  body.innerHTML = `
    <div class="card">
      <div class="lit-summary">
        <h3 class="subhead">Supervisor review</h3>
        <div class="lit-summary-meta">
          <span class="pill ${report.mode === "thorough" ? "ok" : ""}">${escapeHtml(report.mode)} mode</span>
          <span class="muted small">${(report.cluster_revisions || []).length} cluster(s) reviewed</span>
          <span class="muted small">Generated: ${generated}</span>
        </div>
      </div>
      <div class="review-toggle">
        <button class="btn sm active" data-r-mode="critique">Critique</button>
        <button class="btn sm" data-r-mode="track-changes">Track changes</button>
      </div>
      <div class="review-pane review-pane-critique">
        <h3 class="subhead">Overall</h3>
        <p>${escapeHtml(report.overall_critique || "(not generated — fast mode)")}</p>
        <h3 class="subhead">By section</h3>
        ${sectionsHtml || `<p class="muted small">No section critiques.</p>`}
      </div>
      <div class="review-pane review-pane-track-changes hidden">
        <article class="prose track-changes-prose">${
          trackChanges
            ? renderMarkdown(trackChanges)
            : `<p class="muted small">Track-changes paper not available.</p>`
        }</article>
      </div>
    </div>`;

  // Toggle between critique view and track-changes view.
  const critiquePane = body.querySelector(".review-pane-critique");
  const trackPane = body.querySelector(".review-pane-track-changes");
  body.querySelectorAll("[data-r-mode]").forEach(btn => {
    btn.addEventListener("click", () => {
      body.querySelectorAll("[data-r-mode]").forEach(b =>
        b.classList.toggle("active", b === btn));
      const mode = btn.dataset.rMode;
      critiquePane.classList.toggle("hidden", mode !== "critique");
      trackPane.classList.toggle("hidden", mode !== "track-changes");
    });
  });
}

// ─── Citations tab (Phase 1A) ────────────────────────
//
// Surfaces the references/ package's six-phase pipeline:
//   1. Scan: extract every inline + footnote + bibliography entry
//   2. Verify: Crossref + OpenAlex per source, surface discrepancies
//   3. Fill: walk per-field accept/reject for each disagreement
//   4. Restyle: re-emit the document in any target style or per-journal override
//
// Mirrors the CLI subcommands. Pure browser code; no LLM.

function renderCitationsTab(main, subSectionId) {
  const projSlug = state.current;
  const panel = main.querySelector('[data-panel="citations"]');
  const otherPanels = main.querySelectorAll('.subnav-panel');
  otherPanels.forEach(p => p.classList.toggle('visible', p === panel));
  panel.innerHTML = `
    <div class="citations-tab">
      <header class="citations-header">
        <h2>Citations</h2>
        <p class="muted">Scan a document, verify against Crossref + OpenAlex,
          fill disagreements, restyle for any journal. None of these mutate
          your outline; verified metadata lands on your sources.</p>
      </header>
      <div class="citations-state" data-bind="state">
        <div class="muted small">Loading…</div>
      </div>
      <div class="citations-panes">
        <section class="citations-pane" data-pane="scan">
          <h3>1. Scan</h3>
          <p class="muted small">Detects citation system; extracts inline,
             footnotes, bibliography. ~50 ms; no LLM.</p>
          <button class="btn primary" data-action="scan">Scan rendered paper</button>
          <div class="muted small" data-bind="scan-detail"></div>
        </section>
        <section class="citations-pane" data-pane="verify">
          <h3>2. Verify</h3>
          <p class="muted small">Look up every source against Crossref +
             OpenAlex. Cached by content hash; re-runs are free.</p>
          <label class="filter-row">
            <input type="email" data-bind="email"
                   placeholder="email (for Crossref polite-pool)"
                   style="flex:1; padding:6px 8px; border:1px solid var(--border); border-radius:6px;">
          </label>
          <button class="btn primary" data-action="verify">Verify all sources</button>
          <div class="muted small" data-bind="verify-detail"></div>
        </section>
        <section class="citations-pane" data-pane="fill">
          <h3>3. Fill discrepancies</h3>
          <p class="muted small">Walk each field where the paper disagrees
             with Crossref. Decisions are append-only.</p>
          <button class="btn" data-action="open-fill">Open fill walkthrough</button>
          <div class="muted small" data-bind="fill-detail"></div>
        </section>
        <section class="citations-pane" data-pane="restyle">
          <h3>4. Restyle</h3>
          <p class="muted small">Re-emit your document in any base style or
             per-journal override. Deterministic, instant.</p>
          <div class="restyle-controls" style="display:flex; gap:8px; flex-wrap:wrap; align-items:center;">
            <select data-bind="restyle-style" style="padding:6px 8px; border:1px solid var(--border); border-radius:6px;"></select>
            <select data-bind="restyle-journal" style="padding:6px 8px; border:1px solid var(--border); border-radius:6px;">
              <option value="">— no journal override —</option>
            </select>
            <button class="btn" data-action="install-journals">Install starter library</button>
          </div>
          <button class="btn primary" data-action="restyle" style="margin-top:8px;">Restyle</button>
          <div class="muted small" data-bind="restyle-detail"></div>
        </section>
      </div>
      <div class="citations-fill-walkthrough hidden" data-bind="fill-walkthrough"></div>
    </div>`;
  refreshCitationsState(panel, projSlug);
  wireCitationsActions(panel, projSlug);
}

async function refreshCitationsState(panel, projSlug) {
  const stateBox = panel.querySelector('[data-bind="state"]');
  try {
    const data = await fetchJSON(`/api/projects/${encodeURIComponent(projSlug)}/citations`);
    const scan = data.scan;
    const v = data.verifier;
    const f = data.fill;
    stateBox.innerHTML = `
      <div class="citations-scoreboard">
        <div class="cs-row">
          <strong>Scan:</strong>
          ${scan ? `${scan.detected_system} system · ${scan.counts.inline_total} inline · ${scan.counts.bibliography_entries} bibliography entries`
                 : `<span class="muted">no scan yet</span>`}
        </div>
        <div class="cs-row">
          <strong>Verify:</strong>
          ${v.verified_count > 0
            ? `${v.matched}/${v.verified_count} matched · <span class="${v.errors > 0 ? 'badge-error' : ''}">${v.errors} error(s)</span> · ${v.warnings} warning(s)`
            : `<span class="muted">no verifier cache</span>`}
        </div>
        <div class="cs-row">
          <strong>Fill:</strong>
          ${f.pending_count > 0
            ? `${f.pending_count} pending (${f.pending_by_severity.error} error · ${f.pending_by_severity.warning} warning · ${f.pending_by_severity.info} info) · ${f.decided_count} decided`
            : `<span class="muted">${f.decided_count} decisions recorded</span>`}
        </div>
      </div>`;
    const styleSelect = panel.querySelector('[data-bind="restyle-style"]');
    styleSelect.innerHTML = (data.available_styles || [])
      .map(s => `<option value="${s}">${s}</option>`).join('');
    const journalSelect = panel.querySelector('[data-bind="restyle-journal"]');
    const opts = ['<option value="">— no journal override —</option>'];
    (data.available_journals || []).forEach(j =>
      opts.push(`<option value="${escapeAttr(j)}">${escapeHtml(j)}</option>`));
    journalSelect.innerHTML = opts.join('');
  } catch (err) {
    stateBox.innerHTML = `<div class="muted">Could not load citation state: ${escapeHtml(err.message)}</div>`;
  }
}

function wireCitationsActions(panel, projSlug) {
  panel.querySelector('[data-action="scan"]').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true; btn.textContent = 'Scanning…';
    try {
      const r = await postJSON(
        `/api/projects/${encodeURIComponent(projSlug)}/citations/scan`,
        { match: true });
      panel.querySelector('[data-bind="scan-detail"]').textContent =
        r.error ? `error: ${r.error}` :
        `${r.detected_system} system · ${r.counts.inline_total} inline · ${r.counts.bibliography_entries} bibliography`;
    } catch (err) {
      panel.querySelector('[data-bind="scan-detail"]').textContent = `error: ${err.message}`;
    } finally {
      btn.disabled = false; btn.textContent = 'Scan rendered paper';
      refreshCitationsState(panel, projSlug);
    }
  });

  panel.querySelector('[data-action="verify"]').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    const email = panel.querySelector('[data-bind="email"]').value;
    btn.disabled = true; btn.textContent = 'Verifying…';
    try {
      const r = await postJSON(
        `/api/projects/${encodeURIComponent(projSlug)}/citations/verify`,
        { email });
      panel.querySelector('[data-bind="verify-detail"]').textContent =
        r.error ? `error: ${r.error}` :
        `${r.matched}/${r.verified_count} matched · ${r.errors} error(s) · ${r.warnings} warning(s)`;
    } catch (err) {
      panel.querySelector('[data-bind="verify-detail"]').textContent = `error: ${err.message}`;
    } finally {
      btn.disabled = false; btn.textContent = 'Verify all sources';
      refreshCitationsState(panel, projSlug);
    }
  });

  panel.querySelector('[data-action="open-fill"]').addEventListener('click',
    () => openFillWalkthrough(panel, projSlug));

  panel.querySelector('[data-action="install-journals"]').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true; btn.textContent = 'Installing…';
    try {
      await postJSON(
        `/api/projects/${encodeURIComponent(projSlug)}/citations/journals/install`,
        {});
    } finally {
      btn.disabled = false; btn.textContent = 'Install starter library';
      refreshCitationsState(panel, projSlug);
    }
  });

  panel.querySelector('[data-action="restyle"]').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    const style = panel.querySelector('[data-bind="restyle-style"]').value;
    const journal = panel.querySelector('[data-bind="restyle-journal"]').value;
    const detail = panel.querySelector('[data-bind="restyle-detail"]');
    const document_rel = `outputs/paper.academic.md`;  // sensible default
    btn.disabled = true; btn.textContent = 'Restyling…';
    try {
      const r = await postJSON(
        `/api/projects/${encodeURIComponent(projSlug)}/citations/restyle`,
        { style, journal, document: document_rel });
      if (r.error) {
        detail.textContent = `error: ${r.error}`;
      } else {
        detail.innerHTML =
          `${r.inline_replaced} inline replaced · ${r.bibliography_emitted} bibliography entries · ` +
          `<a href="/api/projects/${encodeURIComponent(projSlug)}/file?path=${encodeURIComponent(r.output_path)}" target="_blank">${escapeHtml(r.output_path)}</a>`;
      }
    } catch (err) {
      detail.textContent = `error: ${err.message}`;
    } finally {
      btn.disabled = false; btn.textContent = 'Restyle';
    }
  });
}

async function openFillWalkthrough(panel, projSlug) {
  const wt = panel.querySelector('[data-bind="fill-walkthrough"]');
  wt.classList.remove('hidden');
  wt.innerHTML = `<div class="muted">Loading candidates…</div>`;
  try {
    const r = await fetchJSON(`/api/projects/${encodeURIComponent(projSlug)}/citations/fill-candidates`);
    const cands = r.candidates || [];
    if (!cands.length) {
      wt.innerHTML = `<div class="empty-state"><h3>Nothing to fill</h3>
        <p>Either every disagreement is decided, or there are no verified sources yet.</p></div>`;
      return;
    }
    wt.innerHTML = `
      <h3>Fill ${cands.length} field(s)</h3>
      <p class="muted small">Pick an action per row. Decisions are append-only.</p>
      <table class="fill-table">
        <thead>
          <tr><th>source</th><th>field</th><th>severity</th><th>paper says</th><th>canonical</th><th>action</th></tr>
        </thead>
        <tbody>
          ${cands.map((c, i) => `
            <tr data-row="${i}">
              <td><code>${escapeHtml(c.source_id)}</code></td>
              <td>${escapeHtml(c.field)}</td>
              <td><span class="badge-${c.severity}">${c.severity}</span></td>
              <td class="paper">${escapeHtml(c.paper_value || '(empty)')}</td>
              <td class="canon">${escapeHtml(c.canonical_value || '(empty)')}</td>
              <td>
                <select data-bind="action">
                  <option value="skip">skip</option>
                  <option value="accept_canonical">accept canonical</option>
                  <option value="reject">keep paper</option>
                  <option value="manual_override">manual…</option>
                </select>
                <input type="text" data-bind="manual" placeholder="manual value" class="hidden"
                       style="width:140px; margin-left:6px; padding:4px 6px;">
              </td>
            </tr>
          `).join('')}
        </tbody>
      </table>
      <button class="btn primary" data-action="apply-fill" style="margin-top:12px;">Apply decisions</button>
    `;
    // Show manual-override input only when its action is selected.
    wt.querySelectorAll('select[data-bind="action"]').forEach(sel => {
      sel.addEventListener('change', () => {
        const manual = sel.parentElement.querySelector('[data-bind="manual"]');
        manual.classList.toggle('hidden', sel.value !== 'manual_override');
      });
    });
    wt.querySelector('[data-action="apply-fill"]').addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true; btn.textContent = 'Applying…';
      const decisions = [];
      wt.querySelectorAll('tbody tr').forEach((tr, i) => {
        const action = tr.querySelector('[data-bind="action"]').value;
        const manual = tr.querySelector('[data-bind="manual"]').value;
        decisions.push({
          source_id: cands[i].source_id,
          field: cands[i].field,
          action,
          chosen_value: manual,
        });
      });
      try {
        const out = await postJSON(
          `/api/projects/${encodeURIComponent(projSlug)}/citations/fill`,
          { decisions });
        wt.innerHTML = `<div class="empty-state"><h3>Applied</h3>
          <p>${out.applied} decision(s) recorded; ${out.skipped} skipped.</p></div>`;
        refreshCitationsState(panel, projSlug);
      } catch (err) {
        btn.disabled = false; btn.textContent = 'Apply decisions';
        alert(`Failed: ${err.message}`);
      }
    });
  } catch (err) {
    wt.innerHTML = `<div class="muted">Failed to load: ${escapeHtml(err.message)}</div>`;
  }
}

