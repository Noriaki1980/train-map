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
import re
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

# Sparkling Dreams Shinkansen (2026年6月19日〜運行のディズニー特別編成)
# 日によって3パターン(A/B/C)あるが、使われる列車番号はこの4つの組み合わせのみ
SPARKLING_DREAMS_TRAIN_NUMBERS = {"636", "815", "836", "659"}


def is_sparkling_dreams(train_number_raw):
    return train_number_raw in SPARKLING_DREAMS_TRAIN_NUMBERS


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
                    "is_special": is_sparkling_dreams(t['trainNumber']),
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
            lat = lng = None
            bearing = None
            result = _interpolate_on_any_detailed_track(from_name, to_name, 0.5)
            if result:
                lat, lng, bearing = result
            if lat is None:
                lat, lng = _interpolate(a["lat"], a["lng"], b["lat"], b["lng"], 0.5)

            for t in entry.get("trains", []):
                positions.append({
                    "trip_id": f"{t['train']}-{t['trainNumber']}",
                    "train_number": f"{train_types.get(t['train'], '?')}{t['trainNumber']}号",
                    "train_type": TRAIN_TYPE_KEY.get(t['train'], "other"),
                    "is_special": is_sparkling_dreams(t['trainNumber']),
                    "direction": "up" if bound_id == "1" else "down",
                    "lat": round(lat, 5),
                    "lng": round(lng, 5),
                    "bearing": round(bearing, 1) if bearing is not None else None,
                    "status": "running",
                    "current_segment": f"{from_name}→{to_name}",
                    "delay_min": t.get("delay", 0),
                })

    return positions


# ---------------------------------------------------------------------------
# 1.5 どこトレ (JR東日本 秋田・山形新幹線) リアルタイムAPI
# ---------------------------------------------------------------------------

DOKOTRAIN_STATUS_URL = "https://doko-train.jp/json/trainstatus/{line_id}.json"

DOKOTRAIN_LINES = {
    "komachi": {"line_id": "110A", "stations_file": "stations_akita.json", "label": "こまち"},
    "tsubasa": {"line_id": "902Y", "stations_file": "stations_yamagata.json", "label": "つばさ"},
}


def load_id_stations(path):
    """doko-train用: 駅ID(文字列) -> {name, lat, lng} の辞書を返す。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {s["id"]: s for s in data["stations"]}


def fetch_dokotrain_status(line_id):
    if requests is None:
        raise RuntimeError("requests がインストールされていません")
    url = DOKOTRAIN_STATUS_URL.format(line_id=line_id)
    resp = requests.get(url, headers=API_HEADERS, timeout=10)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTPエラー: status={resp.status_code}")
    return resp.json()


def parse_dokotrain_positions(raw, stations_by_id, train_type, train_label):
    """
    どこトレのtrainstatus APIレスポンスをtrain_positions.json形式に変換する。

    CUR_STATION が "0" 以外 → その駅に停車中
    CUR_STATION が "0"      → PRE_STATION(直前駅)〜POS_STATION(次駅)の間を走行中
                                (区間内の正確な割合は分からないため中間点(50%)で近似)
    """
    positions = []
    train_status = raw.get("LINE_STATUS", {}).get("TRAIN_STATUS", {})

    for key, t in train_status.items():
        cur = t.get("CUR_STATION", "0")
        pre = t.get("PRE_STATION")
        pos = t.get("POS_STATION")
        bound = t.get("BOUND", "")
        delay = t.get("LATENCY", 0) or 0
        name = t.get("TRAIN_NNAME") or train_label
        no = t.get("TRAIN_NNO", "")

        lat = lng = None
        status = None
        segment = None

        if cur and cur != "0" and cur in stations_by_id:
            st = stations_by_id[cur]
            lat, lng = st["lat"], st["lng"]
            status = "stopped"
            segment = st["name"]
        elif pre in stations_by_id and pos in stations_by_id:
            a, b = stations_by_id[pre], stations_by_id[pos]
            result = _interpolate_on_any_detailed_track(a["name"], b["name"], 0.5)
            if result:
                lat, lng, _bearing = result
            else:
                lat, lng = _interpolate(a["lat"], a["lng"], b["lat"], b["lng"], 0.5)
            status = "running"
            segment = f"{a['name']}→{b['name']}"

        if lat is None:
            continue  # 駅IDが未知で位置を特定できない場合はスキップ

        positions.append({
            "trip_id": f"{train_type}_{key}",
            "train_number": f"{name}{no}号",
            "train_type": train_type,
            "is_special": False,
            "direction": "up" if bound == "1" else "down",
            "lat": round(lat, 5),
            "lng": round(lng, 5),
            "status": status,
            "current_segment": segment,
            "delay_min": delay,
        })

    return positions


def fetch_dokotrain_line(key):
    """akita/yamagataいずれかの1路線分をまとめて取得・変換する。"""
    conf = DOKOTRAIN_LINES[key]
    stations_path = DATA_DIR / conf["stations_file"]
    stations_by_id = load_id_stations(stations_path)
    raw = fetch_dokotrain_status(conf["line_id"])
    return parse_dokotrain_positions(raw, stations_by_id, key, conf["label"])


# ---------------------------------------------------------------------------
# 1.6 北海道新幹線 リアルタイムAPI
# ---------------------------------------------------------------------------

HOKKAIDO_LOCATION_URL = "https://www3.jrhokkaido.co.jp/trainlocation/json/location/now/location_15_now.json"

# 北海道新幹線区間の駅 (奥津軽いまべつ・木古内・新函館北斗の3駅のみ、2026年時点)
HOKKAIDO_STATIONS = {
    "002": {"name": "奥津軽いまべつ", "lat": 41.0764, "lng": 140.5325},
    "018": {"name": "木古内",       "lat": 41.6772, "lng": 140.4267},
    "034": {"name": "新函館北斗",   "lat": 41.9061, "lng": 140.6486},
}


HOKKAIDO_POS_RE = re.compile(r"P(\d)([UD])")


def fetch_hokkaido_positions():
    """
    北海道新幹線の列車位置を取得する。

    `pos` フィールド(例: "R12P7U")は、実測で解読した結果、以下の構造と判明:
      - "P"に続く数字(0〜9): 奥津軽いまべつ(0)〜新函館北斗(9)間の固定スケールでの
        おおまかな位置(列車の向きに関係ない地理的な進捗)
      - 末尾のU/D: 進行方向(U=新函館北斗方面/下り、D=東京方面/上り)
    ※ 数字が1つ(0〜9の10段階)なので、駅間の正確な位置ではなく粗い近似。
    """
    if requests is None:
        raise RuntimeError("requests がインストールされていません")
    resp = requests.get(HOKKAIDO_LOCATION_URL, headers=API_HEADERS, timeout=10)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTPエラー: status={resp.status_code}")
    # このAPIはレスポンス先頭にUTF-8 BOMが付いているため、そのままだと
    # resp.json()がデコードエラーになる。utf-8-sigで明示的にデコードする。
    text = resp.content.decode("utf-8-sig")
    raw = json.loads(text)

    positions = []
    kikonai = HOKKAIDO_STATIONS["018"]  # 解読失敗時の暫定フォールバック(木古内)

    for t in raw.get("trains", []):
        pos_str = t.get("pos", "") or ""
        m = HOKKAIDO_POS_RE.search(pos_str)

        lat = lng = None
        direction = "unknown"
        segment_note = "奥津軽いまべつ〜新函館北斗(区間内位置は詳細不明のため木古内駅で近似)"

        if m:
            digit, ud = int(m.group(1)), m.group(2)
            ratio = digit / 9
            direction = "down" if ud == "U" else "up"
            result = _interpolate_on_any_detailed_track("奥津軽いまべつ", "新函館北斗", ratio)
            if result:
                lat, lng, _bearing = result
                segment_note = f"奥津軽いまべつ〜新函館北斗(進捗 {digit}/9、{'下り' if ud=='U' else '上り'})"

        if lat is None:
            lat, lng = kikonai["lat"], kikonai["lng"]

        positions.append({
            "trip_id": f"hokkaido_{t.get('cbango', '?')}",
            "train_number": f"はやぶさ{t.get('cbango', '?')}",
            "train_type": "hayabusa",
            "is_special": False,
            "direction": direction,
            "lat": round(lat, 5),
            "lng": round(lng, 5),
            "status": "running",
            "current_segment": segment_note,
            "delay_min": t.get("chien", 0) or 0,
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


def _build_order_index(stations):
    """order順にソートした駅名リストを返す。"""
    return sorted(stations.keys(), key=lambda n: stations[n]["order"])


def _route_via_points(stations, order_list, from_name, to_name):
    """
    from_name〜to_name間で、実際に線路上を通る駅(通過駅も含む)の
    緯度経度情報を順番に並べて返す。stations の order フィールドに基づく。
    """
    from_order = stations[from_name]["order"]
    to_order = stations[to_name]["order"]
    if from_order <= to_order:
        names = [n for n in order_list if from_order <= stations[n]["order"] <= to_order]
    else:
        names = [n for n in order_list if to_order <= stations[n]["order"] <= from_order]
        names = list(reversed(names))
    if not names:
        names = [from_name, to_name]
    return [stations[n] for n in names]


def _haversine_km(lat1, lng1, lat2, lng2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _interpolate_along_route(via_points, ratio):
    """
    via_points(通過駅も含む緯度経度のリスト)に沿って、駅間の実距離を基準に
    ratio(0〜1)の地点の座標を求める。通過駅がある区間でも路線の折れ線に
    沿って動くようにするための処理。
    """
    if len(via_points) < 2:
        p = via_points[0]
        return p["lat"], p["lng"]

    dists = [
        _haversine_km(via_points[i]["lat"], via_points[i]["lng"],
                      via_points[i + 1]["lat"], via_points[i + 1]["lng"])
        for i in range(len(via_points) - 1)
    ]
    total = sum(dists)
    if total == 0:
        return via_points[0]["lat"], via_points[0]["lng"]

    target = total * max(0.0, min(1.0, ratio))
    cum = 0.0
    for i, d in enumerate(dists):
        if cum + d >= target or i == len(dists) - 1:
            local_ratio = (target - cum) / d if d > 0 else 0
            return _interpolate(
                via_points[i]["lat"], via_points[i]["lng"],
                via_points[i + 1]["lat"], via_points[i + 1]["lng"], local_ratio
            )
        cum += d
    return via_points[-1]["lat"], via_points[-1]["lng"]


def _load_one_detailed_track(track_filename, meta_filename):
    track_path = DATA_DIR / track_filename
    meta_path = DATA_DIR / meta_filename
    if not (track_path.exists() and meta_path.exists()):
        return None
    with open(track_path, encoding="utf-8") as f:
        track = json.load(f)["coordinates"]  # [[lat,lng], ...]
    with open(meta_path, encoding="utf-8") as f:
        station_arc = json.load(f)["station_arc"]

    cum = [0.0]
    for i in range(1, len(track)):
        cum.append(cum[i - 1] + _haversine_km(track[i - 1][0], track[i - 1][1], track[i][0], track[i][1]))

    return {"track": track, "cum": cum, "station_arc": station_arc}


def _load_detailed_tracks():
    """
    実際の線路形状データ(区間ごとに複数)を読み込む。
    各要素は _load_one_detailed_track() の戻り値。読み込めなかった区間は
    リストに含めない(1区間の失敗が他区間に影響しないようにする)。
    """
    specs = [
        ("track_tokaido.json", "track_tokaido_meta.json"),
        ("track_sanyo.json", "track_sanyo_meta.json"),
        ("track_kyushu.json", "track_kyushu_meta.json"),
        ("track_tohoku.json", "track_tohoku_meta.json"),
        ("track_joetsu.json", "track_joetsu_meta.json"),
        ("track_hokkaido.json", "track_hokkaido_meta.json"),
        ("track_tazawako.json", "track_tazawako_meta.json"),
        ("track_akita_omagari.json", "track_akita_omagari_meta.json"),
        ("track_yamagata.json", "track_yamagata_meta.json"),
        ("track_nishikyushu.json", "track_nishikyushu_meta.json"),
    ]
    tracks = []
    for track_file, meta_file in specs:
        try:
            t = _load_one_detailed_track(track_file, meta_file)
            if t:
                tracks.append(t)
        except Exception as e:
            print(f"[warn] 詳細線路データ({track_file})の読み込みに失敗しました: {e}")
    return tracks


try:
    _DETAILED_TRACKS = _load_detailed_tracks()
except Exception as _e:
    print(f"[warn] 詳細線路データの読み込みに失敗しました(通常の駅間補間にフォールバックします): {_e}")
    _DETAILED_TRACKS = []


def _bearing_deg(lat1, lng1, lat2, lng2):
    """2点間の方位角(度、北=0、東=90)を返す。"""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlmb = math.radians(lng2 - lng1)
    y = math.sin(dlmb) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlmb)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _interpolate_on_detailed_track(detailed, from_name, to_name, ratio):
    """
    detailed(_load_one_detailed_trackの戻り値)を使い、from_name〜to_name間を
    実際の線路カーブに沿って ratio(0〜1) の地点まで進んだ座標と、その地点での
    進行方向(bearing、度)を返す。(lat, lng, bearing) のタプル。
    どちらかの駅がこの詳細データの対象外、または区間の実距離がほぼ0
    (データの起点が2駅分をまとめて指しているなど)の場合はNoneを返す。
    """
    arc = detailed["station_arc"]
    if from_name not in arc or to_name not in arc:
        return None

    from_km = arc[from_name]["arc_km"]
    to_km = arc[to_name]["arc_km"]
    if abs(to_km - from_km) < 0.5:  # 区間の実距離がほぼ0 = データが駅を区別できていない
        return None

    lo, hi = (from_km, to_km) if from_km <= to_km else (to_km, from_km)
    forward = from_km <= to_km
    target_km = lo + (hi - lo) * ratio if forward else hi - (hi - lo) * ratio

    cum = detailed["cum"]
    track = detailed["track"]
    # target_kmに対応するtrack上の区間を探す
    for i in range(len(cum) - 1):
        if cum[i] <= target_km <= cum[i + 1]:
            seg_len = cum[i + 1] - cum[i]
            local_ratio = (target_km - cum[i]) / seg_len if seg_len > 0 else 0
            lat, lng = _interpolate(track[i][0], track[i][1], track[i + 1][0], track[i + 1][1], local_ratio)
            bearing = _bearing_deg(track[i][0], track[i][1], track[i + 1][0], track[i + 1][1])
            if not forward:
                bearing = (bearing + 180) % 360  # 上り方向は逆向き
            return lat, lng, bearing
    lat, lng = (track[-1][0], track[-1][1]) if target_km >= cum[-1] else (track[0][0], track[0][1])
    bearing = _bearing_deg(track[-2][0], track[-2][1], track[-1][0], track[-1][1])
    if not forward:
        bearing = (bearing + 180) % 360
    return lat, lng, bearing


def _interpolate_on_any_detailed_track(from_name, to_name, ratio):
    """登録済みの詳細トラックを順に試し、最初に見つかった結果を返す。無ければNone。"""
    for detailed in _DETAILED_TRACKS:
        result = _interpolate_on_detailed_track(detailed, from_name, to_name, ratio)
        if result:
            return result
    return None


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
    order_list = _build_order_index(stations)

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

                # まず実際の線路形状(東京〜京都・山陽など)があればそれを使う
                lat = lng = None
                bearing = None
                result = _interpolate_on_any_detailed_track(
                    parsed[i]["station"], parsed[i + 1]["station"], ratio
                )
                if result:
                    lat, lng, bearing = result

                if lat is None:
                    # 通過駅も経由地として使い、路線の折れ線に沿って移動させる(フォールバック)
                    via_points = _route_via_points(
                        stations, order_list, parsed[i]["station"], parsed[i + 1]["station"]
                    )
                    lat, lng = _interpolate_along_route(via_points, ratio)

                # 停車中かどうか判定 (発車時刻に達していない= 出発駅で停車中)
                status = "stopped" if ratio <= 0.02 else "running"

                raw_train_no = trip["trip_id"].split("_", 1)[-1]
                positions.append({
                    "trip_id": trip["trip_id"],
                    "train_number": trip["train_number"],
                    "train_type": trip.get("train_type", "other"),
                    "is_special": is_sparkling_dreams(raw_train_no),
                    "direction": trip["direction"],
                    "lat": round(lat, 5),
                    "lng": round(lng, 5),
                    "bearing": round(bearing, 1) if bearing is not None else None,
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

    # 4. 東北新幹線(なすの・やまびこ・はやぶさ/はやて)は、どこトレの調査は
    #    済んでいるがAPI組み込みは別途対応のため、現時点では常に時刻表ベース
    try:
        tohoku_stations_path = DATA_DIR / "stations_tohoku.json"
        if tohoku_stations_path.exists():
            tohoku_stations = load_stations(tohoku_stations_path)
            tohoku_trips = []
            for fname in ["trips_nasuno.json", "trips_yamabiko.json", "trips_hayabusa.json"]:
                path = DATA_DIR / fname
                if path.exists():
                    tohoku_trips.extend(load_trips(path))
            tohoku_positions = calculate_positions(tohoku_trips, tohoku_stations)
            positions.extend(tohoku_positions)
            print(f"[info] 時刻表ベースで東北新幹線 {len(tohoku_positions)} 本を計算しました")
    except Exception as e:
        print(f"[warn] 東北新幹線の時刻表ベース計算に失敗しました: {e}")

    # 5. 上越新幹線(とき・たにがわ)も同様に常に時刻表ベース
    try:
        joetsu_stations_path = DATA_DIR / "stations_joetsu.json"
        joetsu_trips_path = DATA_DIR / "trips_joetsu.json"
        if joetsu_stations_path.exists() and joetsu_trips_path.exists():
            joetsu_stations = load_stations(joetsu_stations_path)
            joetsu_trips = load_trips(joetsu_trips_path)
            joetsu_positions = calculate_positions(joetsu_trips, joetsu_stations)
            positions.extend(joetsu_positions)
            print(f"[info] 時刻表ベースで上越新幹線 {len(joetsu_positions)} 本を計算しました")
    except Exception as e:
        print(f"[warn] 上越新幹線の時刻表ベース計算に失敗しました: {e}")

    # 西九州新幹線(かもめ)もリアルタイムAPIが無いため常に時刻表ベース
    try:
        nk_stations_path = DATA_DIR / "stations_nishikyushu.json"
        nk_trips_path = DATA_DIR / "trips_kamome.json"
        if nk_stations_path.exists() and nk_trips_path.exists():
            nk_stations = load_stations(nk_stations_path)
            nk_trips = load_trips(nk_trips_path)
            nk_positions = calculate_positions(nk_trips, nk_stations)
            positions.extend(nk_positions)
            print(f"[info] 時刻表ベースで西九州新幹線 {len(nk_positions)} 本を計算しました")
    except Exception as e:
        print(f"[warn] 西九州新幹線の時刻表ベース計算に失敗しました: {e}")

    # 6. どこトレ(秋田新幹線こまち・山形新幹線つばさ)はリアルタイムAPIが
    #    使えるので、時刻表ではなくこちらを優先する(ベストエフォート、
    #    失敗しても他の路線の表示には影響させない)
    for key in ("komachi", "tsubasa"):
        try:
            line_positions = fetch_dokotrain_line(key)
            positions.extend(line_positions)
            print(f"[info] どこトレAPIから{DOKOTRAIN_LINES[key]['label']} {len(line_positions)} 本を取得しました")
        except Exception as e:
            print(f"[warn] どこトレAPI({key})の取得に失敗しました: {e}")

    # 7. 北海道新幹線もリアルタイムAPIを試す(区間内の詳細位置は未解読の近似)
    try:
        hokkaido_positions = fetch_hokkaido_positions()
        positions.extend(hokkaido_positions)
        print(f"[info] 北海道新幹線APIから {len(hokkaido_positions)} 本を取得しました")
    except Exception as e:
        print(f"[warn] 北海道新幹線APIの取得に失敗しました: {e}")

    if positions:
        save_positions(positions)
    else:
        print("[warn] train_positions.json は更新しません(既存データを保持)")


if __name__ == "__main__":
    main()
