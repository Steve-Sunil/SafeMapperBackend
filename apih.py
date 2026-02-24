from fastapi import FastAPI, HTTPException
import requests
from gdacs.api import GDACSAPIReader
from math import radians, cos, sin, sqrt, atan2
from datetime import datetime, timezone
from fastapi.middleware.cors import CORSMiddleware
import polyline
import asyncio
import time

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

pari = 0.0


cache = {"weather": None, "gdacs": None, "last_update": 0}

startlt = 0.0
startln = 0.0
endlt = 0.0
endln = 0.0

@app.post("/get-cords")
async def get_cords(start_lat: float, start_lon: float, end_lat: float, end_lon: float):
    global startlt, startln, endlt, endln
    startlt = start_lat
    startln = start_lon
    endlt = end_lat
    endln = end_lon
    # Added a print here so you can see the update in your terminal
    print(f"Cords Updated: {startlt}, {startln} to {endlt}, {endln}")
    return {"status": "success", "message": "Coordinates updated"}

@app.get("/find-safest-route")
async def find_safest_route():
    global startlt, startln, endlt, endln
    
    # Safety Check: Prevent the API from calling GraphHopper with 0,0
    if startlt == 0.0 or startln == 0.0:
        raise HTTPException(
            status_code=400, 
            detail="Coordinates not set. Please call /get-cords first."
        )

    GRAPHHOPPER_KEY = "88686f8e-8373-4adb-8e3e-00202083def0"
    
    # 1. Get Routes Instantly
    gh_url = "https://graphhopper.com/api/1/route"
    gh_params = {
        "point": [f"{startlt},{startln}", f"{endlt},{endln}"],
        "profile": "car",
        "algorithm": "alternative_route",
        "instructions": "false",
        "key": GRAPHHOPPER_KEY
    }
    
    try:
        gh_response = requests.get(gh_url, params=gh_params)
        gh_data = gh_response.json()
        
        if "paths" not in gh_data:
            # Check if GraphHopper returned an error message
            error_detail = gh_data.get("message", "No routes found")
            raise HTTPException(status_code=400, detail=error_detail)

        # 2. Fetch Area Data ONCE
        weather_severity, daily_data = get_weather_severity(startlt, startln)
        incident_density = get_incident_density(startlt, startln)
        night_factor = get_night_factor(daily_data)

        routes_analysis = []

        for idx, path in enumerate(gh_data["paths"]):
            full_coords = polyline.decode(path["points"])
            
            # Sampling: 1 point every 20 for speed
            sampled_points = full_coords[::20] 
            
            route_risks = []
            for lat, lon in sampled_points:
                road_iso = get_road_isolation(lat, lon)
                poi_inv = get_poi_inverse(lat, lon)
                
                risk = (0.35 * incident_density + 0.20 * road_iso + 
                        0.10 * weather_severity + 0.15 * poi_inv + 0.10 * night_factor)
                route_risks.append(risk)
                
            avg_risk = sum(route_risks) / len(route_risks)
            
            routes_analysis.append({
                "route_id": idx + 1,
                "average_risk": round(avg_risk, 3),
                "geometry": full_coords
            })

        safest = min(routes_analysis, key=lambda x: x["average_risk"])
        global pari
        pari = safest["average_risk"]
        return safest["geometry"]

    except Exception as e:
        # Catching connection errors to external APIs
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/risk")
async def calculate_risk():
    return pari