#!/usr/bin/env python3
"""Minimal deterministic eval runner for the shuiqian-news-list skill.

Each case runs `codex exec` in a fresh workspace whose .agents/skills/
contains only a copy of the skill under test (plus whatever global skills
the harness exposes — realistic for actual users). Assertions are plain
program checks, no LLM judging:

  - trigger yes: a concrete /daily/<date>.json URL was requested
  - trigger no:  no concrete /daily/<date>.json request at all
  - render:      every item title and source_url of the day's JSON
                 appears in the final answer; all links in the answer
                 are a subset of links known from the JSON/API
  - 404 branch:  answer contains an expected phrase and fabricates
                 nothing (link-subset check again)

Date-sensitive cases probe the live API first, so the same case stays
valid as backfill progresses ("去年今天" flips 404 -> 200 on its own).

Usage:
  run_evals.py [--only id1,id2] [--keep-workspaces]
Results land in evals/runs/<timestamp>/ (gitignored).
"""

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = ROOT / "skills" / "shuiqian-news-list"
RUNS_DIR = Path(__file__).resolve().parent / "runs"
TZ = ZoneInfo("Asia/Shanghai")
DATA_REPO = "liusining/shuiqian-news-list"
API = f"https://raw.githubusercontent.com/{DATA_REPO}/main/data"
UA = {"User-Agent": f"shuiqian-news-eval/1 (+https://github.com/{DATA_REPO})"}
DAILY_URL_RE = re.compile(r"/daily/(\d{4}-\d{2}-\d{2})\.json")
# Remote-only variant: after a clone the workspace contains local
# data/daily/*.json paths, which must not count as network fetches.
REMOTE_DAILY_RE = re.compile(r"https?://[^\s\"']*/daily/(\d{4}-\d{2}-\d{2})\.json")
LINK_RE = re.compile(r"https?://[^\s)\]>\"']+")
CODEX_TIMEOUT = 600


class GraderError(Exception):
    """The grader could not establish ground truth — the case is unjudgeable,
    which must never be silently graded as the 404 / not-published branch."""


def api_get(path):
    """(status, doc). A real 404 returns (404, None); anything that stops us
    from knowing raises GraderError so the case is reported as an error."""
    req = urllib.request.Request(f"{API}{path}", headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return 404, None
        raise GraderError(f"{API}{path} -> HTTP {e.code}")
    except urllib.error.URLError as e:  # DNS / connection / TLS
        raise GraderError(f"{API}{path} -> {type(e).__name__}: {e.reason}")
    except Exception as e:  # timeout, malformed JSON, …
        raise GraderError(f"{API}{path} -> {type(e).__name__}: {e}")


def resolve_date(spec):
    today = dt.datetime.now(TZ).date()
    if spec == "today":
        return today.isoformat()
    if spec == "yesterday":
        return (today - dt.timedelta(days=1)).isoformat()
    if spec == "day-2":
        return (today - dt.timedelta(days=2)).isoformat()
    if spec == "lastyear":
        try:
            return today.replace(year=today.year - 1).isoformat()
        except ValueError:  # Feb 29
            return today.replace(year=today.year - 1, day=28).isoformat()
    return spec  # literal YYYY-MM-DD


def run_codex(prompt, ws):
    (ws / ".agents" / "skills").mkdir(parents=True)
    shutil.copytree(SKILL_DIR, ws / ".agents" / "skills" / SKILL_DIR.name)
    last = ws / "last.txt"
    try:
        r = subprocess.run(
            ["codex", "exec", "-C", str(ws), "--skip-git-repo-check",
             "-o", str(last), prompt],
            capture_output=True, text=True, timeout=CODEX_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        # One hung codex must not destroy a 1-2.5h run: fail this case and
        # carry on. (Seen 2026-09-12 on n-story, which had passed hours
        # earlier — codex hangs, not a skill regression.)
        def _txt(b):
            return b.decode("utf-8", "replace") if isinstance(b, bytes) else (b or "")
        events = _txt(e.stdout) + _txt(e.stderr)
        answer = last.read_text(encoding="utf-8") if last.exists() else ""
        return events, answer, f"timeout after {CODEX_TIMEOUT}s"
    events = r.stdout + r.stderr
    answer = last.read_text(encoding="utf-8") if last.exists() else ""
    return events, answer, r.returncode


def norm(text):
    """Comparison form ignoring whitespace and punctuation: agents may
    render the double-space separators inside some source titles as a
    comma etc. — harmless formatting freedom, not a content change."""
    return re.sub(r"[^\w]+", "", text)


def link_universe(docs):
    # Repo-scoped, not a bare raw.githubusercontent.com prefix: raw is now the
    # only data channel, so "any file any user hosts on raw" must not count as
    # a legitimate citation.
    urls = {API, f"{API}/index.json",
            f"https://raw.githubusercontent.com/{DATA_REPO}/",
            f"https://github.com/{DATA_REPO}",
            "https://github.com/liusining/shuiqian-news-skill",
            # the data repo's README credits this archive; bulk cases can read
            # it after cloning and legitimately cite it
            "https://github.com/bedtimenews/bedtimenews-archive-contents"}
    for day_doc in docs:
        if day_doc:
            if day_doc.get("article_url"):
                urls.add(day_doc["article_url"])
            for it in day_doc.get("items", []):
                if it.get("source_url"):
                    urls.add(it["source_url"])
                # bodies of weibo-era days keep inline markdown links —
                # rendering them is faithful output, not fabrication
                for u in LINK_RE.findall(it.get("body") or ""):
                    urls.add(u.rstrip(".,;:。，；：)"))
    return urls


def check_links_subset(answer, docs, failures):
    allowed = link_universe(docs)
    for url in LINK_RE.findall(answer):
        url = url.rstrip(".,;:。，；：")
        if any(url.startswith(a) for a in allowed):
            continue
        if DAILY_URL_RE.search(url):  # api daily url itself
            continue
        failures.append(f"fabricated/unknown link: {url[:80]}")


def mentions_date(answer, iso):
    """True if the answer names this date in any ordinary rendering.

    The literal-substring check this replaces failed a correct answer on
    2026-09-12: the agent wrote "最新一期是 **9 月 11 日**" and the grader was
    looking for "2026-09-11". It still catches a genuinely wrong date — the
    same run had another case claim "9 月 10 日" when latest was 09-11."""
    y, m, d = iso.split("-")
    forms = [
        iso, f"{y}/{m}/{d}", f"{m}-{d}", f"{m}/{d}",
        f"{int(m)}-{int(d)}", f"{int(m)}/{int(d)}",
        f"{y}年{int(m)}月{int(d)}日", f"{int(m)}月{int(d)}日",
        f"{y}年{m}月{d}日", f"{m}月{d}日",
    ]
    flat = re.sub(r"[\s*_`]+", "", answer)
    return any(f in flat for f in forms)


def title_overlap(title, hay):
    """Character-bigram overlap of a Chinese title against the normalized
    answer. Measured on the 2026-09-12 run: items that were present but
    reworded scored 0.26-1.00, titles from an unrelated day scored <= 0.14."""
    t = norm(title)
    if len(t) < 2:
        return 1.0 if t and t in hay else 0.0
    grams = {t[i:i + 2] for i in range(len(t) - 1)}
    return sum(1 for g in grams if g in hay) / len(grams)


# Only used for the ~1% of items that carry no source_url (measured over a
# 40-day sample: 4 of 447). Set between the two clusters above.
TITLE_MIN_OVERLAP = 0.20


def check_render(answer, day_doc, failures):
    """Assert every item of the day made it into the answer.

    Presence is proved by source_url, which is exact and unforgeable — agents
    legitimately condense or reword titles ("以色列在黎巴嫩南部爆破真主党地下
    堡垒" -> "以军爆破黎巴嫩地下设施"), and demanding verbatim titles flagged
    correct answers as failures. Only items with no URL fall back to a fuzzy
    title match."""
    hay = norm(answer)
    for it in day_doc.get("items", []):
        if it.get("source_url"):
            if it["source_url"] not in answer:
                failures.append(f"missing item {it['no']} "
                                f"(source_url absent): {it['title'][:30]}")
        elif title_overlap(it["title"], hay) < TITLE_MIN_OVERLAP:
            failures.append(f"missing item {it['no']} "
                            f"(no source_url, title not recognizable): "
                            f"{it['title'][:30]}")


def run_case(ev, keep_ws):
    checks = ev.get("checks", {})
    ws = Path(tempfile.mkdtemp(prefix=f"sqeval-{ev['id']}-"))
    failures = []
    events, answer = "", ""
    try:
        events, answer, rc = run_codex(ev["prompt"], ws)
        if isinstance(rc, str):  # harness problem, not a skill verdict
            failures.append(f"HARNESS ERROR (not a skill regression): codex {rc}")
            return failures, events, answer
        if rc != 0:
            failures.append(f"codex exec rc={rc}")
        fetched = set(DAILY_URL_RE.findall(events))

        if checks.get("trigger") is False:
            if fetched:
                failures.append(f"should not trigger, but fetched {sorted(fetched)}")
            return failures, events, answer

        if "date" in checks:
            date = resolve_date(checks["date"])
            status, doc = api_get(f"/daily/{date}.json")
            if status == 200:
                if date not in fetched:
                    failures.append(f"expected fetch of /daily/{date}.json, "
                                    f"saw {sorted(fetched) or 'none'}")
                check_render(answer, doc, failures)
                check_links_subset(answer, [doc], failures)
            else:  # 404 branch of a dynamic date
                _, idx = api_get("/index.json")
                latest = (idx or {}).get("latest", "")
                is_today = date == dt.datetime.now(TZ).date().isoformat()
                if is_today:
                    if latest and not mentions_date(answer, latest):
                        failures.append(
                            f"today-unpublished: latest {latest} not mentioned")
                elif not any(k in answer for k in ("找不到", "没有", "缺", "无数据")):
                    failures.append("404 branch: no not-found wording in answer")
                check_links_subset(answer, [], failures)
            return failures, events, answer

        if "branch" in checks:
            if not any(p in answer for p in checks["phrases"]):
                failures.append(
                    f"{checks['branch']}: none of {checks['phrases']} in answer")
            check_links_subset(answer, [], failures)
            return failures, events, answer

        if checks.get("bulk"):
            remote = sorted(set(REMOTE_DAILY_RE.findall(events)))
            if remote:
                failures.append(f"bulk case made per-day remote fetches: "
                                f"{remote[:3]}{'...' if len(remote) > 3 else ''}")
            if "git clone" not in events and "Cloning into" not in events:
                failures.append("no clone evidence in events")
            # Bulk deliverables legitimately land in a written file rather
            # than the chat answer (the skill tells agents to aggregate
            # locally) — count workspace .md/.txt files as output too.
            corpus = answer
            for p in ws.rglob("*"):
                if (p.is_file() and p.suffix.lower() in (".md", ".txt")
                        and ".agents" not in p.parts and p.name != "last.txt"
                        and p.stat().st_size < 5_000_000):
                    try:
                        corpus += "\n" + p.read_text(encoding="utf-8")
                    except (UnicodeDecodeError, OSError):
                        pass
            hay = norm(corpus)
            docs = []
            for d in checks["sample_dates"]:
                status, doc = api_get(f"/daily/{d}.json")
                if status != 200 or not doc:
                    failures.append(f"grader could not fetch sample {d}")
                    continue
                docs.append(doc)
                for it in doc.get("items", []):
                    if norm(it["title"]) not in hay:
                        failures.append(f"missing title {d} item {it['no']}: "
                                        f"{it['title'][:24]}")
                    if (not checks.get("titles_only") and it.get("source_url")
                            and it["source_url"] not in corpus):
                        failures.append(f"missing source_url {d} item {it['no']}")
            check_links_subset(answer, docs, failures)
            return failures, events, answer

        if checks.get("noclone"):
            if "Cloning into" in events:
                failures.append("small request must not clone the repo")
            remote = set(REMOTE_DAILY_RE.findall(events))
            docs = []
            for d in checks["dates"]:
                if d not in remote:
                    failures.append(f"expected per-day remote fetch of {d}")
                status, doc = api_get(f"/daily/{d}.json")
                if status == 200 and doc:
                    docs.append(doc)
                    check_render(answer, doc, failures)
            check_links_subset(answer, docs, failures)
            return failures, events, answer

        failures.append("case has no recognized checks")
        return failures, events, answer
    except GraderError as e:
        # Ground truth is unavailable: report it as its own failure rather
        # than letting one unreachable probe kill a multi-hour run.
        failures.append(f"GRADER ERROR (not a skill regression): {e}")
        return failures, events, answer
    finally:
        if not keep_ws:
            shutil.rmtree(ws, ignore_errors=True)


def preflight():
    """Free static checks — a stale address in SKILL.md would otherwise only
    surface after a multi-hour run, as a confusing behavioural failure."""
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    problems = []
    if "sining.ai" in text:
        problems.append("SKILL.md still references the retired sining.ai host")
    if f"{API}/daily/" not in text:
        problems.append("SKILL.md is missing the raw daily URL")
    if f"{API}/index.json" not in text:
        problems.append("SKILL.md is missing the raw index.json URL")
    if f"git clone --depth 1 https://github.com/{DATA_REPO}" not in text:
        problems.append("SKILL.md is missing the bulk clone URL of the data repo")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--keep-workspaces", action="store_true")
    args = ap.parse_args()

    if problems := preflight():
        for p in problems:
            print(f"PREFLIGHT FAIL: {p}")
        return 1

    spec = json.loads((Path(__file__).resolve().parent / "evals.json")
                      .read_text(encoding="utf-8"))
    evals = spec["evals"]
    if args.only:
        wanted = set(args.only.split(","))
        evals = [e for e in evals if e["id"] in wanted]

    stamp = dt.datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    out_dir = RUNS_DIR / stamp
    out_dir.mkdir(parents=True)

    results = []
    for ev in evals:
        print(f"== {ev['id']}: {ev['prompt']}")
        failures, events, answer = run_case(ev, args.keep_workspaces)
        (out_dir / f"{ev['id']}.events.log").write_text(events, encoding="utf-8")
        (out_dir / f"{ev['id']}.answer.md").write_text(answer, encoding="utf-8")
        ok = not failures
        results.append({"id": ev["id"], "pass": ok, "failures": failures})
        print("   PASS" if ok else "   FAIL: " + "; ".join(failures))

    passed = sum(r["pass"] for r in results)
    summary = {"timestamp": stamp, "passed": passed, "total": len(results),
               "results": results}
    (out_dir / "results.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{passed}/{len(results)} passed — details in {out_dir}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
