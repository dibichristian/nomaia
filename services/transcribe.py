"""Transcription locale des messages vocaux (faster-whisper).

Rien ne sort du poste : l'audio est decode et transcrit sur le CPU, puis
efface. Le modele est telecharge une seule fois dans DATA_DIR au premier
vocal, jamais a l'installation : un utilisateur qui n'envoie aucun vocal ne
paie ni le disque ni la memoire.

Le modele pese plusieurs centaines de megaoctets en memoire. Il est charge a
la demande et libere apres une periode d'inactivite : le garder en permanence
multiplierait l'empreinte du service seul.
"""
import gc
import logging
import os
import threading

import config
from models import setting

logger = logging.getLogger("nomaia.transcribe")

# Modeles proposes : au-dela de 'small', le CPU met plus longtemps a
# transcrire que la duree du vocal lui-meme.
VALID_MODELS = ("tiny", "base", "small")

# Inactivite apres laquelle le modele est libere.
IDLE_UNLOAD_S = 600

_lock = threading.Lock()
_model = None
_model_name = None
_unload_timer = None


class TranscriptionError(RuntimeError):
    """Transcription impossible : moteur absent, audio illisible ou vide."""


def is_available():
    """Indique si faster-whisper est installe sur ce poste."""
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def _model_dir():
    """Dossier de telechargement des modeles, hors du dossier d'installation."""
    folder = config.DATA_DIR / "models" / "whisper"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _load(name):
    """Retourne le modele demande, en le chargeant au besoin (verrou tenu)."""
    global _model, _model_name

    if _model is not None and _model_name == name:
        return _model

    # Aucune telemetrie vers Hugging Face, et pas d'avertissement de liens
    # symboliques a chaque chargement : Windows ne les autorise pas sans
    # mode developpeur, le cache fonctionne sans.
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise TranscriptionError(
            "Transcription indisponible : faster-whisper n'est pas installe."
        )

    logger.info("Chargement du modele de transcription '%s'.", name)
    try:
        # int8 : quatre fois moins de memoire que float32, perte de qualite
        # negligeable en francais sur ces tailles de modele.
        _model = WhisperModel(
            name, device="cpu", compute_type="int8", download_root=str(_model_dir())
        )
    except Exception as error:
        _model = None
        _model_name = None
        raise TranscriptionError(f"Modele de transcription non charge : {error}")

    _model_name = name
    return _model


def _schedule_unload():
    """Relance le compte a rebours de liberation du modele (verrou tenu)."""
    global _unload_timer

    if _unload_timer:
        _unload_timer.cancel()
    _unload_timer = threading.Timer(IDLE_UNLOAD_S, unload)
    _unload_timer.daemon = True
    _unload_timer.start()


def unload():
    """Libere le modele en memoire. Sans effet s'il n'est pas charge."""
    global _model, _model_name

    with _lock:
        if _model is None:
            return
        _model = None
        _model_name = None
    gc.collect()
    logger.info("Modele de transcription libere apres inactivite.")


def transcribe(audio_path):
    """Transcrit un fichier audio en texte.

    Le format d'origine de Telegram (OGG/Opus) est decode directement : aucune
    conversion prealable, aucun ffmpeg a installer.

    :param audio_path: chemin du fichier audio
    :return: texte transcrit, jamais vide
    :raises TranscriptionError: moteur absent, audio illisible ou sans parole
    """
    name = setting.get("voice_model")
    if name not in VALID_MODELS:
        name = "small"
    language = (setting.get("voice_language") or "").strip() or None

    with _lock:
        model = _load(name)
        try:
            # Le filtre de silence evite les phrases inventees sur les blancs,
            # defaut connu de Whisper sur les vocaux pris dans la rue.
            segments, _info = model.transcribe(
                str(audio_path), language=language, vad_filter=True, beam_size=5
            )
            texte = " ".join(segment.text.strip() for segment in segments).strip()
        except Exception as error:
            raise TranscriptionError(f"Audio illisible : {error}")
        finally:
            _schedule_unload()

    if not texte:
        raise TranscriptionError("Aucune parole reconnue dans ce vocal.")
    return texte
