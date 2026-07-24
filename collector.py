#!/usr/bin/env python3
"""
Acculligence Think Tank Collector v2

Consumes ONLY config/acquisition_map.json routes that were validated by
build_acquisition_map.py. It does not perform live route guessing.

Inclusion: ANY approved keyword in title OR cleaned MAIN article/report body.
Excluded before matching: navigation, related-content modules, recommendations,
footers, menus, tag clouds and other page furniture.
"""
from __future__ import annotations
import argparse, csv, hashlib, json, re, time
from dataclasses import dataclass, asdict
from datetime import timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse
import requests, feedparser, dateparser
from bs4 import BeautifulSoup
import trafilatura
from dateutil import parser as dtparser

UA="AcculligenceThinkTankCollector/2.0 (+https://acculligence.com)"
TIMEOUT=30
S=requests.Session()
S.headers.update({"User-Agent":UA,"Accept":"text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8"})

@dataclass
class Item:
    source:str; domain:str; title:str; published_at:str; url:str
    matched_keywords:str; body:str; collection_route:str
    review_status:str="pending"; saudi_summary_ar:str=""
    topic:str=""; sentiment:str=""; author:str=""

def get(url,accept=None):
    r=S.get(url,timeout=TIMEOUT,allow_redirects=True,headers={"Accept":accept} if accept else {})
    r.raise_for_status(); return r

def same_domain(url,domain):
    h=urlparse(url).netloc.lower().replace("www.","")
    return h==domain or h.endswith("."+domain)

def norm_url(u):
    p=urlparse(u)
    return f"{p.scheme.lower()}://{p.netloc.lower().replace('www.','')}{p.path.rstrip('/')}"

def parse_date(v):
    if not v:return None
    try:d=dtparser.parse(v)
    except Exception:d=dateparser.parse(v)
    if not d:return None
    if not d.tzinfo:d=d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)

def extract_article(url):
    try:r=get(url)
    except Exception:return None
    text=trafilatura.extract(
        r.text,include_comments=False,include_tables=True,include_links=False,
        favor_recall=True,deduplicate=True
    ) or ""
    if not text.strip():return None
    soup=BeautifulSoup(r.text,"lxml")
    title=""
    og=soup.find("meta",property="og:title")
    if og:title=og.get("content","")
    if not title and soup.title:title=soup.title.get_text(" ",strip=True)
    date=""
    for attrs in [
        {"property":"article:published_time"},{"name":"date"},{"name":"pubdate"},
        {"name":"publication_date"},{"itemprop":"datePublished"}
    ]:
        t=soup.find("meta",attrs=attrs)
        if t and t.get("content"):date=t["content"];break
    if not date:
        t=soup.find("time")
        if t:date=t.get("datetime") or t.get_text(" ",strip=True)
    return title.strip(),text.strip(),date

def keyword_hits(title,body,keywords):
    hay=title+"\n"+body
    return [k for k in keywords if re.search(re.escape(k),hay,re.I if k.isascii() else 0)]

def feed_candidates(url):
    d=feedparser.parse(url); out=[]
    for e in getattr(d,"entries",[]):
        if e.get("link"):
            out.append({"url":e["link"],"title":e.get("title",""),"date":e.get("published") or e.get("updated") or "","route":url})
    return out

def sitemap_candidates(url,domain,limit=30000):
    try:r=get(url,"application/xml,text/xml,*/*")
    except Exception:return []
    soup=BeautifulSoup(r.content,"xml")
    nested=[x.get_text(strip=True) for x in soup.find_all("sitemap") for x in x.find_all("loc")]
    if nested:
        out=[]
        for n in nested[:200]:
            out.extend(sitemap_candidates(n,domain,max(100,limit-len(out))))
            if len(out)>=limit:break
        return out[:limit]
    urls=[x.get_text(strip=True) for x in soup.find_all("url") for x in x.find_all("loc")]
    return [{"url":u,"title":"","date":"","route":url} for u in urls if same_domain(u,domain)][:limit]

def wp_candidates(url,domain,max_pages=100):
    out=[]
    base=url.split("?")[0]
    for page in range(1,max_pages+1):
        q={"per_page":100,"page":page,"_fields":"link,date,title"}
        try:r=get(base+"?"+urlencode(q),"application/json")
        except Exception:break
        try:data=r.json()
        except Exception:break
        if not isinstance(data,list) or not data:break
        for x in data:
            u=x.get("link","")
            if u and same_domain(u,domain):
                title=x.get("title",{}).get("rendered","") if isinstance(x.get("title"),dict) else str(x.get("title",""))
                out.append({"url":u,"title":BeautifulSoup(title,"lxml").get_text(" ",strip=True),"date":x.get("date",""),"route":url})
    return out

def archive_candidates(url,domain,max_pages=60):
    out=[]; current=url
    for _ in range(max_pages):
        try:r=get(current)
        except Exception:break
        soup=BeautifulSoup(r.text,"lxml")
        for a in soup.find_all("a",href=True):
            u=urljoin(r.url,a["href"])
            p=urlparse(u).path.lower()
            if same_domain(u,domain) and any(x in p for x in ["/publication","/research","/analysis","/commentary","/article","/report","/insight","/paper","/study"]):
                out.append({"url":u,"title":a.get_text(" ",strip=True),"date":"","route":url})
        nxt=soup.find("a",rel=lambda x:x and "next" in x) or soup.find("a",string=re.compile(r"next|older|التالي",re.I))
        if not nxt or not nxt.get("href"):break
        current=urljoin(r.url,nxt["href"])
    return out

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--start",required=True);ap.add_argument("--end",required=True)
    ap.add_argument("--map",default="config/acquisition_map.json")
    ap.add_argument("--keywords",default="config/keywords.json")
    ap.add_argument("--output",default="output")
    args=ap.parse_args()
    start=dtparser.parse(args.start+"T00:00:00Z");end=dtparser.parse(args.end+"T23:59:59Z")
    amap=json.load(open(args.map,encoding="utf-8"))["sources"]
    keywords=json.load(open(args.keywords,encoding="utf-8"))["keywords"]
    outdir=Path(args.output);outdir.mkdir(parents=True,exist_ok=True)
    items=[];audit=[];seen=set()

    for i,s in enumerate(amap,1):
        print(f"[{i}/{len(amap)}] {s['source_name']}",flush=True)
        if s["status"]!="validated":
            audit.append({"source":s["source_name"],"domain":s["domain"],"status":"blocked","routes_used":"","candidate_urls":0,"dated_items":0,"matched_items":0,"notes":"No validated official route."})
            continue
        candidates=[];used=[]
        for route in s["routes"]:
            typ,url=route["type"],route["url"]
            try:
                if typ=="feed": c=feed_candidates(url)
                elif typ=="sitemap": c=sitemap_candidates(url,s["domain"])
                elif typ=="wp_api": c=wp_candidates(url,s["domain"])
                else:c=archive_candidates(url,s["domain"])
            except Exception:c=[]
            if c:used.append(url);candidates.extend(c)
        unique=[];local=set()
        for c in candidates:
            try:k=norm_url(c["url"])
            except Exception:continue
            if k not in local:local.add(k);unique.append(c)
        dated=matched=0
        for c in unique:
            key=hashlib.sha1(norm_url(c["url"]).encode()).hexdigest()
            if key in seen:continue
            seen.add(key)
            data=extract_article(c["url"])
            if not data:continue
            title,body,pagedate=data
            d=parse_date(pagedate or c.get("date"))
            if not d or not(start<=d<=end):continue
            dated+=1
            hits=keyword_hits(title,body,keywords)
            if not hits:continue
            matched+=1
            items.append(Item(
                source=s["source_name"],domain=s["domain"],title=title,published_at=d.isoformat(),
                url=c["url"],matched_keywords=" | ".join(hits),body=body,collection_route=c.get("route","")
            ))
        audit.append({"source":s["source_name"],"domain":s["domain"],"status":"complete","routes_used":" | ".join(used),"candidate_urls":len(unique),"dated_items":dated,"matched_items":matched,"notes":""})
    fields=list(Item.__dataclass_fields__)
    art=outdir/f"articles_{args.start}_{args.end}.csv"
    aud=outdir/f"audit_{args.start}_{args.end}.csv"
    with open(art,"w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for x in sorted(items,key=lambda z:z.published_at):w.writerow(asdict(x))
    with open(aud,"w",encoding="utf-8-sig",newline="") as f:
        fields2=["source","domain","status","routes_used","candidate_urls","dated_items","matched_items","notes"]
        w=csv.DictWriter(f,fieldnames=fields2);w.writeheader();w.writerows(audit)
    print(json.dumps({"sources":len(amap),"articles":len(items),"articles_file":str(art),"audit_file":str(aud)},ensure_ascii=False))
if __name__=="__main__":
    main()
