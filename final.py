"""
Final env-based Zerodha token refresher script.

- Loads all secrets from environment variables.
- Logs into Zerodha using Selenium + TOTP.
- Gets KiteConnect access_token.
- Updates MongoDB collection `zerodhatokens`.
- Works locally and in GitHub Actions (headless Chromium).
"""

import os
import logging
import sys
from pathlib import Path
from datetime import datetime, timedelta

import pyotp
import pytz
import yaml
from dotenv import load_dotenv
from kiteconnect import KiteConnect
from retrying import retry
from pymongo import MongoClient

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from webdriver_manager.chrome import ChromeDriverManager

# -------------------------------------------------------------------------
# ENV VARIABLES (SET THESE IN GITHUB SECRETS OR LOCAL .env)
# -------------------------------------------------------------------------

# Load .env locally if present (has no effect in GitHub Actions unless you add a file)
project_root = Path(__file__).parent
dotenv_path = project_root / ".env"
if dotenv_path.is_file():
    load_dotenv(dotenv_path=dotenv_path, override=True)

ZERODHA_USER_ID = os.getenv("ZERODHA_USER_ID")
ZERODHA_PASSWORD = os.getenv("ZERODHA_PASSWORD")
ZERODHA_API_KEY = os.getenv("ZERODHA_API_KEY")
ZERODHA_API_SECRET = os.getenv("ZERODHA_API_SECRET")
ZERODHA_TOTP_SECRET = os.getenv("ZERODHA_TOTP_SECRET")

MONGO_URI = os.getenv("MONGO_URI")
# DB name is already in URI, so leave this empty
MONGO_DB_NAME = ""
MONGO_COLLECTION_NAME = "zerodhatokens"
TOKEN_UPDATED_BY = "aiman.singh30@gmail.com"

required_env = {
    "ZERODHA_USER_ID": ZERODHA_USER_ID,
    "ZERODHA_PASSWORD": ZERODHA_PASSWORD,
    "ZERODHA_API_KEY": ZERODHA_API_KEY,
    "ZERODHA_API_SECRET": ZERODHA_API_SECRET,
    "ZERODHA_TOTP_SECRET": ZERODHA_TOTP_SECRET,
    "MONGO_URI": MONGO_URI,
}
missing = [k for k, v in required_env.items() if not v]
if missing:
    raise ValueError(f"Missing required environment variables: {missing}")

# -------------------------------------------------------------------------
# GENERAL CONFIG
# -------------------------------------------------------------------------

IST = pytz.timezone("Asia/Kolkata")

logging.getLogger("selenium").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_EMBEDDED_APP_CONFIG_YAML = """
user_credentials_map:
  XW7136: "USR1_"
exchanges:
  - NSE
  - BSE
  - MCX
  - NFO
  - BFO
chrome_driver_path: ""
chrome_user_data_dir: ""
"""

# -------------------------------------------------------------------------
# HELPERS
# -------------------------------------------------------------------------

def _find_project_root():
    try:
        return Path(__file__).parent
    except NameError:
        return Path.cwd()


def load_app_config() -> dict:
    try:
        project_root = _find_project_root()
        candidates = [
            project_root / "config" / "app_config.yaml",
            project_root / "app_config.yaml",
            Path("/mnt/data/app_config.yaml"),
        ]
        for config_path in candidates:
            if config_path.is_file():
                with open(config_path, "r", encoding="utf-8") as f:
                    app_config = yaml.safe_load(f) or {}
                logging.info(f"Loaded app_config from {config_path}")
                return app_config

        app_config = yaml.safe_load(_EMBEDDED_APP_CONFIG_YAML) or {}
        logging.info("Using embedded app_config YAML fallback.")
        return app_config

    except Exception as e:
        logging.error(f"Error loading app_config.yaml: {e}")
        raise


APP_CONFIG = load_app_config()

# -------------------------------------------------------------------------
# CREDENTIAL LOADER (NOW ONLY FROM ENV)
# -------------------------------------------------------------------------

def load_config():
    """
    Loads Zerodha credentials from env vars.
    """
    return {
        "api_key": ZERODHA_API_KEY,
        "api_secret": ZERODHA_API_SECRET,
        "usr": ZERODHA_USER_ID,
        "pwd": ZERODHA_PASSWORD,
        "authenticator": ZERODHA_TOTP_SECRET,
    }

# -------------------------------------------------------------------------
# MONGO HELPER
# -------------------------------------------------------------------------

def save_token_to_mongo(access_token: str):
    """
    Upserts the token document in collection `zerodhatokens`.
    Matches document by `updatedBy = TOKEN_UPDATED_BY`.
    """
    client = MongoClient(MONGO_URI)
    try:
        if MONGO_DB_NAME:
            db = client[MONGO_DB_NAME]
        else:
            db = client.get_default_database()

        coll = db[MONGO_COLLECTION_NAME]

        now = datetime.utcnow()
        expires_at = now + timedelta(hours=24)  # adjust if needed

        result = coll.update_one(
            {"updatedBy": TOKEN_UPDATED_BY},
            {
                "$set": {
                    "accessToken": access_token,
                    "updatedAt": now,
                    "expiresAt": expires_at,
                    "isActive": True,
                    "updatedBy": TOKEN_UPDATED_BY,
                },
                "$setOnInsert": {
                    "createdAt": now,
                },
            },
            upsert=True,
        )

        logging.info(
            f"Mongo update: matched={result.matched_count}, modified={result.modified_count}, upserted_id={result.upserted_id}"
        )

    except Exception as e:
        logging.error(f"Failed to save token to MongoDB: {e}")
        raise
    finally:
        try:
            client.close()
        except Exception:
            pass

# -------------------------------------------------------------------------
# ZERODHA CLIENT (LOGIN ONLY)
# -------------------------------------------------------------------------

class ZerodhaClient:
    def __init__(self, user_id="XW7136"):
        self.user_id = user_id
        self.config = load_config()
        self.kite = None
        self.access_token = None

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def login(self):
        """
        Automated login to Zerodha using Selenium.
        Returns (kite, access_token).
        """
        logging.info("Starting Zerodha login…")
        self.kite = KiteConnect(api_key=self.config["api_key"])
        login_url = self.kite.login_url()

        chrome_driver_path = APP_CONFIG.get("chrome_driver_path") or ""
        chrome_user_data_dir = APP_CONFIG.get("chrome_user_data_dir") or ""

        chrome_options = Options()
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option("useAutomationExtension", False)

        # Optional local profile reuse
        if chrome_user_data_dir and not os.getenv("GITHUB_ACTIONS"):
            chrome_options.add_argument(f"--user-data-dir={chrome_user_data_dir}")
            logging.info(f"Using chrome user-data-dir: {chrome_user_data_dir}")

        # Headless + proper flags for GitHub Actions / CI
        if os.getenv("GITHUB_ACTIONS") == "true":
            chrome_bin = os.getenv("CHROME_BIN", "/usr/bin/chromium-browser")
            chrome_options.binary_location = chrome_bin
            chrome_options.add_argument("--headless=new")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--window-size=1920,1080")
            chrome_options.add_argument("--remote-debugging-port=9222")

        driver = None

        # 1) Try explicit driver path from config (if any)
        if chrome_driver_path:
            try:
                service = Service(chrome_driver_path)
                driver = webdriver.Chrome(service=service, options=chrome_options)
                logging.info("Started Chrome via chrome_driver_path.")
            except Exception as e:
                logging.warning(f"Failed with chrome_driver_path: {e}")
                driver = None

        # 2) Use webdriver-manager to download a matching driver
        if driver is None:
            try:
                logging.info("====== WebDriver manager ======")
                driver_path = ChromeDriverManager().install()
                service = Service(driver_path)
                driver = webdriver.Chrome(service=service, options=chrome_options)
                logging.info("Started Chrome via webdriver-manager.")
            except Exception as e:
                logging.error(f"webdriver-manager failed: {e}")
                raise RuntimeError("Unable to start Chrome via webdriver-manager") from e

        try:
            driver.get(login_url)
            wait = WebDriverWait(driver, 20)

            # User ID
            user_id_box = wait.until(
                EC.element_to_be_clickable(
                    (By.XPATH, '//input[@type="text" or @id="userid" or @name="userid"]')
                )
            )
            user_id_box.clear()
            user_id_box.send_keys(self.config["usr"])

            # Password
            password_box = wait.until(
                EC.element_to_be_clickable(
                    (By.XPATH, '//input[@type="password" or @name="password"]')
                )
            )
            password_box.clear()
            password_box.send_keys(self.config["pwd"])

            # Login submit
            submit_btns = driver.find_elements(By.XPATH, '//button[@type="submit"]')
            if submit_btns:
                submit_btns[0].click()
            else:
                alt_btns = driver.find_elements(
                    By.XPATH,
                    "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'login') "
                    "or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sign in')]",
                )
                if alt_btns:
                    alt_btns[0].click()
                else:
                    raise Exception("Login submit button not found")

            # Wait for TOTP page
            import time
            time.sleep(2)

            # TOTP input
            totp_box = wait.until(
                EC.element_to_be_clickable(
                    (By.XPATH, '//input[@type="number" or @name="totp" or @id="totp"]')
                )
            )

            # Generate and enter TOTP
            authkey = pyotp.TOTP(self.config["authenticator"]).now()
            print(f"Generated TOTP: {authkey}")

            max_retries = 3
            for attempt in range(max_retries):
                try:
                    totp_box.clear()
                    totp_box.send_keys(authkey)
                    print("TOTP entered successfully")
                    break
                except Exception:
                    if attempt < max_retries - 1:
                        print(f"Retry {attempt + 1}: re-finding TOTP field")
                        time.sleep(1)
                        totp_box = driver.find_element(
                            By.XPATH,
                            '//input[@type="number" or @name="totp" or @id="totp"]',
                        )
                    else:
                        raise

            # Submit TOTP (or auto-submit)
            time.sleep(2)
            for attempt in range(max_retries):
                try:
                    submit_btns = driver.find_elements(
                        By.XPATH,
                        '//button[@type="submit"] | '
                        '//button[contains(text(), "Continue")] | '
                        '//button[contains(text(), "CONTINUE")]',
                    )

                    if not submit_btns:
                        submit_btns = driver.find_elements(
                            By.XPATH,
                            "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'continue')]",
                        )

                    if submit_btns:
                        submit_btns[-1].click()
                        print("TOTP submit button clicked (Continue)")
                        break
                    else:
                        print("No submit button found, check if auto-submitted")
                        time.sleep(2)
                        if (
                            "request_token" in driver.current_url
                            or "status=success" in driver.current_url
                        ):
                            print("Auto-submitted successfully!")
                            break
                        raise Exception("TOTP submit button not found")
                except Exception:
                    if attempt < max_retries - 1:
                        time.sleep(1)
                    else:
                        raise

            # Wait for redirect with request_token
            wait.until(
                EC.url_contains("request_token") or EC.url_contains("status=success")
            )
            rt_url = driver.current_url

            if "request_token=" in rt_url:
                rt = rt_url.split("request_token=")[1].split("&")[0]
            elif "request_token" in rt_url:
                parts = rt_url.split("request_token")
                if len(parts) > 1:
                    rt = parts[1].lstrip("=&#?/")
                else:
                    raise Exception("Request token not found in URL; login failed?")
            else:
                raise Exception("Request token not found in URL; login failed?")

            data = self.kite.generate_session(rt, api_secret=self.config["api_secret"])
            self.access_token = data["access_token"]
            self.kite.set_access_token(self.access_token)

            logging.info("Login completed successfully.")

            print("\n" + "=" * 70)
            print("✅ LOGIN SUCCESSFUL!")
            print("=" * 70)
            print(f"Access Token: {self.access_token}")
            print(f"User ID: {data.get('user_id', 'N/A')}")
            print(f"User Name: {data.get('user_name', 'N/A')}")
            print(f"Login Time: {data.get('login_time', 'N/A')}")
            print("=" * 70 + "\n")

            return self.kite, self.access_token

        except Exception as e:
            logging.error(f"Login attempt failed: {e}")
            raise
        finally:
            try:
                driver.quit()
            except Exception:
                pass

# -------------------------------------------------------------------------
# MAIN
# -------------------------------------------------------------------------

def main():
    try:
        client = ZerodhaClient(user_id="XW7136")
        _, access_token = client.login()

        # Save token into MongoDB
        save_token_to_mongo(access_token)

        # Last line: token (useful for logs/debug)
        print(access_token)

    except Exception as e:
        logging.error(f"Error in main(): {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
