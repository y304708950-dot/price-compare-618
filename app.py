"""
比价助手 - 多平台价格对比工具
支持：京东、淘宝/天猫、拼多多、苏宁、其他
功能：产品管理、多平台比价、价格历史、数据导出
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
from urllib.parse import urlparse, parse_qs

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
        # Get latest prices per platform
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

    # If prices provided, add them
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
    # Check product exists
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
    # Check if similar product exists
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

    # Return comparison result
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
