from fastapi import FastAPI, HTTPException
import requests
from gdacs.api import GDACSAPIReader
from math import radians, cos, sin, sqrt, atan2
from datetime import datetime, timezone
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
gdacs_client = GDACSAPIReader()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def haversine(lat1, lon1, lat2, lon2):
    R = 6371 
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1-a))
    return R * c

def get_incident_density(lat, lon):
    try:
        events = dict(gdacs_client.latest_events())["features"]
        score = 0
        for event in events:
            ev_lat = event["geometry"]["coordinates"][1]
            ev_lon = event["geometry"]["coordinates"][0]
            distance = haversine(lat, lon, ev_lat, ev_lon)
            if distance < 300:
                severity = event["properties"].get("severitydata", {}).get("severity", 0)
                score += min(severity / 10, 1)
        return min(score, 1)
    except Exception:
        return 0

def get_weather_severity(lat, lon):
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            f"&current_weather=true&daily=sunrise,sunset&timezone=UTC"
        )
        data = requests.get(url).json()
        weather = data["current_weather"]
        wind = weather["windspeed"]
        code = weather["weathercode"]
        score = 0
        if wind > 40: score += 0.5
        if code in [61, 63, 65]: score += 0.3
        if code in [95, 96, 99]: score += 0.7
        return min(score, 1), data["daily"]
    except Exception:
        return 0, {"sunrise": [datetime.now(timezone.utc).isoformat()], "sunset": [datetime.now(timezone.utc).isoformat()]}

def get_road_isolation(lat, lon):
    try:
        query = f'[out:json];way(around:500,{lat},{lon})["highway"];out count;'
        url = "https://overpass-api.de/api/interpreter"
        data = requests.get(url, params={"data": query}).json()
        road_count = int(data.get("elements", [{}])[0].get("tags", {}).get("ways", 0))
        return 1 - min(road_count / 50, 1)
    except Exception:
        return 0.5

def get_poi_inverse(lat, lon):
    try:
        query = f'[out:json];node(around:500,{lat},{lon})["amenity"];out count;'
        url = "https://overpass-api.de/api/interpreter"
        data = requests.get(url, params={"data": query}).json()
        poi_count = int(data.get("elements", [{}])[0].get("tags", {}).get("nodes", 0))
        return 1 - min(poi_count / 30, 1)
    except Exception:
        return 0.5

def get_night_factor(daily_data):
    try:
        # Use UTC to avoid "naive vs aware" datetime crashes
        sunrise = datetime.fromisoformat(daily_data["sunrise"][0].replace('Z', '+00:00'))
        sunset = datetime.fromisoformat(daily_data["sunset"][0].replace('Z', '+00:00'))
        
        now = datetime.now(timezone.utc)

        def to_minutes(dt):
            return dt.hour * 60 + dt.minute

        now_m = to_minutes(now)
        sunrise_m = to_minutes(sunrise)
        sunset_m = to_minutes(sunset)
        noon_m = (sunrise_m + sunset_m) / 2
        
        distance = abs(now_m - noon_m)
        return round(min(distance / 720, 1), 3)
    except Exception:
        return 0.5

@app.get("/risk")
async def calculate_risk(lat: float, lon: float, userReports: float = 0):
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
        "incidentDensity": float(incidentDensity),
        "roadIsolation": float(roadIsolation),
        "weatherSeverity": float(weatherSeverity),
        "poiDensityInverse": float(poiDensityInverse),
        "nightFactor": float(nightFactor),
        "userReports": float(userReports),
        "finalRiskScore": round(float(risk), 3)
    }