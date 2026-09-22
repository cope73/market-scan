#!/usr/bin/env python3
"""Push harness outputs into a Notion page so the daily brief can read them without a public repo.

Requires env NOTION_TOKEN (internal integration secret) and NOTION_PARENT_PAGE (the hub page id).
Creates or overwrites a child page titled 'Trading - Harness Output' with scan, verify, and earnings window.
Silently exits 0 when NOTION_TOKEN is not set, so the workflow keeps working before the secret exists.
"""
import os, sys, json, datetime as dt, urllib.request

TOKEN = os.environ.get("NOTION_TOKEN")
PARENT = os.environ.get("NOTION_PARENT_PAGE", "2233ed8aca6a8041aa81ddc70adc41a5")
TITLE = "Trading - Harness Output"
API = "https://api.notion.com/v1"
H = {"Authorization": f"Bearer {TOKEN}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}

if not TOKEN:
    print("NOTION_TOKEN not set; skipping Notion push"); sys.exit(0)


def call(method, path, body=None):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode() if body else None, headers=H, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def read(p, limit=1900):
    try:
        return open(p).read()
    except FileNotFoundError:
        return f"(missing {p})"


def chunks(text, n=1900):
    return [text[i:i + n] for i in range(0, len(text), n)] or [""]


def code_block(text):
    return {"object": "block", "type": "code", "code": {"language": "plain text",
            "rich_text": [{"type": "text", "text": {"content": c}} for c in chunks(text)]}}


def heading(t):
    return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": t}}]}}


def find_existing():
    res = call("POST", "/search", {"query": TITLE, "filter": {"property": "object", "value": "page"}})
    for r in res.get("results", []):
        t = r.get("properties", {}).get("title", {}).get("title", [])
        if t and t[0]["plain_text"] == TITLE:
            return r["id"]
    return None


def main():
    stamp = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    blocks = [heading(f"Last run {stamp}"),
              heading("scan.txt"), code_block(read("out/scan.txt")),
              heading("verify.txt (anything not OK is excluded today)"), code_block(read("out/verify.txt")),
              heading("universes.json"), code_block(read("universes.json"))]
    pid = find_existing()
    if pid:
        # archive old children, then append fresh ones
        kids = call("GET", f"/blocks/{pid}/children?page_size=100").get("results", [])
        for k in kids:
            call("PATCH", f"/blocks/{k['id']}", {"archived": True})
        for i in range(0, len(blocks), 50):
            call("PATCH", f"/blocks/{pid}/children", {"children": blocks[i:i + 50]})
    else:
        call("POST", "/pages", {"parent": {"page_id": PARENT},
                                 "properties": {"title": {"title": [{"text": {"content": TITLE}}]}},
                                 "children": blocks[:50]})
    print("Notion push ok", stamp)


if __name__ == "__main__":
    main()
