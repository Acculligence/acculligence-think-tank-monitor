#!/usr/bin/env python3
"""Isolated per-source worker for Acculligence Think Tank Monitor v4."""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlencode

import dateparser
import feedparser
import requests
import trafilatura
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

UA = 'AcculligenceThinkTankCollector/4.0 (+https://acculligence.com)'
ARTICLE_HINTS = ('/publication','/research','/analysis','/commentary','/article','/report','/insight','/paper','/study')
MAX_SITEMAP_CHILDREN = 35
MAX_ARCHIVE_PAGES = 3


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8'})
    return s


def fetch(s: requests.Session, url: str, timeout: int, accept: str | None = None) -> requests.Response:
    headers = {'Accept': accept} if accept else {}
    r = s.get(url, timeout=(min(5, timeout), timeout), allow_redirects=True, headers=headers)
    r.raise_for_status(); return r


def same_domain(url: str, domain: str) -> bool:
    host = urlparse(url).netloc.lower().replace('www.','')
    domain = domain.lower().replace('www.','')
    return host == domain or host.endswith('.' + domain)


def normalize_url(url: str) -> str:
    p = urlparse(url)
    return f'{p.scheme.lower()}://{p.netloc.lower().replace("www.","")}{p.path.rstrip("/")}'


def parse_date(value: str | None):
    if not value: return None
    try: d = dtparser.parse(value)
    except Exception: d = dateparser.parse(value)
    if not d: return None
    if not d.tzinfo: d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def in_range(value, start, end) -> bool:
    d = parse_date(value)
    return bool(d and start <= d <= end)


def feed_candidates(s, url, start, end, timeout):
    r = fetch(s, url, timeout, 'application/rss+xml,application/atom+xml,application/xml,text/xml,*/*')
    parsed = feedparser.parse(r.content)
    out = []
    for entry in getattr(parsed, 'entries', []):
        link = entry.get('link')
        date_value = entry.get('published') or entry.get('updated') or entry.get('created') or ''
        if link and in_range(date_value, start, end):
            out.append({'url': link, 'title': entry.get('title',''), 'date': date_value, 'route': url})
    return out


def sitemap_candidates(s, url, domain, start, end, timeout, depth=0):
    if depth > 2: return []
    r = fetch(s, url, timeout, 'application/xml,text/xml,*/*')
    soup = BeautifulSoup(r.content, 'xml')
    indexes = soup.find_all('sitemap')
    if indexes:
        children = []
        for node in indexes:
            loc = node.find('loc'); lm = node.find('lastmod')
            if not loc: continue
            child = loc.get_text(strip=True)
            marker = lm.get_text(strip=True) if lm else ''
            likely = any(x in child.lower() for x in ('post','news','article','publication','research','report','insight'))
            if in_range(marker, start, end) or (not marker and likely): children.append(child)
        out = []
        for child in children[:MAX_SITEMAP_CHILDREN]:
            try: out.extend(sitemap_candidates(s, child, domain, start, end, timeout, depth+1))
            except Exception: pass
        return out

    dated, undated = [], []
    for node in soup.find_all('url'):
        loc = node.find('loc')
        if not loc: continue
        link = loc.get_text(strip=True)
        if not same_domain(link, domain): continue
        if not any(h in urlparse(link).path.lower() for h in ARTICLE_HINTS): continue
        lm = node.find('lastmod'); date_value = lm.get_text(strip=True) if lm else ''
        row = {'url': link, 'title': '', 'date': date_value, 'route': url}
        if date_value and in_range(date_value, start, end): dated.append(row)
        elif not date_value: undated.append(row)
    return dated + undated[:80]


def wp_candidates(s, url, domain, start, end, timeout):
    base = url.split('?')[0]; out = []
    after = start.isoformat().replace('+00:00','Z'); before = end.isoformat().replace('+00:00','Z')
    for page in range(1, 6):
        q = {'per_page':100,'page':page,'after':after,'before':before,'_fields':'link,date,title'}
        try: data = fetch(s, base+'?'+urlencode(q), timeout, 'application/json').json()
        except Exception: break
        if not isinstance(data,list) or not data: break
        for item in data:
            link = item.get('link','')
            if not link or not same_domain(link,domain): continue
            t = item.get('title',{}); title = t.get('rendered','') if isinstance(t,dict) else str(t)
            out.append({'url':link,'title':BeautifulSoup(title,'lxml').get_text(' ',strip=True),'date':item.get('date',''),'route':url})
    return out


def archive_candidates(s, url, domain, timeout):
    out=[]; current=url
    for _ in range(MAX_ARCHIVE_PAGES):
        r=fetch(s,current,timeout); soup=BeautifulSoup(r.text,'lxml')
        for a in soup.find_all('a',href=True):
            link=urljoin(r.url,a['href'])
            if same_domain(link,domain) and any(h in urlparse(link).path.lower() for h in ARTICLE_HINTS):
                out.append({'url':link,'title':a.get_text(' ',strip=True),'date':'','route':url})
        nxt=soup.find('a',rel=lambda v:v and 'next' in v)
        if not nxt or not nxt.get('href'): break
        current=urljoin(r.url,nxt['href'])
    return out[:100]


def candidate_routes(source, start, end, timeout, max_candidates):
    s=session(); candidates=[]; used=[]; errors=[]
    for route in source.get('routes',[]):
        typ,url=route['type'],route['url']
        try:
            if typ=='feed': rows=feed_candidates(s,url,start,end,timeout)
            elif typ=='sitemap': rows=sitemap_candidates(s,url,source['domain'],start,end,timeout)
            elif typ=='wp_api': rows=wp_candidates(s,url,source['domain'],start,end,timeout)
            else: rows=archive_candidates(s,url,source['domain'],timeout)
            if rows: used.append(url); candidates.extend(rows)
        except Exception as exc:
            errors.append(f'{typ} {url}: {type(exc).__name__}')
        if len(candidates) >= max_candidates: break
    unique=[]; seen=set()
    for row in candidates:
        try: key=normalize_url(row['url'])
        except Exception: continue
        if key not in seen:
            seen.add(key); unique.append(row)
        if len(unique)>=max_candidates: break
    return unique, used, errors


def extract_article(candidate, source, keywords, start, end, timeout):
    s=session()
    try: r=fetch(s,candidate['url'],timeout)
    except Exception: return None
    body=trafilatura.extract(r.text,include_comments=False,include_tables=True,include_links=False,favor_recall=True,deduplicate=True) or ''
    if not body.strip(): return None
    soup=BeautifulSoup(r.text,'lxml')
    title=candidate.get('title','').strip()
    og=soup.find('meta',property='og:title')
    if og and og.get('content'): title=og['content'].strip()
    elif not title and soup.title: title=soup.title.get_text(' ',strip=True)
    date_value=''
    for attrs in ({'property':'article:published_time'},{'name':'date'},{'name':'pubdate'},{'name':'publication_date'},{'itemprop':'datePublished'}):
        tag=soup.find('meta',attrs=attrs)
        if tag and tag.get('content'): date_value=tag['content']; break
    if not date_value:
        tag=soup.find('time')
        if tag: date_value=tag.get('datetime') or tag.get_text(' ',strip=True)
    d=parse_date(date_value or candidate.get('date',''))
    if not d or not(start<=d<=end): return None
    hay=title+'\n'+body
    hits=[k for k in keywords if re.search(re.escape(k),hay,re.I if k.isascii() else 0)]
    if not hits: return None
    return {
        'source':source['source_name'],'domain':source['domain'],'title':title,
        'published_at':d.isoformat(),'url':r.url,'matched_keywords':' | '.join(hits),
        'body':body.strip(),'collection_route':candidate.get('route',''),
        'review_status':'pending','saudi_summary_ar':'','topic':'','sentiment':'','author':''
    }


def atomic_write(path: Path, payload: dict):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
    os.replace(tmp,path)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--source',required=True); ap.add_argument('--keywords',required=True)
    ap.add_argument('--start',required=True); ap.add_argument('--end',required=True)
    ap.add_argument('--result',required=True); ap.add_argument('--request-timeout',type=int,default=12)
    ap.add_argument('--max-candidates',type=int,default=240); ap.add_argument('--article-workers',type=int,default=6)
    args=ap.parse_args(); started=time.monotonic()
    source=json.loads(Path(args.source).read_text(encoding='utf-8'))
    keywords=json.loads(Path(args.keywords).read_text(encoding='utf-8'))['keywords']
    start=dtparser.parse(args.start+'T00:00:00Z'); end=dtparser.parse(args.end+'T23:59:59Z')
    candidates,used,errors=candidate_routes(source,start,end,args.request_timeout,args.max_candidates)
    articles=[]
    with ThreadPoolExecutor(max_workers=max(1,args.article_workers)) as pool:
        futures=[pool.submit(extract_article,c,source,keywords,start,end,args.request_timeout) for c in candidates]
        for future in as_completed(futures):
            try:
                article=future.result()
                if article: articles.append(article)
            except Exception: pass
    payload={'articles':articles,'audit':{
        'source':source['source_name'],'domain':source['domain'],
        'status':'complete' if used else ('route_error' if errors else 'no_candidates'),
        'routes_used':' | '.join(used),'candidate_urls':len(candidates),
        'dated_items':len(articles),'matched_items':len(articles),
        'duration_seconds':round(time.monotonic()-started,2),
        'notes':'; '.join(errors)[:1500]
    }}
    atomic_write(Path(args.result),payload)

if __name__=='__main__': main()
