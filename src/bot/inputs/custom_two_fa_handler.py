import os
import time
import hmac
import base64
import struct
import hashlib
import logging
from ibeam.src.two_fa_handlers.two_fa_handler import TwoFaHandler

logger = logging.getLogger(__name__)

TOTP_PERIOD = 30
EXPIRY_BUFFER_SECONDS = 10


def _generate_totp(key: bytes, timestamp: int) -> str:
    """Pure Python TOTP generation (RFC 6238)."""
    msg = struct.pack(">Q", timestamp // TOTP_PERIOD)
    mac = hmac.new(key, msg, hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    binary = struct.unpack('>I', mac[offset:offset + 4])[0] & 0x7FFFFFFF
    code = binary % 1_000_000
    return f"{code:06d}"


class CustomTwoFaHandler(TwoFaHandler):
    def get_two_fa_code(self) -> str:
        secret = os.environ.get('IBKR_TOTP_SECRET')
        if not secret:
            raise ValueError("IBKR_TOTP_SECRET environment variable is missing")

        secret = secret.replace(" ", "").upper()
        key = base64.b32decode(secret, casefold=True)

        now = time.time()
        remaining = TOTP_PERIOD - (now % TOTP_PERIOD)

        if remaining < EXPIRY_BUFFER_SECONDS:
            logger.info(
                f"TOTP code expires in {remaining:.1f}s "
                f"(< {EXPIRY_BUFFER_SECONDS}s buffer), waiting for fresh code..."
            )
            time.sleep(remaining + 0.5)

        now = int(time.time())
        code = _generate_totp(key, now)
        new_remaining = TOTP_PERIOD - (time.time() % TOTP_PERIOD)
        logger.info(f"Generated TOTP code (valid for {new_remaining:.0f}s)")
        return code

    def __str__(self):
        return "CustomTwoFaHandler(Pure Python TOTP with expiry buffer)"
