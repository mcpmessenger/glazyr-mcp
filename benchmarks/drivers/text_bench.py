#!/usr/bin/env python3
"""Text-extraction floor benchmark: fetch + trafilatura, no browser.
Same 3 flows. Tokens = extracted text (+ raw POST response for B).
Flow B is only possible because httpbin's form is static HTML with a
plain POST - noted as a method limit, not a capability."""
import re, time, urllib.request, urllib.parse
import trafilatura, tiktoken

enc = tiktoken.get_encoding('cl100k_base')
UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36')

def fetch(url, data=None):
    req = urllib.request.Request(url, data=data,
                                 headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode('utf-8', errors='replace')

def extract(html):
    return trafilatura.extract(html) or ''

results = {}

# ---- Flow A ----
t0 = time.time(); toks = 0
html = fetch('https://en.wikipedia.org/wiki/Chromium_(web_browser)')
text = extract(html); toks += len(enc.encode(text))
ok_license = 'LGPL' in text
m = re.search(r'href="([^"]*Blink_\(browser_engine\)[^"]*)"', html)
ok_a = False
if m:
    blink_url = urllib.parse.urljoin('https://en.wikipedia.org', m.group(1))
    text2 = extract(fetch(blink_url)); toks += len(enc.encode(text2))
    ok_a = ok_license and 'browser engine' in text2.lower()
results['A'] = (toks, round(time.time() - t0, 1), ok_a)

# ---- Flow B ----
t0 = time.time(); toks = 0
html = fetch('https://httpbin.org/forms/post')
text = extract(html); toks += len(enc.encode(text))
fields = {'custname': 'Dogfood Tester', 'custemail': 'tester@example.com',
          'size': 'medium', 'topping': 'bacon'}
resp = fetch('https://httpbin.org/post',
             data=urllib.parse.urlencode(fields).encode())
toks += len(enc.encode(resp))
ok_b = 'Dogfood Tester' in resp and 'bacon' in resp and 'medium' in resp
results['B'] = (toks, round(time.time() - t0, 1), ok_b)

# ---- Flow C ----
t0 = time.time(); toks = 0
text = extract(fetch('https://www.python.org/downloads/'))
toks += len(enc.encode(text))
ok_c = '3.15.2' in text
results['C'] = (toks, round(time.time() - t0, 1), ok_c)

for k, (tk, secs, ok) in results.items():
    print(f'TEXTBENCH {k}: {tk} tokens, {secs}s, ok={ok}')
print('TEXT_BENCH_DONE')
