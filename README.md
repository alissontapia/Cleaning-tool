#  Data Cleaning Tool — ConceptOps

A Streamlit web application that automates data cleaning and validation using Claude AI for intelligent entity classification (So far organisation and person entity type).

## Features
- **Name Cleaning**: Remove suffixes (Inc, LLC, Ltd, Corp) and special characters
- **Language Detection**: Classify names as English, Latin-based, or Non-English
- **URL Validation**: Check website status (OK, Broken, Need revision, or Social media) and verify connectivity
- **Social Media Detection**: Automatically identify and flag social media links (Facebook, Twitter, Instagram, YouTube, eBay, Dictionary)  — Excluding LinkedIn
- **Entity Classification**: Use Claude AI to classify names as Person, Organization, or Unknown (only for English/Latin-based languages)
- **Duplicate Detection**: Identify duplicate entries by normalized website domain
- **Smart Duplicate Handling**: For duplicate domains, reuse first occurrence's entity classification to save API calls
- **Name-URL Matching**: Check if company names match their website domain
- **Intelligent Caching**: Domain-based caching reduces API calls for identical entities
- **Batch Processing**: Handle large datasets efficiently with rate limiting and language-based filtering

## Quick Start
### Step 1: Clone or Download the Project
```bash
cd ~/Desktop/"Cleaning tool"
```
### Step 2: Create a Python Virtual Environment
**macOS/Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
```

### Step 3: Install Dependencies
```bash
pip3 install --upgrade pip
pip3 install -r requirements.txt
```

### Step 4: Run the Application
```bash
python3 -m streamlit run app.py
```

The app will:
1. Open automatically in your browser at `http://localhost:8501`
2. Display debug information about API key loading
3. Show the upload interface

## 📖 Usage Guide
### 1. Upload a CSV File
**Required columns:**
- `Name` - Company or person name
- `URL` - Website URL (e.g., `example.com` or `https://example.com`)
### 2. Run Cleaning
### 3. Review Results
### 4. Download Results
### Note:
For datasets >10,000 rows, split into smaller CSVs:
   ```bash
   # Example: split data.csv into chunks of 1000
   split -l 1000 data.csv chunk_
   ```

## 🔧 Configuration & Caching
### Language Filtering
The app automatically filters data by language:
- **English & Latin-based**: Full processing (URL validation, entity classification, matching)
- **Non-English**: Skip entity classification (marked as N/A)
- **Unknown**: Skip entity classification (marked as N/A)

### Smart Entity Classification with Duplicate Handling
For 10 identical domains = 1 API call instead of 10
The app uses **domain-based intelligent caching** to minimize API calls:

### Social Media & Blocked Sites
The app automatically identifies and flags these domains as "Social media" (excluding Linkedin)
Processing skipped for social media domains (no entity classification).

### URL Validation Timeout
Default timeout: **15 seconds** per URL
- Modify in code: Find `check_url()` function, change `timeout=15`

### Batch Processing
Default batch size: **10 rows**
- 5-second delay between batches prevents rate limiting
- Modify: Find `batch_size = 10` in batch processing section

## 📊 Performance Tips

| Dataset Size | Estimated Time |
|--------------|-----------------|
| 10 rows      | 1-2 minutes |
| 100 rows     | 10-15 minutes |
| 1,000 rows   | 2-3 hours |
| 10,000+ rows | Use batch export with smaller subsets |



## 📝 Example Workflow

```bash
# 1. Activate environment
source venv/bin/activate  # or appropriate command for your OS

# 2. Start app
python 3 -m streamlit run app.py. ---- # will depend what python version you have

# 3. Upload CSV (data.csv)

# 4. Click "Run Cleaning"

# 5. Wait for processing (~varies by data size)

# 6. Download cleaned_data.csv

# 7. Deactivate when done
deactivate # control + c 
```

## 🎓 Understanding the Columns
**Normalized Name**
- All lowercase
- Suffixes removed (LLC, Inc, Corp, Ltd, etc.)
- Special characters removed
- Used for entity type classification

**Duplicate Flag**
- `True` = This domain appears multiple times in dataset
- `False` = Unique domain
- Useful for identifying merged companies or data quality issues

**Entity Type**
- `Person`: Individual human name
- `Organization`: Company, business, or group
- `Unknown`: Unclear or insufficient data

**Match**
- `Yes`: Name strongly matches domain (>80% similarity)
- `Similar`: Partial match (50-80% similarity)
- `No`: No clear match (<50% similarity)

--------------------------------------------------------------------------------------------------------
## 🚀 Advanced Usage
### Modifying Processing Logic

Edit `app.py` to customize:
- `clean_name()` - Add/remove name cleaning rules -- # TO DOOOOOO
- `classify_entity_claude()` - Modify AI prompt or caching logic
