"""
In666 / CQPAY deposit automation -- SINGLE FILE (2nd payment option for the bot).

Ye ek hi file me sab kuch hai: cipher (AES-128-CBC), login, aur poora deposit
flow. Kisi doosri module ki zarurat nahi -- bas is file ko apne bot me import
kar lo.

    import pay2
    res = pay2.get_payment_url(500)
    # -> {"url": "https://pay.cqpay.me/pay/XXXX",
    #     "orderNo": "...", "amount": "500",
    #     "channel": "UPI", "sub_channel": "UPI.67", "account": "+91-..."}

    res["url"]  <-- yahi final payment URL hai (Telegram button me lagao).

Accounts: bot admin Telegram se accounts set karta hai (pay2.configure_accounts),
warna `accounts.json` file (ya ACCOUNTS env var) se load hote hain. Har naya
order agle account par jaata hai (round-robin) aur koi account fail ho to
automatically next account pe switch ho jaata hai. Thread-safe hai (bot ke liye).

Bot integration (bot.py): admin /pay2 se ON/OFF karta hai. ON hone par ye module
payment link banata hai; purana manual UPI/QR method OFF ho jata hai.

PROXY: saare HTTP calls ek residential proxy ke through jaate hain (gateway
region-lock ke liye). Default ProxyRise India (rotating). Override karne ke liye
`PAY2_PROXY` env var set karo; band karne ke liye `PAY2_PROXY=off`.

Deps:  pip install requests pycryptodome
"""

import base64
import hashlib
import json
import logging
import os
import random
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

# ============================================================================ config

BASE_URL = "https://a666ni.in666.cc"
LOGIN_PATH = "/hall/api/member/login"

DOMAIN = "www.in66677.com"
ORIGIN = f"https://{DOMAIN}"
SITECODE = "2338"

APP_VERSION = "v7.5.183"
APP_VERSION_SHORT = "7.5.183"
APP_BUILD_MS = 1792210926000

CURRENCY = "INR"
CLIENT_TIMEZONE = "+5.5"

# `token` header + cipher key dono isi value se derive hote hain.
STATIC_TOKEN = "b2e3d672-9d88-47a7-81b4-9d7ffc62054f"
SESSION_TOKEN = STATIC_TOKEN

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 "
    "Mobile/15E148 Safari/604.1"
)

# Ek "device"/session identify karte hain.
DEVICE_ID = str(uuid.uuid4())
BROWSER_FINGER_ID = "GEE3-01-" + uuid.uuid4().hex + uuid.uuid4().hex

FINANCE = "/hall/api/finance/pay"
PAY_TAB_CONFIG = "-"
OS_TYPE = 5
PAY_KIND = 100

# Site 503/126/128 ka hardcoded sign key.
STATIC_SIGN_KEY = "fc361cdb770aebc2126cc0dac989c896"

ACCOUNTS_FILE = "accounts.json"
ORDER_COOLDOWN = 90  # failure ke baad account itne second parked rehta hai

# ---------------------------------------------------------------- residential proxy
# Gateway ko India se hi call karna hai. Default: ProxyRise India (rotating).
# Override: PAY2_PROXY env var (poora proxy URL). Disable: PAY2_PROXY=off
DEFAULT_PROXY = (
    ""
)
_ENV_PROXY = os.getenv("PAY2_PROXY", DEFAULT_PROXY).strip().strip('"').strip("'")
# Bot panel se runtime override. None = env/default use karo.
# "" ya "off" = proxy disable, warna pura proxy URL.
_BOT_PROXY = None

_OFF_VALUES = ("off", "none", "no", "false", "0", "")


def _resolve_proxy() -> str:
    src = _ENV_PROXY if _BOT_PROXY is None else _BOT_PROXY
    s = str(src or "").strip()
    return "" if s.lower() in _OFF_VALUES else s


PROXY_URL = _resolve_proxy()


def set_proxy(proxy_url):
    """Bot panel se proxy set/change karo (runtime, restart ke bina).

    proxy_url: None -> env/default par wapas; "off"/"" -> disable; URL -> use.
    Effective proxy URL return karta hai.
    """
    global _BOT_PROXY, PROXY_URL
    if proxy_url is None:
        _BOT_PROXY = None
    else:
        _BOT_PROXY = str(proxy_url).strip()
    PROXY_URL = _resolve_proxy()

    # Purani cached sessions ko drop karo taaki naye proxy se dobara login ho
    mgr = globals().get("_manager")
    if mgr is not None:
        try:
            with mgr._lock:
                for st in mgr._states:
                    st.session = None
        except Exception:
            pass
    return PROXY_URL


def proxy_status():
    """(effective_url, source) batao — source: 'bot' | 'env' | 'off'."""
    eff = _resolve_proxy()
    if not eff:
        return "", "off"
    if _BOT_PROXY is not None:
        return eff, "bot"
    return eff, ("env" if _ENV_PROXY else "default")


def proxy_map():
    """requests ke liye proxies dict, ya None agar proxy disabled hai."""
    if not PROXY_URL:
        return None
    return {"http": PROXY_URL, "https": PROXY_URL}


def _apply_proxy(session: requests.Session) -> requests.Session:
    """Session par configured proxy lagao (agar enabled ho)."""
    p = proxy_map()
    if p:
        session.proxies.update(p)
    return session


class DepositError(Exception):
    """Login / listing / paysubmit me koi bhi failure."""

    def __init__(self, message: str, response=None):
        super().__init__(message)
        self.response = response


def parse_body(resp, cipher) -> dict:
    """HTTP body ko dict me badlo.

    HAR se confirm: site `x-data-mode: json_cipher` par do tarah ke body bhejti hai:
      * plain JSON        -> {"code":1,"msg":"..."}   (jaise payplatformlistV3)
      * raw base64 cipher -> AES-128-CBC ciphertext   (jaise payListV4 / orderInfo)
    Purane clients ke liye `{"encryptString": "..."}` wrapped form bhi handle hota hai.
    """
    resp.raise_for_status()
    text = (resp.text or "").strip()
    if not text:
        return {}

    try:
        data = json.loads(text)
    except ValueError:
        data = None

    if isinstance(data, dict) and isinstance(data.get("encryptString"), str):
        return json.loads(cipher.decrypt(data["encryptString"]))
    if isinstance(data, (dict, list)):
        return data

    # raw base64 ciphertext body
    try:
        return json.loads(cipher.decrypt(text))
    except Exception as exc:
        raise DepositError(f"undecodable response body: {type(exc).__name__}",
                           {"raw": text[:200]}) from exc


# ============================================================================ cipher

IV = b"5421698523412578"


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def key_k1(token: str) -> bytes:
    """md5(token + md5(token))[2:18] -> 16 ASCII bytes (login / static-token path)."""
    return _md5(token + _md5(token))[2:18].encode()


def key_k2(token: str, username: str) -> bytes:
    """md5(token + username)[0:16] -> 16 ASCII bytes (per-user token path)."""
    return _md5(token + username)[:16].encode()


def _zero_pad(data: bytes) -> bytes:
    """CryptoJS ZeroPadding: \\x00 se next 16-byte boundary tak."""
    rem = len(data) % 16
    return data if rem == 0 else data + b"\x00" * (16 - rem)


class Cipher:
    def __init__(self, token: str = STATIC_TOKEN, username: str = "", key_type: str = "k1"):
        if key_type == "k2":
            self.key = key_k2(token, username)
        elif key_type == "k1":
            self.key = key_k1(token)
        else:  # auto (bundle gk()): static token -> k1, warna k2
            self.key = key_k1(token) if token == STATIC_TOKEN else key_k2(token, username)

    def encrypt(self, plaintext: str) -> str:
        data = _zero_pad(plaintext.encode())
        return base64.b64encode(AES.new(self.key, AES.MODE_CBC, IV).encrypt(data)).decode()

    def decrypt(self, ciphertext_b64: str) -> str:
        raw = AES.new(self.key, AES.MODE_CBC, IV).decrypt(base64.b64decode(ciphertext_b64))
        try:
            return unpad(raw, 16).decode()
        except ValueError:
            return raw.rstrip(b"\x00").decode()


# ============================================================================ login

def build_login_payload(phone: str, password: str) -> dict:
    """Plaintext login body (loginType 4). `phone` me country code ho, jaise
    `+91-9840795827` (dash zaruri hai)."""
    return {
        "domain": ORIGIN,
        "clientType": 5,
        "jpush_id": "",
        "loginId": DEVICE_ID,
        "os_type": 5,
        "deviceOsType": 5,
        "deviceModel": "Mobile Safari v18.5",
        "platformType": "1001",
        "operationId": 0,
        "pkgId": 1,
        "fromClub": 2,
        "networkType": "UNKNOWN",
        "version": 9,
        "loginType": 4,
        "phone": phone,
        "userpass": password,
        "geeToken": "disabled",
        "time": int(time.time()),
    }


def build_headers(extra: dict | None = None) -> dict:
    now_ms = int(time.time() * 1000)
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en",
        "appsystem": "iOS 18.5",
        "appversion": APP_VERSION,
        "browserfingerid": BROWSER_FINGER_ID,
        "browsertype": "Mobile Safari v18.5",
        "clienttimezone": CLIENT_TIMEZONE,
        "content-type": "application/json",
        "currency": CURRENCY,
        "device": DEVICE_ID,
        "devicebrand": "iPhone",
        "devicemodel": "Mobile Safari v18.5",
        "domain": DOMAIN,
        "language": "en",
        "operatingsystem": "iOS",
        "origin": ORIGIN,
        "physicaldevicemodel": "unknown",
        "platformtype": "5",
        "priority": "u=1, i",
        "referer": f"{ORIGIN}/",
        "sec-ch-ua": '"Not:A-Brand";v="8", "Chromium";v="156", "Brave";v="156"',
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": '"iOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "cross-site",
        "sec-gpc": "1",
        "sitecode": SITECODE,
        "timestamp": str(int(time.time())),
        "token": SESSION_TOKEN,
        "user-agent": USER_AGENT,
        "webauthndomain": DOMAIN,
        "x-custom-referer": f"{ORIGIN}/home/login",
        "x-data-mode": "json_cipher",
        "x-device": "3-1",
        "x-object-id": json.dumps(
            {
                "uid": "",
                "browserLanguage": "en-US",
                "init": {
                    "device": DEVICE_ID,
                    "created": now_ms,
                    "version": APP_BUILD_MS,
                },
            },
            separators=(",", ":"),
        ),
        "x-request-id": str(uuid.uuid4()),
        "x-version": APP_VERSION_SHORT,
    }
    if extra:
        headers.update(extra)
    return headers


class HallClient:
    """Bas login ke liye client (cipher + headers)."""

    def __init__(self, base_url: str = BASE_URL, token: str = SESSION_TOKEN):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.session = _apply_proxy(requests.Session())
        self.access_token: str | None = None
        self.refresh_token: str | None = None

    def _unwrap_response(self, resp: requests.Response) -> dict:
        return parse_body(resp, Cipher(self.token, key_type="k1"))

    def login(self, phone: str, password: str) -> dict:
        cipher = Cipher(self.token, username=phone, key_type="k1")
        body = {"encryptString": cipher.encrypt(json.dumps(build_login_payload(phone, password)))}

        resp = self.session.post(
            f"{self.base_url}{LOGIN_PATH}",
            headers=build_headers(),
            json=body,
            timeout=20,
        )
        data = self._unwrap_response(resp)

        self.access_token = data.get("accessToken") or data.get("token")
        self.refresh_token = data.get("refreshToken")
        return data


# ============================================================================ session


class HallSession:
    """Authenticated client: ek logged-in account + json_cipher plumbing."""

    def __init__(self, login_data: dict, sitecode: str = SITECODE):
        self.username = str(login_data["username"])
        self.session_key = login_data["session_key"]
        self.jwt_token = login_data["jwt_token"]
        self.userkey = login_data.get("userkey", "")
        self.sitecode = sitecode
        self.session = _apply_proxy(requests.Session())
        self.cipher = Cipher(self.session_key, username=self.username, key_type="k2")
        self.sign_key = ""

    def auth_headers(self) -> dict:
        return build_headers(
            {
                "token": self.session_key,
                "newJwt": self.jwt_token,
                "UserId": self.username,
                "SiteCode": self.sitecode,
                "fingerprint": BROWSER_FINGER_ID,
                "device_id": DEVICE_ID,
                "cache": "1",
            }
        )

    def _unwrap(self, resp: requests.Response) -> dict:
        return parse_body(resp, self.cipher)

    def request(self, path: str, payload: dict | None = None, base_url: str | None = None) -> dict:
        body = None
        if payload is not None:
            body = {"encryptString": self.cipher.encrypt(json.dumps(payload))}

        resp = self.session.post(
            (base_url or BASE_URL) + path,
            headers=self.auth_headers(),
            json=body,
            timeout=20,
        )
        return self._unwrap(resp)

    def plain_post(self, path: str, payload: dict) -> dict:
        """Un-encrypted JSON body (payplatformlistV3), par reply encrypted hi aata hai."""
        resp = self.session.post(
            BASE_URL + path,
            headers=self.auth_headers(),
            json=payload,
            timeout=20,
        )
        return self._unwrap(resp)

    # ------------------------------------------------------------- channel listing

    def pay_kinds(self) -> dict:
        res = self.request(
            f"{FINANCE}/payListV4",
            {"noCache": 0, "payCurrency": "", "osType": OS_TYPE},
        )
        if isinstance(res.get("data"), dict):
            self.sign_key = res["data"].get("sign_key") or self.sign_key
        return res

    def pay_channels(self, kind_type: int) -> dict:
        res = self.request(
            f"{FINANCE}/payTypeV4",
            {"type": kind_type, "noCache": 0, "payTabConfig": PAY_TAB_CONFIG},
        )
        if isinstance(res.get("data"), dict):
            self.sign_key = res["data"].get("sign_key") or self.sign_key
        return res

    def platform_channels(self, payplatformid: int, pay_kind: int = PAY_KIND) -> dict:
        return self.plain_post(
            f"{FINANCE}/payplatformlistV3",
            {"payplatformid": payplatformid, "payCurrency": "", "payKind": pay_kind},
        )

    # --------------------------------------------------------------- order submit

    def paysubmit(self, url: str, payload: dict) -> dict:
        """Bundle fn `Cu()` -> GET {url}?{query}."""
        headers = self.auth_headers()
        headers["Web-Version"] = APP_VERSION
        headers["Sign-key"] = str(payload.get("signKey", ""))
        headers["UserId"] = self.username
        headers.pop("content-type", None)

        query = urlencode({k: v for k, v in payload.items() if v not in (None, "")})
        sep = "&" if "?" in url else "?"
        resp = self.session.get(f"{url}{sep}{query}", headers=headers, timeout=20)
        return self._unwrap(resp)

    # --------------------------------------------------------------- order status

    def order_info(self, order_no: str) -> dict:
        """Deposit order ka latest status (payload HAR se confirm).

        Response me `data.status` (1 = "Wait to pay") aur `data.status_name` aata hai —
        isi se pata chalta hai ki deposit credit/paid hua ya nahi."""
        return self.request(
            f"{FINANCE}/orderInfo",
            {"order_no": str(order_no), "lastOrder": None, "time": int(time.time())},
        )


# ============================================================================ sign


def _sign_key_for(sign_key: str, sitecode: str) -> str:
    """Site 503/126/128 hardcoded sign key use karti hain; baaki returned one."""
    return STATIC_SIGN_KEY if sitecode in ("503", "126", "128") else sign_key


def paysubmit_sign(payload: dict, sign_key: str, sitecode: str, is_merge: bool = False) -> str:
    """Bundle fn `$u()`: ordered `key=value` pairs + `&key=<signKey>`, md5'd."""
    order = [
        "username", "amount", "paytype", "payplatformid", "time",
        "platformId", "gameId", "currencyCode", "mobilePhone", "email",
        "cardNumber", "cardPassword", "networkMerch", "bankCode", "bankName",
        "merchCode",
    ]
    parts = []
    for k in order:
        if k == "payplatformid" and is_merge:
            continue
        v = payload.get(k)
        if v in (None, ""):
            continue
        parts.append(f"{k}={v}")
    if is_merge:
        for k in ("payplatformids", "id"):
            v = payload.get(k)
            if v not in (None, ""):
                parts.append(f"{k}={v}")

    return _md5("&".join(parts) + f"&key={_sign_key_for(sign_key, sitecode)}")


# ============================================================================ helpers


def dig_url(obj, _depth=0):
    """Response me kahin bhi pehla http(s) URL dhoondo."""
    if _depth > 6:
        return None
    if isinstance(obj, str):
        return obj if obj.startswith("http") else None
    if isinstance(obj, dict):
        for key in ("urlData", "url", "payUrl", "payAddress", "qrCode"):
            if isinstance(obj.get(key), str) and obj[key].startswith("http"):
                return obj[key]
        for v in obj.values():
            found = dig_url(v, _depth + 1)
            if found:
                return found
    if isinstance(obj, list):
        for v in obj:
            found = dig_url(v, _depth + 1)
            if found:
                return found
    return None


def pay_token(url_or_token) -> str:
    """Payment URL se CQPAY token nikaalo (aakhri path segment).

        https://pay.cqpay.me/pay/24BE8JX37PGD  ->  24BE8JX37PGD

    CQPAY ke get_info endpoints isi token ko `parameter` me lete hain.
    """
    s = str(url_or_token or "").strip().rstrip("/")
    if not s:
        return ""
    if "://" in s:
        s = s.split("#", 1)[0].split("?", 1)[0].rstrip("/")
        return s.rsplit("/", 1)[-1]
    return s


# ------------------------------------------------------------- CQPAY status / UTR
# pay.cqpay.me (payment page) ke 2 endpoints:
#   1) get_info/ieskjkkwq -> order ka current status (payment receive hui ya nahi)
#   2) get_info/refshjsas -> user ka UTR/refNo 3rd-party site par submit + verify
# Dono body form-urlencoded leti hain: parameter={pay_token} (2nd me + refNo).
# Aakhri path segment (ieskjkkwq / refshjsas) frontend ke obfuscated route names hain.
CQPAY_BASE = "https://pay.cqpay.me"
CQPAY_INFO_PATH = "/pay-api/pay/get_info/ieskjkkwq"
CQPAY_REF_PATH = "/pay-api/pay/get_info/refshjsas"


def cqpay_headers(pay_token_value: str = "") -> dict:
    """pay.cqpay.me requests ke headers (curl se match)."""
    ref = f"{CQPAY_BASE}/pay/{pay_token_value}" if pay_token_value else f"{CQPAY_BASE}/"
    return {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.8",
        "Connection": "keep-alive",
        "Origin": CQPAY_BASE,
        "Referer": ref,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Sec-GPC": "1",
        "User-Agent": USER_AGENT,
        "content-type": "application/x-www-form-urlencoded",
        "sec-ch-ua": '"Not:A-Brand";v="8", "Chromium";v="156", "Brave";v="156"',
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": '"iOS"',
    }


def _parse_cqpay(resp: requests.Response) -> dict:
    """pay.cqpay.me reply ko dict me badlo (plain JSON expected; warna raw)."""
    resp.raise_for_status()
    text = (resp.text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return {"raw": text[:500]}


def check_payment(url_or_token, session: requests.Session | None = None) -> dict:
    """Payment receive hui ya nahi, ye pata karo (pay.cqpay.me get_info).

    `url_or_token` = paysubmit ka payment URL ya uska token. Return: gateway ka
    JSON (jisme status/paid jaise fields hote hain).
    """
    token = pay_token(url_or_token)
    if not token:
        raise DepositError("payment token required")
    sess = session or _apply_proxy(requests.Session())
    resp = sess.post(
        CQPAY_BASE + CQPAY_INFO_PATH,
        headers=cqpay_headers(token),
        params={"t": token},
        data={"parameter": token},
        timeout=20,
    )
    data = _parse_cqpay(resp)
    try:
        logging.getLogger(__name__).info("pay2 check_payment(%s) -> %s", token, data)
    except Exception:
        pass
    return data


def submit_utr(url_or_token, ref_no, session: requests.Session | None = None) -> dict:
    """User ka UTR/refNo 3rd-party site par submit + verify karo (refshjsas).

    `ref_no` = user ka UTR / 12-digit reference number. Return: gateway ka JSON.
    """
    token = pay_token(url_or_token)
    if not token:
        raise DepositError("payment token required")
    ref = str(ref_no or "").strip()
    if not ref:
        raise DepositError("refNo/UTR required")
    sess = session or _apply_proxy(requests.Session())
    resp = sess.post(
        CQPAY_BASE + CQPAY_REF_PATH,
        headers=cqpay_headers(token),
        data={"parameter": token, "refNo": ref},
        timeout=20,
    )
    return _parse_cqpay(resp)


_SUCCESS_WORDS = ("success", "paid", "complete", "completed", "finish", "finished",
                  "received", "credited", "recharge success")
_FAIL_WORDS = ("wait", "pending", "unpaid", "fail", "cancel", "close", "closed",
               "expire", "timeout", "not paid", "processing", "review", "refund")


def is_paid(status) -> bool:
    """check_payment/get_info response me se "paid hua ya nahi" nikaalo.

    Site ke JS me status enum hai: wait (default) / success / onlineFailed /
    timeout / cancel / failed. Server `statusName` + `status` dono bhejta hai,
    isliye pehle text signals, phir flags, phir numeric status check hote hain.
    NOTE: false-positive (bina payment premium dena) se bachne ke liye ye
    conservative hai — format alag ho to raw response `check_payment` log karta hai.
    """
    if not isinstance(status, dict):
        return False
    data = status.get("data") if isinstance(status.get("data"), (dict, list)) else status
    if isinstance(data, list):
        data = data[0] if data and isinstance(data[0], dict) else {}
    if not isinstance(data, dict):
        return False

    # 1. text signals (statusName / msg / remark etc.)
    for key in ("status_name", "statusName", "statusText", "stateName",
                "msg", "message", "remark", "statusMsg"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            low = val.lower()
            if any(w in low for w in _FAIL_WORDS):
                return False
            if any(w in low for w in _SUCCESS_WORDS):
                return True

    # 2. boolean / flag fields
    for key in ("paid", "isPaid", "is_paid", "success", "isSuccess"):
        v = data.get(key)
        if isinstance(v, bool):
            if v:
                return True
        elif str(v).lower() in ("true", "1", "yes"):
            return True

    # 3. numeric status fallback: 1 = "Wait to pay" => success aksar 2+
    st = data.get("status")
    if isinstance(st, (int, float)) and not isinstance(st, bool):
        return int(st) >= 2
    return False


_BALANCE_HINTS = ("balance", "money", "wallet", "usermoney", "coin", "score", "credit", "gold")


def _dig_balance(obj, _depth=0):
    """Login response me se balance-jaisa number dhoondo (best-effort).
    Exact key milte hi return; warna nested me recurse."""
    if _depth > 5 or obj is None:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if any(h in kl for h in _BALANCE_HINTS):
                num = _as_float(v)
                if num is not None:
                    return num
        for v in obj.values():
            found = _dig_balance(v, _depth + 1)
            if found is not None:
                return found
    if isinstance(obj, list):
        for v in obj:
            found = _dig_balance(v, _depth + 1)
            if found is not None:
                return found
    return None


def _as_float(value):
    """Number-ish value ko float me badlo. '₹100', '1,000.50', '200 INR' bhi
    chalta hai; bilkul parse na ho to None (crash nahi)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^0-9.]", "", str(value))
    if not cleaned or cleaned.count(".") > 1 or cleaned == ".":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def normalize_amount(amount) -> str:
    """Plan amount ko gateway-friendly number string me badlo.

    Bot ke plan amounts aksar '₹100', '100rs', '1,000.50', 99, 99.0 jaise hote
    hain — inhe '100', '100', '1000.5', '99', '99' bana dete hain. Bilkul
    non-numeric value par ValueError raise karta hai.
    """
    if isinstance(amount, bool) or amount is None:
        raise ValueError("amount is required")
    if isinstance(amount, (int, float)):
        f = float(amount)
    else:
        cleaned = re.sub(r"[^0-9.]", "", str(amount))
        if not cleaned or cleaned.count(".") > 1 or cleaned == ".":
            raise ValueError(f"invalid amount: {amount!r}")
        try:
            f = float(cleaned)
        except ValueError:
            raise ValueError(f"invalid amount: {amount!r}")
    if f <= 0:
        raise ValueError(f"amount must be > 0 (got {amount!r})")
    return str(int(f)) if f.is_integer() else f"{f:g}"


def fits_amount(amount, ch: dict) -> bool:
    amt = _as_float(amount)
    if amt is None:
        return True
    lo = _as_float(ch.get("min_recharge_limit"))
    hi = _as_float(ch.get("max_recharge_limit"))
    if lo is not None and lo > 0 and amt < lo:
        return False
    if hi is not None and hi > 0 and amt > hi:
        return False
    return True


def pick_channel(amount, channels: list) -> dict | None:
    """Pehla channel jiska recharge limit `amount` allow kare."""
    for ch in channels:
        if fits_amount(amount, ch):
            return ch
    return None


def random_rn() -> str:
    """Bundle `Tc()`: 8-digit random number (`rn`)."""
    return str(random.randint(10_000_000, 99_999_999))


def build_payment_payload(s: HallSession, amount, parent: dict, sub: dict) -> dict:
    """Bundle ka `De` object for the paysubmit GET (built inside `bd()`)."""
    open_way = sub.get("openWay") or parent.get("combineOpenWay") or 5
    payload = {
        "username": s.username,
        "amount": str(amount),
        "paytype": parent.get("paymentid") or parent.get("payplatformid"),
        "payplatformid": str(sub.get("payplatformid") or sub.get("id")),
        "time": int(time.time()),
        "language": "en",
        "siteCode": s.sitecode,
        "currencyCode": sub.get("payCurrency") or CURRENCY,
        "source": "web",
        "version": open_way,
        "pnt": "NORMAL",
        "tk": s.session_key,
        "fp": BROWSER_FINGER_ID,
        "xDevice": "3-1",
        "ld": DEVICE_ID,
        "rn": random_rn(),
        "payEntryId": 0,
        "merchCode": sub.get("merchCode", ""),
    }
    payload["sign"] = paysubmit_sign(payload, s.sign_key, s.sitecode)
    payload["signKey"] = s.sign_key[-4:]
    payload["webUrl"] = DOMAIN
    payload["webVersion"] = APP_VERSION
    return payload


# ============================================================================ order


def create_deposit(s: HallSession, amount, kind_type: int = 1,
                   channel_index: int = 0, sub_index: int | None = None) -> dict:
    """Poora deposit flow ek logged-in session par:
    payListV4 -> payTypeV4 -> payplatformlistV3 -> GET paysubmit."""
    kinds = s.pay_kinds()
    kind_list = (kinds.get("data") or {}).get("list") or []
    if kinds.get("code") != 1 or not kind_list:
        raise DepositError("payListV4 failed", kinds)

    if kind_type not in [k["type"] for k in kind_list]:
        kind_type = kind_list[0]["type"]

    res = s.pay_channels(kind_type)
    channels = ((res.get("data") or {}).get("payKind") or {}).get("list") or []
    if not channels:
        raise DepositError("no pay channels returned", res)
    parent = channels[channel_index] if channel_index < len(channels) else channels[0]

    plat = s.platform_channels(parent.get("payplatformid"))
    pdata = plat.get("data") or {}
    pay_url, subs = pdata.get("url"), pdata.get("list") or []
    if plat.get("code") != 1 or not pay_url or not subs:
        raise DepositError("no paysubmit url / sub-channels", plat)

    if sub_index is not None and sub_index < len(subs):
        sub = subs[sub_index]
    else:
        sub = pick_channel(amount, subs) or subs[0]

    resp = s.paysubmit(pay_url, build_payment_payload(s, amount, pdata, sub))
    inner = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    url = inner.get("url") or inner.get("payAddress") or dig_url(resp)
    if not url:
        raise DepositError(inner.get("msg") or "paysubmit did not return a URL", resp)

    return {
        "url": url,
        "orderNo": inner.get("orderNo") or "",
        "amount": str(amount),
        "channel": parent.get("payname") or "",
        "sub_channel": sub.get("merch_desc") or "",
        "account": s.username,
    }


# ============================================================================ multi-account


class AccountState:
    """Ek account ka runtime state (session cache + failure tracking)."""

    def __init__(self, phone: str, password: str):
        self.phone = phone
        self.password = password
        self.session: HallSession | None = None
        self.balance = None  # login ke response se (best-effort)
        self.busy = False
        self.failures = 0
        self.orders = 0
        self.cooldown_until = 0.0

    def __repr__(self) -> str:
        return (f"<Account {self.phone} orders={self.orders} "
                f"failures={self.failures} cooling={self.cooldown_until > time.time()}>")


class AccountManager:
    """Multi-account payment gateway: round-robin rotation + fail par auto-switch.

    accounts.json:
        {"accounts": [{"phone": "+91-XXXXXXXXXX", "password": "secret", "enabled": true}]}

    Bot `accounts` (list) directly de sakta hai — us case me wahi use hote hain
    (accounts.json ignore ho jaati hai). Thread-safe (Telegram bot ke liye).
    """

    def __init__(self, config_path: str | Path = ACCOUNTS_FILE,
                 cooldown: int = ORDER_COOLDOWN, max_attempts: int | None = None,
                 accounts: list | None = None):
        self.config_path = Path(config_path)
        self.cooldown = cooldown
        self.max_attempts = max_attempts
        # Bot se seedha diye gaye accounts (Telegram admin commands) file se priority lete hain.
        self._accounts_override: list = list(accounts) if accounts else []
        self._lock = threading.Lock()
        self._states: list[AccountState] = []
        self._cursor = 0
        self.reload()

    # ------------------------------------------------------------------ config

    def _load_raw(self) -> list:
        """Accounts: bot override (agar diye gaye) → warna accounts.json file.
        Missing/corrupt file par khaali list (crash nahi hota)."""
        if self._accounts_override:
            return self._accounts_override
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return data.get("accounts") if isinstance(data, dict) else data

    def reload(self) -> int:
        """accounts dobara padho (sessions reuse hote hain)."""
        raw = self._load_raw()

        known = {(st.phone, st.password): st for st in self._states}
        states = []
        for acc in raw or []:
            if not isinstance(acc, dict):
                continue
            if acc.get("enabled", True) is False:
                continue
            phone, password = acc.get("phone"), acc.get("password")
            if not phone or not password:
                continue
            key = (phone, password)
            states.append(known.get(key) or AccountState(phone, password))

        with self._lock:
            self._states = states
            self._cursor = 0
        return len(states)

    def active_count(self) -> int:
        """Abhi kitne accounts active hain (cooldown ke regardless)."""
        with self._lock:
            return len(self._states)

    def stats(self) -> list[dict]:
        with self._lock:
            return [
                {"phone": st.phone, "orders": st.orders, "failures": st.failures,
                 "cooldown": max(0, round(st.cooldown_until - time.time())), "busy": st.busy}
                for st in self._states
            ]

    # --------------------------------------------------------------- scheduling

    def _pick_locked(self, skip: set) -> AccountState | None:
        """Round-robin se next account chuno. Lock ke andar hi call karo."""
        n = len(self._states)
        if not n:
            return None
        now = time.time()
        order = [(self._cursor + i) % n for i in range(n)]
        candidates = [i for i in order if self._states[i] not in skip]
        if not candidates:
            return None
        ready = [i for i in candidates
                 if not self._states[i].busy and self._states[i].cooldown_until <= now]
        idx = (ready or candidates)[0]
        self._cursor = (idx + 1) % n
        return self._states[idx]

    def _session_for(self, st: AccountState) -> HallSession:
        if st.session is not None:
            return st.session
        login_data = HallClient().login(st.phone, st.password)
        if login_data.get("code") != 1:
            raise DepositError(f"login failed: {login_data.get('msg')}")
        data = login_data.get("data") or login_data
        st.balance = _dig_balance(data)
        st.session = HallSession(data)
        return st.session

    def balances(self, force: bool = False) -> list[dict]:
        """Har account ka balance login se nikaalo. Fail par balance=None."""
        out = []
        for st in list(self._states):
            if force or st.session is None:
                try:
                    with self._lock:
                        st.session = None  # fresh login (balance update ke liye)
                    st.balance = None
                    self._session_for(st)
                except Exception as e:
                    out.append({"phone": st.phone, "balance": None,
                                "error": type(e).__name__})
                    continue
            out.append({"phone": st.phone, "balance": st.balance})
        return out

    # ------------------------------------------------------------------- public

    def get_payment_url(self, amount, kind_type: int = 1, channel_index: int = 0,
                        sub_index: int | None = None) -> dict:
        """Kisi bhi free account se deposit order banao aur payment URL return karo.
        Sab accounts fail hone par DepositError raise hota hai."""
        amount = normalize_amount(amount)

        limit = self.max_attempts or len(self._states)
        tried: set = set()
        errors: list[str] = []

        while len(tried) < limit:
            with self._lock:
                st = self._pick_locked(tried)
                if st is None:
                    break
                st.busy = True
            tried.add(st)

            try:
                result = create_deposit(self._session_for(st), amount,
                                        kind_type, channel_index, sub_index)
                with self._lock:
                    st.failures = 0
                    st.orders += 1
                result["account"] = st.phone
                return result
            except (DepositError, requests.RequestException, KeyError,
                    ValueError, json.JSONDecodeError) as exc:
                with self._lock:
                    st.failures += 1
                    st.cooldown_until = time.time() + self.cooldown
                    st.session = None  # agli baar fresh login
                errors.append(f"{st.phone}: {exc}")
            finally:
                with self._lock:
                    st.busy = False

        if errors:
            raise DepositError("all accounts failed: " + "; ".join(errors))
        raise DepositError(f"no accounts configured in {self.config_path}")


_manager: AccountManager | None = None
_manager_lock = threading.Lock()


def configure_accounts(accounts, config_path: str | Path = ACCOUNTS_FILE,
                       cooldown: int = ORDER_COOLDOWN) -> int:
    """Bot admin jab bhi accounts change kare, ye call karo.

    `accounts` = [{"phone": "...", "password": "..."}] — khaali list dene par
    manager accounts.json file fallback use karega. Active account count return karta hai.
    """
    global _manager
    with _manager_lock:
        _manager = AccountManager(config_path=config_path, cooldown=cooldown,
                                  accounts=accounts)
        return _manager.active_count()


def get_payment_url(amount, config_path: str | Path = ACCOUNTS_FILE, **kwargs) -> dict:
    """Bot ke liye shortcut: ek shared AccountManager lazily bana kar use karta hai."""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = AccountManager(config_path)
    return _manager.get_payment_url(amount, **kwargs)


def get_balances(config_path: str | Path = ACCOUNTS_FILE, fresh: bool = True) -> list[dict]:
    """Sabhi accounts ka balance: [{"phone": "...", "balance": 1234.5 | None}, ...]."""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = AccountManager(config_path)
    return _manager.balances(force=fresh)


# ============================================================================ cli


def run(phone: str, password: str, amount: str, kind_type: int = 1, channel_index: int = 0):
    login_data = HallClient().login(phone, password)
    if login_data.get("code") != 1:
        print("Login failed:", json.dumps(login_data, ensure_ascii=False))
        return

    s = HallSession(login_data.get("data") or login_data)
    print(f"[+] Logged in as {s.username}")

    try:
        result = create_deposit(s, amount, kind_type, channel_index)
    except DepositError as exc:
        print("[-] Failed:", exc)
        if exc.response is not None:
            print(json.dumps(exc.response, indent=2, ensure_ascii=False)[:1200])
        return

    print(f"[+] sign_key = {s.sign_key}")
    print(f"[+] account = {result['account']}, channel = {result['channel']}, "
          f"sub = {result['sub_channel']}, amount = {result['amount']}")
    print(f"[+] orderNo = {result['orderNo']}")
    print("\n=== PAYMENT URL ===")
    print(result["url"])
    return result["url"]


if __name__ == "__main__":
    import sys

    phone = sys.argv[1] if len(sys.argv) > 1 else input("Phone (+91-XXXXXXXXXX)? ")
    password = sys.argv[2] if len(sys.argv) > 2 else input("Password? ")

    amount = sys.argv[3] if len(sys.argv) > 3 else ""
    while True:
        try:
            amount = normalize_amount(amount)
            break
        except ValueError:
            amount = input("Kitne ka deposit karna hai? (jaise 500 ya 199.50) ").strip()

    run(phone, password, amount)
