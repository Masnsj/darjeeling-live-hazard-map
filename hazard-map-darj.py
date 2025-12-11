"""
darjeeling_full_live_explained.py

Interactive Darjeeling hazard map (Folium + pywebview).
Added full explanations beside each important line using comments.
"""

# ------------------------- Imports -------------------------

import os                     # For path & temp file handling
import time                   # For sleep timer in refresh loop
import threading              # Background update thread
import tempfile               # Temporary HTML file for map
import math                   # Needed for haversine & small calculations
import webbrowser             # To fallback-open the map if pywebview fails
from datetime import datetime # For timestamps
from dateutil import tz       # Local timezone conversion

import folium                 # Map library
import requests               # API calls
import webview                # Desktop window wrapper for HTML map

# ------------------------- Configuration -------------------------

# List of places to display as bubbles: (Name, Latitude, Longitude)
BUBBLES = [
    ("Chowrasta / Mall", 27.0359, 88.2626),
    ("Ghoom", 27.0470, 88.2632),
    ("Lebong", 27.0161, 88.2536),
    ("Sonada", 26.9607, 88.2960),
    ("Jorebungalow", 27.0260, 88.2714),
    ("Teesta Bazaar", 27.1919, 88.5168),      # Bubble for Teesta river area
    ("Tiger Hill", 27.0195, 88.2430),
    ("Kurseong", 26.8820, 88.2774),
    ("Happy Valley", 27.0448, 88.2792),
    ("North Point", 27.0613, 88.2715),
    ("Darjeeling Zoo", 27.0423, 88.2767),
    ("Cart Road Area", 27.0300, 88.2800),
    ("Glenary's / Observatory", 27.0350, 88.2640),
    ("Batasia Loop", 27.0465, 88.2588),
]

MAP_CENTER = (27.0360, 88.2627)   # Center of Darjeeling map
ZOOM_START = 13                    # Map zoom
BUBBLE_RADIUS_M = 2000             # Bubble circle radius in meters (2 km)

# Open-Meteo free APIs
OPEN_METEO_WEATHER = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_AQ = "https://air-quality-api.open-meteo.com/v1/air-quality"

# ThingSpeak public demo channel for Teesta (may/may not have data)
TEESTA_THINGSPEAK = "https://api.thingspeak.com/channels/1231845/feeds.json?results=1"

REFRESH_SECONDS = 3 * 60           # Auto-update every 3 minutes

# Temp HTML output file for the live map
OUT_HTML = os.path.join(tempfile.gettempdir(), "darjeeling_live_map.html")

# ------------------------- Utility Functions -------------------------

def jitter_if_close(coord, existing, min_dist_m=400):
    """Jitter a point slightly if it is too close to another bubble to avoid overlap."""
    lat, lon = coord
    for e in existing:
        # Compute distance in km
        d = haversine(lat, lon, e[0], e[1])
        if d < min_dist_m/1000:  # If within 400m → jitter
            lat += 0.0006        # Small shift
            lon += 0.0006
    return lat, lon

def haversine(lat1, lon1, lat2, lon2):
    """Calculate distance between two lat/lon points using haversine formula."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return 2 * R * math.asin(math.sqrt(a))

def pm25_to_category(pm25):
    """Convert PM2.5 number to AQI category."""
    if pm25 is None: return "N/A"
    if pm25 <= 12: return "Good"
    if pm25 <= 35.4: return "Moderate"
    if pm25 <= 55.4: return "Unhealthy for Sensitive Groups"
    if pm25 <= 150.4: return "Unhealthy"
    if pm25 <= 250.4: return "Very Unhealthy"
    return "Hazardous"

# ------------------------- API Fetching -------------------------

def fetch_open_meteo_weather(lat, lon):
    """Fetch weather (temp, humidity, rainfall, pressure)."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,relativehumidity_2m,precipitation,pressure_msl",
        "current_weather": "true",
        "timezone": "Asia/Kolkata"
    }
    r = requests.get(OPEN_METEO_WEATHER, params=params, timeout=10)
    r.raise_for_status()
    return r.json()

def fetch_open_meteo_aq(lat, lon):
    """Fetch PM2.5, PM10 and gases."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "pm10,pm2_5,carbon_monoxide,nitrogen_dioxide,ozone",
        "timezone": "Asia/Kolkata"
    }
    r = requests.get(OPEN_METEO_AQ, params=params, timeout=10)
    r.raise_for_status()
    return r.json()

def fetch_teesta_level():
    """
    Best-effort attempt to get Teesta river water level.
    1) Try ThingSpeak public channel.
    2) If fail → look up CWC website & check if 'Teesta' appears.
    """
    # Try ThingSpeak
    try:
        r = requests.get(TEESTA_THINGSPEAK, timeout=8)
        if r.status_code == 200:
            j = r.json()
            if j.get("feeds") and j["feeds"][0].get("field1"):
                val = float(j["feeds"][0]["field1"])
                # Categorize depth
                if val > 400: status = "FLOOD WARNING"
                elif val > 300: status = "HIGH"
                elif val < 100: status = "LOW"
                else: status = "NORMAL"
                return val, "cm (ThingSpeak)", status
    except Exception:
        pass

    # Try CWC FFS website
    try:
        r = requests.get("https://ffs.india-water.gov.in/", timeout=8)
        if "teesta" in r.text.lower():
            return None, "CWC FFS mention", "SEE_CWC"
    except:
        pass

    return None, None, "NO_PUBLIC_RIVER_DATA"

# ------------------------- Hazard Scoring -------------------------

def compute_landslide_score(hum, pres_drop_3h, elev_m=2000):
    """Compute landslide risk score using humidity+pressure heuristics."""
    score = 0
    if hum: score += max(0, hum - 60) * 0.8    # Humidity effect
    if pres_drop_3h > 0: score += min(30, pres_drop_3h * 8)   # Pressure drop
    score += min(15, (elev_m - 500)/100)       # Elevation factor
    score = max(0, min(100, score))            # Clamp 0–100

    # Label based on score
    if score >= 85: label = "confirm 99%"
    elif score >= 60: label = "high"
    elif score >= 35: label = "mid"
    else: label = "no"
    return round(score,1), label

def rainfall_scale(mm):
    """Convert mm rain to text scale."""
    if mm < 0.2: return "No"
    if mm < 5: return "Mid"
    if mm < 20: return "High"
    return "Confirm 99%"

# ------------------------- Data Gathering -------------------------

def gather_bubble_data():
    """Fetch all weather+AQI data for all bubbles."""
    results = []
    
    for name, lat, lon in BUBBLES:
        try:
            # Fetch weather
            w = fetch_open_meteo_weather(lat, lon)
            hourly = w["hourly"]

            # Find current hour index
            now = datetime.now(tz=tz.gettz("Asia/Kolkata"))
            now_key = now.strftime("%Y-%m-%dT%H:00")
            try:
                idx = hourly["time"].index(now_key)
            except:
                idx = 0

            # Extract weather metrics
            precip_now = float(hourly["precipitation"][idx])
            precip_next1 = float(hourly["precipitation"][idx+1]) if idx+1 < len(hourly["precipitation"]) else 0
            hum = float(hourly["relativehumidity_2m"][idx])
            pres = float(hourly["pressure_msl"][idx])
            temp = float(hourly["temperature_2m"][idx])
            pres_prev = float(hourly["pressure_msl"][idx-3]) if idx >= 3 else None
            pres_drop = (pres_prev - pres) if pres_prev else 0

            # Fetch AQI
            try:
                aq = fetch_open_meteo_aq(lat, lon)
                ah = aq["hourly"]
                aidx = ah["time"].index(now_key)
                pm25 = float(ah["pm2_5"][aidx])
                pm10 = float(ah["pm10"][aidx])
            except:
                pm25 = pm10 = None

            # Compute landslide
            score, label = compute_landslide_score(hum, pres_drop)

            results.append({
                "name": name, "lat": lat, "lon": lon,
                "temp": temp, "humid": hum, "pressure": pres,
                "precip_now": precip_now, "precip_next1": precip_next1,
                "pressure_drop_3h": pres_drop,
                "landslide_score": score, "landslide_label": label,
                "pm25": pm25, "pm10": pm10,
                "aqi_category": pm25_to_category(pm25)
            })

        except Exception as e:
            # In case of API failure → placeholder entry
            results.append({
                "name": name, "lat": lat, "lon": lon,
                "temp": None, "humid": None, "pressure": None,
                "precip_now": 0, "precip_next1": 0,
                "pressure_drop_3h": 0,
                "landslide_score": 0, "landslide_label": "no",
                "pm25": None, "pm10": None,
                "aqi_category": "N/A",
                "error": str(e)
            })

    return results

# Bubble color logic
def color_for_result(res):
    label = res["landslide_label"]
    if label == "confirm 99%": return "darkred"
    if label == "high": return "red"
    if label == "mid": return "orange"
    if res["precip_now"] >= 5: return "blue"
    return "green"

# ------------------------- Map Builder -------------------------

def build_map(data, timestamp, teesta):
    """Build folium map and insert bubbles + info boxes."""
    m = folium.Map(location=MAP_CENTER, zoom_start=ZOOM_START)

    # Restrict view area slightly
    m.fit_bounds([[26.98, 88.22], [27.14, 88.32]])

    # Title box (top-left)
    title = f"""
    <div style="position: fixed; top: 8px; left: 8px; 
         background: white; padding:10px; z-index:9999;">
         <b>Darjeeling Live Hazard Map</b><br>
         Updated: {timestamp}<br>
         <i>Data: Open-Meteo + best-effort Teesta</i>
    </div>
    """
    m.get_root().html.add_child(folium.Element(title))

    # Teesta box (top-right)
    if teesta[2] == "SEE_CWC":
        teesta_text = "CWC mentions found (see FFS)"
    elif teesta[2] == "NO_PUBLIC_RIVER_DATA":
        teesta_text = "No public river data"
    elif teesta[0] is None:
        teesta_text = f"No numeric data ({teesta[2]})"
    else:
        teesta_text = f"{teesta[0]} {teesta[1]} — {teesta[2]}"

    tbox = f"""
    <div style="position: fixed; top: 8px; right: 8px;
         background: white; padding:10px; z-index:9999;">
         <b>Teesta:</b> {teesta_text}
    </div>
    """
    m.get_root().html.add_child(folium.Element(tbox))

    # Add bubbles
    placed = []
    for r in data:
        lat, lon = r["lat"], r["lon"]

        # Move slightly if overlapping
        for p in placed:
            if haversine(lat, lon, p[0], p[1]) < 0.4:
                lat += 0.0015
                lon += 0.0025
        placed.append((lat, lon))

        # Build popup text
        popup = f"""
        <b>{r['name']}</b><br>
        Temp: {r['temp']} °C<br>
        Humidity: {r['humid']}%<br>
        Rain: {r['precip_now']} mm<br>
        Landslide: {r['landslide_label']} (score {r['landslide_score']})<br>
        PM2.5: {r['pm25']} ({r['aqi_category']})<br>
        PM10: {r['pm10']}
        """

        # Draw circle
        folium.Circle(
            location=(lat, lon),
            radius=BUBBLE_RADIUS_M,
            color=color_for_result(r),
            fill=True,
            fill_opacity=0.35,
            popup=popup
        ).add_to(m)

        # Marker
        folium.Marker(
            location=(lat, lon),
            popup=popup,
            icon=folium.Icon(color="blue")
        ).add_to(m)

    # Save to HTML
    m.save(OUT_HTML)

# ------------------------- Background Updater -------------------------

def updater_loop(window):
    """Continuously refresh data & reload map."""
    while True:
        try:
            data = gather_bubble_data()                    # Fetch latest
            teesta = fetch_teesta_level()                 # Fetch river level
            ts = datetime.now(tz=tz.gettz("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M")
            build_map(data, ts, teesta)                   # Rebuild map
            window.load_url("file://" + OUT_HTML)         # Reload inside app
        except:
            pass
        time.sleep(REFRESH_SECONDS)                       # Wait 3 minutes

# ------------------------- Main -------------------------

def main():
    data = gather_bubble_data()
    teesta = fetch_teesta_level()
    ts = datetime.now(tz=tz.gettz("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M")
    build_map(data, ts, teesta)

    file_url = "file://" + OUT_HTML
    window = webview.create_window("Darjeeling Live Map", file_url, width=1100, height=800)

    threading.Thread(target=updater_loop, args=(window,), daemon=True).start()
    webview.start()

if __name__ == "__main__":
    main()

