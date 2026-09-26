#!/usr/bin/env python3
"""
Profile generator for github.com/ahmedali-aihub.

Renders every SVG under assets/ in two finishes -- black titanium (dark mode)
and silver titanium (light mode) -- and rewrites the auto-generated sections
of README.md (recent activity, metrics data table).

Standard library only. In GitHub Actions it reads GITHUB_TOKEN and uses the
GraphQL API. Without a token it falls back to the REST API plus the public
contributions page, so it also runs locally for previews:

    python scripts/generate.py            # fetch live data, render everything
    python scripts/generate.py --offline  # re-render from assets/data.json
"""

from __future__ import annotations

import datetime as dt
import html
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "profile.config.json").read_text(encoding="utf-8"))
ASSETS = ROOT / "assets"
README = ROOT / "README.md"
DATA_CACHE = ASSETS / "data.json"

SANS = ("-apple-system,BlinkMacSystemFont,'SF Pro Display','SF Pro Text','Segoe UI',"
        "'Helvetica Neue',Helvetica,Arial,sans-serif")
MONO = "'SF Mono',ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"


# --------------------------------------------------------------------------
# Palettes. Nothing outside black / graphite / silver / white is allowed.
# --------------------------------------------------------------------------

class Palette(dict):
    __getattr__ = dict.__getitem__


DARK = Palette(
    mode="dark",
    bg_top="#1d1d20", bg_bot="#0a0a0b",
    glow="#ffffff", glow_op=0.07,
    grain_hi="1", grain_op=0.3,
    edge_a="#9a9aa0", edge_b="#2a2a2d", edge_c="#5a5a5f",
    ink="#f5f5f7", ink2="#c7c7cc", ink3="#8e8e93", ink4="#636366",
    hair="#2c2c2e", hair2="#3a3a3c", plate="#ffffff", plate_op=0.035,
    chrome=[("0", "#ffffff"), ("0.42", "#e5e5ea"), ("0.5", "#8e8e93"),
            ("0.62", "#c7c7cc"), ("1", "#f5f5f7")],
    shine="#ffffff", shine_op=0.9, sweep_op=0.07,
    mark="#d1d1d6", mark2="#636366", surface="#141416",
    heat=["#232326", "#3a3a3c", "#636366", "#a1a1a6", "#f2f2f7"],
    node="#161618",
)

LIGHT = Palette(
    mode="light",
    bg_top="#fdfdfe", bg_bot="#dcdce1",
    glow="#ffffff", glow_op=0.55,
    grain_hi="0", grain_op=0.32,
    edge_a="#ffffff", edge_b="#a1a1a6", edge_c="#c7c7cc",
    ink="#1d1d1f", ink2="#3a3a3c", ink3="#6e6e73", ink4="#8e8e93",
    hair="#d2d2d7", hair2="#c7c7cc", plate="#ffffff", plate_op=0.45,
    chrome=[("0", "#3a3a3c"), ("0.45", "#1d1d1f"), ("0.52", "#6e6e73"),
            ("0.64", "#2c2c2e"), ("1", "#48484a")],
    shine="#ffffff", shine_op=0.75, sweep_op=0.35,
    mark="#3a3a3c", mark2="#aeaeb2", surface="#f2f2f5",
    heat=["#e3e3e8", "#c7c7cc", "#8e8e93", "#48484a", "#1d1d1f"],
    node="#f5f5f7",
)


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def fmt(n: int) -> str:
    return f"{n:,}"


def fdate(d: dt.date, year: bool = True) -> str:
    return f"{d.day} {d:%b %Y}" if year else f"{d.day} {d:%b}"


def display_name(repo: str) -> str:
    return CFG.get("display_names", {}).get(repo, repo)


# --------------------------------------------------------------------------
# Data layer
# --------------------------------------------------------------------------

UA = "ahmedali-aihub-profile-generator"


def http(url: str, token: str | None = None, payload: dict | None = None,
         accept: str = "application/vnd.github+json", timeout: int = 30) -> bytes:
    headers = {"User-Agent": UA, "Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (502, 503, 504) and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError:
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("unreachable")


def rest(path: str, token: str | None):
    return json.loads(http(f"https://api.github.com{path}", token))


def graphql(query: str, variables: dict, token: str):
    out = json.loads(http("https://api.github.com/graphql", token,
                          {"query": query, "variables": variables}))
    if out.get("errors"):
        raise RuntimeError(f"GraphQL error: {out['errors']}")
    return out["data"]


GQL_PROFILE = """
query($login: String!) {
  user(login: $login) {
    login name createdAt
    followers { totalCount }
    pullRequests { totalCount }
    issues { totalCount }
    repositories(first: 100, ownerAffiliations: OWNER, isFork: false, privacy: PUBLIC,
                 orderBy: {field: PUSHED_AT, direction: DESC}) {
      nodes {
        name url homepageUrl pushedAt stargazerCount forkCount
        primaryLanguage { name }
        languages(first: 20, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name } }
        }
      }
    }
  }
}"""

GQL_CALENDAR = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      contributionCalendar { weeks { contributionDays { date contributionCount } } }
    }
  }
}"""


def year_windows(start: dt.datetime, end: dt.datetime):
    """contributionsCollection accepts at most one year per query."""
    cur = start
    while cur < end:
        nxt = min(cur + dt.timedelta(days=365) - dt.timedelta(seconds=1), end)
        yield cur, nxt
        cur = nxt + dt.timedelta(seconds=1)


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def fetch_graphql(login: str, token: str) -> dict:
    u = graphql(GQL_PROFILE, {"login": login}, token)["user"]
    created = parse_ts(u["createdAt"])
    now = dt.datetime.now(dt.timezone.utc)
    calendar: dict[str, int] = {}
    commits = 0
    for a, b in year_windows(created, now):
        cc = graphql(GQL_CALENDAR, {"login": login, "from": iso(a), "to": iso(b)},
                     token)["user"]["contributionsCollection"]
        commits += cc["totalCommitContributions"]
        for w in cc["contributionCalendar"]["weeks"]:
            for d in w["contributionDays"]:
                calendar[d["date"]] = max(calendar.get(d["date"], 0), d["contributionCount"])
    repos = []
    for r in u["repositories"]["nodes"]:
        repos.append({
            "name": r["name"], "url": r["url"], "homepage": r["homepageUrl"] or "",
            "pushed_at": r["pushedAt"], "stars": r["stargazerCount"], "forks": r["forkCount"],
            "language": (r["primaryLanguage"] or {}).get("name"),
            "languages": {e["node"]["name"]: e["size"] for e in r["languages"]["edges"]},
        })
    return {
        "login": u["login"], "name": u["name"], "created_at": u["createdAt"],
        "followers": u["followers"]["totalCount"], "prs": u["pullRequests"]["totalCount"],
        "issues": u["issues"]["totalCount"], "commits": commits,
        "repos": repos, "calendar": calendar,
    }


TD_RE = re.compile(r'<td[^>]*?data-date="(\d{4}-\d{2}-\d{2})"[^>]*?id="([^"]+)"')
TIP_RE = re.compile(r'<tool-tip[^>]*?for="([^"]+)"[^>]*>([^<]*)</tool-tip>')


def scrape_calendar(login: str, created: dt.datetime) -> dict[str, int]:
    """Token-free fallback: the public contributions fragment, one year at a time."""
    calendar: dict[str, int] = {}
    for year in range(created.year, dt.date.today().year + 1):
        page = http(f"https://github.com/users/{login}/contributions"
                    f"?from={year}-01-01&to={year}-12-31", accept="text/html").decode("utf-8", "replace")
        ids = {cell_id: date for date, cell_id in TD_RE.findall(page)}
        for cell_id, text in TIP_RE.findall(page):
            if cell_id not in ids:
                continue
            m = re.match(r"\s*([\d,]+) contribution", text)
            calendar[ids[cell_id]] = int(m.group(1).replace(",", "")) if m else 0
    return calendar


def fetch_rest(login: str, token: str | None) -> dict:
    u = rest(f"/users/{login}", token)
    repos = []
    for r in rest(f"/users/{login}/repos?per_page=100&type=owner&sort=pushed", token):
        if r["fork"]:
            continue
        repos.append({
            "name": r["name"], "url": r["html_url"], "homepage": r.get("homepage") or "",
            "pushed_at": r["pushed_at"], "stars": r["stargazers_count"], "forks": r["forks_count"],
            "language": r.get("language"),
            "languages": rest(f"/repos/{login}/{r['name']}/languages", token),
        })

    def search_count(kind: str, q: str) -> int:
        return rest(f"/search/{kind}?q={q}&per_page=1", token)["total_count"]

    return {
        "login": u["login"], "name": u.get("name"), "created_at": u["created_at"],
        "followers": u["followers"],
        "prs": search_count("issues", f"author:{login}+type:pr"),
        "issues": search_count("issues", f"author:{login}+type:issue"),
        "commits": search_count("commits", f"author:{login}"),
        "repos": repos,
        "calendar": scrape_calendar(login, parse_ts(u["created_at"])),
    }


def check_live(url: str) -> bool:
    """A homepage counts as a live demo only if it actually answers."""
    if not url.startswith("http"):
        return False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status < 400
    except Exception:
        return False


def fetch(login: str, token: str | None) -> dict:
    data = fetch_graphql(login, token) if token else fetch_rest(login, token)
    # Starring your own repo is not a star earned. The user's public starred
    # list answers this for every repo in one call, with or without a token.
    starred = {s["full_name"].lower() for s in rest(f"/users/{login}/starred?per_page=100", token)}
    for r in data["repos"]:
        r["live"] = check_live(r["homepage"])
        r["self_starred"] = f"{login}/{r['name']}".lower() in starred
    data["events"] = rest(f"/users/{login}/events/public?per_page=100", token)
    data["fetched_at"] = iso(dt.datetime.now(dt.timezone.utc))
    data["source"] = "graphql" if token else "rest+scrape"
    return data


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

def compute(data: dict) -> dict:
    exclude = set(CFG.get("exclude_repos", []))
    repos = [r for r in data["repos"] if r["name"] not in exclude]
    today = parse_ts(data["fetched_at"]).date()
    created = parse_ts(data["created_at"]).date()
    cal = {dt.date.fromisoformat(k): v for k, v in data["calendar"].items()}
    count = lambda d: cal.get(d, 0)  # noqa: E731

    # Streaks. Today only breaks a streak once it is over.
    cur, d = 0, today if count(today) else today - dt.timedelta(days=1)
    while count(d) > 0:
        cur += 1
        d -= dt.timedelta(days=1)
    cur_range = (d + dt.timedelta(days=1), d + dt.timedelta(days=cur)) if cur else None
    best, best_range, run, run_start = 0, None, 0, None
    d = min(cal) if cal else today
    while d <= today:
        if count(d):
            run += 1
            run_start = run_start if run > 1 else d
            if run > best:
                best, best_range = run, (run_start, d)
        else:
            run = 0
        d += dt.timedelta(days=1)

    since_join = [created + dt.timedelta(days=i) for i in range((today - created).days + 1)]
    window = [today - dt.timedelta(days=i) for i in range(364, -1, -1)]
    year_total = sum(count(d) for d in window)
    active_year = sum(1 for d in window if count(d))
    busiest = max(window, key=lambda d: (count(d), d))

    months = []
    y, m = today.year, today.month
    for _ in range(12):
        months.append((y, m))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    months.reverse()
    monthly = [(f"{dt.date(y, m, 1):%b}", sum(v for k, v in cal.items() if k.year == y and k.month == m))
               for y, m in months]

    weekday = [0] * 7  # Sunday-first, matching the calendar rows
    for d in window:
        weekday[(d.weekday() + 1) % 7] += count(d)

    # Languages: bytes^a * repos^b, the same balance github-readme-stats offers.
    lc = CFG.get("languages", {})
    sw, cw = lc.get("size_weight", 0.5), lc.get("count_weight", 0.5)
    lang_bytes, lang_repos = defaultdict(int), defaultdict(int)
    for r in repos:
        for name, size in r["languages"].items():
            lang_bytes[name] += size
            lang_repos[name] += 1
    scores = {k: (lang_bytes[k] ** sw) * (lang_repos[k] ** cw) for k in lang_bytes}
    total_score = sum(scores.values()) or 1
    languages = [{"name": k, "pct": 100 * s / total_score, "repos": lang_repos[k], "bytes": lang_bytes[k]}
                 for k, s in sorted(scores.items(), key=lambda kv: -kv[1])][: lc.get("top", 6)]

    recent = sorted((r for r in repos if r["languages"]), key=lambda r: r["pushed_at"], reverse=True)

    return {
        "today": today, "created": created, "cal": cal,
        "total": sum(count(d) for d in since_join),
        "commits": data["commits"], "prs": data["prs"], "issues": data["issues"],
        "followers": data["followers"],
        "repos": len(repos), "stars": sum(r["stars"] - int(r.get("self_starred", False)) for r in repos),
        "live_demos": sum(1 for r in repos if r.get("live")),
        "languages_all": len(lang_bytes), "languages": languages,
        "streak": cur, "streak_range": cur_range, "longest": best, "longest_range": best_range,
        "active_days": sum(1 for d in since_join if count(d)), "days_since_join": len(since_join),
        "year_total": year_total, "active_year": active_year,
        "busiest": busiest, "busiest_count": count(busiest),
        "monthly": monthly, "weekday": weekday, "window": window,
        "recent": recent, "last_push": recent[0] if recent else None,
    }


# --------------------------------------------------------------------------
# SVG primitives
# --------------------------------------------------------------------------

def chrome_gradient(P, gid="chrome", x1="0", y1="0", x2="0", y2="1", units=""):
    stops = "".join(f'<stop offset="{o}" stop-color="{c}"/>' for o, c in P.chrome)
    return f'<linearGradient id="{gid}" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}"{units}>{stops}</linearGradient>'


def card(P, w: int, h: int, body: str, *, title: str, desc: str, rx: int = 26,
         defs: str = "", css: str = "", sweep: bool = True, sweep_period: float = 9.0) -> str:
    """A brushed-titanium slab: vertical gradient, horizontal brush grain,
    a soft top-left light, a bevelled edge, and a periodic shine sweep."""
    g = P.grain_hi
    band = 240
    sweep_el = (f'<rect class="sweep" x="0" y="{-h}" width="{band}" height="{3 * h}" fill="url(#sweep)"/>'
                if sweep else "")
    return f"""<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-labelledby="t d">
<title id="t">{esc(title)}</title>
<desc id="d">{esc(desc)}</desc>
<defs>
<linearGradient id="bg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{P.bg_top}"/><stop offset="1" stop-color="{P.bg_bot}"/></linearGradient>
<radialGradient id="glow" cx="0.18" cy="-0.1" r="0.9"><stop offset="0" stop-color="{P.glow}" stop-opacity="{P.glow_op}"/><stop offset="1" stop-color="{P.glow}" stop-opacity="0"/></radialGradient>
<linearGradient id="bevel" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{P.edge_a}"/><stop offset="0.35" stop-color="{P.edge_b}"/><stop offset="0.75" stop-color="{P.edge_b}"/><stop offset="1" stop-color="{P.edge_c}"/></linearGradient>
<linearGradient id="sweep" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset="0.5" stop-color="#fff" stop-opacity="{P.sweep_op}"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>
<filter id="brush" x="0" y="0" width="1" height="1" color-interpolation-filters="sRGB">
<feTurbulence type="fractalNoise" baseFrequency="0.0016 1.3" numOctaves="2" seed="9" result="n"/>
<feColorMatrix in="n" type="matrix" result="hi" values="0 0 0 0 {g} 0 0 0 0 {g} 0 0 0 0 {g} 0.8 0 0 0 -0.4"/>
<feColorMatrix in="n" type="matrix" result="lo" values="0 0 0 0 {1 - int(g)} 0 0 0 0 {1 - int(g)} 0 0 0 0 {1 - int(g)} -0.6 0 0 0 0.28"/>
<feMerge><feMergeNode in="lo"/><feMergeNode in="hi"/></feMerge>
</filter>
<clipPath id="clip"><rect x="1" y="1" width="{w - 2}" height="{h - 2}" rx="{rx}"/></clipPath>
{defs}
</defs>
<style>
text{{font-family:{SANS};}}
.mono{{font-family:{MONO};}}
.num{{font-variant-numeric:tabular-nums;}}
.sweep{{transform:translateX({-band - h}px);animation:sweep {sweep_period}s cubic-bezier(.45,0,.2,1) infinite;}}
@keyframes sweep{{0%{{transform:translateX({-band - h}px) skewX(-18deg);}}45%,100%{{transform:translateX({w + h}px) skewX(-18deg);}}}}
.rise{{animation:rise .9s cubic-bezier(.2,.7,.2,1) both;}}
@keyframes rise{{from{{opacity:0;transform:translateY(10px);}}}}
.fade{{animation:fade 1s ease both;}}
@keyframes fade{{from{{opacity:0;}}}}
.live{{animation:live 2s ease-in-out infinite;}}
@keyframes live{{0%,100%{{opacity:1;}}50%{{opacity:.35;}}}}
{css}
@media (prefers-reduced-motion:reduce){{*{{animation:none!important;}}.sweep{{display:none;}}}}
</style>
<g clip-path="url(#clip)">
<rect width="{w}" height="{h}" fill="url(#bg)"/>
<rect width="{w}" height="{h}" filter="url(#brush)" opacity="{P.grain_op}"/>
<rect width="{w}" height="{h}" fill="url(#glow)"/>
{body}
{sweep_el}
</g>
<rect x="1" y="1" width="{w - 2}" height="{h - 2}" rx="{rx}" fill="none" stroke="url(#bevel)" stroke-width="1.5"/>
<rect x="3" y="3" width="{w - 6}" height="{h - 6}" rx="{rx - 2}" fill="none" stroke="{P.glow}" stroke-opacity="{0.05 if P.mode == 'dark' else 0.6}"/>
</svg>
"""


def live_badge(P, x_right: float, y: float, text: str, fs: float = 13) -> str:
    """Right-aligned 'LIVE · date' marker with a pulsing dot."""
    cw = fs * 0.6 + 1.2
    return (f'<g><text x="{x_right}" y="{y + fs * 0.36:.1f}" text-anchor="end" class="mono" font-size="{fs}" '
            f'letter-spacing="1.2" fill="{P.ink3}">{esc(text)}</text>'
            f'<circle cx="{x_right - len(text) * cw - 10:.1f}" cy="{y}" r="4" fill="{P.ink}" class="live"/></g>')


def bar_h(x: float, y: float, w: float, h: float, r: float = 4) -> str:
    """Horizontal bar: square at the baseline (left), rounded data-end (right)."""
    if w <= 0:
        return ""
    r = min(r, w, h / 2)
    return (f"M{x:.1f},{y:.1f}H{x + w - r:.1f}A{r},{r} 0 0 1 {x + w:.1f},{y + r:.1f}"
            f"V{y + h - r:.1f}A{r},{r} 0 0 1 {x + w - r:.1f},{y + h:.1f}H{x:.1f}Z")


def bar_v(x: float, base: float, w: float, h: float, r: float = 4) -> str:
    """Column: square at the baseline (bottom), rounded data-end (top)."""
    if h <= 0:
        return ""
    r = min(r, h, w / 2)
    top = base - h
    return (f"M{x:.1f},{base:.1f}V{top + r:.1f}A{r},{r} 0 0 1 {x + r:.1f},{top:.1f}"
            f"H{x + w - r:.1f}A{r},{r} 0 0 1 {x + w:.1f},{top + r:.1f}V{base:.1f}Z")


ICONS = {
    "commit": '<circle cx="8" cy="8" r="3"/><path d="M0.5 8H5M11 8H15.5"/>',
    "repo": '<path d="M3 13.5V2.5A1 1 0 0 1 4 1.5H13V11.5H4A1 1 0 0 0 3 12.5A1 1 0 0 0 4 13.5H13"/><path d="M6 4.5H10"/>',
    "star": '<path d="M8 1.5L9.9 5.6L14.3 6.1L11 9.1L11.9 13.5L8 11.3L4.1 13.5L5 9.1L1.7 6.1L6.1 5.6Z"/>',
    "live": '<circle cx="8" cy="8" r="6.5"/><path d="M1.5 8H14.5M8 1.5C10 3.5 10.8 5.6 10.8 8S10 12.5 8 14.5C6 12.5 5.2 10.4 5.2 8S6 3.5 8 1.5Z"/>',
    "code": '<path d="M5.5 4L1.5 8L5.5 12M10.5 4L14.5 8L10.5 12"/>',
}


def icon(name: str, x: float, y: float, color: str, scale: float = 1.15) -> str:
    return (f'<g transform="translate({x},{y}) scale({scale})" fill="none" stroke="{color}" stroke-width="1.35" '
            f'stroke-linecap="round" stroke-linejoin="round">{ICONS[name]}</g>')


def roman(n: int) -> str:
    out = ""
    for v, s in [(10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]:
        while n >= v:
            out, n = out + s, n - v
    return out


def shine_gradient(P, width: int, travel: tuple[int, int], dur: float) -> str:
    """A narrow highlight that glides across text filled with url(#textShine)."""
    return (f'<linearGradient id="textShine" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="{width}" y2="0">'
            f'<stop offset="0" stop-color="{P.shine}" stop-opacity="0"/>'
            f'<stop offset="0.5" stop-color="{P.shine}" stop-opacity="{P.shine_op}"/>'
            f'<stop offset="1" stop-color="{P.shine}" stop-opacity="0"/>'
            f'<animateTransform attributeName="gradientTransform" type="translate" '
            f'values="{travel[0]} 0;{travel[1]} 0;{travel[1]} 0" keyTimes="0;0.45;1" dur="{dur}s" '
            f'repeatCount="indefinite"/></linearGradient>')


# --------------------------------------------------------------------------
# Hero
# --------------------------------------------------------------------------

def _catmull(pts: list[tuple[float, float]], closed: bool) -> tuple[str, list[tuple[float, float]]]:
    """Smooth path through pts (Catmull-Rom as cubic Béziers) plus a dense polyline of it."""
    n = len(pts)
    idx = (lambda i: pts[i % n]) if closed else (lambda i: pts[max(0, min(n - 1, i))])
    segs = n if closed else n - 1
    d = f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"
    dense = []
    for i in range(segs):
        p0, p1, p2, p3 = idx(i - 1), idx(i), idx(i + 1), idx(i + 2)
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d += f" C{c1[0]:.1f},{c1[1]:.1f} {c2[0]:.1f},{c2[1]:.1f} {p2[0]:.1f},{p2[1]:.1f}"
        for s in range(12):
            t = s / 12
            mt = 1 - t
            dense.append((mt ** 3 * p1[0] + 3 * mt * mt * t * c1[0] + 3 * mt * t * t * c2[0] + t ** 3 * p2[0],
                          mt ** 3 * p1[1] + 3 * mt * mt * t * c1[1] + 3 * mt * t * t * c2[1] + t ** 3 * p2[1]))
    return d + (" Z" if closed else ""), dense


def _inside(pt: tuple[float, float], poly: list[tuple[float, float]]) -> bool:
    x, y = pt
    hit = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


# Lateral view, facing left, as three overlapping parts drawn back to front:
# brainstem, cerebellum (tucked under the occipital lobe), then the cerebrum.
BRAINSTEM = [(0.555, 0.70), (0.645, 0.70), (0.668, 0.80), (0.672, 0.92), (0.655, 1.0), (0.585, 1.0),
             (0.565, 0.925), (0.545, 0.845), (0.55, 0.76)]
CEREBELLUM = [(0.64, 0.70), (0.72, 0.625), (0.84, 0.595), (0.94, 0.625), (0.978, 0.70), (0.945, 0.79),
              (0.845, 0.848), (0.73, 0.838), (0.66, 0.785)]
CEREBRUM = [(0.02, 0.40), (0.04, 0.25), (0.12, 0.12), (0.25, 0.04), (0.42, 0.005), (0.60, 0.02), (0.75, 0.08),
            (0.87, 0.17), (0.955, 0.29), (0.99, 0.41), (0.975, 0.52), (0.93, 0.585), (0.84, 0.625), (0.72, 0.645),
            (0.62, 0.685), (0.54, 0.735), (0.42, 0.75), (0.31, 0.735), (0.22, 0.695), (0.175, 0.635),
            (0.19, 0.588), (0.13, 0.562), (0.06, 0.50)]
BRAIN_SULCI = [  # (points, weight): landmarks heavier, secondary folds lighter
    ([(0.19, 0.588), (0.30, 0.525), (0.44, 0.475), (0.56, 0.425), (0.63, 0.355)], 1.7),  # lateral fissure
    ([(0.49, 0.01), (0.48, 0.12), (0.45, 0.22), (0.46, 0.31), (0.42, 0.435)], 1.4),      # central sulcus
    ([(0.10, 0.19), (0.20, 0.14), (0.33, 0.13)], 0.8),                                   # superior frontal
    ([(0.07, 0.34), (0.18, 0.30), (0.31, 0.31)], 0.8),                                   # inferior frontal
    ([(0.60, 0.20), (0.70, 0.16), (0.82, 0.23)], 0.8),                                   # intraparietal
    ([(0.25, 0.65), (0.37, 0.62), (0.50, 0.58), (0.62, 0.51)], 1.0),                     # superior temporal
    ([(0.80, 0.33), (0.88, 0.38), (0.93, 0.47)], 0.7),                                   # occipital
]
FOLIA = [[(0.66, 0.70), (0.80, 0.655), (0.97, 0.66)], [(0.67, 0.745), (0.81, 0.71), (0.97, 0.715)],
         [(0.69, 0.79), (0.82, 0.765), (0.955, 0.76)], [(0.73, 0.83), (0.84, 0.815), (0.92, 0.80)]]


def _wiggle(pts, amp, rng):
    """Insert a sideways-offset midpoint in every segment so folds meander like gyri."""
    out = [pts[0]]
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        L = math.hypot(x2 - x1, y2 - y1) or 1
        off = rng.choice((-1, 1)) * amp * rng.uniform(0.5, 1)
        out += [((x1 + x2) / 2 - (y2 - y1) / L * off, (y1 + y2) / 2 + (x2 - x1) / L * off), (x2, y2)]
    return out


def brain_art(P, bx: float, by: float, bw: float, bh: float, seed: int = 7):
    """Glowing wireframe brain: cerebrum with grown folds (scripts/brain_folds.json),
    striated cerebellum and brainstem, drawn back to front. Seeded, so it is
    identical on every refresh. Returns (svg, shapes, at)."""
    import random
    rng = random.Random(seed)
    s = bw / 318                              # detail scale vs. the size the folds were grown at
    at = lambda u, v: (bx + u * bw, by + v * bh)  # noqa: E731
    shapes = {k: _catmull([at(u, v) for u, v in pts], closed=True)
              for k, pts in (("stem", BRAINSTEM), ("cbl", CEREBELLUM), ("cbr", CEREBRUM))}
    covers = {"stem": ["cbl", "cbr"], "cbl": ["cbr"], "cbr": []}

    def visible(pt, part):
        return _inside(pt, shapes[part][1]) and not any(_inside(pt, shapes[c][1]) for c in covers[part])

    def edge_dist(pt, part):
        return min(math.dist(pt, q) for q in shapes[part][1][::3])

    folds_file = Path(__file__).with_name("brain_folds.json")
    folds = json.loads(folds_file.read_text(encoding="utf-8"))["folds"] if folds_file.exists() else []
    out = [f'<ellipse cx="{bx + 0.5 * bw:.1f}" cy="{by + 0.42 * bh:.1f}" rx="{bw * 0.72:.1f}" '
           f'ry="{bh * 0.66:.1f}" fill="url(#bloom)"/>']
    for part in ("stem", "cbl", "cbr"):
        d, poly = shapes[part]
        rim = [q for q in poly[::5] if not any(_inside(q, shapes[c][1]) for c in covers[part])]
        inner: list[tuple[float, float]] = []
        for _ in range(7000 if part == "cbr" else 2500):
            q = (bx + rng.random() * bw, by + rng.random() * bh)
            if visible(q, part) and edge_dist(q, part) > 6 * s and all(math.dist(q, o) > 19 * s for o in inner):
                inner.append(q)
        nodes = rim + inner
        edges = set()
        for i, q in enumerate(nodes):
            for dd, j in sorted((math.dist(q, o), j) for j, o in enumerate(nodes) if j != i)[:4]:
                mid = ((q[0] + nodes[j][0]) / 2, (q[1] + nodes[j][1]) / 2)
                if dd <= 36 * s and _inside(mid, poly) and not (i < len(rim) and j < len(rim)):
                    edges.add((min(i, j), max(i, j)))
        out.append(f'<path d="{d}" fill="{P.node}" fill-opacity="0.94"/><path d="{d}" fill="url(#brainFill)"/>')
        if part == "cbr":
            out.append(f'<ellipse cx="{bx + 0.48 * bw:.1f}" cy="{by + 0.34 * bh:.1f}" rx="{bw * 0.46:.1f}" '
                       f'ry="{bh * 0.32:.1f}" fill="url(#core)"><animate attributeName="opacity" values="0.55;1;0.55" '
                       f'dur="4.8s" repeatCount="indefinite"/></ellipse>')
        firing = set(rng.sample(sorted(edges), min(len(edges) // 6, 34)))
        for (i, j) in sorted(edges):
            (x1, y1), (x2, y2) = nodes[i], nodes[j]
            anim = ""
            if (i, j) in firing:
                anim = (f'<animate attributeName="stroke-opacity" values="0.1;0.9;0.1;0.1" keyTimes="0;0.12;0.3;1" '
                        f'dur="{rng.uniform(3.2, 5.6):.1f}s" begin="{rng.uniform(0, 5):.1f}s" repeatCount="indefinite"/>')
            out.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{P.ink2}" '
                       f'stroke-opacity="0.1" stroke-width="0.8">{anim}</line>')
        if part == "cbl":
            a0, a1 = FOLIA[0], FOLIA[-1]
            out.append(f'<clipPath id="cblClip"><path d="{d}"/></clipPath><g clip-path="url(#cblClip)" filter="url(#glow)">')
            for k in range(9):
                t = k / 8
                pts = [at(a0[m][0] * (1 - t) + a1[m][0] * t + (0.03 if m == 2 else -0.03 if m == 0 else 0),
                          a0[m][1] * (1 - t) + a1[m][1] * t) for m in range(3)]
                fd, _ = _catmull(_wiggle(pts, 1.6 * s, rng), closed=False)
                out.append(f'<path d="{fd}" fill="none" stroke="{P.ink2}" stroke-opacity="0.6" stroke-width="{1.1 * s:.2f}"/>')
            out.append("</g>")
        if part == "cbr":
            out.append(f'<clipPath id="cbrClip"><path d="{d}"/></clipPath><g clip-path="url(#cbrClip)" filter="url(#glow)">')
            for fold in folds:
                fd, _ = _catmull([at(u, v) for u, v in fold], closed=False)
                out.append(f'<path d="{fd}" fill="none" stroke="{P.ink2}" stroke-opacity="0.62" '
                           f'stroke-width="{1.05 * s:.2f}" stroke-linecap="round"/>')
            for pts, _w in BRAIN_SULCI[:2]:
                sd, _ = _catmull(_wiggle([at(u, v) for u, v in pts], 3.5 * s, rng), closed=False)
                out.append(f'<path d="{sd}" fill="none" stroke="{P.ink}" stroke-opacity="0.7" '
                           f'stroke-width="{1.9 * s:.2f}" stroke-linecap="round"/>')
            out.append("</g>")
        out.append(f'<path d="{d}" fill="none" stroke="url(#chromeEdge)" stroke-width="{1.7 * s:.2f}"/>')
        for i, (x, y) in enumerate(nodes):
            if i < len(rim):
                continue
            roll = rng.random()
            r = (2.4 if roll < 0.1 else 1.5 if roll < 0.45 else 1.0) * s
            tw = (f'<animate attributeName="opacity" values="1;0.2;1" dur="{rng.uniform(2.2, 4.8):.1f}s" '
                  f'begin="{rng.uniform(0, 4):.1f}s" repeatCount="indefinite"/>' if rng.random() < 0.35 else "")
            glow = f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{8 * s:.1f}" fill="url(#hubGlow)" opacity="0.75"/>' if roll < 0.1 else ""
            out.append(f'{glow}<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.2f}" fill="{P.ink}">{tw}</circle>')
    return "".join(out), shapes, at


CORE_ICONS = {  # 44 × 44 line icons
    "agent": '<rect x="9" y="15" width="26" height="21" rx="6"/><circle cx="17" cy="25.5" r="2.4"/>'
             '<circle cx="27" cy="25.5" r="2.4"/><path d="M22 15V9"/><circle cx="22" cy="7" r="2"/>'
             '<path d="M9 24H5M39 24H35M17 31.5H27"/>',
    "rag": '<path d="M8 5H24L30 11V35H8Z"/><path d="M24 5V11H30"/><path d="M12 16H24M12 21H22M12 26H18"/>'
           '<circle cx="30" cy="30" r="7"/><path d="M35 35L40 40"/>',
    "gen": '<path d="M38 5C26 7 15 16 11 33L10 38"/><path d="M38 5C37 16 29 27 14 31"/><path d="M20 20L28 13"/>'
           '<path d="M6 40H30"/><path d="M8 5L9.5 9L13.5 10.5L9.5 12L8 16L6.5 12L2.5 10.5L6.5 9Z"/>',
    "llm": '<path d="M6 8H34A4 4 0 0 1 38 12V26A4 4 0 0 1 34 30H18L10 37V30H6A4 4 0 0 1 2 26V12A4 4 0 0 1 6 8Z"/>'
           '<circle cx="12" cy="19" r="1.8"/><circle cx="20" cy="19" r="1.8"/><circle cx="28" cy="19" r="1.8"/>',
    "ml": '<path d="M5 5V39H40"/><rect x="10" y="26" width="5" height="10"/><rect x="18" y="20" width="5" height="16"/>'
          '<rect x="26" y="14" width="5" height="22"/><path d="M9 20L18 13L26 16L38 6"/><circle cx="38" cy="6" r="2"/>',
    "dl": '<path d="M22 5L39 13L22 21L5 13Z"/><path d="M5 20L22 28L39 20"/><path d="M5 27L22 35L39 27"/>',
}


def core(P) -> str:
    """The AI core: a glowing brain at the centre, an AI badge above and a holographic
    platform below on one axis, and circuit traces carrying signals out to one panel
    per technology (config "brain"), three on each side."""
    import random
    rng = random.Random(21)
    W, H = 1200, 760
    ax = W / 2
    techs = CFG["brain"]
    bw = 400
    bh = bw * 280 / 318
    bx, by = 380, 176
    art, shapes, at = brain_art(P, bx, by, bw, bh)
    def crossings(poly, *, y=None, x=None):
        """Exact intersections of a closed polyline with a horizontal (y) or vertical (x) line."""
        hits = []
        for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
            if y is not None and (y1 - y) * (y2 - y) <= 0 and y1 != y2:
                hits.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
            if x is not None and (x1 - x) * (x2 - x) <= 0 and x1 != x2:
                hits.append(y1 + (x - x1) * (y2 - y1) / (x2 - x1))
        return hits

    def edge_x(y, side):
        xs = [h for _d, poly in shapes.values() for h in crossings(poly, y=y)]
        return (min(xs) if side < 0 else max(xs)) if xs else None

    def top_y(x):
        return min(crossings(shapes["cbr"][1], x=x))

    PW, PH = 270, 118
    rows = [150, 330, 510]
    panels = [(34 if i < 3 else W - 34 - PW, rows[i % 3], -1 if i < 3 else 1, t) for i, t in enumerate(techs)]

    # Main traces: brain edge → panel, PCB-style with 45° chamfers.
    mains = []
    for i, (px, py, side, _t) in enumerate(panels):
        ty = py + PH / 2
        sy = by + (0.24, 0.46, 0.70)[i % 3] * bh
        ex = edge_x(sy, side)
        x1 = ex + side * 14
        pe = px + PW if side < 0 else px
        dy = ty - sy
        if abs(dy) <= abs(pe - x1) - 22:
            d = f"M{ex:.1f},{sy:.1f} H{x1:.1f} L{x1 + side * abs(dy):.1f},{ty:.1f} H{pe:.1f}"
        else:
            c, g = 12, (1 if dy > 0 else -1)
            d = (f"M{ex:.1f},{sy:.1f} H{x1:.1f} L{x1 + side * c:.1f},{sy + g * c:.1f} V{ty - g * c:.1f} "
                 f"L{x1 + side * 2 * c:.1f},{ty:.1f} H{pe:.1f}")
        mains.append((d, sy, side, pe, ty))

    # Decorative traces fanning out from both sides; a few run behind the panels to the edge.
    yc = by + 0.42 * bh
    back, deco, vias = [], [], []
    for side in (-1, 1):
        limit = 34 + PW + 12 if side < 0 else W - 34 - PW - 12
        y = by + 0.08 * bh
        while y <= by + 0.9 * bh:
            yy, y = y, y + 15
            if any(abs(yy - m[1]) < 10 and m[2] == side for m in mains):
                continue
            ex = edge_x(yy, side)
            if ex is None:
                continue
            x1 = ex + side * rng.uniform(8, 22)
            jog = (yy - yc) * 0.28
            x2, y2 = x1 + side * abs(jog), yy + jog
            if (side < 0 and x2 < limit) or (side > 0 and x2 > limit):
                continue
            if rng.random() < 0.24:
                back.append(f"M{ex:.1f},{yy:.1f} H{x1:.1f} L{x2:.1f},{y2:.1f} H{12 if side < 0 else W - 12}")
            else:
                xe = x2 + side * rng.uniform(10, 46)
                xe = max(xe, limit) if side < 0 else min(xe, limit)
                deco.append(f"M{ex:.1f},{yy:.1f} H{x1:.1f} L{x2:.1f},{y2:.1f} H{xe:.1f}")
                vias.append((xe, y2))

    # The vertical axis: brain → AI badge above, base node → brainstem below.
    badge_y, badge_r = 76, 42
    node_y = 652
    t0 = top_y(ax)
    up_path = f"M{ax},{t0:.1f} V{badge_y + badge_r + 2}"
    for dx in (-24, 24):
        sgn = 1 if dx > 0 else -1
        x0 = ax + dx
        deco.append(f"M{x0},{top_y(x0):.1f} V170 L{x0 + sgn * 44},126 V40")
        vias.append((x0 + sgn * 44, 40))
    for dx in (-70, 70):
        sgn = 1 if dx > 0 else -1
        x0 = ax + dx
        deco.append(f"M{x0},{top_y(x0):.1f} V150 L{x0 + sgn * 30},120 H{x0 + sgn * 120}")
        vias.append((x0 + sgn * 120, 120))
    stem_x, stem_y = at(0.612, 0.995)
    base_path = f"M{ax},{node_y - 26} V{stem_y + 54:.1f} L{stem_x:.1f},{stem_y + 30:.1f} V{stem_y - 2:.1f}"
    for u, v, sgn in ((0.83, 0.845, 1), (0.36, 0.745, -1)):
        x0, y0 = at(u, v)
        deco.append(f"M{x0:.1f},{y0:.1f} V{y0 + 70:.1f} L{x0 - sgn * 32:.1f},{y0 + 102:.1f} V{node_y - 40}")
        vias.append((x0 - sgn * 32, node_y - 40))

    out = []
    # Dust.
    for _ in range(90):
        x, y = rng.uniform(16, W - 16), rng.uniform(16, H - 16)
        if bx - 20 < x < bx + bw + 20 and by - 20 < y < by + bh + 20:
            continue
        tw = (f'<animate attributeName="opacity" values="0.7;0.1;0.7" dur="{rng.uniform(2.5, 6):.1f}s" '
              f'begin="{rng.uniform(0, 4):.1f}s" repeatCount="indefinite"/>' if rng.random() < 0.35 else "")
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{rng.uniform(0.6, 1.5):.2f}" fill="{P.ink3}" '
                   f'opacity="{rng.uniform(0.25, 0.7):.2f}">{tw}</circle>')
    trace = lambda d, op=0.5, w=1.2, extra="": (  # noqa: E731
        f'<path d="{d}" fill="none" stroke="{P.ink3}" stroke-opacity="{op}" stroke-width="{w}" '
        f'stroke-linejoin="round"{extra}/>')
    out += [trace(d, 0.28, 1.1) for d in back]
    out.append(art)
    out += [trace(d, 0.55, 1.2) for d in deco]
    out.append(trace(up_path, 0.85, 1.8) + trace(base_path, 0.85, 1.8))
    for i, (d, *_rest) in enumerate(mains):
        out.append(f'<path id="main{i}" d="{d}" fill="none" stroke="{P.ink2}" stroke-opacity="0.85" '
                   f'stroke-width="1.6" stroke-linejoin="round"/>')
    out += [f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.8" fill="{P.node}" stroke="{P.ink3}" stroke-width="1.2"/>'
            for x, y in vias]

    # AI badge.
    out.append(f'<circle cx="{ax}" cy="{badge_y}" r="95" fill="url(#bloom)"/>'
               f'<circle cx="{ax}" cy="{badge_y}" r="62" fill="none" stroke="{P.hair2}"/>'
               f'<circle cx="{ax}" cy="{badge_y}" r="54" fill="none" stroke="{P.ink3}" stroke-opacity="0.7" '
               f'stroke-width="1.5" stroke-dasharray="2 7"><animateTransform attributeName="transform" type="rotate" '
               f'from="0 {ax} {badge_y}" to="360 {ax} {badge_y}" dur="40s" repeatCount="indefinite"/></circle>'
               f'<circle cx="{ax}" cy="{badge_y}" r="{badge_r}" fill="{P.node}" stroke="url(#chromeEdge)" stroke-width="2.2"/>'
               f'<circle cx="{ax}" cy="{badge_y}" r="{badge_r}" fill="none" stroke="{P.ink}" stroke-width="2.4" opacity="0">'
               f'<animate attributeName="opacity" values="0;0;1;0;0" keyTimes="0;0.6;0.63;0.9;1" dur="2.4s" repeatCount="indefinite"/></circle>'
               f'<text x="{ax}" y="{badge_y + 12}" text-anchor="middle" font-size="34" font-weight="700" '
               f'letter-spacing="1" fill="url(#chrome)">AI</text>')

    # Holographic base.
    out.append(f'<path d="M{ax - 34},{node_y + 64} L{ax - 20},{node_y} H{ax + 20} L{ax + 34},{node_y + 64} Z" fill="url(#beam)"/>')
    for rx, ry, op in ((190, 26, 0.35), (135, 18, 0.5), (82, 11, 0.75)):
        out.append(f'<ellipse cx="{ax}" cy="{node_y + 64}" rx="{rx}" ry="{ry}" fill="none" stroke="{P.ink3}" '
                   f'stroke-opacity="{op}" stroke-width="1.3"/>')
    out.append(f'<ellipse cx="{ax}" cy="{node_y + 64}" rx="60" ry="8" fill="none" stroke="{P.ink}" stroke-width="1.4">'
               f'<animate attributeName="rx" values="60;230" dur="3s" repeatCount="indefinite"/>'
               f'<animate attributeName="ry" values="8;32" dur="3s" repeatCount="indefinite"/>'
               f'<animate attributeName="opacity" values="0.8;0" dur="3s" repeatCount="indefinite"/></ellipse>'
               f'<circle cx="{ax}" cy="{node_y}" r="60" fill="url(#bloom)"/>'
               f'<circle cx="{ax}" cy="{node_y}" r="25" fill="{P.node}" stroke="url(#chromeEdge)" stroke-width="2"/>'
               f'<circle cx="{ax}" cy="{node_y}" r="15" fill="none" stroke="{P.ink3}" stroke-width="1.3"/>'
               f'<circle cx="{ax}" cy="{node_y}" r="5.5" fill="{P.ink}"><animate attributeName="r" values="4.5;7;4.5" '
               f'dur="2.4s" repeatCount="indefinite"/></circle>')

    # Signals.
    def pulse(path_ref: str, dur: float, begin: float, travel: float, r: float = 2.8) -> str:
        return (f'<g opacity="0"><circle r="{r * 3.2:.1f}" fill="url(#hubGlow)"/><circle r="{r}" fill="{P.ink}"/>'
                f'<animateMotion dur="{dur:.2f}s" begin="{begin:.2f}s" repeatCount="indefinite" keyPoints="0;1;1" '
                f'keyTimes="0;{travel};1" calcMode="linear"><mpath xlink:href="#{path_ref}"/></animateMotion>'
                f'<animate attributeName="opacity" dur="{dur:.2f}s" begin="{begin:.2f}s" repeatCount="indefinite" '
                f'values="0;1;1;0;0" keyTimes="0;0.04;{travel - 0.03:.2f};{travel:.2f};1"/></g>')

    out.append(f'<path id="up" d="{up_path}" fill="none"/><path id="base" d="{base_path}" fill="none"/>')
    out.append(pulse("up", 2.4, 0, 0.6) + pulse("base", 2.4, 1.2, 0.6))
    for k, d in enumerate(rng.sample(back + deco, min(10, len(back) + len(deco)))):
        out.append(f'<path id="deco{k}" d="{d}" fill="none"/>' + pulse(f"deco{k}", rng.uniform(2.4, 4.2), rng.uniform(0, 3), 0.7, 2.2))
    T = 7.2
    step = T / len(panels)
    for i in range(len(panels)):
        start = i * step
        out.append(pulse(f"main{i}", T, start, round(0.9 / T, 4), 3.0))

    # Panels.
    for i, (px, py, side, t) in enumerate(panels):
        arrive = i * step + 0.9
        a, b = arrive / T, min((arrive + step) / T, 0.999)
        win = (f'<animate attributeName="opacity" calcMode="discrete" values="0;1;0" keyTimes="0;{a:.4f};{b:.4f}" '
               f'dur="{T}s" repeatCount="indefinite"/>')
        _d, _sy, _side, pe, ty = mains[i]
        icon_svg = CORE_ICONS.get(t.get("icon", "agent"), CORE_ICONS["agent"])
        bars = "".join(f'<rect x="{px + PW - 58 + k * 7}" y="{py + 30 - hgt:.1f}" width="3.5" height="{hgt:.1f}" '
                       f'rx="1" fill="{P.ink4}"/>' for k, hgt in enumerate(rng.uniform(4, 15) for _ in range(6)))
        filled = rng.randint(5, 11)
        segs = "".join(f'<rect x="{px + 88 + k * 14}" y="{py + PH - 24}" width="11" height="3" rx="1.5" '
                       f'fill="{P.ink3 if k < filled else P.hair2}"/>' for k in range(12))
        brackets = (f'<path d="M{px + 6},{py + 20} V{py + 6} H{px + 20} M{px + PW - 20},{py + 6} H{px + PW - 6} V{py + 20} '
                    f'M{px + 6},{py + PH - 20} V{py + PH - 6} H{px + 20} M{px + PW - 20},{py + PH - 6} H{px + PW - 6} '
                    f'V{py + PH - 20}" fill="none" stroke="{P.ink3}" stroke-width="1.5"/>')
        out.append(f'<g class="rise" style="animation-delay:{0.15 + i * 0.1:.2f}s">'
                   f'<rect x="{px}" y="{py}" width="{PW}" height="{PH}" rx="16" fill="{P.node}" fill-opacity="0.9" stroke="{P.hair2}"/>'
                   f'<rect x="{px}" y="{py}" width="{PW}" height="{PH}" rx="16" fill="{P.plate}" fill-opacity="{P.plate_op}"/>'
                   f'{brackets}{bars}{segs}'
                   f'<g transform="translate({px + 20},{py + 32}) scale(1.2)" fill="none" stroke="{P.ink3}" stroke-width="1.5" '
                   f'stroke-linecap="round" stroke-linejoin="round">{icon_svg}</g>'
                   f'<g transform="translate({px + 20},{py + 32}) scale(1.2)" fill="none" stroke="{P.ink}" stroke-width="1.6" '
                   f'stroke-linecap="round" stroke-linejoin="round" opacity="0">{icon_svg}{win}</g>'
                   f'<text x="{px + 88}" y="{py + 54}" font-size="19" font-weight="600" fill="{P.ink}">{esc(t["label"])}</text>'
                   f'<text x="{px + 88}" y="{py + 76}" class="mono" font-size="13" fill="{P.ink3}">{esc(t["sub"])}</text>'
                   f'<rect x="{px}" y="{py}" width="{PW}" height="{PH}" rx="16" fill="none" stroke="{P.ink}" stroke-width="1.6" opacity="0">{win}</rect>'
                   f'<circle cx="{pe}" cy="{ty}" r="3.6" fill="{P.ink}"/></g>')

    defs = (chrome_gradient(P) + chrome_gradient(P, "chromeEdge", "0", "0", "1", "1")
            + '<filter id="glow" x="-10%" y="-10%" width="120%" height="120%"><feGaussianBlur stdDeviation="2.2" result="b"/>'
              '<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>'
            + f'<radialGradient id="bloom"><stop offset="0" stop-color="{P.ink}" stop-opacity="{0.2 if P.mode == "dark" else 0.12}"/>'
              f'<stop offset="1" stop-color="{P.ink}" stop-opacity="0"/></radialGradient>'
            + f'<radialGradient id="core"><stop offset="0" stop-color="{P.ink}" stop-opacity="{0.34 if P.mode == "dark" else 0.16}"/>'
              f'<stop offset="0.55" stop-color="{P.ink}" stop-opacity="{0.08 if P.mode == "dark" else 0.05}"/>'
              f'<stop offset="1" stop-color="{P.ink}" stop-opacity="0"/></radialGradient>'
            + f'<radialGradient id="brainFill" cx="0.45" cy="0.35" r="0.7"><stop offset="0" stop-color="{P.ink}" stop-opacity="0.07"/>'
              f'<stop offset="1" stop-color="{P.ink}" stop-opacity="0.015"/></radialGradient>'
            + f'<radialGradient id="hubGlow"><stop offset="0" stop-color="{P.ink}" stop-opacity="0.6"/>'
              f'<stop offset="1" stop-color="{P.ink}" stop-opacity="0"/></radialGradient>'
            + f'<linearGradient id="beam" x1="0" y1="1" x2="0" y2="0"><stop offset="0" stop-color="{P.ink}" stop-opacity="0.22"/>'
              f'<stop offset="1" stop-color="{P.ink}" stop-opacity="0"/></linearGradient>')
    techs_desc = ", ".join(f"{t['label']} ({t['sub']})" for t in techs)
    return card(P, W, H, "".join(out), defs=defs, rx=30, sweep_period=10, title="AI core",
                desc=f"A glowing brain between an AI badge and a holographic base, with circuit traces "
                     f"carrying signals to {techs_desc}.")


def hero(P, M) -> str:
    W, H = 1200, 400
    name, eyebrow, status = CFG["name"], CFG["eyebrow"], CFG["status"]
    lp = M["last_push"]
    push_line = (f"last push  →  {display_name(lp['name'])}  ·  {fdate(parse_ts(lp['pushed_at']).date())}"
                 if lp else "")
    pill_w = 46 + len(status) * 9 + 22
    px = W / 2 - pill_w / 2

    # Faint circuit traces at both edges, echoing the AI core below.
    import random
    rng = random.Random(3)
    edge = []
    for side in (-1, 1):
        for k in range(6):
            y = 92 + k * 44
            x0 = 0 if side < 0 else W
            x1 = x0 - side * rng.uniform(50, 110)
            jog = rng.choice((-1, 1)) * rng.uniform(8, 18)
            xe = x1 - side * (abs(jog) + rng.uniform(30, 90))
            d = f"M{x0},{y} H{x1:.1f} L{x1 - side * abs(jog):.1f},{y + jog:.1f} H{xe:.1f}"
            edge.append(f'<path id="e{side}{k}" d="{d}" fill="none" stroke="{P.ink3}" stroke-opacity="0.35" stroke-width="1.2"/>'
                        f'<circle cx="{xe:.1f}" cy="{y + jog:.1f}" r="2.6" fill="{P.node}" stroke="{P.ink3}" stroke-opacity="0.6"/>')
            if k in (1, 4):
                dur, begin = rng.uniform(2.6, 3.6), rng.uniform(0, 2)
                edge.append(f'<g opacity="0"><circle r="7" fill="url(#pulseGlow)"/><circle r="2.2" fill="{P.ink}"/>'
                            f'<animateMotion dur="{dur:.2f}s" begin="{begin:.2f}s" repeatCount="indefinite" keyPoints="0;1;1" '
                            f'keyTimes="0;0.75;1" calcMode="linear"><mpath xlink:href="#e{side}{k}"/></animateMotion>'
                            f'<animate attributeName="opacity" dur="{dur:.2f}s" begin="{begin:.2f}s" repeatCount="indefinite" '
                            f'values="0;1;1;0;0" keyTimes="0;0.05;0.72;0.75;1"/></g>')

    body = f"""
<g>{"".join(edge)}</g>
<g class="rise" style="animation-delay:.05s"><text x="{W / 2}" y="94" text-anchor="middle" font-size="15" font-weight="600" letter-spacing="3.4" fill="{P.ink3}">{esc(eyebrow)}</text></g>
<g class="rise" style="animation-delay:.15s">
<text x="{W / 2}" y="204" text-anchor="middle" font-size="112" font-weight="700" letter-spacing="-4" fill="url(#chrome)">{esc(name)}</text>
<text x="{W / 2}" y="204" text-anchor="middle" font-size="112" font-weight="700" letter-spacing="-4" fill="url(#textShine)">{esc(name)}</text>
</g>
<g class="rise" style="animation-delay:.3s">
<text x="{W / 2}" y="252" text-anchor="middle" font-size="28" font-weight="500" fill="{P.ink2}">{esc(CFG['role'])}</text>
<text x="{W / 2}" y="286" text-anchor="middle" font-size="19" fill="{P.ink3}">{esc(CFG['company'])}<tspan fill="{P.ink4}">  ·  </tspan>{esc(CFG['city'])}</text>
</g>
<g class="rise" style="animation-delay:.45s">
<rect x="{px:.0f}" y="308" width="{pill_w:.0f}" height="38" rx="19" fill="{P.plate}" fill-opacity="{P.plate_op * 1.5:.3f}" stroke="{P.hair2}"/>
<circle cx="{px + 23:.0f}" cy="327" r="5" fill="{P.ink}"/>
<circle cx="{px + 23:.0f}" cy="327" r="5" fill="none" stroke="{P.ink}" stroke-width="1.5"><animate attributeName="r" values="5;14" dur="2.2s" repeatCount="indefinite"/><animate attributeName="opacity" values="0.8;0" dur="2.2s" repeatCount="indefinite"/></circle>
<text x="{px + 42:.0f}" y="332" class="mono" font-size="15" fill="{P.ink2}">{esc(status)}</text>
<text x="{W / 2}" y="378" text-anchor="middle" class="mono" font-size="14" fill="{P.ink4}">{esc(push_line)}</text>
</g>"""

    defs = (chrome_gradient(P) + shine_gradient(P, 300, (150, 1100), 7)
            + f'<radialGradient id="pulseGlow"><stop offset="0" stop-color="{P.ink}" stop-opacity="0.55"/>'
              f'<stop offset="1" stop-color="{P.ink}" stop-opacity="0"/></radialGradient>')
    return card(P, W, H, body, rx=30, defs=defs, sweep_period=8,
                title=f"{name} — {CFG['role']} at {CFG['company']}",
                desc=f"{CFG['city']}. Status: {status}. {push_line}.")


# --------------------------------------------------------------------------
# Typing tagline — a prompt bar with per-character typing (SMIL, discrete).
# --------------------------------------------------------------------------

def typing(P, phrases: list[str]) -> str:
    W, H = 1200, 84
    fs = 24
    cw = fs * 0.6              # monospace advance; textLength pins it exactly
    x0 = 92
    t_type, t_hold, t_del, t_gap = 0.052, 2.1, 0.02, 0.45
    frames, windows, t = [], [], 0.0
    for p in phrases:
        start, n = t, len(p)
        for k in range(n + 1):
            frames.append((t, k))
            t += t_type if k < n else t_hold
        for k in range(n - 1, -1, -1):
            frames.append((t, k))
            t += t_del if k > 0 else t_gap
        windows.append((start, t))
    T = t
    kt = ";".join(f"{f[0] / T:.5f}" for f in frames)
    widths = ";".join(f"{f[1] * cw:.1f}" for f in frames)
    cursor_x = ";".join(f"{x0 + f[1] * cw + 1:.1f}" for f in frames)

    texts = ""
    for i, p in enumerate(phrases):
        a, b = windows[i][0] / T, windows[i][1] / T
        vis = (f'values="1;0" keyTimes="0;{b:.5f}"' if i == 0
               else f'values="0;1;0" keyTimes="0;{a:.5f};{b:.5f}"')
        texts += (f'<text x="{x0}" y="51" class="mono" font-size="{fs}" fill="{P.ink}" '
                  f'textLength="{len(p) * cw:.1f}" lengthAdjust="spacingAndGlyphs" opacity="{1 if i == 0 else 0}">{esc(p)}'
                  f'<animate attributeName="opacity" calcMode="discrete" {vis} dur="{T:.2f}s" repeatCount="indefinite"/></text>')

    first_w = len(phrases[0]) * cw
    body = f"""
<g transform="translate(40,26)" fill="{P.ink3}"><path d="M16 0C16.9 7.6 20.4 11.1 28 12C20.4 12.9 16.9 16.4 16 24C15.1 16.4 11.6 12.9 4 12C11.6 11.1 15.1 7.6 16 0Z" transform="scale(0.75)"/><circle cx="24" cy="3" r="1.7"/></g>
<clipPath id="typed"><rect x="{x0}" y="14" height="56" width="{first_w:.1f}"><animate attributeName="width" calcMode="discrete" values="{widths}" keyTimes="{kt}" dur="{T:.2f}s" repeatCount="indefinite"/></rect></clipPath>
<g clip-path="url(#typed)">{texts}</g>
<rect y="28" width="2.6" height="30" rx="1" fill="{P.ink}" x="{x0 + first_w + 1:.1f}"><animate attributeName="x" calcMode="discrete" values="{cursor_x}" keyTimes="{kt}" dur="{T:.2f}s" repeatCount="indefinite"/><animate attributeName="opacity" values="1;1;0;0" keyTimes="0;0.5;0.5;1" calcMode="discrete" dur="1.05s" repeatCount="indefinite"/></rect>
<text x="{W - 94}" y="47" text-anchor="end" class="mono" font-size="13" letter-spacing="1.4" fill="{P.ink4}">STREAMING</text>
<circle cx="{W - 56}" cy="42" r="20" fill="{P.ink}" fill-opacity="{0.92 if P.mode == 'dark' else 0.88}"/>
<path d="M{W - 56},51 V33 M{W - 63.5},40 L{W - 56},32.5 L{W - 48.5},40" fill="none" stroke="{P.bg_bot}" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"/>"""
    return card(P, W, H, body, rx=42, sweep_period=11, title="Tagline", desc=" / ".join(phrases))


# --------------------------------------------------------------------------
# Spec sheet — the headline numbers, each tied to a repo.
# --------------------------------------------------------------------------

def specs(P, M) -> str:
    W, H = 1200, 342
    items = CFG["specs"]
    cols, pad, y0, row_h = 3, 40, 70, 128
    cell_w = (W - 2 * pad) / cols
    body = (f'<text x="{pad}" y="48" font-size="15" font-weight="600" letter-spacing="3" fill="{P.ink3}">TECH SPECS</text>'
            f'<text x="{W - pad}" y="48" text-anchor="end" class="mono" font-size="13.5" fill="{P.ink4}">'
            f'every number is measured in a public repo · expand a project below for the method</text>')
    for i, s in enumerate(items):
        c, r = i % cols, i // cols
        x, y = pad + c * cell_w, y0 + r * row_h
        if c:
            body += f'<line x1="{x - 1:.1f}" y1="{y + 18}" x2="{x - 1:.1f}" y2="{y + row_h - 12}" stroke="{P.hair}"/>'
        lx = x + (26 if c else 0)
        body += (f'<g class="rise" style="animation-delay:{0.1 + i * 0.1:.1f}s">'
                 f'<text x="{lx:.1f}" y="{y + 62}" font-size="56" font-weight="600" letter-spacing="-1.5" fill="url(#chrome)">{esc(s["value"])}</text>'
                 f'<text x="{lx:.1f}" y="{y + 92}" font-size="18" fill="{P.ink2}">{esc(s["label"])}</text>'
                 f'<text x="{lx:.1f}" y="{y + 115}" class="mono" font-size="13.5" fill="{P.ink4}">{esc(s["proof"])}</text></g>')
    body += f'<line x1="{pad}" y1="{y0 + row_h}" x2="{W - pad}" y2="{y0 + row_h}" stroke="{P.hair}"/>'
    return card(P, W, H, body, defs=chrome_gradient(P),
                title="Tech specs", desc="; ".join(f"{s['value']} {s['label']} ({s['proof']})" for s in items))


# --------------------------------------------------------------------------
# Stats · Streak · Languages · Evals · Trophies · Dashboard · Footer
# --------------------------------------------------------------------------

def sparkline(P, vals: list[int], x: float, y: float, w: float, h: float) -> str:
    if not vals:
        return ""
    mx = max(vals) or 1
    step = w / (len(vals) - 1 or 1)
    pts = [(x + i * step, y + h - (v / mx) * h) for i, v in enumerate(vals)]
    line = "M" + " L".join(f"{a:.1f},{b:.1f}" for a, b in pts)
    area = line + f" L{pts[-1][0]:.1f},{y + h} L{pts[0][0]:.1f},{y + h} Z"
    ex, ey = pts[-1]
    return (f'<path d="{area}" fill="{P.mark}" fill-opacity="0.10"/>'
            f'<path d="{line}" fill="none" stroke="{P.mark}" stroke-width="2" stroke-linejoin="round" '
            f'stroke-linecap="round" class="draw" pathLength="1"/>'
            f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="4.5" fill="{P.ink}" stroke="{P.surface}" stroke-width="2"/>')


DRAW_CSS = (".draw{stroke-dasharray:1;stroke-dashoffset:0;animation:draw 1.6s .4s cubic-bezier(.3,.6,.2,1) both;}"
            "@keyframes draw{from{stroke-dashoffset:1;}}")
GROW_CSS = (".grow{transform-box:fill-box;transform-origin:left center;animation:grow 1.1s cubic-bezier(.2,.7,.2,1) both;}"
            "@keyframes grow{from{transform:scaleX(0);}}")


def card_title(P, W: int, title: str, stamp: str | None, sub: str | None = None) -> str:
    out = f'<text x="32" y="52" font-size="22" font-weight="600" fill="{P.ink}">{esc(title)}</text>'
    if stamp:
        out += live_badge(P, W - 32, 46, stamp)
    if sub:
        out += f'<text x="32" y="80" class="mono" font-size="12.5" fill="{P.ink4}">{esc(sub)}</text>'
    return out


def stats(P, M, stamp: str) -> str:
    W, H = 600, 300
    rows = [("commit", "Commits", M["commits"]), ("repo", "Public repositories", M["repos"]),
            ("live", "Live demos", M["live_demos"]), ("code", "Languages", M["languages_all"]),
            ("star", "Stars from others", M["stars"])]
    body = (card_title(P, W, "GitHub activity", stamp)
            + f'<g class="rise" style="animation-delay:.1s"><text x="30" y="152" font-size="76" font-weight="600" letter-spacing="-2.5" fill="url(#chrome)">{fmt(M["total"])}</text>'
            f'<text x="33" y="182" font-size="17" fill="{P.ink2}">contributions</text>'
            f'<text x="33" y="204" class="mono" font-size="13" fill="{P.ink4}">since joining · {fdate(M["created"])}</text></g>'
            + sparkline(P, [v for _, v in M["monthly"]], 34, 222, 206, 38)
            + f'<text x="33" y="282" class="mono" font-size="12" fill="{P.ink4}">monthly · last 12 months</text>')
    for i, (ic, label, val) in enumerate(rows):
        y = 100 + i * 42
        body += (f'<g class="rise" style="animation-delay:{0.2 + i * 0.08:.2f}s">'
                 + icon(ic, 288, y - 14, P.ink3)
                 + f'<text x="318" y="{y}" font-size="17" fill="{P.ink2}">{label}</text>'
                 f'<text x="568" y="{y}" text-anchor="end" font-size="20" font-weight="600" class="num" fill="{P.ink}">{fmt(val)}</text>'
                 + (f'<line x1="288" y1="{y + 18}" x2="568" y2="{y + 18}" stroke="{P.hair}"/>' if i < len(rows) - 1 else "")
                 + "</g>")
    return card(P, W, H, body, defs=chrome_gradient(P), css=DRAW_CSS, title="GitHub activity",
                desc=f"{M['total']} contributions since {fdate(M['created'])}; {M['commits']} commits, "
                     f"{M['repos']} public repositories, {M['live_demos']} live demos, "
                     f"{M['languages_all']} languages, {M['stars']} stars from others.")


def streak(P, M, stamp: str) -> str:
    W, H = 600, 300
    cur, best = M["streak"], M["longest"]
    rng = lambda r: f"{fdate(r[0], False)} – {fdate(r[1], False)}" if r else "—"  # noqa: E731
    frac = (cur / best) if best else 0
    r, cx, cy = 60, 300, 160
    circ = 2 * math.pi * r

    def col(x, val, label, sub, delay):
        return (f'<g class="rise" style="animation-delay:{delay}s">'
                f'<text x="{x}" y="164" text-anchor="middle" font-size="44" font-weight="600" fill="{P.ink}">{val}</text>'
                f'<text x="{x}" y="195" text-anchor="middle" font-size="16.5" fill="{P.ink2}">{label}</text>'
                f'<text x="{x}" y="217" text-anchor="middle" class="mono" font-size="12.5" fill="{P.ink4}">{esc(sub)}</text></g>')

    body = (card_title(P, W, "Contribution streak", stamp)
            + col(102, fmt(best), "Longest streak", rng(M["longest_range"]), 0.15)
            + col(498, fmt(M["active_days"]), "Active days", f"of {M['days_since_join']} since joining", 0.25)
            + f'<line x1="194" y1="100" x2="194" y2="236" stroke="{P.hair}"/>'
              f'<line x1="406" y1="100" x2="406" y2="236" stroke="{P.hair}"/>'
            + f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{P.hair2}" stroke-width="6"/>'
              f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="url(#chromeRing)" stroke-width="6" stroke-linecap="round" '
              f'stroke-dasharray="{circ * frac:.1f} {circ:.1f}" transform="rotate(-90 {cx} {cy})" class="ring"/>'
            + f'<text x="{cx}" y="{cy + 15}" text-anchor="middle" font-size="48" font-weight="600" fill="url(#chrome)">{cur}</text>'
              f'<text x="{cx}" y="{cy + 37}" text-anchor="middle" class="mono" font-size="12" letter-spacing="1" fill="{P.ink4}">DAYS</text>'
            + f'<text x="{cx}" y="254" text-anchor="middle" font-size="16.5" fill="{P.ink2}">Current streak</text>'
              f'<text x="{cx}" y="276" text-anchor="middle" class="mono" font-size="12.5" fill="{P.ink4}">'
              f'{esc(rng(M["streak_range"]) if cur else "starts with the next commit")}</text>'
            + f'<text x="{cx}" y="{cy - r - 13}" text-anchor="middle" class="mono" font-size="11.5" letter-spacing="1" fill="{P.ink4}">'
              f'{int(round(frac * 100))}% OF LONGEST</text>')
    css = (f".ring{{animation:ring 1.6s .3s cubic-bezier(.3,.6,.2,1) both;}}"
           f"@keyframes ring{{from{{stroke-dasharray:0 {circ:.1f};}}}}")
    return card(P, W, H, body, defs=chrome_gradient(P) + chrome_gradient(P, "chromeRing", "0", "0", "1", "1"), css=css,
                title="Contribution streak",
                desc=f"Current streak {cur} days; longest {best} days ({rng(M['longest_range'])}); "
                     f"{M['active_days']} active days of {M['days_since_join']} since joining.")


def languages(P, M, stamp: str) -> str:
    W, H = 600, 330
    langs = M["languages"]
    x0, x1 = 180, 420
    top = max((l["pct"] for l in langs), default=1)
    body = card_title(P, W, "Top languages", stamp, "share of code across public repos · √bytes × √repos")
    for i, l in enumerate(langs):
        y = 110 + i * 35
        bw = (x1 - x0) * l["pct"] / top
        body += (f'<g class="rise" style="animation-delay:{0.1 + i * 0.07:.2f}s">'
                 f'<text x="32" y="{y + 10}" font-size="16.5" fill="{P.ink2}">{esc(l["name"])}</text>'
                 f'<rect x="{x0}" y="{y}" width="{x1 - x0}" height="11" fill="{P.hair}" fill-opacity="0.6"/>'
                 f'<path d="{bar_h(x0, y, bw, 11)}" fill="{P.mark}" class="grow" style="animation-delay:{0.25 + i * 0.07:.2f}s"/>'
                 f'<text x="{x0 + bw + 8:.1f}" y="{y + 10.5}" font-size="15" font-weight="600" class="num" fill="{P.ink}">{l["pct"]:.1f}%</text>'
                 f'<text x="568" y="{y + 10.5}" text-anchor="end" class="mono" font-size="12.5" fill="{P.ink4}">'
                 f'{l["repos"]} repo{"s" if l["repos"] != 1 else ""}</text></g>')
    return card(P, W, H, body, css=GROW_CSS, title="Top languages",
                desc="; ".join(f"{l['name']} {l['pct']:.1f}% ({l['repos']} repos)" for l in langs))


def evals(P) -> str:
    """Model vs. baseline, as rates on one 0–1 axis. Two series: legend + direct labels."""
    W, H = 600, 330
    rows = CFG["evals"]
    x0, x1 = 32, 456
    body = (card_title(P, W, "Evals vs. baselines", None, "measured in my repos · same test sets · higher is better")
            + f'<rect x="418" y="37" width="11" height="11" rx="2" fill="{P.mark2}"/>'
              f'<text x="435" y="47.5" font-size="14" fill="{P.ink3}">Baseline</text>'
              f'<rect x="515" y="37" width="11" height="11" rx="2" fill="{P.mark}"/>'
              f'<text x="532" y="47.5" font-size="14" fill="{P.ink3}">Mine</text>')
    for i, e in enumerate(rows):
        y = 112 + i * 54
        shown = e.get("display") or [f'{e["baseline"]:.3f}', f'{e["model"]:.3f}']
        body += (f'<g class="rise" style="animation-delay:{0.1 + i * 0.08:.2f}s">'
                 f'<text x="{x0}" y="{y}" font-size="15" fill="{P.ink2}">{esc(e["label"])}'
                 f'<tspan fill="{P.ink4}" font-size="12.5">  vs {esc(e["baseline_name"])}</tspan></text>')
        for j, (v, color) in enumerate(((e["baseline"], P.mark2), (e["model"], P.mark))):
            by = y + 10 + j * 13
            bw = max((x1 - x0) * v, 0)
            body += (f'<path d="{bar_h(x0, by, bw, 10)}" fill="{color}" class="grow" '
                     f'style="animation-delay:{0.25 + i * 0.08 + j * 0.06:.2f}s"/>'
                     f'<text x="{x0 + bw + 8:.1f}" y="{by + 9.5}" font-size="13.5" class="num" '
                     f'font-weight="{600 if j else 400}" fill="{P.ink if j else P.ink3}">{esc(shown[j])}</text>')
        body += "</g>"
    return card(P, W, H, body, css=GROW_CSS, title="Evals vs. baselines",
                desc="; ".join(f"{e['label']}: mine {(e.get('display') or [0, e['model']])[1]} vs "
                               f"{e['baseline_name']} {(e.get('display') or [e['baseline']])[0]}" for e in rows))


TROPHIES = [  # key, label, thresholds
    ("commits", "Commits", [1, 10, 50, 100, 250, 500, 1000, 2500, 5000]),
    ("longest", "Longest streak", [1, 3, 7, 14, 30, 60, 100, 200, 365]),
    ("repos", "Repositories", [1, 3, 5, 10, 20, 35, 50, 75, 100]),
    ("languages_all", "Languages", [1, 3, 5, 7, 10, 13, 16, 20, 25]),
    ("live_demos", "Live demos", [1, 2, 3, 5, 8, 12, 16, 20, 30]),
    ("total", "Contributions", [1, 50, 100, 250, 500, 1000, 2500, 5000, 10000]),
]


def trophy_tiers(M):
    out = []
    for key, label, th in TROPHIES:
        v = M[key]
        tier = sum(1 for t in th if v >= t)
        nxt = th[tier] if tier < len(th) else None
        prev = th[tier - 1] if tier else 0
        prog = 1.0 if nxt is None else (v - prev) / (nxt - prev)
        out.append({"label": label, "value": v, "tier": tier, "next": nxt, "progress": max(0.0, min(1.0, prog))})
    return out


def trophies(P, M) -> str:
    W, H = 1200, 252
    tiers = trophy_tiers(M)
    pad, gap = 32, 14
    pw = (W - 2 * pad - gap * (len(tiers) - 1)) / len(tiers)
    body = ""
    for i, t in enumerate(tiers):
        x = pad + i * (pw + gap)
        cx = x + pw / 2
        dim = ' opacity="0.45"' if t["tier"] == 0 else ""
        nxt = "max tier" if t["next"] is None else "next tier · " + fmt(t["next"])
        body += (f'<g class="rise" style="animation-delay:{0.08 * i:.2f}s"{dim}>'
                 f'<rect x="{x:.1f}" y="22" width="{pw:.1f}" height="208" rx="20" fill="{P.plate}" fill-opacity="{P.plate_op}" stroke="{P.hair}"/>'
                 f'<circle cx="{cx:.1f}" cy="78" r="32" fill="none" stroke="url(#chromeEdge)" stroke-width="2"/>'
                 f'<circle cx="{cx:.1f}" cy="78" r="26" fill="{P.node}" stroke="{P.hair2}"/>'
                 f'<circle cx="{cx:.1f}" cy="78" r="26" fill="url(#medal)"/>'
                 f'<text x="{cx:.1f}" y="86" text-anchor="middle" font-size="22" font-weight="600" letter-spacing="0.5" '
                 f'fill="url(#chrome)">{roman(t["tier"]) if t["tier"] else "—"}</text>'
                 f'<text x="{cx:.1f}" y="140" text-anchor="middle" font-size="16" fill="{P.ink2}">{t["label"]}</text>'
                 f'<text x="{cx:.1f}" y="172" text-anchor="middle" font-size="27" font-weight="600" fill="{P.ink}">{fmt(t["value"])}</text>'
                 f'<rect x="{cx - 60:.1f}" y="187" width="120" height="3.5" rx="1.75" fill="{P.hair2}"/>'
                 f'<rect x="{cx - 60:.1f}" y="187" width="{120 * t["progress"]:.1f}" height="3.5" rx="1.75" fill="{P.ink2}"/>'
                 f'<text x="{cx:.1f}" y="212" text-anchor="middle" class="mono" font-size="12.5" fill="{P.ink4}">{nxt}</text></g>')
    defs = (chrome_gradient(P) + chrome_gradient(P, "chromeEdge", "0", "0", "1", "1")
            + f'<radialGradient id="medal" cx="0.35" cy="0.3" r="0.8"><stop offset="0" stop-color="{P.glow}" '
              f'stop-opacity="{0.12 if P.mode == "dark" else 0.7}"/><stop offset="1" stop-color="{P.glow}" stop-opacity="0"/></radialGradient>')
    return card(P, W, H, body, defs=defs, title="Milestones",
                desc="; ".join(f"{t['label']}: {t['value']} (tier {t['tier']}"
                               + (f", next at {t['next']})" if t["next"] else ", max)") for t in tiers))


def heat_levels(values: list[int]) -> list[int]:
    nz = sorted(v for v in values if v)
    if not nz:
        return [0] * len(values)
    q = lambda p: nz[min(len(nz) - 1, int(p * len(nz)))]  # noqa: E731
    q1, q2, q3 = q(0.25), q(0.5), q(0.75)
    return [0 if v == 0 else 1 if v <= q1 else 2 if v <= q2 else 3 if v <= q3 else 4 for v in values]


def dashboard(P, M, stamp: str) -> str:
    W, H = 1200, 704
    pad = 40
    cal, today = M["cal"], M["today"]
    body = (f'<text x="{pad}" y="62" font-size="26" font-weight="600" fill="{P.ink}">Engineering dashboard</text>'
            f'<text x="{pad}" y="90" class="mono" font-size="13.5" fill="{P.ink4}">rolling 12 months · rebuilt from the GitHub API by a scheduled Action</text>'
            + live_badge(P, W - pad, 56, stamp, 13.5))

    kpis = [(fmt(M["year_total"]), "Contributions, 12 mo"), (fmt(M["active_year"]), "Active days, 12 mo"),
            (f'{M["streak"]} d', "Current streak"), (f'{M["longest"]} d', "Longest streak"),
            (fmt(M["busiest_count"]), f"Busiest day · {fdate(M['busiest'], False)}")]
    kw = (W - 2 * pad) / len(kpis)
    for i, (v, label) in enumerate(kpis):
        x = pad + i * kw + (0 if i == 0 else 24)
        if i:
            body += f'<line x1="{pad + i * kw:.1f}" y1="120" x2="{pad + i * kw:.1f}" y2="200" stroke="{P.hair}"/>'
        body += (f'<g class="rise" style="animation-delay:{0.08 * i:.2f}s">'
                 f'<text x="{x:.1f}" y="162" font-size="40" font-weight="600" letter-spacing="-1" fill="url(#chrome)">{v}</text>'
                 f'<text x="{x:.1f}" y="192" font-size="16" fill="{P.ink3}">{esc(label)}</text></g>')
    body += f'<line x1="{pad}" y1="222" x2="{W - pad}" y2="222" stroke="{P.hair}"/>'

    # Contribution calendar — one sequential ramp (graphite → silver), Sunday-first rows.
    first = M["window"][0]
    start = first - dt.timedelta(days=(first.weekday() + 1) % 7)
    ncols = (today - start).days // 7 + 1
    cell, gap = 12, 3
    gx, gy = pad + 42, 298
    below = gy + 7 * (cell + gap)
    days = [start + dt.timedelta(days=i) for i in range(ncols * 7)]
    days = [d for d in days if d <= today]
    levels = heat_levels([cal.get(d, 0) for d in days])
    body += f'<text x="{pad}" y="258" font-size="17" font-weight="600" fill="{P.ink2}">Contribution calendar</text>'
    for row, lab in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        body += (f'<text x="{pad}" y="{gy + row * (cell + gap) + 10}" class="mono" font-size="12" '
                 f'fill="{P.ink4}">{lab}</text>')
    last_month, cells, joined = None, [], None
    for d, lv in zip(days, levels):
        col, row = (d - start).days // 7, (d.weekday() + 1) % 7
        x, y = gx + col * (cell + gap), gy + row * (cell + gap)
        if row == 0 and d.month != last_month and d.day <= 7:
            body += f'<text x="{x}" y="{gy - 10}" class="mono" font-size="12" fill="{P.ink4}">{d:%b}</text>'
            last_month = d.month
        pre = ' opacity="0.3"' if d < M["created"] else ""
        cells.append(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="2.5" fill="{P.heat[lv]}"{pre}/>')
        if d == M["created"]:
            joined = x + cell / 2
    body += f'<g class="fade" style="animation-delay:.2s">{"".join(cells)}</g>'
    if joined is not None:
        body += (f'<line x1="{joined}" y1="{below + 2}" x2="{joined}" y2="{below + 9}" stroke="{P.ink4}"/>'
                 f'<text x="{joined}" y="{below + 24}" text-anchor="middle" class="mono" font-size="12" '
                 f'fill="{P.ink4}">joined GitHub · {fdate(M["created"], False)}</text>')
    grid_right = gx + ncols * (cell + gap) - gap
    lg_x = grid_right - 5 * (cell + gap) - 34
    body += f'<text x="{lg_x - 8}" y="{below + 23}" text-anchor="end" class="mono" font-size="12" fill="{P.ink4}">Less</text>'
    for i in range(5):
        body += f'<rect x="{lg_x + i * (cell + gap)}" y="{below + 12}" width="{cell}" height="{cell}" rx="2.5" fill="{P.heat[i]}"/>'
    body += f'<text x="{lg_x + 5 * (cell + gap) + 4}" y="{below + 23}" class="mono" font-size="12" fill="{P.ink4}">More</text>'

    # Weekday rhythm — one series; the busiest weekday is emphasised, the rest recede.
    wx0, wx1 = max(grid_right + 46, 925), W - pad
    base, maxh = below - gap, 92
    wk = M["weekday"]
    wmax = max(wk) or 1
    slot = (wx1 - wx0) / 7
    body += (f'<text x="{wx0}" y="258" font-size="17" font-weight="600" fill="{P.ink2}">Weekday rhythm</text>'
             f'<line x1="{wx0}" y1="{base}" x2="{wx1}" y2="{base}" stroke="{P.hair2}"/>')
    for i, (v, lab) in enumerate(zip(wk, ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"])):
        bx = wx0 + i * slot + (slot - 18) / 2
        hgt = maxh * v / wmax
        peak = v == wmax and v > 0
        body += (f'<path d="{bar_v(bx, base, 18, hgt)}" fill="{P.mark if peak else P.mark2}" class="up" '
                 f'style="animation-delay:{0.3 + i * 0.05:.2f}s"/>'
                 f'<text x="{bx + 9:.1f}" y="{below + 23}" text-anchor="middle" class="mono" font-size="12" fill="{P.ink4}">{lab}</text>')
        if peak:
            body += (f'<text x="{bx + 9:.1f}" y="{base - hgt - 9:.1f}" text-anchor="middle" font-size="15" '
                     f'font-weight="600" fill="{P.ink}">{v}</text>')
    body += f'<line x1="{pad}" y1="452" x2="{W - pad}" y2="452" stroke="{P.hair}"/>'

    # Monthly trend — single line, 10% wash, labelled endpoint (and peak, if different).
    mx0, mx1, my0, my1 = pad + 40, 716, 516, 626
    vals = [v for _, v in M["monthly"]]
    vmax = max(vals) or 1
    nice = 10 ** max(0, len(str(vmax)) - 1)
    ytop = math.ceil(vmax / nice) * nice
    step = (mx1 - mx0) / (len(vals) - 1)
    pts = [(mx0 + i * step, my1 - (v / ytop) * (my1 - my0)) for i, v in enumerate(vals)]
    line = "M" + " L".join(f"{a:.1f},{b:.1f}" for a, b in pts)
    body += (f'<text x="{pad}" y="488" font-size="17" font-weight="600" fill="{P.ink2}">Monthly contributions</text>'
             f'<line x1="{mx0}" y1="{my0}" x2="{mx1}" y2="{my0}" stroke="{P.hair}"/>'
             f'<line x1="{mx0}" y1="{my1}" x2="{mx1}" y2="{my1}" stroke="{P.hair2}"/>'
             f'<text x="{mx0 - 10}" y="{my0 + 4}" text-anchor="end" class="mono num" font-size="12" fill="{P.ink4}">{fmt(ytop)}</text>'
             f'<text x="{mx0 - 10}" y="{my1 + 4}" text-anchor="end" class="mono num" font-size="12" fill="{P.ink4}">0</text>'
             f'<path d="{line} L{pts[-1][0]:.1f},{my1} L{pts[0][0]:.1f},{my1} Z" fill="{P.mark}" fill-opacity="0.10"/>'
             f'<path d="{line}" fill="none" stroke="{P.mark}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" class="draw" pathLength="1"/>')
    for i, (lab, _) in enumerate(M["monthly"]):
        body += (f'<text x="{mx0 + i * step:.1f}" y="{my1 + 24}" text-anchor="middle" class="mono" font-size="12" '
                 f'fill="{P.ink4}">{lab}</text>')
    ex, ey = pts[-1]
    peak_i = max(range(len(vals)), key=lambda i: vals[i])
    body += (f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="4.5" fill="{P.ink}" stroke="{P.surface}" stroke-width="2"/>'
             f'<text x="{ex:.1f}" y="{ey - 13:.1f}" text-anchor="middle" font-size="15" font-weight="600" fill="{P.ink}">{vals[-1]}</text>')
    if peak_i != len(vals) - 1 and vals[peak_i]:
        px, py = pts[peak_i]
        body += (f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4" fill="{P.ink2}" stroke="{P.surface}" stroke-width="2"/>'
                 f'<text x="{px:.1f}" y="{py - 13:.1f}" text-anchor="middle" font-size="15" font-weight="600" fill="{P.ink2}">{vals[peak_i]}</text>')

    # Recently shipped — what moved last, two lines per repo so names never collide with dates.
    rx0, rx1 = 784, W - pad
    body += f'<text x="{rx0}" y="488" font-size="17" font-weight="600" fill="{P.ink2}">Recently shipped</text>'
    for i, r in enumerate(M["recent"][:4]):
        y = 522 + i * 44
        meta = f'{r["language"] or "—"} · pushed {fdate(parse_ts(r["pushed_at"]).date(), False)}'
        if r.get("live"):
            meta += " · live demo"
        body += (f'<g class="rise" style="animation-delay:{0.5 + i * 0.08:.2f}s">'
                 f'<text x="{rx0}" y="{y}" font-size="16" fill="{P.ink}">{esc(display_name(r["name"]))}</text>'
                 f'<text x="{rx0}" y="{y + 19}" class="mono" font-size="12.5" fill="{P.ink4}">{esc(meta)}</text>'
                 + (f'<line x1="{rx0}" y1="{y + 30}" x2="{rx1}" y2="{y + 30}" stroke="{P.hair}"/>' if i < 3 else "")
                 + "</g>")

    css = (DRAW_CSS + ".up{transform-box:fill-box;transform-origin:center bottom;animation:up 1s cubic-bezier(.2,.7,.2,1) both;}"
           "@keyframes up{from{transform:scaleY(0);}}")
    return card(P, W, H, body, defs=chrome_gradient(P), css=css, sweep_period=12,
                title="Engineering dashboard",
                desc=f"Last 12 months: {M['year_total']} contributions on {M['active_year']} active days; "
                     f"current streak {M['streak']} days, longest {M['longest']} days; busiest day "
                     f"{fdate(M['busiest'])} with {M['busiest_count']}. Monthly: "
                     + ", ".join(f"{m} {v}" for m, v in M["monthly"]) + ".")


def footer(P) -> str:
    W, H = 1200, 168
    line = f"Designed by {CFG['name']} in {CFG['city'].split(',')[0]}."
    body = (f'<text x="{W / 2}" y="84" text-anchor="middle" font-size="36" font-weight="600" letter-spacing="-0.6" fill="url(#chrome)">{esc(line)}</text>'
            f'<text x="{W / 2}" y="84" text-anchor="middle" font-size="36" font-weight="600" letter-spacing="-0.6" fill="url(#textShine)">{esc(line)}</text>'
            f'<text x="{W / 2}" y="120" text-anchor="middle" class="mono" font-size="14" letter-spacing="0.4" fill="{P.ink4}">'
            f'hand-built SVG · no third-party card services · regenerated by GitHub Actions</text>')
    return card(P, W, H, body, defs=chrome_gradient(P) + shine_gradient(P, 240, (80, 1020), 8), rx=30,
                sweep=False, title=line, desc="Profile artwork is hand-built SVG, regenerated by GitHub Actions.")


def divider(P) -> str:
    W, H = 1200, 24
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="divider">
<defs>
<linearGradient id="l" x1="0" x2="1"><stop offset="0" stop-color="{P.ink4}" stop-opacity="0"/><stop offset="0.5" stop-color="{P.ink4}"/><stop offset="1" stop-color="{P.ink4}" stop-opacity="0"/></linearGradient>
<linearGradient id="s" x1="0" x2="1"><stop offset="0" stop-color="{P.ink}" stop-opacity="0"/><stop offset="0.5" stop-color="{P.ink}"/><stop offset="1" stop-color="{P.ink}" stop-opacity="0"/></linearGradient>
</defs>
<style>.s{{animation:s 6s cubic-bezier(.45,0,.2,1) infinite;}}@keyframes s{{0%{{transform:translateX(-200px);}}60%,100%{{transform:translateX({W}px);}}}}@media (prefers-reduced-motion:reduce){{.s{{animation:none;opacity:0;}}}}</style>
<rect x="0" y="11.5" width="{W}" height="1" fill="url(#l)"/>
<rect class="s" x="0" y="11" width="200" height="2" rx="1" fill="url(#s)"/>
<circle cx="{W / 2}" cy="12" r="2.5" fill="{P.ink3}"/>
</svg>
"""


# --------------------------------------------------------------------------
# README sections
# --------------------------------------------------------------------------

def activity_md(data: dict, limit: int) -> str:
    exclude = set(CFG.get("exclude_repos", []))
    login = data["login"]
    own = {r["name"] for r in data["repos"]}
    groups: dict[tuple, dict] = {}
    order = []
    for e in data["events"]:
        full = e["repo"]["name"]
        owner, _, repo = full.partition("/")
        if repo in exclude:
            continue
        day = parse_ts(e["created_at"]).date()
        p, typ = e.get("payload", {}), e["type"]
        link = f'<a href="https://github.com/{full}">{esc(display_name(repo) if owner == login else full)}</a>'
        if typ == "PushEvent":
            key, text = (day, "push", full), None
        elif typ == "CreateEvent" and p.get("ref_type") == "repository":
            key, text = (day, "create", full), f"Created repository {link}"
        elif typ == "CreateEvent" and p.get("ref_type") == "tag":
            key, text = (day, "tag", full, p.get("ref")), f"Tagged <code>{esc(p.get('ref'))}</code> in {link}"
        elif typ == "ReleaseEvent":
            rel = p.get("release", {})
            key, text = (day, "rel", full, rel.get("tag_name")), f"Released <code>{esc(rel.get('tag_name'))}</code> of {link}"
        elif typ == "PullRequestEvent" and p.get("action") in ("opened", "closed"):
            pr = p.get("pull_request", {})
            verb = "Merged" if pr.get("merged") else "Opened" if p["action"] == "opened" else "Closed"
            key = (day, "pr", full, pr.get("number"), verb)
            text = f"{verb} PR <a href=\"{esc(pr.get('html_url', ''))}\">#{pr.get('number')}</a> in {link}"
        elif typ == "IssuesEvent" and p.get("action") in ("opened", "closed"):
            iss = p.get("issue", {})
            key = (day, "issue", full, iss.get("number"), p["action"])
            text = f"{p['action'].capitalize()} issue <a href=\"{esc(iss.get('html_url', ''))}\">#{iss.get('number')}</a> in {link}"
        elif typ == "ForkEvent":
            key, text = (day, "fork", full), f"Forked {link}"
        elif typ == "WatchEvent" and repo not in own:
            key, text = (day, "star", full), f"Starred {link}"
        elif typ == "PublicEvent":
            key, text = (day, "public", full), f"Open-sourced {link}"
        else:
            continue
        if key not in groups:
            groups[key] = {"day": day, "text": text, "n": 0, "link": link}
            order.append(key)
        groups[key]["n"] += 1
    lines = []
    for key in order[:limit]:
        g = groups[key]
        if key[1] == "push":
            g["text"] = f"Pushed to {g['link']}" + (f" <sub>· {g['n']} pushes</sub>" if g["n"] > 1 else "")
        lines.append(f"- <code>{g['day']:%d %b}</code>&nbsp; {g['text']}")
    return "\n".join(lines) if lines else "- <sub>No public activity in the last 90 days.</sub>"


def metrics_table_md(M) -> str:
    rows = [
        ("Contributions since joining", fmt(M["total"])), ("Commits", fmt(M["commits"])),
        ("Pull requests", fmt(M["prs"])), ("Issues", fmt(M["issues"])),
        ("Public repositories", fmt(M["repos"])), ("Stars from others", fmt(M["stars"])),
        ("Live demos (homepage responds)", fmt(M["live_demos"])),
        ("Current streak", f'{M["streak"]} days'), ("Longest streak", f'{M["longest"]} days'),
        ("Active days since joining", f'{M["active_days"]} of {M["days_since_join"]}'),
        ("Busiest day (12 mo)", f'{fdate(M["busiest"])} · {M["busiest_count"]}'),
    ]
    out = ["| Metric | Value |", "|---|---:|"] + [f"| {a} | {b} |" for a, b in rows]
    out += ["", "| Language | Share | Repos |", "|---|---:|---:|"]
    out += [f'| {l["name"]} | {l["pct"]:.1f}% | {l["repos"]} |' for l in M["languages"]]
    out += ["", "| Month | " + " | ".join(m for m, _ in M["monthly"]) + " |",
            "|---|" + "---:|" * len(M["monthly"]),
            "| Contributions | " + " | ".join(str(v) for _, v in M["monthly"]) + " |"]
    return "\n".join(out)


def replace_section(text: str, name: str, content: str) -> str:
    pat = re.compile(rf"(<!--START_SECTION:{name}-->)(.*?)(<!--END_SECTION:{name}-->)", re.S)
    if not pat.search(text):
        print(f"  ! README has no {name} markers; skipped", file=sys.stderr)
        return text
    return pat.sub(lambda m: f"{m.group(1)}\n{content}\n{m.group(3)}", text)


# --------------------------------------------------------------------------

def main() -> None:
    offline = "--offline" in sys.argv
    token = os.environ.get("GITHUB_TOKEN") or None
    login = os.environ.get("PROFILE_USER") or CFG["user"]
    ASSETS.mkdir(exist_ok=True)

    if offline:
        data = json.loads(DATA_CACHE.read_text(encoding="utf-8"))
    else:
        print(f"fetching {login} via {'GraphQL' if token else 'REST + public calendar'} ...")
        data = fetch(login, token)
        slim = {k: v for k, v in data.items() if k != "events"}
        slim["events"] = [{"type": e["type"], "repo": e["repo"], "created_at": e["created_at"],
                           "payload": {k: e["payload"].get(k) for k in
                                       ("ref_type", "ref", "action", "release", "pull_request", "issue")
                                       if k in e.get("payload", {})}}
                          for e in data["events"]]
        DATA_CACHE.write_text(json.dumps(slim, indent=1, sort_keys=True), encoding="utf-8")
        data = slim

    M = compute(data)
    stamp = "LIVE · " + fdate(M["today"]).upper()
    for P in (DARK, LIGHT):
        m = P.mode
        files = {
            f"hero-{m}.svg": hero(P, M),
            f"core-{m}.svg": core(P),
            f"typing-{m}.svg": typing(P, CFG["typing"]),
            f"specs-{m}.svg": specs(P, M),
            f"stats-{m}.svg": stats(P, M, stamp),
            f"streak-{m}.svg": streak(P, M, stamp),
            f"languages-{m}.svg": languages(P, M, stamp),
            f"evals-{m}.svg": evals(P),
            f"dashboard-{m}.svg": dashboard(P, M, stamp),
            f"footer-{m}.svg": footer(P),
            f"divider-{m}.svg": divider(P),
        }
        for fname, svg in files.items():
            (ASSETS / fname).write_text(svg, encoding="utf-8", newline="\n")
    print(f"  rendered {len(files) * 2} SVGs into assets/")

    if README.exists():
        text = README.read_text(encoding="utf-8")
        text = replace_section(text, "activity", activity_md(data, CFG.get("activity_limit", 8)))
        text = replace_section(text, "metrics-table", metrics_table_md(M))
        README.write_text(text, encoding="utf-8", newline="\n")
        print("  updated README activity + metrics table")

    print(f"  total={M['total']} commits={M['commits']} streak={M['streak']} longest={M['longest']} "
          f"repos={M['repos']} live={M['live_demos']} langs={[l['name'] for l in M['languages']]}")


if __name__ == "__main__":
    main()
