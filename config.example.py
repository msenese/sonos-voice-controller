HA_URL = "http://10.0.0.240:8123"
HA_TOKEN = "your-token-here"
EI_WS_URL = "ws://localhost:4912"
SONOS_ENTITY = "media_player.office_1"

THRESHOLD = 0.92
SONOS_PLAY_THRESHOLD = 0.85
COOLDOWN = 3.0
CONSECUTIVE_REQUIRED = 2

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
