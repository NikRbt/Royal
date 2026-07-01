import json
import os
import random
import time
import uuid
from threading import Lock

from flask import Flask, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit, join_room, disconnect
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'db.json')

ADMIN_NAMES = ['admin']  # Namen (lowercase), die automatisch Admin-Rechte bekommen

DEFAULT_SETTINGS = {
    "betting_seconds": 15,
    "spin_seconds": 3,
    "result_seconds": 5,
    "start_coins": 1000,
}

RED_NUMBERS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}

MAX_CHAT_LEN = 500
MAX_GLOBAL_HISTORY = 200
MAX_PRIVATE_HISTORY = 200
MAX_COIN_HISTORY = 300

db_lock = Lock()

DEFAULT_DB = {
    "users": [],
    "transactions": [],
    "round_history": [],  # letzte Ergebnisse {number, color, ts}
    "global_chat": [],
    "private_chats": {},  # key "id1_id2" (sortiert) -> [messages]
    "settings": dict(DEFAULT_SETTINGS),
    "coin_history": [],  # Snapshots {ts, total, top:[{name,coins}]}
    "durak_games": [],  # aktive Spiele
    "durak_stats": {},  # userId -> {wins, losses}
}


def load_db():
    if not os.path.exists(DB_PATH):
        save_db(DEFAULT_DB)
        return json.loads(json.dumps(DEFAULT_DB))
    with open(DB_PATH, 'r', encoding='utf-8') as f:
        db = json.load(f)
    # Migration für ältere db.json-Dateien ohne neuere Felder
    db.setdefault('global_chat', [])
    db.setdefault('private_chats', {})
    db.setdefault('settings', dict(DEFAULT_SETTINGS))
    for k, v in DEFAULT_SETTINGS.items():
        db['settings'].setdefault(k, v)
    db.setdefault('coin_history', [])
    db.setdefault('durak_games', [])
    db.setdefault('durak_stats', {})
    return db


def get_settings(db):
    return db.get('settings', dict(DEFAULT_SETTINGS))


def save_db(data):
    with open(DB_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_user(db, user_id):
    return next((u for u in db['users'] if u['id'] == user_id), None)


def get_user_by_name(db, name):
    return next((u for u in db['users'] if u['name'].lower() == name.lower()), None)


def add_transaction(db, user_id, name, amount, reason):
    tx = {
        "id": uuid.uuid4().hex[:8],
        "userId": user_id,
        "name": name,
        "amount": amount,
        "reason": reason,
        "ts": int(time.time() * 1000),
    }
    db['transactions'].append(tx)
    if len(db['transactions']) > 500:
        db['transactions'] = db['transactions'][-500:]
    return tx


def private_chat_key(id_a, id_b):
    return '_'.join(sorted([id_a, id_b]))


def number_color(n):
    if n == 0:
        return "green"
    return "red" if n in RED_NUMBERS else "black"


# static_folder angepasst, template_folder deaktiviert, da Dateien flach liegen
app = Flask(__name__, static_folder='.', template_folder='.')
app.config['SECRET_KEY'] = 'roulette-secret'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# sid -> {"userId":..., "name":...}
online = {}


def sids_for_user(user_id):
    return [sid for sid, v in online.items() if v['userId'] == user_id]


# ============ ROULETTE STATE (im Speicher) ============
roulette = {
    "phase": "betting",       # betting | spinning | result
    "round_id": 1,
    "ends_at": time.time() + DEFAULT_SETTINGS['betting_seconds'],
    "bets": {},                # userId -> list of {type, value, amount}
    "result_number": None,
}
roulette_lock = Lock()


# 📁 HIER KORRIGIERT: send_from_directory nutzt jetzt BASE_DIR statt 'templates'
@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')


@app.route('/admin.html')
def admin_page():
    return send_from_directory(BASE_DIR, 'admin.html')


@app.route('/dashboard.html')
def dashboard_page():
    return send_from_directory(BASE_DIR, 'dashboard.html')


@app.route('/durak.html')
def durak_page():
    return send_from_directory(BASE_DIR, 'durak.html')


def public_user(u):
    return {"id": u['id'], "name": u['name'], "coins": u['coins'], "isAdmin": u['isAdmin']}


@app.route('/api/login', methods=['POST'])
def login():
    data = request.json or {}
    name = (data.get('name') or '').strip()
    password = data.get('password') or ''

    if not name or len(name) > 24:
        return jsonify({"error": "Ungültiger Name"}), 400
    if not password or len(password) < 4:
        return jsonify({"error": "Passwort muss mindestens 4 Zeichen haben"}), 400

    with db_lock:
        db = load_db()
        user = get_user_by_name(db, name)

        if not user:
            start_coins = get_settings(db)['start_coins']
            user = {
                "id": uuid.uuid4().hex[:10],
                "name": name,
                "coins": start_coins,
                "isAdmin": name.lower() in ADMIN_NAMES,
                "password_hash": generate_password_hash(password),
            }
            db['users'].append(user)
            add_transaction(db, user['id'], user['name'], start_coins, "Startguthaben")
            save_db(db)
        elif not user.get('password_hash'):
            user['password_hash'] = generate_password_hash(password)
            save_db(db)
        else:
            if not check_password_hash(user['password_hash'], password):
                return jsonify({"error": "Falsches Passwort"}), 401

    return jsonify({"user": public_user(user)})


def public_users(db):
    online_user_ids = {v['userId'] for v in online.values()}
    return [
        {
            "id": u['id'], "name": u['name'], "coins": u['coins'], "isAdmin": u['isAdmin'],
            "online": u['id'] in online_user_ids,
        }
        for u in db['users']
    ]


def broadcast_users():
    with db_lock:
        db = load_db()
        users = public_users(db)
    socketio.emit('users:update', users)


def public_roulette_state():
    with roulette_lock:
        bet_totals = {}
        for uid, bets in roulette['bets'].items():
            for b in bets:
                key = f"{b['type']}:{b['value']}"
                bet_totals[key] = bet_totals.get(key, 0) + b['amount']
        return {
            "phase": roulette['phase'],
            "round_id": roulette['round_id'],
            "ends_at": roulette['ends_at'],
            "bet_totals": bet_totals,
            "result_number": roulette['result_number'],
            "result_color": number_color(roulette['result_number']) if roulette['result_number'] is not None else None,
        }


def my_bets_payload(user_id):
    with roulette_lock:
        return roulette['bets'].get(user_id, [])


# ============ DASHBOARD / LEADERBOARD ============

def dashboard_payload(db):
    leaderboard = sorted(db['users'], key=lambda u: u['coins'], reverse=True)
    online_ids = {v['userId'] for v in online.values()}
    return {
        "leaderboard": [
            {"id": u['id'], "name": u['name'], "coins": u['coins'], "isAdmin": u['isAdmin'], "online": u['id'] in online_ids}
            for u in leaderboard[:50]
        ],
        "history": db['coin_history'][-MAX_COIN_HISTORY:],
        "total_users": len(db['users']),
        "total_coins": sum(u['coins'] for u in db['users']),
        "online_count": len(online_ids),
    }


def record_coin_snapshot(db):
    leaderboard = sorted(db['users'], key=lambda u: u['coins'], reverse=True)
    entry = {
        "ts": int(time.time() * 1000),
        "total": sum(u['coins'] for u in db['users']),
        "top": [{"name": u['name'], "coins": u['coins']} for u in leaderboard[:5]],
    }
    db['coin_history'].append(entry)
    db['coin_history'] = db['coin_history'][-MAX_COIN_HISTORY:]


def broadcast_dashboard():
    with db_lock:
        db = load_db()
        payload = dashboard_payload(db)
    socketio.emit('dashboard:update', payload)


# ============ SOCKET.IO — ROULETTE ============

@socketio.on('identify')
def on_identify(user_id):
    with db_lock:
        db = load_db()
        user = get_user(db, user_id)
        history = db['round_history'][-20:]
    if not user:
        return
    online[request.sid] = {"userId": user['id'], "name": user['name']}
    join_room('global')
    emit('roulette:state', public_roulette_state())
    emit('roulette:myBets', my_bets_payload(user['id']))
    emit('roulette:history', history)
    broadcast_users()


@socketio.on('roulette:bet')
def on_bet(data):
    me = online.get(request.sid)
    if not me:
        return
    bet_type = data.get('type')
    value = data.get('value')
    amount = data.get('amount')

    if not isinstance(amount, (int, float)) or amount <= 0:
        emit('roulette:error', "Ungültiger Einsatz.")
        return
    amount = int(amount)

    valid_types = {'number', 'red', 'black', 'odd', 'even', 'low', 'high'}
    if bet_type not in valid_types:
        emit('roulette:error', "Ungültige Wette.")
        return
    if bet_type == 'number':
        try:
            value = int(value)
        except (TypeError, ValueError):
            emit('roulette:error', "Ungültige Zahl.")
            return
        if not (0 <= value <= 36):
            emit('roulette:error', "Zahl muss zwischen 0 und 36 liegen.")
            return
    else:
        value = bet_type

    with roulette_lock:
        if roulette['phase'] != 'betting':
            emit('roulette:error', "Wetten gerade nicht möglich — warte auf die nächste Runde.")
            return

    with db_lock:
        db = load_db()
        user = get_user(db, me['userId'])
        if not user or user['coins'] < amount:
            emit('roulette:error', "Nicht genug Coins.")
            return
        user['coins'] -= amount
        add_transaction(db, user['id'], user['name'], -amount, f"Einsatz Roulette ({bet_type} {value})")
        save_db(db)
        new_coins = user['coins']

    with roulette_lock:
        roulette['bets'].setdefault(me['userId'], []).append(
            {"type": bet_type, "value": value, "amount": amount}
        )

    emit('roulette:coins', new_coins)
    emit('roulette:myBets', my_bets_payload(me['userId']))
    socketio.emit('roulette:state', public_roulette_state(), room='global')
    broadcast_users()


@socketio.on('roulette:removeBet')
def on_remove_bet(data):
    me = online.get(request.sid)
    if not me:
        return

    with roulette_lock:
        if roulette['phase'] != 'betting':
            emit('roulette:error', "Einsätze können nur während der Setzphase entfernt werden.")
            return
        user_bets = roulette['bets'].get(me['userId'], [])
        index = (data or {}).get('index')

        if index is None:
            removed = user_bets
            roulette['bets'][me['userId']] = []
        else:
            try:
                index = int(index)
            except (TypeError, ValueError):
                return
            if not (0 <= index < len(user_bets)):
                emit('roulette:error', "Einsatz nicht gefunden.")
                return
            removed = [user_bets.pop(index)]

    refund_total = sum(b['amount'] for b in removed)
    if refund_total <= 0:
        return

    with db_lock:
        db = load_db()
        user = get_user(db, me['userId'])
        if not user:
            return
        user['coins'] += refund_total
        for b in removed:
            add_transaction(db, user['id'], user['name'], b['amount'], f"Einsatz zurückgenommen ({b['type']} {b['value']})")
        save_db(db)
        new_coins = user['coins']

    emit('roulette:coins', new_coins)
    emit('roulette:myBets', my_bets_payload(me['userId']))
    socketio.emit('roulette:state', public_roulette_state(), room='global')
    broadcast_users()


@socketio.on('disconnect')
def on_disconnect():
    online.pop(request.sid, None)
    broadcast_users()


# ============ SOCKET.IO — CHAT ============

@socketio.on('chat:sendGlobal')
def on_chat_global(data):
    me = online.get(request.sid)
    if not me:
        return
    text = ((data or {}).get('text') or '').strip()
    if not text or len(text) > MAX_CHAT_LEN:
        return
    msg = {
        "id": uuid.uuid4().hex[:8],
        "userId": me['userId'],
        "name": me['name'],
        "text": text,
        "ts": int(time.time() * 1000),
    }
    with db_lock:
        db = load_db()
        db['global_chat'].append(msg)
        db['global_chat'] = db['global_chat'][-MAX_GLOBAL_HISTORY:]
        save_db(db)
    socketio.emit('chat:global', msg, room='global')


@socketio.on('chat:sendPrivate')
def on_chat_private(data):
    me = online.get(request.sid)
    if not me:
        return
    to_id = (data or {}).get('to')
    text = ((data or {}).get('text') or '').strip()
    if not to_id or not text or len(text) > MAX_CHAT_LEN or to_id == me['userId']:
        return

    with db_lock:
        db = load_db()
        target_user = get_user(db, to_id)
        if not target_user:
            return
        msg = {
            "id": uuid.uuid4().hex[:8],
            "from": me['userId'],
            "fromName": me['name'],
            "to": to_id,
            "toName": target_user['name'],
            "text": text,
            "ts": int(time.time() * 1000),
        }
        key = private_chat_key(me['userId'], to_id)
        db['private_chats'].setdefault(key, []).append(msg)
        db['private_chats'][key] = db['private_chats'][key][-MAX_PRIVATE_HISTORY:]
        save_db(db)

    for sid in sids_for_user(me['userId']) + sids_for_user(to_id):
        socketio.emit('chat:private', msg, room=sid)


@socketio.on('chat:history')
def on_chat_history(data):
    me = online.get(request.sid)
    if not me:
        return
    kind = (data or {}).get('type')
    with db_lock:
        db = load_db()
        if kind == 'global':
            emit('chat:globalHistory', db['global_chat'][-MAX_GLOBAL_HISTORY:])
        elif kind == 'private':
            other_id = (data or {}).get('with')
            if not other_id:
                return
            key = private_chat_key(me['userId'], other_id)
            emit('chat:privateHistory', {
                "with": other_id,
                "messages": db['private_chats'].get(key, []),
            })


# ============ ROULETTE ENGINE TIMERS ============

def run_round_payouts(db, win_num):
    color = number_color(win_num)
    is_odd = (win_num % 2 != 0) and (win_num != 0)
    is_even = (win_num % 2 == 0) and (win_num != 0)
    is_low = (1 <= win_num <= 18)
    is_high = (19 <= win_num <= 36)

    with roulette_lock:
        all_bets = dict(roulette['bets'])

    for uid, bets in all_bets.items():
        user = get_user(db, uid)
        if not user:
            continue
        
        total_won = 0
        for b in bets:
            b_type = b['type']
            val = b['value']
            amt = b['amount']

            won = False
            multiplier = 0

            if b_type == 'number' and val == win_num:
                won, multiplier = True, 35
            elif b_type == 'red' and color == 'red':
                won, multiplier = True, 1
            elif b_type == 'black' and color == 'black':
                won, multiplier = True, 1
            elif b_type == 'odd' and is_odd:
                won, multiplier = True, 1
            elif b_type == 'even' and is_even:
                won, multiplier = True, 1
            elif b_type == 'low' and is_low:
                won, multiplier = True, 1
            elif b_type == 'high' and is_high:
                won, multiplier = True, 1

            if won:
                payout = amt + (amt * multiplier)
                total_won += payout

        if total_won > 0:
            user['coins'] += total_won
            add_transaction(db, user['id'], user['name'], total_won, f"Gewinn Roulette (Runde #{roulette['round_id']})")
            
            for sid in sids_for_user(user['id']):
                socketio.emit('roulette:coins', user['coins'], room=sid)

    db['round_history'].append({
        "number": win_num,
        "color": color,
        "ts": int(time.time() * 1000)
    })
    db['round_history'] = db['round_history'][-30:]
    record_coin_snapshot(db)
    save_db(db)


def roulette_game_loop():
    while True:
        with db_lock:
            settings = get_settings(load_db())

        # --- SETZPHASE ---
        with roulette_lock:
            roulette['phase'] = 'betting'
            roulette['bets'] = {}
            roulette['result_number'] = None
            roulette['ends_at'] = time.time() + settings['betting_seconds']

        socketio.emit('roulette:state', public_roulette_state(), room='global')
        time.sleep(settings['betting_seconds'])

        # --- ROLLPHASE ---
        result_number = random.randint(0, 36)
        with roulette_lock:
            roulette['phase'] = 'spinning'
            roulette['result_number'] = result_number
            roulette['ends_at'] = time.time() + settings['spin_seconds']

        socketio.emit('roulette:state', public_roulette_state(), room='global')
        time.sleep(settings['spin_seconds'])

        # --- GEWINNAUSZAHLUNG ---
        with db_lock:
            db = load_db()
            run_round_payouts(db, result_number)

        with roulette_lock:
            roulette['phase'] = 'result'
            roulette['ends_at'] = time.time() + settings['result_seconds']

        socketio.emit('roulette:state', public_roulette_state(), room='global')
        with db_lock:
            db = load_db()
            history = db['round_history'][-20:]
        socketio.emit('roulette:history', history, room='global')
        broadcast_dashboard()
        broadcast_users()
        
        time.sleep(settings['result_seconds'])
        
        with roulette_lock:
            roulette['round_id'] += 1


# ============ MAIN APP RUNNER ============

if __name__ == '__main__':
    with db_lock:
        load_db()
    
    # Startet den Roulette-Loop im Hintergrund thread-safe
    socketio.start_background_task(roulette_game_loop)
    
    # Port dynamisch für Render auslesen
    port = int(os.environ.get('PORT', 3000))
    socketio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)