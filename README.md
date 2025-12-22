# Media Library

A self-hosted, barcode-driven media catalog for **books, movies, and video games**, optimized for mobile scanning and lightweight browsing. Designed to run locally (e.g., Raspberry Pi) using Docker.

---

## Features

- 📷 Scan barcodes using your phone camera
- 🔎 Automatic metadata lookup
- 🗂 SQLite-based local storage
- 📚 Browse, search, edit, and delete entries
- 📴 Offline-friendly with caching via Service Worker
- 🐳 Dockerized for easy deployment

---

## Project Structure

```
media-library/
├── app/
│   ├── main.py
│   └── requirements.txt
├── static/
│   ├── scan.html
│   ├── library.html
│   └── zxing.min.js
├── media/
│   └── sq.js
├── data/
│   └── media.db
├── Dockerfile
├── docker-compose.yml
└── migrations.md
```

---

## Backend

The backend is built with **FastAPI** and handles:

- Barcode scan submissions (`POST /scan`)
- Media listing and filtering (`GET /media`)
- Editing and deletion (`PUT /media/{id}`, `DELETE /media/{id}`)
- Serving static UI files

The SQLite database is stored under `/data` and mounted as a Docker volume.

---

## Frontend

### Scan UI

- Mobile-first camera interface
- Uses ZXing for barcode decoding
- Displays live server responses
- Designed for rapid multi-item scanning

### Library UI

- Search and filter by media type
- Offline cache using `localStorage`
- Modal-based editing
- Delete confirmation
- Network status indicator

---

## Offline Support

A service worker (`media/sq.js`) enables:

- Cached media lists
- Offline browsing
- Automatic refresh when back online

---

## Deployment

### Requirements

- Docker
- Docker Compose

### Run

```bash
docker compose up -d
```

Access the app:

- Scan: `http://<host>:8000/ui/scan.html`
- Library: `http://<host>:8000/ui/library.html`

---

## Environment Variables

Create a `.env` file (not committed to Git):

- Barcode lookup API key(s)

---

## Dependencies

### Python

- fastapi
- uvicorn
- requests
- python-multipart

### JavaScript

- ZXing (barcode scanning)

---

## Philosophy

- Local-first
- Private by default
- Lightweight and low-power friendly
- Designed for personal collections

---

## License

Personal / private use. Adjust as needed.
