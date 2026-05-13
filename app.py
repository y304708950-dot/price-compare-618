"""
比价助手 - 多平台价格对比工具
支持：京东、淘宝/天猫、拼多多、苏宁、抖音
功能：产品管理、多平台比价、价格历史、数据导出、自动抓取
"""
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import sqlite3
import json
import re
import os
import csv
import io
from datetime import datetime
from urllib.parse import urlparse, parse_qs, quote_plus
import concurrent.futures

app = Flask(__name__, static_folder='.')
CORS(app)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prices.db')

# ===== Database =====
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT DEFAULT '',
            image_url TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT DEFAULT (datetime('now', 'localtime'))
        );
        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            price REAL NOT NULL,
            url TEXT DEFAULT '',
            note TEXT DEFAULT '',
            recorded_at TEXT DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_prices_product ON prices(product_id);
        CREATE INDEX IF NOT EXISTS idx_prices_recorded ON prices(recorded_at);
        CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);
    ''')
    conn.commit()
    conn.close()

init_db()

# ===== URL Detection =====
PLATFORMS = {
    'jd.com': '京东',
    'taobao.com': '淘宝',
    'tmall.com': '天猫',
    'yangkeduo.com': '拼多多',
    'pinduoduo.com': '拼多多',
    'suning.com': '苏宁',
    'douyin.com': '抖音',
    'kuaishou.com': '快手',
}

def detect_platform(url):
    """Detect e-commerce platform from URL"""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower().replace('www.', '')
        for key, name in PLATFORMS.items():
            if key in domain:
                return name
    except:
        pass
    return '其他'

def extract_product_info(url):
    """Extract product name hint from URL"""
    info = {'platform': detect_platform(url), 'sku': '', 'name_hint': ''}
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        if 'jd.com' in domain:
            m = re.search(r'/(\d{6,})\.html', url)
            if m:
                info['sku'] = m.group(1)
                info['name_hint'] = f'京东商品 {m.group(1)}'

        elif 'taobao.com' in domain or 'tmall.com' in domain:
            qs = parse_qs(parsed.query)
            item_id = qs.get('id', [''])[0]
            if item_id:
                info['sku'] = item_id
                info['name_hint'] = f'淘宝/天猫商品 {item_id}'

        elif 'yangkeduo.com' in domain or 'pinduoduo.com' in domain:
            qs = parse_qs(parsed.query)
            goods_id = qs.get('goods_id', [''])[0]
            if goods_id:
                info['sku'] = goods_id
                info['name_hint'] = f'拼多多商品 {goods_id}'

        elif 'suning.com' in domain:
            m = re.search(r'/(\d{8,})\.html', url)
            if m:
                info['sku'] = m.group(1)
                info['name_hint'] = f'苏宁商品 {m.group(1)}'

    except:
        pass
    return info

# ===== Price Search =====
def extract_price_from_text(text):
    """Extract price from text like ¥199, 到手5499元, 售价6499元"""
    if not text:
        return None
    # Ordered by specificity - most reliable first
    patterns = [
        (r'[¥￥]\s*([\d,]+\.?\d*)', 'sym'),          # ¥199, ￥1,999.9
        (r'到手([\d,]+\.?\d*)\s*元', 'tohand'),       # 到手5499元 (actual price)
        (r'售价([\d,]+\.?\d*)\s*元', 'sale'),          # 售价6499元
        (r'优惠价([\d,]+\.?\d*)\s*元', 'discount'),    # 优惠价6899元
        (r'([\d,]+\.?\d*)\s*元起', 'from'),            # 199元起
        (r'价格[：:]\s*([\d,]+\.?\d*)', 'label'),      # 价格：199
        (r'([\d,]+\.?\d*)\s*元', 'yuan'),              # 199元 (least specific)
    ]
    for pat, tag in patterns:
        m = re.search(pat, text)
        if m:
            try:
                val = float(m.group(1).replace(',', ''))
                if 500 < val < 50000:  # phone/elec price range
                    return val
                elif 10 < val < 50000:  # wider range for other products
                    return val
            except:
                continue
    return None

def is_discount_amount(text, price):
    """Check if the extracted value is likely a discount amount, not actual price"""
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
    """Search prices across platforms using ddgs (DuckDuckGo)"""
    results = []
    try:
        from ddgs import DDGS
    except ImportError:
        return []

    # Strategy 1: Per-platform keyword searches (more reliable than site: filter)
    queries = [
        ('京东', f'{product_name} 京东 价格'),
        ('天猫', f'{product_name} 天猫 价格'),
        ('淘宝', f'{product_name} 淘宝 价格'),
        ('拼多多', f'{product_name} 拼多多 百亿补贴'),
        ('苏宁', f'{product_name} 苏宁 价格'),
    ]

    # Also try a general comparison search
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
                # Skip if extracted value is a discount amount
                if price and is_discount_amount(combined, price):
                    price = None
                # Try to find actual price in ¥ symbol context
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
                    return {
                        'platform': platform_name,
                        'price': price,
                        'url': href,
                        'title': title[:80],
                        'source': 'search'
                    }
        except Exception as e:
            print(f'Search error [{platform_name}]: {e}')
        return None

    def search_general():
        """Extract prices from comparison/review articles"""
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

                # Find all prices in this result
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

                # Try to associate prices with platforms
                for plat, keywords in platform_keywords.items():
                    if plat in found:
                        continue
                    for kw in keywords:
                        if kw in combined:
                            # Find price near this keyword
                            idx = combined.index(kw)
                            context = combined[max(0,idx-30):idx+50]
                            ctx_price = extract_price_from_text(context)
                            if ctx_price:
                                found[plat] = {
                                    'platform': plat,
                                    'price': ctx_price,
                                    'url': href,
                                    'title': title[:80],
                                    'source': 'comparison'
                                }
                                break
                            elif price_candidates:
                                # Use smallest price as best guess
                                found[plat] = {
                                    'platform': plat,
                                    'price': min(price_candidates),
                                    'url': href,
                                    'title': title[:80],
                                    'source': 'comparison'
                                }
                                break
            return list(found.values())
        except Exception as e:
            print(f'General search error: {e}')
            return []

    # Run platform searches in parallel
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

    # Deduplicate by platform (keep lowest price)
    best = {}
    for r in results:
        plat = r['platform']
        if plat not in best or r['price'] < best[plat]['price']:
            best[plat] = r
    results = list(best.values())

    # Sort by price
    results.sort(key=lambda x: x['price'])
    for i, r in enumerate(results):
        r['is_cheapest'] = (i == 0)
    return results

# ===== API Routes =====

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/parse-url', methods=['POST'])
def parse_url():
    """Parse a product URL to extract platform and info"""
    data = request.json
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': '请提供链接'}), 400
    info = extract_product_info(url)
    return jsonify(info)

@app.route('/api/search-prices', methods=['POST'])
def search_prices():
    """Auto search prices for a product across platforms"""
    data = request.json
    product_name = data.get('name', '').strip()
    
    if not product_name:
        return jsonify({'error': '请输入商品名称'}), 400
    
    try:
        results = search_platform_prices(product_name)
        
        if not results:
            return jsonify({
                'product_name': product_name,
                'results': [],
                'message': '未找到价格信息，请尝试手动输入'
            })
        
        # Calculate savings
        prices = [r['price'] for r in results]
        savings = max(prices) - min(prices) if len(prices) >= 2 else 0
        
        return jsonify({
            'product_name': product_name,
            'results': results,
            'savings': savings,
            'cheapest': results[0] if results else None
        })
    except Exception as e:
        return jsonify({'error': f'搜索失败: {str(e)}'}), 500

@app.route('/api/search-and-save', methods=['POST'])
def search_and_save():
    """Search prices and save to database"""
    data = request.json
    product_name = data.get('name', '').strip()
    
    if not product_name:
        return jsonify({'error': '请输入商品名称'}), 400
    
    try:
        search_results = search_platform_prices(product_name)
        
        if not search_results:
            return jsonify({'error': '未找到价格信息'}), 404
        
        conn = get_db()
        
        # Check if product exists
        existing = conn.execute(
            'SELECT id FROM products WHERE name = ? ORDER BY updated_at DESC LIMIT 1',
            (product_name,)
        ).fetchone()
        
        if existing:
            product_id = existing['id']
        else:
            cur = conn.execute(
                'INSERT INTO products (name, category) VALUES (?, ?)',
                (product_name, data.get('category', ''))
            )
            product_id = cur.lastrowid
        
        # Save prices
        for result in search_results:
            conn.execute(
                'INSERT INTO prices (product_id, platform, price, url, note) VALUES (?, ?, ?, ?, ?)',
                (product_id, result['platform'], result['price'], 
                 result.get('url', ''), f"自动抓取 - {result.get('title', '')}")
            )
        
        conn.execute(
            'UPDATE products SET updated_at = datetime("now","localtime") WHERE id = ?',
            (product_id,)
        )
        conn.commit()
        
        # Get latest prices for response
        latest_prices = conn.execute('''
            SELECT platform, price, url FROM prices
            WHERE product_id = ? AND id IN (
                SELECT MAX(id) FROM prices WHERE product_id = ? GROUP BY platform
            )
            ORDER BY price ASC
        ''', (product_id, product_id)).fetchall()
        
        conn.close()
        
        return jsonify({
            'product_id': product_id,
            'name': product_name,
            'prices': [{
                'platform': pr['platform'],
                'price': pr['price'],
                'url': pr['url'],
                'is_cheapest': i == 0
            } for i, pr in enumerate(latest_prices)],
            'savings': latest_prices[-1]['price'] - latest_prices[0]['price'] if len(latest_prices) >= 2 else 0,
            'message': f'已找到 {len(search_results)} 个平台的价格'
        }), 201
        
    except Exception as e:
        return jsonify({'error': f'搜索失败: {str(e)}'}), 500

# --- Products CRUD ---
@app.route('/api/products', methods=['GET'])
def list_products():
    """List all products with latest prices"""
    conn = get_db()
    search = request.args.get('search', '').strip()
    category = request.args.get('category', '').strip()

    query = '''
        SELECT p.*,
            (SELECT MIN(pr.price) FROM prices pr WHERE pr.product_id = p.id AND pr.recorded_at = (
                SELECT MAX(pr2.recorded_at) FROM prices pr2 WHERE pr2.product_id = p.id AND pr2.platform = pr.platform
            )) as min_price,
            (SELECT MAX(pr.price) FROM prices pr WHERE pr.product_id = p.id AND pr.recorded_at = (
                SELECT MAX(pr2.recorded_at) FROM prices pr2 WHERE pr2.product_id = p.id AND pr2.platform = pr.platform
            )) as max_price,
            (SELECT COUNT(DISTINCT pr.platform) FROM prices pr WHERE pr.product_id = p.id) as platform_count
        FROM products p
    '''
    params = []
    conditions = []

    if search:
        conditions.append('p.name LIKE ?')
        params.append(f'%{search}%')
    if category:
        conditions.append('p.category = ?')
        params.append(category)

    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)

    query += ' ORDER BY p.updated_at DESC'

    rows = conn.execute(query, params).fetchall()
    products = []
    for row in rows:
        price_rows = conn.execute('''
            SELECT platform, price, url, note, recorded_at FROM prices
            WHERE product_id = ? AND id IN (
                SELECT MAX(id) FROM prices WHERE product_id = ? GROUP BY platform
            )
            ORDER BY price ASC
        ''', (row['id'], row['id'])).fetchall()

        products.append({
            'id': row['id'],
            'name': row['name'],
            'category': row['category'],
            'image_url': row['image_url'],
            'created_at': row['created_at'],
            'updated_at': row['updated_at'],
            'min_price': row['min_price'],
            'max_price': row['max_price'],
            'platform_count': row['platform_count'],
            'prices': [{
                'platform': pr['platform'],
                'price': pr['price'],
                'url': pr['url'],
                'note': pr['note'],
                'recorded_at': pr['recorded_at']
            } for pr in price_rows]
        })

    conn.close()
    return jsonify(products)

@app.route('/api/products', methods=['POST'])
def create_product():
    """Create a new product"""
    data = request.json
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': '商品名称不能为空'}), 400

    conn = get_db()
    cur = conn.execute(
        'INSERT INTO products (name, category, image_url) VALUES (?, ?, ?)',
        (name, data.get('category', ''), data.get('image_url', ''))
    )
    product_id = cur.lastrowid
    conn.commit()

    prices = data.get('prices', [])
    for pr in prices:
        if pr.get('price') and pr.get('platform'):
            conn.execute(
                'INSERT INTO prices (product_id, platform, price, url, note) VALUES (?, ?, ?, ?, ?)',
                (product_id, pr['platform'], float(pr['price']), pr.get('url', ''), pr.get('note', ''))
            )
    conn.commit()
    conn.close()

    return jsonify({'id': product_id, 'message': '创建成功'}), 201

@app.route('/api/products/<int:product_id>', methods=['GET'])
def get_product(product_id):
    """Get product details with all price history"""
    conn = get_db()
    product = conn.execute('SELECT * FROM products WHERE id = ?', (product_id,)).fetchone()
    if not product:
        conn.close()
        return jsonify({'error': '商品不存在'}), 404

    prices = conn.execute(
        'SELECT * FROM prices WHERE product_id = ? ORDER BY platform, recorded_at DESC',
        (product_id,)
    ).fetchall()

    conn.close()
    return jsonify({
        'id': product['id'],
        'name': product['name'],
        'category': product['category'],
        'image_url': product['image_url'],
        'created_at': product['created_at'],
        'updated_at': product['updated_at'],
        'prices': [{
            'id': pr['id'],
            'platform': pr['platform'],
            'price': pr['price'],
            'url': pr['url'],
            'note': pr['note'],
            'recorded_at': pr['recorded_at']
        } for pr in prices]
    })

@app.route('/api/products/<int:product_id>', methods=['PUT'])
def update_product(product_id):
    """Update product info"""
    data = request.json
    conn = get_db()
    conn.execute(
        'UPDATE products SET name = ?, category = ?, image_url = ?, updated_at = datetime("now","localtime") WHERE id = ?',
        (data.get('name', ''), data.get('category', ''), data.get('image_url', ''), product_id)
    )
    conn.commit()
    conn.close()
    return jsonify({'message': '更新成功'})

@app.route('/api/products/<int:product_id>', methods=['DELETE'])
def delete_product(product_id):
    """Delete a product and all its prices"""
    conn = get_db()
    conn.execute('DELETE FROM products WHERE id = ?', (product_id,))
    conn.commit()
    conn.close()
    return jsonify({'message': '删除成功'})

# --- Prices ---
@app.route('/api/products/<int:product_id>/prices', methods=['POST'])
def add_price(product_id):
    """Add a price record for a product"""
    data = request.json
    platform = data.get('platform', '').strip()
    price = data.get('price')
    if not platform or price is None:
        return jsonify({'error': '平台和价格不能为空'}), 400

    conn = get_db()
    product = conn.execute('SELECT id FROM products WHERE id = ?', (product_id,)).fetchone()
    if not product:
        conn.close()
        return jsonify({'error': '商品不存在'}), 404

    conn.execute(
        'INSERT INTO prices (product_id, platform, price, url, note) VALUES (?, ?, ?, ?, ?)',
        (product_id, platform, float(price), data.get('url', ''), data.get('note', ''))
    )
    conn.execute(
        'UPDATE products SET updated_at = datetime("now","localtime") WHERE id = ?',
        (product_id,)
    )
    conn.commit()
    conn.close()
    return jsonify({'message': '价格记录已添加'}), 201

@app.route('/api/prices/<int:price_id>', methods=['DELETE'])
def delete_price(price_id):
    """Delete a price record"""
    conn = get_db()
    conn.execute('DELETE FROM prices WHERE id = ?', (price_id,))
    conn.commit()
    conn.close()
    return jsonify({'message': '已删除'})

# --- Export ---
@app.route('/api/export', methods=['GET'])
def export_data():
    """Export all data as CSV"""
    conn = get_db()
    rows = conn.execute('''
        SELECT p.name, p.category, pr.platform, pr.price, pr.url, pr.note, pr.recorded_at
        FROM prices pr
        JOIN products p ON p.id = pr.product_id
        ORDER BY p.name, pr.platform, pr.recorded_at DESC
    ''').fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['商品名称', '分类', '平台', '价格', '链接', '备注', '记录时间'])
    for row in rows:
        writer.writerow([row['name'], row['category'], row['platform'],
                        f"¥{row['price']:.2f}", row['url'], row['note'], row['recorded_at']])

    return output.getvalue(), 200, {
        'Content-Type': 'text/csv; charset=utf-8',
        'Content-Disposition': f'attachment; filename=比价数据_{datetime.now().strftime("%Y%m%d")}.csv'
    }

# --- Stats ---
@app.route('/api/stats', methods=['GET'])
def stats():
    """Get overall stats"""
    conn = get_db()
    product_count = conn.execute('SELECT COUNT(*) FROM products').fetchone()[0]
    price_count = conn.execute('SELECT COUNT(*) FROM prices').fetchone()[0]
    categories = conn.execute(
        "SELECT DISTINCT category FROM products WHERE category != ''"
    ).fetchall()
    conn.close()
    return jsonify({
        'product_count': product_count,
        'price_count': price_count,
        'categories': [c['category'] for c in categories]
    })

# --- Quick Compare ---
@app.route('/api/quick-compare', methods=['POST'])
def quick_compare():
    """Quick compare: input product name + multiple platform prices at once"""
    data = request.json
    name = data.get('name', '').strip()
    prices = data.get('prices', [])

    if not name:
        return jsonify({'error': '商品名称不能为空'}), 400
    if len(prices) < 1:
        return jsonify({'error': '至少需要一个平台价格'}), 400

    conn = get_db()
    existing = conn.execute(
        'SELECT id FROM products WHERE name = ? ORDER BY updated_at DESC LIMIT 1',
        (name,)
    ).fetchone()

    if existing:
        product_id = existing['id']
    else:
        cur = conn.execute(
            'INSERT INTO products (name, category) VALUES (?, ?)',
            (name, data.get('category', ''))
        )
        product_id = cur.lastrowid

    for pr in prices:
        if pr.get('platform') and pr.get('price'):
            conn.execute(
                'INSERT INTO prices (product_id, platform, price, url, note) VALUES (?, ?, ?, ?, ?)',
                (product_id, pr['platform'], float(pr['price']),
                 pr.get('url', ''), pr.get('note', ''))
            )

    conn.execute(
        'UPDATE products SET updated_at = datetime("now","localtime") WHERE id = ?',
        (product_id,)
    )
    conn.commit()

    latest_prices = conn.execute('''
        SELECT platform, price, url FROM prices
        WHERE product_id = ? AND id IN (
            SELECT MAX(id) FROM prices WHERE product_id = ? GROUP BY platform
        )
        ORDER BY price ASC
    ''', (product_id, product_id)).fetchall()
    conn.close()

    return jsonify({
        'product_id': product_id,
        'name': name,
        'prices': [{
            'platform': pr['platform'],
            'price': pr['price'],
            'url': pr['url'],
            'is_cheapest': i == 0
        } for i, pr in enumerate(latest_prices)],
        'savings': latest_prices[-1]['price'] - latest_prices[0]['price'] if len(latest_prices) >= 2 else 0
    }), 201

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
