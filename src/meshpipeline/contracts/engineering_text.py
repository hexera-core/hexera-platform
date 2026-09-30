# Responsibility: Turn the math markup a model writes into the plain engineering text a person reads.
# Owns: the LaTeX-to-Unicode tables, the superscript and subscript alphabets, and what is never touched.
# Boundaries: pure text in, text out - no I/O, no imports beyond `re`; ui/js/core/engineering_text.js
# mirrors every rule here, and tests/fixtures/engineering_text_cases.json holds both to the same cases.
"""Engineering text for people.

The intake model writes LaTeX - ``\\(y^+=30\\text{–}300\\)`` - and the chat has no math renderer
(nor should it pull one in), so the markup reached the user raw. This turns the markup a model
actually produces into the Unicode an engineer writes by hand, ``y⁺ = 30–300``, and leaves
everything else exactly as written.

It is conservative by construction:

* Code is never touched. Fenced blocks, `inline code` (patch and file names), URLs, file names with
  a CAD or data extension and file-system paths pass through byte for byte.
* Inside math delimiters - ``\\( \\)``, ``\\[ \\]``, ``$$ $$`` and a ``$...$`` that is plainly math
  rather than money - the markup is rendered: commands, superscripts, subscripts, ``\\text``.
* Outside them only markup that cannot be anything else changes: a KNOWN backslash command, a caret
  superscript (``10^5``, ``m^2``, ``y^+``), a braced subscript after a short symbol (``U_{\\infty}``),
  ``y+``, ``k-omega`` / ``k-epsilon`` and a hyphen between two plain numbers (``30-300``). A bare
  ``_x`` is an identifier (``inlet_1``) and stays.
* What has no Unicode form stays readable rather than vanishing: ``C_{D}`` becomes ``C_D`` and
  ``10^{0.8}`` becomes ``10^0.8``.

Patterns are ASCII-only (``re.ASCII``), spell whitespace out rather than use ``\\s``, end on
``\\Z`` and keep every lookbehind fixed-width, so the JavaScript mirror compiles the same
expressions (``$`` for ``\\Z``) and gives the same answer.
"""
from __future__ import annotations

import re
from typing import Final

__all__ = ["plain"]

_WS: Final = " \t\n\r\f\v"
_NNBSP: Final = "\u202f"      # LaTeX's \, - a thin space that, like \, itself, never breaks a line

_SUP: Final[dict[str, str]] = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸",
    "9": "⁹", "+": "⁺", "-": "⁻", "−": "⁻", "=": "⁼", "(": "⁽", ")": "⁾",
    "a": "ᵃ", "b": "ᵇ", "c": "ᶜ", "d": "ᵈ", "e": "ᵉ", "f": "ᶠ", "g": "ᵍ", "h": "ʰ", "i": "ⁱ",
    "j": "ʲ", "k": "ᵏ", "l": "ˡ", "m": "ᵐ", "n": "ⁿ", "o": "ᵒ", "p": "ᵖ", "r": "ʳ", "s": "ˢ",
    "t": "ᵗ", "u": "ᵘ", "v": "ᵛ", "w": "ʷ", "x": "ˣ", "y": "ʸ", "z": "ᶻ",
    "A": "ᴬ", "B": "ᴮ", "D": "ᴰ", "E": "ᴱ", "G": "ᴳ", "H": "ᴴ", "I": "ᴵ", "J": "ᴶ", "K": "ᴷ",
    "L": "ᴸ", "M": "ᴹ", "N": "ᴺ", "O": "ᴼ", "P": "ᴾ", "R": "ᴿ", "T": "ᵀ", "U": "ᵁ", "V": "ⱽ",
    "W": "ᵂ",
}
_SUB: Final[dict[str, str]] = {
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆", "7": "₇", "8": "₈",
    "9": "₉", "+": "₊", "-": "₋", "−": "₋", "=": "₌", "(": "₍", ")": "₎",
    "a": "ₐ", "e": "ₑ", "h": "ₕ", "i": "ᵢ", "j": "ⱼ", "k": "ₖ", "l": "ₗ", "m": "ₘ", "n": "ₙ",
    "o": "ₒ", "p": "ₚ", "r": "ᵣ", "s": "ₛ", "t": "ₜ", "u": "ᵤ", "v": "ᵥ", "x": "ₓ",
    "β": "ᵦ", "γ": "ᵧ", "ρ": "ᵨ", "φ": "ᵩ", "χ": "ᵪ",
}
#: Raised as themselves: a degree sign is never lifted, and there is no superscript star or prime.
_SUP_AS_IS: Final = frozenset("*°′″‴†")
#: Lowered as themselves: U_\infty reads U∞.
_SUB_AS_IS: Final = frozenset("∞*′")

#: Control words that stand for one symbol. \circ is the degree sign here: in this product it only
#: ever follows a caret in a temperature or an angle.
_SYMBOLS: Final[dict[str, str]] = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ", "varrho": "ρ",
    "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ",
    "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ",
    "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓", "ast": "*", "star": "*",
    "approx": "≈", "simeq": "≃", "sim": "~", "cong": "≅", "equiv": "≡", "propto": "∝",
    "ne": "≠", "neq": "≠", "le": "≤", "leq": "≤", "leqslant": "≤", "ge": "≥", "geq": "≥",
    "geqslant": "≥", "ll": "≪", "gg": "≫", "lt": "<", "gt": ">",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "Rightarrow": "⇒", "leftrightarrow": "↔",
    "infty": "∞", "partial": "∂", "nabla": "∇", "sum": "∑", "int": "∫",
    "circ": "°", "degree": "°", "prime": "′", "dagger": "†",
    "ldots": "…", "dots": "…", "cdots": "…",
    "quad": " ", "qquad": " ", "enspace": " ", "thinspace": _NNBSP,
    # siunitx, so \SI{15}{\celsius} and \si{\metre\per\second} read as a person writes them
    "celsius": "°C", "degreeCelsius": "°C", "metre": "m", "meter": "m", "second": "s",
    "kilogram": "kg", "gram": "g", "kelvin": "K", "pascal": "Pa", "newton": "N", "joule": "J",
    "watt": "W", "hertz": "Hz", "litre": "L", "liter": "L", "radian": "rad", "percent": "%",
    "kilo": "k", "mega": "M", "giga": "G", "milli": "m", "micro": "μ", "nano": "n", "centi": "c",
    "per": "/", "squared": "²", "cubed": "³",
}
#: Control words that only shape layout, and so render as nothing.
_DROPPED: Final = frozenset((
    "left", "right", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr", "displaystyle",
    "textstyle", "scriptstyle", "limits", "nolimits", "rm", "bf", "it", "mathstrut", "strut"))
#: Control symbols: a backslash and one non-letter.
_CONTROL_SYMBOLS: Final[dict[str, str]] = {
    ",": _NNBSP, ";": " ", ":": " ", ">": " ", " ": " ", "!": "",
    "%": "%", "_": "_", "#": "#", "&": "&", "$": "$", "{": "{", "}": "}",
}
#: Wrappers whose argument is TEXT (read as prose) and whose argument is MATH (rendered as math).
_TEXT_WRAPPERS: Final = frozenset((
    "text", "textrm", "textnormal", "textit", "textbf", "textsf", "texttt", "textup", "mbox", "emph"))
_MATH_WRAPPERS: Final = frozenset((
    "mathrm", "mathit", "mathbf", "mathsf", "mathtt", "mathcal", "mathbb", "mathnormal",
    "boldsymbol", "bm", "operatorname", "si", "unit", "num"))
_FRACTIONS: Final = frozenset(("frac", "dfrac", "tfrac"))
#: \frac{1}{2}\rho U^2 is ½ρU², where 1/2ρU² would read as a division by ρU².
_VULGAR: Final[dict[tuple[str, str], str]] = {
    ("1", "2"): "½", ("1", "3"): "⅓", ("2", "3"): "⅔", ("1", "4"): "¼", ("3", "4"): "¾",
    ("1", "5"): "⅕", ("1", "6"): "⅙", ("1", "8"): "⅛"}
_ROOTS: Final[dict[str, str]] = {"": "√", "2": "√", "3": "∛", "4": "∜"}
_OPERATOR_NAMES: Final = frozenset((
    "sin", "cos", "tan", "sec", "csc", "cot", "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh",
    "exp", "log", "ln", "lg", "max", "min", "sup", "inf", "lim", "det", "arg", "dim", "gcd"))
_QUANTITIES: Final = frozenset(("SI", "qty"))
_ACCENTS: Final[dict[str, str]] = {
    "bar": "\u0304", "overline": "\u0304", "hat": "\u0302", "widehat": "\u0302",
    "tilde": "\u0303", "widetilde": "\u0303", "dot": "\u0307", "ddot": "\u0308", "vec": "\u20d7"}

_GREEK: Final = "\u0391-\u03a9\u03b1-\u03c9"
_SUPERSCRIPT_DIGITS: Final = "\u2070\u00b9\u00b2\u00b3\u2074-\u2079"

# Never touched: code, links, paths and file names. Order matters - a fence before a backtick.
_EXTENSIONS: Final = ("step|stp|stl|iges|igs|brep|x_t|sat|vtp|vtk|vtu|obj|ply|off|msh|unv|cgns|"
                      "foam|h5|json|yaml|yml|txt|csv|log|dat|py|sh|zip|gz|tar|png|jpe?g|3mf|gltf|glb")
_PROTECTED = re.compile(
    r"```.*?```"                                             # fenced code
    r"|`[^`\n]+`"                                            # inline code - patch and file names
    r"|https?://[^ \t\n\r\f\v<>()\[\]`]+"                    # links
    r"|(?<![A-Za-z])[A-Z]:\\[\w .()-]+\\[^ \t\n\r\f\v`]*"    # Windows paths
    r"|(?<![\w/~.])(?:~|\.{1,2})?/(?:[\w.-]+/)+[\w.-]*"      # POSIX paths
    r"|(?<![\w.-])[\w-]+(?:\.[\w-]+)*\.(?:" + _EXTENSIONS + r")(?!\w)",   # file names
    re.S | re.A | re.I)

_MATH_SPAN = re.compile(
    r"\\\((?P<inline>.+?)\\\)"
    r"|\\\[(?P<display>.+?)\\\]"
    r"|\$\$(?P<block>.+?)\$\$"
    # $...$ only when it is plainly math: no space inside either dollar, no digit or letter
    # touching the outside of either, and (checked below) a math sign or a lone symbol inside -
    # so "$5 to $10" and "costs $5/$10" are money and stay.
    r"|(?<![\\$A-Za-z0-9])\$(?=[^ \t\n$])(?P<dollar>[^$\n]{1,160}?)(?<=[^ \t\n\\])\$(?![A-Za-z0-9$])",
    re.S | re.A)
_DOLLAR_IS_MATH = re.compile(r"[\\^_=]|^[A-Za-z" + _GREEK + r"]{1,2}\Z", re.A)

_CONTROL = re.compile(r"\\(?:[A-Za-z]+|.)", re.S | re.A)
#: A caret's number, read leniently as a person means it: 10^-5 is 10⁻⁵ and 10^25 is 10²⁵. Only
#: a minus is taken - y^+30 is y⁺ then 30. A decimal is taken whole so it stays 10^0.8.
_SIGNED_DIGITS = re.compile(r"[\-−]?[0-9]+(?:\.[0-9]+)?", re.A)
_ATOM = re.compile(r"[+\-−]?[^ \t\n+\-−×·/=<>≈≤≥^_()]+", re.A)
_SIMPLE = re.compile(r"[+\-−]?[A-Za-z0-9." + _GREEK + r"]+", re.A)

# prose: when a caret or an underscore is markup rather than part of a word
_RAISE_AFTER = re.compile(r"[A-Za-z0-9)\]}°" + _GREEK + r"]", re.A)
_LOWER_AFTER = re.compile(r"(?:^|[^A-Za-z0-9_" + _GREEK + r"])[A-Za-z" + _GREEK + r"]{1,3}\Z", re.A)
_PROSE_LETTER = re.compile(r"[A-Za-z]", re.A)
_ALNUM = re.compile(r"[A-Za-z0-9]", re.A)

# math: the spacing LaTeX gives relations and a multiplication cross
_RELATION = re.compile(r"[ \t\n]*([=≈≃≅≡≤≥≠<>~≪≫∝→←⇒↔×])[ \t\n]*", re.A)
#: In math an en dash between two numbers is a range however it was spaced (30 \text{–} 300), and
#: so is a hyphen spaced on one side only - the trace of \text{-} between spaced numbers. A hyphen
#: spaced on BOTH sides is the writer's minus, a subtraction: 40 - 5 stays 40 - 5, never 40–5.
_MATH_EN_DASH = re.compile(r"([0-9]) ?– ?([0-9])", re.A)
_MATH_LOPSIDED_HYPHEN = re.compile(r"([0-9])(?: -|- )([0-9])", re.A)
_NUMBER_END = re.compile(r"[0-9" + _SUPERSCRIPT_DIGITS + r"]", re.A)
_SPACED_OPERATORS: Final = frozenset("+-−±")
_DEGREE_UNIT = re.compile(r"°[ \t]+([CFK])(?![A-Za-z])", re.A)
_SPACES = re.compile(r"[ \t\n]{2,}", re.A)

# plain-text notation, applied outside code once the markup is rendered
_Y_PLUS = re.compile(r"(?<![A-Za-z0-9_+\-])y\+(?![A-Za-z0-9_+])", re.A)
_K_MODEL = re.compile(
    r"(?<![A-Za-z0-9_])([kK])([-–])(omega|Omega|OMEGA|epsilon|Epsilon|EPSILON)(?![A-Za-z0-9_])", re.A)
_TIMES_TEN = re.compile(r"([0-9]) ?[x*] ?(10[⁻⁺]?[" + _SUPERSCRIPT_DIGITS + r"]+)", re.A)
#: A hyphen between two plain numbers is a range. Not a date (2026-09-30), an id (3f2a-4566), a
#: time (10:30-11), a version (v1.2-3), a file name (12-34.stl) or a NACA 6-series name (NACA 64-212).
_RANGE = re.compile(
    r"(?<![A-Za-z0-9_.,\-–/:#])(?<!NACA )([0-9]+(?:\.[0-9]+)?)-([0-9]+(?:\.[0-9]+)?)"
    r"(?![A-Za-z0-9_\-–/]|[.,][0-9])", re.A)
_TRIGGER = re.compile(r"[\\^$_]|y\+|[kK][-–][OoEe]|[0-9]-[0-9]|[0-9] ?[x*] ?10", re.A)


def plain(text: str) -> str:
    """``text`` as a person should read it: math markup rendered as Unicode, code untouched.

    Idempotent on what a model writes - plain(plain(t)) == plain(t) for every shared case and every
    harness reply - so text that already went through it (a stored reply read back, the console's
    own pass over it) does not change again."""
    if not isinstance(text, str) or not text or not _TRIGGER.search(text):
        return text
    out: list[str] = []
    pos = 0
    for m in _PROTECTED.finditer(text):
        out.append(_free(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_free(text[pos:]))
    return "".join(out)


def _free(s: str) -> str:
    if not s:
        return s
    parts: list[str] = []
    pos = 0
    for m in _MATH_SPAN.finditer(s):
        body = m.group("inline") or m.group("display") or m.group("block")
        if body is None:
            body = m.group("dollar")
            if not _DOLLAR_IS_MATH.search(body):
                continue                    # money, not math: left in the prose around it
        parts.append(_render(s[pos:m.start()], math=False))
        parts.append(_math(body))
        pos = m.end()
    parts.append(_render(s[pos:], math=False))
    t = "".join(parts)
    t = _TIMES_TEN.sub(r"\1 × \2", t)
    t = _Y_PLUS.sub("y⁺", t)
    t = _K_MODEL.sub(lambda m: m.group(1) + m.group(2)
                     + ("ω" if m.group(3).lower() == "omega" else "ε"), t)
    return _RANGE.sub(r"\1–\2", t)


def _math(body: str) -> str:
    t = _render(body, math=True)
    t = _RELATION.sub(r" \1 ", t)
    t = _MATH_EN_DASH.sub(r"\1–\2", t)
    t = _MATH_LOPSIDED_HYPHEN.sub(r"\1-\2", t)
    t = _DEGREE_UNIT.sub(r"°\1", t)
    return _SPACES.sub(" ", t).strip(_WS)


def _render(src: str, *, math: bool) -> str:
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        done: tuple[str, int] | None = None
        if ch == "\\":
            done = _command(src, i, math=math)
        elif ch in "^_":
            done = _script(src, i, math=math)
        elif math and ch in _WS:
            k = _skip_ws(src, i)
            done = (" " if _math_space_kept(_last(out), src[i - 1] if i else "",
                                            src[k] if k < n else "") else "", k)
        elif math and ch in "{}":
            done = ("", i + 1)              # grouping braces
        elif math and ch in "~&":
            done = (" ", i + 1)             # a tie, an alignment tab
        if done is None:
            out.append(ch)
            i += 1
        else:
            out.append(done[0])
            i = done[1]
    return "".join(out)


def _math_space_kept(last: str, before: str, after: str) -> bool:
    """Whether a space the writer typed inside math is kept. TeX ignores them all and spaces the
    formula itself; this keeps the ones that carry that spacing in plain text - after a comma,
    between a number and the unit after it (10 m/s), and around a + or - the writer spaced out -
    and drops the rest, so \\rho U L is ρUL and y^+ \\approx 30 is y⁺ ≈ 30."""
    if not last or not after:
        return False
    if last in ",;":
        return True
    if _NUMBER_END.match(last) and (after == "\\" or _PROSE_LETTER.match(after)):
        return True
    return before in _SPACED_OPERATORS or after in _SPACED_OPERATORS


def _last(out: list[str]) -> str:
    for piece in reversed(out):
        if piece:
            return piece[-1]
    return ""


def _skip_ws(src: str, j: int) -> int:
    while j < len(src) and src[j] in _WS:
        j += 1
    return j


def _arg(src: str, j: int) -> tuple[str, str, int] | None:
    """One TeX argument at ``j``: ("group", its content, end), ("control", the command, end) or
    ("char", the character, end). None when there is none, or the group never closes."""
    j = _skip_ws(src, j)
    if j >= len(src) or src[j] == "}":
        return None
    if src[j] == "{":
        depth, k = 0, j
        while k < len(src):
            c = src[k]
            if c == "\\":
                k += 2
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return "group", src[j + 1:k], k + 1
            k += 1
        return None
    if src[j] == "\\":
        m = _CONTROL.match(src, j)
        if m is None:
            return None
        return "control", m.group(0), m.end()
    return "char", src[j], j + 1


def _command(src: str, i: int, *, math: bool) -> tuple[str, int] | None:
    m = _CONTROL.match(src, i)
    if m is None:
        return None
    word, j = m.group(0)[1:], m.end()
    if not _PROSE_LETTER.match(word):
        if word in _CONTROL_SYMBOLS:
            return _CONTROL_SYMBOLS[word], j
        if word == "\\":
            # a line break in math; in prose a literal pair, kept whole so "\\times" stays
            return (" " if math else src[i:j]), j
        return None
    done = _control_word(src, word, j, math=math)
    if done is None and math:
        return word, j      # in math, a command that cannot be read (\frac with no arguments) reads as its name
    return done


def _control_word(src: str, word: str, j: int, *, math: bool) -> tuple[str, int] | None:
    if word in _TEXT_WRAPPERS or word in _MATH_WRAPPERS:
        a = _arg(src, j)
        if a is None:
            return "", j
        return _render(a[1], math=word in _MATH_WRAPPERS), a[2]
    if word in ("textsuperscript", "textsubscript"):
        a = _arg(src, j)
        if a is None:
            return None
        body = _render(a[1], math=False)
        return (_raise(body) if word == "textsuperscript" else _lower(body)), a[2]
    if word in _FRACTIONS:
        num = _arg(src, j)
        den = _arg(src, num[2]) if num else None
        if num is None or den is None:
            return None
        top, bottom = _math(num[1]), _math(den[1])
        return _VULGAR.get((top, bottom)) or f"{_operand(top)}/{_operand(bottom)}", den[2]
    if word in _QUANTITIES:
        value = _arg(src, j)
        unit = _arg(src, value[2]) if value else None
        if value is None or unit is None:
            return None
        return f"{_math(value[1])} {_math(unit[1])}", unit[2]
    if word == "sqrt":
        root = "√"
        k = _skip_ws(src, j)
        if k < len(src) and src[k] == "[" and "]" in src[k:]:
            close = src.index("]", k)
            index = _math(src[k + 1:close])     # \sqrt[3]{8} is ∛8, \sqrt[n]{x} is ⁿ√x - never √
            root = _ROOTS.get(index) or _raise(index) + "√"
            j = close + 1
        a = _arg(src, j)
        if a is None:
            return None
        return root + _operand(_math(a[1])), a[2]
    if word in _ACCENTS:
        a = _arg(src, j)
        if a is None:
            return None
        mark = _ACCENTS[word]
        return "".join(c if c in _WS else c + mark for c in _math(a[1])), a[2]
    if word in _SYMBOLS:
        # TeX ends a control word at the space after it ("\Delta p" is Δp). In prose that space is
        # the writer's own ("k–\omega SST"), so it stays.
        return _SYMBOLS[word], (_skip_ws(src, j) if math else j)
    if word in _DROPPED:
        return "", (_skip_ws(src, j) if math else j)
    if math:
        # any other name reads as itself (\Re is Re); an operator name keeps TeX's space before
        # its argument, so \sin\theta is "sin θ" and \max(a) stays "max(a)"
        k = _skip_ws(src, j)
        if word in _OPERATOR_NAMES and k < len(src) and (src[k] == "\\" or _ALNUM.match(src[k])):
            return word + " ", k
        return word, j
    return None                             # prose: an unknown command is somebody's text


def _script(src: str, i: int, *, math: bool) -> tuple[str, int] | None:
    op = src[i]
    if not math:
        if op == "^" and not (i > 0 and _RAISE_AFTER.match(src[i - 1])):
            return None
        if op == "_" and not (i > 0 and (src[i - 1] == "}" or _LOWER_AFTER.search(src[max(0, i - 4):i]))):
            return None
    digits = _SIGNED_DIGITS.match(src, i + 1)
    if digits is not None and not (not math and op == "_"):
        return _raise(digits.group(0)) if op == "^" else _lower(digits.group(0)), digits.end()
    a = _arg(src, i + 1)
    if a is None:
        return None
    kind, raw, end = a
    if not math:
        if kind == "group" and (len(raw) > 24 or "\n" in raw):
            return None
        if kind == "control" and raw[1:] not in _SYMBOLS:
            return None
        if kind == "char":
            if op == "_":
                return None             # a bare _x outside math is an identifier: inlet_1
            if raw not in "+-*" and not (_PROSE_LETTER.match(raw)
                                         and not (end < len(src) and _PROSE_LETTER.match(src[end]))):
                return None
    body = _math(raw) if kind != "char" else raw
    return (_raise(body) if op == "^" else _lower(body)), end


def _raise(body: str) -> str:
    body = body.strip(_WS)
    if not body:
        return ""
    if all(c in _SUP_AS_IS for c in body):
        return body
    if all(c in _SUP for c in body):
        return "".join(_SUP[c] for c in body)
    return "^" + (body if _SIMPLE.fullmatch(body) else f"({body})")


def _lower(body: str) -> str:
    body = body.strip(_WS)
    if not body:
        return ""
    if all(c in _SUB_AS_IS for c in body):
        return body
    if all(c in _SUB for c in body):
        return "".join(_SUB[c] for c in body)
    return "_" + (body if _SIMPLE.fullmatch(body) else f"({body})")


def _operand(r: str) -> str:
    r = r.strip(_WS)
    return r if _ATOM.fullmatch(r) else f"({r})"
