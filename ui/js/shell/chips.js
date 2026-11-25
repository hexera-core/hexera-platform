// Responsibility: Hold the chips under the chat box and the notes box - the files attached and the faces picked - and draw them.
// Boundaries: state and DOM for the chip row only; uploading a file (shell/attach.js) and picking a face (viewer/face_pick.js) add chips here.

/* THE CHIP ROW UNDER EACH BOX. One row per box ("chat", "notes"), empty and invisible until something is attached
 * or picked. The row's state lives here, not in the DOM: the notes box is redrawn with the plan card, and its chips
 * must survive that, so mount() draws the row again from what is held.
 *
 *   add(box, {kind: "image" | "file" | "face", label, value, thumb?, title?, pending?, onRemove?}) -> key
 *   update(box, key, patch), remove(box, key), clear(box), items(box)
 *   payload(box) -> {attachments: [id...], faces: [{id, label}...]}  (what the send carries)
 *   busy(box) -> a chip is still uploading (the send waits)
 *   active() / setActive(box): the box the user used last - a picked face goes under it (the chat box by default)
 *   listen(fn) -> unlisten: fn(box, items) hears EVERY change of any row - add, update, remove and clear (a clear is
 *                silent only to the chips' own onRemove) - so a picked face can be un-lit when its chip goes
 */

const _boxes = new Map();
let _seq = 0;
let _active = "chat";
const _listeners = new Set();

function box(name) {
  if (!_boxes.has(name)) _boxes.set(name, { items: [], host: null, onChange: null });
  return _boxes.get(name);
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function draw(name) {
  const b = box(name);
  const host = b.host;
  if (host && host.isConnected) {
    host.replaceChildren();
    host.hidden = !b.items.length;
    for (const it of b.items) {
      const chip = el("span", "box-chip box-chip-" + it.kind + (it.pending ? " is-pending" : ""));
      if (it.title) chip.title = it.title;
      if (it.thumb) {
        const img = el("img", "box-chip-thumb");
        img.src = it.thumb; img.alt = "";
        chip.appendChild(img);
      }
      if (it.label) chip.appendChild(el("span", "box-chip-label", it.label));
      const x = el("button", "box-chip-x", "✕");
      x.type = "button";
      x.setAttribute("aria-label", "Remove " + (it.label || "this"));
      x.onclick = () => remove(name, it.key);
      chip.appendChild(x);
      host.appendChild(chip);
    }
  }
  if (b.onChange) b.onChange(items(name));
  for (const fn of _listeners) { try { fn(name, items(name)); } catch (e) { console.warn("chips listener", e); } }
}

export function listen(fn) { _listeners.add(fn); return () => _listeners.delete(fn); }

/** Put the row right after `anchor` (the box's row) - again after a redraw, with the chips it holds. `input` is the
 *  box's textarea: using it makes this the active box. `onChange(items)` hears every change (one listener per box,
 *  replaced on each mount). Returns the row. */
export function mount(name, anchor, { input = null, onChange = null } = {}) {
  const b = box(name);
  let host = b.host;
  if (!host || !host.isConnected || host.previousElementSibling !== anchor) {
    host = el("div", "box-chips");
    host.dataset.box = name;
    anchor.insertAdjacentElement("afterend", host);
    b.host = host;
  }
  b.onChange = onChange;
  if (input) input.addEventListener("focus", () => setActive(name));
  draw(name);
  return host;
}

export function add(name, chip) {
  const key = "c" + (++_seq);
  box(name).items.push({ ...chip, key });
  draw(name);
  return key;
}

export function update(name, key, patch) {
  const it = box(name).items.find((i) => i.key === key);
  if (!it) return;
  Object.assign(it, patch);
  draw(name);
}

export function remove(name, key) {
  const b = box(name);
  const it = b.items.find((i) => i.key === key);
  if (!it) return;
  b.items = b.items.filter((i) => i !== it);
  if (it.onRemove) it.onRemove(it);
  draw(name);
}

/** Empty the row (after a send). `silent`: the chips' onRemove is not called - what was sent stays sent. `keys`: only
 *  these chips go (the ones the send carried; a face picked while the reply was on its way stays for the next one). */
export function clear(name, { silent = true, keys = null } = {}) {
  const b = box(name);
  const only = keys ? new Set(keys) : null;
  const gone = only ? b.items.filter((i) => only.has(i.key)) : b.items;
  b.items = only ? b.items.filter((i) => !only.has(i.key)) : [];
  if (!silent) gone.forEach((it) => it.onRemove && it.onRemove(it));
  draw(name);
}

export function items(name) { return box(name).items.slice(); }

export function busy(name) { return box(name).items.some((i) => i.pending); }

export function payload(name) {
  const its = box(name).items.filter((i) => !i.pending && i.value != null);
  return {
    attachments: its.filter((i) => i.kind === "image" || i.kind === "file").map((i) => i.value),
    faces: its.filter((i) => i.kind === "face").map((i) => i.value),
  };
}

export function active() { return _active; }
export function setActive(name) { _active = name; }
