from __future__ import annotations

import json
import sqlite3
import re
import time
from contextlib import closing
from pathlib import Path
import os
from typing import Any, List
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin, quote_plus, urlparse

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ==========================================
# ⚙️ НАСТРОЙКИ КЛУБА (МЕНЯЙ ПОД СЕБЯ)
# ==========================================
MY_TEAM_NAME = "Парнас" 
STANDINGS_URL = "https://www.spbhl.ru/Standings?TournamentID=6425" # Ссылка на таблицу
CACHE_TTL = 3600 * 2 # Кэшировать парсинг на 2 часа

# 1. Задаем базовую папку проекта
BASE_DIR = Path(__file__).resolve().parent

# 2. Умный выбор пути
if os.path.exists("/data"):
    DB_PATH = Path("/data/hockey_team_v4.db")
else:
    DB_PATH = BASE_DIR / "hockey_team_v4.db"

app = FastAPI(title="Hockey Tactical Manager")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# --- МЕНЕДЖЕР ВЕБ-СОКЕТОВ ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                pass

manager = ConnectionManager()

# --- СХЕМЫ ДАННЫХ (Pydantic) ---
class UserRegister(BaseModel):
    first_name: str
    last_name: str
    password: str

class UserLogin(BaseModel):
    first_name: str
    last_name: str
    password: str

class PlayerAction(BaseModel):
    user_id: int

class GameCreate(BaseModel):
    title: str
    game_type: str    
    date: str
    time: str
    player_limit: int
    location: str = ""

class SaveTactics(BaseModel):
    tactics: dict[str, dict[str, Any]] 
    
class ChatMessage(BaseModel):
    sender_name: str
    text: str

class UrlPayload(BaseModel):
    url: str

# --- ПАРСЕР СПБХЛ ---

@dataclass
class NextGame:
    title: str
    dt_iso: str | None
    source_url: str | None
    arena_url: str | None
    
def base_url_from(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"

def fetch_html(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text

def build_yandex_maps_url(address: str) -> str:
    return "https://yandex.ru/maps/?text=" + quote_plus(address.strip())

def parse_match_place_info(match_url: str) -> tuple[str | None, str | None, str | None, str | None]:
    page_html = fetch_html(match_url)
    soup = BeautifulSoup(page_html, "html.parser")
    base_url = base_url_from(match_url)

    arena_url, arena_name, arena_address, map_url = None, None, None, None
    center = soup.select_one("div.large-4.cell.text-center")
    scope = center if center else soup

    a_arena = scope.find("a", href=lambda x: isinstance(x, str) and ("ArenaID=" in x) and ("Arena" in x))
    if a_arena and a_arena.get("href"):
        arena_url = urljoin(base_url, a_arena["href"])
        txt = a_arena.get_text(" ", strip=True)
        arena_name = txt if txt else (a_arena.get("title") or "").strip() or None

        descs = scope.find_all("span", class_="description")
        for sp in descs:
            cand = sp.get_text(" ", strip=True)
            if cand and ("," in cand) and re.search(r"\d", cand):
                arena_address = cand
                break

        if not arena_address:
            parent = a_arena.find_parent(["div", "td", "p", "section"]) or a_arena.parent
            block_text = parent.get_text("\n", strip=True) if parent else ""
            for line in block_text.splitlines():
                line = line.strip()
                if line and ("," in line) and re.search(r"\d", line):
                    arena_address = line
                    break

    if not arena_address:
        text = soup.get_text("\n", strip=True)
        m = re.search(r"Адрес[:\s]*([^\n]+)", text, flags=re.IGNORECASE)
        if m:
            cand = m.group(1).strip()
            if ("," in cand) and re.search(r"\d", cand): arena_address = cand
        if not arena_address:
            for line in text.split("\n"):
                line = line.strip()
                if line and ("," in line) and re.search(r"\d", line) and len(line) >= 12:
                    arena_address = line
                    break

    if arena_address: map_url = build_yandex_maps_url(arena_address)
    return arena_name, arena_address, map_url, arena_url

def parse_next_game_from_team_page(team_url: str, my_team_name: str) -> NextGame | None:
    if "vhlspb.ru" in team_url: return _parse_next_game_vhlspb(team_url, my_team_name)
    page_html = fetch_html(team_url)
    soup = BeautifulSoup(page_html, "html.parser")
    base_url = base_url_from(team_url)   
    table = soup.find("table", id="MatchGridView")
    
    if not table:
        now_utc = datetime.now(timezone.utc)
        cur_date, cur_time = None, None
        candidates = []
        for el in soup.select("h6, a"):
            txt = el.get_text(" ", strip=True)
            m_date = re.search(r"(\d{2}\.\d{2}\.\d{4})", txt)
            if m_date: cur_date = m_date.group(1); continue
            m_time = re.search(r"(\d{1,2}:\d{2})", txt)
            if m_time and cur_date: cur_time = m_time.group(1); continue
            
            if el.name == "a" and el.get("href") and "MatchID=" in el.get("href") and cur_date and cur_time:
                match_url = urljoin(base_url, el["href"])
                try:
                    dt = datetime.strptime(cur_date, "%d.%m.%Y").replace(tzinfo=timezone.utc)
                    h, mi = cur_time.split(":")
                    dt = dt.replace(hour=int(h), minute=int(mi))
                except Exception: continue
                if dt <= now_utc: continue
                
                teams_clean = re.sub(r"\b\d+\b", " ", txt)
                teams_clean = re.sub(r"\s+", " ", teams_clean).strip()
                opponent = teams_clean.replace(my_team_name, "").replace(" - ", " ").strip()
                opponent = re.sub(r"\s+", " ", opponent).strip()
                base_title = "Матч лиги\n" f"🆚 {opponent}"
                
                candidates.append((dt, NextGame(title=base_title, dt_iso=dt.isoformat(), source_url=match_url, arena_url=None)))
                
        if not candidates: return None
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]

    rows = table.find_all("tr")[1:]
    candidates = []
    now_utc = datetime.now(timezone.utc)

    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 8: continue
        date_text, time_text = cols[3].get_text(" ", strip=True), cols[4].get_text(" ", strip=True)
        arena_url = None
        a_arena = cols[5].find("a", href=True)
        if a_arena and a_arena.get("href"): arena_url = urljoin(base_url, a_arena["href"])

        dt = None
        for p in reversed(date_text.split()):
            try: dt = datetime.strptime(p, "%d.%m.%Y").replace(tzinfo=timezone.utc); break
            except ValueError: continue
        if not dt: continue

        m = re.search(r"(\d{1,2}:\d{2})", time_text)
        if m:
            try:
                h, mi = m.group(1).split(":")
                dt = dt.replace(hour=int(h), minute=int(mi))
            except Exception: pass

        if dt <= now_utc: continue
        opponent = cols[6].get_text(" ", strip=True).replace(my_team_name, "").replace(" - ", "").strip()
        match_url = None
        a_match = row.find("a", href=lambda x: isinstance(x, str) and "MatchID=" in x)
        if a_match and a_match.get("href"): match_url = urljoin(base_url, a_match["href"])
        
        base_title = f"🆚 {opponent}"
        candidates.append((dt, NextGame(title=base_title, dt_iso=dt.isoformat(), source_url=match_url, arena_url=arena_url)))

    if not candidates: return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]

def _parse_next_game_vhlspb(team_url: str, my_team_name: str) -> NextGame | None:
    page_html = fetch_html(team_url)
    soup = BeautifulSoup(page_html, "html.parser")
    base_url = base_url_from(team_url)
    now_utc = datetime.now(timezone.utc)
    candidates = []

    for a in soup.select('a[href*="MatchID="]'):
        href = a.get("href") or ""
        if "Match" not in href: continue
        teams_text = a.get_text(" ", strip=True)
        if not teams_text or my_team_name.lower() not in teams_text.lower(): continue

        block = a.find_parent(["div", "li", "tr", "section"]) or a.parent
        block_text = block.get_text("\n", strip=True) if block else soup.get_text("\n", strip=True)
        m_date = re.search(r"(\d{2}\.\d{2}\.\d{4})", block_text)
        m_time = re.search(r"(\d{1,2}:\d{2})", block_text)
        if not (m_date and m_time): continue

        try:
            dt = datetime.strptime(m_date.group(1), "%d.%m.%Y").replace(tzinfo=timezone.utc)
            h, mi = m_time.group(1).split(":")
            dt = dt.replace(hour=int(h), minute=int(mi))
        except Exception: continue

        if dt <= now_utc: continue
        teams_clean = re.sub(r"\s+", " ", teams_text).strip()
        opponent = re.sub(re.escape(my_team_name), "", teams_clean, flags=re.IGNORECASE).replace(" - ", " ").strip()
        opponent = re.sub(r"\s+", " ", opponent).strip()

        candidates.append((dt, NextGame(title=f"🆚 {opponent}", dt_iso=dt.isoformat(), source_url=urljoin(base_url, href), arena_url=None)))

    if not candidates: return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]

def parse_spbhl_standings(url: str) -> dict:
    page_html = fetch_html(url)
    soup = BeautifulSoup(page_html, "html.parser")
    text = soup.get_text("\n", strip=True)
    if "Турнирное положение" not in text: raise ValueError("Не нашел блок 'Турнирное положение'")
    tail = text.split("Турнирное положение", 1)[1]

    def _norm(s: str): return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()
    lines = [_norm(ln) for ln in tail.split("\n") if _norm(ln)]
    if not lines: raise ValueError("Пустая секция")
    
    try: idx_no = lines.index("№")
    except ValueError: raise ValueError("Не нашел '№' в заголовке")

    idx = idx_no + 2
    team_numbers = []
    while idx < len(lines) and lines[idx].isdigit():
        team_numbers.append(int(lines[idx]))
        idx += 1

    expected_tail = ["И", "В", "ВБ", "ПБ", "П", "РШ", "ШТ", "О"]
    header_end_idx = idx + len(expected_tail)
    rows, i = [], header_end_idx

    while i < len(lines):
        if "№ - место" in lines[i]: break
        if not lines[i].isdigit(): i += 1; continue
        pos = int(lines[i])
        if i + 1 >= len(lines): break
        team_name = lines[i + 1].strip()
        i += 2

        tokens = []
        while i < len(lines):
            if "№ - место" in lines[i]: break
            tokens.append(lines[i])
            i += 1
            if len(tokens) >= 8:
                t8 = tokens[-8:]
                if t8[0].isdigit() and t8[1].isdigit() and t8[2].isdigit() and t8[3].isdigit() and t8[4].isdigit() and re.match(r"^\d{1,3}-\d{1,3}$", t8[5]) and t8[6].isdigit() and t8[7].isdigit():
                    break

        if len(tokens) < 8: continue
        stat = tokens[-8:]
        
        rows.append({
            "pos": pos, "team": team_name,
            "gp": int(stat[0]), "w": int(stat[1]), "wb": int(stat[2]), "pb": int(stat[3]),
            "l": int(stat[4]), "gd": stat[5], "pim": int(stat[6]), "pts": int(stat[7]),
            "is_my_team": (MY_TEAM_NAME.lower() in team_name.lower())
        })

    return {"rows": rows}

def parse_player_stats(url: str):
    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")
    photo_url = ""
    
    for img in soup.find_all('img'):
        src = img.get('src', '')
        src_lower = src.lower()
        if 'nophoto' in src_lower or 'no-photo' in src_lower or 'noplayer' in src_lower: continue
        if 'player' in src_lower or 'upload' in src_lower or 'photo' in src_lower or 'imagehandler' in src_lower or 'handler' in src_lower:
            if 'logo' not in src_lower and 'icon' not in src_lower and 'bg' not in src_lower:
                photo_url = urljoin(base_url_from(url), src)
                break
                
    stats_data = []
    
    try:
        th_gp = soup.find(lambda t: t.name in ["th", "td"] and t.get_text(strip=True) == "И")
        if th_gp:
            table = th_gp.find_parent("table")
            tr = th_gp.find_parent("tr")
            
            def clean_h(s):
                for eng, rus in zip("OCPAETMH", "ОСРАЕТМН"):
                    s = s.replace(eng, rus)
                return s.strip().upper()
                
            headers_raw = [th.get_text(" ", strip=True) for th in tr.find_all(["th", "td"])]
            headers = [clean_h(h) for h in headers_raw]
            
            target_row = None
            rows = table.find_all("tr")[1:]
            
            # ШАГ 1: Ищем общую строку (карьера)
            for r in rows:
                cells = r.find_all(["td", "th"])
                if cells:
                    row_text = cells[0].get_text(strip=True).lower()
                    if "все сезоны" in row_text or "итого" in row_text:
                        target_row = r
                        break
            
            # ШАГ 2: Ищем строку турнира (по названию команды)
            if not target_row:
                for r in rows:
                    if MY_TEAM_NAME.lower() in r.get_text(strip=True).lower():
                        target_row = r
                        break
                        
            # ШАГ 3: Fallback - последняя строка
            if not target_row and len(rows) > 0:
                target_row = rows[-1]
                
            if target_row:
                cols = target_row.find_all(["td", "th"])
                
                def get_val(*possible_names):
                    for name in possible_names:
                        cn = clean_h(name)
                        if cn in headers:
                            try: return cols[headers.index(cn)].get_text(strip=True)
                            except: pass
                    return "-"

                gp = get_val("И", "ИГРЫ")
                
                # УМНАЯ ПРОВЕРКА НА ВРАТАРЯ (по уникальным колонкам)
                if "МИН" in headers or "БР" in headers or "НА 0" in headers or "%" in headers:
                    # Вратарь: Г = Пропущенные, В = Победы, % = Процент, СР = КН
                    psh = get_val("Г", "ПШ") 
                    ob = get_val("%", "%ОБ", "ОБ")
                    kn = get_val("СР", "КН")
                    w = get_val("В", "ПОБЕДЫ")
                    
                    stats_data = [
                        {"L": "И", "V": gp},
                        {"L": "В", "V": w},
                        {"L": "ПШ", "V": psh},
                        {"L": "%ОБ", "V": ob},
                        {"L": "КН", "V": kn}
                    ]
                else:
                    # ПОЛЕВОЙ ИГРОК
                    g = get_val("Г", "Ш", "ШАЙБЫ", "ГОЛЫ")
                    a = get_val("П", "ПЕРЕДАЧИ", "ПАСЫ")
                    pim = get_val("ШТ", "ШТР", "ШТРАФ")
                    pts_base = get_val("О", "ОЧК", "ОЧКИ")
                    pts_avg = get_val("О СР.", "О СР", "СР")
                    
                    pts_final = pts_base
                    if pts_avg != "-" and pts_avg != "":
                        pts_final = f"{pts_base} ({pts_avg})"
                        
                    stats_data = [
                        {"L": "И", "V": gp},
                        {"L": "Г", "V": g},
                        {"L": "П", "V": a},
                        {"L": "О", "V": pts_final},
                        {"L": "Шт", "V": pim}
                    ]

    except Exception: 
        pass
        
    if not stats_data:
        stats_data = [{"L":"И","V":"-"}, {"L":"Г","V":"-"}, {"L":"П","V":"-"}, {"L":"О","V":"-"}, {"L":"Шт","V":"-"}]
        
    return {"photo_url": photo_url, "stats": stats_data}


# --- КЭШИРОВАНИЕ ДАННЫХ ---
PLAYER_CACHE = {} 
STANDINGS_CACHE = {"time": 0, "data": None}

# --- РАБОТА С БАЗОЙ ДАННЫХ ---
def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    with closing(db_connect()) as conn:
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, first_name TEXT NOT NULL, last_name TEXT NOT NULL, password TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0)""")
        try: cur.execute("ALTER TABLE users ADD COLUMN has_pass INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError: pass 
        try: cur.execute("ALTER TABLE users ADD COLUMN spbhl_url TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError: pass 

        cur.execute("""CREATE TABLE IF NOT EXISTS games (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, game_type TEXT NOT NULL DEFAULT 'training', date TEXT NOT NULL, time TEXT NOT NULL, player_limit INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'open', payments_open INTEGER NOT NULL DEFAULT 0, signups_json TEXT NOT NULL DEFAULT '[]', reserve_json TEXT NOT NULL DEFAULT '[]', cancelled_json TEXT NOT NULL DEFAULT '[]', payments_json TEXT NOT NULL DEFAULT '{}', tactics_json TEXT NOT NULL DEFAULT '{}')""")
        try: cur.execute("ALTER TABLE games ADD COLUMN location TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError: pass 
        
        cur.execute("""CREATE TABLE IF NOT EXISTS chat_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, sender_name TEXT NOT NULL, text TEXT NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        admin_exists = cur.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0]
        if admin_exists == 0:
            cur.execute("INSERT INTO users (first_name, last_name, password, is_admin) VALUES (?, ?, ?, ?)", ("Админ", "Главный", "admin123", 1))
        conn.commit()

@app.on_event("startup")
def startup() -> None: init_db()

def get_game(game_id: int) -> dict[str, Any]:
    with closing(db_connect()) as conn:
        row = conn.cursor().execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    if not row: raise HTTPException(status_code=404, detail="Игра не найдена")
    return {"id": row["id"], "title": row["title"], "game_type": row["game_type"], "date": row["date"], "time": row["time"], "limit": row["player_limit"], "status": row["status"], "payments_open": bool(row["payments_open"]), "signups": json.loads(row["signups_json"]), "reserve": json.loads(row["reserve_json"]), "cancelled": json.loads(row["cancelled_json"]), "payments": json.loads(row["payments_json"]), "tactics": json.loads(row["tactics_json"]), "location": row["location"] if "location" in row.keys() else ""}

def save_game(game_id: int, game: dict[str, Any]) -> None:
    with closing(db_connect()) as conn:
        conn.cursor().execute("""UPDATE games SET title = ?, game_type = ?, date = ?, time = ?, player_limit = ?, status = ?, payments_open = ?, signups_json = ?, reserve_json = ?, cancelled_json = ?, payments_json = ?, tactics_json = ?, location = ? WHERE id = ?""", (game["title"], game["game_type"], game["date"], game["time"], game["limit"], game["status"], 1 if game["payments_open"] else 0, json.dumps(game["signups"], ensure_ascii=False), json.dumps(game["reserve"], ensure_ascii=False), json.dumps(game["cancelled"], ensure_ascii=False), json.dumps(game["payments"], ensure_ascii=False), json.dumps(game["tactics"], ensure_ascii=False), game.get("location", ""), game_id))
        conn.commit()

def serialize_state() -> dict[str, Any]:
    with closing(db_connect()) as conn:
        cur = conn.cursor()
        user_rows = cur.execute("SELECT id, first_name, last_name, is_admin, has_pass, spbhl_url FROM users").fetchall()
        active_games = cur.execute("SELECT id FROM games WHERE status != 'finished' AND status != 'canceled' ORDER BY id DESC").fetchall()
        finished_games = cur.execute("SELECT game_type, signups_json FROM games WHERE status = 'finished'").fetchall()
        
    stats_train = {r["id"]: 0 for r in user_rows}; stats_league = {r["id"]: 0 for r in user_rows}
    for fg in finished_games:
        for uid in json.loads(fg["signups_json"]):
            if uid in stats_train:
                if fg["game_type"] == "training": stats_train[uid] += 1
                else: stats_league[uid] += 1
                
    return {"users": [{"id": r["id"], "name": f"{r['first_name']} {r['last_name']}", "is_admin": bool(r["is_admin"]), "has_pass": bool(r["has_pass"]), "spbhl_url": r["spbhl_url"] if "spbhl_url" in r.keys() else "", "stats_train": stats_train[r["id"]], "stats_league": stats_league[r["id"]]} for r in user_rows], "games": [get_game(r["id"]) for r in active_games]}

# --- API ЭНДПОИНТЫ ПАРСЕРА ---
@app.post("/api/admin/parse_game")
def api_parse_game(payload: UrlPayload):
    try:
        url = payload.url.strip()
        if not url.startswith("http"): url = "https://" + url
        ng = parse_next_game_from_team_page(url, MY_TEAM_NAME)
        if not ng: raise Exception("Ближайшие игры не найдены")
        loc = ""
        if ng.source_url:
            arena_name, arena_address, _, _ = parse_match_place_info(ng.source_url)
            loc = arena_name or arena_address or ""
            
        dt = datetime.fromisoformat(ng.dt_iso)
        title_lines = ng.title.split('\n')
        opp = "Матч лиги"
        for line in title_lines:
            if "🆚" in line: opp = line.replace("🆚", "").strip()
                
        return {"title": opp, "date": dt.strftime("%d.%m.%Y"), "time": dt.strftime("%H:%M"), "location": loc}
    except Exception as e:
        raise HTTPException(400, str(e))

@app.post("/api/admin/user/{user_id}/spbhl")
async def save_user_spbhl(user_id: int, payload: UrlPayload):
    with closing(db_connect()) as conn:
        url = payload.url.strip()
        if url and not url.startswith("http"): url = "https://" + url
        conn.cursor().execute("UPDATE users SET spbhl_url = ? WHERE id = ?", (url, user_id))
        conn.commit()
    if user_id in PLAYER_CACHE: del PLAYER_CACHE[user_id]
    await manager.broadcast("update")
    return {"status": "ok"}

@app.get("/api/standings")
def api_standings():
    if not STANDINGS_URL: return {"error": "not configured"}
    now = time.time()
    if STANDINGS_CACHE["data"] and (now - STANDINGS_CACHE["time"]) < CACHE_TTL: return STANDINGS_CACHE["data"]
    try:
        data = parse_spbhl_standings(STANDINGS_URL)
        STANDINGS_CACHE["time"] = now; STANDINGS_CACHE["data"] = data
        return data
    except Exception as e: return {"error": str(e)}

@app.get("/api/player/{user_id}/spbhl_stats")
def api_player_stats(user_id: int):
    with closing(db_connect()) as conn:
        row = conn.cursor().execute("SELECT spbhl_url FROM users WHERE id=?", (user_id,)).fetchone()
    if not row or not row["spbhl_url"]: return {"error": "no url"}
    url = row["spbhl_url"]
    
    now = time.time()
    if user_id in PLAYER_CACHE and (now - PLAYER_CACHE[user_id]["time"]) < CACHE_TTL: 
        return PLAYER_CACHE[user_id]["data"]
        
    try:
        data = parse_player_stats(url)
        PLAYER_CACHE[user_id] = {"time": now, "data": data}
        return data
    except Exception as e: 
        return {"error": "parse error"}

# --- СТАНДАРТНЫЕ API ЭНДПОИНТЫ ---
@app.get("/", response_class=HTMLResponse)
def index() -> str: return HTML_PAGE
@app.get("/api/state")
def api_state() -> dict[str, Any]: return serialize_state()
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True: await websocket.receive_text()
    except WebSocketDisconnect: manager.disconnect(websocket)

@app.post("/api/auth/register")
async def api_register(payload: UserRegister) -> dict[str, Any]:
    # 1. Убираем лишние пробелы и делаем Первую Букву Заглавной
    f_name = payload.first_name.strip().capitalize()
    l_name = payload.last_name.strip().capitalize()
    
    if not f_name or not l_name or not payload.password: 
        raise HTTPException(status_code=400, detail="Заполните все поля")
        
    # 2. Строгая проверка: только русские буквы (и дефис для двойных имен)
    if not re.fullmatch(r'[А-Яа-яЁё]+(-[А-Яа-яЁё]+)?', f_name) or not re.fullmatch(r'[А-Яа-яЁё]+(-[А-Яа-яЁё]+)?', l_name):
        raise HTTPException(status_code=400, detail="Пишите имя и фамилию только русскими буквами!")

    with closing(db_connect()) as conn:
        cur = conn.cursor()
        # 3. Проверка на дубликаты с понятной ошибкой
        if cur.execute("SELECT id FROM users WHERE first_name=? AND last_name=?", (f_name, l_name)).fetchone(): 
            raise HTTPException(status_code=400, detail="Вы уже зарегистрированы! Если забыли пароль — напишите админу.")
            
        cur.execute("INSERT INTO users (first_name, last_name, password) VALUES (?, ?, ?)", (f_name, l_name, payload.password))
        conn.commit()
        user_id = cur.lastrowid
        
    await manager.broadcast("update")
    res = serialize_state()
    res["just_registered"] = {"id": user_id, "name": f"{f_name} {l_name}", "is_admin": False}
    return res

@app.post("/api/auth/login")
def api_login(payload: UserLogin) -> dict[str, Any]:
    with closing(db_connect()) as conn:
        row = conn.cursor().execute("SELECT id, first_name, last_name, is_admin FROM users WHERE first_name=? AND last_name=? AND password=?", (payload.first_name.strip(), payload.last_name.strip(), payload.password)).fetchone()
    if not row: raise HTTPException(status_code=400, detail="Неверные данные")
    res = serialize_state()
    res["user"] = {"id": row["id"], "name": f"{row['first_name']} {row['last_name']}", "is_admin": bool(row['is_admin'])}
    return res

@app.post("/api/admin/user/{user_id}/delete")
async def admin_delete_user(user_id: int) -> dict[str, Any]:
    with closing(db_connect()) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE id = ? AND is_admin = 0", (user_id,))
        game_rows = cur.execute("SELECT id FROM games WHERE status != 'finished' AND status != 'canceled'").fetchall()
        game_ids = [r["id"] for r in game_rows]
        conn.commit()
    for gid in game_ids:
        g = get_game(gid)
        g["signups"] = [x for x in g["signups"] if x != user_id]
        g["reserve"] = [x for x in g["reserve"] if x != user_id]
        g["cancelled"] = [x for x in g["cancelled"] if x != user_id]
        if str(user_id) in g["payments"]: del g["payments"][str(user_id)]
        if str(user_id) in g["tactics"]: del g["tactics"][str(user_id)]
        save_game(gid, g)
    await manager.broadcast("update")
    return serialize_state()

@app.post("/api/admin/user/{user_id}/toggle_pass")
async def admin_toggle_pass(user_id: int) -> dict[str, Any]:
    with closing(db_connect()) as conn:
        cur = conn.cursor()
        current = cur.execute("SELECT has_pass FROM users WHERE id = ?", (user_id,)).fetchone()
        if current:
            cur.execute("UPDATE users SET has_pass = ? WHERE id = ?", (0 if current["has_pass"] else 1, user_id))
            conn.commit()
    await manager.broadcast("update")
    return serialize_state()

@app.post("/api/admin/game/create")
async def admin_create_game(payload: GameCreate) -> dict[str, Any]:
    with closing(db_connect()) as conn:
        conn.cursor().execute("INSERT INTO games (title, game_type, date, time, player_limit, location) VALUES (?, ?, ?, ?, ?, ?)", (payload.title.strip(), payload.game_type, payload.date.strip(), payload.time.strip(), payload.player_limit, payload.location.strip()))
        conn.commit()
    await manager.broadcast("update")
    return serialize_state()

@app.post("/api/player/game/{game_id}/signup")
async def player_signup(game_id: int, payload: PlayerAction) -> dict[str, Any]:
    game = get_game(game_id)
    if game["status"] != "open": raise HTTPException(status_code=400, detail="Запись закрыта")
    uid = payload.user_id
    if uid in game["signups"] or uid in game["reserve"]: return serialize_state()
    game["cancelled"] = [x for x in game["cancelled"] if x != uid]
    if game["game_type"] == "league" or len(game["signups"]) < game["limit"]:
        game["signups"].append(uid); game["payments"][str(uid)] = "pending"
    else: game["reserve"].append(uid)
    save_game(game_id, game)
    await manager.broadcast("update")
    return serialize_state()

@app.post("/api/player/game/{game_id}/cancel")
async def player_cancel(game_id: int, payload: PlayerAction) -> dict[str, Any]:
    game = get_game(game_id)
    uid = payload.user_id
    was_main = uid in game["signups"]
    game["signups"] = [x for x in game["signups"] if x != uid]
    game["reserve"] = [x for x in game["reserve"] if x != uid]
    if uid not in game["cancelled"]: game["cancelled"].append(uid)
    if game["game_type"] == "training" and was_main and game["reserve"]:
        promoted = game["reserve"].pop(0)
        game["signups"].append(promoted); game["payments"][str(promoted)] = "pending"
    if str(uid) in game["tactics"]: del game["tactics"][str(uid)]
    save_game(game_id, game)
    await manager.broadcast("update")
    return serialize_state()

@app.post("/api/player/game/{game_id}/pay")
async def player_pay(game_id: int, payload: PlayerAction) -> dict[str, Any]:
    game = get_game(game_id)
    if not game["payments_open"]: raise HTTPException(status_code=400, detail="Оплаты закрыты")
    game["payments"][str(payload.user_id)] = "paid"
    save_game(game_id, game)
    await manager.broadcast("update")
    return serialize_state()

@app.post("/api/admin/game/{game_id}/tactics")
async def admin_save_tactics(game_id: int, payload: SaveTactics) -> dict[str, Any]:
    game = get_game(game_id)
    game["tactics"] = payload.tactics
    save_game(game_id, game)
    await manager.broadcast("update")
    return serialize_state()

@app.post("/api/admin/game/{game_id}/action/{action}")
async def admin_game_action(game_id: int, action: str) -> dict[str, Any]:
    game = get_game(game_id)
    if action == "open": game["status"] = "open"
    elif action == "close": game["status"] = "closed"
    elif action == "payments": game["payments_open"] = True
    elif action == "finish": game["status"] = "finished"
    elif action == "cancel": game["status"] = "canceled"
    save_game(game_id, game)
    await manager.broadcast("update")
    return serialize_state()
    
@app.get("/api/admin/user/{user_id}/history")
async def user_history(user_id: int):
    with closing(db_connect()) as conn:
        finished_games = conn.cursor().execute("SELECT title, date, game_type, signups_json FROM games WHERE status = 'finished' ORDER BY id DESC").fetchall()
    history = []
    for fg in finished_games:
        if user_id in json.loads(fg["signups_json"]):
            history.append({"title": fg["title"], "date": fg["date"], "type": "🏆 Матч" if fg["game_type"] == "league" else "🏒 Трен"})
    return history

@app.get("/api/chat/messages")
async def get_chat_messages():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT * FROM chat_messages ORDER BY id DESC LIMIT 50")
        messages = [dict(row) for row in cursor.fetchall()]
        messages.reverse() 
        return messages

@app.post("/api/chat/send")
async def send_chat_message(msg: ChatMessage):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("INSERT INTO chat_messages (sender_name, text) VALUES (?, ?)", (msg.sender_name, msg.text))
        conn.commit()
    await manager.broadcast("chat_update") 
    return {"status": "ok"}
    
@app.post("/api/admin/chat/clear")
async def admin_clear_chat():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("DELETE FROM chat_messages")
        conn.commit()
    await manager.broadcast("chat_update") 
    return {"status": "ok"}

# --- ИНТЕРФЕЙС ---
HTML_PAGE = r'''<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=0" />
  <title>ХК Парнас</title>
  <link rel="manifest" href="/static/manifest.json">
  <meta name="theme-color" content="#0f172a">
  <link rel="apple-touch-icon" href="/static/icon.png">
  <meta name="apple-mobile-web-app-capable" content="yes">

  <style>
    :root { --bg: #e0f2fe; --card: #ffffff; --muted: #64748b; --text: #0f172a; --dark: #0f172a; --dark2: #1e293b; --border: #bae6fd; --shadow: 0 10px 30px rgba(0, 210, 255, 0.1); --neon: #00d2ff; }
    * { box-sizing: border-box; }
    body { margin:0; background:var(--bg); color:var(--text); font-family: system-ui, sans-serif; padding-bottom: 40px; }
    .wrap { max-width: 460px; margin: 0 auto; padding: 16px; }
    .card { background:var(--card); border:1px solid var(--border); border-radius:24px; padding:20px; box-shadow:var(--shadow); margin-bottom:16px; }
    .title { font-size:24px; font-weight:900; text-align:center; display:flex; align-items:center; justify-content:center; gap:10px; color: var(--dark); text-transform: uppercase; letter-spacing: 1px;}
    .h2 { font-size:17px; font-weight:800; margin-bottom:10px; margin-top:4px;}
    .tabs, .grid2, .grid3 { display:grid; gap:10px; margin-bottom:14px;}
    .tabs, .grid2 { grid-template-columns:1fr 1fr; }
    .grid3 { grid-template-columns:repeat(3, 1fr); }
    button, input, select { width:100%; border-radius:14px; padding:12px; font-size:14px; font-weight:700; box-sizing: border-box;}
    input, select { border:1px solid var(--border); font-weight:500; margin-bottom:10px; background: white;}
    button { border:0; cursor:pointer; }
    .btn-tab.active, .btn-dark { background:var(--dark); color:white; border: 1px solid var(--neon); box-shadow: 0 4px 10px rgba(0,210,255,0.2); }
    .btn-tab, .btn-light { background:white; color:var(--text); border:1px solid var(--border); }
    .btn-outline { background:transparent; color:white; border:1px solid rgba(255,255,255,.3); }
    .btn-green { background:#10b981; color:white; }
    .btn-blue { background:#2563eb; color:white; }
    .btn-danger { background:#ef4444; color:white; padding: 6px 12px; font-size:12px; width:auto; border-radius:10px;}
    .hero { background:linear-gradient(135deg, var(--dark), var(--dark2)); color:white; border:1px solid var(--neon); position:relative; box-shadow: 0 5px 20px rgba(0, 210, 255, 0.15);}
    .badge { padding:6px 12px; border-radius:999px; font-size:11px; font-weight:800; text-transform: uppercase;}
    .open { background:#22c55e; color:white; } .closed { background:#64748b; color:white; }
    .item { display:flex; align-items:center; justify-content:space-between; background:#f8fafc; border-radius:12px; padding:10px 14px; margin-bottom:6px; font-size:14px; border:1px solid var(--border);}
    .notice { padding:12px; background:#ffe4e6; color:#be123c; border-radius:14px; font-size:13px; font-weight:700; margin-bottom:14px; display:none; text-align:center;}
    .type-badge { font-size: 11px; padding:4px 8px; border-radius:6px; background: rgba(0, 210, 255, 0.2); color: var(--neon); display: inline-block; margin-bottom: 6px; font-weight: 800;}
    .t-block { background:#f8fafc; border:1px solid var(--border); border-radius:14px; padding:10px; margin-bottom:8px; font-size:13px;}
    .t-row { display: flex; justify-content: space-between; padding: 3px 0; border-bottom: 1px dashed var(--border);}
    .t-row:last-child { border:0; }
    ::-webkit-scrollbar { width: 4px; } ::-webkit-scrollbar-track { background: transparent; } ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card" style="padding:14px; border-bottom: 3px solid var(--neon);">
      <div class="title">
        <img src="/static/icon.png" style="height:36px; filter: drop-shadow(0 0 8px rgba(0,210,255,0.6));" onerror="this.style.display='none'"> 
        ХК ПАРНАС
      </div>
    </div>
    
    <div class="tabs" id="top-tabs" style="display: none; grid-template-columns: 1fr 1fr 1fr;">
      <button id="tab-player" class="btn-tab active">Игрок</button>
      <button id="tab-standings" class="btn-tab">Таблица</button>
      <button id="tab-admin" class="btn-tab">Админ</button>
    </div>
    
    <div id="error-notice" class="notice"></div>
    <div id="app"></div>

    <div id="chat-container" style="display: none;">
      <div class="card" style="margin-top: 10px;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
          <h3 style="margin: 0; font-size: 16px;">💬 Раздевалка (Чат)</h3>
          <button id="btn-clear-chat" class="btn-danger" style="display: none; padding: 4px 10px; font-size: 11px; width: auto;" onclick="if(confirm('Точно удалить все сообщения в чате?')) clearChat()">Очистить</button>
        </div>
        <div id="chat-box" style="height: 250px; overflow-y: auto; background: #f8fafc; padding: 10px; border-radius: 8px; margin-bottom: 10px; border: 1px solid var(--border); font-size: 14px; display: flex; flex-direction: column; gap: 8px;"></div>
        <div style="display: flex; gap: 5px;">
          <input type="text" id="chat-input" placeholder="Написать..." style="flex: 1; padding: 10px; border-radius: 6px; margin-bottom: 0;">
          <button onclick="sendMsg()" class="btn-dark" style="padding: 10px 15px; width: auto;">Отправить</button>
        </div>
      </div>
    </div>
  </div>

  <div id="history-modal" style="display:none; position:fixed; top:0; left:0; right:0; bottom:0; background:rgba(15, 23, 42, 0.7); z-index:999; padding:20px; align-items:center; justify-content:center; backdrop-filter: blur(4px);">
    <div class="card" style="width:100%; max-width:400px; max-height:80vh; overflow-y:auto; position:relative; box-shadow: 0 20px 40px rgba(0,0,0,0.2);">
      <button onclick="document.getElementById('history-modal').style.display='none'" style="position:absolute; right:15px; top:15px; width:auto; padding:6px 12px; background:var(--border); color:var(--text); border-radius:10px; font-size:12px;">Закрыть</button>
      <div class="h2" id="history-name" style="margin-bottom: 15px; padding-right: 70px;">История</div>
      <div id="history-content" style="font-size:14px;">Загрузка...</div>
    </div>
  </div>

  <script>
    let state = null;
    let currentView = 'player';
    let currentUser = JSON.parse(localStorage.getItem('hockey_user')) || null;
    let activeConstructorGameId = null;
    let searchFilter = '';
    let statusFilter = 'all';

    const elTabPlayer = document.getElementById('tab-player');
    const elTabStandings = document.getElementById('tab-standings');
    const elTabAdmin = document.getElementById('tab-admin');
    elTabPlayer.onclick = () => { currentView = 'player'; syncTabs(); render(); };
    elTabStandings.onclick = () => { currentView = 'standings'; syncTabs(); render(); };
    elTabAdmin.onclick = () => { currentView = 'admin'; syncTabs(); render(); };

    function connectWebSocket() {
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const ws = new WebSocket(`${protocol}//${window.location.host}/ws`);
      ws.onmessage = function(event) { if (event.data === "update") silentLoad(); else if (event.data === "chat_update") loadChat(); };
      ws.onclose = function() { setTimeout(connectWebSocket, 3000); };
    }

    function syncTabs() {
      elTabPlayer.classList.toggle('active', currentView === 'player');
      elTabStandings.classList.toggle('active', currentView === 'standings');
      elTabAdmin.classList.toggle('active', currentView === 'admin');
    }
    
    function showError(txt) {
      const el = document.getElementById('error-notice'); el.textContent = txt; el.style.display = 'block';
      setTimeout(() => el.style.display = 'none', 3000);
    }

    async function req(url, method = 'POST', body = null) {
      try {
        const res = await fetch(url, { method, headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : null });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Ошибка');
        if (data.just_registered || data.user) { currentUser = data.just_registered || data.user; localStorage.setItem('hockey_user', JSON.stringify(currentUser)); }
        state = data; render();
      } catch (err) { showError(err.message); }
    }

    function userById(id) { return state.users.find(u => u.id === id) || { name: 'Удалён' }; }

    async function showHistory(uid, uname) {
      const mod = document.getElementById('history-modal');
      document.getElementById('history-name').textContent = '📜 ' + uname;
      document.getElementById('history-content').innerHTML = 'Загрузка...';
      mod.style.display = 'flex';
      try {
        const res = await fetch('/api/admin/user/' + uid + '/history');
        const data = await res.json();
        if (data.length === 0) { document.getElementById('history-content').innerHTML = '<div style="color:var(--muted); text-align:center; padding: 20px;">Нет завершенных игр</div>'; return; }
        let h = '';
        data.forEach(item => {
          const safeTitle = item.title || 'Без названия'; const safeDate = item.date || '??.??';
          h += `<div style="padding:10px 0; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center;"><div><div style="font-weight:700; font-size:13px;">${safeTitle}</div><div style="color:var(--muted); font-size:11px; margin-top:2px;">${item.type}</div></div><div style="color:var(--muted); font-size:12px; font-weight:600; background:#f1f5f9; padding:4px 8px; border-radius:6px;">${safeDate}</div></div>`;
        });
        document.getElementById('history-content').innerHTML = h;
      } catch(e) { document.getElementById('history-content').innerHTML = '<div style="color:#ef4444;">Ошибка загрузки</div>'; }
    }

    async function loadSpbhlStats(uid) {
        try {
            const res = await fetch('/api/player/' + uid + '/spbhl_stats');
            const data = await res.json();
            const box = document.getElementById('spbhl-stats-box');
            if(!box) return;
            
            if (data.error || !data.stats || !Array.isArray(data.stats)) { 
                box.innerHTML = '<div style="font-size:12px; color:var(--muted); text-align:center; width:100%;">Ошибка загрузки данных СПБХЛ</div>';
                return; 
            }
            
            let photoHtml = data.photo_url 
              ? `<img src="${data.photo_url}" style="width:54px; height:54px; border-radius:50%; object-fit:cover; border:2px solid var(--neon); flex-shrink:0;">`
              : `<div style="width:54px; height:54px; border-radius:50%; background:#e2e8f0; display:flex; align-items:center; justify-content:center; font-size:24px; flex-shrink:0;">🏒</div>`;
              
            let statsHtml = data.stats.map(s => `
                <div style="background:white; padding:4px 6px; border-radius:8px; border:1px solid var(--border); flex:1; min-width:35px; text-align:center;">
                    <div style="color:var(--muted); font-size:10px;">${s.L}</div>
                    <b style="font-size:12px;">${s.V}</b>
                </div>
            `).join('');
              
            box.innerHTML = `
              ${photoHtml}
              <div style="flex:1;">
                <div style="font-size:11px; color:var(--muted); text-transform:uppercase; font-weight:800; margin-bottom:4px;">Статистика профиля</div>
                <div style="display:flex; justify-content:space-between; gap:4px;">
                  ${statsHtml}
                </div>
              </div>
            `;
        } catch(e) { 
            const box = document.getElementById('spbhl-stats-box');
            if(box) box.innerHTML = '<div style="font-size:12px; color:#ef4444; text-align:center; width:100%;">Ошибка загрузки.</div>';
        }
    }

    function renderPlayer() {
      if (!currentUser) return renderPlayerAuth();
      const myUser = userById(currentUser.id); 
      
      let html = `
        <div class="card" style="padding:16px;">
          <div style="display:flex; justify-content:space-between; align-items:flex-start;">
            <div>
              <span style="color:var(--muted); font-size:12px;">Профиль:</span> <strong style="font-size:16px;">${myUser.name}</strong>
              <div style="margin-top: 10px; font-size: 13px; line-height: 1.5;">
                🏒 Тренировок: <b>${myUser.stats_train}</b><br>
                🏆 Игр лиги: <b>${myUser.stats_league}</b>
              </div>
              <button class="btn-light" style="margin-top: 12px; padding: 6px 12px; font-size: 12px; width: auto; border-radius: 8px; font-weight: 800;" onclick="showHistory(${myUser.id}, 'Моя история')">📜 Посмотреть историю</button>
            </div>
            <div style="text-align: right;"><button class="btn-light" style="width:auto; padding:6px 12px; font-size:12px; border-radius:10px; background: #f8fafc;" onclick="logout()">Выйти</button></div>
          </div>
          ${myUser.spbhl_url ? `<div id="spbhl-stats-box" style="margin-top:14px; padding:12px; background:#f8fafc; border-radius:12px; border:1px solid #e2e8f0; display:flex; gap:14px; align-items:center;"><div style="font-size:12px; color:var(--muted); text-align:center; width:100%;">Загрузка статистики СПБХЛ...</div></div>` : ''}
        </div>
      `;

      if (state.games.length === 0) return html + `<div class="card" style="text-align:center; color:var(--muted); padding:30px;">Событий пока не запланировано.</div>`;

      state.games.forEach(g => {
        const isSigned = g.signups.includes(myUser.id);
        const isReserved = g.reserve.includes(myUser.id);
        const needsToPay = g.payments_open && isSigned && g.payments[String(myUser.id)] !== 'paid' && (g.game_type === 'league' || (g.game_type === 'training' && !myUser.has_pass));
        const rosterHtml = g.signups.map((id, i) => item(`${i+1}. ${userById(id).name}`)).join('') || '<div style="color:var(--muted); font-size:13px; padding:6px;">Никто еще не записался</div>';
        const reserveHtml = g.reserve.map((id, i) => item(`${i+1}. ${userById(id).name}`)).join('') || '';

        html += `
          <div class="card hero">
            <div style="display:flex; justify-content:space-between; align-items:flex-start;">
              <div><span class="type-badge">${g.game_type === 'league' ? '🏆 ОФИЦИАЛЬНЫЙ МАТЧ' : '🏒 ТРЕНИРОВКА'}</span><div style="font-size:22px; font-weight:800;">${g.title}</div><div style="font-size:13px; color:var(--neon); margin-top:2px;">📅 ${g.date} • ⏰ ${g.time}${g.location ? ' • 📍 ' + g.location : ''}</div></div>
              <span class="badge ${g.status}">${g.status === 'open' ? 'Сбор' : 'Закрыт'}</span>
            </div>
            <div style="margin-top:14px; font-size:13px; background:rgba(255,255,255,0.08); padding:12px; border-radius:14px;">
              <div>👥 Проголосовало: <strong>${g.signups.length} ${g.game_type === 'training' ? 'из ' + g.limit : 'чел.'}</strong></div>
              ${g.game_type === 'training' ? `<div>🪑 Резерв: <strong>${g.reserve.length} чел.</strong></div>` : ''}
              <div style="margin-top:6px; border-top:1px solid rgba(255,255,255,0.1); padding-top:6px;">Мой статус: <strong>${isSigned ? '🟢 В составе' : (isReserved ? '⏳ В резерве' : '❌ Не записан')}</strong></div>
            </div>
            <div class="grid2" style="margin-top:14px; margin-bottom:0;"><button class="btn-light" onclick="req('/api/player/game/${g.id}/signup', 'POST', {user_id: ${myUser.id}})">Иду</button><button class="btn-outline" onclick="req('/api/player/game/${g.id}/cancel', 'POST', {user_id: ${myUser.id}})">Не иду</button></div>
            ${needsToPay ? `<button class="btn-green" style="margin-top:10px;" onclick="req('/api/player/game/${g.id}/pay', 'POST', {user_id: ${myUser.id}})">💸 Я оплатил</button>` : ''}
            ${g.payments_open && isSigned && g.game_type === 'training' && myUser.has_pass ? `<div style="margin-top:10px; padding:10px; background:#dcfce7; color:#166534; text-align:center; border-radius:12px; font-size:13px; font-weight:800; border: 1px solid #bbf7d0;">🎟 Оплачено по абонементу</div>` : ''}
            ${g.payments_open && isSigned && g.payments[String(myUser.id)] === 'paid' ? `<div style="margin-top:10px; padding:10px; background:#f1f5f9; color:var(--muted); text-align:center; border-radius:12px; font-size:13px; font-weight:800;">✅ Оплата подтверждена</div>` : ''}
          </div>
          <div class="card" style="margin-top:-10px; margin-bottom:24px; border-top-left-radius:0; border-top-right-radius:0; border-top:0;">
            ${renderTacticsView(g)}
            <div class="h2" style="font-size:14px; color:var(--muted); margin-top:16px;">Общий список:</div>
            <div style="max-height: 250px; overflow-y: auto; padding-right: 4px; margin-bottom: 10px;">${rosterHtml}</div>
            ${g.game_type==='training' && g.reserve.length > 0 ? `<div class="h2" style="font-size:14px; color:var(--muted); margin-top:10px;">В резерве:</div><div style="max-height: 200px; overflow-y: auto; padding-right: 4px;">${reserveHtml}</div>` : ''}
          </div>
        `;
      });
      return html;
    }

    function renderStandings() {
      let html = `<div class="card"><div class="h2" style="text-align:center; margin-bottom:15px;">🏆 Турнирная таблица</div><div id="standings-box" style="font-size:13px; text-align:center; padding:20px; color:var(--muted);">Загрузка таблицы...</div></div>`;
      setTimeout(async () => {
        try {
            const res = await fetch('/api/standings');
            const data = await res.json();
            if(data.error) throw new Error();
            let tbl = `<table style="width:100%; border-collapse: collapse; margin-top:10px; font-size:12px; text-align:center;"><tr style="background:#e2e8f0; font-weight:bold;"><td style="padding:6px; border-radius:8px 0 0 8px;">№</td><td style="text-align:left;">Команда</td><td>И</td><td>В</td><td>О</td><td style="border-radius:0 8px 8px 0;">ШТ</td></tr>`;
            data.rows.forEach(r => { tbl += `<tr style="${r.is_my_team ? 'background:#dcfce7; font-weight:bold;' : 'border-bottom:1px solid #e2e8f0;'}"><td style="padding:6px;">${r.pos}</td><td style="text-align:left; max-width:120px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${r.team}</td><td>${r.gp}</td><td>${r.w}</td><td>${r.pts}</td><td>${r.pim}</td></tr>`; });
            tbl += `</table>`;
            document.getElementById('standings-box').innerHTML = tbl;
        } catch(e) { document.getElementById('standings-box').innerHTML = '<div style="color:#ef4444;">Не удалось загрузить таблицу с сайта лиги</div>'; }
      }, 50);
      return html;
    }

    async function runParser() {
        const url = document.getElementById('parse-url').value.trim();
        if(!url) return showError("Вставьте ссылку на команду");
        document.getElementById('parse-btn').textContent = 'Парсинг...';
        try {
            const res = await fetch('/api/admin/parse_game', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({url: url}) });
            const data = await res.json();
            if(data.error || res.status !== 200) throw new Error(data.detail || "Игры не найдены");
            
            document.getElementById('new-type').value = 'league';
            document.getElementById('l-box').style.display = 'none';
            document.getElementById('loc-training-box').style.display = 'none';
            document.getElementById('loc-league-box').style.display = 'block';
            
            document.getElementById('new-title').value = data.title;
            document.getElementById('new-date').value = data.date;
            document.getElementById('new-time').value = data.time;
            document.getElementById('new-loc-league').value = data.location;
            document.getElementById('parse-btn').textContent = 'Успешно!';
            setTimeout(() => document.getElementById('parse-btn').textContent = 'Спарсить игру', 2000);
        } catch(e) { 
            showError(e.message); 
            document.getElementById('parse-btn').textContent = 'Спарсить игру'; 
        }
    }

    function renderStandings() {
    let html = `<div class="card"><div class="h2" style="text-align:center; margin-bottom:15px;">🏆 Турнирная таблица</div><div id="standings-box" style="font-size:13px; text-align:center; padding:20px; color:var(--muted);">Загрузка таблицы...</div></div>`;
    setTimeout(async () => {
        try {
            const res = await fetch('/api/standings');
            const data = await res.json();
            if (data.error) throw new Error();
            let tbl = `<table style="width:100%; border-collapse: collapse; margin-top:10px; font-size:12px; text-align:center;"><tr style="background:#e2e8f0; font-weight:bold;"><td style="padding:6px; border-radius:8px 0 0 8px;">№</td><td style="text-align:left;">Команда</td><td>И</td><td>В</td><td>О</td><td style="border-radius:0 8px 8px 0;">ШТ</td></tr>`;
            data.rows.forEach(r => {
                tbl += `<tr style="${r.is_my_team ? 'background:#dcfce7; font-weight:bold;' : 'border-bottom:1px solid #e2e8f0;'}"><td style="padding:6px;">${r.pos}</td><td style="text-align:left; max-width:120px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${r.team}</td><td>${r.gp}</td><td>${r.w}</td><td>${r.pts}</td><td>${r.pim}</td></tr>`;
            });
            tbl += `</table>`;
            document.getElementById('standings-box').innerHTML = tbl;
        } catch (e) {
            document.getElementById('standings-box').innerHTML = '<div style="color:#ef4444;">Не удалось загрузить таблицу с сайта лиги</div>';
        }
      }, 50);
      return html;
    }

    function renderAdmin() {
        if (!currentUser || !currentUser.is_admin) return `<div class="card"><div class="h2">Вход в админку</div><input type="password" id="admin-key" placeholder="Пароль"><button class="btn-dark" onclick="checkAdminKey()">Войти</button></div>`;

        let html = `
        <div class="card" style="border: 2px dashed var(--neon); background:#f0fdfd;">
        <div class="h2" style="color:#0891b2;">⚡ Авто-сбор матча (Парсер)</div>
        <input type="text" id="parse-url" placeholder="Вставь ссылку на страницу команды">
        <button id="parse-btn" class="btn-dark" style="background:#0891b2; border:none;" onclick="runParser()">Спарсить игру</button>
        </div>

        <div class="card" style="border: 2px solid var(--dark); background:#f8fafc;">
        <div class="h2" style="color:var(--dark);">📆 Добавить событие вручную</div>
        <select id="new-type" onchange="document.getElementById('l-box').style.display = this.value === 'league' ? 'none' : 'block'; document.getElementById('loc-training-box').style.display = this.value === 'training' ? 'block' : 'none'; document.getElementById('loc-league-box').style.display = this.value === 'league' ? 'block' : 'none';">
            <option value="training">🏒 Тренировка</option><option value="league">🏆 Игра лиги</option>
        </select>
        <input type="text" id="new-title" placeholder="Название (Двусторонка или соперник)">
        <div class="grid2" style="margin-bottom:0;"><input type="text" id="new-date" placeholder="Дата (Пт, 5 Июн)"><input type="text" id="new-time" placeholder="Время"></div>
        <div id="loc-training-box"><select id="new-loc-training"><option value="Арена ближняя">📍 Арена ближняя</option><option value="Арена дальняя">📍 Арена дальняя</option></select></div>
        <div id="loc-league-box" style="display:none;"><input type="text" id="new-loc-league" placeholder="📍 Локация (Арена соперника)"></div>
        <div id="l-box"><input type="number" id="new-limit" placeholder="Лимит в основе" value="15"></div>
        <button class="btn-dark" onclick="createGame()">Опубликовать</button>
        </div>

        <div class="card" style="border: 2px solid var(--muted); background:#f8fafc;">
        <div class="h2" style="color:var(--muted);">👥 Состав клуба (${state.users.length} чел)</div>
        <input type="text" id="user-search-input" placeholder="🔍 Поиск по фамилии..." oninput="searchFilter = this.value; updateUsersList();" value="${searchFilter}" style="margin-bottom:10px;">
        <div class="grid3" style="margin-bottom:10px;">
            <button class="btn-light ${statusFilter === 'all' ? 'active' : ''}" style="font-size:12px; padding:6px;" onclick="statusFilter='all'; render();">Все</button>
            <button class="btn-light ${statusFilter === 'has_pass' ? 'active' : ''}" style="font-size:12px; padding:6px;" onclick="statusFilter='has_pass'; render();">С абон.</button>
            <button class="btn-light ${statusFilter === 'no_pass' ? 'active' : ''}" style="font-size:12px; padding:6px;" onclick="statusFilter='no_pass'; render();">Без абон.</button>
        </div>
        <div id="users-list-container">
            ${renderUsersList()}
        </div>
        </div>
        `;

        state.games.forEach(g => {
            if (activeConstructorGameId === g.id) { html += renderTacticsConstructor(g); return; }
            const roster = g.signups.map((id, i) => `<div class="item"><div>${i + 1}. ${userById(id).name}</div><div style="font-size:12px; font-weight:700;">${g.payments[String(id)] === 'paid' ? '✅ Оплачено' : (g.game_type === 'training' && userById(id).has_pass ? '<span style="color:#166534;">🎟 По абонементу</span>' : '⏳ Ждет')}</div></div>`).join('') || '<div class="item">Никого нет</div>';
            html += `
            <div class="card" style="border: 1px dashed var(--muted); background:#fafafa;">
                <div class="h2">${g.title} <span style="font-size:12px; font-weight:normal; color:var(--muted);">(${g.date}${g.location ? ', ' + g.location : ''})</span></div>
                <button class="btn-blue" style="margin-bottom:12px;" onclick="activeConstructorGameId=${g.id}; render();">🛠️ Конструктор звеньев / заявки</button>
                <div class="grid3" style="margin-bottom:8px;"><button class="btn-light" style="font-size:11px; padding:6px;" onclick="req('/api/admin/game/${g.id}/action/open')">Открыть сбор</button><button class="btn-light" style="font-size:11px; padding:6px;" onclick="req('/api/admin/game/${g.id}/action/close')">Закрыть сбор</button><button class="btn-light" style="font-size:11px; padding:6px;" onclick="req('/api/admin/game/${g.id}/action/payments')">Оплаты</button></div>
                <div class="grid2"><button class="btn-dark" style="font-size:12px; padding:8px; background:#10b981; border:none;" onclick="if(confirm('Точно завершить сбор и отправить игру в архив? (Игрокам запишется статистика)')) req('/api/admin/game/${g.id}/action/finish')">Завершить / В архив</button><button class="btn-dark" style="font-size:12px; padding:8px; background:#ef4444; border:none;" onclick="if(confirm('Удалить игру навсегда?')) req('/api/admin/game/${g.id}/action/cancel')">Удалить</button></div>
                <div style="margin-top:14px;"><strong>Голоса игроков (${g.signups.length} чел):</strong><div style="max-height: 250px; overflow-y: auto; padding-right: 4px; margin-top: 8px;">${roster}</div></div>
            </div>`;
        });
        return html;
    }

    function renderUsersList() {
        let filteredUsers = state.users.filter(u => {
            const matchesSearch = u.name.toLowerCase().includes(searchFilter.toLowerCase());
            const matchesStatus = statusFilter === 'all' ? true : (statusFilter === 'has_pass' ? u.has_pass : !u.has_pass);
            return matchesSearch && matchesStatus;
        });

        return `
        <div style="font-size:12px; color:var(--muted); text-align:center; margin-bottom:10px;">Найдено: ${filteredUsers.length} чел.</div>
        <div style="max-height: 400px; overflow-y: auto; padding-right: 4px;">
            ${filteredUsers.map(u => {
                if (u.is_admin) return `<div class="item" style="background:#e2e8f0;"><strong>👑 ${u.name} (Админ)</strong></div>`;
                return `
                <div class="item" style="flex-wrap: wrap; flex-direction: column; align-items: stretch; gap: 8px;">
                <div style="display:flex; justify-content:space-between; align-items:center;"><div><strong>${u.name}</strong><br><span style="font-size:11px; color:var(--muted);">Посетил: Трен <b>${u.stats_train}</b> | Игр <b>${u.stats_league}</b></span></div><button class="btn-light" style="padding:6px 10px; font-size:11px; width:auto; border-radius:10px; background: white;" onclick="showHistory(${u.id}, '${u.name}')">📜 Карточка</button></div>
                <div style="display:flex; gap:6px;"><input type="text" id="spbhl-${u.id}" placeholder="🔗 Ссылка СПБХЛ" value="${u.spbhl_url || ''}" style="font-size:11px; padding:6px; margin:0; flex:1;"><button class="btn-dark" style="padding:6px; font-size:11px; width:auto;" onclick="req('/api/admin/user/${u.id}/spbhl', 'POST', {url: document.getElementById('spbhl-${u.id}').value})">💾</button></div>
                <div style="display:flex; gap:6px;"><button class="btn-light" style="padding:6px; font-size:12px; flex:1; background:${u.has_pass ? '#dcfce7' : '#fff'}; border-color:${u.has_pass ? '#22c55e' : 'var(--border)'};" onclick="req('/api/admin/user/${u.id}/toggle_pass')">${u.has_pass ? '🎟 Абонемент' : '🎟 Нет абон.'}</button><button class="btn-danger" style="padding:6px; font-size:12px; width:auto;" onclick="deleteUser(${u.id}, '${u.name}')">Удалить</button></div>
                </div>`;
            }).join('')}
        </div>
        `;
    }

    function updateUsersList() {
        const container = document.getElementById('users-list-container');
        if (container) {
            container.innerHTML = renderUsersList();
            document.getElementById('user-search-input').focus();
        }
    }

    function renderTacticsView(g) {
        const t = g.tactics;
        if (!Object.keys(t).length) return '<p style="color:var(--muted); font-size:13px; text-align:center;">Состав звеньев еще не утвержден тренером.</p>';
        if (g.game_type === 'training') {
            let teams = { white: { 1: [], 2: [], 3: [], gk: [] }, black: { 1: [], 2: [], 3: [], gk: [] } };
            for (let uid in t) {
                let p = t[uid];
                if (!teams[p.team]) continue;
                if (p.role === 'gk') teams[p.team].gk.push(uid);
                else if (p.line >= 1 && p.line <= 3) teams[p.team][p.line].push(uid);
            }
            const rT = (tN, cB) => {
                let h = `<div style="margin-top:10px; font-weight:800; text-align:center; padding:6px; border-radius:10px; ${cB}"></div><div class="t-block"><strong>🥅 Вратари:</strong> ${teams[tN].gk.map(id => userById(Number(id)).name).join(', ') || 'нет'}</div>`;
                for (let l = 1; l <= 3; l++) {
                    if (teams[tN][l].length === 0) continue;
                    h += `<div class="t-block"><strong>⚡ Пятерка ${l}:</strong><br>` + teams[tN][l].map(id => `<div class="t-row"><span>${userById(Number(id)).name}</span><span style="color:var(--muted); font-size:11px;">${t[id].role === 'forward' ? '🔹 Нап' : '🔸 Защ'}</span></div>`).join('') + `</div>`;
                }
                return h;
            };
            return `<div class="h2" style="font-size:15px; border-top:1px solid var(--border); padding-top:10px; margin-top:10px;">📋 ТАКТИЧЕСКИЙ СОСТАВ:</div><div class="grid2" style="gap:14px; align-items: flex-start;"><div>${rT('white', 'background:#e2e8f0; color:#0f172a; border:1px solid #cbd5e1;').replace('"></div>', '">🤍 БЕЛЫЕ</div>')}</div><div>${rT('black', 'background:#0f172a; color:white;').replace('"></div>', '">🖤 ЧЕРНЫЕ</div>')}</div></div>`;
        } else {
            let lines = { 1: [], 2: [], 3: [], gk: [], res: [] };
            g.signups.forEach(uid => {
                let p = t[String(uid)];
                if (!p || p.team === 'reserve') lines.res.push(uid);
                else if (p.role === 'gk') lines.gk.push(uid);
                else if (p.line >= 1 && p.line <= 3) lines[p.line].push(uid);
            });
            let h = `<div class="h2" style="font-size:15px; border-top:1px solid var(--border); padding-top:10px; margin-top:10px;">🏆 ЗАЯВКА НА МАТЧ (ОСНОВА):</div><div class="t-block"><strong>🥅 Вратари в заявке:</strong> ${lines.gk.map(id => userById(id).name).join(', ') || 'не назначены'}</div>`;
            for (let l = 1; l <= 3; l++) {
                h += `<div class="t-block"><strong>🔥 Звено ${l}:</strong>` + (lines[l].map(id => `<div class="t-row"><span>${userById(id).name}</span><span style="color:var(--muted); font-size:11px;">${t[String(id)].role === 'forward' ? 'Нападающий' : 'Защитник'}</span></div>`).join('') || '<div style="color:var(--muted);">Не сформировано</div>') + `</div>`;
            }
            if (lines.res.length > 0) {
                h += `<div class="h2" style="font-size:14px; color:var(--muted);">🔄 Официальный резерв на игру:</div><div class="t-block" style="background:#fff1f2; border-color:#fecdd3;">` + lines.res.map((id, i) => `<div>${i + 1}. ${userById(id).name}</div>`).join('') + `</div>`;
            }
            return h;
        }
    }

    function renderTacticsConstructor(g) {
      let html = `<div class="card" style="border:2px solid #10b981; background:#f0fdf4;"><div class="h2" style="color:#166534;">🛠️ Конструктор звеньев и заявок</div>`;
      g.signups.forEach(uid => {
        const user = userById(uid); const pT = g.tactics[String(uid)] || { team: g.game_type==='training'?'white':'line', line: 1, role: 'forward' };
        html += `<div style="background:white; padding:10px; border-radius:12px; border:1px solid var(--border); margin-bottom:8px; font-size:13px;"><div style="font-weight:800; margin-bottom:6px; color:var(--dark);">${user.name}</div><div style="display:grid; grid-template-columns: repeat(3, 1fr); gap:6px;"><select id="t-team-${uid}" style="padding:6px; font-size:12px; margin:0;">${g.game_type === 'training' ? `<option value="white" ${pT.team==='white'?'selected':''}>🤍 Белые</option><option value="black" ${pT.team==='black'?'selected':''}>🖤 Черные</option>` : `<option value="line" ${pT.team==='line'?'selected':''}>🟢 В заявку</option><option value="reserve" ${pT.team==='reserve'?'selected':''}>🔴 В Резерв</option>`}</select><select id="t-line-${uid}" style="padding:6px; font-size:12px; margin:0;"><option value="1" ${pT.line==1?'selected':''}>1-е звено</option><option value="2" ${pT.line==2?'selected':''}>2-е звено</option><option value="3" ${pT.line==3?'selected':''}>3-е звено</option></select><select id="t-role-${uid}" style="padding:6px; font-size:12px; margin:0;"><option value="forward" ${pT.role==='forward'?'selected':''}>🏃 Нападающий</option><option value="defender" ${pT.role==='defender'?'selected':''}>🛡️ Защитник</option><option value="gk" ${pT.role==='gk'?'selected':''}>🥅 Вратарь</option></select></div></div>`;
      });
      return html + `<div class="grid2" style="margin-top:12px;"><button class="btn-green" onclick="saveTactics(${g.id})">💾 Сохранить</button><button class="btn-light" onclick="activeConstructorGameId=null; render();">Закрыть</button></div></div>`;
    }

    async function saveTactics(gameId) {
      const g = state.games.find(x => x.id === gameId); let payload = { tactics: {} };
      g.signups.forEach(uid => { payload.tactics[String(uid)] = { team: document.getElementById(`t-team-${uid}`).value, line: parseInt(document.getElementById(`t-line-${uid}`).value), role: document.getElementById(`t-role-${uid}`).value }; });
      await req(`/api/admin/game/${gameId}/tactics`, 'POST', payload); activeConstructorGameId = null;
    }

    async function deleteUser(id, name) { if (confirm(`Удалить игрока ${name}?`)) await req(`/api/admin/user/${id}/delete`, 'POST'); }
    function renderPlayerAuth() { return `<div class="card"><div class="h2">Вход / Регистрация команды</div><input type="text" id="auth-fname" placeholder="Имя"><input type="text" id="auth-lname" placeholder="Фамилия"><input type="password" id="auth-pass" placeholder="Пароль"><div class="grid2"><button class="btn-dark" onclick="login()">Войти</button><button class="btn-light" onclick="register()">Регистрация</button></div><div style="margin-top:14px; font-size:12px; color:var(--muted); text-align:center;">Указывайте реальные имя и фамилию на русском языке. Забыли пароль? Напишите админу!</div></div>`; }    async function register() { await req('/api/auth/register', 'POST', { first_name: document.getElementById('auth-fname').value, last_name: document.getElementById('auth-lname').value, password: document.getElementById('auth-pass').value }); }
    async function login() { await req('/api/auth/login', 'POST', { first_name: document.getElementById('auth-fname').value, last_name: document.getElementById('auth-lname').value, password: document.getElementById('auth-pass').value }); }
    function logout() { localStorage.removeItem('hockey_user'); currentUser = null; render(); }
    function checkAdminKey() { if (document.getElementById('admin-key').value === 'admin123') { currentUser = { id: 1, name: 'Админ Главный', is_admin: true }; localStorage.setItem('hockey_user', JSON.stringify(currentUser)); render(); } else { alert('Неверно!'); } }
    async function createGame() { 
      const t = document.getElementById('new-title').value.trim(); const d = document.getElementById('new-date').value.trim(); const tm = document.getElementById('new-time').value.trim(); const gType = document.getElementById('new-type').value;
      const loc = gType === 'training' ? document.getElementById('new-loc-training').value : document.getElementById('new-loc-league').value.trim();
      if (!t || !d) { showError("Заполните название и дату!"); return; }
      await req('/api/admin/game/create', 'POST', { title: t, game_type: gType, date: d, time: tm, location: loc, player_limit: gType === 'league' ? 999 : parseInt(document.getElementById('new-limit').value) }); 
    }
    function item(left) { return `<div class="item"><div>${left}</div></div>`; }

    async function loadChat() {
      try {
        const res = await fetch('/api/chat/messages', { cache: 'no-store' }); if (!res.ok) return;
        const box = document.getElementById('chat-box');
        box.innerHTML = (await res.json()).map(m => `<div style="background: white; padding: 8px 12px; border-radius: 12px; border: 1px solid var(--border); border-bottom-left-radius: 4px;"><strong style="color:var(--dark); font-size:12px; display:block; margin-bottom:2px;">${m.sender_name}</strong><div style="word-wrap: break-word;">${m.text}</div></div>`).join('');
        box.scrollTop = box.scrollHeight;
      } catch (e) {}
    }
    async function sendMsg() { const i = document.getElementById('chat-input'); const t = i.value.trim(); if (!t) return; i.value = ''; try { await fetch('/api/chat/send', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sender_name: currentUser ? currentUser.name : 'Аноним', text: t }) }); } catch (e) { showError("Ошибка отправки"); } }
    document.getElementById('chat-input').addEventListener('keypress', function (e) { if (e.key === 'Enter') sendMsg(); });
    async function clearChat() { try { await fetch('/api/admin/chat/clear', { method: 'POST' }); } catch (e) { showError("Ошибка очистки"); } }

    function render() {
      const c = document.getElementById('app'); const cc = document.getElementById('chat-container'); const bcc = document.getElementById('btn-clear-chat'); 
      if (!state) { c.innerHTML = 'Загрузка...'; return; }
      
      if (currentView === 'player') { 
          c.innerHTML = renderPlayer(); 
          if (currentUser) {
              const myUser = userById(currentUser.id);
              if (myUser && myUser.spbhl_url) loadSpbhlStats(myUser.id);
          }
      } 
      else if (currentView === 'standings') c.innerHTML = renderStandings(); 
      else c.innerHTML = renderAdmin();
      
      if (currentUser) { cc.style.display = 'block'; if (bcc) bcc.style.display = currentUser.is_admin ? 'block' : 'none'; if (document.getElementById('chat-box').innerHTML.trim() === '') loadChat(); } else cc.style.display = 'none';
    }
    
    const TEAMS_SECRET = 'hockey1954';
    function renderSiteAuth() { document.getElementById('app').innerHTML = `<div class="card" style="text-align:center; margin-top:20px; border:1px solid var(--neon); box-shadow: 0 10px 30px rgba(0,210,255,0.1);"><img src="/static/icon.png" style="width:90px; height:90px; object-fit:contain; margin-bottom:15px; filter: drop-shadow(0 0 15px rgba(0,210,255,0.5));" onerror="this.style.display='none'"><div class="h2" style="font-size:22px; text-transform:uppercase;">ХК Парнас</div><p style="font-size:13px; color:var(--muted); margin-bottom:16px;">Введите секретный пароль клуба</p><input type="password" id="site-key" placeholder="Пароль"><button class="btn-dark" style="border:1px solid var(--neon); box-shadow:0 4px 15px rgba(0,210,255,0.3);" onclick="if(document.getElementById('site-key').value === TEAMS_SECRET){localStorage.setItem('site_pass', TEAMS_SECRET); runApp();} else showError('Неверный пароль!');">Войти</button></div>`; }
    async function runApp() { document.getElementById('top-tabs').style.display = 'grid'; const res = await fetch('/api/state', { cache: 'no-store' }); state = await res.json(); render(); connectWebSocket(); }
    function load() { if (localStorage.getItem('site_pass') !== TEAMS_SECRET) renderSiteAuth(); else runApp(); }
    async function silentLoad() { const res = await fetch('/api/state', { cache: 'no-store' }); state = await res.json(); if (activeConstructorGameId === null) render(); }
    load();
  </script>
</body>
</html>
'''