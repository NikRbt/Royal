from flask import Flask, send_from_directory, request, jsonify
from flask_socketio import SocketIO, emit
import os
import uuid

app = Flask(__name__, static_folder='.', template_folder='templates')
app.config['SECRET_KEY'] = 'roulette-secret'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

online = {}
active_games = {}

@app.route('/')
def index():
    return send_from_directory('templates', 'index.html')

@app.route('/blackjack.html')
def blackjack():
    return send_from_directory('templates', 'blackjack.html')

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name: return jsonify({"error": "Name fehlt"}), 400
    return jsonify({"user": {"id": uuid.uuid4().hex[:8], "name": name, "coins": 1000, "isAdmin": False}})

@socketio.on('identify')
def on_identify(user_id):
    online[request.sid] = {"userId": user_id, "name": "Spieler"}
    emit('bj:lobby', {"games": list(active_games.values())})

@socketio.on('bj:createGame')
def on_bj_create(data):
    me = online.get(request.sid)
    if not me: return emit('bj:error', "Nicht eingeloggt")
    
    bet = int(data.get('bet', 50))
    game_id = uuid.uuid4().hex[:8]
    
    game = {
        "id": game_id,
        "host": me["userId"],
        "players": [me["userId"]],
        "bet": bet,
        "state": "waiting"
    }
    active_games[game_id] = game
    emit('bj:gameState', game)
    socketio.emit('bj:lobby', {"games": list(active_games.values())})

if __name__ == '__main__':
    print("✅ Ready for Render")
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 3000)), allow_unsafe_werkzeug=True)