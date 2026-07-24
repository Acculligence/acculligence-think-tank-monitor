#!/usr/bin/env python3
"""
Build and validate a deterministic acquisition map for every source.

The mapper searches ONLY the official source domain for:
- RSS/Atom feeds declared in HTML
- robots.txt sitemap declarations
- sitemap index / XML sitemaps
- WordPress REST API
- common feed endpoints
- all-publications / research / analysis archive pages

It validates every route before adding it to config/acquisition_map.json.
The collector never guesses routes; it consumes this validated map.
"""
from __future__ import annotations
import argparse, json, re, time
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests, feedparser
from bs4 import BeautifulSoup

UA="AcculligenceThinkTankMapper/2.0 (+https://acculligence.com)"
TIMEOUT=30
S=requests.Session()
S.headers.update({"User-Agent":UA,"Accept":"text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8"})

ARCHIVE_WORDS=[
    "publications","research","analysis","commentary","articles","insights",
    "reports","studies","papers","publications & resources","our work"
]
COMMON_FEEDS=["/feed/","/feed","/rss","/rss.xml","/feed.xml","/atom.xml","/index.xml"]
COMMON_SITEMAPS=["/sitemap.xml","/sitemap_index.xml","/sitemap-index.xml"]

def get(url, accept=None):
    h={"Accept":accept} if accept else {}
    r=S.get(url,timeout=TIMEOUT,allow_redirects=True,headers=h)
    r.raise_for_status()
    return r

def same_domain(url,domain):
    host=urlparse(url).netloc.lower().replace("www.","")
    return host==domain or host.endswith("."+domain)

def valid_feed(url):
    try:
        d=feedparser.parse(url)
        if not getattr(d,"entries",None): return False,0
        good=sum(1 for e in d.entries if e.get("link") and (e.get("title") or e.get("summary")))
        return good>0,good
    except Exception:
        return False,0

def valid_sitemap(url):
    try:
        r=get(url,"application/xml,text/xml,*/*")
        soup=BeautifulSoup(r.content,"xml")
        locs=[x.get_text(strip=True) for x in soup.find_all("loc")]
        return len(locs)>0,len(locs)
    except Exception:
        return False,0

def valid_wp_api(url):
    try:
        r=get(url,"application/json")
        data=r.json()
        return isinstance(data,list) and len(data)>0,len(data)
    except Exception:
        return False,0

def valid_archive(url,domain):
    try:
        r=get(url)
        if not same_domain(r.url,domain): return False,0
        soup=BeautifulSoup(r.text,"lxml")
        links=[]
        for a in soup.find_all("a",href=True):
            u=urljoin(r.url,a["href"])
            if same_domain(u,domain):
                p=urlparse(u).path.lower()
                if any(x in p for x in ["/publication","/research","/analysis","/commentary","/article","/report","/insight","/paper","/study"]):
                    links.append(u)
        return len(set(links))>=3,len(set(links))
    except Exception:
        return False,0

def discover(source):
    domain=source["domain"]
    roots=[f"https://{domain}/",f"https://www.{domain}/"]
    homepage=None; html=""
    for u in roots:
        try:
            r=get(u); homepage=r.url; html=r.text; break
        except Exception:
            pass
    result={
        "source_id":source["id"],"source_name":source["name"],"domain":domain,
        "status":"blocked","routes":[],"notes":[]
    }
    if not homepage:
        result["notes"].append("Homepage unreachable from mapper.")
        return result

    soup=BeautifulSoup(html,"lxml")
    candidates=[]

    # Declared feeds.
    for link in soup.find_all("link",href=True):
        typ=(link.get("type") or "").lower()
        rel=" ".join(link.get("rel") or []).lower()
        if "alternate" in rel and ("rss" in typ or "atom" in typ or "xml" in typ):
            candidates.append(("feed",urljoin(homepage,link["href"]),"html-declared"))

    # robots.txt sitemaps.
    root=f"{urlparse(homepage).scheme}://{urlparse(homepage).netloc}"
    try:
        robots=get(urljoin(root,"/robots.txt")).text
        for line in robots.splitlines():
            if line.lower().startswith("sitemap:"):
                candidates.append(("sitemap",line.split(":",1)[1].strip(),"robots.txt"))
    except Exception:
        pass

    for p in COMMON_FEEDS:
        candidates.append(("feed",urljoin(root,p),"common-endpoint"))
    for p in COMMON_SITEMAPS:
        candidates.append(("sitemap",urljoin(root,p),"common-endpoint"))

    # WordPress REST API: fetch enough posts to allow backfill.
    candidates.append(("wp_api",urljoin(root,"/wp-json/wp/v2/posts?per_page=100&page=1"),"wordpress-api"))

    # Official archive pages linked from homepage.
    for a in soup.find_all("a",href=True):
        text=" ".join(a.get_text(" ",strip=True).split()).lower()
        href=urljoin(homepage,a["href"])
        if same_domain(href,domain) and any(w in text for w in ARCHIVE_WORDS):
            candidates.append(("archive",href,"homepage-link"))

    # Registry direct feed is tested, but Google News is not accepted as an official route.
    reg=source.get("registry_feed","")
    if source.get("registry_origin")=="direct" and reg:
        candidates.insert(0,("feed",reg,"registry-direct"))

    dedup=set()
    for typ,url,origin in candidates:
        key=(typ,url.rstrip("/"))
        if key in dedup or not same_domain(url,domain):
            continue
        dedup.add(key)
        ok=count=False
        if typ=="feed": ok,count=valid_feed(url)
        elif typ=="sitemap": ok,count=valid_sitemap(url)
        elif typ=="wp_api": ok,count=valid_wp_api(url)
        elif typ=="archive": ok,count=valid_archive(url,domain)
        if ok:
            result["routes"].append({
                "type":typ,"url":url,"origin":origin,"validation_count":count,
                "priority":{"wp_api":1,"feed":2,"sitemap":3,"archive":4}[typ]
            })

    result["routes"]=sorted(result["routes"],key=lambda x:(x["priority"],-x["validation_count"]))
    if result["routes"]:
        result["status"]="validated"
    else:
        result["notes"].append("No validated official route. Manual mapping required; Google News is intentionally excluded from the primary map.")
    return result

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--sources",default="config/sources.json")
    ap.add_argument("--output",default="config/acquisition_map.json")
    ap.add_argument("--max-sources",type=int,default=0)
    ap.add_argument("--sleep",type=float,default=0.4)
    args=ap.parse_args()
    sources=json.load(open(args.sources,encoding="utf-8"))
    if args.max_sources: sources=sources[:args.max_sources]
    results=[]
    for i,s in enumerate(sources,1):
        print(f"[{i}/{len(sources)}] Mapping {s['name']} ({s['domain']})",flush=True)
        results.append(discover(s))
        time.sleep(args.sleep)
    payload={
        "generated_at":__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "policy":"Official validated routes only. Google News may be used later only as an explicitly approved fallback.",
        "sources":results
    }
    Path(args.output).parent.mkdir(parents=True,exist_ok=True)
    json.dump(payload,open(args.output,"w",encoding="utf-8"),ensure_ascii=False,indent=2)
    validated=sum(x["status"]=="validated" for x in results)
    print(json.dumps({"total":len(results),"validated":validated,"blocked":len(results)-validated},ensure_ascii=False))
if __name__=="__main__":
    main()
