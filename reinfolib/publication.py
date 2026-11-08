"""Read the official homepage's public news feed, without a user API key.

The website currently ships its own public feed subscription token in JavaScript.
Discover it in same-origin static chunks; never execute JS, persist the token, use
it for another endpoint, or substitute the user's XIT001 credential. Format drift
fails closed and does not prevent independent quarter probing.
"""
import json
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser

HOME = 'https://www.reinfolib.mlit.go.jp/'
NEWS = HOME + 'in-api/api-auc/auc/news?language=ja'
CAP = 2_000_000


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        elif tag in ('br', 'p', 'div'):
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        elif tag in ('p', 'div'):
            self.parts.append(' ')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def lookup(*, transport=None, clock=None, end=None, gate=None):
    clock = clock or time.time
    end = min(end if end is not None else float('inf'), clock() + 15)
    if transport is None:
        if __package__ and '.' in __package__:
            from ..client import default_transport
        else:
            from client import default_transport
        transport = default_transport
    result = {'source_url': HOME, 'backing_url': NEWS, 'status': 'unavailable',
              'announcements': [], 'fetched_at': None}
    def get(url, headers=None):
        def send():
            remaining = end - clock()
            if remaining <= 0:
                raise TimeoutError
            status, _, payload = transport(url, headers or {'Accept': 'text/html'}, remaining, CAP)
            if status != 200 or len(payload) > CAP:
                raise ValueError
            return payload.decode('utf-8')
        return gate.run(send, deadline=end) if gate is not None else send()
    try:
        page = get(HOME)
        paths = list(dict.fromkeys(re.findall(
            r'<script[^>]+src=["\'](/_next/static/chunks/[A-Za-z0-9_.-]+\.js)["\']', page)))
        token = None
        for path in paths[:20]:
            source = get(HOME.rstrip('/') + path)
            if 'baseURL:"https://www.reinfolib.mlit.go.jp/in-api"' not in source:
                continue
            match = re.search(r'"Ocp-Apim-Subscription-Key":"([A-Za-z0-9-]{16,128})"', source)
            if match:
                token = match[1]
                break
        if token is None:
            raise ValueError
        body = json.loads(get(NEWS, {'Accept': 'application/json', 'Ocp-Apim-Subscription-Key': token}))
        if not isinstance(body, dict) or not isinstance(body.get('data'), list) or len(body['data']) > 500:
            raise ValueError
        announcements = []
        for item in body['data']:
            if not isinstance(item, dict) or not isinstance(item.get('content'), str) or not isinstance(item.get('publish_from'), str):
                raise ValueError
            parser = _Text()
            parser.feed(item['content'])
            text = ' '.join(''.join(parser.parts).split())
            if any(word in text for word in ('不動産取引価格情報', '成約価格情報')):
                announcements.append({'published_at': item['publish_from'], 'text': text})
        result.update(status='checked', announcements=announcements,
                      fetched_at=datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'))
    except Exception:  # fixed result only: never expose token or upstream exceptions
        pass
    return result
