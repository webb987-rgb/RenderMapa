import streamlit as st
from curl_cffi import requests
import pandas as pd
import folium
from folium.plugins import HeatMap
from streamlit_folium import st_folium, folium_static
from streamlit_autorefresh import st_autorefresh
import ast
import datetime
import os
import csv
import pytz
import streamlit.components.v1 as components
import hashlib
import plotly.express as px
import plotly.graph_objects as go

# --- Google Sheets ---
import gspread
from google.oauth2.service_account import Credentials
import json

# --- 1. CONFIGURATION & TIMEZONE ---
st.set_page_config(page_title="Wolt BI Radar PRO v29.0", layout="wide", page_icon="📡")

local_tz = pytz.timezone("Europe/Belgrade")

CITIES = {
    "Nis": {"coords": (43.3209, 21.8958), "slug": "nis"},
    "Belgrade": {"coords": (44.7866, 20.4489), "slug": "beograd"},
    "Novi Sad": {"coords": (45.2671, 19.8335), "slug": "novi-sad"},
    "Kragujevac": {"coords": (44.0128, 20.9114), "slug": "kragujevac"}
}

DB_FILE = "radar_history.csv"

# Google Sheets worksheet name (tab u spreadsheetu)
GS_WORKSHEET = "radar_snapshots"

# --- 2. SESSION STATE ---
if 'lat' not in st.session_state:
    st.session_state.lat, st.session_state.lon = CITIES["Nis"]["coords"]
if 'current_city' not in st.session_state:
    st.session_state.current_city = "Nis"
if 'timer_active' not in st.session_state:
    st.session_state.timer_active = False
if 'map_data_hash' not in st.session_state:
    st.session_state.map_data_hash = ""

# --- 3. UI COMPONENTS ---
def countdown_timer(minutes):
    seconds = minutes * 60
    html_code = f"""
    <div id="timer-container" style="padding:15px; border-radius:10px; background-color:#f8f9fa; text-align:center; border: 1px solid #e9ecef; margin-bottom: 20px;">
        <p style="margin:0; font-size:0.85rem; color:#6c757d; font-family:sans-serif; text-transform: uppercase; letter-spacing: 1px;">Next Refresh In:</p>
        <span id="timer" style="font-size:2rem; font-weight:bold; color:#00c2e8; font-family: 'Courier New', monospace;">--:--</span>
    </div>
    <script>
        var timeLeft = {seconds};
        var timerDisplay = document.getElementById('timer');
        function updateTimer() {{
            var mins = Math.floor(timeLeft / 60);
            var secs = timeLeft % 60;
            timerDisplay.innerHTML = (mins < 10 ? "0" : "") + mins + ":" + (secs < 10 ? "0" : "") + secs;
            if (timeLeft <= 0) {{ clearInterval(interval); }}
            timeLeft--;
        }}
        var interval = setInterval(updateTimer, 1000);
        updateTimer();
    </script>
    """
    return components.html(html_code, height=120)

# --- 3.5 WOLT API HEADERS ---
WOLT_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9,sr-RS;q=0.8,sr;q=0.7",
    "app-currency-format": "wqQxLDIzNC41Ng==",
    "app-language": "en",
    "client-version": "1.16.109",
    "clientversionnumber": "1.16.109",
    "content-type": "application/json",
    "origin": "https://wolt.com",
    "platform": "Web",
    "priority": "u=1, i",
    "referer": "https://wolt.com/",
    "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
}

# --- 4. GOOGLE SHEETS HELPERS ---

@st.cache_resource
def get_gspread_client():
    try:
        import json as _json
        raw = dict(st.secrets["gcp_service_account"])
        creds_dict = _json.loads(_json.dumps(dict(raw)))
        client = gspread.service_account_from_dict(creds_dict)
        return client
    except Exception as e:
        st.sidebar.error(f"gspread auth greka: {e}")
        return None


def get_or_create_worksheet(client, spreadsheet_id, worksheet_name):
    try:
        sh = client.open_by_key(spreadsheet_id)
        try:
            ws = sh.worksheet(worksheet_name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=worksheet_name, rows=10000, cols=10)
            ws.append_row(["timestamp", "city", "Name", "Rating_Count", "Rating", "Online", "Cuisine_Details"])
        return ws
    except Exception as e:
        return None


def save_to_gsheets(df, city):
    try:
        client = get_gspread_client()
        if client is None:
            return False, "Google Sheets nije konfigurisan (nedostaju secrets)."

        spreadsheet_id = st.secrets["google_sheets"]["spreadsheet_id"]
        ws = get_or_create_worksheet(client, spreadsheet_id, GS_WORKSHEET)
        if ws is None:
            return False, "Ne mogu da otvorim/kreiram worksheet."

        ts = datetime.datetime.now(local_tz).strftime('%Y-%m-%d %H:%M:%S')
        rows = []
        for _, row in df.iterrows():
            rows.append([
                ts,
                city,
                str(row.get("Name", "")),
                int(row.get("Rating_Count", 0)),
                float(row.get("Rating", 0)),
                bool(row.get("Online", False)),
                str(row.get("Cuisine_Details", "")),
            ])
        ws.append_rows(rows, value_input_option="RAW")
        return True, f"✅ Snimljeno {len(rows)} restorana u Google Sheets ({ts})"
    except Exception as e:
        return False, f"Greška pri snimanju u GSheets: {e}"


@st.cache_data(ttl=900)
def load_from_gsheets(city=None):
    try:
        client = get_gspread_client()
        if client is None:
            return pd.DataFrame()

        spreadsheet_id = st.secrets["google_sheets"]["spreadsheet_id"]
        sh = client.open_by_key(spreadsheet_id)
        ws = sh.worksheet(GS_WORKSHEET)

        records = ws.get_all_records()
        if not records:
            return pd.DataFrame()

        df = pd.DataFrame(records)
        df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
        df = df.dropna(subset=['timestamp'])
        df['Rating_Count'] = pd.to_numeric(df['Rating_Count'], errors='coerce').fillna(0).astype(int)
        df['Rating'] = pd.to_numeric(df['Rating'], errors='coerce').fillna(0.0)

        if city and 'city' in df.columns:
            df = df[df['city'] == city]

        return df
    except Exception:
        return pd.DataFrame()


def gsheets_configured():
    try:
        _ = st.secrets["gcp_service_account"]
        _ = st.secrets["google_sheets"]["spreadsheet_id"]
        return True
    except Exception as e:
        st.sidebar.error(f"Secrets greška: {e}")
        return False


# --- 5. DATA SCRAPER ---

@st.cache_data(ttl=900)
def fetch_venue_list(lat, lon, city_slug):
    cols = ["Name", "Wolt Link", "Cuisine_Raw", "Cuisine_Details",
            "Lat", "Lon", "Status", "Online", "Rating", "Rating_Count"]
    empty_df = pd.DataFrame(columns=cols)

    url = "https://consumer-api.wolt.com/v1/pages/restaurants"
    params = {"lat": float(lat), "lon": float(lon)}

    try:
        r = requests.get(url, params=params, headers=WOLT_HEADERS, impersonate="chrome124", timeout=15)

        st.session_state['raw_api_debug'] = {
            "Endpoint": url,
            "HTTP Status": r.status_code,
            "Prvih 300 karaktera": r.text[:300],
            "Response Headers": dict(r.headers),
        }

        if r.status_code != 200:
            # Fallback: ako smo rate-limited (429) ili druga greska, vrati posljednje dobre podatke ako postoje
            last_good_key = f"last_good_venues_{city_slug}"
            if last_good_key in st.session_state:
                st.session_state['raw_api_debug']["Napomena"] = f"HTTP {r.status_code} — prikazujem poslednje keširane podatke"
                return st.session_state[last_good_key]
            return empty_df

        data = r.json()
        sections = data.get("sections", [])
        venue_map = {}

        for section in sections:
            for item in section.get("items", []):
                v = item.get("venue")
                if not v or not isinstance(v, dict):
                    continue

                v_slug = v.get("slug", "")
                if not v_slug or v_slug in venue_map:
                    continue

                loc = v.get("location")
                v_lat, v_lon = 0.0, 0.0
                if isinstance(loc, list) and len(loc) >= 2:
                    v_lat, v_lon = float(loc[1]), float(loc[0])
                elif isinstance(loc, dict):
                    v_lat = float(loc.get("latitude", loc.get("lat", 0)))
                    v_lon = float(loc.get("longitude", loc.get("lon", 0)))

                rating_dict = v.get("rating") or {}
                score = rating_dict.get("score", 0) if isinstance(rating_dict, dict) else 0
                volume = rating_dict.get("volume", 0) if isinstance(rating_dict, dict) else 0

                online = bool(v.get("online", False))

                raw_tags = []
                for field in ("tags", "categories", "food_categories", "cuisine_tags"):
                    val = v.get(field)
                    if isinstance(val, list):
                        for c in val:
                            if isinstance(c, dict):
                                name = c.get("name") or c.get("title") or c.get("slug", "")
                                if name:
                                    raw_tags.append(str(name))
                            elif isinstance(c, str) and c:
                                raw_tags.append(c)

                venue_map[v_slug] = {
                    "Name": v.get("name", "Unknown"),
                    "Wolt Link": f"https://wolt.com/en/srb/{city_slug}/restaurant/{v_slug}",
                    "Cuisine_Raw": sorted(set(raw_tags)),
                    "Cuisine_Details": ", ".join(sorted(set(raw_tags))) if raw_tags else "Other",
                    "Lat": v_lat,
                    "Lon": v_lon,
                    "Status": "Open 🟢" if online else "Closed 🔴",
                    "Online": online,
                    "Rating": score,
                    "Rating_Count": int(volume),
                }

        # Dump svih kljuceva prvog venue objekta da vidimo strukturu API-ja
        first_venue_keys = {}
        for section in sections:
            for item in section.get("items", []):
                v = item.get("venue")
                if v and isinstance(v, dict):
                    first_venue_keys = {k: str(v[k])[:200] for k in v.keys()}
                    break
            if first_venue_keys:
                break

        st.session_state['debug_sections'] = {
            "endpoint": url,
            "broj_sekcija": len(sections),
            "restorana_pronadjeno": len(venue_map),
            "online": sum(1 for v in venue_map.values() if v["Online"]),
            "offline": sum(1 for v in venue_map.values() if not v["Online"]),
            "prvi_venue_kljucevi": first_venue_keys,
        }

        if venue_map:
            df_result = pd.DataFrame(list(venue_map.values())).drop_duplicates(subset=['Name'])
            st.session_state[f"last_good_venues_{city_slug}"] = df_result
            return df_result

    except Exception as e:
        st.session_state['raw_api_debug'] = {"Fatalna greška": str(e)}

    return empty_df


@st.cache_data(ttl=900)
def fetch_wolt_data(lat, lon, city_slug):
    cols = ["Name", "Wolt Link", "Cuisine_Raw", "Cuisine_Details",
            "Lat", "Lon", "Status", "Online", "Rating", "Rating_Count"]
    empty_df = pd.DataFrame(columns=cols)

    df_venue = fetch_venue_list(lat, lon, city_slug)
    if not df_venue.empty:
        return df_venue

    url = "https://consumer-api.wolt.com/v1/pages/category/restaurants"
    payload = {"lat": float(lat), "lon": float(lon)}

    try:
        r = requests.post(url, json=payload, headers=WOLT_HEADERS, impersonate="chrome124", timeout=15)

        if r.status_code != 200:
            return empty_df

        data = r.json()
        sections = data.get("sections", [])
        venue_map = {}

        for section in sections:
            for item in section.get("items", []):
                details = item.get("link", {}).get("menu_item_details", {})
                v_slug = details.get("venue_slug", "")
                if not v_slug or v_slug in venue_map:
                    continue

                rating_dict = details.get("venue_rating", {})
                score = rating_dict.get("score", 0) if isinstance(rating_dict, dict) else 0
                volume = rating_dict.get("volume", 0) if isinstance(rating_dict, dict) else 0

                estimate = details.get("estimate_range", "")
                online = bool(estimate and estimate != "")

                venue_map[v_slug] = {
                    "Name": details.get("venue_name", "Unknown"),
                    "Wolt Link": f"https://wolt.com/en/srb/{city_slug}/restaurant/{v_slug}",
                    "Cuisine_Raw": [],
                    "Cuisine_Details": "Other",
                    "Lat": 0.0,
                    "Lon": 0.0,
                    "Status": "Open 🟢" if online else "Closed 🔴",
                    "Online": online,
                    "Rating": score,
                    "Rating_Count": int(volume),
                }

        if venue_map:
            return pd.DataFrame(list(venue_map.values())).drop_duplicates(subset=['Name'])

    except Exception as e:
        st.session_state['raw_api_debug'] = {"Fatalna greška (fallback)": str(e)}

    return empty_df


def save_snapshot(df, city):
    if not df.empty:
        df_save = df[["Name", "Rating_Count", "Rating", "Online", "Cuisine_Details"]].copy()
        df_save['timestamp'] = datetime.datetime.now(local_tz).strftime('%Y-%m-%d %H:%M:%S')
        df_save['city'] = city
        file_exists = os.path.exists(DB_FILE)
        df_save.to_csv(DB_FILE, mode='a', header=not file_exists, index=False, quoting=csv.QUOTE_ALL)
        return True
    return False


def load_history(city):
    if not os.path.exists(DB_FILE):
        return pd.DataFrame()
    try:
        h = pd.read_csv(DB_FILE)
        h['timestamp'] = pd.to_datetime(h['timestamp'], errors='coerce')
        h = h.dropna(subset=['timestamp'])
        h['Rating_Count'] = pd.to_numeric(h['Rating_Count'], errors='coerce').fillna(0).astype(int)
        if 'city' in h.columns:
            h = h[h['city'] == city]
        return h
    except Exception:
        return pd.DataFrame()


def auto_save_if_needed(df, city):
    h = load_history(city)
    now = datetime.datetime.now(local_tz)
    if h.empty:
        save_snapshot(df, city)
        return True
    last_ts = h['timestamp'].max()
    if (now.replace(tzinfo=None) - last_ts.replace(tzinfo=None)) > datetime.timedelta(minutes=5):
        save_snapshot(df, city)
        return True
    return False


def parse_cuisine_raw(val):
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            parsed = ast.literal_eval(val)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return [v.strip() for v in val.split(",") if v.strip()] if val else []
    return []


def df_hash(df):
    if df.empty:
        return "empty"
    return hashlib.md5(pd.util.hash_pandas_object(df[["Name", "Online"]]).to_json().encode()).hexdigest()[:12]


# --- 6. SIDEBAR ---
st.sidebar.title("🛠️ Control Panel")
city_name = st.sidebar.selectbox("City:", list(CITIES.keys()))
if city_name != st.session_state.current_city:
    st.session_state.current_city = city_name
    st.session_state.lat, st.session_state.lon = CITIES[city_name]["coords"]
    st.cache_data.clear()
    st.rerun()

st.sidebar.markdown("---")
if 'last_force_refresh' not in st.session_state:
    st.session_state.last_force_refresh = None

force_refresh_cooldown_sec = 120  # 2 min cooldown izmedju force refresh klikova
can_force_refresh = True
if st.session_state.last_force_refresh:
    elapsed = (datetime.datetime.now(local_tz) - st.session_state.last_force_refresh).total_seconds()
    if elapsed < force_refresh_cooldown_sec:
        can_force_refresh = False
        st.sidebar.caption(f"⏳ Sačekaj još {int(force_refresh_cooldown_sec - elapsed)}s pre sledećeg force refresh-a")

if st.sidebar.button("🔄 Force Refresh (Clear Cache)", disabled=not can_force_refresh):
    st.cache_data.clear()
    st.session_state.pop('debug_sections', None)
    st.session_state.last_force_refresh = datetime.datetime.now(local_tz)
    st.rerun()

filter_status = st.sidebar.radio("Show only:", ["All", "Open 🟢", "Closed 🔴"])

st.sidebar.markdown("---")
refresh_min = st.sidebar.number_input(
    "Refresh Interval (min):", min_value=10, max_value=60, value=15, step=5,
    help="Minimum 10 min — kraći interval rizikuje blokadu od strane Wolt/CloudFront WAF-a."
)
st.session_state.timer_active = st.sidebar.toggle("▶️ Activate Timer", value=st.session_state.timer_active)

if st.session_state.timer_active:
    countdown_timer(refresh_min)
    st_autorefresh(interval=refresh_min * 60000, key="global_refresh")

st.sidebar.markdown("---")
if gsheets_configured():
    st.sidebar.success("🔗 Google Sheets: Aktivan")
else:
    st.sidebar.warning("⚠️ Google Sheets: Nije konfigurisan\n\nDodaj secrets u `.streamlit/secrets.toml`")

# --- 7. DATA PROCESSING ---
df_raw = fetch_wolt_data(st.session_state.lat, st.session_state.lon, CITIES[city_name]["slug"])

if not df_raw.empty and 'Cuisine_Raw' in df_raw.columns:
    df_raw['Cuisine_Raw'] = df_raw['Cuisine_Raw'].apply(parse_cuisine_raw)

df_main = df_raw.copy()

if not df_raw.empty:
    if filter_status == "Open 🟢":
        df_main = df_raw[df_raw['Online'] == True]
    elif filter_status == "Closed 🔴":
        df_main = df_raw[df_raw['Online'] == False]
    auto_save_if_needed(df_raw, city_name)

    if gsheets_configured():
        gs_key = f"gs_saved_{city_name}_{datetime.datetime.now(local_tz).strftime('%Y-%m-%d_%H')}"
        if gs_key not in st.session_state:
            ok, msg = save_to_gsheets(df_raw, city_name)
            st.session_state[gs_key] = msg

# =============================================================================
# TABS — novi redosled:
# 1. 📊 Rating History
# 2. 📈 Traffic
# 3. 📉 Traffic Tracker
# 4. 🔍 Market Analysis
# 5. 🟢 Radar
# 6. ☁️ Service Cloud
# =============================================================================
tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "📊 Rating History",
    "📈 Traffic",
    "📉 Traffic Tracker",
    "🔍 Market Analysis",
    "🟢 Radar",
    "☁️ Service Cloud"
])

# --- TAB 1: RATING HISTORY (Google Sheets) ---
with tab1:
    st.title("📊 Rating History — Google Sheets")

    if not gsheets_configured():
        st.error("❌ Google Sheets nije konfigurisan.")
        st.markdown("""
        ### Kako podesiti Google Sheets integraciju:

        **1. Kreiraj Google Service Account**
        - Idi na [Google Cloud Console](https://console.cloud.google.com/)
        - Kreiraj novi projekat (ili koristi postojeći)
        - Omogući **Google Sheets API** i **Google Drive API**
        - Idi na *IAM & Admin → Service Accounts → Create*
        - Preuzmi JSON ključ (Download JSON)

        **2. Kreiraj Google Sheet**
        - Napravi novi spreadsheet na [sheets.google.com](https://sheets.google.com)
        - Podijeli sheet sa email adresom service accounta (kao Editor)
        - Kopiraj Sheet ID iz URL-a (dio između `/d/` i `/edit`)

        **3. Dodaj secrets u `.streamlit/secrets.toml`**
        ```toml
        [gcp_service_account]
        type = "service_account"
        project_id = "tvoj-project-id"
        private_key_id = "abc123..."
        private_key = "-----BEGIN RSA PRIVATE KEY-----\\nMIIE...\\n-----END RSA PRIVATE KEY-----\\n"
        client_email = "ime@project.iam.gserviceaccount.com"
        client_id = "123456789"
        auth_uri = "https://accounts.google.com/o/oauth2/auth"
        token_uri = "https://oauth2.googleapis.com/token"

        [google_sheets]
        spreadsheet_id = "1AbCdEfGhIjKlMnOpQrStUvWxYz"
        ```

        **4. Restartuj app** — podaci će se automatski snimati svaki sat.
        """)
    else:
        col_gs1, col_gs2, col_gs3 = st.columns([2, 1, 1])
        with col_gs2:
            if st.button("💾 Snimi snapshot sada", use_container_width=True):
                if not df_raw.empty:
                    with st.spinner("Snimam u Google Sheets..."):
                        ok, msg = save_to_gsheets(df_raw, city_name)
                        if ok:
                            st.success(msg)
                            load_from_gsheets.clear()
                        else:
                            st.error(msg)
                else:
                    st.warning("Nema podataka za snimanje.")

        with col_gs3:
            if st.button("🌍 Snimi sve gradove", use_container_width=True):
                progress = st.progress(0, text="Učitavam gradove...")
                results = []
                for i, (cname, cinfo) in enumerate(CITIES.items()):
                    progress.progress((i) / len(CITIES), text=f"Skidam {cname}...")
                    try:
                        df_city = fetch_wolt_data(cinfo["coords"][0], cinfo["coords"][1], cinfo["slug"])
                        if not df_city.empty:
                            ok, msg = save_to_gsheets(df_city, cname)
                            results.append(f"{'✅' if ok else '❌'} {cname}: {msg}")
                        else:
                            results.append(f"⚠️ {cname}: nema podataka")
                    except Exception as e:
                        results.append(f"❌ {cname}: {e}")
                progress.progress(1.0, text="Gotovo!")
                load_from_gsheets.clear()
                for r in results:
                    st.write(r)

        if st.button("🔄 Osvježi historiju", use_container_width=True):
            load_from_gsheets.clear()
            st.rerun()

        with st.spinner("Učitavam historiju iz Google Sheets..."):
            gs_history_all = load_from_gsheets()

        if gs_history_all.empty:
            gs_history = pd.DataFrame()
        else:
            available_cities = sorted(gs_history_all['city'].unique().tolist()) if 'city' in gs_history_all.columns else [city_name]
            selected_city_history = st.selectbox("🏙️ Grad:", available_cities, index=available_cities.index(city_name) if city_name in available_cities else 0)
            gs_history = gs_history_all[gs_history_all['city'] == selected_city_history].copy()

        if gs_history.empty:
            st.info("📭 Nema podataka u Google Sheets za ovaj grad. Pritisni 'Snimi snapshot sada' da počneš prikupljati podatke.")
        else:
            gs_history['date'] = gs_history['timestamp'].dt.date

            first_date = gs_history['date'].min()
            last_date = gs_history['date'].max()
            unique_days = gs_history['date'].nunique()
            total_records = len(gs_history)

            cm1, cm2, cm3, cm4 = st.columns(4)
            cm1.metric("📅 Praćenje od", str(first_date))
            cm2.metric("📅 Posljednji dan", str(last_date))
            cm3.metric("📆 Broj dana", unique_days)
            cm4.metric("📝 Ukupno zapisa", total_records)

            st.divider()

            if 'Name' not in gs_history.columns:
                st.error(f"❌ Sheet nema ispravne kolone. Pronađene kolone: {list(gs_history.columns)}")
                st.stop()
            all_restaurants = sorted(gs_history['Name'].unique().tolist())

            # =====================================================
            # KALENDAR — PRVO NA VRHU (prije filtera restorana)
            # =====================================================
            st.subheader("🗓️ Kalendar — nove ocjene po danu")

            df_cal = gs_history.copy()
            df_cal['date'] = df_cal['timestamp'].dt.date

            daily_max_cal = df_cal.groupby(['date', 'Name'])['Rating_Count'].max()
            daily_min_cal = df_cal.groupby(['date', 'Name'])['Rating_Count'].min()
            daily_delta = (daily_max_cal - daily_min_cal).reset_index()
            daily_delta.columns = ['date', 'Name', 'delta']

            cal_data = daily_delta.groupby('date')['delta'].sum().reset_index()
            cal_data['date'] = pd.to_datetime(cal_data['date'])
            cal_data = cal_data.sort_values('date')

            if cal_data.empty or cal_data['delta'].sum() == 0:
                st.info("Nema dovoljno podataka za kalendar — potrebno je bar 2 snimka u istom danu.")
            else:
                import calendar
                cal_data['year'] = cal_data['date'].dt.year
                cal_data['month'] = cal_data['date'].dt.month
                cal_data['day'] = cal_data['date'].dt.day
                cal_data['weekday'] = cal_data['date'].dt.weekday
                cal_data['month_name'] = cal_data['date'].dt.strftime('%B %Y')
                cal_data['label'] = cal_data['delta'].apply(lambda x: f"+{int(x):,}" if x > 0 else "0")

                for month_name, month_df in cal_data.groupby('month_name', sort=False):
                    st.markdown(f"**{month_name}**")

                    year = month_df['year'].iloc[0]
                    month = month_df['month'].iloc[0]
                    _, num_days = calendar.monthrange(year, month)
                    first_weekday = calendar.monthrange(year, month)[0]

                    day_map = {row['day']: (row['delta'], row['label']) for _, row in month_df.iterrows()}

                    days_header = ['Pon', 'Uto', 'Sri', 'Čet', 'Pet', 'Sub', 'Ned']
                    html = '<table style="border-collapse:collapse;width:100%;font-family:sans-serif;font-size:13px;">'
                    html += '<tr>' + ''.join(f'<th style="text-align:center;padding:6px;color:#888;">{d}</th>' for d in days_header) + '</tr>'

                    day = 1
                    week_cells = ['<td></td>'] * first_weekday
                    while day <= num_days:
                        delta_val, label = day_map.get(day, (0, ''))
                        max_delta = max((v for v, _ in day_map.values()), default=1) or 1
                        intensity = min(int((delta_val / max_delta) * 200), 200) if delta_val > 0 else 0
                        bg = f"rgb({255-intensity//2}, {255-intensity//4}, {255-intensity})" if delta_val > 0 else "#f5f5f5"
                        text_color = "#1a1a2e" if intensity < 120 else "#fff"
                        cell = f"""<td style="border:1px solid #e0e0e0;border-radius:6px;padding:8px 4px;text-align:center;background:{bg};min-width:40px;">
                            <div style="font-weight:bold;color:#333;">{day}</div>
                            <div style="color:{text_color};font-size:11px;font-weight:600;">{label}</div>
                        </td>"""
                        week_cells.append(cell)
                        if len(week_cells) == 7:
                            html += '<tr>' + ''.join(week_cells) + '</tr>'
                            week_cells = []
                        day += 1

                    if week_cells:
                        while len(week_cells) < 7:
                            week_cells.append('<td></td>')
                        html += '<tr>' + ''.join(week_cells) + '</tr>'

                    html += '</table>'
                    st.markdown(html, unsafe_allow_html=True)
                    st.markdown("<br>", unsafe_allow_html=True)

            st.divider()

            # =====================================================
            # FILTER RESTORANA — ispod kalendara
            # =====================================================
            col_f1, col_f2 = st.columns([3, 1])
            with col_f1:
                selected_restaurants = st.multiselect(
                    "🏪 Odaberi restorane za prikaz:",
                    options=all_restaurants,
                    default=all_restaurants[:10] if len(all_restaurants) >= 10 else all_restaurants,
                    help="Odaberi max 20 restorana za čitljiv graf"
                )
            with col_f2:
                metric_choice = st.selectbox("📊 Metrika:", ["Rating_Count (ocjene)", "Est. narudžbe (×10)"])

            if not selected_restaurants:
                st.warning("Odaberi bar jedan restoran.")
            else:
                df_filtered = gs_history[gs_history['Name'].isin(selected_restaurants)].copy()

                # =====================================================
                # LINE CHART
                # =====================================================
                daily_avg = df_filtered.groupby(['date', 'Name'])['Rating_Count'].max().reset_index()
                daily_avg.columns = ['Datum', 'Restoran', 'Ocjena_Count']
                daily_avg['Datum'] = pd.to_datetime(daily_avg['Datum'])
                daily_avg = daily_avg.sort_values('Datum')

                if metric_choice == "Est. narudžbe (×10)":
                    daily_avg['Vrijednost'] = daily_avg.groupby('Restoran')['Ocjena_Count'].diff().fillna(0) * 10
                    y_label = "Procijenjene narudžbe"
                    chart_title = "📦 Procijenjene dnevne narudžbe po restoranu"
                else:
                    daily_avg['Vrijednost'] = daily_avg['Ocjena_Count']
                    y_label = "Ukupan broj ocjena"
                    chart_title = "⭐ Kumulativni broj ocjena po restoranu"

                st.subheader(chart_title)
                fig_line = px.line(
                    daily_avg, x='Datum', y='Vrijednost', color='Restoran',
                    markers=True,
                    labels={'Vrijednost': y_label, 'Datum': 'Datum'},
                    height=500
                )
                fig_line.update_layout(
                    legend=dict(orientation="v", yanchor="top", y=1, xanchor="left", x=1.01),
                    hovermode="x unified"
                )
                st.plotly_chart(fig_line, use_container_width=True)

                st.divider()

                # =====================================================
                # BAR CHART
                # =====================================================
                st.subheader("🏆 Top restorani — ukupni rast ocjena (cijeli period)")

                pivot = gs_history[gs_history['Name'].isin(selected_restaurants)].copy()
                pivot_agg = pivot.groupby('Name').agg(
                    min_count=('Rating_Count', 'min'),
                    max_count=('Rating_Count', 'max'),
                ).reset_index()
                pivot_agg['Rast_ocjena'] = pivot_agg['max_count'] - pivot_agg['min_count']
                pivot_agg['Est_narudzbi'] = pivot_agg['Rast_ocjena'] * 10
                pivot_agg = pivot_agg.sort_values('Est_narudzbi', ascending=False)

                fig_bar = px.bar(
                    pivot_agg, x='Name', y='Est_narudzbi',
                    color='Est_narudzbi', color_continuous_scale='Viridis',
                    labels={'Name': 'Restoran', 'Est_narudzbi': 'Est. narudžbe'},
                    height=450, text='Est_narudzbi'
                )
                fig_bar.update_traces(texttemplate='%{text:,}', textposition='outside')
                fig_bar.update_layout(showlegend=False, xaxis_tickangle=-35)
                st.plotly_chart(fig_bar, use_container_width=True)

                st.divider()

                # =====================================================
                # RAW DATA
                # =====================================================
                with st.expander("📋 Sirovi podaci iz Google Sheets"):
                    st.dataframe(
                        gs_history[gs_history['Name'].isin(selected_restaurants)]
                        .sort_values(['Name', 'timestamp'], ascending=[True, False]),
                        use_container_width=True,
                        hide_index=True
                    )
                    csv_export = gs_history.to_csv(index=False).encode('utf-8')
                    st.download_button(
                        "⬇️ Export sve podatke kao CSV",
                        csv_export,
                        f"wolt_history_{city_name}_{datetime.date.today()}.csv",
                        "text/csv"
                    )

# --- TAB 2: TRAFFIC ---
with tab2:
    st.title("📈 Traffic")

    if df_raw.empty:
        st.error("❌ Nema podataka za prikaz.")
    else:
        h = load_history(city_name)
        unique_timestamps = sorted(h['timestamp'].unique()) if not h.empty else []
        num_scans = len(unique_timestamps)

        if num_scans <= 1:
            ts_label = unique_timestamps[0].strftime('%d.%m.%Y u %H:%M:%S') if num_scans == 1 else "upravo sada"
            st.info(f"📋 **Ovo je prvi scan** — {ts_label}.")

            display_df = df_raw[["Name", "Rating_Count", "Rating", "Online", "Cuisine_Details"]].copy()
            display_df = display_df.rename(columns={
                "Name": "Restoran", "Rating_Count": "Broj ocena",
                "Rating": "Ocena", "Online": "Status", "Cuisine_Details": "Kuhinja"
            })
            display_df["Status"] = display_df["Status"].apply(lambda x: "🟢 Otvoren" if x else "🔴 Zatvoren")
            display_df = display_df.sort_values("Broj ocena", ascending=False)
            st.dataframe(display_df, use_container_width=True, hide_index=True)

        else:
            prev_ts = unique_timestamps[-1]
            curr_ts = datetime.datetime.now(local_tz)

            df_prev = h[h['timestamp'] == prev_ts][["Name", "Rating_Count"]].copy()
            df_prev = df_prev.rename(columns={"Rating_Count": "Ocene_pre"})

            df_curr = df_raw[["Name", "Rating_Count", "Rating", "Online", "Cuisine_Details"]].copy()
            df_curr = df_curr.rename(columns={"Rating_Count": "Ocene_sada"})

            merged = pd.merge(df_curr, df_prev, on="Name", how="left")
            merged["Ocene_pre"] = merged["Ocene_pre"].fillna(0).astype(int)
            merged["Ocene_sada"] = merged["Ocene_sada"].fillna(0).astype(int)
            merged["Δ Ocena"] = merged["Ocene_sada"] - merged["Ocene_pre"]
            merged["Est. narudžbi"] = merged["Δ Ocena"] * 10

            new_restaurants = merged[merged["Ocene_pre"] == 0]["Name"].tolist()
            total_new_orders = int(merged[merged["Δ Ocena"] > 0]["Est. narudžbi"].sum())
            active_count = int((merged["Δ Ocena"] > 0).sum())

            col1, col2, col3, col4 = st.columns(4)
            col1.metric("🕐 Prethodni scan", prev_ts.strftime('%d.%m. %H:%M'))
            col2.metric("🕐 Trenutni scan", curr_ts.strftime('%d.%m. %H:%M'))
            col3.metric("📦 Est. novih narudžbi", total_new_orders)
            col4.metric("🔥 Aktivnih restorana", active_count)

            st.divider()

            display = merged[[
                "Name", "Online", "Cuisine_Details",
                "Ocene_pre", "Ocene_sada", "Δ Ocena", "Est. narudžbi"
            ]].copy()
            display = display.rename(columns={
                "Name": "Restoran", "Online": "Status", "Cuisine_Details": "Kuhinja",
                "Ocene_pre": f"Ocene_prije ({prev_ts.strftime('%d.%m %H:%M')})",
                "Ocene_sada": f"Ocene_sada ({curr_ts.strftime('%d.%m %H:%M')})",
            })
            display["Status"] = display["Status"].apply(lambda x: "🟢" if x else "🔴")

            st.dataframe(
                display.sort_values("Δ Ocena", ascending=False),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Δ Ocena": st.column_config.NumberColumn("Δ Ocena", format="%+d"),
                    "Est. narudžbi": st.column_config.NumberColumn("Est. narudžbi", format="%+d"),
                }
            )

            if new_restaurants:
                st.info(
                    f"🆕 **Novi restorani od poslednjeg scana:** {', '.join(new_restaurants[:10])}" +
                    (f" i još {len(new_restaurants)-10}" if len(new_restaurants) > 10 else "")
                )

            st.divider()
            if st.button("💾 Preuzmi istoriju kao CSV"):
                full_h = load_history(city_name)
                csv_data = full_h.to_csv(index=False).encode('utf-8')
                st.download_button("⬇️ Preuzmi radar_history.csv", csv_data, "radar_history.csv", "text/csv")

# --- TAB 3: TRAFFIC TRACKER ---
with tab3:
    st.title("📉 Traffic Tracker")

    if df_raw.empty:
        st.error("❌ Nema podataka za prikaz.")
    else:
        h = load_history(city_name)
        unique_timestamps = sorted(h['timestamp'].unique()) if not h.empty else []
        num_scans = len(unique_timestamps)

        if num_scans <= 1:
            ts_label = unique_timestamps[0].strftime('%d.%m.%Y u %H:%M:%S') if num_scans == 1 else "upravo sada"
            st.info(f"📋 **Ovo je prvi scan** — {ts_label}.")

            display_df = df_raw[["Name", "Rating_Count", "Rating", "Online", "Cuisine_Details"]].copy()
            display_df = display_df.rename(columns={
                "Name": "Restoran", "Rating_Count": "Broj ocena",
                "Rating": "Ocena", "Online": "Status", "Cuisine_Details": "Kuhinja"
            })
            display_df["Status"] = display_df["Status"].apply(lambda x: "🟢 Otvoren" if x else "🔴 Zatvoren")
            display_df = display_df.sort_values("Broj ocena", ascending=False)
            st.dataframe(display_df, use_container_width=True, hide_index=True)

        else:
            prev_ts = unique_timestamps[-1]
            curr_ts = datetime.datetime.now(local_tz)

            df_prev = h[h['timestamp'] == prev_ts][["Name", "Rating_Count"]].copy()
            df_prev = df_prev.rename(columns={"Rating_Count": "Ocene_pre"})

            df_curr = df_raw[["Name", "Rating_Count", "Rating", "Online", "Cuisine_Details"]].copy()
            df_curr = df_curr.rename(columns={"Rating_Count": "Ocene_sada"})

            merged = pd.merge(df_curr, df_prev, on="Name", how="left")
            merged["Ocene_pre"] = merged["Ocene_pre"].fillna(0).astype(int)
            merged["Ocene_sada"] = merged["Ocene_sada"].fillna(0).astype(int)
            merged["Δ Ocena"] = merged["Ocene_sada"] - merged["Ocene_pre"]
            merged["Est. narudžbi"] = merged["Δ Ocena"] * 10

            new_restaurants = merged[merged["Ocene_pre"] == 0]["Name"].tolist()
            total_new_orders = int(merged[merged["Δ Ocena"] > 0]["Est. narudžbi"].sum())
            active_count = int((merged["Δ Ocena"] > 0).sum())

            col1, col2, col3, col4 = st.columns(4)
            col1.metric("🕐 Prethodni scan", prev_ts.strftime('%d.%m. %H:%M'))
            col2.metric("🕐 Trenutni scan", curr_ts.strftime('%d.%m. %H:%M'))
            col3.metric("📦 Est. novih narudžbi", total_new_orders)
            col4.metric("🔥 Aktivnih restorana", active_count)

            st.divider()

            display = merged[[
                "Name", "Online", "Cuisine_Details",
                "Ocene_pre", "Ocene_sada", "Δ Ocena", "Est. narudžbi"
            ]].copy()
            display = display.rename(columns={
                "Name": "Restoran", "Online": "Status", "Cuisine_Details": "Kuhinja",
                "Ocene_pre": f"Ocene_prije ({prev_ts.strftime('%d.%m %H:%M')})",
                "Ocene_sada": f"Ocene_sada ({curr_ts.strftime('%d.%m %H:%M')})",
            })
            display["Status"] = display["Status"].apply(lambda x: "🟢" if x else "🔴")

            st.dataframe(
                display.sort_values("Δ Ocena", ascending=False),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Δ Ocena": st.column_config.NumberColumn("Δ Ocena", format="%+d"),
                    "Est. narudžbi": st.column_config.NumberColumn("Est. narudžbi", format="%+d"),
                }
            )

            if new_restaurants:
                st.info(
                    f"🆕 **Novi restorani od poslednjeg scana:** {', '.join(new_restaurants[:10])}" +
                    (f" i još {len(new_restaurants)-10}" if len(new_restaurants) > 10 else "")
                )

            st.divider()
            if st.button("💾 Preuzmi istoriju kao CSV", key="csv_tab3"):
                full_h = load_history(city_name)
                csv_data = full_h.to_csv(index=False).encode('utf-8')
                st.download_button("⬇️ Preuzmi radar_history.csv", csv_data, "radar_history.csv", "text/csv")

# --- TAB 4: MARKET ANALYSIS ---
with tab4:
    st.title("🔍 Market Analysis")
    if df_main.empty:
        st.error("❌ Podaci nisu učitani.")
    else:
        flat_cats = [item for sublist in df_main['Cuisine_Raw'] for item in sublist if item]
        unique_cats = sorted(list(set(flat_cats)))

        if not unique_cats:
            st.warning("⚠️ Nema podataka o kuhinjama.")
            st.dataframe(df_main[["Name", "Cuisine_Raw", "Cuisine_Details"]].head(10), use_container_width=True)
        else:
            selection = st.selectbox("🍽️ Filter by Cuisine:", ["All"] + unique_cats)

            df_f = df_main
            if selection != "All":
                df_f = df_main[df_main['Cuisine_Raw'].apply(
                    lambda x: selection in x if isinstance(x, list) else False
                )]

            col1, col2, col3 = st.columns(3)
            col1.metric("🏪 Ukupno restorana", len(df_f))
            col2.metric("🟢 Otvoreni", len(df_f[df_f['Online'] == True]))
            col3.metric("🔴 Zatvoreni", len(df_f[df_f['Online'] == False]))

            map2_hash = df_hash(df_f)
            m2 = folium.Map(location=[st.session_state.lat, st.session_state.lon], zoom_start=14)
            for _, row in df_f.iterrows():
                color = "green" if row['Online'] else "red"
                folium.CircleMarker(
                    [row['Lat'], row['Lon']], radius=8, color=color, fill=True,
                    tooltip=f"{row['Name']} | {row['Cuisine_Details']}"
                ).add_to(m2)
            st_folium(m2, width="100%", height=500, key=f"m2_{map2_hash}_{selection}", returned_objects=[])

            st.subheader(f"📋 Restorani ({len(df_f)})")
            st.dataframe(
                df_f[["Name", "Status", "Rating", "Rating_Count", "Cuisine_Details", "Wolt Link"]],
                use_container_width=True,
                hide_index=True,
                column_config={"Wolt Link": st.column_config.LinkColumn("Link")}
            )

            if selection == "All" and flat_cats:
                st.subheader("📊 Distribucija kuhinja")
                from collections import Counter
                cuisine_counts = Counter(flat_cats)
                cuisine_df = pd.DataFrame(
                    cuisine_counts.items(), columns=["Kuhinja", "Broj restorana"]
                ).sort_values("Broj restorana", ascending=False).head(20)
                fig = px.bar(cuisine_df, x="Kuhinja", y="Broj restorana",
                             color="Broj restorana", color_continuous_scale="Blues")
                fig.update_layout(showlegend=False, height=400)
                st.plotly_chart(fig, use_container_width=True)

# --- TAB 5: RADAR ---
with tab5:
    st.title("🟢 Radar")
    if df_main.empty:
        st.error("❌ Podaci nisu učitani.")
        if 'raw_api_debug' in st.session_state:
            st.json(st.session_state['raw_api_debug'])
    else:
        col_m1, col_m2 = st.columns(2)
        col_m1.metric("Open 🟢", len(df_main[df_main['Online'] == True]))
        col_m2.metric("Closed 🔴", len(df_main[df_main['Online'] == False]))

        current_hash = df_hash(df_main)
        render_key = f"m1_{current_hash}_{st.session_state.lat:.4f}_{st.session_state.lon:.4f}"

        m1 = folium.Map(location=[st.session_state.lat, st.session_state.lon], zoom_start=14)
        folium.Marker(
            [st.session_state.lat, st.session_state.lon],
            icon=folium.Icon(color='blue', icon='home')
        ).add_to(m1)
        for _, row in df_main.iterrows():
            color = "green" if row['Online'] else "red"
            folium.CircleMarker(
                [row['Lat'], row['Lon']], radius=7, color=color, fill=True,
                tooltip=f"{row['Name']} | {row['Status']}"
            ).add_to(m1)

        map_resp = st_folium(m1, width="100%", height=500, key=render_key, returned_objects=["last_clicked"])
        if map_resp and map_resp.get("last_clicked"):
            st.session_state.lat = map_resp["last_clicked"]["lat"]
            st.session_state.lon = map_resp["last_clicked"]["lng"]
            st.rerun()

        st.dataframe(
            df_main[["Name", "Status", "Rating", "Rating_Count", "Cuisine_Details", "Wolt Link"]],
            use_container_width=True,
            hide_index=True,
            column_config={"Wolt Link": st.column_config.LinkColumn("Link")}
        )

        with st.expander("🗂️ Debug — API info"):
            if 'debug_sections' in st.session_state:
                st.json(st.session_state['debug_sections'])
            if 'raw_api_debug' in st.session_state:
                st.json(st.session_state['raw_api_debug'])

# --- TAB 6: SERVICE CLOUD ---
with tab6:
    st.title("☁️ Service Cloud")
    m4 = folium.Map(location=[st.session_state.lat, st.session_state.lon], zoom_start=13, tiles="cartodbpositron")
    df_a = df_main[df_main['Online'] == True] if not df_main.empty else pd.DataFrame()

    if not df_a.empty:
        pts = [[row['Lat'], row['Lon'], 1.0] for _, row in df_a.iterrows()]
        inverted_gradient = {
            0.2: '#FF0000',
            0.4: '#FF8C00',
            0.6: '#FFFF00',
            0.8: '#00FF00',
            1.0: '#0000FF'
        }
        HeatMap(pts, radius=45, blur=30, gradient=inverted_gradient).add_to(m4)

    heatmap_hash = df_hash(df_a) if not df_a.empty else "empty"
    folium_static(m4, width=1400, height=800)
