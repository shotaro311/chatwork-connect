#!/usr/bin/env python3
"""Chatwork API CLI（標準ライブラリのみ・追加インストール不要）

使い方:
  python3 chatwork.py me                      # 接続テスト（自分のアカウント情報）
  python3 chatwork.py status                  # 未読・メンションの全体サマリー
  python3 chatwork.py rooms                   # ルーム一覧
  python3 chatwork.py rooms --unread-only     # 未読があるルームだけ
  python3 chatwork.py messages ROOM_ID        # 未読メッセージを取得
  python3 chatwork.py messages ROOM_ID --force  # 最新100件を取得（既読含む）
  python3 chatwork.py send ROOM_ID "本文"     # メッセージ送信（承認フローはSkill側で必須）
  python3 chatwork.py mark-read ROOM_ID       # ルームを既読にする
  python3 chatwork.py archive                 # 全ルームの新着をローカルログに蓄積
  python3 chatwork.py archive --rooms ID,ID   # 指定ルームだけ蓄積

APIトークンの読み込み順:
  1. 環境変数 CHATWORK_API_TOKEN
  2. ~/.config/chatwork-connect/.env   ← 推奨（プラグイン更新で消えない）
  3. パッケージ直下の .env

ログの保存先: ~/.config/chatwork-connect/logs/（環境変数 CHATWORK_DATA_DIR で変更可）
"""
import argparse
import json
import sys
import os
import time
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

API_BASE = "https://api.chatwork.com/v2"
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CHATWORK_DATA_DIR", "")) if os.environ.get("CHATWORK_DATA_DIR") \
    else Path.home() / ".config" / "chatwork-connect"
ARCHIVE_ROOM_CAP = 80        # 1回のarchiveで巡回する最大ルーム数（利用回数制限対策）
ARCHIVE_SLEEP_SEC = 0.3      # ルーム間の待機

SETTINGS_PATH = DATA_DIR / "settings.json"
IMPORT_STATE_PATH = DATA_DIR / "import-state.json"
DEFAULT_SETTINGS = {
    # 過去ログの一括取り込み。既定はオフ。
    # 公式ドキュメントに無いパラメータを使うため、利用者が明示的にオンにしたときだけ動く。
    "history_import": False,
}
IMPORT_SLEEP_SEC = 6.0       # 取り込み時のリクエスト間隔（速度制限対策）
IMPORT_MAX_REQUESTS = 200    # 1回の実行での最大リクエスト数（既定）


class RateLimited(Exception):
    """429（利用回数制限）。呼び出し側で中断・再開の判断に使う。"""


def _parse_env_file(path: Path) -> str:
    # utf-8-sig: Windows で作られた BOM 付きファイルも読めるようにする
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip().lstrip("﻿")
        if line.startswith("CHATWORK_API_TOKEN="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            if value and value != "ここにトークンを貼り付け":
                return value
    return ""


def load_token() -> str:
    token = os.environ.get("CHATWORK_API_TOKEN", "").strip()
    if token:
        return token
    for env_path in (DATA_DIR / ".env", PACKAGE_ROOT / ".env"):
        if env_path.exists():
            token = _parse_env_file(env_path)
            if token:
                return token
    sys.exit(
        "ERROR: APIトークンが未設定です。\n"
        f"  {DATA_DIR / '.env'} を作成し、CHATWORK_API_TOKEN=（トークン）を設定してください。\n"
        "  取得方法は SETUP.md を参照。"
    )


def request(method: str, path: str, params: dict | None = None):
    token = load_token()
    url = API_BASE + path
    data = None
    if method == "GET" and params:
        url += "?" + urllib.parse.urlencode(params)
    elif method != "GET" and params:
        data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("x-chatworktoken", token)
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            if res.status == 204:
                return None
            body = res.read().decode("utf-8")
            return json.loads(body) if body.strip() else None
    except urllib.error.HTTPError as e:
        if e.code == 401:
            sys.exit("ERROR: 認証エラー(401)。APIトークンが正しいか確認してください。")
        if e.code == 429:
            raise RateLimited("利用回数制限(429)")
        detail = e.read().decode("utf-8", "replace")
        sys.exit(f"ERROR: HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"ERROR: 接続に失敗しました（ネットワークを確認してください）: {e.reason}")


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    if SETTINGS_PATH.exists():
        try:
            s.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            print(f"警告: {SETTINGS_PATH} を読めませんでした。既定値を使います。", file=sys.stderr)
    return s


def save_settings(s: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(s, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _to_bool(value: str) -> bool:
    v = value.strip().lower()
    if v in ("on", "true", "yes", "1", "オン"):
        return True
    if v in ("off", "false", "no", "0", "オフ"):
        return False
    sys.exit(f"ERROR: on / off のどちらかを指定してください（受け取った値: {value}）")


def fmt_time(epoch: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


def out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def simplify_message(m: dict, room_id=None) -> dict:
    d = {
        "message_id": m.get("message_id"),
        "from": (m.get("account") or {}).get("name"),
        "time": fmt_time(m.get("send_time", 0)),
        "send_time": m.get("send_time"),
        "body": m.get("body"),
    }
    if room_id is not None:
        d["room_id"] = room_id
    return d


def cmd_me(_args) -> None:
    me = request("GET", "/me")
    out({
        "account_id": me.get("account_id"),
        "name": me.get("name"),
        "organization_name": me.get("organization_name"),
        "chatwork_id": me.get("chatwork_id"),
    })
    print("\nOK: 接続に成功しました。", file=sys.stderr)


def cmd_status(_args) -> None:
    out(request("GET", "/my/status"))


def cmd_rooms(args) -> None:
    rooms = request("GET", "/rooms") or []
    simplified = [
        {
            "room_id": r.get("room_id"),
            "name": r.get("name"),
            "type": r.get("type"),
            "unread_num": r.get("unread_num"),
            "mention_num": r.get("mention_num"),
        }
        for r in rooms
    ]
    if args.unread_only:
        simplified = [r for r in simplified if (r["unread_num"] or 0) > 0]
        if not simplified:
            print("未読のあるルームはありません。")
            return
    out(simplified)


def cmd_messages(args) -> None:
    params = {"force": 1} if args.force else {"force": 0}
    messages = request("GET", f"/rooms/{args.room_id}/messages", params)
    if not messages:
        print("新着メッセージはありません。")
        return
    out([simplify_message(m) for m in messages])


def cmd_send(args) -> None:
    result = request("POST", f"/rooms/{args.room_id}/messages", {"body": args.body})
    out(result)
    print("\nOK: 送信しました。", file=sys.stderr)


def cmd_mark_read(args) -> None:
    result = request("PUT", f"/rooms/{args.room_id}/messages/read")
    out(result)
    print("\nOK: 既読にしました。", file=sys.stderr)


def _load_logged_ids(log_path: Path) -> set:
    ids = set()
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                ids.add(json.loads(line).get("message_id"))
            except json.JSONDecodeError:
                continue
    return ids


def cmd_archive(args) -> None:
    """各ルームの最新メッセージを取得し、ローカルログ（1ルーム=1ファイルのJSONL）へ追記する。

    公式APIは各ルーム最新100件までしか取得できないため、
    これを定期的に実行して履歴を蓄積していく。実行間隔の目安は活発なルームで1日1回以上。
    """
    logs_dir = DATA_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    rooms = request("GET", "/rooms") or []
    if args.rooms:
        wanted = {s.strip() for s in args.rooms.split(",")}
        rooms = [r for r in rooms if str(r.get("room_id")) in wanted]
    if len(rooms) > ARCHIVE_ROOM_CAP:
        print(f"注意: ルーム数が{len(rooms)}件のため、先頭{ARCHIVE_ROOM_CAP}件のみ処理します。"
              f"残りは --rooms で指定して実行してください。", file=sys.stderr)
        rooms = rooms[:ARCHIVE_ROOM_CAP]

    # ルーム名の対応表を更新
    names_path = logs_dir / "rooms.json"
    names = {}
    if names_path.exists():
        try:
            names = json.loads(names_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            names = {}
    for r in rooms:
        names[str(r.get("room_id"))] = r.get("name")
    names_path.write_text(json.dumps(names, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = []
    total_new = 0
    for r in rooms:
        room_id = r.get("room_id")
        log_path = logs_dir / f"{room_id}.jsonl"
        known_ids = _load_logged_ids(log_path)
        messages = request("GET", f"/rooms/{room_id}/messages", {"force": 1}) or []
        fresh = [m for m in messages if m.get("message_id") not in known_ids]
        if fresh:
            fresh.sort(key=lambda m: m.get("send_time", 0))
            with log_path.open("a", encoding="utf-8") as f:
                for m in fresh:
                    f.write(json.dumps(simplify_message(m, room_id), ensure_ascii=False) + "\n")
            summary.append({"room_id": room_id, "name": r.get("name"), "new": len(fresh)})
            total_new += len(fresh)
        time.sleep(ARCHIVE_SLEEP_SEC)

    out({
        "checked_rooms": len(rooms),
        "new_messages": total_new,
        "updated": summary,
        "log_dir": str(logs_dir),
    })
    print(f"\nOK: {len(rooms)}ルームを確認し、{total_new}件を新規保存しました。", file=sys.stderr)


def cmd_config(args) -> None:
    s = load_settings()
    if args.set:
        if "=" not in args.set:
            sys.exit("ERROR: --set は key=value の形式で指定してください（例: history_import=on）")
        key, value = args.set.split("=", 1)
        key = key.strip()
        if key not in DEFAULT_SETTINGS:
            sys.exit(f"ERROR: 不明な設定項目です: {key}（指定可能: {', '.join(DEFAULT_SETTINGS)}）")
        s[key] = _to_bool(value)
        save_settings(s)
        state = "オン" if s[key] else "オフ"
        print(f"OK: {key} を {state} にしました。", file=sys.stderr)
    out({k: ("on" if v else "off") if isinstance(v, bool) else v for k, v in s.items()})


def _load_import_state() -> dict:
    if IMPORT_STATE_PATH.exists():
        try:
            return json.loads(IMPORT_STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_import_state(state: dict) -> None:
    IMPORT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    IMPORT_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")


def cmd_import_history(args) -> None:
    """過去ログの一括取り込み（既定はオフ）。

    公式ドキュメントに記載のない message_id パラメータでページ送りする。
    公式仕様ではないため、将来動かなくなる可能性がある。
    速度制限が厳しいため、途中で止まっても再実行で続きから再開できる。
    """
    settings = load_settings()
    if not settings.get("history_import"):
        sys.exit(
            "過去ログの一括取り込みは、既定でオフになっています。\n"
            "\n"
            "  この機能は、チャットワークの公式ドキュメントに記載のない方法を使います。\n"
            "  将来動かなくなる可能性があること、取り込みに数時間かかる場合があることを\n"
            "  ご承知のうえで、オンにしてください。\n"
            "\n"
            "  オンにする:  python3 scripts/chatwork.py config --set history_import=on\n"
            "  オフに戻す:  python3 scripts/chatwork.py config --set history_import=off"
        )

    logs_dir = DATA_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    state = _load_import_state()
    cutoff = time.time() - args.days * 86400 if args.days else None

    rooms = request("GET", "/rooms") or []
    if args.rooms:
        wanted = {s.strip() for s in args.rooms.split(",")}
        rooms = [r for r in rooms if str(r.get("room_id")) in wanted]
    if not rooms:
        sys.exit("ERROR: 対象のルームが見つかりませんでした。")

    print(f"{len(rooms)}ルームを対象に取り込みます。"
          f"（間隔{IMPORT_SLEEP_SEC}秒・最大{args.max_requests}リクエスト）", file=sys.stderr)
    if cutoff:
        print(f"直近{args.days}日以内のメッセージのみ保存します。", file=sys.stderr)

    requests_used = 0
    summary = []
    interrupted = False

    for r in rooms:
        room_id = str(r.get("room_id"))
        log_path = logs_dir / f"{room_id}.jsonl"
        known_ids = _load_logged_ids(log_path)
        cursor = state.get(room_id, "0")
        saved = 0
        done = False

        while requests_used < args.max_requests:
            try:
                batch = request("GET", f"/rooms/{room_id}/messages",
                                {"force": 1, "message_id": cursor})
            except RateLimited:
                print(f"  制限に達したため中断します（{r.get('name')}）。"
                      f"しばらく待って再実行すると続きから再開します。", file=sys.stderr)
                interrupted = True
                break
            requests_used += 1

            if not batch:
                done = True
                break

            new_cursor = str(batch[-1].get("message_id"))
            if new_cursor == cursor:
                done = True   # 進まなくなったら終了（これ以上遡れない）
                break

            fresh = [m for m in batch if m.get("message_id") not in known_ids]
            if cutoff:
                fresh = [m for m in fresh if m.get("send_time", 0) >= cutoff]
            if fresh:
                fresh.sort(key=lambda m: m.get("send_time", 0))
                with log_path.open("a", encoding="utf-8") as f:
                    for m in fresh:
                        f.write(json.dumps(simplify_message(m, int(room_id)),
                                           ensure_ascii=False) + "\n")
                        known_ids.add(m.get("message_id"))
                saved += len(fresh)

            cursor = new_cursor
            state[room_id] = cursor
            _save_import_state(state)
            print(f"  {r.get('name')}: {saved}件保存 "
                  f"（{requests_used}/{args.max_requests}リクエスト）", file=sys.stderr)
            time.sleep(IMPORT_SLEEP_SEC)

        summary.append({"room_id": int(room_id), "name": r.get("name"),
                        "saved": saved, "completed": done})
        if interrupted or requests_used >= args.max_requests:
            break

    out({
        "requests_used": requests_used,
        "total_saved": sum(s["saved"] for s in summary),
        "rooms": summary,
        "interrupted": interrupted or requests_used >= args.max_requests,
        "log_dir": str(logs_dir),
    })
    if interrupted or requests_used >= args.max_requests:
        print("\n途中で終了しました。同じコマンドを再実行すると続きから再開します。", file=sys.stderr)
    else:
        print("\nOK: 取り込みが完了しました。", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Chatwork API CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("me", help="接続テスト").set_defaults(func=cmd_me)
    sub.add_parser("status", help="未読・メンションのサマリー").set_defaults(func=cmd_status)

    p_rooms = sub.add_parser("rooms", help="ルーム一覧")
    p_rooms.add_argument("--unread-only", action="store_true", help="未読があるルームだけ表示")
    p_rooms.set_defaults(func=cmd_rooms)

    p_msg = sub.add_parser("messages", help="メッセージ取得")
    p_msg.add_argument("room_id")
    p_msg.add_argument("--force", action="store_true", help="未読に関係なく最新100件を取得")
    p_msg.set_defaults(func=cmd_messages)

    p_send = sub.add_parser("send", help="メッセージ送信")
    p_send.add_argument("room_id")
    p_send.add_argument("body")
    p_send.set_defaults(func=cmd_send)

    p_read = sub.add_parser("mark-read", help="ルームを既読にする")
    p_read.add_argument("room_id")
    p_read.set_defaults(func=cmd_mark_read)

    p_arch = sub.add_parser("archive", help="新着メッセージをローカルログへ蓄積")
    p_arch.add_argument("--rooms", help="対象ルームIDをカンマ区切りで指定（省略時は全ルーム）")
    p_arch.set_defaults(func=cmd_archive)

    p_conf = sub.add_parser("config", help="設定の確認・変更")
    p_conf.add_argument("--set", help="設定を変更（例: history_import=on）")
    p_conf.set_defaults(func=cmd_config)

    p_imp = sub.add_parser("import-history", help="過去ログの一括取り込み（既定はオフ）")
    p_imp.add_argument("--rooms", help="対象ルームIDをカンマ区切りで指定（省略時は全ルーム）")
    p_imp.add_argument("--days", type=int, help="直近N日以内のメッセージのみ保存（省略時は全期間）")
    p_imp.add_argument("--max-requests", type=int, default=IMPORT_MAX_REQUESTS,
                       help=f"1回の実行での最大リクエスト数（既定: {IMPORT_MAX_REQUESTS}）")
    p_imp.set_defaults(func=cmd_import_history)

    args = parser.parse_args()
    try:
        args.func(args)
    except RateLimited:
        sys.exit("ERROR: 利用回数制限(429)。5分ほど待ってから再実行してください。")


if __name__ == "__main__":
    main()
