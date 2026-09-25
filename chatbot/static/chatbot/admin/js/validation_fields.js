(function () {
  "use strict";

  /* ── Dynamic add / remove rows for choices & error messages ── */

  function addRow(containerId, prefix) {
    var container = document.getElementById(containerId);
    if (!container) return;
    var rows = container.querySelectorAll(".dynamic-row");
    var idx = rows.length;
    var row = document.createElement("div");
    row.className = "dynamic-row";
    row.style.cssText =
      "display:flex;gap:8px;margin-bottom:4px;align-items:center;";
    row.innerHTML =
      '<input type="text" name="' + prefix + "_key_" + idx +
      '" placeholder="key" style="width:140px;">' +
      '<input type="text" name="' + prefix + "_label_" + idx +
      '" placeholder="label (English)" style="width:260px;">' +
      '<a href="#" class="remove-row" ' +
      'style="color:red;font-weight:bold;text-decoration:none;" ' +
      'title="Remove">\u2715</a>';
    container.appendChild(row);
    attachRemove(row);
  }

  function attachRemove(scope) {
    scope.querySelectorAll(".remove-row").forEach(function (link) {
      link.addEventListener("click", function (e) {
        e.preventDefault();
        link.closest(".dynamic-row").remove();
      });
    });
  }

  function initDynamicRows() {
    document.querySelectorAll(".add-choice-btn").forEach(function (btn) {
      if (btn.dataset.bound) return;
      btn.dataset.bound = "1";
      btn.addEventListener("click", function (e) {
        e.preventDefault();
        addRow(btn.getAttribute("data-container"), "choice");
      });
    });
    document.querySelectorAll(".add-error-btn").forEach(function (btn) {
      if (btn.dataset.bound) return;
      btn.dataset.bound = "1";
      btn.addEventListener("click", function (e) {
        e.preventDefault();
        addRow(btn.getAttribute("data-container"), "error");
      });
    });
    attachRemove(document);
  }

  /* ── Conditional visibility of MC-config fieldset ── */

  function toggleMcConfig() {
    document.querySelectorAll("select[name$='-validation_type']").forEach(
      function (sel) {
        var inlineForm = sel.closest(".inline-related");
        if (!inlineForm) return;
        var mcFieldset = inlineForm.querySelector("fieldset.mc-config");
        if (!mcFieldset) return;

        function update() {
          if (sel.value === "MULTIPLE_CHOICE") {
            mcFieldset.classList.add("mc-visible");
            mcFieldset.classList.remove("collapsed");
          } else {
            mcFieldset.classList.remove("mc-visible");
          }
        }

        update();
        if (!sel.dataset.mcBound) {
          sel.dataset.mcBound = "1";
          sel.addEventListener("change", update);
        }
      }
    );
  }

  /* ── Bootstrap ── */

  function init() {
    initDynamicRows();
    toggleMcConfig();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // Re-init when Django adds a new inline form (the "Add another" link)
  var observer = new MutationObserver(function () {
    init();
  });
  var group = document.querySelector(".inline-group");
  if (group) {
    observer.observe(group, { childList: true, subtree: true });
  }
})();
