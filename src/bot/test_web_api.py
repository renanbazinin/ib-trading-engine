import requests
import urllib3
import os
from dotenv import load_dotenv

# Suppress insecure request warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def test_connection():
    load_dotenv()
    
    # We use the host IP if running from outside docker, or the service name if inside
    base_url = os.getenv("IBKR_WEB_API_BASE_URL", "https://127.0.0.1:5000/v1/api")
    # Clean the URL to get the root for the login check
    root_url = base_url.split("/v1/api")[0]
    
    print(f"--- IBKR Web API Tester ---")
    print(f"Target Base URL: {base_url}")
    print(f"Target Root URL: {root_url}")
    print(f"---------------------------")
    
    try:
        print(f"1. Checking Gateway Root Redirect...")
        response = requests.get(f"{root_url}/", verify=False, timeout=5, allow_redirects=False)
        print(f"   Status Code: {response.status_code}")
        location = response.headers.get("Location", "")
        if response.status_code in (200, 302, 303, 307, 308):
            print(f"   [SUCCESS] Gateway root is reachable.")
            if location:
                print(f"   Redirect Location: {location}")
        else:
            print(f"   [WARNING] Unexpected gateway root status.")
    except Exception as e:
        print(f"   [ERROR] Gateway root unreachable: {e}")

    try:
        print(f"\n2. Checking SSO Login Route...")
        login_url = f"{root_url}/sso/Login?forwardTo=22&RL=1&ip2loc=on"
        print(f"   Trying: {login_url}")
        response = requests.get(login_url, verify=False, timeout=5, allow_redirects=False)
        print(f"   Status Code: {response.status_code}")
        if response.status_code in (200, 302, 303, 307, 308):
            print(f"   [SUCCESS] SSO route is reachable.")
        else:
            print(f"   [WARNING] SSO route returned unexpected status.")
    except Exception as e:
        print(f"   [ERROR] SSO route unreachable: {e}")

    try:
        print(f"\n3. Checking Session Status (/iserver/auth/status)...")
        status_url = f"{base_url}/iserver/auth/status"
        response = requests.get(status_url, verify=False, timeout=5)
        print(f"   Status Code: {response.status_code}")

        if response.status_code == 200:
            try:
                payload = response.json()
                print(f"   Response: {payload}")
                authenticated = bool(payload.get("authenticated", False))
                if authenticated:
                    print("   [SUCCESS] Session is authenticated.")
                else:
                    print("   [WARNING] Session reached API but is not authenticated yet.")
            except ValueError:
                print("   [WARNING] 200 received but body is not valid JSON.")
        elif response.status_code == 401:
            print("   [INFO] Session is not authenticated yet (expected before manual login/2FA).")
        else:
            print(f"   [WARNING] Unexpected auth status response body: {response.text[:200]}")

        print(f"\n4. Checking Tickle (/v1/api/tickle)...")
        tickle_url = f"{base_url}/tickle"
        tickle_resp = requests.get(tickle_url, verify=False, timeout=5)
        print(f"   Status Code: {tickle_resp.status_code}")
        if tickle_resp.status_code == 200:
            print("   [SUCCESS] Tickle endpoint is reachable.")
        elif tickle_resp.status_code == 401:
            print("   [INFO] Tickle reachable but unauthenticated (expected before login).")
        else:
            print(f"   [WARNING] Unexpected tickle response body: {tickle_resp.text[:200]}")
    except Exception as e:
        print(f"   [ERROR] Session checks failed: {e}")

if __name__ == "__main__":
    test_connection()
