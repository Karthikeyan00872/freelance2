# Rani Cab 🚖

**Book a Cab Across Tamil Nadu** — a full-stack cab-booking platform with live ride tracking, built with a Flask backend and a vanilla HTML/JS frontend.

Rani Cab connects riders and drivers in real time: riders request a ride, nearby drivers see it instantly, and both sides track the trip live on a map until it's complete. An admin dashboard oversees drivers, rides, and platform settings.

---

## Features

- **Rider flow** — sign up / log in (including Google Sign-In), request a ride, track the assigned driver live, view ride history.
- **Driver flow** — go online/offline, see available ride requests, accept/start/complete/cancel rides, view performance stats.
- **Admin dashboard** — manage drivers, monitor platform overview, configure app settings.
- **Live location** — real-time driver location updates over Socket.IO, rendered on embedded Google Maps.
- **Auth** — email/password with hashed passwords, Google OAuth login, session-based auth, forgot-password flow via email (SMTP).
- **Multiple cab categories** — Mini, Mini 4-Seater, SUV, and more (see `src/`).

## Tech Stack

| Layer      | Technology |
|------------|------------|
| Backend    | Python, Flask, Flask-SocketIO, Flask-Cors |
| Database   | MongoDB (via PyMongo) |
| Cache/Realtime backing | Redis |
| Auth       | Google Auth, Werkzeug password hashing, session cookies |
| Email      | SMTP (e.g. Gmail) via `smtplib` |
| Frontend   | Vanilla HTML, CSS, JavaScript, Google Maps embeds |
| Realtime   | Socket.IO |

## Project Structure

```
freelance2/
├── backend/
│   ├── app.py              # Flask app: routes, sockets, auth, rides, admin logic
│   └── requirements.txt    # Python dependencies
├── frontend/
│   ├── index.html          # Landing page / rider booking entry point
│   ├── rider_dashboard.html
│   ├── driver_dashboard.html
│   └── admin.html
├── src/                    # Static assets (logo, cab category images)
├── run.ps1                 # Windows helper script to run backend + frontend together
└── README.md
```

## Getting Started

### Prerequisites

- Python 3.10+
- MongoDB running locally or a connection URI
- Redis running locally or a connection URL
- A Google Cloud OAuth Client ID (for Google Sign-In)
- (Optional) An SMTP account for sending password-reset emails

### 1. Clone the repo

```bash
git clone https://github.com/Karthikeyan00872/freelance2.git
cd freelance2
```

### 2. Set up a virtual environment and install dependencies

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r backend/requirements.txt
```

### 3. Configure environment variables

Create a `.env` file in the project root:

```env
SECRET_KEY=your-secret-key
MONGODB_URI=mongodb://localhost:27017/rani_cab
REDIS_URL=redis://localhost:6379/0
GOOGLE_CLIENT_ID=your-google-oauth-client-id
CORS_ORIGINS=http://localhost:5500,http://127.0.0.1:5500
ADMIN_USERNAME=admin
ADMIN_PASSWORD=change-me
SENDER_EMAIL=your-email@gmail.com
SENDER_APP_PASSWORD=your-app-password
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SOCKETIO_ASYNC_MODE=threading
PORT=5000
```

> All of these have sensible defaults in code except `GOOGLE_CLIENT_ID`, `SENDER_EMAIL`, and `SENDER_APP_PASSWORD`, which are required for Google Sign-In and password-reset emails to work.

### 4. Run the app

**Windows (helper script):**

```powershell
./run.ps1
```

This starts the Flask backend on `http://127.0.0.1:5000` and serves the frontend on `http://127.0.0.1:5500`.

**Manually (any OS):**

```bash
python backend/app.py
# in a second terminal, serve the frontend
python -m http.server 5500
```

Then open `http://127.0.0.1:5500/frontend/index.html` in your browser.

## API Overview

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/health` | Health check |
| GET | `/api/config` | Public runtime config |
| POST | `/api/auth/register` | Register a new user |
| POST | `/api/auth/login` | Email/password login |
| POST | `/api/auth/google` | Google OAuth login |
| POST | `/api/auth/logout` | Log out |
| GET | `/api/auth/me` | Current session user |
| POST | `/api/auth/forgot-password/request` | Start password reset |
| POST | `/api/auth/forgot-password/verify` | Verify reset code |
| POST | `/api/rides/request` | Rider requests a ride |
| GET | `/api/rides/available` | Driver sees open ride requests |
| POST | `/api/rides/<ride_id>/accept` | Driver accepts a ride |
| POST | `/api/rides/<ride_id>/start` | Driver starts a ride |
| POST | `/api/rides/<ride_id>/complete` | Driver completes a ride |
| POST | `/api/rides/<ride_id>/cancel` | Cancel a ride |
| GET | `/api/rides/active` | Current active ride |
| GET | `/api/rides/history` | Past rides |
| POST | `/api/driver/toggle-online` | Driver goes online/offline |
| GET | `/api/driver/performance` | Driver stats |
| GET / POST | `/api/admin/settings` | Get/update admin settings |
| GET | `/api/admin/overview` | Platform overview for admin |
| POST | `/api/admin/drivers` | Admin adds a driver |

**Socket.IO events:** `connect`, `driver_location` (live driver position broadcast).

## License

No license specified yet.
