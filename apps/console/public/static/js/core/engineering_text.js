// Responsibility: Show engineering text the way an engineer prints it - math markup as Unicode, numbers kept with their units.
// Owns: the console's mirror of the server normaliser, and the no-break joins between a number and its unit.
// Boundaries: pure functions - no DOM, no network, no state. Every rule mirrors
// src/meshpipeline/contracts/engineering_text.py; tests/fixtures/engineering_text_cases.json holds both to the same cases.

/* The server already makes a model's reply plain before it is stored (\(y^+=30\text{–}300\) is
 * stored as "y⁺ = 30–300"). This pass is the same rule again for text that was stored BEFORE
 * that - an old conversation reopened from history - and it adds the one thing that only matters
 * on screen: a number and its unit never break across a line. It is a mirror, not a math
 * renderer: no library, no layout, just the same conservative substitutions. Code - `patch`
 * and file names, links, paths - is never touched. */

const WS = " \t\n\r\f\v";
const NNBSP = "\u202f";     // LaTeX's \, - a thin space that never breaks a line
const NBSP = "\u00a0";

const SUP = new Map(Object.entries({
  "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸",
  "9": "⁹", "+": "⁺", "-": "⁻", "−": "⁻", "=": "⁼", "(": "⁽", ")": "⁾",
  "a": "ᵃ", "b": "ᵇ", "c": "ᶜ", "d": "ᵈ", "e": "ᵉ", "f": "ᶠ", "g": "ᵍ", "h": "ʰ", "i": "ⁱ",
  "j": "ʲ", "k": "ᵏ", "l": "ˡ", "m": "ᵐ", "n": "ⁿ", "o": "ᵒ", "p": "ᵖ", "r": "ʳ", "s": "ˢ",
  "t": "ᵗ", "u": "ᵘ", "v": "ᵛ", "w": "ʷ", "x": "ˣ", "y": "ʸ", "z": "ᶻ",
  "A": "ᴬ", "B": "ᴮ", "D": "ᴰ", "E": "ᴱ", "G": "ᴳ", "H": "ᴴ", "I": "ᴵ", "J": "ᴶ", "K": "ᴷ",
  "L": "ᴸ", "M": "ᴹ", "N": "ᴺ", "O": "ᴼ", "P": "ᴾ", "R": "ᴿ", "T": "ᵀ", "U": "ᵁ", "V": "ⱽ",
  "W": "ᵂ",
}));
const SUB = new Map(Object.entries({
  "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆", "7": "₇", "8": "₈",
  "9": "₉", "+": "₊", "-": "₋", "−": "₋", "=": "₌", "(": "₍", ")": "₎",
  "a": "ₐ", "e": "ₑ", "h": "ₕ", "i": "ᵢ", "j": "ⱼ", "k": "ₖ", "l": "ₗ", "m": "ₘ", "n": "ₙ",
  "o": "ₒ", "p": "ₚ", "r": "ᵣ", "s": "ₛ", "t": "ₜ", "u": "ᵤ", "v": "ᵥ", "x": "ₓ",
  "β": "ᵦ", "γ": "ᵧ", "ρ": "ᵨ", "φ": "ᵩ", "χ": "ᵪ",
}));
const SUP_AS_IS = new Set("*°′″‴†");
const SUB_AS_IS = new Set("∞*′");

const SYMBOLS = new Map(Object.entries({
  alpha: "α", beta: "β", gamma: "γ", delta: "δ", epsilon: "ε", varepsilon: "ε",
  zeta: "ζ", eta: "η", theta: "θ", vartheta: "ϑ", iota: "ι", kappa: "κ",
  lambda: "λ", mu: "μ", nu: "ν", xi: "ξ", pi: "π", rho: "ρ", varrho: "ρ",
  sigma: "σ", tau: "τ", upsilon: "υ", phi: "φ", varphi: "φ", chi: "χ", psi: "ψ",
  omega: "ω",
  Gamma: "Γ", Delta: "Δ", Theta: "Θ", Lambda: "Λ", Xi: "Ξ", Pi: "Π", Sigma: "Σ",
  Upsilon: "Υ", Phi: "Φ", Psi: "Ψ", Omega: "Ω",
  times: "×", cdot: "·", div: "÷", pm: "±", mp: "∓", ast: "*", star: "*",
  approx: "≈", simeq: "≃", sim: "~", cong: "≅", equiv: "≡", propto: "∝",
  ne: "≠", neq: "≠", le: "≤", leq: "≤", leqslant: "≤", ge: "≥", geq: "≥",
  geqslant: "≥", ll: "≪", gg: "≫", lt: "<", gt: ">",
  to: "→", rightarrow: "→", leftarrow: "←", Rightarrow: "⇒", leftrightarrow: "↔",
  infty: "∞", partial: "∂", nabla: "∇", sum: "∑", int: "∫",
  circ: "°", degree: "°", prime: "′", dagger: "†",
  ldots: "…", dots: "…", cdots: "…",
  quad: " ", qquad: " ", enspace: " ", thinspace: NNBSP,
  celsius: "°C", degreeCelsius: "°C", metre: "m", meter: "m", second: "s",
  kilogram: "kg", gram: "g", kelvin: "K", pascal: "Pa", newton: "N", joule: "J",
  watt: "W", hertz: "Hz", litre: "L", liter: "L", radian: "rad", percent: "%",
  kilo: "k", mega: "M", giga: "G", milli: "m", micro: "μ", nano: "n", centi: "c",
  per: "/", squared: "²", cubed: "³",
}));
const DROPPED = new Set([
  "left", "right", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr", "displaystyle",
  "textstyle", "scriptstyle", "limits", "nolimits", "rm", "bf", "it", "mathstrut", "strut"]);
const CONTROL_SYMBOLS = new Map(Object.entries({
  ",": NNBSP, ";": " ", ":": " ", ">": " ", " ": " ", "!": "",
  "%": "%", "_": "_", "#": "#", "&": "&", "$": "$", "{": "{", "}": "}",
}));
const TEXT_WRAPPERS = new Set([
  "text", "textrm", "textnormal", "textit", "textbf", "textsf", "texttt", "textup", "mbox", "emph"]);
const MATH_WRAPPERS = new Set([
  "mathrm", "mathit", "mathbf", "mathsf", "mathtt", "mathcal", "mathbb", "mathnormal",
  "boldsymbol", "bm", "operatorname", "si", "unit", "num"]);
const FRACTIONS = new Set(["frac", "dfrac", "tfrac"]);
const VULGAR = new Map(Object.entries({
  "1/2": "½", "1/3": "⅓", "2/3": "⅔", "1/4": "¼", "3/4": "¾", "1/5": "⅕", "1/6": "⅙", "1/8": "⅛"}));
const ROOTS = new Map([["", "√"], ["2", "√"], ["3", "∛"], ["4", "∜"]]);
const OPERATOR_NAMES = new Set([
  "sin", "cos", "tan", "sec", "csc", "cot", "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh",
  "exp", "log", "ln", "lg", "max", "min", "sup", "inf", "lim", "det", "arg", "dim", "gcd"]);
const QUANTITIES = new Set(["SI", "qty"]);
const ACCENTS = new Map(Object.entries({
  bar: "\u0304", overline: "\u0304", hat: "\u0302", widehat: "\u0302",
  tilde: "\u0303", widetilde: "\u0303", dot: "\u0307", ddot: "\u0308", vec: "\u20d7"}));

const GREEK = "\u0391-\u03a9\u03b1-\u03c9";
const SUPERSCRIPT_DIGITS = "\u2070\u00b9\u00b2\u00b3\u2074-\u2079";

const EXTENSIONS = "step|stp|stl|iges|igs|brep|x_t|sat|vtp|vtk|vtu|obj|ply|off|msh|unv|cgns|"
  + "foam|h5|json|yaml|yml|txt|csv|log|dat|py|sh|zip|gz|tar|png|jpe?g|3mf|gltf|glb";
// Never touched: code, links, paths and file names. Order matters - a fence before a backtick.
const PROTECTED_SOURCE = "```.*?```"
  + "|`[^`\\n]+`"
  + "|https?://[^ \\t\\n\\r\\f\\v<>()\\[\\]`]+"
  + "|(?<![A-Za-z])[A-Z]:\\\\[\\w .()-]+\\\\[^ \\t\\n\\r\\f\\v`]*"
  + "|(?<![\\w/~.])(?:~|\\.{1,2})?/(?:[\\w.-]+/)+[\\w.-]*"
  + "|(?<![\\w.-])[\\w-]+(?:\\.[\\w-]+)*\\.(?:" + EXTENSIONS + ")(?!\\w)";
const protectedRe = () => new RegExp(PROTECTED_SOURCE, "gsi");

const MATH_SPAN = new RegExp(
  "\\\\\\((?<inline>.+?)\\\\\\)"
  + "|\\\\\\[(?<display>.+?)\\\\\\]"
  + "|\\$\\$(?<block>.+?)\\$\\$"
  + "|(?<![\\\\$A-Za-z0-9])\\$(?=[^ \\t\\n$])(?<dollar>[^$\\n]{1,160}?)(?<=[^ \\t\\n\\\\])\\$(?![A-Za-z0-9$])",
  "gsu");     // u: count characters, not UTF-16 units, as Python does
const DOLLAR_IS_MATH = new RegExp("[\\\\^_=]|^[A-Za-z" + GREEK + "]{1,2}$");

const CONTROL = /\\(?:[A-Za-z]+|.)/ys;
const SIGNED_DIGITS = /[-−]?[0-9]+(?:\.[0-9]+)?/y;
const ATOM = /^[+\-−]?[^ \t\n+\-−×·/=<>≈≤≥^_()]+$/;
const SIMPLE = new RegExp("^[+\\-−]?[A-Za-z0-9." + GREEK + "]+$");

const RAISE_AFTER = new RegExp("^[A-Za-z0-9)\\]}°" + GREEK + "]$");
const LOWER_AFTER = new RegExp("(?:^|[^A-Za-z0-9_" + GREEK + "])[A-Za-z" + GREEK + "]{1,3}$");
const LETTER = /^[A-Za-z]/;
const ALNUM = /^[A-Za-z0-9]/;

const RELATION = /[ \t\n]*([=≈≃≅≡≤≥≠<>~≪≫∝→←⇒↔×])[ \t\n]*/g;
// an en dash, or a hyphen spaced on one side only, is a range; a hyphen spaced on both is a minus
const MATH_EN_DASH = /([0-9]) ?– ?([0-9])/g;
const MATH_LOPSIDED_HYPHEN = /([0-9])(?: -|- )([0-9])/g;
const NUMBER_END = new RegExp("^[0-9" + SUPERSCRIPT_DIGITS + "]$");
const SPACED_OPERATORS = new Set("+-−±");
const DEGREE_UNIT = /°[ \t]+([CFK])(?![A-Za-z])/g;
const SPACES = /[ \t\n]{2,}/g;

const Y_PLUS = /(?<![A-Za-z0-9_+\-])y\+(?![A-Za-z0-9_+])/g;
const K_MODEL = /(?<![A-Za-z0-9_])([kK])([-–])(omega|Omega|OMEGA|epsilon|Epsilon|EPSILON)(?![A-Za-z0-9_])/g;
const TIMES_TEN = new RegExp("([0-9]) ?[x*] ?(10[⁻⁺]?[" + SUPERSCRIPT_DIGITS + "]+)", "g");
// A hyphen between two plain numbers is a range - not a date, an id, a time, a file name or a NACA name.
const RANGE = /(?<![A-Za-z0-9_.,\-–/:#])(?<!NACA )([0-9]+(?:\.[0-9]+)?)-([0-9]+(?:\.[0-9]+)?)(?![A-Za-z0-9_\-–/]|[.,][0-9])/g;
const TRIGGER = /[\\^$_]|y\+|[kK][-–][OoEe]|[0-9]-[0-9]|[0-9] ?[x*] ?10/;

/** Model-written text as a person should read it: math markup rendered as Unicode, code untouched.
 *  The same answer as the server's `plain` for every input, and idempotent on what a model writes. */
export function plain(text) {
  if (typeof text !== "string" || !text || !TRIGGER.test(text)) return text;
  return eachFree(text, free);
}

/** `plain`, and then what only matters on screen: a number stays on the line with its unit
 *  (40 m/s, 15 °C, 32 mm), a × between numbers and a relation before a number do not strand
 *  their operands. For assistant text about to go through mdBlock. */
export function prettyText(text) {
  const t = plain(text);
  if (typeof t !== "string" || !t) return t;
  return eachFree(t, joinUnits);
}

// the no-break joins - the console's own; the server stores ordinary spaces
const UNIT = "(?:°C|°F|°|%|rpm|min|atm|rad|mol|[kMGmμµnc]?(?:m|s|Pa|N|W|J|Hz|g|K|L|bar)|h)";
const NUMBER_UNIT = new RegExp("([0-9" + SUPERSCRIPT_DIGITS + "])([ \\u2009])(?=" + UNIT + "(?![A-Za-z]))", "g");
const TIMES_BETWEEN = new RegExp("([0-9" + SUPERSCRIPT_DIGITS + "]) × (?=[0-9])", "g");
const RELATION_NUMBER = /([=≈≤≥<>~±]) (?=[0-9−-])/g;

function joinUnits(s) {
  return s.replace(NUMBER_UNIT, (_m, d, sp) => d + (sp === " " ? NBSP : NNBSP))
    .replace(TIMES_BETWEEN, "$1" + NBSP + "×" + NBSP)
    .replace(RELATION_NUMBER, "$1" + NBSP);
}

function eachFree(text, fn) {
  let out = "", pos = 0;
  for (const m of text.matchAll(protectedRe())) {
    out += fn(text.slice(pos, m.index)) + m[0];
    pos = m.index + m[0].length;
  }
  return out + fn(text.slice(pos));
}

function free(s) {
  if (!s) return s;
  let t = "", pos = 0;
  for (const m of s.matchAll(MATH_SPAN)) {
    let body = m.groups.inline || m.groups.display || m.groups.block;
    if (body === undefined) {
      body = m.groups.dollar;
      if (!DOLLAR_IS_MATH.test(body)) continue;       // money, not math
    }
    t += render(s.slice(pos, m.index), false) + math(body);
    pos = m.index + m[0].length;
  }
  t += render(s.slice(pos), false);
  t = t.replace(TIMES_TEN, "$1 × $2");
  t = t.replace(Y_PLUS, "y⁺");
  t = t.replace(K_MODEL, (_m, k, dash, word) => k + dash + (word.toLowerCase() === "omega" ? "ω" : "ε"));
  return t.replace(RANGE, "$1–$2");
}

function strip(s) {
  let a = 0, b = s.length;
  while (a < b && WS.includes(s[a])) a++;
  while (b > a && WS.includes(s[b - 1])) b--;
  return s.slice(a, b);
}

function math(body) {
  let t = render(body, true);
  t = t.replace(RELATION, " $1 ");
  t = t.replace(MATH_EN_DASH, "$1–$2");
  t = t.replace(MATH_LOPSIDED_HYPHEN, "$1-$2");
  t = t.replace(DEGREE_UNIT, "°$1");
  return strip(t.replace(SPACES, " "));
}

function render(src, isMath) {
  const out = [];
  let i = 0;
  const n = src.length;
  while (i < n) {
    const ch = src[i];
    let done = null;
    if (ch === "\\") done = command(src, i, isMath);
    else if (ch === "^" || ch === "_") done = script(src, i, isMath);
    else if (isMath && WS.includes(ch)) {
      const k = skipWs(src, i);
      done = [mathSpaceKept(last(out), i ? src[i - 1] : "", k < n ? src[k] : "") ? " " : "", k];
    } else if (isMath && (ch === "{" || ch === "}")) done = ["", i + 1];
    else if (isMath && (ch === "~" || ch === "&")) done = [" ", i + 1];
    if (done === null) { out.push(ch); i += 1; }
    else { out.push(done[0]); i = done[1]; }
  }
  return out.join("");
}

function mathSpaceKept(lastCh, before, after) {
  if (!lastCh || !after) return false;
  if (lastCh === "," || lastCh === ";") return true;
  if (NUMBER_END.test(lastCh) && (after === "\\" || LETTER.test(after))) return true;
  return SPACED_OPERATORS.has(before) || SPACED_OPERATORS.has(after);
}

function last(out) {
  for (let k = out.length - 1; k >= 0; k--) if (out[k]) return out[k][out[k].length - 1];
  return "";
}

function skipWs(src, j) {
  while (j < src.length && WS.includes(src[j])) j++;
  return j;
}

function control(src, j) {
  CONTROL.lastIndex = j;
  const m = CONTROL.exec(src);
  return m ? [m[0], j + m[0].length] : null;
}

function arg(src, j) {
  j = skipWs(src, j);
  if (j >= src.length || src[j] === "}") return null;
  if (src[j] === "{") {
    let depth = 0, k = j;
    while (k < src.length) {
      const c = src[k];
      if (c === "\\") { k += 2; continue; }
      if (c === "{") depth++;
      else if (c === "}") { depth--; if (depth === 0) return ["group", src.slice(j + 1, k), k + 1]; }
      k++;
    }
    return null;
  }
  if (src[j] === "\\") {
    const c = control(src, j);
    return c ? ["control", c[0], c[1]] : null;
  }
  // one CHARACTER, not one UTF-16 unit - an emoji is two units and one argument, as in Python
  const ch = String.fromCodePoint(src.codePointAt(j));
  return ["char", ch, j + ch.length];
}

function command(src, i, isMath) {
  const c = control(src, i);
  if (!c) return null;
  const word = c[0].slice(1);
  const j = c[1];
  if (!LETTER.test(word)) {
    if (CONTROL_SYMBOLS.has(word)) return [CONTROL_SYMBOLS.get(word), j];
    if (word === "\\") return [isMath ? " " : src.slice(i, j), j];
    return null;
  }
  const done = controlWord(src, word, j, isMath);
  // in math, a command that cannot be read (\frac with no arguments) reads as its name
  return done === null && isMath ? [word, j] : done;
}

function controlWord(src, word, j, isMath) {
  if (TEXT_WRAPPERS.has(word) || MATH_WRAPPERS.has(word)) {
    const a = arg(src, j);
    if (!a) return ["", j];
    return [render(a[1], MATH_WRAPPERS.has(word)), a[2]];
  }
  if (word === "textsuperscript" || word === "textsubscript") {
    const a = arg(src, j);
    if (!a) return null;
    const body = render(a[1], false);
    return [word === "textsuperscript" ? raise(body) : lower(body), a[2]];
  }
  if (FRACTIONS.has(word)) {
    const num = arg(src, j);
    const den = num ? arg(src, num[2]) : null;
    if (!num || !den) return null;
    const top = math(num[1]), bottom = math(den[1]);
    return [VULGAR.get(top + "/" + bottom) || operand(top) + "/" + operand(bottom), den[2]];
  }
  if (QUANTITIES.has(word)) {
    const value = arg(src, j);
    const unit = value ? arg(src, value[2]) : null;
    if (!value || !unit) return null;
    return [math(value[1]) + " " + math(unit[1]), unit[2]];
  }
  if (word === "sqrt") {
    let root = "√";
    const k = skipWs(src, j);
    if (k < src.length && src[k] === "[" && src.indexOf("]", k) !== -1) {
      const close = src.indexOf("]", k);
      const index = math(src.slice(k + 1, close));      // \sqrt[3]{8} is ∛8, never √8
      root = ROOTS.get(index) || raise(index) + "√";
      j = close + 1;
    }
    const a = arg(src, j);
    if (!a) return null;
    return [root + operand(math(a[1])), a[2]];
  }
  if (ACCENTS.has(word)) {
    const a = arg(src, j);
    if (!a) return null;
    const mark = ACCENTS.get(word);
    return [Array.from(math(a[1]), (ch) => (WS.includes(ch) ? ch : ch + mark)).join(""), a[2]];
  }
  if (SYMBOLS.has(word)) return [SYMBOLS.get(word), isMath ? skipWs(src, j) : j];
  if (DROPPED.has(word)) return ["", isMath ? skipWs(src, j) : j];
  if (isMath) {
    const k = skipWs(src, j);
    if (OPERATOR_NAMES.has(word) && k < src.length && (src[k] === "\\" || ALNUM.test(src[k]))) return [word + " ", k];
    return [word, j];
  }
  return null;
}

function script(src, i, isMath) {
  const op = src[i];
  if (!isMath) {
    if (op === "^" && !(i > 0 && RAISE_AFTER.test(src[i - 1]))) return null;
    if (op === "_" && !(i > 0 && (src[i - 1] === "}" || LOWER_AFTER.test(src.slice(Math.max(0, i - 4), i))))) return null;
  }
  SIGNED_DIGITS.lastIndex = i + 1;
  const digits = SIGNED_DIGITS.exec(src);
  if (digits && !(!isMath && op === "_")) {
    return [op === "^" ? raise(digits[0]) : lower(digits[0]), i + 1 + digits[0].length];
  }
  const a = arg(src, i + 1);
  if (!a) return null;
  const [kind, raw, end] = a;
  if (!isMath) {
    if (kind === "group" && (Array.from(raw).length > 24 || raw.includes("\n"))) return null;
    if (kind === "control" && !SYMBOLS.has(raw.slice(1))) return null;
    if (kind === "char") {
      if (op === "_") return null;
      if (!"+-*".includes(raw) && !(LETTER.test(raw) && !(end < src.length && LETTER.test(src[end])))) return null;
    }
  }
  const body = kind !== "char" ? math(raw) : raw;
  return [op === "^" ? raise(body) : lower(body), end];
}

function mapped(body, table, asIs, mark) {
  body = strip(body);
  if (!body) return "";
  const chars = Array.from(body);
  if (chars.every((c) => asIs.has(c))) return body;
  if (chars.every((c) => table.has(c))) return chars.map((c) => table.get(c)).join("");
  return mark + (SIMPLE.test(body) ? body : "(" + body + ")");
}

function raise(body) { return mapped(body, SUP, SUP_AS_IS, "^"); }
function lower(body) { return mapped(body, SUB, SUB_AS_IS, "_"); }

function operand(r) {
  r = strip(r);
  return ATOM.test(r) ? r : "(" + r + ")";
}
