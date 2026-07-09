import argparse
import json
import math
import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parent
SERVICE_URL = "http://www.fjqxfw.cn:8096/ztq30_fj_jc/service.do"
IMAGE_BASE = "http://www.fjqxfw.cn:8099/ftp/"
FTP_BASE = "http://www.fjqxfw.cn:8099/ftp/"
WEATHER_AREA_URL = FTP_BASE + "ztq_fj/ztq_area/and/lv3.json"
OBS_STATION_URL = FTP_BASE + "ztq/ztq_area/and/2Fj_stations20260626102716.json"
CWA_API_KEY = os.environ.get("CWA_API_KEY") or os.environ.get("CWB_API_KEY") or ""
CWA_OBS_URL = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0001-001"
TOKEN = "10002-45aad81b00cdfd743b1c6cdb8aaf2b9aef18ec05"
USER_ID = "202309275065959"
USER_AGENT = "zhi tian qi-jue ce/4.0.13 (iPhone; iOS 26.5; Scale/3.00)"
CACHE_FILE = ROOT / "weather_runtime_cache.json"
CURRENT_TTL_SECONDS = 600
HISTORY_TTL_SECONDS = 900
STATION_TTL_SECONDS = 600
CWA_TTL_SECONDS = 600
CACHE: Dict[str, Any] = {"current_weather": {}, "station_history": {}, "weather_history": {}}
TREND_TYPES = {
    "temperature": {"type": 11, "label": "气温", "unit": "°C"},
    "rainfall": {"type": 10, "label": "雨量", "unit": "mm"},
    "wind": {"type": 12, "label": "风况", "unit": "m/s"},
    "humidity": {"type": 17, "label": "湿度", "unit": "%"},
    "visibility": {"type": 13, "label": "能见度", "unit": "m"},
    "pressure": {"type": 14, "label": "气压", "unit": "hPa"},
}


def request_json(payload: Dict[str, Any]) -> Dict[str, Any]:
    body = urllib.parse.urlencode({"p": json.dumps(payload, ensure_ascii=False)}).encode("utf-8")
    request = urllib.request.Request(
        SERVICE_URL,
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read()

    # The service returns valid JSON but some Chinese fields are not reliably UTF-8.
    text = raw.decode("utf-8", errors="replace")
    if "\ufffd" in text:
        text = raw.decode("gb18030", errors="replace")
    return repair_text(json.loads(text))


def load_runtime_cache() -> None:
    if not CACHE_FILE.exists():
        return
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    for key in ["current_weather", "station_history", "weather_history"]:
        if isinstance(data.get(key), dict):
            CACHE[key] = data[key]


def save_runtime_cache() -> None:
    payload = {
        "current_weather": CACHE.get("current_weather", {}),
        "station_history": CACHE.get("station_history", {}),
        "weather_history": CACHE.get("weather_history", {}),
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def cache_fresh(entry: Dict[str, Any], ttl_seconds: int) -> bool:
    fetched = entry.get("fetched_at_epoch")
    return isinstance(fetched, (int, float)) and datetime.now().timestamp() - fetched < ttl_seconds


def fetch_url_json(url: str) -> Dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
    text = raw.decode("utf-8", errors="replace")
    return repair_text(json.loads(text))


def fetch_cwa_json(resource_url: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not CWA_API_KEY:
        raise RuntimeError("CWA_API_KEY is not configured")
    query = {"Authorization": CWA_API_KEY, "format": "JSON"}
    if params:
        query.update({key: value for key, value in params.items() if value not in {"", None}})
    url = resource_url + "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8", errors="replace"))


def repair_text(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: repair_text(item) for key, item in value.items()}
    if isinstance(value, list):
        return [repair_text(item) for item in value]
    if not isinstance(value, str):
        return value

    # Several endpoints return text that looks like UTF-8 bytes decoded as GBK.
    # This recovers common strings such as "绂忓缓" -> "福建".
    try:
        repaired = value.encode("gb18030", errors="strict").decode("utf-8", errors="strict")
    except UnicodeError:
        return value.replace("\ufffd", "")

    score_before = sum(1 for char in value if "\u4e00" <= char <= "\u9fff")
    score_after = sum(1 for char in repaired if "\u4e00" <= char <= "\u9fff")
    if score_after >= score_before and repaired != value:
        return repaired
    return value.replace("\ufffd", "")


def get_station_cache() -> Dict[str, Any]:
    if "stations" in CACHE:
        return CACHE["stations"]

    weather_rows = fetch_url_json(WEATHER_AREA_URL).get("ROW", [])
    obs_rows = fetch_url_json(OBS_STATION_URL).get("ROW", [])

    weather_areas = []
    for row in weather_rows:
        lon, lat = parse_location(row.get("LOCATION", ""))
        if lon is None or lat is None:
            continue
        weather_areas.append(
            {
                "kind": "weather_area",
                "id": str(row.get("ID", "")),
                "name": row.get("NAME", ""),
                "city": row.get("CITY", ""),
                "parent_id": str(row.get("PARENT_ID", "")),
                "code": str(row.get("CODE", "")),
                "pinyin": row.get("PINGYIN", ""),
                "py": row.get("PY", ""),
                "lon": lon,
                "lat": lat,
            }
        )

    obs_stations = []
    for row in obs_rows:
        lon = parse_float(row.get("LONGITUDE"))
        lat = parse_float(row.get("LATITUDE"))
        if lon is None or lat is None:
            continue
        obs_stations.append(
            {
                "kind": "observation_station",
                "id": str(row.get("ID", "")),
                "station_id": str(row.get("STATIONID", "")),
                "name": row.get("STATIONNAME", ""),
                "lon": lon,
                "lat": lat,
            }
        )

    attach_nearest_city(obs_stations, weather_areas)

    CACHE["stations"] = {
        "weather_areas": weather_areas,
        "obs_stations": obs_stations,
        "loaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return CACHE["stations"]


def attach_nearest_city(obs_stations: List[Dict[str, Any]], weather_areas: List[Dict[str, Any]]) -> None:
    if not obs_stations or not weather_areas:
        return
    for station in obs_stations:
        nearest = min(
            weather_areas,
            key=lambda area: (station["lon"] - area["lon"]) ** 2 + (station["lat"] - area["lat"]) ** 2,
        )
        station["city"] = nearest.get("city", "")
        station["area_id"] = nearest.get("id", "")
        station["area_name"] = nearest.get("name", "")


def parse_location(value: str) -> tuple[Any, Any]:
    parts = str(value or "").split(",")
    if len(parts) != 2:
        return None, None
    return parse_float(parts[0]), parse_float(parts[1])


def parse_float(value: Any) -> Any:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def distance_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius = 6371.0088
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def search_stations(keyword: str, limit: int) -> Dict[str, Any]:
    cache = get_station_cache()
    key = keyword.strip().lower()

    def match(row: Dict[str, Any]) -> bool:
        haystack = " ".join(
            str(row.get(field, ""))
            for field in ["id", "station_id", "name", "city", "pinyin", "py", "code"]
        ).lower()
        return key in haystack

    if not key:
        weather = cache["weather_areas"][:limit]
        obs = cache["obs_stations"][:limit]
    else:
        weather = [row for row in cache["weather_areas"] if match(row)][:limit]
        obs = [row for row in cache["obs_stations"] if match(row)][:limit]
    auto_stations = search_auto_stations(keyword, limit) if key else []

    return {
        "ok": True,
        "loaded_at": cache["loaded_at"],
        "weather_areas": weather,
        "obs_stations": obs,
        "auto_stations": auto_stations,
        "counts": {
            "weather_areas": len(cache["weather_areas"]),
            "obs_stations": len(cache["obs_stations"]),
        },
    }


def search_auto_stations(keyword: str, limit: int) -> List[Dict[str, Any]]:
    payload = {
        "h": {"p": "", "path": "queryStation"},
        "b": {"station_info_controller": {"station_name": keyword, "is_fj": None}},
    }
    data = request_json(payload)
    rows = data.get("b", {}).get("station_info_controller", {}).get("data", []) or []
    result = []
    for row in rows[:limit]:
        result.append(
            {
                "kind": "auto_station",
                "station_id": str(row.get("station_id", "")),
                "name": row.get("station_name", ""),
            }
        )
    return result


def nearest_stations(lon: float, lat: float, limit: int) -> Dict[str, Any]:
    cache = get_station_cache()

    def attach_distance(row: Dict[str, Any]) -> Dict[str, Any]:
        clone = dict(row)
        clone["distance_km"] = round(distance_km(lon, lat, row["lon"], row["lat"]), 3)
        return clone

    weather = sorted((attach_distance(row) for row in cache["weather_areas"]), key=lambda row: row["distance_km"])[:limit]
    obs = sorted((attach_distance(row) for row in cache["obs_stations"]), key=lambda row: row["distance_km"])[:limit]

    return {
        "ok": True,
        "loaded_at": cache["loaded_at"],
        "origin": {"lon": lon, "lat": lat},
        "weather_areas": weather,
        "obs_stations": obs,
        "counts": {
            "weather_areas": len(cache["weather_areas"]),
            "obs_stations": len(cache["obs_stations"]),
        },
    }


def list_weather_cities() -> Dict[str, Any]:
    cache = get_station_cache()
    counts: Dict[str, int] = {}
    for row in cache["obs_stations"]:
        city = row.get("city") or "未分组"
        counts[city] = counts.get(city, 0) + 1
    cities = [{"name": name, "count": count} for name, count in sorted(counts.items(), key=lambda item: item[0])]
    return {
        "ok": True,
        "loaded_at": cache["loaded_at"],
        "cities": cities,
        "total_auto_stations": len(cache["obs_stations"]),
    }


def normalize_city_name(city: str) -> str:
    city = str(city or "").strip()
    return city[:-1] if city.endswith("市") else city


def city_matches(row: Dict[str, Any], cities: List[str]) -> bool:
    if not cities:
        return True
    row_city = normalize_city_name(row.get("city", ""))
    city_set = {normalize_city_name(city) for city in cities if city}
    return row_city in city_set


def gis_current(query: Dict[str, List[str]]) -> Dict[str, Any]:
    cache = get_station_cache()
    limit = max(1, min(int(query.get("limit", ["600"])[0]), len(cache["obs_stations"])))
    realtime_limit = max(0, min(int(query.get("realtime_limit", ["240"])[0]), limit))
    hide_empty = query.get("hide_empty", ["0"])[0] in {"1", "true", "yes"}
    include_taiwan = query.get("include_taiwan", ["0"])[0] in {"1", "true", "yes"}
    keyword = query.get("q", [""])[0].strip().lower()
    city = query.get("city", [""])[0].strip()
    cities = [item.strip() for item in query.get("cities", [""])[0].split(",") if item.strip()]
    if city and not cities:
        cities = [city]
    bbox_text = query.get("bbox", [""])[0]

    rows = cache["obs_stations"]
    if cities:
        rows = [row for row in rows if city_matches(row, cities)]
    if keyword:
        rows = [
            row for row in rows
            if keyword in " ".join(str(row.get(field, "")) for field in ["name", "city", "id", "pinyin", "py"]).lower()
        ]
    bbox = parse_bbox(bbox_text)
    if bbox:
        min_lon, min_lat, max_lon, max_lat = bbox
        rows = [row for row in rows if min_lon <= row["lon"] <= max_lon and min_lat <= row["lat"] <= max_lat]

    selected = rows[:limit]
    current_by_station: Dict[str, Dict[str, Any]] = {}
    features = []
    errors = []
    realtime_rows = selected[:realtime_limit]
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(fetch_cached_station_realtime, row["station_id"]): row for row in realtime_rows}
        for future in as_completed(futures):
            row = futures[future]
            try:
                current_by_station[row["station_id"]] = future.result()
            except Exception as exc:
                errors.append({"station_id": row["station_id"], "name": row["name"], "error": str(exc)})

    for row in selected:
        current_data = current_by_station.get(row["station_id"]) or get_cached_station_realtime(row["station_id"])
        current = normalize_station_weather(current_data, row)
        has_current = any(current.get(field) not in {"", None} for field in ["temperature", "humidity", "pressure", "wind"])
        if hide_empty and not has_current:
            continue
        features.append(
            {
                "area_id": row.get("area_id", ""),
                "station_id": row["station_id"],
                "name": row["name"],
                "city": row.get("city", ""),
                "area_name": row.get("area_name", ""),
                "lon": row["lon"],
                "lat": row["lat"],
                "updated": current.get("updated", ""),
                "temperature": current.get("temperature", ""),
                "humidity": current.get("humidity", ""),
                "rainfall": current.get("rainfall", ""),
                "pressure": current.get("pressure", ""),
                "wind": current.get("wind", ""),
                "wind_dir": current.get("wind_dir", ""),
                "current": current,
                "stats24h": summarize_local_history(row["station_id"]),
            }
        )

    if include_taiwan:
        try:
            taiwan_rows = fetch_cached_cwa_observations()
            if keyword:
                taiwan_rows = [
                    row for row in taiwan_rows
                    if keyword in " ".join(str(row.get(field, "")) for field in ["station_id", "name", "city", "area_name"]).lower()
                ]
            if bbox:
                min_lon, min_lat, max_lon, max_lat = bbox
                taiwan_rows = [
                    row for row in taiwan_rows
                    if min_lon <= float(row["lon"]) <= max_lon and min_lat <= float(row["lat"]) <= max_lat
                ]
            if hide_empty:
                taiwan_rows = [
                    row for row in taiwan_rows
                    if any(row.get(field) not in {"", None} for field in ["temperature", "humidity", "pressure", "wind"])
                ]
            features.extend(taiwan_rows)
        except Exception as exc:
            errors.append({"source": "taiwan_cwa", "error": str(exc)})

    features.sort(key=lambda item: (item.get("city", ""), item.get("name", "")))
    realtime_count = sum(1 for item in features if item.get("updated"))
    save_runtime_cache()
    return {
        "ok": True,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "auto stations + fycx_sstq + local rolling cache",
        "city": city,
        "cities": cities,
        "total_auto_stations": len(cache["obs_stations"]),
        "matched_auto_stations": len(rows),
        "matched_weather_areas": len(rows),
        "returned": len(features),
        "realtime_requested": realtime_limit,
        "realtime_returned": realtime_count,
        "features": features,
        "errors": errors[:20],
    }


def parse_bbox(value: str) -> Any:
    if not value:
        return None
    try:
        parts = [float(part) for part in value.split(",")]
    except ValueError:
        return None
    if len(parts) != 4:
        return None
    return min(parts[0], parts[2]), min(parts[1], parts[3]), max(parts[0], parts[2]), max(parts[1], parts[3])


def full_image_url(path: str) -> str:
    if path.startswith(("http://", "https://")):
        return path
    return IMAGE_BASE + path.lstrip("/")


def clean_time(value: str, img_path: str) -> str:
    match = re.search(r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})[日\s]+(\d{1,2})[时:](\d{1,2})", value or "")
    if match:
        year, month, day, hour, minute = [int(part) for part in match.groups()]
        return f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}"

    if value and "\ufffd" not in value and "?" not in value:
        return value

    match = re.search(r"/(\d{12})\d+\.(?:jpg|png|jpeg|webp)$", img_path, re.I)
    if not match:
        return value or ""
    try:
        parsed = datetime.strptime(match.group(1), "%Y%m%d%H%M")
        return parsed.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value or ""


def parse_frame_time(value: Any) -> Any:
    text = str(value or "").strip()
    for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y/%m/%d %H:%M"]:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def filter_frames_by_time(frames: List[Dict[str, Any]], start: str, end: str) -> List[Dict[str, Any]]:
    start_dt = parse_frame_time(start)
    end_dt = parse_frame_time(end)
    if not start_dt and not end_dt:
        return frames
    result = []
    for frame in frames:
        frame_dt = parse_frame_time(frame.get("time"))
        if not frame_dt:
            result.append(frame)
            continue
        if start_dt and frame_dt < start_dt:
            continue
        if end_dt and frame_dt > end_dt:
            continue
        result.append(frame)
    return result


def fetch_mosaic(area_id: str) -> List[Dict[str, Any]]:
    payload = {
        "b": {"fycx_fbt_ld": {"img_type": "3", "type": "", "falg": "", "area_id": area_id}},
        "h": {"p": TOKEN, "user_id": USER_ID},
    }
    data = request_json(payload)
    items = data.get("b", {}).get("fycx_fbt_ld", {}).get("info_list", [])
    result = []
    for item in items:
        img = item.get("img_url", "")
        result.append(
            {
                "kind": "mosaic",
                "time": item.get("pub_time", ""),
                "img": img,
                "url": full_image_url(img),
                "proxy": "/proxy-image?url=" + urllib.parse.quote(full_image_url(img), safe=""),
                "min_lon": item.get("min_lon", ""),
                "max_lon": item.get("max_lon", ""),
                "min_lat": item.get("min_lat", ""),
                "max_lat": item.get("max_lat", ""),
                "area_id": area_id,
            }
        )
    return result


def fetch_sequence(station_id: str, count: str) -> List[Dict[str, Any]]:
    payload = {
        "h": {"p": TOKEN},
        "b": {"leida": {"count": str(count), "station_id": str(station_id)}},
    }
    data = request_json(payload)
    items = data.get("b", {}).get("leida", {}).get("idex", [])
    result = []
    for item in items:
        img = item.get("img", "")
        url = full_image_url(img)
        result.append(
            {
                "kind": "sequence",
                "station_id": station_id,
                "time": clean_time(item.get("actiontime", ""), img),
                "img": img,
                "url": url,
                "proxy": "/proxy-image?url=" + urllib.parse.quote(url, safe=""),
                "min_lon": item.get("min_lon", ""),
                "max_lon": item.get("max_lon", ""),
                "min_lat": item.get("min_lat", ""),
                "max_lat": item.get("max_lat", ""),
            }
        )
    return result


def fetch_image_products() -> List[Dict[str, Any]]:
    payload = {
        "h": {"p": TOKEN, "user_id": USER_ID},
        "b": {"ztq_img": {"phone_type": "I", "size_type": "1"}},
    }
    data = request_json(payload)
    block = data.get("b", {}).get("ztq_img", {})
    rows = block.get("dataList", [])
    if isinstance(rows, dict):
        rows = [rows]
    result = []
    for index, item in enumerate(rows):
        img = item.get("url") or item.get("img_url") or item.get("httpurl") or ""
        if not img:
            continue
        url = item.get("httpurl") or full_image_url(img)
        result.append(
            {
                "kind": "imagery",
                "product": item.get("title") or "综合云图",
                "time": clean_time(item.get("pub_time") or item.get("time") or item.get("uptime") or "", img),
                "img": img,
                "url": url,
                "proxy": "/proxy-image?url=" + urllib.parse.quote(url, safe=""),
                "index": index + 1,
            }
        )
    return result


def fetch_radar_groups() -> List[Dict[str, Any]]:
    payload = {
        "h": {"p": TOKEN},
        "b": {"leiDa_Group_List": {}},
    }
    data = request_json(payload)
    return data.get("b", {}).get("leiDa_Group_List", {}).get("groupList", [])


def fetch_current_weather(area: str) -> Dict[str, Any]:
    cache = CACHE.setdefault("current_weather", {})
    cached = cache.get(str(area))
    if isinstance(cached, dict) and cache_fresh(cached, CURRENT_TTL_SECONDS):
        return cached["data"]

    payload = {
        "h": {"p": TOKEN},
        "b": {"sstq_grid_v2": {"area": area}},
    }
    data = request_json(payload)
    raw = data.get("b", {}).get("sstq_grid_v2", {}).get("sstq_grid", {})
    result = {
        "area": area,
        "station_id": raw.get("stationId", ""),
        "station_name": raw.get("stationname", ""),
        "city": raw.get("cityName", ""),
        "updated": raw.get("upt", "") or raw.get("upt_en", ""),
        "temperature": raw.get("ct", ""),
        "body_temperature": raw.get("body_temp", ""),
        "humidity": raw.get("humidity", ""),
        "rainfall": raw.get("rainfall", ""),
        "pressure": raw.get("airpressure", "") or raw.get("vaporpressuser", ""),
        "wind": raw.get("wind", ""),
        "wind_dir": raw.get("winddir_current_org", "") or raw.get("winddir_current", ""),
        "weather": raw.get("wt_daytime", "") or raw.get("wt_night", ""),
        "lon": raw.get("lon", ""),
        "lat": raw.get("lat", ""),
        "raw": raw,
    }
    cache[str(area)] = {"fetched_at_epoch": datetime.now().timestamp(), "data": result}
    append_weather_snapshot(str(area), result)
    return result


def append_weather_snapshot(area: str, weather: Dict[str, Any]) -> None:
    history = CACHE.setdefault("weather_history", {}).setdefault(str(area), [])
    snapshot = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "temperature": parse_float(weather.get("temperature")),
        "humidity": parse_float(weather.get("humidity")),
        "rainfall": parse_float(weather.get("rainfall")),
        "pressure": parse_float(weather.get("pressure")),
        "wind": parse_float(weather.get("wind")),
    }
    history.append(snapshot)
    cutoff = datetime.now() - timedelta(hours=24)
    CACHE["weather_history"][str(area)] = [
        item for item in history
        if parse_iso(item.get("time")) and parse_iso(item.get("time")) >= cutoff
    ]


def parse_iso(value: Any) -> Any:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def summarize_local_history(area: str) -> Dict[str, Any]:
    rows = CACHE.setdefault("weather_history", {}).get(str(area), [])

    def values(field: str) -> List[float]:
        return [row[field] for row in rows if isinstance(row.get(field), (int, float))]

    temps = values("temperature")
    humidity = values("humidity")
    rain = values("rainfall")
    wind = values("wind")
    pressure = values("pressure")
    return {
        "source": "local_cache",
        "sample_count": len(rows),
        "temperature_max": max(temps) if temps else None,
        "temperature_min": min(temps) if temps else None,
        "humidity_max": max(humidity) if humidity else None,
        "humidity_min": min(humidity) if humidity else None,
        "rainfall_sum": round(sum(rain), 2) if rain else None,
        "wind_max": max(wind) if wind else None,
        "pressure_max": max(pressure) if pressure else None,
        "pressure_min": min(pressure) if pressure else None,
        "note": "本地滚动缓存统计，服务启动后持续积累；点选站点会请求官方过去24小时趋势。",
    }


def fetch_station_realtime(station_id: str) -> Dict[str, Any]:
    payload = {
        "h": {"p": {}},
        "b": {"fycx_sstq": {"stationid": station_id}},
    }
    data = request_json(payload)
    raw = data.get("b", {}).get("fycx_sstq", {})
    return {"station_id": station_id, "raw": raw, **raw}


def fetch_cached_station_realtime(station_id: str) -> Dict[str, Any]:
    cache = CACHE.setdefault("station_realtime", {})
    cached = cache.get(str(station_id))
    if isinstance(cached, dict) and cache_fresh(cached, STATION_TTL_SECONDS):
        return cached["data"]
    data = fetch_station_realtime(station_id)
    cache[str(station_id)] = {"fetched_at_epoch": datetime.now().timestamp(), "data": data}
    return data


def get_cached_station_realtime(station_id: str) -> Dict[str, Any]:
    cache = CACHE.setdefault("station_realtime", {})
    cached = cache.get(str(station_id))
    if isinstance(cached, dict) and cache_fresh(cached, STATION_TTL_SECONDS):
        return cached["data"]
    return {}


def normalize_station_weather(data: Dict[str, Any], station: Dict[str, Any]) -> Dict[str, Any]:
    raw = data.get("raw", data) if isinstance(data, dict) else {}
    result = {
        "area": station.get("area_id", ""),
        "station_id": data.get("station_id") or station.get("station_id", ""),
        "station_name": raw.get("stationname") or station.get("name", ""),
        "city": station.get("city", ""),
        "updated": raw.get("upt", ""),
        "temperature": raw.get("ct", ""),
        "body_temperature": raw.get("body_temp", ""),
        "humidity": raw.get("humidity", ""),
        "rainfall": raw.get("rainfall", ""),
        "pressure": raw.get("vaporpressuser", "") or raw.get("airpressure", ""),
        "wind": raw.get("wind_speed", "") or raw.get("windspeed_twominuteave", ""),
        "wind_dir": raw.get("wind_dir", "") or raw.get("winddir_twominuteave", ""),
        "weather": raw.get("wt_daytime", "") or raw.get("wt_night", ""),
        "lon": raw.get("lon", "") or station.get("lon", ""),
        "lat": raw.get("lat", "") or station.get("lat", ""),
        "raw": raw,
    }
    if any(result.get(field) not in {"", None} for field in ["temperature", "humidity", "rainfall", "pressure", "wind"]):
        append_weather_snapshot(str(result["station_id"]), result)
    return result


def fetch_cached_cwa_observations() -> List[Dict[str, Any]]:
    cache = CACHE.setdefault("taiwan_cwa", {})
    cached = cache.get("O-A0001-001")
    if isinstance(cached, dict) and cache_fresh(cached, CWA_TTL_SECONDS):
        return cached.get("data", [])
    data = fetch_cwa_json(CWA_OBS_URL)
    rows = normalize_cwa_observations(data)
    cache["O-A0001-001"] = {"fetched_at_epoch": datetime.now().timestamp(), "data": rows}
    return rows


def normalize_cwa_observations(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    records = data.get("records", {}) if isinstance(data, dict) else {}
    stations = records.get("Station") or records.get("station") or records.get("location") or []
    rows = []
    for station in stations:
        if not isinstance(station, dict):
            continue
        row = normalize_cwa_station(station)
        if row:
            rows.append(row)
    return rows


def normalize_cwa_station(station: Dict[str, Any]) -> Dict[str, Any] | None:
    station_id = str(first_value(station, ["StationId", "StationID", "stationId", "station_id", "locationName"]) or "").strip()
    name = str(first_value(station, ["StationName", "stationName", "locationName"]) or station_id).strip()
    geo = station.get("GeoInfo") or {}
    weather = station.get("WeatherElement") or {}
    lat, lon = cwa_lat_lon(station, geo)
    if lat is None or lon is None:
        return None
    obs_time = first_value(station.get("ObsTime") or {}, ["DateTime", "obsTime"])
    if not obs_time:
        obs_time = first_value(station, ["DateTime", "time", "obsTime"])
    county = first_value(geo, ["CountyName", "countyName"]) or first_value(station, ["CountyName", "countyName"])
    town = first_value(geo, ["TownName", "townName"]) or first_value(station, ["TownName", "townName"])
    wind_dir_value = cwa_value(weather, "WindDirection", station)
    current = {
        "area": "taiwan",
        "station_id": f"TW:{station_id}" if station_id else f"TW:{name}",
        "station_name": name,
        "city": county or "台湾",
        "updated": obs_time or "",
        "temperature": cwa_metric(cwa_value(weather, "AirTemperature", station)),
        "humidity": cwa_metric(cwa_value(weather, "RelativeHumidity", station)),
        "rainfall": cwa_metric(cwa_value(weather, "Precipitation", station)),
        "pressure": cwa_metric(cwa_value(weather, "AirPressure", station)),
        "wind": cwa_metric(cwa_value(weather, "WindSpeed", station)),
        "wind_dir": cwa_wind_dir(wind_dir_value),
        "weather": cwa_value(weather, "Weather", station) or "",
        "lon": lon,
        "lat": lat,
        "raw": station,
    }
    return {
        "source": "taiwan_cwa",
        "kind": "taiwan_auto_station",
        "area_id": "taiwan",
        "station_id": current["station_id"],
        "name": name,
        "city": county or "台湾",
        "area_name": town or county or "台湾",
        "lon": lon,
        "lat": lat,
        "updated": current["updated"],
        "temperature": current["temperature"],
        "humidity": current["humidity"],
        "rainfall": current["rainfall"],
        "pressure": current["pressure"],
        "wind": current["wind"],
        "wind_dir": current["wind_dir"],
        "current": current,
        "stats24h": {},
    }


def cwa_lat_lon(station: Dict[str, Any], geo: Dict[str, Any]) -> tuple[Any, Any]:
    lat = parse_float(first_value(station, ["StationLatitude", "lat", "latitude"]))
    lon = parse_float(first_value(station, ["StationLongitude", "lon", "longitude"]))
    if lat is not None and lon is not None:
        return lat, lon
    lat = parse_float(first_value(geo, ["StationLatitude", "lat", "latitude"]))
    lon = parse_float(first_value(geo, ["StationLongitude", "lon", "longitude"]))
    if lat is not None and lon is not None:
        return lat, lon
    for coord in geo.get("Coordinates", []) or []:
        lat = parse_float(first_value(coord, ["StationLatitude", "lat", "latitude"]))
        lon = parse_float(first_value(coord, ["StationLongitude", "lon", "longitude"]))
        if lat is not None and lon is not None:
            return lat, lon
    return None, None


def first_value(data: Dict[str, Any], keys: List[str]) -> Any:
    if not isinstance(data, dict):
        return None
    for key in keys:
        if data.get(key) not in {"", None}:
            return data.get(key)
    return None


def cwa_value(weather: Any, key: str, station: Dict[str, Any]) -> Any:
    if isinstance(weather, dict):
        value = weather.get(key)
        if isinstance(value, dict):
            if key == "Precipitation":
                value = value.get("Now", value)
            return first_value(value, ["Value", "value", "Precipitation", "AirTemperature", "WindSpeed", "WindDirection"])
        if value not in {"", None}:
            return value
    if isinstance(weather, list):
        for item in weather:
            if not isinstance(item, dict):
                continue
            name = item.get("elementName") or item.get("ElementName") or item.get("name")
            if name == key:
                value = item.get("elementValue") or item.get("ElementValue") or item.get("value")
                if isinstance(value, list) and value:
                    value = value[0]
                if isinstance(value, dict):
                    return first_value(value, ["value", "Value", "measures"])
                return value
    return first_value(station, [key, key[0].lower() + key[1:]])


def cwa_metric(value: Any) -> Any:
    parsed = parse_float(value)
    if parsed is None or parsed <= -90:
        return ""
    return parsed


def cwa_wind_dir(value: Any) -> str:
    parsed = parse_float(value)
    if parsed is None:
        return str(value or "")
    directions = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]
    return directions[int((parsed + 22.5) // 45) % 8]


def fetch_station_trend(station_id: str, metric: str) -> Dict[str, Any]:
    trend = TREND_TYPES[metric]
    payload = {
        "h": {"p": {}},
        "b": {"fycx_trend_sta": {"channel": "2", "stationid": station_id, "type": trend["type"]}},
    }
    data = request_json(payload)
    raw = data.get("b", {}).get("fycx_trend_sta", {})
    sk_list = raw.get("sk_list", []) or []
    values = []
    for item in sk_list:
        value = parse_float(item.get("val"))
        if value is not None:
            values.append(value)
    summary = {
        "max": max(values) if values else None,
        "min": min(values) if values else None,
        "sum": round(sum(values), 2) if values else None,
        "count": len(values),
    }
    return {
        "metric": metric,
        "label": trend["label"],
        "unit": trend["unit"],
        "sk_time": raw.get("sk_time", ""),
        "yb_time": raw.get("yb_time", ""),
        "sk_list": sk_list,
        "yb_list": raw.get("yb_list", []) or [],
        "summary": summary,
    }


def station_history(station_id: str, metrics: List[str]) -> Dict[str, Any]:
    metrics = [metric for metric in metrics if metric in TREND_TYPES] or ["temperature", "rainfall", "wind", "humidity", "pressure"]
    cache_key = station_id + "|" + ",".join(metrics)
    cache = CACHE.setdefault("station_history", {})
    cached = cache.get(cache_key)
    if isinstance(cached, dict) and cache_fresh(cached, HISTORY_TTL_SECONDS) and station_history_has_points(cached.get("data", {}), metrics):
        return cached["data"]

    trends = {}
    for metric in metrics:
        trends[metric] = fetch_station_trend(station_id, metric)

    point_counts = {metric: len((trends.get(metric) or {}).get("sk_list", []) or []) for metric in metrics}
    result = {
        "ok": True,
        "station_id": station_id,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "fycx_trend_sta",
        "realtime": fetch_station_realtime(station_id),
        "trends": trends,
        "point_counts": point_counts,
    }
    cache[cache_key] = {"fetched_at_epoch": datetime.now().timestamp(), "data": result}
    save_runtime_cache()
    return result


def station_history_has_points(data: Dict[str, Any], metrics: List[str]) -> bool:
    trends = data.get("trends", {}) if isinstance(data, dict) else {}
    for metric in metrics:
        rows = (trends.get(metric) or {}).get("sk_list", []) if isinstance(trends, dict) else []
        if rows:
            return True
    return False


def fetch_hourly_forecast(county_id: str, count: str) -> List[Dict[str, Any]]:
    payload = {
        "h": {"p": TOKEN},
        "b": {"grid_forecast_new": {"count": str(count), "county_id": county_id, "page": "1"}},
    }
    data = request_json(payload)
    block = data.get("b", {}).get("grid_forecast_new", {})
    rows = block.get("today", [])
    result = []
    for item in rows:
        result.append(
            {
                "datetime": item.get("w_datetime", ""),
                "time": item.get("time", ""),
                "weather": item.get("desc", ""),
                "temperature": item.get("temperature", ""),
                "humidity": item.get("rh", ""),
                "rainfall": item.get("rainfall", ""),
                "pressure": item.get("airpressure", ""),
                "wind_speed": item.get("windspeed", ""),
                "gust_speed": item.get("gustspeed", ""),
                "wind_dir": item.get("winddir", ""),
                "visibility": item.get("visibility", ""),
            }
        )
    return result


def safe_fetch(label: str, errors: List[Dict[str, str]], func: Any, default: Any) -> Any:
    try:
        return func()
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        errors.append({"source": label, "error": message})
        print(f"[upstream-error] {label}: {message}")
        return default


def api_latest(query: Dict[str, List[str]]) -> Dict[str, Any]:
    area_id = query.get("area_id", ["25169"])[0]
    weather_area = query.get("weather_area", ["32336"])[0]
    forecast_count = query.get("forecast_count", ["12"])[0]
    count = query.get("count", ["16"])[0]
    radar_from = query.get("radar_from", [""])[0]
    radar_to = query.get("radar_to", [""])[0]
    station_ids = query.get("station_id", ["20002"])

    upstream_errors: List[Dict[str, str]] = []
    mosaics = safe_fetch("fycx_fbt_ld", upstream_errors, lambda: fetch_mosaic(area_id), [])
    sequences = []
    for station_id in station_ids:
        sequences.extend(safe_fetch(f"leida:{station_id}", upstream_errors, lambda station_id=station_id: fetch_sequence(station_id, count), []))
    unfiltered_count = len(sequences)
    sequences = filter_frames_by_time(sequences, radar_from, radar_to)
    radar_groups = safe_fetch("leiDa_Group_List", upstream_errors, fetch_radar_groups, [])
    current_weather = safe_fetch("sstq_grid_v2", upstream_errors, lambda: fetch_current_weather(weather_area), {})
    hourly_forecast = safe_fetch("grid_forecast_new", upstream_errors, lambda: fetch_hourly_forecast(weather_area, forecast_count), [])
    imagery = safe_fetch("ztq_img", upstream_errors, fetch_image_products, [])

    return {
        "ok": True,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "service_url": SERVICE_URL,
        "area_id": area_id,
        "weather_area": weather_area,
        "station_ids": station_ids,
        "radar_query": {
            "from": radar_from,
            "to": radar_to,
            "requested_count": count,
            "unfiltered_count": unfiltered_count,
            "filtered_count": len(sequences),
        },
        "radar_groups": radar_groups,
        "current_weather": current_weather,
        "hourly_forecast": hourly_forecast,
        "mosaics": mosaics,
        "sequences": sequences,
        "imagery": imagery,
        "upstream_errors": upstream_errors,
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        try:
            if parsed.path in {"/", "/radar_har_ui.html"}:
                self.send_file(ROOT / "radar_har_ui.html")
            elif parsed.path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
            elif parsed.path == "/api/latest":
                self.send_json(api_latest(query))
            elif parsed.path == "/radar-latest.jpg":
                self.send_latest_radar_image(query)
            elif parsed.path == "/api/search-stations":
                keyword = query.get("q", [""])[0]
                limit = int(query.get("limit", ["20"])[0])
                self.send_json(search_stations(keyword, limit))
            elif parsed.path == "/api/nearest-stations":
                lon = float(query.get("lon", [""])[0])
                lat = float(query.get("lat", [""])[0])
                limit = int(query.get("limit", ["10"])[0])
                self.send_json(nearest_stations(lon, lat, limit))
            elif parsed.path == "/api/weather-history":
                station_id = query.get("station_id", [""])[0].strip()
                if not station_id:
                    self.send_json({"ok": False, "error": "station_id is required"}, status=400)
                else:
                    metrics = [item.strip() for item in query.get("metrics", [""])[0].split(",") if item.strip()]
                    self.send_json(station_history(station_id, metrics))
            elif parsed.path == "/api/auto-station":
                station_id = query.get("station_id", [""])[0].strip()
                if not station_id:
                    self.send_json({"ok": False, "error": "station_id is required"}, status=400)
                else:
                    self.send_json({"ok": True, "station": fetch_station_realtime(station_id)})
            elif parsed.path == "/api/gis-current":
                self.send_json(gis_current(query))
            elif parsed.path == "/api/weather-cities":
                self.send_json(list_weather_cities())
            elif parsed.path == "/api/station-cache":
                cache = get_station_cache()
                self.send_json(
                    {
                        "ok": True,
                        "loaded_at": cache["loaded_at"],
                        "counts": {
                            "weather_areas": len(cache["weather_areas"]),
                            "obs_stations": len(cache["obs_stations"]),
                        },
                    }
                )
            elif parsed.path == "/healthz":
                self.send_json({"ok": True, "service": "fujian-meteorology-system"})
            elif parsed.path == "/proxy-image":
                self.proxy_image(query)
            else:
                self.send_error(404, "Not found")
        except Exception as exc:
            print(f"[request-error] {parsed.path}: {type(exc).__name__}: {exc}")
            self.send_json({"ok": False, "error": str(exc)}, status=500)

    def send_file(self, path: Path) -> None:
        if not path.exists():
            self.send_error(404, "Not found")
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, data: Dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def proxy_image(self, query: Dict[str, List[str]]) -> None:
        url = query.get("url", [""])[0]
        if not url.startswith("http://www.fjqxfw.cn:8099/ftp/"):
            self.send_error(400, "Invalid image URL")
            return

        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
            content_type = response.headers.get("Content-Type") or mimetypes.guess_type(url)[0] or "image/jpeg"

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_latest_radar_image(self, query: Dict[str, List[str]]) -> None:
        station_id = query.get("station_id", ["20002"])[0]
        count = query.get("count", ["6"])[0]
        frames = fetch_sequence(station_id, count)
        if not frames:
            self.send_error(404, "No radar image available")
            return
        latest = frames[-1]
        url = latest.get("url", "")
        if not url.startswith("http://www.fjqxfw.cn:8099/ftp/"):
            self.send_error(502, "Invalid upstream image URL")
            return
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
            content_type = response.headers.get("Content-Type") or mimetypes.guess_type(url)[0] or "image/jpeg"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Radar-Station", str(station_id))
        self.send_header("X-Radar-Time", str(latest.get("time", "")))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))


def main() -> None:
    load_runtime_cache()
    parser = argparse.ArgumentParser(description="福建气象系统 Web 服务")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"福建气象系统: http://{args.host}:{args.port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
