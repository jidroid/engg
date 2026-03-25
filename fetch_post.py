#!/usr/bin/env python3
"""
Daily engineering blog post picker.
Reads merged_feeds.csv, picks a weighted-random blog, fetches a recent post,
and generates index.html for GitHub Pages.
"""

import csv
import json
import os
import random
import re
import html as html_module
import socket
from datetime import datetime, timezone

import google.generativeai as genai
import feedparser

FEED_TIMEOUT = 10
MAX_RECENT_POSTS = 15
MAX_RETRIES = 30
NUM_CANDIDATE_BLOGS = 5
SERVED_URLS_PATH = "served_urls.txt"


def normalize_url(url):
    """Normalize a URL so http/https and trailing-slash variants match."""
    url = url.strip()
    url = re.sub(r"^https?://", "https://", url)
    url = url.rstrip("/")
    return url


def load_served_urls():
    if not os.path.exists(SERVED_URLS_PATH):
        return set()
    with open(SERVED_URLS_PATH, encoding="utf-8") as f:
        return {normalize_url(line) for line in f if line.strip()}


def save_served_url(url):
    with open(SERVED_URLS_PATH, "a", encoding="utf-8") as f:
        f.write(normalize_url(url) + "\n")


def load_blogs(csv_path="merged_feeds.csv"):
    blogs = []
    seen = set()
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rss_url = row["rss_url"].strip()
            if rss_url and rss_url not in seen:
                seen.add(rss_url)
                blogs.append(row)
    return blogs


def fetch_best_post(blogs, served_urls):
    """Sample NUM_CANDIDATE_BLOGS blogs, collect their posts, let Gemini pick the best."""
    weights = [max(1, int(row["score"])) for row in blogs]
    tried = set()
    candidates = []  # list of (blog, post)

    for _ in range(MAX_RETRIES):
        if len(tried) >= NUM_CANDIDATE_BLOGS:
            break

        (blog,) = random.choices(blogs, weights=weights, k=1)
        rss_url = blog["rss_url"].strip()
        if rss_url in tried:
            continue
        tried.add(rss_url)

        print(f"  Trying: {blog['name']}  ({rss_url})")
        try:
            socket.setdefaulttimeout(FEED_TIMEOUT)
            feed = feedparser.parse(
                rss_url,
                request_headers={"User-Agent": "Mozilla/5.0"},
            )
            entries = [e for e in feed.entries[:MAX_RECENT_POSTS] if e.get("link")]
            for post in entries:
                if normalize_url(post["link"]) not in served_urls:
                    candidates.append((blog, post))
                    print(f"    {post.get('title', '')[:70]}")
                else:
                    print(f"    [skip-served] {post.get('title', '')[:70]}")
        except Exception as e:
            print(f"    Error: {e}")

    if not candidates:
        return None, None

    print(f"\nRanking {len(candidates)} posts with Gemini...")
    best_index = rank_candidates(candidates)
    return candidates[best_index]


def rank_candidates(candidates):
    """
    Use Gemini to pick the most technically valuable post from the candidates.
    candidates: list of (blog_row, post) tuples.
    Returns the index of the best candidate, or falls back to longest summary.
    """
    numbered = []
    for i, (blog, post) in enumerate(candidates):
        title = (post.get("title") or "Untitled").strip()
        summary = get_summary(post, max_chars=200)
        numbered.append(f"{i}. [{blog['name']}] {title}\n   {summary}")

    prompt = (
        "You are helping engineers discover high-quality technical blog posts.\n\n"
        "From the list below, pick the ONE post most likely to contain deep technical "
        "learnings — e.g. architecture decisions, system design, debugging stories, "
        "performance investigations, or engineering trade-offs. Avoid announcements, "
        "product launches, hiring posts, roundups, and generic tutorials.\n\n"
        "Posts:\n"
        + "\n\n".join(numbered)
        + "\n\nReply with a JSON object containing a single key \"index\" "
        "(0-based integer). Example: {\"index\": 3}"
    )

    try:
        genai.configure(api_key=os.environ["GEMINI_API_KEY"])
        model = genai.GenerativeModel("gemini-2.0-flash")
        response = model.generate_content(prompt)
        raw = response.text.strip()
        index = json.loads(raw)["index"]
        if 0 <= index < len(candidates):
            print(f"  Gemini picked index {index}: {candidates[index][1].get('title', '')}")
            return index
    except Exception as e:
        print(f"  Gemini ranking failed ({e}), falling back to longest summary")

    # Fallback: pick the post with the longest summary (most content)
    return max(range(len(candidates)), key=lambda i: len(get_summary(candidates[i][1])))


def strip_html(text):
    text = re.sub(r"<[^>]+>", "", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def get_summary(post, max_chars=350):
    raw = ""
    if post.get("content"):
        raw = post.content[0].get("value", "")
    if not raw:
        raw = post.get("summary", "")
    text = strip_html(raw)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "…"
    return text


def format_date(post):
    for key in ("published", "updated"):
        val = post.get(key)
        if val:
            # feedparser parsed struct
            st = post.get(f"{key}_parsed")
            if st:
                try:
                    dt = datetime(*st[:6], tzinfo=timezone.utc)
                    return dt.strftime("%b %-d, %Y")
                except Exception:
                    pass
            return val[:20]
    return ""


def generate_html(blog, post, total_blogs):
    now = datetime.now(timezone.utc)
    today = now.strftime(f"%A, %B {now.day}, %Y")

    title = html_module.escape(post.get("title", "Untitled").strip())
    link = post.get("link", blog["url"])
    blog_name = html_module.escape(blog["name"])
    blog_url = html_module.escape(blog["url"])
    blog_type = blog.get("type", "")
    summary = html_module.escape(get_summary(post))
    author = html_module.escape((post.get("author") or "").strip())
    pub_date = format_date(post)

    meta_parts = []
    if author:
        meta_parts.append(f"by {author}")
    if pub_date:
        meta_parts.append(pub_date)
    meta_html = " · ".join(meta_parts)

    tier_badge = ""
    if "Tier 1" in blog_type:
        tier_badge = '<span class="tier tier1">Tier 1</span>'
    elif "Tier 2" in blog_type:
        tier_badge = '<span class="tier tier2">Tier 2</span>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Daily Eng Read — {today}</title>
  <meta property="og:title" content="{title}">
  <meta property="og:description" content="{summary}">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    :root {{
      --bg: #f4f5f7;
      --card: #ffffff;
      --accent: #4361ee;
      --accent-light: #eef0fd;
      --text: #111827;
      --muted: #6b7280;
      --border: #e5e7eb;
      --radius: 16px;
    }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      background: var(--bg);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 2rem 1rem;
      color: var(--text);
    }}

    .card {{
      background: var(--card);
      border-radius: var(--radius);
      padding: 2.5rem 3rem;
      max-width: 680px;
      width: 100%;
      box-shadow: 0 2px 16px rgba(0,0,0,0.07), 0 0 0 1px rgba(0,0,0,0.04);
    }}

    .date-label {{
      font-size: 0.72rem;
      font-weight: 600;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.1em;
      margin-bottom: 1.5rem;
    }}

    .source-row {{
      display: flex;
      align-items: center;
      gap: 0.5rem;
      margin-bottom: 1.25rem;
      flex-wrap: wrap;
    }}

    .source {{
      display: inline-flex;
      align-items: center;
      background: var(--accent-light);
      color: var(--accent);
      padding: 0.3rem 0.85rem;
      border-radius: 100px;
      font-size: 0.82rem;
      font-weight: 600;
      text-decoration: none;
      transition: opacity 0.15s;
    }}
    .source:hover {{ opacity: 0.75; }}

    .tier {{
      font-size: 0.7rem;
      font-weight: 600;
      padding: 0.2rem 0.6rem;
      border-radius: 100px;
      letter-spacing: 0.04em;
    }}
    .tier1 {{ background: #fef9c3; color: #a16207; }}
    .tier2 {{ background: #f0fdf4; color: #166534; }}

    h1 {{
      font-size: clamp(1.35rem, 3vw, 1.85rem);
      line-height: 1.3;
      color: var(--text);
      margin-bottom: 0.75rem;
      font-weight: 700;
    }}

    .meta {{
      font-size: 0.82rem;
      color: var(--muted);
      margin-bottom: 1.25rem;
    }}

    .excerpt {{
      color: #374151;
      line-height: 1.75;
      font-size: 0.97rem;
      margin-bottom: 2rem;
    }}

    .btn {{
      display: inline-block;
      background: var(--accent);
      color: #fff;
      padding: 0.8rem 2rem;
      border-radius: 8px;
      text-decoration: none;
      font-weight: 600;
      font-size: 0.97rem;
      transition: opacity 0.15s, transform 0.1s;
    }}
    .btn:hover {{ opacity: 0.88; transform: translateY(-1px); }}
    .btn:active {{ transform: translateY(0); }}

    .footer {{
      margin-top: 2rem;
      padding-top: 1.5rem;
      border-top: 1px solid var(--border);
      font-size: 0.75rem;
      color: #9ca3af;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 0.5rem;
    }}

    @media (max-width: 600px) {{
      .card {{ padding: 1.75rem 1.5rem; }}
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="date-label">Today's Engineering Read &mdash; {today}</div>

    <div class="source-row">
      <a class="source" href="{blog_url}" target="_blank" rel="noopener">{blog_name}</a>
      {tier_badge}
    </div>

    <h1>{title}</h1>

    {"" if not meta_html else f'<div class="meta">{meta_html}</div>'}

    {"" if not summary else f'<p class="excerpt">{summary}</p>'}

    <a class="btn" href="{link}" target="_blank" rel="noopener">Read Post &rarr;</a>

    <div class="footer">
      <span>Refreshes daily &middot; {total_blogs} blogs in rotation</span>
      <span>Powered by GitHub Actions</span>
    </div>
  </div>
</body>
</html>
"""


def main():
    print("Loading blogs...")
    blogs = load_blogs()
    print(f"  {len(blogs)} unique feeds loaded")

    served_urls = load_served_urls()
    print(f"  {len(served_urls)} previously served URLs loaded")

    blog, post = None, None
    for attempt in range(3):
        print(f"\nFetching best post (attempt {attempt + 1})...")
        blog, post = fetch_best_post(blogs, served_urls)
        if blog and post:
            break
        print("  No unseen posts found, retrying with new candidates...")

    if not blog or not post:
        print("ERROR: Could not fetch any unseen post after retries.")
        raise SystemExit(1)

    post_url = post.get("link", "").strip()
    print(f"\nSelected: {blog['name']}")
    print(f"  Post: {post.get('title', '(no title)')}")

    save_served_url(post_url)
    print(f"  Saved to {SERVED_URLS_PATH}")

    html = generate_html(blog, post, len(blogs))
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    print("\nindex.html written successfully.")


if __name__ == "__main__":
    main()
