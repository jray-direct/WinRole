/* Clicks inside the job pane. The pane HTML is injected with innerHTML, so
   handlers live here on document and survive each swap. */
(function () {
  function rootOf(target) {
    return target.closest("[data-ai-root]");
  }

  function setStatus(root, message) {
    var status = root.querySelector("[data-ai-status]");
    if (status) status.textContent = message || "";
  }

  async function postJSON(url, body) {
    var res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify(body || {})
    });
    var data = await res.json().catch(function () {
      return { ok: false, message: "Could not read the response." };
    });
    if (!data.message && !res.ok) data.message = "Request failed.";
    return data;
  }

  document.addEventListener("click", async function (event) {
    var button = event.target.closest("[data-ai]");
    if (!button) return;
    var root = rootOf(button);
    if (!root) return;
    event.preventDefault();
    if (root.dataset.aiReady !== "1") {
      setStatus(root, "Set AI_API_KEY to explain jobs and draft cover letters.");
      return;
    }
    var kind = button.getAttribute("data-ai");
    var url = {
      cover: root.dataset.coverUrl,
      bullets: root.dataset.bulletsUrl,
      interview: root.dataset.interviewUrl
    }[kind] || root.dataset.explainUrl;
    var waiting = {
      cover: "Drafting…",
      bullets: "Rewriting bullets…",
      interview: "Writing questions…"
    }[kind] || "Reading the posting…";
    setStatus(root, waiting);
    button.disabled = true;
    try {
      var data = await postJSON(url, {});
      if (!data.ok) {
        setStatus(root, data.silent ? "" : (data.message || "Request failed."));
        return;
      }
      setStatus(root, "");
      if (kind === "cover") {
        var draft = root.querySelector("[data-ai-draft]");
        var area = root.querySelector("[data-ai-draft-text]");
        if (area) area.value = data.text || "";
        if (draft) draft.hidden = false;
      } else if (kind === "bullets") {
        var bullets = root.querySelector("[data-ai-bullets]");
        var bulletsArea = root.querySelector("[data-ai-bullets-text]");
        if (bulletsArea) bulletsArea.value = data.text || "";
        if (bullets) bullets.hidden = false;
      } else if (kind === "interview") {
        var interview = root.querySelector("[data-ai-interview]");
        var list = root.querySelector("[data-ai-interview-list]");
        if (list) {
          list.textContent = "";
          (data.questions || []).forEach(function (question) {
            var item = document.createElement("li");
            item.textContent = question;
            list.appendChild(item);
          });
        }
        if (interview) interview.hidden = false;
      } else {
        var explain = root.querySelector("[data-ai-explain]");
        if (explain) {
          explain.hidden = false;
          explain.textContent = data.text || "";
        }
      }
    } catch (err) {
      setStatus(root, "Could not reach the server.");
    } finally {
      button.disabled = false;
    }
  });

  document.addEventListener("click", async function (event) {
    var save = event.target.closest("[data-cover-save]");
    if (!save) return;
    var root = rootOf(save);
    if (!root) return;
    event.preventDefault();
    var area = root.querySelector("[data-ai-draft-text]");
    var body = area ? area.value.trim() : "";
    if (!body) {
      setStatus(root, "Nothing to save.");
      return;
    }
    save.disabled = true;
    setStatus(root, "Saving…");
    try {
      var data = await postJSON(root.dataset.saveUrl, { body: body });
      if (!data.ok) {
        setStatus(root, data.message || "Could not save.");
        return;
      }
      var select = document.querySelector('select[name="cover_id"]');
      if (select) {
        var option = document.createElement("option");
        option.value = String(data.id);
        option.textContent = data.name;
        option.selected = true;
        select.appendChild(option);
      }
      setStatus(root, "Saved to the vault. It is selected as this job’s cover letter.");
    } catch (err) {
      setStatus(root, "Could not reach the server.");
    } finally {
      save.disabled = false;
    }
  });

  document.addEventListener("click", async function (event) {
    var save = event.target.closest("[data-bullets-save]");
    if (!save) return;
    var root = rootOf(save);
    if (!root) return;
    event.preventDefault();
    var area = root.querySelector("[data-ai-bullets-text]");
    var body = area ? area.value.trim() : "";
    if (!body) {
      setStatus(root, "Nothing to save.");
      return;
    }
    save.disabled = true;
    setStatus(root, "Saving…");
    try {
      var data = await postJSON(root.dataset.bulletsSaveUrl, { body: body });
      if (!data.ok) {
        setStatus(root, data.message || "Could not save.");
        return;
      }
      setStatus(root, "Saved. The uploaded file was not replaced.");
    } catch (err) {
      setStatus(root, "Could not reach the server.");
    } finally {
      save.disabled = false;
    }
  });

  document.addEventListener("click", async function (event) {
    var button = event.target.closest("[data-suggest-keyword]");
    if (!button) return;
    event.preventDefault();
    var input = document.getElementById("scan-keyword");
    var before = input ? input.value : "";
    button.disabled = true;
    try {
      var data = await postJSON(button.getAttribute("data-url"), {});
      if (data.ok && data.keyword && input) input.value = data.keyword;
      else if (input) input.value = before;
    } catch (err) {
      if (input) input.value = before;
    } finally {
      button.disabled = false;
    }
  });

  document.addEventListener("click", async function (event) {
    var button = event.target.closest("[data-follow-up]");
    if (!button) return;
    event.preventDefault();
    var form = button.closest("form");
    if (!form) return;
    var notes = form.querySelector('[name="notes"]');
    var status = form.querySelector('[name="status"]');
    var statusBefore = status ? status.value : "";
    button.disabled = true;
    try {
      var data = await postJSON(button.getAttribute("data-url"), {});
      if (status) status.value = statusBefore;
      if (data.ok && notes && data.notes) notes.value = data.notes;
    } catch (err) {
      if (status) status.value = statusBefore;
    } finally {
      button.disabled = false;
    }
  });

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-dismiss-suggestion]");
    if (!button) return;
    event.preventDefault();
    var chip = button.closest("[data-suggestion]");
    if (chip) chip.remove();
  });
})();
