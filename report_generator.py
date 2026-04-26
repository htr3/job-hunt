"""Report generation — CSV, JSON, and a self-contained dark-theme HTML page.

`generate_reports(jobs, config)` writes whichever formats are requested by
`output.format` (`csv` | `html` | `json` | `both`), into `<project>/reports/`,
and optionally opens the HTML in the default browser.
"""
from __future__ import annotations

import csv
import html as _html
import json
import logging
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent
_REPORTS_DIR = _PROJECT_ROOT / "reports"

_CSV_FIELDS = [
    "match_score",
    "title",
    "company",
    "location",
    "platform",
    "salary",
    "experience",
    "job_type",
    "posted_date",
    "skills",
    "url",
]


def _job_to_dict(job: Any) -> dict[str, Any]:
    skills = getattr(job, "skills", []) or []
    if isinstance(skills, list):
        skills_str = ", ".join(str(s) for s in skills if s)
    else:
        skills_str = str(skills)
    return {
        "match_score": round(float(getattr(job, "match_score", 0.0) or 0.0), 1),
        "title": getattr(job, "title", "") or "",
        "company": getattr(job, "company", "") or "",
        "location": getattr(job, "location", "") or "",
        "platform": getattr(job, "platform", "") or "",
        "salary": getattr(job, "salary", "") or "",
        "experience": getattr(job, "experience", "") or "",
        "job_type": getattr(job, "job_type", "") or "",
        "posted_date": getattr(job, "posted_date", "") or "",
        "skills": skills_str,
        "url": getattr(job, "url", "") or "",
        "description": (getattr(job, "description", "") or ""),
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, rows: list[dict[str, Any]], meta: dict[str, Any]) -> None:
    payload = {"generated_at": meta.get("generated_at"), "count": len(rows), "jobs": rows}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Job Hunt Report — {generated_at}</title>
<style>
  :root {{
    --bg: #0f172a; --panel: #1e293b; --text: #e2e8f0; --muted: #94a3b8;
    --accent: #38bdf8; --border: #334155;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 24px; background: var(--bg); color: var(--text);
         font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; }}
  h1 {{ margin: 0 0 4px; font-size: 22px; }}
  .meta {{ color: var(--muted); margin-bottom: 18px; font-size: 13px; }}
  .filters {{ margin-bottom: 14px; }}
  input[type=search] {{
    width: 360px; max-width: 100%; padding: 8px 12px; background: var(--panel);
    color: var(--text); border: 1px solid var(--border); border-radius: 8px;
    font-size: 14px;
  }}
  table {{ width: 100%; border-collapse: collapse; background: var(--panel);
          border: 1px solid var(--border); border-radius: 8px; overflow: hidden;
          font-size: 13px; }}
  th, td {{ text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--border);
           vertical-align: top; }}
  th {{ background: #111827; color: var(--muted); font-weight: 600;
        text-transform: uppercase; letter-spacing: .04em; font-size: 11px; cursor: pointer; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: rgba(56, 189, 248, 0.06); }}
  a {{ color: var(--accent); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .score {{ display: inline-block; min-width: 38px; padding: 2px 8px; border-radius: 999px;
            background: #0ea5e9; color: white; font-weight: 600; text-align: center; }}
  .score.low {{ background: #475569; }}
  .score.mid {{ background: #d97706; }}
  .pill {{ display: inline-block; padding: 1px 8px; border-radius: 999px;
           background: #334155; color: var(--muted); font-size: 11px; margin-right: 4px; }}
  td.skills {{ max-width: 260px; }}
  .skill {{ display: inline-block; padding: 1px 7px; margin: 1px;
            background: #0b2540; color: #93c5fd; border-radius: 4px; font-size: 11px; }}
</style>
</head>
<body>
  <h1>Job Hunt Report</h1>
  <div class="meta">{count} jobs · generated {generated_at}</div>
  <div class="filters">
    <input id="q" type="search" placeholder="Filter by title, company, location, skills..." />
  </div>
  <table id="jobs">
    <thead>
      <tr>
        <th data-k="match_score">Score</th>
        <th data-k="title">Title</th>
        <th data-k="company">Company</th>
        <th data-k="location">Location</th>
        <th data-k="platform">Platform</th>
        <th data-k="salary">Salary</th>
        <th class="skills">Skills</th>
        <th>Link</th>
      </tr>
    </thead>
    <tbody>
      {rows}
    </tbody>
  </table>
<script>
  const q = document.getElementById('q');
  const tbody = document.querySelector('#jobs tbody');
  q.addEventListener('input', () => {{
    const v = q.value.toLowerCase();
    for (const tr of tbody.querySelectorAll('tr')) {{
      tr.style.display = tr.textContent.toLowerCase().includes(v) ? '' : 'none';
    }}
  }});
  document.querySelectorAll('#jobs th[data-k]').forEach(th => {{
    th.addEventListener('click', () => {{
      const k = th.dataset.k;
      const rows = [...tbody.querySelectorAll('tr')];
      const asc = th.dataset.dir !== 'asc';
      rows.sort((a, b) => {{
        const av = a.dataset[k] || '';
        const bv = b.dataset[k] || '';
        const an = parseFloat(av), bn = parseFloat(bv);
        if (!isNaN(an) && !isNaN(bn)) return asc ? an - bn : bn - an;
        return asc ? av.localeCompare(bv) : bv.localeCompare(av);
      }});
      th.dataset.dir = asc ? 'asc' : 'desc';
      rows.forEach(r => tbody.appendChild(r));
    }});
  }});
</script>
</body>
</html>
"""


def _score_class(score: float) -> str:
    if score >= 70:
        return ""
    if score >= 40:
        return "mid"
    return "low"


def _render_row(job: dict[str, Any]) -> str:
    esc = _html.escape
    skills_html = " ".join(
        f'<span class="skill">{esc(s.strip())}</span>'
        for s in (job.get("skills") or "").split(",") if s.strip()
    )
    score = float(job.get("match_score") or 0.0)
    url = job.get("url") or ""
    return (
        f'<tr data-match_score="{score}" '
        f'data-title="{esc(job.get("title",""))}" '
        f'data-company="{esc(job.get("company",""))}" '
        f'data-location="{esc(job.get("location",""))}" '
        f'data-platform="{esc(job.get("platform",""))}" '
        f'data-salary="{esc(job.get("salary",""))}">'
        f'<td><span class="score {_score_class(score)}">{score:.0f}</span></td>'
        f'<td>{esc(job.get("title",""))}</td>'
        f'<td>{esc(job.get("company",""))}</td>'
        f'<td>{esc(job.get("location",""))}</td>'
        f'<td><span class="pill">{esc(job.get("platform",""))}</span></td>'
        f'<td>{esc(job.get("salary",""))}</td>'
        f'<td class="skills">{skills_html}</td>'
        f'<td>{"<a href=" + chr(34) + esc(url) + chr(34) + " target=" + chr(34) + "_blank" + chr(34) + " rel=" + chr(34) + "noopener" + chr(34) + ">Open</a>" if url else ""}</td>'
        f'</tr>'
    )


def _write_html(path: Path, rows: list[dict[str, Any]], generated_at: str) -> None:
    body = "\n      ".join(_render_row(r) for r in rows) if rows else (
        '<tr><td colspan="8" style="text-align:center;color:#94a3b8;padding:24px">'
        "No jobs to display.</td></tr>"
    )
    html_doc = _HTML_TEMPLATE.format(
        generated_at=_html.escape(generated_at), count=len(rows), rows=body
    )
    path.write_text(html_doc, encoding="utf-8")


def generate_reports(
    jobs: list[Any],
    config: dict[str, Any],
    reports_dir: Path = _REPORTS_DIR,
) -> dict[str, Path]:
    """Write selected report formats and return {format: path} for those written."""
    output = (config or {}).get("output", {}) or {}
    fmt = (output.get("format") or "both").lower()
    auto_open = bool(output.get("auto_open_report", False))

    reports_dir.mkdir(parents=True, exist_ok=True)

    rows = [_job_to_dict(j) for j in jobs]
    rows.sort(key=lambda r: (-(r.get("match_score") or 0.0), r.get("title", "")))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    written: dict[str, Path] = {}

    want_csv = fmt in ("csv", "both")
    want_html = fmt in ("html", "both")
    want_json = fmt in ("json", "both") or bool(
        (config.get("export") or {}).get("json")
    )

    if want_csv:
        p = reports_dir / f"jobs_{ts}.csv"
        _write_csv(p, rows)
        written["csv"] = p
        logger.info("Wrote CSV report: %s (%d rows)", p, len(rows))

    if want_html:
        p = reports_dir / f"jobs_{ts}.html"
        _write_html(p, rows, generated_at)
        written["html"] = p
        logger.info("Wrote HTML report: %s (%d rows)", p, len(rows))

    if want_json:
        p = reports_dir / f"jobs_{ts}.json"
        _write_json(p, rows, {"generated_at": generated_at})
        written["json"] = p
        logger.info("Wrote JSON report: %s (%d rows)", p, len(rows))

    if auto_open and "html" in written:
        try:
            webbrowser.open(written["html"].as_uri())
        except Exception as e:  # pragma: no cover
            logger.debug("Could not auto-open HTML report: %s", e)

    return written
