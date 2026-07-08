import jwt, time, uuid, os
from pathlib import Path
from dotenv import load_dotenv

from jyske_mcp.config import ENV_FILE

load_dotenv(ENV_FILE)

APP_ID = os.environ["ENABLE_BANKING_APP_ID"]
PRIVATE_KEY = Path(os.environ["ENABLE_BANKING_PRIVATE_KEY_PATH"]).expanduser().read_text()
BASE_URL = "https://api.enablebanking.com"
REDIRECT_URL = os.environ["ENABLE_BANKING_REDIRECT_URL"]


def make_token():
    now = int(time.time())
    payload = {
        "iss": "enablebanking.com",
        "aud": "api.enablebanking.com",
        "iat": now,
        "exp": now + 3600,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, PRIVATE_KEY, algorithm="RS256", headers={"kid": APP_ID})


def auth_headers():
    return {"Authorization": f"Bearer {make_token()}"}
