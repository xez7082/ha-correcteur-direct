"""Constantes du Correcteur en direct."""
from __future__ import annotations

DOMAIN = "correcteur_direct"
NOM = "Correcteur en direct"
VERSION = "1.2.1"

# Options
CONF_API_KEY = "api_key"
CONF_MODEL = "modele"
CONF_LEVEL = "niveau"
CONF_AUTO_SERVICES = "auto_services"
CONF_AUTO_FILES = "auto_fichiers"
CONF_NOTIFY = "service_notification"
CONF_MAX_CALLS = "appels_max_heure"
CONF_VERIFY_DELAY = "delai_verification"
CONF_COOLDOWN = "delai_reanalyse_h"
CONF_MIN_CONFIDENCE = "confiance_min"

MODELES = [
    "mistral-small-latest",
    "mistral-medium-latest",
    "mistral-large-latest",
]

DEFAUTS = {
    CONF_MODEL: "mistral-small-latest",
    CONF_LEVEL: "ERROR",
    CONF_AUTO_SERVICES: True,
    CONF_AUTO_FILES: False,
    CONF_NOTIFY: "",
    CONF_MAX_CALLS: 20,
    CONF_VERIFY_DELAY: 120,
    CONF_COOLDOWN: 6,
    CONF_MIN_CONFIDENCE: 75,
}

MISTRAL_CHAT_URL = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_MODELS_URL = "https://api.mistral.ai/v1/models"

# Services Home Assistant que l'IA a le droit de proposer.
# Aucun d'eux ne détruit de données : ce sont des rechargements.
SERVICES_SURS = {
    "homeassistant.reload_all",
    "homeassistant.reload_core_config",
    "homeassistant.reload_config_entry",
    "homeassistant.reload_custom_templates",
    "homeassistant.update_entity",
    "frontend.reload_themes",
    "automation.reload",
    "script.reload",
    "scene.reload",
    "template.reload",
    "group.reload",
    "input_boolean.reload",
    "input_number.reload",
    "input_select.reload",
    "input_text.reload",
    "input_datetime.reload",
    "input_button.reload",
    "timer.reload",
    "counter.reload",
    "schedule.reload",
    "zone.reload",
    "person.reload",
    "rest.reload",
    "command_line.reload",
    "mqtt.reload",
    "filter.reload",
    "min_max.reload",
    "statistics.reload",
    "trend.reload",
    "universal.reload",
    "generic_thermostat.reload",
    "bayesian.reload",
}

TYPES_CORRECTIF = (
    "aucun",
    "recharger_integration",
    "appel_service",
    "modif_fichier",
    "redemarrage",
)

# Dossiers de /config jamais lus ni modifiés
DOSSIERS_EXCLUS = {
    ".storage",
    ".cloud",
    ".git",
    "custom_components",
    "deps",
    "tts",
    "www",
    "backups",
    "media",
    "image",
    "node_modules",
    "correcteur_direct_sauvegardes",
}
FICHIERS_EXCLUS = {"secrets.yaml", "known_devices.yaml"}
DOSSIER_SAUVEGARDES = "correcteur_direct_sauvegardes"
SAUVEGARDES_MAX = 50

# Statuts d'un problème
ST_FILE = "en_file"
ST_ANALYSE = "analyse"
ST_PROPOSE = "proposé"
ST_APPLICATION = "application"
ST_VERIF = "vérification"
ST_CORRIGE = "corrigé"
ST_ECHEC = "échec"
ST_SANS = "sans_correctif"
ST_ERREUR_IA = "erreur_ia"
ST_QUOTA = "quota"
ST_IGNORE = "ignoré"
ST_RECIDIVE = "récidive"
ST_ANNULE = "annulé"

STATUTS_OUVERTS = {
    ST_FILE, ST_ANALYSE, ST_PROPOSE, ST_APPLICATION, ST_VERIF,
    ST_ECHEC, ST_QUOTA, ST_RECIDIVE,
}

PROBLEMES_MAX = 200

SIGNAL_MAJ = f"{DOMAIN}_maj"
PANEL_URL = "correcteur-direct"
STATIC_URL = "/correcteur_direct_static"
