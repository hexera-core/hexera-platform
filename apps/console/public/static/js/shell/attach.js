// Responsibility: Let the user attach pictures and text files to the chat box and the notes box - the paper-clip,
//   Ctrl+V of a picture, a drop on the box - upload each one and show it as a chip; and open what was sent.
// Boundaries: the chip row itself is shell/chips.js; the send (composer.js, render/stage.js) only reads its payload.

/* ATTACHING, JUST LIKE A CHAT APP. Each box gets one paper-clip button and nothing else until something is attached.
 *
 *   wire(name, {input, row, msg})  - the box `name` ("chat" | "notes"): `input` its textarea, `row` the element the
 *                                    clip goes into (before the textarea), `msg` the one plain line under the box.
 *   idle(name)                     - resolves once no file in that box is still uploading (the send waits on it).
 *   mountAttach()                  - once, from the composer: the chat box, the notes box each time the plan card
 *                                    draws it again, and the sent pictures/files in the conversation and the notes.
 *
 * A sent picture or file is drawn by the renderer as `[data-att]` markup (render/stage.js attHtml): this module fills
 * a picture's thumbnail and opens the stored file on a click. The route is owner-scoped, so the bytes are fetched
 * with the session's headers as an object URL (api/endpoints.js attachmentBlobUrl), once per file.
 *
 * Errors are one plain line under the box. The server checks every file again by its bytes; its line is shown as is.
 */
import { attachmentBlobUrl, uploadAttachment } from "../api/endpoints.js";
import { get as getState } from "../core/state.js";
import * as chips from "./chips.js";

export const MAX_FILES = 5;
const MB = 1024 * 1024;
const IMAGE_MAX = 10 * MB;
const FILE_MAX = 20 * MB;
const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);
const FILE_TYPES = new Set(["application/pdf", "text/plain", "text/markdown", "text/csv", "text/x-markdown"]);
const IMAGE_SUFFIXES = new Set([".png", ".jpg", ".jpeg", ".webp"]);
const FILE_SUFFIXES = new Set([".pdf", ".txt", ".md", ".csv"]);
const ACCEPT = "image/png,image/jpeg,image/webp,application/pdf,text/plain,text/markdown,text/csv,.png,.jpg,.jpeg,"
  + ".webp,.pdf,.txt,.md,.csv";

export const TIP = "Attach a picture or a file. It is sent to the AI with your words.";
export const TOO_MANY = `Only ${MAX_FILES} files per message.`;
export const NOT_THIS_KIND = "That kind of file cannot be attached. Pictures, PDF and text files can.";
export const TOO_LARGE_FILE = "That file is too large: 20 MB is the most.";
export const TOO_LARGE_PICTURE = "That picture is too large: 10 MB is the most.";

const CLIP_SVG = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" '
  + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 11.5l-8.6 8.6a5 5 0 0 1-7.1-7.1'
  + 'l8.6-8.6a3.4 3.4 0 0 1 4.8 4.8l-8.6 8.6a1.7 1.7 0 0 1-2.4-2.4l7.9-7.9"/></svg>';

/** "image" | "file" | null - what a picked, pasted or dropped file is, by its type or its name. Pure. */
export function kindOf(file) {
  const type = String((file && file.type) || "").toLowerCase();
  const name = String((file && file.name) || "").toLowerCase();
  const dot = name.lastIndexOf(".");
  const suffix = dot < 0 ? "" : name.slice(dot);
  if (IMAGE_TYPES.has(type) || (!type && IMAGE_SUFFIXES.has(suffix))) return "image";
  if (FILE_TYPES.has(type) || FILE_SUFFIXES.has(suffix)) return "file";
  return null;
}

/** The plain line that refuses `file` here before it is sent, or "". Pure. */
export function refusal(file) {
  const kind = kindOf(file);
  if (!kind) return NOT_THIS_KIND;
  if (kind === "image" && file.size > IMAGE_MAX) return TOO_LARGE_PICTURE;
  if (kind === "file" && file.size > FILE_MAX) return TOO_LARGE_FILE;
  return "";
}

const _wired = new WeakSet();
const _waiters = new Map();     // box -> [resolve...] waiting for its uploads to finish
const _msg = new Map();         // box -> its message line

/** The one plain line under the box `name` ("" clears it). */
export function say(name, line) {
  const m = _msg.get(name);
  if (m && m.isConnected) m.textContent = line || "";
}

function settle(name) {
  if (chips.busy(name)) return;
  const w = _waiters.get(name) || [];
  _waiters.delete(name);
  w.forEach((r) => r());
}

export function idle(name) {
  if (!chips.busy(name)) return Promise.resolve();
  return new Promise((r) => { const w = _waiters.get(name) || []; w.push(r); _waiters.set(name, w); });
}

function attached(name) { return chips.items(name).filter((i) => i.kind === "image" || i.kind === "file").length; }

/** Take `files` into the box `name`: each one checked here, then uploaded behind its own chip. */
export function take(name, files) {
  const list = Array.from(files || []);
  if (!list.length) return;
  const sessionId = getState.sessionId();
  if (!sessionId) return;
  let line = "";
  for (const file of list) {
    const why = refusal(file);
    if (why) { line = why; continue; }
    if (attached(name) >= MAX_FILES) { line = TOO_MANY; break; }
    const kind = kindOf(file);
    const thumb = kind === "image" ? URL.createObjectURL(file) : null;
    const label = kind === "image" ? "" : (file.name || "file");
    const key = chips.add(name, {
      kind, label, thumb, title: file.name || "", pending: true,
      onRemove: () => { if (thumb) URL.revokeObjectURL(thumb); },
    });
    uploadAttachment(sessionId, file).then((row) => {
      chips.update(name, key, { pending: false, value: row.id, row,
        label: kind === "image" ? "" : fileLabel(row), title: row.name || file.name || "" });
    }, (e) => {
      const it = chips.items(name).find((i) => i.key === key);
      if (it) { if (thumb) URL.revokeObjectURL(thumb); it.onRemove = null; chips.remove(name, key); }
      say(name, (e && e.message) || "That file could not be attached. Try again.");
    }).finally(() => settle(name));
  }
  say(name, line);
}

/** A file's chip: its name, and its pages when it has them ("spec.pdf · 12 pages"). Pure. */
export function fileLabel(row) {
  const n = row && row.pages;
  return String((row && row.name) || "file") + (n ? ` · ${n} page${n === 1 ? "" : "s"}` : "");
}

/** The rows of what the box is about to send, for the user's own bubble ({id, kind, name}). */
export function sentRows(name) {
  return chips.items(name).filter((i) => i.row && !i.pending).map((i) => ({ id: i.row.id, kind: i.row.kind,
    name: i.row.name, pages: i.row.pages }));
}

/** The box `name` takes attachments: the clip before `input` inside `row`, paste and drop on the box. */
export function wire(name, { input, row, msg = null }) {
  if (!input || !row || _wired.has(input)) return;
  _wired.add(input);
  if (msg) _msg.set(name, msg);
  const picker = document.createElement("input");
  picker.type = "file"; picker.multiple = true; picker.accept = ACCEPT; picker.hidden = true;
  picker.className = "attach-picker";
  const clip = document.createElement("button");
  clip.type = "button"; clip.className = "attach-clip"; clip.title = TIP;
  clip.setAttribute("aria-label", "Attach a picture or a file");
  clip.innerHTML = CLIP_SVG;
  clip.disabled = input.disabled;
  // the clip is open exactly when the box is (the chat box is closed while a run goes or the stage is up)
  new MutationObserver(() => { clip.disabled = input.disabled; }).observe(input, { attributes: true,
    attributeFilter: ["disabled"] });
  clip.onclick = () => { if (!input.disabled) { chips.setActive(name); picker.click(); } };
  picker.onchange = () => { take(name, picker.files); picker.value = ""; input.focus(); };
  row.insertBefore(picker, input);
  row.insertBefore(clip, input);
  input.addEventListener("input", () => say(name, ""));
  // Ctrl+V: a picture on the clipboard is attached; words are pasted as words (a copied table pastes as text)
  input.addEventListener("paste", (e) => {
    const dt = e.clipboardData;
    if (!dt || input.disabled) return;
    if (String(dt.getData("text/plain") || "").trim()) return;
    const files = Array.from(dt.files || []);
    if (!files.length) return;
    e.preventDefault();
    take(name, files);
  });
  // A DROP ON THE BOX is an attachment, never the CAD upload the page's own drop handler takes (the box is open only
  // once the session has its geometry, so a CAD file here could only be refused there): whatever it is, it stops here
  // and gets this box's own line.
  const zone = row.parentElement || row;
  const ours = (dt) => !!dt && Array.from(dt.items || []).some((i) => i.kind === "file")
    && !input.disabled;
  zone.addEventListener("dragover", (e) => {
    if (!ours(e.dataTransfer)) return;
    e.preventDefault(); e.stopPropagation();
    document.body.classList.remove("dragover"); zone.classList.add("attach-over");
  });
  zone.addEventListener("dragleave", (e) => { if (!zone.contains(e.relatedTarget)) zone.classList.remove("attach-over"); });
  zone.addEventListener("drop", (e) => {
    zone.classList.remove("attach-over");
    const files = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
    if (input.disabled || !files.length) return;
    e.preventDefault(); e.stopPropagation();
    document.body.classList.remove("dragover");
    chips.setActive(name);
    take(name, files);
  });
}

/* WHAT WAS SENT: a picture's thumbnail and a click that opens the stored file. One object URL per file. */
const _urls = new Map();

function blobUrl(sid, id) {
  const k = sid + "/" + id;
  if (!_urls.has(k)) _urls.set(k, attachmentBlobUrl(sid, id).catch((e) => { _urls.delete(k); throw e; }));
  return _urls.get(k);
}

export function fill(root) {
  if (!root || !root.querySelectorAll) return;
  root.querySelectorAll("[data-att]:not([data-att-on])").forEach((a) => {
    a.setAttribute("data-att-on", "1");
    const sid = a.getAttribute("data-sid") || getState.sessionId();
    const id = a.getAttribute("data-att");
    if (!sid || !id) return;
    const img = a.querySelector("img");
    if (img) blobUrl(sid, id).then((u) => { img.src = u; }, () => { a.classList.add("att-gone"); });
    a.addEventListener("click", (e) => {
      e.preventDefault();
      blobUrl(sid, id).then((u) => window.open(u, "_blank", "noopener"), () => {});
    });
  });
}

function wireNotes(root) {
  root.querySelectorAll(".rc-notes-in").forEach((inp) => {
    const box = inp.closest(".rc-notes-box");
    wire("notes", { input: inp, row: inp.parentElement, msg: box && box.querySelector(".rc-notes-msg") });
  });
}

let _mounted = false;
export function mountAttach() {
  if (_mounted) return;
  _mounted = true;
  const chat = document.getElementById("chat-input");
  const inner = document.getElementById("input-inner");
  if (chat && inner) {
    // the chat box's one plain line, under its chip row
    const line = document.createElement("div");
    line.className = "box-msg"; line.setAttribute("role", "status");
    const bar = inner.parentElement;
    bar.appendChild(line);
    wire("chat", { input: chat, row: inner, msg: line });
  }
  const stage = document.getElementById("stage");
  if (!stage) return;
  const look = () => { wireNotes(stage); fill(stage); };
  // the conversation and the plan card are drawn by the renderer at any time: what it draws is filled as it lands
  new MutationObserver(look).observe(stage, { childList: true, subtree: true });
  look();
}
