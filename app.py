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
    "durak_games": [],  # aktive Spiele {id, players:[{id,name,coins_bet}], state, deck, table, ...}
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


@app.route('/durak.html')
def durak_page():
    return send_from_directory('templates', 'durak.html')




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


# ============ DURAK CARD GAME ============

SUITS = ['♠', '♣', '♥', '♦']
RANKS = ['6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
RANK_ORDER = {'6': 6, '7': 7, '8': 8, '9': 9, '10': 10, 'J': 11, 'Q': 12, 'K': 13, 'A': 14}

def make_deck():
    """Erstelle ein 36er-Deck (6 bis Ace)"""
    return [{'suit': s, 'rank': r} for s in SUITS for r in RANKS]

def card_to_str(card):
    return f"{card['rank']}{card['suit']}"

def str_to_card(s):
    if len(s) < 2:
        return None
    suit = s[-1]
    rank = s[:-1]
    if suit in SUITS and rank in RANKS:
        return {'suit': suit, 'rank': rank}
    return None

def can_beat(attacker_card, defender_card, trump_suit):
    """Kann defender_card die attacker_card schlagen?"""
    if defender_card['suit'] == trump_suit and attacker_card['suit'] != trump_suit:
        return True
    if defender_card['suit'] != attacker_card['suit']:
        return False
    return RANK_ORDER[defender_card['rank']] > RANK_ORDER[attacker_card['rank']]

def can_play_as_attack(card, table, hand):
    """Kann diese Karte als Angriff gespielt werden?"""
    if not table:  # Erste Karte des Angriffs
        return True
    # Danach nur Karten mit Rängen die bereits auf dem Tisch liegen
    valid_ranks = set()
    for attack, defense in table:
        valid_ranks.add(attack['rank'])
        if defense:
            valid_ranks.add(defense['rank'])
    return card['rank'] in valid_ranks

def create_game_object(game_id, players, bet_amount):
    """Neues Durak-Spiel-Objekt erstellen"""
    deck = make_deck()
    import random
    random.shuffle(deck)
    
    attacker_idx = 0
    defender_idx = 1
    
    player_hands = {}
    for i, p in enumerate(players):
        player_hands[p['id']] = []
    
    # Karten verteilen (6 an jeden)
    for _ in range(6):
        for i, p in enumerate(players):
            if deck:
                player_hands[p['id']].append(deck.pop())
    
    return {
        "id": game_id,
        "players": players,  # [{id, name, coins_bet}]
        "player_hands": player_hands,  # userId -> [cards]
        "attacker_idx": attacker_idx,
        "defender_idx": defender_idx,
        "deck": deck,
        "trump_card": deck[-1] if deck else None,
        "table": [],  # [(attack_card, defense_card), ...]
        "state": "playing",  # playing | game_over
        "message": "",
        "created_at": int(time.time() * 1000),
        "bet_amount": bet_amount,
    }

def get_durak_game(db, game_id):
    return next((g for g in db['durak_games'] if g['id'] == game_id), None)

def draw_cards_to_player(game, player_idx, from_attacker=False):
    """Verteile Karten vom Deck an Spieler"""
    player_id = game['players'][player_idx]['id']
    target_count = 6
    draw_from_idx = game['attacker_idx'] if not from_attacker else game['defender_idx']
    
    while len(game['player_hands'][player_id]) < target_count and game['deck']:
        game['player_hands'][player_id].append(game['deck'].pop())

def get_public_game_state(game):
    """Gebe Game-State für Clients zurück (keine verdeckten Karten)"""
    return {
        "id": game['id'],
        "players": [{"id": p['id'], "name": p['name'], "card_count": len(game['player_hands'][p['id']])} for p in game['players']],
        "attacker_idx": game['attacker_idx'],
        "defender_idx": game['defender_idx'],
        "table": game['table'],
        "trump_card": game['trump_card'],
        "state": game['state'],
        "message": game['message'],
    }

def broadcast_durak_games():
    """Alle verfügbaren Lobbys broadcasten"""
    with db_lock:
        db = load_db()
        lobbies = [g for g in db['durak_games'] if g['state'] == 'waiting']
    socketio.emit('durak:lobbies', [
        {
            "id": g['id'],
            "players": g['players'],
            "player_count": len(g['players']),
            "max_players": 4,
            "bet_amount": g['bet_amount'],
        }
        for g in lobbies
    ])

# ============ DURAK SOCKET EVENTS ============

@socketio.on('durak:listLobbies')
def on_durak_list_lobbies():
    broadcast_durak_games()

@socketio.on('durak:createGame')
def on_durak_create_game(data):
    me = online.get(request.sid)
    if not me:
        return
    bet_amount = int((data or {}).get('bet_amount', 0))
    if bet_amount <= 0:
        return
    
    with db_lock:
        db = load_db()
        user = get_user(db, me['userId'])
        if not user or user['coins'] < bet_amount:
            emit('durak:error', "Nicht genug Coins für diesen Einsatz")
            return
        
        game_id = uuid.uuid4().hex[:8]
        game = create_game_object(game_id, [{"id": me['userId'], "name": me['name'], "coins_bet": bet_amount}], bet_amount)
        game['state'] = 'waiting'
        db['durak_games'].append(game)
        save_db(db)
    
    broadcast_durak_games()
    emit('durak:gameCreated', {"game_id": game_id})

@socketio.on('durak:joinGame')
def on_durak_join_game(data):
    me = online.get(request.sid)
    if not me:
        return
    game_id = (data or {}).get('game_id')
    
    with db_lock:
        db = load_db()
        game = get_durak_game(db, game_id)
        if not game:
            emit('durak:error', "Spiel nicht gefunden")
            return
        if game['state'] != 'waiting':
            emit('durak:error', "Spiel hat bereits gestartet")
            return
        if len(game['players']) >= 4:
            emit('durak:error', "Spiel ist voll")
            return
        
        bet_amount = game['bet_amount']
        user = get_user(db, me['userId'])
        if not user or user['coins'] < bet_amount:
            emit('durak:error', "Nicht genug Coins für diesen Einsatz")
            return
        
        # Spieler zum Spiel hinzufügen
        if not any(p['id'] == me['userId'] for p in game['players']):
            game['players'].append({"id": me['userId'], "name": me['name'], "coins_bet": bet_amount})
            game['player_hands'][me['userId']] = []
            
            # Karten verteilen wenn der 2. Spieler beitritt
            if len(game['players']) >= 2:
                deck = game['deck']
                for _ in range(6):
                    for p in game['players']:
                        if deck:
                            game['player_hands'][p['id']].append(deck.pop())
                game['state'] = 'playing'
            
            save_db(db)
    
    broadcast_durak_games()
    socketio.emit('durak:gameUpdated', {"game_id": game_id}, room=f"durak-{game_id}")
    for p in game['players']:
        for sid in sids_for_user(p['id']):
            socketio.emit('durak:gameState', get_public_game_state(game), room=sid)
            socketio.emit('durak:myHand', game['player_hands'][p['id']], room=sid)

@socketio.on('durak:playCard')
def on_durak_play_card(data):
    me = online.get(request.sid)
    if not me:
        return
    game_id = (data or {}).get('game_id')
    card_str = (data or {}).get('card')
    
    card = str_to_card(card_str)
    if not card:
        return
    
    with db_lock:
        db = load_db()
        game = get_durak_game(db, game_id)
        if not game or game['state'] != 'playing':
            return
        
        player_idx = next((i for i, p in enumerate(game['players']) if p['id'] == me['userId']), None)
        if player_idx is None:
            return
        
        if player_idx != game['attacker_idx']:
            emit('durak:error', "Du bist nicht der Angreifer")
            return
        
        if card not in game['player_hands'][me['userId']]:
            emit('durak:error', "Diese Karte hast du nicht")
            return
        
        if len(game['table']) >= 6:
            emit('durak:error', "Zu viele Karten auf dem Tisch")
            return
        
        if not can_play_as_attack(card, game['table'], game['player_hands'][me['userId']]):
            emit('durak:error', "Ungültige Angriffskarte")
            return
        
        game['player_hands'][me['userId']].remove(card)
        game['table'].append([card, None])
        save_db(db)
        
        game['message'] = f"{me['name']} spielte {card_to_str(card)}"
        broadcast_to_game_players(game)

@socketio.on('durak:defendCard')
def on_durak_defend_card(data):
    me = online.get(request.sid)
    if not me:
        return
    game_id = (data or {}).get('game_id')
    attack_idx = int((data or {}).get('attack_idx', -1))
    defend_card_str = (data or {}).get('card')
    
    defend_card = str_to_card(defend_card_str)
    if not defend_card or attack_idx < 0:
        return
    
    with db_lock:
        db = load_db()
        game = get_durak_game(db, game_id)
        if not game or game['state'] != 'playing':
            return
        
        player_idx = next((i for i, p in enumerate(game['players']) if p['id'] == me['userId']), None)
        if player_idx is None or player_idx != game['defender_idx']:
            emit('durak:error', "Du bist nicht der Verteidiger")
            return
        
        if defend_card not in game['player_hands'][me['userId']]:
            emit('durak:error', "Diese Karte hast du nicht")
            return
        
        if attack_idx >= len(game['table']):
            emit('durak:error', "Ungültiger Angriff")
            return
        
        attack_card = game['table'][attack_idx][0]
        if not can_beat(attack_card, defend_card, game['trump_card']['suit']):
            emit('durak:error', "Diese Karte schlägt nicht")
            return
        
        game['player_hands'][me['userId']].remove(defend_card)
        game['table'][attack_idx][1] = defend_card
        save_db(db)
        
        game['message'] = f"{me['name']} schlug ab mit {card_to_str(defend_card)}"
        broadcast_to_game_players(game)

@socketio.on('durak:takeCards')
def on_durak_take_cards(data):
    me = online.get(request.sid)
    if not me:
        return
    game_id = (data or {}).get('game_id')
    
    with db_lock:
        db = load_db()
        game = get_durak_game(db, game_id)
        if not game or game['state'] != 'playing':
            return
        
        player_idx = next((i for i, p in enumerate(game['players']) if p['id'] == me['userId']), None)
        if player_idx != game['defender_idx']:
            return
        
        for attack, defend in game['table']:
            game['player_hands'][me['userId']].append(attack)
            if defend:
                game['player_hands'][me['userId']].append(defend)
        
        game['table'] = []
        game['message'] = f"{me['name']} hat abgenommen 😞"
        
        attacker = game['attacker_idx']
        game['defender_idx'] = (game['defender_idx'] + 1) % len(game['players'])
        game['attacker_idx'] = attacker
        
        check_game_end(game)
        save_db(db)
        broadcast_to_game_players(game)

@socketio.on('durak:endRound')
def on_durak_end_round(data):
    me = online.get(request.sid)
    if not me:
        return
    game_id = (data or {}).get('game_id')
    
    with db_lock:
        db = load_db()
        game = get_durak_game(db, game_id)
        if not game or game['state'] != 'playing':
            return
        
        if me['userId'] != game['players'][game['attacker_idx']]['id']:
            return
        
        game['table'] = []
        old_attacker = game['attacker_idx']
        game['attacker_idx'] = game['defender_idx']
        game['defender_idx'] = (game['defender_idx'] + 1) % len(game['players'])
        
        for i in range(len(game['players'])):
            draw_cards_to_player(game, i)
        
        check_game_end(game)
        save_db(db)
        broadcast_to_game_players(game)

def check_game_end(game):
    """Prüfe ob jemand alle Karten aufgebraucht hat"""
    players_with_cards = [p for p in game['players'] if game['player_hands'][p['id']]]
    if len(players_with_cards) == 1:
        game['state'] = 'game_over'
        loser = players_with_cards[0]
        game['message'] = f"{loser['name']} ist der DURAK! 🤦"

def broadcast_to_game_players(game):
    """Spiel-State an alle Spieler des Spiels schicken"""
    state = get_public_game_state(game)
    for p in game['players']:
        for sid in sids_for_user(p['id']):
            socketio.emit('durak:gameState', state, room=sid)
            socketio.emit('durak:myHand', game['player_hands'][p['id']], room=sid)



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


# ============ DURAK (Kartenspiel, klassische Variante, 2-4 Spieler) ============

DURAK_SUITS = ['S', 'H', 'D', 'C']
DURAK_SUIT_SYMBOL = {'S': '♠', 'H': '♥', 'D': '♦', 'C': '♣'}
DURAK_RANK_LABEL = {6: '6', 7: '7', 8: '8', 9: '9', 10: '10', 11: 'J', 12: 'Q', 13: 'K', 14: 'A'}
DURAK_MAX_SEATS = 4
DURAK_HAND_SIZE = 6

durak_lock = Lock()
durak_tables = {}  # table_id -> table dict


def durak_build_deck():
    deck = [{"r": r, "s": s} for s in DURAK_SUITS for r in range(6, 15)]
    random.shuffle(deck)
    return deck


def durak_card_label(card):
    return f"{DURAK_RANK_LABEL[card['r']]}{DURAK_SUIT_SYMBOL[card['s']]}"


def durak_card_eq(a, b):
    return a['r'] == b['r'] and a['s'] == b['s']


def durak_beats(attack, defense, trump_suit):
    if defense['s'] == attack['s'] and defense['r'] > attack['r']:
        return True
    if defense['s'] == trump_suit and attack['s'] != trump_suit:
        return True
    return False


def durak_new_table(host_id, host_name, stake):
    table_id = uuid.uuid4().hex[:8]
    table = {
        "id": table_id,
        "stake": stake,
        "status": "waiting",  # waiting | playing | finished
        "seats": [{"userId": host_id, "name": host_name}],
        "hands": {host_id: []},
        "deck": [],
        "trump_suit": None,
        "trump_card": None,
        "table_cards": [],     # [{"attack": card, "defense": card|None}]
        "order": [],
        "attacker": None,
        "defender": None,
        "finished_order": [],
        "durak": None,
        "pot": stake,
        "log": [f"{host_name} hat den Tisch eröffnet (Einsatz {stake} 🪙)."],
    }
    durak_tables[table_id] = table
    return table


def durak_public_table_list():
    return [
        {
            "id": t['id'], "stake": t['stake'], "status": t['status'],
            "players": [s['name'] for s in t['seats']],
            "seatCount": len(t['seats']), "maxSeats": DURAK_MAX_SEATS,
        }
        for t in durak_tables.values() if t['status'] == 'waiting'
    ]


def durak_broadcast_table_list():
    socketio.emit('durak:tablesUpdate', durak_public_table_list())


def durak_active_players(table):
    return [uid for uid in table['order'] if uid not in table['finished_order'] and uid != table['durak']]


def durak_next_active_index(table, idx):
    n = len(table['order'])
    for i in range(1, n + 1):
        cand = (idx + i) % n
        uid = table['order'][cand]
        if uid not in table['finished_order'] and uid != table['durak']:
            return cand
    return idx


def durak_refill_hands(table, start_idx):
    n = len(table['order'])
    for i in range(n):
        uid = table['order'][(start_idx + i) % n]
        if uid in table['finished_order'] or uid == table['durak']:
            continue
        hand = table['hands'][uid]
        while len(hand) < DURAK_HAND_SIZE and table['deck']:
            hand.append(table['deck'].pop())


def durak_check_finished(table):
    for uid in list(table['order']):
        if uid in table['finished_order'] or uid == table['durak']:
            continue
        if not table['deck'] and len(table['hands'][uid]) == 0:
            table['finished_order'].append(uid)
            name = next(s['name'] for s in table['seats'] if s['userId'] == uid)
            table['log'].append(f"{name} hat keine Karten mehr und ist raus!")

    active = durak_active_players(table)
    if table['status'] == 'playing' and not table['deck'] and len(active) == 1:
        table['durak'] = active[0]
        table['status'] = 'finished'
        durak_finish_game(table)


def durak_finish_game(table):
    name = next(s['name'] for s in table['seats'] if s['userId'] == table['durak'])
    table['log'].append(f"😭 {name} ist der Durak und verliert den Einsatz.")
    winners = list(table['finished_order'])
    if not winners:
        winners = [uid for uid in table['order'] if uid != table['durak']]
    pot = table['pot']
    share = pot // len(winners) if winners else 0
    remainder = pot - share * len(winners)

    with db_lock:
        db = load_db()
        for i, uid in enumerate(winners):
            user = get_user(db, uid)
            if not user:
                continue
            amount = share + (remainder if i == 0 else 0)
            if amount > 0:
                user['coins'] += amount
                add_transaction(db, user['id'], user['name'], amount, "Durak-Gewinn")
        save_db(db)
    broadcast_users()
    broadcast_dashboard()


def durak_start_table(table):
    table['status'] = 'playing'
    table['order'] = [s['userId'] for s in table['seats']]
    for uid in table['order']:
        table['hands'][uid] = []
    table['deck'] = durak_build_deck()
    durak_refill_hands(table, 0)
    table['trump_card'] = table['deck'][0] if table['deck'] else table['hands'][table['order'][0]][0]
    table['trump_suit'] = table['trump_card']['s']
    table['attacker'] = table['order'][0]
    def_idx = durak_next_active_index(table, 0)
    table['defender'] = table['order'][def_idx]
    table['table_cards'] = []
    names = [s['name'] for s in table['seats']]
    table['log'].append(f"🎮 Spiel gestartet mit {', '.join(names)}. Trumpf: {durak_card_label(table['trump_card'])}")


def durak_view_for(table, viewer_id):
    seats_view = []
    for s in table['seats']:
        hand = table['hands'].get(s['userId'], [])
        seats_view.append({
            "userId": s['userId'], "name": s['name'],
            "handCount": len(hand),
            "hand": hand if s['userId'] == viewer_id else None,
            "isAttacker": s['userId'] == table['attacker'],
            "isDefender": s['userId'] == table['defender'],
            "finished": s['userId'] in table['finished_order'],
            "isDurak": s['userId'] == table['durak'],
        })
    return {
        "id": table['id'],
        "stake": table['stake'],
        "pot": table['pot'],
        "status": table['status'],
        "seats": seats_view,
        "deckCount": len(table['deck']),
        "trumpCard": table['trump_card'],
        "trumpSuit": table['trump_suit'],
        "tableCards": table['table_cards'],
        "attacker": table['attacker'],
        "defender": table['defender'],
        "durak": table['durak'],
        "log": table['log'][-12:],
        "youAreAttacker": viewer_id == table['attacker'],
        "youAreDefender": viewer_id == table['defender'],
    }


def durak_broadcast_state(table):
    for s in table['seats']:
        view = durak_view_for(table, s['userId'])
        for sid in sids_for_user(s['userId']):
            socketio.emit('durak:state', view, room=sid)


def durak_get_table_or_error(table_id):
    table = durak_tables.get(table_id)
    if not table:
        emit('durak:error', "Tisch nicht gefunden.")
        return None
    return table


@socketio.on('durak:listTables')
def on_durak_list():
    emit('durak:tablesUpdate', durak_public_table_list())


@socketio.on('durak:createTable')
def on_durak_create(data):
    me = online.get(request.sid)
    if not me:
        return
    try:
        stake = int((data or {}).get('stake', 0))
    except (TypeError, ValueError):
        stake = 0
    if stake <= 0:
        emit('durak:error', "Ungültiger Einsatz.")
        return

    with db_lock:
        db = load_db()
        user = get_user(db, me['userId'])
        if not user or user['coins'] < stake:
            emit('durak:error', "Nicht genug Coins für diesen Einsatz.")
            return
        user['coins'] -= stake
        add_transaction(db, user['id'], user['name'], -stake, "Durak-Einsatz (Tisch erstellt)")
        save_db(db)
        new_coins = user['coins']

    with durak_lock:
        table = durak_new_table(me['userId'], me['name'], stake)

    emit('roulette:coins', new_coins)
    emit('durak:joined', {"tableId": table['id']})
    durak_broadcast_state(table)
    durak_broadcast_table_list()
    broadcast_users()


@socketio.on('durak:joinTable')
def on_durak_join(data):
    me = online.get(request.sid)
    if not me:
        return
    table_id = (data or {}).get('tableId')
    with durak_lock:
        table = durak_get_table_or_error(table_id)
        if not table:
            return
        if table['status'] != 'waiting':
            emit('durak:error', "Dieser Tisch läuft bereits oder ist beendet.")
            return
        if len(table['seats']) >= DURAK_MAX_SEATS:
            emit('durak:error', "Tisch ist voll.")
            return
        if any(s['userId'] == me['userId'] for s in table['seats']):
            emit('durak:error', "Du sitzt bereits an diesem Tisch.")
            return
        stake = table['stake']

    with db_lock:
        db = load_db()
        user = get_user(db, me['userId'])
        if not user or user['coins'] < stake:
            emit('durak:error', "Nicht genug Coins für diesen Einsatz.")
            return
        user['coins'] -= stake
        add_transaction(db, user['id'], user['name'], -stake, "Durak-Einsatz (Tisch beigetreten)")
        save_db(db)
        new_coins = user['coins']

    with durak_lock:
        table['seats'].append({"userId": me['userId'], "name": me['name']})
        table['hands'][me['userId']] = []
        table['pot'] += stake
        table['log'].append(f"{me['name']} ist dem Tisch beigetreten ({len(table['seats'])}/{DURAK_MAX_SEATS}).")

    emit('roulette:coins', new_coins)
    emit('durak:joined', {"tableId": table['id']})
    durak_broadcast_state(table)
    durak_broadcast_table_list()
    broadcast_users()


@socketio.on('durak:leaveTable')
def on_durak_leave(data):
    me = online.get(request.sid)
    if not me:
        return
    table_id = (data or {}).get('tableId')
    with durak_lock:
        table = durak_tables.get(table_id)
        if not table:
            return
        if table['status'] != 'waiting':
            # Nach Spielende einfach aus der Ansicht entfernen, kein Refund mehr nötig
            if table['status'] == 'finished':
                table['seats'] = [s for s in table['seats'] if s['userId'] != me['userId']]
                if not table['seats']:
                    durak_tables.pop(table_id, None)
            return
        was_host = table['seats'][0]['userId'] == me['userId']
        table['seats'] = [s for s in table['seats'] if s['userId'] != me['userId']]
        stake = table['stake']

    with db_lock:
        db = load_db()
        user = get_user(db, me['userId'])
        if user:
            user['coins'] += stake
            add_transaction(db, user['id'], user['name'], stake, "Durak-Einsatz zurückerstattet (Tisch verlassen)")
            save_db(db)
            new_coins = user['coins']
            emit('roulette:coins', new_coins)

    with durak_lock:
        if not table['seats'] or was_host:
            # Tisch ohne Gastgeber/Spieler -> auflösen, restliche Einsätze zurückerstatten
            remaining = list(table['seats'])
            table['seats'] = []
            durak_tables.pop(table_id, None)
            with db_lock:
                db = load_db()
                for s in remaining:
                    u = get_user(db, s['userId'])
                    if u:
                        u['coins'] += stake
                        add_transaction(db, u['id'], u['name'], stake, "Durak-Einsatz zurückerstattet (Tisch aufgelöst)")
                save_db(db)
            for s in remaining:
                for sid in sids_for_user(s['userId']):
                    socketio.emit('durak:tableClosed', {"tableId": table_id}, room=sid)

    broadcast_users()
    durak_broadcast_table_list()


@socketio.on('durak:startGame')
def on_durak_start(data):
    me = online.get(request.sid)
    if not me:
        return
    table_id = (data or {}).get('tableId')
    with durak_lock:
        table = durak_get_table_or_error(table_id)
        if not table:
            return
        if table['seats'][0]['userId'] != me['userId']:
            emit('durak:error', "Nur der Gastgeber kann das Spiel starten.")
            return
        if table['status'] != 'waiting':
            return
        if len(table['seats']) < 2:
            emit('durak:error', "Mindestens 2 Spieler nötig.")
            return
        durak_start_table(table)

    durak_broadcast_state(table)
    durak_broadcast_table_list()


@socketio.on('durak:playAttack')
def on_durak_attack(data):
    me = online.get(request.sid)
    if not me:
        return
    table_id = (data or {}).get('tableId')
    card = (data or {}).get('card')
    with durak_lock:
        table = durak_get_table_or_error(table_id)
        if not table or table['status'] != 'playing':
            return
        if table['attacker'] != me['userId']:
            emit('durak:error', "Du bist nicht am Zug (Angreifer).")
            return
        if table['table_cards'] and table['table_cards'][-1]['defense'] is None:
            emit('durak:error', "Warte, bis die letzte Karte abgewehrt oder aufgenommen wurde.")
            return
        if len(table['table_cards']) >= DURAK_HAND_SIZE:
            emit('durak:error', "Maximale Anzahl Angriffe für diese Runde erreicht.")
            return
        hand = table['hands'][me['userId']]
        match_idx = next((i for i, c in enumerate(hand) if durak_card_eq(c, card)), None)
        if match_idx is None:
            emit('durak:error', "Diese Karte hast du nicht auf der Hand.")
            return
        if table['table_cards']:
            ranks_on_table = {c['attack']['r'] for c in table['table_cards']} | {c['defense']['r'] for c in table['table_cards'] if c['defense']}
            if card['r'] not in ranks_on_table:
                emit('durak:error', "Diese Karte passt im Rang nicht zu den Karten auf dem Tisch.")
                return
        played = hand.pop(match_idx)
        table['table_cards'].append({"attack": played, "defense": None})
        table['log'].append(f"{me['name']} greift mit {durak_card_label(played)} an.")

    durak_broadcast_state(table)


@socketio.on('durak:playDefense')
def on_durak_defense(data):
    me = online.get(request.sid)
    if not me:
        return
    table_id = (data or {}).get('tableId')
    card = (data or {}).get('card')
    with durak_lock:
        table = durak_get_table_or_error(table_id)
        if not table or table['status'] != 'playing':
            return
        if table['defender'] != me['userId']:
            emit('durak:error', "Du bist nicht am Zug (Verteidiger).")
            return
        if not table['table_cards'] or table['table_cards'][-1]['defense'] is not None:
            emit('durak:error', "Es gibt aktuell nichts abzuwehren.")
            return
        pending = table['table_cards'][-1]['attack']
        hand = table['hands'][me['userId']]
        match_idx = next((i for i, c in enumerate(hand) if durak_card_eq(c, card)), None)
        if match_idx is None:
            emit('durak:error', "Diese Karte hast du nicht auf der Hand.")
            return
        if not durak_beats(pending, card, table['trump_suit']):
            emit('durak:error', "Diese Karte schlägt den Angriff nicht.")
            return
        played = hand.pop(match_idx)
        table['table_cards'][-1]['defense'] = played
        table['log'].append(f"{me['name']} wehrt mit {durak_card_label(played)} ab.")

    durak_broadcast_state(table)


@socketio.on('durak:takeCards')
def on_durak_take(data):
    me = online.get(request.sid)
    if not me:
        return
    table_id = (data or {}).get('tableId')
    with durak_lock:
        table = durak_get_table_or_error(table_id)
        if not table or table['status'] != 'playing':
            return
        if table['defender'] != me['userId']:
            emit('durak:error', "Nur der Verteidiger kann Karten aufnehmen.")
            return
        if not table['table_cards']:
            return

        hand = table['hands'][me['userId']]
        for pair in table['table_cards']:
            hand.append(pair['attack'])
            if pair['defense']:
                hand.append(pair['defense'])
        table['log'].append(f"{me['name']} nimmt alle Karten vom Tisch auf.")
        table['table_cards'] = []

        attacker_idx = table['order'].index(table['attacker'])
        durak_refill_hands(table, attacker_idx)
        durak_check_finished(table)

        if table['status'] == 'playing':
            defender_idx = table['order'].index(table['defender'])
            new_attacker_idx = durak_next_active_index(table, defender_idx)
            table['attacker'] = table['order'][new_attacker_idx]
            table['defender'] = table['order'][durak_next_active_index(table, new_attacker_idx)]

    durak_broadcast_state(table)
    if table['status'] == 'finished':
        durak_broadcast_table_list()
        broadcast_users()


@socketio.on('durak:pass')
def on_durak_pass(data):
    me = online.get(request.sid)
    if not me:
        return
    table_id = (data or {}).get('tableId')
    with durak_lock:
        table = durak_get_table_or_error(table_id)
        if not table or table['status'] != 'playing':
            return
        if table['attacker'] != me['userId']:
            emit('durak:error', "Nur der Angreifer kann die Runde abschließen.")
            return
        if not table['table_cards'] or table['table_cards'][-1]['defense'] is None:
            emit('durak:error', "Es gibt noch eine unverteidigte Karte.")
            return

        table['log'].append(f"{me['name']} schließt den Angriff ab — alle Karten wurden abgewehrt.")
        table['table_cards'] = []

        attacker_idx = table['order'].index(table['attacker'])
        durak_refill_hands(table, attacker_idx)
        durak_check_finished(table)

        if table['status'] == 'playing':
            defender_idx = table['order'].index(table['defender'])
            new_attacker_idx = defender_idx
            table['attacker'] = table['order'][new_attacker_idx]
            table['defender'] = table['order'][durak_next_active_index(table, new_attacker_idx)]

    durak_broadcast_state(table)
    if table['status'] == 'finished':
        durak_broadcast_table_list()
        broadcast_users()


if __name__ == '__main__':
    load_db()
    socketio.start_background_task(roulette_game_loop)
    port = int(os.environ.get('PORT', 3000))
    print(f"✅ Roulette App läuft auf http://localhost:{port}")
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
