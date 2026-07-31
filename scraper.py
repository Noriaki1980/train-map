"""
shinkansen-map scraper.py

【方針転換】
traininfo.jr-central.co.jp の内部API (train_location_info.json) が
実際の列車位置・遅延情報をリアルタイムで返すことが判明したため、
時刻表からの推定(calculate_positions系)より、こちらを優先して使う。

  1. fetch_train_positions()   : リアルタイム列車位置APIを取得 (メイン)
  2. parse_train_positions()   : 取得結果をtrain_positions.json形式に変換
  3. calculate_positions()     : [フォールバック/補完用] 時刻表ベースの推定
                                   (trips_*.jsonがある場合のみ使用)

GitHub Actionsで数分おきに fetch_train_positions() → parse → 保存、を実行する想定。
"""

import json
import math
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import requests
except ImportError:
    requests = None  # requestsが無い環境でも他の関数は使えるようにしておく

JST = ZoneInfo("Asia/Tokyo")
DATA_DIR = Path(__file__).parent / "data"


# ---------------------------------------------------------------------------
# 1. 時刻表取得 (スタブ: ネットワークアクセスが必要なため未実装)
# ---------------------------------------------------------------------------

def fetch_timetable_kodama():
    """
    JR東海・JR西日本の駅時刻表ページから、こだま号の時刻表を取得する。

    TODO: 実装が必要。以下のいずれかの方針を想定:
      - 各駅の時刻表ページをrequests + BeautifulSoupでスクレイピング
      - JRグループが個別に公開している時刻データ(CSV等)があれば利用
      - 交通新聞社の時刻データ販売サービス等、有償データの利用を検討

    戻り値のイメージ (build_trips_json に渡す形):
        [
            {
                "trip_id": "kodama_633",
                "train_number": "633号",
                "direction": "down",  # down: 東京->博多方面, up: 博多->東京方面
                "service_days": ["weekday", "saturday", "sunday_holiday"],
                "stops": [
                    {"station": "東京", "arr": None, "dep": "06:00"},
                    {"station": "品川", "arr": "06:07", "dep": "06:08"},
                    ...
                ]
            },
            ...
        ]
    """
    raise NotImplementedError(
        "fetch_timetable_kodama() はまだ実装されていません。"
        "ネットワークアクセスのある環境で実装してください。"
    )


def build_trips_json(trips, output_path):
    """取得した時刻表データをtrips_*.json形式で保存する。

    jreit-mapと同様、取得に失敗した場合は既存ファイルを上書きしない設計にする。
    """
    output_path = Path(output_path)
    payload = {
        "line": "tokaido_sanyo",
        "train_type": "kodama",
        "updated_at": datetime.now(JST).isoformat(),
        "trips": trips,
    }
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"saved: {output_path} ({len(trips)} trips)")


# ---------------------------------------------------------------------------
# 0. リアルタイム列車位置API (メイン方式)
# ---------------------------------------------------------------------------

TRAIN_LOCATION_URL = "https://traininfo.jr-central.co.jp/shinkansen/var/train_info/train_location_info.json"

API_HEADERS = {
    # サーバーがrefererを見ている可能性があるため、ブラウザからのアクセスに寄せる
    "Referer": "https://traininfo.jr-central.co.jp/shinkansen/sp/ja/ti08.html",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}


def fetch_train_positions():
    """train_location_info.json を取得して生JSON(dict)を返す。

    失敗した場合は例外を送出する。呼び出し側(main)でtry/exceptして、
    既存のtrain_positions.jsonを保持する設計にすること。
    """
    if requests is None:
        raise RuntimeError("requests がインストールされていません: pip install requests")

    timestamp_ms = int(datetime.now().timestamp() * 1000)
    url = f"{TRAIN_LOCATION_URL}?timestamp={timestamp_ms}"
    resp = requests.get(url, headers=API_HEADERS, timeout=10)

    if resp.status_code != 200:
        preview = resp.text[:300].replace("\n", " ")
        raise RuntimeError(
            f"HTTPエラー: status={resp.status_code}, body_preview='{preview}'"
        )

    # 診断用: JSONとして読めなかった場合に、ステータスコードと中身の
    # 先頭部分をログに残す(GitHub Actions側からのアクセスがブロック
    # されている場合、空の本文や別内容が返ってくることがあるため)
    try:
        return resp.json()
    except ValueError as e:
        preview = resp.text[:300].replace("\n", " ")
        raise RuntimeError(
            f"JSON解析失敗: status={resp.status_code}, "
            f"content-length={len(resp.content)}, body_preview='{preview}'"
        ) from e


def load_jr_id_map(path=None):
    path = path or (DATA_DIR / "jr_id_map.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


TRAIN_TYPE_KEY = {
    "1": "hikari", "2": "kodama", "6": "nozomi",
    "10": "mizuho", "11": "sakura", "12": "tsubame",
}


def parse_train_positions(raw, stations, jr_id_map):
    """train_location_info.json の生データを、地図表示用の共通フォーマットに変換する。

    atStation    : 駅に停車中 -> その駅の座標をそのまま使う (status="stopped")
    betweenStation: 駅間走行中 -> 発車駅と、進行方向の次駅の中間点を使う (status="running")
                     ※ このAPIは「区間内のどこにいるか」までは返さないため、
                        簡易的に中間点(50%)で表示する。より滑らかにしたい場合は
                        時刻表ベースの補間(calculate_positions)と組み合わせる。

    bound "1" = 東京方面(上り) = stationOrder上のindexが減る方向
    bound "2" = 博多方面(下り) = stationOrder上のindexが増える方向
    betweenStationの"station"は「その駅を発車した直後」を意味する。
    """
    station_order = jr_id_map["stationOrder"]  # JR側ID。物理順(東京→博多)
    train_types = jr_id_map["trainTypes"]
    id_to_name = {jr_id: s["name"] for jr_id, s in zip(station_order,
                  sorted(stations.values(), key=lambda s: s["order"]))}

    positions = []
    info = raw.get("trainLocationInfo", {})

    # --- 停車中の列車 ---
    at_station = info.get("atStation", {}).get("bounds", {})
    for bound_id, entries in at_station.items():
        for entry in entries:
            st_id = entry["station"]
            st_name = id_to_name.get(st_id)
            if not st_name or st_name not in stations:
                continue
            st = stations[st_name]
            for t in entry.get("trains", []):
                positions.append({
                    "trip_id": f"{t['train']}-{t['trainNumber']}",
                    "train_number": f"{train_types.get(t['train'], '?')}{t['trainNumber']}号",
                    "train_type": TRAIN_TYPE_KEY.get(t['train'], "other"),
                    "direction": "up" if bound_id == "1" else "down",
                    "lat": st["lat"],
                    "lng": st["lng"],
                    "status": "stopped",
                    "current_segment": st_name,
                    "delay_min": t.get("delay", 0),
                })

    # --- 駅間走行中の列車 ---
    between = info.get("betweenStation", {}).get("bounds", {})
    for bound_id, entries in between.items():
        for entry in entries:
            st_id = entry["station"]
            if st_id not in station_order:
                continue
            idx = station_order.index(st_id)
            # bound "1"(上り): 次駅はindexが1つ小さい駅 / bound "2"(下り): 1つ大きい駅
            next_idx = idx - 1 if bound_id == "1" else idx + 1
            if not (0 <= next_idx < len(station_order)):
                continue
            from_name = id_to_name.get(st_id)
            to_name = id_to_name.get(station_order[next_idx])
            if not from_name or not to_name:
                continue
            if from_name not in stations or to_name not in stations:
                continue
            a, b = stations[from_name], stations[to_name]
            lat, lng = _interpolate(a["lat"], a["lng"], b["lat"], b["lng"], 0.5)

            for t in entry.get("trains", []):
                positions.append({
                    "trip_id": f"{t['train']}-{t['trainNumber']}",
                    "train_number": f"{train_types.get(t['train'], '?')}{t['trainNumber']}号",
                    "train_type": TRAIN_TYPE_KEY.get(t['train'], "other"),
                    "direction": "up" if bound_id == "1" else "down",
                    "lat": round(lat, 5),
                    "lng": round(lng, 5),
                    "status": "running",
                    "current_segment": f"{from_name}→{to_name}",
                    "delay_min": t.get("delay", 0),
                })

    return positions


# ---------------------------------------------------------------------------
# 2. 駅データ読み込み
# ---------------------------------------------------------------------------

def load_stations(path=None):
    """stations.jsonを読み込み、駅名 -> {lat, lng, order} の辞書を返す。"""
    path = path or (DATA_DIR / "stations.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {s["name"]: s for s in data["stations"]}


def load_trips(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["trips"]


# ---------------------------------------------------------------------------
# 3. 位置計算 (時刻表ベースの線形補間)
# ---------------------------------------------------------------------------

def _parse_hm(hm_str, base_date):
    """'HH:MM'文字列をbase_dateのdatetimeに変換 (JST)。"""
    h, m = map(int, hm_str.split(":"))
    return datetime.combine(base_date, time(h, m), tzinfo=JST)


def _interpolate(lat1, lng1, lat2, lng2, ratio):
    """2駅間を直線補間 (簡易版。将来は路線GeoJSONに沿った補間に差し替え可)。"""
    ratio = max(0.0, min(1.0, ratio))
    return lat1 + (lat2 - lat1) * ratio, lng1 + (lng2 - lng1) * ratio


def calculate_positions(trips, stations, now=None):
    """
    現在時刻(now)における各列車の位置を計算する。

    - 運行前 / 運行後の列車はスキップ (positionsに含めない)
    - 駅間走行中は直線補間で緯度経度を算出
    - 停車中(dep未到達)の場合はその駅の座標をそのまま返す

    戻り値: [{"trip_id", "train_number", "direction", "lat", "lng",
              "status", "current_segment"}]
    """
    now = now or datetime.now(JST)
    base_date = now.date()
    positions = []

    for trip in trips:
        stops = trip["stops"]
        # 各stopの発着時刻をdatetimeに変換 (Noneはスキップ用にそのまま保持)
        parsed = []
        for s in stops:
            arr = _parse_hm(s["arr"], base_date) if s.get("arr") else None
            dep = _parse_hm(s["dep"], base_date) if s.get("dep") else None
            parsed.append({"station": s["station"], "arr": arr, "dep": dep})

        # 始発前 / 終着後は対象外
        first_time = parsed[0]["dep"] or parsed[0]["arr"]
        last_time = parsed[-1]["arr"] or parsed[-1]["dep"]
        if now < first_time or now > last_time:
            continue

        # 現在どの区間にいるか探索
        found = False
        for i in range(len(parsed) - 1):
            seg_start = parsed[i]["dep"] or parsed[i]["arr"]
            seg_end = parsed[i + 1]["arr"] or parsed[i + 1]["dep"]

            if seg_start <= now <= seg_end:
                st_from = stations.get(parsed[i]["station"])
                st_to = stations.get(parsed[i + 1]["station"])
                if not st_from or not st_to:
                    break  # stations.jsonに駅名が無い場合はスキップ

                total = (seg_end - seg_start).total_seconds()
                elapsed = (now - seg_start).total_seconds()
                ratio = elapsed / total if total > 0 else 0

                lat, lng = _interpolate(
                    st_from["lat"], st_from["lng"], st_to["lat"], st_to["lng"], ratio
                )

                # 停車中かどうか判定 (発車時刻に達していない= 出発駅で停車中)
                status = "stopped" if ratio <= 0.02 else "running"

                positions.append({
                    "trip_id": trip["trip_id"],
                    "train_number": trip["train_number"],
                    "train_type": trip.get("train_type", "other"),
                    "direction": trip["direction"],
                    "lat": round(lat, 5),
                    "lng": round(lng, 5),
                    "status": status,
                    "current_segment": f"{parsed[i]['station']}→{parsed[i+1]['station']}",
                })
                found = True
                break

        if not found:
            continue

    return positions


def save_positions(positions, output_path=None):
    output_path = Path(output_path or (DATA_DIR / "train_positions.json"))
    payload = {
        "updated_at": datetime.now(JST).isoformat(),
        "count": len(positions),
        "trains": positions,
    }
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"saved: {output_path} ({len(positions)} trains)")


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------

def main():
    stations = load_stations()
    positions = []

    # 1. まずリアルタイムAPIを試す(東海道・山陽新幹線のみカバー、博多まで)
    api_ok = False
    try:
        jr_id_map = load_jr_id_map()
        raw = fetch_train_positions()
        positions.extend(parse_train_positions(raw, stations, jr_id_map))
        api_ok = True
        print("[info] リアルタイムAPIから取得しました(東海道・山陽)")
    except Exception as e:
        print(f"[warn] リアルタイムAPI取得に失敗しました: {e}")

    # 2. APIが失敗した場合のみ、東海道・山陽も時刻表ベースで補う
    if not api_ok:
        try:
            trips = []
            for fname in ["trips_kodama.json", "trips_hikari.json"]:
                path = DATA_DIR / fname
                if path.exists():
                    trips.extend(load_trips(path))
            fallback = calculate_positions(trips, stations)
            positions.extend(fallback)
            print(f"[info] 時刻表ベースで東海道・山陽 {len(fallback)} 本を計算しました")
        except Exception as e:
            print(f"[warn] 東海道・山陽の時刻表ベース計算に失敗しました: {e}")

    # 3. 九州新幹線はAPIの対象外(博多で追跡が止まる)なので、
    #    APIの成否に関わらず常に時刻表ベースで補う
    try:
        kyushu_path = DATA_DIR / "trips_kyushu.json"
        if kyushu_path.exists():
            kyushu_trips = load_trips(kyushu_path)
            kyushu_positions = calculate_positions(kyushu_trips, stations)
            positions.extend(kyushu_positions)
            print(f"[info] 時刻表ベースで九州新幹線 {len(kyushu_positions)} 本を計算しました")
    except Exception as e:
        print(f"[warn] 九州新幹線の時刻表ベース計算に失敗しました: {e}")

    if positions:
        save_positions(positions)
    else:
        print("[warn] train_positions.json は更新しません(既存データを保持)")


if __name__ == "__main__":
    main()
