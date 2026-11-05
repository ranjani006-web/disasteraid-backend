from flask import Flask, request, jsonify
import requests
import mysql.connector
import os
import unicodedata
import time
import threading
from datetime import datetime, timezone
HTTP_HEADERS = {
    "User-Agent": "DisasterAid/1.0 (Disaster Warning Application)",
    "Accept": "application/json"
}
app = Flask(__name__)
# --------------------------------------------------
# WEATHER CACHE
# --------------------------------------------------

weather_cache = {}
WEATHER_CACHE_SECONDS = 1800

# --------------------------------------------------
# DATABASE CONFIGURATION
# --------------------------------------------------
DB_CONFIG = {
    "host": "disasteraid-mysql-ranjanichittu6-7272.i.aivencloud.com",
    "port": 21863,
    "user": "avnadmin",
    "password": os.environ.get("DISASTERAID_DB_PASSWORD"),
    "database": "defaultdb",
    "ssl_ca": os.environ.get(
        "DISASTERAID_DB_SSL_CA",
        os.path.join(os.path.dirname(__file__), "ca.pem")
    ),
    "ssl_verify_cert": True,
    "ssl_verify_identity": True
}
# --------------------------------------------------
# IMD CACHE
# --------------------------------------------------

IMD_CACHE_SECONDS = 300
IMD_CACHE = {
    "features": None,
    "saved_at": 0
}
IMD_CACHE_LOCK = threading.Lock()


def get_imd_features():
    """Get IMD district warnings, reusing data for five minutes."""

    now = time.monotonic()

    with IMD_CACHE_LOCK:
        if (
            IMD_CACHE["features"] is not None
            and now - IMD_CACHE["saved_at"] < IMD_CACHE_SECONDS
        ):
            return IMD_CACHE["features"]

    response = requests.get(
        "https://reactjs.imd.gov.in/geoserver/ows",
        params={
            "service": "WFS",
            "version": "1.1.0",
            "request": "GetFeature",
            "typename": "imd:district_warnings_india",
            "outputFormat": "application/json"
        },
        headers=HTTP_HEADERS,
        timeout=25
    )

    response.raise_for_status()
    data = response.json()

    features = data.get("features")

    if not isinstance(features, list):
        raise ValueError("Unexpected IMD response format")

    with IMD_CACHE_LOCK:
        IMD_CACHE["features"] = features
        IMD_CACHE["saved_at"] = time.monotonic()

    return features


# --------------------------------------------------
# COMMON FUNCTIONS
# --------------------------------------------------

def get_db_connection():
    return mysql.connector.connect(**DB_CONFIG)


def normalize(value):
    """Normalize names and remove invisible Unicode characters."""

    text = unicodedata.normalize("NFKC", str(value or ""))

    text = "".join(
        char for char in text
        if unicodedata.category(char) not in ("Cf", "Cc")
    )

    return " ".join(
        text.upper()
        .replace("_", " ")
        .replace("-", " ")
        .split()
    )


def clean_location(value):
    text = unicodedata.normalize("NFKC", str(value or ""))

    text = "".join(
        char for char in text
        if unicodedata.category(char) not in ("Cf", "Cc")
    )

    return text.strip()


def get_indian_place(location):
    """Resolve a location using Open-Meteo India geocoding."""

    response = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={
            "name": location,
            "count": 10,
            "language": "en",
            "format": "json",
            "countryCode": "IN"
        },
        headers=HTTP_HEADERS,
        timeout=15
    )

    response.raise_for_status()

    places = response.json().get("results") or []

    return next(
        (
            place for place in places
            if place.get("country_code") == "IN"
        ),
        None
    )


def get_location_candidates(location, place):
    """Build safe name/district candidates."""

    candidates = {
        normalize(location),
        normalize(place.get("name")),
        normalize(place.get("admin1")),
        normalize(place.get("admin2")),
        normalize(place.get("admin3"))
    }

    candidates.discard("")

    aliases = {
        "BENGALURU": {
            "BANGALORE",
            "BENGALURU URBAN",
            "BANGALORE URBAN"
        },
        "BENGALURU URBAN": {
            "BENGALURU",
            "BANGALORE",
            "BANGALORE URBAN"
        },
        "BANGALORE": {
            "BENGALURU",
            "BENGALURU URBAN",
            "BANGALORE URBAN"
        },
        "BANGALORE URBAN": {
            "BENGALURU",
            "BENGALURU URBAN",
            "BANGALORE"
        },
        "MYSURU": {"MYSORE"},
        "MYSORE": {"MYSURU"},
        "MANGALURU": {"MANGALORE"},
        "MANGALORE": {"MANGALURU"}
    }

    expanded = set(candidates)

    for candidate in candidates:
        expanded.update(aliases.get(candidate, set()))

    # Never substitute Kamrup or Kamrup Rural for
    # Kamrup Metropolitan.

    return expanded

def point_in_ring(longitude, latitude, ring):
    """Return 0 if outside, 1 if inside, 2 if on the boundary."""

    inside = False
    epsilon = 1e-9

    if not ring or len(ring) < 3:
        return 0

    for i in range(len(ring)):
        x1, y1 = float(ring[i][0]), float(ring[i][1])
        x2, y2 = float(ring[(i + 1) % len(ring)][0]), float(ring[(i + 1) % len(ring)][1])

        cross = (
            (longitude - x1) * (y2 - y1)
            - (latitude - y1) * (x2 - x1)
        )

        if (
            abs(cross) <= epsilon
            and min(x1, x2) - epsilon <= longitude <= max(x1, x2) + epsilon
            and min(y1, y2) - epsilon <= latitude <= max(y1, y2) + epsilon
        ):
            return 2

        if (y1 > latitude) != (y2 > latitude):
            intersection = (
                x1 + (latitude - y1) * (x2 - x1) / (y2 - y1)
            )

            if longitude < intersection:
                inside = not inside

    return 1 if inside else 0


def point_in_polygon(longitude, latitude, polygon):
    """Check a GeoJSON Polygon, including its holes."""

    if not polygon:
        return False

    outer = point_in_ring(longitude, latitude, polygon[0])

    if outer == 0:
        return False

    if outer == 2:
        return True

    for hole in polygon[1:]:
        result = point_in_ring(longitude, latitude, hole)

        if result == 1:
            return False

        if result == 2:
            return True

    return True


def point_in_geometry(longitude, latitude, geometry):
    """Check a GeoJSON Polygon or MultiPolygon."""

    if not isinstance(geometry, dict):
        return False

    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")

    if not coordinates:
        return False

    if geometry_type == "Polygon":
        return point_in_polygon(
            longitude, latitude, coordinates
        )

    if geometry_type == "MultiPolygon":
        return any(
            point_in_polygon(longitude, latitude, polygon)
            for polygon in coordinates
        )

    return False

# --------------------------------------------------
# HOME AND HEALTH
# --------------------------------------------------

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "success": True,
        "message": "DisasterAid Backend is Running!"
    }), 200


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "success": True,
        "backend": "running"
    }), 200


# --------------------------------------------------
# WEATHER API
# --------------------------------------------------

@app.route("/api/weather", methods=["GET"])
def get_weather():

    location = clean_location(
        request.args.get("location", "Bengaluru")
    )

    if not location:
        return jsonify({
            "success": False,
            "message": "Please enter a location."
        }), 400

    try:
        place = get_indian_place(location)

        if not place:
            return jsonify({
                "success": False,
                "message": "Location not found. Check the spelling."
            }), 404

        latitude = place["latitude"]
        longitude = place["longitude"]
        cache_key = f"{latitude:.4f},{longitude:.4f}"
        current_time = time.time()

        cached_weather = weather_cache.get(cache_key)

        if cached_weather and (
            current_time - cached_weather["saved_at"]
            < WEATHER_CACHE_SECONDS
        ):
            return jsonify(cached_weather["data"]), 200 

        weather_response = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": latitude,
                "longitude": longitude,
                "current": (
                    "temperature_2m,relative_humidity_2m,"
                    "precipitation,weather_code,wind_speed_10m"
                ),
                "daily": (
                    "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_sum,rain_sum,wind_speed_10m_max"
                ),
                "forecast_days": 7,
                "timezone": "auto"
            },
            headers=HTTP_HEADERS,
            timeout=20
        )

        weather_response.raise_for_status()
        weather_data = weather_response.json()

        daily = weather_data.get("daily") or {}
        forecast = []

        dates = daily.get("time", [])

        fields = {
            "weather_code": "weather_code",
            "temperature_max": "temperature_2m_max",
            "temperature_min": "temperature_2m_min",
            "rain": "rain_sum",
            "precipitation": "precipitation_sum",
            "wind_speed": "wind_speed_10m_max"
        }

        for index, date in enumerate(dates):

            day_data = {"date": date}

            for output_key, source_key in fields.items():

                values = daily.get(source_key, [])

                day_data[output_key] = (
                    values[index]
                    if index < len(values)
                    else None
                )

            forecast.append(day_data)

        weather_result = {
            "success": True,
            "location": place.get("name", location),
            "country": place.get("country", ""),
            "latitude": latitude,
            "longitude": longitude,
            "current_weather": weather_data.get("current", {}),
            "daily": daily,
            "forecast": forecast
        }

        weather_cache[cache_key] = {
            "data": weather_result,
            "saved_at": time.time()
        }

        return jsonify(weather_result), 200

  except requests.RequestException:
        app.logger.exception("Weather API request failed")

        cached_weather = weather_cache.get(cache_key)

        if cached_weather:
            app.logger.warning(
                "Returning cached weather because the weather service failed."
            )

            return jsonify(cached_weather["data"]), 200

        return jsonify({
            "success": False,
            "message": "Weather service is temporarily unavailable."
        }), 502
    except (KeyError, IndexError, TypeError, ValueError):
        app.logger.exception("Unexpected weather response format")

        return jsonify({
            "success": False,
            "message": "Could not read the weather information."
        }), 502


# --------------------------------------------------
# DISASTER REPORT API
# --------------------------------------------------

@app.route("/api/report", methods=["POST"])
def submit_report():

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({
            "success": False,
            "message": "Please submit a valid report."
        }), 400

    name = clean_location(data.get("name"))
    disaster = clean_location(data.get("disaster"))
    location = clean_location(data.get("location"))
    description = clean_location(data.get("description"))

    if not all([name, disaster, location, description]):
        return jsonify({
            "success": False,
            "message": "Please fill in all report fields."
        }), 400

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor()

        cursor.execute(
            """
            INSERT INTO disaster_reports
                (name, disaster, location, description)
            VALUES (%s, %s, %s, %s)
            """,
            (name, disaster, location, description)
        )

        connection.commit()

        return jsonify({
            "success": True,
            "message": "Disaster report saved successfully.",
            "report_id": cursor.lastrowid
        }), 201

    except mysql.connector.Error:
        if connection and connection.is_connected():
            connection.rollback()

        app.logger.exception("MySQL could not save the disaster report")

        return jsonify({
            "success": False,
            "message": "Could not save the report. Check MySQL settings."
        }), 500

    finally:
        if cursor:
            cursor.close()

        if connection and connection.is_connected():
            connection.close()


# --------------------------------------------------
# OFFICIAL IMD WARNING API
# --------------------------------------------------

@app.route("/api/official-warning", methods=["GET"])
def official_warning():

    location = clean_location(
        request.args.get("location", "")
    )

    if not location:
        return jsonify({
            "official_warning_available": False,
            "message": "Please enter a location.",
            "warning": None
        }), 400

    try:
        # Step 1: Resolve the Indian location.
        place = get_indian_place(location)

        if not place:
            return jsonify({
                "official_warning_available": False,
                "source": "India Meteorological Department (IMD)",
                "location": location,
                "message": (
                    "Location could not be identified in India. "
                    "Try a city or district name."
                ),
                "warning": None
            }), 200

             
        # Step 2: Get the location coordinates.
        try:
            latitude = float(place["latitude"])
            longitude = float(place["longitude"])
        except (KeyError, TypeError, ValueError):
            return jsonify({
                "official_warning_available": False,
                "source": "India Meteorological Department (IMD)",
                "location": location,
                "message": "Location coordinates are unavailable.",
                "warning": None
            }), 200

        # Step 3: Get the IMD district warning records.
        features = get_imd_features()

        # Step 4: Match the actual district boundary.
        matches = []

        for feature in features:
            if not isinstance(feature, dict):
                continue

            properties = feature.get("properties") or {}
            geometry = feature.get("geometry")

            if not isinstance(properties, dict):
                continue

            if point_in_geometry(longitude, latitude, geometry):
                matches.append(properties)


       
        # Step 5: No matching record.
        if not matches:

            return jsonify({
                "official_warning_available": False,
                "source": "India Meteorological Department (IMD)",
                "location": location,
                "resolved_place": place.get("name"),
                "possible_district": place.get("admin2"),
                "message": (
                    "No matching district record was found in the "
                    "queried official data. This does not mean the area "
                    "is safe. Please check the official IMD warning service."
                ),
                "warning": None
            }), 200

        # Step 6: Prefer the latest available record.
        warning = max(
            matches,
            key=lambda item: str(
                item.get("updated_at")
                or item.get("Date")
                or ""
            )
        )

        # Existing project warning-code descriptions.
        warning_descriptions = {
            "1": "No Warning",
            "2": "Heavy Rain",
            "3": "Heavy Snow",
            "4": "Thunderstorm and Lightning",
            "5": "Hailstorm",
            "6": "Dust Storm",
            "7": "Dust-Raising Winds",
            "8": "Strong Surface Winds",
            "9": "Heat Wave",
            "10": "Hot Day",
            "11": "Warm Night",
            "12": "Cold Wave",
            "13": "Cold Day",
            "14": "Ground Frost",
            "15": "Fog",
            "16": "Very Heavy Rain",
            "17": "Extremely Heavy Rain"
        }

        color_descriptions = {
            1: "Red",
            2: "Orange",
            3: "Yellow",
            4: "Green"
        }

        # Step 7: Build the five-day warning list.
        forecast_days = []

        for day in range(1, 6):

            raw_codes = str(
                warning.get(f"Day_{day}") or ""
            )

            codes = [
                code.strip()
                for code in raw_codes.split(",")
                if code.strip()
            ]

            color_code = warning.get(f"Day{day}_Color")

            try:
                color_key = int(color_code)
            except (TypeError, ValueError):
                color_key = None

            forecast_days.append({
                "day": day,
                "warning_codes": codes,
                "warning_descriptions": [
                    warning_descriptions.get(
                        code,
                        "Unrecognized warning code"
                    )
                    for code in codes
                ],
                "color_code": color_code,
                "color_description": color_descriptions.get(
                    color_key,
                    "Unknown"
                )
            })

        # Step 8: Return the matched official record.
        return jsonify({
            "official_warning_available": True,
            "source": "India Meteorological Department (IMD) GeoServer",
            "location": location,
            "district": warning.get("District"),
            "issue_date": warning.get("Date"),
            "updated_at": warning.get("updated_at"),
            "forecast_days": forecast_days,
            "warning": {
                **warning,
                "forecast_days": forecast_days
            }
        }), 200

    except requests.RequestException:
        app.logger.exception(
            "Official warning network request failed"
        )

        return jsonify({
            "official_warning_available": False,
            "source": "India Meteorological Department (IMD)",
            "location": location,
            "message": (
                "Official warning data could not be retrieved. "
                "Please check the official IMD warning service."
            ),
            "warning": None
        }), 502

    except (ValueError, TypeError, KeyError):
        app.logger.exception(
            "Official warning response could not be read"
        )

        return jsonify({
            "official_warning_available": False,
            "source": "India Meteorological Department (IMD)",
            "location": location,
            "message": (
                "The official warning response could not be interpreted. "
                "Please check the official IMD warning service."
            ),
            "warning": None
        }), 502


# --------------------------------------------------
# START BACKEND
# --------------------------------------------------

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False
    )
