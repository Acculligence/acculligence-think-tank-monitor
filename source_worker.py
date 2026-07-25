#!/usr/bin/env python3
"""Acculligence Think Tank Monitor v5 source worker.

Key correction:
- Uses the existing Acculligence dashboard feed registry first.
- Preserves feed metadata and summaries.
- Direct feeds: fetch and clean the original page, with feed content as fallback.
- Google News fallback: capture dated feed items even when the redirect cannot be
  resolved, and classify them as discovery_partial rather than silently losing them.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timezone
from pathlib import Path
from urllib.parse import urlparse

import dateparser
import feedparser
import requests
import trafilatura
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

UA = "AcculligenceThinkTankCollector/5.0 (+https://acculligence.com)"

def session():
    s=requests.Session()
    s.headers.update({
        "User-Agent":UA,
        "Accept":"text/html,application/xhtml+xml,application/rss+xml,application/atom+xml,application/xml;q=0.9,*/*;q=0.8"
    })
    return s

def fetch(s,url,timeout,accept=None):
    headers={"Accept":accept} if accept else {}
    r=s.get(url,timeout=(min(5,timeout),timeout),allow_redirects=True,headers=headers)
    r.raise_for_status()
    return r

def parse_date(value):
    if not value: return None
    try: d=dtparser.parse(str(value))
    except Exception: d=dateparser.parse(str(value))
    if not d: return None
    if not d.tzinfo: d=d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)

def clean_html(value):
    if not value: return ""
    return BeautifulSoup(html.unescape(str(value)),"lxml").get_text(" ",strip=True)

def keyword_hits(title,body,keywords):
    hay=f"{title}\n{body}"
    hits=[]
    for keyword in keywords:
        flags=re.I if keyword.isascii() else 0
        if re.search(re.escape(keyword),hay,flags):
            hits.append(keyword)
    return hits

def feed_candidates(source,start,end,timeout,max_candidates):
    s=session()
    candidates=[]
    errors=[]
    feed_items=0
    routes_used=[]
    for route in source.get("routes",[]):
        if route.get("type")!="feed": continue
        url=route["url"]
        try:
            r=fetch(s,url,timeout,"application/rss+xml,application/atom+xml,application/xml,text/xml,*/*")
            parsed=feedparser.parse(r.content)
            entries=list(getattr(parsed,"entries",[]))
            feed_items += len(entries)
            if entries:
                routes_used.append(url)
            for entry in entries:
                date_value=entry.get("published") or entry.get("updated") or entry.get("created") or ""
                d=parse_date(date_value)
                if not d or not(start<=d<=end):
                    continue
                title=clean_html(entry.get("title",""))
                summary=clean_html(entry.get("summary") or entry.get("description") or "")
                content=""
                if entry.get("content"):
                    content=" ".join(clean_html(part.get("value","")) for part in entry.content if isinstance(part,dict))
                link=entry.get("link","")
                candidates.append({
                    "url":link,
                    "title":title,
                    "date":d.isoformat(),
                    "summary":summary,
                    "feed_content":content,
                    "route":url,
                    "origin":route.get("origin",""),
                    "source_url":entry.get("source",{}).get("href","") if isinstance(entry.get("source"),dict) else "",
                })
                if len(candidates)>=max_candidates: break
        except Exception as exc:
            errors.append(f"feed {url}: {type(exc).__name__}: {str(exc)[:180]}")
        if len(candidates)>=max_candidates: break
    return candidates,routes_used,errors,feed_items

def page_metadata(soup):
    title=""
    og=soup.find("meta",property="og:title")
    if og and og.get("content"): title=og["content"].strip()
    elif soup.title: title=soup.title.get_text(" ",strip=True)
    date_value=""
    for attrs in (
        {"property":"article:published_time"},{"name":"date"},{"name":"pubdate"},
        {"name":"publication_date"},{"itemprop":"datePublished"},
    ):
        tag=soup.find("meta",attrs=attrs)
        if tag and tag.get("content"):
            date_value=tag["content"]; break
    if not date_value:
        tag=soup.find("time")
        if tag: date_value=tag.get("datetime") or tag.get_text(" ",strip=True)
    return title,date_value

def extract_candidate(candidate,source,keywords,start,end,timeout):
    feed_date=parse_date(candidate.get("date"))
    feed_text=(candidate.get("feed_content") or candidate.get("summary") or "").strip()
    title=candidate.get("title","").strip()
    url=candidate.get("url","")
    origin=(candidate.get("origin") or "").lower()

    # Google News is a discovery fallback. Its redirect may not expose the source
    # page reliably to a server-side client, so preserve the feed item instead of
    # discarding it.
    if origin=="google_news" or "news.google.com" in urlparse(url).netloc:
        hits=keyword_hits(title,feed_text,keywords)
        if not hits: return None
        return {
            "source":source["source_name"],"domain":source["domain"],"title":title,
            "published_at":feed_date.isoformat() if feed_date else "",
            "url":url,"matched_keywords":" | ".join(hits),"body":feed_text,
            "collection_route":candidate.get("route",""),
            "review_status":"discovery_partial","saudi_summary_ar":"",
            "topic":"","sentiment":"","author":""
        }

    page_body=""
    final_url=url
    page_title=""
    page_date=None
    if url:
        try:
            s=session()
            r=fetch(s,url,timeout)
            final_url=r.url
            page_body=trafilatura.extract(
                r.text,include_comments=False,include_tables=True,
                include_links=False,favor_recall=True,deduplicate=True
            ) or ""
            soup=BeautifulSoup(r.text,"lxml")
            page_title,date_value=page_metadata(soup)
            page_date=parse_date(date_value)
        except Exception:
            pass

    body=(page_body or feed_text).strip()
    title=(page_title or title).strip()
    published=page_date or feed_date
    if not published or not(start<=published<=end): return None
    if not body: body=title
    hits=keyword_hits(title,body,keywords)
    if not hits: return None
    return {
        "source":source["source_name"],"domain":source["domain"],"title":title,
        "published_at":published.isoformat(),"url":final_url or url,
        "matched_keywords":" | ".join(hits),"body":body,
        "collection_route":candidate.get("route",""),
        "review_status":"pending" if page_body else "feed_text_fallback",
        "saudi_summary_ar":"","topic":"","sentiment":"","author":""
    }

def atomic_write(path,payload):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(payload,ensure_ascii=False),encoding="utf-8")
    os.replace(tmp,path)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--source",required=True)
    ap.add_argument("--keywords",required=True)
    ap.add_argument("--start",required=True)
    ap.add_argument("--end",required=True)
    ap.add_argument("--result",required=True)
    ap.add_argument("--request-timeout",type=int,default=12)
    ap.add_argument("--max-candidates",type=int,default=240)
    ap.add_argument("--article-workers",type=int,default=6)
    args=ap.parse_args()
    started=time.monotonic()

    source=json.loads(Path(args.source).read_text(encoding="utf-8"))
    keywords=json.loads(Path(args.keywords).read_text(encoding="utf-8"))["keywords"]
    start=dtparser.parse(args.start+"T00:00:00Z")
    end=dtparser.parse(args.end+"T23:59:59Z")

    candidates,used,errors,feed_items=feed_candidates(
        source,start,end,args.request_timeout,args.max_candidates
    )
    articles=[]
    with ThreadPoolExecutor(max_workers=max(1,args.article_workers)) as pool:
        futures=[
            pool.submit(extract_candidate,c,source,keywords,start,end,args.request_timeout)
            for c in candidates
        ]
        for future in as_completed(futures):
            try:
                article=future.result()
                if article: articles.append(article)
            except Exception:
                pass

    status="complete" if used else ("route_error" if errors else "no_candidates")
    payload={"articles":articles,"audit":{
        "source":source["source_name"],"domain":source["domain"],
        "status":status,"routes_used":" | ".join(used),
        "candidate_urls":len(candidates),"dated_items":len(candidates),
        "feed_items":feed_items,"matched_items":len(articles),
        "duration_seconds":round(time.monotonic()-started,2),
        "notes":"; ".join(errors)[:1500],
    }}
    atomic_write(args.result,payload)

if __name__=="__main__":
    main()
