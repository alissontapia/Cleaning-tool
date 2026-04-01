import streamlit as st
import pandas as pd
import json
import re
import tldextract
from rapidfuzz import fuzz
import requests
from requests.exceptions import (

    ConnectionError, Timeout, SSLError, InvalidURL, RequestException

)
import os
from dotenv import load_dotenv
import anthropic
import time
from langdetect import detect, LangDetectException

# ---------------- SETUP ----------------



load_dotenv('.env')

api_key = os.getenv("ANTHROPIC_API_KEY")

client = anthropic.Anthropic(api_key=api_key)



# Caches

entity_cache = {}



# ---------------- HELPERS ----------------



def normalize_domain_for_cache(url):

    if not url:

        return ""

    url = str(url).strip().lower()

    if not url.startswith("http"):

        url = "https://" + url

    return url



def clean_name(name):

    if pd.isna(name):

        return ""

    name = str(name).lower()

    suffixes = ["inc", "llc", "ltd", "corp", "co", "company", "group", "plc"]

    for s in suffixes:

        name = re.sub(rf"\b{s}\b", "", name)

    name = re.sub(r"[^\w\s]", "", name)

    return name.strip()



def extract_domain(url):

    if pd.isna(url):

        return ""

    if not str(url).startswith("http"):

        url = "https://" + str(url)

    ext = tldextract.extract(url)

    return f"{ext.domain}.{ext.suffix}"



def normalize_url(url):

    url = str(url).strip()

    if not url.startswith("http"):

        url = "https://" + url

    return url



# ---------------- URL CHECK ----------------



BLOCKED = ["facebook.com", "instagram.com", "x.com", "twitter.com", "youtube.com"]



def check_url(url):

    if pd.isna(url) or not str(url).strip():

        return "Need revision"



    url = normalize_url(url)



    if any(b in url.lower() for b in BLOCKED):

        return "Social media"



    try:

        response = requests.head(url, timeout=10, allow_redirects=True)

        status = response.status_code



        if 200 <= status < 400:

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



# ---------------- LANGUAGE ----------------



def detect_language(text):

    if not text:

        return "Unknown"

    try:

        lang = detect(text)

        if lang == "en":

            return "English"

        if lang in ["es","fr","pt","it","de","nl"]:

            return "Latin-based"

        return "Non-English"

    except LangDetectException:

        return "Unknown"



# ---------------- CLAUDE (COMBINED) ----------------



def enrich_with_claude(name, url):
    cache_key = normalize_domain_for_cache(url)
    if cache_key in entity_cache:
        return entity_cache[cache_key]

    system_prompt = "You are a data classification bot. You only output raw, valid JSON. Never include conversational filler."
    prompt = f"""
    Classify and enrich the following entity.
    
    Entity Name: {name}
    URL: {url}
    
    Return EXACTLY a JSON object with these keys:
    - "entity_type": (Must be "Person", "Organization", or "Unknown")
    - "aliases": (A comma-separated string of known aliases, or an empty string)
    - "description": (1 short sentence describing the entity)
    """

    try:
        response = client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=200,
            system=system_prompt, # Put instructions here
            messages=[{"role": "user", "content": prompt}]
        )

        text = response.content[0].text.strip()
        
        # Parse the JSON safely
        try:
            data = json.loads(text)
            entity_type = data.get("entity_type", "Unknown")
            aliases = data.get("aliases", "")
            desc = data.get("description", "")
        except json.JSONDecodeError:
            # Fallback if Claude messes up the JSON
            entity_type, aliases, desc = "Unknown", "", "JSON Parse Error"

        entity_cache[cache_key] = (entity_type, aliases, desc)
        return entity_type, aliases, desc

    except Exception as e:
        print(f"API Error: {e}") # Good to log this
        return "Unknown", "", ""



# ---------------- MATCH ----------------



def match_name_url(name, domain):

    if not name or not domain:

        return "Unknown"

    score = fuzz.partial_ratio(name.replace(" ", ""), domain)

    if score > 80:

        return "Yes"

    elif score > 50:

        return "Similar"

    return "No"



# ---------------- STREAMLIT ----------------



st.title("Data Cleaning Tool")



uploaded_file = st.file_uploader("Upload CSV", type=["csv"])



if uploaded_file:



    df = pd.read_csv(uploaded_file)



    # Validate columns

    if not all(c in df.columns for c in ["Name", "URL"]):

        st.error("CSV must contain Name and URL columns")

        st.stop()



    st.write(df.head())



    if st.button("Run Cleaning"):



        progress = st.progress(0)



        # ---------------- STEP 1: DEDUP BEFORE ANYTHING ----------------

        st.write("Deduplicating URLs before API calls...")



        df["Raw URL"] = df["URL"].astype(str)

        unique_urls = df["Raw URL"].dropna().unique()



        enrichment_map = {}



        for i, url in enumerate(unique_urls):

            name_sample = df[df["Raw URL"] == url]["Name"].iloc[0]



            entity_type, aliases, desc = enrich_with_claude(name_sample, url)



            enrichment_map[url] = (entity_type, aliases, desc)



            if i % 10 == 0:

                time.sleep(2)



        progress.progress(30)



        # Map back

        df["Entity Type"] = df["Raw URL"].map(lambda x: enrichment_map.get(x, ("Unknown","",""))[0])

        df["Aliases"] = df["Raw URL"].map(lambda x: enrichment_map.get(x, ("","",""))[1])

        df["Description"] = df["Raw URL"].map(lambda x: enrichment_map.get(x, ("","",""))[2])



        # ---------------- STEP 2: CLEAN ----------------



        df["Normalized Name"] = df["Name"].apply(clean_name)

        df["Normalized Website"] = df["URL"].apply(extract_domain)



        progress.progress(50)



        # ---------------- STEP 3: URL CHECK ----------------



        df["Website Status"] = df["URL"].apply(check_url)



        progress.progress(70)



        # ---------------- STEP 4: LANGUAGE ----------------



        df["Language"] = df["Normalized Name"].apply(detect_language)



        progress.progress(80)



        # ---------------- STEP 5: MATCH ----------------



        df["Match"] = df.apply(

            lambda r: match_name_url(r["Normalized Name"], r["Normalized Website"]),

            axis=1

        )



        progress.progress(100)



        st.success("Done")



        st.write(df.head(10))



        csv = df.to_csv(index=False).encode("utf-8")



        st.download_button("Download", csv, "cleaned.csv")