import json
import os
import re
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify
from datetime import datetime
from urllib.parse import urljoin
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

CONFIG_FILE = 'config.json'
ADMIN_PASSWORD = "1111" # 기본 비밀번호

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"boards": []}

def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

def get_headers(url):
    return {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Referer': url
    }

def parse_date(date_str):
    if not date_str: return datetime(1900, 1, 1)
    try:
        clean_date = re.sub(r'[^0-9-]', '-', date_str.replace('.', '-')).strip('-')
        parts = clean_date.split('-')
        if len(parts[0]) == 2: parts[0] = '20' + parts[0]
        return datetime.strptime("-".join(parts[:3]), '%Y-%m-%d')
    except:
        return datetime(1900, 1, 1)

def scrape_board(url, name, keyword):
    posts = []
    try:
        session = requests.Session()
        response = session.get(url, headers=get_headers(url), verify=False, timeout=15)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        
        rows = soup.select('table tbody tr, .board-list tr, .bbs-list tr, .list_type li, .news-list li, .search-result-list li, .list-wrap li')
        if not rows:
            rows = soup.select('.title, .subject, .txt_left, .tit')

        for row in rows:
            title_elem = row.select_one('a, .tit, .subject, .title')
            if not title_elem: continue
            
            title = title_elem.get_text(strip=True)
            if len(title) < 3: continue
            
            # 키워드 필터링
            if keyword and keyword.strip():
                if keyword.strip() not in title:
                    continue
            
            link = title_elem.get('href', '')
            if not link or '#' in link or 'javascript' in link:
                parent_a = row.find_parent('a') or row.find('a')
                if parent_a: link = parent_a.get('href', '')

            full_link = urljoin(url, link)
            
            date_val = ""
            for elem in row.select('td, span, .date, .reg_date, .day'):
                txt = elem.get_text(strip=True)
                if re.search(r'\d{2,4}[-./]\d{1,2}[-./]\d{1,2}', txt):
                    date_val = txt
                    break
            
            posts.append({
                'title': title,
                'link': full_link,
                'date': date_val,
                'dt_obj': parse_date(date_val),
                'source': name
            })
            
        posts.sort(key=lambda x: x['dt_obj'], reverse=True)
        
    except Exception as e:
        print(f"Error scraping {name}: {e}")
    
    return posts

@app.route('/')
def index():
    config = load_config()
    return render_template('index.html', boards=config.get('boards', []))

@app.route('/api/scrape_all')
def api_scrape_all():
    config = load_config()
    all_results = []
    integrated_feed = [] # 통합 피드용
    
    for board in config.get('boards', []):
        kw = board.get('keyword', '')
        posts = scrape_board(board['url'], board['name'], kw)
        
        # 개별 게시판 결과 저장
        clean_posts = []
        for p in posts:
            integrated_feed.append(p.copy()) # 통합 피드에 추가
            p.pop('dt_obj', None) # JSON 전송을 위해 제거
            clean_posts.append(p)

        all_results.append({
            'name': board['name'],
            'url': board['url'],
            'keyword': kw,
            'posts': clean_posts[:15]
        })
    
    # 통합 피드 최신순 정렬 및 상위 30개 추출
    integrated_feed.sort(key=lambda x: x['dt_obj'], reverse=True)
    for p in integrated_feed: p.pop('dt_obj', None)
    
    return jsonify({
        'success': True,
        'data': all_results,
        'latest_posts': integrated_feed[:30],
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    })

@app.route('/api/boards', methods=['POST', 'DELETE'])
def manage_boards():
    data = request.json
    if data.get('password') != ADMIN_PASSWORD:
        return jsonify({'success': False, 'message': 'Password Denied'}), 403
    
    config = load_config()
    if request.method == 'POST':
        config['boards'].append({'name': data['name'], 'url': data['url'], 'keyword': data.get('keyword', '')})
    elif request.method == 'DELETE':
        config['boards'] = [b for b in config['boards'] if b['url'] != data['url']]
    save_config(config)
    return jsonify({'success': True})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)