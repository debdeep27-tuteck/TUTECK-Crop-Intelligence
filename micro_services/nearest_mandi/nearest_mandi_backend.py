"""
nearest_mandi_backend.py

Microservice to find the nearest mandis (markets) from a given user location using free map APIs (OpenStreetMap Overpass API).
"""
import os
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
import math

app = Flask(__name__)
CORS(app)

PORT = int(os.environ.get("NEAREST_MANDI_PORT", 5012))
OVERPASS_URL = "http://overpass-api.de/api/interpreter"

def get_distance(lat1, lon1, lat2, lon2):
    """Calculate the great-circle distance between two points on the Earth surface."""
    R = 6371.0 # radius of earth in km
    
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    
    a = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    distance = R * c
    return distance

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "nearest-mandi-backend"})

@app.route("/api/nearest-mandi/locate", methods=["GET"])
def locate_mandi():
    lat = request.args.get("lat")
    lon = request.args.get("lon")
    radius = request.args.get("radius", 5000) # Default 5km radius in meters

    if not lat or not lon:
        return jsonify({"error": "lat and lon are required parameters"}), 400

    try:
        lat = float(lat)
        lon = float(lon)
        radius = float(radius)
    except ValueError:
        return jsonify({"error": "lat, lon, and radius must be valid numbers"}), 400

    # Overpass QL query to find marketplaces (mandis) within the radius
    overpass_query = f"""
    [out:json][timeout:25];
    (
      node["amenity"="marketplace"](around:{radius},{lat},{lon});
      way["amenity"="marketplace"](around:{radius},{lat},{lon});
    );
    out center;
    """

    OVERPASS_URLS = [
        "https://overpass-api.de/api/interpreter",
        "https://lz4.overpass-api.de/api/interpreter",
        "https://z.overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter"
    ]

    headers = {
        'User-Agent': 'TuteckCropIntelligence/1.0',
        'Accept': '*/*'
    }
    
    data = None
    last_error = None

    for url in OVERPASS_URLS:
        try:
            response = requests.post(url, data=overpass_query.encode('utf-8'), headers=headers, timeout=15)
            response.raise_for_status()
            data = response.json()
            break  # Success, exit the loop
        except requests.exceptions.RequestException as e:
            last_error = e
            continue
            
    if data is None:
        raise Exception(f"All Overpass API endpoints failed. Last error: {last_error}")

    try:
        elements = data.get("elements", [])
        mandis = []
        for element in elements:
            m_lat = element.get("lat")
            m_lon = element.get("lon")
            
            # If way element, coordinates are in 'center'
            if element.get("type") == "way" and "center" in element:
                m_lat = element["center"].get("lat")
                m_lon = element["center"].get("lon")
                
            if m_lat is not None and m_lon is not None:
                dist = get_distance(lat, lon, m_lat, m_lon)
                tags = element.get("tags", {})
                
                # Resolve market name with fallbacks
                name = (
                    tags.get("name")
                    or tags.get("name:en")
                    or tags.get("name:hi")
                    or tags.get("name:bn")
                    or tags.get("operator")
                    or tags.get("official_name")
                    or tags.get("alt_name")
                )
                
                locality = (
                    tags.get("addr:suburb")
                    or tags.get("addr:neighbourhood")
                    or tags.get("addr:city")
                    or tags.get("addr:district")
                    or tags.get("addr:street")
                )
                
                if not name:
                    if locality:
                        name = f"{locality} Agricultural Market"
                    else:
                        name = "Krishi Mandi / APMC Yard"
                
                # Format clean address
                addr_parts = [
                    tags.get("addr:street"),
                    tags.get("addr:suburb"),
                    tags.get("addr:city"),
                    tags.get("addr:district"),
                    tags.get("addr:state"),
                    tags.get("addr:postcode")
                ]
                clean_addr = tags.get("addr:full") or ", ".join([p for p in addr_parts if p])
                
                # Classify market type
                produce = tags.get("produce") or tags.get("market") or tags.get("shop")
                market_type = "APMC / Krishi Mandi"
                if produce:
                    market_type = f"{produce.capitalize()} Market"
                elif "wholesale" in tags or tags.get("wholesale") == "yes":
                    market_type = "Wholesale Mandi"
                elif tags.get("amenity") == "marketplace":
                    market_type = "Agricultural Haat"

                mandis.append({
                    "id": element.get("id"),
                    "name": name,
                    "address": clean_addr or (f"{locality}, India" if locality else "Local Agricultural Zone"),
                    "market_type": market_type,
                    "lat": m_lat,
                    "lon": m_lon,
                    "distance_km": round(dist, 2),
                    "tags": tags
                })
        
        # Sort by distance
        mandis.sort(key=lambda x: x["distance_km"])
        
        return jsonify({
            "status": "success",
            "lat": lat,
            "lon": lon,
            "radius_m": radius,
            "mandis": mandis
        })

    except Exception as e:
        return jsonify({
            "status": "error", 
            "error": "Failed to retrieve mandi records from the map provider.", 
            "details": str(e)
        })

if __name__ == "__main__":
    print("=" * 55)
    print(f"  NEAREST MANDI BACKEND — Running on http://127.0.0.1:{PORT}")
    print("=" * 55)
    app.run(host="127.0.0.1", port=PORT, debug=False)
