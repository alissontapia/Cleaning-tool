import streamlit as st          # Web framework for UI
import pandas as pd             # Data manipulation library
import re                       # Regular expressions for text patterns
import tldextract               # Extract domain parts from URLs
from rapidfuzz import fuzz      # Fuzzy string matching
import requests                 # HTTP requests library
from requests.exceptions import (
    ConnectionError,
    Timeout,
    SSLError,
    InvalidURL,
    RequestException
)
import os
from dotenv import load_dotenv
import anthropic                # ← ADD: Claude API
import time                     # ← ADD: For rate limiting
from langdetect import detect, LangDetectException  # Language detection

# Load environment variables
load_dotenv(dotenv_path='.env')
api_key = os.getenv("ANTHROPIC_API_KEY")
print(f".env exists: {os.path.exists('.env')}")
print(f"Loaded API key: {api_key[:10] if api_key else 'None'}")
print(f"CWD: {os.getcwd()}")

# Initialize Claude client
client = anthropic.Anthropic(api_key=api_key)
print(f"Client created, has api_key attr: {hasattr(client, 'api_key')}")
if hasattr(client, 'api_key'):
    print(f"Client api_key: {client.api_key[:10] if client.api_key else 'None'}")

# Cache for entity classifications to avoid duplicate API calls
# Keys: either normalized domain (primary) or cleaned name (fallback)
entity_cache = {}

# Add to UI for debugging
st.write(f"Debug: .env exists: {os.path.exists('.env')}")
st.write(f"Debug: API key loaded: {api_key[:10] if api_key else 'None'}")

# Add to UI for debugging
st.write(f"Debug: Client has api_key: {hasattr(client, 'api_key')}")
if hasattr(client, 'api_key'):
    st.write(f"Debug: Client api_key set: {bool(client.api_key)}")

def normalize_domain_for_cache(domain):
    """Normalize domain to https:// format for consistent caching."""
    if not domain or not isinstance(domain, str):
        return ""
    
    domain = domain.strip().lower()
    if not domain.startswith("http"):
        domain = "https://" + domain
    if not domain.startswith("https://"):
        domain = domain.replace("http://", "https://")
    return domain

def classify_entity_claude(name, domain=None):
    """
    Classify a name as Person, Organization, or Unknown using Claude API.
    Uses domain-based caching (primary) + name-based caching (fallback).
    
    Args:
        name (str): The name to classify.
        domain (str, optional): The normalized domain to use as primary cache key.
    
    Returns:
        str: "Person", "Organization", or "Unknown".
    """
    if not name or not isinstance(name, str) or name.strip() == "":
        return "Unknown"
    
    # PRIMARY: Check domain cache first (most reliable deduplication)
    if domain:
        cache_key_domain = normalize_domain_for_cache(domain)
        if cache_key_domain and cache_key_domain in entity_cache:
            return entity_cache[cache_key_domain]
    
    # FALLBACK: Check cleaned name cache
    cache_key_name = clean_name(name) if name else None
    if cache_key_name and cache_key_name in entity_cache:
        return entity_cache[cache_key_name]
    
    # Debug: Check if client has api_key
    if not client.api_key:
        print(f"Client API key is None for name: {name}")
        return "Unknown"
    
    # Prompt design: Keep it simple and specific for consistency
    prompt = f"""Classify the following name as exactly one of: "Person", "Organization", or "Unknown". 
If it's a human individual's name, return "Person". 
If it's a company, business, or group name, return "Organization". 
If unclear or not applicable, return "Unknown".

Name: {name.strip()}

Respond with only the classification word, nothing else."""

    try:
        # Call Claude API
        response = client.messages.create(
            model="claude-sonnet-4-6",  # Latest Sonnet model
            max_tokens=10,  # Minimal output needed
            messages=[{"role": "user", "content": prompt}]
        )
        
        # Extract and clean the result
        result = response.content[0].text.strip()
        
        # Validate response
        if result in ["Person", "Organization", "Unknown"]:
            # Cache by both domain (primary) and name (fallback) for future hits
            if domain:
                cache_key_domain = normalize_domain_for_cache(domain)
                if cache_key_domain:
                    entity_cache[cache_key_domain] = result
            if cache_key_name:
                entity_cache[cache_key_name] = result
            return result
        else:
            return "Unknown"  # Fallback for unexpected output
    
    except anthropic.RateLimitError:
        print(f"Rate limit hit for name: {name}. Retrying after delay...")
        time.sleep(60)  # Wait 1 minute before retrying
        return classify_entity_claude(name, domain)  # Recursive retry with same parameters
    
    except Exception as e:
        print(f"Claude API error for '{name}': {e}")
        return "Unknown"  # Graceful fallback


# Config
BLOCKED = [
    "facebook.com", "instagram.com", "x.com",
    "twitter.com", "youtube.com", "ebay.", "dictionary."
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------------- FUNCTIONS ----------------

# 1. Clean Name
def clean_name(name):
    if pd.isna(name):
        return ""

    name = str(name).lower()

    suffixes = [
        "inc", "llc", "ltd", "corp", "co", "company", "group", "plc"
    ]

    for s in suffixes:
        name = re.sub(rf"\b{s}\b", "", name)

    name = re.sub(r"[^\w\s]", "", name)
    return name.strip()


# 2. Normalize Website (root domain)
def extract_domain(url):
    if pd.isna(url):
        return ""

    if not str(url).startswith("http"):
        url = "https://" + str(url)

    ext = tldextract.extract(url)
    return f"{ext.domain}.{ext.suffix}"


# 3. Normalize URL
def normalize_url(url):
    """Ensure the URL has http/https prefix."""
    url = str(url).strip()
    if not url.startswith("http"):
        url = "https://" + url
    return url


# 4. Check if URL is blocked
def is_blocked(url):
    """Check if the URL contains blocked keywords."""
    return any(keyword in url.lower() for keyword in BLOCKED)


# 5. Website Status - Returns: OK, Broken, Need revision, Social media
def check_url(url):
    """Validate a single URL and return status."""
    if pd.isna(url) or not str(url).strip():
        return "Need revision"

    url = normalize_url(url)

    if is_blocked(url):
        return "Social media"

    try:
        response = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        status = response.status_code

        if 200 <= status < 400:
            return "OK"
        elif status == 403:
            return "OK"
        elif status == 404:
            return "Broken"
        else:
            return "Need revision"

    except Timeout:
        return "OK"
    except (SSLError, ConnectionError, InvalidURL):
        return "Broken"
    except RequestException:
        return "Broken"

# 5b. Language Detection
def detect_language(text):
    """Detect language: English, Latin-based, or Non-English."""
    if not text or not isinstance(text, str) or text.strip() == "":
        return "Unknown"
    
    try:
        lang = detect(text)
        if lang == "en":
            return "English"
        latin_based = ["es", "fr", "pt", "it", "de", "nl", "ro", "pl", "sv", "da", "no"]
        if lang in latin_based:
            return "Latin-based"
        return "Non-English"
    except LangDetectException:
        return "Unknown"

# 6. Entity Type (with Claude API) - now passes domain for better caching
def get_entity_type(name, domain=None):
    return classify_entity_claude(name, domain)
    
# 6. Name-URL Match
def match_name_url(name, domain):
    if not name or not domain:
        return "Unknown"

    name_clean = name.replace(" ", "")
    score = fuzz.partial_ratio(name_clean, domain)

    if score > 80:
        return "Yes"
    elif score > 50:
        return "Similar"
    else:
        return "No"
# ---------------- STREAMLIT UI ----------------

st.title(" Data Cleaning Tool ")

uploaded_file = st.file_uploader("Upload CSV", type=["csv"])

if uploaded_file:

    df = pd.read_csv(uploaded_file)

    st.write("Preview:", df.head())

    if st.button("Run Cleaning"):
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        # Clean Name
        status_text.text("Cleaning names...")
        df["Normalized Name"] = df["Name"].apply(clean_name)
        progress_bar.progress(20)

        # Normalize Website
        status_text.text("Extracting domains...")
        df["Normalized Website"] = df["URL"].apply(extract_domain)
        progress_bar.progress(40)

        # Duplicate Flag
        status_text.text("Checking duplicates...")
        df["Duplicate Flag"] = df["Normalized Website"].duplicated(keep=False)
        progress_bar.progress(60)

        # Website Status
        status_text.text("Validating URLs (this may take a while)...")
        df["Website Status"] = df["URL"].apply(check_url)
        progress_bar.progress(70)
        
        # Language Detection
        status_text.text("Detecting language...")
        df["Language"] = df["Normalized Name"].apply(detect_language)
        progress_bar.progress(75)
        
        # Entity Type (Claude API) - ONLY for English & Latin-based, skip duplicates
        status_text.text("Identifying entity types...")
        entity_results = ["N/A"] * len(df)
        
        # First pass: identify first occurrence of each domain and call Claude API
        seen_domains = {}
        batch_count = 0
        
        for idx, row in df.iterrows():
            # Skip if social media or broken
            if row["Website Status"] in ["Social media", "Broken"]:
                entity_results[idx] = "N/A"
                continue
            
            # Skip if not English or Latin-based
            if row["Language"] not in ["English", "Latin-based"]:
                entity_results[idx] = "N/A"
                continue
            
            domain = row["Normalized Website"]
            
            if domain not in seen_domains:
                # First occurrence: call Claude API
                seen_domains[domain] = idx
                result = get_entity_type(row["Name"], domain)
                entity_results[idx] = result
                batch_count += 1
                
                # Rate limiting: delay every batch_size calls
                if batch_count % 10 == 0:
                    time.sleep(5)
            else:
                # Duplicate: mark for population from first occurrence
                first_idx = seen_domains[domain]
                entity_results[idx] = entity_results[first_idx]
        
        df["Entity Type"] = entity_results
        progress_bar.progress(85)

        # Name-URL Match
        status_text.text("Matching names to URLs...")
        df["Match"] = df.apply(
            lambda row: match_name_url(row["Normalized Name"], row["Normalized Website"]),
            axis=1
        )
        progress_bar.progress(100)
        
        status_text.text("")
        st.success("✅ Processing completed!")

        # Reorganize columns: Original data, Normalized data, Flags, Entity/Language, Match
        output_columns = [
            # Original data
            "Name", "URL",
            # Normalized data
            "Normalized Name", "Normalized Website",
            # Duplicate flag
            "Duplicate Flag",
            # Website status and entity type
            "Website Status", "Entity Type", "Language",
            # Match result
            "Match"
        ]
        df_output = df[output_columns]
        
        st.write(df_output.head(10))

        # Download
        csv = df_output.to_csv(index=False).encode("utf-8")

        st.download_button(
            "📥 Download Cleaned CSV",
            csv,
            "cleaned_data.csv",
            "text/csv"
        )
