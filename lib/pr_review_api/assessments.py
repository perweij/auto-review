"""Model assessments for display; legacy recovery never rewrites saved drafts."""
import re


SEVERITIES = ("Critical", "High", "Medium", "Low")
_LEVEL = r"(Critical|High|Medium|Low)(?=\s*(?:$|[,;|().—–-]))"
_LABEL = re.compile(r"^Severity\s*:\s*" + _LEVEL, re.IGNORECASE)
_INLINE = re.compile(r"^\s*[-—–:|]\s*" + _LEVEL, re.IGNORECASE)
_IDENTITY = re.compile(r"^\[?([A-Za-z0-9_-]+)\]?(?=\s|[.:—–]|$)(.*)$")


def plain(text):
    """Strip common inline Markdown decoration, leaving finding IDs intact."""
    return text.replace("**", "").replace("__", "").replace("`", "").strip()


def report_severities(report, identities):
    """Recover explicit ID/level headings, Severity labels, and ID/Severity tables.

    Do not interpret arbitrary prose, confidence, quoted text, or code as a
    severity. Conflicting recorded levels remain unknown instead of guessing.
    """
    found = {identity: set() for identity in identities}
    current, columns, fence = None, None, None
    for raw in report.splitlines():
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            marker = line[:3]
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence or raw.startswith(("    ", "\t")) or line.startswith(">"):
            continue
        if line.startswith("|"):
            current = None
            cells = [plain(cell) for cell in line.strip("|").split("|")]
            names = [cell.lower() for cell in cells]
            id_column = next((i for i, name in enumerate(names) if name in ("id", "finding id")), None)
            if id_column is not None:
                columns = (id_column, names.index("severity")) if "severity" in names else None
            elif columns and max(columns) < len(cells):
                identity, level = (cells[i] for i in columns)
                if identity in found and level.title() in SEVERITIES:
                    found[identity].add(level.title())
            continue
        columns = None
        text = plain(re.sub(r"^#{1,6}\s+", "", line))
        match = _IDENTITY.match(text)
        if match and match[1] in found:
            level = _INLINE.match(match[2])
            current = match[1] if level or line.startswith(("#", "**", "__")) else None
            if level:
                found[current].add(level[1].title())
        elif line.startswith("#"):
            current = None
        elif current:
            level = _LABEL.match(re.sub(r"^[-*+]\s+", "", text))
            if level:
                found[current].add(level[1].title())
    return {identity: next(iter(levels)) for identity, levels in found.items() if len(levels) == 1}


def finding_assessments(synthesis):
    """Return immutable model metadata, preferring structured severity."""
    findings = synthesis.get("findings", [])
    recovered = report_severities(synthesis.get("report_md", ""), {f["id"] for f in findings})
    result = {}
    for finding in findings:
        identity = finding["id"]
        severity = finding.get("severity") if "severity" in finding else recovered.get(identity)
        if severity not in SEVERITIES:
            severity = None
        result[identity] = {
            "id": identity, "severity": severity,
            "severity_source": ("structured" if "severity" in finding else "report") if severity else None,
            "blocking": finding.get("blocking") if type(finding.get("blocking")) is bool else None,
        }
    return result


def comment_assessment(comment, findings):
    """Summarize grouped findings without assigning a rating to human comments."""
    entries = [findings.get(identity, {"id": identity, "severity": None,
                                       "severity_source": None, "blocking": None})
               for identity in comment.get("finding_ids", [])]
    levels = [entry["severity"] for entry in entries if entry["severity"]]
    return {"severity": min(levels, key=SEVERITIES.index) if levels else None,
            "severity_incomplete": any(entry["severity"] is None for entry in entries),
            "finding_assessments": entries}
