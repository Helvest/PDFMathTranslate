#!/usr/bin/env bash
# Traduit les PDFs de "PDFs Originaux" vers "PDFs Traduits" (FR, mono only).
# Prérequis : proxy Hermes actif ->  hermes proxy start --provider nous --port 8645
set -euo pipefail

ROOT="$(cygpath -m "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)")"
# Les donnees vivent HORS du depot, un dossier par projet :
#   ../Projets/<nom>/{source,traduits,downloads,analyse}
# Le projet se choisit par PDF2ZH_PROJET, sinon le premier trouve.
PROJETS="$(cygpath -m "$(cd "$ROOT/.." && pwd)")/Projets"
if [ -n "${PDF2ZH_WORK:-}" ]; then
  WORK="$PDF2ZH_WORK"                      # surcharge explicite (compatibilite)
elif [ -n "${PDF2ZH_PROJET:-}" ]; then
  WORK="$PROJETS/$PDF2ZH_PROJET"
else
  WORK="$(ls -d "$PROJETS"/*/ 2>/dev/null | head -1)"
  WORK="${WORK%/}"
fi
[ -d "$WORK" ] || { echo "ERREUR: projet introuvable ($WORK)"; echo "Projets disponibles :"; ls "$PROJETS" 2>/dev/null; exit 1; }
V2="$ROOT/pdf2zh/kernel/PDFMathTranslate-next.git/.venv/Scripts/pdf2zh_next.exe"

# Polices custom : sitecustomize.py est importe automatiquement par Python si
# son dossier est dans PYTHONPATH. Il charge <projet>/analyse/polices.csv et
# patche le FontMapper de BabelDOC. PDF2ZH_NO_FONTS=1 pour desactiver.
export PYTHONPATH="$ROOT${PYTHONPATH:+;$PYTHONPATH}"

MODEL="${PDF2ZH_MODEL:-inclusionai/ling-3.0-flash-sante:free}"
# Extraction de glossaire : longcat-2.5, seul modele gratuit qui supporte
# response_format=json_object (ling-sante renvoie HTTP 400). L'extracteur
# demande request_json_mode=True (automatic_term_extractor.py:331).
TERM_MODEL="${PDF2ZH_TERM_MODEL:-meituan/longcat-2.5-preview:free}"
PROXY="${PDF2ZH_PROXY:-http://127.0.0.1:8645/v1}"
LANG_IN="${PDF2ZH_LANG_IN:-en}"
LANG_OUT="${PDF2ZH_LANG_OUT:-fr}"
QPS="${PDF2ZH_QPS:-5}"
TERM_QPS="${PDF2ZH_TERM_QPS:-5}"
# Parallelisme interne : 5 workers = optimum mesure (2,9x). Le pool de termes
# reste a 1 pour ne pas declencher de 429 sur l'extraction.
POOL="${PDF2ZH_POOL:-5}"
TERM_POOL="${PDF2ZH_TERM_POOL:-1}"
GLOSSARY="${PDF2ZH_GLOSSARY:-$WORK/analyse/glossaire.csv}"

if ! curl -sf "$PROXY/models" -H "Authorization: Bearer hermes" >/dev/null; then
  echo "AVERTISSEMENT: proxy Hermes injoignable sur $PROXY" >&2
  echo "  la traduction va echouer. Lance: hermes proxy start --provider nous --port 8645" >&2
fi

FILES=("$@")
if [ ${#FILES[@]} -eq 0 ]; then
  shopt -s nullglob
  FILES=("$WORK/source"/*.pdf)
fi

for f in "${FILES[@]}"; do
  echo "=== $(basename "$f") ==="
  "$V2" "$f" \
    --openai \
    --openai-base-url "$PROXY" \
    --openai-api-key hermes \
    --openai-model "$MODEL" \
    --openai-reasoning-effort none \
    --openai-send-reasoning-effort \
    --lang-in "$LANG_IN" \
    --lang-out "$LANG_OUT" \
    --no-dual \
    --no-auto-extract-glossary \
    --glossaries "$GLOSSARY" \
    --watermark-output-mode no_watermark \
    --auto-enable-ocr-workaround \
    --output "$WORK/traduits" \
    --qps "$QPS" \
    --pool-max-workers "$POOL" \
    --term-pool-max-workers "$TERM_POOL" \
    --min-text-length 2
done

echo "=== TERMINE ==="
