HA_URL = "http://192.168.50.212:8123"
HA_TOKEN = "your-token-here"
EI_WS_URL = "ws://localhost:4912"
SONOS_ENTITY = "media_player.office_1"

THRESHOLD = 0.92
SONOS_PLAY_THRESHOLD = 0.85
SONOS_MUTE_THRESHOLD = 0.9
COOLDOWN = 3.0
CONSECUTIVE_REQUIRED = 2

SONOS_PAUSE_ENABLED = True
SONOS_PLAY_ENABLED = True
SONOS_MUTE_ENABLED = True

# Only needed for the dashboard's training-mode upload button.
# Get one from the Edge Impulse project: Dashboard > Keys > Add API key.
EI_API_KEY = "your-edge-impulse-api-key-here"

# Your Edge Impulse project ID (visible in the Studio URL: studio.edgeimpulse.com/studio/<id>).
# Not a secret, but needed for the retrain/build/deploy dashboard feature.
EI_PROJECT_ID = "your-project-id-here"

# A second key with the Admin role, used ONLY for triggering the retrain job
# (Edge Impulse requires Admin for /jobs/retrain; Ingestion + deployment covers
# everything else the dashboard does). Keeping it separate from EI_API_KEY
# limits which action can use the broader-privilege key.
EI_ADMIN_API_KEY = "your-edge-impulse-admin-api-key-here"

# Temporary stand-in for a not-yet-trained wake word: while VOICE_ASSISTANT_SECRET
# is left as the placeholder below, "sonos mute" triggers keep their existing
# mute-toggle behavior only. Once set, "sonos mute" ALSO captures a few seconds
# of follow-up audio (Buffer mode only -- see audio-buffer.py's /capture-forward)
# and relays it to an external voice-assistant HTTP endpoint on the LAN.
VOICE_ASSISTANT_URL = "http://192.168.50.212:8420/voice"
VOICE_ASSISTANT_SECRET = "your-voice-assistant-shared-secret-here"
