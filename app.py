from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import numpy as np
import requests
from datetime import datetime
import joblib
from math import radians, sin, cos, sqrt, atan2
from database import init_db, create_user, verify_user, save_prediction
import socket
import threading

app = Flask(__name__)
app.secret_key = "traffic_secret_key"

# --------------------------------------------------
# CONFIG
# --------------------------------------------------
TOMTOM_API_KEY = "73dFzGvUs8XYDyW0XnaxM97HsFIT7TlX"

# --------------------------------------------------
# LOAD RANDOM FOREST MODEL & SCALER
# --------------------------------------------------
model = joblib.load("traffic_random_forest_model.pkl")
scaler = joblib.load("traffic_scaler.pkl")

# --------------------------------------------------
# FEATURE ORDER (MUST MATCH TRAINING)
# --------------------------------------------------
FEATURES = [
    "vehicle_count",
    "avg_speed",
    "traffic_density",
    "signal_wait_time",
    "lane_count",
    "weather",
    "hour",
    "day_type"
]

# --------------------------------------------------
# DATABASE INITIALIZATION
# --------------------------------------------------

# Initialize database at import time for compatibility with older Flask
try:
    init_db()
except Exception as e:
    print("Database init warning:", e)

# --------------------------------------------------
# UTIL FUNCTIONS
# --------------------------------------------------
def safe_float(val, default=0.0):
    try:
        return float(val)
    except:
        return default

def calculate_distance(lat1, lon1, lat2, lon2):
    """Calculate distance between two points in kilometers using Haversine formula"""
    R = 6371  # Earth's radius in kilometers
    
    lat1_rad = radians(lat1)
    lat2_rad = radians(lat2)
    delta_lat = radians(lat2 - lat1)
    delta_lon = radians(lon2 - lon1)
    
    a = sin(delta_lat / 2) ** 2 + cos(lat1_rad) * cos(lat2_rad) * sin(delta_lon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    
    distance = R * c
    return distance

# --------------------------------------------------
# UDP & LCD helpers
# --------------------------------------------------
UDP_PORT = 4210
BROADCAST_IP = "255.255.255.255"

# Global latest LCD message (in-memory)
last_lcd_message = ""

def update_lcd_display(message):
    """Update attached LCD display or fallback to writing a file/console.

    This function tries to call a real LCD driver if available. If not,
    it writes the message to `lcd_display.txt` and logs to console so
    simple setups can read the file to simulate an LCD.
    """
    global last_lcd_message
    try:
        # Normalize and extract traffic level if present
        msg = str(message)
        level = None

        # Common payload formats: "PREDICTION|<label>|...", "ALERT|HIGH_TRAFFIC|...", or plain text containing Low/Medium/High
        parts = msg.split("|")
        if parts and parts[0].upper() in ("PREDICTION", "ALERT", "TRAFFIC_ALERT"):
            # search remaining parts for keywords
            for p in parts[1:]:
                p_up = p.upper()
                if "HIGH" in p_up or "RED" in p_up or "🔴" in p_up:
                    level = "HIGH"
                    break
                if "MEDIUM" in p_up or "YELLOW" in p_up or "🟡" in p_up:
                    level = "MEDIUM"
                    break
                if "LOW" in p_up or "GREEN" in p_up or "🟢" in p_up:
                    level = "LOW"
                    break

        # If not structured, do a general search
        if level is None:
            up = msg.upper()
            if "HIGH" in up or "🔴" in msg:
                level = "HIGH"
            elif "MEDIUM" in up or "YELLOW" in msg or "🟡" in msg:
                level = "MEDIUM"
            elif "LOW" in up or "GREEN" in msg or "🟢" in msg:
                level = "LOW"

        display_text = level if level is not None else msg

        # Try common Raspberry Pi libraries (optional)
        try:
            from RPLCD.i2c import CharLCD
            # NOTE: user must configure correct i2c address for their LCD
            lcd = CharLCD('PCF8574', 0x27)
            lcd.clear()
            lcd.write_string(str(display_text)[:32])
            last_lcd_message = str(display_text)
            return True
        except Exception:
            # Not available or failed — fallback
            pass

        # Fallback: write to a file and print only the simplified level
        with open("lcd_display.txt", "w", encoding="utf-8") as f:
            f.write(str(display_text) + "\n")
        print(f"LCD updated (fallback): {display_text}")
        last_lcd_message = str(display_text)
        return True
    except Exception as e:
        print(f"Failed to update LCD: {e}")
        return False


def extract_level_from_message(msg):
    """Return simplified traffic level 'LOW'|'MEDIUM'|'HIGH' if found in msg, else None."""
    try:
        s = str(msg)
        parts = s.split("|")
        # Check structured parts first
        for p in parts:
            up = p.upper()
            if "HIGH" in up or "🔴" in p:
                return "HIGH"
            if "MEDIUM" in up or "YELLOW" in up or "🟡" in p:
                return "MEDIUM"
            if "LOW" in up or "GREEN" in up or "🟢" in p:
                return "LOW"
        # Fallback: search whole message
        up = s.upper()
        if "HIGH" in up or "🔴" in s:
            return "HIGH"
        if "MEDIUM" in up or "YELLOW" in up or "🟡" in s:
            return "MEDIUM"
        if "LOW" in up or "GREEN" in up or "🟢" in s:
            return "LOW"
    except Exception:
        pass
    return None


def send_udp_broadcast(message):
    """Send a UDP broadcast message (synchronous)."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(str(message).encode('utf-8'), (BROADCAST_IP, UDP_PORT))
        sock.close()
        print(f"UDP broadcast sent: {message}")
        return True
    except Exception as e:
        print(f"UDP send error: {e}")
        return False


def send_udp_broadcast_async(message):
    thread = threading.Thread(target=send_udp_broadcast, args=(message,), daemon=True)
    thread.start()


def udp_listener():
    """Listen for UDP messages on UDP_PORT and update LCD on receipt."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Allow reuse so multiple listeners can bind in dev environments
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception:
            pass
        s.bind(("", UDP_PORT))
        print(f"UDP listener started on port {UDP_PORT}")
        while True:
            data, addr = s.recvfrom(4096)
            try:
                msg = data.decode('utf-8', errors='replace')
            except Exception:
                msg = str(data)
            # Extract simplified level when possible
            level = extract_level_from_message(msg)
            display_text = level if level is not None else msg
            print(f"UDP received from {addr}: {display_text}")
            # Update LCD with simplified level (or full message if no level)
            update_lcd_display(display_text)
    except Exception as e:
        print(f"UDP listener error: {e}")

# --------------------------------------------------
# GEOCODING (PLACE → LAT/LON)
# --------------------------------------------------
def geocode_location(place):
    try:
        url = "https://nominatim.openstreetmap.org/search"
        params = {"q": place, "format": "json", "limit": 1}
        headers = {"User-Agent": "Traffic-Predictor-App"}

        r = requests.get(url, params=params, headers=headers, timeout=6)
        if r.status_code != 200:
            return None, None

        data = r.json()
        if not data:
            return None, None

        return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        print("Geocode error:", e)
        return None, None

# --------------------------------------------------
# TOMTOM TRAFFIC DATA
# --------------------------------------------------
def fetch_traffic_data(lat, lon):
    try:
        url = "https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json"
        params = {"point": f"{lat},{lon}", "key": TOMTOM_API_KEY}

        r = requests.get(url, params=params, timeout=8)
        if r.status_code != 200:
            raise Exception("TomTom traffic API failed")

        data = r.json()
        flow = data.get("flowSegmentData")
        if not flow:
            raise Exception("No traffic segment")

        avg_speed = safe_float(flow.get("currentSpeed", 30))
        free_speed = safe_float(flow.get("freeFlowSpeed", 60))

        raw_density = 1 - (avg_speed / free_speed) if free_speed > 0 else 0.6
        traffic_density = min(1.0, max(0.3, raw_density + 0.3))

        vehicle_count = int(traffic_density * 2500)

        return vehicle_count, avg_speed, traffic_density

    except Exception as e:
        print("Traffic fallback:", e)
        return 1500, 20, 0.7

# --------------------------------------------------
# WEATHER DATA
# --------------------------------------------------
def fetch_weather(lat, lon):
    try:
        url = "https://api.tomtom.com/weather/forecast/1hour/json"
        params = {"lat": lat, "lon": lon, "key": TOMTOM_API_KEY}

        r = requests.get(url, params=params, timeout=6)
        if r.status_code != 200:
            return 0

        data = r.json()
        if "results" in data and data["results"]:
            return int(data["results"][0].get("weatherCode", 0)) % 4
        return 0
    except Exception as e:
        print("Weather error:", e)
        return 0

# --------------------------------------------------
# OTHER FEATURE ESTIMATIONS
# --------------------------------------------------
def fetch_lane_count():
    return np.random.choice([2, 3, 4], p=[0.4, 0.4, 0.2])

def estimate_signal_wait(density):
    return min(180, int(density * 180))

# --------------------------------------------------
# TOMTOM ROUTE CALCULATION
# --------------------------------------------------
def calculate_tomtom_route(start_lat, start_lon, dest_lat, dest_lon, route_type="fastest"):
    """Calculate route using TomTom API with different route types"""
    try:
        # Validate coordinates
        if not (-90 <= start_lat <= 90 and -180 <= start_lon <= 180):
            print(f"Invalid start coordinates: {start_lat}, {start_lon}")
            return None
        if not (-90 <= dest_lat <= 90 and -180 <= dest_lon <= 180):
            print(f"Invalid destination coordinates: {dest_lat}, {dest_lon}")
            return None
        
        # Check if distance is reasonable for road routing (max 5000 km)
        distance = calculate_distance(start_lat, start_lon, dest_lat, dest_lon)
        print(f"Straight-line distance: {distance:.1f} km")
        
        if distance > 5000:
            print(f"⚠ Distance too large ({distance:.1f} km) - destinations likely on different continents")
            return {"error": "too_far", "distance": distance}
        
        if distance < 0.1:
            print(f"⚠ Distance too small ({distance:.1f} km) - start and destination are the same")
            return {"error": "too_close", "distance": distance}
            
        url = f"https://api.tomtom.com/routing/1/calculateRoute/{start_lat},{start_lon}:{dest_lat},{dest_lon}/json"
        
        params = {
            "key": TOMTOM_API_KEY,
            "routeType": route_type,
            "traffic": "true",
            "travelMode": "car"
        }

        print(f"Calling TomTom API for {route_type} route: {start_lat},{start_lon} -> {dest_lat},{dest_lon}")
        r = requests.get(url, params=params, timeout=15)
        
        print(f"TomTom API status: {r.status_code}")
        
        if r.status_code != 200:
            error_text = r.text[:500]
            print(f"TomTom API error: {error_text}")
            
            # Check for specific TomTom errors
            if "NO_ROUTE_FOUND" in error_text:
                print("⚠ No road route available between these locations")
                return {"error": "no_route", "distance": distance}
            
            return None

        data = r.json()
        
        if "routes" not in data:
            print(f"No 'routes' key in response. Keys: {list(data.keys())}")
            return None
            
        if not data["routes"]:
            print("Empty routes array")
            return None
        
        route = data["routes"][0]
        summary = route.get("summary", {})
        
        # Verify legs exist
        if "legs" not in route or not route["legs"]:
            print("No legs in route")
            return None
            
        leg = route["legs"][0]
        if "points" not in leg or not leg["points"]:
            print("No points in leg")
            return None
        
        distance_meters = summary.get("lengthInMeters", 0)
        travel_time_seconds = summary.get("travelTimeInSeconds", 0)
        traffic_delay_seconds = summary.get("trafficDelayInSeconds", 0)
        
        # Calculate traffic delay if API didn't provide it or it's 0
        if traffic_delay_seconds <= 0 and distance_meters > 0 and travel_time_seconds > 0:
            # Use traffic data to estimate delay
            try:
                vehicle_count, current_speed, traffic_density = fetch_traffic_data(start_lat, start_lon)
                
                # Estimate speeds for delay calculation
                # If we have multiple points, use average of sampled points
                avg_current_speed = current_speed if current_speed > 0 else 30
                free_flow_speed = 80  # Default free flow speed for urban areas
                
                # If traffic density is high, reduce free flow estimate
                if traffic_density > 0.8:
                    free_flow_speed = 60
                elif traffic_density < 0.5:
                    free_flow_speed = 90
                
                distance_km = distance_meters / 1000
                
                # Calculate expected time at free flow speed
                free_flow_time_min = (distance_km / free_flow_speed) * 60
                
                # Current travel time
                current_time_min = travel_time_seconds / 60
                
                # Delay is the difference
                calculated_delay_min = max(0, current_time_min - free_flow_time_min)
                traffic_delay_seconds = int(calculated_delay_min * 60)
                
                print(f"  Calculated delay: {calculated_delay_min:.1f} min (density: {traffic_density:.2f})")
            except Exception as e:
                print(f"  Could not calculate delay: {e}")
                # Use a percentage-based estimate: 10-30% of travel time based on density
                base_delay = 0.1 * travel_time_seconds
                traffic_delay_seconds = max(int(base_delay), 0)
        
        result = {
            "points": leg["points"],
            "distance_km": round(distance_meters / 1000, 2),
            "travel_time_min": round(travel_time_seconds / 60, 1),
            "traffic_delay_min": round(traffic_delay_seconds / 60, 1) if traffic_delay_seconds > 0 else round(max(0.5, 0.1 * (travel_time_seconds / 60)), 1),
            "route_type": route_type
        }
        
        print(f"✓ Route calculated: {result['distance_km']}km, {result['travel_time_min']}min, {result['traffic_delay_min']}min delay")
        return result
        
    except requests.exceptions.Timeout:
        print(f"TomTom API timeout for {route_type} route")
        return None
    except requests.exceptions.RequestException as e:
        print(f"TomTom request error ({route_type}):", e)
        return None
    except Exception as e:
        print(f"TomTom route error ({route_type}):", e)
        import traceback
        traceback.print_exc()
        return None

# --------------------------------------------------
# ROUTES
# --------------------------------------------------
@app.route("/")
def home():
    return render_template("home.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or not password:
            return render_template("register.html", error="Username and password required")

        user_id = create_user(username, password)
        if user_id is None:
            return render_template("register.html", error="Username already taken")
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user_id = verify_user(username, password)
        if user_id:
            session["user"] = username
            session["user_id"] = user_id
            return redirect(url_for("index"))
        else:
            return render_template("login.html", error="Invalid username or password")
    return render_template("login.html")

@app.route("/index")
def index():
    if "user" not in session:
        return redirect(url_for("login"))
    return render_template("index.html")


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

# --------------------------------------------------
# LOCATION NAME → LAT/LON
# --------------------------------------------------
@app.route("/resolve_location", methods=["POST"])
def resolve_location():
    place = request.form.get("location", "").strip()

    lat, lon = geocode_location(place)
    if lat is None:
        return jsonify({"success": False, "error": "Location not found"})

    return jsonify({
        "success": True,
        "latitude": lat,
        "longitude": lon
    })

# --------------------------------------------------
# AUTO FETCH TRAFFIC DATA
# --------------------------------------------------
@app.route("/fetch_data", methods=["POST"])
def fetch_data():
    lat = safe_float(request.form.get("latitude"))
    lon = safe_float(request.form.get("longitude"))

    if lat == 0 or lon == 0:
        return jsonify({"success": False, "error": "Invalid coordinates"})

    vehicle_count, avg_speed, traffic_density = fetch_traffic_data(lat, lon)
    weather = fetch_weather(lat, lon)
    lane_count = fetch_lane_count()
    signal_wait = estimate_signal_wait(traffic_density)

    now = datetime.now()
    hour = now.hour
    day_type = 0 if now.weekday() < 5 else 1

    return jsonify({
        "success": True,
        "vehicle_count": int(vehicle_count),
        "avg_speed": round(avg_speed, 2),
        "traffic_density": round(traffic_density, 2),
        "signal_wait_time": int(signal_wait),
        "lane_count": int(lane_count),
        "weather": int(weather),
        "hour": int(hour),
        "day_type": int(day_type)
    })

# --------------------------------------------------
# ROUTE COMPARISON FOR HIGH TRAFFIC
# --------------------------------------------------
@app.route("/compare_routes", methods=["POST"])
def compare_routes():
    try:
        start_lat = safe_float(request.form.get("start_lat"))
        start_lon = safe_float(request.form.get("start_lon"))
        destination = request.form.get("destination", "").strip()

        print(f"\n=== Route Comparison Request ===")
        print(f"Start: {start_lat}, {start_lon}")
        print(f"Destination: {destination}")

        if not destination:
            return jsonify({"success": False, "error": "Destination required"})

        if start_lat == 0 or start_lon == 0:
            return jsonify({"success": False, "error": "Invalid starting location. Please allow location access."})

        # Geocode destination
        print(f"Geocoding destination: {destination}")
        dest_lat, dest_lon = geocode_location(destination)
        if dest_lat is None:
            return jsonify({"success": False, "error": f"Could not find location: {destination}. Try a different name."})
        
        print(f"Destination coordinates: {dest_lat}, {dest_lon}")
        
        # Check straight-line distance first
        straight_distance = calculate_distance(start_lat, start_lon, dest_lat, dest_lon)
        print(f"Straight-line distance: {straight_distance:.1f} km")
        
        # Provide early feedback for unreasonable distances
        if straight_distance > 5000:
            return jsonify({
                "success": False, 
                "error": f"Destination too far ({straight_distance:.0f} km). Route comparison works best for distances under 5000 km. For long-distance travel, consider flying instead."
            })
        
        if straight_distance < 0.1:
            return jsonify({
                "success": False,
                "error": "Start and destination are too close (less than 100 meters). No route needed."
            })

        # Calculate multiple routes
        routes = []
        route_types = ["fastest", "shortest", "eco"]
        errors = []
        special_error = None
        
        for route_type in route_types:
            print(f"\nCalculating {route_type} route...")
            route_data = calculate_tomtom_route(start_lat, start_lon, dest_lat, dest_lon, route_type)
            
            if route_data:
                # Check if it's an error response
                if isinstance(route_data, dict) and "error" in route_data:
                    special_error = route_data
                    errors.append(route_type)
                    print(f"✗ {route_type} route failed: {route_data['error']}")
                else:
                    routes.append(route_data)
                    print(f"✓ {route_type} route added")
            else:
                errors.append(route_type)
                print(f"✗ {route_type} route failed")

        if not routes:
            # Provide specific error messages
            if special_error:
                if special_error["error"] == "too_far":
                    error_msg = f"Cannot calculate road routes for this distance ({special_error['distance']:.0f} km). The locations may be on different continents or separated by large bodies of water."
                elif special_error["error"] == "too_close":
                    error_msg = "Start and destination are the same location. No route needed."
                elif special_error["error"] == "no_route":
                    error_msg = f"No road route available between these locations. They may be separated by water or have no connecting roads ({special_error['distance']:.0f} km apart)."
                else:
                    error_msg = "Route calculation failed due to location constraints."
            else:
                error_msg = f"Could not calculate any routes. Failed: {', '.join(errors)}. Check console for details."
            
            print(f"ERROR: {error_msg}")
            return jsonify({"success": False, "error": error_msg})

        # Find best route (lowest travel time + traffic delay)
        best_route = min(routes, key=lambda r: r["travel_time_min"] + r["traffic_delay_min"])
        best_route["is_recommended"] = True

        # Mark other routes and calculate time savings
        for route in routes:
            if route != best_route:
                route["is_recommended"] = False

        # Calculate recommendation message with better time formatting
        best_total_time = best_route["travel_time_min"] + best_route["traffic_delay_min"]
        
        if len(routes) > 1:
            other_routes = [r for r in routes if not r["is_recommended"]]
            avg_other_time = sum(r["travel_time_min"] + r["traffic_delay_min"] for r in other_routes) / len(other_routes)
            time_saved = avg_other_time - best_total_time
            
            if time_saved >= 60:
                hours = int(time_saved // 60)
                mins = int(time_saved % 60)
                time_str = f"{hours} hr {mins} min" if mins > 0 else f"{hours} hr"
            else:
                time_str = f"{int(time_saved)} min"
            
            recommendation = f"🚀 Recommended: {best_route['route_type'].title()} route - saves {time_str} on average"
        else:
            recommendation = f"🚀 Best available: {best_route['route_type'].title()} route"

        # Get current date and time
        now = datetime.now()
        search_date = now.strftime("%Y-%m-%d")
        search_time = now.strftime("%H:%M:%S")

        return jsonify({
            "success": True,
            "routes": routes,
            "dest_lat": float(dest_lat),
            "dest_lon": float(dest_lon),
            "recommendation": recommendation,
            "search_date": search_date,
            "search_time": search_time
        })

    except Exception as e:
        print("Route comparison error:", e)
        return jsonify({"success": False, "error": f"Route comparison failed: {str(e)}"})

# --------------------------------------------------
# ROUTE (CURRENT LOCATION → DESTINATION)
# --------------------------------------------------
@app.route("/route", methods=["POST"])
def route():
    try:
        start_lat = safe_float(request.form.get("start_lat"))
        start_lon = safe_float(request.form.get("start_lon"))
        destination = request.form.get("destination", "").strip()

        if not destination:
            return jsonify({"success": False, "error": "Destination required"})

        if start_lat == 0 or start_lon == 0:
            return jsonify({"success": False, "error": "Invalid starting location"})

        dest_lat, dest_lon = geocode_location(destination)
        if dest_lat is None:
            return jsonify({"success": False, "error": "Destination not found"})
        
        # Check distance
        distance = calculate_distance(start_lat, start_lon, dest_lat, dest_lon)
        if distance > 5000:
            return jsonify({
                "success": False, 
                "error": f"Destination too far ({distance:.0f} km). Cannot calculate road route."
            })

        # Try TomTom first
        # Get current date and time
        now = datetime.now()
        search_date = now.strftime("%Y-%m-%d")
        search_time = now.strftime("%H:%M:%S")

        if route_data and isinstance(route_data, dict) and "error" not in route_data:
            return jsonify({
                "success": True,
                "route": route_data["points"],
                "dest_lat": float(dest_lat),
                "dest_lon": float(dest_lon),
                "distance": route_data["distance_km"],
                "time": route_data["travel_time_min"],
                "traffic_delay": route_data["traffic_delay_min"],
                "search_date": search_date,
                "search_time": search_time
            })

        # Fallback: straight line
        route_points = []
        for i in range(21):
            t = i / 20
            route_points.append({
                "latitude": start_lat + t * (dest_lat - start_lat),
                "longitude": start_lon + t * (dest_lon - start_lon)
            })

        return jsonify({
            "success": True,
            "route": route_points,
            "dest_lat": float(dest_lat),
            "dest_lon": float(dest_lon),
            "source": "fallback",
            "search_date": search_date,
            "search_time": search_time
        })

    except Exception as e:
        print("Route error:", e)
        return jsonify({"success": False, "error": str(e)})

# --------------------------------------------------
# PREDICT (RANDOM FOREST)
# --------------------------------------------------
@app.route("/predict", methods=["POST"])
def predict():
    try:
        input_data = [safe_float(request.form.get(f)) for f in FEATURES]

        arr = np.array(input_data).reshape(1, -1)
        scaled = scaler.transform(arr)

        pred = int(model.predict(scaled)[0])

        labels = {
            0: "🟢 Low Traffic",
            1: "🟡 Medium Traffic",
            2: "🔴 High Traffic"
        }

        # Store prediction in session for route comparison
        session["last_prediction"] = pred
        session["traffic_density"] = float(input_data[2])

        # Persist prediction if user logged in
        user_id = session.get("user_id")
        try:
            if user_id:
                save_prediction(user_id, int(input_data[0]), float(input_data[1]), float(input_data[2]), int(pred))
        except Exception as e:
            print("Warning: failed to save prediction:", e)

        # Prepare a concise broadcast and LCD message
        label = labels.get(pred, str(pred))
        coordinates = request.form.get("coordinates", "") or ""
        broadcast_msg = f"PREDICTION|{label}|Veh:{int(input_data[0])}|Spd:{float(input_data[1]):.1f}|Den:{float(input_data[2]):.2f}|{coordinates}|{datetime.now().isoformat()}"

        # Broadcast prediction over UDP (non-blocking)
        try:
            send_udp_broadcast_async(broadcast_msg)
        except Exception as e:
            print(f"Warning: failed to broadcast prediction: {e}")

        # Also update the LCD immediately (local update) with simplified level
        try:
            level_map = {0: "LOW", 1: "MEDIUM", 2: "HIGH"}
            simple_level = level_map.get(pred, labels.get(pred, ""))
            update_lcd_display(simple_level)
        except Exception as e:
            print(f"Warning: failed to update LCD: {e}")

        return render_template("index.html", 
                             prediction=labels[pred],
                             prediction_level=pred,
                             show_route_comparison=(pred == 2))

    except Exception as e:
        print("Prediction error:", e)
        return render_template("index.html", error="Prediction failed")

# --------------------------------------------------
# UDP BROADCAST FUNCTION (Added from Request)
# --------------------------------------------------
def start_interactive_broadcast():
    """Optional interactive broadcaster for manual testing (console input).

    This is kept for convenience but not used by the web flow. It runs in a
    daemon thread and allows typing messages to broadcast manually.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    print("Interactive UDP Broadcast Ready (type messages to send)")
    while True:
        try:
            msg = input()
            if not msg:
                continue
            sock.sendto(msg.encode('utf-8'), (BROADCAST_IP, UDP_PORT))
        except (EOFError, KeyboardInterrupt):
            break
        except Exception as e:
            print(f"UDP interactive error: {e}")

# --------------------------------------------------
# MAIN ENTRY POINT
# --------------------------------------------------
@app.route('/lcd_status', methods=['GET'])
def lcd_status():
    return jsonify({"last_message": last_lcd_message})


if __name__ == "__main__":
    # Start UDP listener thread to receive prediction/alert messages
    listener_thread = threading.Thread(target=udp_listener, daemon=True)
    listener_thread.start()

    # Optional interactive broadcaster (useful for manual testing)
    try:
        interactive_thread = threading.Thread(target=start_interactive_broadcast, daemon=True)
        interactive_thread.start()
    except Exception:
        pass

    # Run the Flask application on port 8000
    app.run(host="0.0.0.0", port=8000, debug=True, use_reloader=False)