"""Serve the fictional Travel Desk portfolio demo."""
import argparse
from contextlib import closing
import csv
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import os
import secrets
import sqlite3
from threading import BoundedSemaphore
from urllib.parse import urlsplit

from demo import COOKIE, SessionLimit, Sessions, change, workspace
from workflow import ROOT, ROLES, DEPARTMENTS, PEOPLE, PolicyViolation, connect, dashboard, snapshot

ASSETS = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
          "/style.css": ("style.css", "text/css")}
POST_ROUTES = {"/api/session", "/api/role", "/api/reset", "/api/trips"}
LOG = logging.getLogger("travel_desk")


class Server(ThreadingHTTPServer):
    def __init__(self, address, sessions, secure=False):
        self.sessions = sessions
        self.secure = secure
        self.connections = BoundedSemaphore(24)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "TravelDesk"
    sys_version = ""

    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def log_request(self, code="-", size="-"):
        # Avoid recording request URLs, cookie values, IP addresses and request bodies.
        route = urlsplit(self.path).path
        if route == "/healthz" and code == 200:
            return
        known = route in ASSETS or route in POST_ROUTES or route in ("/healthz", "/api/report.csv", "/api/summary.csv")
        LOG.info("%s %s %s", self.command, route if known else "[unknown]", code)

    def log_message(self, format, *args):
        pass

    def send_bytes(self, code, content, mime):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if cookie := getattr(self, "session_cookie", None):
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, code, payload):
        self.send_bytes(code, json.dumps(payload).encode(), "application/json; charset=utf-8")

    def token(self):
        try:
            value = SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE)
            return value.value if value else None
        except CookieError:
            return None

    def read_payload(self):
        if self.headers.get("X-Travel-Demo") != "1":
            raise PermissionError("Demo requests must come from the workspace.")
        length = int(self.headers.get("Content-Length", 0))
        if not 0 < length <= 16000 or self.headers.get_content_type() != "application/json":
            raise ValueError("A JSON request of up to 16 KB is required.")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("Provide a JSON object.")
        return data

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        try:
            route = urlsplit(self.path).path
            post = self.command == "POST"
            if not post and route in ASSETS:
                name, mime = ASSETS[route]
                return self.send_bytes(200, (ROOT / "public" / name).read_bytes(), mime + "; charset=utf-8")
            if not post and route == "/healthz":
                return self.send_json(200, {"status": "ok"})
            if route not in (POST_ROUTES if post else ("/api/trips", "/api/report.csv", "/api/summary.csv")):
                return self.send_json(404, {"error": "Not found."})

            data = self.read_payload() if post else None
            with self.server.sessions.open(self.token(), create=route == "/api/session",
                                           recover=not post and route == "/api/trips") as (token, path, is_new):
                if is_new:
                    secure = "; Secure" if self.server.secure else ""
                    self.session_cookie = f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict{secure}"
                if route == "/api/reset":
                    self.server.sessions.fresh(path)
                with closing(connect(path)) as db:
                    actor, revision = db.execute("SELECT actor,revision FROM demo_session").fetchone()
                    if route == "/api/role":
                        selected = data.get("actor")
                        if not isinstance(selected, str) or selected not in ROLES:
                            raise ValueError("Choose a demo role.")
                        if selected != actor:
                            actor = selected
                            with db:
                                db.execute("UPDATE demo_session SET actor=?,revision=?", (actor, secrets.token_hex(16)))
                    if post and route == "/api/trips":
                        if data.get("actor") != actor or data.get("revision") != revision:
                            raise PermissionError("The demo role or session changed. Refresh before trying again.")
                        if path.stat().st_size >= 2_000_000:
                            raise SessionLimit("This workspace reached its storage limit. Reset it to continue.")
                        payload = {"id": change(db, actor, data)}
                    elif route.endswith(".csv"):
                        content = self.report(route, db, actor)
                    else:
                        payload = workspace(db, actor)
                        if is_new and route == "/api/trips":
                            payload["session_restored"] = True

            if route.endswith(".csv"):
                return self.send_bytes(200, content, "text/csv; charset=utf-8")
            self.send_json(200, payload)
        except PermissionError as exc:
            self.send_json(403, {"error": str(exc)})
        except SessionLimit as exc:
            self.send_json(503, {"error": str(exc)})
        except sqlite3.IntegrityError:
            self.send_json(409, {"error": "That booking category or receipt reference already exists."})
        except PolicyViolation as exc:
            self.send_json(422, {"error": str(exc)})
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            self.send_json(400, {"error": str(exc) if isinstance(exc, ValueError) else "Check the required fields."})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            LOG.info("Client connection closed")
        except Exception as exc:
            LOG.error("Request failed (%s)", type(exc).__name__)
            self.send_json(500, {"error": "The demo could not complete this request. Try again."})

    def report(self, route, db, actor):
        output = io.StringIO()
        writer = csv.writer(output)
        if route == "/api/report.csv":
            if ROLES[actor] != "finance":
                raise PermissionError("Finance role required.")
            writer.writerow(["Trip", "Employee", "Department", "Cost center", "Status", "Budget USD", "Provider net USD",
                             "Employee expenses USD", "Statement USD", "Variance USD", "Voucher"])
            for trip in dashboard(db, actor):
                booking, statement = trip["provider_net"], trip["provider_total"]
                writer.writerow([trip["id"], PEOPLE[trip["owner"]], DEPARTMENTS[trip["owner"]], trip["cost_center"],
                                 trip["status"], trip["budget"]/100, booking/100, trip["employee_total"]/100,
                                 "" if statement is None else statement/100,
                                 "" if statement is None else (statement-booking)/100, trip["voucher"]])
        else:
            if ROLES[actor] not in ("manager", "finance"):
                raise PermissionError("Manager or Finance role required.")
            writer.writerow(["Group", "Name", "Trips", "Provider net USD", "Employee expenses USD"])
            for group, rows in snapshot(db, actor)["reports"].items():
                for name, values in rows.items():
                    writer.writerow([group, name, values["trips"], f"{values['provider']/100:.2f}", f"{values['employee']/100:.2f}"])
        return output.getvalue().encode()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    options = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with Server((options.host, options.port), Sessions(), secure=os.getenv("DEMO_SECURE_COOKIE", "0") == "1") as server:
        LOG.info("Travel Desk listening on %s:%s", options.host, server.server_port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
