import urllib.request, re

url = 'http://fastp150.com/get.php?username=dcs17966&password=29665wru&type=m3u_plus&output=ts'
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
with urllib.request.urlopen(req, timeout=10) as r:
    content = r.read().decode('utf-8', 'ignore')

lines = content.replace('\r\n', '\n').replace('\r', '\n').split('\n')
items = []
current = {}

for line in lines:
    line = line.strip()
    if not line or line.startswith('#EXTM3U'):
        continue
    if line.startswith('#EXTINF:'):
        info = line[8:]
        parts = info.split(',', 1)
        attrs = parts[0].strip()
        name = parts[1].strip() if len(parts) > 1 else 'Sem nome'
        current = {'name': name}

        tvg = re.search(r'tvg-name="([^"]*)"', attrs)
        if tvg: current['name'] = tvg.group(1)
        logo = re.search(r'tvg-logo="([^"]*)"', attrs)
        if logo: current['stream_icon'] = logo.group(1)
        grp = re.search(r'group-title="([^"]*)"', attrs)
        if grp: current['category_name'] = grp.group(1)
        else: current['category_name'] = 'Geral'
    elif not line.startswith('#'):
        if line and current.get('name'):
            items.append({**current, 'stream_id': abs(hash(line)) % (10**12), '_url': line})
        current = {}

print(f'Parsed {len(items)} items')
print('Sample items:')
for i in items[:5]:
    print(f"  {i['name']} | {i['category_name']}")