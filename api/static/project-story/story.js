(function () {
  "use strict";

  var story = window.PROJECT_STORY || { sources: [] };

  function create(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function activatePipeline(name, focus) {
    document.querySelectorAll("[data-pipeline-tab]").forEach(function (tab) {
      var active = tab.dataset.pipelineTab === name;
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      if (active && focus) tab.focus();
    });
    document.querySelectorAll("[data-pipeline-panel]").forEach(function (panel) {
      var active = panel.dataset.pipelinePanel === name;
      panel.hidden = !active;
      panel.classList.toggle("is-active", active);
    });
  }

  var pipelineTabs = Array.from(document.querySelectorAll("[data-pipeline-tab]"));
  pipelineTabs.forEach(function (tab, index) {
    tab.addEventListener("click", function () {
      activatePipeline(tab.dataset.pipelineTab, false);
    });
    tab.addEventListener("keydown", function (event) {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      var direction = event.key === "ArrowRight" ? 1 : -1;
      var next = (index + direction + pipelineTabs.length) % pipelineTabs.length;
      activatePipeline(pipelineTabs[next].dataset.pipelineTab, true);
    });
  });

  var sourceDialog = document.getElementById("sourceDialog");
  var sourceList = document.getElementById("sourceDialogList");

  function renderSources(focusId) {
    if (!sourceList) return;
    sourceList.replaceChildren();
    story.sources.forEach(function (source) {
      var item = create("article", "source-entry" + (source.id === focusId ? " focused" : ""));
      item.dataset.sourceEntry = source.id;
      var number = create("span", "source-number", source.number);
      var body = create("div");
      body.append(
        create("small", "", source.kind),
        create("h3", "", source.title),
        create("p", "", source.detail)
      );
      if (source.artifact) body.appendChild(create("code", "", source.artifact));
      if (source.url) {
        var link = create("a", "", "Open source ↗");
        link.href = source.url;
        link.target = "_blank";
        link.rel = "noreferrer noopener";
        body.appendChild(link);
      }
      item.append(number, body);
      sourceList.appendChild(item);
    });
  }

  function openSources(focusId) {
    if (!sourceDialog) return;
    renderSources(focusId);
    sourceDialog.showModal();
    if (focusId) {
      window.requestAnimationFrame(function () {
        var entry = sourceList.querySelector('[data-source-entry="' + focusId + '"]');
        if (entry) entry.scrollIntoView({ block: "center" });
      });
    }
  }

  document.querySelectorAll("[data-open-sources], #openSources").forEach(function (button) {
    button.addEventListener("click", function () { openSources(); });
  });

  document.querySelectorAll(".cite").forEach(function (button) {
    button.addEventListener("click", function () { openSources(button.dataset.source); });
  });

  if (sourceDialog) {
    sourceDialog.querySelector(".dialog-close").addEventListener("click", function () {
      sourceDialog.close();
    });
    sourceDialog.addEventListener("click", function (event) {
      if (event.target === sourceDialog) sourceDialog.close();
    });
  }

  var lightbox = document.getElementById("imageLightbox");
  var lightboxImage = document.getElementById("lightboxImage");
  var lightboxCaption = document.getElementById("lightboxCaption");

  document.querySelectorAll("[data-lightbox-src]").forEach(function (button) {
    button.addEventListener("click", function () {
      if (!lightbox) return;
      lightboxImage.src = button.dataset.lightboxSrc;
      lightboxImage.alt = button.dataset.lightboxAlt || "";
      lightboxCaption.textContent = button.dataset.lightboxAlt || "";
      lightbox.showModal();
    });
  });

  if (lightbox) {
    lightbox.querySelector(".lightbox-close").addEventListener("click", function () {
      lightbox.close();
    });
    lightbox.addEventListener("click", function (event) {
      if (event.target === lightbox || event.target.classList.contains("lightbox-stage")) {
        lightbox.close();
      }
    });
  }

  var progress = document.getElementById("readingProgress");
  function updateProgress() {
    if (!progress) return;
    var max = document.documentElement.scrollHeight - window.innerHeight;
    var value = max > 0 ? window.scrollY / max : 0;
    progress.style.transform = "scaleX(" + Math.min(1, Math.max(0, value)) + ")";
  }
  updateProgress();
  window.addEventListener("scroll", updateProgress, { passive: true });
  window.addEventListener("resize", updateProgress);

  var navLinks = Array.from(document.querySelectorAll(".chapter-nav a"));
  var sections = navLinks.map(function (link) {
    return document.querySelector(link.getAttribute("href"));
  }).filter(Boolean);

  if ("IntersectionObserver" in window) {
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        navLinks.forEach(function (link) {
          link.classList.toggle("active", link.getAttribute("href") === "#" + entry.target.id);
        });
      });
    }, { rootMargin: "-20% 0px -68% 0px" });
    sections.forEach(function (section) { observer.observe(section); });
  }
})();
