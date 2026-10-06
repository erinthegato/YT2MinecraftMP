/* app.js - watch the job the page was sent to, and show what happens.
 *
 * The page is perfectly usable without this: it renders the current state
 * server-side and there is a refresh link.  This only saves refreshing, by
 * asking the JSON endpoint every so often until the file is ready.
 */
(function () {
  "use strict";

  var box = document.getElementById("job");
  if (!box) {
    return;
  }

  var statusUrl = box.getAttribute("data-status-url");
  var statusLine = document.getElementById("status");
  var fill = document.getElementById("fill");
  var download = document.getElementById("download");
  var downloadLink = document.getElementById("download-link");
  var errorBox = document.getElementById("error");
  var duration = document.getElementById("fact-duration");
  var size = document.getElementById("fact-size");
  var timer = null;

  function show(element, visible) {
    if (element) {
      element.classList.toggle("hidden", !visible);
    }
  }

  function stop() {
    if (timer) {
      window.clearInterval(timer);
      timer = null;
    }
  }

  function render(data) {
    if (statusLine) {
      statusLine.textContent = data.message || data.status;
    }
    if (fill) {
      fill.style.width = (data.percent || 0) + "%";
    }
    if (duration && data.duration_text) {
      duration.textContent = data.duration_text;
    }
    if (size && data.size_text) {
      size.textContent = data.size_text;
    }

    if (data.status === "done") {
      if (downloadLink && data.download_url) {
        downloadLink.href = data.download_url;
      }
      show(download, true);
      stop();
    } else if (data.status === "error") {
      if (errorBox) {
        errorBox.textContent = data.error || "the conversion failed";
      }
      show(errorBox, true);
      stop();
    }
  }

  function poll() {
    window
      .fetch(statusUrl, { headers: { Accept: "application/json" } })
      .then(function (response) {
        return response.json();
      })
      .then(render)
      .catch(function () {
        /* a blip in the network - the next tick tries again */
      });
  }

  poll();
  timer = window.setInterval(poll, 800);
})();
