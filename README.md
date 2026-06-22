# Anthem Georgia MRF Downloader

Python + Playwright scraper for downloading Anthem machine-readable files by employer search term.

## Features

- Searches the Anthem machine-readable file portal by employer name
- Scans autocomplete suggestions for a search term (for example: `Georgia`)
- Clicks each employer result one by one
- Opens the results page
- Collects downloadable file links (`.json`, `.json.gz`, `.gz`, `.pdf`)
- Downloads files into employer-specific folders
- Skips files that already exist
- Logs progress to a `.log` file

## Requirements

- Python 3.10+
- Playwright
- Requests
- Chromium installed for Playwright

## Installation

```bash
pip install -r requirements.txt
python -m playwright install
