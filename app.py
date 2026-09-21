"""
Smart Parking Finder Agent
--------------------------
A Gradio app that finds nearby parking spots for any address.

- Works out of the box using FREE OpenStreetMap data (no key needed).
- Optionally accepts a Google Maps API key for richer results
  (ratings, business names, live data) via the Google Places API.

Run with:  python app.py
Then open the local URL Gradio prints in your browser.
"""

import math
import os

import folium
import gradio as gr
import requests

# Google Maps API key: set the GOOGLE_MAPS_API_KEY environment variable to override.
# A hardcoded fallback is included below per user request — be aware that anyone who
# gets a copy of this file also gets this key. Restrict it in Google Cloud Console
# (Places API + Geocoding API only) to limit the damage if it leaks.
GOOGLE_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "AIzaSyDcsaBC9t9Tzn7BWyGcZOB12V5D1JE85kc")


# ---------------------------------------------------------------------------
# Geocoding: turn an address into (lat, lon)
# ---------------------------------------------------------------------------
def geocode_address(address: str, google_api_key: str | None = None):
    if google_api_key:
        url = "https://maps.googleapis.com/maps/api/geocode/json"
        params = {"address": address, "key": google_api_key}
        try:
            r = requests.get(url, params=params, timeout=10).json()
        except Exception as e:
            return None, None, f"Google geocoding request failed: {e}"
        if r.get("status") == "OK":
            loc = r["results"][0]["geometry"]["location"]
            return loc["lat"], loc["lng"], None
        return None, None, f"Google geocoding failed: {r.get('status')}"

    # Free fallback: OpenStreetMap Nominatim
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": address, "format": "json", "limit": 1}
    headers = {"User-Agent": "SmartParkingFinderAgent/1.0"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10).json()
    except Exception as e:
        return None, None, f"Geocoding request failed: {e}"
    if r:
        return float(r[0]["lat"]), float(r[0]["lon"]), None
    return None, None, "Could not find that address. Try being more specific."


# ---------------------------------------------------------------------------
# Distance helper
# ---------------------------------------------------------------------------
def haversine(lat1, lon1, lat2, lon2):
    R = 6371  # km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Parking search: free OpenStreetMap Overpass API
# ---------------------------------------------------------------------------
def find_parking_osm(lat, lon, radius_m):
    query = f"""
    [out:json][timeout:25];
    (
      node["amenity"="parking"](around:{radius_m},{lat},{lon});
      way["amenity"="parking"](around:{radius_m},{lat},{lon});
    );
    out center;
    """
    mirrors = [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://overpass.openstreetmap.ru/api/interpreter",
    ]
    data = None
    last_err = None
    for mirror in mirrors:
        try:
            r = requests.post(mirror, data={"data": query}, timeout=25)
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:
            last_err = e
            continue
    if data is None:
        raise RuntimeError(f"All OpenStreetMap servers are unavailable right now ({last_err})")

    results = []
    for el in data.get("elements", []):
        if el["type"] == "node":
            plat, plon = el["lat"], el["lon"]
        else:
            center = el.get("center")
            if not center:
                continue
            plat, plon = center["lat"], center["lon"]

        tags = el.get("tags", {})
        results.append(
            {
                "name": tags.get("name", "Unnamed Parking"),
                "lat": plat,
                "lon": plon,
                "distance_km": round(haversine(lat, lon, plat, plon), 2),
                "extra_1": tags.get("fee", "unknown fee"),
                "extra_2": tags.get("capacity", "unknown capacity"),
            }
        )
    results.sort(key=lambda x: x["distance_km"])
    return results


# ---------------------------------------------------------------------------
# Parking search: Google Places API (used only if a key is supplied)
# ---------------------------------------------------------------------------
def find_parking_google(lat, lon, radius_m, api_key):
    url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    params = {"location": f"{lat},{lon}", "radius": radius_m, "type": "parking", "key": api_key}
    r = requests.get(url, params=params, timeout=10).json()

    if r.get("status") not in ("OK", "ZERO_RESULTS"):
        return None, r.get("status")

    results = []
    for place in r.get("results", []):
        ploc = place["geometry"]["location"]
        results.append(
            {
                "name": place.get("name", "Unnamed"),
                "lat": ploc["lat"],
                "lon": ploc["lng"],
                "distance_km": round(haversine(lat, lon, ploc["lat"], ploc["lng"]), 2),
                "extra_1": place.get("rating", "N/A"),
                "extra_2": place.get("vicinity", ""),
            }
        )
    results.sort(key=lambda x: x["distance_km"])
    return results, None


# ---------------------------------------------------------------------------
# Map rendering
# ---------------------------------------------------------------------------
def build_map(lat, lon, spots):
    m = folium.Map(location=[lat, lon], zoom_start=15)
    folium.Marker(
        [lat, lon], tooltip="You are here", icon=folium.Icon(color="red", icon="user", prefix="fa")
    ).add_to(m)
    for s in spots:
        folium.Marker(
            [s["lat"], s["lon"]],
            tooltip=f"{s['name']} ({s['distance_km']} km)",
            icon=folium.Icon(color="blue", icon="car", prefix="fa"),
        ).add_to(m)
    return m._repr_html_()


# ---------------------------------------------------------------------------
# Main search handler wired to the UI
# ---------------------------------------------------------------------------
def search_parking(address, radius_km, google_api_key_input=None):
    if not address or not address.strip():
        return "Please enter an address or location.", None, ""

    radius_m = int(radius_km * 1000)
    # A key typed into the UI overrides the default; otherwise fall back to
    # the environment variable / hardcoded default set above.
    override = google_api_key_input.strip() if google_api_key_input else ""
    key = override or (GOOGLE_API_KEY.strip() if GOOGLE_API_KEY else None)

    geocode_note = ""
    lat, lon, err = geocode_address(address.strip(), key)
    if err and key:
        # Google geocoding failed (bad key, billing, restrictions, etc.) — fall back to free geocoding
        lat, lon, err2 = geocode_address(address.strip(), None)
        if err2:
            return (
                f"Could not locate '{address}'. Google geocoding failed ({err}); "
                f"free geocoding also failed ({err2}). Try a fuller address, e.g. "
                f"'Symbiosis International University, Nagpur, Maharashtra, India'.",
                None,
                "",
            )
        geocode_note = f" [Google geocoding failed ({err}) — used free geocoding instead]"
        key = None  # don't try Google Places later either, since this key is clearly broken
    elif err:
        return (
            f"Could not locate '{address}'. Try a fuller address, e.g. "
            f"'Symbiosis International University, Nagpur, Maharashtra, India'.",
            None,
            "",
        )

    try:
        if key:
            spots, gerr = find_parking_google(lat, lon, radius_m, key)
            if gerr:
                spots = find_parking_osm(lat, lon, radius_m)
                source_note = f"(Google Places failed: {gerr} — used free OpenStreetMap data instead)"
                col3, col4 = "Fee", "Capacity"
            else:
                source_note = "(via Google Places API)"
                col3, col4 = "Rating", "Address"
        else:
            spots = find_parking_osm(lat, lon, radius_m)
            source_note = "(via free OpenStreetMap data — add a Google API key above for richer results)"
            col3, col4 = "Fee", "Capacity"
    except Exception as e:
        return f"Error while searching for parking: {e}", None, ""

    if not spots:
        return (
            f"Found the location, but no parking spots are tagged in the data within "
            f"{radius_km} km. {source_note}{geocode_note} Try increasing the radius or "
            f"searching a landmark/road name instead of a building name.",
            None,
            "",
        )

    map_html = build_map(lat, lon, spots)
    table = [[s["name"], s["distance_km"], s["extra_1"], s["extra_2"]] for s in spots[:20]]
    status = f"Found {len(spots)} parking spot(s) near '{address}' {source_note}{geocode_note}"
    return status, gr.Dataframe(value=table, headers=["Name", "Distance (km)", col3, col4]), map_html


# ---------------------------------------------------------------------------
# Gradio UI
# ---------------------------------------------------------------------------
with gr.Blocks(title="Smart Parking Finder Agent") as demo:
    gr.Markdown(
        "# 🚗 Smart Parking Finder Agent\n"
        "Find nearby parking spots for any address."
    )

    with gr.Row():
        address = gr.Textbox(label="Address / Location", placeholder="e.g. Times Square, New York", scale=3)
        radius = gr.Slider(0.5, 5, value=1.5, step=0.5, label="Search radius (km)", scale=2)

    api_key_input = gr.Textbox(
        label="Google Maps API Key (optional)",
        placeholder="Leave blank to use the default key, or paste your own to override it",
        type="password",
    )

    search_btn = gr.Button("Find Parking", variant="primary")

    status = gr.Textbox(label="Status", interactive=False)
    result_table = gr.Dataframe(
        headers=["Name", "Distance (km)", "Fee/Rating", "Capacity/Address"],
        label="Nearby Parking Spots",
    )
    map_output = gr.HTML(label="Map")

    search_btn.click(
        search_parking,
        inputs=[address, radius, api_key_input],
        outputs=[status, result_table, map_output],
    )

if __name__ == "__main__":
    demo.launch(share=True)
