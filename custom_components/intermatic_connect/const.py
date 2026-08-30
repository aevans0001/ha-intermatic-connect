"""Constants for Intermatic Connect."""

DOMAIN = "intermatic_connect"
PLATFORMS = ["switch", "binary_sensor", "calendar"]

CONF_REFRESH_TOKEN = "refresh_token"
CONF_REFRESH_USERNAME = "refresh_username"

API_BASE = "https://mobile.api.intermatic.io"
USER_POOL_ID = "us-east-1_NmoAaiX9i"
CLIENT_ID = "6jtaa4lk35c5d07qko5b2b8jjg"
CLIENT_SECRET = "6vm2n1evm0g4k2kvo6k46742atle9n2epkc6s58m5761b7pna9q"

OUTPUT_COMBINED = 0x40
RELAY_ON = 2404
RELAY_OFF = 2304
DELETE_EVENT = "255,255,255,255,255,255,255,255,255,255,255,255,255,255"

SCAN_INTERVAL_SECONDS = 30
