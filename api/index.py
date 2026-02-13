from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import json

# Import your existing script
import tee_times


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Basic query params (we'll wire these properly next step)
        qs = parse_qs(urlparse(self.path).query)
        players = int(qs.get("players", ["2"])[0])
        before = qs.get("before", ["10:00"])[0]
        courses = qs.get("courses", ["hamersley,collier,marangaroo,whaleback"])[0].split(",")

        # For now, just return the parameters so we know it works.
        # Next step: call your real tee_times search function and return results.
        payload = {
            "ok": True,
            "players": players,
            "before": before,
            "courses": courses,
            "note": "Endpoint is live. Next step will return real tee times."
        }

        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
