import hashlib
import hmac
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from pathlib import Path

from server import (
    SotoHandler,
    blackjack_score,
    database,
    initialize_database,
    load_bot_token,
    play_blackjack_action,
    settle_game,
    start_blackjack,
    verify_init_data,
    _poker_hand_rank,
)


BOT_TOKEN = "test-bot-token"


def signed_init_data(user_id, first_name, auth_date=None):
    values = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "user": json.dumps({"id": user_id, "first_name": first_name}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class TelegramAuthTests(unittest.TestCase):
    def test_accepts_valid_signature(self):
        raw = signed_init_data(123, "Taylor", now := int(time.time()))
        self.assertEqual(verify_init_data(raw, BOT_TOKEN, now), {"id": 123, "first_name": "Taylor"})

    def test_rejects_modified_payload(self):
        raw = signed_init_data(123, "Taylor").replace("Taylor", "Morgan")
        with self.assertRaisesRegex(ValueError, "signature"):
            verify_init_data(raw, BOT_TOKEN)

    def test_rejects_expired_signature(self):
        now = int(time.time())
        raw = signed_init_data(123, "Taylor", now - 25 * 60 * 60)
        with self.assertRaisesRegex(ValueError, "expired"):
            verify_init_data(raw, BOT_TOKEN, now)


class TokenConfigTests(unittest.TestCase):
    def test_loads_token_file_without_logging_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = Path(temp_dir) / "token.txt"
            token_path.write_text("local-test-token\n", encoding="utf-8")
            self.assertEqual(load_bot_token(token_path, {}), "local-test-token")

    def test_environment_token_takes_precedence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = Path(temp_dir) / "token.txt"
            token_path.write_text("file-token", encoding="utf-8")
            self.assertEqual(
                load_bot_token(token_path, {"TELEGRAM_BOT_TOKEN": "environment-token"}),
                "environment-token",
            )


class BlackjackTests(unittest.TestCase):
    def test_aces_are_scored_flexibly_without_removing_cards(self):
        cards = [
            {"rank": "A", "suit": "♠", "value": 11},
            {"rank": "A", "suit": "♥", "value": 11},
        ]
        self.assertEqual(blackjack_score(cards), 12)
        self.assertEqual(len(cards), 2)

    def test_player_can_hit_and_stand_to_win(self):
        draw_order = [
            {"rank": "10", "suit": "♠", "value": 10},
            {"rank": "7", "suit": "♦", "value": 7},
            {"rank": "9", "suit": "♥", "value": 9},
            {"rank": "7", "suit": "♣", "value": 7},
            {"rank": "2", "suit": "♠", "value": 2},
            {"rank": "10", "suit": "♦", "value": 10},
        ]

        class FixedDeck:
            def shuffle(self, deck):
                filler = [{"rank": "2", "suit": "♣", "value": 2}] * (len(deck) - len(draw_order))
                deck[:] = filler + list(reversed(draw_order))

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "blackjack.sqlite3"
            initialize_database(db_path)
            with database(db_path) as connection:
                connection.execute(
                    "INSERT INTO users (telegram_id, first_name) VALUES (?, ?)", (701, "Player")
                )
                hand = start_blackjack(connection, 701, 25, FixedDeck())
                self.assertEqual((hand["status"], hand["player_score"], hand["dealer_hidden"]), ("playing", 17, True))

                hand = play_blackjack_action(connection, 701, "hit")
                self.assertEqual((hand["status"], hand["player_score"]), ("playing", 19))

                hand = play_blackjack_action(connection, 701, "stand")
                self.assertEqual((hand["status"], hand["result"], hand["payout"]), ("won", "won", 50))
                self.assertFalse(hand["dealer_hidden"])
                self.assertEqual(hand["balance"], 2505)


class PokerHandTests(unittest.TestCase):
    def test_straight_flush_beats_full_house_and_wheel_is_low_straight(self):
        straight_flush = [
            {"rank": rank, "suit": "♠"} for rank in (10, 11, 12, 13, 14)
        ]
        full_house = [
            {"rank": rank, "suit": suit}
            for rank, suit in ((9, "♠"), (9, "♥"), (9, "♦"), (4, "♣"), (4, "♠"))
        ]
        wheel = [
            {"rank": rank, "suit": suit}
            for rank, suit in ((14, "♠"), (2, "♥"), (3, "♦"), (4, "♣"), (5, "♠"))
        ]
        self.assertGreater(_poker_hand_rank(straight_flush), _poker_hand_rank(full_house))
        self.assertEqual(_poker_hand_rank(wheel), (4, 5))


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "test.sqlite3"
        initialize_database(cls.db_path)
        handler = type(
            "TestSotoHandler",
            (SotoHandler,),
            {"db_path": cls.db_path, "bot_token": BOT_TOKEN, "bot_username": "soto_test_bot"},
        )
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp_dir.cleanup()

    def api(self, user_id, name, path, payload=None, authenticated=True):
        body = None if payload is None else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = f"tma {signed_init_data(user_id, name)}"
        request = Request(f"{self.base_url}{path}", data=body, headers=headers)
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()

    def test_requires_telegram_authentication(self):
        status, data = self.api(1, "One", "/api/me", authenticated=False)
        self.assertEqual(status, 401)
        self.assertIn("Telegram", data["error"])

    def test_first_run_name_is_saved_to_the_telegram_account(self):
        status, account = self.api(606, "Casey", "/api/me")
        self.assertEqual(status, 200)
        self.assertIsNone(account["user"]["display_name"])

        status, saved = self.api(606, "Casey", "/api/me/name", {"name": "Casey Cardsharp"})
        self.assertEqual((status, saved["user"]["display_name"]), (200, "Casey Cardsharp"))
        _, account = self.api(606, "Casey", "/api/me")
        self.assertEqual(account["user"]["display_name"], "Casey Cardsharp")

    def test_blackjack_start_and_stand_are_account_authoritative(self):
        status, initial = self.api(707, "Player", "/api/games/blackjack/start", {"bet": 25})
        self.assertEqual(status, 200)
        if initial["status"] == "playing":
            self.assertTrue(initial["dealer_hidden"])
            original_balance = initial["balance"]
            status, duplicate = self.api(707, "Player", "/api/games/blackjack/start", {"bet": 500})
            self.assertEqual(status, 200)
            self.assertEqual((duplicate["hand_id"], duplicate["balance"]), (initial["hand_id"], original_balance))
            status, initial = self.api(707, "Player", "/api/games/blackjack/stand", {})
            self.assertEqual(status, 200)
        self.assertIn(initial["status"], ("won", "lost", "push"))
        self.assertFalse(initial["dealer_hidden"])
        _, account = self.api(707, "Player", "/api/me")
        self.assertEqual(initial["balance"], account["balance"])

    def test_serves_app_and_client_script(self):
        with urlopen(f"{self.base_url}/") as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b'src="/app.js"', response.read())
        with urlopen(f"{self.base_url}/app.js") as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"/api/me", response.read())

    def test_game_updates_only_authenticated_players_balance(self):
        status, account = self.api(101, "One", "/api/me")
        self.assertEqual((status, account["balance"]), (200, 2480))

        status, outcome = self.api(101, "One", "/api/games/coin", {"bet": 500, "choice": "Heads"})
        self.assertEqual(status, 200)
        self.assertIn(outcome["balance"], (1980, 2980))

        status, other_account = self.api(202, "Two", "/api/me")
        self.assertEqual((status, other_account["balance"]), (200, 2480))

        status, _ = self.api(202, "Two", "/api/games/coin", {"bet": 500, "choice": "Heads"}, authenticated=False)
        self.assertEqual(status, 401)
        _, other_account = self.api(202, "Two", "/api/me")
        self.assertEqual(other_account["balance"], 2480)

    def test_room_can_be_created_and_joined_from_another_account(self):
        status, room = self.api(303, "Host", "/api/rooms", {})
        self.assertEqual(status, 201)
        self.assertRegex(room["code"], r"^[A-F0-9]{8}$")
        self.assertIn(f"startapp=room_{room['code']}", room["share_url"])

        status, joined = self.api(404, "Guest", "/api/rooms/join", {"code": room["code"]})
        self.assertEqual((status, joined["players"]), (200, 2))
        status, room_info = self.api(404, "Guest", f"/api/rooms/{room['code']}")
        self.assertEqual((status, room_info["players"]), (200, 2))
        status, room_list = self.api(404, "Guest", "/api/me/rooms")
        self.assertEqual((status, room_list["rooms"][0]["code"]), (200, room["code"]))
        self.assertEqual(room_list["rooms"][0]["players"], 2)

    def test_room_starts_playable_poker_and_settles_a_hand(self):
        _, room = self.api(811, "Host", "/api/rooms", {})
        status, joined = self.api(812, "Guest", "/api/rooms/join", {"code": room["code"]})
        self.assertEqual((status, joined["players"]), (200, 2))

        status, table = self.api(811, "Host", f"/api/rooms/{room['code']}/poker/start", {})
        self.assertEqual(status, 200)
        game = table["game"]
        self.assertEqual(game["status"], "playing")
        self.assertEqual(sum(player["stack"] for player in game["players"]) + game["pot"], 1000)
        self.assertEqual(len(next(player for player in game["players"] if player["telegram_id"] == 811)["hole_cards"]), 2)
        self.assertEqual(next(player for player in game["players"] if player["telegram_id"] == 812)["hole_cards"], [])

        player_id = game["current_player_id"]
        player_name = "Host" if player_id == 811 else "Guest"
        status, table = self.api(
            player_id,
            player_name,
            f"/api/rooms/{room['code']}/poker/action",
            {"action": "raise", "raise_to": game["current_bet"] + game["min_raise"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(table["game"]["current_bet"], 20)

        status, guest_table = self.api(812, "Guest", f"/api/rooms/{room['code']}")
        self.assertEqual(status, 200)
        self.assertEqual(len(next(player for player in guest_table["game"]["players"] if player["telegram_id"] == 812)["hole_cards"]), 2)
        self.assertEqual(next(player for player in guest_table["game"]["players"] if player["telegram_id"] == 811)["hole_cards"], [])

        for _ in range(12):
            game = table["game"]
            if game["status"] != "playing":
                break
            player_id = game["current_player_id"]
            player = next(seat for seat in game["players"] if seat["telegram_id"] == player_id)
            action = "call" if game["current_bet"] > player["current_bet"] else "check"
            name = "Host" if player_id == 811 else "Guest"
            status, table = self.api(
                player_id,
                name,
                f"/api/rooms/{room['code']}/poker/action",
                {"action": action},
            )
            self.assertEqual(status, 200)

        game = table["game"]
        self.assertIn(game["status"], ("hand_complete", "finished"))
        self.assertEqual(len(game["board"]), 5)
        self.assertEqual(sum(player["stack"] for player in game["players"]), 1000)
        self.assertTrue(game["winners"])
        if game["showdown"]:
            self.assertTrue(all(len(player["hole_cards"]) == 2 for player in game["players"]))

        status, next_hand = self.api(812, "Guest", f"/api/rooms/{room['code']}/poker/start", {})
        self.assertEqual(status, 200)
        self.assertEqual(next_hand["game"]["status"], "playing")

    def test_fold_awards_the_pot_without_revealing_folded_cards(self):
        _, room = self.api(831, "Host", "/api/rooms", {})
        self.api(832, "Guest", "/api/rooms/join", {"code": room["code"]})
        _, table = self.api(831, "Host", f"/api/rooms/{room['code']}/poker/start", {})
        folded_id = table["game"]["current_player_id"]
        folded_name = "Host" if folded_id == 831 else "Guest"
        status, table = self.api(
            folded_id,
            folded_name,
            f"/api/rooms/{room['code']}/poker/action",
            {"action": "fold"},
        )
        self.assertEqual(status, 200)
        game = table["game"]
        self.assertEqual(game["status"], "hand_complete")
        self.assertFalse(game["showdown"])
        other_id = next(player["telegram_id"] for player in game["players"] if player["telegram_id"] != folded_id)
        other_name = "Host" if other_id == 831 else "Guest"
        status, other_table = self.api(other_id, other_name, f"/api/rooms/{room['code']}")
        self.assertEqual(status, 200)
        folded_player = next(player for player in other_table["game"]["players"] if player["telegram_id"] == folded_id)
        other_player = next(player for player in other_table["game"]["players"] if player["telegram_id"] == other_id)
        self.assertEqual(folded_player["hole_cards"], [])
        self.assertEqual(len(other_player["hole_cards"]), 2)
        self.assertEqual(sum(player["stack"] for player in other_table["game"]["players"]), 1000)

    def test_new_players_cannot_join_after_poker_starts(self):
        _, room = self.api(821, "Host", "/api/rooms", {})
        self.api(822, "Guest", "/api/rooms/join", {"code": room["code"]})
        self.api(821, "Host", f"/api/rooms/{room['code']}/poker/start", {})
        status, result = self.api(823, "Late", "/api/rooms/join", {"code": room["code"]})
        self.assertEqual(status, 400)
        self.assertIn("already started", result["error"])

    def test_rejects_invalid_bet_without_changing_balance(self):
        status, outcome = self.api(505, "Player", "/api/games/coin", {"bet": 501, "choice": "Heads"})
        self.assertEqual(status, 400)
        self.assertIn("between 25 and 500", outcome["error"])
        _, account = self.api(505, "Player", "/api/me")
        self.assertEqual(account["balance"], 2480)


if __name__ == "__main__":
    unittest.main()