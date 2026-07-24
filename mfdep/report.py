"""Render a Result as text, CSV, JSON or a self-contained HTML page.

Ordering is deliberate: writers and DDL come before readers. When someone asks
"what depends on this table" they are almost always about to change it, and the
programs that INSERT/UPDATE/DELETE or run a LOAD are the ones that break.
"""

from __future__ import annotations

import csv
import html
import json
import sys
from collections import Counter

from .config import DDL, DECLARE, HEURISTIC, LOCK, READ, UTILITY, WRITE
from .graph import Result

_ORDER = {WRITE: 0, DDL: 1, UTILITY: 2, LOCK: 3, DECLARE: 4, READ: 5}


def _by_access(res: Result) -> dict[str, list]:
    out: dict[str, list] = {}
    for r in res.refs:
        out.setdefault(r.access, []).append(r)
    return dict(sorted(out.items(), key=lambda kv: _ORDER.get(kv[0], 9)))


def _counts(res: Result) -> Counter:
    return Counter(r.access for r in res.refs)


# ---------------------------------------------------------------- text

def text_report(res: Result, verbose: bool = False, out=sys.stdout) -> None:
    w = out.write
    bar = "=" * 78
    w(f"{bar}\nDB2 TABLE DEPENDENCIES: {res.spec}\n{bar}\n\n")

    if not res.refs and not res.steps:
        w("No dependencies found.\n\n")
        w("Before concluding the table is unused, check:\n")
        w("  * the index actually covers the right libraries  (mfdep stats)\n")
        w("  * the name is spelled as it appears in source, not as in the catalog\n")
        w("  * the blind spots below - dynamic SQL hides table names entirely\n\n")

    counts = _counts(res)
    w("SUMMARY\n")
    w(f"  Resolved objects      {len(res.targets):>6}\n")
    w(f"  Direct references     {len(res.refs):>6}   "
      f"({counts.get(WRITE, 0)} write, {counts.get(READ, 0)} read, "
      f"{counts.get(DDL, 0)} DDL, {counts.get(UTILITY, 0)} utility)\n")
    w(f"  Programs              {len(res.programs):>6}\n")
    w(f"  Jobs / JCL members    {len(res.jobs):>6}\n")
    w(f"  Copybooks in chain    {len(res.copybooks):>6}\n")
    w(f"  Blind spots           {len(res.blind_spots):>6}\n\n")

    if len(res.targets) > 1:
        w("RESOLVED OBJECTS\n")
        for schema, table, why in sorted(res.targets):
            name = f"{schema}.{table}" if schema else table
            w(f"  {name:<40} {why}\n")
        w("\n")

    for access, refs in _by_access(res).items():
        label = {WRITE: "WRITERS - these change the data",
                 DDL: "DDL - these change the structure",
                 UTILITY: "UTILITIES - LOAD / REORG / COPY / RUNSTATS",
                 LOCK: "EXPLICIT LOCKS",
                 DECLARE: "DECLARATIONS (DCLGEN)",
                 READ: "READERS"}.get(access, access)
        w(f"{label}  ({len(refs)})\n")
        shown = refs if verbose else refs[:40]
        for r in shown:
            qual = f"{r.schema}.{r.table}" if r.schema else f"(none).{r.table}"
            flag = " [heuristic]" if r.confidence <= HEURISTIC else ""
            via = f" via {r.via}" if r.via else ""
            w(f"  {r.path}:{r.line}\n")
            w(f"      {r.stmt:<18} {qual}{via}{flag}\n")
            if r.snippet:
                w(f"      | {r.snippet[:100]}\n")
        if len(refs) > len(shown):
            w(f"  ... {len(refs) - len(shown)} more (use --verbose)\n")
        w("\n")

    if res.programs:
        w(f"PROGRAMS ({len(res.programs)})\n")
        for name, node in sorted(res.programs.items(),
                                 key=lambda kv: (kv[1].depth, kv[0])):
            w(f"  {name:<12} depth {node.depth}  {node.reason}\n")
            if verbose:
                for p in node.files:
                    w(f"      {p}\n")
        w("\n")

    if res.jobs:
        # Real jobs first: those are the things that actually fail at 03:00.
        # A PROC in this list is a link in the chain, not a schedulable unit.
        ordered = sorted(res.jobs.items(),
                         key=lambda kv: (kv[1][0].kind != "JCL", kv[0]))
        n_jcl = sum(1 for _, s in ordered if s[0].kind == "JCL")
        w(f"JOBS AND PROCS ({n_jcl} job(s), {len(ordered) - n_jcl} proc(s))\n")
        for job, steps in ordered:
            tag = "JOB " if steps[0].kind == "JCL" else "PROC"
            w(f"  [{tag}] {job}\n")
            for s in steps:
                w(f"      STEP {s.step:<10} {s.reason:<32} {s.path}:{s.line}\n")
                # Cataloged decks the step reads: this is where a LOAD, UNLOAD
                # or sort deck lives when it is not inlined in the JCL.
                for dd, dsn, kind, path in s.decks:
                    w(f"          {dd:<8} -> [{kind}] {dsn}\n")
                    if verbose:
                        w(f"                     {path}\n")
                if verbose:
                    for dd, dsn, disp in s.datasets:
                        w(f"          {dd:<8} DD  {dsn} {disp}\n")
        w("\n")

        if not verbose and any(s.datasets for s in res.steps):
            w("  (--verbose also lists every DD dataset for these steps)\n\n")

    if res.data_links:
        w(f"DATA-LINKED STEPS ({len(res.data_links)}) - share a dataset with "
          f"the above\n")
        w("  Not SQL dependencies, but sort/merge and copy decks hard-code the\n"
          "  table's physical layout, so a column change breaks them silently.\n")
        for d in sorted(res.data_links, key=lambda x: (x.job, x.step)):
            w(f"  [{d.kind:<7}] {d.job}/{d.step:<10} {d.pgm:<10} "
              f"{d.direction} {d.dataset}\n")
            w(f"      shared with {d.via_job}   {d.path}:{d.line}\n")
            for dd, dsn, kind, path in d.decks:
                w(f"          {dd:<8} -> [{kind}] {dsn}\n")
        w("\n")

    if res.blind_spots:
        w(f"BLIND SPOTS ({len(res.blind_spots)}) - where this answer is incomplete\n")
        by_kind: dict[str, list] = {}
        for path, line, kind, detail in res.blind_spots:
            by_kind.setdefault(kind, []).append((path, line, detail))
        for kind, rows in sorted(by_kind.items()):
            w(f"  {kind} ({len(rows)})\n")
            for path, line, detail in (rows if verbose else rows[:10]):
                loc = f"{path}:{line}" if line else path
                w(f"      {loc}\n")
                if detail:
                    w(f"        {detail[:110]}\n")
            if not verbose and len(rows) > 10:
                w(f"      ... {len(rows) - 10} more (use --verbose)\n")
        w("\n")

    if res.notes:
        w("NOTES\n")
        for n in res.notes:
            w(f"  * {n}\n")
        w("\n")


# ---------------------------------------------------------------- csv

_CSV_HEADER = ["record_type", "path", "member", "artifact_kind", "line",
               "schema", "table", "access", "statement", "confidence", "via",
               "job", "step", "program", "detail"]


def csv_report(res: Result, path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        wr = csv.writer(fh)
        wr.writerow(_CSV_HEADER)
        for r in res.refs:
            wr.writerow(["REFERENCE", r.path, r.member, r.kind, r.line, r.schema,
                         r.table, r.access, r.stmt, r.confidence, r.via,
                         "", "", "", r.snippet])
        for name, node in sorted(res.programs.items()):
            wr.writerow(["PROGRAM", ";".join(node.files), "", "", "", "", "",
                         "", "", "", "", "", "", name, node.reason])
        for s in res.steps:
            wr.writerow(["JOB_STEP", s.path, "", "JCL", s.line, "", "", "", "",
                         "", "", s.job, s.step, s.resolved_pgm or s.pgm, s.reason])
        for s in res.steps:
            for dd, dsn, kind, path in s.decks:
                wr.writerow(["CONTROL_DECK", path, "", kind, "", "", "", "", "",
                             "", dd, s.job, s.step, s.resolved_pgm or s.pgm,
                             f"referenced as {dsn}"])
        for d in res.data_links:
            wr.writerow(["DATA_LINK", d.path, "", d.kind, d.line, "", "", "",
                         "", "", d.direction, d.job, d.step, d.pgm,
                         f"{d.dataset} shared with {d.via_job}"])
        for p, line, kind, detail in res.blind_spots:
            wr.writerow(["BLIND_SPOT", p, "", "", line, "", "", "", "", "", "",
                         "", "", "", f"{kind}: {detail}"])


# ---------------------------------------------------------------- json

def json_report(res: Result, path: str | None = None) -> str:
    payload = {
        "table": res.spec,
        "resolved_objects": [
            {"schema": s, "table": t, "why": w} for s, t, w in res.targets],
        "summary": {
            "references": len(res.refs),
            "by_access": dict(_counts(res)),
            "programs": len(res.programs),
            "jobs": len(res.jobs),
            "copybooks": len(res.copybooks),
            "blind_spots": len(res.blind_spots),
        },
        "references": [r.__dict__ for r in res.refs],
        "programs": [
            {"name": n, "depth": p.depth, "reason": p.reason, "files": p.files}
            for n, p in sorted(res.programs.items())],
        "jobs": [
            {"job": j, "steps": [s.__dict__ for s in steps]}
            for j, steps in sorted(res.jobs.items())],
        "data_links": [d.__dict__ for d in res.data_links],
        "blind_spots": [
            {"path": p, "line": l, "kind": k, "detail": d}
            for p, l, k, d in res.blind_spots],
        "notes": res.notes,
    }
    text = json.dumps(payload, indent=2)
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return text


# ---------------------------------------------------------------- html

_HTML_HEAD = """<!doctype html><html><head><meta charset="utf-8">
<title>Dependencies: {title}</title>
<style>
 body{{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;
      background:#f6f7f9;color:#1b1f24}}
 header{{background:#12213a;color:#fff;padding:18px 28px}}
 header h1{{margin:0;font-size:19px;font-weight:600}}
 header .sub{{opacity:.75;font-size:13px;margin-top:4px}}
 main{{padding:22px 28px;max-width:1500px}}
 .cards{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:22px}}
 .card{{background:#fff;border:1px solid #dfe3e8;border-radius:8px;
        padding:12px 18px;min-width:110px}}
 .card b{{display:block;font-size:24px;font-weight:600}}
 .card span{{font-size:12px;color:#5b6673;text-transform:uppercase;
             letter-spacing:.04em}}
 h2{{font-size:15px;margin:26px 0 10px;text-transform:uppercase;
     letter-spacing:.05em;color:#3c4653}}
 table{{border-collapse:collapse;width:100%;background:#fff;
        border:1px solid #dfe3e8;border-radius:8px;overflow:hidden}}
 th,td{{text-align:left;padding:7px 10px;border-bottom:1px solid #eceff2;
        font-size:13px;vertical-align:top}}
 th{{background:#f0f2f5;font-weight:600;cursor:pointer;user-select:none;
     position:sticky;top:0}}
 tr:hover td{{background:#fafbfc}}
 code{{font:12px/1.4 Consolas,monospace;color:#40484f;overflow-wrap:break-word}}
 code.path{{color:#6b7480;font-size:11px}}
 .member{{font:600 12px Consolas,monospace;color:#1b1f24}}
 .tag{{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;
       font-weight:600}}
 .WRITE{{background:#fde8e8;color:#a11}} .READ{{background:#e8f0fe;color:#14459c}}
 .DDL{{background:#fff2d8;color:#8a5a00}} .UTILITY{{background:#e9f7ec;color:#186b32}}
 .LOCK,.DECLARE{{background:#eee;color:#444}}
 .heur{{color:#a15c00;font-size:11px}}
 .filter{{margin-bottom:10px;padding:7px 10px;width:320px;border:1px solid #cfd5dc;
          border-radius:6px;font-size:13px}}
 .note{{background:#fff8e1;border-left:3px solid #e8a33d;padding:9px 13px;
        margin:7px 0;font-size:13px}}
 .empty{{color:#79838f;font-style:italic}}
</style></head><body>
<header><h1>DB2 table dependencies &mdash; {title}</h1>
<div class="sub">{subtitle}</div></header><main>
"""

_HTML_TAIL = """
<script>
document.querySelectorAll('table').forEach(function(t){
  t.querySelectorAll('th').forEach(function(th,i){
    th.onclick=function(){
      var b=t.tBodies[0],rows=[].slice.call(b.rows),
          asc=!(th.dataset.asc==='1');
      th.dataset.asc=asc?'1':'0';
      rows.sort(function(x,y){
        var a=x.cells[i].innerText.trim(),c=y.cells[i].innerText.trim();
        var na=parseFloat(a),nc=parseFloat(c);
        if(!isNaN(na)&&!isNaN(nc))return asc?na-nc:nc-na;
        return asc?a.localeCompare(c):c.localeCompare(a);});
      rows.forEach(function(r){b.appendChild(r)});};});});
document.querySelectorAll('.filter').forEach(function(inp){
  inp.oninput=function(){
    var q=inp.value.toUpperCase(),
        t=document.getElementById(inp.dataset.target);
    [].slice.call(t.tBodies[0].rows).forEach(function(r){
      r.style.display=r.innerText.toUpperCase().indexOf(q)>-1?'':'none';});};});
</script></main></body></html>"""


def _esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def _path_cell(path: str, member: str = "") -> str:
    """Render a path that wraps at separators instead of mid-token.

    `word-break: break-all` chops `PROD.COBOL.SRC` into `PRO / D.COBOL.SRC`,
    which makes a library name unreadable. <wbr> offers the browser a break
    opportunity only at the backslashes.
    """
    wrapped = _esc(path).replace("\\", "\\<wbr>").replace("/", "/<wbr>")
    head = f'<div class="member">{_esc(member)}</div>' if member else ""
    return f'{head}<code class="path" title="{_esc(path)}">{wrapped}</code>'


def html_report(res: Result, path: str) -> None:
    counts = _counts(res)
    parts = [_HTML_HEAD.format(
        title=_esc(res.spec),
        subtitle=_esc(f"{len(res.refs)} references &middot; {len(res.programs)} "
                      f"programs &middot; {len(res.jobs)} jobs &middot; "
                      f"{len(res.blind_spots)} blind spots").replace("&amp;", "&"))]

    parts.append('<div class="cards">')
    for label, value in (("References", len(res.refs)),
                         ("Writers", counts.get(WRITE, 0)),
                         ("Readers", counts.get(READ, 0)),
                         ("DDL", counts.get(DDL, 0)),
                         ("Utility", counts.get(UTILITY, 0)),
                         ("Programs", len(res.programs)),
                         ("Jobs", len(res.jobs)),
                         ("Blind spots", len(res.blind_spots))):
        parts.append(f'<div class="card"><b>{value}</b><span>{label}</span></div>')
    parts.append("</div>")

    for n in res.notes:
        parts.append(f'<div class="note">{_esc(n)}</div>')

    # references
    parts.append('<h2>References</h2>')
    parts.append('<input class="filter" data-target="refs" '
                 'placeholder="Filter references...">')
    parts.append('<table id="refs"><thead><tr><th>Access</th><th>Statement</th>'
                 '<th>Object</th><th>Artifact</th><th>Member / file</th>'
                 '<th>Line</th><th>Evidence</th></tr></thead><tbody>')
    if not res.refs:
        parts.append('<tr><td colspan="7" class="empty">none</td></tr>')
    for r in sorted(res.refs, key=lambda x: (_ORDER.get(x.access, 9), x.path)):
        heur = ' <span class="heur">heuristic</span>' if r.confidence <= HEURISTIC else ""
        obj = f"{r.schema}.{r.table}" if r.schema else f"(none).{r.table}"
        parts.append(
            f'<tr><td><span class="tag {_esc(r.access)}">{_esc(r.access)}</span>{heur}</td>'
            f"<td>{_esc(r.stmt)}</td><td><code>{_esc(obj)}</code></td>"
            f"<td>{_esc(r.kind)}</td><td>{_path_cell(r.path, r.member)}</td>"
            f"<td>{r.line}</td><td><code>{_esc(r.snippet[:120])}</code></td></tr>")
    parts.append("</tbody></table>")

    # jobs
    parts.append('<h2>Jobs and steps</h2>')
    parts.append('<input class="filter" data-target="jobs" '
                 'placeholder="Filter jobs...">')
    parts.append('<table id="jobs"><thead><tr><th>Job</th><th>Step</th>'
                 '<th>Program</th><th>Why</th><th>File</th><th>Line</th>'
                 '</tr></thead><tbody>')
    if not res.steps:
        parts.append('<tr><td colspan="6" class="empty">none</td></tr>')
    for s in res.steps:
        parts.append(
            f"<tr><td>{_esc(s.job)}</td><td>{_esc(s.step)}</td>"
            f"<td>{_esc(s.resolved_pgm or s.pgm)}</td><td>{_esc(s.reason)}</td>"
            f"<td>{_path_cell(s.path)}</td><td>{s.line}</td></tr>")
    parts.append("</tbody></table>")

    # data links
    if res.data_links:
        parts.append('<h2>Data-linked steps</h2>')
        parts.append('<div class="note">Not SQL dependencies. These steps share '
                     'a dataset with a step above &mdash; sort, merge and copy '
                     'decks hard-code the table&rsquo;s physical layout, so a '
                     'column change breaks them with no SQL to search for.</div>')
        parts.append('<table><thead><tr><th>Job</th><th>Step</th><th>Program</th>'
                     '<th>Direction</th><th>Dataset</th><th>Shared with</th>'
                     '<th>Deck</th><th>File</th></tr></thead><tbody>')
        for d in sorted(res.data_links, key=lambda x: (x.job, x.step)):
            decks = "<br>".join(f"[{_esc(k)}] {_esc(dsn)}"
                                for _dd, dsn, k, _p in d.decks) or "&mdash;"
            parts.append(
                f"<tr><td>{_esc(d.job)}</td><td>{_esc(d.step)}</td>"
                f"<td>{_esc(d.pgm)}</td><td>{_esc(d.direction)}</td>"
                f"<td><code>{_esc(d.dataset)}</code></td>"
                f"<td>{_esc(d.via_job)}</td><td>{decks}</td>"
                f"<td>{_path_cell(d.path)}</td></tr>")
        parts.append("</tbody></table>")

    # programs
    parts.append('<h2>Programs</h2><table><thead><tr><th>Program</th>'
                 '<th>Depth</th><th>Why</th><th>Files</th></tr></thead><tbody>')
    if not res.programs:
        parts.append('<tr><td colspan="4" class="empty">none</td></tr>')
    for name, node in sorted(res.programs.items(),
                             key=lambda kv: (kv[1].depth, kv[0])):
        parts.append(
            f"<tr><td>{_esc(name)}</td><td>{node.depth}</td>"
            f"<td>{_esc(node.reason)}</td>"
            f"<td>{''.join(_path_cell(f) for f in node.files[:4])}</td></tr>")
    parts.append("</tbody></table>")

    # blind spots
    parts.append('<h2>Blind spots &mdash; where this answer is incomplete</h2>')
    parts.append('<table><thead><tr><th>Kind</th><th>File</th><th>Line</th>'
                 '<th>Detail</th></tr></thead><tbody>')
    if not res.blind_spots:
        parts.append('<tr><td colspan="4" class="empty">none</td></tr>')
    for p, line, kind, detail in res.blind_spots:
        parts.append(f"<tr><td>{_esc(kind)}</td><td>{_path_cell(p)}</td>"
                     f"<td>{line or ''}</td><td>{_esc(detail[:200])}</td></tr>")
    parts.append("</tbody></table>")

    parts.append(_HTML_TAIL)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(parts))
