"""Fetch, filter, and cache sourced crude-oil fundamentals for live analysis.

The default sources are the U.S. Energy Information Administration's public
RSS feeds. Only headlines, summaries, publication dates, and source links are
passed to the analyst. Cached reports remain available between refreshes and
when a feed is temporarily unreachable.
"""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
import json
import re
import threading
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import requests

from .config import settings
from .storage import db

_refresh_lock=threading.Lock()
_RELEVANT=re.compile(r"\b(crude|petroleum|oil|opec|inventory|inventories|barrel|refin(?:ery|ing)|energy supply|energy demand)\b",re.IGNORECASE)

def _now():
    """Return an aware UTC timestamp for cache age calculations."""
    return datetime.now(timezone.utc)

def _iso(value):
    """Normalize RSS or Atom publication timestamps to ISO 8601 when possible."""
    value=(value or '').strip()
    if not value: return None
    try: parsed=parsedate_to_datetime(value)
    except (TypeError,ValueError,OverflowError):
        try: parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        except ValueError: return None
    if parsed.tzinfo is None: parsed=parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()

def _text(element):
    """Extract readable plain text from a feed field without retaining markup."""
    if element is None: return ''
    raw=' '.join(part.strip() for part in element.itertext() if part and part.strip())
    return re.sub(r'\s+',' ',unescape(re.sub(r'<[^>]*>',' ',raw))).strip()

def _field(element,*names):
    """Find a direct RSS/Atom child using its namespace-independent local name."""
    for child in element:
        local=child.tag.rsplit('}',1)[-1].casefold()
        if local in names: return child
    return None

def _parse_feed(content,feed_url):
    """Parse RSS 2.0 or Atom entries into normalized sourced reports."""
    root=ET.fromstring(content)
    entries=[node for node in root.iter() if node.tag.rsplit('}',1)[-1].casefold() in {'item','entry'}]
    host=urlparse(feed_url).hostname or 'Unknown source'
    reports=[]
    for entry in entries:
        title=_text(_field(entry,'title'))
        summary=_text(_field(entry,'description','summary','content'))
        if not title or not _RELEVANT.search(title+' '+summary): continue
        link_node=_field(entry,'link')
        link=(link_node.attrib.get('href') if link_node is not None else None) or _text(link_node)
        published=_text(_field(entry,'pubdate','published','updated','date'))
        reports.append({'title':title[:240],'summary':summary[:1200],'source':host,'published_at':_iso(published),'url':link[:1000] if link else feed_url})
    return reports

def _feeds():
    """Return the configured public feed URLs in declared priority order."""
    return [value.strip() for value in settings.fundamental_feed_urls.split(',') if value.strip()]

def _row():
    """Read the single persisted feed cache row, if one has been created."""
    with db() as con:
        return con.execute('SELECT fetched_at,last_attempt_at,last_error,feed_signature,sources FROM fundamental_cache WHERE cache_id=1').fetchone()

def get_fundamental_sources(force=False):
    """Return timestamped cached reports, refreshing only when the interval expires.

    A failed refresh records a safe error and preserves the last successful
    reports. A lock prevents the UI poll and scheduler from fetching together.
    """
    urls=_feeds(); signature=','.join(urls); now=_now()
    cached=_row()
    last_attempt=datetime.fromisoformat(cached['last_attempt_at']) if cached and cached['last_attempt_at'] else None
    fetched=datetime.fromisoformat(cached['fetched_at']) if cached and cached['fetched_at'] else None
    refresh_after=timedelta(minutes=max(1,int(settings.fundamental_refresh_minutes)))
    config_changed=bool(cached and cached['feed_signature']!=signature)
    due=force or not cached or config_changed or (now-(last_attempt or fetched or datetime.min.replace(tzinfo=timezone.utc)))>=refresh_after
    if due and urls:
        with _refresh_lock:
            # Another request may have refreshed while this caller waited.
            cached=_row(); now=_now()
            last_attempt=datetime.fromisoformat(cached['last_attempt_at']) if cached and cached['last_attempt_at'] else None
            fetched=datetime.fromisoformat(cached['fetched_at']) if cached and cached['fetched_at'] else None
            config_changed=bool(cached and cached['feed_signature']!=signature)
            due=force or not cached or config_changed or (now-(last_attempt or fetched or datetime.min.replace(tzinfo=timezone.utc)))>=refresh_after
            if due:
                attempt_at=now.isoformat(); collected=[]; successes=0; failures=[]
                for url in urls:
                    try:
                        response=requests.get(url,headers={'Accept':'application/rss+xml, application/atom+xml, application/xml, text/xml','User-Agent':'MCXCrudeAI/1.0 local research feed reader'},timeout=15)
                        response.raise_for_status()
                        collected.extend(_parse_feed(response.content,url)); successes+=1
                    except (requests.RequestException,ET.ParseError,ValueError) as exc:
                        failures.append(type(exc).__name__)
                if successes:
                    unique={item['url']:item for item in collected}
                    if failures and cached:
                        unique.update({item['url']:item for item in json.loads(cached['sources'])})
                    sources=sorted(unique.values(),key=lambda item:item.get('published_at') or '',reverse=True)[:max(1,min(50,int(settings.fundamental_max_items)))]
                    fetched_at=attempt_at; error='; '.join(failures) if failures else None
                else:
                    sources=json.loads(cached['sources']) if cached else []
                    fetched_at=cached['fetched_at'] if cached else None
                    error='All configured fundamental feeds were unavailable or invalid.'
                with db() as con:
                    con.execute('INSERT INTO fundamental_cache(cache_id,fetched_at,last_attempt_at,last_error,feed_signature,sources) VALUES(1,?,?,?,?,?) ON CONFLICT(cache_id) DO UPDATE SET fetched_at=excluded.fetched_at,last_attempt_at=excluded.last_attempt_at,last_error=excluded.last_error,feed_signature=excluded.feed_signature,sources=excluded.sources',(fetched_at,attempt_at,error,signature,json.dumps(sources)))
                return sources
    return json.loads(cached['sources']) if cached else []

def fundamental_status():
    """Return safe feed/cache metadata suitable for a local status card."""
    sources=get_fundamental_sources()
    cached=_row()
    return {'configured_feed_count':len(_feeds()),'refresh_minutes':max(1,int(settings.fundamental_refresh_minutes)),'source_count':len(sources),'last_fetched_at':cached['fetched_at'] if cached else None,'last_attempt_at':cached['last_attempt_at'] if cached else None,'last_error':cached['last_error'] if cached else None,'sources':sources}
