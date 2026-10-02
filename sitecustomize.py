"""Charge automatiquement les polices custom au demarrage de Python.

Python importe `sitecustomize` au demarrage de chaque interpreteur, si le
module est trouvable. En le placant dans le dossier du projet et en ajoutant
ce dossier au PYTHONPATH, le patch des polices s'applique partout : que l'on
lance pdf2zh_next.exe, un script, ou un test.

Pour desactiver : supprimer ce fichier, ou definir PDF2ZH_NO_FONTS=1.
"""
import os
import sys
from pathlib import Path

# --- garde-fous -----------------------------------------------------------
_actif = not os.environ.get("PDF2ZH_NO_FONTS")

if _actif:
    # Le dossier du projet doit etre importable (polices.py, patch_polices.py).
    _repo = Path(__file__).resolve().parent
    if str(_repo) not in sys.path:
        sys.path.insert(0, str(_repo))

    try:
        import polices as _polices
        import patch_polices as _patch

        _chargees = _polices.charger()
        if _chargees:
            _patch.appliquer(_chargees)
    except Exception as _e:  # noqa: BLE001
        # Ne JAMAIS empecher le demarrage de Python a cause des polices.
        import logging

        logging.getLogger(__name__).warning(
            "polices custom non chargees (%s: %s) - on continue sans",
            type(_e).__name__,
            _e,
        )
