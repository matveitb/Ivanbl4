#!/usr/bin/env bash
# Полный прогон анализа. Положите прошивки в firmware/ и запустите:
#
#   ./analyze.sh firmware/stock.bin firmware/csok1.bin firmware/csok2.bin
#
# Результаты появятся в out/.

set -eu
cd "$(dirname "$0")"
export PYTHONPATH="$PWD/tools:${PYTHONPATH:-}"
mkdir -p out

STOCK="${1:-}"
CSOK1="${2:-}"
CSOK2="${3:-}"

if [ -z "$STOCK" ]; then
  echo "Использование: $0 <сток.bin> [csok1.bin] [csok2.bin]" >&2
  exit 2
fi

echo "############ 1. ОБЗОР ОБРАЗОВ ############"
for f in "$STOCK" "$CSOK1" "$CSOK2"; do
  [ -n "$f" ] || continue
  echo
  out_txt="out/$(basename "${f%.*}")_info.txt"
  python3 tools/fwinfo.py "$f" > "$out_txt"
  echo "  -> $out_txt"
  sed -n '1,25p' "$out_txt"
done

if [ -n "$CSOK1" ] && [ -n "$CSOK2" ]; then
  echo
  echo "############ 2. ДИФФ csok1 vs csok2 (карты зажигания) ############"
  python3 tools/fwdiff.py "$CSOK1" "$CSOK2" --identify \
      --out out/diff_csok1_csok2.json | tee out/diff_csok1_csok2.txt
fi

if [ -n "$CSOK1" ]; then
  echo
  echo "############ 3. ДИФФ сток vs csok1 (всё, что правил калибровщик) ############"
  python3 tools/fwdiff.py "$STOCK" "$CSOK1" --identify \
      --out out/diff_stock_csok1.json | tee out/diff_stock_csok1.txt
fi

echo
echo "############ 4. ПОЛНОЕ СКАНИРОВАНИЕ КАРТ (сток) ############"
python3 tools/mapscan.py "$STOCK" --out out/maps_stock.json --top 60 \
    | tee out/maps_stock.txt

echo
echo "############ 5. ЭКСПОРТ ДЛЯ WinOLS ############"
python3 tools/a2lgen.py out/maps_stock.json \
    --a2l out/stock.a2l --csv out/stock.csv

echo
echo "############ 6. КОНТРОЛЬНЫЕ СУММЫ ############"
python3 tools/checksum.py "$STOCK" --align 0x1000 --max-results 40 \
    | tee out/checksum_stock.txt || true

echo
echo "Готово. Смотрите каталог out/"
