#!/usr/bin/env bash
# Traduit les PDFs de "PDFs Originaux" vers "PDFs Traduits" (FR, mono only).
# Prérequis : proxy Hermes actif ->  hermes proxy start --provider nous --port 8645
set -euo pipefail

ROOT="$(cygpath -m "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)")"
V2="$ROOT/pdf2zh/kernel/PDFMathTranslate-next.git/.venv/Scripts/pdf2zh_next.exe"

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
GLOSSARY="${PDF2ZH_GLOSSARY:-$ROOT/Travail/analyse/glossaire.csv}"

curl -sf "$PROXY/models" -H "Authorization: Bearer hermes" >/dev/null \
  || { echo "ERREUR: proxy Hermes injoignable sur $PROXY"; echo "Lance: hermes proxy start --provider nous --port 8645"; exit 1; }

FILES=("$@")
if [ ${#FILES[@]} -eq 0 ]; then
  shopt -s nullglob
  FILES=("$ROOT/PDFs Originaux"/*.pdf)
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
    --output "$ROOT/PDFs Traduits" \
    --qps "$QPS" \
    --min-text-length 2
done

echo "=== TERMINE ==="
