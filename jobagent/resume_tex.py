"""Read, edit and compile the LaTeX master resume.

The tailoring stage never sees or writes LaTeX. This module renders the .tex as plain text with
an ID on every editable line, applies Claude's edits (plain text) back into the .tex so the
formatting stays exactly as designed, and compiles the result to check the page count.

Editable lines:
  S1    the summary paragraph                  rewrite
  K1..  a Technical Skills row (list part only) rewrite
  B1..  a bullet                                rewrite, remove, move (within its list), add after
  P1..  a Projects entry (title line)           remove (drops the whole project)
"""

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

PDFLATEX = "/Library/TeX/texbin/pdflatex"


@dataclass
class Unit:
    id: str
    kind: str      # summary | skill | bullet | entry
    start: int     # span in the .tex body that this unit owns
    end: int
    text: str      # plain-text rendering
    section: str
    block: int = 0     # bullets: which list they belong to
    label: str = ""    # skills: the bold label, which edits keep
    row_start: int = 0 # skills: where the row (label included) starts


# ---------------------------------------------------------------- LaTeX <-> plain text

def _strip_cmd(text: str, cmd: str) -> str:
    """\\cmd{x} -> x, handling nested braces."""
    out, i, key = [], 0, "\\" + cmd + "{"
    while (j := text.find(key, i)) != -1:
        out.append(text[i:j])
        k, depth = j + len(key), 1
        while depth and k < len(text):
            depth += {"{": 1, "}": -1}.get(text[k], 0)
            k += 1
        out.append(text[j + len(key):k - 1])
        i = k
    out.append(text[i:])
    return "".join(out)


def latex_to_plain(tex: str) -> str:
    t = re.sub(r"(?<!\\)%.*", "", tex)
    t = re.sub(r"\\(?:LARGE|Large|large|normalfont|bfseries|itshape)\b\s*", "", t)
    t = re.sub(r"\\(begin|end)\{[^}]*\}", "", t)
    t = re.sub(r"\\\\\[[^\]]*\]", "\n", t)  # \\[3pt]
    t = re.sub(r"\\link\{[^}]*\}\{([^}]*)\}", r"\1", t)
    t = re.sub(r"\\href\{[^}]*\}\{([^}]*)\}", r"\1", t)
    t = re.sub(r"\\textcolor\{[^}]*\}\{([^}]*)\}", r"\1", t)
    for cmd in ("textbf", "textit", "mbox", "emph"):
        t = _strip_cmd(t, cmd)
    t = re.sub(r"\{\\normalfont\s*(.*?)\}", r"\1", t)
    t = t.replace("$|$", "|").replace("\\%", "%").replace("\\&", "&").replace("\\$", "$").replace("\\#", "#")
    t = t.replace("\\_", "_").replace("---", "—").replace("--", "–").replace("\\ ", " ").replace("~", " ")
    t = t.replace("\\\\", "\n").replace("\\par", "\n")
    t = re.sub(r"\\[a-zA-Z]+\*?(\[[^\]]*\])?", "", t)  # any remaining command
    t = t.replace("{", "").replace("}", "")
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r" *\n *", "\n", t).strip()


def plain_to_latex(text: str) -> str:
    t = " ".join(text.split())
    t = t.replace("\\", "").replace("{", "").replace("}", "")
    for ch in "&%$#_":
        t = t.replace(ch, "\\" + ch)
    return t.replace("—", "---").replace("–", "--").replace("~", "\\textasciitilde{}")


def _one_line(tex: str) -> str:
    return " ".join(latex_to_plain(tex).split())


def same_text(a: str, b: str) -> bool:
    norm = lambda s: " ".join(s.replace("–", "-").replace("—", "-").split()).strip().lower()
    return norm(a) == norm(b)


# ---------------------------------------------------------------- parsing

def _matching_brace(text: str, open_idx: int) -> int:
    depth = 0
    for k in range(open_idx, len(text)):
        depth += {"{": 1, "}": -1}.get(text[k], 0)
        if depth == 0:
            return k
    raise ValueError("unbalanced braces")


def parse(tex: str) -> list[Unit]:
    units: list[Unit] = []
    sections = [(m.start(), m.group(1)) for m in re.finditer(r"\\section\*\{([^}]*)\}", tex)]
    end_doc = tex.find("\\end{document}")

    def section_at(pos: int) -> str:
        name = ""
        for start, title in sections:
            if start <= pos:
                name = title
        return name

    def section_end(title: str) -> int:
        for i, (start, t) in enumerate(sections):
            if t == title:
                return sections[i + 1][0] if i + 1 < len(sections) else end_doc
        return -1

    # Summary: the paragraph after \section*{Summary}
    m = re.search(r"\\section\*\{Summary\}\s*\n", tex)
    if m:
        body_end = section_end("Summary")
        para = tex[m.end():body_end]
        para = re.split(r"\n\s*\n|\n%", para)[0]
        units.append(Unit("S1", "summary", m.end(), m.end() + len(para.rstrip()), _one_line(para), "Summary"))

    # Skills rows: \textbf{Label:} & list \\
    for i, m in enumerate(re.finditer(r"\\textbf\{([^}]*?):\}\s*&\s*(.*?)\s*\\\\", tex, re.S), 1):
        units.append(Unit(f"K{i}", "skill", m.start(2), m.end(2), _one_line(m.group(2)),
                          section_at(m.start()), label=latex_to_plain(m.group(1)), row_start=m.start()))

    # Bullets, grouped by list
    for b, lst in enumerate(re.finditer(r"\\begin\{itemize\}(.*?)\\end\{itemize\}", tex, re.S), 1):
        inner_start = lst.start(1)
        for m in re.finditer(r"[ \t]*\\item\s+(.*?)\s*\n(?=\s*(?:\\item|$))", lst.group(1) + "\n", re.S):
            start, end = inner_start + m.start(), min(inner_start + m.end(), lst.end(1))
            units.append(Unit("", "bullet", start, end, _one_line(m.group(1)), section_at(start), block=b))
    for i, u in enumerate((u for u in units if u.kind == "bullet"), 1):
        u.id = f"B{i}"

    # Projects entries: \entry{...}{date} through the end of its bullet list
    proj_start, proj_end = next(((s, section_end(t)) for s, t in sections if t == "Projects"), (-1, -1))
    i = 0
    for m in re.finditer(r"\\entry\{", tex):
        if not proj_start <= m.start() < proj_end:
            continue
        title_end = _matching_brace(tex, m.end() - 1)
        date_end = _matching_brace(tex, title_end + 1)
        lst_end = tex.find("\\end{itemize}", date_end)
        nxt = tex.find("\\entry{", date_end)
        end = lst_end + len("\\end{itemize}") if lst_end != -1 and (nxt == -1 or lst_end < nxt) else date_end + 1
        tail = re.match(r"\s*\\smallskip\s*\n", tex[end:])
        end += tail.end() if tail else 0
        i += 1
        title = _one_line(tex[m.end():title_end]) + " — " + _one_line(tex[title_end + 2:date_end])
        units.append(Unit(f"P{i}", "entry", m.start(), end, title, "Projects"))
    return units


def render(tex: str, units: list[Unit]) -> str:
    """Plain-text resume with [ID] in front of every editable line."""
    body_start = tex.find("\\begin{document}") + len("\\begin{document}")
    body = tex[body_start:tex.find("\\end{document}")]
    marks = []  # (pos, length to replace, marker)
    for u in units:
        if u.kind == "entry":
            marks.append((u.start - body_start, 0, f"\x00{u.id}\x00"))
        else:
            start = u.row_start if u.kind == "skill" else u.start
            marks.append((start - body_start, u.end - start, f"\x00{u.id}\x00"))
    for pos, length, marker in sorted(marks, reverse=True):
        body = body[:pos] + marker + ("\n" if length else "") + body[pos + length:]
    body = re.sub(r"\\section\*\{([^}]*)\}", lambda m: f"\n\n## {m.group(1).upper()}\n", body)
    # \entry{title}{date} -> "title — date" on its own line
    while (j := body.find("\\entry{")) != -1:
        title_end = _matching_brace(body, j + len("\\entry"))
        date_end = _matching_brace(body, title_end + 1)
        title = " ".join(body[j + len("\\entry{"):title_end].split())
        body = body[:j] + "\n" + title + " — " + body[title_end + 2:date_end] + "\n" + body[date_end + 1:]
    body = re.sub(r"\\(begin|end)\{tabular\}(\{[^\n]*\})?|\\setlength\{[^}]*\}\{[^}]*\}|\\vspace\{[^}]*\}", "", body)
    text = latex_to_plain(body)
    by_id = {u.id: u for u in units}

    def line_for(m):
        u = by_id[m.group(1)]
        if u.kind == "skill":
            return f"[{u.id}] {u.label}: {u.text}"
        if u.kind == "bullet":
            return f"[{u.id}] - {u.text}"
        if u.kind == "entry":
            return f"[{u.id}] "
        return f"[{u.id}] {u.text}"
    text = re.sub(r"\x00([SKBP]\d+)\x00", line_for, text)
    text = re.sub(r"(\[P\d+\]) \n+", r"\1 ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------------------------------------------------------------- editing

class EditError(Exception):
    pass


def apply_edits(tex: str, units: list[Unit], edits: list[dict]) -> str:
    """Apply validated edits (dicts with op, target_id, new_text, anchor_id, position)."""
    by_id = {u.id: u for u in units}
    replacement: dict[str, str] = {}      # unit id -> new LaTeX for its span
    after: dict[str, list[str]] = {}      # unit id -> bullet lines to insert after it
    before: dict[str, list[str]] = {}
    removed_entries = set()

    def bullet_line(latex_text: str) -> str:
        return f"  \\item {latex_text}\n"

    for e in edits:
        u = by_id[e["target_id"]]
        if e["op"] == "rewrite":
            replacement[u.id] = bullet_line(plain_to_latex(e["new_text"])) if u.kind == "bullet" else plain_to_latex(e["new_text"])
        elif e["op"] == "remove":
            replacement[u.id] = ""
            if u.kind == "entry":
                removed_entries.add(u.id)
        elif e["op"] == "add":
            after.setdefault(u.id, []).append(bullet_line(plain_to_latex(e["new_text"])))
        elif e["op"] == "move":
            line = tex[u.start:u.end]
            replacement[u.id] = ""
            (after if e["position"] == "after" else before).setdefault(e["anchor_id"], []).append(line)

    out, pos = [], 0
    for u in sorted(units, key=lambda u: u.start):
        if u.start < pos:  # inside a removed entry
            continue
        out.append(tex[pos:u.start])
        if u.id in removed_entries:
            pos = u.end
            continue
        out.extend(before.get(u.id, []))
        if u.kind == "entry":   # entries are only removable; keep them and continue inside
            pos = u.start
            continue
        current = tex[u.start:u.end]
        if u.id in replacement and u.kind == "bullet" and replacement[u.id] and not current.endswith("\n"):
            replacement[u.id] = replacement[u.id].rstrip("\n")
        out.append(replacement.get(u.id, current))
        out.extend(after.get(u.id, []))
        pos = u.end
    out.append(tex[pos:])
    return "".join(out)


# ---------------------------------------------------------------- compiling

def compile_pdf(tex: str, out_dir: Path, name: str) -> tuple[Path | None, int, str]:
    """Compile to out_dir/name.pdf. Returns (pdf path or None, page count, error text)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"{name}.tex"
        src.write_text(tex)
        proc = subprocess.run([PDFLATEX, "-interaction=nonstopmode", "-halt-on-error", src.name],
                              cwd=tmp, capture_output=True, text=True, timeout=120)
        log = (Path(tmp) / f"{name}.log").read_text(errors="replace") if (Path(tmp) / f"{name}.log").exists() else proc.stdout
        m = re.search(r"Output written on .*?\((\d+) pages?", log.replace("\n", ""))
        if proc.returncode != 0 or not m:
            errors = [l for l in log.splitlines() if l.startswith("!")]
            return None, 0, "\n".join(errors[:5]) or "pdflatex failed"
        pdf = out_dir / f"{name}.pdf"
        pdf.write_bytes((Path(tmp) / f"{name}.pdf").read_bytes())
        return pdf, int(m.group(1)), ""


def page_count(tex: str) -> tuple[int, str]:
    """Compile in a scratch folder just to count pages."""
    with tempfile.TemporaryDirectory() as tmp:
        _, pages, err = compile_pdf(tex, Path(tmp), "check")
        return pages, err


def spare_lines(tex: str, units: list[Unit], page_limit: int, max_probe: int = 3) -> int:
    """How many extra one-line bullets fit before the resume exceeds page_limit (0..max_probe).
    Found by compiling the resume with short filler bullets added; returns -1 if it can't be measured."""
    anchor = next((u for u in units if u.kind == "bullet"), None)
    if anchor is None:
        return -1
    for n in range(1, max_probe + 1):
        filler = [{"op": "add", "target_id": anchor.id, "new_text": "Filler line"}] * n
        pages, err = page_count(apply_edits(tex, units, filler))
        if err:
            return -1
        if pages > page_limit:
            return n - 1
    return max_probe
