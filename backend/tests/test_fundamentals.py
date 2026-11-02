"""Tests for the timestamped EIA RSS reader and persistent hourly cache."""
from mcx_ai import fundamentals
from mcx_ai.config import settings
from mcx_ai.storage import init_db


def test_fundamental_cache_refreshes_once_and_reuses_last_success(monkeypatch,tmp_path):
    """Refresh at first use, reuse reports between refreshes, and preserve on error."""
    monkeypatch.setattr(settings,'db_path',str(tmp_path/'fundamentals.db'))
    monkeypatch.setattr(settings,'fundamental_feed_urls','https://example.gov/oil.xml')
    monkeypatch.setattr(settings,'fundamental_refresh_minutes',60)
    monkeypatch.setattr(settings,'fundamental_max_items',12)
    init_db()
    xml=b'''<?xml version="1.0"?><rss version="2.0"><channel><item>
      <title>Crude oil inventory report</title><link>https://example.gov/report/1</link>
      <pubDate>Mon, 02 Nov 2026 04:00:00 GMT</pubDate>
      <description>Official update on petroleum supply and crude stocks.</description>
      </item></channel></rss>'''
    calls={'count':0}
    class Response:
        """Minimal successful RSS response used to avoid a network dependency."""
        content=xml
        def raise_for_status(self): """Match requests.Response status validation."""
    def get(*args,**kwargs):
        """Count fetch attempts and then simulate an unavailable feed."""
        calls['count']+=1
        if calls['count']>1: raise fundamentals.requests.ConnectionError('offline')
        return Response()
    monkeypatch.setattr(fundamentals.requests,'get',get)
    first=fundamentals.get_fundamental_sources()
    assert len(first)==1 and first[0]['source']=='example.gov'
    assert first[0]['published_at']=='2026-11-02T04:00:00+00:00'
    assert fundamentals.get_fundamental_sources()==first
    assert calls['count']==1
    assert fundamentals.get_fundamental_sources(force=True)==first
    status=fundamentals.fundamental_status()
    assert status['source_count']==1 and status['refresh_minutes']==60
    assert status['last_error'] is not None and calls['count']==2
