// Responsibility: Split one assistant turn into the pieces the product renders differently.
// Owns: the two block markers the server writes into a reply, and where each block ends.
// Boundaries: pure - a string in, strings out. No DOM, no fetch, no state.

/* THE ASSISTANT TURN IS NOT ALL ONE THING.
 *
 * The server prepends the Surveyor's receipt to the model's words, and on the confirmation
 * turn it splices the geometry agent's own caveat into the middle of them. Both arrived as
 * plain text in the chat bubble, so the measurement, the vision model's reading and a warning
 * about the plan all read as the same paragraph in the same voice.
 *
 * This is the one place that knows where those blocks begin and end. It never DROPS a
 * character: every line of the input lands in exactly one segment, and a block whose shape is
 * not recognised stays in the prose where it was, visible, rather than being swallowed.
 */

/** The Surveyor's receipt, written by agents/intake/geometry_brief.surveyor_panel. */
const SURVEYOR_MARK = "- - - THE SURVEYOR - - -";

/** The geometry agent's own words to the customer, spliced into the confirmation turn by
 *  agents/intake/executor - what it concluded, what it assumed, and what it still needs. */
const AGENT_MARK = "WHAT THE GEOMETRY AGENT FOUND";

/** The sentence that follows the caveat on the confirmation turn. It is the QUESTION being
 *  asked, not part of the warning, so it goes back to the prose. */
const PROCEED = /^Shall I proceed with mesh generation\?/;

/** A line inside the Surveyor's block: blank, an indented value, or an ALL-CAPS section head
 *  at column zero (optionally carrying the section's parenthetical note).
 *
 *  Prose never matches: the model writes sentences, which carry lower-case letters and are not
 *  indented by two spaces. An unrecognised head therefore ENDS the block early and the rest is
 *  rendered as prose - wrong-looking, but nothing is lost, which is the trade this makes.
 *
 *  The head must be at least FOUR characters, which is "SEEN" and is deliberately longer than the
 *  two-letter shouts a model writes on their own line - "OK" on the line after the panel would
 *  otherwise be swallowed into it as an empty section. */
function insidePanel(line) {
  return line.trim() === ""
    || /^ {2,}\S/.test(line)
    || /^[A-Z][A-Z0-9 ]{2,}[A-Z0-9](\s*\(.*\))?\s*$/.test(line);
}

function trimBlank(lines) {
  let a = 0, b = lines.length;
  while (a < b && !lines[a].trim()) a++;
  while (b > a && !lines[b - 1].trim()) b--;
  return lines.slice(a, b);
}

/** One assistant turn as an ordered list of `{kind, text}`, kind being "prose", "surveyor" or
 *  "caveat". Order is document order, so the caveat still sits where the server put it: after
 *  the requirements and before the question it qualifies. */
export function segments(text) {
  const lines = String(text == null ? "" : text).split("\n");
  const out = [];
  let prose = [];

  const flush = () => {
    const body = trimBlank(prose).join("\n");
    if (body.trim()) out.push({ kind: "prose", text: body });
    prose = [];
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    if (line.trim() === SURVEYOR_MARK) {
      let j = i + 1;
      const body = [];
      while (j < lines.length && insidePanel(lines[j])) body.push(lines[j++]);
      const block = trimBlank(body).join("\n");
      if (block.trim()) {
        flush();
        out.push({ kind: "surveyor", text: block });
        i = j - 1;
        continue;
      }
      // A marker with nothing under it is not a panel. Keep the line as prose rather than
      // deleting a line the server chose to send.
      prose.push(line);
      continue;
    }

    if (line.trim() === AGENT_MARK) {
      let j = i + 1;
      const body = [];
      while (j < lines.length && !PROCEED.test(lines[j].trim())) body.push(lines[j++]);
      const block = trimBlank(body).join("\n");
      if (block.trim()) {
        flush();
        out.push({ kind: "caveat", text: block });
        i = j - 1;
        continue;
      }
      prose.push(line);
      continue;
    }

    prose.push(line);
  }
  flush();
  return out;
}
