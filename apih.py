from fastapi import FastAPI
import requests
from gdacs.api import GDACSAPIReader
from math import radians, cos, sin, sqrt, atan2
from datetime import datetime
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
gdacs_client = GDACSAPIReader()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# Utility: Distance Calculator
# -----------------------------
def haversine(lat1, lon1, lat2, lon2):
    R = 6371  # Earth radius in km
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)

    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1-a))
    return R * c


# -----------------------------
# 1️⃣ Incident Density (GDACS)
# -----------------------------
def get_incident_density(lat, lon):
    events = dict(gdacs_client.latest_events())["features"]

    score = 0
    for event in events:
        ev_lat = event["geometry"]["coordinates"][1]
        ev_lon = event["geometry"]["coordinates"][0]

        distance = haversine(lat, lon, ev_lat, ev_lon)

        if distance < 300:  # 300 km radius
            severity = event["properties"]["severitydata"]["severity"]
            score += min(severity / 10, 1)

    return min(score, 1)


# -----------------------------
# 2️⃣ Weather Severity (Open-Meteo)
# -----------------------------
def get_weather_severity(lat, lon):
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        f"&current_weather=true"
        f"&daily=sunrise,sunset"
        f"&timezone=auto"
    )

    data = requests.get(url).json()

    weather = data["current_weather"]
    wind = weather["windspeed"]
    code = weather["weathercode"]

    score = 0

    if wind > 40:
        score += 0.5

    if code in [61, 63, 65]:  # rain
        score += 0.3

    if code in [95, 96, 99]:  # thunderstorm
        score += 0.7

    return min(score, 1), data["daily"]


# -----------------------------
# 3️⃣ Road Isolation (OSM)
# -----------------------------
def get_road_isolation(lat, lon):
    query = f"""
    [out:json];
    (
      way(around:500,{lat},{lon})["highway"];
    );
    out body;
    """

    url = "https://overpass-api.de/api/interpreter"
    data = requests.get(url, params={"data": query}).json()

    road_count = len(data["elements"])

    return 1 - min(road_count / 50, 1)


# -----------------------------
# 4️⃣ POI Density Inverse (OSM)
# -----------------------------
def get_poi_inverse(lat, lon):
    query = f"""
    [out:json];
    (
      node(around:500,{lat},{lon})["amenity"];
    );
    out body;
    """

    url = "https://overpass-api.de/api/interpreter"
    data = requests.get(url, params={"data": query}).json()

    poi_count = len(data["elements"])

    return 1 - min(poi_count / 30, 1)


# -----------------------------
# 5️⃣ Night Factor
# -----------------------------

def get_night_factor(daily_data):
    sunrise_str = daily_data["sunrise"][0]
    sunset_str = daily_data["sunset"][0]

    # Convert to datetime
    sunrise = datetime.fromisoformat(sunrise_str)
    sunset = datetime.fromisoformat(sunset_str)

    now = datetime.now(sunrise.tzinfo)

    # Convert everything to minutes since midnight
    def to_minutes(dt):
        return dt.hour * 60 + dt.minute

    now_m = to_minutes(now)
    sunrise_m = to_minutes(sunrise)
    sunset_m = to_minutes(sunset)

    # Solar noon
    noon_m = (sunrise_m + sunset_m) / 2

    # Distance from noon
    distance = abs(now_m - noon_m)

    # Maximum possible distance in a day
    max_distance = 720  # 12 hours

    # Normalize 0–1
    night_factor = min(distance / max_distance, 1)

    return round(night_factor, 3)

# -----------------------------
# MAIN RISK ENDPOINT
# -----------------------------
@app.get("/risk")
def calculate_risk(lat: float, lon: float, userReports: float = 0):

    incidentDensity = get_incident_density(lat, lon)
    weatherSeverity, daily_data = get_weather_severity(lat, lon)
    roadIsolation = get_road_isolation(lat, lon)
    poiDensityInverse = get_poi_inverse(lat, lon)
    nightFactor = get_night_factor(daily_data)

    risk = (
        0.35 * incidentDensity +
        0.20 * roadIsolation +
        0.10 * weatherSeverity +
        0.15 * poiDensityInverse +
        0.10 * nightFactor +
        0.10 * userReports
    )

    return {
         
        "incidentDensity": incidentDensity,
        "roadIsolation": roadIsolation,
        "weatherSeverity": weatherSeverity,
        "poiDensityInverse": poiDensityInverse,
        "nightFactor": nightFactor,
        "userReports": userReports,
        "finalRiskScore": round(risk, 3)
    }