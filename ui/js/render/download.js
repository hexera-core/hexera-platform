// Responsibility: Start a deliverable's download from a link the API mints at click time, and try
//                 once more with a new link if the store refuses that download.
// Boundaries: it asks the API for links and hands them to the browser; it draws no panel.

/* WHY A DOWNLOAD BUTTON DOES NOT JUST FOLLOW ITS HREF. The links in the job read are signed when the
   page loads, and a signed link dies. A user who read the result for longer than that, or whose
   900 MB FEA deck dropped and resumed later, got a 403 from the store instead of their file. So a
   click asks the API for a NEW link (its life scales with the file's size), and hands that to the
   browser. The href stays as it was: it is what a middle-click or "copy link" takes, and what the
   click falls back to if the API cannot be reached at all.

   HOW A REFUSED DOWNLOAD IS NOTICED. The link is opened in a hidden frame and served as an
   attachment, so a download that starts never loads a page in the frame - the browser hands it to
   its download manager and the frame stays on about:blank. A refused or expired link loads the
   store's error page instead, and that `load` is the signal to fetch a new link and try once more. */

import { downloadLink } from "../api/endpoints.js";

/** How long after starting a download a page in its frame still counts as a refusal. The store
 *  answers a refused link at once; the window only stops a very late event being misread. */
export const REFUSAL_WATCH_MS = 120000;
/** How long a download's frame is kept. A download the browser took over does not need it. */
const FRAME_KEEP_MS = 3600000;

const safeId = (v) => String(v == null ? "" : v).replace(/[^A-Za-z0-9_-]/g, "");

/** The attributes that make a download button ask for a fresh link: empty when the row carries
 *  no id (an older API), so the button simply follows its href as it always did. */
export function downloadAttrs(job, file) {
  const id = safeId(file && file.id);
  const jobId = safeId(job);
  return id && jobId ? ` data-job="${jobId}" data-artifact-id="${id}"` : "";
}

/** A new link for one deliverable; asked once more if the first ask fails on the way (the
 *  network, a server error). A refusal that will not change - not signed in, not found - is not
 *  asked again. */
export async function freshLink(job, artifactId, { fetchLink = downloadLink } = {}) {
  try {
    return await fetchLink(job, artifactId);
  } catch (e) {
    if (e && [401, 403, 404].includes(e.status)) throw e;
    return fetchLink(job, artifactId);
  }
}

/** Hand `url` to the browser through a hidden frame. `onRefused` runs if the frame loads a page -
 *  the store's refusal - instead of handing the file to the download manager. */
export function startDownload(url, { onRefused, doc = globalThis.document,
                                     watchMs = REFUSAL_WATCH_MS } = {}) {
  const frame = doc.createElement("iframe");
  frame.hidden = true;
  frame.setAttribute("aria-hidden", "true");
  frame.className = "dl-frame";
  doc.body.appendChild(frame);
  let watching = true;
  const stop = setTimeout(() => { watching = false; }, watchMs);
  frame.addEventListener("load", () => {
    // A frame that is still on about:blank has not navigated (a browser may announce the blank
    // page it starts on); one whose page cannot be read has loaded the store's answer.
    try {
      const href = frame.contentWindow && frame.contentWindow.location.href;
      if (!href || href === "about:blank") return;
    } catch { /* another origin's page: the store answered with a page, not a file */ }
    if (!watching) return;
    watching = false;
    clearTimeout(stop);
    if (onRefused) onRefused();
  });
  frame.src = url;
  setTimeout(() => frame.remove(), FRAME_KEEP_MS);
  return frame;
}

/** One Download click: a fresh link, then the download; if the store refuses it, one more fresh
 *  link and one more try. `fallback` runs when no link could be had at all. Resolves to what
 *  happened, and `retry` (when the first try was refused) to the second attempt. */
export async function downloadFresh(job, artifactId,
                                    { fetchLink = downloadLink, start = startDownload,
                                      fallback } = {}) {
  let link;
  try {
    link = await freshLink(job, artifactId, { fetchLink });
  } catch (error) {
    if (fallback) fallback();
    return { started: false, error };
  }
  const outcome = { started: true, retried: false, retry: null };
  start(link.download_url, {
    onRefused: () => {
      if (outcome.retried) return outcome.retry;
      outcome.retried = true;
      outcome.retry = freshLink(job, artifactId, { fetchLink })
        .then((again) => { start(again.download_url, {}); return true; })
        .catch(() => false);
      return outcome.retry;
    },
  });
  return outcome;
}

/** Make every download button under `root` that carries `downloadAttrs` fetch its link at click
 *  time. Idempotent: binding the same panel twice still downloads once per click. */
export function bindFreshDownloads(root, deps = {}) {
  if (!root || root.__freshDownloads) return;
  root.__freshDownloads = true;
  root.addEventListener("click", (e) => {
    const a = e.target && e.target.closest ? e.target.closest("a[data-artifact-id]") : null;
    if (!a || !root.contains(a)) return;
    e.preventDefault();
    const href = a.getAttribute("href") || "";
    downloadFresh(a.dataset.job, a.dataset.artifactId, {
      ...deps,
      fallback: deps.fallback || (() => {
        if (href && href !== "#") globalThis.open(href, "_blank", "noopener");
      }),
    });
  });
}
