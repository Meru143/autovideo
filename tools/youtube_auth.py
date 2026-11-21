"""Authorize a YouTube channel using secrets/google_oauth_client.json and write secrets/youtube_<channel>.json.

Usage:
    python tools/youtube_auth.py <channel>
"""
import os
import sys
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

def authorize(channel: str, port: int = 8080) -> Path:
    root = Path(__file__).resolve().parent.parent
    client_secrets = root / "secrets" / "google_oauth_client.json"
    if not client_secrets.is_file():
        raise FileNotFoundError(f"Missing OAuth client config at {client_secrets}")
        
    token_out = root / "secrets" / f"youtube_{channel}.json"
    url_file = root / "secrets" / "youtube_auth_url.txt"
    
    flow = InstalledAppFlow.from_client_secrets_file(
        str(client_secrets),
        scopes=SCOPES,
    )
    
    class PromptHandler:
        def format(self, url):
            url_file.write_text(url, encoding="utf-8")
            print("\n" + "=" * 60, flush=True)
            print(f"GOOGLE OAUTH URL FOR {channel.upper()}:", flush=True)
            print(url, flush=True)
            print("=" * 60 + "\n", flush=True)
            try:
                os.startfile(url)
                print("Opened browser window via Windows Shell.", flush=True)
            except Exception as e:
                print(f"Note: Could not invoke os.startfile ({e}). Please copy/paste the URL above.", flush=True)
            print(f"Waiting for authorization callback on http://localhost:{port}/ ...", flush=True)
            return ""

    creds = flow.run_local_server(
        host="localhost",
        port=port,
        prompt="consent",
        access_type="offline",
        authorization_prompt_message=PromptHandler(),
        open_browser=False,
    )
    
    token_out.parent.mkdir(parents=True, exist_ok=True)
    token_out.write_text(creds.to_json(), encoding="utf-8")
    print(f"\nSuccessfully saved authorized credentials to: {token_out}", flush=True)
    
    if url_file.exists():
        url_file.unlink(missing_ok=True)
        
    return token_out

if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("Usage: python tools/youtube_auth.py <channel>", file=sys.stderr)
        sys.exit(2)
    authorize(sys.argv[1].strip())
