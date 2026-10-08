import hashlib
import hmac
import itertools
import json
import os
import secrets
import sqlite3
import sys
import time
from contextlib import contextmanager
from getpass import getpass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlparse


ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("SOTO_DB_PATH", ROOT / "soto.sqlite3"))
STARTING_BALANCE = 2480
MAX_INIT_DATA_AGE = 24 * 60 * 60
SYMBOLS = ("🍒", "⭐", "🍀", "🔔", "💎")
CARD_SUITS = ("♠", "♥", "♦", "♣")


class AuthenticationError(ValueError):
    pass


def verify_init_data(raw_data, bot_token, now=None):
    values = dict(parse_qsl(raw_data, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    if not received_hash:
        raise ValueError("Missing Telegram signature")

    try:
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("Invalid Telegram init data") from error

    current_time = int(time.time()) if now is None else int(now)
    if auth_date > current_time + 60 or current_time - auth_date > MAX_INIT_DATA_AGE:
        raise ValueError("Telegram session expired; reopen the app")

    check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received_hash, expected_hash):
        raise ValueError("Invalid Telegram signature")
    return {"id": user_id, "first_name": str(user.get("first_name", "Player"))[:64]}


def connect(db_path=DB_PATH):
    connection = sqlite3.connect(db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def database(db_path=DB_PATH):
    connection = connect(db_path)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database(db_path=DB_PATH):
    with database(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                first_name TEXT NOT NULL,
                display_name TEXT,
                balance INTEGER NOT NULL DEFAULT 2480 CHECK (balance >= 0)
            );
            CREATE TABLE IF NOT EXISTS rooms (
                code TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL REFERENCES users(telegram_id),
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS room_members (
                room_code TEXT NOT NULL REFERENCES rooms(code) ON DELETE CASCADE,
                telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                joined_at INTEGER NOT NULL,
                PRIMARY KEY (room_code, telegram_id)
            );
            CREATE TABLE IF NOT EXISTS poker_games (
                room_code TEXT PRIMARY KEY REFERENCES rooms(code) ON DELETE CASCADE,
                state TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS blackjack_hands (
                id TEXT PRIMARY KEY,
                telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                bet INTEGER NOT NULL,
                deck TEXT NOT NULL,
                player_cards TEXT NOT NULL,
                dealer_cards TEXT NOT NULL,
                status TEXT NOT NULL,
                payout INTEGER NOT NULL DEFAULT 0,
                result TEXT
            );
            """
        )
        user_columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
        if "display_name" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN display_name TEXT")


def load_bot_token(token_path=ROOT / "token.txt", environ=None):
    environment = os.environ if environ is None else environ
    token = environment.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        return token
    try:
        return token_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def get_or_create_user(connection, user):
    connection.execute(
        "INSERT OR IGNORE INTO users (telegram_id, first_name) VALUES (?, ?)",
        (user["id"], user["first_name"]),
    )
    connection.execute(
        "UPDATE users SET first_name = ? WHERE telegram_id = ?",
        (user["first_name"], user["id"]),
    )
    return connection.execute(
        "SELECT telegram_id, first_name, display_name, balance FROM users WHERE telegram_id = ?",
        (user["id"],),
    ).fetchone()


def blackjack_score(cards):
    total = sum(card["value"] for card in cards)
    aces = sum(card["rank"] == "A" for card in cards)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def blackjack_hand_data(connection, hand):
    player_cards = json.loads(hand["player_cards"])
    dealer_cards = json.loads(hand["dealer_cards"])
    status = hand["status"]
    visible_dealer_cards = dealer_cards if status != "playing" else dealer_cards[:1]
    return {
        "hand_id": hand["id"],
        "bet": hand["bet"],
        "status": status,
        "player_cards": player_cards,
        "dealer_cards": visible_dealer_cards,
        "dealer_hidden": status == "playing",
        "player_score": blackjack_score(player_cards),
        "dealer_score": blackjack_score(dealer_cards if status != "playing" else dealer_cards[:1]),
        "payout": hand["payout"],
        "result": hand["result"],
        "balance": connection.execute(
            "SELECT balance FROM users WHERE telegram_id = ?", (hand["telegram_id"],)
        ).fetchone()["balance"],
    }


def _complete_blackjack(connection, hand, deck, player_cards, dealer_cards, result, payout):
    connection.execute(
        "UPDATE users SET balance = balance + ? WHERE telegram_id = ?",
        (payout, hand["telegram_id"]),
    )
    connection.execute(
        "UPDATE blackjack_hands SET deck = ?, player_cards = ?, dealer_cards = ?, "
        "status = ?, payout = ?, result = ? WHERE id = ?",
        (
            json.dumps(deck),
            json.dumps(player_cards),
            json.dumps(dealer_cards),
            result,
            payout,
            result,
            hand["id"],
        ),
    )
    updated = connection.execute("SELECT * FROM blackjack_hands WHERE id = ?", (hand["id"],)).fetchone()
    return blackjack_hand_data(connection, updated)


def _resolve_blackjack(connection, hand, deck, player_cards, dealer_cards):
    player_score = blackjack_score(player_cards)
    if player_score > 21:
        return _complete_blackjack(connection, hand, deck, player_cards, dealer_cards, "lost", 0)
    while blackjack_score(dealer_cards) < 17:
        dealer_cards.append(deck.pop())
    dealer_score = blackjack_score(dealer_cards)
    if dealer_score > 21 or player_score > dealer_score:
        return _complete_blackjack(connection, hand, deck, player_cards, dealer_cards, "won", hand["bet"] * 2)
    if player_score == dealer_score:
        return _complete_blackjack(connection, hand, deck, player_cards, dealer_cards, "push", hand["bet"])
    return _complete_blackjack(connection, hand, deck, player_cards, dealer_cards, "lost", 0)


def start_blackjack(connection, user_id, bet, rng=secrets.SystemRandom()):
    if isinstance(bet, bool) or not isinstance(bet, int) or not 25 <= bet <= 500:
        raise ValueError("Bet must be between 25 and 500 play coins")
    existing = connection.execute(
        "SELECT * FROM blackjack_hands WHERE telegram_id = ? AND status = 'playing'",
        (user_id,),
    ).fetchone()
    if existing:
        return blackjack_hand_data(connection, existing)
    user = connection.execute("SELECT balance FROM users WHERE telegram_id = ?", (user_id,)).fetchone()
    if user["balance"] < bet:
        raise ValueError("Not enough play coins")

    deck = [
        {"rank": rank, "suit": suit, "value": value}
        for suit in CARD_SUITS
        for rank, value in (("A", 11), ("2", 2), ("3", 3), ("4", 4), ("5", 5), ("6", 6),
                            ("7", 7), ("8", 8), ("9", 9), ("10", 10), ("J", 10), ("Q", 10), ("K", 10))
    ]
    rng.shuffle(deck)
    player_cards = [deck.pop(), deck.pop()]
    dealer_cards = [deck.pop(), deck.pop()]
    connection.execute("UPDATE users SET balance = balance - ? WHERE telegram_id = ?", (bet, user_id))
    hand_id = secrets.token_urlsafe(12)
    connection.execute(
        "INSERT INTO blackjack_hands "
        "(id, telegram_id, bet, deck, player_cards, dealer_cards, status) VALUES (?, ?, ?, ?, ?, ?, 'playing')",
        (hand_id, user_id, bet, json.dumps(deck), json.dumps(player_cards), json.dumps(dealer_cards)),
    )
    hand = connection.execute("SELECT * FROM blackjack_hands WHERE id = ?", (hand_id,)).fetchone()
    player_score = blackjack_score(player_cards)
    dealer_score = blackjack_score(dealer_cards)
    if player_score == 21 or dealer_score == 21:
        if player_score == dealer_score:
            return _complete_blackjack(connection, hand, deck, player_cards, dealer_cards, "push", bet)
        if player_score == 21:
            return _complete_blackjack(connection, hand, deck, player_cards, dealer_cards, "won", bet * 2)
        return _complete_blackjack(connection, hand, deck, player_cards, dealer_cards, "lost", 0)
    return blackjack_hand_data(connection, hand)


def play_blackjack_action(connection, user_id, action):
    hand = connection.execute(
        "SELECT * FROM blackjack_hands WHERE telegram_id = ? AND status = 'playing'",
        (user_id,),
    ).fetchone()
    if hand is None:
        raise ValueError("Start a new hand first")
    deck = json.loads(hand["deck"])
    player_cards = json.loads(hand["player_cards"])
    dealer_cards = json.loads(hand["dealer_cards"])
    if action == "hit":
        player_cards.append(deck.pop())
        if blackjack_score(player_cards) >= 21:
            return _resolve_blackjack(connection, hand, deck, player_cards, dealer_cards)
        connection.execute(
            "UPDATE blackjack_hands SET deck = ?, player_cards = ? WHERE id = ?",
            (json.dumps(deck), json.dumps(player_cards), hand["id"]),
        )
    elif action == "stand":
        return _resolve_blackjack(connection, hand, deck, player_cards, dealer_cards)
    else:
        raise ValueError("Unknown blackjack action")
    updated = connection.execute("SELECT * FROM blackjack_hands WHERE id = ?", (hand["id"],)).fetchone()
    return blackjack_hand_data(connection, updated)


def settle_game(game, bet, choice=None, rng=secrets.SystemRandom()):
    if isinstance(bet, bool) or not isinstance(bet, int) or not 25 <= bet <= 500:
        raise ValueError("Bet must be between 25 and 500 play coins")

    if game == "coin":
        if choice not in ("Heads", "Tails"):
            raise ValueError("Choose Heads or Tails")
        result = rng.choice(("Heads", "Tails"))
        won = result == choice
        return {"result": result, "won": won, "payout": bet * 2 if won else 0}

    if game == "slots":
        reels = [rng.choice(SYMBOLS) for _ in range(3)]
        highest_match = max(reels.count(symbol) for symbol in set(reels))
        multiplier = 5 if highest_match == 3 else 2 if highest_match == 2 else 0
        return {"reels": reels, "won": multiplier > 0, "payout": bet * multiplier}

    raise ValueError("Unknown game")


POKER_STARTING_STACK = 500
POKER_SMALL_BLIND = 5
POKER_BIG_BLIND = 10


def _poker_card(rank, suit):
    return {"rank": rank, "suit": suit}


def _poker_hand_rank(cards):
    ranks = sorted((card["rank"] for card in cards), reverse=True)
    counts = {rank: ranks.count(rank) for rank in set(ranks)}
    groups = sorted(counts, key=lambda rank: (counts[rank], rank), reverse=True)
    flush = len({card["suit"] for card in cards}) == 1
    unique_ranks = set(ranks)
    if 14 in unique_ranks:
        unique_ranks.add(1)
    straight_high = next(
        (high for high in range(14, 4, -1) if all(rank in unique_ranks for rank in range(high - 4, high + 1))),
        0,
    )
    if flush and straight_high:
        return (8, straight_high)
    if counts[groups[0]] == 4:
        return (7, groups[0], groups[1])
    if counts[groups[0]] == 3 and counts[groups[1]] == 2:
        return (6, groups[0], groups[1])
    if flush:
        return (5, *ranks)
    if straight_high:
        return (4, straight_high)
    if counts[groups[0]] == 3:
        return (3, groups[0], *sorted((rank for rank in ranks if rank != groups[0]), reverse=True))
    pairs = sorted((rank for rank, count in counts.items() if count == 2), reverse=True)
    if len(pairs) >= 2:
        kicker = next(rank for rank in ranks if rank not in pairs)
        return (2, pairs[0], pairs[1], kicker)
    if pairs:
        return (1, pairs[0], *sorted((rank for rank in ranks if rank != pairs[0]), reverse=True))
    return (0, *ranks)


def _best_poker_hand(cards):
    return max(_poker_hand_rank(combo) for combo in itertools.combinations(cards, 5))


def _poker_next_index(players, index, include_all_in=False):
    for step in range(1, len(players) + 1):
        candidate = (index + step) % len(players)
        player = players[candidate]
        if not player["folded"] and (include_all_in or not player["all_in"]):
            return candidate
    return index


def _poker_pay(player, amount):
    paid = min(player["stack"], amount)
    player["stack"] -= paid
    player["current_bet"] += paid
    player["committed"] += paid
    player["all_in"] = player["stack"] == 0
    return paid


def _poker_complete_hand(state, winners, showdown=False):
    state["last_pot"] = sum(player["committed"] for player in state["players"])
    state["winners"] = winners
    state["status"] = "hand_complete"
    state["showdown"] = showdown
    state["pot"] = 0
    for player in state["players"]:
        player["current_bet"] = 0
        player["committed"] = 0
        player["acted"] = False
    if sum(player["stack"] > 0 for player in state["players"]) < 2:
        state["status"] = "finished"


def _poker_award_folded_hand(state, winner):
    pot = sum(player["committed"] for player in state["players"])
    winner["stack"] += pot
    _poker_complete_hand(state, [{"telegram_id": winner["telegram_id"], "amount": pot}])


def _poker_showdown(state):
    players = state["players"]
    contributions = sorted({player["committed"] for player in players if player["committed"] > 0})
    previous = 0
    winnings = {}
    for level in contributions:
        contributors = [player for player in players if player["committed"] >= level]
        pot = (level - previous) * len(contributors)
        eligible = [player for player in contributors if not player["folded"]]
        if eligible:
            best = max(_best_poker_hand(player["hole_cards"] + state["board"]) for player in eligible)
            winners = [
                player for player in eligible
                if _best_poker_hand(player["hole_cards"] + state["board"]) == best
            ]
            share, odd_chips = divmod(pot, len(winners))
            winners.sort(key=lambda player: players.index(player))
            for index, player in enumerate(winners):
                amount = share + (1 if index < odd_chips else 0)
                player["stack"] += amount
                winnings[player["telegram_id"]] = winnings.get(player["telegram_id"], 0) + amount
        else:
            for player in contributors:
                player["stack"] += level - previous
        previous = level
    _poker_complete_hand(
        state,
        [{"telegram_id": player_id, "amount": amount} for player_id, amount in winnings.items()],
        showdown=True,
    )


def _poker_advance(state):
    players = state["players"]
    contenders = [player for player in players if not player["folded"]]
    if len(contenders) == 1:
        _poker_award_folded_hand(state, contenders[0])
        return

    active = [player for player in contenders if not player["all_in"]]
    round_complete = all(player["acted"] and player["current_bet"] == state["current_bet"] for player in active)
    if active and not round_complete:
        return

    streets = ("preflop", "flop", "turn", "river")
    street_index = streets.index(state["street"])
    if street_index == len(streets) - 1 or not active:
        if not state["board"]:
            state["board"].extend(state["deck"].pop() for _ in range(3))
        while len(state["board"]) < 5:
            state["board"].append(state["deck"].pop())
        state["street"] = "river"
        _poker_showdown(state)
        return

    next_street = streets[street_index + 1]
    state["street"] = next_street
    if next_street == "flop":
        state["board"].extend(state["deck"].pop() for _ in range(3))
    else:
        state["board"].append(state["deck"].pop())
    state["current_bet"] = 0
    state["min_raise"] = POKER_BIG_BLIND
    for player in players:
        player["current_bet"] = 0
        player["acted"] = player["folded"] or player["all_in"]
    state["current_player"] = _poker_next_index(players, state["button"])


def _poker_begin_hand(state, rng=secrets.SystemRandom()):
    players = state["players"]
    eligible = [index for index, player in enumerate(players) if player["stack"] > 0]
    if len(eligible) < 2:
        state["status"] = "finished"
        return
    old_button = state.get("button", -1)
    button = next(
        (index for step in range(1, len(players) + 1)
         if (index := (old_button + step) % len(players)) in eligible),
        eligible[0],
    )
    state.update({
        "status": "playing",
        "street": "preflop",
        "board": [],
        "pot": 0,
        "current_bet": 0,
        "min_raise": POKER_BIG_BLIND,
        "button": button,
        "winners": [],
        "last_pot": 0,
        "showdown": False,
    })
    deck = [
        _poker_card(rank, suit)
        for suit in CARD_SUITS
        for rank in range(2, 15)
    ]
    rng.shuffle(deck)
    state["deck"] = deck
    for player in players:
        player.update({
            "folded": player["stack"] == 0,
            "all_in": player["stack"] == 0,
            "current_bet": 0,
            "committed": 0,
            "acted": player["stack"] == 0,
            "hole_cards": [deck.pop(), deck.pop()] if player["stack"] > 0 else [],
        })
    small_blind = _poker_next_index(players, button)
    big_blind = _poker_next_index(players, small_blind)
    _poker_pay(players[small_blind], POKER_SMALL_BLIND)
    _poker_pay(players[big_blind], POKER_BIG_BLIND)
    state["current_bet"] = max(player["current_bet"] for player in players)
    state["pot"] = sum(player["committed"] for player in players)
    state["current_player"] = _poker_next_index(players, big_blind)
    _poker_advance(state)


def _poker_public_state(connection, room_code, viewer_id):
    room = connection.execute(
        "SELECT r.code, r.owner_id, COALESCE(u.display_name, u.first_name) AS owner_name "
        "FROM rooms r JOIN users u ON u.telegram_id = r.owner_id WHERE r.code = ?",
        (room_code,),
    ).fetchone()
    if room is None:
        raise ValueError("Room not found")
    members = connection.execute(
        "SELECT m.telegram_id, COALESCE(u.display_name, u.first_name) AS name "
        "FROM room_members m JOIN users u ON u.telegram_id = m.telegram_id "
        "WHERE m.room_code = ? ORDER BY m.joined_at, m.telegram_id",
        (room_code,),
    ).fetchall()
    if not any(member["telegram_id"] == viewer_id for member in members):
        raise ValueError("Join this room to see the table")
    game_row = connection.execute(
        "SELECT state FROM poker_games WHERE room_code = ?", (room_code,)
    ).fetchone()
    game = None
    if game_row:
        state = json.loads(game_row["state"])
        game = {key: value for key, value in state.items() if key != "deck"}
        game["players"] = [
            {
                **{key: value for key, value in player.items() if key != "hole_cards"},
                "hole_cards": player["hole_cards"] if (
                    player["telegram_id"] == viewer_id or (state["showdown"] and not player["folded"])
                ) else [],
            }
            for player in state["players"]
        ]
        game["current_player_id"] = (
            state["players"][state["current_player"]]["telegram_id"]
            if state["status"] == "playing" else None
        )
    return {
        **dict(room),
        "players": len(members),
        "members": [dict(member) for member in members],
        "game": game,
    }


def _poker_start(connection, room_code, user_id):
    room = connection.execute(
        "SELECT owner_id FROM rooms WHERE code = ?", (room_code,)
    ).fetchone()
    if room is None:
        raise ValueError("Room not found")
    members = connection.execute(
        "SELECT m.telegram_id, COALESCE(u.display_name, u.first_name) AS name "
        "FROM room_members m JOIN users u ON u.telegram_id = m.telegram_id "
        "WHERE m.room_code = ? ORDER BY m.joined_at, m.telegram_id",
        (room_code,),
    ).fetchall()
    if not any(member["telegram_id"] == user_id for member in members):
        raise ValueError("You are not seated at this table")
    if not 2 <= len(members) <= 8:
        raise ValueError("Poker needs 2 to 8 players in the room")
    existing = connection.execute(
        "SELECT state FROM poker_games WHERE room_code = ?", (room_code,)
    ).fetchone()
    if existing:
        state = json.loads(existing["state"])
        if state["status"] == "playing":
            return _poker_public_state(connection, room_code, user_id)
        state["players"] = [
            player for player in state["players"]
            if any(member["telegram_id"] == player["telegram_id"] for member in members)
        ]
        _poker_begin_hand(state)
    else:
        if room["owner_id"] != user_id:
            raise ValueError("Only the room host can start the table")
        state = {
            "status": "waiting",
            "players": [
                {
                    "telegram_id": member["telegram_id"],
                    "name": member["name"],
                    "stack": POKER_STARTING_STACK,
                }
                for member in members
            ],
            "button": -1,
        }
        _poker_begin_hand(state)
    connection.execute(
        "INSERT INTO poker_games (room_code, state, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(room_code) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at",
        (room_code, json.dumps(state), int(time.time())),
    )
    return _poker_public_state(connection, room_code, user_id)


def _poker_action(connection, room_code, user_id, payload):
    row = connection.execute(
        "SELECT state FROM poker_games WHERE room_code = ?", (room_code,)
    ).fetchone()
    if row is None:
        raise ValueError("The host must start the table first")
    state = json.loads(row["state"])
    if state["status"] != "playing":
        raise ValueError("Start the next hand when everyone is ready")
    player_index = next(
        (index for index, player in enumerate(state["players"]) if player["telegram_id"] == user_id),
        None,
    )
    if player_index is None:
        raise ValueError("You are not seated at this table")
    if player_index != state["current_player"]:
        raise ValueError("Wait for your turn")
    player = state["players"][player_index]
    action = payload.get("action")
    to_call = state["current_bet"] - player["current_bet"]
    if action == "fold":
        player["folded"] = True
        player["acted"] = True
    elif action == "check":
        if to_call:
            raise ValueError("You must call or fold")
        player["acted"] = True
    elif action == "call":
        if to_call <= 0:
            raise ValueError("There is nothing to call; check instead")
        _poker_pay(player, to_call)
        player["acted"] = True
    elif action == "raise":
        raise_to = payload.get("raise_to")
        if isinstance(raise_to, bool) or not isinstance(raise_to, int):
            raise ValueError("Enter a valid raise amount")
        if raise_to < state["current_bet"] + state["min_raise"]:
            raise ValueError(f"Minimum raise is {state['current_bet'] + state['min_raise']} chips")
        if raise_to > player["current_bet"] + player["stack"]:
            raise ValueError("You do not have enough table chips for that raise")
        increase = raise_to - state["current_bet"]
        _poker_pay(player, raise_to - player["current_bet"])
        state["current_bet"] = raise_to
        state["min_raise"] = increase
        for other in state["players"]:
            if other is not player and not other["folded"] and not other["all_in"]:
                other["acted"] = False
        player["acted"] = True
    else:
        raise ValueError("Unknown poker action")
    state["pot"] = sum(candidate["committed"] for candidate in state["players"])
    if state["status"] == "playing":
        state["current_player"] = _poker_next_index(state["players"], player_index)
    _poker_advance(state)
    connection.execute(
        "UPDATE poker_games SET state = ?, updated_at = ? WHERE room_code = ?",
        (json.dumps(state), int(time.time()), room_code),
    )
    return _poker_public_state(connection, room_code, user_id)


class SotoHandler(BaseHTTPRequestHandler):
    db_path = DB_PATH
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    bot_username = os.environ.get("TELEGRAM_BOT_USERNAME", "").lstrip("@")

    def log_message(self, format_string, *args):
        print(f"{self.address_string()} - {format_string % args}")

    def respond(self, status, data, content_type="application/json; charset=utf-8"):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 4096:
                raise ValueError("Request is too large")
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("Request body must be an object")
            return payload
        except (ValueError, json.JSONDecodeError) as error:
            raise ValueError("Invalid request body") from error

    def authenticated_user(self):
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("tma ") or not self.bot_token:
            raise AuthenticationError("Open Soto from Telegram to continue")
        try:
            return verify_init_data(authorization[4:], self.bot_token)
        except ValueError as error:
            raise AuthenticationError(str(error)) from error

    def do_GET(self):
        route = urlparse(self.path).path
        if route in ("/", "/index.html"):
            self.respond(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
            return
        if route == "/app.js":
            self.respond(200, (ROOT / "app.js").read_bytes(), "text/javascript; charset=utf-8")
            return
        if not route.startswith("/api/"):
            self.respond(404, {"error": "Not found"})
            return
        try:
            user = self.authenticated_user()
            if route == "/api/me":
                with database(self.db_path) as connection:
                    row = get_or_create_user(connection, user)
                self.respond(
                    200,
                    {
                        "user": {**user, "display_name": row["display_name"]},
                        "balance": row["balance"],
                        "app_url": self.app_url(),
                    },
                )
                return
            if route == "/api/me/rooms":
                with database(self.db_path) as connection:
                    rooms = connection.execute(
                        "SELECT r.code, r.owner_id, COALESCE(u.display_name, u.first_name) AS owner_name, "
                        "(SELECT COUNT(*) FROM room_members m WHERE m.room_code = r.code) AS players "
                        "FROM rooms r JOIN users u ON u.telegram_id = r.owner_id "
                        "WHERE EXISTS (SELECT 1 FROM room_members m WHERE m.room_code = r.code "
                        "AND m.telegram_id = ?) ORDER BY r.created_at DESC",
                        (user["id"],),
                    ).fetchall()
                self.respond(200, {"rooms": [{**dict(room), "share_url": self.room_url(room["code"])} for room in rooms]})
                return
            if route.startswith("/api/rooms/"):
                code = route.removeprefix("/api/rooms/").upper().strip("/")
                with database(self.db_path) as connection:
                    room = connection.execute(
                        "SELECT r.code, r.owner_id, COALESCE(u.display_name, u.first_name) AS owner_name, "
                        "(SELECT COUNT(*) FROM room_members m WHERE m.room_code = r.code) AS players "
                        "FROM rooms r JOIN users u ON u.telegram_id = r.owner_id WHERE r.code = ?",
                        (code,),
                    ).fetchone()
                    result = None if room is None else _poker_public_state(connection, code, user["id"])
                    if result is not None:
                        result["share_url"] = self.room_url(code)
                if result is None:
                    self.respond(404, {"error": "Room not found"})
                else:
                    self.respond(200, result)
                return
            self.respond(404, {"error": "Not found"})
        except AuthenticationError as error:
            self.respond(401, {"error": str(error)})
        except ValueError as error:
            self.respond(403, {"error": str(error)})

    def do_POST(self):
        route = urlparse(self.path).path
        if not route.startswith("/api/"):
            self.respond(404, {"error": "Not found"})
            return
        try:
            user = self.authenticated_user()
            payload = self.read_json()
            with database(self.db_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = get_or_create_user(connection, user)
                if route == "/api/me/name":
                    display_name = str(payload.get("name", "")).strip()
                    if not 1 <= len(display_name) <= 24 or any(ord(char) < 32 for char in display_name):
                        raise ValueError("Name must be 1 to 24 characters")
                    connection.execute(
                        "UPDATE users SET display_name = ? WHERE telegram_id = ?",
                        (display_name, user["id"]),
                    )
                    connection.commit()
                    self.respond(200, {"user": {**user, "display_name": display_name}})
                    return
                if route == "/api/rooms":
                    code = secrets.token_hex(4).upper()
                    now = int(time.time())
                    connection.execute(
                        "INSERT INTO rooms (code, owner_id, created_at) VALUES (?, ?, ?)",
                        (code, user["id"], now),
                    )
                    connection.execute(
                        "INSERT INTO room_members (room_code, telegram_id, joined_at) VALUES (?, ?, ?)",
                        (code, user["id"], now),
                    )
                    result = {"code": code, "players": 1, "share_url": self.room_url(code)}
                    connection.commit()
                    self.respond(201, result)
                    return

                if route == "/api/rooms/join":
                    code = str(payload.get("code", "")).upper()
                    room = connection.execute("SELECT code FROM rooms WHERE code = ?", (code,)).fetchone()
                    if room is None:
                        raise ValueError("Room not found")
                    already_joined = connection.execute(
                        "SELECT 1 FROM room_members WHERE room_code = ? AND telegram_id = ?",
                        (code, user["id"]),
                    ).fetchone()
                    poker = connection.execute(
                        "SELECT state FROM poker_games WHERE room_code = ?", (code,)
                    ).fetchone()
                    if poker and not already_joined and json.loads(poker["state"])["status"] in (
                        "playing", "hand_complete", "finished"
                    ):
                        raise ValueError("This poker table has already started")
                    count = connection.execute(
                        "SELECT COUNT(*) FROM room_members WHERE room_code = ?", (code,)
                    ).fetchone()[0]
                    if count >= 8 and not already_joined:
                        raise ValueError("This room is full")
                    connection.execute(
                        "INSERT OR IGNORE INTO room_members (room_code, telegram_id, joined_at) "
                        "VALUES (?, ?, ?)",
                        (code, user["id"], int(time.time())),
                    )
                    count = connection.execute(
                        "SELECT COUNT(*) FROM room_members WHERE room_code = ?", (code,)
                    ).fetchone()[0]
                    connection.commit()
                    self.respond(200, {"code": code, "players": count, "share_url": self.room_url(code)})
                    return

                if route.startswith("/api/rooms/") and route.endswith("/poker/start"):
                    code = route.split("/")[3].upper()
                    result = _poker_start(connection, code, user["id"])
                    result["share_url"] = self.room_url(code)
                    connection.commit()
                    self.respond(200, result)
                    return

                if route.startswith("/api/rooms/") and route.endswith("/poker/action"):
                    code = route.split("/")[3].upper()
                    result = _poker_action(connection, code, user["id"], payload)
                    result["share_url"] = self.room_url(code)
                    connection.commit()
                    self.respond(200, result)
                    return

                if route in (
                    "/api/games/blackjack/start",
                    "/api/games/blackjack/hit",
                    "/api/games/blackjack/stand",
                ):
                    if route.endswith("/start"):
                        result = start_blackjack(connection, user["id"], payload.get("bet"))
                    else:
                        action = "hit" if route.endswith("/hit") else "stand"
                        result = play_blackjack_action(connection, user["id"], action)
                    connection.commit()
                    self.respond(200, result)
                    return

                game = route.removeprefix("/api/games/")
                outcome = settle_game(game, payload.get("bet"), payload.get("choice"))
                bet = payload["bet"]
                if row["balance"] < bet:
                    raise ValueError("Not enough play coins")
                new_balance = row["balance"] - bet + outcome["payout"]
                connection.execute(
                    "UPDATE users SET balance = ? WHERE telegram_id = ?",
                    (new_balance, user["id"]),
                )
                connection.commit()
            self.respond(200, {"balance": new_balance, **outcome})
        except AuthenticationError as error:
            self.respond(401, {"error": str(error)})
        except ValueError as error:
            self.respond(400, {"error": str(error)})
        except sqlite3.IntegrityError:
            self.respond(404, {"error": "Room not found"})

    def room_url(self, code):
        if self.bot_username:
            return f"https://t.me/{self.bot_username}?startapp=room_{code}"
        return ""

    def app_url(self):
        if self.bot_username:
            return f"https://t.me/{self.bot_username}?startapp"
        return ""


def main():
    if not SotoHandler.bot_token:
        SotoHandler.bot_token = load_bot_token()
    if not SotoHandler.bot_token:
        if not sys.stdin.isatty():
            raise SystemExit("Set TELEGRAM_BOT_TOKEN or provide token.txt before starting the app.")
        SotoHandler.bot_token = getpass("Telegram bot token (input hidden): ").strip()
    if not SotoHandler.bot_token:
        raise SystemExit("A Telegram bot token is required.")
    if not SotoHandler.bot_username and sys.stdin.isatty():
        try:
            SotoHandler.bot_username = input("Telegram bot username (without @): ").strip().lstrip("@")
        except EOFError:
            pass
    if not SotoHandler.bot_username:
        print("TELEGRAM_BOT_USERNAME is not set; Telegram invite links will be unavailable.")
    initialize_database(SotoHandler.db_path)
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8000"))), SotoHandler)
    print(f"Soto listening on http://127.0.0.1:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nSoto stopped")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()