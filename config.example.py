HA_URL = "http://192.168.50.212:8123"
HA_TOKEN = "your-token-here"
EI_WS_URL = "ws://localhost:4912"
# Confirm this against Home Assistant's actual current state, not just
# what the entity_id implies -- entity_ids don't reliably track a Sonos
# room rename or a speaker move in real time.
# (confirmed live 2026-08-11: the office speakers got moved upstairs and
# paired into media_player.bedroom_tv as satellites, and a previously
# "Nightstand"-named pair got moved into the office -- but stayed
# media_player.nightstand in HA, friendly_name and all, despite no longer
# describing where the speaker physically is)
# (confirmed live 2026-08-14, ~3 days later: media_player.nightstand
# disappeared outright -- 404 on a direct state lookup -- and was replaced
# by a fresh media_player.office, presumably the Sonos app's room rename
# finally propagating into HA's entity naming. Silent failure mode: HA
# service calls against a nonexistent entity_id still return HTTP success
# with zero matched entities, so sonos-controller.py kept logging
# "[HA] Paused Sonos" etc. with no error for days after the entity behind
# SONOS_ENTITY had already stopped existing -- worth an occasional sanity
# check against reality, not just watching the logs for errors).
SONOS_ENTITY = "media_player.office"

THRESHOLD = 0.92
SONOS_PLAY_THRESHOLD = 0.85
SONOS_MUTE_THRESHOLD = 0.9
COOLDOWN = 3.0
# The model's Performance Calibration emits one isolated high-confidence
# window per utterance, not a sustained run -- requiring more than 1 here
# means real detections get silently dropped waiting for a second window
# that never comes. Confirmed live: 1 is reliable, 2 misses real commands.
CONSECUTIVE_REQUIRED = 1

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
