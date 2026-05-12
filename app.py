from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import re, json, time
import requests
from urllib.parse import quote, urlparse, parse_qs

app = Flask(__name__, static_folder='.')
CORS(app)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
}

def scrape_jd(sku_id):
    """Scrape JD product info"""
    try:
        # Try JD item page
        url = f'https://item.jd.com/{sku_id}.html'
        r = requests.get(url, headers=HEADERS, timeout=10)
        
        # Extract title
        title_m = re.search(r'<title>(.*?)</title>', r.text)
        title = title_m.group(1).strip() if title_m else ''
        if '京东' in title and len(title) < 30:
            title = ''  # Generic page
        
        # Try to get price from various patterns
        price = None
        price_patterns = [
            r'"p":"([\d.]+)"',
            r'class="price"[^>]*>.*?[¥￥]\s*([\d,.]+)',
            r'data-price="([\d.]+)"',
            r'price.*?[¥￥]\s*([\d,.]+)',
        ]
        for pat in price_patterns:
            m = re.search(pat, r.text)
            if m:
                try:
                    price = float(m.group(1).replace(',', ''))
                    if price > 0:
                        break
                except:
                    continue
        
        # Try JD price API as fallback
        if not price:
            try:
                pr = requests.get(
                    f'https://p.3.cn/prices/mgets?skuIds=J_{sku_id}',
                    headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://item.jd.com/'},
                    timeout=5
                )
                data = pr.json()
                if data and isinstance(data, list) and data[0].get('p'):
                    price = float(data[0]['p'])
            except:
                pass
        
        return {
            'platform': '京东',
            'sku': sku_id,
            'url': url,
            'title': title,
            'price': price,
            'status': 'ok' if price else 'partial'
        }
    except Exception as e:
        return {'platform': '京东', 'sku': sku_id, 'url': f'https://item.jd.com/{sku_id}.html', 'error': str(e), 'status': 'error'}

def scrape_taobao(item_id):
    """Scrape Taobao/Tmall - limited due to anti-bot"""
    return {
        'platform': '淘宝/天猫',
        'sku': item_id,
        'url': f'https://item.taobao.com/item.htm?id={item_id}',
        'title': '',
        'price': None,
        'status': 'needs_login',
        'note': '淘宝需登录，建议手动输入价格'
    }

def scrape_pdd(goods_id):
    """Scrape Pinduoduo"""
    try:
        url = f'https://mobile.yangkeduo.com/goods.html?goods_id={goods_id}'
        r = requests.get(url, headers={
            **HEADERS,
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15'
        }, timeout=10, allow_redirects=True)
        
        title_m = re.search(r'<title>(.*?)</title>', r.text)
        title = title_m.group(1).strip() if title_m else ''
        
        price = None
        m = re.search(r'"minGroupPrice":\s*"?([\d.]+)"?', r.text)
        if m:
            price = float(m.group(1)) / 100  # PDD prices in cents
        if not price:
            m = re.search(r'"minNormalPrice":\s*"?([\d.]+)"?', r.text)
            if m:
                price = float(m.group(1)) / 100
        
        return {
            'platform': '拼多多',
            'sku': goods_id,
            'url': url,
            'title': title[:80],
            'price': price,
            'status': 'ok' if price else 'partial'
        }
    except Exception as e:
        return {'platform': '拼多多', 'sku': goods_id, 'url': f'https://mobile.yangkeduo.com/goods.html?goods_id={goods_id}', 'error': str(e), 'status': 'error'}

def scrape_url(url):
    """Auto-detect platform and scrape"""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    
    if 'jd.com' in domain:
        # Extract SKU from URL
        m = re.search(r'/(\d{6,})\.html', url)
        if m:
            return scrape_jd(m.group(1))
        # Try query param
        qs = parse_qs(parsed.query)
        if 'sku' in qs:
            return scrape_jd(qs['sku'][0])
    
    elif 'taobao.com' in domain or 'tmall.com' in domain:
        qs = parse_qs(parsed.query)
        item_id = qs.get('id', [''])[0]
        if item_id:
            return scrape_taobao(item_id)
    
    elif 'yangkeduo.com' in domain or 'pinduoduo.com' in domain:
        qs = parse_qs(parsed.query)
        goods_id = qs.get('goods_id', [''])[0]
        if goods_id:
            return scrape_pdd(goods_id)
    
    elif 'suning.com' in domain:
        m = re.search(r'/(\d{8,})\.html', url)
        if m:
            return {'platform': '苏宁', 'sku': m.group(1), 'url': url, 'status': 'partial', 'note': '苏宁暂不支持自动抓取'}
    
    return {'platform': '未知', 'url': url, 'status': 'unknown', 'note': '未识别的平台'}

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/scrape', methods=['POST'])
def api_scrape():
    data = request.json
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': '请提供商品链接'}), 400
    result = scrape_url(url)
    return jsonify(result)

@app.route('/api/search', methods=['POST'])
def api_search():
    """Search for a product keyword and try to find prices"""
    data = request.json
    keyword = data.get('keyword', '').strip()
    if not keyword:
        return jsonify({'error': '请提供搜索关键词'}), 400
    
    results = []
    
    # Try JD search
    try:
        r = requests.get(
            f'https://search.jd.com/Search?keyword={quote(keyword)}&enc=utf-8',
            headers=HEADERS, timeout=10
        )
        # Extract first few products
        items = re.findall(r'data-sku="(\d+)"', r.text)
        titles = re.findall(r'<div class="p-name.*?<em>(.*?)</em>', r.text, re.DOTALL)
        prices = re.findall(r'<div class="p-price".*?<i>([\d.]+)</i>', r.text, re.DOTALL)
        
        for i in range(min(5, len(items))):
            item = {
                'platform': '京东',
                'sku': items[i],
                'url': f'https://item.jd.com/{items[i]}.html',
                'title': re.sub(r'<[^>]+>', '', titles[i]).strip() if i < len(titles) else '',
                'price': float(prices[i]) if i < len(prices) else None,
                'status': 'ok' if i < len(prices) else 'partial'
            }
            results.append(item)
    except Exception as e:
        results.append({'platform': '京东', 'error': str(e), 'status': 'error'})
    
    return jsonify({'keyword': keyword, 'results': results})

@app.route('/api/deals')
def api_deals():
    """Get current 618 deals"""
    deals = [
        {'platform': '京东', 'title': 'iPhone 16 Pro 256GB', 'original': 8999, 'sale': 7699, 'url': 'https://item.jd.com/100082928498.html', 'discount': '直降1300'},
        {'platform': '京东', 'title': 'MacBook Air M3', 'original': 8999, 'sale': 7499, 'url': 'https://item.jd.com/100055662630.html', 'discount': '直降1500'},
        {'platform': '京东', 'title': '戴森 V15 Detect', 'original': 5490, 'sale': 3990, 'url': 'https://item.jd.com/100011501490.html', 'discount': '直降1500'},
        {'platform': '京东', 'title': '茅台飞天 53度 500ml', 'original': 1499, 'sale': 1499, 'url': 'https://item.jd.com/100000425734.html', 'discount': '限量抢购'},
        {'platform': '京东', 'title': 'AirPods Pro 2', 'original': 1899, 'sale': 1599, 'url': 'https://item.jd.com/100038004794.html', 'discount': '直降300'},
        {'platform': '京东', 'title': '小米14 Ultra 16+512GB', 'original': 6499, 'sale': 5999, 'url': 'https://item.jd.com/100083235538.html', 'discount': '直降500'},
    ]
    return jsonify(deals)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
