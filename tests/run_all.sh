#!/bin/zsh
# Полный прогон: макет на scale 0.5, 0.75, 1 и 2, режим длинных страниц и (по желанию) реальный сайт.
# Запуск: tests/run_all.sh [адрес реального сайта]
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
OUT="$HERE/out"
PORT=8765
TEST_VENV="${WWW2PDF_TEST_VENV:-$HOME/.www2pdf-tests}"
rm -rf "$OUT"; mkdir -p "$OUT"

[ -x "$TEST_VENV/bin/python" ] || python3 -m venv "$TEST_VENV"
"$TEST_VENV/bin/python" -m pip install --quiet --disable-pip-version-check pdfplumber pypdf beautifulsoup4 || exit 1
[ "$(uname)" = "Darwin" ] && "$TEST_VENV/bin/python" -m pip install --quiet --disable-pip-version-check pyobjc-framework-Quartz
PY="$TEST_VENV/bin/python"

"$PY" "$HERE/make_mock.py" "$OUT/site" || exit 1
"$PY" -m http.server "$PORT" --bind 127.0.0.1 --directory "$OUT/site" >/dev/null 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null' EXIT
sleep 1

FAILED=0
URL="http://127.0.0.1:$PORT/"
for SCALE in 0.5 0.75 1 2; do
  echo "=== Макет, scale $SCALE ==="
  python3 "$ROOT/www2pdf.py" "$URL" -o "$OUT/mock_$SCALE.pdf" --scale "$SCALE" 2>/dev/null | head -6
  [ "${pipestatus[1]}" = "0" ] || FAILED=1
  "$PY" "$HERE/verify.py" "$OUT/mock_$SCALE.pdf" "$URL" --markers "$OUT/site/markers.txt" --json "$OUT/mock_$SCALE.json" | tail -22
  [ "${pipestatus[1]}" = "0" ] || FAILED=1
done

echo "=== Макет, длинные страницы ==="
python3 "$ROOT/www2pdf.py" "$URL" -o "$OUT/mock_long.pdf" --long-pages 2>/dev/null | head -2
[ "${pipestatus[1]}" = "0" ] || FAILED=1
"$PY" "$HERE/verify.py" "$OUT/mock_long.pdf" "$URL" --markers "$OUT/site/markers.txt" | tail -3
[ "${pipestatus[1]}" = "0" ] || FAILED=1

echo "=== Макет, Letter с полями 12 мм ==="
python3 "$ROOT/www2pdf.py" "$URL" -o "$OUT/mock_letter.pdf" --paper Letter --margin 12 2>/dev/null | head -2
[ "${pipestatus[1]}" = "0" ] || FAILED=1
"$PY" "$HERE/verify.py" "$OUT/mock_letter.pdf" "$URL" --markers "$OUT/site/markers.txt" | tail -3
[ "${pipestatus[1]}" = "0" ] || FAILED=1

echo "=== Верификатор ловит испорченные PDF ==="
"$PY" "$HERE/test_mutations.py" "$OUT/mock_0.75.pdf" "$URL" "$OUT/site" || FAILED=1

echo "=== Каверзные сайты ==="
"$PY" "$HERE/test_edges.py" || FAILED=1

if [ $# -ge 1 ]; then
  echo "=== Реальный сайт: $1 ==="
  python3 "$ROOT/www2pdf.py" "$1" -o "$OUT/real.pdf" --report "$OUT/real_tool.json" 2>/dev/null | grep -v 'нет цели'
  [ "${pipestatus[1]}" = "0" ] || FAILED=1
  "$PY" "$HERE/verify.py" "$OUT/real.pdf" "$1" --json "$OUT/real.json"
  [ $? = 0 ] || FAILED=1
  if [ "$(uname)" = "Darwin" ]; then
    echo "=== PDFKit (движок Preview) ==="
    "$PY" "$HERE/pdfkit_check.py" "$OUT/real.pdf" || FAILED=1
  fi
fi

if [ "$FAILED" = "0" ]; then echo "ИТОГ: все проверки прошли"; else echo "ИТОГ: ЕСТЬ ОШИБКИ"; fi
exit $FAILED
