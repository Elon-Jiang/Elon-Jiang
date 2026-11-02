#!/usr/bin/env python3
"""Generate GitHub-safe SVG profile cards using Python's standard library.

Live: python scripts/generate_stats.py --public
Offline: python scripts/generate_stats.py --offline data/public-contributions.json
GitHub Actions: GITHUB_TOKEN is supplied by the workflow, never saved to disk.
"""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import escape
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.github.com"
THEMES = {
    "light": {"bg": "#ffffff", "fg": "#202124", "muted": "#83858b", "line": "#eeedf2", "accent": "#9565c9", "soft": "#f5f0fb", "sub": "#55565d"},
    "dark": {"bg": "#0d1117", "fg": "#e6edf3", "muted": "#89929e", "line": "#252c36", "accent": "#ba93ed", "soft": "#231b32", "sub": "#b1bac4"},
}
FONT = "'Noto Sans CJK SC','PingFang SC','Microsoft YaHei',sans-serif"
MONO = "'SFMono-Regular',Consolas,'Liberation Mono',monospace"


def api_get(path, token=None):
    headers = {"User-Agent": "Elon-Jiang-profile", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with urllib.request.urlopen(urllib.request.Request(API + path, headers=headers), timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # Fail before writing outputs: an API error must never reset a card to 0.
        raise RuntimeError(f"GitHub API returned HTTP {error.code} for {path.split('?')[0]}; existing cards were preserved.") from None


def search_prs(username, token):
    query = f"is:pr is:public author:{username} -user:{username}"
    results = []
    total = None
    for page in range(1, 11):
        params = urllib.parse.urlencode({"q": query, "per_page": 100, "page": page, "sort": "created", "order": "desc"})
        result = api_get("/search/issues?" + params, token)
        if result.get("incomplete_results"):
            raise RuntimeError("GitHub returned incomplete search results; existing cards were preserved.")
        if total is None:
            total = result["total_count"]
            if total > 1000:
                raise RuntimeError("More than 1,000 PRs: partition the search before refreshing. Existing cards were preserved.")
        results.extend(result["items"])
        if len(results) >= total:
            break
    unique = {item["id"]: item for item in results}
    if len(unique) != total:
        raise RuntimeError("Search changed during pagination. Retry; existing cards were preserved.")
    prs = []
    for item in unique.values():
        repo = item["repository_url"].split("/repos/", 1)[1]
        if repo.split("/", 1)[0].lower() == username.lower():
            continue
        pull = item["pull_request"]
        # merged_at is part of the REST search response for PRs. Never infer
        # a merge from closed_at: rejected/withdrawn PRs are also closed.
        if "merged_at" not in pull:
            pull = api_get("/repos/" + repo + "/pulls/" + str(item["number"]), token)
        prs.append({"id": item["id"], "number": item["number"], "repository": repo,
                    "title": item["title"], "url": item["html_url"], "state": item["state"],
                    "draft": bool(item.get("draft", pull.get("draft", False))),
                    "created_at": item["created_at"], "merged_at": pull.get("merged_at")})
    return sorted(prs, key=lambda p: p["created_at"], reverse=True)


def fetch_data(config, public=False):
    # --public deliberately avoids consulting local credentials.
    token = None if public else os.environ.get("GITHUB_TOKEN")
    username = config["username"]
    prs = search_prs(username, token)
    repositories = sorted({p["repository"] for p in prs})

    def repo_metadata(full_name):
        repo = api_get("/repos/" + full_name, token)
        return {"full_name": full_name, "url": repo["html_url"], "stars": repo["stargazers_count"]}

    # A small pool keeps requests modest and the daily refresh quick.
    with ThreadPoolExecutor(max_workers=3) as pool:
        repos = list(pool.map(repo_metadata, repositories))
    profile = api_get("/users/" + username, token)
    return {"schema_version": 1, "username": username,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "GitHub REST API: public PRs authored by this user in repositories owned by others",
            "profile": {key: profile.get(key) for key in ("login", "name", "avatar_url", "followers", "following", "bio")},
            "pull_requests": prs, "repositories": repos}


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def summarize(data):
    prs = data["pull_requests"]
    merged = [p for p in prs if p["merged_at"]]
    opened = [p for p in prs if p["state"] == "open" and not p["merged_at"]]
    closed = [p for p in prs if p["state"] == "closed" and not p["merged_at"]]
    repo_stats = []
    for repo in data["repositories"]:
        contributions = [p for p in prs if p["repository"] == repo["full_name"]]
        repo_stats.append({**repo, "merged": sum(bool(p["merged_at"]) for p in contributions),
                           "open": sum(p["state"] == "open" and not p["merged_at"] for p in contributions),
                           "total": len(contributions)})
    repo_stats.sort(key=lambda r: (-r["merged"], -r["open"], -r["total"], -r["stars"], r["full_name"].lower()))
    return {"total": len(prs), "merged": len(merged), "open": len(opened), "closed": len(closed),
            "repos": repo_stats}


def text(x, y, value, size=16, color="fg", weight=400, mono=False, anchor=None, spacing=None):
    attributes = f'x="{x}" y="{y}" font-size="{size}" fill="{{{color}}}" font-weight="{weight}"'
    attributes += f' font-family="{escape(MONO if mono else FONT, quote=True)}"'
    if anchor:
        attributes += f' text-anchor="{anchor}"'
    if spacing is not None:
        attributes += f' letter-spacing="{spacing}"'
    return "<text " + attributes + ">" + escape(str(value)) + "</text>"


def line(y, x1=48, x2=952):
    return f'<path d="M{x1} {y}H{x2}" stroke="{{line}}" fill="none"/>'


def svg_document(parts, theme, height, title, desc):
    content = "\n".join(parts)
    for key, value in THEMES[theme].items():
        content = content.replace("{" + key + "}", value)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="{height}" viewBox="0 0 1000 {height}" role="img" aria-labelledby="title desc">'
            f'<title id="title">{escape(title)}</title><desc id="desc">{escape(desc)}</desc>'
            f'<rect width="1000" height="{height}" fill="{THEMES[theme]["bg"]}"/>' + content + '</svg>\n')


def hero_svg(config, theme):
    parts = [text(48, 39, "▪ " + config["role"], 11, "accent", 600, True, spacing=1.8),
             text(48, 119, config["name"], 47, weight=700),
             text(208, 117, "· " + config["english_name"], 42, "sub", 400),
             text(48, 169, config["intro"], 16, "sub"),
             text(48, 199, config["subtitle"], 13, "muted")]
    return svg_document(parts, theme, 246, config["name"] + " · " + config["english_name"], config["intro"] + " " + config["subtitle"])


def stars_label(value):
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)


def stats_svg(config, data, stats, theme):
    now = parse_time(data["updated_at"]).astimezone(ZoneInfo(config["timezone"]))
    rows = stats["repos"][:config["max_repositories"]]
    footer_y = 385 + 65 * len(rows)
    parts = [text(48, 31, "▪ OPEN SOURCE CONTRIBUTIONS", 11, "accent", 600, True, spacing=2),
             text(952, 31, "github.com/" + config["username"], 11, "muted", mono=True, anchor="end"),
             line(53), text(48, 145, stats["merged"], 73, weight=700),
             text(132 + max(0, len(str(stats["merged"])) - 1) * 43, 141, "个 PR 已合并", 21, weight=650),
             text(770, 139, stats["open"], 42, "accent", 600, anchor="end"),
             text(788, 139, "个待合并", 16, "sub"),
             text(48, 179, f'{stats["total"]} 个公开 PR · {len(stats["repos"])} 个贡献仓库 · 仅统计提给他人仓库的贡献', 13, "muted")]

    # Display real activity even before the first merge; label created vs merged explicitly.
    use_merged = stats["merged"] > 0
    field = "merged_at" if use_merged else "created_at"
    chart_title = "近 30 天 PR 合并时间" if use_merged else "近 30 天 PR 提交时间"
    parts.append(text(48, 219, chart_title, 11, "muted"))
    start_date = now.date() - timedelta(days=29)
    counts = Counter()
    for pr in data["pull_requests"]:
        if pr.get(field):
            date = parse_time(pr[field]).astimezone(ZoneInfo(config["timezone"])).date()
            if start_date <= date <= now.date():
                counts[(date - start_date).days] += 1
    parts.append(line(285))
    for day, count in sorted(counts.items()):
        x = 54 + day / 29 * 892
        # Keep very busy days inside the graph; the title carries the full count.
        visible = min(count, 5)
        for index in range(visible):
            y = 274 - index * 10
            parts.append(f'<circle cx="{x:.2f}" cy="{y}" r="3.6" fill="{{accent}}"><title>{start_date + timedelta(days=day)}: {count} 个 PR</title></circle>')
        if count > 5:
            parts.append(text(x, 217, "+" + str(count - 5), 10, "accent", mono=True, anchor="middle"))
    if not counts:
        parts.append(text(500, 260, "这 30 天暂无记录", 12, "muted", anchor="middle"))
    parts.extend([text(48, 309, start_date.strftime("%m-%d"), 10, "muted", mono=True),
                  text(952, 309, now.strftime("%m-%d"), 10, "muted", mono=True, anchor="end"), line(332)])
    if rows:
        parts.extend([text(48, 364, "贡献仓库", 11, "muted"), text(779, 364, "PR 状态", 11, "muted", anchor="end"),
                      text(952, 364, "仓库 STARS", 11, "muted", anchor="end")])
    else:
        parts.append(text(48, 377, "公开 PR 提交后，这里会自动显示贡献仓库。", 14, "muted"))
    colors = ["#8d66bd", "#4386b7", "#d17870", "#5b9c83", "#a58a47", "#7281be"]
    for index, repo in enumerate(rows):
        y = 404 + index * 65
        full_name = repo["full_name"]
        short = "".join(re.findall(r"[A-Z]|^[a-z]", full_name.split("/")[1])[:2]).upper()
        if len(short) < 2:
            short = full_name.split("/")[1][:2].upper()
        parts.extend([f'<rect x="48" y="{y-19}" width="28" height="28" rx="7" fill="{colors[index % len(colors)]}" fill-opacity="0.12"/>',
                      f'<text x="62" y="{y}" text-anchor="middle" font-family="{escape(MONO, quote=True)}" font-size="10" font-weight="700" fill="{colors[index % len(colors)]}">{escape(short)}</text>',
                      text(96, y + 1, full_name, 16, weight=600, mono=True),
                      text(779, y + 1, f'{repo["merged"]} 合并 · {repo["open"]} 待合并', 12, "sub", anchor="end"),
                      text(952, y + 1, "☆ " + stars_label(repo["stars"]), 13, "sub", 500, True, "end"),
                      line(y + 29)])
    remaining = len(stats["repos"]) - len(rows)
    if remaining:
        parts.append(text(96, footer_y + 6, f"… 另有 {remaining} 个贡献仓库，完整 PR 链接见下方", 12, "muted"))
    parts.extend([line(footer_y + 40), text(48, footer_y + 68, "数据来源 GitHub REST API · 每日自动刷新", 11, "muted"),
                  text(952, footer_y + 68, "更新于 " + now.strftime("%Y-%m-%d %H:%M") + " (UTC+8)", 10, "muted", mono=True, anchor="end")])
    desc = f'{stats["merged"]} 个 PR 已合并，{stats["open"]} 个待合并，{stats["closed"]} 个关闭未合并。仓库星标是所贡献项目的星标。'
    return svg_document(parts, theme, footer_y + 94, "公开开源贡献统计", desc)


def picture(kind, alt):
    return (f'<picture>\n  <source media="(prefers-color-scheme: dark)" srcset="./assets/{kind}-dark.svg">\n'
            f'  <source media="(prefers-color-scheme: light)" srcset="./assets/{kind}-light.svg">\n'
            f'  <img alt="{escape(alt, quote=True)}" src="./assets/{kind}-light.svg" width="100%">\n</picture>')


def markdown_escape(value):
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("[", "\\[").replace("]", "\\]")


def readme(config, data, stats):
    query = urllib.parse.urlencode({"q": "is:pr author:" + config["username"] + " is:public -user:" + config["username"]})
    body = ["<!-- Generated by scripts/generate_stats.py. Edit profile.json to customize. -->", "",
            picture("hero", config["name"] + " · " + config["english_name"] + " — " + config["intro"]), "",
            picture("contributions", f'{stats["merged"]} 个 PR 已合并 · {stats["open"]} 个待合并'), "",
            f'[{stats["total"]} 个公开 PR](https://github.com/pulls?{query}) · **{stats["merged"]} 已合并 / {stats["open"]} 待合并 / {stats["closed"]} 关闭未合并**', "",
            "仅统计公开仓库中由我创建、提给他人仓库的 PR；仓库 Star 数属于所贡献的项目。", "",
            "<details>", f'<summary>查看全部 {stats["total"]} 个 PR 与贡献仓库</summary>', "",
            "| 仓库 | PR | 状态 |", "| :--- | :--- | :--- |"]
    for pr in data["pull_requests"]:
        state = "已合并" if pr["merged_at"] else ("草稿" if pr.get("draft") else "待合并") if pr["state"] == "open" else "关闭未合并"
        title = markdown_escape(pr["title"])
        repo = pr["repository"]
        body.append(f'| [{repo}](https://github.com/{repo}) | [#{pr["number"]} {title}]({pr["url"]}) | {state} |')
    body.extend(["", "</details>", "", "---", "", f'<sub>统计每日自动更新 · [更新记录](https://github.com/{config["username"]}/{config["username"]}/actions/workflows/update-profile.yml)</sub>', ""])
    return "\n".join(body)


def preview(config, data, stats):
    profile = data["profile"]
    full_list = "".join(f'<a class="pr" href="{escape(p["url"], quote=True)}"><span>{escape(p["repository"])} <b>#{p["number"]}</b></span><small>{"已合并" if p["merged_at"] else "待合并" if p["state"] == "open" else "关闭未合并"}</small></a>' for p in data["pull_requests"])
    avatar = "assets/avatar.png"
    return '''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Elon Jiang · GitHub 主页预览</title>
<style>
:root{color-scheme:light;--bg:#fff;--fg:#202124;--muted:#7c7f85;--line:#e9e9ed;--soft:#f7f7f9;--accent:#9565c9}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px 'PingFang SC','Microsoft YaHei',sans-serif}body.dark{color-scheme:dark;--bg:#0d1117;--fg:#e6edf3;--muted:#89929e;--line:#252c36;--soft:#161b22;--accent:#ba93ed}a{color:inherit;text-decoration:none}a:hover{color:var(--accent)}.toolbar{height:65px;display:flex;align-items:center;justify-content:space-between;padding:0 34px;border-bottom:1px solid var(--line);font-size:13px;color:var(--muted)}.brand{display:flex;align-items:center;gap:12px;color:var(--fg);font-weight:600}.mark{width:24px;height:24px;background:var(--fg);color:var(--bg);border-radius:50%;display:grid;place-items:center;font:13px Georgia}.controls{display:flex;align-items:center;gap:14px}button{border:1px solid var(--line);background:var(--bg);color:var(--fg);padding:7px 14px;border-radius:7px;cursor:pointer}button[aria-pressed=true]{border-color:var(--accent);color:var(--accent)}.layout{max-width:1330px;margin:44px auto 80px;display:grid;grid-template-columns:290px minmax(0,1fr);gap:38px;padding:0 28px}.sidebar{padding:0 10px}.avatar{display:block;width:100%;aspect-ratio:1;border-radius:50%;object-fit:cover;border:1px solid var(--line)}h1{font-size:25px;margin:22px 0 2px;letter-spacing:-.5px}.handle{font-size:20px;font-weight:300;color:var(--muted);margin-bottom:25px}.followers{font-size:13px;color:var(--muted)}.profile-link{display:block;margin-top:20px;font-size:13px;overflow-wrap:anywhere}.preview-label{margin-top:26px;padding-top:18px;border-top:1px solid var(--line);font-size:12px;line-height:1.8;color:var(--muted)}.readme{min-width:0;border:1px solid var(--line);border-radius:9px;overflow:hidden}.file-label{padding:14px 25px;border-bottom:1px solid var(--line);font:12px 'SFMono-Regular',Consolas,monospace;color:var(--muted)}.cards{padding:16px 18px 26px}.card{display:block;width:100%;height:auto}.footnote{font-size:12px;color:var(--muted);line-height:1.8;margin:16px 40px}.footnote b{color:var(--fg);font-weight:500}details{margin:24px 40px 10px;font-size:13px}summary{cursor:pointer;color:var(--accent);margin-bottom:14px}.pr{display:flex;justify-content:space-between;gap:16px;padding:12px 0;border-bottom:1px solid var(--line);font-size:12px}.pr small{white-space:nowrap;color:var(--muted)}.pr b{font-weight:400;color:var(--muted)}@media(max-width:760px){.toolbar{padding:0 16px}.controls>a{display:none}.layout{display:block;padding:0 12px;margin:24px auto}.sidebar{display:grid;grid-template-columns:64px 1fr;column-gap:16px;padding:0 10px;margin-bottom:22px}.avatar{width:64px;grid-row:1/3}h1{margin:3px 0 0;font-size:21px}.handle{font-size:16px;margin:0}.followers,.profile-link,.preview-label{display:none}.cards{padding:8px 0 20px}.file-label{padding:12px 16px}.footnote{margin:12px 18px}details{margin:20px 18px}.toolbar .brand{font-size:12px}}
</style>
<header class="toolbar"><a class="brand" href="https://github.com/Elon-Jiang"><span class="mark">e</span>Elon-Jiang / 主页预览</a><div class="controls"><button id="light" aria-pressed="true">浅色</button><button id="dark" aria-pressed="false">深色</button><a href="https://github.com/Elon-Jiang">查看公开主页 ↗</a></div></header>
<main class="layout"><aside class="sidebar"><img class="avatar" src="''' + avatar + '''" alt="GitHub 头像"><h1>''' + escape(profile.get("name") or config["username"]) + '''</h1><div class="handle">''' + escape(config["username"]) + '''</div><div class="followers">''' + str(profile.get("followers") or 0) + ''' followers · ''' + str(profile.get("following") or 0) + ''' following</div><a class="profile-link" href="https://github.com/Elon-Jiang">↗ github.com/Elon-Jiang</a><div class="preview-label">个人主页效果预览<br>右侧内容将显示在 GitHub 主页 README 中。</div></aside>
<section class="readme"><div class="file-label">Elon-Jiang / README.md</div><div class="cards"><img class="card" data-kind="hero" src="assets/hero-light.svg" alt="个人介绍"><img class="card" data-kind="contributions" src="assets/contributions-light.svg" alt="真实公开贡献统计"><p class="footnote"><b>''' + f'{stats["total"]} 个公开 PR · {stats["merged"]} 已合并 · {stats["open"]} 待合并' + '''</b><br>仅统计提给他人仓库的公开 PR。Star 数属于所贡献的项目。</p><details><summary>查看全部 PR 与贡献仓库</summary>''' + full_list + '''</details></div></section></main>
<script>
function theme(value){document.body.classList.toggle('dark',value==='dark');document.querySelectorAll('[data-kind]').forEach(img=>img.src='assets/'+img.dataset.kind+'-'+value+'.svg');['light','dark'].forEach(k=>document.getElementById(k).setAttribute('aria-pressed',String(k===value)))}
document.getElementById('light').onclick=()=>theme('light');document.getElementById('dark').onclick=()=>theme('dark');
if(window.matchMedia('(prefers-color-scheme: dark)').matches)theme('dark');
</script></html>
'''


def generate(config, data):
    if data["username"].lower() != config["username"].lower():
        raise ValueError("Snapshot username does not match profile.json")
    stats = summarize(data)
    # Construct everything before writing any of the output files.
    outputs = {"README.md": readme(config, data, stats), "preview.html": preview(config, data, stats),
               "data/public-contributions.json": json.dumps(data, ensure_ascii=False, indent=2) + "\n"}
    for theme in THEMES:
        outputs[f"assets/hero-{theme}.svg"] = hero_svg(config, theme)
        outputs[f"assets/contributions-{theme}.svg"] = stats_svg(config, data, stats, theme)
    for path, content in outputs.items():
        target = ROOT / path
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(target)
    print(json.dumps({"total_public_prs": stats["total"], "merged": stats["merged"], "open": stats["open"], "closed_unmerged": stats["closed"], "repositories": len(stats["repos"]), "updated_at": data["updated_at"]}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", type=Path, help="Regenerate using an existing public snapshot; no network or credentials")
    parser.add_argument("--public", action="store_true", help="Use unauthenticated public API access; do not read credentials")
    args = parser.parse_args()
    config = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))
    if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", config["username"]):
        raise ValueError("Invalid GitHub username")
    if not isinstance(config["max_repositories"], int) or not 1 <= config["max_repositories"] <= 20:
        raise ValueError("max_repositories must be between 1 and 20")
    data = json.loads(args.offline.read_text(encoding="utf-8")) if args.offline else fetch_data(config, args.public)
    generate(config, data)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Refresh failed: " + str(error), file=sys.stderr)
        sys.exit(1)
