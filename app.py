"""
比价助手 - 多平台价格自动搜索工具
数据存储在前端 localStorage，后端只负责搜索
"""
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import re
import os
from urllib.parse import quote_plus
import concurrent.futures

app = Flask(__name__, static_folder='.')
CORS(app)

# ===== Price extraction =====
def extract_price_from_text(text):
    if not text:
        return None
    patterns = [
        (r'[¥￥]\s*([\d,]+\.?\d*)', 'sym'),
        (r'到手([\d,]+\.?\d*)\s*元', 'tohand'),
        (r'售价([\d,]+\.?\d*)\s*元', 'sale'),
        (r'优惠价([\d,]+\.?\d*)\s*元', 'discount'),
        (r'([\d,]+\.?\d*)\s*元起', 'from'),
        (r'价格[：:]\s*([\d,]+\.?\d*)', 'label'),
        (r'([\d,]+\.?\d*)\s*元', 'yuan'),
    ]
    for pat, tag in patterns:
        m = re.search(pat, text)
        if m:
            try:
                val = float(m.group(1).replace(',', ''))
                if 500 < val < 50000:
                    return val
                elif 10 < val < 50000:
                    return val
            except:
                continue
    return None

def is_discount_amount(text, price):
    discount_patterns = [
        r'直降\s*[¥￥]?\s*' + re.escape(str(int(price))),
        r'降\s*[¥￥]?\s*' + re.escape(str(int(price))) + r'\s*元',
        r'省\s*[¥￥]?\s*' + re.escape(str(int(price))),
        r'减\s*[¥￥]?\s*' + re.escape(str(int(price))),
        r'优惠\s*[¥￥]?\s*' + re.escape(str(int(price))),
    ]
    for pat in discount_patterns:
        if re.search(pat, text):
            return True
    return False

def search_platform_prices(product_name):
    results = []
    try:
        from ddgs import DDGS
    except ImportError:
        return []

    queries = [
        ('京东', f'{product_name} 京东 价格'),
        ('天猫', f'{product_name} 天猫 价格'),
        ('淘宝', f'{product_name} 淘宝 价格'),
        ('拼多多', f'{product_name} 拼多多 百亿补贴'),
        ('苏宁', f'{product_name} 苏宁 价格'),
    ]
    general_q = f'{product_name} 价格 比价 最低价'

    def search_one(platform_name, query):
        try:
            ddgs = DDGS()
            raw = ddgs.text(query, max_results=5)
            for r in raw:
                title = r.get('title', '')
                body = r.get('body', '')
                href = r.get('href', '')
                combined = title + ' ' + body
                price = extract_price_from_text(combined)
                if price and is_discount_amount(combined, price):
                    price = None
                if not price:
                    for m in re.finditer(r'[¥￥]\s*([\d,]+)', combined):
                        try:
                            v = float(m.group(1).replace(',', ''))
                            if 500 < v < 50000 and not is_discount_amount(combined, v):
                                price = v
                                break
                        except:
                            pass
                if price:
                    return {'platform': platform_name, 'price': price, 'url': href, 'title': title[:80], 'source': 'search'}
        except Exception as e:
            print(f'Search error [{platform_name}]: {e}')
        return None

    def search_general():
        try:
            ddgs = DDGS()
            raw = ddgs.text(general_q, max_results=10)
            found = {}
            platform_keywords = {
                '京东': ['京东', 'jd', 'JD'],
                '天猫': ['天猫', 'tmall', 'Tmall'],
                '拼多多': ['拼多多', 'pdd', '百亿补贴', 'PDD'],
                '苏宁': ['苏宁', 'suning'],
                '抖音': ['抖音', 'douyin', '直播'],
            }
            for r in raw:
                title = r.get('title', '')
                body = r.get('body', '')
                href = r.get('href', '')
                combined = title + ' ' + body
                all_prices = re.findall(r'[¥￥]\s*([\d,]+)', combined)
                yuan_prices = re.findall(r'(\d{3,5})\s*元', combined)
                price_candidates = []
                for p in all_prices + yuan_prices:
                    try:
                        v = float(p.replace(',', ''))
                        if 100 < v < 50000:
                            price_candidates.append(v)
                    except:
                        pass
                if not price_candidates:
                    continue
                for plat, keywords in platform_keywords.items():
                    if plat in found:
                        continue
                    for kw in keywords:
                        if kw in combined:
                            idx = combined.index(kw)
                            context = combined[max(0,idx-30):idx+50]
                            ctx_price = extract_price_from_text(context)
                            if ctx_price:
                                found[plat] = {'platform': plat, 'price': ctx_price, 'url': href, 'title': title[:80], 'source': 'comparison'}
                                break
                            elif price_candidates:
                                found[plat] = {'platform': plat, 'price': min(price_candidates), 'url': href, 'title': title[:80], 'source': 'comparison'}
                                break
            return list(found.values())
        except Exception as e:
            print(f'General search error: {e}')
            return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(search_one, p, q) for p, q in queries]
        futures.append(executor.submit(search_general))
        for f in concurrent.futures.as_completed(futures):
            res = f.result()
            if res:
                if isinstance(res, list):
                    results.extend(res)
                else:
                    results.append(res)

    best = {}
    for r in results:
        plat = r['platform']
        if plat not in best or r['price'] < best[plat]['price']:
            best[plat] = r
    results = list(best.values())
    results.sort(key=lambda x: x['price'])
    for i, r in enumerate(results):
        r['is_cheapest'] = (i == 0)
    return results

# ===== API Routes =====
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/search-prices', methods=['POST'])
def search_prices():
    data = request.json
    product_name = data.get('name', '').strip()
    if not product_name:
        return jsonify({'error': '请输入商品名称'}), 400
    try:
        results = search_platform_prices(product_name)
        if not results:
            return jsonify({'product_name': product_name, 'results': [], 'message': '未找到价格信息，请手动输入'})
        prices = [r['price'] for r in results]
        savings = max(prices) - min(prices) if len(prices) >= 2 else 0
        return jsonify({'product_name': product_name, 'results': results, 'savings': savings, 'cheapest': results[0] if results else None})
    except Exception as e:
        return jsonify({'error': f'搜索失败: {str(e)}'}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
