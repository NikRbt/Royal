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


app = Flask(__name__, static_folder='static', template_folder='templates')
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


@app.route('/')
def index():
    return send_from_directory('templates', 'index.html')


@app.route('/admin.html')
def admin_page():
    return send_from_directory('templates', 'admin.html')


@app.route('/dashboard.html')
def dashboard_page():
    return send_from_directory('templates', 'dashboard.html')


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
            # Neuer Benutzer -> Account wird mit diesem Passwort angelegt
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
            # Migration: älterer Account ohne Passwort -> jetzt damit absichern
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
            # Alle Einsätze dieser Runde zurücknehmen
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


# ============ ROULETTE GAME LOOP ============

def payout_multiplier(bet_type):
    return {
        'number': 35,
        'red': 1, 'black': 1,
        'odd': 1, 'even': 1,
        'low': 1, 'high': 1,
    }.get(bet_type, 0)


def bet_wins(bet, result_number):
    color = number_color(result_number)
    t, v = bet['type'], bet['value']
    if t == 'number':
        return v == result_number
    if t == 'red':
        return color == 'red'
    if t == 'black':
        return color == 'black'
    if t == 'odd':
        return result_number != 0 and result_number % 2 == 1
    if t == 'even':
        return result_number != 0 and result_number % 2 == 0
    if t == 'low':
        return 1 <= result_number <= 18
    if t == 'high':
        return 19 <= result_number <= 36
    return False


def run_round_payouts(result_number):
    with roulette_lock:
        bets_snapshot = {uid: list(b) for uid, b in roulette['bets'].items()}

    with db_lock:
        db = load_db()
        for user_id, bets in bets_snapshot.items():
            user = get_user(db, user_id)
            if not user:
                continue
            total_win = 0
            for bet in bets:
                if bet_wins(bet, result_number):
                    mult = payout_multiplier(bet['type'])
                    win_amount = bet['amount'] * (mult + 1)  # Einsatz zurück + Gewinn
                    total_win += win_amount
            if total_win > 0:
                user['coins'] += total_win
                add_transaction(db, user['id'], user['name'], total_win, f"Gewinn Roulette (Zahl {result_number})")

        db['round_history'].append({
            "number": result_number, "color": number_color(result_number), "ts": int(time.time() * 1000)
        })
        db['round_history'] = db['round_history'][-30:]
        record_coin_snapshot(db)
        save_db(db)


def roulette_game_loop():
    while True:
        with db_lock:
            settings = get_settings(load_db())

        # --- BETTING PHASE ---
        with roulette_lock:
            roulette['phase'] = 'betting'
            roulette['bets'] = {}
            roulette['result_number'] = None
            roulette['ends_at'] = time.time() + settings['betting_seconds']
        socketio.emit('roulette:state', public_roulette_state(), room='global')
        time.sleep(settings['betting_seconds'])

        with db_lock:
            settings = get_settings(load_db())

        # --- SPINNING PHASE ---
        result_number = random.randint(0, 36)
        with roulette_lock:
            roulette['phase'] = 'spinning'
            roulette['result_number'] = result_number
            roulette['ends_at'] = time.time() + settings['spin_seconds']
        socketio.emit('roulette:state', public_roulette_state(), room='global')
        time.sleep(settings['spin_seconds'])

        with db_lock:
            settings = get_settings(load_db())

        # --- PAYOUT & RESULT PHASE ---
        run_round_payouts(result_number)
        with roulette_lock:
            roulette['phase'] = 'result'
            roulette['ends_at'] = time.time() + settings['result_seconds']
            roulette['round_id'] += 1
        socketio.emit('roulette:state', public_roulette_state(), room='global')
        broadcast_users()
        broadcast_dashboard()
        time.sleep(settings['result_seconds'])


# ============ ADMIN API ============

def require_admin():
    user_id = request.headers.get('x-user-id')
    with db_lock:
        db = load_db()
        user = get_user(db, user_id)
    return user if user and user.get('isAdmin') else None


@app.route('/api/dashboard')
def dashboard_api():
    with db_lock:
        db = load_db()
    return jsonify(dashboard_payload(db))


@app.route('/api/admin/users')
def admin_users():
    if not require_admin():
        return jsonify({"error": "Kein Zugriff"}), 403
    with db_lock:
        db = load_db()
    return jsonify([public_user(u) for u in db['users']])


@app.route('/api/admin/transactions')
def admin_transactions():
    if not require_admin():
        return jsonify({"error": "Kein Zugriff"}), 403
    with db_lock:
        db = load_db()
    return jsonify(list(reversed(db['transactions'][-200:])))


@app.route('/api/admin/adjust-coins', methods=['POST'])
def admin_adjust_coins():
    if not require_admin():
        return jsonify({"error": "Kein Zugriff"}), 403
    data = request.json or {}
    user_id = data.get('userId')
    amount = int(data.get('amount', 0))
    reason = data.get('reason') or "Admin-Anpassung"

    with db_lock:
        db = load_db()
        user = get_user(db, user_id)
        if not user:
            return jsonify({"error": "User nicht gefunden"}), 404
        user['coins'] = max(0, user['coins'] + amount)
        add_transaction(db, user_id, user['name'], amount, reason)
        save_db(db)
        new_coins = user['coins']
    broadcast_users()
    broadcast_dashboard()
    return jsonify({"ok": True, "coins": new_coins})


@app.route('/api/admin/settings', methods=['GET'])
def admin_get_settings():
    if not require_admin():
        return jsonify({"error": "Kein Zugriff"}), 403
    with db_lock:
        db = load_db()
    return jsonify(get_settings(db))


@app.route('/api/admin/settings', methods=['POST'])
def admin_update_settings():
    if not require_admin():
        return jsonify({"error": "Kein Zugriff"}), 403
    data = request.json or {}
    with db_lock:
        db = load_db()
        settings = db.setdefault('settings', dict(DEFAULT_SETTINGS))
        for key in ('betting_seconds', 'spin_seconds', 'result_seconds'):
            if key in data:
                try:
                    val = int(data[key])
                    if 2 <= val <= 600:
                        settings[key] = val
                except (TypeError, ValueError):
                    pass
        if 'start_coins' in data:
            try:
                val = int(data['start_coins'])
                if 0 <= val <= 1_000_000:
                    settings['start_coins'] = val
            except (TypeError, ValueError):
                pass
        save_db(db)
        new_settings = get_settings(db)
    return jsonify({"ok": True, "settings": new_settings})


@app.route('/api/admin/set-admin', methods=['POST'])
def admin_set_admin():
    acting_admin = require_admin()
    if not acting_admin:
        return jsonify({"error": "Kein Zugriff"}), 403
    data = request.json or {}
    user_id = data.get('userId')
    is_admin = bool(data.get('isAdmin'))

    with db_lock:
        db = load_db()
        user = get_user(db, user_id)
        if not user:
            return jsonify({"error": "User nicht gefunden"}), 404
        if user['id'] == acting_admin['id'] and not is_admin:
            return jsonify({"error": "Du kannst dir selbst nicht die Admin-Rechte entziehen"}), 400
        user['isAdmin'] = is_admin
        save_db(db)
    broadcast_users()
    return jsonify({"ok": True})


@app.route('/api/admin/reset-password', methods=['POST'])
def admin_reset_password():
    if not require_admin():
        return jsonify({"error": "Kein Zugriff"}), 403
    data = request.json or {}
    user_id = data.get('userId')
    new_password = data.get('newPassword') or ''
    if len(new_password) < 4:
        return jsonify({"error": "Passwort muss mindestens 4 Zeichen haben"}), 400

    with db_lock:
        db = load_db()
        user = get_user(db, user_id)
        if not user:
            return jsonify({"error": "User nicht gefunden"}), 404
        user['password_hash'] = generate_password_hash(new_password)
        save_db(db)
    return jsonify({"ok": True})


@app.route('/api/admin/rename-user', methods=['POST'])
def admin_rename_user():
    if not require_admin():
        return jsonify({"error": "Kein Zugriff"}), 403
    data = request.json or {}
    user_id = data.get('userId')
    new_name = (data.get('newName') or '').strip()
    if not new_name or len(new_name) > 24:
        return jsonify({"error": "Ungültiger Name"}), 400

    with db_lock:
        db = load_db()
        user = get_user(db, user_id)
        if not user:
            return jsonify({"error": "User nicht gefunden"}), 404
        if get_user_by_name(db, new_name) and new_name.lower() != user['name'].lower():
            return jsonify({"error": "Name bereits vergeben"}), 400
        user['name'] = new_name
        save_db(db)
    broadcast_users()
    broadcast_dashboard()
    return jsonify({"ok": True})


@app.route('/api/admin/kick-user', methods=['POST'])
def admin_kick_user():
    if not require_admin():
        return jsonify({"error": "Kein Zugriff"}), 403
    data = request.json or {}
    user_id = data.get('userId')
    for sid in sids_for_user(user_id):
        socketio.emit('account:kicked', {}, room=sid)
        disconnect(sid=sid)
    online_ids_before = list(online.keys())
    for sid in online_ids_before:
        if online.get(sid, {}).get('userId') == user_id:
            online.pop(sid, None)
    broadcast_users()
    return jsonify({"ok": True})


@app.route('/api/admin/delete-user', methods=['POST'])
def admin_delete_user():
    acting_admin = require_admin()
    if not acting_admin:
        return jsonify({"error": "Kein Zugriff"}), 403
    data = request.json or {}
    user_id = data.get('userId')
    if user_id == acting_admin['id']:
        return jsonify({"error": "Du kannst deinen eigenen Account hier nicht löschen"}), 400

    with db_lock:
        db = load_db()
        before = len(db['users'])
        db['users'] = [u for u in db['users'] if u['id'] != user_id]
        if len(db['users']) == before:
            return jsonify({"error": "User nicht gefunden"}), 404
        save_db(db)

    for sid in sids_for_user(user_id):
        socketio.emit('account:deleted', {}, room=sid)
        disconnect(sid=sid)
    online_ids_before = list(online.keys())
    for sid in online_ids_before:
        if online.get(sid, {}).get('userId') == user_id:
            online.pop(sid, None)
    broadcast_users()
    broadcast_dashboard()
    return jsonify({"ok": True})


@app.route('/api/admin/broadcast', methods=['POST'])
def admin_broadcast():
    if not require_admin():
        return jsonify({"error": "Kein Zugriff"}), 403
    data = request.json or {}
    text = (data.get('text') or '').strip()
    if not text or len(text) > MAX_CHAT_LEN:
        return jsonify({"error": "Ungültige Nachricht"}), 400

    msg = {
        "id": uuid.uuid4().hex[:8],
        "userId": "system",
        "name": "📢 Admin-Ansage",
        "text": text,
        "ts": int(time.time() * 1000),
        "system": True,
    }
    with db_lock:
        db = load_db()
        db['global_chat'].append(msg)
        db['global_chat'] = db['global_chat'][-MAX_GLOBAL_HISTORY:]
        save_db(db)
    socketio.emit('chat:global', msg, room='global')
    return jsonify({"ok": True})


@app.route('/api/admin/stats')
def admin_stats():
    if not require_admin():
        return jsonify({"error": "Kein Zugriff"}), 403
    with db_lock:
        db = load_db()
    return jsonify({
        "total_users": len(db['users']),
        "online_users": len({v['userId'] for v in online.values()}),
        "total_coins": sum(u['coins'] for u in db['users']),
        "total_transactions": len(db['transactions']),
        "round_id": roulette['round_id'],
        "current_phase": roulette['phase'],
        "settings": get_settings(db),
    })


if __name__ == '__main__':
    load_db()
    socketio.start_background_task(roulette_game_loop)
    port = int(os.environ.get('PORT', 3000))
    print(f"✅ Roulette App läuft auf http://localhost:{port}")
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
